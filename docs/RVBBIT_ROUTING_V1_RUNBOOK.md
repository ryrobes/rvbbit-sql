# Rvbbit Routing V1 Runbook

This is the operator checklist for the adaptive router.

## Safe Defaults

Fresh installs are conservative. Without an active route profile, Rvbbit keeps
queries on the native PostgreSQL/Rvbbit path after hard-rule rewrites. That
avoids surprising Duck/DataFusion execution on a new workload.

```sql
SELECT jsonb_pretty(rvbbit.route_status());
SELECT jsonb_pretty(rvbbit.route_current_profile());
```

Expected cold-install state:

- `current_profile.profile_source = "none"`
- `current_profile.profile_name = null`
- route explanations for Rvbbit tables choose `rvbbit_native` with
  `route_source = "no-profile"` unless a hard rule applies.

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

For v1, offline forced-run training is the supported UX:

```bash
RVBBIT_ROUTE_IMPORT=1 ./bench/rebuild_rvbbit_route_training.sh
```

Online observations can seed later profiles:

```sql
SELECT jsonb_pretty(rvbbit.route_eval('bench-combined'));
SELECT jsonb_pretty(rvbbit.route_train('workload-current', 3, 0.05));
```

Use `route_train(...)` only after you have enough observations across candidate
families. Forced benchmark profiles remain the more reliable baseline.
