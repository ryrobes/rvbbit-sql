"""Application-layer sharing and lifecycle for published artifacts.

Artifact ownership is the sole management capability. View grants may target
one observed human or one flat application Team. The protected Everyone Team
is resolved dynamically by ``rvbbit.artifact_can_view`` against Warehouse's
already-verified human subject; it is never a public/anonymous grant.

Archive is deliberately orthogonal to sharing. It suppresses every ordinary
viewer path while retaining grants, immutable versions, lineage, pins, and
receipts for a reversible restore.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

import application_teams


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$", re.I)


class ArtifactAccessError(Exception):
    """Typed error shared by Gallery routes and MCP tools."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


# 0254 installs the same shape for extension upgrades. This additive copy lets
# a Warehouse image self-heal safely when it starts ahead of pg_rvbbit. The
# singleton marker makes the existing-artifact Everyone grant genuinely
# one-time: artifacts published after installation remain private.
DDL = """
ALTER TABLE rvbbit.dashboards
    ADD COLUMN IF NOT EXISTS archived boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS archived_at timestamptz,
    ADD COLUMN IF NOT EXISTS archived_by text,
    ADD COLUMN IF NOT EXISTS access_revision bigint NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS dashboards_active_updated_idx
    ON rvbbit.dashboards (updated_at DESC) WHERE NOT archived;
CREATE INDEX IF NOT EXISTS dashboards_owner_archived_idx
    ON rvbbit.dashboards (lower(owner_email),archived,updated_at DESC);

CREATE TABLE IF NOT EXISTS rvbbit.artifact_view_grants (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    artifact_id bigint NOT NULL REFERENCES rvbbit.dashboards(id) ON DELETE CASCADE,
    team_id uuid REFERENCES rvbbit.teams(id) ON DELETE CASCADE,
    principal_email text REFERENCES rvbbit.application_principals(email) ON DELETE CASCADE,
    granted_by text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT artifact_view_grants_one_grantee_check CHECK (
        num_nonnulls(team_id,principal_email)=1
    ),
    CONSTRAINT artifact_view_grants_email_normalized_check CHECK (
        principal_email IS NULL OR
        (principal_email=lower(btrim(principal_email)) AND principal_email LIKE '%@%')
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS artifact_view_grants_team_key
    ON rvbbit.artifact_view_grants (artifact_id,team_id) WHERE team_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS artifact_view_grants_person_key
    ON rvbbit.artifact_view_grants (artifact_id,principal_email)
    WHERE principal_email IS NOT NULL;
CREATE INDEX IF NOT EXISTS artifact_view_grants_team_lookup_idx
    ON rvbbit.artifact_view_grants (team_id,artifact_id) WHERE team_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS artifact_view_grants_person_lookup_idx
    ON rvbbit.artifact_view_grants (principal_email,artifact_id)
    WHERE principal_email IS NOT NULL;

CREATE TABLE IF NOT EXISTS rvbbit.artifact_access_events (
    event_id uuid PRIMARY KEY,
    artifact_id bigint,
    artifact_slug text NOT NULL,
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
    CONSTRAINT artifact_access_events_payload_check CHECK (
        jsonb_typeof(before_state)='object'
        AND jsonb_typeof(after_state)='object'
        AND jsonb_typeof(detail)='object'
    )
);
-- Audit receipts outlive their source row even if a database administrator
-- performs an out-of-band hard delete. Application surfaces only archive.
ALTER TABLE rvbbit.artifact_access_events
    DROP CONSTRAINT IF EXISTS artifact_access_events_artifact_id_fkey;
CREATE INDEX IF NOT EXISTS artifact_access_events_artifact_created_idx
    ON rvbbit.artifact_access_events (artifact_id,created_at DESC);
CREATE INDEX IF NOT EXISTS artifact_access_events_subject_created_idx
    ON rvbbit.artifact_access_events (human_subject,created_at DESC);

CREATE OR REPLACE FUNCTION rvbbit._artifact_access_events_append_only()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'Artifact access events are append-only';
END
$fn$;
DROP TRIGGER IF EXISTS artifact_access_events_append_only ON rvbbit.artifact_access_events;
CREATE TRIGGER artifact_access_events_append_only
BEFORE UPDATE OR DELETE ON rvbbit.artifact_access_events
FOR EACH ROW EXECUTE FUNCTION rvbbit._artifact_access_events_append_only();

CREATE TABLE IF NOT EXISTS rvbbit.artifact_access_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    grandfathered_at timestamptz NOT NULL DEFAULT now(),
    grandfathered_count integer NOT NULL DEFAULT 0
);

DO $do$
DECLARE
    first_install boolean := false;
    granted_count integer := 0;
BEGIN
    INSERT INTO rvbbit.artifact_access_state (singleton)
    VALUES (true)
    ON CONFLICT (singleton) DO NOTHING
    RETURNING true INTO first_install;

    IF coalesce(first_install,false) THEN
        INSERT INTO rvbbit.artifact_view_grants
            (artifact_id,team_id,granted_by)
        SELECT d.id,t.id,'system:0254-grandfather'
        FROM rvbbit.dashboards d
        CROSS JOIN rvbbit.teams t
        WHERE t.system_key='everyone'
        ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS granted_count = ROW_COUNT;
        UPDATE rvbbit.artifact_access_state
        SET grandfathered_count=granted_count
        WHERE singleton;
    END IF;
END
$do$;

CREATE OR REPLACE FUNCTION rvbbit.artifact_can_view(
    p_artifact_id bigint,
    p_subject text,
    p_include_archived boolean DEFAULT false
) RETURNS boolean
LANGUAGE sql STABLE
AS $fn$
    SELECT EXISTS (
        SELECT 1
        FROM rvbbit.dashboards d
        WHERE d.id=p_artifact_id
          AND p_subject IS NOT NULL
          AND btrim(p_subject) LIKE '%@%'
          AND (
              NOT d.archived
              OR (
                  p_include_archived
                  AND lower(d.owner_email)=lower(btrim(p_subject))
              )
          )
          AND (
              lower(d.owner_email)=lower(btrim(p_subject))
              OR EXISTS (
                  SELECT 1 FROM rvbbit.artifact_view_grants g
                  WHERE g.artifact_id=d.id
                    AND g.principal_email=lower(btrim(p_subject))
              )
              OR EXISTS (
                  SELECT 1
                  FROM rvbbit.artifact_view_grants g
                  JOIN rvbbit.teams t ON t.id=g.team_id AND NOT t.archived
                  WHERE g.artifact_id=d.id
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

CREATE OR REPLACE VIEW rvbbit.live_apps AS
  SELECT d.id, d.slug, d.name, d.description, d.owner_email, d.team, d.status,
         d.runtime_kind, d.app_kind, d.latest_version, d.manifest, d.last_health,
         d.last_debug_at, d.created_at, d.updated_at,
         coalesce(dep.queries, 0)::int AS queries,
         coalesce(dep.tables, 0)::int AS tables,
         coalesce(dep.metrics, 0)::int AS metrics,
         coalesce(dep.semantic_objects, 0)::int AS semantic_objects,
         d.area_id,area.label AS area_label,d.area_source,d.area_confidence,d.area_updated_at,
         d.archived,d.archived_at,d.archived_by,d.access_revision
  FROM rvbbit.dashboards d
  LEFT JOIN rvbbit.artifact_areas area ON area.id=d.area_id
  LEFT JOIN LATERAL (
    SELECT
           count(*) FILTER (WHERE kind = 'query') AS queries,
           count(*) FILTER (WHERE kind = 'table') AS tables,
           count(*) FILTER (WHERE kind = 'metric') AS metrics,
           count(*) FILTER (WHERE kind = 'semantic') AS semantic_objects
    FROM rvbbit.dashboard_deps
    WHERE dashboard_id = d.id AND version = d.latest_version
  ) dep ON true;
"""


