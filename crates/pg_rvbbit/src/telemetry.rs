//! Specialist + LLM telemetry — usage rollups + health probes.
//!
//! Surfaces what's already buried inside `rvbbit.receipts.sub_calls`
//! as queryable SQL, plus a live `/health` probe across every
//! registered specialist.
//!
//!   SELECT * FROM rvbbit.specialist_usage ORDER BY total_latency_ms DESC;
//!   SELECT * FROM rvbbit.specialist_health();
//!
//! No new storage cost — the rollup view aggregates over receipts in
//! real time. For long-term retention you can `CREATE TABLE
//! specialist_usage_archive AS SELECT * FROM rvbbit.specialist_usage;`
//! on a schedule.

use std::time::{Duration, Instant};

use pgrx::extension_sql;
use pgrx::prelude::*;
use reqwest::blocking::Client;

extension_sql!(
    r#"
-- Per-specialist usage rolled up from rvbbit.receipts.sub_calls.
-- Updates in real time; zero storage cost (it's just a view).
CREATE OR REPLACE VIEW rvbbit.specialist_usage AS
WITH expanded AS (
    SELECT
        sub->>'model'                 AS specialist,
        (sub->>'tokens_in')::int      AS tokens_in,
        (sub->>'tokens_out')::int     AS tokens_out,
        (sub->>'latency_ms')::int     AS latency_ms,
        sub->>'error'                 AS error,
        r.operator,
        r.invocation_at
    FROM rvbbit.receipts r,
         jsonb_array_elements(r.sub_calls) AS sub
    WHERE sub->>'kind' = 'specialist'
)
SELECT
    specialist,
    count(*)                                                AS n_calls,
    count(*) FILTER (WHERE error IS NOT NULL)               AS n_errors,
    count(DISTINCT operator)                                AS n_operators_using,
    coalesce(sum(tokens_in), 0)                             AS total_tokens_in,
    coalesce(sum(tokens_out), 0)                            AS total_tokens_out,
    coalesce(sum(latency_ms), 0)                            AS total_latency_ms,
    coalesce(round(avg(latency_ms))::int, 0)                AS avg_latency_ms,
    coalesce(percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms)::int, 0) AS p50_latency_ms,
    coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::int, 0) AS p95_latency_ms,
    min(invocation_at)                                      AS first_call_at,
    max(invocation_at)                                      AS last_call_at
FROM expanded
WHERE specialist IS NOT NULL
GROUP BY specialist;

-- Same rollup shape for the LLM kind, so users can compare "how much
-- am I spending on LLM vs specialist routes" at a glance.
CREATE OR REPLACE VIEW rvbbit.llm_usage AS
WITH expanded AS (
    SELECT
        sub->>'model'                 AS model,
        (sub->>'tokens_in')::int      AS tokens_in,
        (sub->>'tokens_out')::int     AS tokens_out,
        (sub->>'latency_ms')::int     AS latency_ms,
        sub->>'error'                 AS error,
        r.operator,
        r.invocation_at
    FROM rvbbit.receipts r,
         jsonb_array_elements(r.sub_calls) AS sub
    WHERE sub->>'kind' = 'llm'
)
SELECT
    model,
    count(*)                                                AS n_calls,
    count(*) FILTER (WHERE error IS NOT NULL)               AS n_errors,
    count(DISTINCT operator)                                AS n_operators_using,
    coalesce(sum(tokens_in), 0)                             AS total_tokens_in,
    coalesce(sum(tokens_out), 0)                            AS total_tokens_out,
    coalesce(sum(latency_ms), 0)                            AS total_latency_ms,
    coalesce(round(avg(latency_ms))::int, 0)                AS avg_latency_ms,
    coalesce(percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms)::int, 0) AS p50_latency_ms,
    coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::int, 0) AS p95_latency_ms,
    min(invocation_at)                                      AS first_call_at,
    max(invocation_at)                                      AS last_call_at
FROM expanded
WHERE model IS NOT NULL
GROUP BY model;

-- Per-(server, tool) MCP call rollup. Real-time view over
-- rvbbit.mcp_invocations — what tools are getting hit, how fast, how
-- often they error. Parallel to specialist_usage / llm_usage.
CREATE OR REPLACE VIEW rvbbit.mcp_usage AS
SELECT
    server,
    tool,
    count(*)                                                AS n_calls,
    count(*) FILTER (WHERE error IS NOT NULL)               AS n_errors,
    coalesce(sum(latency_ms), 0)                            AS total_latency_ms,
    coalesce(round(avg(latency_ms))::int, 0)                AS avg_latency_ms,
    coalesce(percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms)::int, 0) AS p50_latency_ms,
    coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::int, 0) AS p95_latency_ms,
    min(invocation_at)                                      AS first_call_at,
    max(invocation_at)                                      AS last_call_at
