-- 0253_application_everyone_team — protected wildcard for authenticated users.
--
-- Everyone is deliberately not a materialized copy of application_principals.
-- Artifact authorization will match it against the verified request subject,
-- so newly authenticated users are included immediately and service actors are
-- not.  system_key, rather than the seed UUID, is the semantic identity.

INSERT INTO rvbbit.teams
    (id,slug,name,description,system_key,created_by,updated_by)
VALUES
    ('00000000-0000-4000-8000-000000000002'::uuid,'everyone','Everyone',
     'Automatically includes every user with a verified application sign-in.',
     'everyone','system','system')
ON CONFLICT (slug) DO UPDATE SET
    name=excluded.name,
    description=excluded.description,
    system_key=excluded.system_key,
    archived=false,
    updated_by='system',
    updated_at=now()
WHERE rvbbit.teams.system_key IS DISTINCT FROM 'everyone';

-- Normalize a same-named ordinary Team created before Everyone became a
-- system primitive. Dynamic membership must never coexist with explicit rows.
DELETE FROM rvbbit.team_members
WHERE team_id=(SELECT id FROM rvbbit.teams WHERE system_key='everyone');

CREATE OR REPLACE FUNCTION rvbbit._protect_system_teams()
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
    IF OLD.system_key='everyone' THEN
        RAISE EXCEPTION 'The Everyone Team is a protected authenticated-user wildcard and cannot be changed or deleted';
    END IF;
    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$fn$;

DROP TRIGGER IF EXISTS protect_admins_team ON rvbbit.teams;
DROP TRIGGER IF EXISTS protect_system_teams ON rvbbit.teams;
CREATE TRIGGER protect_system_teams
BEFORE UPDATE OR DELETE ON rvbbit.teams
FOR EACH ROW EXECUTE FUNCTION rvbbit._protect_system_teams();

CREATE OR REPLACE FUNCTION rvbbit._protect_everyone_membership()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    target_team_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        target_team_id := OLD.team_id;
    ELSE
        target_team_id := NEW.team_id;
    END IF;
    IF EXISTS (
        SELECT 1 FROM rvbbit.teams
        WHERE id=target_team_id AND system_key='everyone'
    ) THEN
        RAISE EXCEPTION 'The Everyone Team has dynamic membership and cannot contain explicit members';
    END IF;
    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$fn$;

DROP TRIGGER IF EXISTS protect_everyone_membership ON rvbbit.team_members;
CREATE TRIGGER protect_everyone_membership
BEFORE INSERT OR UPDATE OR DELETE ON rvbbit.team_members
FOR EACH ROW EXECUTE FUNCTION rvbbit._protect_everyone_membership();

COMMENT ON FUNCTION rvbbit._protect_everyone_membership() IS
    'Prevents materialized members on the dynamic Everyone application Team.';
