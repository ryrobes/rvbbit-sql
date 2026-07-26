-- 0212_colony_peer_capabilities
--
-- "Colony" (docs/PEER_CAPABILITIES_PLAN.md): a DataRabbit client running
-- locally can attach an LLM, a hosted ML model, or a locally-run MCP
-- server to this database, callable by anyone in scope, for as long as
-- the client stays up. Transport is a Postgres-native queue, not a relay
-- — request-shaped, matching how every clover_* operator already works
-- (local Warren or remote API alike, none of them stream today either).
--
-- Scope is a real PG role (burrow mode already uses roles as identity —
-- pg_has_role() is the entire enforcement mechanism, no new permission
-- system). The caller side is enqueue_peer_request + poll_peer_response —
-- two plpgsql functions, not one (see the comment above
-- request_peer_capability's removal below for why) — directly testable
-- from psql. The Rust Transport impl (peer_queue, specialists/peer_queue.rs)
-- is a thin wrapper that opens its OWN client-side postgres::Client
-- connection (route_log.rs's proven pattern) and calls the same functions —
-- SPI is illegal on a prewarm pool thread, so predict() must never touch SPI.

CREATE TABLE IF NOT EXISTS rvbbit.peer_backends (
    backend_name   text PRIMARY KEY,
    kind           text NOT NULL CHECK (kind IN ('llm','embedding','specialist','mcp')),
    template       text,               -- 'openai_chat' | 'ollama_native' | 'gradio' | ...
    model_digest   text,               -- fingerprint for cluster-identity matching, when available
    scope_role     text NOT NULL,      -- a real PG role; caller must be pg_has_role(.., 'member')
    shared_by      text NOT NULL DEFAULT session_user,
    description    text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rvbbit.peer_backend_presence (
    backend_name      text NOT NULL REFERENCES rvbbit.peer_backends(backend_name) ON DELETE CASCADE,
    instance_id       uuid NOT NULL,
    last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
    queue_depth       int NOT NULL DEFAULT 0,
    PRIMARY KEY (backend_name, instance_id)
);

CREATE TABLE IF NOT EXISTS rvbbit.peer_capability_requests (
    request_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    backend_name text NOT NULL REFERENCES rvbbit.peer_backends(backend_name),
    payload      jsonb NOT NULL,
    requested_by text NOT NULL,
    status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','claimed','done','failed')),
    claimed_by   uuid,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rvbbit.peer_capability_responses (
    request_id   uuid PRIMARY KEY REFERENCES rvbbit.peer_capability_requests(request_id),
    response     jsonb,
    error        text,
    completed_at timestamptz NOT NULL DEFAULT now()
);

-- NOTIFY is a doorbell, never the delivery truck — payload is the
-- (backend_name, request_id) pair only, never the actual prompt/response.
-- One shared channel; listeners filter by backend_name client-side, so a
-- runner never has to re-subscribe when a new backend gets shared.
CREATE OR REPLACE FUNCTION rvbbit.notify_peer_work() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('rvbbit_peer_work',
        jsonb_build_object('backend_name', NEW.backend_name, 'request_id', NEW.request_id)::text);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS rvbbit_notify_peer_work ON rvbbit.peer_capability_requests;
CREATE TRIGGER rvbbit_notify_peer_work
    AFTER INSERT ON rvbbit.peer_capability_requests
    FOR EACH ROW EXECUTE FUNCTION rvbbit.notify_peer_work();

-- SECURITY DEFINER throughout this file: callers are ordinary burrow-scoped
-- roles with no direct grants on these tables (same shape as burrow_enroll,
-- migration 0203) — each function's own checks (role existence, pg_has_role
-- scope, backend liveness) ARE the authorization boundary, not table ACLs.
CREATE OR REPLACE FUNCTION rvbbit.register_peer_backend(
    p_backend_name text,
    p_kind         text,
    p_scope_role   text,
    p_template     text DEFAULT NULL,
    p_description  text DEFAULT NULL,
    p_model_digest text DEFAULT NULL
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = p_scope_role) THEN
        RAISE EXCEPTION 'register_peer_backend: role % does not exist', p_scope_role;
    END IF;
    INSERT INTO rvbbit.peer_backends (backend_name, kind, template, model_digest, scope_role, shared_by, description)
    VALUES (p_backend_name, p_kind, p_template, p_model_digest, p_scope_role, session_user, p_description)
    ON CONFLICT (backend_name) DO UPDATE
       SET kind = EXCLUDED.kind, template = EXCLUDED.template, model_digest = EXCLUDED.model_digest,
           scope_role = EXCLUDED.scope_role, description = EXCLUDED.description;
END;
$$;

-- Runner side: claim up to one pending request for this backend.
-- FOR UPDATE SKIP LOCKED is the whole clustering story — N peer instances
-- registered under one backend_name each running this get exactly-once
-- delivery for free, no coordination needed beyond the row lock.
CREATE OR REPLACE FUNCTION rvbbit.claim_peer_request(p_backend_name text, p_instance_id uuid)
RETURNS TABLE(request_id uuid, payload jsonb) LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    r record;
BEGIN
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
$$;

CREATE OR REPLACE FUNCTION rvbbit.complete_peer_request(
    p_request_id uuid, p_response jsonb DEFAULT NULL, p_error text DEFAULT NULL
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rvbbit.peer_capability_responses (request_id, response, error)
    VALUES (p_request_id, p_response, p_error)
    ON CONFLICT (request_id) DO UPDATE
       SET response = EXCLUDED.response, error = EXCLUDED.error, completed_at = now();
    UPDATE rvbbit.peer_capability_requests
       SET status = CASE WHEN p_error IS NULL THEN 'done' ELSE 'failed' END
     WHERE request_id = p_request_id;
END;
$$;

CREATE OR REPLACE FUNCTION rvbbit.peer_heartbeat(p_backend_name text, p_instance_id uuid, p_queue_depth int DEFAULT 0)
RETURNS void LANGUAGE sql SECURITY DEFINER AS $$
    INSERT INTO rvbbit.peer_backend_presence (backend_name, instance_id, last_heartbeat_at, queue_depth)
    VALUES (p_backend_name, p_instance_id, clock_timestamp(), p_queue_depth)
    ON CONFLICT (backend_name, instance_id) DO UPDATE
       SET last_heartbeat_at = clock_timestamp(), queue_depth = EXCLUDED.queue_depth;
$$;

-- Presence view: a backend is "live" iff at least one instance has
-- heartbeat within the last 15s — a timestamp check, not raw socket
-- liveness (sleep/wake and flaky wifi lie about TCP state; this doesn't).
CREATE OR REPLACE VIEW rvbbit.peer_backends_live AS
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

-- Caller side is deliberately TWO functions, not one. A single plpgsql call
-- that both INSERTs and then polls would hold that INSERT uncommitted for
-- the whole wait — Postgres runs one top-level statement as one implicit
-- transaction, so the runner's session (a different backend) could never
-- see the pending row until the caller's own function returned, which it
-- never would, because it was waiting on the runner. Splitting into two
-- top-level statements lets the enqueue commit (autocommit) before polling
-- starts, so the runner's claim_peer_request actually sees it. Confirmed
-- live: the single-function version deadlocked this exact way in testing.
CREATE OR REPLACE FUNCTION rvbbit.enqueue_peer_request(p_backend_name text, p_payload jsonb)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER AS $$
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

    INSERT INTO rvbbit.peer_capability_requests (backend_name, payload, requested_by)
    VALUES (p_backend_name, p_payload, session_user)
    RETURNING request_id INTO v_request_id;

    RETURN v_request_id;
END;
$$;

-- Bounded by p_timeout_ms (default GUC, same shape as
-- rvbbit.duck_backend_timeout_s) — a peer that never claims/answers
-- raises a clear timeout error rather than hanging the caller forever.
-- Safe to call as its own top-level statement even mid-wait: each loop
-- iteration is a fresh SELECT under read-committed, so it sees rows other
-- backends commit while this one is running.
CREATE OR REPLACE FUNCTION rvbbit.poll_peer_response(p_request_id uuid, p_timeout_ms int DEFAULT 30000)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_response jsonb;
    v_error    text;
    v_status   text;
    v_deadline timestamptz := clock_timestamp() + make_interval(secs => p_timeout_ms / 1000.0);
BEGIN
    LOOP
        SELECT r.response, r.error, q.status INTO v_response, v_error, v_status
        FROM rvbbit.peer_capability_requests q
        LEFT JOIN rvbbit.peer_capability_responses r ON r.request_id = q.request_id
        WHERE q.request_id = p_request_id;

        IF v_status IS NULL THEN
            RAISE EXCEPTION 'poll_peer_response: no such request %', p_request_id;
        END IF;
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
$$;

-- Deliberately no combined request_peer_capability(): wrapping enqueue+poll
-- back into one call (plpgsql OR sql-language — both execute inline within
-- the caller's single top-level statement) reintroduces the exact same
-- uncommitted-INSERT deadlock. Callers MUST issue enqueue_peer_request and
-- poll_peer_response as two separate statements over one connection.

-- A Colony-backed specialist/LLM is "just another transport" on the same
-- rvbbit.backends registry every other provider uses (register_backend
-- under the same name as register_peer_backend's backend_name).
ALTER TABLE rvbbit.backends DROP CONSTRAINT IF EXISTS backends_transport_check;
ALTER TABLE rvbbit.backends ADD CONSTRAINT backends_transport_check
    CHECK (transport IN ('rvbbit', 'gradio', 'openai', 'local_embed', 'stub',
                         'openai_chat', 'anthropic', 'gemini', 'peer_queue'));

-- Wrapper DDL for a same-version .so addition (0044/0135/0139/0193 precedent).
CREATE OR REPLACE FUNCTION rvbbit.call_specialist(
    specialist_name text,
    input jsonb
) RETURNS jsonb LANGUAGE c VOLATILE AS '$libdir/pg_rvbbit', 'call_specialist_wrapper';

COMMENT ON FUNCTION rvbbit.call_specialist(text, jsonb) IS
    'Ad-hoc single-call test of any registered backend (any transport), bypassing operators/flows.';

-- Not Colony-specific: found while proving peer_queue's real-caller identity
-- fix end-to-end (a burrow-enrolled non-superuser role calling ANY
-- specialist needs these — GRANT ... ON ALL TABLES predates backends_gen
-- and doesn't cover sequences, and rvbbit_users never got either grant).
-- Without it, load_spec_from_spi's own SPI read fails for every ordinary
-- burrow user, on every transport, not just peer_queue.
GRANT SELECT ON rvbbit.backends_gen TO rvbbit_users;
GRANT SELECT ON rvbbit.warren_backend_status TO rvbbit_users;
