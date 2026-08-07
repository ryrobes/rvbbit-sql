"""Application-layer people and Teams for Calliope authorization.

The authenticated Warehouse request remains the identity authority.  This
module only consumes the already-decided human ``subject`` and never accepts an
``acting_as`` value from a tool or browser payload.

Teams are intentionally flat.  The protected ``Admins`` Team is the sole
capability for changing Teams.  The protected ``Everyone`` Team is a dynamic
wildcard for every verified application subject; ordinary Teams retain only
explicit observed-person membership.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping
from typing import Any, Callable
from urllib.parse import urlsplit


ADMIN_TEAM_ID = "00000000-0000-4000-8000-000000000001"
ADMIN_TEAM_SLUG = "admins"
ADMIN_TEAM_SYSTEM_KEY = "admins"
EVERYONE_TEAM_ID = "00000000-0000-4000-8000-000000000002"
EVERYONE_TEAM_SLUG = "everyone"
EVERYONE_TEAM_SYSTEM_KEY = "everyone"
EVERYONE_TEAM_DESCRIPTION = (
    "Automatically includes every user with a verified application sign-in."
)
SYSTEM_TEAM_KEYS = frozenset({ADMIN_TEAM_SYSTEM_KEY, EVERYONE_TEAM_SYSTEM_KEY})
BOOTSTRAP_ADMINS_ENV = "WAREHOUSE_TEAM_BOOTSTRAP_ADMINS"
_SERVICE_IDENTITY_ENVS = (
    "WAREHOUSE_HERMES_MCP_CALLER",
    "WAREHOUSE_MCP_STATIC_CALLER",
)

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,188}$")
_TEAM_SLUG_RE = re.compile(r"[^a-z0-9]+")


class TeamError(Exception):
    """Typed application error shared by MCP and native Calliope routes."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


# Shape-compatible with migrations 0252-0253. Extension upgrades install it;
# Warehouse service upgrades self-heal additively before exposing the tools.
DDL = f"""
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
    CONSTRAINT teams_slug_check CHECK (slug ~ '^[a-z0-9][a-z0-9-]{{0,79}}$'),
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
    before_state jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    after_state jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    detail jsonb NOT NULL DEFAULT '{{}}'::jsonb,
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

INSERT INTO rvbbit.teams
    (id,slug,name,description,system_key,created_by,updated_by)
VALUES
    ('{ADMIN_TEAM_ID}'::uuid,'{ADMIN_TEAM_SLUG}','Admins',
     'Members may create, edit, archive, and change membership for application Teams.',
     '{ADMIN_TEAM_SYSTEM_KEY}','system','system')
ON CONFLICT (id) DO NOTHING;

INSERT INTO rvbbit.teams
    (id,slug,name,description,system_key,created_by,updated_by)
VALUES
    ('{EVERYONE_TEAM_ID}'::uuid,'{EVERYONE_TEAM_SLUG}','Everyone',
     '{EVERYONE_TEAM_DESCRIPTION}',
     '{EVERYONE_TEAM_SYSTEM_KEY}','system','system')
ON CONFLICT (slug) DO UPDATE SET
    name=excluded.name,
    description=excluded.description,
    system_key=excluded.system_key,
    archived=false,
    updated_by='system',
    updated_at=now()
WHERE rvbbit.teams.system_key IS DISTINCT FROM '{EVERYONE_TEAM_SYSTEM_KEY}';

-- Everyone is a rule, not a materialized list.  This also normalizes an
-- ordinary Team named Everyone created before the system Team shipped.
DELETE FROM rvbbit.team_members
WHERE team_id=(SELECT id FROM rvbbit.teams WHERE system_key='{EVERYONE_TEAM_SYSTEM_KEY}');

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

CREATE OR REPLACE FUNCTION rvbbit._protect_last_team_admin()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF OLD.team_id='{ADMIN_TEAM_ID}'::uuid THEN
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
"""


