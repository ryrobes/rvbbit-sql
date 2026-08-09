-- Enroll every live RVBBIT table that already has current accelerator files
-- but still has an effective `manual` refresh policy into the automatic
-- freshness executor. This is ordinary PostgreSQL SQL and can be run directly
-- in DataRabbit; no psql variables or backslash commands are required.
--
-- Reporting-oriented defaults:
--   * target: become eligible after being dirty for 15 minutes;
--   * never refresh one table more often than every 15 minutes;
--   * cap one table at eight automatic refreshes per rolling 24 hours;
--   * rebuild fully at 50 percent dirty drift in the freshness lane;
--   * mark 16 current row groups as eligible for the separate off-hours fold lane.
--
-- The single global accel_tick heartbeat supplies the server-wide concurrency
-- limit. Existing engine/layout deny lists, time-travel retention, and any
-- more-conservative per-table intervals are preserved. Tables in
-- rvbbit.accel_exclude_schemas (normally `cubes`) are intentionally skipped
-- because another lifecycle owns them.
WITH config(
    strategy,
    freshness_target_secs,
    min_interval_secs,
    daily_refresh_budget,
    full_rebuild_drift_ratio,
    max_row_groups_before_rebuild
) AS MATERIALIZED (
    VALUES (
        'target'::text,
        900::integer,
        900::integer,
        8::integer,
        0.50::double precision,
        16::integer
    )
),
targets AS MATERIALIZED (
    SELECT t.table_oid,
           n.nspname AS schema_name,
           cls.relname AS table_name,
           built.row_groups,
           built.accelerated_rows,
           built.accelerated_bytes,
           effective.strategy AS previous_strategy,
           effective.explicit AS previous_policy_explicit,
           effective.active AS previous_policy_active,
           policy.note AS previous_note,
           wanted.strategy AS desired_strategy,
           coalesce(
               policy.freshness_target_secs,
               wanted.freshness_target_secs
           ) AS desired_freshness_target_secs,
           greatest(
               coalesce(policy.min_interval_secs, wanted.min_interval_secs),
               wanted.min_interval_secs
           ) AS desired_min_interval_secs,
           coalesce(
               policy.daily_refresh_budget,
               wanted.daily_refresh_budget
           ) AS desired_daily_refresh_budget,
           coalesce(
               policy.full_rebuild_drift_ratio,
               wanted.full_rebuild_drift_ratio
           ) AS desired_full_rebuild_drift_ratio,
           coalesce(
               policy.max_row_groups_before_rebuild,
               wanted.max_row_groups_before_rebuild
           ) AS desired_max_row_groups_before_rebuild,
           policy.max_tombstones_before_rebuild
               AS desired_max_tombstones_before_rebuild,
           coalesce(policy.lance_separate, true) AS desired_lance_separate
      FROM rvbbit.tables t
      JOIN pg_catalog.pg_class cls
        ON cls.oid = t.table_oid
      JOIN pg_catalog.pg_namespace n
        ON n.oid = cls.relnamespace
      JOIN rvbbit.accel_policy_effective effective
        ON effective.table_oid = t.table_oid
      LEFT JOIN rvbbit.accel_policy policy
        ON policy.table_oid = t.table_oid
      CROSS JOIN config wanted
      JOIN LATERAL (
          SELECT count(*)::bigint AS row_groups,
                 sum(rg.n_rows)::bigint AS accelerated_rows,
                 sum(rg.n_bytes)::bigint AS accelerated_bytes
            FROM rvbbit.row_groups rg
           WHERE rg.table_oid = t.table_oid
             AND (
                 t.min_visible_generation = 0
                 OR rg.generation = t.min_visible_generation
             )
          HAVING count(*) > 0
      ) built ON true
     WHERE coalesce(t.acceleration_enabled, true)
       AND cls.relkind IN ('r', 'p')
       AND effective.strategy = 'manual'
       AND NOT (
           n.nspname = ANY (rvbbit._accel_excluded_schemas())
       )
),
applied AS MATERIALIZED (
    SELECT target.*,
           rvbbit.set_accel_policy(
               target.table_oid::regclass,
               strategy => target.desired_strategy,
               freshness_target_secs =>
                   target.desired_freshness_target_secs,
               min_interval_secs => target.desired_min_interval_secs,
               daily_refresh_budget => target.desired_daily_refresh_budget,
               full_rebuild_drift_ratio =>
                   target.desired_full_rebuild_drift_ratio,
               max_row_groups_before_rebuild =>
                   target.desired_max_row_groups_before_rebuild,
               max_tombstones_before_rebuild =>
                   target.desired_max_tombstones_before_rebuild,
               lance_separate => target.desired_lance_separate,
               active => true,
               note => concat_ws(
                   ' | ',
                   nullif(target.previous_note, ''),
                   'Reporting autopilot: target refresh for an existing accelerator'
               )
           ) AS policy_result
      FROM targets target
)
SELECT format('%I.%I', schema_name, table_name) AS qualified_table,
       pg_size_pretty(accelerated_bytes) AS accelerator_size,
       accelerated_rows,
       row_groups,
       previous_strategy,
       previous_policy_explicit,
       previous_policy_active,
       policy_result ->> 'strategy' AS refresh_strategy,
       (policy_result ->> 'freshness_target_secs')::integer
           AS freshness_target_secs,
       (policy_result ->> 'min_interval_secs')::integer
           AS min_interval_secs,
       (policy_result ->> 'daily_refresh_budget')::integer
           AS daily_refresh_budget,
       (policy_result ->> 'max_row_groups_before_rebuild')::integer
           AS max_row_groups_before_rebuild
  FROM applied
 ORDER BY accelerated_bytes DESC, schema_name, table_name;

