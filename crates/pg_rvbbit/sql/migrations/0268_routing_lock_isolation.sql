-- 0268: Keep accelerator maintenance relation-local from the query router's
-- point of view.
--
-- The Duck/DataFusion sidecar previously proved catalog freshness by opening
-- every accelerated heap on every request. A long reader plus a queued
-- ACCESS EXCLUSIVE writer then caused PostgreSQL's fair lock queue to hold an
-- unrelated route probe behind that table. This append-only cursor is updated
-- transactionally by catalog mutations, so the sidecar can detect committed
-- changes by reading one small metadata index and refresh without touching the
-- accelerated heap relations themselves.

CREATE TABLE IF NOT EXISTS rvbbit.accel_catalog_changes (
    id          bigserial PRIMARY KEY,
    txid        xid8 NOT NULL UNIQUE,
    changed_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    source      text NOT NULL
);

CREATE INDEX IF NOT EXISTS accel_catalog_changes_time_idx
    ON rvbbit.accel_catalog_changes (changed_at DESC);

ALTER TABLE rvbbit.accel_catalog_changes SET (
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_threshold = 1000,
    autovacuum_analyze_scale_factor = 0.05,
    autovacuum_analyze_threshold = 1000
);

COMMENT ON TABLE rvbbit.accel_catalog_changes IS
    'Transactional append-only invalidation cursor for route executors. One committed row per metadata-changing transaction replaces synchronous all-relation fingerprints.';

CREATE OR REPLACE FUNCTION rvbbit._mark_accel_catalog_changed()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, rvbbit
AS $$
BEGIN
    INSERT INTO rvbbit.accel_catalog_changes (txid, source)
    VALUES (
        pg_current_xact_id(),
        format('%I.%I', TG_TABLE_SCHEMA, TG_TABLE_NAME)
    )
    ON CONFLICT (txid) DO UPDATE
       SET changed_at = clock_timestamp(),
           source = EXCLUDED.source;
    RETURN NULL;
END;
$$;

COMMENT ON FUNCTION rvbbit._mark_accel_catalog_changed() IS
    'Statement-trigger helper for the sidecar catalog cursor; changes become visible only when their originating transaction commits.';

-- ALTER TABLE and related DDL can change pg_attribute/pg_class state without
-- writing one of the accelerator metadata tables. Mark DDL transactions too;
-- the txid uniqueness constraint still collapses this to one cursor row.
CREATE OR REPLACE FUNCTION rvbbit._mark_accel_catalog_ddl_changed()
RETURNS event_trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, rvbbit
AS $$
BEGIN
    INSERT INTO rvbbit.accel_catalog_changes (txid, source)
    VALUES (
        pg_current_xact_id(),
        'ddl.' || lower(replace(TG_TAG, ' ', '_'))
    )
    ON CONFLICT (txid) DO UPDATE
       SET changed_at = clock_timestamp(),
           source = EXCLUDED.source;
END;
$$;

-- On a fresh install this migration is both extension-owned SQL and later
-- replayed by rvbbit.migrate(). PostgreSQL will not let the replay drop an
-- event trigger owned by the extension. The trigger already follows the
-- CREATE OR REPLACE function above, so only seed it when absent.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_event_trigger
         WHERE evtname = 'rvbbit_accel_catalog_ddl_changed'
    ) THEN
        CREATE EVENT TRIGGER rvbbit_accel_catalog_ddl_changed
            ON ddl_command_end
            EXECUTE FUNCTION rvbbit._mark_accel_catalog_ddl_changed();
    END IF;
END;
$$;

DO $$
DECLARE
    target text;
    trigger_name text;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'rvbbit.tables',
        'rvbbit.row_groups',
        'rvbbit.row_group_variants',
        'rvbbit.layout_variant_status',
        'rvbbit.delete_log',
        'rvbbit.table_dirty_markers',
        'rvbbit.acceleration_state'
    ] LOOP
        IF to_regclass(target) IS NULL THEN
            CONTINUE;
        END IF;
        trigger_name := 'accel_catalog_change_' || replace(target, '.', '_');
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %s', trigger_name, target);
        EXECUTE format(
            'CREATE TRIGGER %I AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s '
            'FOR EACH STATEMENT EXECUTE FUNCTION rvbbit._mark_accel_catalog_changed()',
            trigger_name,
            target
        );
    END LOOP;
END;
$$;

-- Give a newly upgraded sidecar a committed cursor even before the first
-- accelerator mutation. This row also invalidates any catalog snapshot held
-- across an extension upgrade.
INSERT INTO rvbbit.accel_catalog_changes (txid, source)
VALUES (pg_current_xact_id(), 'migration.0268')
ON CONFLICT (txid) DO UPDATE
   SET changed_at = clock_timestamp(),
       source = EXCLUDED.source;

-- A SQL function cannot commit between loop iterations, so a multi-table
-- accel_tick batch necessarily retains each table's transaction locks until
-- the whole statement ends. Put the one-table boundary at the public function,
-- not only in schedule_accel_tick: pg_cron is often installed in a different
-- database, where this migration cannot rewrite an existing accel_tick(4) job.
-- Preserve the instrumented implementation under an internal name, fixing its
-- PL/pgSQL output-variable qualifiers as part of the copy.
DO $$
DECLARE
    public_body text;
    core_definition text;
