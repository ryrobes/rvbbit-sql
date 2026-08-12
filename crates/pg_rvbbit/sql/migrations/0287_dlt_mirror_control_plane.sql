-- 0287: dlt-backed SQL mirror control plane.
--
-- This is deliberately separate from the older postgres_fdw Temporal Mirror:
--   * source credentials are canonical credential references, never JSONB;
--   * destinations are source-named schemas of RVBBIT-registered Postgres heap
--     tables in the same extension-enabled RVBBIT database;
--   * acceleration is an independent, optional policy after ingestion;
--   * the worker owns dlt execution while Postgres owns typed configuration,
--     queueing, run receipts, and table-level status.
--
-- Snapshot replacement is the safe default because it propagates source-side
-- deletes and does not require a source cursor. Incremental upsert is opt-in
-- and requires both a unique primary key and a monotonic update cursor. dlt may
-- use a transient technical staging schema for that upsert; it is not a product
-- data layer and is truncated by the worker after each load.

CREATE TABLE IF NOT EXISTS rvbbit.mirror_connections (
    connection_name text PRIMARY KEY,
    label text NOT NULL,
    dialect text NOT NULL,
    credential_ref text GENERATED ALWAYS AS (
        'mirror/' || connection_name || '/SOURCE_DSN'
    ) STORED,
    enabled boolean NOT NULL DEFAULT true,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_tested_at timestamptz,
    last_test_ok boolean,
    last_test_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL DEFAULT session_user,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_by text NOT NULL DEFAULT session_user,
    CONSTRAINT mirror_connections_name_check
        CHECK (connection_name ~ '^[a-z][a-z0-9_-]{2,62}$'),
    CONSTRAINT mirror_connections_label_check
        CHECK (char_length(btrim(label)) BETWEEN 1 AND 160),
    CONSTRAINT mirror_connections_dialect_check
        CHECK (dialect IN ('postgresql','mysql','mariadb','mssql','oracle','db2')),
    CONSTRAINT mirror_connections_metadata_check
        CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT mirror_connections_error_check
        CHECK (last_test_error IS NULL OR char_length(last_test_error) <= 2000)
);

CREATE TABLE IF NOT EXISTS rvbbit.mirror_jobs (
    job_name text PRIMARY KEY,
    connection_name text NOT NULL
        REFERENCES rvbbit.mirror_connections(connection_name) ON DELETE RESTRICT,
    source_schema text NOT NULL,
    destination_schema text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    schedule_seconds integer,
    next_run_at timestamptz,
    chunk_size integer NOT NULL DEFAULT 50000,
    reflection_level text NOT NULL DEFAULT 'full',
    loader_file_format text NOT NULL DEFAULT 'insert_values',
    last_run_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL DEFAULT session_user,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_by text NOT NULL DEFAULT session_user,
    CONSTRAINT mirror_jobs_name_check
        CHECK (job_name ~ '^[a-z][a-z0-9_-]{2,62}$'),
    CONSTRAINT mirror_jobs_source_schema_check
        CHECK (char_length(source_schema) BETWEEN 1 AND 255),
    CONSTRAINT mirror_jobs_destination_schema_check
        CHECK (
            destination_schema ~ '^[a-z][a-z0-9_]{0,47}$'
            AND destination_schema !~ '^pg_'
            AND destination_schema NOT IN (
                'rvbbit','pg_catalog','information_schema','public','pg_toast'
            )
        ),
    CONSTRAINT mirror_jobs_schedule_check
        CHECK (schedule_seconds IS NULL OR schedule_seconds BETWEEN 60 AND 2678400),
    CONSTRAINT mirror_jobs_chunk_check CHECK (chunk_size BETWEEN 100 AND 1000000),
    CONSTRAINT mirror_jobs_reflection_check
        CHECK (reflection_level IN ('minimal','full','full_with_precision')),
    CONSTRAINT mirror_jobs_loader_check
        CHECK (loader_file_format = 'insert_values')
);

