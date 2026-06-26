# Metrics, KPIs & a built-in BI layer

This document covers the metrics suite rvbbit gained in June 2026: versioned
SQL metrics, KPI checks, a bitemporal run model, a durable observation history,
rolling/relative-time baselines, and three desktop apps in the lens.

It is **built in, but opt-in**. There is no separate metrics store, no metric
DSL, no service to run. A metric is a row in a plain table and a `SELECT` that
reduces to one headline value at a data-time; a KPI is that plus one more
`SELECT` that returns a boolean. If you never define one, nothing changes. If
you do, you get systematic, versioned, time-travelable reporting over the
rvbbit tables you already have — **all your data, no lock-in.** Drop the
extension and you keep every metric definition and every recorded observation
as ordinary Postgres rows.

The things this layer can do that a bolt-on BI tool can't:

1. **Bitemporal reporting for free.** Every metric run is parameterized by two
   independent time axes — *def-time* (which version of the metric definition)
   and *data-time* (rvbbit AS OF over the underlying tables). "Today's
   definition over last quarter's data" and "last quarter's definition over
   today's data" are both one function call.
2. **KPIs whose *threshold* is versioned too.** A check is part of the metric
   definition, so you can ask "was this green under the threshold we *believed
   in* last quarter?" — an audit question most tools can't answer because they
   don't version the definition of "good."
3. **A durable, verdict-stamped history that survives data reaping** — written
   automatically when the underlying data changes (compaction is the trigger),
   not on a dumb clock.
4. **Rolling/delta metrics in one line** via relative-time references
   (`{metric:self.-1day}`), built directly on the snapshot-per-generation model.
5. **None of it is a new system.** Defs, versions, observations, and
   dependencies are all plain `rvbbit.*` tables you can `SELECT` from, join,
   back up, and reason about.

See [TIME_TRAVEL.md](TIME_TRAVEL.md) for the AS-OF mechanics this builds on and
[LAKEHOUSE.md](LAKEHOUSE.md) for the columnar/generation storage underneath.

---

## The core idea: two independent time axes

A classic metrics tool has three hard problems: (1) latency, (2) "as of now vs.
as of then," and (3) the metric *definition* shifting over time, which compounds
(2). rvbbit already solves (1) with its OLAP layer and (2) with time travel. The
metrics layer solves (3) by storing definitions as **plain, append-versioned
rows** with a `created_at` column — so *def-time* is a simple `created_at`
filter, fully decoupled from *data-time* (rvbbit AS OF). The two are orthogonal:

```
            data-time  ───────────────►
 def-time   ┌──────────────┬──────────────┐
   │        │ old def       │ old def       │
   ▼        │ old data      │ today's data  │
            ├──────────────┼──────────────┤
            │ today's def   │ today's def   │   ← every cell is one call
            │ old data      │ today's data  │
            └──────────────┴──────────────┘
```

```sql
-- def-time × data-time, independently:
SELECT * FROM rvbbit.metric('revenue', '{}'::jsonb,
    p_def_as_of  => '2025-01-01',   -- the metric as we defined it then
    p_data_as_of => now());         -- over the data as it is now
```

---

## Defining a metric

A metric is a name + a SQL template + optional default params, grain,
description, owner. `define_metric` appends a **new version** every time; the
definition is never mutated. For materialization and KPI boards, the definition
must be scalar: exactly one result row and one headline value. Return one
column, alias the headline as `value`, or set `labels.metric_value_column` when
the one-row result carries extra context columns.

```sql
SELECT rvbbit.define_metric(
  'revenue',
  $$SELECT sum(amount) AS value
    FROM orders
    WHERE amount >= {min}$$,
  '{"min": 0}'::jsonb,            -- default params
  'all',                          -- grain (descriptive)
  'Revenue',                      -- description
  'analytics');                   -- owner
-- → 1   (the new version number)
```

### Template tokens

