"""Application-layer read sharing for Calliope notebooks.

Notebook ownership remains the sole management and execution capability. View
grants may target one observed human or one flat application Team; contributors
are deliberately not modeled here yet. The protected Everyone Team means every
verified signed-in application subject, never anonymous/public access.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

import application_teams


class CalliopeAccessError(Exception):
    """Typed error shared by native Calliope routes and MCP tools."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


# Migration 0257 installs the same shape for extension upgrades. This additive
# copy lets a Warehouse service image safely start ahead of pg_rvbbit.
DDL = """
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
"""


def ensure_tables(conn_factory: Callable[..., Any]) -> None:
    with conn_factory() as conn:
        conn.execute(DDL)


def _auth_value(authorization: Any, name: str, default: Any = None) -> Any:
    if isinstance(authorization, dict):
        return authorization.get(name, default)
    return getattr(authorization, name, default)


def _subject(authorization: Any) -> str:
    value = str(_auth_value(authorization, "subject") or "").strip().lower()
    if not value:
        raise CalliopeAccessError(
            "APPLICATION_SUBJECT_REQUIRED",
            "Notebook sharing requires a verified signed-in user.",
            403,
        )
    return value[:320]


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


def _session_id(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        raise CalliopeAccessError(
            "INVALID_SESSION", "Choose a valid Calliope notebook."
        ) from None


def _uuid_list(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple, set)):
        raise CalliopeAccessError(
            "INVALID_GRANTS", "Team grants must be supplied as a list."
        )
    result: list[str] = []
    for value in values:
        try:
            normalized = str(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            raise CalliopeAccessError(
                "INVALID_GRANTS", "Choose valid Teams to share with."
            ) from None
        if normalized not in result:
            result.append(normalized)
    return result


def _email_list(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple, set)):
        raise CalliopeAccessError(
            "INVALID_GRANTS", "People grants must be supplied as a list."
        )
    result: list[str] = []
    for value in values:
        try:
            email = application_teams.normalize_email(value)
        except application_teams.TeamError as exc:
            raise CalliopeAccessError("INVALID_GRANTS", str(exc)) from exc
        if email not in result:
            result.append(email)
    return result


def avatar_url(value: Any) -> str | None:
    try:
        key = str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None
    return f"/api/calliope/avatars/{key}"


def _person(row: Any) -> dict[str, Any]:
    item = dict(row or {})
    key = item.pop("avatar_key", None)
    item.pop("is_owner", None)
    item["avatar_url"] = avatar_url(key)
    return item


def _session_kind(conn: Any, session_id: str) -> str:
    row = conn.execute(
        "SELECT CASE "
        "WHEN EXISTS (SELECT 1 FROM rvbbit.calliope_briefs b WHERE b.session_id=%s::uuid) "
        "THEN 'brief' "
        "WHEN EXISTS (SELECT 1 FROM rvbbit.calliope_workflow_runs r WHERE r.session_id=%s::uuid) "
        "OR EXISTS (SELECT 1 FROM rvbbit.calliope_surfaces f WHERE f.session_id=%s::uuid "
        " AND f.source->>'origin'='calliope_instrument_run') THEN 'run' "
        "WHEN EXISTS (SELECT 1 FROM rvbbit.calliope_surfaces f WHERE f.session_id=%s::uuid "
        " AND f.source->>'origin'='calliope_action_library') THEN 'action' "
        "ELSE 'chat' END AS kind",
        (session_id, session_id, session_id, session_id),
    ).fetchone() or {}
    return str(row.get("kind") or "chat")


