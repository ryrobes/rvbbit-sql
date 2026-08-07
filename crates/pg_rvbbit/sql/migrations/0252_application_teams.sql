-- 0252_application_teams — flat application Teams with one protected Admins Team.
--
-- This is application-layer authorization, independent of Postgres roles.  A
-- trusted Warehouse human subject may appear in application_principals; that
-- observation grants nothing.  Team membership is explicit, and membership in
-- the protected Admins Team is the sole authority for changing Teams through
-- Calliope/MCP.  Artifact grants build on these rows in a later migration.

CREATE TABLE IF NOT EXISTS rvbbit.application_principals (
    email text PRIMARY KEY,
    display_name text,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_auth_mode text,
    last_channel text,
    observation_count bigint NOT NULL DEFAULT 1,
    CONSTRAINT application_principals_email_normalized_check
        CHECK (email=lower(btrim(email)) AND email LIKE '%@%')
);

CREATE TABLE IF NOT EXISTS rvbbit.teams (
    id uuid PRIMARY KEY,
    slug text UNIQUE NOT NULL,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    system_key text UNIQUE,
    archived boolean NOT NULL DEFAULT false,
    revision bigint NOT NULL DEFAULT 1,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT teams_slug_check CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,79}$'),
    CONSTRAINT teams_name_check CHECK (length(btrim(name)) BETWEEN 1 AND 120),
    CONSTRAINT teams_revision_check CHECK (revision >= 1)
);
CREATE UNIQUE INDEX IF NOT EXISTS teams_name_ci_key ON rvbbit.teams (lower(name));
CREATE INDEX IF NOT EXISTS teams_active_name_idx ON rvbbit.teams (archived,name);

CREATE TABLE IF NOT EXISTS rvbbit.team_members (
    team_id uuid NOT NULL REFERENCES rvbbit.teams(id) ON DELETE CASCADE,
    principal_email text NOT NULL REFERENCES rvbbit.application_principals(email),
    added_by text NOT NULL,
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (team_id,principal_email),
    CONSTRAINT team_members_email_normalized_check
        CHECK (principal_email=lower(btrim(principal_email)) AND principal_email LIKE '%@%')
);
CREATE INDEX IF NOT EXISTS team_members_principal_idx
    ON rvbbit.team_members (principal_email,team_id);

CREATE TABLE IF NOT EXISTS rvbbit.team_events (
    event_id uuid PRIMARY KEY,
    team_id uuid NOT NULL REFERENCES rvbbit.teams(id),
    event_type text NOT NULL,
    credential_actor text,
    human_subject text NOT NULL,
    auth_mode text NOT NULL,
    delegated boolean NOT NULL DEFAULT false,
    platform text,
    session_ref text,
    before_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT team_events_payload_check CHECK (
        jsonb_typeof(before_state)='object'
        AND jsonb_typeof(after_state)='object'
        AND jsonb_typeof(detail)='object'
    )
);
CREATE INDEX IF NOT EXISTS team_events_team_created_idx
    ON rvbbit.team_events (team_id,created_at DESC);
CREATE INDEX IF NOT EXISTS team_events_subject_created_idx
    ON rvbbit.team_events (human_subject,created_at DESC);

INSERT INTO rvbbit.teams
    (id,slug,name,description,system_key,created_by,updated_by)
VALUES
    ('00000000-0000-4000-8000-000000000001'::uuid,'admins','Admins',
     'Members may create, edit, archive, and change membership for application Teams.',
     'admins','system','system')
ON CONFLICT (id) DO NOTHING;

CREATE OR REPLACE FUNCTION rvbbit._protect_admins_team()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF OLD.system_key='admins' THEN
        IF TG_OP='DELETE' THEN
            RAISE EXCEPTION 'The Admins Team cannot be deleted';
        END IF;
        IF NEW.id<>OLD.id OR NEW.slug<>'admins' OR NEW.name<>'Admins'
           OR NEW.system_key<>'admins' OR NEW.archived THEN
            RAISE EXCEPTION 'The Admins Team cannot be renamed or archived';
        END IF;
    END IF;
    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$fn$;
DROP TRIGGER IF EXISTS protect_admins_team ON rvbbit.teams;
CREATE TRIGGER protect_admins_team
BEFORE UPDATE OR DELETE ON rvbbit.teams
FOR EACH ROW EXECUTE FUNCTION rvbbit._protect_admins_team();

CREATE OR REPLACE FUNCTION rvbbit._protect_last_team_admin()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF OLD.team_id='00000000-0000-4000-8000-000000000001'::uuid THEN
        -- Serialize even raw-SQL membership changes on the protected Team;
        -- application updates already take this same row lock first.
        PERFORM 1 FROM rvbbit.teams WHERE id=OLD.team_id FOR UPDATE;
        IF (SELECT count(*) FROM rvbbit.team_members
            WHERE team_id=OLD.team_id) <= 1 THEN
            RAISE EXCEPTION 'The Admins Team must retain at least one member';
        END IF;
    END IF;
    RETURN OLD;
END
$fn$;
DROP TRIGGER IF EXISTS protect_last_team_admin ON rvbbit.team_members;
CREATE TRIGGER protect_last_team_admin
BEFORE DELETE ON rvbbit.team_members
FOR EACH ROW EXECUTE FUNCTION rvbbit._protect_last_team_admin();

CREATE OR REPLACE FUNCTION rvbbit._team_events_append_only()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'Team audit events are append-only';
END
$fn$;
DROP TRIGGER IF EXISTS team_events_append_only ON rvbbit.team_events;
CREATE TRIGGER team_events_append_only
BEFORE UPDATE OR DELETE ON rvbbit.team_events
FOR EACH ROW EXECUTE FUNCTION rvbbit._team_events_append_only();

COMMENT ON TABLE rvbbit.application_principals IS
    'Trusted human subjects observed by Warehouse. Directory evidence only; a row grants no access.';
COMMENT ON TABLE rvbbit.teams IS
    'Flat application authorization groups. The system_key=admins Team governs Team mutations.';
COMMENT ON TABLE rvbbit.team_events IS
    'Append-only Team mutation ledger retaining credential actor and authorized human subject.';
