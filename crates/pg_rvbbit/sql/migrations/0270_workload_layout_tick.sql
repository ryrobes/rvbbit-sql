-- 0270: Give accepted non-Vortex layouts their own observable heartbeat.
--
-- Vortex remains on variant_tick(), where it is derived from canonical
-- Parquet and repaired through variant_build_queue.  Accepted cluster/Hive
-- layouts are discovered from current catalog truth on every tick, so a fresh
-- Vortex file can never mask a missing governed workload layout.
--
-- Lock lanes:
--   gate (1381187156, 7) shared     coexist with freshness; exclude folds
--   gate (1381187156, 8) shared     workload workers may coexist; exclude
--                                  Vortex/full/Lance heavy maintenance
--   slot (1381187156, 91..98)       one backend per configured worker slot
--   table bigint advisory lock      one maintenance writer per table

CREATE OR REPLACE FUNCTION rvbbit.refresh_workload_layout_variants(rel oid)
RETURNS bigint
LANGUAGE c
STRICT
AS '$libdir/pg_rvbbit', 'refresh_workload_layout_variants_wrapper';

-- The workload advisor predates the recent extension_sql_file migrations and
-- historically created this table only when rvbbit.migrate() ran after
-- CREATE EXTENSION.  Seed its idempotent prerequisite here as well so a fresh
-- extension install can create the live candidate view before that first
-- migrate call. Migration 0109 will harmlessly preserve it afterward.
CREATE TABLE IF NOT EXISTS rvbbit.workload_layout_recommendations (
    table_oid      oid NOT NULL REFERENCES rvbbit.tables(table_oid) ON DELETE CASCADE,
    layout_kind    text NOT NULL,
    column_name    text NOT NULL,
    layout         text NOT NULL,
    score          double precision NOT NULL DEFAULT 0,
    observations   bigint NOT NULL DEFAULT 0,
    weighted_ms    double precision NOT NULL DEFAULT 0,
    role_counts    jsonb NOT NULL DEFAULT '{}'::jsonb,
    sample_shapes  text[] NOT NULL DEFAULT ARRAY[]::text[],
    reason         text NOT NULL DEFAULT '',
    details        jsonb NOT NULL DEFAULT '{}'::jsonb,
    status         text NOT NULL DEFAULT 'candidate',
    recommended_at timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, layout_kind, column_name),
    CHECK (layout_kind IN ('cluster', 'hive')),
    CHECK (status IN ('candidate', 'accepted', 'rejected', 'retired')),
    CHECK (score >= 0),
    CHECK (observations >= 0),
    CHECK (weighted_ms >= 0)
);

CREATE INDEX IF NOT EXISTS workload_layout_recommendations_status_idx
    ON rvbbit.workload_layout_recommendations
       (status, score DESC, updated_at DESC);

CREATE OR REPLACE FUNCTION rvbbit.workload_layout_variants_pending(rel oid)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    WITH canonical AS (
        SELECT count(*)::bigint AS row_groups,
               coalesce(sum(rg.n_rows), 0)::bigint AS rows,
               max(rg.created_at) AS newest_row_group
          FROM rvbbit.row_groups rg
         WHERE rg.table_oid = rel
    )
    SELECT coalesce(bool_or(
        s.status IS DISTINCT FROM 'ready'
        OR (
            canonical.row_groups > 0
            AND (
                s.expected_rows IS DISTINCT FROM canonical.rows
                OR s.actual_rows IS DISTINCT FROM canonical.rows
                OR s.refreshed_at < canonical.newest_row_group
            )
        )
    ), false)
      FROM rvbbit.workload_layout_recommendations r
      CROSS JOIN canonical
      LEFT JOIN rvbbit.layout_variant_status s
        ON s.table_oid = r.table_oid
       AND lower(s.layout) = lower(r.layout)
     WHERE r.table_oid = rel
       AND r.status = 'accepted'
$$;

COMMENT ON FUNCTION rvbbit.workload_layout_variants_pending(oid) IS
    'True when an accepted cluster/Hive layout is missing, invalid, row-count stale, or older than its canonical Parquet input.';