def _owner_session(
    conn: Any,
    session_id: Any,
    subject: str,
    *,
    lock: bool = False,
    require_shareable: bool = False,
) -> dict[str, Any]:
    normalized = _session_id(session_id)
    suffix = " FOR UPDATE" if lock else ""
    row = conn.execute(
        "SELECT id::text,title,owner_email,archived,access_revision,created_at,updated_at "
        "FROM rvbbit.calliope_sessions WHERE id=%s::uuid" + suffix,
        (normalized,),
    ).fetchone()
    if not row or str(row.get("owner_email") or "").strip().lower() != subject:
        raise CalliopeAccessError(
            "SESSION_NOT_FOUND", "That notebook is not available.", 404
        )
    result = dict(row)
    result["kind"] = _session_kind(conn, normalized)
    if require_shareable and result["kind"] != "chat":
        raise CalliopeAccessError(
            "SESSION_NOT_SHAREABLE",
            "Only ordinary chat notebooks can be shared in this first collaboration release.",
            409,
        )
    return result


def require_owner(
    conn: Any, session_id: Any, authorization: Any, *, require_shareable: bool = False
) -> dict[str, Any]:
    return _owner_session(
        conn,
        session_id,
        _subject(authorization),
        require_shareable=require_shareable,
    )


def require_view(
    conn: Any,
    session_id: Any,
    subject: Any,
    *,
    include_archived: bool = False,
) -> dict[str, Any]:
    normalized = _session_id(session_id)
    viewer = str(subject or "").strip().lower()
    row = conn.execute(
        "SELECT s.*,CASE WHEN lower(s.owner_email)=lower(%s) THEN 'owner' ELSE 'viewer' END "
        "AS access_role,p.display_name AS owner_display_name,p.avatar_key::text AS owner_avatar_key,"
        "(SELECT count(*)::int FROM rvbbit.calliope_session_view_grants g "
        " WHERE g.session_id=s.id) AS share_count "
        "FROM rvbbit.calliope_sessions s "
        "LEFT JOIN rvbbit.application_principals p ON p.email=lower(s.owner_email) "
        "WHERE s.id=%s::uuid AND rvbbit.calliope_session_can_view(s.id,%s,%s)",
        (viewer, normalized, viewer, bool(include_archived)),
    ).fetchone()
    if not row:
        raise CalliopeAccessError(
            "SESSION_NOT_FOUND", "That notebook is not available.", 404
        )
    return dict(row)


def can_view(conn: Any, session_id: Any, subject: Any) -> bool:
    try:
        require_view(conn, session_id, subject)
        return True
    except CalliopeAccessError:
        return False


def _grants(
    conn: Any, session_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    teams = [dict(row) for row in conn.execute(
        "SELECT t.id::text,t.slug,t.name,t.description,t.system_key,t.archived,t.revision "
        "FROM rvbbit.calliope_session_view_grants g "
        "JOIN rvbbit.teams t ON t.id=g.team_id WHERE g.session_id=%s::uuid "
        "ORDER BY CASE t.system_key WHEN 'everyone' THEN 0 WHEN 'admins' THEN 1 ELSE 2 END,t.name",
        (session_id,),
    ).fetchall()]
    people = [_person(row) for row in conn.execute(
        "SELECT g.principal_email AS email,p.display_name,p.last_seen_at,p.avatar_key::text "
        "FROM rvbbit.calliope_session_view_grants g "
        "LEFT JOIN rvbbit.application_principals p ON p.email=g.principal_email "
        "WHERE g.session_id=%s::uuid AND g.principal_email IS NOT NULL "
        "ORDER BY coalesce(p.display_name,g.principal_email),g.principal_email",
        (session_id,),
    ).fetchall()]
    return teams, people


def _team_options(conn: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT t.id::text,t.slug,t.name,t.description,t.system_key,t.revision,"
        "count(m.principal_email)::int AS member_count "
        "FROM rvbbit.teams t LEFT JOIN rvbbit.team_members m ON m.team_id=t.id "
        "WHERE NOT t.archived GROUP BY t.id ORDER BY CASE t.system_key "
        "WHEN 'everyone' THEN 0 WHEN 'admins' THEN 1 ELSE 2 END,t.name"
    ).fetchall()]


