-- 0272: One derived-layout worker fleet.
--
-- Canonical Parquet freshness remains owned by accel_tick. This lane owns the
-- layouts derived from that canonical state:
--   * vortex_scan is an automatic target represented by variant_build_queue;
--   * cluster/Hive layouts are governed targets represented by accepted
--     workload_layout_recommendations.
--
-- Workers share the heavy-maintenance gate, so different tables' derived
-- layouts may build concurrently while full/Lance canonical work remains
-- exclusive. Each worker invocation still claims at most one table.

CREATE TABLE IF NOT EXISTS rvbbit.layout_tick_runs (
    id                  bigserial PRIMARY KEY,
    table_oid           oid REFERENCES rvbbit.tables(table_oid) ON DELETE SET NULL,
    table_name          text NOT NULL,
    target_generation   bigint NOT NULL DEFAULT 0,
    target_row_groups   bigint NOT NULL DEFAULT 0,
    target_rows         bigint NOT NULL DEFAULT 0,
    target_bytes        bigint NOT NULL DEFAULT 0,
    target_layouts      text[] NOT NULL DEFAULT ARRAY[]::text[],
    worker_slot         integer NOT NULL DEFAULT 1,
    worker_count        integer NOT NULL DEFAULT 1,
    attempt             integer NOT NULL DEFAULT 1,
    action              text NOT NULL,
    status              text NOT NULL,
    rows_written        bigint,
    started_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    retry_at            timestamptz,
    error               text,
    details             jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (worker_slot > 0),
    CHECK (worker_count > 0),
    CHECK (attempt > 0)
);

CREATE INDEX IF NOT EXISTS layout_tick_runs_table_time_idx
    ON rvbbit.layout_tick_runs (table_oid, started_at DESC);
CREATE INDEX IF NOT EXISTS layout_tick_runs_retry_idx
    ON rvbbit.layout_tick_runs (retry_at)
    WHERE retry_at IS NOT NULL;

ALTER TABLE rvbbit.layout_tick_runs SET (
    autovacuum_vacuum_scale_factor = 0.01,
    autovacuum_vacuum_threshold = 100,
    autovacuum_analyze_scale_factor = 0.02,
    autovacuum_analyze_threshold = 100
);

CREATE OR REPLACE VIEW rvbbit.layout_tick_candidates AS
WITH canonical AS (
    SELECT t.table_oid,
           t.table_oid::regclass::text AS table_name,
           coalesce(max(rg.generation), 0)::bigint AS target_generation,
           count(rg.rg_id)::bigint AS target_row_groups,
           coalesce(sum(rg.n_rows), 0)::bigint AS target_rows,
           coalesce(sum(rg.n_bytes), 0)::bigint AS target_bytes,
           max(rg.created_at) AS newest_row_group
      FROM rvbbit.tables t
      JOIN pg_catalog.pg_class c
        ON c.oid = t.table_oid
      LEFT JOIN rvbbit.row_groups rg
        ON rg.table_oid = t.table_oid
     WHERE c.relkind IN ('r', 'p')
       AND coalesce(t.acceleration_enabled, true)
     GROUP BY t.table_oid
), workload AS (
    SELECT w.table_oid,
           w.accepted_layouts,
           w.pending_layouts,
           w.accepted_layout_names,
           w.pending_layout_names,
           w.priority,
           w.recommendation_updated_at
      FROM rvbbit.workload_layout_tick_candidates w
)
SELECT canonical.table_oid,
       canonical.table_name,
       canonical.target_generation,
       canonical.target_row_groups,
       canonical.target_rows,
       canonical.target_bytes,
       canonical.newest_row_group,
       (queue.table_oid IS NOT NULL) AS vortex_pending,
       queue.reason AS vortex_reason,
       queue.priority AS vortex_priority,
       queue.requested_at AS vortex_requested_at,
       queue.available_at AS vortex_available_at,
       queue.attempts AS vortex_attempts,
       (workload.table_oid IS NOT NULL) AS workload_pending,
       coalesce(workload.accepted_layouts, 0)::integer AS accepted_layouts,
       coalesce(workload.pending_layouts, 0)::integer AS pending_workload_layouts,
       coalesce(workload.accepted_layout_names, ARRAY[]::text[])
           AS accepted_layout_names,
       coalesce(workload.pending_layout_names, ARRAY[]::text[])
           AS pending_workload_layout_names,
       (
           CASE WHEN queue.table_oid IS NOT NULL
                THEN ARRAY['vortex_scan']::text[]
                ELSE ARRAY[]::text[] END
           || coalesce(workload.pending_layout_names, ARRAY[]::text[])
       ) AS target_layouts,
       (
           CASE WHEN queue.table_oid IS NOT NULL THEN 1 ELSE 0 END
           + coalesce(workload.pending_layouts, 0)
       )::integer AS pending_layouts,
       greatest(
           coalesce(queue.priority, 0)::double precision,
           coalesce(workload.priority, 0)::double precision
       ) AS priority,
       least(
           queue.requested_at,
           CASE WHEN workload.table_oid IS NOT NULL
                THEN greatest(
                    coalesce(canonical.newest_row_group, '-infinity'::timestamptz),
                    workload.recommendation_updated_at
                )
                ELSE NULL END
       ) AS pending_since
  FROM canonical
  LEFT JOIN rvbbit.variant_build_queue queue
    ON queue.table_oid = canonical.table_oid
  LEFT JOIN workload
    ON workload.table_oid = canonical.table_oid
 WHERE queue.table_oid IS NOT NULL
    OR workload.table_oid IS NOT NULL;

