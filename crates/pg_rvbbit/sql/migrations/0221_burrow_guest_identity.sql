-- 0221: Burrow federated identity — one door for MCP and DataRabbit
--
-- Google (or any future IdP) proves WHO you are; Postgres decides WHAT you
-- may touch. Between the two sits a resolution step that turns a verified
-- email into a PG role. Three outcomes, and the third is the interesting one:
--
--   1. an explicit rvbbit.identity_map row              -> that role
--   2. the email IS a role (cloud-IAM convention:        -> that role
--      Azure Entra / Cloud SQL IAM name roles by email)
--   3. nothing matches                                   -> rvbbit_guest
--
-- Case 3 is a state neither system can express alone: "OAuth says yes, the
-- database says I can't map this user." It is not an error — it is somebody
-- who needs provisioning. We record it (rvbbit.identity_unmapped) so the DBA
-- has a queue to work from instead of a support ticket.
--
-- NON-BURROW INSTALLS ARE UNAFFECTED. Everything here is inert unless the
-- warehouse runs with WAREHOUSE_AUTH=pg: the resolver is only ever called on
-- that path, and rvbbit_guest is created NOLOGIN WITH NO GRANTS AT ALL. It
-- cannot be connected as, and it can read nothing. Following the rule the
-- rest of these migrations already keep: top-level GRANTs touch only
-- rvbbit-owned objects; anything reaching into a customer's schema happens
-- inside a function a DBA calls deliberately (see burrow_grant_guest below).

-- ── the guest role ──────────────────────────────────────────────────────────
-- NOLOGIN is not a limitation here: SET ROLE into a NOLOGIN role works fine
-- (verified), and guest is only ever reached by the service account doing
-- SET ROLE — never by connecting as it. Keeping NOLOGIN means it can never
-- become a real account if someone later sets a password or the box uses
-- `trust` in pg_hba, and burrow_enroll() (which requires rolcanlogin) will
-- refuse to enroll it as though it were a person.
DO $guest$
BEGIN
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rvbbit_guest') THEN
            CREATE ROLE rvbbit_guest NOLOGIN;
        END IF;
        COMMENT ON ROLE rvbbit_guest IS
            'Burrow: authenticated by the IdP but not mapped to a PG account. Deliberately holds NO grants — surfaces show a request-access state rather than a broken one. Grant into it (or use rvbbit.burrow_grant_guest) only if you want a real read-only tier. docs/BURROW_PLAN.md';
        -- the service account must be a member to SET ROLE into it
        EXECUTE format('GRANT rvbbit_guest TO %I', session_user);
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'burrow: skipping rvbbit_guest creation (needs CREATEROLE) — create it manually to use Burrow federated login';
    END;
END $guest$;

-- ── explicit mapping ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rvbbit.identity_map (
    identity   text PRIMARY KEY,          -- verified email from the IdP, lowercased
    role_name  text NOT NULL,             -- the PG role to execute as
    enabled    boolean NOT NULL DEFAULT true,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text DEFAULT session_user
);
COMMENT ON TABLE rvbbit.identity_map IS
    'Burrow: IdP identity -> PG role. The escape hatch when the role is not simply named after the email. DBA-owned and one-way — a client never gets to ask for a role. docs/BURROW_PLAN.md';

-- ── the unmapped queue (an onboarding inbox, not an error log) ──────────────
CREATE TABLE IF NOT EXISTS rvbbit.identity_unmapped (
    identity   text PRIMARY KEY,
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now(),
    attempts   integer NOT NULL DEFAULT 1,
    via        text                        -- which IdP proved it
);
COMMENT ON TABLE rvbbit.identity_unmapped IS
    'Burrow: people who authenticated successfully but have no PG account yet — the provisioning queue. Rows disappear from relevance once identity_map or a matching role exists. docs/BURROW_PLAN.md';

-- ── resolution ──────────────────────────────────────────────────────────────
-- SECURITY DEFINER: reads pg_roles and writes the unmapped queue on behalf of
-- a service account that need not own either.
CREATE OR REPLACE FUNCTION rvbbit.resolve_identity(p_identity text, p_via text DEFAULT NULL)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER AS $fn$
DECLARE
    v_id   text := lower(btrim(coalesce(p_identity, '')));
    v_role text;
BEGIN
    IF v_id = '' THEN
        RETURN NULL;
    END IF;

    -- 1. explicit map wins, always.
    SELECT role_name INTO v_role
    FROM rvbbit.identity_map WHERE identity = v_id AND enabled;

    -- 2. otherwise the email may BE the role (the cloud-IAM convention).
    --    Guarded on length: Postgres truncates identifiers to 63 bytes with
    --    only a NOTICE, so two long addresses sharing a 63-byte prefix would
    --    silently collide into one account. Refuse rather than truncate —
    --    over-long identities need an explicit identity_map row.
    IF v_role IS NULL AND octet_length(v_id) <= 63 THEN
        SELECT rolname INTO v_role FROM pg_roles
        WHERE rolname = v_id AND rolcanlogin AND NOT rolsuper;
    END IF;

    -- 3. unmapped: remember them so somebody can provision them.
    IF v_role IS NULL THEN
        INSERT INTO rvbbit.identity_unmapped (identity, via)
        VALUES (v_id, p_via)
        ON CONFLICT (identity) DO UPDATE
            SET last_seen = now(),
                attempts  = rvbbit.identity_unmapped.attempts + 1,
                via       = coalesce(EXCLUDED.via, rvbbit.identity_unmapped.via);
        RETURN NULL;
    END IF;

    -- A mapped identity is no longer waiting on anyone.
    DELETE FROM rvbbit.identity_unmapped WHERE identity = v_id;
    RETURN v_role;
END $fn$;

COMMENT ON FUNCTION rvbbit.resolve_identity(text, text) IS
    'Burrow: verified IdP identity -> PG role, or NULL (caller falls back to rvbbit_guest). Records unmapped identities as a provisioning queue. docs/BURROW_PLAN.md';

-- ── optional: give guest a real read-only tier ─────────────────────────────
-- NOT called by this migration. rvbbit never grants on a customer's schema on
-- its own — that is the DBA's decision, made once, deliberately, and visible
-- in the audit log as their action rather than ours.
CREATE OR REPLACE FUNCTION rvbbit.burrow_grant_guest(p_schema text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER AS $fn$
BEGIN
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO rvbbit_guest', p_schema);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO rvbbit_guest', p_schema);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO rvbbit_guest', p_schema);
    RETURN format('rvbbit_guest may now read %I', p_schema);
END $fn$;

COMMENT ON FUNCTION rvbbit.burrow_grant_guest(text) IS
    'Opt-in: promote rvbbit_guest from blind to read-only over one schema. Deliberate, DBA-invoked; the default guest sees nothing. docs/BURROW_PLAN.md';

-- Convenience view: who is waiting, and can we already place them?
CREATE OR REPLACE VIEW rvbbit.identity_pending AS
SELECT u.identity, u.via, u.attempts, u.first_seen, u.last_seen,
       EXISTS (SELECT 1 FROM pg_roles r
               WHERE r.rolname = u.identity AND r.rolcanlogin) AS role_now_exists
FROM rvbbit.identity_unmapped u
ORDER BY u.last_seen DESC;

COMMENT ON VIEW rvbbit.identity_pending IS
    'Burrow provisioning queue: authenticated identities with no PG account. role_now_exists flags ones a DBA created since — they resolve on next login. docs/BURROW_PLAN.md';
