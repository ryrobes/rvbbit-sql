"""Focused privacy, OAuth, sync, and Brief contracts for Google Calendar v1."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import auth  # noqa: E402
import calliope  # noqa: E402


class _Result:
    def __init__(self, rows):
        self.rows = rows if isinstance(rows, list) else ([rows] if rows else [])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def test_calendar_schema_is_migrated_self_healing_and_private_by_owner():
    migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0237_calliope_google_calendar.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")

    assert "0237_calliope_google_calendar" in registry
    assert "owner_email text PRIMARY KEY" in migration
    assert "refresh_token_ciphertext text NOT NULL" in migration
    assert "calliope_private_calendar_edges" in migration
    assert "shared Brain" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_google_calendar_connections" in calliope._GOOGLE_CALENDAR_DDL
    assert "conn.execute(_GOOGLE_CALENDAR_DDL)" in backend
    assert "WAREHOUSE_GOOGLE_TOKEN_KEY" in backend
    assert "raw Google event payloads" in migration


def test_refresh_tokens_are_encrypted_and_private_events_are_redacted(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_GOOGLE_TOKEN_KEY", "calendar-test-key")
    token = "1//private-offline-refresh-token"
    ciphertext = calliope._encrypt_google_calendar_token(token)

    assert token not in ciphertext
    assert calliope._decrypt_google_calendar_token(ciphertext) == token

    private = calliope._normalize_google_calendar_event({
        "id": "private-1",
        "status": "confirmed",
        "visibility": "private",
        "summary": "Confidential acquisition discussion",
        "description": "Do not copy this into company memory",
        "location": "Secret room",
        "htmlLink": "https://calendar.google.com/private",
        "hangoutLink": "https://meet.google.com/private",
        "organizer": {"email": "ceo@example.com"},
        "attendees": [{"email": "lawyer@example.com"}],
        "start": {"dateTime": "2026-08-04T10:00:00-04:00"},
        "end": {"dateTime": "2026-08-04T11:00:00-04:00"},
    }, owner="owner@example.com")

    assert private["summary"] == "Private event"
    assert private["description"] == ""
    assert private["location"] == ""
    assert private["organizer"] == {}
    assert private["attendees"] == []
    assert private["html_link"] is None
    assert private["meeting_link"] is None


def test_normalized_event_keeps_only_bounded_brief_and_edge_fields():
    event = calliope._normalize_google_calendar_event({
        "id": "evt-1",
        "summary": "Customer renewal prep",
        "description": "Review the renewal risks with the account team.",
        "location": "Conference room 3",
        "updated": "2026-08-03T12:00:00Z",
        "start": {"dateTime": "2026-08-04T09:00:00-04:00"},
        "end": {"dateTime": "2026-08-04T09:30:00-04:00"},
        "organizer": {"email": "owner@example.com", "displayName": "Owner"},
        "attendees": [
            {"email": "owner@example.com", "self": True, "responseStatus": "accepted"},
            {"email": "ada@example.com", "displayName": "Ada", "responseStatus": "accepted"},
        ],
        "conferenceData": {"entryPoints": [{
            "entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij",
        }]},
        "ignoredRawField": {"must": "not survive"},
    }, owner="owner@example.com")

    assert event["starts_at"] == datetime(2026, 8, 4, 13, tzinfo=timezone.utc)
    assert event["response_status"] == "accepted"
    assert event["meeting_link"] == "https://meet.google.com/abc-defg-hij"
    assert event["attendees"][1]["email"] == "ada@example.com"
    assert "ignoredRawField" not in event


def test_calendar_observations_are_owner_filtered_and_emit_private_graph_refs():
    now = datetime(2026, 8, 3, 14, tzinfo=timezone.utc)
    queries = []
    connection = {
        "owner_email": "owner@example.com",
        "status": "connected",
        "last_error": None,
    }
    event = {
        "event_id": "evt-1",
        "summary": "Customer renewal prep",
        "description": "Review renewal risk.",
        "location": "Conference room 3",
        "html_link": "https://calendar.google.com/event?eid=1",
        "meeting_link": "https://meet.google.com/abc",
        "organizer": {"email": "owner@example.com", "display_name": "Owner"},
        "attendees": [
            {"email": "owner@example.com", "self": True},
            {"email": "ada@example.com", "display_name": "Ada"},
        ],
        "starts_at": now + timedelta(hours=2),
        "ends_at": now + timedelta(hours=3),
        "status": "confirmed",
        "event_type": "default",
        "all_day": False,
    }

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            queries.append((query, params))
            if "calendar_connections" in query:
                return _Result(connection)
            return _Result([event])

    items, coverage, warnings = calliope._brief_calendar_observations(
        Connection,
        "owner@example.com",
        now - timedelta(days=1),
        now + timedelta(days=14),
        now,
        ZoneInfo("UTC"),
    )

    assert warnings == []
    assert len(items) == 1
    assert items[0]["provenance"]["brief_section"] == "coming_up"
    assert items[0]["provenance"]["viewer_relation"]["truth"] == "observed"
    assert items[0]["provenance"]["feedback_allowed"] is False
    assert {edge["kind"] for edge in items[0]["provenance"]["entity_refs"]} == {"person", "place"}
    assert all(params[0] == "owner@example.com" for _query, params in queries)
    assert "response_status,'')<>'declined'" in queries[1][0]
    assert coverage[0]["scope"] == "personal"


def test_expired_incremental_token_falls_back_to_full_sync(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(calliope, "_google_calendar_connection", lambda *_args: {
        "status": "connected",
        "sync_token": "stale-token",
        "last_synced_at": now - timedelta(hours=1),
        "last_full_sync_at": now - timedelta(days=1),
    })

    async def access(*_args):
        return "short-lived-access-token"

    calls = []

    async def fetch(_access, *, sync_token, now):
        calls.append(sync_token)
        if sync_token:
            raise calliope._GoogleCalendarSyncTokenExpired("gone")
        return [({"id": "evt-1", "status": "cancelled"}, "UTC")], "fresh-token"

    persisted = {}

    def persist(_factory, owner, items, token, *, full_sync):
        persisted.update(owner=owner, items=items, token=token, full_sync=full_sync)

    monkeypatch.setattr(calliope, "_google_calendar_access_token", access)
    monkeypatch.setattr(calliope, "_fetch_google_calendar_events", fetch)
    monkeypatch.setattr(calliope, "_persist_google_calendar_events", persist)
    monkeypatch.setattr(calliope, "_google_calendar_status", lambda *_args: {"connected": True})

    result = asyncio.run(calliope._sync_google_calendar(object(), "owner@example.com", force=True))

    assert result == {"connected": True}
    assert calls == ["stale-token", None]
    assert persisted == {
        "owner": "owner@example.com",
        "items": [({"id": "evt-1", "status": "cancelled"}, "UTC")],
        "token": "fresh-token",
        "full_sync": True,
    }


def test_disabled_calendar_api_error_is_actionable(monkeypatch):
    class Response:
        status_code = 403

        @staticmethod
        def json():
            return {
                "error": {
                    "code": 403,
                    "message": (
                        "Google Calendar API has not been used in project "
                        "488166772690 before or it is disabled."
                    ),
                    "errors": [{"reason": "accessNotConfigured"}],
                    "details": [{
                        "reason": "SERVICE_DISABLED",
                        "metadata": {
                            "consumer": "projects/488166772690",
                            "service": "calendar-json.googleapis.com",
                        },
                    }],
                }
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(calliope.httpx, "AsyncClient", Client)

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(calliope._fetch_google_calendar_events(
            "short-lived-access-token",
            sync_token=None,
            now=datetime(2026, 8, 3, tzinfo=timezone.utc),
        ))

    message = str(caught.value)
    assert "Google Calendar API is disabled" in message
    assert "Google Cloud project 488166772690" in message
    assert "retry sync" in message


def test_full_calendar_sync_uses_token_compatible_bounded_query(monkeypatch):
    requested = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "items": [],
                "timeZone": "America/New_York",
                "nextSyncToken": "fresh-incremental-token",
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, *_args, **kwargs):
            requested.update(kwargs.get("params") or {})
            return Response()

    monkeypatch.setattr(calliope.httpx, "AsyncClient", Client)

    items, token = asyncio.run(calliope._fetch_google_calendar_events(
        "short-lived-access-token",
        sync_token=None,
        now=datetime(2026, 8, 3, tzinfo=timezone.utc),
    ))

    assert items == []
    assert token == "fresh-incremental-token"
    assert "timeMin" in requested and "timeMax" in requested
    assert "orderBy" not in requested


def test_calendar_oauth_is_incremental_single_use_and_account_bound(monkeypatch):
    routes = {}

    class MCP:
        @staticmethod
        def custom_route(path, methods):
            def register(handler):
                routes[(path, tuple(methods))] = handler
                return handler
            return register

    class Provider:
        public = "https://warehouse.example"

        @staticmethod
        def has_pending(_txn):
            return True

    monkeypatch.setattr(auth, "GOOGLE_CLIENT_ID", "test.apps.googleusercontent.com")
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(auth, "GOOGLE_HD", "example.com")
    monkeypatch.setattr(auth, "_GCAL_FLOWS", auth._GoogleCalendarFlows())
    monkeypatch.setattr(auth, "_GFLOWS", auth._GoogleFlows())
    monkeypatch.setattr(auth, "read_session_full", lambda _request: {
        "identity": "owner@example.com", "mapped": True, "via": "google",
    })
    grants = []

    async def grant(owner, payload):
        grants.append((owner, payload))

    auth.register_login_route(MCP(), Provider(), google_calendar_grant=grant)
    assert ("/auth/google/calendar/start", ("GET",)) in routes

    class Request:
        headers = {}
        client = None

        def __init__(self, query):
            self.query_params = query

    start = asyncio.run(routes[("/auth/google/calendar/start", ("GET",))](
        Request({"next": "/calliope?session=abc"})
    ))
    params = parse_qs(urlparse(start.headers["location"]).query)
    assert params["access_type"] == ["offline"]
    assert params["include_granted_scopes"] == ["true"]
    assert params["prompt"] == ["consent"]
    assert params["scope"] == [f"openid email {calliope._GOOGLE_CALENDAR_SCOPE}"]

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "id_token": "signed-token",
                "access_token": "access",
                "refresh_token": "refresh",
                "scope": f"openid email {calliope._GOOGLE_CALENDAR_SCOPE}",
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(auth, "google_domain_ok", lambda _claims: True)
    monkeypatch.setattr(auth, "_email_allowed", lambda _email: True)
    verified_email = {"value": "other@example.com"}
    monkeypatch.setattr(auth, "verify_google_id_token", lambda *_args: {
        "email": verified_email["value"], "email_verified": True,
    })
    callback = routes[("/auth/google/callback", ("GET",))]
    mismatch = asyncio.run(callback(Request({"state": params["state"][0], "code": "code"})))
    assert parse_qs(urlparse(mismatch.headers["location"]).query)["calendar"] == ["account_mismatch"]
    assert grants == []

    second = asyncio.run(routes[("/auth/google/calendar/start", ("GET",))](Request({})))
    second_state = parse_qs(urlparse(second.headers["location"]).query)["state"][0]
    verified_email["value"] = "owner@example.com"
    connected = asyncio.run(callback(Request({"state": second_state, "code": "code"})))
    assert parse_qs(urlparse(connected.headers["location"]).query)["calendar"] == ["connected"]
    assert grants[0][0] == "owner@example.com"
    replay = asyncio.run(callback(Request({"state": second_state, "code": "code"})))
    assert replay.status_code == 400


def test_calendar_ui_and_routes_are_gated_by_google_auth_configuration():
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    auth_source = (HERE / "auth.py").read_text(encoding="utf-8")
    server = (HERE / "server.py").read_text(encoding="utf-8")
    html = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")

    assert 'id="google-calendar-open"' in html and " hidden " in html
    assert 'payload["google_calendar"] = True' in backend
    assert "if google_calendar_enabled:" in backend
    assert '@mcp.custom_route("/api/calliope/calendar", methods=["GET"])' in backend
    assert 'if google_calendar_grant is not None:' in auth_source
    assert '@mcp.custom_route("/auth/google/calendar/start", methods=["GET"])' in auth_source
    assert "if auth.google_enabled() and calliope.CalliopeConfig.from_env().enabled" in server
    assert "els.calendarOpen.hidden = !enabled" in script
    assert "if (!state.config?.google_calendar) return \"\"" in script
    assert 'syncError ? "Retry sync"' in script
