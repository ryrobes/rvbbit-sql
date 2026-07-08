-- 0132_gqe_warm_public_tables
--
-- Fix (2026-07-08, found on the Blackwell box): rvbbit.warm_gpu_gqe() picked
-- the smallest accelerated table regardless of schema, but GQE refuses
-- schema-qualified references — so on databases where that table lives in a
-- non-public schema the warm probe returned 'unavailable' on every heartbeat
-- and the router prior never engaged. The picker now considers only tables
-- whose regclass renders unqualified. Body otherwise identical to 0124.

CREATE OR REPLACE FUNCTION rvbbit.warm_gpu_gqe() RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
    v_tbl   regclass;
    v_ok    boolean := false;
    v_err   text := NULL;
    v_route text;
    v_sql   text;
    v_prior text := lower(coalesce(nullif(current_setting('rvbbit.route_gpu_gqe_prior', true), ''), 'off'));
BEGIN
    -- Self-gating: the heartbeat calls this unconditionally, so it must be a
    -- cheap no-op when the prior is disabled.
    IF v_prior IN ('off','false','0','no') THEN
        RETURN jsonb_build_object('status', 'disabled');
    END IF;

    -- Smallest accelerated table: a trivial forced-GQE count(*) over it starts
    -- the GQE server and proves it can serve a query.
    -- Smallest PUBLIC-schema accelerated table only: GQE does not support
    -- schema-qualified table references, so a qualified pick made the warm
    -- probe return 'unavailable' forever on databases whose smallest
    -- accelerated table lives outside public (0132).
    SELECT rg.table_oid::regclass INTO v_tbl
    FROM rvbbit.row_groups rg
    WHERE rg.table_oid::regclass::text NOT LIKE '%.%'
    GROUP BY rg.table_oid
    ORDER BY sum(rg.n_rows) ASC NULLS LAST
    LIMIT 1;

    IF v_tbl IS NULL THEN
        RETURN jsonb_build_object('status', 'no_accelerated_tables');
    END IF;

    v_sql := format('SELECT count(*) FROM %s', v_tbl);
    PERFORM set_config('rvbbit.route_force_candidate', 'gpu_gqe', true);

    -- Confirm GQE would ACTUALLY serve this query (eligible + gate on). A
    -- forced-but-ineligible GQE silently falls back to native, which must NOT be
    -- recorded as warm. route_explain re-runs candidate_availability without
    -- executing, so its `route` is the true engine that would run.
    SELECT rvbbit.route_explain(v_sql)->>'route' INTO v_route;
    IF v_route IS DISTINCT FROM 'gpu_gqe' THEN
        PERFORM set_config('rvbbit.route_force_candidate', '', true);
        RETURN jsonb_build_object('status', 'unavailable', 'table', v_tbl::text, 'route', v_route);
    END IF;

    BEGIN
        -- fail-open OFF so a GQE failure ERRORS (caught below) instead of
        -- silently falling back to native and falsely reporting warm.
        PERFORM set_config('rvbbit.duck_backend_fail_open', 'off', true);
        EXECUTE v_sql;
        v_ok := true;
    EXCEPTION WHEN OTHERS THEN
        v_err := SQLERRM;
    END;
    -- Restore (is_local, but accel_tick calls us inside its own txn).
    PERFORM set_config('rvbbit.route_force_candidate', '', true);
    PERFORM set_config('rvbbit.duck_backend_fail_open', 'on', true);

    IF v_ok THEN
        INSERT INTO rvbbit.gqe_warm_state (id, warm_at) VALUES (1, clock_timestamp())
        ON CONFLICT (id) DO UPDATE SET warm_at = excluded.warm_at;
    END IF;

    RETURN jsonb_build_object(
        'status', CASE WHEN v_ok THEN 'warm' ELSE 'failed' END,
        'table', v_tbl::text,
        'error', v_err
    );
END $fn$;