def _audience(conn: Any, session: dict[str, Any]) -> dict[str, Any]:
    session_id = str(session["id"])
    teams, people = _grants(conn, session_id)
    owner_row = conn.execute(
        "SELECT %s AS email,p.display_name,p.avatar_key::text "
        "FROM (SELECT 1) seed LEFT JOIN rvbbit.application_principals p "
        "ON p.email=lower(%s)",
        (session["owner_email"], session["owner_email"]),
    ).fetchone() or {"email": session["owner_email"]}
    owner = _person(owner_row)
    participants = [_person(row) for row in conn.execute(
        "SELECT p.email,p.display_name,p.avatar_key::text,"
        "(p.email=lower(%s)) AS is_owner "
        "FROM rvbbit.application_principals p WHERE p.email=lower(%s) OR EXISTS ("
        " SELECT 1 FROM rvbbit.calliope_session_view_grants g "
        " WHERE g.session_id=%s::uuid AND g.principal_email=p.email"
        ") OR EXISTS ("
        " SELECT 1 FROM rvbbit.calliope_session_view_grants g "
        " JOIN rvbbit.teams t ON t.id=g.team_id AND NOT t.archived "
        " JOIN rvbbit.team_members m ON m.team_id=t.id "
        " WHERE g.session_id=%s::uuid AND m.principal_email=p.email"
        ") ORDER BY is_owner DESC,coalesce(p.display_name,p.email),p.email LIMIT 16",
        (
            session["owner_email"], session["owner_email"], session_id,
            session_id,
        ),
    ).fetchall()]
    participant_total_row = conn.execute(
        "SELECT count(*)::int AS total "
        "FROM rvbbit.application_principals p WHERE p.email=lower(%s) OR EXISTS ("
        " SELECT 1 FROM rvbbit.calliope_session_view_grants g "
        " WHERE g.session_id=%s::uuid AND g.principal_email=p.email"
        ") OR EXISTS ("
        " SELECT 1 FROM rvbbit.calliope_session_view_grants g "
        " JOIN rvbbit.teams t ON t.id=g.team_id AND NOT t.archived "
        " JOIN rvbbit.team_members m ON m.team_id=t.id "
        " WHERE g.session_id=%s::uuid AND m.principal_email=p.email"
        ")",
        (session["owner_email"], session_id, session_id),
    ).fetchone() or {}
    participant_total = max(1, int(participant_total_row.get("total") or 0))
    if not any(str(item.get("email")) == str(owner.get("email")) for item in participants):
        participants.insert(0, owner)
    everyone = any(team.get("system_key") == "everyone" for team in teams)
    if everyone:
        summary = "Shared with everyone signed in"
    elif teams or people:
        pieces = []
        if teams:
            pieces.append(f"{len(teams)} Team{'s' if len(teams) != 1 else ''}")
        if people:
            pieces.append(f"{len(people)} person{'s' if len(people) != 1 else ''}")
        summary = "Shared with " + " and ".join(pieces)
    else:
        summary = "Only you"
    return {
        "owner": owner,
        "teams": teams,
        "people": people,
        "participants": participants,
        "participant_count": None if everyone else participant_total,
        "summary": summary,
        "private": not teams and not people,
        "everyone": everyone,
    }


def audience_for_viewer(
    conn_factory: Callable[..., Any], session_id: Any, subject: Any
) -> dict[str, Any]:
    with conn_factory() as conn:
        session = require_view(conn, session_id, subject)
        return _audience(conn, session)


def _record_event(
    conn: Any,
    session: dict[str, Any],
    authorization: Any,
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    detail: dict[str, Any],
) -> None:
    auth = _event_auth(authorization)
    conn.execute(
        "INSERT INTO rvbbit.calliope_session_access_events "
        "(event_id,session_id,event_type,credential_actor,human_subject,auth_mode,delegated,"
        "platform,session_ref,before_state,after_state,detail) "
        "VALUES (%s::uuid,%s::uuid,'sharing_updated',%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)",
        (
            str(uuid.uuid4()), session["id"], auth["credential_actor"],
            auth["human_subject"], auth["auth_mode"], auth["delegated"],
            auth["platform"], auth["session_ref"], json.dumps(before),
            json.dumps(after), json.dumps(detail),
        ),
    )


