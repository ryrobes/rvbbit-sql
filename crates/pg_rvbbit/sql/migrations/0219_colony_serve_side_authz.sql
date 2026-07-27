-- 0219_colony_serve_side_authz
--
-- Two defects found in an adversarial review of the shipped Colony
-- surface (4.1.6), both verified live on bench before and after this fix.
--
-- 1) SERVE-SIDE AUTHORIZATION WAS MISSING (critical).
--    The scope check lived only on enqueue_peer_request — the "who may USE
--    this capability" side. claim_peer_request / complete_peer_request /
--    peer_heartbeat had NO authorization at all, and being SECURITY DEFINER
--    they bypass table grants. So ANY role that can execute them (public
--    execute on a SECURITY DEFINER function) could, for a backend it has no
--    scope membership in:
--      - claim pending requests and read their payloads   → eavesdropping
--        on every prompt sent to any shared capability (no request_id
--        needed; it's a pull),
--      - complete them with attacker-authored content     → the caller
--        receives forged output believing it came from the shared model,
--      - starve the legitimate runner                     → denial of service.
--    Reproduced end to end as a non-member role, including a forged answer
--    delivered back to the real caller.
--
--    Fix: serving a backend now requires being its SHARER — the identity
--    recorded in peer_backends.shared_by at registration (which is the same
--    session_user the sharer's runner claims with; burrow's SET LOCAL ROLE
--    moves current_user, not session_user, so this holds in both modes) —
--    or a member of that role, or rvbbit_admin. Superusers still pass, which
--    is correct: the default deployment's runner connects as one.
--
-- 2) PAUSE STOPPED WORKING (regression introduced in 0218).
--    0213 added `WHERE b.enabled` to peer_backends_live; 0218's rebuild of
--    that view (needed so b.* picked up client_host) dropped the predicate.
--    enqueue_peer_request gates liveness on that view, so a PAUSED backend
--    still accepted work — set_peer_backend_enabled(name,false) hid the
--    backend from the roster while silently continuing to serve. Restored.
--
-- poll_peer_response also gained the missing ownership check: it answered
-- for any request_id presented, so a leaked/guessed id disclosed another
-- caller's answer. requested_by is already recorded at enqueue, so this
-- needs no schema change.

-- Restore the pause predicate (DROP+CREATE: b.*-shaped view, see 0218).
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

-- "May this session act as the runner for this backend?"
--
-- Compares against session_user ONLY, never current_user: inside a
-- SECURITY DEFINER function current_user is the function's OWNER
-- (postgres), so any check including it passes for everybody — the first
-- cut of this fix did exactly that and the attack replay caught it.
-- session_user is also the right identity on the merits: it is what
-- register_peer_backend records in shared_by, and burrow's SET LOCAL ROLE
-- moves current_user while leaving session_user pinned to the connection's
-- account, so register-time and claim-time identities agree in both modes.
--
-- shared_by is free text (a role that may since have been dropped), so the
-- pg_has_role call is guarded on the role still existing — pg_has_role
-- raises on an unknown name rather than returning false.
CREATE OR REPLACE FUNCTION rvbbit._peer_may_serve(p_backend_name text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
AS $may_serve$
    SELECT EXISTS (
        SELECT 1
        FROM rvbbit.peer_backends b
        WHERE b.backend_name = p_backend_name
          AND (
                b.shared_by = session_user
             OR (EXISTS (SELECT 1 FROM pg_roles WHERE rolname = b.shared_by)
                 AND pg_has_role(session_user, b.shared_by, 'MEMBER'))
             OR pg_has_role(session_user, 'rvbbit_admin', 'MEMBER')
          )
    );
$may_serve$;

CREATE OR REPLACE FUNCTION rvbbit.claim_peer_request(p_backend_name text, p_instance_id uuid)
RETURNS TABLE(request_id uuid, payload jsonb)
LANGUAGE plpgsql
SECURITY DEFINER
AS $claim_fn$
DECLARE
    r record;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM rvbbit.peer_backends WHERE backend_name = p_backend_name) THEN
        RAISE EXCEPTION 'claim_peer_request: no such peer backend %', p_backend_name;
    END IF;
    IF NOT rvbbit._peer_may_serve(p_backend_name) THEN
        RAISE EXCEPTION 'claim_peer_request: % may not serve backend % (only its sharer runs its runner)',
            session_user, p_backend_name;
    END IF;

    SELECT pcr.request_id, pcr.payload INTO r
    FROM rvbbit.peer_capability_requests pcr
    WHERE pcr.backend_name = p_backend_name AND pcr.status = 'pending'
    ORDER BY pcr.created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1;

    IF r.request_id IS NULL THEN
        RETURN;
    END IF;

    UPDATE rvbbit.peer_capability_requests
       SET status = 'claimed', claimed_by = p_instance_id
     WHERE peer_capability_requests.request_id = r.request_id;

    request_id := r.request_id;
    payload := r.payload;
    RETURN NEXT;