CREATE OR REPLACE VIEW rvbbit.workload_layout_tick_candidates AS
WITH canonical AS (
    SELECT t.table_oid,
           t.table_oid::regclass::text AS table_name,
           count(rg.rg_id)::bigint AS target_row_groups,
           coalesce(sum(rg.n_rows), 0)::bigint AS target_rows,
           coalesce(max(rg.generation), 0)::bigint AS target_generation,
           max(rg.created_at) AS newest_row_group
      FROM rvbbit.tables t
      JOIN pg_catalog.pg_class c
        ON c.oid = t.table_oid
      LEFT JOIN rvbbit.row_groups rg
        ON rg.table_oid = t.table_oid
     WHERE c.relkind IN ('r', 'p')
       AND coalesce(t.acceleration_enabled, true)
     GROUP BY t.table_oid
), accepted AS (
    SELECT r.table_oid,
           count(*)::integer AS accepted_layouts,
           count(*) FILTER (
               WHERE s.status IS DISTINCT FROM 'ready'
                  OR s.expected_rows IS DISTINCT FROM canonical.target_rows
                  OR s.actual_rows IS DISTINCT FROM canonical.target_rows
                  OR s.refreshed_at < canonical.newest_row_group
           )::integer AS pending_layouts,
           array_agg(r.layout ORDER BY r.layout_kind, r.column_name)
               AS accepted_layout_names,
           array_agg(r.layout ORDER BY r.layout_kind, r.column_name) FILTER (
               WHERE s.status IS DISTINCT FROM 'ready'
                  OR s.expected_rows IS DISTINCT FROM canonical.target_rows
                  OR s.actual_rows IS DISTINCT FROM canonical.target_rows
                  OR s.refreshed_at < canonical.newest_row_group
           ) AS pending_layout_names,
           coalesce(max(r.score), 0)::double precision AS priority,
           max(r.updated_at) AS recommendation_updated_at
      FROM rvbbit.workload_layout_recommendations r
      JOIN canonical
        ON canonical.table_oid = r.table_oid
      LEFT JOIN rvbbit.layout_variant_status s
        ON s.table_oid = r.table_oid
       AND lower(s.layout) = lower(r.layout)
     WHERE r.status = 'accepted'
     GROUP BY r.table_oid
)
SELECT canonical.table_oid,
       canonical.table_name,
       canonical.target_generation,
       canonical.target_row_groups,
       canonical.target_rows,
       canonical.newest_row_group,
       accepted.accepted_layouts,
       accepted.pending_layouts,
       accepted.accepted_layout_names,
       accepted.pending_layout_names,
       accepted.priority,
       accepted.recommendation_updated_at
  FROM canonical
  JOIN accepted
    ON accepted.table_oid = canonical.table_oid
 WHERE canonical.target_row_groups > 0
   AND accepted.pending_layouts > 0;

COMMENT ON VIEW rvbbit.workload_layout_tick_candidates IS
    'Live backlog of accepted cluster/Hive layouts that are missing or stale relative to canonical Parquet. Vortex is intentionally excluded.';

