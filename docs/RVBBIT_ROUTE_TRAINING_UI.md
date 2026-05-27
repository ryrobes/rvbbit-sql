# Rvbbit Route Training UI Contract

This document is the UI-facing contract for named route profiles, SQL-native
query training, query benchmarking, and the current "learning" workflow. Use it
when building UI for inspecting profile queries, adding new training queries,
removing stale training, comparing candidate engines, and activating profiles.

For general online routing telemetry, see `docs/RVBBIT_ROUTING_UI.md`. This
document focuses on table-backed training state and profile curation.

## Mental Model

Rvbbit routing has two related data paths:

- **Online routing telemetry** records what normal user queries chose and how
  they performed. These rows live in `rvbbit.route_decisions` and
  `rvbbit.route_executions`.
- **Training data** deliberately runs one SQL query against multiple candidate
  engines, validates the results, stores timings, and rebuilds a named profile.
  These rows live in `rvbbit.route_training_queries`,
  `rvbbit.route_training_runs`, and `rvbbit.route_training_results`.

A **profile** is a named route policy in `rvbbit.route_profiles`. The active
profile is the default policy for normal queries. A session can temporarily use
another profile with `rvbbit.route_use_profile(...)`.

A **training query** is a saved SQL statement plus its extracted route features.
It belongs to one profile and is keyed by `(profile_name, query_hash)`. Training
the same query again updates that saved query and appends a new run/results.

A **training run** is one call to `rvbbit.route_train_query(...)`. It stores the
requested repeats, candidate list, settings, status, and summary JSON.

A **training result** is one candidate/repeat measurement. It stores elapsed
time, row count, result digest, validation status, error text, and the route
document observed for that candidate.

A **profile entry** is the accepted route choice for a query shape after
`rvbbit.route_profile_rebuild(...)` compares validated candidate medians.
Accepted entries live in `rvbbit.route_profile_entries`; rejected shapes remain
inside the generated profile JSON under `profile->'rejected'`.

Validated training timings are also copied into `rvbbit.route_observations`
with a `source` like `sql-train:<profile>:<run_id>`. This keeps the older
observation-based profile tooling useful, but the UI should treat the
`route_training_*` tables as the source of truth for named query curation.

## Candidate Engines

Candidate names used by the SQL API:

- `rvbbit_native`: vanilla PostgreSQL/Rvbbit path.
- `datafusion_vector`: in-process DataFusion over authoritative parquet row
  groups.
- `duck_vector`: DuckDB sidecar over authoritative parquet row groups.
- `pg_rowstore`: retained shadow heap rowstore path.
- `datafusion_hive`: DataFusion over Hive-style parquet variants.
- `duck_hive`: DuckDB over Hive-style parquet variants.

`candidates => 'all'` expands to native plus DataFusion vector, Duck vector,
Postgres rowstore, DataFusion Hive, and Duck Hive. Native is always included
even if the caller omits it, because it is the correctness baseline.

Some candidates may be skipped at run time. Common reasons are disabled route
gates, parquet not being authoritative yet, missing Hive variants, or missing
retained shadow heaps.

## Core Tables And Views

### Profiles

Function:

```sql
SELECT rvbbit.route_profiles();
```

Returns profile rows as JSON, including:

- `name`
- `active`
- `created_at`
- `updated_at`
- `entries`
- `points`
- candidate entry counts
- `avg_confidence`
- `generated_by`
- `imported_from_name`

Table: `rvbbit.route_profiles`

Important columns:

- `name`: profile identifier.
- `active`: true for the global active profile. Only one profile can be active.
- `profile`: generated/imported JSON profile.
- `created_at`, `updated_at`: lifecycle timestamps.

View: `rvbbit.route_profile_summary`

Use this for the profile detail table. Important columns:

- `profile_name`
- `active`
- `profile_updated_at`
- `shape_key`
- `shape_family`
- `choice`
- `confidence`
- `reason`
- `observations`
- `native_ms`
- `duck_ms`
- `duck_hive_ms`
- `datafusion_ms`
- `datafusion_hive_ms`
- `pg_ms`

### Training Queries

Table: `rvbbit.route_training_queries`

One row per saved training query per profile.

Important columns:

- `id`: stable training query id for UI actions.
- `profile_name`: owning profile.
- `query_sql`: source SQL text to display/edit carefully.
- `query_hash`: normalized query hash used for upsert.
- `shape_key`: exact route shape key.
- `shape_family`: shape key with scale buckets removed.
- `features`: route feature JSON. Treat this as expandable debug data.
- `label`: optional human-readable UI label.
- `enabled`: only enabled rows are used by profile rebuild.
- `created_by`
- `created_at`
- `updated_at`