def ensure_tables(conn_factory: Callable[..., Any]) -> None:
    with conn_factory() as conn:
        conn.execute(DDL)


def _auth_value(authorization: Any, name: str, default: Any = None) -> Any:
    if isinstance(authorization, dict):
        return authorization.get(name, default)
    return getattr(authorization, name, default)


def _subject(authorization: Any) -> str:
    value = _auth_value(authorization, "subject")
    if not value:
        raise ArtifactAccessError(
            "APPLICATION_SUBJECT_REQUIRED",
            "Artifact access requires a direct signed-in user or trusted Calliope delegation.",
            403,
        )
    try:
        return application_teams.normalize_email(value)
    except application_teams.TeamError as exc:
        raise ArtifactAccessError("APPLICATION_SUBJECT_REQUIRED", str(exc), 403) from exc


def _slug(value: Any) -> str:
    slug = str(value or "").strip()
    if not _SLUG_RE.fullmatch(slug):
        raise ArtifactAccessError("INVALID_ARTIFACT", "Choose a valid published artifact.")
    return slug


def _uuid_list(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple, set)):
        raise ArtifactAccessError("INVALID_GRANTS", "Team grants must be supplied as a list.")
    result: list[str] = []
    for value in values:
        try:
            normalized = str(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            raise ArtifactAccessError("INVALID_GRANTS", "Choose valid Teams to share with.") from None
        if normalized not in result:
            result.append(normalized)
    return result


def _email_list(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple, set)):
        raise ArtifactAccessError("INVALID_GRANTS", "Individual grants must be supplied as a list.")
    result: list[str] = []
    for value in values:
        try:
            email = application_teams.normalize_email(value)
        except application_teams.TeamError as exc:
            raise ArtifactAccessError("INVALID_GRANTS", str(exc)) from exc
        if email not in result:
            result.append(email)
    return result


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


