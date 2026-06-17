-- 0043_cube_refresh_resource_cap
--
-- Cube rebuild was maxing the box and starving foreground queries. The cause is
-- CPU oversubscription, not locking: snapshot_load runs the cube's aggregation
-- through the in-process DataFusion pool, which fans out to min(cores, 8) worker
-- threads — on an 8-core box that alone pegs every core, so concurrent query
-- backends get no CPU. compact() adds more.
--
-- Two relief valves (the 0029 between-cubes pacing only helped BETWEEN cubes):
--
-- 1. refresh_cube caps engine parallelism for its own duration: it routes the
--    heavy cube query off the in-process DataFusion pool onto the SEPARATE duck
--    sidecar process (df_inprocess=off) and bounds that sidecar's thread count.
--    A capped, separate process can't pin all cores — foreground PG keeps the
--    rest. SET LOCAL = transaction-scoped; refresh_all_cubes COMMITs per cube, so
--    putting the cap in refresh_cube re-applies it for every cube. Tune with:
--        SET rvbbit.refresh_max_threads = '3';   -- threads of N cores (default 2)
--
-- 2. refresh_all_cubes_bg() dispatches the (capped) bulk refresh to a detached
--    backend via dblink async, so the calling session returns immediately and
--    isn't blocked. The detached backend is a fresh autocommit connection, so the
--    per-cube cap applies cleanly there too.

-- ── 1. Capped refresh_cube ───────────────────────────────────────────
CREATE OR REPLACE FUNCTION rvbbit.refresh_cube(p_name text)
RETURNS bigint LANGUAGE plpgsql AS $fn$
DECLARE
    v_dest regclass := to_regclass('cubes.' || quote_ident(p_name));
    v_sql  text;
    v_rows bigint;
    -- Thread budget for the refresh's heavy query. Default 2 (leaves headroom on
    -- a small box). Override per-session/globally with rvbbit.refresh_max_threads.
    v_cap  text := coalesce(nullif(current_setting('rvbbit.refresh_max_threads', true), ''), '2');
BEGIN
    IF v_dest IS NULL THEN
        RAISE EXCEPTION 'rvbbit.refresh_cube: cube % does not exist (define it first)', p_name;
    END IF;
    SELECT sql INTO v_sql FROM rvbbit.cube_catalog WHERE name = p_name;
    IF v_sql IS NULL THEN
        RAISE EXCEPTION 'rvbbit.refresh_cube: no definition for cube %', p_name;
    END IF;

    -- Resource cap (see header). Route the cube query to the bounded duck sidecar
    -- instead of the all-cores in-process DataFusion pool, scoped to this refresh.
    PERFORM set_config('rvbbit.df_inprocess', 'off', true);
    PERFORM set_config('rvbbit.duck_threads', v_cap, true);

    BEGIN
        SELECT rows_loaded INTO v_rows FROM rvbbit.snapshot_load(v_dest, v_sql);
        UPDATE rvbbit.cube_control
           SET refreshed_at = now(), last_rows = v_rows, last_error = NULL, updated_at = now()
         WHERE cube_name = p_name;
    EXCEPTION WHEN OTHERS THEN
        UPDATE rvbbit.cube_control SET last_error = SQLERRM, updated_at = now() WHERE cube_name = p_name;
        RAISE;
    END;
    RETURN v_rows;
END;
$fn$;

-- ── 2. Background (non-blocking) bulk refresh ────────────────────────
-- Fire the capped refresh_all_cubes on a detached dblink connection and return
-- immediately. The work runs in its own backend (concurrent, capped), so the
-- calling session is free. The async connection is tied to THIS session's
-- lifetime — for fully unattended runs (survive disconnect), schedule
-- refresh_all_cubes via the Scheduler/pg_cron instead.
CREATE OR REPLACE FUNCTION rvbbit.refresh_all_cubes_bg(
    p_category      text    DEFAULT NULL,
    p_subcategory   text    DEFAULT NULL,
    p_sleep_seconds numeric DEFAULT 0.5
) RETURNS text LANGUAGE plpgsql AS $fn$
DECLARE
    -- Loopback conninfo. Default = same DB over the local socket (inherits the
    -- current user). Override via rvbbit.bg_conninfo if your auth differs.
    v_conn text := coalesce(nullif(current_setting('rvbbit.bg_conninfo', true), ''),
                            'dbname=' || current_database());
    v_name text := 'rvbbit_refresh_bg';
    v_cmd  text;
BEGIN
    CREATE EXTENSION IF NOT EXISTS dblink;

    -- Reuse one named async connection. If a prior dispatch is still running,
    -- don't start a second; if it finished, drain + recycle the connection.
    IF v_name = ANY (coalesce(dblink_get_connections(), ARRAY[]::text[])) THEN
        IF dblink_is_busy(v_name) = 1 THEN
            RETURN format('a background cube refresh is already running (conn %s); not starting another', v_name);
        END IF;
        PERFORM dblink_get_result(v_name);
        PERFORM dblink_disconnect(v_name);
    END IF;

    v_cmd := format('CALL rvbbit.refresh_all_cubes(%L, %L, %s)',
                    p_category, p_subcategory, coalesce(p_sleep_seconds, 0.5)::text);

    PERFORM dblink_connect(v_name, v_conn);
    PERFORM dblink_send_query(v_name, v_cmd);

    RETURN format(
        'cube refresh dispatched in background (conn=%s, capped to rvbbit.refresh_max_threads). '
        'This session is not blocked. Tied to this session; for unattended runs use the Scheduler.',
        v_conn);
END;
$fn$;