def normalize_email(value: Any) -> str:
    email = str(value or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        raise TeamError("INVALID_PRINCIPAL", "Choose a valid observed email address.")
    return email


def _name(value: Any) -> str:
    name = re.sub(r"\s+", " ", str(value or "").strip())
    if not 1 <= len(name) <= 120:
        raise TeamError("INVALID_TEAM_NAME", "Team names must be between 1 and 120 characters.")
    if name.casefold() in SYSTEM_TEAM_KEYS:
        raise TeamError(
            "RESERVED_TEAM_NAME",
            f"{name.title()} is a protected application Team.",
        )
    return name


def _description(value: Any) -> str:
    description = str(value or "").strip()
    if len(description) > 2_000:
        raise TeamError("INVALID_TEAM_DESCRIPTION", "Team descriptions may contain at most 2,000 characters.")
    return description


def _normalized_email_list(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple, set)):
        raise TeamError("INVALID_TEAM_MEMBERS", "Team members must be supplied as a list of observed emails.")
    return list(dict.fromkeys(normalize_email(value) for value in values))


def _slug_base(name: str) -> str:
    slug = _TEAM_SLUG_RE.sub("-", name.casefold()).strip("-")[:72]
    return slug or "team"


def _auth_value(authorization: Any, name: str, default: Any = None) -> Any:
    if isinstance(authorization, dict):
        return authorization.get(name, default)
    return getattr(authorization, name, default)


def _subject(authorization: Any) -> str:
    value = _auth_value(authorization, "subject")
    if not value:
        raise TeamError(
            "APPLICATION_SUBJECT_REQUIRED",
            "Teams require a direct signed-in user or trusted Calliope delegation.",
            403,
        )
    return normalize_email(value)


def _event_auth(authorization: Any) -> dict[str, Any]:
    subject = _subject(authorization)
    return {
        "credential_actor": str(_auth_value(authorization, "actor") or subject)[:320],
        "human_subject": subject,
        "auth_mode": str(_auth_value(authorization, "mode") or "application")[:80],
        "delegated": bool(_auth_value(authorization, "delegated", False)),
        "platform": str(_auth_value(authorization, "platform") or "")[:80] or None,
        "session_ref": str(_auth_value(authorization, "session_ref") or "")[:500] or None,
    }


def authorization_matches_team(team: Any, authorization: Any) -> bool:
    """Evaluate a Team only against Warehouse's already-verified subject.

    Everyone deliberately does not consult ``application_principals``: a newly
    authenticated user matches immediately, even before their best-effort
    observation row has committed.  A service/anonymous context has no subject
    and therefore never matches the wildcard.
    """
    raw_subject = _auth_value(authorization, "subject")
    if not raw_subject or not isinstance(team, Mapping) or team.get("archived"):
        return False
    try:
        subject = normalize_email(raw_subject)
    except TeamError:
        return False
    if team.get("system_key") == EVERYONE_TEAM_SYSTEM_KEY:
        return True
    members = team.get("members") if isinstance(team.get("members"), list) else []
    return subject in {
        str(member).strip().lower() for member in members if member
    }


def _google_avatar_url(value: Any) -> str | None:
    """Retain only bounded Google-hosted URLs from a verified profile claim."""
    candidate = str(value or "").strip()[:2048]
    if not candidate or any(ord(character) < 32 for character in candidate):
        return None
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not (host == "googleusercontent.com" or host.endswith(".googleusercontent.com"))
    ):
        return None
    return candidate


