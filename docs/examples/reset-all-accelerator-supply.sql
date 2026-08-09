-- Retire every currently materialized RVBBIT accelerator while preserving the
-- table registry and the evidence used by the workload observer.
--
-- Use this only for an intentional server-wide reset. Before running it, pause
-- every accel_tick, fold, variant, and layout worker. The observe-only
-- rvbbit.accel_autopilot_observe() heartbeat may remain enabled.
--
-- The reset is atomic:
--   * all explicit refresh policies become manual (other policy fields remain);
--   * every canonical, variant, and text-dictionary file is queued as orphaned;
--   * live accelerator manifests and derived metadata are cleared;
--   * registered tables remain enabled and continue to collect dirty markers;
--   * observer candidates, observations, and operation history are preserved.
--
-- Physical files are not unlinked in this transaction. The ordinary orphan
-- reaper removes them only after its MVCC grace period (30 minutes by default).

BEGIN;

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '10min';

-- Serialize with the legacy freshness executor and the workload observer. The
-- current parallel workers must still be paused before this script is run.
SELECT pg_advisory_xact_lock(1381187156, 7);
SELECT pg_advisory_xact_lock(1381187156, 9);

DO $guard$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM rvbbit.acceleration_operations
         WHERE status = 'running'
    ) THEN
        RAISE EXCEPTION
            'acceleration operations are still running; pause workers and wait for them to finish';
    END IF;

    -- Deleting a canonical file is safe only when PostgreSQL still holds the
    -- authoritative rows. A compacted parquet-authoritative table must first be
    -- restored to heap storage and is deliberately outside this bulk reset.
    IF EXISTS (
        SELECT 1
          FROM rvbbit.row_groups rg
          JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
         WHERE NOT coalesce(t.shadow_heap_retained, false)
    ) THEN
        RAISE EXCEPTION
            'one or more accelerators are parquet-authoritative; restore their heap rows before reset';
    END IF;

    -- The local reaper only unlinks filesystem paths. Do not silently strand
    -- separately published or cold-tier objects.
    IF EXISTS (
        SELECT 1
          FROM rvbbit.row_groups rg
         WHERE nullif(to_jsonb(rg)->>'cold_url', '') IS NOT NULL
            OR nullif(to_jsonb(rg)->>'published_url', '') IS NOT NULL
    ) THEN
        RAISE EXCEPTION
            'one or more row groups have cold/published URLs; retire those provider objects explicitly first';
    END IF;
END
$guard$;

CREATE TEMP TABLE rvbbit_accel_supply_reset_stats (
    metric text PRIMARY KEY,
    value  bigint NOT NULL
) ON COMMIT PRESERVE ROWS;

CREATE TEMP TABLE rvbbit_accel_supply_reset_paths
ON COMMIT PRESERVE ROWS AS
SELECT DISTINCT ON (path)
       path,
       table_oid
  FROM (
      SELECT rg.path, rg.table_oid
        FROM rvbbit.row_groups rg
      UNION ALL
      SELECT v.path, v.table_oid
        FROM rvbbit.row_group_variants v
      UNION ALL
      SELECT d.path, d.table_oid
        FROM rvbbit.text_dictionaries d
  ) AS files
 WHERE path IS NOT NULL
   AND btrim(path) <> ''
 ORDER BY path, table_oid;

INSERT INTO rvbbit_accel_supply_reset_stats (metric, value)
SELECT 'registered_tables', count(*) FROM rvbbit.tables
UNION ALL
SELECT 'built_tables', count(DISTINCT table_oid) FROM rvbbit.row_groups
UNION ALL
SELECT 'canonical_row_groups', count(*) FROM rvbbit.row_groups
UNION ALL
SELECT 'canonical_bytes', coalesce(sum(n_bytes), 0) FROM rvbbit.row_groups
UNION ALL
SELECT 'variant_files', count(*) FROM rvbbit.row_group_variants
UNION ALL
SELECT 'variant_bytes', coalesce(sum(n_bytes), 0) FROM rvbbit.row_group_variants
UNION ALL
SELECT 'dictionary_files', count(*) FROM rvbbit.text_dictionaries
UNION ALL
SELECT 'dictionary_bytes', coalesce(sum(n_bytes), 0) FROM rvbbit.text_dictionaries
UNION ALL
SELECT 'unique_paths_queued', count(*) FROM rvbbit_accel_supply_reset_paths;

WITH changed AS (
    UPDATE rvbbit.accel_policy
       SET strategy = 'manual',
           note = concat_ws(
               ' | ',
               nullif(note, ''),
               'Operator reset: accelerator supply retired for workload re-selection'
           ),
           updated_at = clock_timestamp()
     WHERE strategy <> 'manual'
     RETURNING 1
)
INSERT INTO rvbbit_accel_supply_reset_stats (metric, value)
SELECT 'policies_changed_to_manual', count(*) FROM changed;

