-- 0218_colony_client_host
--
-- A colony member is a MACHINE, not just a role: the Fleet topology wants
-- to name peer nodes by the client host that's actually answering
-- ("rvbbit-dev45"), with the sharing role as the secondary identity
-- ("@postgres") — and one user sharing from two laptops is genuinely two
-- nodes. Registration now records the sharer's hostname (best-effort,
-- self-reported by the lens client via os.hostname(); NULL from older
-- clients, in which case the UI falls back to @role).
--
-- The old 6-arg register_peer_backend must be DROPPED, not just replaced:
-- adding a defaulted 7th param creates a second overload, and every
-- existing ≤6-arg call site would then match both → "function is not
-- unique" (see the psycopg overload trap — same failure mode, house
-- precedent is drop-the-stale-overload).

ALTER TABLE rvbbit.peer_backends ADD COLUMN IF NOT EXISTS client_host text;

-- Rebuild the presence view so b.* picks up client_host. DROP+CREATE,
-- never OR REPLACE — the new column shifts every position after it
-- (0135 doctrine).
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
) p ON p.instance_count > 0;

DROP FUNCTION IF EXISTS rvbbit.register_peer_backend(text, text, text, text, text, text);

CREATE OR REPLACE FUNCTION rvbbit.register_peer_backend(p_backend_name text, p_kind text, p_scope_role text, p_template text DEFAULT NULL::text, p_description text DEFAULT NULL::text, p_model_digest text DEFAULT NULL::text, p_client_host text DEFAULT NULL::text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $register_fn$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = p_scope_role) THEN
        RAISE EXCEPTION 'register_peer_backend: role % does not exist', p_scope_role;
    END IF;
    INSERT INTO rvbbit.peer_backends (backend_name, kind, template, model_digest, scope_role, shared_by, description, client_host)
    VALUES (p_backend_name, p_kind, p_template, p_model_digest, p_scope_role, session_user, p_description, p_client_host)
    ON CONFLICT (backend_name) DO UPDATE
       SET kind = EXCLUDED.kind, template = EXCLUDED.template, model_digest = EXCLUDED.model_digest,
           scope_role = EXCLUDED.scope_role, description = EXCLUDED.description,
           -- newest registration wins; an old client that doesn't report a
           -- host keeps whatever was known rather than clobbering to NULL
           client_host = coalesce(EXCLUDED.client_host, rvbbit.peer_backends.client_host);
    BEGIN
        PERFORM rvbbit.capability_crawl();
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'register_peer_backend: capability_crawl failed (%) — run rvbbit.capability_crawl() manually', SQLERRM;
    END;
END;
$register_fn$;
