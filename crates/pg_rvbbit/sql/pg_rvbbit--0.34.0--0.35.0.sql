-- pg_rvbbit 0.34.0 -> 0.35.0
-- MCP Phase 3 — typed wrappers + observability.
--
-- - rvbbit.generate_mcp_wrappers(server text) -> int : reads
--   rvbbit.mcp_tools and builds a typed SETOF-jsonb SQL function per
--   tool in a per-server schema. Idempotent.
-- - rvbbit.mcp_usage  : per-(server, tool) rollup over mcp_invocations.
-- - rvbbit.mcp_health : per-server health snapshot.

CREATE FUNCTION rvbbit.generate_mcp_wrappers(server text) RETURNS int4
    AS 'MODULE_PATHNAME', 'generate_mcp_wrappers_wrapper'
    LANGUAGE c VOLATILE;

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

CREATE OR REPLACE VIEW rvbbit.mcp_health AS
SELECT
    s.name,
    s.transport,
    coalesce(t.n_tools, 0)                  AS n_tools,
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
    SELECT server,
           max(invocation_at) FILTER (WHERE error IS NULL)     AS last_call_at,
           max(invocation_at) FILTER (WHERE error IS NOT NULL) AS last_error_at
    FROM rvbbit.mcp_invocations
    GROUP BY server
) i ON i.server = s.name;