def observe_principal_on_connection(
    conn: Any,
    authorization: Any,
    *,
    channel: str | None = None,
    display_name: str | None = None,
    avatar_provider: str | None = None,
    avatar_source_url: str | None = None,
) -> str | None:
    """Record only Warehouse-authorized human subjects as Team candidates."""
    raw_subject = _auth_value(authorization, "subject")
    if not raw_subject:
        return None
    email = normalize_email(raw_subject)
    name = re.sub(r"\s+", " ", str(display_name or "").strip())[:160] or None
    provider = str(avatar_provider or "").strip().lower()
    source_url = (
        _google_avatar_url(avatar_source_url)
        if provider == "google"
        else None
    )
    provider = "google" if source_url else None
    auth_mode = str(_auth_value(authorization, "mode") or "application")[:80]
    last_channel = str(channel or _auth_value(authorization, "platform") or "")[:80] or None
    conn.execute(
        "INSERT INTO rvbbit.application_principals "
        "(email,display_name,last_auth_mode,last_channel,avatar_provider,avatar_source_url) "
        "VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (email) DO UPDATE SET "
        "display_name=coalesce(excluded.display_name,rvbbit.application_principals.display_name),"
        "last_seen_at=now(),last_auth_mode=excluded.last_auth_mode,"
        "last_channel=coalesce(excluded.last_channel,rvbbit.application_principals.last_channel),"
        "avatar_provider=coalesce(excluded.avatar_provider,rvbbit.application_principals.avatar_provider),"
        "avatar_source_url=coalesce(excluded.avatar_source_url,rvbbit.application_principals.avatar_source_url),"
        "observation_count=rvbbit.application_principals.observation_count+1 "
        "WHERE rvbbit.application_principals.last_seen_at < now()-interval '5 minutes' "
        "OR rvbbit.application_principals.last_auth_mode IS DISTINCT FROM excluded.last_auth_mode "
        "OR rvbbit.application_principals.last_channel IS DISTINCT FROM excluded.last_channel "
        "OR (excluded.display_name IS NOT NULL AND "
        "rvbbit.application_principals.display_name IS DISTINCT FROM excluded.display_name) "
        "OR (excluded.avatar_source_url IS NOT NULL AND "
        "rvbbit.application_principals.avatar_source_url IS DISTINCT FROM excluded.avatar_source_url)",
        (email, name, auth_mode, last_channel, provider, source_url),
    )
    return email


