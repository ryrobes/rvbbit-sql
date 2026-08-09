-- 0271: Scale freshness slots without giving up the one-table transaction.
--
-- accel_tick_worker() remains one table per invocation. A top-level CALL to
-- accel_tick_worker_pass() may run several invocations serially, committing
-- after each table so relation and advisory locks never accumulate across the
-- pass. Slots prefer a stable OID partition and then steal unlocked work.

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

    IF position('worker_count BETWEEN 1 AND 8' IN definition) = 0 THEN
        needle := 'parallel_worker := worker_count = 2 AND worker_slot BETWEEN 1 AND 2;';
        replacement := E'parallel_worker := worker_count BETWEEN 1 AND 8\n'
            || '        AND worker_slot BETWEEN 1 AND worker_count;';
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0271 could not widen the freshness worker cohort';
        END IF;
        definition := replace(definition, needle, replacement);
        changed := true;
    END IF;

    IF position('mod(f.table_oid::bigint, worker_count::bigint)' IN definition) = 0 THEN
        needle := '         ORDER BY rvbbit.current_replacement_pending(f.table_oid) DESC,';
        replacement := E'         ORDER BY CASE\n'
            || E'                      WHEN parallel_worker THEN\n'
            || E'                          (mod(f.table_oid::bigint, worker_count::bigint)\n'
            || E'                              + 1 = worker_slot)::integer\n'
            || E'                      ELSE 1\n'
            || E'                  END DESC,\n'
            || '                  rvbbit.current_replacement_pending(f.table_oid) DESC,';
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0271 could not add stable freshness slot ordering';
        END IF;
        definition := replace(definition, needle, replacement);
        changed := true;
    END IF;

    IF changed THEN
        EXECUTE definition;
    END IF;
END;
$migration$;

DO $migration$
DECLARE
    definition text;
    needle text;
    replacement text;
BEGIN
    definition := pg_get_functiondef(
        'rvbbit.accel_tick_worker(integer,integer,boolean,integer)'::regprocedure
    );
    IF position('worker_count NOT BETWEEN 1 AND 8' IN definition) = 0 THEN
        needle := 'IF worker_count NOT BETWEEN 1 AND 2 THEN';
        replacement := 'IF worker_count NOT BETWEEN 1 AND 8 THEN';
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0271 could not widen accel_tick_worker validation';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := 'worker_count must be between 1 and 2 (got %)';
        replacement := 'worker_count must be between 1 and 8 (got %)';
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0271 could not update accel_tick_worker validation message';
        END IF;
        definition := replace(definition, needle, replacement);
        EXECUTE definition;
    END IF;
END;
$migration$;

COMMENT ON FUNCTION rvbbit._accel_tick_batch(integer, boolean, integer) IS
    'Internal one-transaction freshness body. Up to eight worker slots use stable table preferences, work stealing, shared lane gating, non-waiting claims, and a singleton heavy-action gate.';

COMMENT ON FUNCTION rvbbit.accel_tick_worker(integer, integer, boolean, integer) IS
    'One bounded freshness worker. Slots 1..worker_count (up to eight) may refresh distinct tables concurrently while collisions and folds are skipped without waiting.';

CREATE OR REPLACE PROCEDURE rvbbit.accel_tick_worker_pass(
    worker_slot integer,
    worker_count integer DEFAULT 2,
    tables_per_pass integer DEFAULT 4,
    lance_budget integer DEFAULT 1
)
LANGUAGE plpgsql
AS $procedure$
DECLARE
    pass_no integer;
    completed integer;
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
    IF lance_budget < 0 THEN
        RAISE EXCEPTION 'lance_budget must be non-negative (got %)', lance_budget;
    END IF;

    FOR pass_no IN 1..tables_per_pass LOOP
        SELECT count(*) FILTER (
                   WHERE tick.executed
                     AND tick.status IS DISTINCT FROM 'failed'
               )::integer
          INTO completed
          FROM rvbbit.accel_tick_worker(
                   worker_slot,
                   worker_count,
                   false,
                   lance_budget
               ) AS tick;

        -- Transaction control is why this is a PROCEDURE rather than another
        -- function. Each completed table releases its locks before the next
        -- candidate is considered. CALL must therefore be top-level, as it is
        -- when pg_cron executes the command.
        COMMIT;
        EXIT WHEN coalesce(completed, 0) = 0;
    END LOOP;
END;
$procedure$;

COMMENT ON PROCEDURE rvbbit.accel_tick_worker_pass(integer, integer, integer, integer) IS
    'Runs up to tables_per_pass freshness actions serially with a real commit between tables. Invoke with top-level CALL or through schedule_accel_tick_workers.';

CREATE OR REPLACE FUNCTION rvbbit.schedule_accel_tick_worker_passes(
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
            'WHERE database = %L AND (jobname = %L OR jobname ~ %L);',
            cron_home,
            this_db,
            'rvbbit_accel_tick',
            '^rvbbit_accel_tick_worker_[0-9]+$'
        );
        FOR slot IN 1..workers LOOP
            external_hint := external_hint || format(
                ' SELECT cron.schedule_in_database(%L, %L, %L, %L);',
                format('rvbbit_accel_tick_worker_%s', slot),
                cron_schedule,
                format(
                    'CALL rvbbit.accel_tick_worker_pass(%s, %s, %s, 1)',
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
        RAISE EXCEPTION 'pg_cron is not installed; cannot schedule accelerator workers.'
            USING HINT = 'Run top-level CALL rvbbit.accel_tick_worker_pass(slot, workers, tables_per_pass, 1) from independent scheduler sessions.';
    END IF;

    FOR stale IN
        SELECT jobid
          FROM cron.job
         WHERE jobname = 'rvbbit_accel_tick'
            OR jobname ~ '^rvbbit_accel_tick_worker_[0-9]+$'
    LOOP
        EXECUTE 'SELECT cron.unschedule($1)' USING stale.jobid;
    END LOOP;

    FOR slot IN 1..workers LOOP
        job_name := format('rvbbit_accel_tick_worker_%s', slot);
        command := format(
            'CALL rvbbit.accel_tick_worker_pass(%s, %s, %s, 1)',
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
        'jobs', jobs
    );
END;
$function$;

COMMENT ON FUNCTION rvbbit.schedule_accel_tick_worker_passes(text, integer, integer) IS
    'Schedules 1..8 freshness slots, each processing up to tables_per_pass tables with a commit between tables.';

-- Preserve the original two-argument helper while making four serial,
-- transaction-isolated tables per slot the ergonomic default.
CREATE OR REPLACE FUNCTION rvbbit.schedule_accel_tick_workers(
    cron_schedule text DEFAULT '* * * * *',
    workers integer DEFAULT 2
) RETURNS jsonb
LANGUAGE sql
VOLATILE
AS $function$
    SELECT rvbbit.schedule_accel_tick_worker_passes(
        cron_schedule,
        workers,
        4
    )
$function$;

COMMENT ON FUNCTION rvbbit.schedule_accel_tick_workers(text, integer) IS
    'Schedules 1..8 freshness slots with four serial, separately committed tables per slot and per cron run. Use schedule_accel_tick_worker_passes for a different pass size.';
