-- 0149_backend_health_llm_usage.sql
-- backend_health only counted SPECIALIST sub-calls (specialist_usage joins
-- on sub_calls[kind=specialist].model = backends.name), so LLM-transport
-- backends (openai_chat — e.g. the hosted hutch_llm) sat at n_calls=0
-- forever: the Capabilities card never earned its REGISTERED·USED state or
-- call stats even while hutch_ask() billed real tokens through it. Found
-- during the Gemma Lanes quiet-launch poke, 2026-07-15.
--
-- LLM sub-calls carry the backend name in value->>'backend' (kind='llm');
-- specialist sub-calls carry it in value->>'model'. Count both, per backend.
-- Column list/types must match the existing view exactly (CREATE OR REPLACE).

CREATE OR REPLACE VIEW rvbbit.backend_health AS
WITH expanded AS (
    SELECT
        CASE WHEN sub.value ->> 'kind' = 'llm'
             THEN sub.value ->> 'backend'
             ELSE sub.value ->> 'model'
        END                                        AS backend_name,
        (sub.value ->> 'latency_ms')::integer      AS latency_ms,
        sub.value ->> 'error'                      AS error,
        r.invocation_at
    FROM rvbbit.receipts r,
         LATERAL jsonb_array_elements(r.sub_calls) sub(value)
    WHERE sub.value ->> 'kind' IN ('specialist', 'llm')
),
usage AS (
    SELECT
        backend_name,
        count(*)                                          AS n_calls,
        count(*) FILTER (WHERE error IS NOT NULL)         AS n_errors,
        coalesce(round(avg(latency_ms))::integer, 0)      AS avg_latency_ms,
        round(percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms))::integer AS p50_latency_ms,
        round(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms))::integer AS p95_latency_ms,
        min(invocation_at)                                AS first_call_at,
        max(invocation_at)                                AS last_call_at
    FROM expanded
    WHERE backend_name IS NOT NULL
    GROUP BY backend_name
)
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
    coalesce(u.n_calls, 0::bigint)  AS n_calls,
    coalesce(u.n_errors, 0::bigint) AS n_errors,
    u.avg_latency_ms,
    u.p50_latency_ms,
    u.p95_latency_ms,
    u.first_call_at,
    u.last_call_at,
    b.created_at
FROM rvbbit.backends b
LEFT JOIN usage u ON u.backend_name = b.name;