def _snapshot(
    conn: Any, session: dict[str, Any], *, include_events: bool = False
) -> dict[str, Any]:
    audience = _audience(conn, session)
    result = {
        "session": {
            **session,
            "id": str(session["id"]),
            "access_revision": int(session.get("access_revision") or 1),
        },
        "grants": {"teams": audience["teams"], "people": audience["people"]},
        "audience": audience,
        "summary": audience["summary"],
        "private": audience["private"],
        "everyone": audience["everyone"],
    }
    if include_events:
        result["events"] = [dict(row) for row in conn.execute(
            "SELECT event_id::text,event_type,credential_actor,human_subject,auth_mode,"
            "delegated,platform,session_ref,before_state,after_state,detail,created_at "
            "FROM rvbbit.calliope_session_access_events WHERE session_id=%s::uuid "
            "ORDER BY created_at DESC,event_id DESC LIMIT 30",
            (str(session["id"]),),
        ).fetchall()]
    return result


def get_access(
    conn_factory: Callable[..., Any], authorization: Any, session_id: Any
) -> dict[str, Any]:
    subject = _subject(authorization)
    with conn_factory() as conn:
        session = _owner_session(
            conn, session_id, subject, require_shareable=True
        )
        result = _snapshot(conn, session, include_events=True)
        result["subject"] = subject
        result["teams"] = _team_options(conn)
        return result


