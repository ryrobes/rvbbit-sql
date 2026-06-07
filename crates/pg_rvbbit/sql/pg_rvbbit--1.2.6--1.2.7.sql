-- pg_rvbbit 1.2.6 -> 1.2.7
--
-- run_sync executor hardening (from adversarial review):
--  * Self-healing singleton lock (a crashed run can't permanently wedge sync).
--    A leaked SESSION advisory lock would silently disable all future runs under
--    a persistent cron bgworker; a lock ROW heartbeated every table and stolen
--    when stale recovers automatically.
--  * Truncate SQLERRM in pg_notify payloads (pg_notify raises on >= 8000 bytes;
--    JSON-escaped FDW errors easily exceed that, and the raise inside an EXCEPTION
--    handler would escape the procedure).
--  * Surface spec-listed tables that are MISSING after import (LIMIT TO silently
--    drops non-existent names) as action='error' instead of losing them.
--  * fdw_setup_server reconciles server/user-mapping OPTIONS every run (so a
--    changed host/password takes effect, not just first-create).
--  * sync_table errors clearly on a zero-storable-column source.

CREATE TABLE IF NOT EXISTS rvbbit.sync_lock (
    id          integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    acquired_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    pid         integer NOT NULL
);

CREATE OR REPLACE FUNCTION rvbbit.fdw_setup_server(
    server_name text, host text, port integer, dbname text,
    user_name text, password text, fetch_size integer DEFAULT 10000
) RETURNS text LANGUAGE plpgsql AS $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS postgres_fdw;
    EXECUTE format(
        'CREATE SERVER IF NOT EXISTS %I FOREIGN DATA WRAPPER postgres_fdw '
        'OPTIONS (host %L, port %L, dbname %L, fetch_size %L)',
        server_name, host, port::text, dbname, fetch_size::text);
    -- reconcile options on repeat runs (CREATE IF NOT EXISTS is a no-op once it
    -- exists, so a changed host/port/db/fetch_size would otherwise be ignored).
    EXECUTE format(
        'ALTER SERVER %I OPTIONS (SET host %L, SET port %L, SET dbname %L, SET fetch_size %L)',
        server_name, host, port::text, dbname, fetch_size::text);
    EXECUTE format(
        'CREATE USER MAPPING IF NOT EXISTS FOR CURRENT_USER SERVER %I '
        'OPTIONS (user %L, password %L)',
        server_name, user_name, password);
    EXECUTE format(
        'ALTER USER MAPPING FOR CURRENT_USER SERVER %I OPTIONS (SET user %L, SET password %L)',
        server_name, user_name, password);
    RETURN format('postgres_fdw server "%s" ready (-> %s@%s:%s/%s)',
        server_name, user_name, host, port::text, dbname);
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.sync_table(fdw_table regclass, dest_schema text, dest_name text)
RETURNS TABLE (generation bigint, rows_loaded bigint, action text)
LANGUAGE plpgsql AS $$
DECLARE
    v_dest_qual text := format('%I.%I', dest_schema, dest_name);
    v_dest_oid  oid;
    v_col_ddl   text;
    v_stmt      text;
    v_select    text;
BEGIN
    v_dest_oid := to_regclass(v_dest_qual);
    IF v_dest_oid IS NULL THEN
        EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', dest_schema);
        SELECT string_agg(format('%I %s', a.attname, rvbbit.rvbbit_storable_type(a.atttypid, a.atttypmod)), ', ' ORDER BY a.attnum)
        INTO v_col_ddl
        FROM pg_attribute a
        WHERE a.attrelid = fdw_table AND a.attnum > 0 AND NOT a.attisdropped;
        IF v_col_ddl IS NULL THEN
            RAISE EXCEPTION 'sync_table: source % has no storable columns', fdw_table;
        END IF;
        EXECUTE format('CREATE TABLE %s (%s) USING rvbbit', v_dest_qual, v_col_ddl);
        v_dest_oid := to_regclass(v_dest_qual);
    ELSE
        FOR v_stmt IN
            SELECT format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS %I %s',
                          v_dest_qual, a.attname, rvbbit.rvbbit_storable_type(a.atttypid, a.atttypmod))
            FROM pg_attribute a
            WHERE a.attrelid = fdw_table AND a.attnum > 0 AND NOT a.attisdropped
              AND NOT EXISTS (
                  SELECT 1 FROM pg_attribute d
                  WHERE d.attrelid = v_dest_oid AND d.attname = a.attname
                    AND d.attnum > 0 AND NOT d.attisdropped)
        LOOP
            EXECUTE v_stmt;
        END LOOP;
    END IF;

    SELECT string_agg(
        CASE WHEN EXISTS (
                SELECT 1 FROM pg_attribute f
                WHERE f.attrelid = fdw_table AND f.attname = d.attname
                  AND f.attnum > 0 AND NOT f.attisdropped)
             THEN format('%I::%s', d.attname, format_type(d.atttypid, d.atttypmod))
             ELSE format('NULL::%s AS %I', format_type(d.atttypid, d.atttypmod), d.attname)
        END, ', ' ORDER BY d.attnum)
    INTO v_select
    FROM pg_attribute d
    WHERE d.attrelid = v_dest_oid AND d.attnum > 0 AND NOT d.attisdropped;

    RETURN QUERY
        SELECT * FROM rvbbit.snapshot_load(
            v_dest_oid::regclass,
            format('SELECT %s FROM %s', v_select, fdw_table::text));