### Training Runs

Table: `rvbbit.route_training_runs`

One row per call to `rvbbit.route_train_query(...)`.

Important columns:

- `id`: run id.
- `training_query_id`
- `profile_name`
- `started_at`
- `finished_at`
- `status`: currently `running` during execution and `finished` after summary
  persistence.
- `repeats`: number of repeats requested. The function clamps this to `1..100`.
- `candidates`: measured candidate list after expansion.
- `settings`: JSON settings, including `min_gain_pct`, `activate`, and
  `candidate_request`.
- `summary`: JSON response returned by the training function.

The current training function is synchronous. The row is inserted as
`running`, then updated to `finished`, but callers should not rely on polling
live progress from another session because the function normally commits as one
transaction. Show a blocking or background-job state in the UI until the SQL
call returns.

### Training Results

Table: `rvbbit.route_training_results`

One row per candidate/repeat measurement.

Important columns:

- `id`
- `run_id`
- `training_query_id`
- `profile_name`
- `observed_at`
- `candidate`
- `repeat_idx`
- `elapsed_ms`
- `rows_returned`
- `result_digest`
- `status`: `ok`, `error`, or `skipped`.
- `validation_status`: see validation semantics below.
- `error`: candidate error or skip reason.
- `route_doc`: full route explanation JSON for the forced candidate.

View: `rvbbit.route_training_summary`

This is the easiest view for candidate comparison cards. It groups by profile,
training query, and candidate, then exposes:

- `profile_name`
- `training_query_id`
- `query_hash`
- `shape_key`
- `shape_family`
- `label`
- `enabled`
- `candidate`
- `ok_runs`
- `error_runs`
- `median_ms`
- `first_seen`
- `last_seen`
- `last_validation_status`
- `last_error`

## Validation Semantics

Training executes the original SELECT through normal SPI with a local
`rvbbit.route_force_candidate` setting for each candidate. Results are digested
and compared to native.

`validation_status` values:

- `baseline`: native baseline result.
- `ok`: candidate digest and row count matched the native baseline.
- `mismatch`: candidate returned a different digest or row count.
- `no_baseline`: non-native candidate ran before a native baseline was
  available.
- `error`: candidate execution failed.
- `skipped`: candidate was not available.

Only rows with `status = 'ok'`, `validation_status IN ('baseline', 'ok')`, and
positive `elapsed_ms` are eligible for profile rebuilds.

If the query has an `ORDER BY`, the result digest is order-sensitive. Without an
`ORDER BY`, row hashes are sorted before digesting so equivalent unordered
results validate.

## Profile Rebuild Semantics

`rvbbit.route_profile_rebuild(profile_name, min_gain_pct, activate)` reads all
enabled, validated training results for the profile.

For each shape:

- At least two validated candidate medians are required.
- The fastest median is compared against the second-fastest median.
- The accepted gain must be at least `min_gain_pct`.
- Candidate-specific confidence floors also apply:
  - `pg_rowstore`: 25 percent.
  - Hive candidates: `RVBBIT_ROUTE_HIVE_MIN_CONFIDENCE` or
    `rvbbit.route_hive_min_confidence`, default 8 percent.
  - Other candidates: 5 percent.

Accepted shapes become `rvbbit.route_profile_entries`. Rejected shapes are
kept in profile JSON with the rejection reason and candidate medians.

If a rebuild produces accepted entries, it activates the profile when
`activate => true` or when that profile was already active. If no entries are
accepted, the profile is stored inactive.

## Recommended Screens

### Profiles

Show all profiles from `rvbbit.route_profiles()` with:

- active badge
- entry count
- profile point count
- candidate mix
- average confidence
- updated timestamp
- generated/import source

Actions:

- create profile
- activate profile
- retire profile
- open profile detail

### Profile Detail

Show:

- profile header from `rvbbit.route_profiles()`
- accepted entries from `rvbbit.route_profile_summary`
- saved training queries from `rvbbit.route_training_queries`
- candidate timing summaries from `rvbbit.route_training_summary`
- recent online route executions from `rvbbit.route_runtime_summary`

Useful badges:

- `active` or `draft`
- `accepted` when a `shape_key` has a profile entry
- `rejected` when profile JSON contains that shape under `rejected`
- candidate name
- validation status
- route source from `route_doc->>'route_source'`

### Training Query Detail

Show:

- label
- source SQL
- enabled state
- shape key and shape family
- last trained timestamp
- per-candidate medians
- run history
- per-repeat result rows
- validation and error details

