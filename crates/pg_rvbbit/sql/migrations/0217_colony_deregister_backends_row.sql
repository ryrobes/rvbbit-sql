-- 0217_colony_deregister_backends_row
--
-- Colony registration is a PAIR of rows under one name — the lens register
-- route calls register_backend (transport 'peer_queue', the generic catalog
-- entry every transport shares) AND register_peer_backend (Colony's own
-- scope/presence table). Detach only cleaned the peer side, orphaning the
-- rvbbit.backends row forever: every long-detached backend kept haunting the
-- Specialists window and the Fleet topology as a dead peer_queue entry.
-- deregister_peer_backend now removes its paired catalog row too, and this
-- migration sweeps the orphans that already accumulated.

CREATE OR REPLACE FUNCTION rvbbit.deregister_peer_backend(p_backend_name text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $deregister_fn$
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
    -- The paired generic-catalog row (the lens register route creates both
    -- together under the same name). Guarded on transport so a
    -- coincidentally same-named non-peer backend is never touched.
    DELETE FROM rvbbit.backends WHERE name = p_backend_name AND transport = 'peer_queue';
    BEGIN
        PERFORM rvbbit.capability_crawl();
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'deregister_peer_backend: capability_crawl failed (%) — run rvbbit.capability_crawl() manually', SQLERRM;
    END;
END;
$deregister_fn$;

-- One-time sweep of orphans created before this fix.
DELETE FROM rvbbit.backends b
 WHERE b.transport = 'peer_queue'
   AND NOT EXISTS (SELECT 1 FROM rvbbit.peer_backends p WHERE p.backend_name = b.name);