FROM rvbbit.mcp_invocations
GROUP BY server, tool;

-- Per-server MCP health snapshot. Joins the registry with discovery
-- state and the invocation audit so an operator dashboard can show
-- which servers are configured, how many tools each exposes, when each
-- was last successfully called, and when it last errored.
CREATE OR REPLACE VIEW rvbbit.mcp_health AS
SELECT
    s.name,
    s.transport,
    coalesce(t.n_tools, 0)                  AS n_tools,
    coalesce(r.n_resources, 0)              AS n_resources,
    t.last_discovered_at,
    i.last_call_at,
    i.last_error_at,
    s.created_at
FROM rvbbit.mcp_servers s
LEFT JOIN (
    SELECT server,
           count(*)::int        AS n_tools,
           max(discovered_at)   AS last_discovered_at
    FROM rvbbit.mcp_tools
    GROUP BY server
) t ON t.server = s.name
LEFT JOIN (
    SELECT server, count(*)::int AS n_resources
    FROM rvbbit.mcp_resources
    GROUP BY server
) r ON r.server = s.name
LEFT JOIN (
    SELECT server,
           max(invocation_at) FILTER (WHERE error IS NULL)     AS last_call_at,
           max(invocation_at) FILTER (WHERE error IS NOT NULL) AS last_error_at
    FROM rvbbit.mcp_invocations
    GROUP BY server
) i ON i.server = s.name;

-- Consolidated backend registry + usage snapshot. Passive view: it does not
-- make network calls. Use rvbbit.backend_probe(name) or specialist_health()
-- when the UI needs an active check.
CREATE OR REPLACE VIEW rvbbit.backend_health AS
SELECT
    b.name,
    b.transport,
    b.endpoint_url,
    b.batch_size,
    b.max_concurrent,
    b.timeout_ms,
    b.auth_header_env,
    b.transport_opts,
    b.description,
    b.source_provider,
    b.source_model,
    b.source_revision,
    b.install_manifest,
    coalesce(u.n_calls, 0) AS n_calls,
    coalesce(u.n_errors, 0) AS n_errors,
    u.avg_latency_ms,
    u.p50_latency_ms,
    u.p95_latency_ms,
    u.first_call_at,
    u.last_call_at,
    b.created_at
FROM rvbbit.backends b
LEFT JOIN rvbbit.specialist_usage u ON u.specialist = b.name;
"#,
    name = "create_telemetry_views",
    requires = ["rvbbit_bootstrap"]
);