-- Policy enrollment needs one bounded global worker SET; do not create one cron
-- job per table. From DataRabbit Scheduled Tasks, start with three distinct
-- jobs every 15 minutes at a non-round minute (cron: 7,22,37,52 * * * *):
-- replace/disable the old `rvbbit_accel_tick` singleton job, then add:
--
--     CALL rvbbit.accel_tick_worker_pass(1, 3, 4, 1);
--     CALL rvbbit.accel_tick_worker_pass(2, 3, 4, 1);
--     CALL rvbbit.accel_tick_worker_pass(3, 3, 4, 1);
--
-- Each pass handles up to four tables, but commits after every table. The
-- workers share the freshness lane, prefer their own stable table partition,
-- and steal unlocked work. Delta refreshes can use every worker. Full/Lance
-- and derived-layout work share two heavy slots by default, so one long full
-- rebuild no longer collapses the fleet to one worker. Preview each worker
-- without doing any work:
--
--     SELECT * FROM rvbbit.accel_tick_worker(1, 3, true, 1);
--     SELECT * FROM rvbbit.accel_tick_worker(2, 3, true, 1);
--     SELECT * FROM rvbbit.accel_tick_worker(3, 3, true, 1);
--
-- If pg_cron lives in this database, the equivalent helper is:
--
--     SELECT rvbbit.schedule_accel_tick_workers('7,22,37,52 * * * *', 3);
--
-- The pass size defaults to four. To choose another value (1..16), use:
--
--     SELECT rvbbit.schedule_accel_tick_worker_passes(
--         '7,22,37,52 * * * *', 3, 6
--     );
--
-- When pg_cron has a different home database (the packaged default is
-- `postgres`), use Scheduled Tasks; the helper will also raise an error with
-- the exact cron.schedule_in_database(...) command needed there.
--
-- Inspect or tune the concurrency shared by full/Lance, Vortex, and accepted
-- cluster/Hive layout builds. One is the old conservative behavior; two is the
-- default; raise it only when the server has matching I/O and CPU headroom:
--
--     SELECT rvbbit.accel_maintenance_heavy_slots();
--     SELECT * FROM rvbbit.accel_heavy_slot_activity;
--     SELECT rvbbit.set_accel_maintenance_heavy_slots(2);
--
-- Vortex plus accepted cluster/Hive layouts use a separate unified worker
-- fleet. Three minute workers, each committing up to four tables, are:
--
--     CALL rvbbit.layout_tick_worker_pass(1, 3, 4);
--     CALL rvbbit.layout_tick_worker_pass(2, 3, 4);
--     CALL rvbbit.layout_tick_worker_pass(3, 3, 4);
--
-- Or, when pg_cron lives in this database:
--
--     SELECT rvbbit.schedule_layout_tick_workers('* * * * *', 3);
--
-- That helper retires the old serial `rvbbit_variant_tick` job and prior
-- workload-layout workers so only one fleet owns derived-layout work.
--
-- Clean fragmentation no longer forces a long rebuild in that frequent
-- heartbeat. Inspect and preview the independent maintenance lane with:
--
--     SELECT * FROM rvbbit.accel_fold_candidates WHERE fold_due;
--     SELECT * FROM rvbbit.accel_fold_tick(1, true);
--
-- A conservative weekly fold (one table/transaction, Sunday 03:17 in the
-- pg_cron timezone) can be installed with:
--
--     SELECT rvbbit.schedule_accel_fold_tick('17 3 * * 0', 1);
