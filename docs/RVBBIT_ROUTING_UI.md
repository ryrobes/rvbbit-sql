# Rvbbit Adaptive Routing Observability

This document is the UI/data contract for building an adaptive query routing
dashboard. It describes the stable catalog surfaces a UI should read. Do not
scrape logs or infer routing state from rewritten SQL.

For the SQL-native training/profile curation UI contract, including adding and
removing saved training queries, candidate benchmarking, validation status, and
profile rebuild actions, see `docs/RVBBIT_ROUTE_TRAINING_UI.md`.

## Concepts

- **Candidate**: the execution engine selected or measured for a query shape.
  Current values are `rvbbit_native`, `duck_vector`, `duck_hive`,
  `datafusion_vector`, `datafusion_hive`, and `pg_rowstore`.
- **Route decision**: what the normal backend router chose at rewrite time.
  This is cheap telemetry and does not include runtime.
- **Route execution**: runtime for a routed query after executor completion.
  This is the primary online workload signal.
- **Observation**: explicit benchmark/training timing for a candidate. These
  rows compare possible candidates and are used by `rvbbit.route_train(...)`.
- **SQL training run**: table-backed profiling started by
  `rvbbit.route_train_query(...)`. These runs preserve the source query,
  candidate timings, validation status, errors, and generated profile updates.
  `duck_hive`/`datafusion_hive` require Hive parquet variants, and can be
  disabled with `RVBBIT_ROUTE_HIVE=0`. `pg_rowstore` is routable when the
  referenced Rvbbit tables have retained shadow heaps; the router applies a
  higher confidence bar before selecting it.
- **Effective profile**: the route profile used for a decision. By default this
  is the active profile. Advanced users can select a non-default profile with
  `SET LOCAL rvbbit.route_profile = 'profile-name'` or
  `SELECT rvbbit.route_use_profile('profile-name')`.
- **Shape key**: normalized query shape including feature buckets. Use it for
  exact grouping.
- **Shape family**: shape key with scale-sensitive row-count buckets removed.
  Use it for charts across data sizes.

## Control Functions

```sql
SELECT rvbbit.route_decision_log_status();
```

Returns JSON:

- `enabled`: route telemetry is enabled for this backend.
- `started`: background writer has been started in this backend.
- `scope`: currently `backend`; these counters are process-local, not global.
- `backend_pid`: PID for the backend reporting these counters.
- `queue_len`: queued telemetry events not yet flushed.
- `queue_capacity`: bounded queue size.
- `enqueued`: total route telemetry events accepted by this backend.
- `dropped`: events dropped because the queue was full or writes failed.
- `written`: total events written.
- `decision_written`: route decision rows written.
- `execution_written`: route execution rows written.
- `write_errors`: failed batch insert attempts.
- `connect_errors`: failed background-writer database connections.

```sql
SELECT rvbbit.route_cache_reset();
```

Clears only the current backend route cache. Useful for cold/warm tests.

```sql
SELECT rvbbit.route_current_profile();
SELECT rvbbit.route_profiles();
SELECT rvbbit.route_status();
SELECT rvbbit.route_use_profile('dashboard-fast-path', local => true);
SELECT rvbbit.route_clear_profile(local => true);
```

Profile controls:

- `route_current_profile()`: returns the requested profile, effective profile,
  profile source, optional warning, and profile update epoch.
- `route_profiles()`: returns profile rows as JSON, including active state,
  entry/point counts, candidate mix, average confidence, and provenance fields.
- `route_status()`: returns the current profile, profile list, candidate gates,
  runtime fail-open settings, and route catalog row counts. This is the best
  one-call health check for a UI header.
- `route_use_profile(...)`: sets `rvbbit.route_profile` after validating the
  profile exists. With `local => true`, scope is transaction-local.
- `route_clear_profile(...)`: clears the explicit profile override and returns
  to the active default profile.

