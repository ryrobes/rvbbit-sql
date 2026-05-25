# Rvbbit Adaptive Routing Production Goal

This is a future `/goal` brief for taking the adaptive routing system from
benchmark-proven prototype to pseudo-production-ready architecture. "Production
ready" here means defensible on paper, with clear guardrails, observability,
training workflows, rollback, and correctness coverage. It still needs real
workload validation before being treated as operationally mature.

## Goal Prompt

Make Rvbbit's adaptive query routing system production-shaped.

The router should be the default path for normal Rvbbit tables, choosing among
`rvbbit_native`, `duck_vector`, `duck_hive`, `datafusion_vector`,
`datafusion_hive`, and `pg_rowstore` only when the candidate is eligible,
trained, and safe. It must preserve PostgreSQL semantics, degrade cleanly to
native execution, expose clear `EXPLAIN`/observability surfaces, and support an
operator-friendly training loop that can be started from SQL.

Do not optimize only for ClickBench/TPC-H. Use those suites as repeatable
signals, but every change should improve or harden general SQL behavior.

## Current Baseline

- Native auto routing is available through `BENCH_SYSTEMS=rvbbit`.
- Forced benchmark targets exist for Duck, DataFusion, Hive layouts, and
  preserved heap/rowstore.
- Route profiles are stored in `rvbbit.route_profiles`,
  `rvbbit.route_profile_entries`, and `rvbbit.route_profile_points`.
- Online telemetry is stored in `rvbbit.route_decisions` and
  `rvbbit.route_executions`.
- UI-facing observability surfaces are documented in
  `docs/RVBBIT_ROUTING_UI.md`.
- Hive parquet variants and shadow heap are useful but should remain
  configurable because they increase write/space cost.

## Non-Negotiables

- Correctness beats speed. A wrong route must be treated as a blocker.
- Unsupported SQL must fall back to `rvbbit_native` without user intervention.
- Router decisions must be explainable.
- Training must never mutate production route behavior until a profile is
  explicitly activated or promoted by policy.
- A route profile must be versioned and rollbackable.
- Background telemetry/training must be best-effort and bounded. Query latency
  cannot depend on telemetry writes.

## Work Slices

### 1. Establish The Safety Contract

- Define the exact eligibility contract for each candidate:
  `duck_vector`, `duck_hive`, `datafusion_vector`, `datafusion_hive`,
  `pg_rowstore`, and `rvbbit_native`.
- Audit snapshot, MVCC, mutation, DDL, volatile function, collation, timezone,
  parameter, prepared statement, and security-definer behavior.
- Add explicit rejection reasons for every unsupported shape.
- Make `rvbbit.route_explain(query)` the source of truth for route availability.

Acceptance:

- Every candidate has a documented "safe when" and "reject when" matrix.
- Rejected routes produce stable machine-readable reasons.
- A test verifies fallback for non-Rvbbit tables and mixed unsupported queries.

### 2. Harden The Route Decision Layer

- Ensure `rvbbit` uses the native router by default in normal backend execution.
- Confirm profile loading, cache invalidation, and route cache reset behavior.
- Add route profile version identifiers to decisions/executions.
- Add candidate availability fields for layout state:
  compacted parquet available, hive variant available, shadow heap retained,
  shadow heap dirty, table mutation generation, and table row group count.
- Make route selection deterministic for identical query shape and profile state.

Acceptance:

- `SELECT rvbbit.route_explain(...)` and actual execution agree on the chosen
  route unless exploration is explicitly enabled.
- Extension reload/redeploy does not silently drop the active profile.
- Route decisions log the active profile name/version.

### 3. Productionize Candidate Execution

- Move remaining benchmark-only execution assumptions into proper backend-safe
  paths.
- Finish Rust-native DuckDB integration or clearly isolate any sidecar process
  behind a stable local protocol.
- Harden DataFusion failures so unsupported functions or planner errors are
  eligibility rejections, not query failures.
- Verify Hive layout discovery and file globbing for Duck and DataFusion.
- Verify `pg_rowstore` only routes when the preserved heap is valid for the
  queried snapshot.
- Add route-level timeout and max-output protections.

Acceptance:

- A forced failure in Duck/DataFusion falls back or fails in a controlled,
  explainable way.
- Candidate execution has integration tests for empty result, scalar aggregate,
  wide output, text, date/time, NULLs, LIMIT/OFFSET, ORDER BY, LIKE, regex, joins,
  and grouped aggregates.

### 4. Build SQL-Driven Training UX

Add SQL APIs that let users train without knowing benchmark scripts.

Proposed catalog:

```sql
CREATE TABLE rvbbit.route_training_jobs (...);
CREATE TABLE rvbbit.route_training_job_queries (...);
CREATE TABLE rvbbit.route_training_job_results (...);
```

Proposed functions:

```sql
SELECT rvbbit.route_train_async(
  profile_name => 'workload-2026-05-23',
  source => 'pg_stat_statements',
  min_calls => 10,
  max_queries => 500,
  candidates => ARRAY[
    'rvbbit_native',
    'duck_vector',
    'duck_hive',
    'datafusion_vector',
    'datafusion_hive',
    'pg_rowstore'
  ],
  activate => false
);

SELECT rvbbit.route_train_async(
  profile_name => 'explicit-set',
  source_sql => $$
    SELECT query_text
    FROM app_candidate_queries
    WHERE train_route = true
  $$,
  activate => false
);

SELECT rvbbit.route_training_status(job_id);
SELECT rvbbit.route_training_cancel(job_id);
SELECT rvbbit.route_activate_profile('workload-2026-05-23');
```

Implementation path:

- Start with a SQL function that inserts a job row and sends `NOTIFY`.
- Have a supervised worker process consume jobs and run candidate timings.
- Later, consider a PostgreSQL background worker or `pg_cron` integration.
- Support query sources:
  explicit SQL table, `pg_stat_statements`, `rvbbit.route_executions`, benchmark
  profile files, and pasted query lists.
- Store raw observations first; generate profiles as a separate promotion step.

Acceptance:

- A user can start training from SQL and watch progress from SQL.
- Training has cancellation, timeout, candidate allowlist, row limit, and sample
  size controls.
- Training writes observations and produces an inactive candidate profile.
- Activation is explicit unless a policy flag enables auto-promotion.

### 5. Profile Lifecycle And Promotion

- Add profile states: `draft`, `trained`, `canary`, `active`, `retired`.
- Add metadata: created_by, source, corpus, candidate set, training window,
  profile version, parent profile, and notes.
- Support multiple active-ish profile scopes:
  global default, database default, role default, relation/workload tag default,
  session override, transaction-local override, and per-query hint.
- Add canary routing controls:
  route N percent of matching shapes to a candidate profile, compare latency,
  and rollback automatically on error/regret threshold.
- Add import/export stability tests for profiles.

Acceptance:

- Active profile rollback is one SQL call.
- A canary profile can be tested on online traffic without replacing the active
  profile globally.
- Profile export/import round trips without losing candidates or curve points.

### 6. Profile Selection And Query Hints

Support advanced users who need different routing behavior for different
workloads. The default profile should remain the normal path, but users should
be able to select a profile explicitly when they know a query belongs to a
special workload.

Recommended precedence:

1. Per-query hint.
2. Transaction-local setting.
3. Session setting.
4. Role/database/relation/workload mapping.
5. Active global default profile.
6. No profile: eligibility/hard-rule fallback only.

Start with GUC-based control because it is PostgreSQL-native and easy to make
safe:

```sql
SET LOCAL rvbbit.route_profile = 'dashboard-fast-path';
SELECT ... FROM rvbbit_table ...;

SET rvbbit.route_profile = 'etl-batch-profile';
```

Add helper functions for app code that should avoid raw `SET` calls:

```sql
SELECT rvbbit.route_use_profile('dashboard-fast-path', local => true);
SELECT rvbbit.route_clear_profile(local => true);
```

Optional advanced syntax can be SQL comments parsed from the original query
string in planner/rewrite hooks:

```sql
/*+ RVBBIT_PROFILE(dashboard-fast-path) */
SELECT ... FROM hits WHERE ...;

/*+ RVBBIT_CANDIDATES(duck_vector,datafusion_vector,rvbbit_native) */
SELECT ... FROM lineitem WHERE ...;
```

PostgreSQL does not have native optimizer hints, so comment hints must be
treated as Rvbbit-specific advisory metadata, not SQL semantics. They should be
disabled or restricted by policy if needed:

```sql
SET rvbbit.route_comment_hints = off;
SET rvbbit.route_allowed_profiles = 'default,dashboard-fast-path,etl-batch-profile';
```

Implementation sketch:

- Add `route_profile_name` to decision/execution telemetry and route cache keys.
- Add `rvbbit.route_profile` GUC with empty/default meaning "use active
  default".
- Add `rvbbit.route_comment_hints` GUC, default off until tested.
- In the planner/rewrite hook, parse only a tiny allowlisted comment grammar
  from the query string. Ignore unknown hints.
- Validate hinted profiles exist, are active/canary/allowed, and are visible to
  the current role.
- Never let a hint bypass candidate safety checks. It may choose a profile or
  narrow candidates, but eligibility still wins.
- Record hint source in telemetry: `profile-source=session`,
  `profile-source=transaction`, `profile-source=comment-hint`, etc.
- Include the effective profile in `rvbbit.route_explain(...)`.

Acceptance:

- A transaction-local profile hint changes routing for one query block and then
  reverts automatically.
- Comment hints, if enabled, select profiles but cannot force unsafe candidates.
- Route cache keys include the effective profile, so profile hints cannot reuse
  stale decisions from a different profile.