extension_sql!(
    r#"
-- Release-oriented diagnostics. These functions are intentionally tabular
-- and JSON-rich so psql, acceptance tests, and a UI can all use the same
-- surface.
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
    v_disabled bigint;
    v_am_bound bigint;
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
    SELECT count(*) INTO v_disabled
    FROM rvbbit.tables
    WHERE NOT coalesce(acceleration_enabled, true);
    SELECT count(*) INTO v_am_bound
    FROM pg_class c
    JOIN pg_am a ON a.oid = c.relam
    WHERE a.amname = 'rvbbit';

    SELECT count(*) INTO v_row_groups FROM rvbbit.row_groups;
    SELECT count(*) INTO v_variants FROM rvbbit.row_group_variants;

    RETURN QUERY
    SELECT
        'storage'::text,
        'rvbbit_tables'::text,
        CASE WHEN coalesce(v_dirty, 0) > 0 THEN 'warn' ELSE 'ok' END::text,
        jsonb_build_object(
            'tables', coalesce(v_rvbbit_tables, 0),
            'disabled_tables', coalesce(v_disabled, 0),
            'dirty_shadow_heaps', coalesce(v_dirty, 0),
            'row_groups', coalesce(v_row_groups, 0),
            'layout_variants', coalesce(v_variants, 0)
        );

    RETURN QUERY
    SELECT
        'storage'::text,
        'access_method_aliases'::text,
        CASE WHEN coalesce(v_am_bound, 0) > 0 THEN 'warn' ELSE 'ok' END::text,
        jsonb_build_object(
            'am_bound_tables', coalesce(v_am_bound, 0),
            'impact', CASE
                WHEN coalesce(v_am_bound, 0) > 0
                THEN 'DROP EXTENSION pg_rvbbit will be blocked until these tables are disabled or converted to heap'
                ELSE 'all registered acceleration tables are heap catalog tables'
            END,
            'fix', 'SELECT rvbbit.disable_table(''schema.table''::regclass)'
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
"#,
    name = "create_doctor_functions",
    requires = [
        "create_telemetry_views",
        "provider_catalog_schema",
        "rvbbit_cost_catalog"
    ]
);

/// Live /health probe across every registered specialist. Derives the
/// health URL from each specialist's endpoint by stripping the path
/// and appending `/health`. Reports reachability + round-trip latency
/// + whatever the sidecar returns in its JSON body.
///
/// Use to catch drift (sidecar restarted with a different model name),
/// dead containers, slow / overloaded specialists.
#[pg_extern(volatile)]
fn specialist_health() -> TableIterator<
    'static,
    (
        name!(specialist, String),
        name!(transport, String),
        name!(endpoint, String),
        name!(reachable, bool),
        name!(latency_ms, i32),
        name!(reported_model, Option<String>),
        name!(error, Option<String>),
    ),
> {
    let specs = load_all_specs();
    let client = Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
        .ok();
    let mut out: Vec<(
        String,
        String,
        String,
        bool,
        i32,
        Option<String>,
        Option<String>,
    )> = Vec::new();

    for s in specs {
        let probe = health_url(&s.endpoint_url);
        match (&client, probe) {
            (Some(c), Some(probe_url)) => {
                let t0 = Instant::now();
                match c.get(&probe_url).send() {
                    Ok(resp) => {
                        let latency = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;
                        let status = resp.status();
                        if status.is_success() {
                            let body = resp.text().unwrap_or_default();
                            let reported_model: Option<String> =
                                serde_json::from_str::<serde_json::Value>(&body)
                                    .ok()
                                    .and_then(|v| {
                                        v.get("model")
                                            .and_then(|m| m.as_str().map(|s| s.to_string()))
                                    });
                            out.push((
                                s.name,
                                s.transport,
                                probe_url,
                                true,
                                latency,
                                reported_model,
                                None,
                            ));
                        } else {
                            out.push((
                                s.name,
                                s.transport,
                                probe_url,
                                false,
                                latency,
                                None,
                                Some(format!("HTTP {}", status.as_u16())),
                            ));
                        }
                    }
                    Err(e) => {
                        let latency = t0.elapsed().as_millis().min(i32::MAX as u128) as i32;
                        out.push((
                            s.name,
                            s.transport,
                            probe_url,
                            false,
                            latency,
                            None,
                            Some(e.to_string()),
                        ));
                    }
                }
            }
            (_, None) => {
                out.push((
                    s.name,
                    s.transport,
                    s.endpoint_url,
                    false,
                    0,
                    None,
                    Some("could not derive /health URL".into()),
                ));
            }
            (None, _) => {
                out.push((
                    s.name,
                    s.transport,
                    s.endpoint_url,
                    false,
                    0,
                    None,
                    Some("http client init failed".into()),
                ));
            }
        }
    }

    TableIterator::new(out.into_iter())
}

struct SpecRow {
    name: String,
    transport: String,
    endpoint_url: String,
}

fn load_all_specs() -> Vec<SpecRow> {
    let mut out = Vec::new();
    let _ = Spi::connect(|client| -> Result<(), pgrx::spi::Error> {
        let table = client.select(
            "SELECT name, transport, endpoint_url FROM rvbbit.backends ORDER BY name",
            None,
            &[],
        )?;
        for row in table {
            let name: Option<String> = row.get(1)?;
            let transport: Option<String> = row.get(2)?;
            let endpoint: Option<String> = row.get(3)?;
            if let (Some(n), Some(t), Some(e)) = (name, transport, endpoint) {
                out.push(SpecRow {
                    name: n,
                    transport: t,
                    endpoint_url: e,
                });
            }
        }
        Ok(())
    });
    out
}

/// Derive a `/health` URL from a specialist endpoint. Strips the path
/// + query, appends `/health`. Returns None if the endpoint isn't a
/// parseable URL.
fn health_url(endpoint: &str) -> Option<String> {
    let mut u = reqwest::Url::parse(endpoint).ok()?;
    u.set_path("/health");
    u.set_query(None);
    u.set_fragment(None);
    Some(u.to_string())
}

#[cfg(test)]
mod tests {
    use super::health_url;

    #[test]
    fn health_url_strips_path_query_fragment() {
        assert_eq!(
            health_url("http://embed:8080/predict"),
            Some("http://embed:8080/health".into())
        );
        assert_eq!(
            health_url("http://rerank:7860/api/predict"),
            Some("http://rerank:7860/health".into())
        );
        assert_eq!(
            health_url("https://api.openai.com/v1/embeddings?key=x"),
            Some("https://api.openai.com/health".into())
        );
        assert_eq!(health_url("not a url"), None);
    }
}