END;
$$;

CREATE OR REPLACE PROCEDURE rvbbit.run_sync(p_job_name text DEFAULT NULL, dry_run boolean DEFAULT false)
LANGUAGE plpgsql AS $$
DECLARE
    v_rid        uuid := gen_random_uuid();
    v_lock_pid   integer;
    v_jobs       text[];
    v_jn         text;
    v_spec       jsonb;
    v_srv        jsonb;
    v_remote     text;
    v_fdw_schema text;
    v_dest_schema text;
    v_spec_tbls  text[];
    v_tbls       text[];
    v_missing    text;
    v_tbl        text;
    v_fdw_tbl    regclass;
    v_t0         timestamptz;
    v_gen        bigint;
    v_rows       bigint;
    v_action     text;
    v_job_ok     boolean;
BEGIN
    -- Self-healing singleton: steal a lock not heartbeated in > 1h (crashed run).
    INSERT INTO rvbbit.sync_lock (id, acquired_at, pid)
    VALUES (1, clock_timestamp(), pg_backend_pid())
    ON CONFLICT (id) DO UPDATE
        SET acquired_at = clock_timestamp(), pid = pg_backend_pid()
        WHERE rvbbit.sync_lock.acquired_at < clock_timestamp() - interval '1 hour'
    RETURNING pid INTO v_lock_pid;
    IF v_lock_pid IS DISTINCT FROM pg_backend_pid() THEN
        RAISE NOTICE 'rvbbit.run_sync is already running; skipping';
        RETURN;
    END IF;
    COMMIT;

    SELECT array_agg(job_name ORDER BY job_name) INTO v_jobs
    FROM rvbbit.sync_jobs
    WHERE enabled AND (p_job_name IS NULL OR job_name = p_job_name);

    FOREACH v_jn IN ARRAY coalesce(v_jobs, ARRAY[]::text[]) LOOP
        SELECT spec INTO v_spec FROM rvbbit.sync_jobs WHERE job_name = v_jn;
        v_srv := v_spec->'server';
        v_remote := coalesce(v_spec->>'remote_schema', 'public');
        v_fdw_schema := coalesce(v_spec->>'fdw_schema', 'rvbbit_fdw');
        v_dest_schema := coalesce(v_spec->>'dest_schema', 'public');

        IF v_spec ? 'tables' AND jsonb_typeof(v_spec->'tables') = 'array' THEN
            SELECT array_agg(t) INTO v_spec_tbls FROM jsonb_array_elements_text(v_spec->'tables') AS t;
        ELSE
            v_spec_tbls := NULL;
        END IF;
        IF v_spec_tbls IS NOT NULL AND cardinality(v_spec_tbls) = 0 THEN
            v_spec_tbls := NULL;  -- empty array => whole schema
        END IF;

        v_job_ok := true;
        BEGIN
            PERFORM rvbbit.fdw_setup_server(
                v_srv->>'name', v_srv->>'host', (v_srv->>'port')::int, v_srv->>'dbname',
                v_srv->>'user', v_srv->>'password', coalesce((v_srv->>'fetch_size')::int, 10000));
            PERFORM rvbbit.fdw_import(v_srv->>'name', v_remote, v_fdw_schema, v_spec_tbls);
        EXCEPTION WHEN OTHERS THEN
            v_job_ok := false;
            INSERT INTO rvbbit.sync_runs(run_id, job_name, action, error, started_at)
            VALUES (v_rid, v_jn, 'error', 'provisioning: ' || left(SQLERRM, 500), clock_timestamp());
            PERFORM pg_notify('rvbbit_sync_error',
                json_build_object('job', v_jn, 'phase', 'provision', 'error', left(SQLERRM, 500))::text);
        END;
        COMMIT;

        IF v_job_ok THEN
            -- foreign tables actually present after import
            SELECT array_agg(c.relname ORDER BY c.relname) INTO v_tbls
            FROM pg_foreign_table ft
            JOIN pg_class c ON c.oid = ft.ftrelid
            JOIN pg_namespace ns ON ns.oid = c.relnamespace
            WHERE ns.nspname = v_fdw_schema
              AND (v_spec_tbls IS NULL OR c.relname = ANY(v_spec_tbls));

            -- spec tables that did NOT import (missing on source / import failed)
            IF v_spec_tbls IS NOT NULL THEN
                FOREACH v_missing IN ARRAY v_spec_tbls LOOP
                    IF v_missing <> ALL (coalesce(v_tbls, ARRAY[]::text[])) THEN
                        INSERT INTO rvbbit.sync_runs(run_id, job_name, source_table, action, error, started_at)
                        VALUES (v_rid, v_jn, v_missing, 'error',
                                'not found in fdw schema after import (missing on source or import failed)',
                                clock_timestamp());
                        PERFORM pg_notify('rvbbit_sync_error',
                            json_build_object('job', v_jn, 'table', v_missing, 'error', 'missing on source')::text);
                    END IF;
                END LOOP;
                COMMIT;
            END IF;

            IF NOT dry_run THEN
                FOREACH v_tbl IN ARRAY coalesce(v_tbls, ARRAY[]::text[]) LOOP
                    v_fdw_tbl := to_regclass(format('%I.%I', v_fdw_schema, v_tbl));
                    v_t0 := clock_timestamp();
                    BEGIN
                        SELECT generation, rows_loaded, action INTO v_gen, v_rows, v_action
                        FROM rvbbit.sync_table(v_fdw_tbl, v_dest_schema, v_tbl);
                        INSERT INTO rvbbit.sync_runs(run_id, job_name, source_table, dest_table, action, generation, rows_loaded, elapsed_ms, started_at)
                        VALUES (v_rid, v_jn, v_tbl, format('%I.%I', v_dest_schema, v_tbl), v_action, v_gen, v_rows,
                                (extract(epoch FROM clock_timestamp() - v_t0) * 1000)::int, v_t0);
                    EXCEPTION WHEN OTHERS THEN
                        INSERT INTO rvbbit.sync_runs(run_id, job_name, source_table, dest_table, action, elapsed_ms, error, started_at)
                        VALUES (v_rid, v_jn, v_tbl, format('%I.%I', v_dest_schema, v_tbl), 'error',
                                (extract(epoch FROM clock_timestamp() - v_t0) * 1000)::int, left(SQLERRM, 500), v_t0);
                        PERFORM pg_notify('rvbbit_sync_error',
                            json_build_object('job', v_jn, 'table', v_tbl, 'error', left(SQLERRM, 500))::text);
                    END;
                    -- heartbeat the lock so a long sweep isn't seen as crashed
                    UPDATE rvbbit.sync_lock SET acquired_at = clock_timestamp()
                    WHERE id = 1 AND pid = pg_backend_pid();
                    COMMIT;
                END LOOP;
            END IF;
        END IF;

        UPDATE rvbbit.sync_jobs SET last_run_at = now() WHERE job_name = v_jn;
        COMMIT;
    END LOOP;

    DELETE FROM rvbbit.sync_lock WHERE id = 1 AND pid = pg_backend_pid();
    COMMIT;
END;
$$;
