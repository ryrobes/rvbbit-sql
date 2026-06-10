# Alerts — reactive condition → operator automation

## The idea

A durable, async, deduped, audited **"SQL/semantic condition → operator action"**
engine. KPI thresholds are use-case #1, but the same primitive covers Drift
events, accelerator-freshness breaches, operator error-rate spikes, and
arbitrary SQL/semantic predicates. It's *Postgres triggers, but async, stateful,
rate-bounded, audited, and able to safely reach external services* — which
native triggers fundamentally can't do.

The structural insight that makes it cheap: **both ends are already operators.**
- The **action** is an operator/`mcp_call`/`flow()` — composed-or-single is one
  calling shape, with receipts capturing I/O for free.
- The **condition** is *also* just "a query that returns `(entity_key, score/status)`" —
  nothing requires it to be deterministic SQL. A z-score over the observation
  log, an embedding-distance novelty check, existing Drift/PSI, or an LLM judge
  in a flow are the *same shape* as `WHERE value > 100`.

So the whole system is **operators in (condition) → a stateful reconciler →
operators out (action)**. Almost everything is reused; the genuinely new part is
the reconciler discipline (edge-triggering, dedup, rate bounds, the kill-switch).

## Decisions locked (from design discussion)

- **Periodic reconciler, not event-driven.** A pg_cron sweep reads current state
  of every rule, diffs against stored state, enqueues transitions. Level-triggered-
  with-memory ⇒ missed ticks / mid-sweep crashes are harmless (next sweep re-derives
  from current reality). Decouples the *alert clock* from the *data/compaction clock*.
- **Sweeper enqueues; a separate worker acts.** Never make the external call inline
  in the tick (don't block the heartbeat, don't burst a synchronized herd). Sweep
  diffs + enqueues fast; a paced worker drains `alert_queue`. ⇒ two-stage kill-switch
  (pause sweep = stop deciding; pause worker = keep deciding+logging, stop acting =
  a free live dry-run).
- **Fire-and-forget for v1.** No external-artifact lifecycle (no auto-close on
  recovery). Close-the-loop (open ticket on breach, close on `fail→pass`) is a
  later phase that only needs to store the action's output artifact id.
- **Edge-triggered with state — required even for fire-and-forget.** Verdicts stay
  `fail` across ticks, so fire only on `pass→fail` transitions, never on the level.
  `alert_state(rule, entity)` holds `last_status` + a consecutive counter.
- **Pause semantics = suppress.** On resume, state advances; we fire only on what's
  *still* breaching, logging the suppressed transitions. (A breach that opens *and*
  closes entirely inside a pause window is dropped — acceptable for internal/low-stakes.)