- UI/SQL telemetry can show which profile was used and why.

### 7. Training And Evaluation Loop

Codify the loop:

1. Capture workload queries.
2. Measure each eligible candidate with forced routes.
3. Import observations.
4. Train a draft profile.
5. Evaluate held-out regret.
6. Activate or canary.
7. Monitor online decisions/executions.
8. Feed online observations back into the next training run.

Add scripts/functions for:

- Full benchmark corpus rebuild across ClickBench and TPC-H sizes.
- Held-out evaluation by suite, scale, and shape family.
- Regret report: chosen route vs oracle route per shape.
- Drift report: online runtime vs trained expectation.
- Candidate health report: failure rate, timeout rate, unsupported-shape rate.

Acceptance:

- One command rebuilds the benchmark training corpus.
- One SQL workflow trains a profile from observed workload history.
- Held-out regret is reported before profile activation.

### 8. Observability And UI Contract

- Keep `docs/RVBBIT_ROUTING_UI.md` current with every new table, enum, view, and
  route source.
- Add views for:
  active profile state, profile candidates, profile regret, training jobs,
  candidate availability, online drift, and recent route errors.
- Add `EXPLAIN (RVBBIT)` or an equivalent stable SQL function that shows:
  chosen candidate, rejected candidates, reasons, profile match, confidence,
  expected latency, and route cache status.

Acceptance:

- A UI can show current routing state without scraping logs.
- A UI can show whether `pg_rowstore`, `duck_hive`, and `datafusion_hive` are
  disabled by config, unavailable by layout, or rejected by model.
- Route health can be debugged from SQL alone.

### 9. Test Matrix

Run these before declaring the router pseudo-production-ready:

- Rust unit tests for feature extraction, shape keys, profile matching, curve
  interpolation, candidate gating, and fallback.
- SQL integration tests for `route_explain`, profile import/export/train,
  decisions, executions, and training jobs.
- Correctness tests comparing routed results to PostgreSQL native results.
- Mutation tests: INSERT, UPDATE, DELETE, TRUNCATE, VACUUM, ANALYZE, REINDEX,
  extension reload, table rename, column rename, type changes, and DROP.
- Snapshot tests: concurrent readers/writers, dirty shadow heap, stale parquet,
  and compaction during reads.
- Benchmark tests:
  ClickBench, TPC-H, TATP, plus at least one new held-out analytical workload.
- Fault tests:
  killed sidecar, missing parquet file, corrupted hive layout, timeout,
  candidate crash, route telemetry queue overflow.

Acceptance:

- Routed result sets match native PostgreSQL for all supported test queries.
- Unsupported queries either run native or emit controlled errors only when the
  native path itself would error.
- Benchmarks show no severe regression when routing is enabled but no profile is
  loaded.

### 10. Operational Defaults And Knobs

Define defaults for:

- `rvbbit.route_profile`
- `rvbbit.route_comment_hints`
- `rvbbit.route_allowed_profiles`
- `rvbbit.route_duck_vector`
- `rvbbit.route_duck_hive`
- `rvbbit.route_datafusion_vector`
- `rvbbit.route_datafusion_hive`
- `rvbbit.route_pg_rowstore`
- `rvbbit.route_hive`
- `rvbbit.route_hive_min_confidence`
- `rvbbit.route_profile_min_confidence`
- `rvbbit.compact_hive_layout`
- `rvbbit.compact_keep_heap`
- training timeouts, candidate allowlists, and retention windows

Recommended posture:

- Routing on by default.
- Conservative candidate eligibility.
- Hive and shadow heap configurable because they increase storage/write cost.
- Exploration off by default in production.
- Comment hints off by default until the grammar and security model are
  hardened; GUC profile selection can ship first.
- Training jobs never auto-activate unless explicitly configured.

Acceptance:

- `SHOW`/SQL functions expose the effective routing configuration.
- Docs explain how to disable expensive paths for write-sensitive deployments.
- Defaults are safe for ordinary users and useful for analytical users.

## Final Definition Of Done

- `BENCH_SYSTEMS=rvbbit` exercises the adaptive router by default.
- `rvbbit.route_explain(...)` explains every route choice and rejection.
- Active profiles survive extension reset/reload in the expected deployment
  path, or the restore/import story is documented and tested.
- Users can train from SQL, monitor training from SQL, and activate/rollback
  profiles from SQL.
- Users can select non-default route profiles with transaction/session controls,
  and optionally with guarded comment hints.
- Telemetry tables and dashboard views describe online route mix, latency,
  errors, drift, and candidate availability.
- Forced benchmark targets remain available for investigation.
- A fresh trained auto profile beats or matches the best fixed candidate on
  mixed benchmark corpuses within an agreed regret threshold.
- Correctness tests prove routed execution matches PostgreSQL semantics for the
  supported route surface.