def _record_event(
    conn: Any,
    team_id: str,
    event_type: str,
    authorization: Any,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    auth = _event_auth(authorization)
    conn.execute(
        "INSERT INTO rvbbit.team_events "
        "(event_id,team_id,event_type,credential_actor,human_subject,auth_mode,delegated,"
        "platform,session_ref,before_state,after_state,detail) "
        "VALUES (%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)",
        (
            str(uuid.uuid4()), team_id, event_type,
            auth["credential_actor"], auth["human_subject"], auth["auth_mode"],
            auth["delegated"], auth["platform"], auth["session_ref"],
            json.dumps(before or {}), json.dumps(after or {}), json.dumps(detail or {}),
        ),
    )


def _bootstrap_admins(conn: Any) -> None:
    emails = _normalized_email_list([
        value for value in os.environ.get(BOOTSTRAP_ADMINS_ENV, "").split(",")
        if value.strip()
    ])
    if not emails:
        return
    added: list[str] = []
    for email in emails:
        conn.execute(
            "INSERT INTO rvbbit.application_principals "
            "(email,last_auth_mode,last_channel) VALUES (%s,'bootstrap_config','server') "
            "ON CONFLICT (email) DO NOTHING",
            (email,),
        )
        row = conn.execute(
            "INSERT INTO rvbbit.team_members (team_id,principal_email,added_by) "
            "VALUES (%s::uuid,%s,'bootstrap_config') ON CONFLICT DO NOTHING "
            "RETURNING principal_email",
            (ADMIN_TEAM_ID, email),
        ).fetchone()
        if row:
            added.append(email)
    if not added:
        return
    team = conn.execute(
        "UPDATE rvbbit.teams SET revision=revision+1,updated_by='bootstrap_config',updated_at=now() "
        "WHERE id=%s::uuid RETURNING revision",
        (ADMIN_TEAM_ID,),
    ).fetchone()
    conn.execute(
        "INSERT INTO rvbbit.team_events "
        "(event_id,team_id,event_type,credential_actor,human_subject,auth_mode,detail,after_state) "
        "VALUES (%s::uuid,%s::uuid,'admins_bootstrapped','server','bootstrap_config',"
        "'bootstrap_config',%s::jsonb,%s::jsonb)",
        (
            str(uuid.uuid4()), ADMIN_TEAM_ID,
            json.dumps({"added": added, "environment": BOOTSTRAP_ADMINS_ENV}),
            json.dumps({"revision": int((team or {}).get("revision") or 1)}),
        ),
    )


def _backfill_observed_principals(conn: Any) -> None:
    available = conn.execute(
        "SELECT to_regclass('rvbbit.mcp_activity') IS NOT NULL AS activity,"
        "to_regclass('rvbbit.calliope_sessions') IS NOT NULL AS sessions"
    ).fetchone() or {}
    sources: list[str] = []
    if available.get("activity"):
        # Modern activity receipts carry the one authorization subject decided
        # by Warehouse.  Older receipts predate that column being populated,
        # but signed browser surfaces still retained their verified owner in
        # caller.  Recover only those bounded browser surfaces: arbitrary old
        # direct-MCP/static-key callers must not become Team candidates.
        sources.extend([
            "SELECT lower(btrim(subject)) AS email,ts AS first_seen_at,"
            "ts AS last_seen_at,coalesce(nullif(auth_mode,''),'application') AS auth_mode,"
            "channel,0 AS source_rank FROM rvbbit.mcp_activity "
            "WHERE subject IS NOT NULL AND length(btrim(subject))<=254 "
            "AND btrim(subject) ~ '^[^@[:space:]]{1,64}@[^@[:space:]]{1,188}$'",
            "SELECT lower(btrim(caller)) AS email,ts AS first_seen_at,"
            "ts AS last_seen_at,'browser_session' AS auth_mode,channel,2 AS source_rank "
            "FROM rvbbit.mcp_activity WHERE subject IS NULL AND channel='web' "
            "AND client_app IN ('dashboard','gallery','calliope','warehouse_web') "
            "AND caller IS NOT NULL AND length(btrim(caller))<=254 "
            "AND btrim(caller) ~ '^[^@[:space:]]{1,64}@[^@[:space:]]{1,188}$'",
        ])
    if available.get("sessions"):
        # A Calliope session can only be created from Warehouse's signed
        # browser session.  It is therefore a durable identity ledger for
        # installs whose activity rows predate actor/subject provenance.
        sources.append(
            "SELECT lower(btrim(owner_email)) AS email,created_at AS first_seen_at,"
            "updated_at AS last_seen_at,'browser_session' AS auth_mode,'web' AS channel,"
            "1 AS source_rank FROM rvbbit.calliope_sessions "
            "WHERE owner_email IS NOT NULL AND length(btrim(owner_email))<=254 "
            "AND btrim(owner_email) ~ '^[^@[:space:]]{1,64}@[^@[:space:]]{1,188}$'"
        )
    if not sources:
        return
    service_emails = ["calliope@system"]
    for env_name in _SERVICE_IDENTITY_ENVS:
        candidate = str(os.environ.get(env_name) or "").strip().lower()
        if _EMAIL_RE.fullmatch(candidate):
            service_emails.append(candidate)
    service_emails = list(dict.fromkeys(service_emails))
    conn.execute(
        "WITH raw_observations AS (" + " UNION ALL ".join(sources) + "),"
        "human_observations AS (SELECT * FROM raw_observations "
        "WHERE NOT (email=ANY(%s::text[]))),"
        "observations AS (SELECT email,"
        "min(first_seen_at) OVER (PARTITION BY email) AS first_seen_at,"
        "max(last_seen_at) OVER (PARTITION BY email) AS last_seen_at,auth_mode,channel,"
        "row_number() OVER (PARTITION BY email ORDER BY last_seen_at DESC,source_rank) AS rn "
        "FROM human_observations) INSERT INTO rvbbit.application_principals "
        "(email,first_seen_at,last_seen_at,last_auth_mode,last_channel) "
        "SELECT email,first_seen_at,last_seen_at,auth_mode,channel FROM observations WHERE rn=1 "
        "ON CONFLICT (email) DO UPDATE SET "
        "first_seen_at=least(rvbbit.application_principals.first_seen_at,excluded.first_seen_at),"
        "last_seen_at=greatest(rvbbit.application_principals.last_seen_at,excluded.last_seen_at),"
        "last_auth_mode=CASE WHEN excluded.last_seen_at>=rvbbit.application_principals.last_seen_at "
        "THEN excluded.last_auth_mode ELSE rvbbit.application_principals.last_auth_mode END,"
        "last_channel=CASE WHEN excluded.last_seen_at>=rvbbit.application_principals.last_seen_at "
        "THEN excluded.last_channel ELSE rvbbit.application_principals.last_channel END",
        (service_emails,),
    )


def ensure_tables(conn_factory: Callable[..., Any]) -> None:
    with conn_factory() as conn:
        conn.execute(DDL)
        _backfill_observed_principals(conn)
        _bootstrap_admins(conn)


def _is_admin(conn: Any, subject: str) -> bool:
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM rvbbit.team_members m "
        "JOIN rvbbit.teams t ON t.id=m.team_id "
        "WHERE t.system_key='admins' AND NOT t.archived AND m.principal_email=%s) AS allowed",
        (subject,),
    ).fetchone() or {}
    return bool(row.get("allowed"))


