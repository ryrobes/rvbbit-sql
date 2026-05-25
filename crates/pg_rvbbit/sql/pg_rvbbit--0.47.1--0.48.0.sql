-- pg_rvbbit 0.47.1 -> 0.48.0
-- Cost ledger and delayed receipt queue.

CREATE TABLE IF NOT EXISTS rvbbit.cost_events (
    event_id               bigserial PRIMARY KEY,
    cost_request_id        uuid NOT NULL DEFAULT gen_random_uuid(),
    query_id               uuid,
    receipt_id             uuid,
    sub_call_index         int,
    source                 text NOT NULL,
    backend                text,
    transport              text,
    model                  text,
    tool                   text,
    provider_request_id    text,
    provider_generation_id text,
    upstream_id            text,
    status                 text NOT NULL,
    cost_source            text NOT NULL,
    tokens_in              int,
    tokens_out             int,
    native_tokens_in       int,
    native_tokens_out      int,
    reasoning_tokens       int,
    cached_tokens          int,
    cost_usd               numeric(18, 9),
    currency               text NOT NULL DEFAULT 'USD',
    raw                    jsonb,
    created_at             timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT cost_events_status_check
        CHECK (status IN ('pending', 'settled', 'estimated', 'free', 'uncosted', 'error')),
    CONSTRAINT cost_events_source_check
        CHECK (source IN ('operator', 'mcp', 'specialist', 'prewarm', 'manual'))
);

CREATE INDEX IF NOT EXISTS cost_events_request_idx
    ON rvbbit.cost_events (cost_request_id, event_id DESC);
CREATE INDEX IF NOT EXISTS cost_events_query_idx
    ON rvbbit.cost_events (query_id, event_id DESC);
CREATE INDEX IF NOT EXISTS cost_events_receipt_idx
    ON rvbbit.cost_events (receipt_id, sub_call_index);
CREATE INDEX IF NOT EXISTS cost_events_generation_idx
    ON rvbbit.cost_events (provider_generation_id)
    WHERE provider_generation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS rvbbit.cost_policies (
    target_kind      text NOT NULL,
    target_name      text NOT NULL,
    policy           text NOT NULL,
    model            text,
    fixed_cost_usd   numeric(18, 9),
    input_per_mtok   numeric(18, 9),
    output_per_mtok  numeric(18, 9),
    currency         text NOT NULL DEFAULT 'USD',
    notes            text,
    updated_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (target_kind, target_name),
    CONSTRAINT cost_policies_target_kind_check
        CHECK (target_kind IN ('backend', 'mcp_tool', 'model')),
    CONSTRAINT cost_policies_policy_check
        CHECK (policy IN ('free', 'fixed', 'model_rate', 'provider_settled', 'unknown'))
);

