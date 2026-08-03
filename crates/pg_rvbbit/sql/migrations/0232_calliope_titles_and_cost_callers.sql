-- 0232: Calliope generated notebook names and caller-attributed Hermes costs.
--
-- Session names are allowed to start as provisional and become a concise
-- one-sentence summary after the first substantive turn.  A manual rename is
-- durable: the service changes title_source to 'manual' and never overwrites
-- it.  Hermes calls join the same receipt/cost ledger as semantic operators,
-- with caller carrying the authenticated Calliope owner.

ALTER TABLE rvbbit.calliope_sessions
    ADD COLUMN IF NOT EXISTS title_source text NOT NULL DEFAULT 'system';
ALTER TABLE rvbbit.calliope_sessions
    ADD COLUMN IF NOT EXISTS title_generated_at timestamptz;

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'calliope_sessions_title_source_check'
          AND conrelid = 'rvbbit.calliope_sessions'::regclass
    ) THEN
        ALTER TABLE rvbbit.calliope_sessions
            ADD CONSTRAINT calliope_sessions_title_source_check
            CHECK (title_source IN ('provisional','generated','manual','system'));
    END IF;
END
$do$;

ALTER TABLE rvbbit.receipts
    ADD COLUMN IF NOT EXISTS caller text;
ALTER TABLE rvbbit.cost_events
    ADD COLUMN IF NOT EXISTS caller text;

CREATE INDEX IF NOT EXISTS receipts_caller_time_idx
    ON rvbbit.receipts (caller, invocation_at DESC)
    WHERE caller IS NOT NULL;
CREATE INDEX IF NOT EXISTS cost_events_caller_time_idx
    ON rvbbit.cost_events (caller, created_at DESC)
    WHERE caller IS NOT NULL;

ALTER TABLE rvbbit.cost_events
    DROP CONSTRAINT IF EXISTS cost_events_source_check;
ALTER TABLE rvbbit.cost_events
    ADD CONSTRAINT cost_events_source_check
    CHECK (source IN ('operator','mcp','specialist','prewarm','manual','hermes'));

CREATE OR REPLACE VIEW rvbbit.cost_latest AS
SELECT DISTINCT ON (cost_request_id) *
FROM rvbbit.cost_events
ORDER BY cost_request_id,event_id DESC;

CREATE OR REPLACE VIEW rvbbit.caller_costs AS
SELECT
    caller,
    count(*) AS costed_calls,
    count(*) FILTER (WHERE status = 'pending') AS pending_calls,
    count(*) FILTER (WHERE status = 'estimated') AS estimated_calls,
    count(*) FILTER (WHERE status = 'uncosted') AS uncosted_calls,
    count(*) FILTER (WHERE status = 'error') AS error_calls,
    coalesce(sum(cost_usd) FILTER (WHERE status <> 'error'), 0)::numeric(18,9)
        AS total_cost_usd,
    min(created_at) AS first_event_at,
    max(created_at) AS last_event_at
FROM rvbbit.cost_latest
WHERE caller IS NOT NULL
GROUP BY caller;

COMMENT ON COLUMN rvbbit.calliope_sessions.title_source IS
    'provisional may be LLM-named once; generated is the resulting name; manual and system are never auto-overwritten.';
COMMENT ON COLUMN rvbbit.receipts.caller IS
    'Authenticated human or service identity that initiated the receipt, when known.';
COMMENT ON COLUMN rvbbit.cost_events.caller IS
    'Authenticated human or service identity to use for cost attribution and rollups.';
COMMENT ON VIEW rvbbit.caller_costs IS
    'Latest semantic, MCP, and Hermes cost state grouped by authenticated caller.';
