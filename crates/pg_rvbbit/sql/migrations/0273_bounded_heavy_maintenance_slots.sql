-- 0273: Replace the singleton heavy-maintenance lock with a bounded pool.
--
-- Canonical delta refreshes remain outside this pool. Full rebuilds, separate
-- Lance construction, automatic Vortex, and accepted cluster/Hive layouts all
-- consume one shared slot. The global gate stays shared for these workers so
-- legacy exclusive maintenance still excludes the entire fleet safely.

CREATE TABLE IF NOT EXISTS rvbbit.accel_maintenance_config (
    singleton       boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    heavy_slots     integer NOT NULL DEFAULT 2
                        CHECK (heavy_slots BETWEEN 1 AND 8),
    updated_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_by      text NOT NULL DEFAULT session_user
);

INSERT INTO rvbbit.accel_maintenance_config (singleton, heavy_slots)
VALUES (true, 2)
ON CONFLICT (singleton) DO NOTHING;

COMMENT ON TABLE rvbbit.accel_maintenance_config IS
    'Singleton concurrency controls shared by accelerator freshness and derived-layout workers.';

CREATE OR REPLACE FUNCTION rvbbit.accel_maintenance_heavy_slots()
RETURNS integer
LANGUAGE sql
STABLE
AS $function$
    SELECT greatest(1, least(8, coalesce(
        (
            SELECT config.heavy_slots
              FROM rvbbit.accel_maintenance_config config
             WHERE config.singleton
        ),
        2
    )))::integer
$function$;

COMMENT ON FUNCTION rvbbit.accel_maintenance_heavy_slots() IS
    'Returns the shared 1..8 concurrency limit for full/Lance and derived-layout maintenance.';

CREATE OR REPLACE FUNCTION rvbbit.set_accel_maintenance_heavy_slots(
    slot_count integer
) RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $function$
DECLARE
    previous_count integer;
BEGIN
    IF slot_count NOT BETWEEN 1 AND 8 THEN
        RAISE EXCEPTION
            'heavy maintenance slot_count must be between 1 and 8 (got %)',
            slot_count;
    END IF;

    SELECT config.heavy_slots
      INTO previous_count
      FROM rvbbit.accel_maintenance_config config
     WHERE config.singleton
     FOR UPDATE;

    INSERT INTO rvbbit.accel_maintenance_config (
        singleton, heavy_slots, updated_at, updated_by
    ) VALUES (
        true, slot_count, clock_timestamp(), session_user
    )
    ON CONFLICT (singleton) DO UPDATE
       SET heavy_slots = EXCLUDED.heavy_slots,
           updated_at = EXCLUDED.updated_at,
           updated_by = EXCLUDED.updated_by;

    RETURN jsonb_build_object(
        'status', 'configured',
        'previous_heavy_slots', previous_count,
        'heavy_slots', slot_count,
        'updated_by', session_user
    );
END;
$function$;

COMMENT ON FUNCTION rvbbit.set_accel_maintenance_heavy_slots(integer) IS
    'Sets the database-wide heavy-maintenance concurrency shared by freshness and layout workers; new worker transactions observe it immediately.';