CREATE TABLE IF NOT EXISTS rvbbit.mirror_tables (
    job_name text NOT NULL
        REFERENCES rvbbit.mirror_jobs(job_name) ON DELETE CASCADE,
    source_table text NOT NULL,
    destination_table text NOT NULL,
    load_mode text NOT NULL DEFAULT 'snapshot',
    primary_key text[],
    cursor_column text,
    initial_cursor jsonb,
    included_columns text[],
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (job_name, source_table),
    CONSTRAINT mirror_tables_source_check
        CHECK (char_length(source_table) BETWEEN 1 AND 255),
    CONSTRAINT mirror_tables_destination_check
        CHECK (destination_table ~ '^_?[a-z0-9]+(_[a-z0-9]+)*$'),
    CONSTRAINT mirror_tables_mode_check
        CHECK (load_mode IN ('snapshot','incremental_upsert')),
    CONSTRAINT mirror_tables_incremental_check
        CHECK (
            load_mode = 'snapshot'
            OR (
                coalesce(cardinality(primary_key), 0) > 0
                AND cursor_column IS NOT NULL
                AND btrim(cursor_column) <> ''
            )
        ),
    CONSTRAINT mirror_tables_primary_key_check
        CHECK (primary_key IS NULL OR cardinality(primary_key) BETWEEN 1 AND 32),
    CONSTRAINT mirror_tables_columns_check
        CHECK (included_columns IS NULL OR cardinality(included_columns) BETWEEN 1 AND 1024),
    UNIQUE (job_name, destination_table)
);

CREATE TABLE IF NOT EXISTS rvbbit.mirror_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_name text NOT NULL
        REFERENCES rvbbit.mirror_jobs(job_name) ON DELETE RESTRICT,
    trigger text NOT NULL DEFAULT 'manual',
    status text NOT NULL DEFAULT 'queued',
    worker_id text,
    attempt integer NOT NULL DEFAULT 0,
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    requested_by text NOT NULL DEFAULT session_user,
    started_at timestamptz,
    heartbeat_at timestamptz,
    finished_at timestamptz,
    tables_succeeded integer NOT NULL DEFAULT 0,
    tables_failed integer NOT NULL DEFAULT 0,
    rows_loaded bigint NOT NULL DEFAULT 0,
    load_ids text[] NOT NULL DEFAULT ARRAY[]::text[],
    error_code text,
    error_message text,
    CONSTRAINT mirror_runs_trigger_check
        CHECK (trigger IN ('manual','schedule','bootstrap','retry','api')),
    CONSTRAINT mirror_runs_status_check
        CHECK (status IN ('queued','running','succeeded','partial','failed','cancelled')),
    CONSTRAINT mirror_runs_counts_check
        CHECK (
            attempt >= 0 AND tables_succeeded >= 0
            AND tables_failed >= 0 AND rows_loaded >= 0
        ),
    CONSTRAINT mirror_runs_error_check
        CHECK (error_message IS NULL OR char_length(error_message) <= 2000)
);

CREATE UNIQUE INDEX IF NOT EXISTS mirror_runs_one_active_job_idx
    ON rvbbit.mirror_runs (job_name)
    WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS mirror_runs_job_requested_idx
    ON rvbbit.mirror_runs (job_name, requested_at DESC);
CREATE INDEX IF NOT EXISTS mirror_runs_queue_idx
    ON rvbbit.mirror_runs (requested_at)
    WHERE status = 'queued';

CREATE TABLE IF NOT EXISTS rvbbit.mirror_table_runs (
    run_id uuid NOT NULL
        REFERENCES rvbbit.mirror_runs(run_id) ON DELETE CASCADE,
    source_table text NOT NULL,
    destination_table text NOT NULL,
    load_mode text NOT NULL,
    status text NOT NULL DEFAULT 'running',
    rows_loaded bigint NOT NULL DEFAULT 0,
    load_ids text[] NOT NULL DEFAULT ARRAY[]::text[],
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    error_code text,
    error_message text,
    PRIMARY KEY (run_id, source_table),
    CONSTRAINT mirror_table_runs_status_check
        CHECK (status IN ('running','succeeded','failed','skipped')),
    CONSTRAINT mirror_table_runs_rows_check CHECK (rows_loaded >= 0),
    CONSTRAINT mirror_table_runs_error_check
        CHECK (error_message IS NULL OR char_length(error_message) <= 2000)
);

