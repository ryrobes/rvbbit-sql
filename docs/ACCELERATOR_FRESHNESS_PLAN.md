# Accelerator Freshness — a managed, observable, value-driven control plane

> Status: Layers 1–3 + UI cockpit SHIPPED & verified (2026-06-05) — uncommitted on `main`.
> Heartbeat = pg_cron calling `rvbbit.accel_tick()`. 9 pg_tests + full live E2E green.
> Remaining/deferred: the north-star gradient+union (below); per-table route-attribution
> for realized native-vs-accel speedup; richer cockpit "projected staleness curve" preview.
> Companion to `SHAPE_MATERIALIZATION_PLAN.md`. The north star (gradient dirtiness +
> heap-tail union) is parked at the end as a roadmap item, not part of this build.

## The reframe (why this isn't "a button vs a cron job")

The accelerator (Parquet / Vortex / Lance row groups behind the backend query router)
is a **derived, materialized** mirror of an rvbbit table's heap. Writes make it stale.
The user's own observation reorders the whole problem:

> *"if the accelerator can't keep up then none of your SELECTs will use it since it will
> always be dirty."*

Grounded in code, that is exactly right and it is **safe**:

- `planner.rs::parquet_authoritative_for_oid()` gates acceleration on
  `pg_relation_size==0 OR (shadow_heap_retained AND NOT shadow_heap_dirty)`. If a table
  is dirty, the planner **does not even add the custom-scan path** — Postgres seq-scans
  the heap.
- `duck_backend_fail_open` (default on) demotes any acceleration error to a native scan.

So **stale acceleration is never executed.** Freshness is a *performance/cost*
optimization, **never** a correctness one. That liberates the design: be lazy about
low-value tables (eat the native scan), aggressive about hot ones — never risking a
wrong answer. The question becomes the **materialized-view / LSM-compaction** question:
*which tables are worth the rebuild cost given how fast they change and how they're
queried?* That is a **value-vs-cost policy**, which is what a Bret-Victor control plane
can make legible.

### The real enemy is the binary dirty bit

`rvbbit.tables.shadow_heap_dirty` is a single **per-table boolean**, flipped by a
statement-level `AFTER` trigger (`mark_shadow_heap_dirty()`) on *any* DML, and only
cleared by a refresh/rebuild. So **one INSERT turns acceleration off for the whole
table** until the whole table is caught up. There is no "92% of row groups are still
fresh, only the tail moved." That all-or-nothing cliff — not the scheduling — is what
makes the write-heavy case feel scary. Layers 1–3 manage *around* the bit; the north
star *replaces* it.

## What already exists (do not rebuild)

The map of `compact.rs` / `catalog.rs` / `router.rs` found the substrate is largely
built — the controller loop is just open:

- **Auto-delta already works.** `rvbbit.refresh_acceleration(rel, refresh_variants)`
  reads `acceleration_state.last_refresh_xid`, computes a safe upper xid from
  `pg_snapshot_xmin`, calls `export_to_parquet_xid_range(rel, lo, hi)` (writes **only new
  row groups** for new rows), delta-refreshes the layout variants, clears the dirty bit,
  re-installs the trigger, and advances the watermark. Returns
  `{status: ok|noop, rows_written, row_groups_written, ...}`. **This is the delta
  primitive** — the executor orchestrates it, it does not reimplement it.
  (Edge case: in the bootstrap branch `last_xid=0 AND existing_rgs>0 AND dirty` it
  *raises* and tells you to rebuild first → executor catches and escalates.)
- **Full rebuild** = `rvbbit.rebuild_acceleration(rel, refresh_variants)` — wipes derived
  state, re-exports from the heap. The escalation path.
- **Freshness state** already tracked: `acceleration_state` (last_refresh_xid /
  _generation / _rows / _row_groups / _at), `acceleration_status` view (the authoritative
  computation), `acceleration_operations` (+`_phases`) — rebuild cost & history,
  `delete_log` — tombstones, `row_groups` — parquet rows/bytes/generation.
- **Lance caveat:** `refresh_lance_dataset()` is **always `WriteMode::Overwrite`** — Lance
  -accelerated tables (`tables.lance_url IS NOT NULL`) are the genuinely *expensive*
  refreshes. Flag them and budget them separately.

### Gaps this build fills

- No `dirty_since` / `last_write_at` → can't age staleness.
- No **fused rollup** joining supply-side freshness with demand. (Route telemetry —
  `route_executions` — keys by `query_hash`/`shape_family`, **not `table_oid`**, so it
  can't be joined per-table. But `pg_stat_get_numscans(oid)` gives real per-table demand,
  and heap `seq_scan`s on an accelerated table *are* the "eligible-but-unused" slow-path
  signal. Realized native-vs-accel A/B speedup is deferred — needs a small future
  `table_oid` stamp into route logging.)
- No **policy** expressing the user's cost/latency intent.
- No **executor** that turns policy + freshness + budget into the right refresh action.

## The design — three layers

