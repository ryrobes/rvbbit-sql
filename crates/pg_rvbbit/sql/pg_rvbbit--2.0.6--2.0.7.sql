-- Upgrade pg_rvbbit 2.0.6 -> 2.0.7
--
-- Temporal Mirror: run_sync writes snapshots ONLY by default — it skips the heavy
-- inline columnar-variant (vortex) build so a big sync can't monopolize the box.
-- Sync-scoped (SET LOCAL per table); manual compact()/rebuild_acceleration() and an
-- explicit rvbbit.compact_variants_sync override are unaffected. The snapshot/
-- generation (the time-travel watermark) is still written every run. Function-body
-- only (CREATE OR REPLACE), idempotent.

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
    -- fingerprint gate (skip DROP+IMPORT FOREIGN SCHEMA when the remote is unchanged)
    v_fp         text;
    v_prev_fp    text;
    v_remote_n   int;
    v_ft_present int;
    v_force      boolean;
    v_skip_variants boolean;
BEGIN
    -- Self-healing singleton lock. Steal it when the holder is provably gone —
    -- its backend pid is no longer active, or the lock predates this server's
    -- start (a restart killed the holder mid-sync, e.g. a deploy) — or, as a last
    -- resort, when it hasn't heartbeated in > 1h. Without the first two checks a
    -- crashed/killed run wedged every subsequent run for a full hour.
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

    -- Sync writes the snapshot (the time-travel generation) but skips the heavy
    -- inline columnar-variant build (vortex) BY DEFAULT — a big sync shouldn't
    -- monopolize the box building accelerators it can maintain off the critical
    -- path. We force rvbbit.compact_variants_sync=off per-table (SET LOCAL, reverts
    -- at each COMMIT, sync-scoped — manual compact()/rebuild_acceleration in other
    -- sessions are unaffected). An EXPLICIT rvbbit.compact_variants_sync override
    -- (on or off) still wins, so this only sets the default when it's unset.
    v_skip_variants := nullif(current_setting('rvbbit.compact_variants_sync', true), '') IS NULL;

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
            -- storm that slows every query DB-wide) when the remote shape is unchanged
            -- AND the foreign tables are all present. Re-import only on drift / first
            -- run / missing FTs / explicit force. ~20ms fingerprint vs per-table DDL.
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
                v_t0 := clock_timestamp();
                PERFORM rvbbit.fdw_import(v_srv->>'name', v_remote, v_fdw_schema, v_spec_tbls);
                -- Record the IMPORT FOREIGN SCHEMA phase as a first-class row: it was
                -- previously invisible (no per-table row), so the overview could not
                -- show import time. action='import', rows_loaded = # foreign tables.
                -- Its ABSENCE in a sweep = the import was skipped (schema unchanged).
                INSERT INTO rvbbit.sync_runs(run_id, job_name, action, rows_loaded, elapsed_ms, started_at)
                VALUES (v_rid, v_jn, 'import', v_remote_n,
                        (extract(epoch FROM clock_timestamp() - v_t0) * 1000)::int, v_t0);
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
                    -- snapshot-only: skip the inline vortex build for this table's
                    -- compact (transaction-local, reverts at the COMMIT below).
                    IF v_skip_variants THEN
                        PERFORM set_config('rvbbit.compact_variants_sync', 'off', true);
                    END IF;
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