Fresh installs with no active profile use deterministic cold-start rules:
`profile_source='none'`, `profile_name=NULL`, native rewrites and row-returning
queries stay on `rvbbit_native`, and analytical parquet shapes route to
`datafusion_vector` when the parquet catalog is authoritative. Treat
`guc-missing` as an operator warning; it means a requested profile name does
not exist and the router did not silently fall back to the active profile.

## Route Decision Log

Table: `rvbbit.route_decisions`

One row per routed decision made by the normal backend rewriter. Writes are
best-effort and asynchronous.

Important columns:

- `decided_at`: decision timestamp.
- `backend_pid`: PID of the backend that made the decision.
- `database_name`, `role_name`: database/user context.
- `query_hash`: stable hash of normalized SQL.
- `shape_key`, `shape_family`: grouping keys.
- `profile_name`: effective profile used for this route, nullable when no
  profile was available.
- `profile_source`: `active`, `guc`, `guc-missing`, `none`, or
  `catalog-missing`.
- `route`: high-level route name.
- `candidate`: selected engine, nullable only for rejected/unknown cases.
- `route_source`: why this route path was selected, for example
  `profile-entry-fast`, `profile-entry`, `hard-rule-fast`, `eligibility-fast`.
- `reason`: human-readable route reason.
- `confidence`: profile confidence, nullable for hard rules.
- `cache_hit`: whether the route decision came from the backend route cache.
- `rewritten`: whether SQL was rewritten to a vector sidecar call.
- `features`: JSONB feature object used by the router.
- `route_doc`: JSONB full route document.

Summary view: `rvbbit.route_decision_summary`

Good for route mix cards and cache-hit counts:

```sql
SELECT
  candidate,
  profile_name,
  profile_source,
  route_source,
  sum(decisions) AS decisions,
  sum(cache_hits) AS cache_hits,
  sum(rewritten_count) AS rewritten
FROM rvbbit.route_decision_summary
GROUP BY candidate, profile_name, profile_source, route_source
ORDER BY decisions DESC;
```

## Route Execution Log

Table: `rvbbit.route_executions`

One row per successfully completed routed query execution. Writes are
best-effort and asynchronous. This is online workload telemetry, not forced
candidate comparison.

Important columns:

- `executed_at`: completion timestamp.
- `backend_pid`, `database_name`, `role_name`: execution context.
- `query_hash`, `shape_key`, `shape_family`: same semantics as decisions.
- `profile_name`, `profile_source`: effective route profile and how it was
  selected.
- `route`, `candidate`, `route_source`, `reason`, `confidence`: chosen route.
- `cache_hit`, `rewritten`: copied from the decision.
- `elapsed_ms`: executor elapsed time from `ExecutorStart` to `ExecutorEnd`.
- `rows_returned`: `es_processed` at executor end.
- `status`: currently `ok` for completed executions.
- `features`, `route_doc`: copied from decision.

Summary view: `rvbbit.route_runtime_summary`

Good for latency dashboards:

```sql
SELECT
  candidate,
  profile_name,
  profile_source,
  route_source,
  sum(executions) AS executions,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY median_ms) AS median_of_shapes_ms,
  max(p95_ms) AS worst_shape_p95_ms,
  sum(cache_hits) AS cache_hits
FROM rvbbit.route_runtime_summary
GROUP BY candidate, profile_name, profile_source, route_source
ORDER BY executions DESC;
```

Slow shapes:

```sql
SELECT
  shape_family,
  profile_name,
  candidate,
  route_source,
  executions,
  median_ms,
  p95_ms,
  last_reason
FROM rvbbit.route_runtime_summary
ORDER BY p95_ms DESC
LIMIT 20;
```

Recent executions:

```sql
SELECT
  executed_at,
  profile_name,
  candidate,
  route_source,
  elapsed_ms,
  rows_returned,
  cache_hit,
  left(reason, 120) AS reason
FROM rvbbit.route_executions
ORDER BY executed_at DESC
LIMIT 100;
```

