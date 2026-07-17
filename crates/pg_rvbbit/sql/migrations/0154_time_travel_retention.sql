-- 0154_time_travel_retention.sql
-- Bounded time-travel history by DEFAULT. Generations accumulate forever on
-- refresh-in-place tables (a month of 2-hourly cube refreshes = hundreds of
-- generations of parquet nobody will ever scrub back to). Field evidence:
-- a 1,683-table ETL deployment reached 53GB of delete_log and 184 retained
-- generations on one cube before anyone noticed.
--
-- Policy resolution, per table:
--   accel_policy.time_travel_keep_days  (NULL = inherit global)
--   -> settings key 'time_travel_keep_days'  (absent = 30)
--   -> 0 (or negative) at either level = unbounded (the old behavior)
--
-- Enforcement rides the hourly maintain_storage() sweep as its own phase,
-- BEFORE prune_delete_log (which exists to drop tombstones stranded by
-- exactly this reaping). reap_generations itself is double-gated: it only
-- touches generations BELOW the table's min_visible_generation floor
-- (already unreachable by any AS-OF) AND older than the cutoff.

-- Per-table override. NULL = inherit the global setting; 0 = unbounded.
ALTER TABLE rvbbit.accel_policy
    ADD COLUMN IF NOT EXISTS time_travel_keep_days integer;

COMMENT ON COLUMN rvbbit.accel_policy.time_travel_keep_days IS
    'Days of time-travel history to retain for this table. NULL = inherit the '
    'time_travel_keep_days setting (default 30). 0 = keep everything (unbounded).';