| Token | Resolves to |
|---|---|
| `{param}` | a safe SQL literal (`quote_nullable` of the value) |
| `{param!}` | raw text — identifiers / SQL fragments (caller's responsibility) |
| `{metric:NAME}` | another metric inlined as a `(subquery)` — give it an alias |
| `{metric:self.-1day}` | a *rolling* reference — see [Relative-time references](#relative-time-references) |

Params are a flat namespace: a caller's params override the definition's
defaults, and a referenced metric's defaults flow in underneath.

### Composition

```sql
SELECT rvbbit.define_metric('high_value_revenue',
  $$SELECT sum(amount) AS value
    FROM orders
    WHERE amount > {floor}$$,
  '{"floor": 1000000}'::jsonb);
```

`{metric:NAME}` is still inlined as a subquery at def-time, so reusable rowsets
can exist for SQL composition. They should not be materialized as metrics. If
the thing you want to preserve is a rowset, make it a cube or table and define
scalar metrics over it.

---

## Running a metric

```sql
-- official metric value: one scalar payload
SELECT rvbbit.metric_scalar('revenue', '{"min": 50}'::jsonb);

-- lower-level rowset runner, useful for preview/composition/debugging
SELECT * FROM rvbbit.metric('revenue', '{"min": 50}'::jsonb);

-- see the exact composed SQL without running it (the observable surface)
SELECT rvbbit.metric_sql('revenue', '{"min": 50}'::jsonb);

-- preview an UNSAVED draft body (powers the Creator's live preview)
SELECT rvbbit.preview_metric_sql(
  'SELECT sum(amount) AS total FROM orders WHERE amount >= {min}',
  '{"min": 50}'::jsonb);

-- the catalog (latest version of each metric) and version history
SELECT * FROM rvbbit.metric_catalog;
SELECT * FROM rvbbit.metric_versions('revenue');
```

`metric_scalar()` is the product contract used by materialization. `metric()` is
the lower-level rowset runner underneath — it composes the body, pins the
data-time AS OF (via the `rvbbit.as_of_timestamp` GUC, which reaches nested
execution the leading-comment directive can't), and runs it. Everything flows
through the same router, vortex/duck/native engines, and time-travel machinery
as any other query.

---

## KPIs: a check is part of the definition

A metric becomes a **KPI** when its definition carries a `check_sql`. The check
runs against the metric's result, which is exposed to it as a CTE named
`metric`, and must reduce to **exactly one row** yielding an `ok` boolean (and,
optionally, `status` / `value` / `target` / anything else for display).
Thresholds are just `{param}` tokens — so they have *versioned defaults* and are
*overridable per call*.

```sql
SELECT rvbbit.define_metric(
  'daily_revenue',
  'SELECT sum(amount) AS total FROM orders',
  '{"target": 1000000}'::jsonb,
  'all', 'Revenue must clear target', 'analytics',
  '{}'::jsonb,
  -- the check (8th arg): one row, an `ok` boolean
  $$SELECT total >= {target} AS ok,
           total            AS value,
           {target}::numeric AS target
    FROM metric$$);

-- the verdict, across both temporal axes
SELECT rvbbit.check_metric('daily_revenue');
-- → {"ok": true, "value": 1250000, "target": 1000000, "status": "pass"}
```

### Thresholds as params (the sugar)

Because the threshold is a param, you can keep the audited default *and* ask
what-if questions without a new version:

```sql
-- "last quarter's data, last quarter's def, BUT sub in threshold X"
SELECT rvbbit.check_metric('daily_revenue',
  '{"target": 1500000}'::jsonb,         -- override, caller wins
  '2025-01-01',                         -- def-time
  '2025-01-01');                        -- data-time
```

### The bitemporal threshold

Because `check_sql` lives on the **versioned** definition row, moving a
threshold creates a new version — so the verdict is auditable across def-time:

```sql
-- v1: target 150 ;  v2: target 300 (a later, stricter version)
-- SAME data (total = 200):
SELECT rvbbit.check_metric('rev', '{}', def_as_of => v1_time);  -- pass (200 ≥ 150)
SELECT rvbbit.check_metric('rev', '{}', def_as_of => now());    -- fail (200 ≥ 300)
```

"What it *would* have been under a newer definition" stays a **live** query
(`check_metric` over a past `data_as_of`); the recorded history (below) keeps
what was *actually reported*. Neither rewrites the other.

A `NULL` `ok` is never treated as "pass" (a KPI over missing data does not read
as healthy). `metric_catalog` / `metric_versions` expose `check_sql`, so the UI
can tell metrics from KPIs.

---

## Relative-time references (rolling baselines)

`{metric:NAME.OFFSET}` / `{metric:self.OFFSET}` resolve to the target's **scalar
headline at a shifted data-time** (`base ± OFFSET`, definition held fixed). A
single statement can't carry two rvbbit AS-OFs, so a relative reference is
*eager-evaluated* at the shifted instant and spliced inline as a numeric
literal. Rolling / delta / week-over-week become one-liners:

```sql
-- rolling threshold check: "must not shrink vs the prior snapshot"
$$SELECT total >= {metric:self.-1day} AS ok, total AS value FROM metric$$

-- delta in a metric body
$$SELECT sum(amount) AS total, sum(amount) - {metric:self.-1day} AS delta FROM orders$$

-- cross-metric, week-over-week
$$SELECT total, total::numeric / {metric:revenue.-7days} - 1 AS wow FROM {metric:revenue} r$$
```

`OFFSET` is a signed amount + unit (`-1day`, `-12hours`, `-30seconds`,
`+1week`, `-1month`) or an alias (`yesterday`, `lastweek`, `lastmonth`). The
"headline" is a `value` field if present, else the first numeric result field —
so relative references target scalar metrics. The shift is on the **data-time**
axis only; the definition stays current.

---

## Materialization: a durable, verdict-stamped history

Live reads stay live — the past is reconstructable by re-running AS OF, because
the generations *are* the history. So rvbbit does **not** materialize just to
*have* a history. It materializes as a durable **log of what was reported** that
(a) outlives generation reaping and (b) records the KPI verdict *as decided*:

```sql
rvbbit.metric_observations
  metric_name, metric_version,
  def_as_of, data_as_of, data_generation,   -- the full bitemporal coordinates
  params, value (jsonb), verdict (jsonb), status,
  observed_at, trigger                       -- compaction | cron | manual | backfill
```

### Compaction is the trigger

A metric's value only changes when its underlying data changes — which in rvbbit
is exactly a new generation. So the default cadence isn't a clock: a new
generation **enqueues** itself (if a metric depends on the table), and
`materialize_tick()` (a `pg_cron` heartbeat) drains the queue, materializing each
dependent metric at `def_as_of` = the generation's commit time — so the verdict
is captured *as it was believed then*. One observation per `(metric, generation)`
— a clean, deduplicated history aligned to the data's own heartbeat. With the
Temporal Mirror, each sync run becomes one observation, automatically.

```sql
-- read the durable series
SELECT data_generation, value->>'value' AS total, status, trigger
FROM rvbbit.metric_history('daily_revenue');

-- chart the durable scalar observation series
SELECT bucket, value, status, metric_version, trigger
FROM rvbbit.metric_series('daily_revenue',
    now() - interval '90 days', now(), 'week', '{}'::jsonb)
ORDER BY bucket;

-- one JSON object with latest definition, latest observation, and source freshness
SELECT rvbbit.metric_provenance('daily_revenue');

-- snapshot on demand (e.g. backfill an older generation with the current def)
SELECT rvbbit.materialize_metric('daily_revenue', '{}'::jsonb, now(),
    p_data_as_of => '2025-01-01', p_trigger => 'backfill');

-- policy: every metric defaults to compaction-materialized; toggle / add a cron
SELECT rvbbit.set_materialize('daily_revenue',
    p_on_compaction => true, p_cron_schedule => NULL);

-- register the drain as a pg_cron heartbeat (or call materialize_tick() yourself)
SELECT rvbbit.schedule_materialize_tick('* * * * *');
```

Dependencies (which metric reads which table) are auto-derived from each
definition via `route_explain` and cached in `rvbbit.metric_dependencies`;
`define_metric` refreshes them. Observations are **immutable** — they are the
record of what you reported, not a cache to be rewritten.

---

## The apps (rvbbit-lens)

A **Metrics** folder on the desktop with three apps, none of which you need:

- **Catalog** — a sortable, searchable table of every metric, flagging which are
  KPIs; click to inspect, edit to author.
- **Creator** — author/version a metric (name, SQL, params, grain, check) with a
  **live resolved-SQL preview** (`preview_metric_sql` / `preview_check_sql`) and
  a live verdict badge. Save appends a version.
- **Inspector** — the showcase: run a metric across **both** temporal axes (a
  def-time version picker + a data-time snapshot picker), see the live resolved
  SQL, a results grid, the pass/fail verdict flipping as you move the axes, and a
  **Trend** tab — the materialized series as a verdict-colored bar strip plus a
  "Materialize now" button.
- **KPI Board** — a matrix over the durable observation log. Cells drill into
  the exact value, definition version, trigger, source freshness, and reproducible
  SQL. Sparklines open a normal SQL block backed by `metric_series(...)` with a
  line chart already configured.

---

## API reference

**Definition & run**

| Function | Returns | |
|---|---|---|
| `define_metric(name, sql, params jsonb, grain, description, owner, labels jsonb, check text)` | `int` | append a version (deps + materialize policy auto-set) |
| `metric_scalar(name, params jsonb, def_as_of timestamptz, data_as_of timestamptz)` | `jsonb` | official scalar value across the two axes |
| `metric(name, params jsonb, def_as_of timestamptz, data_as_of timestamptz)` | `SETOF jsonb` | lower-level rowset runner for preview/composition/debugging |
| `metric_sql(name, params jsonb, def_as_of timestamptz)` | `text` | the composed SQL (no run) |
| `preview_metric_sql(draft_sql, params jsonb, def_as_of timestamptz)` | `text` | compose an unsaved draft |
| `metric_versions(name)` | `TABLE` | version history |

**KPI checks**

| Function | Returns | |
|---|---|---|
| `check_metric(name, params jsonb, def_as_of, data_as_of)` | `jsonb` | the verdict (`NULL` if not a KPI) |
| `preview_check_sql(metric_sql, check_sql, params jsonb, def_as_of, data_as_of)` | `jsonb` | verdict for a draft |

**Materialization**

| Function | Returns | |
|---|---|---|
| `materialize_metric(name, params, def_as_of, data_as_of, data_generation, trigger)` | `bigint` | append one observation |
| `metric_history(name, limit)` | `TABLE` | the durable series |
| `metric_series(name, from, to, bucket, params, stale_after)` | `TABLE` | bucketed numeric observation series for SQL blocks/charts |
| `metric_provenance(name, stale_after)` | `jsonb` | latest definition, latest observation, and dependency freshness |
| `metric_scalar_audit(metrics, def_as_of, data_as_of)` | `TABLE` | find metric definitions that are still not scalar-safe |
| `metric_dependency_freshness(metrics, stale_after)` | `TABLE` | check whether metric source tables have stopped receiving fresh source data |
| `scalarize_latest_metric(name, time_col, value_col)` | `int` | append a scalar version of a period-series metric |
| `scalarize_metric_observations_latest(name, time_col, value_col)` | `int` | rewrite legacy rowset observations to latest-period scalar payloads |
| `scalarize_metric_observations_count(name)` | `int` | rewrite legacy rowset observations to row-count scalar payloads |
| `scalarize_metric_observations_one_row(metrics)` | `int` | rewrite legacy single-row observation arrays to scalar payloads |
| `set_materialize(name, on_compaction, cron_schedule, enabled)` | `void` | per-metric policy |
| `materialize_tick(max)` | `int` | drain the compaction queue (pg_cron) |
| `schedule_materialize_tick(cron, budget)` | `bigint` | register the heartbeat |
| `refresh_metric_dependencies(name)` | `int` | re-derive table deps |

**Tables & views** (all plain, `SELECT`-able)

| Object | |
|---|---|
| `rvbbit.metric_defs` | append-versioned definitions (the source of truth) |
| `rvbbit.metric_catalog` (view) | latest version per metric |
| `rvbbit.metric_observations` | the durable, immutable observation log |
| `rvbbit.metric_materialize` | per-metric materialization policy |
| `rvbbit.metric_dependencies` | derived metric → table dependencies |

**GUCs:** `rvbbit.as_of_timestamp`, `rvbbit.as_of_generation` (the data-time
axis; see [TIME_TRAVEL.md](TIME_TRAVEL.md)).

Shipped across extension versions 1.2.8 → 1.2.13. Internal helpers are prefixed
`_` (e.g. `_run_check`, `_resolve_relative_refs`) and are not part of the stable
surface.