## Training Observations

Table: `rvbbit.route_observations`

Rows here are explicit measured candidate timings, usually imported from
forced benchmark runs or generated by training/exploration jobs. Unlike
`route_executions`, observations can compare several candidates for the same
shape.

Summary view: `rvbbit.route_observation_summary`

Profile view: `rvbbit.route_profile_summary`

SQL-native training tables:

For the full UI contract and action recipes, see
`docs/RVBBIT_ROUTE_TRAINING_UI.md`.

- `rvbbit.route_training_queries`: one row per named-profile training query,
  including SQL text, feature JSON, shape key, label, enabled flag, and owner.
- `rvbbit.route_training_runs`: one row per invocation of
  `route_train_query(...)`, with repeats, candidate list, settings, status, and
  JSON summary.
- `rvbbit.route_training_results`: one row per candidate repeat, including
  elapsed time, rows returned, result digest, validation status, error, and
  route doc.
- `rvbbit.route_training_summary`: UI-friendly candidate medians and latest
  status by training query.

UI actions can call:

```sql
SELECT rvbbit.route_train_query(
  profile_name => 'dashboard-fast-path',
  query => $SQL$SELECT ...$SQL$,
  repeats => 3,
  min_gain_pct => 0.05,
  activate => true,
  candidates => 'all',
  label => 'dashboard panel name'
);

SELECT rvbbit.route_training_delete_query('dashboard-fast-path', 123, rebuild => true);
SELECT rvbbit.route_profile_rebuild('dashboard-fast-path', 0.05, true);
```

Training profile JSON and `route_profile_entries.entry` include
`candidate_medians`; when Hive or heap is measured, look for `duck_hive`,
`datafusion_hive`, `pg_rowstore`, the matching `*_ms_median` fields, and
`oracle_choice`. Generated profiles may select higher-cost candidates only when
they clear their confidence thresholds.

Useful comparison:

```sql
SELECT
  rs.shape_family,
  rs.best_candidate AS trained_best,
  rt.candidate AS online_candidate,
  rt.executions,
  rt.median_ms AS online_median_ms,
  rs.best_median_ms AS trained_median_ms
FROM rvbbit.route_shape_summary rs
JOIN rvbbit.route_runtime_summary rt
  ON rt.shape_family = rs.shape_family
ORDER BY rt.executions DESC
LIMIT 50;
```

## Suggested Dashboard Panels

1. Route mix over time: stacked area by `candidate` from
   `rvbbit.route_executions`.
2. Cache health: cache-hit ratio from `route_decision_summary` and the current
   backend queue status from `route_decision_log_status()`.
3. Latency by candidate: p50/p95 from `route_runtime_summary`.
4. Slowest shape families: `route_runtime_summary ORDER BY p95_ms DESC`.
5. Router source mix: `route_source` counts to show profile vs hard-rule vs
   eligibility decisions.
6. Profile mix: route decisions/executions by `profile_name` and
   `profile_source`, highlighting explicit overrides and missing-profile cases.
7. Install health: render `route_status()` with active profile, enabled
   candidates, fail-open state, and catalog counts.
8. Online vs trained mismatch: join online runtime summaries to trained profile
   summaries by `shape_family`.
9. Telemetry health: dropped/write/connect error counters from the status JSON
   for the backend serving the UI request. Use table counts for global history.

## Notes And Limits

- Telemetry is best-effort. Dropped rows must not be treated as query failures.
- Execution rows currently represent completed executions. Error-path capture is
  a future hardening step.
- Online route executions do not prove the selected route was globally optimal;
  they only measure the route actually used. Forced candidate runs or controlled
  exploration are required to compute regret.
- `shape_key` is for exact matching. `shape_family` is for cross-scale charts.
- The UI should treat `features` and `route_doc` as expandable/debug JSON, not
  as the primary charting surface.