-- Ergonomic setter (the System Health window and docs point here).
CREATE OR REPLACE FUNCTION rvbbit.set_time_travel_retention(
    reloid regclass,
    keep_days integer
) RETURNS jsonb
LANGUAGE plpgsql AS $$
BEGIN
    IF keep_days IS NOT NULL AND keep_days < 0 THEN
        RAISE EXCEPTION 'keep_days must be NULL (inherit), 0 (unbounded), or positive — got %', keep_days;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM rvbbit.tables WHERE table_oid = reloid::oid) THEN
        RAISE EXCEPTION '% is not an rvbbit-registered table (no acceleration -> no time-travel history to bound)', reloid;
    END IF;
    INSERT INTO rvbbit.accel_policy (table_oid, time_travel_keep_days)
    VALUES (reloid::oid, keep_days)
    ON CONFLICT (table_oid) DO UPDATE
       SET time_travel_keep_days = EXCLUDED.time_travel_keep_days,
           updated_at = clock_timestamp();
    RETURN jsonb_build_object(
        'table', reloid::text,
        'time_travel_keep_days', keep_days,
        'effective', coalesce(
            keep_days,
            coalesce((SELECT (value #>> '{}')::int FROM rvbbit.settings
                      WHERE key = 'time_travel_keep_days'), 30))
    );
END $$;

-- Policy-driven sweep: reap each table according to ITS effective retention.
-- Cheap when idle — the EXISTS pre-check skips tables with nothing reapable,
-- and reap_generations does metadata deletes only (files go through the
-- orphaned-file reaper's grace period, never unlinked in-txn).
CREATE OR REPLACE FUNCTION rvbbit.reap_generations_by_policy()
RETURNS TABLE (relname text, keep_days integer, generations_reaped bigint,
               row_groups_reaped bigint, files_unlinked integer)
LANGUAGE plpgsql AS $$
DECLARE
    default_keep integer := coalesce((
        SELECT (value #>> '{}')::int FROM rvbbit.settings
        WHERE key = 'time_travel_keep_days'), 30);
    rec record;
BEGIN
    FOR rec IN
        SELECT t.table_oid::regclass AS rel,
               coalesce(ap.time_travel_keep_days, default_keep) AS keep
        FROM rvbbit.tables t
        LEFT JOIN rvbbit.accel_policy ap ON ap.table_oid = t.table_oid
        WHERE t.min_visible_generation > 0
          AND coalesce(ap.time_travel_keep_days, default_keep) > 0
          AND EXISTS (
              SELECT 1 FROM rvbbit.generations g
              WHERE g.table_oid = t.table_oid
                AND g.generation < t.min_visible_generation
                AND g.committed_at < now() - make_interval(
                        days => coalesce(ap.time_travel_keep_days, default_keep)))
    LOOP
        RETURN QUERY
        SELECT r.relname, rec.keep, r.generations_reaped, r.row_groups_reaped, r.files_unlinked
        FROM rvbbit.reap_generations(rec.rel, rec.keep) r;
    END LOOP;
END $$;

-- Re-issue maintain_storage with the retention phase (the canonical copy
-- in extension_sql only applies to fresh installs).
CREATE OR REPLACE FUNCTION rvbbit.maintain_storage(
    max_tables bigint DEFAULT 4,
    refresh_variants boolean DEFAULT true
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    rec record;
    n bigint;
    compacted jsonb := '[]'::jsonb;
    refreshed jsonb := '[]'::jsonb;
    errors jsonb := '[]'::jsonb;
    logs_reaped jsonb := '[]'::jsonb;
    orphaned_files_reaped jsonb := '{}'::jsonb;
    tombstones_pruned jsonb := '[]'::jsonb;
    generations_reaped_j jsonb := '[]'::jsonb;
    cap bigint := greatest(coalesce(max_tables, 0), 0);
BEGIN
    IF cap = 0 THEN
        RETURN jsonb_build_object(
            'compacted', compacted,
            'refreshed_variants', refreshed,
            'errors', errors,
            'skipped', 'max_tables is zero'
        );
    END IF;

    -- Keep per-table autovacuum tuning asserted (no-op once applied; picks up
    -- tables that bootstrap lazily after install). Never stomps operator
    -- overrides — see rvbbit.tune_metadata_autovacuum.
    BEGIN
        PERFORM rvbbit.tune_metadata_autovacuum();
    EXCEPTION WHEN OTHERS THEN
        errors := errors || jsonb_build_array(
            jsonb_build_object('phase', 'tune_metadata_autovacuum', 'error', SQLERRM)
        );
    END;

    FOR rec IN
        SELECT t.table_oid::regclass AS rel
        FROM rvbbit.tables t
        JOIN rvbbit.table_dirty_state ds ON ds.table_oid = t.table_oid
        JOIN pg_class c ON c.oid = t.table_oid
        WHERE ds.shadow_heap_dirty
        ORDER BY t.created_at
        LIMIT cap
    LOOP
        BEGIN
            SELECT count(*) INTO n FROM rvbbit.compact(rec.rel);
            compacted := compacted || jsonb_build_array(
                jsonb_build_object('table', rec.rel::text, 'row_groups', n)
            );
        EXCEPTION WHEN OTHERS THEN
            errors := errors || jsonb_build_array(
                jsonb_build_object('table', rec.rel::text, 'phase', 'compact', 'error', SQLERRM)
            );
        END;
    END LOOP;

    IF refresh_variants THEN
        FOR rec IN
            WITH candidates AS (
                SELECT
                    t.table_oid,
                    t.table_oid::regclass AS rel,
                    coalesce(max(rg.created_at), '-infinity'::timestamptz) AS newest_rg,
                    coalesce(max(rgv.created_at), '-infinity'::timestamptz) AS newest_variant,
                    count(rg.*) AS row_groups,
                    count(rgv.*) AS variants
                FROM rvbbit.tables t
                JOIN pg_class c ON c.oid = t.table_oid
                LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = t.table_oid
                LEFT JOIN rvbbit.row_group_variants rgv ON rgv.table_oid = t.table_oid
                GROUP BY t.table_oid
            )
            SELECT rel
            FROM candidates
            WHERE row_groups > 0
              AND (variants = 0 OR newest_variant < newest_rg)
            ORDER BY newest_rg DESC
            LIMIT cap
        LOOP
            BEGIN
                SELECT rvbbit.refresh_layout_variants(rec.rel) INTO n;
                refreshed := refreshed || jsonb_build_array(
                    jsonb_build_object('table', rec.rel::text, 'variants', n)
                );
            EXCEPTION WHEN OTHERS THEN
                errors := errors || jsonb_build_array(
                    jsonb_build_object('table', rec.rel::text, 'phase', 'refresh_variants', 'error', SQLERRM)
                );
            END;
        END LOOP;
    END IF;

    -- resources-02/ops-02: trim the append-only telemetry logs on the same
    -- maintenance heartbeat. Isolated so a reap failure never fails maintenance.
    BEGIN
        SELECT coalesce(
                   jsonb_agg(jsonb_build_object('table', table_name, 'rows', rows_reaped)),
                   '[]'::jsonb)
          INTO logs_reaped
          FROM rvbbit.reap_logs();
    EXCEPTION WHEN OTHERS THEN
        errors := errors || jsonb_build_array(
            jsonb_build_object('phase', 'reap_logs', 'error', SQLERRM)
        );
    END;

    -- Time-travel retention: reap generations past each table's policy
    -- (accel_policy.time_travel_keep_days, else the time_travel_keep_days
    -- setting, default 30; 0 = unbounded). Runs BEFORE prune_delete_log so
    -- tombstones stranded by this sweep's reaps are pruned in the same pass.
    BEGIN
        SELECT coalesce(
                   jsonb_agg(jsonb_build_object(
                       'table', g.relname, 'keep_days', g.keep_days,
                       'generations', g.generations_reaped)),
                   '[]'::jsonb)
          INTO generations_reaped_j
          FROM rvbbit.reap_generations_by_policy() AS g;
    EXCEPTION WHEN OTHERS THEN
        errors := errors || jsonb_build_array(
            jsonb_build_object('phase', 'reap_generations_by_policy', 'error', SQLERRM)
        );
    END;

    -- Drop tombstones stranded by generation reaping or trigger-less table
    -- drops. Cost scales with distinct row groups, not tombstone count.
    BEGIN
        SELECT coalesce(
                   jsonb_agg(jsonb_build_object('table', table_name, 'tombstones', p.tombstones_pruned)),
                   '[]'::jsonb)
          INTO tombstones_pruned
          FROM rvbbit.prune_delete_log() AS p;
    EXCEPTION WHEN OTHERS THEN
        errors := errors || jsonb_build_array(
            jsonb_build_object('phase', 'prune_delete_log', 'error', SQLERRM)
        );
    END;

    -- Reap old accelerator files only after their metadata swap has committed
    -- and aged past the grace period. This protects readers that planned
    -- against the previous row-group set while a fold was committing — and,
    -- once remote engine workers exist, files a warren may be mid-scan on:
    -- the grace (settings key reap_grace_minutes, default 30) must exceed the
    -- max remote query time.
    BEGIN
        SELECT to_jsonb(r)
          INTO orphaned_files_reaped
          FROM rvbbit.reap_orphaned_files(
              make_interval(mins => coalesce((
                  SELECT (value #>> '{}')::int FROM rvbbit.settings
                  WHERE key = 'reap_grace_minutes'), 30))
          ) AS r;
    EXCEPTION WHEN OTHERS THEN
        errors := errors || jsonb_build_array(
            jsonb_build_object('phase', 'reap_orphaned_files', 'error', SQLERRM)
        );
    END;

    RETURN jsonb_build_object(
        'compacted', compacted,
        'refreshed_variants', refreshed,
        'logs_reaped', logs_reaped,
        'tombstones_pruned', tombstones_pruned,
        'generations_reaped', generations_reaped_j,
        'orphaned_files_reaped', coalesce(orphaned_files_reaped, '{}'::jsonb),
        'errors', errors
    );
END $$;