COMMENT ON VIEW rvbbit.layout_tick_candidates IS
    'Unified desired-layout backlog: automatic Vortex targets plus accepted cluster/Hive targets, resolved against current canonical Parquet state.';

CREATE OR REPLACE VIEW rvbbit.layout_tick_ready_candidates AS
WITH state AS (
    SELECT candidate.*,
           last_run.id AS last_run_id,
           last_run.status AS last_status,
           last_run.attempt AS last_attempt,
           last_run.retry_at AS last_retry_at,
           last_run.target_generation AS last_target_generation,
           last_run.target_layouts AS last_target_layouts,
           last_run.details AS last_details,
           (
               candidate.vortex_pending
               AND candidate.vortex_available_at <= clock_timestamp()
           ) AS vortex_ready,
           (
               candidate.workload_pending
               AND (
                   last_run.id IS NULL
                   OR last_run.retry_at IS NULL
                   OR last_run.retry_at <= clock_timestamp()
                   OR last_run.target_generation
                       IS DISTINCT FROM candidate.target_generation
                   OR last_run.details -> 'accepted_targets'
                       IS DISTINCT FROM
                           to_jsonb(candidate.pending_workload_layout_names)
               )
           ) AS workload_ready
      FROM rvbbit.layout_tick_candidates candidate
      LEFT JOIN LATERAL (
          SELECT run.id,
                 run.status,
                 run.attempt,
                 run.retry_at,
                 run.target_generation,
                 run.target_layouts,
                 run.details
            FROM rvbbit.layout_tick_runs run
           WHERE run.table_oid = candidate.table_oid
             AND coalesce(
                     (run.details ->> 'workload_attempted')::boolean,
                     false
                 )
           ORDER BY run.id DESC
           LIMIT 1
      ) last_run ON true
)
SELECT *
  FROM state
 WHERE vortex_ready OR workload_ready;

COMMENT ON VIEW rvbbit.layout_tick_ready_candidates IS
    'Actionable unified layout backlog after Vortex and accepted-layout retry windows are applied.';