def replace_access(
    conn_factory: Callable[..., Any],
    authorization: Any,
    session_id: Any,
    expected_revision: Any,
    *,
    team_ids: Any = None,
    people: Any = None,
    confirm_everyone: bool = False,
) -> dict[str, Any]:
    subject = _subject(authorization)
    try:
        owner_email = application_teams.normalize_email(subject)
    except application_teams.TeamError as exc:
        raise CalliopeAccessError(
            "SHARING_IDENTITY_REQUIRED",
            "Notebook sharing requires an email-backed application identity.",
            403,
        ) from exc
    desired_teams = _uuid_list(team_ids)
    desired_people = [email for email in _email_list(people) if email != owner_email]
    if isinstance(expected_revision, bool):
        raise CalliopeAccessError(
            "INVALID_REVISION", "Inspect the current sharing revision first."
        )
    try:
        revision = int(expected_revision)
    except (TypeError, ValueError):
        raise CalliopeAccessError(
            "INVALID_REVISION", "Inspect the current sharing revision first."
        ) from None

    with conn_factory() as conn:
        with conn.transaction():
            session = _owner_session(
                conn, session_id, subject, lock=True, require_shareable=True
            )
            if int(session.get("access_revision") or 1) != revision:
                raise CalliopeAccessError(
                    "SESSION_ACCESS_CONFLICT",
                    "Sharing changed while it was open. Refresh before saving.",
                    409,
                )
            team_rows = conn.execute(
                "SELECT id::text,name,system_key FROM rvbbit.teams "
                "WHERE id=ANY(%s::uuid[]) AND NOT archived",
                (desired_teams,),
            ).fetchall() if desired_teams else []
            found_team_ids = {str(row["id"]) for row in team_rows}
            if any(team_id not in found_team_ids for team_id in desired_teams):
                raise CalliopeAccessError(
                    "TEAM_NOT_FOUND", "One selected Team is no longer available.", 409
                )
            if desired_people:
                observed = {
                    str(row["email"]) for row in conn.execute(
                        "SELECT email FROM rvbbit.application_principals "
                        "WHERE email=ANY(%s::text[])",
                        (desired_people,),
                    ).fetchall()
                }
                missing = [email for email in desired_people if email not in observed]
                if missing:
                    raise CalliopeAccessError(
                        "PERSON_NOT_OBSERVED",
                        "These people have not signed in through a trusted application surface: "
                        + ", ".join(missing[:8]),
                    )
            current_teams, current_people = _grants(conn, str(session["id"]))
            current_team_ids = {str(team["id"]) for team in current_teams}
            current_emails = {str(person["email"]) for person in current_people}
            desired_team_set = set(desired_teams)
            desired_email_set = set(desired_people)
            added_team_ids = desired_team_set - current_team_ids
            removed_team_ids = current_team_ids - desired_team_set
            added_people = desired_email_set - current_emails
            removed_people = current_emails - desired_email_set
            everyone_added = any(
                str(row["id"]) in added_team_ids and row.get("system_key") == "everyone"
                for row in team_rows
            )
            if everyone_added and confirm_everyone is not True:
                raise CalliopeAccessError(
                    "EVERYONE_CONFIRMATION_REQUIRED",
                    "Confirm explicitly before sharing this notebook with Everyone.",
                    409,
                )
            if not (added_team_ids or removed_team_ids or added_people or removed_people):
                result = _snapshot(conn, session, include_events=True)
                result.update({"subject": subject, "teams": _team_options(conn), "changed": False})
                return result
            before = {
                "revision": revision,
                "team_ids": sorted(current_team_ids),
                "people": sorted(current_emails),
            }
            if removed_team_ids:
                conn.execute(
                    "DELETE FROM rvbbit.calliope_session_view_grants "
                    "WHERE session_id=%s::uuid AND team_id=ANY(%s::uuid[])",
                    (session["id"], sorted(removed_team_ids)),
                )
            if removed_people:
                conn.execute(
                    "DELETE FROM rvbbit.calliope_session_view_grants "
                    "WHERE session_id=%s::uuid AND principal_email=ANY(%s::text[])",
                    (session["id"], sorted(removed_people)),
                )
            for team_id in sorted(added_team_ids):
                conn.execute(
                    "INSERT INTO rvbbit.calliope_session_view_grants "
                    "(session_id,team_id,granted_by) VALUES (%s::uuid,%s::uuid,%s)",
                    (session["id"], team_id, subject),
                )
            for email in sorted(added_people):
                conn.execute(
                    "INSERT INTO rvbbit.calliope_session_view_grants "
                    "(session_id,principal_email,granted_by) VALUES (%s::uuid,%s,%s)",
                    (session["id"], email, subject),
                )
            updated = dict(conn.execute(
                "UPDATE rvbbit.calliope_sessions SET access_revision=access_revision+1 "
                "WHERE id=%s::uuid RETURNING id::text,title,owner_email,archived,"
                "access_revision,created_at,updated_at",
                (session["id"],),
            ).fetchone())
            updated["kind"] = "chat"
            after = {
                "revision": int(updated["access_revision"]),
                "team_ids": sorted(desired_team_set),
                "people": sorted(desired_email_set),
            }
            _record_event(
                conn,
                updated,
                authorization,
                before=before,
                after=after,
                detail={
                    "teams_added": sorted(added_team_ids),
                    "teams_removed": sorted(removed_team_ids),
                    "people_added": sorted(added_people),
                    "people_removed": sorted(removed_people),
                },
            )
        result = _snapshot(conn, updated, include_events=True)
        result.update({"subject": subject, "teams": _team_options(conn), "changed": True})
        return result


def visible_digest(conn_factory: Callable[..., Any], subject: Any) -> str:
    viewer = str(subject or "").strip().lower()
    if not viewer:
        return ""
    with conn_factory() as conn:
        row = conn.execute(
            "SELECT md5(coalesce(string_agg(concat_ws(':',s.id::text,s.access_revision::text,"
            "extract(epoch FROM s.updated_at)::text,s.archived::text,coalesce(("
            " SELECT extract(epoch FROM max(t.updated_at))::text "
            " FROM rvbbit.calliope_session_view_grants g JOIN rvbbit.teams t ON t.id=g.team_id "
            " WHERE g.session_id=s.id),'')),',' ORDER BY s.id::text),'')) AS digest "
            "FROM rvbbit.calliope_sessions s "
            "WHERE rvbbit.calliope_session_can_view(s.id,%s,false)",
            (viewer,),
        ).fetchone() or {}
    return str(row.get("digest") or "")