Actions:

- retrain query
- edit label by retraining the same SQL with a new label
- disable query and rebuild
- delete query and rebuild
- inspect `rvbbit.route_explain(query_sql)`

### Add Or Train Query

Recommended UI fields:

- profile name
- SQL text
- label
- repeats, default `3`
- minimum gain, default `0.05`
- candidates, default `all`
- activate after rebuild, default false for draft workflows and true only for
  explicit "train and activate" actions

The training call is synchronous. It may run the query many times and can take
as long as the slowest candidate times repeats. Run it from a background UI job
or show a blocking progress state.

## Function Response Shapes

`rvbbit.route_train_query(...)` returns JSON shaped like:

```json
{
  "profile": "dashboard-fast-path",
  "training_query_id": 12,
  "run_id": 34,
  "results": [
    {
      "candidate": "datafusion_vector",
      "runs": 3,
      "ok_runs": 3,
      "error_runs": 0,
      "skipped_runs": 0,
      "median_ms": 41.2,
      "last_validation_status": "ok",
      "last_error": null
    }
  ],
  "rebuild": {
    "profile": "dashboard-fast-path",
    "active": false,
    "entries": 1,
    "rejected": 0,
    "points": 1,
    "training_observation_count": 6,
    "profile_json": {}
  }
}
```

`rvbbit.route_profile_rebuild(...)` returns the nested `rebuild` object shown
above.

`rvbbit.route_training_delete_query(...)` returns:

```json
{
  "action": "deleted",
  "profile": "dashboard-fast-path",
  "training_query_id": 12,
  "deleted": 1,
  "rebuild": {}
}
```

When `rebuild => false`, the `rebuild` field is null.

## SQL Recipes

### Create A Draft Profile

```sql
SELECT jsonb_pretty(rvbbit.route_create_profile('dashboard-fast-path', false));
```

### List Profiles

```sql
SELECT jsonb_pretty(rvbbit.route_profiles());
```

### Profile Entries

```sql
SELECT *
FROM rvbbit.route_profile_summary
WHERE profile_name = $1
ORDER BY confidence DESC, observations DESC, shape_family;
```

### Rejected Shapes

```sql
SELECT
  rejected.key AS shape_key,
  rejected.value AS rejection
FROM rvbbit.route_profiles rp
CROSS JOIN LATERAL jsonb_each(coalesce(rp.profile->'rejected', '{}'::jsonb)) AS rejected
WHERE rp.name = $1
ORDER BY rejected.key;
```

### Training Query List

```sql
SELECT
  tq.id,
  tq.profile_name,
  tq.label,
  tq.enabled,
  tq.query_hash,
  tq.shape_key,
  tq.shape_family,
  tq.created_by,
  tq.created_at,
  tq.updated_at,
  count(DISTINCT r.id) AS runs,
  max(r.finished_at) AS last_finished_at
FROM rvbbit.route_training_queries tq
LEFT JOIN rvbbit.route_training_runs r
  ON r.training_query_id = tq.id
WHERE tq.profile_name = $1
GROUP BY
  tq.id,
  tq.profile_name,
  tq.label,
  tq.enabled,
  tq.query_hash,
  tq.shape_key,
  tq.shape_family,
  tq.created_by,
  tq.created_at,
  tq.updated_at
ORDER BY tq.updated_at DESC;
```

### Candidate Summary For One Training Query

```sql
SELECT *
FROM rvbbit.route_training_summary
WHERE profile_name = $1
  AND training_query_id = $2
ORDER BY median_ms NULLS LAST, candidate;
```

### Run History

```sql
SELECT
  id,
  started_at,
  finished_at,
  status,
  repeats,
  candidates,
  settings,
  summary
FROM rvbbit.route_training_runs
WHERE profile_name = $1
  AND training_query_id = $2
ORDER BY started_at DESC;
```

### Per-Repeat Results

```sql
SELECT
  candidate,
  repeat_idx,
  elapsed_ms,
  rows_returned,
  status,
  validation_status,
  error,
  route_doc->>'route_source' AS route_source,
  route_doc
FROM rvbbit.route_training_results
WHERE run_id = $1
ORDER BY candidate, repeat_idx;
```

### Train Or Retrain A Query

```sql
SELECT jsonb_pretty(rvbbit.route_train_query(
  profile_name => $1,
  query => $2,
  repeats => 3,
  min_gain_pct => 0.05,
  activate => false,
  candidates => 'all',
  label => $3
));
```

Training the same normalized query again upserts
`rvbbit.route_training_queries` and appends a new run/results.

### Rebuild A Profile