def _record_event(
    conn: Any,
    artifact: dict[str, Any],
    event_type: str,
    authorization: Any,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    auth = _event_auth(authorization)
    conn.execute(
        "INSERT INTO rvbbit.artifact_access_events "
        "(event_id,artifact_id,artifact_slug,event_type,credential_actor,human_subject,"
        "auth_mode,delegated,platform,session_ref,before_state,after_state,detail) "
        "VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)",
        (
            str(uuid.uuid4()), artifact["id"], artifact["slug"], event_type,
            auth["credential_actor"], auth["human_subject"], auth["auth_mode"],
            auth["delegated"], auth["platform"], auth["session_ref"],
            json.dumps(before or {}), json.dumps(after or {}), json.dumps(detail or {}),
        ),
    )


def can_view(conn: Any, slug: Any, subject: Any, *, include_archived: bool = False) -> bool:
    """Resolve one artifact against a subject already verified by Warehouse."""
    try:
        normalized_slug = _slug(slug)
        normalized_subject = application_teams.normalize_email(subject)
    except (ArtifactAccessError, application_teams.TeamError):
        return False
    row = conn.execute(
        "SELECT rvbbit.artifact_can_view(id,%s,%s) AS allowed "
        "FROM rvbbit.dashboards WHERE slug=%s",
        (normalized_subject, bool(include_archived), normalized_slug),
    ).fetchone() or {}
    return bool(row.get("allowed"))


def require_view(conn: Any, slug: Any, subject: Any) -> dict[str, Any]:
    normalized_slug = _slug(slug)
    normalized_subject = application_teams.normalize_email(subject)
    row = conn.execute(
        "SELECT id,slug,name,owner_email,app_kind,runtime_kind,latest_version,archived,"
        "access_revision,rvbbit.artifact_can_view(id,%s,false) AS allowed "
        "FROM rvbbit.dashboards WHERE slug=%s",
        (normalized_subject, normalized_slug),
    ).fetchone()
    if not row or not row.get("allowed"):
        raise ArtifactAccessError("ARTIFACT_NOT_FOUND", "That artifact is not available.", 404)
    return dict(row)


def require_owner(conn: Any, slug: Any, authorization: Any) -> dict[str, Any]:
    """Require the immutable owner for an artifact-management operation."""
    return _owner_artifact(conn, slug, _subject(authorization))


def _owner_artifact(
    conn: Any, slug: Any, subject: str, *, lock: bool = False
) -> dict[str, Any]:
    suffix = " FOR UPDATE" if lock else ""
    row = conn.execute(
        "SELECT id,slug,name,description,owner_email,app_kind,runtime_kind,latest_version,"
        "archived,archived_at,archived_by,access_revision,updated_at "
        "FROM rvbbit.dashboards WHERE slug=%s" + suffix,
        (_slug(slug),),
    ).fetchone()
    if not row or str(row.get("owner_email") or "").strip().lower() != subject:
        # Do not disclose whether an inaccessible artifact exists.
        raise ArtifactAccessError("ARTIFACT_NOT_FOUND", "That artifact is not available.", 404)
    return dict(row)


def _grants(conn: Any, artifact_id: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    teams = [dict(row) for row in conn.execute(
        "SELECT t.id::text,t.slug,t.name,t.description,t.system_key,t.archived "
        "FROM rvbbit.artifact_view_grants g JOIN rvbbit.teams t ON t.id=g.team_id "
        "WHERE g.artifact_id=%s ORDER BY CASE t.system_key WHEN 'everyone' THEN 0 "
        "WHEN 'admins' THEN 1 ELSE 2 END,t.name",
        (artifact_id,),
    ).fetchall()]
    people = [dict(row) for row in conn.execute(
        "SELECT g.principal_email AS email,p.display_name,p.last_seen_at "
        "FROM rvbbit.artifact_view_grants g "
        "LEFT JOIN rvbbit.application_principals p ON p.email=g.principal_email "
        "WHERE g.artifact_id=%s AND g.principal_email IS NOT NULL "
        "ORDER BY coalesce(p.display_name,g.principal_email),g.principal_email",
        (artifact_id,),
    ).fetchall()]
    return teams, people


def _team_options(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT t.id::text,t.slug,t.name,t.description,t.system_key,t.revision,"
        "count(m.principal_email)::int AS member_count "
        "FROM rvbbit.teams t LEFT JOIN rvbbit.team_members m ON m.team_id=t.id "
        "WHERE NOT t.archived GROUP BY t.id ORDER BY CASE t.system_key "
        "WHEN 'everyone' THEN 0 WHEN 'admins' THEN 1 ELSE 2 END,t.name"
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        dynamic = item.get("system_key") == "everyone"
        item["dynamic_membership"] = dynamic
        item["membership_rule"] = "authenticated_users" if dynamic else "explicit_members"
        item["member_count"] = None if dynamic else int(item.get("member_count") or 0)
        result.append(item)
    return result


def _snapshot(conn: Any, artifact: dict[str, Any], *, include_events: bool = False) -> dict[str, Any]:
    teams, people = _grants(conn, artifact["id"])
    everyone = any(team.get("system_key") == "everyone" for team in teams)
    if everyone:
        summary = "Everyone signed in"
    elif teams or people:
        pieces = []
        if teams:
            pieces.append(f"{len(teams)} Team{'s' if len(teams) != 1 else ''}")
        if people:
            pieces.append(f"{len(people)} person{'s' if len(people) != 1 else ''}")
        summary = "Shared with " + " and ".join(pieces)
    else:
        summary = "Only you"
    result = {
        "artifact": {
            **artifact,
            "id": int(artifact["id"]),
            "archived": bool(artifact.get("archived")),
            "access_revision": int(artifact.get("access_revision") or 1),
        },
        "grants": {"teams": teams, "people": people},
        "summary": summary,
        "private": not teams and not people,
        "everyone": everyone,
    }
    if include_events:
        result["events"] = [dict(row) for row in conn.execute(
            "SELECT event_id::text,event_type,credential_actor,human_subject,auth_mode,"
            "delegated,platform,session_ref,before_state,after_state,detail,created_at "
            "FROM rvbbit.artifact_access_events WHERE artifact_id=%s "
            "ORDER BY created_at DESC,event_id DESC LIMIT 30",
            (artifact["id"],),
        ).fetchall()]
    return result


def get_access(
    conn_factory: Callable[..., Any], authorization: Any, slug: Any
) -> dict[str, Any]:
    subject = _subject(authorization)
    with conn_factory() as conn:
        artifact = _owner_artifact(conn, slug, subject)
        result = _snapshot(conn, artifact, include_events=True)
        result["subject"] = subject
        result["teams"] = _team_options(conn)
        return result


def search_people(
    conn_factory: Callable[..., Any], authorization: Any, query: Any = "", limit: Any = 100
) -> dict[str, Any]:
    subject = _subject(authorization)
    normalized = re.sub(r"\s+", " ", str(query or "").strip())[:240]
    terms = [term for term in re.split(r"[\W_]+", normalized.casefold()) if term][:16]
    patterns = [f"%{term}%" for term in terms]
    try:
        bounded = max(1, min(int(limit or 100), 300))
    except (TypeError, ValueError):
        bounded = 100
    with conn_factory() as conn:
        rows = conn.execute(
            "SELECT email,display_name,last_seen_at,last_auth_mode,last_channel "
            "FROM rvbbit.application_principals WHERE email<>%s AND ("
            "cardinality(%s::text[])=0 "
            "OR lower(concat_ws(' ',email,display_name)) ILIKE ALL(%s::text[])) "
            "ORDER BY last_seen_at DESC,email LIMIT %s",
            (subject, patterns, patterns, bounded),
        ).fetchall()
    return {"subject": subject, "people": [dict(row) for row in rows], "count": len(rows)}


def replace_access(
    conn_factory: Callable[..., Any],
    authorization: Any,
    slug: Any,
    expected_revision: Any,
    *,
    team_ids: Any = None,
    people: Any = None,
    confirm_everyone: bool = False,
) -> dict[str, Any]:
    subject = _subject(authorization)
    desired_teams = _uuid_list(team_ids)
    desired_people = [
        email for email in _email_list(people) if email != subject
    ]
    if isinstance(expected_revision, bool):
        raise ArtifactAccessError("INVALID_REVISION", "Inspect the current sharing revision first.")
    try:
        revision = int(expected_revision)
    except (TypeError, ValueError):
        raise ArtifactAccessError("INVALID_REVISION", "Inspect the current sharing revision first.") from None

    with conn_factory() as conn:
        with conn.transaction():
            artifact = _owner_artifact(conn, slug, subject, lock=True)
            if int(artifact.get("access_revision") or 1) != revision:
                raise ArtifactAccessError(
                    "ARTIFACT_ACCESS_CONFLICT",
                    "Sharing changed while it was open. Refresh before saving.",
                    409,
                )
            team_rows = conn.execute(
                "SELECT id::text,name,system_key FROM rvbbit.teams "
                "WHERE id=ANY(%s::uuid[]) AND NOT archived",
                (desired_teams,),
            ).fetchall() if desired_teams else []
            found_team_ids = {str(row["id"]) for row in team_rows}
            missing_teams = [team_id for team_id in desired_teams if team_id not in found_team_ids]
            if missing_teams:
                raise ArtifactAccessError("TEAM_NOT_FOUND", "One selected Team is no longer available.", 409)
            if desired_people:
                observed = {
                    str(row["email"]) for row in conn.execute(
                        "SELECT email FROM rvbbit.application_principals WHERE email=ANY(%s::text[])",
                        (desired_people,),
                    ).fetchall()
                }
                missing_people = [email for email in desired_people if email not in observed]
                if missing_people:
                    raise ArtifactAccessError(
                        "PERSON_NOT_OBSERVED",
                        "These people have not signed in through a trusted application surface: "
                        + ", ".join(missing_people[:8]),
                    )
            current_teams, current_people = _grants(conn, artifact["id"])
            current_team_ids = {str(team["id"]) for team in current_teams}
            current_emails = {str(person["email"]) for person in current_people}
            desired_team_set = set(desired_teams)
            desired_email_set = set(desired_people)
            added_team_ids = desired_team_set - current_team_ids
            everyone_added = any(
                str(row["id"]) in added_team_ids and row.get("system_key") == "everyone"
                for row in team_rows
            )
            if everyone_added and confirm_everyone is not True:
                raise ArtifactAccessError(
                    "EVERYONE_CONFIRMATION_REQUIRED",
                    "Confirm explicitly before sharing this artifact with Everyone.",
                    409,
                )
            removed_team_ids = current_team_ids - desired_team_set
            added_people = desired_email_set - current_emails
            removed_people = current_emails - desired_email_set
            if not (added_team_ids or removed_team_ids or added_people or removed_people):
                result = _snapshot(conn, artifact, include_events=True)
                result.update({"subject": subject, "changed": False})
                return result
            before = {
                "revision": revision,
                "team_ids": sorted(current_team_ids),
                "people": sorted(current_emails),
                "archived": bool(artifact.get("archived")),
            }
            if removed_team_ids:
                conn.execute(
                    "DELETE FROM rvbbit.artifact_view_grants "
                    "WHERE artifact_id=%s AND team_id=ANY(%s::uuid[])",
                    (artifact["id"], sorted(removed_team_ids)),
                )
            if removed_people:
                conn.execute(
                    "DELETE FROM rvbbit.artifact_view_grants "
                    "WHERE artifact_id=%s AND principal_email=ANY(%s::text[])",
                    (artifact["id"], sorted(removed_people)),
                )
            for team_id in sorted(added_team_ids):
                conn.execute(
                    "INSERT INTO rvbbit.artifact_view_grants (artifact_id,team_id,granted_by) "
                    "VALUES (%s,%s::uuid,%s)",
                    (artifact["id"], team_id, subject),
                )
            for email in sorted(added_people):
                conn.execute(
                    "INSERT INTO rvbbit.artifact_view_grants "
                    "(artifact_id,principal_email,granted_by) VALUES (%s,%s,%s)",
                    (artifact["id"], email, subject),
                )
            updated = dict(conn.execute(
                "UPDATE rvbbit.dashboards SET access_revision=access_revision+1 "
                "WHERE id=%s RETURNING id,slug,name,description,owner_email,app_kind,runtime_kind,"
                "latest_version,archived,archived_at,archived_by,access_revision,updated_at",
                (artifact["id"],),
            ).fetchone())
            after = {
                "revision": updated["access_revision"],
                "team_ids": sorted(desired_team_set),
                "people": sorted(desired_email_set),
                "archived": bool(updated.get("archived")),
            }
            _record_event(
                conn, updated, "sharing_updated", authorization,
                before=before, after=after,
                detail={
                    "teams_added": sorted(added_team_ids),
                    "teams_removed": sorted(removed_team_ids),
                    "people_added": sorted(added_people),
                    "people_removed": sorted(removed_people),
                },
            )
        result = _snapshot(conn, updated, include_events=True)
        result.update({"subject": subject, "changed": True})
        return result


def set_archived(
    conn_factory: Callable[..., Any],
    authorization: Any,
    slug: Any,
    expected_revision: Any,
    archived: Any,
) -> dict[str, Any]:
    subject = _subject(authorization)
    if not isinstance(archived, bool) or isinstance(expected_revision, bool):
        raise ArtifactAccessError("INVALID_LIFECYCLE_CHANGE", "Archive state and revision are required.")
    try:
        revision = int(expected_revision)
    except (TypeError, ValueError):
        raise ArtifactAccessError("INVALID_LIFECYCLE_CHANGE", "Archive state and revision are required.") from None
    with conn_factory() as conn:
        with conn.transaction():
            artifact = _owner_artifact(conn, slug, subject, lock=True)
            if int(artifact.get("access_revision") or 1) != revision:
                raise ArtifactAccessError(
                    "ARTIFACT_ACCESS_CONFLICT",
                    "This artifact changed while it was open. Refresh before continuing.",
                    409,
                )
            if bool(artifact.get("archived")) == archived:
                result = _snapshot(conn, artifact, include_events=True)
                result.update({"subject": subject, "changed": False})
                return result
            before_snapshot = _snapshot(conn, artifact)
            updated = dict(conn.execute(
                "UPDATE rvbbit.dashboards SET archived=%s,"
                "archived_at=CASE WHEN %s THEN now() ELSE NULL END,"
                "archived_by=CASE WHEN %s THEN %s ELSE NULL END,"
                "access_revision=access_revision+1 WHERE id=%s "
                "RETURNING id,slug,name,description,owner_email,app_kind,runtime_kind,"
                "latest_version,archived,archived_at,archived_by,access_revision,updated_at",
                (archived, archived, archived, subject, artifact["id"]),
            ).fetchone())
            after_snapshot = _snapshot(conn, updated)
            _record_event(
                conn, updated, "artifact_archived" if archived else "artifact_restored",
                authorization,
                before={
                    "revision": artifact["access_revision"],
                    "archived": bool(artifact.get("archived")),
                    "sharing": before_snapshot["summary"],
                },
                after={
                    "revision": updated["access_revision"],
                    "archived": archived,
                    "sharing": after_snapshot["summary"],
                },
            )
        result = _snapshot(conn, updated, include_events=True)
        result.update({"subject": subject, "changed": True})
        return result