CREATE OR REPLACE FUNCTION rvbbit._build_automatic_vortex_target(rel oid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $function$
DECLARE
    resolved_name text;
    current_generation bigint;
    current_row_groups bigint;
    current_rows bigint;
    current_bytes bigint;
    build_result bigint := 0;
    operation_id bigint;
    started timestamptz := clock_timestamp();
    finished timestamptz;
    valid boolean := false;
    build_status text := 'planned';
    build_error text;
    retry_at_value timestamptz;
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM rvbbit.variant_build_queue queue
         WHERE queue.table_oid = rel
    ) THEN
        RETURN jsonb_build_object(
            'status', 'skipped',
            'reason', 'automatic Vortex target is no longer pending',
            'rows_written', 0
        );
    END IF;

    SELECT class.oid::regclass::text,
           coalesce(max(groups.generation), 0)::bigint,
           count(groups.rg_id)::bigint,
           coalesce(sum(groups.n_rows), 0)::bigint,
           coalesce(sum(groups.n_bytes), 0)::bigint
      INTO resolved_name, current_generation, current_row_groups,
           current_rows, current_bytes
      FROM pg_catalog.pg_class class
      LEFT JOIN rvbbit.row_groups groups
        ON groups.table_oid = class.oid
     WHERE class.oid = rel
     GROUP BY class.oid;

    IF NOT FOUND THEN
        DELETE FROM rvbbit.variant_build_queue queue
         WHERE queue.table_oid = rel;
        RETURN jsonb_build_object(
            'status', 'skipped',
            'reason', 'relation no longer exists',
            'rows_written', 0
        );
    END IF;

    IF current_row_groups = 0 THEN
        DELETE FROM rvbbit.variant_build_queue queue
         WHERE queue.table_oid = rel;
        RETURN jsonb_build_object(
            'status', 'skipped',
            'reason', 'no canonical row groups',
            'rows_written', 0,
            'target_generation', current_generation
        );
    END IF;

    UPDATE rvbbit.variant_build_queue queue
       SET target_generation = current_generation,
           target_row_groups = current_row_groups,
           target_rows = current_rows,
           target_bytes = current_bytes,
           last_started_at = started,
           last_error = NULL
     WHERE queue.table_oid = rel;

    INSERT INTO rvbbit.acceleration_operations (
        table_oid, table_name, operation, status, settings
    ) VALUES (
        rel, resolved_name, 'variant_build', 'running',
        jsonb_build_object(
            'source', 'unified_layout_tick',
            'layout', 'vortex_scan',
            'target_generation', current_generation,
            'target_row_groups', current_row_groups,
            'target_bytes', current_bytes
        )
    ) RETURNING id INTO operation_id;

    BEGIN
        PERFORM set_config(
            'rvbbit.acceleration_operation_id',
            operation_id::text,
            true
        );
        PERFORM set_config('rvbbit.variant_vortex_only', 'on', true);
        PERFORM set_config('rvbbit.variant_stage_operation_id', '', true);
        SELECT rvbbit.refresh_layout_variants(rel)
          INTO build_result;
        PERFORM set_config('rvbbit.variant_vortex_only', 'off', true);

        IF build_result < 0 THEN
            DELETE FROM rvbbit.variant_build_queue queue
             WHERE queue.table_oid = rel;
            UPDATE rvbbit.acceleration_operations operation
               SET status = 'noop',
                   finished_at = clock_timestamp(),
                   variants_rows = 0,
                   settings = operation.settings || jsonb_build_object(
                       'result', 'Vortex disabled by global or per-table policy'
                   )
             WHERE operation.id = operation_id;
            build_status := 'skipped';
            build_result := 0;
        ELSE
            SELECT EXISTS (
                SELECT 1
                  FROM rvbbit.layout_variant_status status
                 WHERE status.table_oid = rel
                   AND status.layout = 'vortex_scan'
                   AND status.status = 'ready'
                   AND status.expected_rows = current_rows
                   AND status.actual_rows = current_rows
                   AND status.file_count > 0
                   AND NOT EXISTS (
                       SELECT 1
                         FROM rvbbit.row_groups groups
                        WHERE groups.table_oid = rel
                          AND NOT EXISTS (
                              SELECT 1
                                FROM rvbbit.row_group_variants variant
                               WHERE variant.table_oid = groups.table_oid
                                 AND variant.layout = 'vortex_scan'
                                 AND variant.rg_id = groups.rg_id
                          )
                   )
            ) INTO valid;
            IF NOT valid THEN
                RAISE EXCEPTION
                    'Vortex replacement did not validate against canonical generation %',
                    current_generation;
            END IF;

            DELETE FROM rvbbit.variant_build_queue queue
             WHERE queue.table_oid = rel;
            UPDATE rvbbit.acceleration_operations operation
               SET status = 'ok',
                   finished_at = clock_timestamp(),
                   variants_rows = build_result,
                   generation_after = current_generation
             WHERE operation.id = operation_id;
            build_status := 'ok';
        END IF;
    EXCEPTION WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS build_error = MESSAGE_TEXT;
        PERFORM set_config('rvbbit.variant_vortex_only', 'off', true);
        build_status := 'failed';
        build_result := 0;

        UPDATE rvbbit.acceleration_operations operation
           SET status = 'failed',
               finished_at = clock_timestamp(),
               error = build_error
         WHERE operation.id = operation_id;

        UPDATE rvbbit.variant_build_queue queue
           SET target_generation = current_generation,
               target_row_groups = current_row_groups,
               target_rows = current_rows,
               target_bytes = current_bytes,
               attempts = queue.attempts + 1,
               last_finished_at = clock_timestamp(),
               last_error = build_error,
               available_at = clock_timestamp()
                   + make_interval(
                       secs => least(
                           3600,
                           30 * (2 ^ least(queue.attempts + 1, 7))
                       )::integer
                   )
         WHERE queue.table_oid = rel
         RETURNING queue.available_at INTO retry_at_value;
    END;

    finished := clock_timestamp();
    INSERT INTO rvbbit.variant_build_runs (
        table_oid, table_name, target_generation, target_row_groups,
        target_bytes, action, status, rows_written, started_at,
        finished_at, error
    ) VALUES (
        rel, resolved_name, current_generation, current_row_groups,
        current_bytes, 'build_vortex', build_status, build_result,
        started, finished, build_error
    );

    RETURN jsonb_strip_nulls(jsonb_build_object(
        'status', build_status,
        'layout', 'vortex_scan',
        'rows_written', build_result,
        'target_generation', current_generation,
        'target_row_groups', current_row_groups,
        'target_rows', current_rows,
        'target_bytes', current_bytes,
        'retry_at', retry_at_value,
        'error', build_error
    ));
