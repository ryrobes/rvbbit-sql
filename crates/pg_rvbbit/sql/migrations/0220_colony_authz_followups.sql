-- 0220_colony_authz_followups
--
-- Three follow-ups from the same review round that produced 0219.
--
-- 1) NOTIFY payload leaked request ids on an unscoped channel.
--    rvbbit.notify_peer_work() broadcast {backend_name, request_id} on
--    'rvbbit_peer_work'. Postgres NOTIFY channels have no ACL — ANY connected
--    role can LISTEN — so every request id in the database was harvestable by
--    anyone, which is precisely the input the (pre-0219) unauthenticated
--    poll_peer_response needed. 0219 closed the use; this closes the source.
--    Nothing consumes the id (the runner polls via claim_peer_request; the
--    channel name has no subscriber in either repo), so the payload drops to
--    backend_name — enough for a future wake signal, no per-request ids.
--
-- 2) Abandoned requests were never retired.
--    A runner that dies mid-claim (or a backend nobody is running) left rows
--    'pending'/'claimed' forever: queue depth and roster stats counted
--    in-flight work that could never resolve, and nothing in the schema ever
--    cleaned it (claim only touches 'pending'; detach only sweeps on full
--    deregistration).
--
--    NOT fixed inside poll_peer_response's timeout branch, which is where it
--    intuitively belongs: RAISE EXCEPTION aborts the function's own
--    subtransaction, so an UPDATE there is rolled back by the very raise that
--    follows it (verified empirically before writing this). The retirement
--    has to happen on a path that commits — hence a reaper called from
--    enqueue_peer_request, which is both a committing path and exactly when
--    the queue is in use. Also exposed standalone for pg_cron/manual use.
--
-- 3) _peer_may_serve relied on AND short-circuit for its role-exists guard.
--    Postgres does not guarantee left-to-right evaluation of AND, so a
--    backend whose sharer role was later dropped could raise "role ... does
--    not exist" instead of cleanly denying. CASE guarantees ordered
--    evaluation, and to_regrole() returns NULL rather than raising.

-- Supports both the reaper's sweep and claim_peer_request's hot path; partial,
-- so they stay small no matter how much completed history accumulates.
CREATE INDEX IF NOT EXISTS peer_capability_requests_pending_idx
    ON rvbbit.peer_capability_requests (backend_name, created_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS peer_capability_requests_open_idx
    ON rvbbit.peer_capability_requests (created_at)
    WHERE status IN ('pending', 'claimed');

CREATE OR REPLACE FUNCTION rvbbit.notify_peer_work() RETURNS trigger
LANGUAGE plpgsql AS $notify_fn$
BEGIN
    -- backend_name only: NOTIFY channels are unscoped, so anything in here is
    -- readable by every role on the database.
    PERFORM pg_notify('rvbbit_peer_work',
        jsonb_build_object('backend_name', NEW.backend_name)::text);
    RETURN NEW;
END;
$notify_fn$;

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
             OR CASE
                    WHEN to_regrole(b.shared_by) IS NULL THEN false
                    ELSE pg_has_role(session_user, b.shared_by, 'MEMBER')
                END
             OR pg_has_role(session_user, 'rvbbit_admin', 'MEMBER')
          )
    );
$may_serve$;

-- Retire requests no runner will ever finish. Default window is generous:
-- a legitimate call is bounded by the caller's own poll timeout (30s by
-- default, minutes at worst), so an hour-old open row means nobody is coming.
CREATE OR REPLACE FUNCTION rvbbit.reap_stale_peer_requests(p_stale interval DEFAULT '1 hour')
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
AS $reap_fn$
DECLARE
    v_n integer;
BEGIN
    WITH stale AS (
        UPDATE rvbbit.peer_capability_requests
           SET status = 'failed'
         WHERE status IN ('pending', 'claimed')
           AND created_at < now() - p_stale
        RETURNING request_id
    ), logged AS (
        INSERT INTO rvbbit.peer_capability_responses (request_id, response, error)
        SELECT request_id, NULL, 'abandoned: no runner completed this request'
        FROM stale
        ON CONFLICT (request_id) DO NOTHING
        RETURNING request_id
    )
    SELECT count(*) INTO v_n FROM stale;
    RETURN v_n;
END;
$reap_fn$;

CREATE OR REPLACE FUNCTION rvbbit.enqueue_peer_request(p_backend_name text, p_payload jsonb)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
AS $enqueue_fn$
DECLARE
    v_scope_role text;
    v_request_id uuid;
BEGIN
    SELECT scope_role INTO v_scope_role FROM rvbbit.peer_backends WHERE backend_name = p_backend_name;
    IF v_scope_role IS NULL THEN
        RAISE EXCEPTION 'enqueue_peer_request: no such peer backend %', p_backend_name;
    END IF;
    IF NOT pg_has_role(session_user, v_scope_role, 'member') THEN
        RAISE EXCEPTION 'enqueue_peer_request: % is not in scope % for backend %',
            session_user, v_scope_role, p_backend_name;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM rvbbit.peer_backends_live WHERE backend_name = p_backend_name) THEN
        RAISE EXCEPTION 'enqueue_peer_request: % has no live instance right now', p_backend_name;
    END IF;

    -- Opportunistic hygiene on a path that commits (see header note 2).
    -- Index-backed and almost always a no-op; failure here must never block a
    -- legitimate call.
    BEGIN
        PERFORM rvbbit.reap_stale_peer_requests();
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'enqueue_peer_request: reap skipped (%)', SQLERRM;
    END;

    INSERT INTO rvbbit.peer_capability_requests (backend_name, payload, requested_by)
    VALUES (p_backend_name, p_payload, session_user)
    RETURNING request_id INTO v_request_id;

    RETURN v_request_id;
END;
$enqueue_fn$;
