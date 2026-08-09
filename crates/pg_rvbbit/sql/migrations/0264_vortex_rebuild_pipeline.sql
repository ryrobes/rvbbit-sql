-- 0264: Make Vortex a first-class derived stage of canonical acceleration.
--
-- Canonical parquet remains authoritative.  Vortex is encoded from those
-- files beside the active layout, validated, and metadata-swapped atomically.
-- A coalescing queue repairs legacy gaps and rare encoder failures without the
-- hourly maintain(storage_tables => N) bottleneck.

ALTER TABLE rvbbit.row_group_variants
    ADD COLUMN IF NOT EXISTS build_operation_id bigint;

ALTER TABLE rvbbit.acceleration_operations
    DROP CONSTRAINT IF EXISTS acceleration_operations_operation_check;
ALTER TABLE rvbbit.acceleration_operations
    ADD CONSTRAINT acceleration_operations_operation_check
    CHECK (operation IN (
        'refresh_acceleration', 'rebuild_acceleration',
        'compact_acceleration', 'legacy_compact', 'variant_build'
    ));

CREATE INDEX IF NOT EXISTS row_group_variants_build_operation_idx
    ON rvbbit.row_group_variants (table_oid, build_operation_id)
    WHERE build_operation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS rvbbit.variant_build_queue (
    table_oid          oid PRIMARY KEY REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    target_generation  bigint NOT NULL DEFAULT 0,
    target_row_groups  bigint NOT NULL DEFAULT 0,
    target_rows        bigint NOT NULL DEFAULT 0,
    target_bytes       bigint NOT NULL DEFAULT 0,
    reason             text NOT NULL DEFAULT 'repair',
    priority           integer NOT NULL DEFAULT 0,
    requested_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    available_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    attempts           integer NOT NULL DEFAULT 0,
    last_started_at    timestamptz,
    last_finished_at   timestamptz,
    last_error         text,
    CHECK (target_generation >= 0),
    CHECK (target_row_groups >= 0),
    CHECK (target_rows >= 0),
    CHECK (target_bytes >= 0),
    CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS variant_build_queue_ready_idx
    ON rvbbit.variant_build_queue (priority DESC, available_at, requested_at);

CREATE TABLE IF NOT EXISTS rvbbit.variant_build_runs (
    id                 bigserial PRIMARY KEY,
    table_oid          oid REFERENCES rvbbit.tables(table_oid) ON DELETE SET NULL,
    table_name         text NOT NULL,
    target_generation  bigint,
    target_row_groups  bigint,
    target_bytes       bigint,
    action             text NOT NULL,
    status             text NOT NULL,
    rows_written       bigint,
    started_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    error              text
);

CREATE INDEX IF NOT EXISTS variant_build_runs_table_time_idx
    ON rvbbit.variant_build_runs (table_oid, started_at DESC);

ALTER TABLE rvbbit.variant_build_runs SET (
    autovacuum_vacuum_scale_factor = 0.01,
    autovacuum_vacuum_threshold = 1000,
    autovacuum_analyze_scale_factor = 0.02,
    autovacuum_analyze_threshold = 1000
);

ALTER TABLE rvbbit.variant_build_queue SET (
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_threshold = 50,
    autovacuum_analyze_scale_factor = 0.05,
    autovacuum_analyze_threshold = 50,
    toast.autovacuum_vacuum_scale_factor = 0.02,
    toast.autovacuum_vacuum_threshold = 50
);

CREATE OR REPLACE FUNCTION rvbbit.reap_logs(max_age interval DEFAULT interval '14 days')
RETURNS TABLE (table_name text, rows_reaped bigint)
LANGUAGE plpgsql AS $$
DECLARE
    spec record;
    cutoff timestamptz := now() - max_age;
    n bigint;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            ('rvbbit.accel_tick_runs',    'ran_at'),
            ('rvbbit.route_decisions',    'decided_at'),
            ('rvbbit.route_executions',   'executed_at'),
            ('rvbbit.variant_build_runs', 'started_at'),
            ('rvbbit.mcp_invocations',    'invocation_at'),
            ('rvbbit.cost_events',        'created_at'),
            ('rvbbit.sync_runs',          'started_at'),
            ('rvbbit.receipts',           'invocation_at')
        ) AS t(tbl, col)
    LOOP
        IF to_regclass(spec.tbl) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('DELETE FROM %s WHERE %I < $1', spec.tbl, spec.col) USING cutoff;
        GET DIAGNOSTICS n = ROW_COUNT;
        table_name := spec.tbl;
        rows_reaped := n;
        RETURN NEXT;
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION rvbbit.enqueue_variant_build(
    rel regclass,
    build_reason text DEFAULT 'repair',
    build_priority integer DEFAULT 0
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    generation_now bigint;
    row_groups_now bigint;
    rows_now bigint;
    bytes_now bigint;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(rel) THEN
        RAISE EXCEPTION '% is not an rvbbit table', rel;
    END IF;

    SELECT coalesce(max(generation), 0)::bigint,
           count(*)::bigint,
           coalesce(sum(n_rows), 0)::bigint,
           coalesce(sum(n_bytes), 0)::bigint
      INTO generation_now, row_groups_now, rows_now, bytes_now
      FROM rvbbit.row_groups
     WHERE table_oid = rel;

    IF row_groups_now = 0 THEN
        DELETE FROM rvbbit.variant_build_queue WHERE table_oid = rel;
        RETURN jsonb_build_object(
            'status', 'skipped', 'table', rel::text, 'reason', 'no canonical row groups'
        );
    END IF;

    INSERT INTO rvbbit.variant_build_queue (
        table_oid, target_generation, target_row_groups, target_rows,
        target_bytes, reason, priority, requested_at, available_at,
        attempts, last_error
    ) VALUES (
        rel, generation_now, row_groups_now, rows_now, bytes_now,
        coalesce(nullif(build_reason, ''), 'repair'),
        coalesce(build_priority, 0), clock_timestamp(), clock_timestamp(), 0, NULL
    )
    ON CONFLICT (table_oid) DO UPDATE SET
        target_generation = EXCLUDED.target_generation,
        target_row_groups = EXCLUDED.target_row_groups,
        target_rows = EXCLUDED.target_rows,
        target_bytes = EXCLUDED.target_bytes,
        reason = EXCLUDED.reason,
        priority = greatest(rvbbit.variant_build_queue.priority, EXCLUDED.priority),
        requested_at = EXCLUDED.requested_at,
        available_at = EXCLUDED.available_at,
        attempts = 0,
        last_error = NULL;

    RETURN jsonb_build_object(
        'status', 'queued',
        'table', rel::text,
        'target_generation', generation_now,
        'target_row_groups', row_groups_now,
        'target_rows', rows_now,
        'target_bytes', bytes_now,
        'reason', coalesce(nullif(build_reason, ''), 'repair')
    );
END;
$$;

COMMENT ON FUNCTION rvbbit.enqueue_variant_build(regclass, text, integer) IS
    'Coalesce a Vortex repair request to the latest committed canonical generation for one table.';

-- A lightweight safety net for canonical mutations outside the normal
-- refresh/rebuild functions.  It performs no scans: the worker resolves the
-- latest generation and size when it claims the one coalesced table row.
CREATE OR REPLACE FUNCTION rvbbit.note_variant_canonical_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    changed_oid oid := CASE WHEN TG_OP = 'DELETE' THEN OLD.table_oid ELSE NEW.table_oid END;
    changed_generation bigint := CASE WHEN TG_OP = 'DELETE' THEN OLD.generation ELSE NEW.generation END;
BEGIN
    IF lower(coalesce(current_setting('rvbbit.suppress_variant_queue', true), 'off'))
       IN ('on', 'true', '1', 'yes') THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;

    -- DROP processing removes rvbbit.tables first and lets its FK cascade
    -- retire row_groups. That is teardown, not a request to rebuild a layout;
    -- avoid resurrecting a queue row whose parent catalog row is already gone.
    IF NOT EXISTS (
        SELECT 1 FROM rvbbit.tables t WHERE t.table_oid = changed_oid
    ) THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;

    INSERT INTO rvbbit.variant_build_queue (
        table_oid, target_generation, reason, requested_at, available_at
    ) VALUES (
        changed_oid, greatest(coalesce(changed_generation, 0), 0),
        'canonical_row_groups_changed', clock_timestamp(), clock_timestamp()
    )
    ON CONFLICT (table_oid) DO UPDATE SET
        target_generation = greatest(
            rvbbit.variant_build_queue.target_generation,
            EXCLUDED.target_generation
        ),
        reason = EXCLUDED.reason,
        requested_at = EXCLUDED.requested_at,
        available_at = EXCLUDED.available_at,
        attempts = 0,
        last_error = NULL;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS rvbbit_variant_canonical_change ON rvbbit.row_groups;
CREATE TRIGGER rvbbit_variant_canonical_change
AFTER INSERT OR UPDATE OF path, n_rows, n_bytes, generation OR DELETE
ON rvbbit.row_groups
FOR EACH ROW
EXECUTE FUNCTION rvbbit.note_variant_canonical_change();

CREATE OR REPLACE VIEW rvbbit.variant_build_status AS
SELECT q.table_oid,
       q.table_oid::regclass::text AS table_name,
       q.target_generation,
       q.target_row_groups,
       q.target_rows,
       q.target_bytes,
       q.reason,
       q.priority,
       q.requested_at,
       q.available_at,
       q.attempts,
       q.last_started_at,
       q.last_finished_at,
       q.last_error,
       coalesce(rg.current_generation, 0) AS current_generation,
       coalesce(rg.current_row_groups, 0) AS current_row_groups,
       coalesce(rg.current_rows, 0) AS current_rows,
       coalesce(rg.current_bytes, 0) AS current_bytes,
       s.status AS vortex_status,
       s.actual_rows AS vortex_rows,
       s.file_count AS vortex_files
FROM rvbbit.variant_build_queue q
LEFT JOIN LATERAL (
    SELECT max(generation)::bigint AS current_generation,
           count(*)::bigint AS current_row_groups,
           coalesce(sum(n_rows), 0)::bigint AS current_rows,
           coalesce(sum(n_bytes), 0)::bigint AS current_bytes
      FROM rvbbit.row_groups rg0
     WHERE rg0.table_oid = q.table_oid
) rg ON true
LEFT JOIN rvbbit.layout_variant_status s
  ON s.table_oid = q.table_oid AND s.layout = 'vortex_scan';

CREATE OR REPLACE FUNCTION rvbbit.variant_tick(
    max_tables integer DEFAULT 1,
    dry_run boolean DEFAULT false
)
RETURNS TABLE (
    table_oid oid,
    table_name text,
    target_generation bigint,
    target_row_groups bigint,
    target_bytes bigint,
    action text,
    executed boolean,
    status text,
    rows_written bigint,
    error text
)
LANGUAGE plpgsql
AS $$
DECLARE
    cand record;
    current_generation bigint;
    current_row_groups bigint;
    current_rows bigint;
    current_bytes bigint;
    build_result bigint;
    operation_id bigint;
    started timestamptz;
    valid boolean;
BEGIN
    IF NOT dry_run AND NOT pg_try_advisory_xact_lock(1381187156, 8) THEN
        RETURN;
    END IF;

    FOR cand IN
        SELECT q.*,
               c.oid::regclass::text AS resolved_name
          FROM rvbbit.variant_build_queue q
          JOIN pg_catalog.pg_class c ON c.oid = q.table_oid
         WHERE q.available_at <= clock_timestamp()
         ORDER BY q.priority DESC, q.requested_at, q.table_oid
         LIMIT greatest(coalesce(max_tables, 1), 1)
    LOOP
        variant_tick.table_oid := cand.table_oid;
        variant_tick.table_name := cand.resolved_name;
        variant_tick.target_generation := cand.target_generation;
        variant_tick.target_row_groups := cand.target_row_groups;
        variant_tick.target_bytes := cand.target_bytes;
        variant_tick.action := 'build_vortex';
        variant_tick.executed := false;
        variant_tick.status := 'planned';
        variant_tick.rows_written := NULL;
        variant_tick.error := NULL;

        SELECT coalesce(max(generation), 0)::bigint,
               count(*)::bigint,
               coalesce(sum(n_rows), 0)::bigint,
               coalesce(sum(n_bytes), 0)::bigint
          INTO current_generation, current_row_groups, current_rows, current_bytes
          FROM rvbbit.row_groups rg_current
         WHERE rg_current.table_oid = cand.table_oid;

        variant_tick.target_generation := current_generation;
        variant_tick.target_row_groups := current_row_groups;
        variant_tick.target_bytes := current_bytes;

        IF current_row_groups = 0 THEN
            variant_tick.action := 'drop_empty_request';
            variant_tick.status := 'skipped';
            IF NOT dry_run THEN
                DELETE FROM rvbbit.variant_build_queue q
                 WHERE q.table_oid = cand.table_oid;
                variant_tick.executed := true;
            END IF;
            RETURN NEXT;
            CONTINUE;
        END IF;

        IF dry_run THEN
            RETURN NEXT;
            CONTINUE;
        END IF;

        IF NOT pg_try_advisory_xact_lock(
            (1380336724::bigint << 32) | cand.table_oid::bigint
        ) THEN
            UPDATE rvbbit.variant_build_queue q
               SET available_at = clock_timestamp() + interval '1 minute',
                   last_error = 'canonical rebuild or compaction is in progress'
             WHERE q.table_oid = cand.table_oid;
            variant_tick.status := 'deferred';
            variant_tick.error := 'canonical rebuild or compaction is in progress';
            RETURN NEXT;
            CONTINUE;
        END IF;

        started := clock_timestamp();
        BEGIN
            UPDATE rvbbit.variant_build_queue q
               SET target_generation = current_generation,
                   target_row_groups = current_row_groups,
                   target_rows = current_rows,
                   target_bytes = current_bytes,
                   last_started_at = started,
                   last_error = NULL
             WHERE q.table_oid = cand.table_oid;

            INSERT INTO rvbbit.acceleration_operations (
                table_oid, table_name, operation, status, settings
            ) VALUES (
                cand.table_oid, cand.resolved_name,
                'variant_build', 'running',
                jsonb_build_object(
                    'source', 'canonical_parquet',
                    'target_generation', current_generation,
                    'target_row_groups', current_row_groups,
                    'target_bytes', current_bytes,
                    'queue_reason', cand.reason
                )
            ) RETURNING id INTO operation_id;

            PERFORM set_config('rvbbit.acceleration_operation_id', operation_id::text, true);
            PERFORM set_config('rvbbit.variant_vortex_only', 'on', true);
            PERFORM set_config('rvbbit.variant_stage_operation_id', '', true);
            SELECT rvbbit.refresh_layout_variants(cand.table_oid)
              INTO build_result;

            IF build_result < 0 THEN
                DELETE FROM rvbbit.variant_build_queue q
                 WHERE q.table_oid = cand.table_oid;
                UPDATE rvbbit.acceleration_operations
                   SET status = 'noop',
                       finished_at = clock_timestamp(),
                       variants_rows = 0,
                       settings = settings || jsonb_build_object(
                           'result', 'vortex disabled by global or per-table policy'
                       )
                 WHERE id = operation_id;
                variant_tick.executed := true;
                variant_tick.status := 'skipped';
                variant_tick.rows_written := 0;
            ELSE
                SELECT EXISTS (
                    SELECT 1
                      FROM rvbbit.layout_variant_status s
                     WHERE s.table_oid = cand.table_oid
                       AND s.layout = 'vortex_scan'
                       AND s.status = 'ready'
                       AND s.expected_rows = current_rows
                       AND s.actual_rows = current_rows
                       AND s.file_count > 0
                       AND NOT EXISTS (
                           SELECT 1
                             FROM rvbbit.row_groups rg
                            WHERE rg.table_oid = cand.table_oid
                              AND NOT EXISTS (
                                  SELECT 1
                                    FROM rvbbit.row_group_variants v
                                   WHERE v.table_oid = rg.table_oid
                                     AND v.layout = 'vortex_scan'
                                     AND v.rg_id = rg.rg_id
                              )
                       )
                ) INTO valid;
                IF NOT valid THEN
                    RAISE EXCEPTION 'Vortex replacement did not validate against canonical generation %',
                        current_generation;
                END IF;

                DELETE FROM rvbbit.variant_build_queue q
                 WHERE q.table_oid = cand.table_oid;
                UPDATE rvbbit.acceleration_operations
                   SET status = 'ok',
                       finished_at = clock_timestamp(),
                       variants_rows = build_result,
                       generation_after = current_generation
                 WHERE id = operation_id;
                variant_tick.executed := true;
                variant_tick.status := 'ok';
                variant_tick.rows_written := build_result;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            variant_tick.executed := true;
            variant_tick.status := 'failed';
            variant_tick.error := SQLERRM;
            UPDATE rvbbit.variant_build_queue q
               SET target_generation = current_generation,
                   target_row_groups = current_row_groups,
                   target_rows = current_rows,
                   target_bytes = current_bytes,
                   attempts = q.attempts + 1,
                   last_finished_at = clock_timestamp(),
                   last_error = SQLERRM,
                   available_at = clock_timestamp()
                       + make_interval(secs => least(3600, 30 * (2 ^ least(q.attempts + 1, 7))))
             WHERE q.table_oid = cand.table_oid;
        END;

        INSERT INTO rvbbit.variant_build_runs (
            table_oid, table_name, target_generation, target_row_groups,
            target_bytes, action, status, rows_written, started_at,
            finished_at, error
        ) VALUES (
            cand.table_oid, cand.resolved_name, current_generation,
            current_row_groups, current_bytes, variant_tick.action,
            variant_tick.status, variant_tick.rows_written, started,
            clock_timestamp(), variant_tick.error
        );
        RETURN NEXT;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION rvbbit.variant_tick(integer, boolean) IS
    'Build queued Vortex layouts from canonical parquet. Requests coalesce per table; failures back off and canonical scans remain authoritative.';

CREATE OR REPLACE FUNCTION rvbbit.schedule_variant_tick(
    cron_schedule text DEFAULT '* * * * *',
    max_tables integer DEFAULT 1
) RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    jobid bigint;
    cron_home text := current_setting('cron.database_name', true);
    this_db text := current_database();
    command text := format('SELECT rvbbit.variant_tick(%s)', greatest(coalesce(max_tables, 1), 1));
BEGIN
    IF cron_home IS NOT NULL AND cron_home <> '' AND cron_home <> this_db THEN
        RAISE EXCEPTION 'pg_cron home database is %, not %; cron.* is not callable here.',
            cron_home, this_db
            USING HINT = format(
                'Use the Scheduler UI, or connect to %L and run: SELECT cron.schedule_in_database(%L, %L, %L, %L);',
                cron_home, 'rvbbit_variant_tick', cron_schedule, command, this_db
            );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        RAISE EXCEPTION 'pg_cron is not installed; cannot schedule the Vortex heartbeat.'
            USING HINT = 'Install pg_cron or call rvbbit.variant_tick() manually.';
    END IF;
    EXECUTE format(
        'SELECT cron.schedule(%L, %L, %L)',
        'rvbbit_variant_tick', cron_schedule, command
    ) INTO jobid;
    RETURN jobid;
END;
$$;

-- Seed only genuine legacy/missing layouts. Explicit per-table Vortex denies
-- remain quiet; a global runtime disable is discovered by variant_tick(), which
-- removes the request as skipped rather than retrying forever.
WITH canonical AS (
    SELECT t.table_oid,
           coalesce(max(rg.generation), 0)::bigint AS generation,
           count(rg.*)::bigint AS row_groups,
           coalesce(sum(rg.n_rows), 0)::bigint AS rows,
           coalesce(sum(rg.n_bytes), 0)::bigint AS bytes
      FROM rvbbit.tables t
      JOIN rvbbit.row_groups rg ON rg.table_oid = t.table_oid
      LEFT JOIN rvbbit.accel_policy p ON p.table_oid = t.table_oid
     WHERE NOT coalesce('vortex' = ANY(p.denied_layouts), false)
     GROUP BY t.table_oid
), pending AS (
    SELECT c.*
      FROM canonical c
     WHERE c.row_groups > 0
       AND NOT EXISTS (
           SELECT 1
             FROM rvbbit.layout_variant_status s
            WHERE s.table_oid = c.table_oid
              AND s.layout = 'vortex_scan'
              AND s.status = 'ready'
              AND s.expected_rows = c.rows
              AND s.actual_rows = c.rows
              AND s.file_count > 0
              AND NOT EXISTS (
                  SELECT 1
                    FROM rvbbit.row_groups rg
                   WHERE rg.table_oid = c.table_oid
                     AND NOT EXISTS (
                         SELECT 1
                           FROM rvbbit.row_group_variants v
                          WHERE v.table_oid = rg.table_oid
                            AND v.layout = 'vortex_scan'
                            AND v.rg_id = rg.rg_id
                     )
              )
       )
)
INSERT INTO rvbbit.variant_build_queue (
    table_oid, target_generation, target_row_groups, target_rows,
    target_bytes, reason, priority, requested_at, available_at
)
SELECT table_oid, generation, row_groups, rows, bytes,
       'migration_0264_legacy_gap', 10, clock_timestamp(), clock_timestamp()
  FROM pending
ON CONFLICT (table_oid) DO UPDATE SET
    target_generation = EXCLUDED.target_generation,
    target_row_groups = EXCLUDED.target_row_groups,
    target_rows = EXCLUDED.target_rows,
    target_bytes = EXCLUDED.target_bytes,
    reason = EXCLUDED.reason,
    priority = greatest(rvbbit.variant_build_queue.priority, EXCLUDED.priority),
    requested_at = EXCLUDED.requested_at,
    available_at = EXCLUDED.available_at;

-- Existing installations already have the staged canonical rebuild function;
-- replace it so Vortex is built from those staged parquet files and published
-- in the same metadata handoff.
CREATE OR REPLACE FUNCTION rvbbit.rebuild_acceleration(
    reloid regclass,
    refresh_variants boolean DEFAULT true
) RETURNS jsonb LANGUAGE plpgsql AS $$
<<accel_rebuild>>
DECLARE
    op_id bigint;
    table_name_text text := reloid::text;
    dropped_rgs int := 0;
    rebuilt_rows bigint := 0;
    row_groups_written bigint := 0;
    variants_rows bigint;
    generation_after bigint := 0;
    pre_max_rg_id bigint := -1;
    baseline_max_rg_id bigint := -1;
    staged_max_rg_id bigint := -1;
    staging_rg_base bigint := 0;
    baseline_generation bigint := 0;
    catchup_generation bigint := 0;
    snapshot_floor_before bigint := 0;
    safe_upper_xid numeric;
    scan_snapshot text;
    scan_upper_xid numeric;
    final_upper_xid numeric;
    phase_id bigint;
    catchup_phase_id bigint;
    phase_bytes_written bigint := 0;
    catchup_rows bigint := 0;
    catchup_row_groups bigint := 0;
    vortex_stage_rows bigint := 0;
    vortex_stage_result bigint;
    vortex_stage_attempted boolean := false;
    vortex_stage_enabled boolean := false;
    vortex_stage_ok boolean := true;
    vortex_stage_error text;
    vortex_expected_rows bigint := 0;
    vortex_actual_rows bigint := 0;
    vortex_file_count integer := 0;
    remapped_tombstones int := 0;
    queued_orphan_files int := 0;
    orphan_paths text[];
    staged_orphan_paths text[];
    final_lock_attempts int := 0;
    final_lock_attempt_timeout_ms int := 100;
    final_lock_retry_sleep_ms int := 50;
    final_lock_max_wait_ms int := 5000;
    final_lock_deadline timestamptz;
    final_lock_acquired boolean := false;
    previous_lock_timeout text;
BEGIN
    IF NOT rvbbit.is_rvbbit_table(reloid) THEN
        RAISE EXCEPTION '% is not an rvbbit table', reloid;
    END IF;

    -- Serialize canonical publication and the independent Vortex retry worker
    -- without taking a heap lock.  The same class/key is used by compact(),
    -- current-history pruning, and variant_tick(); advisory xact locks are
    -- re-entrant when this function is called from accel_tick().
    PERFORM pg_advisory_xact_lock((1380336724::bigint << 32) | reloid::oid::bigint);

    -- Data-loss guard. rebuild regenerates the accelerator FROM the heap. If the
    -- heap is not authoritative (shadow_heap_retained = false — e.g. a legacy
    -- compact(keep_heap := false) truncated it) AND accelerator row groups already
    -- exist, exporting from the empty/partial heap would delete the only surviving
    -- copy of the data (the zero-rows branch below drops every generation and
    -- queues every parquet file for the reaper). Refuse instead. Fresh tables have
    -- no row groups yet (guard skipped); retained-heap tables are unaffected.
    IF EXISTS (SELECT 1 FROM rvbbit.row_groups WHERE table_oid = reloid)
       AND NOT coalesce(
             (SELECT t.shadow_heap_retained FROM rvbbit.tables t
               WHERE t.table_oid = reloid),
             true) THEN
        RAISE EXCEPTION 'rvbbit.rebuild_acceleration: % has a non-authoritative (truncated) heap but existing accelerator row groups; rebuilding from the heap would destroy data. Restore the heap contents or use rvbbit.refresh_acceleration instead.', reloid;
    END IF;

    -- Define the logical fold snapshot without taking a table lock. The long
    -- baseline scan exports only rows visible to this snapshot; after it
    -- finishes, a short final lock appends rows not visible to this snapshot
    -- and remaps concurrent tombstones onto the staged baseline.
    scan_snapshot := pg_current_snapshot()::text;
    scan_upper_xid := greatest(
        0::numeric,
        (pg_snapshot_xmax(scan_snapshot::pg_snapshot)::text)::numeric - 1
    );
    safe_upper_xid := scan_upper_xid;

    INSERT INTO rvbbit.acceleration_operations (
        table_oid, table_name, operation, status,
        watermark_before, watermark_after, settings
    ) VALUES (
        reloid, table_name_text, 'rebuild_acceleration', 'running',
        NULL, safe_upper_xid,
        jsonb_build_object(
            'refresh_variants', refresh_variants,
            'mode', 'lagged_staged_full_heap_fold',
            'heap_guard', 'none_during_baseline_scan',
            'final_guard', 'polite LOCK TABLE IN SHARE MODE polling',
            'final_lock_attempt_timeout_ms', final_lock_attempt_timeout_ms,
            'final_lock_retry_sleep_ms', final_lock_retry_sleep_ms,
            'final_lock_max_wait_ms', final_lock_max_wait_ms,
            'scan_snapshot', scan_snapshot,
            'scan_upper_xid', scan_upper_xid,
            'metadata_swap', 'post_catchup_export',
            'file_reap', 'queued_after_swap',
            'variant_refresh', CASE WHEN refresh_variants THEN 'staged_from_canonical_parquet' ELSE 'skipped' END
        )
    )
    RETURNING id INTO op_id;

    -- Capture the old file set, but keep it live while the expensive full
    -- export runs. Other transactions keep reading the old accelerator until
    -- this transaction commits the metadata swap; physical files are queued for
    -- a later reap instead of being unlinked while old catalog rows are still
    -- MVCC-visible.
    SELECT count(*)::int, coalesce(max(rg_id), -1)::bigint
      INTO dropped_rgs, pre_max_rg_id
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;

    SELECT coalesce(min_visible_generation, 0)
      INTO snapshot_floor_before
      FROM rvbbit.tables
     WHERE table_oid = reloid;

    SELECT greatest(coalesce(max(generation), 0) + 1, (op_id * 2) + 1)
      INTO baseline_generation
      FROM rvbbit.row_groups
     WHERE table_oid = reloid;
    catchup_generation := baseline_generation + 1;
    staging_rg_base := greatest(pre_max_rg_id + 1, op_id * 1000000000);

    SELECT array_agg(path ORDER BY path)
      INTO orphan_paths
      FROM (
          SELECT path FROM rvbbit.row_groups WHERE table_oid = reloid
          UNION ALL
          SELECT path FROM rvbbit.row_group_variants WHERE table_oid = reloid
          UNION ALL
          SELECT path FROM rvbbit.text_dictionaries WHERE table_oid = reloid
      ) old_files;

    INSERT INTO rvbbit.acceleration_operation_phases (
        operation_id, table_oid, table_name, phase, layout, status, details
    ) VALUES (
        op_id, reloid, table_name_text, 'canonical_full_export', 'scan', 'running',
        jsonb_build_object(
            'source', 'heap',
            'mode', 'lagged_staged_full_heap_fold',
            'dropped_row_groups', dropped_rgs,
            'old_max_rg_id', pre_max_rg_id,
            'staging_rg_base', staging_rg_base,
            'baseline_generation', baseline_generation,
            'scan_snapshot', scan_snapshot
        )
    )
    RETURNING id INTO phase_id;

    SELECT rvbbit.export_to_parquet_snapshot_visible_at(
        reloid::oid,
        scan_snapshot,
        staging_rg_base,
        baseline_generation
    )
      INTO rebuilt_rows;

    SELECT count(*)::bigint, coalesce(max(generation), 0)::bigint
      INTO row_groups_written, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id >= staging_rg_base;
    SELECT coalesce(max(rg_id), pre_max_rg_id)::bigint
      INTO baseline_max_rg_id
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id >= staging_rg_base;

    SELECT coalesce(sum(n_bytes), 0)::bigint
      INTO phase_bytes_written
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id >= staging_rg_base;

    UPDATE rvbbit.acceleration_operation_phases
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = rebuilt_rows,
           row_groups_written = accel_rebuild.row_groups_written,
           files_written = accel_rebuild.row_groups_written::integer,
           bytes_written = phase_bytes_written,
           expected_rows = rebuilt_rows,
           actual_rows = rebuilt_rows
     WHERE id = phase_id;

    -- Convert the large baseline from its freshly-written canonical parquet
    -- while OLTP writers are still free to proceed.  This is intentionally a
    -- best-effort derived build: canonical publication must survive a Vortex
    -- encoder failure, which is handed to the coalescing repair queue below.
    IF refresh_variants AND row_groups_written > 0 THEN
        vortex_stage_attempted := true;
        BEGIN
            PERFORM set_config('rvbbit.acceleration_operation_id', op_id::text, true);
            PERFORM set_config('rvbbit.variant_stage_operation_id', op_id::text, true);
            PERFORM set_config('rvbbit.variant_stage_min_rg_id', staging_rg_base::text, true);
            PERFORM set_config('rvbbit.variant_stage_max_rg_id', baseline_max_rg_id::text, true);
            SELECT rvbbit.refresh_layout_variants(reloid::oid)
              INTO vortex_stage_result;
            vortex_stage_enabled := vortex_stage_result >= 0;
            IF vortex_stage_enabled THEN
                vortex_stage_rows := vortex_stage_rows + vortex_stage_result;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            vortex_stage_enabled := true;
            vortex_stage_ok := false;
            vortex_stage_error := SQLERRM;
        END;
    END IF;

    -- Take the short write-blocking handoff lock only after the expensive
    -- baseline scan. Poll with a short lock timeout so a busy table does not
    -- leave a SHARE lock request queued in front of later OLTP writers.
    previous_lock_timeout := current_setting('lock_timeout');
    PERFORM set_config('lock_timeout', final_lock_attempt_timeout_ms::text || 'ms', true);
    final_lock_deadline := clock_timestamp()
        + ((final_lock_max_wait_ms::text || ' milliseconds')::interval);
    WHILE clock_timestamp() < final_lock_deadline LOOP
        final_lock_attempts := final_lock_attempts + 1;
        BEGIN
            EXECUTE format('LOCK TABLE %s IN SHARE MODE', reloid);
            final_lock_acquired := true;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            PERFORM pg_sleep(final_lock_retry_sleep_ms::double precision / 1000.0);
        END;
    END LOOP;
    PERFORM set_config('lock_timeout', previous_lock_timeout, true);

    IF NOT final_lock_acquired THEN
        SELECT array_agg(path ORDER BY path)
          INTO staged_orphan_paths
          FROM (
              SELECT path FROM rvbbit.row_groups
               WHERE table_oid = reloid AND rg_id >= staging_rg_base
              UNION ALL
              SELECT path FROM rvbbit.row_group_variants
               WHERE table_oid = reloid AND build_operation_id = op_id
              UNION ALL
              SELECT path FROM rvbbit.text_dictionaries
               WHERE table_oid = reloid AND rg_id >= staging_rg_base
          ) staged_files;

        DELETE FROM rvbbit.row_group_variants
         WHERE table_oid = reloid
           AND build_operation_id = op_id;
        DELETE FROM rvbbit.row_groups
         WHERE table_oid = reloid
           AND rg_id >= staging_rg_base;
        DELETE FROM rvbbit.generations
         WHERE table_oid = reloid
           AND NOT EXISTS (
               SELECT 1
               FROM rvbbit.row_groups rg
               WHERE rg.table_oid = rvbbit.generations.table_oid
                 AND rg.generation = rvbbit.generations.generation
           );
        -- Row-group INSERT/DELETE triggers observed the abandoned staged set.
        -- The old canonical + Vortex metadata is still authoritative, so this
        -- failed handoff must not leave a spurious repair request behind.
        DELETE FROM rvbbit.variant_build_queue WHERE table_oid = reloid;

        IF staged_orphan_paths IS NOT NULL THEN
            INSERT INTO rvbbit.orphaned_files (path, table_oid, reason, operation_id)
            SELECT DISTINCT p, reloid, 'rebuild_acceleration_final_lock_busy', op_id
            FROM unnest(staged_orphan_paths) AS p
            WHERE p IS NOT NULL AND btrim(p) <> ''
            ON CONFLICT (path) DO UPDATE
               SET table_oid = EXCLUDED.table_oid,
                   reason = EXCLUDED.reason,
                   operation_id = EXCLUDED.operation_id,
                   queued_at = clock_timestamp(),
                   last_error = NULL;
            GET DIAGNOSTICS queued_orphan_files = ROW_COUNT;
        END IF;

        UPDATE rvbbit.acceleration_operation_phases
           SET details = details || jsonb_build_object(
                   'cleaned_up_after_final_lock_busy', true,
                   'final_lock_attempts', final_lock_attempts
               )
         WHERE id = phase_id;

        UPDATE rvbbit.acceleration_operations
           SET status = 'noop',
               finished_at = clock_timestamp(),
               rows_written = 0,
               row_groups_written = 0,
               variants_rows = NULL,
               generation_after = NULL,
               error = 'final lock busy',
               settings = settings || jsonb_build_object(
                   'final_lock_attempts', final_lock_attempts,
                   'final_lock_acquired', false,
                   'queued_orphan_files', queued_orphan_files,
                   'metadata_swap', 'skipped_final_lock_busy'
               )
         WHERE id = op_id;

        RETURN jsonb_build_object(
            'status', 'noop',
            'operation_id', op_id,
            'table', table_name_text,
            'operation', 'rebuild_acceleration',
            'reason', 'final_lock_busy',
            'final_lock_attempts', final_lock_attempts,
            'queued_orphan_files', queued_orphan_files,
            'baseline_rows', rebuilt_rows,
            'catchup_rows', 0,
            'remapped_tombstones', 0,
            'rows_written', 0,
            'row_groups_written', 0,
            'variants_rows', NULL,
            'generation_after', NULL,
            'watermark_after', scan_upper_xid
        );
    END IF;

    final_upper_xid := greatest(
        0::numeric,
        (pg_snapshot_xmax(pg_current_snapshot())::text)::numeric - 1
    );
    safe_upper_xid := final_upper_xid;

    INSERT INTO rvbbit.acceleration_operation_phases (
        operation_id, table_oid, table_name, phase, layout, status, details
    ) VALUES (
        op_id, reloid, table_name_text, 'canonical_gap_export', 'scan', 'running',
        jsonb_build_object(
            'source', 'heap',
            'mode', 'snapshot_gap',
            'scan_snapshot', scan_snapshot,
            'baseline_max_rg_id', baseline_max_rg_id,
            'catchup_generation', catchup_generation
        )
    )
    RETURNING id INTO catchup_phase_id;

    SELECT rvbbit.export_to_parquet_snapshot_gap_at(
        reloid::oid,
        scan_snapshot,
        greatest(baseline_max_rg_id + 1, staging_rg_base),
        catchup_generation
    )
      INTO catchup_rows;

    SELECT count(*)::bigint
      INTO catchup_row_groups
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id >= staging_rg_base
       AND rg_id > baseline_max_rg_id;

    UPDATE rvbbit.acceleration_operation_phases
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = catchup_rows,
           row_groups_written = catchup_row_groups,
           files_written = catchup_row_groups::integer,
           expected_rows = catchup_rows,
           actual_rows = catchup_rows
     WHERE id = catchup_phase_id;

    SELECT coalesce(max(rg_id), -1)::bigint
      INTO staged_max_rg_id
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id >= staging_rg_base;

    -- The final heap lock is already held here.  Only convert the usually tiny
    -- catch-up range; the large baseline Vortex files were produced before the
    -- lock.  If the baseline was empty, this one call covers the entire staged
    -- canonical set.
    IF refresh_variants
       AND staged_max_rg_id >= staging_rg_base
       AND (NOT vortex_stage_attempted
            OR (vortex_stage_enabled AND vortex_stage_ok
                AND staged_max_rg_id > baseline_max_rg_id)) THEN
        vortex_stage_attempted := true;
        BEGIN
            PERFORM set_config('rvbbit.acceleration_operation_id', op_id::text, true);
            PERFORM set_config('rvbbit.variant_stage_operation_id', op_id::text, true);
            PERFORM set_config(
                'rvbbit.variant_stage_min_rg_id',
                CASE
                    WHEN vortex_stage_enabled THEN (baseline_max_rg_id + 1)::text
                    ELSE staging_rg_base::text
                END,
                true
            );
            PERFORM set_config('rvbbit.variant_stage_max_rg_id', staged_max_rg_id::text, true);
            SELECT rvbbit.refresh_layout_variants(reloid::oid)
              INTO vortex_stage_result;
            IF vortex_stage_result < 0 THEN
                vortex_stage_enabled := false;
            ELSE
                vortex_stage_enabled := true;
                vortex_stage_rows := vortex_stage_rows + vortex_stage_result;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            vortex_stage_enabled := true;
            vortex_stage_ok := false;
            vortex_stage_error := SQLERRM;
        END;
    END IF;

    WITH remapped AS (
        INSERT INTO rvbbit.delete_log
            (table_oid, rg_id, ordinal, deleted_xid, deleted_generation)
        SELECT reloid,
               staged_m.rg_id,
               staged_m.ordinal,
               dl.deleted_xid,
               dl.deleted_generation
        FROM rvbbit.delete_log dl
        JOIN rvbbit.row_identity_map old_m
          ON old_m.table_oid = dl.table_oid
         AND old_m.rg_id = dl.rg_id
         AND old_m.ordinal = dl.ordinal
        JOIN rvbbit.row_identity_map staged_m
         ON staged_m.table_oid = old_m.table_oid
         AND staged_m.key_json = old_m.key_json
         AND staged_m.rg_id >= staging_rg_base
         AND staged_m.rg_id <= baseline_max_rg_id
        WHERE dl.table_oid = reloid
          AND dl.rg_id <= pre_max_rg_id
          AND NOT pg_visible_in_snapshot(dl.deleted_xid, scan_snapshot::pg_snapshot)
        ON CONFLICT (table_oid, rg_id, ordinal) DO UPDATE SET
            deleted_xid = EXCLUDED.deleted_xid,
            deleted_generation = EXCLUDED.deleted_generation
        RETURNING 1
    )
    SELECT count(*)::int INTO remapped_tombstones FROM remapped;

    SELECT count(*)::bigint, coalesce(max(generation), 0)::bigint
      INTO row_groups_written, generation_after
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id >= staging_rg_base;

    -- Validate the derived set before the canonical handoff.  A failed Vortex
    -- stage is discarded and queued for retry; it never aborts or delays the
    -- authoritative parquet publication.
    SELECT coalesce(sum(n_rows), 0)::bigint
      INTO vortex_expected_rows
      FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id >= staging_rg_base;
    SELECT coalesce(sum(n_rows), 0)::bigint, count(*)::integer
      INTO vortex_actual_rows, vortex_file_count
      FROM rvbbit.row_group_variants
     WHERE table_oid = reloid
       AND layout = 'vortex_scan'
       AND build_operation_id = op_id;

    IF vortex_stage_enabled AND vortex_stage_ok
       AND (vortex_expected_rows <= 0
            OR vortex_actual_rows <> vortex_expected_rows
            OR vortex_file_count <= 0) THEN
        vortex_stage_ok := false;
        vortex_stage_error := format(
            'staged Vortex validation failed: expected %s rows, catalog has %s rows in %s files',
            vortex_expected_rows,
            vortex_actual_rows,
            vortex_file_count
        );
    END IF;

    -- Atomic metadata swap inside this transaction: remove old row groups and
    -- their dependent stats/identity rows. Old tombstones are discarded after
    -- any concurrent, post-snapshot tombstones have been remapped onto the
    -- staged baseline row-group ordinals above.  A valid staged Vortex set is
    -- retained by operation id while every prior variant disappears atomically.
    DELETE FROM rvbbit.delete_log
     WHERE table_oid = reloid
       AND rg_id <= pre_max_rg_id;
    DELETE FROM rvbbit.layout_variant_status WHERE table_oid = reloid;
    IF vortex_stage_enabled AND vortex_stage_ok THEN
        DELETE FROM rvbbit.row_group_variants
         WHERE table_oid = reloid
           AND build_operation_id IS DISTINCT FROM op_id;
        INSERT INTO rvbbit.layout_variant_status (
            table_oid, layout, status, expected_rows, actual_rows,
            file_count, status_message, refreshed_at
        ) VALUES (
            reloid, 'vortex_scan', 'ready', vortex_expected_rows,
            vortex_actual_rows, vortex_file_count, NULL, clock_timestamp()
        )
        ON CONFLICT (table_oid, layout) DO UPDATE SET
            status = EXCLUDED.status,
            expected_rows = EXCLUDED.expected_rows,
            actual_rows = EXCLUDED.actual_rows,
            file_count = EXCLUDED.file_count,
            status_message = NULL,
            refreshed_at = EXCLUDED.refreshed_at;
        variants_rows := vortex_actual_rows;
    ELSE
        SELECT array_agg(path ORDER BY path)
          INTO staged_orphan_paths
          FROM rvbbit.row_group_variants
         WHERE table_oid = reloid
           AND build_operation_id = op_id;
        orphan_paths := coalesce(orphan_paths, ARRAY[]::text[])
            || coalesce(staged_orphan_paths, ARRAY[]::text[]);
        DELETE FROM rvbbit.row_group_variants WHERE table_oid = reloid;
        variants_rows := NULL;
    END IF;
    DELETE FROM rvbbit.row_groups
     WHERE table_oid = reloid
       AND rg_id <= pre_max_rg_id;
    IF row_groups_written > 0 THEN
        DELETE FROM rvbbit.generations
         WHERE table_oid = reloid
           AND NOT EXISTS (
               SELECT 1
               FROM rvbbit.row_groups rg
               WHERE rg.table_oid = rvbbit.generations.table_oid
                 AND rg.generation = rvbbit.generations.generation
           );
    ELSE
        DELETE FROM rvbbit.generations WHERE table_oid = reloid;
        generation_after := 0;
    END IF;

    UPDATE rvbbit.tables
       SET shadow_heap_retained = true,
           shadow_heap_dirty = false,
           dirty_has_insert = false,
           dirty_has_update = false,
           dirty_has_delete = false,
           dirty_has_truncate = false,
           min_visible_generation = CASE
               WHEN snapshot_floor_before > 0 AND generation_after > 0
               THEN generation_after
               ELSE min_visible_generation
           END,
           next_generation = greatest(next_generation, generation_after + 1),
           ctid_identity_relfilenode = CASE
               WHEN rvbbit.accel_identity_mode(reloid) = 'ctid'
               THEN pg_relation_filenode(reloid)
               ELSE ctid_identity_relfilenode
           END
     WHERE table_oid = reloid;
    PERFORM rvbbit.clear_table_dirty_markers(reloid::oid);

    DELETE FROM rvbbit.acceleration_state WHERE table_oid = reloid;

    IF orphan_paths IS NOT NULL THEN
        INSERT INTO rvbbit.orphaned_files (path, table_oid, reason, operation_id)
        SELECT DISTINCT p, reloid, 'rebuild_acceleration_staged_swap', op_id
        FROM unnest(orphan_paths) AS p
        WHERE p IS NOT NULL AND btrim(p) <> ''
        ON CONFLICT (path) DO UPDATE
           SET table_oid = EXCLUDED.table_oid,
               reason = EXCLUDED.reason,
               operation_id = EXCLUDED.operation_id,
               queued_at = clock_timestamp(),
               last_error = NULL;
        GET DIAGNOSTICS queued_orphan_files = ROW_COUNT;
    END IF;

    IF refresh_variants AND row_groups_written > 0 THEN
        IF vortex_stage_enabled AND vortex_stage_ok THEN
            DELETE FROM rvbbit.variant_build_queue WHERE table_oid = reloid;
        ELSIF vortex_stage_enabled OR NOT vortex_stage_attempted THEN
            INSERT INTO rvbbit.variant_build_queue (
                table_oid, target_generation, target_row_groups,
                target_rows, target_bytes, reason, priority,
                requested_at, available_at, attempts, last_error
            )
            SELECT reloid,
                   generation_after,
                   count(*)::bigint,
                   coalesce(sum(n_rows), 0)::bigint,
                   coalesce(sum(n_bytes), 0)::bigint,
                   'canonical_rebuild_stage_failed',
                   100,
                   clock_timestamp(),
                   clock_timestamp(),
                   0,
                   vortex_stage_error
              FROM rvbbit.row_groups
             WHERE table_oid = reloid
            ON CONFLICT (table_oid) DO UPDATE SET
                target_generation = EXCLUDED.target_generation,
                target_row_groups = EXCLUDED.target_row_groups,
                target_rows = EXCLUDED.target_rows,
                target_bytes = EXCLUDED.target_bytes,
                reason = EXCLUDED.reason,
                priority = greatest(rvbbit.variant_build_queue.priority, EXCLUDED.priority),
                requested_at = EXCLUDED.requested_at,
                available_at = EXCLUDED.available_at,
                attempts = 0,
                last_error = EXCLUDED.last_error;
        END IF;
    ELSIF NOT refresh_variants THEN
        DELETE FROM rvbbit.variant_build_queue WHERE table_oid = reloid;
    END IF;

    INSERT INTO rvbbit.acceleration_state (
        table_oid,
        last_refresh_xid,
        last_refresh_generation,
        last_refresh_rows,
        last_refresh_row_groups,
        refresh_relfilenode,
        last_refresh_at,
        updated_at
    ) VALUES (
        reloid,
        safe_upper_xid,
        generation_after,
        coalesce(rebuilt_rows, 0) + coalesce(catchup_rows, 0),
        coalesce(row_groups_written, 0),
        pg_relation_filenode(reloid),
        clock_timestamp(),
        clock_timestamp()
    )
    ON CONFLICT (table_oid) DO UPDATE
       SET last_refresh_xid = EXCLUDED.last_refresh_xid,
           last_refresh_generation = EXCLUDED.last_refresh_generation,
           last_refresh_rows = EXCLUDED.last_refresh_rows,
           last_refresh_row_groups = EXCLUDED.last_refresh_row_groups,
           refresh_relfilenode = EXCLUDED.refresh_relfilenode,
           last_refresh_at = EXCLUDED.last_refresh_at,
           updated_at = EXCLUDED.updated_at;

    PERFORM rvbbit.install_shadow_heap_dirty_triggers(reloid);

    UPDATE rvbbit.acceleration_operations
       SET status = 'ok',
           finished_at = clock_timestamp(),
           rows_written = rebuilt_rows + coalesce(catchup_rows, 0),
           row_groups_written = accel_rebuild.row_groups_written,
           variants_rows = accel_rebuild.variants_rows,
           generation_after = accel_rebuild.generation_after,
           watermark_after = safe_upper_xid,
           settings = settings || jsonb_build_object(
               'dropped_row_groups', dropped_rgs,
               'old_max_rg_id', pre_max_rg_id,
               'baseline_max_rg_id', baseline_max_rg_id,
               'staging_rg_base', staging_rg_base,
               'baseline_generation', baseline_generation,
               'catchup_generation', catchup_generation,
               'baseline_rows', rebuilt_rows,
               'catchup_rows', catchup_rows,
               'catchup_row_groups', catchup_row_groups,
               'remapped_tombstones', remapped_tombstones,
               'final_lock_attempts', final_lock_attempts,
               'final_lock_acquired', true,
               'queued_orphan_files', queued_orphan_files,
               'metadata_swap', 'lagged_staged',
               'watermark_after', safe_upper_xid
           )
     WHERE id = op_id;

    RETURN jsonb_build_object(
        'status', 'ok',
        'operation_id', op_id,
        'table', table_name_text,
        'operation', 'rebuild_acceleration',
        'dropped_row_groups', dropped_rgs,
        'queued_orphan_files', queued_orphan_files,
        'baseline_rows', rebuilt_rows,
        'catchup_rows', catchup_rows,
        'remapped_tombstones', remapped_tombstones,
        'rows_written', rebuilt_rows + coalesce(catchup_rows, 0),
        'row_groups_written', row_groups_written,
        'variants_rows', variants_rows,
        'generation_after', generation_after,
        'watermark_after', safe_upper_xid
    );
EXCEPTION WHEN OTHERS THEN
    IF op_id IS NOT NULL THEN
        UPDATE rvbbit.acceleration_operation_phases
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = SQLERRM
         WHERE operation_id = op_id
           AND status = 'running';
        UPDATE rvbbit.acceleration_operations
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = SQLERRM
         WHERE id = op_id;
    END IF;
    RAISE;
END $$;