def _require_admin(conn: Any, subject: str) -> None:
    if not _is_admin(conn, subject):
        raise TeamError(
            "TEAM_ADMIN_REQUIRED",
            "Only members of the Admins Team may change Teams.",
            403,
        )


def _shape_team(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["id"] = str(result["id"])
    result["members"] = list(result.get("members") or [])
    system_key = result.get("system_key")
    dynamic = system_key == EVERYONE_TEAM_SYSTEM_KEY
    result["membership_rule"] = (
        "authenticated_users" if dynamic else "explicit_members"
    )
    result["dynamic_membership"] = dynamic
    result["member_count"] = None if dynamic else len(result["members"])
    result["protected"] = system_key in SYSTEM_TEAM_KEYS
    return result


def _team_snapshot(conn: Any, team_id: str, *, include_events: bool = False) -> dict[str, Any]:
    row = conn.execute(
        "SELECT t.id::text,t.slug,t.name,t.description,t.system_key,t.archived,t.revision,"
        "t.created_by,t.created_at,t.updated_by,t.updated_at,"
        "coalesce(array_agg(m.principal_email ORDER BY m.principal_email) "
        "FILTER (WHERE m.principal_email IS NOT NULL),ARRAY[]::text[]) AS members "
        "FROM rvbbit.teams t LEFT JOIN rvbbit.team_members m ON m.team_id=t.id "
        "WHERE t.id=%s::uuid GROUP BY t.id",
        (team_id,),
    ).fetchone()
    if not row:
        raise TeamError("TEAM_NOT_FOUND", "That Team is no longer available.", 404)
    result = _shape_team(row)
    if include_events:
        events = conn.execute(
            "SELECT event_id::text,event_type,credential_actor,human_subject,auth_mode,delegated,"
            "platform,session_ref,before_state,after_state,detail,created_at "
            "FROM rvbbit.team_events WHERE team_id=%s::uuid "
            "ORDER BY created_at DESC,event_id DESC LIMIT 30",
            (team_id,),
        ).fetchall()
        result["events"] = [dict(event) for event in events]
    return result


def people_search(
    conn_factory: Callable[..., Any],
    authorization: Any,
    query: str | None = None,
    limit: int = 100,
    display_name: str | None = None,
) -> dict[str, Any]:
    subject = _subject(authorization)
    normalized_query = re.sub(r"\s+", " ", str(query or "").strip())[:240]
    # Treat punctuation as a word boundary so a person's ordinary written
    # name can match an email local part ("Jane Smith" -> jane.smith@...).
    search_terms = [
        term for term in re.split(r"[\W_]+", normalized_query.casefold()) if term
    ][:16]
    search_patterns = [f"%{term}%" for term in search_terms]
    bounded_limit = max(1, min(int(limit or 100), 500))
    with conn_factory() as conn:
        observe_principal_on_connection(
            conn, authorization, display_name=display_name
        )
        rows = conn.execute(
            "SELECT p.email,p.display_name,p.avatar_key::text,p.first_seen_at,p.last_seen_at,p.last_auth_mode,p.last_channel,"
            "p.observation_count,coalesce(array_agg(t.slug ORDER BY t.name) "
            "FILTER (WHERE t.id IS NOT NULL),ARRAY[]::text[]) AS teams "
            "FROM rvbbit.application_principals p "
            "LEFT JOIN rvbbit.team_members m ON m.principal_email=p.email "
            "LEFT JOIN rvbbit.teams t ON t.id=m.team_id AND NOT t.archived "
            "WHERE (cardinality(%s::text[])=0 OR "
            "lower(concat_ws(' ',p.email,p.display_name)) ILIKE ALL(%s::text[])) "
            "GROUP BY p.email ORDER BY p.last_seen_at DESC,p.email LIMIT %s",
            (search_patterns, search_patterns, bounded_limit),
        ).fetchall()
        people = [dict(row) for row in rows]
        return {
            "subject": subject,
            "can_manage": _is_admin(conn, subject),
            "people": people,
            "count": len(people),
        }


def list_teams(
    conn_factory: Callable[..., Any],
    authorization: Any,
    query: str | None = None,
    include_archived: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    subject = _subject(authorization)
    normalized_query = re.sub(r"\s+", " ", str(query or "").strip())[:240]
    bounded_limit = max(1, min(int(limit or 200), 500))
    with conn_factory() as conn:
        observe_principal_on_connection(conn, authorization)
        rows = conn.execute(
            "SELECT t.id::text,t.slug,t.name,t.description,t.system_key,t.archived,t.revision,"
            "t.created_by,t.created_at,t.updated_by,t.updated_at,"
            "coalesce(array_agg(m.principal_email ORDER BY m.principal_email) "
            "FILTER (WHERE m.principal_email IS NOT NULL),ARRAY[]::text[]) AS members,"
            "(SELECT max(e.created_at) FROM rvbbit.team_events e WHERE e.team_id=t.id) AS last_changed_at "
            "FROM rvbbit.teams t LEFT JOIN rvbbit.team_members m ON m.team_id=t.id "
            "WHERE (%s OR NOT t.archived) "
            "AND (%s='' OR t.name ILIKE '%%'||%s||'%%' OR t.description ILIKE '%%'||%s||'%%') "
            "GROUP BY t.id ORDER BY CASE t.system_key WHEN 'admins' THEN 0 "
            "WHEN 'everyone' THEN 1 ELSE 2 END,t.archived,t.name LIMIT %s",
            (bool(include_archived), normalized_query, normalized_query, normalized_query, bounded_limit),
        ).fetchall()
        teams = []
        for row in rows:
            teams.append(_shape_team(row))
        return {
            "subject": subject,
            "can_manage": _is_admin(conn, subject),
            "teams": teams,
            "count": len(teams),
        }


def get_team(
    conn_factory: Callable[..., Any], authorization: Any, team_id: Any
) -> dict[str, Any]:
    subject = _subject(authorization)
    try:
        normalized_id = str(uuid.UUID(str(team_id)))
    except (TypeError, ValueError):
        raise TeamError("INVALID_TEAM_ID", "Choose a valid Team.") from None
    with conn_factory() as conn:
        observe_principal_on_connection(conn, authorization)
        can_manage = _is_admin(conn, subject)
        return {
            "subject": subject,
            "can_manage": can_manage,
            # Mutation receipts carry transport/session provenance and are an
            # administrator diagnostic, not a general company directory field.
            "team": _team_snapshot(conn, normalized_id, include_events=can_manage),
        }


def _unique_slug(conn: Any, name: str) -> str:
    base = _slug_base(name)
    slug = base
    suffix = 2
    while conn.execute("SELECT 1 AS found FROM rvbbit.teams WHERE slug=%s", (slug,)).fetchone():
        tail = f"-{suffix}"
        slug = base[:80-len(tail)].rstrip("-") + tail
        suffix += 1
    return slug


def _require_observed(conn: Any, emails: list[str]) -> None:
    if not emails:
        return
    rows = conn.execute(
        "SELECT email FROM rvbbit.application_principals WHERE email=ANY(%s::text[])",
        (emails,),
    ).fetchall()
    found = {str(row["email"]) for row in rows}
    missing = [email for email in emails if email not in found]
    if missing:
        raise TeamError(
            "TEAM_MEMBER_NOT_OBSERVED",
            "These people have not yet been observed through a trusted Calliope sign-in: "
            + ", ".join(missing[:8]),
        )


def create_team(
    conn_factory: Callable[..., Any],
    authorization: Any,
    name: Any,
    description: Any = "",
    members: Any = None,
) -> dict[str, Any]:
    subject = _subject(authorization)
    normalized_name = _name(name)
    normalized_description = _description(description)
    member_emails = _normalized_email_list(members)
    team_id = str(uuid.uuid4())
    with conn_factory() as conn:
        with conn.transaction():
            observe_principal_on_connection(conn, authorization)
            _require_admin(conn, subject)
            _require_observed(conn, member_emails)
            slug = _unique_slug(conn, normalized_name)
            try:
                conn.execute(
                    "INSERT INTO rvbbit.teams "
                    "(id,slug,name,description,created_by,updated_by) "
                    "VALUES (%s::uuid,%s,%s,%s,%s,%s)",
                    (team_id, slug, normalized_name, normalized_description, subject, subject),
                )
            except Exception as exc:
                if "teams_name_ci_key" in str(exc) or "duplicate key" in str(exc).lower():
                    raise TeamError("TEAM_NAME_EXISTS", "A Team with that name already exists.", 409) from exc
                raise
            for email in member_emails:
                conn.execute(
                    "INSERT INTO rvbbit.team_members (team_id,principal_email,added_by) "
                    "VALUES (%s::uuid,%s,%s)",
                    (team_id, email, subject),
                )
            after = _team_snapshot(conn, team_id)
            _record_event(
                conn, team_id, "team_created", authorization,
                after={"name": normalized_name, "revision": 1, "members": member_emails},
            )
        return {
            "subject": subject,
            "can_manage": True,
            "team": _team_snapshot(conn, team_id, include_events=True),
            "changed": True,
        }


def update_team(
    conn_factory: Callable[..., Any],
    authorization: Any,
    team_id: Any,
    expected_revision: Any,
    *,
    name: Any = None,
    description: Any = None,
    add_members: Any = None,
    remove_members: Any = None,
    archived: Any = None,
) -> dict[str, Any]:
    subject = _subject(authorization)
    try:
        normalized_id = str(uuid.UUID(str(team_id)))
        if isinstance(expected_revision, bool):
            raise ValueError
        revision = int(expected_revision)
    except (TypeError, ValueError):
        raise TeamError("INVALID_TEAM_CHANGE", "A valid Team and expected revision are required.") from None
    if archived is not None and not isinstance(archived, bool):
        raise TeamError("INVALID_TEAM_CHANGE", "Archived must be true or false.")
    additions = _normalized_email_list(add_members)
    removals = _normalized_email_list(remove_members)
    overlap = sorted(set(additions).intersection(removals))
    if overlap:
        raise TeamError("INVALID_TEAM_CHANGE", "The same person cannot be added and removed in one change.")

    with conn_factory() as conn:
        with conn.transaction():
            observe_principal_on_connection(conn, authorization)
            _require_admin(conn, subject)
            current = conn.execute(
                "SELECT id::text,slug,name,description,system_key,archived,revision "
                "FROM rvbbit.teams WHERE id=%s::uuid FOR UPDATE",
                (normalized_id,),
            ).fetchone()
            if not current:
                raise TeamError("TEAM_NOT_FOUND", "That Team is no longer available.", 404)
            current = dict(current)
            if int(current["revision"]) != revision:
                raise TeamError(
                    "TEAM_REVISION_CONFLICT",
                    "This Team changed while it was open. Refresh it before applying your change.",
                    409,
                )
            system_key = current.get("system_key")
            if system_key == EVERYONE_TEAM_SYSTEM_KEY:
                requested_name = (
                    "Everyone" if name is None
                    else re.sub(r"\s+", " ", str(name).strip())
                )
                requested_description = (
                    EVERYONE_TEAM_DESCRIPTION if description is None
                    else _description(description)
                )
                requested_archived = False if archived is None else archived
                if (
                    requested_name != "Everyone"
                    or requested_description != EVERYONE_TEAM_DESCRIPTION
                    or requested_archived is not False
                    or additions
                    or removals
                ):
                    raise TeamError(
                        "PROTECTED_TEAM",
                        "Everyone is the protected authenticated-user wildcard and cannot be changed.",
                    )
                return {
                    "subject": subject,
                    "can_manage": True,
                    "team": _team_snapshot(conn, normalized_id, include_events=True),
                    "changed": False,
                }
            protected = system_key == ADMIN_TEAM_SYSTEM_KEY
            if name is None:
                next_name = current["name"]
            elif protected and re.sub(r"\s+", " ", str(name).strip()).casefold() == "admins":
                # Agents often send a complete fetched object back unchanged.
                # Accept the canonical protected name while still rejecting
                # Admins as the name of any ordinary/new Team.
                next_name = "Admins"
            else:
                next_name = _name(name)
            next_description = current["description"] if description is None else _description(description)
            next_archived = bool(current["archived"]) if archived is None else bool(archived)
            if protected and (next_name != "Admins" or next_archived):
                raise TeamError(
                    "PROTECTED_TEAM",
                    "The Admins Team cannot be renamed or archived.",
                )
            _require_observed(conn, additions)
            existing_rows = conn.execute(
                "SELECT principal_email FROM rvbbit.team_members WHERE team_id=%s::uuid FOR UPDATE",
                (normalized_id,),
            ).fetchall()
            existing = {str(row["principal_email"]) for row in existing_rows}
            effective_add = [email for email in additions if email not in existing]
            effective_remove = [email for email in removals if email in existing]
            final_members = (existing | set(effective_add)) - set(effective_remove)
            if protected and not final_members:
                raise TeamError(
                    "LAST_TEAM_ADMIN",
                    "The Admins Team must retain at least one member.",
                )
            fields_changed = (
                next_name != current["name"]
                or next_description != current["description"]
                or next_archived != bool(current["archived"])
            )
            if not fields_changed and not effective_add and not effective_remove:
                return {
                    "subject": subject,
                    "can_manage": True,
                    "team": _team_snapshot(conn, normalized_id, include_events=True),
                    "changed": False,
                }
            try:
                conn.execute(
                    "UPDATE rvbbit.teams SET name=%s,description=%s,archived=%s,"
                    "revision=revision+1,updated_by=%s,updated_at=now() WHERE id=%s::uuid",
                    (next_name, next_description, next_archived, subject, normalized_id),
                )
            except Exception as exc:
                if "teams_name_ci_key" in str(exc) or "duplicate key" in str(exc).lower():
                    raise TeamError("TEAM_NAME_EXISTS", "A Team with that name already exists.", 409) from exc
                raise
            for email in effective_add:
                conn.execute(
                    "INSERT INTO rvbbit.team_members (team_id,principal_email,added_by) "
                    "VALUES (%s::uuid,%s,%s)",
                    (normalized_id, email, subject),
                )
            if effective_remove:
                conn.execute(
                    "DELETE FROM rvbbit.team_members "
                    "WHERE team_id=%s::uuid AND principal_email=ANY(%s::text[])",
                    (normalized_id, effective_remove),
                )
            after = _team_snapshot(conn, normalized_id)
            _record_event(
                conn, normalized_id, "team_updated", authorization,
                before={
                    "name": current["name"], "description": current["description"],
                    "archived": bool(current["archived"]), "revision": revision,
                    "members": sorted(existing),
                },
                after={
                    "name": after["name"], "description": after["description"],
                    "archived": after["archived"], "revision": after["revision"],
                    "members": after["members"],
                },
                detail={"added": effective_add, "removed": effective_remove},
            )
        return {
            "subject": subject,
            "can_manage": True,
            "team": _team_snapshot(conn, normalized_id, include_events=True),
            "changed": True,
        }
