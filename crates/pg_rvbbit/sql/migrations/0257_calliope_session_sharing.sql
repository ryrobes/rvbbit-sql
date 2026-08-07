-- 0257: Native, identity-scoped read sharing for Calliope notebooks.
--
-- Ownership remains the only management/execution capability. A view grant
-- may target one observed application principal or one flat application Team;
-- the protected Everyone Team means every verified signed-in user, never an
-- anonymous/public link. Grant changes are revisioned and append-only audited.

ALTER TABLE rvbbit.application_principals
    ADD COLUMN IF NOT EXISTS avatar_key uuid,
    ADD COLUMN IF NOT EXISTS avatar_provider text,
    ADD COLUMN IF NOT EXISTS avatar_source_url text;
UPDATE rvbbit.application_principals
   SET avatar_key=gen_random_uuid()
 WHERE avatar_key IS NULL;
ALTER TABLE rvbbit.application_principals
    ALTER COLUMN avatar_key SET DEFAULT gen_random_uuid(),
    ALTER COLUMN avatar_key SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS application_principals_avatar_key_key
    ON rvbbit.application_principals (avatar_key);

ALTER TABLE rvbbit.calliope_sessions
    ADD COLUMN IF NOT EXISTS access_revision bigint NOT NULL DEFAULT 1;

ALTER TABLE rvbbit.calliope_turns
    ADD COLUMN IF NOT EXISTS author_email text,
    ADD COLUMN IF NOT EXISTS author_display_name text;
UPDATE rvbbit.calliope_turns t
   SET author_email=lower(btrim(s.owner_email))
  FROM rvbbit.calliope_sessions s
 WHERE s.id=t.session_id AND nullif(btrim(t.author_email),'') IS NULL;
ALTER TABLE rvbbit.calliope_turns
    ALTER COLUMN author_email SET NOT NULL;
CREATE OR REPLACE FUNCTION rvbbit._calliope_turn_author_default()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF nullif(btrim(NEW.author_email),'') IS NULL THEN
        SELECT lower(btrim(s.owner_email)) INTO NEW.author_email
          FROM rvbbit.calliope_sessions s WHERE s.id=NEW.session_id;
    END IF;
    IF nullif(btrim(NEW.author_display_name),'') IS NULL THEN
        NEW.author_display_name := NEW.author_email;
    END IF;
    RETURN NEW;
END
$fn$;
DROP TRIGGER IF EXISTS calliope_turn_author_default ON rvbbit.calliope_turns;
CREATE TRIGGER calliope_turn_author_default
BEFORE INSERT ON rvbbit.calliope_turns
FOR EACH ROW EXECUTE FUNCTION rvbbit._calliope_turn_author_default();
CREATE INDEX IF NOT EXISTS calliope_turns_author_created_idx
    ON rvbbit.calliope_turns (lower(author_email),created_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.calliope_session_view_grants (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES rvbbit.calliope_sessions(id) ON DELETE CASCADE,
    team_id uuid REFERENCES rvbbit.teams(id) ON DELETE CASCADE,
    principal_email text REFERENCES rvbbit.application_principals(email) ON DELETE CASCADE,
    granted_by text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calliope_session_view_grants_one_grantee_check CHECK (
        num_nonnulls(team_id,principal_email)=1
    ),
    CONSTRAINT calliope_session_view_grants_email_normalized_check CHECK (
        principal_email IS NULL OR
        (principal_email=lower(btrim(principal_email)) AND principal_email LIKE '%@%')
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS calliope_session_view_grants_team_key
    ON rvbbit.calliope_session_view_grants (session_id,team_id)
    WHERE team_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS calliope_session_view_grants_person_key
    ON rvbbit.calliope_session_view_grants (session_id,principal_email)
    WHERE principal_email IS NOT NULL;
CREATE INDEX IF NOT EXISTS calliope_session_view_grants_team_lookup_idx
    ON rvbbit.calliope_session_view_grants (team_id,session_id)
    WHERE team_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS calliope_session_view_grants_person_lookup_idx
    ON rvbbit.calliope_session_view_grants (principal_email,session_id)
    WHERE principal_email IS NOT NULL;

CREATE TABLE IF NOT EXISTS rvbbit.calliope_session_access_events (
    event_id uuid PRIMARY KEY,
    session_id uuid,
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
    CONSTRAINT calliope_session_access_events_payload_check CHECK (
        jsonb_typeof(before_state)='object'
        AND jsonb_typeof(after_state)='object'
        AND jsonb_typeof(detail)='object'
    )
);
CREATE INDEX IF NOT EXISTS calliope_session_access_events_session_created_idx
    ON rvbbit.calliope_session_access_events (session_id,created_at DESC);
CREATE INDEX IF NOT EXISTS calliope_session_access_events_subject_created_idx
    ON rvbbit.calliope_session_access_events (human_subject,created_at DESC);

CREATE OR REPLACE FUNCTION rvbbit._calliope_session_access_events_append_only()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'Calliope session access events are append-only';
END
$fn$;
DROP TRIGGER IF EXISTS calliope_session_access_events_append_only
    ON rvbbit.calliope_session_access_events;
CREATE TRIGGER calliope_session_access_events_append_only
BEFORE UPDATE OR DELETE ON rvbbit.calliope_session_access_events
FOR EACH ROW EXECUTE FUNCTION rvbbit._calliope_session_access_events_append_only();

CREATE OR REPLACE FUNCTION rvbbit.calliope_session_can_view(
    p_session_id uuid,
    p_subject text,
    p_include_archived boolean DEFAULT false
) RETURNS boolean
LANGUAGE sql STABLE
AS $fn$
    SELECT EXISTS (
        SELECT 1
        FROM rvbbit.calliope_sessions s
        WHERE s.id=p_session_id
          AND nullif(btrim(p_subject),'') IS NOT NULL
          AND (NOT s.archived OR (
              p_include_archived
              AND lower(s.owner_email)=lower(btrim(p_subject))
          ))
          AND (
              lower(s.owner_email)=lower(btrim(p_subject))
              OR EXISTS (
                  SELECT 1 FROM rvbbit.calliope_session_view_grants g
                  WHERE g.session_id=s.id
                    AND g.principal_email=lower(btrim(p_subject))
              )
              OR EXISTS (
                  SELECT 1
                  FROM rvbbit.calliope_session_view_grants g
                  JOIN rvbbit.teams t ON t.id=g.team_id AND NOT t.archived
                  WHERE g.session_id=s.id
                    AND btrim(p_subject) LIKE '%@%'
                    AND (
                        t.system_key='everyone'
                        OR EXISTS (
                            SELECT 1 FROM rvbbit.team_members m
                            WHERE m.team_id=t.id
                              AND m.principal_email=lower(btrim(p_subject))
                        )
                    )
              )
          )
    )
$fn$;

COMMENT ON TABLE rvbbit.calliope_session_view_grants IS
    'Exact person or Team read grants for ordinary Calliope chat notebooks. Ownership remains the only write capability.';
COMMENT ON TABLE rvbbit.calliope_session_access_events IS
    'Append-only audit receipts for Calliope notebook sharing changes.';
COMMENT ON FUNCTION rvbbit.calliope_session_can_view(uuid,text,boolean) IS
    'True when the verified application subject owns or currently has an exact person/Team view grant for the notebook.';
