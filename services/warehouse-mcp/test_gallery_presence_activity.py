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
        "area_id": "revenue",
        "area_label": "Revenue",
        "area_source": "auto",
        "area_confidence": 0.91,
        "total_views": 12,
        "updated_at": datetime.now(timezone.utc),
    }


def test_gallery_presence_and_shared_activity_are_native_but_unobtrusive(monkeypatch):
    monkeypatch.setattr(calliope, "is_enabled", lambda: True)

    owner_page = server._landing_html(
        [_artifact_row()],
        "OWNER@example.com",
        {
            "identity": "OWNER@example.com",
            "name": "Gallery Owner",
            "via": "google",
            "picture": "https://lh3.googleusercontent.com/a/gallery-owner",
        },
    )
    reader_page = server._landing_html([_artifact_row()], "reader@example.com")

    assert 'id="gallery-presence"' in owner_page
    assert 'id="presence-meeting"' in owner_page
    assert 'id="artifact-activity-dialog"' in owner_page
    assert 'data-gallery-activity="revenue-room"' in owner_page
    assert 'data-gallery-activity="revenue-room"' in reader_page
    assert '<b>12</b> total views</button>' in owner_page
    assert 'class="card-activity"' not in owner_page
    assert 'card-kind' not in owner_page
    assert 'artifact-kind-chips' not in owner_page
    assert 'class="pill dim card-owner" title="owner@example.com"' in owner_page
    assert 'data-card-more aria-expanded="false"' in owner_page
    assert 'id="artifact-sort"' in owner_page
    assert 'id="artifact-area"' in owner_page
    assert 'data-area="revenue"' in owner_page
    assert '<span class="pill dim card-area">Revenue</span>' in owner_page
    assert 'id="artifact-pinned-filter"' in owner_page
    assert 'id="semantic-home-toggle"' in owner_page
    assert 'data-sort-views="12"' in owner_page
    assert "url.searchParams.set('sort',mode)" in server._LANDING_JS
    assert "url.searchParams.set('pinned','1')" in server._LANDING_JS
    assert "window.matchMedia('(max-width:760px)').matches" in server._LANDING_JS
    assert "gallery-mobile-scrolled" in server._LANDING_CSS
    assert "-webkit-mask-image:linear-gradient(90deg,#000 0%,#000 44%,transparent 100%)" in server._LANDING_CSS
    assert ".home-thumb::after" not in server._LANDING_CSS
    assert owner_page.index('class="card-view-count"') < owner_page.index('class="pill dim card-area"')
    assert owner_page.index('class="pill dim card-area"') < owner_page.index('class="foot"')
    assert "shared Gallery insight" in owner_page
    assert "/api/gallery/meetings" in server._LANDING_JS
    assert "/api/gallery/artifacts/" in server._LANDING_JS
    assert ".gallery-presence.compact" in server._LANDING_CSS
    gallery_header = owner_page.split("<nav data-warehouse-header>", 1)[1].split("</nav>", 1)[0]
    assert 'data-warehouse-account' in gallery_header
    assert 'src="/auth/avatar"' in gallery_header
    assert "Gallery Owner" in gallery_header
    assert gallery_header.count("/auth/logout") == 1
    assert ">Sign out</span>" in gallery_header


def test_gallery_tiles_show_zero_views_but_omit_an_unavailable_total(monkeypatch):
    monkeypatch.setattr(calliope, "is_enabled", lambda: False)
    quiet = _artifact_row()
    quiet["total_views"] = 0
    unavailable = _artifact_row()
    unavailable["slug"] = "activity-unavailable"
    unavailable["total_views"] = None

    page = server._landing_html([quiet, unavailable], "reader@example.com")

    assert '<b>0</b> total views</button>' in page
    assert page.count('class="card-view-count"') == 1


def test_landing_rows_add_shared_activity_totals_and_zero_fill(monkeypatch):
    published = [_artifact_row(), {**_artifact_row(), "slug": "quiet-room"}]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            if "FROM rvbbit.live_apps" in query:
                return _Result(published)
            if "tool='artifact_view'" in query:
                assert params == (["revenue-room", "quiet-room"],)
                return _Result([{"slug": "revenue-room", "total_views": 12}])
            raise AssertionError(query)

    monkeypatch.setattr(server, "_conn", lambda: Connection())

    rows = server._landing_rows()

    assert rows[0]["total_views"] == 12
    assert rows[1]["total_views"] == 0