CREATE OR REPLACE FUNCTION rvbbit._try_claim_accel_heavy_slot(
    requested_limit integer DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql
VOLATILE
AS $function$
DECLARE
    slot_limit integer := greatest(
        1,
        least(
            8,
            coalesce(
                requested_limit,
                rvbbit.accel_maintenance_heavy_slots()
            )
        )
    );
    slot integer;
BEGIN
    PERFORM set_config('rvbbit.accel_maintenance_heavy_slot', '', true);
    PERFORM set_config(
        'rvbbit.accel_maintenance_heavy_slot_limit',
        slot_limit::text,
        true
    );

    -- Claim capacity before joining the shared global lane. A worker that
    -- finds the pool full therefore retains no gate lock while it steals
    -- cheaper delta work or exits.
    FOR slot IN 1..slot_limit LOOP
        IF pg_try_advisory_xact_lock(1381187156, 80 + slot) THEN
            -- Old variant_tick/fold-compatible paths may still hold this gate
            -- exclusively. New heavy workers hold it shared, preserving that
            -- exclusion while allowing the bounded cohort to coexist.
            IF NOT pg_try_advisory_xact_lock_shared(1381187156, 8) THEN
                RETURN 0;
            END IF;
            PERFORM set_config(
                'rvbbit.accel_maintenance_heavy_slot',
                slot::text,
                true
            );
            RETURN slot;
        END IF;
    END LOOP;

    RETURN 0;
END;
$function$;

COMMENT ON FUNCTION rvbbit._try_claim_accel_heavy_slot(integer) IS
    'Internal non-waiting transaction-scoped claim on one configured heavy-maintenance slot; returns zero when capacity or the global lane is unavailable.';

CREATE OR REPLACE VIEW rvbbit.accel_heavy_slot_activity AS
WITH config AS (
    SELECT rvbbit.accel_maintenance_heavy_slots() AS heavy_slots
), slots AS (
    SELECT generate_series(1, 8)::integer AS heavy_slot
), holders AS (
    SELECT (locks.objid::integer - 80) AS heavy_slot,
           locks.pid AS backend_pid,
           activity.datname AS database_name,
           activity.usename AS user_name,
           activity.application_name,
           activity.state,
           activity.query_start,
           activity.wait_event_type,
           activity.wait_event
      FROM pg_catalog.pg_locks locks
      LEFT JOIN pg_catalog.pg_stat_activity activity
        ON activity.pid = locks.pid
     WHERE locks.locktype = 'advisory'
       AND locks.granted
       AND locks.classid::bigint = 1381187156
       AND locks.objid::integer BETWEEN 81 AND 88
)
SELECT slots.heavy_slot,
       slots.heavy_slot <= config.heavy_slots AS enabled,
       CASE
           WHEN slots.heavy_slot > config.heavy_slots
            AND holders.backend_pid IS NOT NULL THEN 'draining'
           WHEN slots.heavy_slot > config.heavy_slots THEN 'disabled'
           WHEN holders.backend_pid IS NULL THEN 'free'
           ELSE 'busy'
       END AS status,
       holders.backend_pid,
       holders.database_name,
       holders.user_name,
       holders.application_name,
       holders.state,
       holders.query_start,
       holders.wait_event_type,
       holders.wait_event
  FROM slots
 CROSS JOIN config
  LEFT JOIN holders USING (heavy_slot)
 ORDER BY slots.heavy_slot;

COMMENT ON VIEW rvbbit.accel_heavy_slot_activity IS
    'Live state for all eight possible heavy-maintenance slots, including disabled, free, and owning backend details.';

-- Freshness workers: full/Lance actions and delta-to-full fallbacks use the
-- bounded pool. Deltas continue to bypass it.
DO $migration$
DECLARE
    definition text;
    needle text;
    replacement text;
    changed boolean := false;
BEGIN
    definition := pg_get_functiondef(
        'rvbbit._accel_tick_batch(integer,boolean,integer)'::regprocedure
    );

    IF position('rvbbit._try_claim_accel_heavy_slot' IN definition) = 0 THEN
        needle := 'NOT pg_try_advisory_xact_lock(1381187156, 8)';
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0273 could not find the freshness heavy-lane claims';
        END IF;
        definition := replace(
            definition,
            needle,
            'rvbbit._try_claim_accel_heavy_slot() = 0'
        );
        definition := replace(
            definition,
            'heavy maintenance lane busy',
            'heavy maintenance slots busy'
        );
        changed := true;
    END IF;

    IF position('heavy_slot_limit' IN definition) = 0 THEN
        needle := $needle$
                'tombstones', cand.tombstones
$needle$;
        replacement := $replacement$
                'tombstones', cand.tombstones,
                'heavy_slot', nullif(
                    current_setting(
                        'rvbbit.accel_maintenance_heavy_slot', true
                    ),
                    ''
                )::integer,
                'heavy_slot_limit',
                    rvbbit.accel_maintenance_heavy_slots()
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0273 could not annotate freshness table starts';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := $needle$
                'error', _accel_tick_batch.error,
                'result', res
$needle$;
        replacement := $replacement$
                'error', _accel_tick_batch.error,
                'result', res,
                'heavy_slot', nullif(
                    current_setting(
                        'rvbbit.accel_maintenance_heavy_slot', true
                    ),
                    ''
                )::integer,
                'heavy_slot_limit',
                    rvbbit.accel_maintenance_heavy_slots()
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0273 could not annotate freshness table finishes';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := $needle$
                'lance_tables_executed', lance_acted
$needle$;
        replacement := $replacement$
                'lance_tables_executed', lance_acted,
                'heavy_slot_limit',
                    rvbbit.accel_maintenance_heavy_slots()
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0273 could not annotate freshness sweep finishes';
        END IF;
        definition := replace(definition, needle, replacement);
        changed := true;
    END IF;

    IF changed THEN
        EXECUTE definition;
    END IF;
END;
$migration$;

COMMENT ON FUNCTION rvbbit._accel_tick_batch(integer, boolean, integer) IS
    'Internal one-transaction freshness body. Delta work runs across up to eight workers; full/Lance work consumes the shared configurable heavy-slot pool.';

-- Legacy serial Vortex entry point: retain compatibility, but participate in
-- the same pool instead of excluding every other heavy worker globally.
DO $migration$
DECLARE
    definition text;
    needle text;
    replacement text;
BEGIN
    -- This legacy function is migration-owned rather than part of the base
    -- extension SQL. CREATE EXTENSION therefore reaches 0273 before the first
    -- rvbbit.migrate() pass has created it; that later pass re-enters 0273 and
    -- applies this compatibility patch.
    IF to_regprocedure('rvbbit.variant_tick(integer,boolean)') IS NULL THEN
        RETURN;
    END IF;

    definition := pg_get_functiondef(
        'rvbbit.variant_tick(integer,boolean)'::regprocedure
    );
    IF position('rvbbit._try_claim_accel_heavy_slot' IN definition) = 0 THEN
        needle := $needle$
    IF NOT dry_run AND NOT pg_try_advisory_xact_lock(1381187156, 8) THEN
$needle$;
        replacement := $replacement$
    IF NOT dry_run
       AND rvbbit._try_claim_accel_heavy_slot() = 0 THEN
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0273 could not replace the legacy Vortex gate';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := $needle$
                    'source', 'canonical_parquet',
                    'target_generation', current_generation,
$needle$;
        replacement := $replacement$
                    'source', 'canonical_parquet',
                    'heavy_slot', nullif(
                        current_setting(
                            'rvbbit.accel_maintenance_heavy_slot', true
                        ),
                        ''
                    )::integer,
                    'heavy_slot_limit',
                        rvbbit.accel_maintenance_heavy_slots(),
                    'target_generation', current_generation,
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0273 could not annotate legacy Vortex operations';
        END IF;
        definition := replace(definition, needle, replacement);
        EXECUTE definition;
    END IF;

    EXECUTE $comment$
        COMMENT ON FUNCTION rvbbit.variant_tick(integer, boolean) IS
        'Compatibility Vortex queue consumer. New installs use layout_tick workers; direct legacy calls share the bounded heavy-maintenance pool.'
    $comment$;
END;
$migration$;

-- The pre-unification accepted-layout worker remains callable, so make it a
-- good citizen of the same capacity pool as its replacement.
DO $migration$
DECLARE
    definition text;
    needle text;
    replacement text;
BEGIN
    definition := pg_get_functiondef(
        'rvbbit.workload_layout_tick_worker(integer,integer,boolean)'
            ::regprocedure
    );
    IF position('rvbbit._try_claim_accel_heavy_slot' IN definition) = 0 THEN
        needle := $needle$
        IF NOT pg_try_advisory_xact_lock_shared(1381187156, 8) THEN
            RETURN;
        END IF;
$needle$;
        replacement := $replacement$
        IF rvbbit._try_claim_accel_heavy_slot() = 0 THEN
            RETURN;
        END IF;
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0273 could not replace the legacy layout gate';
        END IF;
        definition := replace(definition, needle, replacement);
        definition := replace(
            definition,
            $needle$                    'source', 'accepted_workload_layouts'
$needle$,
            $replacement$                    'source', 'accepted_workload_layouts',
                    'heavy_slot', nullif(
                        current_setting(
                            'rvbbit.accel_maintenance_heavy_slot', true
                        ),
                        ''
                    )::integer,
                    'heavy_slot_limit',
                    rvbbit.accel_maintenance_heavy_slots()
$replacement$
        );
        definition := replace(
            definition,
            $needle$        IF rvbbit._try_claim_accel_heavy_slot() = 0 THEN
            RETURN;
        END IF;
        IF NOT pg_try_advisory_xact_lock(1381187156, 90 + worker_slot) THEN
            RETURN;
        END IF;
$needle$,
            $replacement$        IF NOT pg_try_advisory_xact_lock(1381187156, 90 + worker_slot) THEN
            RETURN;
        END IF;
        IF rvbbit._try_claim_accel_heavy_slot() = 0 THEN
            RETURN;
        END IF;
$replacement$
        );
        EXECUTE definition;
    END IF;
END;
$migration$;

COMMENT ON FUNCTION rvbbit.workload_layout_tick_worker(integer, integer, boolean) IS
    'Legacy accepted-layout worker. Direct calls share the same bounded heavy pool as freshness, Vortex, and unified layout workers.';

-- Unified automatic Vortex + accepted layout fleet.
DO $migration$
DECLARE
    definition text;
    needle text;
    replacement text;
BEGIN
    definition := pg_get_functiondef(
        'rvbbit.layout_tick_worker(integer,integer,boolean)'::regprocedure
    );
    IF position('rvbbit._try_claim_accel_heavy_slot' IN definition) = 0 THEN
        needle := $needle$
        IF NOT pg_try_advisory_xact_lock_shared(1381187156, 8) THEN
            RETURN;
        END IF;
$needle$;
        replacement := $replacement$
        IF rvbbit._try_claim_accel_heavy_slot() = 0 THEN
            RETURN;
        END IF;
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0273 could not replace the unified layout gate';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := $needle$
            'source', 'unified_layout_tick'
$needle$;
        replacement := $replacement$
            'source', 'unified_layout_tick',
            'heavy_slot', nullif(
                current_setting(
                    'rvbbit.accel_maintenance_heavy_slot', true
                ),
                ''
            )::integer,
            'heavy_slot_limit', rvbbit.accel_maintenance_heavy_slots()
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0273 could not annotate unified layout runs';
        END IF;
        definition := replace(definition, needle, replacement);
        definition := replace(
            definition,
            $needle$        IF rvbbit._try_claim_accel_heavy_slot() = 0 THEN
            RETURN;
        END IF;
        -- Reuse the workload-layout slot keys so accidentally overlapping old
        -- and new scheduler jobs skip rather than multiply concurrency.
        IF NOT pg_try_advisory_xact_lock(1381187156, 90 + worker_slot) THEN
            RETURN;
        END IF;
$needle$,
            $replacement$        -- Reuse the workload-layout slot keys so accidentally overlapping old
        -- and new scheduler jobs skip rather than multiply concurrency.
        IF NOT pg_try_advisory_xact_lock(1381187156, 90 + worker_slot) THEN
            RETURN;
        END IF;
        IF rvbbit._try_claim_accel_heavy_slot() = 0 THEN
            RETURN;
        END IF;
$replacement$
        );
        EXECUTE definition;
    END IF;
END;
$migration$;

COMMENT ON FUNCTION rvbbit.layout_tick_worker(integer, integer, boolean) IS
    'Builds one table of automatic Vortex and/or accepted cluster/Hive targets while consuming one shared configurable heavy-maintenance slot.';

DO $migration$
DECLARE
    definition text;
    needle text;
    replacement text;
BEGIN
    definition := pg_get_functiondef(
        'rvbbit._build_automatic_vortex_target(oid)'::regprocedure
    );
    IF position('heavy_slot_limit' IN definition) = 0 THEN
        needle := $needle$
            'source', 'unified_layout_tick',
            'layout', 'vortex_scan',
$needle$;
        replacement := $replacement$
            'source', 'unified_layout_tick',
            'layout', 'vortex_scan',
            'heavy_slot', nullif(
                current_setting(
                    'rvbbit.accel_maintenance_heavy_slot', true
                ),
                ''
            )::integer,
            'heavy_slot_limit', rvbbit.accel_maintenance_heavy_slots(),
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0273 could not annotate automatic Vortex operations';
        END IF;
        definition := replace(definition, needle, replacement);
        EXECUTE definition;
    END IF;
END;
$migration$;