BEGIN
    SELECT p.prosrc
      INTO public_body
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'rvbbit'
       AND p.proname = 'accel_tick'
       AND p.proargtypes = '23 16 23'::oidvector;

    IF public_body IS NULL THEN
        RAISE EXCEPTION 'rvbbit.accel_tick(integer,boolean,integer) is required before migration 0268';
    END IF;

    -- A fresh migrate() may have just recreated the public transaction body
    -- after extension SQL already installed this wrapper. Refresh the private
    -- copy whenever the public body is not currently the wrapper. Use CREATE
    -- OR REPLACE instead of dropping the private function: on a fresh install
    -- it is already an extension-owned object, which PostgreSQL correctly
    -- refuses to drop while pg_rvbbit remains installed.
    IF position('rvbbit._accel_tick_batch(' IN public_body) = 0 THEN
        SELECT pg_get_functiondef(
                   'rvbbit.accel_tick(integer,boolean,integer)'::regprocedure
               )
          INTO core_definition;
        core_definition := replace(
            core_definition,
            'FUNCTION rvbbit.accel_tick(',
            'FUNCTION rvbbit._accel_tick_batch('
        );
        core_definition := replace(
            core_definition,
            'accel_tick.',
            '_accel_tick_batch.'
        );
        EXECUTE core_definition;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.accel_tick(
    budget integer DEFAULT NULL::integer,
    dry_run boolean DEFAULT false,
    lance_budget integer DEFAULT 1
)
RETURNS TABLE(
    table_oid oid,
    table_name text,
    strategy text,
    action text,
    reason text,
    drift_rows bigint,
    drift_ratio double precision,
    seconds_dirty double precision,
    heap_seq_scans bigint,
    executed boolean,
    status text,
    rows_written bigint,
    error text
)
LANGUAGE sql
VOLATILE
AS $$
    SELECT *
      FROM rvbbit._accel_tick_batch(
          CASE
              WHEN dry_run THEN budget
              WHEN coalesce(budget, 1) <= 0 THEN 0
              ELSE 1
          END,
          dry_run,
          lance_budget
      )
$$;

COMMENT ON FUNCTION rvbbit._accel_tick_batch(integer, boolean, integer) IS
    'Internal transaction body for accel_tick. Direct multi-table execution retains locks; call rvbbit.accel_tick instead.';

COMMENT ON FUNCTION rvbbit.accel_tick(integer, boolean, integer) IS
    'Lock-isolated freshness heartbeat. Execution is capped at one table per transaction even for legacy larger budgets; dry runs preserve the requested planning budget.';

-- New schedules also make the transaction boundary explicit. Frequent
-- heartbeats preserve throughput without cross-table lock accumulation.
CREATE OR REPLACE FUNCTION rvbbit.schedule_accel_tick(
    cron_schedule text DEFAULT '* * * * *',
    budget        integer DEFAULT 1
) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    jobid     bigint;
    cron_home text := current_setting('cron.database_name', true);
    this_db   text := current_database();
    safe_budget integer := CASE WHEN coalesce(budget, 1) <= 0 THEN 0 ELSE 1 END;
    command   text := format('SELECT rvbbit.accel_tick(%s, false)', safe_budget);
BEGIN
    IF cron_home IS NOT NULL AND cron_home <> '' AND cron_home <> this_db THEN
        RAISE EXCEPTION 'pg_cron home database is %, not %; cron.* is not callable here.',
            cron_home, this_db
            USING HINT = format(
                'Use the Scheduler UI, or connect to %L and run: '
                'SELECT cron.schedule_in_database(%L, %L, %L, %L);',
                cron_home, 'rvbbit_accel_tick', cron_schedule, command, this_db);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        RAISE EXCEPTION 'pg_cron is not installed; cannot schedule the accelerator heartbeat.'
            USING HINT = 'Add pg_cron to shared_preload_libraries and CREATE EXTENSION pg_cron, '
                         'or call rvbbit.accel_tick(1, false) manually.';
    END IF;
    EXECUTE format(
        'SELECT cron.schedule(%L, %L, %L)',
        'rvbbit_accel_tick', cron_schedule, command
    ) INTO jobid;
    RETURN jobid;
END;
$$;

COMMENT ON FUNCTION rvbbit.schedule_accel_tick(text, integer) IS
    'Schedules the high-frequency freshness lane one table per transaction. The budget argument remains for compatibility but positive values are clamped to one.';

-- Update an existing same-database pg_cron job where that catalog is visible.
-- When the cron home is another database the legacy command text may remain,
-- but the public accel_tick wrapper above still clamps it safely at execution.
DO $$
BEGIN
    IF to_regclass('cron.job') IS NOT NULL THEN
        UPDATE cron.job
           SET command = 'SELECT rvbbit.accel_tick(1, false)'
         WHERE jobname = 'rvbbit_accel_tick'
           AND command IS DISTINCT FROM 'SELECT rvbbit.accel_tick(1, false)';
    END IF;
EXCEPTION
    WHEN insufficient_privilege THEN
        RAISE NOTICE 'Could not update cron.job; reschedule rvbbit_accel_tick to SELECT rvbbit.accel_tick(1, false)';
END;
$$;
