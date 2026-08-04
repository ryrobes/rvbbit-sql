"""Focused contracts for Gallery presence and native artifact activity."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import calliope  # noqa: E402
import server  # noqa: E402


class _Result:
    def __init__(self, rows):
        self.rows = rows if isinstance(rows, list) else ([rows] if rows else [])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def _artifact_row(owner="owner@example.com"):
    return {
        "slug": "revenue-room",
        "name": "Revenue Room",
        "description": "A live view of revenue motion.",
        "owner_email": owner,
        "team": "Finance",
        "status": "live",
        "runtime_kind": "python-fastapi",
        "app_kind": "app",
        "latest_version": 4,
        "queries": 3,
        "tables": 2,
        "metrics": 1,
        "semantic_objects": 2,
        "updated_at": datetime.now(timezone.utc),
    }


def test_gallery_presence_and_shared_activity_are_native_but_unobtrusive(monkeypatch):
    monkeypatch.setattr(calliope, "is_enabled", lambda: True)

    owner_page = server._landing_html([_artifact_row()], "OWNER@example.com")
    reader_page = server._landing_html([_artifact_row()], "reader@example.com")

    assert 'id="gallery-presence"' in owner_page
    assert 'id="presence-meeting"' in owner_page
    assert 'id="artifact-activity-dialog"' in owner_page
    assert 'data-gallery-activity="revenue-room"' in owner_page
    assert 'data-gallery-activity="revenue-room"' in reader_page
    assert "shared Gallery insight" in owner_page
    assert "/api/gallery/meetings" in server._LANDING_JS
    assert "/api/gallery/artifacts/" in server._LANDING_JS
    assert ".gallery-presence.compact" in server._LANDING_CSS


def test_artifact_view_receipt_uses_human_identity_and_skips_prefetch(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        server,
        "_record",
        lambda tool, args, result, error, elapsed, caller_override=None: recorded.append(
            (tool, args, result, error, elapsed, caller_override)
        ),
    )

    class Request:
        method = "GET"
        path_params = {"slug": "revenue-room"}
        headers = {"referer": "https://warehouse.example/gallery"}

    document = {"slug": "revenue-room", "version": 4, "app_kind": "app"}
    server._record_artifact_view(
        Request(), document, "Person@Example.com", "google"
    )

    assert len(recorded) == 1
    tool, args, result, error, elapsed, caller = recorded[0]
    assert tool == "artifact_view"
    assert args == {
        "slug": "revenue-room",
        "version": 4,
        "app_kind": "app",
        "surface": "gallery",
        "auth_via": "google",
    }
    assert result["slug"] == "revenue-room"
    assert error is None and elapsed == 0
    assert caller == "Person@Example.com"

    Request.headers = {"purpose": "prefetch"}
    server._record_artifact_view(Request(), document, "Person@Example.com")
    assert len(recorded) == 1


def test_dashboard_route_records_google_identity_only_after_document_resolves(monkeypatch):
    import auth

    routes = {}

    class MCP:
        def custom_route(self, path, methods):
            def register(handler):
                routes[(path, tuple(methods))] = handler
                return handler
            return register

    monkeypatch.setattr(
        auth,
        "read_session_full",
        lambda _request: {
            "sub": "warehouse_role",
            "identity": "human@example.com",
            "mapped": True,
            "via": "google",
        },
    )
    monkeypatch.setattr(server, "_dashboard_version_document", lambda *_args: {
        "slug": "revenue-room",
        "version": 4,
        "latest_version": 4,
        "app_kind": "dashboard",
        "html": "<main>hello</main>",
    })
    receipts = []
    monkeypatch.setattr(
        server,
        "_record_artifact_view",
        lambda request, document, viewer, via: receipts.append(
            (request, document, viewer, via)
        ),
    )
    server.register_dashboard_routes(MCP())

    class URL:
        path = "/d/revenue-room"

    class Request:
        method = "GET"
        path_params = {"slug": "revenue-room"}
        headers = {}
        url = URL()

    response = asyncio.run(routes[("/d/{slug}", ("GET",))](Request()))

    assert response.status_code == 200
    assert receipts[0][1]["version"] == 4
    assert receipts[0][2:] == ("human@example.com", "google")


def test_activity_snapshot_is_shared_with_authenticated_viewers_and_fills_quiet_days(monkeypatch):
    today = datetime.now(timezone.utc).date()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            if "FROM rvbbit.dashboards" in query:
                return _Result({
                    "slug": "revenue-room",
                    "name": "Revenue Room",
                    "owner_email": "owner@example.com",
                    "app_kind": "app",
                    "latest_version": 4,
                })
            if "AS total_views" in query:
                return _Result({
                    "total_views": 12,
                    "unique_viewers": 4,
                    "window_views": 7,
                    "tracked_since": datetime.now(timezone.utc) - timedelta(days=20),
                    "last_viewed_at": datetime.now(timezone.utc) - timedelta(minutes=12),
                })
            if "GROUP BY 1 ORDER BY 1" in query:
                return _Result([
                    {"day": today - timedelta(days=1), "views": 3, "viewers": 2},
                    {"day": today, "views": 4, "viewers": 3},
                ])
            if "GROUP BY lower(caller)" in query:
                return _Result([{
                    "viewer": "owner@example.com",
                    "views": 5,
                    "active_days": 3,
                    "first_viewed_at": datetime.now(timezone.utc) - timedelta(days=20),
                    "last_viewed_at": datetime.now(timezone.utc),
                }])
            if "coalesce(nullif(args->>'version'" in query:
                return _Result([{"version": "4", "views": 12}])
            raise AssertionError(query)

    monkeypatch.setattr(server, "_conn", lambda: Connection())

    snapshot = server._artifact_activity_snapshot(
        "revenue-room", "OWNER@example.com", 7, "UTC"
    )

    assert snapshot["summary"] == {
        "total_views": 12,
        "unique_viewers": 4,
        "repeat_views": 8,
        "window_views": 7,
        "tracked_since": snapshot["summary"]["tracked_since"],
        "last_viewed_at": snapshot["summary"]["last_viewed_at"],
    }
    assert len(snapshot["series"]) == 7
    assert snapshot["series"][-2]["views"] == 3
    assert snapshot["series"][-1]["views"] == 4
    assert snapshot["series"][0]["views"] == 0
    assert snapshot["viewers"][0]["is_you"] is True

    reader_snapshot = server._artifact_activity_snapshot(
        "revenue-room", "reader@example.com", 30, "UTC"
    )
    assert reader_snapshot["summary"]["total_views"] == 12
    assert reader_snapshot["viewers"][0]["viewer"] == "owner@example.com"
    assert reader_snapshot["viewers"][0]["is_you"] is False


def test_upcoming_calendar_snapshot_refreshes_but_keeps_private_payload_small(monkeypatch):
    now = datetime.now(timezone.utc)
    connection = {
        "owner_email": "owner@example.com",
        "status": "connected",
        "last_synced_at": now,
    }
    syncs = []
    monkeypatch.setattr(
        calliope,
        "_google_calendar_connection",
        lambda *_args: connection,
    )

    async def sync(*_args, **kwargs):
        syncs.append(kwargs.get("force"))
        return {"status": "connected"}

    monkeypatch.setattr(calliope, "_sync_google_calendar", sync)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _query, _params):
            return _Result([{
                "event_id": "evt-1",
                "summary": "Planning room",
                "location": "Meet",
                "html_link": "https://calendar.google.com/event?eid=1",
                "meeting_link": "https://meet.google.com/abc-defg-hij",
                "starts_at": now + timedelta(minutes=12),
                "ends_at": now + timedelta(minutes=42),
                "response_status": "accepted",
                "attendees": [
                    {"email": "private-one@example.com"},
                    {"email": "private-two@example.com"},
                ],
            }])

    snapshot = asyncio.run(calliope.google_calendar_upcoming_snapshot(
        Connection, "owner@example.com", horizon_hours=24, limit=2
    ))

    assert syncs == [False]
    assert snapshot["connected"] is True
    assert snapshot["events"][0]["title"] == "Planning room"
    assert snapshot["events"][0]["attendee_count"] == 2
    assert "attendees" not in snapshot["events"][0]
    assert "description" not in snapshot["events"][0]


def test_presence_and_activity_render_in_a_real_browser(monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr(calliope, "is_enabled", lambda: True)
    page_html = server._landing_html([_artifact_row()], "owner@example.com")
    starts = datetime.now(timezone.utc) + timedelta(minutes=9)
    ends = starts + timedelta(minutes=30)

    activity = {
        "artifact": {
            "slug": "revenue-room",
            "name": "Revenue Room",
            "app_kind": "app",
            "owner": "owner@example.com",
            "latest_version": 4,
        },
        "window_days": 30,
        "timezone": "UTC",
        "summary": {
            "total_views": 12,
            "unique_viewers": 3,
            "repeat_views": 9,
            "window_views": 8,
            "tracked_since": (starts - timedelta(days=20)).isoformat(),
            "last_viewed_at": (starts - timedelta(minutes=20)).isoformat(),
        },
        "series": [
            {
                "day": (starts.date() - timedelta(days=29 - index)).isoformat(),
                "views": 0 if index < 26 else index - 25,
                "viewers": 0 if index < 26 else min(3, index - 25),
            }
            for index in range(30)
        ],
        "viewers": [{
            "viewer": "owner@example.com",
            "views": 7,
            "active_days": 4,
            "last_viewed_at": starts.isoformat(),
            "is_you": True,
        }, {
            "viewer": "alex@example.com",
            "views": 5,
            "active_days": 3,
            "last_viewed_at": (starts - timedelta(hours=2)).isoformat(),
            "is_you": False,
        }],
        "versions": [{"version": "4", "views": 12}],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/gallery/meetings"):
                body = json.dumps({
                    "available": True,
                    "connected": True,
                    "events": [{
                        "id": "evt-1",
                        "title": "Planning room",
                        "starts_at": starts.isoformat(),
                        "ends_at": ends.isoformat(),
                        "meeting_url": "https://meet.google.com/abc-defg-hij",
                        "calendar_url": None,
                        "attendee_count": 4,
                    }],
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
            elif self.path.startswith("/api/gallery/artifacts/revenue-room/activity"):
                body = json.dumps(activity).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
            elif self.path in {"/", "/gallery"}:
                body = page_html.encode()
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
            else:
                body = b""
                self.send_response(404)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    errors = []
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                executable_path="/usr/bin/chromium",
                args=["--no-sandbox"],
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{httpd.server_port}/gallery")
            page.wait_for_selector("#presence-meeting:not([hidden])")
            assert page.locator("#presence-hours").inner_text() != "--"
            assert page.locator("#presence-meeting-title").inner_text() == "Planning room"
            assert page.locator("#gallery-presence .presence-clock").evaluate(
                "node => getComputedStyle(node).boxShadow"
            ) != "none"
            page.locator('[data-gallery-activity="revenue-room"]').click()
            page.wait_for_selector("#artifact-activity-dialog[open]")
            page.wait_for_selector(".activity-viewer")
            assert page.locator(".activity-stat").count() == 4
            assert page.locator(".activity-viewer").count() == 2
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

    assert errors == []