CREATE OR REPLACE FUNCTION rvbbit.set_cost_policy(
    target_kind text,
    target_name text,
    policy text,
    fixed_cost_usd numeric DEFAULT NULL,
    input_per_mtok numeric DEFAULT NULL,
    output_per_mtok numeric DEFAULT NULL,
    model text DEFAULT NULL,
    notes text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    row_doc jsonb;
BEGIN
    INSERT INTO rvbbit.cost_policies
        (target_kind, target_name, policy, fixed_cost_usd, input_per_mtok,
         output_per_mtok, model, notes, updated_at)
    VALUES
        (target_kind, target_name, policy, fixed_cost_usd, input_per_mtok,
         output_per_mtok, model, notes, clock_timestamp())
    ON CONFLICT (target_kind, target_name)
    DO UPDATE SET
        policy = EXCLUDED.policy,
        fixed_cost_usd = EXCLUDED.fixed_cost_usd,
        input_per_mtok = EXCLUDED.input_per_mtok,
        output_per_mtok = EXCLUDED.output_per_mtok,
        model = EXCLUDED.model,
        notes = EXCLUDED.notes,
        updated_at = clock_timestamp()
    RETURNING to_jsonb(rvbbit.cost_policies.*) INTO row_doc;
    RETURN row_doc;
END $$;

INSERT INTO rvbbit.cost_policies (target_kind, target_name, policy, notes)
VALUES
    ('backend', 'embed', 'free', 'Default local CPU embedding backend.'),
    ('backend', 'openrouter', 'provider_settled', 'OpenRouter costs settle by generation id.')
ON CONFLICT (target_kind, target_name) DO NOTHING;

CREATE OR REPLACE VIEW rvbbit.cost_latest AS
SELECT DISTINCT ON (cost_request_id)
    *
FROM rvbbit.cost_events
ORDER BY cost_request_id, event_id DESC;

CREATE OR REPLACE VIEW rvbbit.cost_pending AS
SELECT *
FROM rvbbit.cost_latest
WHERE status = 'pending';

CREATE OR REPLACE VIEW rvbbit.query_costs AS
SELECT
    query_id,
    count(*)                                                    AS costed_calls,
    count(*) FILTER (WHERE status = 'pending')                 AS pending_calls,
    count(*) FILTER (WHERE status = 'estimated')               AS estimated_calls,
    count(*) FILTER (WHERE status = 'uncosted')                AS uncosted_calls,
    count(*) FILTER (WHERE status = 'error')                   AS error_calls,
    coalesce(sum(cost_usd) FILTER (WHERE status <> 'error'), 0)::numeric(18,9)
                                                                AS total_cost_usd,
    min(created_at)                                             AS first_event_at,
    max(created_at)                                             AS last_event_at
FROM rvbbit.cost_latest
WHERE query_id IS NOT NULL
GROUP BY query_id;

CREATE OR REPLACE VIEW rvbbit.receipt_costs AS
SELECT
    receipt_id,
    count(*)                                                    AS costed_calls,
    count(*) FILTER (WHERE status = 'pending')                 AS pending_calls,
    count(*) FILTER (WHERE status = 'estimated')               AS estimated_calls,
    count(*) FILTER (WHERE status = 'uncosted')                AS uncosted_calls,
    count(*) FILTER (WHERE status = 'error')                   AS error_calls,
    coalesce(sum(cost_usd) FILTER (WHERE status <> 'error'), 0)::numeric(18,9)
                                                                AS total_cost_usd,
    min(created_at)                                             AS first_event_at,
    max(created_at)                                             AS last_event_at
FROM rvbbit.cost_latest
WHERE receipt_id IS NOT NULL
GROUP BY receipt_id;

CREATE OR REPLACE VIEW rvbbit.receipt_cost_audit AS
WITH receipt_subcalls AS (
    SELECT
        r.receipt_id,
        (sub.ord - 1)::int AS sub_call_index
    FROM rvbbit.receipts r
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE
            WHEN jsonb_typeof(coalesce(r.sub_calls, '[]'::jsonb)) = 'array'
            THEN coalesce(r.sub_calls, '[]'::jsonb)
            ELSE '[]'::jsonb
        END
    ) WITH ORDINALITY AS sub(value, ord)
    WHERE coalesce(sub.value->>'kind', '') IN ('llm', 'specialist', 'mcp')
),
receipt_rollup AS (
    SELECT
        r.receipt_id,
        r.operator,
        r.query_id,
        r.invocation_at,
        count(s.sub_call_index) AS chargeable_sub_calls
    FROM rvbbit.receipts r
    LEFT JOIN receipt_subcalls s ON s.receipt_id = r.receipt_id
    GROUP BY r.receipt_id, r.operator, r.query_id, r.invocation_at
),
cost_rollup AS (
    SELECT
        receipt_id,
        count(DISTINCT sub_call_index) FILTER (WHERE sub_call_index IS NOT NULL)
            AS cost_event_sub_calls,
        count(*) FILTER (WHERE status = 'pending')   AS pending_calls,
        count(*) FILTER (WHERE status = 'estimated') AS estimated_calls,
        count(*) FILTER (WHERE status = 'uncosted')  AS uncosted_calls,
        count(*) FILTER (WHERE status = 'error')     AS error_calls,
        min(created_at) FILTER (WHERE status = 'pending') AS oldest_pending_at,
        coalesce(sum(cost_usd) FILTER (WHERE status <> 'error'), 0)::numeric(18,9)
            AS total_cost_usd
    FROM rvbbit.cost_latest
    WHERE receipt_id IS NOT NULL
    GROUP BY receipt_id
)
SELECT
    r.receipt_id,
    r.operator,
    r.query_id,
    r.invocation_at,
    r.chargeable_sub_calls,
    coalesce(c.cost_event_sub_calls, 0) AS cost_event_sub_calls,
    greatest(r.chargeable_sub_calls - coalesce(c.cost_event_sub_calls, 0), 0)
        AS missing_cost_events,
    coalesce(c.pending_calls, 0) AS pending_calls,
    coalesce(c.estimated_calls, 0) AS estimated_calls,
    coalesce(c.uncosted_calls, 0) AS uncosted_calls,
    coalesce(c.error_calls, 0) AS error_calls,
    c.oldest_pending_at,
    CASE
        WHEN r.chargeable_sub_calls = 0 THEN 'no_chargeable_sub_calls'
        WHEN greatest(r.chargeable_sub_calls - coalesce(c.cost_event_sub_calls, 0), 0) > 0
            THEN 'missing_cost_events'
        WHEN coalesce(c.pending_calls, 0) > 0
             AND c.oldest_pending_at < clock_timestamp() - interval '15 minutes'
            THEN 'stale_pending'
        WHEN coalesce(c.pending_calls, 0) > 0 THEN 'pending'
        WHEN coalesce(c.uncosted_calls, 0) > 0 THEN 'uncosted'
        WHEN coalesce(c.error_calls, 0) > 0 THEN 'errors'
        ELSE 'ok'
    END AS audit_status,
    coalesce(c.total_cost_usd, 0)::numeric(18,9) AS total_cost_usd
FROM receipt_rollup r
LEFT JOIN cost_rollup c ON c.receipt_id = r.receipt_id;

CREATE OR REPLACE VIEW rvbbit.cost_audit_gaps AS
SELECT *
FROM rvbbit.receipt_cost_audit
WHERE audit_status NOT IN ('ok', 'no_chargeable_sub_calls');

CREATE OR REPLACE FUNCTION rvbbit.flush_receipt_queue(
    "limit" bigint DEFAULT 1000
) RETURNS bigint
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'flush_receipt_queue_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.receipt_queue_pending()
RETURNS bigint
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'receipt_queue_pending_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.reconcile_openrouter_costs(
    "limit" bigint DEFAULT 100
) RETURNS bigint
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'reconcile_openrouter_costs_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.backfill_cost_events_from_receipts(
    "limit" bigint DEFAULT 1000
) RETURNS bigint
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'backfill_cost_events_from_receipts_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.cost_audit_summary()
RETURNS jsonb
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'cost_audit_summary_wrapper';

CREATE OR REPLACE FUNCTION rvbbit.maintain_cost_audit(
    queue_limit bigint DEFAULT 10000,
    backfill_limit bigint DEFAULT 10000,
    reconcile_limit bigint DEFAULT 1000
) RETURNS jsonb
STRICT
LANGUAGE c
AS '$libdir/pg_rvbbit', 'maintain_cost_audit_wrapper';