END;
$function$;

COMMENT ON FUNCTION rvbbit._build_automatic_vortex_target(oid) IS
    'Internal table-specific Vortex builder used after the unified layout worker has claimed the table and shared layout lane.';

CREATE OR REPLACE FUNCTION rvbbit.layout_tick_worker(
    worker_slot integer,
    worker_count integer DEFAULT 1,
    dry_run boolean DEFAULT false
)
RETURNS TABLE (
    table_oid oid,
    table_name text,
    target_generation bigint,
    target_layouts text[],
    action text,
    executed boolean,
    status text,
    rows_written bigint,
    error text,
    details jsonb
)
LANGUAGE plpgsql
VOLATILE
AS $function$
DECLARE
    candidate record;
    live record;
    started timestamptz;
    vortex_result jsonb;
    vortex_status text;
    vortex_error text;
    workload_error text;
    workload_rows bigint := 0;
    workload_still_pending boolean := false;
    vortex_still_pending boolean := false;
    attempt_no integer;
    retry_seconds integer;
    retry_at_value timestamptz;
    attempted_count integer;
    successful_count integer;
BEGIN
    IF worker_count NOT BETWEEN 1 AND 8 THEN
        RAISE EXCEPTION 'worker_count must be between 1 and 8 (got %)', worker_count;
    END IF;
    IF worker_slot NOT BETWEEN 1 AND worker_count THEN
        RAISE EXCEPTION
            'worker_slot must be between 1 and worker_count % (got %)',
            worker_count,
            worker_slot;
    END IF;

    IF NOT dry_run THEN
        -- Derived layouts may run beside freshness deltas, but not folds or
        -- canonical full/Lance work. Shared heavy-gate ownership also allows
        -- Vortex builds for different tables to use the configured slots.
        IF NOT pg_try_advisory_xact_lock_shared(1381187156, 7) THEN
            RETURN;
        END IF;
        IF NOT pg_try_advisory_xact_lock_shared(1381187156, 8) THEN
            RETURN;
        END IF;
        -- Reuse the workload-layout slot keys so accidentally overlapping old
        -- and new scheduler jobs skip rather than multiply concurrency.
        IF NOT pg_try_advisory_xact_lock(1381187156, 90 + worker_slot) THEN
            RETURN;
        END IF;
    END IF;

    FOR candidate IN
        SELECT ready.*
          FROM rvbbit.layout_tick_ready_candidates ready
         ORDER BY
               (mod(ready.table_oid::bigint, worker_count::bigint)
                    + 1 = worker_slot) DESC,
               ready.pending_since NULLS FIRST,
               ready.priority DESC,
               ready.table_oid
    LOOP
        layout_tick_worker.table_oid := candidate.table_oid;
        layout_tick_worker.table_name := candidate.table_name;
        layout_tick_worker.target_generation := candidate.target_generation;
        layout_tick_worker.target_layouts := candidate.target_layouts;
        layout_tick_worker.action := CASE
            WHEN candidate.vortex_ready AND candidate.workload_ready
                THEN 'build_vortex_and_accepted_layouts'
            WHEN candidate.vortex_ready THEN 'build_vortex'
            ELSE 'build_accepted_layouts'
        END;
        layout_tick_worker.executed := false;
        layout_tick_worker.status := 'planned';
        layout_tick_worker.rows_written := NULL;
        layout_tick_worker.error := NULL;
        layout_tick_worker.details := jsonb_build_object(
            'vortex_ready', candidate.vortex_ready,
            'workload_ready', candidate.workload_ready,
            'automatic_targets',
                CASE WHEN candidate.vortex_pending
                     THEN jsonb_build_array('vortex_scan')
                     ELSE '[]'::jsonb END,
            'accepted_targets', candidate.pending_workload_layout_names
        );

        IF dry_run THEN
            RETURN NEXT;
            RETURN;
        END IF;

        IF NOT pg_try_advisory_xact_lock(
            (1380336724::bigint << 32) | candidate.table_oid::bigint
        ) THEN
            CONTINUE;
        END IF;

        -- Re-read both source catalogs after claiming the table. A canonical
        -- refresh, old repair job, or manual layout build may have won the race.
        SELECT ready.*
          INTO live
          FROM rvbbit.layout_tick_ready_candidates ready
         WHERE ready.table_oid = candidate.table_oid;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        layout_tick_worker.table_name := live.table_name;
        layout_tick_worker.target_generation := live.target_generation;
        layout_tick_worker.target_layouts := live.target_layouts;
        layout_tick_worker.action := CASE
            WHEN live.vortex_ready AND live.workload_ready
                THEN 'build_vortex_and_accepted_layouts'
            WHEN live.vortex_ready THEN 'build_vortex'
            ELSE 'build_accepted_layouts'
        END;
        layout_tick_worker.executed := true;
        layout_tick_worker.rows_written := 0;
        started := clock_timestamp();
        workload_rows := 0;
        workload_error := NULL;
        vortex_result := NULL;
        vortex_status := NULL;
        vortex_error := NULL;
        retry_at_value := NULL;
        attempted_count := 0;
        successful_count := 0;

        attempt_no := CASE
            WHEN live.last_status IN ('failed', 'partial')
             AND live.last_target_generation
                 IS NOT DISTINCT FROM live.target_generation
             AND live.last_details -> 'accepted_targets'
                 IS NOT DISTINCT FROM
                     to_jsonb(live.pending_workload_layout_names)
            THEN greatest(coalesce(live.last_attempt, 0) + 1, 1)
            ELSE 1
        END;

        PERFORM set_config(
            'application_name',
            left(format(
                'rvbbit/layout s=%s/%s t=%s %s',
                worker_slot,
                worker_count,
                live.table_oid,
                live.table_name
            ), 63),
            true
        );

        IF live.vortex_ready THEN
            attempted_count := attempted_count + 1;
            vortex_result := rvbbit._build_automatic_vortex_target(
                live.table_oid
            );
            vortex_status := coalesce(vortex_result ->> 'status', 'failed');
            vortex_error := vortex_result ->> 'error';
            layout_tick_worker.rows_written :=
                layout_tick_worker.rows_written
                + coalesce((vortex_result ->> 'rows_written')::bigint, 0);
            IF vortex_status IN ('ok', 'skipped') THEN
                successful_count := successful_count + 1;
            END IF;
        END IF;

        IF live.workload_ready THEN
            attempted_count := attempted_count + 1;
            BEGIN
                SELECT rvbbit.refresh_workload_layout_variants(live.table_oid)
                  INTO workload_rows;
            EXCEPTION WHEN OTHERS THEN
                GET STACKED DIAGNOSTICS workload_error = MESSAGE_TEXT;
                workload_rows := 0;
            END;
            layout_tick_worker.rows_written :=
                layout_tick_worker.rows_written + coalesce(workload_rows, 0);

            SELECT rvbbit.workload_layout_variants_pending(live.table_oid)
              INTO workload_still_pending;
            IF workload_error IS NULL AND NOT workload_still_pending THEN
                successful_count := successful_count + 1;
            ELSIF workload_error IS NULL THEN
                workload_error := 'accepted layouts remain pending after build';
            END IF;
        ELSE
            workload_still_pending := live.workload_pending;
        END IF;

        SELECT EXISTS (
            SELECT 1
              FROM rvbbit.variant_build_queue queue
             WHERE queue.table_oid = live.table_oid
        ) INTO vortex_still_pending;

        layout_tick_worker.error := nullif(concat_ws(
            '; ',
            CASE WHEN vortex_status = 'failed'
                 THEN 'vortex: ' || coalesce(vortex_error, 'build failed') END,
            CASE WHEN workload_error IS NOT NULL
                 THEN 'accepted layouts: ' || workload_error END
        ), '');

        layout_tick_worker.status := CASE
            WHEN attempted_count = 0 THEN 'skipped'
            WHEN successful_count = attempted_count THEN 'ok'
            WHEN successful_count = 0 THEN 'failed'
            ELSE 'partial'
        END;

        IF live.workload_ready
           AND (workload_error IS NOT NULL OR workload_still_pending) THEN
            retry_seconds := least(
                3600,
                (30 * power(2, least(attempt_no - 1, 7)))::integer
            );
            retry_at_value := clock_timestamp()
                + make_interval(secs => retry_seconds);
        END IF;

        layout_tick_worker.details := jsonb_strip_nulls(jsonb_build_object(
            'vortex_attempted', live.vortex_ready,
            'vortex_result', vortex_result,
            'vortex_pending_after', vortex_still_pending,
            'workload_attempted', live.workload_ready,
            'workload_rows', workload_rows,
            'workload_error', workload_error,
            'workload_pending_after', workload_still_pending,
            'automatic_targets',
                CASE WHEN live.vortex_pending
                     THEN jsonb_build_array('vortex_scan')
                     ELSE '[]'::jsonb END,
            'accepted_targets', live.pending_workload_layout_names,
            'source', 'unified_layout_tick'
        ));

        INSERT INTO rvbbit.layout_tick_runs (
            table_oid, table_name, target_generation, target_row_groups,
            target_rows, target_bytes, target_layouts, worker_slot,
            worker_count, attempt, action, status, rows_written,
            started_at, finished_at, retry_at, error, details
        ) VALUES (
            live.table_oid, live.table_name, live.target_generation,
            live.target_row_groups, live.target_rows, live.target_bytes,
            live.target_layouts, worker_slot, worker_count, attempt_no,
            layout_tick_worker.action, layout_tick_worker.status,
            layout_tick_worker.rows_written, started, clock_timestamp(),
            retry_at_value, layout_tick_worker.error,
            layout_tick_worker.details
        );

        RETURN NEXT;
        RETURN;
    END LOOP;
