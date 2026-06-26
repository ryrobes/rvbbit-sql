-- Make accelerator runtime capability detection visible on existing installs.
-- Fresh installs get the Rust pg_extern binding from the extension SQL; migrated
-- installs need the C binding created explicitly so `rvbbit.migrate()` is enough
-- after redeploy.

CREATE OR REPLACE FUNCTION rvbbit.accelerator_runtime_status(live boolean DEFAULT false)
RETURNS jsonb
LANGUAGE c
AS '$libdir/pg_rvbbit', 'accelerator_runtime_status_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.doctor(live boolean DEFAULT false)
RETURNS TABLE (
    area text,
    name text,
    status text,
    detail jsonb
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_extversion text;
    v_rvbbit_tables bigint;
    v_row_groups bigint;
    v_variants bigint;
    v_dirty bigint;
    v_route_status jsonb;
    v_cost_total bigint;
    v_cost_problem bigint;
    v_cost_warn bigint;
    v_mcp_servers bigint;
    v_mcp_tools bigint;
    v_warren_nodes bigint;
    v_warren_bad_jobs bigint;
    v_backend_count bigint;
    v_accel_status jsonb;
BEGIN
    SELECT e.extversion INTO v_extversion
    FROM pg_extension e
    WHERE e.extname = 'pg_rvbbit';

    RETURN QUERY
    SELECT
        'core'::text,
        'extension'::text,
        CASE WHEN v_extversion IS NULL THEN 'error' ELSE 'ok' END::text,
        jsonb_build_object('extversion', v_extversion);

    SELECT count(*), count(*) FILTER (WHERE shadow_heap_dirty)
    INTO v_rvbbit_tables, v_dirty
    FROM rvbbit.table_dirty_state;

    SELECT count(*) INTO v_row_groups FROM rvbbit.row_groups;
    SELECT count(*) INTO v_variants FROM rvbbit.row_group_variants;

    RETURN QUERY
    SELECT
        'storage'::text,
        'rvbbit_tables'::text,
        CASE WHEN coalesce(v_dirty, 0) > 0 THEN 'warn' ELSE 'ok' END::text,
        jsonb_build_object(
            'tables', coalesce(v_rvbbit_tables, 0),
            'dirty_shadow_heaps', coalesce(v_dirty, 0),
            'row_groups', coalesce(v_row_groups, 0),
            'layout_variants', coalesce(v_variants, 0)
        );

    BEGIN
        SELECT rvbbit.accelerator_runtime_status(live) INTO v_accel_status;
        RETURN QUERY
        SELECT
            'accelerator'::text,
            'runtime'::text,
            coalesce(nullif(v_accel_status->>'status', ''), 'warn')::text,
            v_accel_status;
    EXCEPTION WHEN undefined_function THEN
        RETURN QUERY
        SELECT
            'accelerator'::text,
            'runtime'::text,
            'warn'::text,
            jsonb_build_object('reason', 'accelerator_runtime_status_unavailable');
    END;

    BEGIN
        SELECT rvbbit.route_status() INTO v_route_status;
        RETURN QUERY
        SELECT
            'routing'::text,
            'route_status'::text,
            'ok'::text,
            v_route_status;
    EXCEPTION WHEN undefined_function THEN
        RETURN QUERY
        SELECT
            'routing'::text,
            'route_status'::text,
            'warn'::text,
            jsonb_build_object('reason', 'route_status_unavailable');
    END;

    SELECT count(*) INTO v_backend_count FROM rvbbit.backends;
    RETURN QUERY
    SELECT
        'backend'::text,
        'registry'::text,
        CASE WHEN coalesce(v_backend_count, 0) > 0 THEN 'ok' ELSE 'error' END::text,
        jsonb_build_object('backends', coalesce(v_backend_count, 0));

    RETURN QUERY SELECT * FROM rvbbit.provider_doctor(live);

    SELECT
        count(*),
        count(*) FILTER (
            WHERE audit_status IN ('missing_cost_events', 'stale_pending', 'errors')
        ),
        count(*) FILTER (
            WHERE audit_status IN ('pending', 'uncosted')
        )
    INTO v_cost_total, v_cost_problem, v_cost_warn
    FROM rvbbit.receipt_cost_audit;

    RETURN QUERY
    SELECT
        'costs'::text,
        'receipt_cost_audit'::text,
        CASE
            WHEN coalesce(v_cost_problem, 0) > 0 THEN 'error'
            WHEN coalesce(v_cost_warn, 0) > 0 THEN 'warn'
            ELSE 'ok'
        END::text,
        jsonb_build_object(
            'receipt_rows', coalesce(v_cost_total, 0),
            'problem_rows', coalesce(v_cost_problem, 0),
            'warning_rows', coalesce(v_cost_warn, 0)
        );

    SELECT count(*) INTO v_mcp_servers FROM rvbbit.mcp_servers;
    SELECT count(*) INTO v_mcp_tools FROM rvbbit.mcp_tools;

    RETURN QUERY
    SELECT
        'mcp'::text,
        'registry'::text,
        'ok'::text,
        jsonb_build_object(
            'servers', coalesce(v_mcp_servers, 0),
            'tools', coalesce(v_mcp_tools, 0)
        );

    SELECT count(*) INTO v_warren_nodes FROM rvbbit.warren_nodes;
    SELECT count(*) FILTER (WHERE wj.status = 'failed')
    INTO v_warren_bad_jobs
    FROM rvbbit.warren_jobs wj;

    RETURN QUERY
    SELECT
        'warren'::text,
        'registry'::text,
        CASE WHEN coalesce(v_warren_bad_jobs, 0) > 0 THEN 'warn' ELSE 'ok' END::text,
        jsonb_build_object(
            'nodes', coalesce(v_warren_nodes, 0),
            'failed_jobs', coalesce(v_warren_bad_jobs, 0)
        );
END
$$;
