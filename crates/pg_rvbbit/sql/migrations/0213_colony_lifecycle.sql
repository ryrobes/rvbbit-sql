-- 0213_colony_lifecycle
--
-- Colony (docs/PEER_CAPABILITIES_PLAN.md) P3 needs pause/resume + a hard
-- "Stop sharing" detach for the sharer's own "My Shared Capabilities"
-- panel — 0212 shipped register/claim/complete but no lifecycle beyond
-- that. `enabled` gates both request-visibility (peer_backends_live) and
-- new enqueues, so a paused backend looks correctly "offline" everywhere
-- at once rather than needing every reader to remember a second flag.

ALTER TABLE rvbbit.peer_backends ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT true;

-- DROP + CREATE, not OR REPLACE: adding `enabled` shifts b.*'s column
-- positions, and CREATE OR REPLACE VIEW refuses to reorder existing columns.
DROP VIEW IF EXISTS rvbbit.peer_backends_live;
CREATE VIEW rvbbit.peer_backends_live AS
SELECT b.*,
       p.instance_count,
       p.min_queue_depth
FROM rvbbit.peer_backends b
JOIN LATERAL (
    SELECT count(*) AS instance_count, min(queue_depth) AS min_queue_depth
    FROM rvbbit.peer_backend_presence pp
    WHERE pp.backend_name = b.backend_name
      AND pp.last_heartbeat_at > clock_timestamp() - interval '15 seconds'
) p ON p.instance_count > 0
WHERE b.enabled;

CREATE OR REPLACE FUNCTION rvbbit.set_peer_backend_enabled(p_backend_name text, p_enabled boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    UPDATE rvbbit.peer_backends SET enabled = p_enabled WHERE backend_name = p_backend_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'set_peer_backend_enabled: no such peer backend %', p_backend_name;
    END IF;
END;
$$;

-- Hard detach: removes the share entirely. Any still-pending requests are
-- failed out rather than left to poll forever against a backend that no
-- longer exists (poll_peer_response would otherwise time out slowly,
-- looking like a hang rather than "the sharer detached").
CREATE OR REPLACE FUNCTION rvbbit.deregister_peer_backend(p_backend_name text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_ids uuid[];
BEGIN
    WITH updated AS (
        UPDATE rvbbit.peer_capability_requests
           SET status = 'failed'
         WHERE backend_name = p_backend_name AND status IN ('pending', 'claimed')
         RETURNING request_id
    )
    SELECT array_agg(request_id) INTO v_ids FROM updated;

    IF v_ids IS NOT NULL THEN
        INSERT INTO rvbbit.peer_capability_responses (request_id, response, error)
        SELECT unnest(v_ids), NULL, 'peer backend was detached by its sharer'
        ON CONFLICT (request_id) DO UPDATE SET error = EXCLUDED.error, completed_at = now();
    END IF;

    DELETE FROM rvbbit.peer_backend_presence WHERE backend_name = p_backend_name;
    DELETE FROM rvbbit.peer_backends WHERE backend_name = p_backend_name;
END;
$$;