END;
$function$;

COMMENT ON FUNCTION rvbbit.layout_tick_worker(integer, integer, boolean) IS
    'Builds one table of automatic Vortex and/or accepted cluster/Hive targets. Up to eight slots share the derived-layout lane and steal unlocked work.';

CREATE OR REPLACE FUNCTION rvbbit.layout_tick(
    dry_run boolean DEFAULT false
)
RETURNS TABLE (
    table_oid oid,
    table_name text,
    target_generation bigint,
    target_layouts text[],
    action text,
    executed boolean,
    status text,
    rows_written bigint,
    error text,
    details jsonb
)
LANGUAGE sql
VOLATILE
AS $function$
    SELECT * FROM rvbbit.layout_tick_worker(1, 1, dry_run)
$function$;

COMMENT ON FUNCTION rvbbit.layout_tick(boolean) IS
    'Conservative singleton entry point for all derived layout targets.';

CREATE OR REPLACE PROCEDURE rvbbit.layout_tick_worker_pass(
    worker_slot integer,
    worker_count integer DEFAULT 1,
    tables_per_pass integer DEFAULT 4
)
LANGUAGE plpgsql
AS $procedure$
DECLARE
    pass_no integer;
    attempted integer;
BEGIN
    IF worker_count NOT BETWEEN 1 AND 8 THEN
        RAISE EXCEPTION 'worker_count must be between 1 and 8 (got %)', worker_count;
    END IF;
    IF worker_slot NOT BETWEEN 1 AND worker_count THEN
        RAISE EXCEPTION
            'worker_slot must be between 1 and worker_count % (got %)',
            worker_count,
            worker_slot;
    END IF;
    IF tables_per_pass NOT BETWEEN 1 AND 16 THEN
        RAISE EXCEPTION
            'tables_per_pass must be between 1 and 16 (got %)',
            tables_per_pass;
    END IF;

    FOR pass_no IN 1..tables_per_pass LOOP
        SELECT count(*) FILTER (WHERE tick.executed)::integer
          INTO attempted
          FROM rvbbit.layout_tick_worker(
                   worker_slot,
                   worker_count,
                   false
               ) AS tick;
        COMMIT;
        EXIT WHEN coalesce(attempted, 0) = 0;
    END LOOP;
