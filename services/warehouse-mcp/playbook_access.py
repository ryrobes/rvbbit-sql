"""Identity-scoped, immutable Calliope Playbooks.

Playbooks are reusable semantic methods, not executable workflow graphs.  Every
root has an explicit ``cap_playbook`` capability policy and is therefore private
to its verified owner until that owner grants exact people or flat Teams.  Only
an approved immutable version is projected into capability discovery.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Callable

import application_teams


_CAPABILITY_RE = re.compile(r"^[a-z0-9][a-z0-9._~-]{0,199}$")
_READINESS = {"ready", "degraded", "blocked"}
_ARRAY_FIELDS = (
    "when_to_use",
    "triggers",
    "when_not_to_use",
    "context_to_gather",
    "method",
    "guardrails",
    "completion_criteria",
    "fallbacks",
    "required_capabilities",
    "preferred_capabilities",
    "optional_capabilities",
)
_REQUIRED_ARRAY_FIELDS = {"when_to_use", "method", "completion_criteria"}
_SOURCE_TURN_WINDOW = 12


class PlaybookError(Exception):
    """Typed error returned consistently by browser and MCP surfaces."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _auth_value(authorization: Any, name: str, default: Any = None) -> Any:
    if isinstance(authorization, dict):
        return authorization.get(name, default)
    return getattr(authorization, name, default)


def _email(value: Any, *, required: bool = True) -> str | None:
    if value in (None, "") and not required:
        return None
    try:
        return application_teams.normalize_email(value)
    except application_teams.TeamError as exc:
        raise PlaybookError(
            "APPLICATION_SUBJECT_REQUIRED",
            "Playbooks require a verified signed-in user.",
            403,
        ) from exc


def _subject(authorization: Any) -> str:
    return str(_email(_auth_value(authorization, "subject")))


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


