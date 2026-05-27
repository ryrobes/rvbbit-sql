# Rvbbit Routing V1 Runbook

This is the operator checklist for the adaptive router.

## Safe Defaults

Fresh installs use deterministic cold-start rules. Without an active route
profile, Rvbbit keeps metadata/native plan rewrites and row-returning queries
on the native PostgreSQL/Rvbbit path. Analytical parquet shapes are split by
cheap shape and physical metadata: small/simple shapes stay native, text-heavy
distinct/top-k/rollup shapes can use Hive parquet variants, and the remaining
analytical shapes use vector execution when the parquet catalog is
authoritative. DuckDB remains available for trained profiles, forced routing,
or fallback when it is the first available candidate in a chosen class.

The no-profile small/simple native cutoff defaults to 500,000 rows. Tune it with
`RVBBIT_ROUTE_NO_PROFILE_NATIVE_MAX_ROWS` or the session setting
`rvbbit.route_no_profile_native_max_rows` when comparing native/DataFusion
crossover points. Variant-first no-profile routing starts at 250,000 rows by
default; tune it with `RVBBIT_ROUTE_NO_PROFILE_VARIANT_MIN_ROWS` or
`rvbbit.route_no_profile_variant_min_rows`.

```sql
SELECT jsonb_pretty(rvbbit.route_status());
SELECT jsonb_pretty(rvbbit.route_current_profile());
```

Expected cold-install state:

- `current_profile.profile_source = "none"`
- `current_profile.profile_name = null`
- route explanations for Rvbbit tables choose either `rvbbit_native` with
  `route_source = "no-profile-native"`, `datafusion_vector` with
  `route_source = "no-profile-datafusion"`, or a Hive candidate with
  `route_source = "no-profile-variant"` after hard rules apply.

## Enable Routing

Import and activate a profile:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.competitors.yml \
  exec -T bench python /bench/rvbbit_route_profile_admin.py \
  import --profile /bench/rvbbit_route_profile.json \
  --name bench-combined --active
```

Then verify:

```sql
SELECT jsonb_pretty(rvbbit.route_status());
SELECT jsonb_pretty(rvbbit.route_profiles());
SELECT rvbbit.route_explain_text('select count(*) from hits');
```

## Runtime Fallback

Duck/DataFusion routing is fail-open by default. If the selected sidecar engine
fails, Rvbbit reruns the original query through native PostgreSQL/Rvbbit with
`rvbbit.duck_backend=off` for the nested execution.

Disable fail-open only while debugging executor errors:

```sql
SET rvbbit.duck_backend_fail_open = off;
```

or:

```bash
export RVBBIT_DUCK_BACKEND_FAIL_OPEN=0
```

## Candidate Gates

Disable individual route destinations when write/storage/runtime constraints
matter more than analytics speed:

```bash
export RVBBIT_ROUTE_DUCK_VECTOR=0
export RVBBIT_ROUTE_DATAFUSION_VECTOR=0
export RVBBIT_ROUTE_HIVE=0
export RVBBIT_ROUTE_PG_ROWSTORE=0
```

Equivalent per-session GUCs:

```sql
SET rvbbit.route_hive = off;
SET rvbbit.route_pg_rowstore = off;
```

Check the effective gates in `rvbbit.route_status()->'candidate_gates'`.

## Observability

The router writes best-effort async telemetry:

```sql
SELECT rvbbit.route_decision_log_status();

SELECT candidate, profile_name, profile_source, route_source, sum(executions)
FROM rvbbit.route_runtime_summary
GROUP BY 1,2,3,4
ORDER BY 5 DESC;
```

Important warning states:

- `profile_source = 'none'`: no active profile, conservative native routing.
- `profile_source = 'guc-missing'`: requested profile does not exist.
- `candidate = 'rvbbit_native'` with `route_source = 'eligibility'`: external
  engines were not safe for the current table state.

## Training Loop

For UI builders, table contracts, candidate/result status semantics, and SQL
recipes for adding/removing saved profile queries, see
`docs/RVBBIT_ROUTE_TRAINING_UI.md`.

Preferred SQL-native workflow for a single query:

```sql
SELECT jsonb_pretty(rvbbit.route_train_query(
  'dashboard-fast-path',
  'SELECT "RegionID", count(DISTINCT "UserID") FROM hits GROUP BY 1 ORDER BY 1',
  repeats => 3,
  min_gain_pct => 0.05,
  activate => true,
  candidates => 'all',
  label => 'dashboard region distinct users'
));
```

This runs the original SELECT through the normal backend path once per
candidate/repeat with `rvbbit.route_force_candidate` scoped locally, validates
candidate result digests against `rvbbit_native`, writes
`route_training_queries`, `route_training_runs`, `route_training_results`, and
then rebuilds the named profile from those persisted results.

Useful SQL controls:

```sql
SELECT * FROM rvbbit.route_training_summary
WHERE profile_name = 'dashboard-fast-path'
ORDER BY last_seen DESC;

SELECT jsonb_pretty(rvbbit.route_profile_rebuild('dashboard-fast-path', 0.05, true));

SELECT jsonb_pretty(rvbbit.route_training_delete_query(
  'dashboard-fast-path',
  42,
  rebuild => true
));
```

Offline forced-run training is still useful for broad benchmark sweeps:

```bash
RVBBIT_ROUTE_IMPORT=1 ./bench/rebuild_rvbbit_route_training.sh
```

Online observations can seed later profiles:

```sql
SELECT jsonb_pretty(rvbbit.route_eval('bench-combined'));
SELECT jsonb_pretty(rvbbit.route_train('workload-current', 3, 0.05));
```

Use `route_train(...)` only after you have enough observations across candidate
families. For named, UI-driven curation, prefer `route_train_query(...)`
because its corpus and run results are table-backed and removable from SQL.