END;
$procedure$;

COMMENT ON PROCEDURE rvbbit.layout_tick_worker_pass(integer, integer, integer) IS
    'Runs up to tables_per_pass unified layout actions with a real commit between tables. Invoke with top-level CALL or through the scheduler helper.';

CREATE OR REPLACE FUNCTION rvbbit.schedule_layout_tick_worker_passes(
    cron_schedule text,
    workers integer,
    tables_per_pass integer
) RETURNS jsonb
LANGUAGE plpgsql
AS $function$
DECLARE
    cron_home text := current_setting('cron.database_name', true);
    this_db text := current_database();
    slot integer;
    stale record;
    jobid bigint;
    jobs jsonb := '[]'::jsonb;
    retired_jobs integer := 0;
    job_name text;
    command text;
    external_hint text;
BEGIN
    IF workers NOT BETWEEN 1 AND 8 THEN
        RAISE EXCEPTION 'workers must be between 1 and 8 (got %)', workers;
    END IF;
    IF tables_per_pass NOT BETWEEN 1 AND 16 THEN
        RAISE EXCEPTION
            'tables_per_pass must be between 1 and 16 (got %)',
            tables_per_pass;
    END IF;

    IF cron_home IS NOT NULL AND cron_home <> '' AND cron_home <> this_db THEN
        external_hint := format(
            'Connect to %I and run: SELECT cron.unschedule(jobid) FROM cron.job '
            'WHERE database = %L AND (jobname IN (%L, %L, %L) '
            'OR jobname ~ %L OR jobname ~ %L);',
            cron_home,
            this_db,
            'rvbbit_variant_tick',
            'rvbbit_workload_layout_tick',
            'rvbbit_layout_tick',
            '^rvbbit_workload_layout_tick_worker_[0-9]+$',
            '^rvbbit_layout_tick_worker_[0-9]+$'
        );
        FOR slot IN 1..workers LOOP
            external_hint := external_hint || format(
                ' SELECT cron.schedule_in_database(%L, %L, %L, %L);',
                format('rvbbit_layout_tick_worker_%s', slot),
                cron_schedule,
                format(
                    'CALL rvbbit.layout_tick_worker_pass(%s, %s, %s)',
                    slot,
                    workers,
                    tables_per_pass
                ),
                this_db
            );
        END LOOP;
        RAISE EXCEPTION 'pg_cron home database is %, not %; cron.* is not callable here.',
            cron_home, this_db
            USING HINT = external_hint;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        RAISE EXCEPTION 'pg_cron is not installed; cannot schedule layout workers.'
            USING HINT = 'Run top-level CALL rvbbit.layout_tick_worker_pass(slot, workers, tables_per_pass) from independent scheduler sessions.';
    END IF;

    FOR stale IN
        SELECT job.jobid
          FROM cron.job job
         WHERE job.database = this_db
           AND (
               job.jobname IN (
                   'rvbbit_variant_tick',
                   'rvbbit_workload_layout_tick',
                   'rvbbit_layout_tick'
               )
               OR job.jobname ~ '^rvbbit_workload_layout_tick_worker_[0-9]+$'
               OR job.jobname ~ '^rvbbit_layout_tick_worker_[0-9]+$'
           )
    LOOP
        EXECUTE 'SELECT cron.unschedule($1)' USING stale.jobid;
        retired_jobs := retired_jobs + 1;
    END LOOP;

    FOR slot IN 1..workers LOOP
        job_name := format('rvbbit_layout_tick_worker_%s', slot);
        command := format(
            'CALL rvbbit.layout_tick_worker_pass(%s, %s, %s)',
            slot,
            workers,
            tables_per_pass
        );
        EXECUTE 'SELECT cron.schedule($1, $2, $3)'
           INTO jobid
          USING job_name, cron_schedule, command;
        jobs := jobs || jsonb_build_array(jsonb_build_object(
            'jobid', jobid,
            'jobname', job_name,
            'slot', slot,
            'tables_per_pass', tables_per_pass,
            'command', command
        ));
    END LOOP;

    RETURN jsonb_build_object(
        'status', 'scheduled',
        'database', this_db,
        'schedule', cron_schedule,
        'workers', workers,
        'tables_per_pass', tables_per_pass,
        'maximum_tables_per_run', workers * tables_per_pass,
        'retired_legacy_jobs', retired_jobs,
        'jobs', jobs
    );
