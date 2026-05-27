-- pg_rvbbit 0.55.0 -> 0.56.0
-- Release diagnostics for install/provider/runtime health.

CREATE TABLE IF NOT EXISTS rvbbit.group_stats (
    table_oid        oid NOT NULL,
    rg_id            bigint NOT NULL,
    group_col        text NOT NULL,
    group_key        text NOT NULL,
    group_value_text text,
    count            bigint NOT NULL,
    agg              jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, rg_id, group_col, group_key),
    FOREIGN KEY (table_oid, rg_id)
        REFERENCES rvbbit.row_groups(table_oid, rg_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS group_stats_lookup_idx
    ON rvbbit.group_stats (table_oid, group_col, group_key);

CREATE TABLE IF NOT EXISTS rvbbit.column_bitmaps (
    table_oid   oid NOT NULL,
    rg_id       bigint NOT NULL,
    column_name text NOT NULL,
    bitmap_kind text NOT NULL,
    value_text  text NOT NULL,
    value_json  jsonb,
    bitmap      bytea NOT NULL,
    n_set       bigint NOT NULL,
    n_total     bigint NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, rg_id, column_name, bitmap_kind, value_text),
    FOREIGN KEY (table_oid, rg_id)
        REFERENCES rvbbit.row_groups(table_oid, rg_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS column_bitmaps_lookup_idx
    ON rvbbit.column_bitmaps (table_oid, column_name, bitmap_kind, value_text);

CREATE TABLE IF NOT EXISTS rvbbit.text_dictionaries (
    table_oid   oid NOT NULL,
    rg_id       bigint NOT NULL,
    column_name text NOT NULL,
    path        text NOT NULL,
    n_rows      bigint NOT NULL,
    n_values    bigint NOT NULL,
    n_nulls     bigint NOT NULL,
    n_empty     bigint NOT NULL,
    n_bytes     bigint NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_oid, rg_id, column_name),
    FOREIGN KEY (table_oid, rg_id)
        REFERENCES rvbbit.row_groups(table_oid, rg_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS text_dictionaries_lookup_idx
    ON rvbbit.text_dictionaries (table_oid, column_name, rg_id);

CREATE OR REPLACE FUNCTION rvbbit.env_present(env_name text)
RETURNS boolean
STRICT STABLE
LANGUAGE c
AS 'MODULE_PATHNAME', 'env_present_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.provider_doctor(live boolean DEFAULT false)
RETURNS TABLE (
    area text,
    name text,
    status text,
    detail jsonb
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_default text;
    v_default_exists boolean;
    b record;
    v_auth_present boolean;
    v_model text;
    v_has_policy boolean;
    v_has_rate boolean;
    v_has_catalog boolean;
    v_status text;
    v_reason text;
    v_probe jsonb;
BEGIN
    SELECT rvbbit.default_provider() INTO v_default;
    SELECT EXISTS(SELECT 1 FROM rvbbit.backends ab WHERE ab.name = v_default)
    INTO v_default_exists;

    RETURN QUERY
    SELECT
        'provider'::text,
        'default'::text,
        CASE WHEN v_default_exists THEN 'ok' ELSE 'error' END::text,
        jsonb_build_object(
            'default_provider', v_default,
            'exists', v_default_exists,
            'env_override_supported', true
        );

    FOR b IN
        SELECT ab.name, ab.transport, ab.endpoint_url, ab.max_concurrent,
               ab.timeout_ms, ab.auth_header_env, ab.transport_opts,
               ab.source_provider, ab.source_model
        FROM rvbbit.backends ab
        WHERE ab.transport IN ('openai_chat', 'anthropic', 'gemini', 'stub')
        ORDER BY ab.name
    LOOP
        v_model := nullif(coalesce(b.transport_opts->>'model', b.source_model), '');
        v_auth_present := b.auth_header_env IS NULL OR rvbbit.env_present(b.auth_header_env);

        SELECT EXISTS(
            SELECT 1
            FROM rvbbit.cost_policies cp
            WHERE cp.target_kind = 'backend'
              AND cp.target_name = b.name
        ) INTO v_has_policy;

        SELECT v_model IS NOT NULL AND EXISTS(
            SELECT 1
            FROM rvbbit.model_rates mr
            WHERE mr.model = v_model
        ) INTO v_has_rate;

        SELECT v_model IS NOT NULL AND EXISTS(
            SELECT 1
            FROM rvbbit.provider_models pm
            WHERE pm.model = v_model
        ) INTO v_has_catalog;

        v_status := 'ok';
        v_reason := NULL;
        v_probe := NULL;

        IF NOT v_auth_present THEN
            v_status := CASE WHEN live THEN 'error' ELSE 'warn' END;
            v_reason := 'missing_auth_env';
        ELSIF live
              AND b.transport IN ('openai_chat', 'anthropic', 'gemini')
              AND v_model IS NULL THEN
            v_status := 'warn';
            v_reason := 'live_probe_skipped_no_default_model';
        ELSIF live THEN
            BEGIN
                SELECT rvbbit.backend_probe(b.name) INTO v_probe;
                IF NOT coalesce((v_probe->>'ok')::boolean, false) THEN
                    v_status := 'error';
                    v_reason := 'probe_failed';
                END IF;
            EXCEPTION WHEN others THEN
                v_status := 'error';
                v_reason := 'probe_exception';
                v_probe := jsonb_build_object('ok', false, 'error', SQLERRM);
            END;
        END IF;

        IF v_status = 'ok'
           AND b.transport <> 'stub'
           AND NOT v_has_policy
           AND NOT v_has_rate THEN
            v_status := 'warn';
            v_reason := 'no_cost_policy_or_model_rate';
        END IF;

        RETURN QUERY
        SELECT
            'provider'::text,
            b.name::text,
            v_status::text,
            jsonb_build_object(
                'transport', b.transport,
                'endpoint_url', b.endpoint_url,
                'max_concurrent', b.max_concurrent,
                'timeout_ms', b.timeout_ms,
                'auth_header_env', b.auth_header_env,
                'auth_present', v_auth_present,
                'model', v_model,
                'source_provider', b.source_provider,
                'source_model', b.source_model,
                'has_cost_policy', v_has_policy,
                'has_model_rate', v_has_rate,
                'has_provider_catalog_row', v_has_catalog,
                'reason', v_reason,
                'probe', v_probe
            );
    END LOOP;
END
$$;

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
    FROM rvbbit.tables;

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
