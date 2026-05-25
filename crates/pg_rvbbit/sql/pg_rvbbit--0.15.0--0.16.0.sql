-- pg_rvbbit 0.15.0 -> 0.16.0
-- Loop 14: specialist + LLM usage rollups (views over receipts.sub_calls)
-- and a live /health probe UDF across every registered specialist.

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

CREATE FUNCTION rvbbit.specialist_health()
RETURNS TABLE(
    specialist      TEXT,
    transport       TEXT,
    endpoint        TEXT,
    reachable       BOOLEAN,
    latency_ms      INT,
    reported_model  TEXT,
    error           TEXT
)
VOLATILE
LANGUAGE c
AS '$libdir/pg_rvbbit', 'specialist_health_wrapper';
