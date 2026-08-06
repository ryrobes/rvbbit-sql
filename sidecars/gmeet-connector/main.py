"""Google Meet -> Rvbbit Brain connector.

The connector uses a Workspace service account with domain-wide delegation to
discover completed Meet conference records, retrieve generated transcripts,
and return the ordinary Brain ``/sync`` manifest contract.  It deliberately
polls the Meet REST API even when a deployment later adds Workspace Events /
Cloud Pub/Sub: Google expires structured transcript entries after 30 days and
recommends REST catch-up as the durable source of truth.

Credentials never enter Postgres.  Set ``GMEET_SA_KEY`` to a service-account
JSON file path or the JSON itself, then configure domain-wide delegation for:

* https://www.googleapis.com/auth/meetings.space.readonly
* https://www.googleapis.com/auth/calendar.events.owned.readonly (optional,
  but gives meetings their Calendar title and attendee metadata)
* https://www.googleapis.com/auth/admin.directory.user.readonly (optional when
  ``GMEET_SUBJECTS`` explicitly lists every user; enables domain discovery and
  maps Meet user IDs back to email addresses)
* https://www.googleapis.com/auth/drive.meet.readonly (recommended; gives the
  generated transcript's canonical Drive ACL and file metadata)
* https://www.googleapis.com/auth/meetings.space.settings (optional; only used
  when an administrator explicitly opts into auto-transcription for upcoming
  Calendar-created meeting spaces)

The connector is strict-default-deny.  Its default ``calendar_invitees_strict``
policy grants the generated Brain document only to the meeting organizer and
exact Calendar invitee email addresses.  If the Calendar event cannot be
resolved, access fails closed to the known organizer (or nobody).  Broader
Drive/group/domain grants never widen that policy.  Legacy ``drive`` and
``drive_and_calendar`` modes remain explicit opt-ins for installations that
prefer Google Drive's artifact ACL semantics.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Iterable

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field


MEET_SCOPE = "https://www.googleapis.com/auth/meetings.space.readonly"
MEET_SETTINGS_SCOPE = "https://www.googleapis.com/auth/meetings.space.settings"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.owned.readonly"
DIRECTORY_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"
DRIVE_MEET_SCOPE = "https://www.googleapis.com/auth/drive.meet.readonly"

EXPECTED_TOKEN = os.environ.get("CONNECTOR_TOKEN", "").strip()
SA_KEY = (
    os.environ.get("GMEET_SA_KEY", "").strip()
    or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
)
MEETING_CODE_RE = re.compile(r"(?<![a-z])([a-z]{3}-[a-z]{4}-[a-z]{3})(?![a-z])", re.I)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

app = FastAPI(title="Rvbbit Google Meet Brain Connector")


class SyncRequest(BaseModel):
    source_id: int | None = None
    folders: list[str] = Field(default_factory=list)  # shared connector contract; unused here
    cursor: str | None = None
    known: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                decoded = json.loads(stripped)
                raw = decoded if isinstance(decoded, list) else [stripped]
            except json.JSONDecodeError:
                raw = re.split(r"[,\n]", stripped)
        else:
            raw = re.split(r"[,\n]", stripped)
    else:
        raw = []
    return list(dict.fromkeys(str(item).strip().lower() for item in raw if str(item).strip()))


def _email(value: Any) -> str | None:
    candidate = str(value or "").strip().lower()
    return candidate if EMAIL_RE.match(candidate) else None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _meeting_code(*values: Any) -> str | None:
    for value in values:
        match = MEETING_CODE_RE.search(str(value or ""))
        if match:
            return match.group(1).lower()
    return None


@lru_cache(maxsize=1)
def _credential_info() -> dict[str, Any]:
    if not SA_KEY:
        raise RuntimeError("GMEET_SA_KEY (or GOOGLE_APPLICATION_CREDENTIALS) is not set")
    if SA_KEY.lstrip().startswith("{"):
        info = json.loads(SA_KEY)
    else:
        with open(SA_KEY, encoding="utf-8") as handle:
            info = json.load(handle)
    if info.get("type") != "service_account":
        raise RuntimeError("Google Meet domain sync requires a service_account credential")
    if not info.get("client_email") or not info.get("private_key"):
        raise RuntimeError("service-account JSON is missing client_email or private_key")
    return info


@lru_cache(maxsize=2048)
def _service(api: str, version: str, subject: str, scopes: tuple[str, ...]):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        _credential_info(), scopes=list(scopes)
    ).with_subject(subject)
    return build(api, version, credentials=creds, cache_discovery=False)


def _execute(request):
    return request.execute(num_retries=3)


def _settings(options: dict[str, Any]) -> dict[str, Any]:
    subjects = _as_list(options.get("subjects")) or _as_list(os.environ.get("GMEET_SUBJECTS", ""))
    admin_subject = _email(options.get("admin_subject")) or _email(os.environ.get("GMEET_ADMIN_SUBJECT"))
    domain = str(options.get("domain") or os.environ.get("GMEET_DOMAIN") or "").strip().lower()
    lookback_days = _as_int(
        options.get("lookback_days", os.environ.get("GMEET_LOOKBACK_DAYS")), 29, 1, 29
    )
    max_subjects = _as_int(
        options.get("max_subjects", os.environ.get("GMEET_MAX_SUBJECTS")), 500, 1, 5000
    )
    discover_default = bool(admin_subject)
    discover_users = _as_bool(
        options.get("discover_users", os.environ.get("GMEET_DISCOVER_USERS")), discover_default
    )
    auto_transcribe = _as_bool(
        options.get("auto_transcribe", os.environ.get("GMEET_AUTO_TRANSCRIBE")), False
    )
    auto_transcribe_days = _as_int(
        options.get("auto_transcribe_days", os.environ.get("GMEET_AUTO_TRANSCRIBE_DAYS")),
        7,
        1,
        30,
    )
    calendar_lookup = _as_bool(
        options.get("calendar_lookup", os.environ.get("GMEET_CALENDAR_LOOKUP")), True
    ) or auto_transcribe
    drive_acl = _as_bool(options.get("drive_acl", os.environ.get("GMEET_DRIVE_ACL")), True)
    acl_mode = str(
        options.get("acl_mode")
        or os.environ.get("GMEET_ACL_MODE")
        or "calendar_invitees_strict"
    ).strip().lower()
    if acl_mode not in {"calendar_invitees_strict", "drive", "drive_and_calendar"}:
        acl_mode = "calendar_invitees_strict"
    return {
        "subjects": subjects,
        "admin_subject": admin_subject,
        "domain": domain,
        "lookback_days": lookback_days,
        "max_subjects": max_subjects,
        "discover_users": discover_users,
        "calendar_lookup": calendar_lookup,
        "auto_transcribe": auto_transcribe,
        "auto_transcribe_days": auto_transcribe_days,
        "drive_acl": drive_acl,
        "acl_mode": acl_mode,
    }


def _directory_users(admin_subject: str, domain: str, maximum: int) -> list[dict[str, Any]]:
    svc = _service("admin", "directory_v1", admin_subject, (DIRECTORY_SCOPE,))
    users: list[dict[str, Any]] = []
    page_token = None
    while len(users) < maximum:
        kwargs: dict[str, Any] = {
            "maxResults": min(500, maximum - len(users)),
            "orderBy": "email",
            "projection": "basic",
            "pageToken": page_token,
        }
        if domain:
            kwargs["domain"] = domain
        else:
            kwargs["customer"] = "my_customer"
        response = _execute(svc.users().list(**kwargs))
        users.extend(
            user for user in response.get("users", [])
            if not user.get("suspended") and _email(user.get("primaryEmail"))
        )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return users[:maximum]


def _resolve_subjects(settings: dict[str, Any], warnings: list[str]) -> tuple[list[str], dict[str, str]]:
    subjects = list(settings["subjects"])
    user_id_to_email: dict[str, str] = {}
    admin_subject = settings.get("admin_subject")
    if settings["discover_users"] and admin_subject:
        try:
            users = _directory_users(admin_subject, settings["domain"], settings["max_subjects"])
            for user in users:
                email = _email(user.get("primaryEmail"))
                if not email:
                    continue
                subjects.append(email)
                user_id = str(user.get("id") or "").strip()
                if user_id:
                    user_id_to_email[user_id] = email
                for alias in user.get("aliases") or []:
                    alias_email = _email(alias)
                    if alias_email and user_id:
                        user_id_to_email.setdefault(user_id, alias_email)
        except Exception as exc:
            warnings.append(f"Directory discovery failed: {exc}")
    if not subjects and admin_subject:
        subjects.append(admin_subject)
    domain = settings["domain"]
    subjects = list(dict.fromkeys(
        email for email in (_email(value) for value in subjects)
        if email and (not domain or email.endswith("@" + domain))
    ))[: settings["max_subjects"]]
    if not subjects:
        raise RuntimeError(
            "no Workspace users to impersonate; set GMEET_ADMIN_SUBJECT for directory discovery "
            "or GMEET_SUBJECTS to a comma-separated email list"
        )
    return subjects, user_id_to_email


def _calendar_events(subject: str, cutoff: datetime, until: datetime) -> list[dict[str, Any]]:
    svc = _service("calendar", "v3", subject, (CALENDAR_SCOPE,))
    events: list[dict[str, Any]] = []
    page_token = None
    while True:
        response = _execute(svc.events().list(
            calendarId="primary",
            timeMin=_iso(cutoff),
            timeMax=_iso(until),
            singleEvents=True,
            showDeleted=False,
            maxResults=2500,
            pageToken=page_token,
        ))
        events.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return events


def _calendar_index(
    subjects: list[str], cutoff: datetime, until: datetime, warnings: list[str]
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str, str]] = set()
    observed = 0
    for subject in subjects:
        try:
            events = _calendar_events(subject, cutoff, until)
        except Exception as exc:
            warnings.append(f"Calendar lookup failed for {subject}: {exc}")
            continue
        observed += len(events)
        for event in events:
            urls = [event.get("hangoutLink")]
            for entry in (event.get("conferenceData") or {}).get("entryPoints") or []:
                urls.append(entry.get("uri"))
            code = _meeting_code(*urls, (event.get("conferenceData") or {}).get("conferenceId"))
            if not code:
                continue
            start = event.get("start") or {}
            key = (
                code,
                str(event.get("iCalUID") or event.get("id") or ""),
                str(start.get("dateTime") or start.get("date") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            by_code[code].append(event)
    for events in by_code.values():
        events.sort(key=lambda event: str(
            (event.get("start") or {}).get("dateTime")
            or (event.get("start") or {}).get("date")
            or ""
        ))
    return dict(by_code), observed


def _calendar_event_for_record(
    events: list[dict[str, Any]], record_start: Any
) -> dict[str, Any] | None:
    if not events:
        return None
    target = _parse_time(record_start)
    if target is None:
        return max(events, key=lambda event: str(event.get("updated") or ""))

    def distance(event: dict[str, Any]) -> float:
        start = event.get("start") or {}
        parsed = _parse_time(start.get("dateTime") or start.get("date"))
        return abs((parsed - target).total_seconds()) if parsed else float("inf")

    return min(events, key=distance)


def _ensure_auto_transcription(subject: str, meeting_code: str) -> bool:
    """Enable auto-transcription for one space; return True when it changed."""
    svc = _service("meet", "v2", subject, (MEET_SETTINGS_SCOPE,))
    space = _execute(svc.spaces().get(name=f"spaces/{meeting_code}"))
    transcription = (
        ((space.get("config") or {}).get("artifactConfig") or {}).get("transcriptionConfig")
        or {}
    )
    if str(transcription.get("autoTranscriptionGeneration") or "").upper() == "ON":
        return False
    name = str(space.get("name") or "").strip()
    if not name:
        raise RuntimeError(f"Google Meet returned no canonical space name for {meeting_code}")
    body = {
        "name": name,
        "config": {
            "artifactConfig": {
                "transcriptionConfig": {"autoTranscriptionGeneration": "ON"},
            },
        },
    }
    _execute(svc.spaces().patch(
        name=name,
        updateMask="config.artifactConfig.transcriptionConfig.autoTranscriptionGeneration",
        body=body,
    ))
    return True


def _configure_upcoming_transcriptions(
    events_by_code: dict[str, list[dict[str, Any]]],
    subjects: list[str],
    now: datetime,
    lookahead_days: int,
    warnings: list[str],
) -> dict[str, int]:
    stats = {"considered": 0, "enabled": 0, "already_enabled": 0, "errors": 0}
    subject_set = set(subjects)
    until = now + timedelta(days=lookahead_days)
    for code, events in sorted(events_by_code.items()):
        upcoming: list[tuple[datetime, dict[str, Any]]] = []
        for event in events:
            start = event.get("start") or {}
            starts_at = _parse_time(start.get("dateTime") or start.get("date"))
            if starts_at and now - timedelta(hours=12) <= starts_at <= until:
                upcoming.append((starts_at, event))
        if not upcoming:
            continue
        stats["considered"] += 1
        upcoming.sort(key=lambda item: item[0])
        organizer = next(
            (
                _email((event.get("organizer") or {}).get("email"))
                for _, event in upcoming
                if _email((event.get("organizer") or {}).get("email")) in subject_set
            ),
            None,
        )
        if not organizer:
            continue
        try:
            changed = _ensure_auto_transcription(organizer, code)
            stats["enabled" if changed else "already_enabled"] += 1
        except Exception as exc:
            stats["errors"] += 1
            warnings.append(f"Auto-transcription setup failed for {code}: {exc}")
    return stats


def _list_conferences(subject: str, cutoff: datetime, until: datetime) -> list[dict[str, Any]]:
    svc = _service("meet", "v2", subject, (MEET_SCOPE,))
    records: list[dict[str, Any]] = []
    page_token = None
    condition = f'start_time>="{_iso(cutoff)}" AND start_time<="{_iso(until)}"'
    while True:
        response = _execute(svc.conferenceRecords().list(
            filter=condition, pageSize=100, pageToken=page_token
        ))
        records.extend(response.get("conferenceRecords", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return records


def _list_transcripts(subject: str, conference: str) -> list[dict[str, Any]]:
    svc = _service("meet", "v2", subject, (MEET_SCOPE,))
    out: list[dict[str, Any]] = []
    page_token = None
    while True:
        response = _execute(svc.conferenceRecords().transcripts().list(
            parent=conference, pageSize=100, pageToken=page_token
        ))
        out.extend(response.get("transcripts", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return out


def _list_entries(subject: str, transcript: str) -> list[dict[str, Any]]:
    svc = _service("meet", "v2", subject, (MEET_SCOPE,))
    out: list[dict[str, Any]] = []
    page_token = None
    while True:
        response = _execute(svc.conferenceRecords().transcripts().entries().list(
            parent=transcript, pageSize=100, pageToken=page_token
        ))
        out.extend(response.get("transcriptEntries", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return out


def _list_participants(subject: str, conference: str) -> list[dict[str, Any]]:
    svc = _service("meet", "v2", subject, (MEET_SCOPE,))
    out: list[dict[str, Any]] = []
    page_token = None
    while True:
        response = _execute(svc.conferenceRecords().participants().list(
            parent=conference, pageSize=250, pageToken=page_token
        ))
        out.extend(response.get("participants", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return out


def _space(subject: str, name: str) -> dict[str, Any]:
    svc = _service("meet", "v2", subject, (MEET_SCOPE,))
    return _execute(svc.spaces().get(name=name))


def _drive_details(subject: str, document_id: str) -> tuple[dict[str, Any], list[str], list[dict[str, str]]]:
    svc = _service("drive", "v3", subject, (DRIVE_MEET_SCOPE,))
    file_meta = _execute(svc.files().get(
        fileId=document_id,
        supportsAllDrives=True,
        fields="id,name,mimeType,modifiedTime,version,webViewLink,owners(displayName,emailAddress)",
    ))
    emails: list[str] = []
    pending: list[dict[str, str]] = []
    page_token = None
    while True:
        response = _execute(svc.permissions().list(
            fileId=document_id,
            supportsAllDrives=True,
            pageSize=100,
            pageToken=page_token,
            fields="permissions(id,type,emailAddress,domain,role,deleted),nextPageToken",
        ))
        for permission in response.get("permissions", []):
            if permission.get("deleted"):
                continue
            kind = permission.get("type")
            if kind == "user":
                email = _email(permission.get("emailAddress"))
                if email:
                    emails.append(email)
            elif kind in {"group", "domain", "anyone"}:
                pending.append({
                    "folder_id": document_id,
                    "grant_kind": kind,
                    "grant_value": (
                        str(permission.get("emailAddress") or "")
                        if kind == "group"
                        else str(permission.get("domain") or ("anyone" if kind == "anyone" else ""))
                    ),
                })
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    for owner in file_meta.get("owners") or []:
        email = _email(owner.get("emailAddress"))
        if email:
            emails.append(email)
    return file_meta, sorted(set(emails)), pending


def _participant_identity(participant: dict[str, Any], user_id_to_email: dict[str, str]) -> dict[str, Any]:
    signed_in = participant.get("signedinUser") or participant.get("signedInUser") or {}
    anonymous = participant.get("anonymousUser") or {}
    phone = participant.get("phoneUser") or {}
    user_resource = str(signed_in.get("user") or "")
    user_id = user_resource.rsplit("/", 1)[-1] if user_resource else ""
    email = user_id_to_email.get(user_id)
    display = (
        signed_in.get("displayName")
        or anonymous.get("displayName")
        or phone.get("displayName")
        or email
        or "Unknown participant"
    )
    kind = "signed_in" if signed_in else ("anonymous" if anonymous else ("phone" if phone else "unknown"))
    return {
        "resource": participant.get("name"),
        "googleUserId": user_id or None,
        "email": email,
        "displayName": str(display),
        "kind": kind,
        "earliestStartTime": participant.get("earliestStartTime"),
        "latestEndTime": participant.get("latestEndTime"),
    }


def _calendar_people(event: dict[str, Any] | None) -> tuple[str | None, list[dict[str, Any]]]:
    event = event or {}
    organizer = _email((event.get("organizer") or {}).get("email"))
    attendees: list[dict[str, Any]] = []
    for attendee in event.get("attendees") or []:
        email = _email(attendee.get("email"))
        if not email:
            continue
        attendees.append({
            "email": email,
            "displayName": attendee.get("displayName"),
            "responseStatus": attendee.get("responseStatus"),
            "organizer": bool(attendee.get("organizer")),
            "self": bool(attendee.get("self")),
        })
    attendees.sort(key=lambda row: str(row.get("email") or ""))
    return organizer, attendees


def _speaker_label(identity: dict[str, Any] | None, participant_resource: str) -> str:
    if not identity:
        return participant_resource.rsplit("/", 1)[-1] or "Unknown participant"
    name = str(identity.get("displayName") or "").strip()
    email = _email(identity.get("email"))
    if name and email and name.lower() != email:
        return f"{name} <{email}>"
    return name or email or "Unknown participant"


def _format_markdown(
    title: str,
    record: dict[str, Any],
    props: dict[str, Any],
    entries: list[dict[str, Any]],
    participant_map: dict[str, dict[str, Any]],
) -> str:
    lines = [f"# {title}", "", "## Meeting", ""]
    facts = [
        ("Started", record.get("startTime")),
        ("Ended", record.get("endTime")),
        ("Organizer", props.get("organizerEmail")),
        ("Google Meet", props.get("meetingUri")),
        ("Calendar", props.get("calendarUrl")),
        ("Transcript", props.get("transcriptUri")),
    ]
    for label, value in facts:
        if value:
            lines.append(f"- **{label}:** {value}")
    attendee_emails = [row.get("email") for row in props.get("attendees") or [] if row.get("email")]
    if attendee_emails:
        lines.append(f"- **Invited:** {', '.join(attendee_emails)}")
    participant_labels = [
        _speaker_label(row, str(row.get("resource") or ""))
        for row in props.get("participants") or []
    ]
    if participant_labels:
        lines.append(f"- **Participants:** {', '.join(dict.fromkeys(participant_labels))}")
    lines.extend(["", "## Transcript", ""])
    for entry in sorted(entries, key=lambda row: str(row.get("startTime") or "")):
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        resource = str(entry.get("participant") or "")
        speaker = _speaker_label(participant_map.get(resource), resource)
        started = str(entry.get("startTime") or "")
        stamp = started[11:19] + " UTC" if len(started) >= 19 else started
        prefix = f"**{speaker}"
        if stamp:
            prefix += f" · {stamp}"
        lines.append(f"{prefix}:** {text}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _hash_document(body: str, props: dict[str, Any]) -> str:
    stable_props = {
        key: props.get(key)
        for key in (
            "title", "startTime", "endTime", "organizerEmail", "attendees",
            "participants", "meetingUri", "calendarEventId", "transcriptDocumentId",
            "transcriptState", "driveVersion",
        )
    }
    payload = body + "\n" + json.dumps(stable_props, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate_subjects(
    organizer: str | None, viewers: Iterable[str], fallback: list[str]
) -> list[str]:
    return list(dict.fromkeys(
        value for value in ([organizer] if organizer else []) + list(viewers) + fallback
        if value
    ))


def _sync(req: SyncRequest) -> dict[str, Any]:
    settings = _settings(req.options)
    warnings: list[str] = []
    subjects, user_id_to_email = _resolve_subjects(settings, warnings)
    now = _now()
    cutoff = now - timedelta(days=settings["lookback_days"])
    calendar_until = now + timedelta(days=(
        settings["auto_transcribe_days"] if settings["auto_transcribe"] else 1
    ))

    calendar_by_code: dict[str, list[dict[str, Any]]] = {}
    calendar_events_seen = 0
    if settings["calendar_lookup"]:
        calendar_by_code, calendar_events_seen = _calendar_index(
            subjects, cutoff, calendar_until, warnings
        )
    auto_transcription_stats = {
        "considered": 0,
        "enabled": 0,
        "already_enabled": 0,
        "errors": 0,
    }
    if settings["auto_transcribe"]:
        auto_transcription_stats = _configure_upcoming_transcriptions(
            calendar_by_code,
            subjects,
            now,
            settings["auto_transcribe_days"],
            warnings,
        )

    records: dict[str, dict[str, Any]] = {}
    record_viewers: dict[str, set[str]] = defaultdict(set)
    subjects_polled = 0
    records_seen = 0
    for subject in subjects:
        try:
            found = _list_conferences(subject, cutoff, now)
            subjects_polled += 1
        except Exception as exc:
            warnings.append(f"Meet conference lookup failed for {subject}: {exc}")
            continue
        records_seen += len(found)
        for record in found:
            name = str(record.get("name") or "").strip()
            if not name:
                continue
            records.setdefault(name, record)
            record_viewers[name].add(subject)

    if subjects_polled == 0:
        raise RuntimeError("Meet API failed for every delegated user: " + "; ".join(warnings[-5:]))

    files: list[dict[str, Any]] = []
    pending: list[dict[str, str]] = []
    pending_keys: set[tuple[str, str, str]] = set()
    meetings_with_transcript = 0
    meetings_without_transcript = 0
    transcript_files = 0
    acl_calendar_unresolved = 0
    drive_grants_ignored = 0

    for conference_name, record in sorted(records.items(), key=lambda item: str(item[1].get("startTime") or "")):
        viewers = sorted(record_viewers[conference_name])
        space_info: dict[str, Any] = {}
        for subject in viewers:
            try:
                space_info = _space(subject, str(record.get("space") or ""))
                break
            except Exception:
                continue
        code = _meeting_code(space_info.get("meetingCode"), space_info.get("meetingUri"))
        calendar_event = _calendar_event_for_record(
            calendar_by_code.get(code or "", []), record.get("startTime")
        )
        organizer, attendees = _calendar_people(calendar_event)
        candidates = _candidate_subjects(organizer, viewers, subjects[:1])

        transcripts: list[dict[str, Any]] = []
        transcript_subject: str | None = None
        transcript_error: Exception | None = None
        for subject in candidates:
            try:
                transcripts = _list_transcripts(subject, conference_name)
                transcript_subject = subject
                break
            except Exception as exc:
                transcript_error = exc
        generated = [
            row for row in transcripts
            if str(row.get("state") or "").upper() in {"FILE_GENERATED", ""}
        ]
        if not generated:
            meetings_without_transcript += 1
            if transcript_error and not transcripts:
                warnings.append(f"Transcript lookup failed for {conference_name}: {transcript_error}")
            continue
        meetings_with_transcript += 1

        try:
            participants_raw = _list_participants(transcript_subject or candidates[0], conference_name)
        except Exception as exc:
            participants_raw = []
            warnings.append(f"Participant lookup failed for {conference_name}: {exc}")
        participants = sorted(
            (_participant_identity(row, user_id_to_email) for row in participants_raw),
            key=lambda row: (
                str(row.get("email") or ""),
                str(row.get("displayName") or ""),
                str(row.get("resource") or ""),
            ),
        )
        participant_map = {
            str(row.get("resource")): row for row in participants if row.get("resource")
        }

        for transcript in generated:
            transcript_name = str(transcript.get("name") or "").strip()
            if not transcript_name:
                continue
            try:
                entries = _list_entries(transcript_subject or candidates[0], transcript_name)
            except Exception as exc:
                warnings.append(f"Transcript entries failed for {transcript_name}: {exc}")
                continue
            destination = transcript.get("docsDestination") or {}
            document_id = str(destination.get("document") or "").strip()
            drive_meta: dict[str, Any] = {}
            drive_emails: list[str] = []
            drive_pending: list[dict[str, str]] = []
            if settings["drive_acl"] and document_id:
                for subject in candidates:
                    try:
                        drive_meta, drive_emails, drive_pending = _drive_details(subject, document_id)
                        break
                    except Exception:
                        continue
                if not drive_meta:
                    warnings.append(f"Drive ACL unavailable for transcript document {document_id}")
            if settings["acl_mode"] == "calendar_invitees_strict":
                # Drive grants are useful audit evidence, but they must never
                # widen an invitee-only meeting policy (including via later
                # approval of a pending group/domain/anyone grant).
                drive_grants_ignored += len(drive_pending)
            else:
                for grant in drive_pending:
                    key = (grant["folder_id"], grant["grant_kind"], grant["grant_value"])
                    if key not in pending_keys:
                        pending_keys.add(key)
                        pending.append(grant)

            drive_owner = next(
                (
                    _email(owner.get("emailAddress"))
                    for owner in drive_meta.get("owners") or []
                    if _email(owner.get("emailAddress"))
                ),
                None,
            )
            canonical_organizer = organizer or drive_owner

            if settings["acl_mode"] == "calendar_invitees_strict":
                exact_emails = [row["email"] for row in attendees if row.get("email")]
                if canonical_organizer:
                    exact_emails.append(canonical_organizer)
                if calendar_event is None:
                    acl_calendar_unresolved += 1
                    warnings.append(
                        f"Calendar event unresolved for {conference_name}; "
                        "transcript access restricted to the known organizer"
                    )
            else:
                exact_emails = list(drive_emails)
                if not exact_emails:
                    exact_emails.extend(viewers)
                    if canonical_organizer:
                        exact_emails.append(canonical_organizer)
            if settings["acl_mode"] == "drive_and_calendar":
                exact_emails.extend(row["email"] for row in attendees if row.get("email"))
            exact_emails = sorted(set(email for email in (_email(v) for v in exact_emails) if email))

            fallback_title = str(drive_meta.get("name") or "").strip()
            title = str((calendar_event or {}).get("summary") or fallback_title or "Google Meet transcript").strip()
            transcript_uri = destination.get("exportUri") or drive_meta.get("webViewLink")
            meeting_uri = space_info.get("meetingUri") or (
                f"https://meet.google.com/{code}" if code else None
            )
            props: dict[str, Any] = {
                "provider": "google_meet",
                "docType": "meeting",
                "title": title,
                "conferenceRecord": conference_name,
                "space": record.get("space"),
                "meetingCode": code,
                "meetingUri": meeting_uri,
                "startTime": record.get("startTime"),
                "endTime": record.get("endTime"),
                "expireTime": record.get("expireTime"),
                "organizerEmail": canonical_organizer,
                "attendees": attendees,
                "participants": participants,
                "calendarEventId": (calendar_event or {}).get("id"),
                "calendarUrl": (calendar_event or {}).get("htmlLink"),
                "transcript": transcript_name,
                "transcriptState": transcript.get("state"),
                "transcriptStartTime": transcript.get("startTime"),
                "transcriptEndTime": transcript.get("endTime"),
                "transcriptDocumentId": document_id or None,
                "transcriptUri": transcript_uri,
                "driveModifiedTime": drive_meta.get("modifiedTime"),
                "driveVersion": drive_meta.get("version"),
                "observedBy": viewers,
                "aclMode": settings["acl_mode"],
                "calendarAclResolved": calendar_event is not None,
            }
            body = _format_markdown(title, record, props, entries, participant_map)
            content_hash = _hash_document(body, props)
            uri = "gmeet:" + transcript_name
            row: dict[str, Any] = {
                "uri": uri,
                "title": title,
                "rel_path": "/meetings/" + str(record.get("startTime") or "unknown")[:7],
                "folder_id": document_id or transcript_name,
                "mime": "text/markdown",
                "modified_at": (
                    drive_meta.get("modifiedTime")
                    or transcript.get("endTime")
                    or record.get("endTime")
                    or record.get("startTime")
                ),
                "occurred_at": record.get("startTime"),
                "author": canonical_organizer,
                "content_hash": content_hash,
                "permissions": exact_emails,
                "props": {key: value for key, value in props.items() if value is not None},
            }
            if req.known.get(uri) != content_hash:
                row["body"] = body
            files.append(row)
            transcript_files += 1

    stats = {
        "provider": "google_meet",
        "lookback_days": settings["lookback_days"],
        "retention_days": 30,
        "subjects_configured": len(subjects),
        "subjects_polled": subjects_polled,
        "calendar_events_seen": calendar_events_seen,
        "conference_records_seen": records_seen,
        "conference_records_unique": len(records),
        "meetings_with_transcript": meetings_with_transcript,
        "meetings_without_transcript": meetings_without_transcript,
        "transcript_files": transcript_files,
        "pending_grants": len(pending),
        "acl_calendar_unresolved": acl_calendar_unresolved,
        "drive_grants_ignored": drive_grants_ignored,
        "warnings": len(warnings),
        "warning_samples": warnings[:20],
        "acl_mode": settings["acl_mode"],
        "auto_transcribe": settings["auto_transcribe"],
        "auto_transcription": auto_transcription_stats,
    }
    return {
        "files": files,
        "pending_grants": pending,
        "cursor": json.dumps({"synced_at": _iso(now), "cutoff": _iso(cutoff)}, separators=(",", ":")),
        "stats": stats,
    }


@app.get("/health")
def health(response: Response) -> dict[str, Any]:
    configured = bool(SA_KEY)
    admin_subject = _email(os.environ.get("GMEET_ADMIN_SUBJECT"))
    subjects = _as_list(os.environ.get("GMEET_SUBJECTS", ""))
    credential_type = None
    credential_email = None
    error = None
    if configured:
        try:
            info = _credential_info()
            credential_type = info.get("type")
            credential_email = info.get("client_email")
        except Exception as exc:
            error = str(exc)
    if error is None and not admin_subject and not subjects:
        error = "set GMEET_ADMIN_SUBJECT or GMEET_SUBJECTS"
    ok = configured and error is None
    if not ok:
        response.status_code = 503
    return {
        "ok": ok,
        "creds_configured": configured,
        "credential_type": credential_type,
        "service_account": credential_email,
        "admin_subject_configured": bool(admin_subject),
        "subjects_configured": len(subjects),
        "error": error,
    }


@app.post("/sync")
def sync(req: SyncRequest, authorization: str = Header(default="")) -> dict[str, Any]:
    if EXPECTED_TOKEN and authorization != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")
    try:
        return _sync(req)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Google Meet sync failed: {exc}") from exc
