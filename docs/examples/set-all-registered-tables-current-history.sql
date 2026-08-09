-- Convert every live RVBBIT-registered table in every schema to current-only
-- history, skipping tables already set to current. Optionally seed a
-- conservative dashboard refresh policy where no explicit policy exists. This
-- is ordinary PostgreSQL SQL and can be run directly in DataRabbit; no psql
-- variables or backslash commands are required.
--
-- Refresh-policy enrollment defaults OFF because an upgraded server may
-- contain hundreds or thousands of cold registered tables. Set the boolean to
-- true only when every converted table should receive this dashboard policy,
-- or apply policies later from the workload observer's ready candidates.
-- Existing explicit policies are always preserved.
--
-- When enabled, the shipped profile is demand-driven: cold tables remain on
-- the heap, while a table observed on the slow path becomes eligible for the
-- single global accel_tick heartbeat. To proactively warm every dirty table,
-- change 'demand' to 'target' and the following NULL to a freshness target such
-- as 900.
WITH config(
    seed_missing_refresh_policies,
    default_strategy,
    freshness_target_secs,
    min_interval_secs,
    daily_refresh_budget,
    full_rebuild_drift_ratio,
    max_row_groups_before_rebuild
) AS MATERIALIZED (
    VALUES (
        false::boolean,           -- opt in only after reviewing schema scope
        'demand'::text,
        NULL::integer,
        900::integer,              -- at most once per 15 minutes
        4::integer,                -- at most four automatic runs/table/day
        0.50::double precision,    -- full rebuild at 50 percent drift
        16::integer                -- fold fragmented accelerators
    )
),
targets AS MATERIALIZED (
    SELECT t.table_oid,
           n.nspname AS schema_name,
           cls.relname AS table_name,
           t.generation_semantics AS previous_generation_semantics,
           t.history_policy AS previous_history_policy,
           policy.table_oid IS NOT NULL AS had_explicit_refresh_policy,
           to_jsonb(policy) AS existing_refresh_policy,
           wanted.seed_missing_refresh_policies,
           wanted.default_strategy,
           wanted.freshness_target_secs,
           wanted.min_interval_secs,
           wanted.daily_refresh_budget,
           wanted.full_rebuild_drift_ratio,
           wanted.max_row_groups_before_rebuild
      FROM rvbbit.tables t
      JOIN pg_catalog.pg_class cls
        ON cls.oid = t.table_oid
      JOIN pg_catalog.pg_namespace n
        ON n.oid = cls.relnamespace
      CROSS JOIN config wanted
      LEFT JOIN rvbbit.accel_policy policy
        ON policy.table_oid = t.table_oid
     WHERE cls.relkind IN ('r', 'p')
       AND t.history_policy IS DISTINCT FROM 'current'
),
storage_applied AS MATERIALIZED (
    SELECT target.*,
           rvbbit.set_acceleration_storage_policy(
               target.table_oid::regclass,
               history_policy => 'current'
           ) AS storage_policy_result
      FROM targets target
),
applied AS MATERIALIZED (
    SELECT target.*,
           CASE
               WHEN target.had_explicit_refresh_policy THEN
                   target.existing_refresh_policy
               WHEN target.seed_missing_refresh_policies THEN
                   rvbbit.set_accel_policy(
                       target.table_oid::regclass,
                       strategy => target.default_strategy,
                       freshness_target_secs => target.freshness_target_secs,
                       min_interval_secs => target.min_interval_secs,
                       daily_refresh_budget => target.daily_refresh_budget,
                       full_rebuild_drift_ratio =>
                           target.full_rebuild_drift_ratio,
                       max_row_groups_before_rebuild =>
                           target.max_row_groups_before_rebuild,
                       lance_separate => true,
                       active => true,
                       note => 'Server dashboard default: demand-driven and globally budgeted'
                   )
               ELSE NULL::jsonb
           END AS refresh_policy_result
      FROM storage_applied target
)
SELECT format('%I.%I', schema_name, table_name) AS qualified_table,
       previous_generation_semantics,
       previous_history_policy,
       storage_policy_result ->> 'history_policy' AS history_policy,
       coalesce((storage_policy_result ->> 'fold_required')::boolean, false)
           AS rebuild_recommended,
       storage_policy_result ->> 'note' AS storage_note,
       storage_policy_result -> 'prune' AS snapshot_history_pruned,
       CASE
           WHEN had_explicit_refresh_policy THEN 'preserved existing'
           WHEN seed_missing_refresh_policies THEN 'seeded dashboard default'
           ELSE 'left implicit manual'
       END AS refresh_policy_action,
       coalesce(refresh_policy_result ->> 'strategy', 'manual')
           AS refresh_strategy,
       (refresh_policy_result ->> 'min_interval_secs')::integer
           AS min_interval_secs,
       (refresh_policy_result ->> 'daily_refresh_budget')::integer
           AS daily_refresh_budget,
       (refresh_policy_result ->> 'max_row_groups_before_rebuild')::integer
           AS max_row_groups_before_rebuild
  FROM applied
 ORDER BY schema_name, table_name;

-- This intentionally does not change generation_semantics or
-- acceleration_enabled. Generation semantics describe the loader contract;
-- snapshot_load() sets snapshot mode when appropriate, and changing a
-- materialized table's mode blindly is unsafe. The storage-policy setter
-- immediately prunes superseded complete snapshots when safe. Cumulative
-- tables keep correctness deltas/tombstones until your later full rebuild.
--
-- Create ONE DataRabbit Scheduled Task for the whole database, initially hourly
-- at a non-round minute (cron: 7 * * * *). Do not create one task per table.
-- Budget 1 means at most one table is refreshed per heartbeat:
--
--     SELECT * FROM rvbbit.accel_tick(1, false);
--
-- Preview the next heartbeat without executing it:
--
--     SELECT * FROM rvbbit.accel_tick(1, true);
--
-- accel_tick is serialized cluster-wide. The hourly starting cadence also
-- bounds its per-table skip telemetry on large catalogs. Once observed rebuild
-- times and tick history are comfortable, move to every 15 minutes and/or raise
-- the per-heartbeat budget from 1 to 2. Avoid a one-minute cadence for a broad
-- legacy catalog until tick baselines are compacted independently of run history.
--
-- max_row_groups_before_rebuild is intentionally not enforced by accel_tick.
-- It is an off-hours fold threshold, so clean fragmented parquet remains
-- authoritative and routeable during the day. Preview that lane independently:
--
--     SELECT * FROM rvbbit.accel_fold_candidates WHERE fold_due;
--     SELECT * FROM rvbbit.accel_fold_tick(1, true);
--
-- Then schedule a deliberately small quiet-window budget, for example:
--
--     SELECT rvbbit.schedule_accel_fold_tick('17 3 * * 0', 1);