def test_landing_rows_keep_artifacts_when_activity_totals_are_unavailable(monkeypatch):
    published = [_artifact_row()]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None):
            if "FROM rvbbit.live_apps" in query:
                return _Result(published)
            raise RuntimeError("activity table unavailable")

    monkeypatch.setattr(server, "_conn", lambda: Connection())

    rows = server._landing_rows()

    assert [row["slug"] for row in rows] == ["revenue-room"]
    assert rows[0]["total_views"] is None


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
    revenue = _artifact_row(
        "warehouse-gallery-owner-with-a-very-long-name-and-team-context"
        "@acceleratedacademy.example"
    )
    alpha = {
        **_artifact_row("analyst@example.com"),
        "slug": "alpha-forecast",
        "name": "Alpha Forecast",
        "app_kind": "dashboard",
        "area_id": "executive",
        "area_label": "Executive",
        "total_views": 47,
        "updated_at": datetime.now(timezone.utc) - timedelta(days=2),
    }
    page_html = server._landing_html([revenue, alpha], "owner@example.com")
    starts = datetime.now(timezone.utc) + timedelta(minutes=90)
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
            elif self.path.startswith("/api/calliope/home"):
                body = json.dumps({"home": {"title": "My Home"}, "items": []}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
            elif self.path.startswith("/api/gallery/metrics"):
                body = json.dumps({
                    "metrics": [{
                        "name": "gross_revenue",
                        "title": "Recognized Gross Revenue Across Active Programs",
                        "description": "Recognized revenue in the selected period.",
                        "category": "Finance",
                        "subcategory": "Enterprise and Institutional Revenue",
                        "grain": "monthly payment transaction settlement cohort",
                        "version": 2,
                        "snapshot": {"value": 2290000, "status": "healthy", "ok": True,
                                     "observed_at": starts.isoformat()},
                        "series": [{"numeric_value": 2100000}, {"numeric_value": 2290000}],
                        "trend": {"direction": "up", "meaning": "good", "percent": 9.0},
                        "display": {"currency": "USD", "decimals": 0,
                                    "preferred_direction": "higher"},
                    }, {
                        "name": "media_spend",
                        "title": "Media Spend",
                        "description": "Current active-channel media investment.",
                        "category": "Marketing",
                        "subcategory": "Spend",
                        "version": 1,
                        "snapshot": {"value": 530000, "status": "observed",
                                     "observed_at": starts.isoformat()},
                        "series": [{"numeric_value": 500000}, {"numeric_value": 530000}],
                        "trend": {"direction": "up", "meaning": "bad", "percent": 6.0},
                        "display": {"currency": "USD", "decimals": 0,
                                    "preferred_direction": "lower"},
                    }],
                    "categories": [{"category": "Finance", "count": 1},
                                   {"category": "Marketing", "count": 1}],
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
            elif self.path.split("?", 1)[0] in {"/", "/gallery"}:
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
            page.wait_for_selector("#semantic-home:not([hidden])")
            assert page.locator("#presence-hours").inner_text() != "--"
            assert page.locator("#presence-meeting-title").inner_text() == "Planning room"
            assert page.locator("#gallery-presence .presence-clock").evaluate(
                "node => getComputedStyle(node).boxShadow"
            ) != "none"
            assert "collapsed" in page.locator("#semantic-home").get_attribute("class")
            assert page.locator("#home-content").evaluate(
                "node => getComputedStyle(node).display"
            ) == "none"
            page.locator("#semantic-home-toggle").click()
            assert "collapsed" not in page.locator("#semantic-home").get_attribute("class")

            # Every top-level artifact toolbar control shares one visual rail.
            # Keep their outer boxes aligned even though they use different
            # native elements (search input, selects, and buttons).
            toolbar_control_heights = page.locator(
                ".gallery-view-switch, #q, #artifact-area, #artifact-sort, "
                "#artifact-pinned-filter"
            ).evaluate_all(
                "nodes => nodes.map(node => node.getBoundingClientRect().height)"
            )
            assert toolbar_control_heights
            assert all(abs(height - 34) < 0.1 for height in toolbar_control_heights)

            revenue_card = page.locator('[data-slug="revenue-room"]')
            view_count = revenue_card.locator(".card-view-count")
            assert view_count.inner_text().lower() == "12 total views"
            assert revenue_card.locator(".card-kind").count() == 0
            assert abs(
                view_count.bounding_box()["y"]
                - revenue_card.locator(".card-area").bounding_box()["y"]
            ) < 5
            assert view_count.bounding_box()["y"] < revenue_card.locator(".foot").bounding_box()["y"]
            owner = revenue_card.locator(".card-owner")
            when = revenue_card.locator(".when")
            owner_box, when_box = owner.bounding_box(), when.bounding_box()
            assert abs(
                owner_box["y"] + owner_box["height"] / 2
                - when_box["y"] - when_box["height"] / 2
            ) < 2
            assert owner.evaluate("node => getComputedStyle(node).textOverflow") == "ellipsis"
            actions = revenue_card.locator(".card-actions")
            assert float(actions.evaluate("node => getComputedStyle(node).opacity")) == 0
            assert revenue_card.locator(".card-more").evaluate(
                "node => getComputedStyle(node).display"
            ) == "none"
            revenue_card.hover()
            page.wait_for_timeout(220)
            assert float(actions.evaluate("node => getComputedStyle(node).opacity")) == 1
            assert revenue_card.locator(".card-action-items button").count() == 2
            view_count.click()
            page.wait_for_selector("#artifact-activity-dialog[open]")
            page.wait_for_selector(".activity-viewer")
            assert page.locator(".activity-stat").count() == 4
            assert page.locator(".activity-viewer").count() == 2
            page.locator("#activity-close").click()

            # Sort and filter state is useful enough to survive a copied URL or refresh.
            page.locator("#artifact-sort").select_option("views")
            assert page.locator(".card").first.get_attribute("data-slug") == "alpha-forecast"
            assert "sort=views" in page.url
            page.locator("#artifact-sort").select_option("name")
            assert page.locator(".card").first.get_attribute("data-slug") == "alpha-forecast"
            page.locator("#artifact-sort").select_option("updated")
            assert page.locator(".card").first.get_attribute("data-slug") == "revenue-room"
            assert "sort=" not in page.url
            page.locator("#q").fill("alpha")
            assert "q=alpha" in page.url and "kind=" not in page.url
            page.reload()
            page.wait_for_selector("#semantic-home:not([hidden])")
            assert page.locator("#q").input_value() == "alpha"
            assert page.locator(".card:visible").count() == 1
            assert page.locator(".card:visible").get_attribute("data-slug") == "alpha-forecast"
            page.locator("#q").fill("")
            page.locator("#artifact-area").select_option("revenue")
            assert "area=revenue" in page.url
            assert page.locator(".card:visible").count() == 1
            assert page.locator(".card:visible").get_attribute("data-slug") == "revenue-room"
            page.reload()
            assert page.locator("#artifact-area").input_value() == "revenue"
            page.locator("#artifact-area").select_option("")
            page.locator("#artifact-pinned-filter").click()
            assert "pinned=1" in page.url
            assert page.locator(".card:visible").count() == 0
            assert "no pinned artifacts" in page.locator("#none").inner_text().lower()
            page.locator("#artifact-pinned-filter").click()

            page.locator('[data-gallery-view="metrics"]').click()
            page.wait_for_selector(".metric-gallery-card")
            assert page.locator(".metric-gallery-card").count() == 2
            metric_sections = page.locator(".metric-category-section")
            assert metric_sections.count() == 2
            assert metric_sections.locator(".metric-category-heading h2").all_inner_texts() == [
                "Finance", "Marketing",
            ]
            metric_grids = page.locator(".metric-gallery-grid")
            assert metric_grids.count() == 2
            assert all("sparse" in value for value in metric_grids.evaluate_all(
                "nodes => nodes.map(node => node.className)"
            ))
            assert all(width <= 422 for width in metric_grids.evaluate_all(
                "nodes => nodes.map(node => node.getBoundingClientRect().width)"
            ))
            assert page.locator(".metric-card-kicker").count() == 0
            assert page.locator('.metric-card-status').count() == 0
            assert page.locator('[data-metric-range="7"]').get_attribute(
                "aria-pressed"
            ) == "true"
            anchors = page.locator(".metric-gallery-card").evaluate_all(
                """nodes => nodes.map(node => {
                    const card = node.getBoundingClientRect();
                    const value = node.querySelector('.metric-card-value').getBoundingClientRect();
                    const title = node.querySelector('.metric-card-title').getBoundingClientRect();
                    const description = node.querySelector('.metric-card-description').getBoundingClientRect();
                    const foot = node.querySelector('.metric-card-foot span');
                    return {
                      top: value.top - card.top,
                      right: card.right - value.right,
                      titleHeight: title.height,
                      descriptionHeight: description.height,
                      footWhiteSpace: getComputedStyle(foot).whiteSpace,
                      footOverflow: getComputedStyle(foot).textOverflow
                    };
                })"""
            )
            assert all(abs(anchor["top"] - 18) < 0.1 for anchor in anchors)
            assert all(abs(anchor["right"] - 18) < 0.1 for anchor in anchors)
            assert all(abs(anchor["titleHeight"] - 46) < 0.1 for anchor in anchors)
            assert all(abs(anchor["descriptionHeight"] - 27) < 0.1 for anchor in anchors)
            assert all(anchor["footWhiteSpace"] == "nowrap" for anchor in anchors)
            assert all(anchor["footOverflow"] == "ellipsis" for anchor in anchors)
            finance_card = page.locator('[data-metric-card="gross_revenue"]')
            finance_actions = finance_card.locator(".metric-card-actions")
            assert float(finance_actions.evaluate(
                "node => getComputedStyle(node).opacity"
            )) == 0
            finance_card.hover()
            page.wait_for_timeout(220)
            assert float(finance_actions.evaluate(
                "node => getComputedStyle(node).opacity"
            )) == 1
            chart_box = finance_card.locator(".metric-card-chart").bounding_box()
            title_box = finance_card.locator(".metric-card-title").bounding_box()
            assert chart_box["y"] + chart_box["height"] <= title_box["y"] - 8
            assert "good" in finance_card.locator(".metric-card-delta").get_attribute("class")
            assert "bad" in page.locator(
                '[data-metric-card="media_spend"] .metric-card-delta'
            ).get_attribute("class")
            with page.expect_response(
                lambda response: "/api/gallery/metrics" in response.url
                and "days=90" in response.url
            ):
                page.locator('[data-metric-range="90"]').click()
            page.wait_for_function(
                "document.querySelector('#metric-browser-status').hidden"
            )
            assert page.locator('[data-metric-range="90"]').get_attribute(
                "aria-pressed"
            ) == "true"
            page.locator('[data-gallery-view="artifacts"]').click()

            # The larger-screen clock/reminders and Calliope capsule stay prominent.
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(260)
            assert "compact" not in page.locator("#gallery-presence").get_attribute("class")
            assert "gallery-mobile-scrolled" not in (
                page.locator("body").get_attribute("class") or ""
            )
            assert page.locator(".calliope-float-copy").evaluate(
                "node => getComputedStyle(node).display"
            ) == "flex"
            assert page.locator(".calliope-float").bounding_box()["width"] > 100

            touch_page = browser.new_page(
                viewport={"width": 390, "height": 844},
                has_touch=True,
                is_mobile=True,
            )
            touch_page.on("pageerror", lambda error: errors.append(str(error)))
            touch_page.goto(f"http://127.0.0.1:{httpd.server_port}/gallery")
            touch_page.wait_for_selector("#semantic-home:not([hidden])")
            mobile_card = touch_page.locator('[data-slug="revenue-room"]')
            more = mobile_card.locator(".card-more")
            assert more.evaluate("node => getComputedStyle(node).display") == "flex"
            more.click()
            assert more.get_attribute("aria-expanded") == "true"
            assert "open" in mobile_card.locator(".card-actions").get_attribute("class")
            assert mobile_card.locator(".card-action-items").evaluate(
                "node => getComputedStyle(node).display"
            ) == "flex"
            touch_page.locator("h1").click()
            assert more.get_attribute("aria-expanded") == "false"
            assert touch_page.locator(".calliope-float-copy").evaluate(
                "node => getComputedStyle(node).display"
            ) == "flex"
            touch_page.evaluate("window.scrollTo(0, 900)")
            touch_page.wait_for_function(
                "document.body.classList.contains('gallery-mobile-scrolled')"
            )
            touch_page.wait_for_timeout(220)
            assert float(touch_page.locator("#gallery-presence").evaluate(
                "node => getComputedStyle(node).opacity"
            )) < 0.05
            assert touch_page.locator(".calliope-float-copy").evaluate(
                "node => getComputedStyle(node).display"
            ) == "none"
            assert touch_page.locator(".calliope-float").bounding_box()["width"] <= 54
            toolbar_box = touch_page.locator(".toolbar").bounding_box()
            assert 55 <= toolbar_box["y"] <= 58
            assert touch_page.locator(".toolbar").evaluate(
                "node => getComputedStyle(node).position"
            ) == "sticky"
            controls_box = touch_page.locator(".gallery-artifact-controls").bounding_box()
            assert controls_box["x"] >= 0
            assert controls_box["x"] + controls_box["width"] <= 390
            touch_page.close()
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

    assert errors == []