def _bounded_text(value: Any, label: str, minimum: int, maximum: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not minimum <= len(text) <= maximum:
        raise PlaybookError(
            "INVALID_PLAYBOOK",
            f"{label} must contain between {minimum} and {maximum} characters.",
        )
    return text


def _string_list(value: Any, label: str, *, required: bool = False) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, (list, tuple)):
        raise PlaybookError("INVALID_PLAYBOOK", f"{label} must be a list of short strings.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise PlaybookError("INVALID_PLAYBOOK", f"Each {label} entry must be text.")
        text = re.sub(r"\s+", " ", item.strip())
        if not text:
            continue
        if len(text) > 800:
            raise PlaybookError("INVALID_PLAYBOOK", f"One {label} entry is too long.")
        if text not in result:
            result.append(text)
        if len(result) > 60:
            raise PlaybookError("INVALID_PLAYBOOK", f"{label} has too many entries.")
    if required and not result:
        raise PlaybookError("INVALID_PLAYBOOK", f"{label} must contain at least one entry.")
    return result


def normalize_contract(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        raise PlaybookError("INVALID_PLAYBOOK", "contract must be a structured object.")
    allowed = {"outcome", "deliverable", *_ARRAY_FIELDS}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PlaybookError(
            "INVALID_PLAYBOOK", "Unsupported contract fields: " + ", ".join(unknown[:12])
        )
    contract = {
        "outcome": _bounded_text(value.get("outcome"), "Outcome", 1, 1200),
        "deliverable": _bounded_text(value.get("deliverable"), "Deliverable", 1, 1200),
    }
    for field in _ARRAY_FIELDS:
        contract[field] = _string_list(
            value.get(field), field.replace("_", " "), required=field in _REQUIRED_ARRAY_FIELDS
        )
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 96_000:
        raise PlaybookError("INVALID_PLAYBOOK", "The Playbook contract is too large.")
    return contract


def _evidence(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PlaybookError("INVALID_PLAYBOOK", "evidence_refs must be a list.")
    if len(value) > 40:
        raise PlaybookError("INVALID_PLAYBOOK", "The Playbook has too many evidence references.")
    result: list[dict[str, Any]] = []
    allowed = {"kind", "ref_id", "label", "source", "version", "handle", "url", "surface_id"}
    for raw in value[:40]:
        if not isinstance(raw, dict):
            raise PlaybookError("INVALID_PLAYBOOK", "Each evidence reference must be an object.")
        item = {
            key: str(raw[key])[:1000]
            for key in allowed
            if raw.get(key) not in (None, "", [], {})
        }
        if item:
            result.append(item)
    if len(json.dumps(result).encode("utf-8")) > 48_000:
        raise PlaybookError("INVALID_PLAYBOOK", "The evidence reference set is too large.")
    return result


def _uuid(value: Any, code: str, message: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        raise PlaybookError(code, message) from None


def _reference(value: Any) -> tuple[str, str]:
    text = str(value or "").strip().lower()
    try:
        return "id", str(uuid.UUID(text))
    except (TypeError, ValueError):
        if not _CAPABILITY_RE.fullmatch(text):
            raise PlaybookError("INVALID_PLAYBOOK_REF", "Choose a valid Playbook reference.")
        return "capability_name", text


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:72]
    return text or "method"


def _surface_version(row: dict[str, Any]) -> str | None:
    """Return the immutable version pointer already carried by a Stage surface."""
    if row.get("artifact_version") not in (None, ""):
        return str(row["artifact_version"])
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    candidates = (
        payload.get("version"),
        (payload.get("playbook") or {}).get("version")
        if isinstance(payload.get("playbook"), dict) else None,
        (payload.get("sketch") or {}).get("revision")
        if isinstance(payload.get("sketch"), dict) else None,
        (payload.get("metric") or {}).get("version")
        if isinstance(payload.get("metric"), dict) else None,
    )
    return next((str(value) for value in candidates if value not in (None, "")), None)


def _pinned_source_evidence(
    conn: Any,
    session_id: str,
    anchor: dict[str, Any] | None,
    source_from_ordinal: int | None,
) -> list[dict[str, Any]]:
    """Build bounded opaque evidence pins without copying private source prose."""
    if not anchor or source_from_ordinal is None:
        return []
    pinned: list[dict[str, Any]] = [{
        "kind": "calliope_turn",
        "ref_id": str(anchor["id"]),
        "label": f"Notebook response {int(anchor['ordinal'])}",
        "source": "Calliope notebook",
        "version": str(int(anchor["ordinal"])),
    }]
    rows = conn.execute(
        "SELECT f.id::text,f.kind,f.title,f.artifact_version,f.payload "
        "FROM rvbbit.calliope_surfaces f JOIN rvbbit.calliope_turns t ON t.id=f.turn_id "
        "WHERE f.session_id=%s::uuid AND t.ordinal BETWEEN %s AND %s "
        "ORDER BY t.ordinal DESC,f.ordinal DESC,f.created_at DESC LIMIT 28",
        (session_id, source_from_ordinal, int(anchor["ordinal"])),
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        item = {
            "kind": str(row.get("kind") or "surface")[:1000],
            "ref_id": str(row["id"]),
            "surface_id": str(row["id"]),
            "label": str(row.get("title") or "Stage evidence")[:1000],
            "source": "Calliope Stage",
        }
        version = _surface_version(row)
        if version:
            item["version"] = version[:1000]
        pinned.append(item)
    return pinned


def _merge_evidence(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for group in groups:
        for item in group:
            key = (
                str(item.get("kind") or ""),
                str(item.get("surface_id") or item.get("ref_id") or ""),
                str(item.get("version") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= 40:
                return merged
    return merged


def _validate_source(
    conn: Any,
    subject: str,
    session_id: Any,
    sketch_id: Any = None,
    source_turn_id: Any = None,
) -> tuple[str, str | None, int | None, dict[str, Any] | None, int | None]:
    session = _uuid(session_id, "INVALID_SESSION", "Choose a valid source notebook.")
    row = conn.execute(
        "SELECT id FROM rvbbit.calliope_sessions "
        "WHERE id=%s::uuid AND lower(owner_email)=lower(%s)",
        (session, subject),
    ).fetchone()
    if not row:
        raise PlaybookError(
            "SOURCE_SESSION_NOT_FOUND",
            "The source notebook is not available to create or revise this Playbook.",
            404,
        )
    anchor = None
    # A browser Save-as-Playbook click records its chosen completed response on
    # the currently running distillation turn.  That server-owned binding wins
    # over model arguments, so the agent cannot accidentally pin a neighboring
    # response after the human clicked a specific one.
    enforced = conn.execute(
        "SELECT playbook_source_turn_id::text AS source_turn_id "
        "FROM rvbbit.calliope_turns WHERE session_id=%s::uuid AND status='running' "
        "AND playbook_source_turn_id IS NOT NULL ORDER BY ordinal DESC LIMIT 1",
        (session,),
    ).fetchone()
    requested_turn = (
        enforced.get("source_turn_id")
        if enforced and enforced.get("source_turn_id")
        else source_turn_id
    )
    if requested_turn not in (None, ""):
        turn_id = _uuid(
            requested_turn,
            "INVALID_SOURCE_TURN",
            "Choose a valid source response.",
        )
        anchor = conn.execute(
            "SELECT id::text,ordinal,status,turn_kind FROM rvbbit.calliope_turns "
            "WHERE id=%s::uuid AND session_id=%s::uuid AND turn_kind='chat' "
            "AND status IN ('running','complete','partial')",
            (turn_id, session),
        ).fetchone()
        if not anchor:
            raise PlaybookError(
                "SOURCE_TURN_NOT_FOUND",
                "The source response is not part of that owned notebook.",
                404,
            )
        anchor = dict(anchor)
    else:
        row = conn.execute(
            "SELECT id::text,ordinal,status,turn_kind FROM rvbbit.calliope_turns "
            "WHERE session_id=%s::uuid AND turn_kind='chat' "
            "AND status IN ('running','complete','partial') "
            "ORDER BY ordinal DESC LIMIT 1",
            (session,),
        ).fetchone()
        anchor = dict(row) if row else None

    source_from_ordinal = None
    if anchor:
        range_row = conn.execute(
            "SELECT min(ordinal)::int AS first_ordinal FROM ("
            " SELECT ordinal FROM rvbbit.calliope_turns WHERE session_id=%s::uuid "
            " AND turn_kind='chat' AND ordinal<=%s ORDER BY ordinal DESC LIMIT %s"
            ") recent",
            (session, int(anchor["ordinal"]), _SOURCE_TURN_WINDOW),
        ).fetchone()
        source_from_ordinal = int(range_row["first_ordinal"])

    sketch = None
    sketch_revision = None
    if sketch_id not in (None, ""):
        sketch = _uuid(sketch_id, "INVALID_SKETCH", "Choose a valid source Sketch.")
        found = conn.execute(
            "SELECT id,revision FROM rvbbit.calliope_sketches "
            "WHERE id=%s::uuid AND session_id=%s::uuid AND lower(owner_email)=lower(%s)",
            (sketch, session, subject),
        ).fetchone()
        if not found:
            raise PlaybookError(
                "SOURCE_SKETCH_NOT_FOUND",
                "The source Sketch is not part of that owned notebook.",
                404,
            )
        sketch_revision = int(found["revision"])
        revision_exists = conn.execute(
            "SELECT 1 FROM rvbbit.calliope_sketch_revisions "
            "WHERE sketch_id=%s::uuid AND revision=%s",
            (sketch, sketch_revision),
        ).fetchone()
        if not revision_exists:
            raise PlaybookError(
                "SOURCE_SKETCH_REVISION_NOT_FOUND",
                "The current source Sketch revision is not available.",
                409,
            )
    return session, sketch, sketch_revision, anchor, source_from_ordinal


def _root(conn: Any, reference: Any, *, lock: bool = False) -> dict[str, Any] | None:
    column, normalized = _reference(reference)
    suffix = " FOR UPDATE" if lock else ""
    cast = "::uuid" if column == "id" else ""
    row = conn.execute(
        "SELECT * FROM rvbbit.calliope_playbooks WHERE " + column + "=%s" + cast + suffix,
        (normalized,),
    ).fetchone()
    return dict(row) if row else None


def _owner_root(conn: Any, reference: Any, subject: str, *, lock: bool = False) -> dict[str, Any]:
    row = _root(conn, reference, lock=lock)
    if not row or str(row.get("owner_email") or "").lower() != subject:
        raise PlaybookError("PLAYBOOK_NOT_FOUND", "That Playbook is not available.", 404)
    return row


def _grants(conn: Any, root: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    params = (root["capability_kind"], root["capability_name"])
    teams = [dict(row) for row in conn.execute(
        "SELECT t.id::text,t.slug,t.name,t.description,t.system_key,t.archived "
        "FROM rvbbit.capability_access_grants g JOIN rvbbit.teams t ON t.id=g.team_id "
        "WHERE g.capability_kind=%s AND g.capability_name=%s "
        "ORDER BY CASE t.system_key WHEN 'everyone' THEN 0 WHEN 'admins' THEN 1 ELSE 2 END,t.name",
        params,
    ).fetchall()]
    people = [dict(row) for row in conn.execute(
        "SELECT g.principal_email AS email,p.display_name,p.last_seen_at "
        "FROM rvbbit.capability_access_grants g LEFT JOIN rvbbit.application_principals p "
        "ON p.email=g.principal_email WHERE g.capability_kind=%s AND g.capability_name=%s "
        "AND g.principal_email IS NOT NULL ORDER BY coalesce(p.display_name,g.principal_email)",
        params,
    ).fetchall()]
    return teams, people


def _audience(teams: list[dict[str, Any]], people: list[dict[str, Any]]) -> str:
    if any(team.get("system_key") == "everyone" for team in teams):
        return "Everyone signed in"
    pieces = []
    if teams:
        pieces.append(f"{len(teams)} Team{'s' if len(teams) != 1 else ''}")
    if people:
        pieces.append(f"{len(people)} person{'s' if len(people) != 1 else ''}")
    return "Only you" if not pieces else "Shared with " + " and ".join(pieces)


def _team_options(conn: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT t.id::text,t.slug,t.name,t.description,t.system_key,t.revision,"
        "CASE WHEN t.system_key='everyone' THEN NULL ELSE count(m.principal_email)::int END AS member_count "
        "FROM rvbbit.teams t LEFT JOIN rvbbit.team_members m ON m.team_id=t.id "
        "WHERE NOT t.archived GROUP BY t.id ORDER BY CASE t.system_key "
        "WHEN 'everyone' THEN 0 WHEN 'admins' THEN 1 ELSE 2 END,t.name"
    ).fetchall()]


def _record_event(
    conn: Any,
    root: dict[str, Any],
    event_type: str,
    authorization: Any,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    auth = _event_auth(authorization)
    conn.execute(
        "INSERT INTO rvbbit.capability_access_events "
        "(event_id,capability_kind,capability_name,event_type,credential_actor,human_subject,"
        "auth_mode,delegated,platform,session_ref,before_state,after_state,detail) "
        "VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)",
        (
            str(uuid.uuid4()), root["capability_kind"], root["capability_name"], event_type,
            auth["credential_actor"], auth["human_subject"], auth["auth_mode"],
            auth["delegated"], auth["platform"], auth["session_ref"],
            json.dumps(before or {}), json.dumps(after or {}), json.dumps(detail or {}),
        ),
    )


def _sync(conn_factory: Callable[..., Any], playbook_id: str) -> dict[str, Any] | None:
    try:
        with conn_factory() as conn:
            row = conn.execute(
                "SELECT rvbbit.sync_calliope_playbook_capability(%s::uuid) AS result",
                (playbook_id,),
            ).fetchone()
            return dict((row or {}).get("result") or {})
    except Exception as exc:  # indexing is repairable by capability_search's stale probe
        return {"status": "deferred", "message": str(exc)[:300]}


def _sync_inbox_handoff(
    conn: Any,
    root: dict[str, Any],
    *,
    version: int,
    title: str,
    synopsis: str,
    source_session_id: str | None,
    approved: bool,
) -> None:
    """Keep one personal Work Inbox receipt aligned with the Playbook root.

    The Inbox is a discoverability surface, not the Playbook source of truth.
    Older deployments can therefore use Playbooks before the service-owned
    Inbox DDL exists; once it does exist, the next draft or approval repairs the
    single deduplicated receipt.
    """
    present = conn.execute(
        "SELECT to_regclass('rvbbit.calliope_work_items') AS relation"
    ).fetchone()
    if not present or not present.get("relation"):
        return
    playbook_id = str(root["id"])
    status = "approved" if approved else "draft"
    context = {
        "playbook_id": playbook_id,
        "capability_name": root["capability_name"],
        "version": int(version),
        "status": status,
        "source_session_id": source_session_id,
        "open_url": f"/calliope?session={source_session_id}" if source_session_id else None,
    }
    conn.execute(
        "INSERT INTO rvbbit.calliope_work_items "
        "(id,owner_email,session_id,kind,source,source_ref,dedupe_key,title,summary,"
        "urgency,state,context,action_prompt) VALUES "
        "(%s::uuid,%s,%s::uuid,%s,'calliope_playbook',%s,%s,%s,%s,'normal','unread',"
        "%s::jsonb,%s) ON CONFLICT (owner_email,source,dedupe_key) DO UPDATE SET "
        "session_id=EXCLUDED.session_id,kind=EXCLUDED.kind,source_ref=EXCLUDED.source_ref,"
        "title=EXCLUDED.title,summary=EXCLUDED.summary,urgency=EXCLUDED.urgency,"
        "state='unread',context=EXCLUDED.context,action_prompt=EXCLUDED.action_prompt,"
        "updated_at=now(),seen_at=NULL,resolved_at=NULL",
        (
            str(uuid.uuid4()),
            root["owner_email"],
            source_session_id,
            "result" if approved else "suggestion",
            playbook_id,
            f"playbook:{playbook_id}",
            ("Playbook ready · " if approved else "Review Playbook · ") + title,
            synopsis,
            json.dumps(context, default=str),
            (
                f"Use, revise, or share the approved Playbook “{title}”."
                if approved
                else f"Review the private Playbook draft “{title}” and approve it only if the method is reusable."
            ),
        ),
    )


def _snapshot(
    conn: Any,
    root: dict[str, Any],
    subject: str,
    version: int | None = None,
    *,
    include_owner_detail: bool = False,
) -> dict[str, Any]:
    owner = str(root["owner_email"]).lower() == subject
    if root.get("archived") and not owner:
        raise PlaybookError("PLAYBOOK_NOT_FOUND", "That Playbook is not available.", 404)
    if not owner and not bool(conn.execute(
        "SELECT rvbbit.capability_can_use(%s,%s,%s) AS allowed",
        (root["capability_kind"], root["capability_name"], subject),
    ).fetchone()["allowed"]):
        raise PlaybookError("PLAYBOOK_NOT_FOUND", "That Playbook is not available.", 404)
    selected_version = version
    if selected_version is None:
        selected_version = int(root["latest_version"] if owner else (root.get("approved_version") or 0))
    if selected_version < 1 or (not owner and selected_version != root.get("approved_version")):
        raise PlaybookError("PLAYBOOK_NOT_FOUND", "That Playbook version is not available.", 404)
    version_row = conn.execute(
        "SELECT * FROM rvbbit.calliope_playbook_versions WHERE playbook_id=%s::uuid AND version=%s",
        (root["id"], selected_version),
    ).fetchone()
    if not version_row:
        raise PlaybookError("PLAYBOOK_NOT_FOUND", "That Playbook version is not available.", 404)
    record = dict(version_row)
    provenance_allowed = owner
    if not provenance_allowed and record.get("source_session_id"):
        provenance_allowed = bool(conn.execute(
            "SELECT rvbbit.calliope_session_can_view(%s::uuid,%s,false) AS allowed",
            (record["source_session_id"], subject),
        ).fetchone()["allowed"])
    result: dict[str, Any] = {
        "playbook": {
            "id": str(root["id"]),
            "capability_name": root["capability_name"],
            "owner_email": root["owner_email"],
            "title": record["title"],
            "synopsis": record["synopsis"],
            "readiness": record["readiness"],
            "version": int(record["version"]),
            "latest_version": int(root["latest_version"]),
            "approved_version": root.get("approved_version"),
            "approved": int(record["version"]) == root.get("approved_version"),
            "has_newer_draft": bool(root.get("approved_version") and root["latest_version"] > root["approved_version"]),
            "archived": bool(root.get("archived")),
            "access_revision": int(root.get("access_revision") or 1),
            "contract_hash": record["contract_hash"],
            "contract": record["semantic_contract"],
            "change_summary": record.get("change_summary") or "",
            # The diagram is part of the immutable Playbook documentation, not
            # merely a link back into its private source notebook.  Keep the
            # underlying Sketch identity private unless provenance is visible,
            # but let every authorized Playbook reader know that a pinned visual
            # snapshot is available through the Playbook-scoped endpoint.
            "has_sketch": bool(record.get("sketch_id") and record.get("sketch_revision")),
            "created_at": record.get("created_at"),
            "updated_at": root.get("updated_at"),
        },
        "subject": subject,
        "is_owner": owner,
    }
    if provenance_allowed:
        result["playbook"].update({
            "source_session_id": str(record["source_session_id"]) if record.get("source_session_id") else None,
            "source_turn_id": str(record["source_turn_id"]) if record.get("source_turn_id") else None,
            "source_from_ordinal": record.get("source_from_ordinal"),
            "source_through_ordinal": record.get("source_through_ordinal"),
            "sketch_id": str(record["sketch_id"]) if record.get("sketch_id") else None,
            "sketch_revision": record.get("sketch_revision"),
            "evidence_refs": record.get("evidence_refs") or [],
        })
    if owner and include_owner_detail:
        teams, people = _grants(conn, root)
        result["access"] = {
            "summary": _audience(teams, people),
            "private": not teams and not people,
            "everyone": any(team.get("system_key") == "everyone" for team in teams),
            "teams": teams,
            "people": people,
            "available_teams": _team_options(conn),
            "events": [dict(row) for row in conn.execute(
                "SELECT event_id::text,event_type,credential_actor,human_subject,auth_mode,delegated,"
                "platform,session_ref,before_state,after_state,detail,created_at "
                "FROM rvbbit.capability_access_events WHERE capability_kind=%s AND capability_name=%s "
                "ORDER BY created_at DESC,event_id DESC LIMIT 30",
                (root["capability_kind"], root["capability_name"]),
            ).fetchall()],
        }
    return result


def draft(
    conn_factory: Callable[..., Any],
    authorization: Any,
    *,
    session_id: Any,
    title: Any,
    synopsis: Any,
    contract: Any,
    readiness: Any = "ready",
    playbook_ref: Any = None,
    expected_version: Any = None,
    change_summary: Any = "",
    evidence_refs: Any = None,
    sketch_id: Any = None,
    source_turn_id: Any = None,
) -> dict[str, Any]:
    subject = _subject(authorization)
    normalized_title = _bounded_text(title, "Title", 1, 180)
    normalized_synopsis = _bounded_text(synopsis, "Synopsis", 1, 1200)
    normalized_contract = normalize_contract(contract)
    normalized_readiness = str(readiness or "ready").strip().lower()
    if normalized_readiness not in _READINESS:
        raise PlaybookError("INVALID_PLAYBOOK", "readiness must be ready, degraded, or blocked.")
    summary = re.sub(r"\s+", " ", str(change_summary or "").strip())[:1000]
    requested_evidence = _evidence(evidence_refs)
    encoded = json.dumps(normalized_contract, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    created = playbook_ref in (None, "")
    with conn_factory() as conn:
        with conn.transaction():
            (
                source_session,
                source_sketch,
                sketch_revision,
                source_turn,
                source_from_ordinal,
            ) = _validate_source(
                conn,
                subject,
                session_id,
                sketch_id,
                source_turn_id,
            )
            pinned_evidence = _pinned_source_evidence(
                conn,
                source_session,
                source_turn,
                source_from_ordinal,
            )
            evidence = _merge_evidence(pinned_evidence, requested_evidence)
            if created:
                if isinstance(expected_version, bool) or expected_version not in (None, 0):
                    raise PlaybookError("INVALID_VERSION", "A new Playbook has no expected version.")
                playbook_id = str(uuid.uuid4())
                capability_name = f"playbook.{_slug(normalized_title)}~{playbook_id[:8]}"
                conn.execute(
                    "INSERT INTO rvbbit.capability_access_policies "
                    "(capability_kind,capability_name,visibility,owner_email,created_by,updated_by) "
                    "VALUES ('cap_playbook',%s,'restricted',%s,%s,%s)",
                    (capability_name, subject, subject, subject),
                )
                root = dict(conn.execute(
                    "INSERT INTO rvbbit.calliope_playbooks "
                    "(id,capability_name,owner_email,source_session_id) "
                    "VALUES (%s::uuid,%s,%s,%s::uuid) RETURNING *",
                    (playbook_id, capability_name, subject, source_session),
                ).fetchone())
                version = 1
            else:
                root = _owner_root(conn, playbook_ref, subject, lock=True)
                if root.get("archived"):
                    raise PlaybookError("PLAYBOOK_ARCHIVED", "Restore this Playbook before revising it.", 409)
                if isinstance(expected_version, bool):
                    raise PlaybookError("INVALID_VERSION", "Read the latest version before revising.")
                try:
                    wanted = int(expected_version)
                except (TypeError, ValueError):
                    raise PlaybookError("INVALID_VERSION", "Read the latest version before revising.") from None
                if wanted != int(root["latest_version"]):
                    raise PlaybookError(
                        "PLAYBOOK_VERSION_CONFLICT",
                        "This Playbook changed while it was open. Read it again before revising.",
                        409,
                    )
                version = wanted + 1
                root = dict(conn.execute(
                    "UPDATE rvbbit.calliope_playbooks SET latest_version=%s,source_session_id=%s::uuid,"
                    "updated_at=now() WHERE id=%s::uuid RETURNING *",
                    (version, source_session, root["id"]),
                ).fetchone())
            conn.execute(
                "INSERT INTO rvbbit.calliope_playbook_versions "
                "(id,playbook_id,version,title,synopsis,readiness,semantic_contract,contract_hash,"
                "evidence_refs,source_session_id,source_turn_id,source_from_ordinal,"
                "source_through_ordinal,sketch_id,sketch_revision,change_summary,created_by) "
                "VALUES (%s::uuid,%s::uuid,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s::uuid,"
                "%s::uuid,%s,%s,%s::uuid,%s,%s,%s)",
                (
                    str(uuid.uuid4()), root["id"], version, normalized_title, normalized_synopsis,
                    normalized_readiness, encoded, digest, json.dumps(evidence), source_session,
                    source_turn.get("id") if source_turn else None,
                    source_from_ordinal,
                    int(source_turn["ordinal"]) if source_turn else None,
                    source_sketch,
                    sketch_revision,
                    summary,
                    subject,
                ),
            )
            _record_event(
                conn, root, "playbook_created" if created else "playbook_revised", authorization,
                before={} if created else {"latest_version": version - 1},
                after={"latest_version": version, "approved_version": root.get("approved_version")},
                detail={
                    "contract_hash": digest,
                    "source_session_id": source_session,
                    "source_turn_id": source_turn.get("id") if source_turn else None,
                    "source_from_ordinal": source_from_ordinal,
                    "source_through_ordinal": (
                        int(source_turn["ordinal"]) if source_turn else None
                    ),
                    "sketch_revision": sketch_revision,
                },
            )
            _sync_inbox_handoff(
                conn,
                root,
                version=version,
                title=normalized_title,
                synopsis=normalized_synopsis,
                source_session_id=source_session,
                approved=False,
            )
        fresh = _root(conn, root["id"])
        result = _snapshot(conn, fresh, subject, version, include_owner_detail=True)
    result["created"] = created
    result["indexed"] = False if not result["playbook"]["approved"] else True
    return result


def read(
    conn_factory: Callable[..., Any], subject: Any, playbook_ref: Any, version: Any = None
) -> dict[str, Any]:
    normalized_subject = str(_email(subject))
    selected = None
    if version is not None:
        if isinstance(version, bool):
            raise PlaybookError("INVALID_VERSION", "Choose a valid Playbook version.")
        try:
            selected = int(version)
        except (TypeError, ValueError):
            raise PlaybookError("INVALID_VERSION", "Choose a valid Playbook version.") from None
    with conn_factory() as conn:
        root = _root(conn, playbook_ref)
        if not root:
            raise PlaybookError("PLAYBOOK_NOT_FOUND", "That Playbook is not available.", 404)
        return _snapshot(conn, root, normalized_subject, selected, include_owner_detail=True)


def list_visible(
    conn_factory: Callable[..., Any],
    subject: Any,
    *,
    include_archived: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Return the person's owned and explicitly shared Playbooks.

    Owners see their latest immutable version, including drafts. Recipients see
    only the approved version that capability authorization already permits.
    This is the personal inventory used by Calliope; Library remains the wider
    administrative catalog.
    """
    normalized_subject = str(_email(subject))
    try:
        selected_limit = max(1, min(int(limit or 100), 200))
    except (TypeError, ValueError):
        selected_limit = 100
    with conn_factory() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT p.* FROM rvbbit.calliope_playbooks p WHERE "
            "(lower(p.owner_email)=lower(%s) AND (%s OR NOT p.archived)) OR "
            "(lower(p.owner_email)<>lower(%s) AND NOT p.archived "
            "AND p.approved_version IS NOT NULL "
            "AND rvbbit.capability_can_use(p.capability_kind,p.capability_name,%s)) "
            "ORDER BY CASE WHEN lower(p.owner_email)=lower(%s) THEN 0 ELSE 1 END,"
            "p.updated_at DESC LIMIT %s",
            (
                normalized_subject,
                bool(include_archived),
                normalized_subject,
                normalized_subject,
                normalized_subject,
                selected_limit,
            ),
        ).fetchall()]
        items: list[dict[str, Any]] = []
        counts = {"drafts": 0, "approved": 0, "shared": 0, "archived": 0, "total": 0}
        for root in rows:
            owner = str(root.get("owner_email") or "").lower() == normalized_subject
            try:
                item = _snapshot(
                    conn,
                    root,
                    normalized_subject,
                    include_owner_detail=False,
                )
            except PlaybookError:
                # Grants may change between the inventory query and snapshot.
                continue
            if root.get("archived"):
                category = "archived"
            elif not owner:
                category = "shared"
            elif int(root.get("latest_version") or 0) == int(root.get("approved_version") or -1):
                category = "approved"
            else:
                category = "drafts"
            item["category"] = category
            items.append(item)
            counts[category] += 1
        counts["total"] = len(items)
        return {"playbooks": items, "counts": counts}


def read_sketch(
    conn_factory: Callable[..., Any], subject: Any, playbook_ref: Any, version: Any = None
) -> dict[str, Any]:
    """Return the exact immutable Sketch revision pinned by a visible Playbook.

    Authorization deliberately follows the Playbook rather than the source
    notebook.  A shared approved Playbook therefore carries its visual
    documentation without disclosing the private notebook or live editable
    Sketch from which that documentation was learned.
    """
    normalized_subject = str(_email(subject))
    selected = None
    if version is not None:
        if isinstance(version, bool):
            raise PlaybookError("INVALID_VERSION", "Choose a valid Playbook version.")
        try:
            selected = int(version)
        except (TypeError, ValueError):
            raise PlaybookError("INVALID_VERSION", "Choose a valid Playbook version.") from None
    with conn_factory() as conn:
        root = _root(conn, playbook_ref)
        if not root:
            raise PlaybookError("PLAYBOOK_NOT_FOUND", "That Playbook is not available.", 404)
        visible = _snapshot(conn, root, normalized_subject, selected)
        visible_playbook = visible["playbook"]
        selected_version = int(visible_playbook["version"])
        row = conn.execute(
            "SELECT v.sketch_id,v.sketch_revision,s.title AS sketch_title,"
            "r.actor,r.actor_email,r.operation_count,r.change_summary,r.elements,"
            "r.app_state,r.created_at FROM rvbbit.calliope_playbook_versions v "
            "JOIN rvbbit.calliope_sketches s ON s.id=v.sketch_id "
            "JOIN rvbbit.calliope_sketch_revisions r "
            "ON r.sketch_id=v.sketch_id AND r.revision=v.sketch_revision "
            "WHERE v.playbook_id=%s::uuid AND v.version=%s",
            (root["id"], selected_version),
        ).fetchone()
        if not row:
            raise PlaybookError(
                "PLAYBOOK_SKETCH_NOT_FOUND",
                "That Playbook version does not have a pinned visual plan.",
                404,
            )
        item = dict(row)
        elements = item.get("elements") if isinstance(item.get("elements"), list) else []
        active_elements = [element for element in elements if not element.get("isDeleted")]
        created_at = item.get("created_at")
        return {
            "sketch": {
                # Present a Playbook-scoped identity to the browser.  The source
                # Sketch UUID stays an internal join key and cannot be reused to
                # probe the private live notebook endpoint.
                "id": f"playbook:{root['id']}:v{selected_version}",
                "title": str(visible_playbook.get("title") or item.get("sketch_title") or "Playbook plan"),
                "revision": int(item["sketch_revision"]),
                "element_count": len(active_elements),
                "last_actor": str(item.get("actor") or "calliope"),
                "last_actor_email": item.get("actor_email"),
                "last_operation_count": int(item.get("operation_count") or 0),
                "last_change_summary": item.get("change_summary") or {},
                "can_undo_calliope": False,
                "has_preview": False,
                "preview_url": None,
                "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
                "updated_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
                "scene": {
                    "elements": elements,
                    "appState": item.get("app_state") or {},
                    "files": {},
                },
            },
            "read_only": True,
            "presentation": True,
            "playbook": {
                "id": str(root["id"]),
                "title": visible_playbook.get("title"),
                "version": selected_version,
            },
        }


def approve(
    conn_factory: Callable[..., Any], authorization: Any, playbook_ref: Any, version: Any
) -> dict[str, Any]:
    subject = _subject(authorization)
    if isinstance(version, bool):
        raise PlaybookError("INVALID_VERSION", "Choose the latest Playbook version.")
    try:
        selected = int(version)
    except (TypeError, ValueError):
        raise PlaybookError("INVALID_VERSION", "Choose the latest Playbook version.") from None
    with conn_factory() as conn:
        with conn.transaction():
            root = _owner_root(conn, playbook_ref, subject, lock=True)
            if root.get("archived"):
                raise PlaybookError("PLAYBOOK_ARCHIVED", "Restore this Playbook before approving it.", 409)
            if selected != int(root["latest_version"]):
                raise PlaybookError("PLAYBOOK_VERSION_CONFLICT", "Only the latest draft can be approved.", 409)
            version_row = conn.execute(
                "SELECT title,synopsis,source_session_id::text AS source_session_id "
                "FROM rvbbit.calliope_playbook_versions WHERE playbook_id=%s::uuid AND version=%s",
                (root["id"], selected),
            ).fetchone()
            if not version_row:
                raise PlaybookError("PLAYBOOK_NOT_FOUND", "That Playbook version is not available.", 404)
            prior = root.get("approved_version")
            if prior != selected:
                root = dict(conn.execute(
                    "UPDATE rvbbit.calliope_playbooks SET approved_version=%s,approved_at=now(),"
                    "approved_by=%s,updated_at=now() WHERE id=%s::uuid RETURNING *",
                    (selected, subject, root["id"]),
                ).fetchone())
                _record_event(
                    conn, root, "playbook_approved", authorization,
                    before={"approved_version": prior}, after={"approved_version": selected},
                )
                _sync_inbox_handoff(
                    conn,
                    root,
                    version=selected,
                    title=str(version_row["title"]),
                    synopsis=str(version_row["synopsis"]),
                    source_session_id=version_row.get("source_session_id"),
                    approved=True,
                )
        fresh = _root(conn, root["id"])
        result = _snapshot(conn, fresh, subject, selected, include_owner_detail=True)
    result["changed"] = prior != selected
    result["capability_index"] = _sync(conn_factory, str(root["id"]))
    return result


def replace_access(
    conn_factory: Callable[..., Any],
    authorization: Any,
    playbook_ref: Any,
    expected_revision: Any,
    *,
    team_ids: Any = None,
    people: Any = None,
    confirm_everyone: bool = False,
) -> dict[str, Any]:
    subject = _subject(authorization)
    if isinstance(expected_revision, bool):
        raise PlaybookError("INVALID_REVISION", "Inspect the current Playbook revision first.")
    try:
        revision = int(expected_revision)
    except (TypeError, ValueError):
        raise PlaybookError("INVALID_REVISION", "Inspect the current Playbook revision first.") from None
    if not isinstance(team_ids if team_ids is not None else [], list):
        raise PlaybookError("INVALID_GRANTS", "Team grants must be a list.")
    desired_teams = list(dict.fromkeys(
        _uuid(item, "INVALID_GRANTS", "Choose valid Teams to share with.") for item in (team_ids or [])
    ))
    if not isinstance(people if people is not None else [], list):
        raise PlaybookError("INVALID_GRANTS", "People grants must be a list.")
    desired_people = []
    for item in people or []:
        person = str(_email(item))
        if person != subject and person not in desired_people:
            desired_people.append(person)
    with conn_factory() as conn:
        with conn.transaction():
            root = _owner_root(conn, playbook_ref, subject, lock=True)
            if int(root.get("access_revision") or 1) != revision:
                raise PlaybookError(
                    "PLAYBOOK_ACCESS_CONFLICT",
                    "Sharing changed while it was open. Read the Playbook again before saving.",
                    409,
                )
            team_rows = conn.execute(
                "SELECT id::text,name,system_key FROM rvbbit.teams "
                "WHERE id=ANY(%s::uuid[]) AND NOT archived",
                (desired_teams,),
            ).fetchall() if desired_teams else []
            if {str(row["id"]) for row in team_rows} != set(desired_teams):
                raise PlaybookError("TEAM_NOT_FOUND", "One selected Team is no longer available.", 409)
            if desired_people:
                observed = {str(row["email"]) for row in conn.execute(
                    "SELECT email FROM rvbbit.application_principals WHERE email=ANY(%s::text[])",
                    (desired_people,),
                ).fetchall()}
                missing = sorted(set(desired_people) - observed)
                if missing:
                    raise PlaybookError(
                        "PERSON_NOT_OBSERVED",
                        "These people have not signed in through a trusted application surface: "
                        + ", ".join(missing[:8]),
                    )
            current_teams, current_people = _grants(conn, root)
            old_team_ids = {str(item["id"]) for item in current_teams}
            old_people = {str(item["email"]) for item in current_people}
            new_team_ids, new_people = set(desired_teams), set(desired_people)
            adding_everyone = any(
                row.get("system_key") == "everyone" and str(row["id"]) not in old_team_ids
                for row in team_rows
            )
            if adding_everyone and confirm_everyone is not True:
                raise PlaybookError(
                    "EVERYONE_CONFIRMATION_REQUIRED",
                    "Confirm explicitly before sharing this Playbook with Everyone.",
                    409,
                )
            changed = old_team_ids != new_team_ids or old_people != new_people
            if changed:
                conn.execute(
                    "DELETE FROM rvbbit.capability_access_grants WHERE capability_kind=%s AND capability_name=%s",
                    (root["capability_kind"], root["capability_name"]),
                )
                for team_id in sorted(new_team_ids):
                    conn.execute(
                        "INSERT INTO rvbbit.capability_access_grants "
                        "(capability_kind,capability_name,team_id,granted_by) VALUES (%s,%s,%s::uuid,%s)",
                        (root["capability_kind"], root["capability_name"], team_id, subject),
                    )
                for person in sorted(new_people):
                    conn.execute(
                        "INSERT INTO rvbbit.capability_access_grants "
                        "(capability_kind,capability_name,principal_email,granted_by) VALUES (%s,%s,%s,%s)",
                        (root["capability_kind"], root["capability_name"], person, subject),
                    )
                root = dict(conn.execute(
                    "UPDATE rvbbit.calliope_playbooks SET access_revision=access_revision+1,updated_at=now() "
                    "WHERE id=%s::uuid RETURNING *",
                    (root["id"],),
                ).fetchone())
                conn.execute(
                    "UPDATE rvbbit.capability_access_policies SET revision=revision+1,updated_by=%s,updated_at=now() "
                    "WHERE capability_kind=%s AND capability_name=%s",
                    (subject, root["capability_kind"], root["capability_name"]),
                )
                _record_event(
                    conn, root, "playbook_sharing_updated", authorization,
                    before={"revision": revision, "team_ids": sorted(old_team_ids), "people": sorted(old_people)},
                    after={"revision": root["access_revision"], "team_ids": sorted(new_team_ids), "people": sorted(new_people)},
                )
        fresh = _root(conn, root["id"])
        result = _snapshot(conn, fresh, subject, include_owner_detail=True)
    result["changed"] = changed
    return result


def set_archived(
    conn_factory: Callable[..., Any],
    authorization: Any,
    playbook_ref: Any,
    expected_revision: Any,
    archived: Any,
) -> dict[str, Any]:
    subject = _subject(authorization)
    if not isinstance(archived, bool) or isinstance(expected_revision, bool):
        raise PlaybookError("INVALID_LIFECYCLE_CHANGE", "Archive state and revision are required.")
    try:
        revision = int(expected_revision)
    except (TypeError, ValueError):
        raise PlaybookError("INVALID_LIFECYCLE_CHANGE", "Archive state and revision are required.") from None
    with conn_factory() as conn:
        with conn.transaction():
            root = _owner_root(conn, playbook_ref, subject, lock=True)
            if int(root.get("access_revision") or 1) != revision:
                raise PlaybookError(
                    "PLAYBOOK_ACCESS_CONFLICT",
                    "This Playbook changed while it was open. Read it again before continuing.",
                    409,
                )
            changed = bool(root.get("archived")) != archived
            if changed:
                root = dict(conn.execute(
                    "UPDATE rvbbit.calliope_playbooks SET archived=%s,"
                    "archived_at=CASE WHEN %s THEN now() ELSE NULL END,"
                    "archived_by=CASE WHEN %s THEN %s ELSE NULL END,"
                    "access_revision=access_revision+1,updated_at=now() WHERE id=%s::uuid RETURNING *",
                    (archived, archived, archived, subject, root["id"]),
                ).fetchone())
                _record_event(
                    conn, root, "playbook_archived" if archived else "playbook_restored", authorization,
                    before={"revision": revision, "archived": not archived},
                    after={"revision": root["access_revision"], "archived": archived},
                )
        fresh = _root(conn, root["id"])
        result = _snapshot(conn, fresh, subject, include_owner_detail=True)
    result["changed"] = changed
    result["capability_index"] = _sync(conn_factory, str(root["id"]))
    return result
