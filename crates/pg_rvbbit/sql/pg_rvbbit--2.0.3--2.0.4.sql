-- Upgrade pg_rvbbit 2.0.3 -> 2.0.4
--
-- Temporal Mirror: skip the DROP + IMPORT FOREIGN SCHEMA churn when the remote
-- schema is unchanged (cheap ~20ms fingerprint vs a per-table DDL storm that
-- invalidates catalog caches DB-wide). Re-imports on drift / first run / missing
-- foreign tables / explicit force. Pure PL/pgSQL — function-body + additive only.


-- 1. Operational metadata: the last-seen remote fingerprint + when we re-imported.
ALTER TABLE rvbbit.sync_jobs ADD COLUMN IF NOT EXISTS fdw_fingerprint text;
ALTER TABLE rvbbit.sync_jobs ADD COLUMN IF NOT EXISTS fdw_imported_at  timestamptz;

-- 2. Cheap remote-schema fingerprint over postgres_fdw — a single stable foreign
--    table per server over the remote information_schema.columns (created once,
--    reused), hashed. Returns the fingerprint + the remote table count so the
--    caller can also detect missing local foreign tables.
CREATE OR REPLACE FUNCTION rvbbit.fdw_remote_fingerprint(
    server_name text, remote_schema text, only_tables text[] DEFAULT NULL
) RETURNS TABLE(fingerprint text, n_tables int)
LANGUAGE plpgsql AS $fn$
DECLARE
    v_meta text := format('%I.%I', 'rvbbit_meta', 'cols__' || server_name);
BEGIN
    CREATE SCHEMA IF NOT EXISTS rvbbit_meta;
    EXECUTE format($f$
        CREATE FOREIGN TABLE IF NOT EXISTS %s (
            table_schema text, table_name text, column_name text,
            ordinal_position int, data_type text,
            character_maximum_length int, numeric_precision int, numeric_scale int, udt_name text
        ) SERVER %I OPTIONS (schema_name 'information_schema', table_name 'columns')
    $f$, v_meta, server_name);
    RETURN QUERY EXECUTE format($q$
        SELECT md5(coalesce(string_agg(
                   table_name||'|'||ordinal_position||'|'||column_name||'|'||data_type||'|'||
                   coalesce(character_maximum_length::text,'')||'|'||
                   coalesce(numeric_precision::text,'')||'|'||
                   coalesce(numeric_scale::text,'')||'|'||udt_name,
                   ',' ORDER BY table_name, ordinal_position), '')),
               count(DISTINCT table_name)::int
        FROM %s
        WHERE table_schema = %L AND (%L::text[] IS NULL OR table_name = ANY(%L::text[]))
    $q$, v_meta, remote_schema, only_tables, only_tables);
END $fn$;

-- 3. Escape hatch: clear stored fingerprints so the next run re-imports (NULL job
--    = all jobs). Use after a manual fdw change, or if you suspect drift slipped by.
CREATE OR REPLACE FUNCTION rvbbit.reset_sync_fingerprint(p_job_name text DEFAULT NULL)
RETURNS integer LANGUAGE sql AS $$
    WITH upd AS (
        UPDATE rvbbit.sync_jobs SET fdw_fingerprint = NULL
        WHERE p_job_name IS NULL OR job_name = p_job_name
        RETURNING 1
    )
    SELECT count(*)::int FROM upd;
$$;

-- 4. run_sync, with the fingerprint gate around fdw_import. Everything else is
--    byte-identical to the shipped procedure.
CREATE OR REPLACE PROCEDURE rvbbit.run_sync(IN p_job_name text DEFAULT NULL::text, IN dry_run boolean DEFAULT false)
LANGUAGE plpgsql AS $procedure$
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
    -- fingerprint gate
    v_fp         text;
    v_prev_fp    text;
    v_remote_n   int;
    v_ft_present int;
    v_force      boolean;
BEGIN
    INSERT INTO rvbbit.sync_lock (id, acquired_at, pid)
    VALUES (1, clock_timestamp(), pg_backend_pid())
    ON CONFLICT (id) DO UPDATE
        SET acquired_at = clock_timestamp(), pid = pg_backend_pid()
        WHERE rvbbit.sync_lock.acquired_at < clock_timestamp() - interval '1 hour'
           OR rvbbit.sync_lock.acquired_at <= pg_postmaster_start_time()
           OR NOT EXISTS (SELECT 1 FROM pg_stat_activity a
                          WHERE a.pid = rvbbit.sync_lock.pid)
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

            -- Skip the expensive DROP + IMPORT FOREIGN SCHEMA (a catalog-invalidation
            -- storm that slows the whole DB) when the remote shape is unchanged AND the
            -- foreign tables are all present. Re-import only on drift / first run /
            -- missing FTs / explicit force.  ~20ms fingerprint vs per-table DDL.
            v_fp := NULL; v_remote_n := NULL;
            BEGIN
                SELECT fingerprint, n_tables INTO v_fp, v_remote_n
                FROM rvbbit.fdw_remote_fingerprint(v_srv->>'name', v_remote, v_spec_tbls);
            EXCEPTION WHEN OTHERS THEN
                v_fp := NULL; v_remote_n := NULL;  -- can't fingerprint => import (surfaces the real error)
            END;

            SELECT fdw_fingerprint INTO v_prev_fp FROM rvbbit.sync_jobs WHERE job_name = v_jn;
            SELECT count(*) INTO v_ft_present
            FROM pg_foreign_table ft
            JOIN pg_class c ON c.oid = ft.ftrelid
            JOIN pg_namespace ns ON ns.oid = c.relnamespace
            WHERE ns.nspname = v_fdw_schema
              AND (v_spec_tbls IS NULL OR c.relname = ANY(v_spec_tbls));
            v_force := lower(coalesce(current_setting('rvbbit.sync_force_reimport', true), 'off'))
                       IN ('1','true','on','yes');

            IF v_force
               OR v_fp IS NULL                              -- couldn't fingerprint => be safe
               OR v_prev_fp IS DISTINCT FROM v_fp           -- schema drift (or first run)
               OR v_ft_present IS DISTINCT FROM v_remote_n  -- foreign tables missing/extra
            THEN
                PERFORM rvbbit.fdw_import(v_srv->>'name', v_remote, v_fdw_schema, v_spec_tbls);
                UPDATE rvbbit.sync_jobs
                   SET fdw_fingerprint = v_fp, fdw_imported_at = now()
                 WHERE job_name = v_jn;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            v_job_ok := false;
            INSERT INTO rvbbit.sync_runs(run_id, job_name, action, error, started_at)
            VALUES (v_rid, v_jn, 'error', 'provisioning: ' || left(SQLERRM, 500), clock_timestamp());
            PERFORM pg_notify('rvbbit_sync_error',
                json_build_object('job', v_jn, 'phase', 'provision', 'error', left(SQLERRM, 500))::text);
        END;
        COMMIT;

        IF v_job_ok THEN
            SELECT array_agg(c.relname ORDER BY c.relname) INTO v_tbls
            FROM pg_foreign_table ft
            JOIN pg_class c ON c.oid = ft.ftrelid
            JOIN pg_namespace ns ON ns.oid = c.relnamespace
            WHERE ns.nspname = v_fdw_schema
              AND (v_spec_tbls IS NULL OR c.relname = ANY(v_spec_tbls));

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
$procedure$;