```sql
SELECT jsonb_pretty(rvbbit.route_profile_rebuild($1, 0.05, false));
```

Use `activate => true` only for an explicit publish action. If the profile is
already active, rebuild keeps it active when accepted entries exist.

### Delete A Training Query

For UIs that need explicit rebuild settings, delete first and rebuild second:

```sql
SELECT jsonb_pretty(rvbbit.route_training_delete_query($1, $2, rebuild => false));
SELECT jsonb_pretty(rvbbit.route_profile_rebuild($1, 0.05, false));
```

The convenience form below deletes and immediately rebuilds with the built-in
defaults of `min_gain_pct = 0.05` and `activate = true`:

```sql
SELECT jsonb_pretty(rvbbit.route_training_delete_query($1, $2, rebuild => true));
```

Deleting a training query cascades its runs and results.

### Disable Or Re-Enable A Training Query

There is not yet a wrapper function for toggling. The system table is ordinary
SQL state, so an admin UI can do:

```sql
UPDATE rvbbit.route_training_queries
SET enabled = false,
    updated_at = now()
WHERE profile_name = $1
  AND id = $2;

SELECT jsonb_pretty(rvbbit.route_profile_rebuild($1, 0.05, false));
```

Use `enabled = true` to re-enable and then rebuild.

### Activate Or Retire Profiles

```sql
SELECT jsonb_pretty(rvbbit.route_activate_profile($1));
SELECT jsonb_pretty(rvbbit.route_retire_profile($1));
```

### Session Profile Override

```sql
SELECT jsonb_pretty(rvbbit.route_use_profile($1, local => true));
SELECT jsonb_pretty(rvbbit.route_clear_profile(local => true));
SELECT jsonb_pretty(rvbbit.route_current_profile());
```

### Explain A Query Before Training

```sql
SELECT jsonb_pretty(rvbbit.route_explain($1));
```

## Training SQL Restrictions

`rvbbit.route_train_query(...)` accepts read-only SELECT workloads that
reference at least one Rvbbit table. The safety check is conservative.

Current restrictions include:

- SQL must start with `SELECT` or `WITH`.
- Multiple statements are rejected.
- DML, DDL, COPY, CALL, DO, LISTEN/NOTIFY, and REFRESH are rejected.
- Volatile or time-varying functions such as `random`, `now`,
  `clock_timestamp`, `generate_series`, UUID generators, and sequence functions
  are rejected.
- Direct references to `rvbbit.`, `pg_`, JSON operators/casts, dollar quoting,
  and a small set of ambiguous natural-language tokens are rejected.

These restrictions are meant to make repeated candidate execution safe and
result validation meaningful. Queries rejected by training can still be normal
user queries; they just are not eligible for this SQL-native training path yet.

## UI Safety Rules

- Treat `query_sql`, `features`, `route_doc`, `error`, and profile JSON as
  sensitive operational data.
- Do not auto-activate newly trained profiles unless the user explicitly asks.
- Show validation mismatches prominently. A mismatched candidate cannot create
  an accepted profile entry.
- Make it clear that training executes the submitted query multiple times.
- Prefer draft rebuilds (`activate => false`) during editing, then a separate
  publish action.
- After delete/disable/re-enable, rebuild the profile so accepted entries match
  the visible corpus.
- Use `shape_key` for exact entry joins and `shape_family` for grouping across
  table sizes.

## Example End-To-End Flow

```sql
-- 1. Create a draft profile.
SELECT rvbbit.route_create_profile('dashboard-fast-path', false);

-- 2. Train one query against all available candidates.
SELECT rvbbit.route_train_query(
  profile_name => 'dashboard-fast-path',
  query => $SQL$
    SELECT "RegionID", count(DISTINCT "UserID")
    FROM hits
    GROUP BY 1
    ORDER BY 1
  $SQL$,
  repeats => 3,
  min_gain_pct => 0.05,
  activate => false,
  candidates => 'all',
  label => 'dashboard region distinct users'
);

-- 3. Inspect candidate medians and validation.
SELECT *
FROM rvbbit.route_training_summary
WHERE profile_name = 'dashboard-fast-path'
ORDER BY training_query_id, median_ms NULLS LAST;

-- 4. Rebuild and inspect accepted entries.
SELECT rvbbit.route_profile_rebuild('dashboard-fast-path', 0.05, false);

SELECT *
FROM rvbbit.route_profile_summary
WHERE profile_name = 'dashboard-fast-path';

-- 5. Publish when the profile looks right.
SELECT rvbbit.route_activate_profile('dashboard-fast-path');
```