WITH queued AS (
    INSERT INTO rvbbit.orphaned_files (
        path, table_oid, reason, operation_id
    )
    SELECT path,
           table_oid,
           'operator_acceleration_supply_reset',
           NULL::bigint
      FROM rvbbit_accel_supply_reset_paths
    ON CONFLICT (path) DO UPDATE
       SET table_oid = EXCLUDED.table_oid,
           reason = EXCLUDED.reason,
           operation_id = NULL,
           queued_at = clock_timestamp(),
           attempts = 0,
           last_attempt_at = NULL,
           last_error = NULL
    RETURNING 1
)
INSERT INTO rvbbit_accel_supply_reset_stats (metric, value)
SELECT 'orphan_rows_queued_or_refreshed', count(*) FROM queued;

-- Listing every dependent table keeps the blast radius explicit and avoids a
-- broad TRUNCATE ... CASCADE. Historical operation/observer tables are not in
-- this list and therefore survive the reset.
TRUNCATE TABLE
    rvbbit.group_stats,
    rvbbit.column_bitmaps,
    rvbbit.text_dictionaries,
    rvbbit.row_identity_map,
    rvbbit.row_group_variants,
    rvbbit.semantic_bitmaps,
    rvbbit.delete_log,
    rvbbit.generations,
    rvbbit.acceleration_state,
    rvbbit.layout_variant_status,
    rvbbit.variant_build_queue,
    rvbbit.table_dirty_markers,
    rvbbit.hot_objects,
    rvbbit.row_groups;

-- The heap is now the only source. A future selected table has an explicit
-- baseline-missing state and rebuild_acceleration() will publish a fresh
-- generation from its current heap contents.
UPDATE rvbbit.tables
   SET min_visible_generation = 0,
       shadow_heap_dirty = false,
       dirty_has_insert = false,
       dirty_has_update = false,
       dirty_has_delete = false,
       dirty_has_truncate = false,
       dirty_since = NULL,
       ctid_identity_relfilenode = NULL;

COMMIT;

SELECT metric, value
  FROM rvbbit_accel_supply_reset_stats
 ORDER BY metric;

SELECT count(*) AS live_row_groups,
       count(DISTINCT table_oid) AS live_accelerated_tables
  FROM rvbbit.row_groups;

SELECT count(*) AS registered_tables,
       count(*) FILTER (WHERE acceleration_enabled) AS enabled_registered_tables
  FROM rvbbit.tables;

DROP TABLE rvbbit_accel_supply_reset_paths;
DROP TABLE rvbbit_accel_supply_reset_stats;

-- Refresh the observer projection after the reset (or wait for its next cron
-- heartbeat). This call is observe-only and never builds a table:
--
--     SELECT rvbbit.accel_autopilot_observe('post-supply-reset');
--
-- Cherry-pick one table and keep it current after recurring loads. `scheduled`
-- means the shared accel_tick worker acts whenever committed DML marks the
-- table dirty; it does not create a cron job per table. A committed TRUNCATE
-- forces a full replacement even if the reload writes the same number of rows.
-- Twelve runs/day leaves headroom for a roughly three-hour load cadence:
--
--     WITH baseline AS MATERIALIZED (
--         SELECT rvbbit.rebuild_acceleration(
--             'schema.table_name'::regclass,
--             true
--         ) AS result
--     ), policy AS MATERIALIZED (
--         SELECT rvbbit.set_accel_policy(
--             'schema.table_name'::regclass,
--             strategy => 'scheduled',
--             freshness_target_secs => NULL,
--             min_interval_secs => 900,
--             daily_refresh_budget => 12,
--             full_rebuild_drift_ratio => 0.5,
--             lance_separate => true,
--             active => true,
--             note => 'Refresh after committed truncate-loads'
--         ) AS result
--         FROM baseline
--     )
--     SELECT baseline.result AS baseline, policy.result AS policy
--       FROM baseline CROSS JOIN policy;
--
-- Keep TRUNCATE and the replacement load in one transaction so a heartbeat can
-- never observe and publish the intentionally empty between-load state. One
-- shared accel_tick worker fleet must be active for scheduled policies to run.
--
-- Once the 30-minute grace has elapsed, unlink in bounded batches. This is a
-- dedicated cleanup action; broad rvbbit.maintain_storage() need not be enabled:
--
--     SELECT * FROM rvbbit.reap_orphaned_files('30 minutes', 1000);