REVOKE ALL ON rvbbit.mirror_connections FROM PUBLIC;
REVOKE ALL ON rvbbit.mirror_jobs FROM PUBLIC;
REVOKE ALL ON rvbbit.mirror_tables FROM PUBLIC;
REVOKE ALL ON rvbbit.mirror_runs FROM PUBLIC;
REVOKE ALL ON rvbbit.mirror_table_runs FROM PUBLIC;

CREATE OR REPLACE FUNCTION rvbbit.upsert_mirror_connection(
    requested_name text,
    requested_label text,
    requested_dialect text,
    requested_metadata jsonb DEFAULT '{}'::jsonb,
    requested_enabled boolean DEFAULT true
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $upsert_mirror_connection$
DECLARE
    normalized_name text := lower(btrim(requested_name));
    normalized_dialect text := lower(btrim(requested_dialect));
    safe_metadata jsonb := coalesce(requested_metadata, '{}'::jsonb);
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    IF jsonb_typeof(safe_metadata) <> 'object' THEN
        RAISE EXCEPTION 'mirror connection metadata must be a JSON object';
    END IF;
    IF safe_metadata ?| ARRAY[
        'value','secret','token','password','private_key','privateKey',
        'credentials','dsn','connection_string','connectionString'
    ] THEN
        RAISE EXCEPTION 'mirror connection metadata contains a forbidden secret-like field';
    END IF;
    INSERT INTO rvbbit.mirror_connections (
        connection_name, label, dialect, enabled, metadata, created_by, updated_by
    ) VALUES (
        normalized_name, btrim(requested_label), normalized_dialect,
        coalesce(requested_enabled, true), safe_metadata, session_user, session_user
    )
    ON CONFLICT (connection_name) DO UPDATE SET
        label = EXCLUDED.label,
        dialect = EXCLUDED.dialect,
        enabled = EXCLUDED.enabled,
        metadata = EXCLUDED.metadata,
        updated_at = clock_timestamp(),
        updated_by = session_user;
    RETURN normalized_name;
END
$upsert_mirror_connection$;

CREATE OR REPLACE FUNCTION rvbbit.set_mirror_connection_credential(
    requested_name text,
    source_dsn text
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $set_mirror_connection_credential$
DECLARE
    normalized_name text := lower(btrim(requested_name));
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    IF NOT EXISTS (
        SELECT 1 FROM rvbbit.mirror_connections c
        WHERE c.connection_name = normalized_name
    ) THEN
        RAISE EXCEPTION 'mirror connection % does not exist', normalized_name;
    END IF;
    RETURN rvbbit.put_credential(
        'mirror', normalized_name, 'SOURCE_DSN', source_dsn,
        'Read-only source database connection for the mirror worker',
        jsonb_build_object('connection', normalized_name)
    );
END
$set_mirror_connection_credential$;

CREATE OR REPLACE FUNCTION rvbbit.resolve_mirror_connection_credential(
    requested_name text
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $resolve_mirror_connection_credential$
DECLARE
    normalized_name text := lower(btrim(requested_name));
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM rvbbit.mirror_connections c
        WHERE c.connection_name = normalized_name AND c.enabled
    ) THEN
        RETURN NULL;
    END IF;
    RETURN rvbbit.resolve_credential(
        rvbbit.credential_ref('mirror', normalized_name, 'SOURCE_DSN'),
        'dlt-mirror', 'source_database'
    );
END
$resolve_mirror_connection_credential$;

CREATE OR REPLACE FUNCTION rvbbit.delete_mirror_connection_credential(
    requested_name text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $delete_mirror_connection_credential$
DECLARE
    normalized_name text := lower(btrim(requested_name));
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    RETURN rvbbit.delete_credential(
        rvbbit.credential_ref('mirror', normalized_name, 'SOURCE_DSN')
    );
END
$delete_mirror_connection_credential$;

CREATE OR REPLACE FUNCTION rvbbit.upsert_mirror_job(
    requested_job text,
    requested_connection text,
    requested_source_schema text,
    requested_destination_schema text,
    requested_schedule_seconds integer DEFAULT NULL,
    requested_enabled boolean DEFAULT true,
    requested_chunk_size integer DEFAULT 50000,
    requested_reflection_level text DEFAULT 'full',
    requested_loader_file_format text DEFAULT 'insert_values'
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $upsert_mirror_job$
DECLARE
    normalized_job text := lower(btrim(requested_job));
    normalized_connection text := lower(btrim(requested_connection));
    normalized_destination_schema text := lower(btrim(requested_destination_schema));
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    INSERT INTO rvbbit.mirror_jobs (
        job_name, connection_name, source_schema, destination_schema,
        enabled, schedule_seconds, next_run_at, chunk_size,
        reflection_level, loader_file_format, created_by, updated_by
    ) VALUES (
        normalized_job, normalized_connection, requested_source_schema,
        normalized_destination_schema, coalesce(requested_enabled, true),
        requested_schedule_seconds,
        CASE
            WHEN coalesce(requested_enabled, true)
             AND requested_schedule_seconds IS NOT NULL THEN clock_timestamp()
            ELSE NULL
        END,
        coalesce(requested_chunk_size, 50000),
        lower(btrim(coalesce(requested_reflection_level, 'full'))),
        lower(btrim(coalesce(requested_loader_file_format, 'insert_values'))),
        session_user, session_user
    )
    ON CONFLICT (job_name) DO UPDATE SET
        connection_name = EXCLUDED.connection_name,
        source_schema = EXCLUDED.source_schema,
        destination_schema = EXCLUDED.destination_schema,
        enabled = EXCLUDED.enabled,
        schedule_seconds = EXCLUDED.schedule_seconds,
        next_run_at = CASE
            WHEN EXCLUDED.enabled AND EXCLUDED.schedule_seconds IS NOT NULL
                THEN coalesce(rvbbit.mirror_jobs.next_run_at, clock_timestamp())
            ELSE NULL
        END,
        chunk_size = EXCLUDED.chunk_size,
        reflection_level = EXCLUDED.reflection_level,
        loader_file_format = EXCLUDED.loader_file_format,
        updated_at = clock_timestamp(),
        updated_by = session_user;
    RETURN normalized_job;
END
$upsert_mirror_job$;

CREATE OR REPLACE FUNCTION rvbbit.upsert_mirror_table(
    requested_job text,
    requested_source_table text,
    requested_destination_table text DEFAULT NULL,
    requested_load_mode text DEFAULT 'snapshot',
    requested_primary_key text[] DEFAULT NULL,
    requested_cursor_column text DEFAULT NULL,
    requested_initial_cursor jsonb DEFAULT NULL,
    requested_included_columns text[] DEFAULT NULL,
    requested_enabled boolean DEFAULT true
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $upsert_mirror_table$
DECLARE
    normalized_job text := lower(btrim(requested_job));
    destination_name text := coalesce(
        nullif(btrim(requested_destination_table), ''),
        lower(regexp_replace(btrim(requested_source_table), '[^A-Za-z0-9_$]+', '_', 'g'))
    );
    normalized_mode text := lower(btrim(coalesce(requested_load_mode, 'snapshot')));
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    INSERT INTO rvbbit.mirror_tables (
        job_name, source_table, destination_table, load_mode, primary_key,
        cursor_column, initial_cursor, included_columns, enabled
    ) VALUES (
        normalized_job, requested_source_table, destination_name, normalized_mode,
        requested_primary_key, nullif(btrim(requested_cursor_column), ''),
        requested_initial_cursor, requested_included_columns,
        coalesce(requested_enabled, true)
    )
    ON CONFLICT (job_name, source_table) DO UPDATE SET
        destination_table = EXCLUDED.destination_table,
        load_mode = EXCLUDED.load_mode,
        primary_key = EXCLUDED.primary_key,
        cursor_column = EXCLUDED.cursor_column,
        initial_cursor = EXCLUDED.initial_cursor,
        included_columns = EXCLUDED.included_columns,
        enabled = EXCLUDED.enabled,
        updated_at = clock_timestamp();
    RETURN destination_name;
END
$upsert_mirror_table$;

CREATE OR REPLACE FUNCTION rvbbit.request_mirror_run(
    requested_job text,
    requested_trigger text DEFAULT 'manual'
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $request_mirror_run$
DECLARE
    normalized_job text := lower(btrim(requested_job));
    normalized_trigger text := lower(btrim(coalesce(requested_trigger, 'manual')));
    requested_id uuid;
BEGIN
    PERFORM rvbbit.require_capability_catalog_admin();
    IF NOT EXISTS (
        SELECT 1 FROM rvbbit.mirror_jobs j
        WHERE j.job_name = normalized_job AND j.enabled
    ) THEN
        RAISE EXCEPTION 'enabled mirror job % does not exist', normalized_job;
    END IF;
    SELECT r.run_id INTO requested_id
    FROM rvbbit.mirror_runs r
    WHERE r.job_name = normalized_job AND r.status IN ('queued','running')
    ORDER BY r.requested_at
    LIMIT 1;
    IF requested_id IS NOT NULL THEN
        RETURN requested_id;
    END IF;
    BEGIN
        INSERT INTO rvbbit.mirror_runs (job_name, trigger, requested_by)
        VALUES (normalized_job, normalized_trigger, session_user)
        RETURNING run_id INTO requested_id;
    EXCEPTION WHEN unique_violation THEN
        SELECT r.run_id INTO requested_id
        FROM rvbbit.mirror_runs r
        WHERE r.job_name = normalized_job AND r.status IN ('queued','running')
        ORDER BY r.requested_at
        LIMIT 1;
    END;
    PERFORM pg_notify(
        'rvbbit_mirror_run',
        json_build_object('run_id', requested_id, 'job', normalized_job)::text
    );
    RETURN requested_id;
END
$request_mirror_run$;

CREATE OR REPLACE FUNCTION rvbbit.enqueue_due_mirror_runs()
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $enqueue_due_mirror_runs$
DECLARE
    queued integer;
BEGIN
    WITH due AS (
        SELECT j.job_name, j.schedule_seconds
        FROM rvbbit.mirror_jobs j
        JOIN rvbbit.mirror_connections c USING (connection_name)
        WHERE j.enabled AND c.enabled
          AND j.schedule_seconds IS NOT NULL
          AND coalesce(j.next_run_at, clock_timestamp()) <= clock_timestamp()
        FOR UPDATE OF j SKIP LOCKED
    ), inserted AS (
        INSERT INTO rvbbit.mirror_runs (job_name, trigger, requested_by)
        SELECT d.job_name, 'schedule', 'dlt-mirror-scheduler'
        FROM due d
        ON CONFLICT DO NOTHING
        RETURNING job_name
    ), advanced AS (
        UPDATE rvbbit.mirror_jobs j
        SET next_run_at = clock_timestamp() + make_interval(secs => d.schedule_seconds),
            updated_at = j.updated_at
        FROM due d
        WHERE j.job_name = d.job_name
        RETURNING j.job_name
    )
    SELECT count(*)::integer INTO queued FROM inserted;
    RETURN coalesce(queued, 0);
END
$enqueue_due_mirror_runs$;

CREATE OR REPLACE FUNCTION rvbbit.claim_mirror_run(requested_worker text)
RETURNS TABLE (run_id uuid, job_name text)
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $claim_mirror_run$
BEGIN
    IF requested_worker IS NULL OR btrim(requested_worker) = '' THEN
        RAISE EXCEPTION 'mirror worker id is required';
    END IF;
    RETURN QUERY
    WITH candidate AS (
        SELECT r.run_id
        FROM rvbbit.mirror_runs r
        WHERE r.status = 'queued'
        ORDER BY r.requested_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE rvbbit.mirror_runs r
    SET status = 'running',
        worker_id = left(btrim(requested_worker), 160),
        attempt = r.attempt + 1,
        started_at = clock_timestamp(),
        heartbeat_at = clock_timestamp()
    FROM candidate c
    WHERE r.run_id = c.run_id
    RETURNING r.run_id, r.job_name;
END
$claim_mirror_run$;

CREATE OR REPLACE FUNCTION rvbbit.heartbeat_mirror_run(
    requested_run_id uuid,
    requested_worker text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $heartbeat_mirror_run$
DECLARE
    changed integer;
BEGIN
    UPDATE rvbbit.mirror_runs r
    SET heartbeat_at = clock_timestamp()
    WHERE r.run_id = requested_run_id
      AND r.status = 'running'
      AND r.worker_id = left(btrim(requested_worker), 160);
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed > 0;
END
$heartbeat_mirror_run$;

CREATE OR REPLACE FUNCTION rvbbit.requeue_stale_mirror_runs(
    stale_after_seconds integer DEFAULT 1800,
    max_attempts integer DEFAULT 5
) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $requeue_stale_mirror_runs$
DECLARE
    changed integer;
BEGIN
    IF stale_after_seconds < 60 OR max_attempts < 1 THEN
        RAISE EXCEPTION 'stale timeout must be at least 60 seconds and max attempts at least 1';
    END IF;
    UPDATE rvbbit.mirror_runs r
    SET status = CASE WHEN r.attempt >= max_attempts THEN 'failed' ELSE 'queued' END,
        worker_id = NULL,
        started_at = CASE WHEN r.attempt >= max_attempts THEN r.started_at ELSE NULL END,
        heartbeat_at = NULL,
        finished_at = CASE
            WHEN r.attempt >= max_attempts THEN clock_timestamp()
            ELSE NULL
        END,
        error_code = CASE
            WHEN r.attempt >= max_attempts THEN 'WORKER_LOST'
            ELSE NULL
        END,
        error_message = CASE
            WHEN r.attempt >= max_attempts
                THEN 'mirror worker heartbeat expired repeatedly'
            ELSE NULL
        END
    WHERE r.status = 'running'
      AND coalesce(r.heartbeat_at, r.started_at, r.requested_at)
          < clock_timestamp() - make_interval(secs => stale_after_seconds);
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed;
END
$requeue_stale_mirror_runs$;

CREATE OR REPLACE FUNCTION rvbbit.record_mirror_table_run(
    requested_run_id uuid,
    requested_source_table text,
    requested_destination_table text,
    requested_load_mode text,
    requested_status text,
    requested_rows_loaded bigint DEFAULT 0,
    requested_load_ids text[] DEFAULT ARRAY[]::text[],
    requested_error_code text DEFAULT NULL,
    requested_error_message text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $record_mirror_table_run$
BEGIN
    INSERT INTO rvbbit.mirror_table_runs (
        run_id, source_table, destination_table, load_mode, status,
        rows_loaded, load_ids, finished_at, error_code, error_message
    ) VALUES (
        requested_run_id, requested_source_table, requested_destination_table,
        requested_load_mode, requested_status,
        greatest(coalesce(requested_rows_loaded, 0), 0),
        coalesce(requested_load_ids, ARRAY[]::text[]),
        CASE WHEN requested_status = 'running' THEN NULL ELSE clock_timestamp() END,
        nullif(left(coalesce(requested_error_code, ''), 120), ''),
        nullif(left(coalesce(requested_error_message, ''), 2000), '')
    )
    ON CONFLICT (run_id, source_table) DO UPDATE SET
        destination_table = EXCLUDED.destination_table,
        load_mode = EXCLUDED.load_mode,
        status = EXCLUDED.status,
        rows_loaded = EXCLUDED.rows_loaded,
        load_ids = EXCLUDED.load_ids,
        finished_at = EXCLUDED.finished_at,
        error_code = EXCLUDED.error_code,
        error_message = EXCLUDED.error_message;
END
$record_mirror_table_run$;

CREATE OR REPLACE FUNCTION rvbbit.finish_mirror_run(
    requested_run_id uuid,
    requested_status text,
    requested_error_code text DEFAULT NULL,
    requested_error_message text DEFAULT NULL
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER VOLATILE
SET search_path = pg_catalog, rvbbit
AS $finish_mirror_run$
DECLARE
    changed integer;
BEGIN
    IF requested_status NOT IN ('succeeded','partial','failed','cancelled') THEN
        RAISE EXCEPTION 'invalid terminal mirror run status';
    END IF;
    UPDATE rvbbit.mirror_runs r
    SET status = requested_status,
        finished_at = clock_timestamp(),
        heartbeat_at = clock_timestamp(),
        tables_succeeded = summary.succeeded,
        tables_failed = summary.failed,
        rows_loaded = summary.rows_loaded,
        load_ids = summary.load_ids,
        error_code = nullif(left(coalesce(requested_error_code, ''), 120), ''),
        error_message = nullif(left(coalesce(requested_error_message, ''), 2000), '')
    FROM (
        SELECT
            count(*) FILTER (WHERE tr.status = 'succeeded')::integer AS succeeded,
            count(*) FILTER (WHERE tr.status = 'failed')::integer AS failed,
            coalesce(sum(tr.rows_loaded), 0)::bigint AS rows_loaded,
            coalesce((
                SELECT array_agg(DISTINCT load_id)
                FROM rvbbit.mirror_table_runs tr_ids
                CROSS JOIN LATERAL unnest(tr_ids.load_ids) load_id
                WHERE tr_ids.run_id = requested_run_id
            ), ARRAY[]::text[]) AS load_ids
        FROM rvbbit.mirror_table_runs tr
        WHERE tr.run_id = requested_run_id
    ) summary
    WHERE r.run_id = requested_run_id AND r.status IN ('queued','running');
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed > 0 THEN
        UPDATE rvbbit.mirror_jobs j
        SET last_run_at = clock_timestamp()
        FROM rvbbit.mirror_runs r
        WHERE r.run_id = requested_run_id AND j.job_name = r.job_name;
    END IF;
    RETURN changed > 0;
END
$finish_mirror_run$;

-- Stable, credential-free lineage projection for DataRabbit diagnostics and
-- Calliope. Names remain recognizable (erp.orders, salesforce.opportunity);
-- lineage does not depend on a synthetic prefix or naming convention alone.
CREATE OR REPLACE VIEW rvbbit.mirror_lineage AS
SELECT
    c.connection_name,
    c.label AS connection_label,
    c.dialect,
    nullif(c.metadata ->> 'host', '') AS source_host,
    nullif(c.metadata ->> 'database', '') AS source_database,
    nullif(c.metadata ->> 'environment', '') AS source_environment,
    j.job_name,
    j.source_schema,
    t.source_table,
    j.destination_schema,
    t.destination_table,
    format('%I.%I', j.destination_schema, t.destination_table)
        AS destination_relation,
    destination.relation_oid IS NOT NULL AS destination_exists,
    am.amname AS destination_access_method,
    greatest(coalesce(pc.reltuples, 0), 0)::bigint AS destination_row_estimate,
    CASE
        WHEN destination.relation_oid IS NULL THEN NULL
        ELSE pg_total_relation_size(destination.relation_oid)
    END::bigint AS destination_bytes,
    stats.last_analyze AS destination_last_analyze,
    t.load_mode,
    t.primary_key,
    t.cursor_column,
    c.enabled AS connection_enabled,
    j.enabled AS job_enabled,
    t.enabled AS table_enabled,
    j.schedule_seconds,
    j.next_run_at,
    latest.run_id AS latest_run_id,
    latest.status AS latest_run_status,
    latest.requested_at AS latest_requested_at,
    latest.started_at AS latest_started_at,
    latest.finished_at AS latest_finished_at,
    latest.rows_loaded AS latest_run_rows_loaded,
    latest.load_ids AS latest_run_load_ids,
    table_receipt.status AS latest_table_status,
    table_receipt.rows_loaded AS latest_table_rows_loaded,
    table_receipt.load_ids AS latest_table_load_ids,
    coalesce(table_receipt.error_code, latest.error_code) AS latest_error_code,
    coalesce(table_receipt.error_message, latest.error_message) AS latest_error_message,
    registry.table_oid IS NOT NULL AS destination_registered,
    coalesce(registry.acceleration_enabled, false) AS acceleration_enabled
FROM rvbbit.mirror_connections c
JOIN rvbbit.mirror_jobs j USING (connection_name)
JOIN rvbbit.mirror_tables t USING (job_name)
LEFT JOIN LATERAL (
    SELECT to_regclass(format('%I.%I', j.destination_schema, t.destination_table))
        AS relation_oid
) destination ON true
LEFT JOIN pg_class pc ON pc.oid = destination.relation_oid
LEFT JOIN pg_am am ON am.oid = pc.relam
LEFT JOIN rvbbit.tables registry ON registry.table_oid = destination.relation_oid
LEFT JOIN pg_stat_user_tables stats ON stats.relid = destination.relation_oid
LEFT JOIN LATERAL (
    SELECT r.run_id, r.status, r.requested_at, r.started_at, r.finished_at,
           r.rows_loaded, r.load_ids, r.error_code, r.error_message
    FROM rvbbit.mirror_runs r
    WHERE r.job_name = j.job_name
    ORDER BY r.requested_at DESC
    LIMIT 1
) latest ON true
LEFT JOIN rvbbit.mirror_table_runs table_receipt
    ON table_receipt.run_id = latest.run_id
   AND table_receipt.source_table = t.source_table;

CREATE OR REPLACE VIEW rvbbit.mirror_run_status AS
SELECT
    r.run_id,
    r.job_name,
    c.connection_name,
    j.source_schema,
    j.destination_schema,
    r.trigger,
    r.status,
    r.attempt,
    r.worker_id,
    r.requested_at,
    r.started_at,
    r.heartbeat_at,
    r.finished_at,
    CASE
        WHEN r.started_at IS NULL THEN NULL
        ELSE extract(epoch FROM (
            coalesce(r.finished_at, clock_timestamp()) - r.started_at
        )) * 1000
    END::bigint AS duration_ms,
    r.tables_succeeded,
    r.tables_failed,
    r.rows_loaded,
    r.load_ids,
    r.error_code,
    r.error_message
FROM rvbbit.mirror_runs r
JOIN rvbbit.mirror_jobs j USING (job_name)
JOIN rvbbit.mirror_connections c USING (connection_name);

REVOKE ALL ON rvbbit.mirror_lineage FROM PUBLIC;
REVOKE ALL ON rvbbit.mirror_run_status FROM PUBLIC;

COMMENT ON TABLE rvbbit.mirror_connections IS
    'Non-secret SQL source descriptors. credential_ref points to the canonical encrypted source DSN.';
COMMENT ON TABLE rvbbit.mirror_jobs IS
    'Typed dlt mirror schedules targeting RVBBIT-registered Postgres heap schemas.';
COMMENT ON TABLE rvbbit.mirror_tables IS
    'Explicit source-to-destination table mappings. Snapshot is default; incremental upsert requires PK and cursor.';
COMMENT ON TABLE rvbbit.mirror_runs IS
    'Credential-free mirror queue and run receipts.';
COMMENT ON TABLE rvbbit.mirror_table_runs IS
    'Per-table dlt load receipts; errors must be redacted by the worker.';
COMMENT ON VIEW rvbbit.mirror_lineage IS
    'Credential-free source-to-RVBBIT relation lineage with registry, acceleration, and latest table/run health.';
COMMENT ON VIEW rvbbit.mirror_run_status IS
    'Credential-free dlt mirror run diagnostics for DataRabbit and operators.';

REVOKE ALL ON FUNCTION rvbbit.upsert_mirror_connection(text, text, text, jsonb, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.set_mirror_connection_credential(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.resolve_mirror_connection_credential(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.delete_mirror_connection_credential(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.upsert_mirror_job(text, text, text, text, integer, boolean, integer, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.upsert_mirror_table(text, text, text, text, text[], text, jsonb, text[], boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.request_mirror_run(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.enqueue_due_mirror_runs() FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.claim_mirror_run(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.heartbeat_mirror_run(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.requeue_stale_mirror_runs(integer, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.record_mirror_table_run(uuid, text, text, text, text, bigint, text[], text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION rvbbit.finish_mirror_run(uuid, text, text, text) FROM PUBLIC;