- **Hysteresis / "failed the last N" is free** because conditions read the
  materialized observation timeseries — `consecutive_n`, `% change vs {metric:NAME.-1day}`,
  `sustained-for`, week-long horizons are all just windowed queries over the log.
  This is *also the primary de-noiser for fuzzy/semantic conditions* (an LLM "yes 3
  sweeps running" is stable; a single "yes" is noise).
- **Score, not vibe.** Prefer conditions that emit a *number* (anomaly score,
  classification confidence, distance) thresholded like any metric, so a semantic
  condition rides the *exact same* threshold+hysteresis path as a numeric KPI — no
  special case. LLM judges return a typed `bool + confidence`, not string-matched text.
- **Cheap filter → expensive confirm.** Gate a semantic condition behind a crude SQL
  prefilter so the model only judges the already-suspicious. (That two-stage condition
  is itself a little flow.)
- **Bounds are per-tick on frequency, NOT cardinality.** A dimensioned rule can fan
  out to N transitions in one sweep ⇒ a per-rule + global **fan-out cap** with overflow
  → aggregate ("N entities breached") or truncate-and-log.
- **Tiered cadence, not one clock.** A small fixed set of sweeps (`fast`/`normal`/`slow`),
  each its own pausable cron job — keeps "synced within a tier, bounded per tier,
  independently pausable" without forcing everything to the slowest clock.
- **Grain is per-rule, derived from the condition query.** Scalar KPI = one entity;
  dimensioned = one entity per group. `alert_state` is keyed by `(rule_id, entity_key)`.
- **SQL-first, UI-builds-SQL.** Rules are pseudo-DDL like `define_metric` (versioned,
  diffable, queryable, self-referential). The UI is a typed form generator that emits
  and runs that SQL — its sweet spot is the action-arg binding (schema-driven form from
  the MCP operator signatures already shipped in capability manifests).

## Schema (rvbbit.alert_*)

- **`alert_rules`** — `id, name, enabled, muted, cadence_tier,
  condition_spec jsonb` (kind: `sql` | `operator` | `flow`; the query/op + how to
  read entity_key + score/status + threshold), `fire_policy jsonb`
  (`consecutive_n`, `cooldown`, edge = `enter_fail` for v1), `cardinality`
  (`per_entity` | `aggregate`), `fan_out_cap`, `action_spec jsonb` (operator +
  args template/SELECT), `created_at, updated_at`. Versioned (created_at axis,
  like `metric_defs`).
- **`alert_state`** — `(rule_id, entity_key) → last_status, score, consecutive,
  last_changed_at, last_fired_at`. The reconciler's memory.
- **`alert_queue`** — `(rule_id, entity_key, transition, rendered_args jsonb,
  enqueued_at, attempts, status)`. Sweep writes; worker drains.
- **`alert_events`** — firing log: `(rule_id, entity_key, transition,
  action_receipt_id, action_output jsonb, external_artifact, status, ts)`.
- **`alert_sweep_runs`** — heartbeat: `(tier, started, finished, rules_evaluated,
  transitions, enqueued, errors)`. Makes the sweeper itself observable.
- Global `alerts_enabled` setting (kill-switch the sweep+worker check).

**Migration gotcha:** these are new *tables* in the extension — they must go
through `extension_sql!` + the proper `pg_rvbbit--A--B.sql` migration chain (the
publish gate `check-migration-chain.py` enforces a contiguous chain). Do NOT
raw-`psql`-apply new-table migrations (orphan non-members break `ALTER EXTENSION UPDATE`).

## Phases (atomic, each testable before the UI)

Each phase ships independently and has a pg_test. **Phases 1–2 need zero external
dependencies** — the riskiest logic (the reconciler state machine) is the most
deterministically testable, which is ideal for a build loop.

- **P0 — Schema + rule DDL.** Tables + `define_alert(...)` / `resolve_alert` /
  `enable`/`disable`/`mute` / `set_alert_cadence`, versioned. *Test:* define a rule,
  read it back, versioning + mute flags work.
- **P1 — Reconciler (`alert_sweep(tier)`), the heart.** Per enabled rule: run the
  condition query → `(entity_key, status/score)`; diff vs `alert_state`; apply
  `fire_policy` (consecutive_n, cooldown, suppress-on-resume); enqueue transitions
  (respect `fan_out_cap` → aggregate/truncate); update `alert_state` **atomically per
  rule** (per-rule txn so a crash can't enqueue-without-advancing → double-fire);
  write a `sweep_runs` heartbeat. Enqueues only — inherently dry-runnable. *Test
  (no external deps):* seed `metric_observations` with a synthetic timeseries →
  assert correct transitions enqueued; re-run → assert no re-fire (level held);
  pass→fail→pass→fail → assert re-arm; consecutive-N; fan-out-cap.
- **P2 — Worker (`alert_worker_tick`).** Drain `alert_queue` at a paced rate
  (max/tick, concurrency cap, global rate cap, per-target circuit breaker); call the
  action operator; capture receipt + output → `alert_events`; v1 = no retry
  (fire-and-forget), mark failed on error. *Test:* a no-op/echo internal action →
  events written, queue drained, rate cap respected. No real MCP needed.
- **P3 — Action arg binding.** Render action args from alert context (rule, entity,
  value, threshold, breaching rows) via existing `{param}` templating + the entity
  row → `jsonb`; **validate against the tool's declared input schema** (capability
  manifest) before enqueue. *Test:* render for a known rule+context → matches
  expected; schema validation rejects a malformed body.
- **P4 — pg_cron wiring + tiers + kill-switch.** Cron jobs per tier calling
  `alert_sweep(tier)` + a worker job; home = `'postgres'` (consistent with the
  existing tick model); `alerts_enabled` global gate; idempotent registration.
  *Test:* registration idempotent; global flag gates both sweep and worker.
- **P5 — Semantic conditions (force multiplier).** Condition kind `operator`/`flow`
  returning `(entity, score)`, thresholded identically to numeric; two-stage SQL
  prefilter → semantic confirm; scored output preferred, typed bool for judges.
  Mostly rides P1 (it's "a query returning entity+score") + an example + prefilter
  wiring. *Test:* a scored condition (z-score SQL or a stub op) fires through the
  same path.
- **P6 — UI: a new dedicated "Alerts" app (last).** Condition-source-agnostic;
  references metric defs. Rules CRUD (SQL-builder form + schema-driven action-arg
  form), live state (armed/breaching now), event log (firing history), dry-run /
  test-fire, kill-switch + mute/snooze. Slots next to the Metrics folder.

## Later (explicitly deferred)

- **Close-the-loop** — store the action's output artifact id; `fail→pass` fires a
  resolve action (close/comment the same ticket). Bidirectional alerting.
- **Self-healing / remediation** — action = an agent that diagnoses + fixes, with
  the *verify* step re-checking the firing KPI. This reintroduces the feedback loop
  deliberately; the loop-guard (an action must not blindly mutate monitored data and
  re-trigger) is what makes it safe.
- **Distill the judge** — every firing + its human outcome is a labeled row ⇒
  `train_model` distills an expensive LLM condition into a cheap specialist.
- **Generalized condition sources** — Drift, accel-freshness, operator error-rate as
  first-class condition kinds.
