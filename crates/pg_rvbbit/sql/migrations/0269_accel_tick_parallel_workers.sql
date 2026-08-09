-- 0269: Bounded parallel accelerator freshness workers.
--
-- Parallelism lives above the one-table transaction boundary introduced by
-- 0268. Two independent pg_cron jobs may therefore refresh two unrelated
-- tables at once without bringing back the old multi-table lock lifetime.
--
-- Lock hierarchy (all transaction-scoped and non-waiting at this layer):
--   gate (1381187156, 7) shared    parallel freshness workers
--   gate (1381187156, 7) exclusive legacy accel_tick / accel_fold_tick
--   slot (1381187156, 71..72)      one backend per configured worker slot
--   table 0x52564254 || table_oid  one maintenance action per table
--   heavy (1381187156, 8)          one full/Lance/Vortex action at a time
--
-- refresh_acceleration/rebuild_acceleration retain their own correctness
-- locks. The table try-lock here is an early claim: a second worker skips a
-- busy candidate instead of waiting behind it and redundantly reconsidering
-- the same table after the first worker commits.

DO $migration$
DECLARE
    definition text;
    needle text;
    replacement text;
BEGIN
    SELECT pg_get_functiondef(
               'rvbbit._accel_tick_batch(integer,boolean,integer)'::regprocedure
           )
      INTO definition;

    IF definition IS NULL THEN
        RAISE EXCEPTION
            'rvbbit._accel_tick_batch(integer,boolean,integer) is required before migration 0269';
    END IF;

    -- Idempotence for extension SQL replay as well as rvbbit.migrate().
    IF position('rvbbit.accel_tick_worker_slot' IN definition) = 0 THEN
        needle := $needle$
    total_rows_written bigint := 0;
BEGIN
$needle$;
        replacement := $replacement$
    total_rows_written bigint := 0;
    live_candidate record;
    worker_slot_setting text := current_setting('rvbbit.accel_tick_worker_slot', true);
    worker_count_setting text := current_setting('rvbbit.accel_tick_worker_count', true);
    worker_slot integer := CASE
        WHEN worker_slot_setting ~ '^[0-9]+$' THEN worker_slot_setting::integer
        ELSE 0
    END;
    worker_count integer := CASE
        WHEN worker_count_setting ~ '^[0-9]+$' THEN worker_count_setting::integer
        ELSE 1
    END;
    parallel_worker boolean := false;
    fallback_deferred boolean := false;
    delta_error text;
BEGIN
    parallel_worker := worker_count = 2 AND worker_slot BETWEEN 1 AND 2;
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0269 could not add accel_tick worker state';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := $needle$
    IF NOT dry_run
       AND lower(coalesce(nullif(current_setting('rvbbit.route_gpu_gqe_prior', true), ''), 'off'))
$needle$;
        replacement := $replacement$
    IF NOT dry_run
       AND (NOT parallel_worker OR worker_slot = 1)
       AND lower(coalesce(nullif(current_setting('rvbbit.route_gpu_gqe_prior', true), ''), 'off'))
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0269 could not assign warm-prior ownership';
        END IF;
        -- Warming is a heartbeat concern, not table work. Only one of the two
        -- workers should perform it per scheduled interval.
        definition := replace(definition, needle, replacement);

        needle := $needle$
    IF NOT dry_run AND NOT pg_try_advisory_xact_lock(1381187156, 7) THEN
        RETURN;
    END IF;
$needle$;
        replacement := $replacement$
    IF NOT dry_run THEN
        IF parallel_worker THEN
            -- Shared gate lets the two freshness slots coexist while the
            -- existing exclusive fold/legacy gate still keeps lanes apart.
            IF NOT pg_try_advisory_xact_lock_shared(1381187156, 7) THEN
                RETURN;
            END IF;
            IF NOT pg_try_advisory_xact_lock(
                1381187156,
                70 + worker_slot
            ) THEN
                RETURN;
            END IF;
        ELSIF NOT pg_try_advisory_xact_lock(1381187156, 7) THEN
            RETURN;
        END IF;
    END IF;
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0269 could not replace accel_tick singleton gate';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := $needle$left(format('rvbbit/fresh s=%s scan', sweep_id), 63)$needle$;
        replacement := $replacement$left(
                format(
                    'rvbbit/fresh s=%s w=%s/%s scan',
                    sweep_id,
                    CASE WHEN parallel_worker THEN worker_slot ELSE 1 END,
                    CASE WHEN parallel_worker THEN worker_count ELSE 1 END
                ),
                63
            )$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0269 could not annotate accel_tick application_name';
        END IF;
        -- Replaces both the initial scan label and the post-table scan label.
        definition := replace(definition, needle, replacement);

        needle := $needle$                'mode', 'freshness_only'