END;
$claim_fn$;

CREATE OR REPLACE FUNCTION rvbbit.complete_peer_request(p_request_id uuid, p_response jsonb DEFAULT NULL::jsonb, p_error text DEFAULT NULL::text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $complete_fn$
DECLARE
    v_backend text;
BEGIN
    SELECT backend_name INTO v_backend
    FROM rvbbit.peer_capability_requests WHERE request_id = p_request_id;
    IF v_backend IS NULL THEN
        RAISE EXCEPTION 'complete_peer_request: no such request %', p_request_id;
    END IF;
    IF NOT rvbbit._peer_may_serve(v_backend) THEN
        RAISE EXCEPTION 'complete_peer_request: % may not serve backend % — refusing to write an answer',
            session_user, v_backend;
    END IF;

    INSERT INTO rvbbit.peer_capability_responses (request_id, response, error)
    VALUES (p_request_id, p_response, p_error)
    ON CONFLICT (request_id) DO UPDATE
       SET response = EXCLUDED.response, error = EXCLUDED.error, completed_at = now();
    UPDATE rvbbit.peer_capability_requests
       SET status = CASE WHEN p_error IS NULL THEN 'done' ELSE 'failed' END
     WHERE request_id = p_request_id;
END;
$complete_fn$;

CREATE OR REPLACE FUNCTION rvbbit.peer_heartbeat(p_backend_name text, p_instance_id uuid, p_queue_depth integer DEFAULT 0)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $heartbeat_fn$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM rvbbit.peer_backends WHERE backend_name = p_backend_name) THEN
        RAISE EXCEPTION 'peer_heartbeat: no such peer backend %', p_backend_name;
    END IF;
    -- Without this, anyone could keep a dead backend looking live (callers
    -- then enqueue into a queue nothing is draining) or fake presence for a
    -- backend they have nothing to do with.
    IF NOT rvbbit._peer_may_serve(p_backend_name) THEN
        RAISE EXCEPTION 'peer_heartbeat: % may not serve backend %', session_user, p_backend_name;
    END IF;

    INSERT INTO rvbbit.peer_backend_presence (backend_name, instance_id, last_heartbeat_at, queue_depth)
    VALUES (p_backend_name, p_instance_id, clock_timestamp(), p_queue_depth)
    ON CONFLICT (backend_name, instance_id) DO UPDATE
       SET last_heartbeat_at = clock_timestamp(), queue_depth = EXCLUDED.queue_depth;
END;
$heartbeat_fn$;

-- The requester (or an admin) may read an answer; a leaked request_id is no
-- longer sufficient on its own.
CREATE OR REPLACE FUNCTION rvbbit.poll_peer_response(p_request_id uuid, p_timeout_ms integer DEFAULT 30000)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
AS $poll_fn$
DECLARE
    v_response jsonb;
    v_error    text;
    v_status   text;
    v_owner    text;
    v_deadline timestamptz := clock_timestamp() + make_interval(secs => p_timeout_ms / 1000.0);
BEGIN
    SELECT requested_by INTO v_owner
    FROM rvbbit.peer_capability_requests WHERE request_id = p_request_id;
    IF v_owner IS NULL THEN
        RAISE EXCEPTION 'poll_peer_response: no such request %', p_request_id;
    END IF;
    -- session_user only — current_user is the owner here (see _peer_may_serve).
    IF NOT (v_owner = session_user
            OR pg_has_role(session_user, 'rvbbit_admin', 'MEMBER')) THEN
        RAISE EXCEPTION 'poll_peer_response: % did not make request %', session_user, p_request_id;
    END IF;

    LOOP
        SELECT r.response, r.error, q.status INTO v_response, v_error, v_status
        FROM rvbbit.peer_capability_requests q
        LEFT JOIN rvbbit.peer_capability_responses r ON r.request_id = q.request_id
        WHERE q.request_id = p_request_id;

        EXIT WHEN v_status IN ('done', 'failed');
        IF clock_timestamp() >= v_deadline THEN
            RAISE EXCEPTION 'poll_peer_response: % timed out after %ms', p_request_id, p_timeout_ms;
        END IF;
        PERFORM pg_sleep(0.05);
    END LOOP;

    IF v_status = 'failed' THEN
        RAISE EXCEPTION 'poll_peer_response: peer error: %', coalesce(v_error, 'unknown');
    END IF;
    RETURN v_response;
END;
$poll_fn$;
