-- pg_rvbbit 1.2.5 -> 1.2.6
--
-- Sync config + executor for the temporal-mirror workflow. A job is a JSON spec
-- (UI-authored, server-side so it's headless-schedulable). run_sync provisions
-- the FDW, (re-)imports the foreign tables (DDL-tolerant), and snapshot_loads
-- each into an rvbbit dest, committing + logging per table so progress is
-- durable and visible cross-connection mid-run.

CREATE TABLE IF NOT EXISTS rvbbit.sync_jobs (
    job_name    text PRIMARY KEY,
    enabled     boolean NOT NULL DEFAULT true,
    spec        jsonb NOT NULL,
    last_run_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rvbbit.sync_runs (
    run_id       uuid NOT NULL,
    job_name     text NOT NULL,
    source_table text,
    dest_table   text,
    action       text,          -- 'snapshot' | 'empty' | 'error'
    generation   bigint,
    rows_loaded  bigint,
    elapsed_ms   integer,
    error        text,
    started_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS sync_runs_job_started_idx ON rvbbit.sync_runs (job_name, started_at DESC);

-- Map a source column type to an rvbbit-storable dest type. rvbbit's columnar
-- engine supports a fixed set (bool, int2/4/8, float4/8, text/varchar/char/name,
-- timestamp(tz), date, jsonb, bytea, real[]). Real DW/Salesforce schemas are
-- full of types it can't store, so the sync coerces: numeric -> double precision
-- (analytics-friendly), and everything else unsupported -> text (lossless copy;
-- a mirror, not the system of record). Supported types pass through unchanged.
CREATE OR REPLACE FUNCTION rvbbit.rvbbit_storable_type(typ oid, typmod integer DEFAULT -1)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN typ IN (16,21,23,20,700,701,25,1043,1042,19,1114,1184,1082,3802,17,1021)
         THEN format_type(typ, typmod)
    WHEN typ = 1700 THEN 'double precision'   -- numeric/decimal
    ELSE 'text'                               -- uuid/json/time/arrays/enums/inet/...
  END
$$;

-- Sync ONE foreign table into an rvbbit dest. DDL-adapt: create the dest
-- (USING rvbbit) with rvbbit-storable column types if missing; else ADD COLUMN
-- for new source columns (soft-drop keeps dest columns). The snapshot_load
-- source query lists the DEST columns explicitly, casting each source column to
-- the dest (storable) type if present else NULL — so added/dropped source
-- columns and unsupported types are all handled.
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

    -- For each DEST column: cast the source column to the dest (storable) type
    -- if the source still has it, else NULL of the dest type.
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

-- Run sync jobs. Procedure (not function) so it can COMMIT per table — durable
-- progress + sync_runs visible mid-run. Singleton via a session advisory lock.
-- p_job_name NULL runs all enabled jobs. Must be called outside an explicit
-- transaction (CALL rvbbit.run_sync(...)).
CREATE OR REPLACE PROCEDURE rvbbit.run_sync(p_job_name text DEFAULT NULL, dry_run boolean DEFAULT false)
LANGUAGE plpgsql AS $$
DECLARE
    v_rid        uuid := gen_random_uuid();
    v_jobs       text[];
    v_jn         text;
    v_spec       jsonb;
    v_srv        jsonb;
    v_remote     text;
    v_fdw_schema text;
    v_dest_schema text;
    v_tbls       text[];
    v_tbl        text;
    v_fdw_tbl    regclass;
    v_t0         timestamptz;
    v_gen        bigint;
    v_rows       bigint;
    v_action     text;
    v_job_ok     boolean;
BEGIN
    IF NOT pg_try_advisory_lock(hashtext('rvbbit.run_sync')) THEN
        RAISE NOTICE 'rvbbit.run_sync is already running in another session; skipping';
        RETURN;
    END IF;

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
            SELECT array_agg(t) INTO v_tbls FROM jsonb_array_elements_text(v_spec->'tables') AS t;
        ELSE
            v_tbls := NULL;
        END IF;

        v_job_ok := true;
        BEGIN
            PERFORM rvbbit.fdw_setup_server(
                v_srv->>'name', v_srv->>'host', (v_srv->>'port')::int, v_srv->>'dbname',
                v_srv->>'user', v_srv->>'password', coalesce((v_srv->>'fetch_size')::int, 10000));
            PERFORM rvbbit.fdw_import(v_srv->>'name', v_remote, v_fdw_schema, v_tbls);
        EXCEPTION WHEN OTHERS THEN
            v_job_ok := false;
            INSERT INTO rvbbit.sync_runs(run_id, job_name, action, error, started_at)
            VALUES (v_rid, v_jn, 'error', 'provisioning: ' || SQLERRM, clock_timestamp());
            PERFORM pg_notify('rvbbit_sync_error',
                json_build_object('job', v_jn, 'phase', 'provision', 'error', SQLERRM)::text);
        END;
        COMMIT;

        IF v_job_ok AND NOT dry_run THEN
            SELECT array_agg(c.relname ORDER BY c.relname) INTO v_tbls
            FROM pg_foreign_table ft
            JOIN pg_class c ON c.oid = ft.ftrelid
            JOIN pg_namespace ns ON ns.oid = c.relnamespace
            WHERE ns.nspname = v_fdw_schema
              AND (v_tbls IS NULL OR c.relname = ANY(v_tbls));

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
                            (extract(epoch FROM clock_timestamp() - v_t0) * 1000)::int, SQLERRM, v_t0);
                    PERFORM pg_notify('rvbbit_sync_error',
                        json_build_object('job', v_jn, 'table', v_tbl, 'error', SQLERRM)::text);
                END;
                COMMIT;
            END LOOP;
        END IF;

        UPDATE rvbbit.sync_jobs SET last_run_at = now() WHERE job_name = v_jn;
        COMMIT;
    END LOOP;

    PERFORM pg_advisory_unlock(hashtext('rvbbit.run_sync'));
END;
$$;