$needle$;
        replacement := $replacement$                'mode', CASE
                    WHEN parallel_worker THEN 'parallel_freshness'
                    ELSE 'freshness_only'
                END,
                'worker_slot', CASE WHEN parallel_worker THEN worker_slot ELSE 1 END,
                'worker_count', CASE WHEN parallel_worker THEN worker_count ELSE 1 END
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0269 could not annotate accel_tick sweep details';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := $needle$
        _accel_tick_batch.error := NULL;

        IF NOT do_execute THEN
$needle$;
        replacement := $replacement$
        _accel_tick_batch.error := NULL;

        IF do_execute AND NOT dry_run AND parallel_worker THEN
            -- Claim before entering refresh/rebuild. The lower-level lock is
            -- re-entrant for rebuild/compact and harmless for delta refresh.
            IF NOT pg_try_advisory_xact_lock(
                (1380336724::bigint << 32) | cand.f_oid::bigint
            ) THEN
                do_execute := false;
                act_reason := 'table maintenance claimed by another worker';
            ELSE
                -- A manual action or another worker may have committed after
                -- this loop's candidate snapshot. Re-read cheap eligibility
                -- facts after the claim and never act on an obsolete policy.
                SELECT f.shadow_heap_dirty,
                       f.seconds_since_refresh,
                       f.row_groups,
                       f.heap_live_tuples,
                       rvbbit.current_replacement_pending(f.table_oid)
                           AS current_replacement_pending,
                       e.active,
                       e.strategy,
                       e.min_interval_secs,
                       e.daily_refresh_budget
                  INTO live_candidate
                  FROM rvbbit.accel_freshness f
                  JOIN rvbbit.accel_policy_effective e
                    ON e.table_oid = f.table_oid
                 WHERE f.table_oid = cand.f_oid;

                IF NOT FOUND
                   OR NOT coalesce(live_candidate.active, false)
                   OR live_candidate.strategy = 'manual' THEN
                    should_act := false;
                    do_execute := false;
                    act_reason := 'table or policy changed before worker claim';
                ELSIF live_candidate.strategy IS DISTINCT FROM cand.strategy THEN
                    do_execute := false;
                    act_reason := 'refresh strategy changed before worker claim';
                ELSIF NOT (
                    coalesce(live_candidate.current_replacement_pending, false)
                    OR coalesce(live_candidate.shadow_heap_dirty, false)
                    OR (
                        coalesce(live_candidate.row_groups, 0) = 0
                        AND coalesce(live_candidate.heap_live_tuples, 0) > 0
                    )
                ) THEN
                    should_act := false;
                    do_execute := false;
                    act_reason := 'candidate became clean before worker claim';
                ELSIF live_candidate.seconds_since_refresh IS NOT NULL
                      AND live_candidate.seconds_since_refresh
                          < live_candidate.min_interval_secs THEN
                    do_execute := false;
                    act_reason := format(
                        'min_interval %ss not elapsed after worker claim',
                        live_candidate.min_interval_secs
                    );
                ELSE
                    -- Recheck the only mutable accounting gate after claiming.
                    IF live_candidate.daily_refresh_budget IS NOT NULL THEN
                        SELECT count(*)
                          INTO used_today
                          FROM rvbbit.accel_tick_runs r
                         WHERE r.table_oid = cand.f_oid
                           AND r.executed
                           AND r.ran_at > now() - interval '24 hours';
                        IF used_today >= live_candidate.daily_refresh_budget THEN
                            do_execute := false;
                            act_reason := format(
                                'daily budget %s exhausted after worker claim',
                                live_candidate.daily_refresh_budget
                            );
                        END IF;
                    END IF;

                    -- Full rebuilds and separate Lance construction are the
                    -- heavy lane. Two deltas may overlap, but only one heavy
                    -- action may consume I/O at a time.
                    IF do_execute
                       AND (prop_action = 'full' OR is_lance)
                       AND NOT pg_try_advisory_xact_lock(1381187156, 8) THEN
                        do_execute := false;
                        act_reason := prop_reason || '; heavy maintenance lane busy';
                    END IF;
                END IF;
            END IF;
        END IF;

        IF NOT do_execute THEN
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0269 could not add accel_tick table claims';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := $needle$
        written_row_groups := NULL;
        PERFORM set_config(
$needle$;
        replacement := $replacement$
        written_row_groups := NULL;
        fallback_deferred := false;
        delta_error := NULL;
        PERFORM set_config(
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0269 could not initialize delta fallback state';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := $needle$
                EXCEPTION WHEN OTHERS THEN
                    prop_action := 'full';
                    act_reason := act_reason || ' (delta->full: ' || SQLERRM || ')';
                    PERFORM set_config(
                        'application_name',
                        left(
                            format(
                                'rvbbit/fresh s=%s o=%s a=full t=%s %s',
                                sweep_id,
                                cand.f_oid,
                                round(extract(epoch FROM table_started)::numeric, 3),
                                cand.f_name
                            ),
                            63
                        ),
                        true
                    );
                    res := rvbbit.rebuild_acceleration(cand.f_oid::regclass, true);
                END;
            END IF;
            _accel_tick_batch.action := prop_action;
            _accel_tick_batch.reason := act_reason;
            _accel_tick_batch.executed := true;
            _accel_tick_batch.status := coalesce(res->>'status', 'ok');
            _accel_tick_batch.rows_written := coalesce((res->>'rows_written')::bigint, 0);
            operation_id := nullif(res->>'operation_id', '')::bigint;
            written_row_groups := nullif(res->>'row_groups_written', '')::bigint;
$needle$;
        replacement := $replacement$
                EXCEPTION WHEN OTHERS THEN
                    delta_error := SQLERRM;
                    prop_action := 'full';
                    act_reason := act_reason || ' (delta->full: ' || delta_error || ')';
                    IF parallel_worker
                       AND NOT pg_try_advisory_xact_lock(1381187156, 8) THEN
                        -- Do not wait behind a long full rebuild. A later tick
                        -- can retry the delta or acquire the heavy lane.
                        fallback_deferred := true;
                        act_reason := act_reason || '; heavy maintenance lane busy';
                        res := jsonb_build_object(
                            'status', 'deferred',
                            'reason', act_reason
                        );
                    ELSE
                        PERFORM set_config(
                            'application_name',
                            left(
                                format(
                                    'rvbbit/fresh s=%s o=%s a=full t=%s %s',
                                    sweep_id,
                                    cand.f_oid,
                                    round(extract(epoch FROM table_started)::numeric, 3),
                                    cand.f_name
                                ),
                                63
                            ),
                            true
                        );
                        res := rvbbit.rebuild_acceleration(cand.f_oid::regclass, true);
                    END IF;
                END;
            END IF;
            _accel_tick_batch.action := prop_action;
            _accel_tick_batch.reason := act_reason;
            IF fallback_deferred THEN
                _accel_tick_batch.executed := false;
                _accel_tick_batch.status := 'deferred';
                _accel_tick_batch.rows_written := 0;
                acted := greatest(acted - 1, 0);
                IF is_lance THEN
                    lance_acted := greatest(lance_acted - 1, 0);
                END IF;
                deferred_count := deferred_count + 1;
            ELSE
                _accel_tick_batch.executed := true;
                _accel_tick_batch.status := coalesce(res->>'status', 'ok');
                _accel_tick_batch.rows_written := coalesce((res->>'rows_written')::bigint, 0);
                operation_id := nullif(res->>'operation_id', '')::bigint;
                written_row_groups := nullif(res->>'row_groups_written', '')::bigint;
            END IF;
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0269 could not guard delta-to-full fallback';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := $needle$                    'rvbbit/fresh s=%s o=%s a=%s t=%s %s',
                    sweep_id,
                    cand.f_oid,
$needle$;
        replacement := $replacement$                    'rvbbit/fresh s=%s w=%s/%s o=%s a=%s t=%s %s',
                    sweep_id,
                    CASE WHEN parallel_worker THEN worker_slot ELSE 1 END,
                    CASE WHEN parallel_worker THEN worker_count ELSE 1 END,
                    cand.f_oid,
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0269 could not annotate live table activity';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := $needle$                                    'rvbbit/fresh s=%s o=%s a=full t=%s %s',
                                    sweep_id,
                                    cand.f_oid,
$needle$;
        replacement := $replacement$                                    'rvbbit/fresh s=%s w=%s/%s o=%s a=full t=%s %s',
                                    sweep_id,
                                    CASE WHEN parallel_worker THEN worker_slot ELSE 1 END,
                                    CASE WHEN parallel_worker THEN worker_count ELSE 1 END,
                                    cand.f_oid,
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0269 could not annotate live fallback activity';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := $needle$            _accel_tick_batch.reason, cand.drift_rows, cand.heap_seq_scans, true,
$needle$;
        replacement := $replacement$            _accel_tick_batch.reason, cand.drift_rows, cand.heap_seq_scans,
            _accel_tick_batch.executed,
$replacement$;
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0269 could not preserve executed accounting';
        END IF;
        definition := replace(definition, needle, replacement);

        EXECUTE definition;
    END IF;
END;
$migration$;

COMMENT ON FUNCTION rvbbit._accel_tick_batch(integer, boolean, integer) IS
    'Internal one-transaction freshness body. Parallel worker context uses shared lane gating, non-waiting worker/table claims, and a singleton heavy-action gate.';

-- Keep the familiar function deterministic even if it is called in the same
-- transaction after accel_tick_worker(): explicitly reset worker context.
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
LANGUAGE plpgsql
VOLATILE
AS $function$
BEGIN
    PERFORM set_config('rvbbit.accel_tick_worker_slot', '0', true);
    PERFORM set_config('rvbbit.accel_tick_worker_count', '1', true);
    RETURN QUERY
    SELECT batch.*
      FROM rvbbit._accel_tick_batch(
          CASE
              WHEN dry_run THEN budget
              WHEN coalesce(budget, 1) <= 0 THEN 0
              ELSE 1
          END,
          dry_run,
          lance_budget
      ) AS batch;
END;
$function$;

COMMENT ON FUNCTION rvbbit.accel_tick(integer, boolean, integer) IS
    'Compatibility singleton freshness heartbeat. Execution remains capped at one table/transaction; use accel_tick_worker from distinct scheduler jobs for bounded parallelism.';

CREATE OR REPLACE FUNCTION rvbbit.accel_tick_worker(
    worker_slot integer,
    worker_count integer DEFAULT 2,
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
LANGUAGE plpgsql
VOLATILE
AS $function$
BEGIN
    IF worker_count NOT BETWEEN 1 AND 2 THEN
        RAISE EXCEPTION 'worker_count must be between 1 and 2 (got %)', worker_count;
    END IF;
    IF worker_slot NOT BETWEEN 1 AND worker_count THEN
        RAISE EXCEPTION
            'worker_slot must be between 1 and worker_count % (got %)',
            worker_count,
            worker_slot;
    END IF;

    PERFORM set_config(
        'rvbbit.accel_tick_worker_slot',
        worker_slot::text,
        true
    );
    PERFORM set_config(
        'rvbbit.accel_tick_worker_count',
        worker_count::text,
        true
    );

    -- Exactly one table per invocation/transaction remains non-negotiable.
    RETURN QUERY
    SELECT batch.*
      FROM rvbbit._accel_tick_batch(1, dry_run, lance_budget) AS batch;
END;
$function$;

COMMENT ON FUNCTION rvbbit.accel_tick_worker(integer, integer, boolean, integer) IS
    'One bounded freshness worker. Schedule slots 1..worker_count as distinct jobs; two slots may refresh different tables concurrently while collisions and folds are skipped without waiting.';

CREATE OR REPLACE FUNCTION rvbbit.schedule_accel_tick_workers(
    cron_schedule text DEFAULT '* * * * *',
    workers integer DEFAULT 2
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
BEGIN
    IF workers NOT BETWEEN 1 AND 2 THEN
        RAISE EXCEPTION 'workers must be between 1 and 2 (got %)', workers;
    END IF;
    IF cron_home IS NOT NULL AND cron_home <> '' AND cron_home <> this_db THEN
        RAISE EXCEPTION 'pg_cron home database is %, not %; cron.* is not callable here.',
            cron_home, this_db
            USING HINT = CASE
                WHEN workers = 1 THEN format(
                    'Connect to %I and run: SELECT cron.unschedule(jobid) FROM cron.job '
                    'WHERE database = %L AND (jobname = %L OR jobname ~ %L); '
                    'SELECT cron.schedule_in_database(%L, %L, %L, %L);',
                    cron_home,
                    this_db,
                    'rvbbit_accel_tick',
                    '^rvbbit_accel_tick_worker_[0-9]+$',
                    'rvbbit_accel_tick_worker_1',
                    cron_schedule,
                    'SELECT rvbbit.accel_tick_worker(1, 1, false, 1)',
                    this_db
                )
                ELSE format(
                    'Connect to %I and run: SELECT cron.unschedule(jobid) FROM cron.job '
                    'WHERE database = %L AND (jobname = %L OR jobname ~ %L); '
                    'SELECT cron.schedule_in_database(%L, %L, %L, %L); '
                    'SELECT cron.schedule_in_database(%L, %L, %L, %L);',
                    cron_home,
                    this_db,
                    'rvbbit_accel_tick',
                    '^rvbbit_accel_tick_worker_[0-9]+$',
                    'rvbbit_accel_tick_worker_1',
                    cron_schedule,
                    'SELECT rvbbit.accel_tick_worker(1, 2, false, 1)',
                    this_db,
                    'rvbbit_accel_tick_worker_2',
                    cron_schedule,
                    'SELECT rvbbit.accel_tick_worker(2, 2, false, 1)',
                    this_db
                )
            END;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        RAISE EXCEPTION 'pg_cron is not installed; cannot schedule accelerator workers.'
            USING HINT = 'Call rvbbit.accel_tick_worker(slot, workers, false, 1) from two independent scheduler transactions.';
    END IF;

    -- Switching modes is explicit and atomic: remove the singleton and any
    -- prior worker layout before installing the requested bounded set.
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
            'SELECT rvbbit.accel_tick_worker(%s, %s, false, 1)',
            slot,
            workers
        );
        EXECUTE 'SELECT cron.schedule($1, $2, $3)'
           INTO jobid
          USING job_name, cron_schedule, command;
        jobs := jobs || jsonb_build_array(jsonb_build_object(
            'jobid', jobid,
            'jobname', job_name,
            'slot', slot,
            'command', command
        ));
    END LOOP;

    RETURN jsonb_build_object(
        'status', 'scheduled',
        'database', this_db,
        'schedule', cron_schedule,
        'workers', workers,
        'jobs', jobs
    );
END;
$function$;

COMMENT ON FUNCTION rvbbit.schedule_accel_tick_workers(text, integer) IS
    'Replaces the singleton freshness cron job with one or two distinct one-table worker jobs. Parallelism is opt-in and capped at two.';

-- Calling the original helper is also an explicit switch back to singleton
-- mode. Without this cleanup, an administrator could accidentally leave the
-- two named worker jobs beside the legacy job after changing their mind.
CREATE OR REPLACE FUNCTION rvbbit.schedule_accel_tick(
    cron_schedule text DEFAULT '* * * * *',
    budget integer DEFAULT 1
) RETURNS bigint
LANGUAGE plpgsql
AS $function$
DECLARE
    jobid bigint;
    stale record;
    cron_home text := current_setting('cron.database_name', true);
    this_db text := current_database();
    safe_budget integer := CASE WHEN coalesce(budget, 1) <= 0 THEN 0 ELSE 1 END;
    command text := format('SELECT rvbbit.accel_tick(%s, false)', safe_budget);
BEGIN
    IF cron_home IS NOT NULL AND cron_home <> '' AND cron_home <> this_db THEN
        RAISE EXCEPTION 'pg_cron home database is %, not %; cron.* is not callable here.',
            cron_home, this_db
            USING HINT = format(
                'Connect to %I and run: SELECT cron.unschedule(jobid) FROM cron.job '
                'WHERE database = %L AND jobname ~ %L; '
                'SELECT cron.schedule_in_database(%L, %L, %L, %L);',
                cron_home,
                this_db,
                '^rvbbit_accel_tick_worker_[0-9]+$',
                'rvbbit_accel_tick',
                cron_schedule,
                command,
                this_db
            );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        RAISE EXCEPTION 'pg_cron is not installed; cannot schedule the accelerator heartbeat.'
            USING HINT = 'Add pg_cron to shared_preload_libraries and CREATE EXTENSION pg_cron, or call rvbbit.accel_tick(1, false) manually.';
    END IF;

    FOR stale IN
        SELECT jobid
          FROM cron.job
         WHERE jobname ~ '^rvbbit_accel_tick_worker_[0-9]+$'
    LOOP
        EXECUTE 'SELECT cron.unschedule($1)' USING stale.jobid;
    END LOOP;

    EXECUTE 'SELECT cron.schedule($1, $2, $3)'
       INTO jobid
      USING 'rvbbit_accel_tick', cron_schedule, command;
    RETURN jobid;
END;
$function$;

COMMENT ON FUNCTION rvbbit.schedule_accel_tick(text, integer) IS
    'Schedules singleton one-table freshness mode and removes any named parallel worker jobs. Use schedule_accel_tick_workers to opt into two workers.';
