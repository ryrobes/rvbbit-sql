-- =====================================================================
-- rvbbit 1.2.13 -> 1.2.14 : Temporal Mirror lock self-heal
-- =====================================================================
-- run_sync's singleton lock only self-healed after 1h of no heartbeat, and never
-- noticed a DEAD holder. A run killed mid-sync (a crash, or a server restart from
-- a deploy) orphaned the lock and wedged EVERY subsequent run for a full hour
-- ("rvbbit.run_sync is already running; skipping" with nothing actually running).
-- Now the lock is also stolen when the holder's backend pid is no longer active,
-- or when the lock predates the current server start. Only the lock-acquire
-- predicate changed.

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