END;
$function$;

COMMENT ON FUNCTION rvbbit.schedule_layout_tick_worker_passes(text, integer, integer) IS
    'Replaces serial Vortex and workload-layout cron jobs with 1..8 unified derived-layout slots and an explicit per-slot pass size.';

CREATE OR REPLACE FUNCTION rvbbit.schedule_layout_tick_workers(
    cron_schedule text DEFAULT '* * * * *',
    workers integer DEFAULT 1
) RETURNS jsonb
LANGUAGE sql
VOLATILE
AS $function$
    SELECT rvbbit.schedule_layout_tick_worker_passes(
        cron_schedule,
        workers,
        4
    )
$function$;

COMMENT ON FUNCTION rvbbit.schedule_layout_tick_workers(text, integer) IS
    'Schedules the unified layout fleet with four separately committed tables per slot and retires legacy serial Vortex/workload jobs.';

-- Preserve the prior helper as a compatibility alias. Existing automation
-- that asks for workload-layout workers now receives the unified fleet.
CREATE OR REPLACE FUNCTION rvbbit.schedule_workload_layout_tick_workers(
    cron_schedule text DEFAULT '* * * * *',
    workers integer DEFAULT 1
) RETURNS jsonb
LANGUAGE sql
VOLATILE
AS $function$
    SELECT rvbbit.schedule_layout_tick_workers(cron_schedule, workers)
$function$;

COMMENT ON FUNCTION rvbbit.schedule_workload_layout_tick_workers(text, integer) IS
    'Compatibility alias for schedule_layout_tick_workers; Vortex and accepted layouts now share one worker fleet.';

-- Retain the unified append-only history alongside the older per-builder logs.
DO $migration$
DECLARE
    definition text;
    needle text := '(''rvbbit.workload_layout_tick_runs'', ''started_at''),';
    replacement text := needle || E'\n            (''rvbbit.layout_tick_runs'', ''started_at''),';
BEGIN
    definition := pg_get_functiondef(
        'rvbbit.reap_logs(interval)'::regprocedure
    );
    IF position('rvbbit.layout_tick_runs' IN definition) = 0 THEN
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0272 could not add unified layout runs to reap_logs';
        END IF;
        definition := replace(definition, needle, replacement);
        EXECUTE definition;
    END IF;
END;
$migration$;