CREATE TABLE IF NOT EXISTS rvbbit.workload_layout_tick_runs (
    id                  bigserial PRIMARY KEY,
    table_oid           oid REFERENCES rvbbit.tables(table_oid) ON DELETE SET NULL,
    table_name          text NOT NULL,
    target_generation   bigint NOT NULL DEFAULT 0,
    target_row_groups   bigint NOT NULL DEFAULT 0,
    target_rows         bigint NOT NULL DEFAULT 0,
    target_layouts      text[] NOT NULL DEFAULT ARRAY[]::text[],
    worker_slot         integer NOT NULL DEFAULT 1,
    worker_count        integer NOT NULL DEFAULT 1,
    attempt             integer NOT NULL DEFAULT 1,
    action              text NOT NULL DEFAULT 'build_workload_layouts',
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

CREATE INDEX IF NOT EXISTS workload_layout_tick_runs_table_time_idx
    ON rvbbit.workload_layout_tick_runs (table_oid, started_at DESC);
CREATE INDEX IF NOT EXISTS workload_layout_tick_runs_retry_idx
    ON rvbbit.workload_layout_tick_runs (retry_at)
    WHERE retry_at IS NOT NULL;

ALTER TABLE rvbbit.workload_layout_tick_runs SET (
    autovacuum_vacuum_scale_factor = 0.01,
    autovacuum_vacuum_threshold = 100,
    autovacuum_analyze_scale_factor = 0.02,
    autovacuum_analyze_threshold = 100
);

CREATE OR REPLACE FUNCTION rvbbit.workload_layout_tick_worker(
    worker_slot integer,
    worker_count integer DEFAULT 1,
    dry_run boolean DEFAULT false
)
RETURNS TABLE (
    table_oid oid,
    table_name text,
    target_generation bigint,
    accepted_layouts integer,
    pending_layouts integer,
    target_layouts text[],
    action text,
    executed boolean,
    status text,
    rows_written bigint,
    error text
)
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    cand record;
    live record;
    build_rows bigint;
    started timestamptz;
    attempt_no integer;
    retry_seconds integer;
    still_pending boolean;
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
        -- Shared gates allow future workload-layout workers to coexist while
        -- preserving the existing exclusive fold and heavy-maintenance lanes.
        IF NOT pg_try_advisory_xact_lock_shared(1381187156, 7) THEN
            RETURN;
        END IF;
        IF NOT pg_try_advisory_xact_lock_shared(1381187156, 8) THEN
            RETURN;
        END IF;
        IF NOT pg_try_advisory_xact_lock(1381187156, 90 + worker_slot) THEN
            RETURN;
        END IF;
    END IF;

    FOR cand IN
        SELECT c.*,
               last_run.status AS last_status,
               last_run.attempt AS last_attempt,
               last_run.retry_at AS last_retry_at,
               last_run.target_generation AS last_target_generation,
               last_run.target_layouts AS last_target_layouts
          FROM rvbbit.workload_layout_tick_candidates c
          LEFT JOIN LATERAL (
              SELECT r.status,
                     r.attempt,
                     r.retry_at,
                     r.target_generation,
                     r.target_layouts
                FROM rvbbit.workload_layout_tick_runs r
               WHERE r.table_oid = c.table_oid
               ORDER BY r.id DESC
               LIMIT 1
          ) last_run ON true
         WHERE dry_run
            OR last_run.retry_at IS NULL
            OR last_run.retry_at <= clock_timestamp()
            OR last_run.target_generation IS DISTINCT FROM c.target_generation
            OR last_run.target_layouts IS DISTINCT FROM c.accepted_layout_names
         ORDER BY
               (mod(c.table_oid::bigint, worker_count) + 1 = worker_slot) DESC,
               c.priority DESC,
               c.recommendation_updated_at,
               c.table_oid
    LOOP
        workload_layout_tick_worker.table_oid := cand.table_oid;
        workload_layout_tick_worker.table_name := cand.table_name;
        workload_layout_tick_worker.target_generation := cand.target_generation;
        workload_layout_tick_worker.accepted_layouts := cand.accepted_layouts;
        workload_layout_tick_worker.pending_layouts := cand.pending_layouts;
        workload_layout_tick_worker.target_layouts := cand.pending_layout_names;
        workload_layout_tick_worker.action := 'build_workload_layouts';
        workload_layout_tick_worker.executed := false;
        workload_layout_tick_worker.status := 'planned';
        workload_layout_tick_worker.rows_written := NULL;
        workload_layout_tick_worker.error := NULL;

        IF dry_run THEN
            RETURN NEXT;
            RETURN;
        END IF;

        -- Work stealing: each slot prefers a stable OID partition, but can
        -- skip a claimed table and take the next available candidate.
        IF NOT pg_try_advisory_xact_lock(
            (1380336724::bigint << 32) | cand.table_oid::bigint
        ) THEN
            CONTINUE;
        END IF;

        -- Re-read catalog truth after the table claim. A canonical refresh or
        -- manual layout build may have completed since the candidate scan.
        SELECT c.*
          INTO live
          FROM rvbbit.workload_layout_tick_candidates c
         WHERE c.table_oid = cand.table_oid;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        attempt_no := CASE
            WHEN cand.last_status IN ('failed', 'partial')
             AND cand.last_target_generation IS NOT DISTINCT FROM live.target_generation
             AND cand.last_target_layouts IS NOT DISTINCT FROM live.accepted_layout_names
            THEN greatest(coalesce(cand.last_attempt, 0) + 1, 1)
            ELSE 1
        END;
        started := clock_timestamp();
        PERFORM set_config(
            'application_name',
            left(format(
                'rvbbit/layout w=%s/%s t=%s %s',
                worker_slot,
                worker_count,
                live.table_oid,
                live.table_name
            ), 63),
            true
        );

        BEGIN
            SELECT rvbbit.refresh_workload_layout_variants(live.table_oid)
              INTO build_rows;
            SELECT rvbbit.workload_layout_variants_pending(live.table_oid)
              INTO still_pending;

            workload_layout_tick_worker.executed := true;
            workload_layout_tick_worker.status := CASE
                WHEN still_pending THEN 'partial'
                ELSE 'ok'
            END;
            workload_layout_tick_worker.rows_written := coalesce(build_rows, 0);
            workload_layout_tick_worker.target_generation := live.target_generation;
            workload_layout_tick_worker.accepted_layouts := live.accepted_layouts;
            workload_layout_tick_worker.pending_layouts := live.pending_layouts;
            workload_layout_tick_worker.target_layouts := live.pending_layout_names;

            INSERT INTO rvbbit.workload_layout_tick_runs (
                table_oid, table_name, target_generation, target_row_groups,
                target_rows, target_layouts, worker_slot, worker_count,
                attempt, status, rows_written, started_at, finished_at,
                retry_at, details
            ) VALUES (
                live.table_oid, live.table_name, live.target_generation,
                live.target_row_groups, live.target_rows,
                live.accepted_layout_names, worker_slot, worker_count,
                attempt_no, workload_layout_tick_worker.status,
                workload_layout_tick_worker.rows_written, started,
                clock_timestamp(),
                CASE WHEN still_pending
                     THEN clock_timestamp() + interval '1 minute'
                     ELSE NULL END,
                jsonb_build_object(
                    'accepted_layouts', live.accepted_layouts,
                    'pending_before', live.pending_layout_names,
                    'pending_after', still_pending,
                    'source', 'accepted_workload_layouts'
                )
            );
        EXCEPTION WHEN OTHERS THEN
            retry_seconds := least(
                3600,
                (30 * power(2, least(attempt_no - 1, 7)))::integer
            );
            workload_layout_tick_worker.executed := true;
            workload_layout_tick_worker.status := 'failed';
            workload_layout_tick_worker.rows_written := 0;
            workload_layout_tick_worker.error := SQLERRM;

            INSERT INTO rvbbit.workload_layout_tick_runs (
                table_oid, table_name, target_generation, target_row_groups,
                target_rows, target_layouts, worker_slot, worker_count,
                attempt, status, rows_written, started_at, finished_at,
                retry_at, error, details
            ) VALUES (
                live.table_oid, live.table_name, live.target_generation,
                live.target_row_groups, live.target_rows,
                live.accepted_layout_names, worker_slot, worker_count,
                attempt_no, 'failed', 0, started, clock_timestamp(),
                clock_timestamp() + make_interval(secs => retry_seconds),
                SQLERRM,
                jsonb_build_object(
                    'accepted_layouts', live.accepted_layouts,
                    'pending_before', live.pending_layout_names,
                    'retry_seconds', retry_seconds,
                    'source', 'accepted_workload_layouts'
                )
            );
        END;

        RETURN NEXT;
        RETURN;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION rvbbit.workload_layout_tick_worker(integer, integer, boolean) IS
    'Builds at most one accepted non-Vortex layout set per transaction. Slots prefer stable table partitions, steal unlocked work, and share the variant lane for bounded future parallelism.';

CREATE OR REPLACE FUNCTION rvbbit.workload_layout_tick(
    dry_run boolean DEFAULT false
)
RETURNS TABLE (
    table_oid oid,
    table_name text,
    target_generation bigint,
    accepted_layouts integer,
    pending_layouts integer,
    target_layouts text[],
    action text,
    executed boolean,
    status text,
    rows_written bigint,
    error text
)
LANGUAGE sql
VOLATILE
AS $$
    SELECT * FROM rvbbit.workload_layout_tick_worker(1, 1, dry_run)
$$;

COMMENT ON FUNCTION rvbbit.workload_layout_tick(boolean) IS
    'Conservative singleton heartbeat for accepted cluster/Hive layouts; Vortex remains owned by variant_tick().';

CREATE OR REPLACE FUNCTION rvbbit.schedule_workload_layout_tick_workers(
    cron_schedule text DEFAULT '* * * * *',
    workers integer DEFAULT 1
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
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
    IF cron_home IS NOT NULL AND cron_home <> '' AND cron_home <> this_db THEN
        external_hint := format(
            'Connect to %I and run: SELECT cron.unschedule(jobid) FROM cron.job '
            'WHERE database = %L AND (jobname = %L OR jobname ~ %L);',
            cron_home,
            this_db,
            'rvbbit_workload_layout_tick',
            '^rvbbit_workload_layout_tick_worker_[0-9]+$'
        );
        FOR slot IN 1..workers LOOP
            external_hint := external_hint || format(
                ' SELECT cron.schedule_in_database(%L, %L, %L, %L);',
                format('rvbbit_workload_layout_tick_worker_%s', slot),
                cron_schedule,
                format(
                    'SELECT rvbbit.workload_layout_tick_worker(%s, %s, false)',
                    slot,
                    workers
                ),
                this_db
            );
        END LOOP;
        RAISE EXCEPTION 'pg_cron home database is %, not %; cron.* is not callable here.',
            cron_home, this_db
            USING HINT = external_hint;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        RAISE EXCEPTION 'pg_cron is not installed; cannot schedule workload layout workers.'
            USING HINT = 'Call rvbbit.workload_layout_tick() from an external scheduler.';
    END IF;

    FOR stale IN
        SELECT j.jobid
          FROM cron.job j
         WHERE j.jobname = 'rvbbit_workload_layout_tick'
            OR j.jobname ~ '^rvbbit_workload_layout_tick_worker_[0-9]+$'
    LOOP
        EXECUTE 'SELECT cron.unschedule($1)' USING stale.jobid;
    END LOOP;

    FOR slot IN 1..workers LOOP
        job_name := format('rvbbit_workload_layout_tick_worker_%s', slot);
        command := format(
            'SELECT rvbbit.workload_layout_tick_worker(%s, %s, false)',
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
$$;

COMMENT ON FUNCTION rvbbit.schedule_workload_layout_tick_workers(text, integer) IS
    'Replaces the accepted-layout heartbeat with a named slot cohort. Defaults to one worker; the lock and work-stealing contract supports bounded expansion later.';

-- A later retention migration accidentally reissued maintain_storage() without
-- the accepted-layout predicate introduced in 0110. Patch the live definition
-- instead of replacing the whole function and risking drift in unrelated
-- retention/reaping phases.
DO $migration$
DECLARE
    definition text;
    needle text;
    replacement text;
BEGIN
    definition := pg_get_functiondef(
        'rvbbit.maintain_storage(bigint,boolean)'::regprocedure
    );
    IF position('workload_layout_variants_pending' IN definition) = 0 THEN
        needle := '                    count(rgv.*) AS variants';
        replacement := needle || E',\n                    rvbbit.workload_layout_variants_pending(t.table_oid) AS workload_pending';
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0270 could not add workload pending state to maintain_storage';
        END IF;
        definition := replace(definition, needle, replacement);

        needle := 'AND (variants = 0 OR newest_variant < newest_rg)';
        replacement := 'AND (variants = 0 OR newest_variant < newest_rg OR workload_pending)';
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0270 could not add workload pending eligibility to maintain_storage';
        END IF;
        definition := replace(definition, needle, replacement);
        EXECUTE definition;
    END IF;
END;
$migration$;

-- Retain the new append-only worker history alongside the existing operational
-- logs. As above, patch only the list entry and preserve the current function.
DO $migration$
DECLARE
    definition text;
    needle text := '(''rvbbit.variant_build_runs'', ''started_at''),';
    replacement text := needle || E'\n            (''rvbbit.workload_layout_tick_runs'', ''started_at''),';
BEGIN
    definition := pg_get_functiondef(
        'rvbbit.reap_logs(interval)'::regprocedure
    );
    IF position('rvbbit.workload_layout_tick_runs' IN definition) = 0 THEN
        IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0270 could not add workload layout runs to reap_logs';
        END IF;
        definition := replace(definition, needle, replacement);
        EXECUTE definition;
    END IF;
END;
$migration$;