Reframe the unit of control: a policy is a **freshness target + a budget**, not a
schedule. Declare intent ("keep within ~5 min stale" / "best-effort under N refreshes a
day"); the engine owns the *when/how*.

### Layer 1 — make freshness legible (`freshness.rs`)

- `rvbbit.tables.dirty_since timestamptz`, `last_write_at timestamptz`, set by the
  existing `mark_shadow_heap_dirty()` trigger (dirty_since stamped only on the
  clean→dirty transition; the view NULLs it when clean so clear-sites need no edits).
- **`rvbbit.accel_freshness`** view — one row per accelerated table, all cheap
  (pg_stat + catalog, no heap scans):
  `shadow_heap_dirty`, `parquet_authoritative`, `dirty_since`, `seconds_dirty`,
  `last_write_at`, `last_refresh_at`, `seconds_since_refresh`, `last_refresh_xid`,
  `parquet_rows`, `row_groups`, `heap_live_tuples` (`pg_stat_get_live_tuples`),
  `est_unmirrored_rows = greatest(0, heap_live_tuples - parquet_rows)`,
  `tombstones` (delete_log count), `drift_rows`, `drift_ratio`,
  `heap_seq_scans` (`pg_stat_get_numscans` — slow-path demand),
  `last_rebuild_ms` / `last_rebuild_rows` (latest ok `acceleration_operations`),
  `lance_accelerated`.
- Auto-delta primitive already exists (`refresh_acceleration`); no new fn needed.

### Layer 2 — per-table policy (`rvbbit.accel_policy`)

Declarative, **default-absent = `manual`** so nothing changes until a table opts in.
`strategy ∈ {manual, scheduled, target, demand, continuous}`, plus guards:
`freshness_target_secs`, `min_interval_secs`, `daily_refresh_budget`,
`full_rebuild_drift_ratio` (LSM major-compaction trigger), `lance_separate`, `active`.
Headline strategy is `target` (a freshness SLO); the rest are escape hatches.
Helpers: `rvbbit.set_accel_policy(...)`; view `rvbbit.accel_policy_effective`
(left-joins policy onto accelerated tables, defaulting missing → manual).

### Layer 3 — the executor (`rvbbit.accel_tick`), pg_cron is the heartbeat not the brain

`rvbbit.accel_tick(budget int)` — called on a heartbeat by **one** pg_cron job. Per
dirty, in-budget table, ordered by value (drift × staleness × demand):
decide **skip / delta / full** per policy and `full_rebuild_drift_ratio`; **prefer
auto-delta** (`refresh_acceleration`), escalate to **full** (`rebuild_acceleration`) on
drift or when delta raises; respect `min_interval_secs`, the per-tick budget, and a
serialized lock so two ticks never collide. Lance-accelerated tables get a stricter
sub-budget (always full-overwrite). Logs to `acceleration_operations` (existing) +
`rvbbit.accel_tick_runs` (new, per-tick summary). Returns `SETOF` per-table actions.
The difference from a dumb cron: it rebuilds **only dirty, high-value, in-budget**
tables — the *control* is the policy+budget, not the clock.

**Demand-driven complement:** ordering by `heap_seq_scans` already approximates
warm-on-miss (tables people actually hit, currently on the slow path, get refreshed
first) without a new execution hook.

## UI — grow Adaptive Routing into the freshness cockpit

`routing-window.tsx` already shows engine choice + p50/p95 + cache rates. Add a
**per-table freshness lane**: fresh/dirty/building chip, lag/drift, demand
(`heap_seq_scans`), last rebuild cost, current policy — with manual **delta / full**
buttons and a **recommended-policy nudge** from `accel_freshness` (hot + cheap-delta →
suggest `target: 5min`; cold + unqueried → `manual`). Show the **projected
consequence** (staleness curve, projected refresh count) before committing a policy. The
heartbeat lives in the scheduler tray (an `accel_tick` cron preset alongside the catalog
-crawl preset); the cockpit is where you *see and steer* it.

## North star (roadmap, not this build) — kill the binary bit

Today the heap *is* an LSM memtable and the parquet row groups *are* the sorted runs —
they're just never read together. If the planner gate became a **lag threshold** instead
of a boolean, and the custom scan could serve
`parquet(generation ≤ watermark) ∪ heap(xmin > watermark)`, then acceleration is **never
fully off** — bulk from parquet, fresh tail from the heap; "refresh" becomes "flush the
tail"; and "always dirty" degrades *proportionally* instead of off a cliff. That's a
planner + custom-scan change (generation-aware union read). Layers 1–3 are designed not
to bake in the binary assumption so this can land later.

## Build order (tests at each stage)

0. **This doc.**
1. **Layer 1** — columns + trigger + `accel_freshness` view. pg_tests: dirty_since
   stamping/clearing, view shape, drift math.
2. **Layer 2** — `accel_policy` + `set_accel_policy` + `accel_policy_effective`.
   pg_tests: default-manual, upsert, effective view.
3. **Layer 3** — `accel_tick` + `accel_tick_runs` + pg_cron preset. pg_tests: dirty→delta
   restores freshness, drift→full, budget/min-interval honored, Lance sub-budget.
4. **UI** — freshness lane + manual buttons + nudge + scheduler preset.
5. **E2E (live, with writes)** — accelerate → write (go dirty) → set policy → tick
   (auto-delta restores) → rollup + routing reflect it → cockpit live. Confirm
   correctness-safe fallback during the dirty window.
