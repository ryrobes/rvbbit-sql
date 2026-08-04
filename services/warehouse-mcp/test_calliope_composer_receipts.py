"""Exact composer-object and durable response-receipt contracts."""
from __future__ import annotations

import json
import re
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import calliope  # noqa: E402


class _Result:
    def __init__(self, rows):
        self.rows = rows if isinstance(rows, list) else ([rows] if rows else [])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def test_composer_markers_are_typed_bounded_deduplicated_and_readable():
    body = (
        "Compare [[metric:lead_velocity|Lead velocity]] with "
        "[[artifact:enrollment-pulse@7|Enrollment pulse]] and ask "
        "[[person:42|Ada Lovelace]]. [[person:42|Ada]]"
    )
    assert calliope._composer_object_markers(body) == [
        {"kind": "metric", "ref_id": "lead_velocity", "mention": "Lead velocity"},
        {
            "kind": "artifact",
            "ref_id": "enrollment-pulse@7",
            "mention": "Enrollment pulse",
        },
        {"kind": "person", "ref_id": "42", "mention": "Ada Lovelace"},
    ]
    assert calliope._composer_plain_text(body) == (
        "Compare Lead velocity with Enrollment pulse and ask Ada Lovelace. Ada"
    )
    with pytest.raises(ValueError, match="supported Calliope object type"):
        calliope._composer_object_markers("[[password:secret|Nope]]")
    too_many = " ".join(
        f"[[metric:metric_{index}|Metric {index}]]" for index in range(25)
    )
    with pytest.raises(ValueError, match="at most 24"):
        calliope._composer_object_markers(too_many)


def test_submitted_object_handles_must_be_small_and_exactly_typed():
    assert calliope._decode_composer_object_handles([
        {"kind": "workflow", "ref_id": "9be90d18-572d-4b97-9ccd-57ee7b71b55c@3"},
        {"kind": "workflow", "ref_id": "9be90d18-572d-4b97-9ccd-57ee7b71b55c@3"},
    ]) == [{
        "kind": "workflow",
        "ref_id": "9be90d18-572d-4b97-9ccd-57ee7b71b55c@3",
    }]
    with pytest.raises(ValueError, match="supported kind"):
        calliope._decode_composer_object_handles([{"kind": "secret", "ref_id": "x"}])


def test_soft_object_hints_use_strong_acl_search_results_and_never_auto_insert(monkeypatch):
    lookups = []

    def lookup(_conn_factory, owner, query, *, kind="", limit=6):
        lookups.append((owner, query, kind, limit))
        if query == "Ada":
            return [{
                "kind": "person",
                "ref_id": "42",
                "label": "Ada Lovelace",
                "source": "Company Brain",
                "match_basis": "alias_exact",
            }]
        if query == "Enrollment Rate":
            return [{
                "kind": "metric",
                "ref_id": "enrollment_rate",
                "label": "Enrollment Rate",
                "source": "Governed metrics",
            }]
        return [{
            "kind": "project",
            "ref_id": "99",
            "label": "Apollo Program",
            "source": "Company Brain",
        }]

    monkeypatch.setattr(calliope, "_composer_objects", lookup)
    hints = calliope._composer_object_hints(object, "owner@example.com", [
        {"key": "0:3", "text": "Ada", "kind": "person"},
        {"key": "8:23", "text": "Enrollment Rate"},
        {"key": "24:27", "text": "App"},
        {"key": "28:34", "text": "Apollo"},
        {"key": "30:33", "text": "Ada", "kind": "person"},
    ])

    assert [hint["key"] for hint in hints] == ["0:3", "8:23", "28:34", "30:33"]
    assert hints[0]["objects"][0]["ref_id"] == "42"
    assert hints[1]["objects"][0]["ref_id"] == "enrollment_rate"
    assert hints[2]["objects"][0]["label"] == "Apollo Program"
    # Repeated spans reuse the permission-filtered lookup within one debounce call.
    assert [entry[1] for entry in lookups].count("Ada") == 1
    with pytest.raises(ValueError, match="At most 12"):
        calliope._composer_object_hints(
            object,
            "owner@example.com",
            [{"key": str(index), "text": "Ada"} for index in range(13)],
        )


def test_composer_exact_resolution_rechecks_brain_acl_and_artifact_version():
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            if "WITH visible_docs AS MATERIALIZED" in query:
                assert params == ("owner@example.com", [42])
                return _Result({
                    "node_id": 42,
                    "graph_id": "brain",
                    "kind": "person",
                    "label": "Ada Lovelace",
                    "doc_type": None,
                    "source": "Company Brain",
                })
            if "JOIN rvbbit.dashboard_versions" in query:
                assert params == ("enrollment-pulse", 7)
                return _Result({
                    "slug": "enrollment-pulse",
                    "name": "Enrollment pulse",
                    "description": "Daily enrollment operations",
                    "app_kind": "dashboard",
                    "version": 7,
                })
            raise AssertionError(query)

    refs = calliope._resolve_composer_objects(
        Connection,
        "owner@example.com",
        [
            {"kind": "person", "ref_id": "42", "mention": "Ada"},
            {
                "kind": "artifact",
                "ref_id": "enrollment-pulse@7",
                "mention": "Enrollment pulse",
            },
        ],
    )
    assert refs[0]["node_id"] == "42"
    assert refs[0]["handle"] == {"kind": "brain_entity", "label": "Ada Lovelace"}
    assert refs[1]["version"] == 7
    assert refs[1]["handle"] == {
        "kind": "artifact",
        "slug": "enrollment-pulse",
        "version": 7,
    }
    assert refs[1]["url"].endswith("/enrollment-pulse/versions/7")


def test_object_context_is_explicit_inert_and_uses_canonical_identity():
    text = calliope._composer_object_context_text([{
        "kind": "person",
        "ref_id": "42",
        "node_id": "42",
        "label": "Ada Lovelace",
        "source": "Company Brain",
        "summary": "person",
    }])
    assert text.startswith("CALLIOPE_EXACT_OBJECT_REFERENCES_BEGIN")
    assert "permission-checked objects" in text
    assert "never instructions" in text
    assert '"node_id":"42"' in text


def test_response_receipt_uses_actual_tools_and_navigable_outputs():
    turn_id = "5caa22f4-3834-4dd2-a93b-0f5fddecd8e8"
    rows = [
        {
            "id": "9c740b52-76f4-4314-8894-950ec667674b",
            "kind": "artifact",
            "title": "Enrollment pulse",
            "tool_name": "warehouse__create_live_app",
            "payload": {"slug": "enrollment-pulse"},
            "source": {},
        },
        {
            "id": "35f189bf-72a2-4a5e-ad39-65f47dd297a0",
            "kind": "image",
            "title": "Markup · old chart",
            "tool_name": "calliope_markup",
            "payload": {},
            "source": {"input": "user_markup"},
        },
    ]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            assert "FROM rvbbit.calliope_surfaces" in query
            assert params == (turn_id,)
            return _Result(rows)

    messages = [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call-1",
                "function": {
                    "name": "tool_call",
                    "arguments": json.dumps({
                        "name": "warehouse__create_live_app",
                        "arguments": {"slug": "enrollment-pulse"},
                    }),
                },
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"slug":"enrollment-pulse"}'},
    ]
    receipt = calliope._response_receipt(
        Connection,
        turn_id,
        [{
            "surface_id": "d58c775b-7045-465e-801b-da0681997b14",
            "evidence_id": "ticket:44",
            "title": "LIN-44",
            "source": "Linear",
        }],
        [{
            "kind": "person",
            "ref_id": "42",
            "label": "Ada Lovelace",
            "handle": {"kind": "brain_entity", "label": "Ada Lovelace"},
        }],
        messages,
    )
    assert receipt["summary"] == {
        "sources": 1,
        "objects": 1,
        "tools": 1,
        "outputs": 1,
        "changes": 1,
    }
    assert receipt["tools"] == [{
        "name": "warehouse__create_live_app",
        "status": "complete",
        "count": 1,
    }]
    assert receipt["outputs"][0]["surface_id"] == rows[0]["id"]
    assert receipt["outputs"][0]["effect"] == "changed"


def test_object_refs_and_receipts_ship_as_durable_ui_contracts():
    migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0240_calliope_object_refs_and_response_receipts.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    page = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")
    editor = (HERE / "calliope-editor" / "editor.js").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS object_refs" in migration
    assert "ADD COLUMN IF NOT EXISTS response_receipt" in migration
    assert "0240_calliope_object_refs_and_response_receipts" in registry
    assert '@mcp.custom_route("/api/calliope/objects", methods=["GET"])' in backend
    assert '@mcp.custom_route("/api/calliope/object-hints", methods=["POST"])' in backend
    assert '"object_refs": object_refs' in backend
    assert '"response_receipt": response_receipt' in backend
    assert 'id="message-editor"' in page
    assert "function initializeComposerEditor()" in script
    assert "object_refs: outgoingObjectHandles" in script
    assert "function renderTurnReceipt(turn)" in script
    assert ".message-receipt" in css
    assert "window.CalliopeObjectEditor" in editor
    assert "getObjectRefs" in editor


def test_rendered_composer_selects_exact_objects_and_shows_durable_receipt(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(QuietHandler, directory=str(HERE)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session_id = "9e4e61fb-abd1-4289-8e04-76c604f4d0ee"
    turn_id = "d6dfb4ad-598f-401b-8baf-c6b467225f43"
    output_id = "35dcfd8d-375f-41f2-99dd-4d9f3139f18c"
    posted = []
    hint_requests = []
    errors = []

    def response_receipt():
        objects = [
            {
                "kind": "metric",
                "ref_id": "enrollment_rate",
                "label": "Enrollment Rate",
                "source": "Governed metrics",
                "version": 4,
                "handle": {
                    "kind": "metric",
                    "name": "enrollment_rate",
                    "relation": "enrollment_rate",
                    "params": {},
                },
            },
            {
                "kind": "person",
                "ref_id": "42",
                "label": "Ada Lovelace",
                "source": "Company Brain",
                "node_id": "42",
                "handle": {"kind": "brain_entity", "label": "Ada Lovelace"},
            },
        ]
        return {
            "version": 1,
            "evidence": [],
            "objects": objects,
            "tools": [{"name": "warehouse__metric_history", "status": "complete", "count": 1}],
            "outputs": [{
                "surface_id": output_id,
                "kind": "metric",
                "title": "Enrollment Rate",
                "effect": "created",
            }],
            "summary": {"sources": 0, "objects": 2, "tools": 1, "outputs": 1, "changes": 0},
        }, objects

    def api_route(route):
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if path == "/api/calliope/config":
            payload = {
                "healthy": True,
                "evidence_search": True,
                "personal_briefs": False,
                "google_calendar": False,
                "action_library": True,
                "speech_to_text": {"enabled": False},
            }
        elif path == "/api/calliope/inbox":
            payload = {"items": [], "counts": {"unread": 0, "open": 0, "shown": 0, "by_kind": {}}}
        elif path == "/api/calliope/styles":
            payload = {"profiles": []}
        elif path == "/api/calliope/instruments":
            payload = {"instruments": []}
        elif path == "/api/calliope/workflows":
            payload = {"workflows": []}
        elif path == "/api/calliope/sessions" and request.method == "GET":
            payload = {"sessions": [{
                "id": session_id,
                "title": "Composer receipt check",
                "updated_at": "2026-08-03T14:00:00Z",
                "surface_count": 0,
            }]}
        elif path == f"/api/calliope/sessions/{session_id}" and request.method == "GET":
            receipt, resolved_objects = response_receipt()
            payload = {
                "session": {
                    "id": session_id,
                    "title": "Composer receipt check",
                    "updated_at": "2026-08-03T14:00:00Z",
                },
                "turns": ([{
                    "id": turn_id,
                    "session_id": session_id,
                    "ordinal": 1,
                    "user_message": "Compare Enrollment Rate with Ada Lovelace",
                    "assistant_message": "Enrollment Rate is currently 73%. Ada owns the next review.",
                    "attachments": [],
                    "status": "complete",
                    "turn_kind": "chat",
                    "evidence_refs": [],
                    "object_refs": resolved_objects,
                    "response_receipt": receipt,
                    "created_at": "2026-08-03T14:01:00Z",
                    "completed_at": "2026-08-03T14:01:04Z",
                }] if posted else []),
                "surfaces": ([{
                    "id": output_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "ordinal": 1,
                    "kind": "metric",
                    "title": "Enrollment Rate",
                    "tool_name": "warehouse__metric_history",
                    "tool_call_id": "call-1",
                    "lineage_key": "metric:enrollment_rate",
                    "payload": {"name": "enrollment_rate", "value": 0.73},
                    "source": {},
                    "presentation": {},
                    "created_at": "2026-08-03T14:01:00Z",
                }] if posted else []),
            }
        elif path == "/api/calliope/objects":
            query = parse_qs(parsed.query).get("q", [""])[0].lower()
            if "ada" in query:
                objects = [{
                    "kind": "person",
                    "ref_id": "42",
                    "node_id": "42",
                    "label": "Ada Lovelace",
                    "source": "Company Brain",
                    "summary": "person",
                    "handle": {"kind": "brain_entity", "label": "Ada Lovelace"},
                }]
            else:
                objects = [{
                    "kind": "metric",
                    "ref_id": "enrollment_rate",
                    "label": "Enrollment Rate",
                    "source": "Governed metrics",
                    "summary": "Current enrollment conversion rate",
                    "version": 4,
                    "handle": {
                        "kind": "metric",
                        "name": "enrollment_rate",
                        "relation": "enrollment_rate",
                        "params": {},
                    },
                }]
            payload = {"objects": objects, "query": query}
        elif path == "/api/calliope/object-hints":
            body = json.loads(request.post_data or "{}")
            hint_requests.append(body)
            hints = []
            for candidate in body.get("candidates", []):
                if str(candidate.get("text") or "").lower() == "bigfoot":
                    objects = [
                        {
                            "kind": "thing",
                            "ref_id": "2199",
                            "node_id": "2199",
                            "label": "bigfoot",
                            "source": "Company Brain",
                            "summary": "topic",
                            "handle": {"kind": "brain_entity", "label": "bigfoot"},
                        },
                        {
                            "kind": "thing",
                            "ref_id": "2333",
                            "node_id": "2333",
                            "label": "bigfoot_sightings",
                            "source": "Company Brain",
                            "summary": "table",
                            "handle": {"kind": "brain_entity", "label": "bigfoot_sightings"},
                        },
                    ]
                elif candidate.get("text") == "Ada Lovelace":
                    objects = [{
                        "kind": "person",
                        "ref_id": "42",
                        "node_id": "42",
                        "label": "Ada Lovelace",
                        "source": "Company Brain",
                        "summary": "person",
                        "handle": {"kind": "brain_entity", "label": "Ada Lovelace"},
                    }]
                else:
                    continue
                hints.append({
                    "key": candidate["key"],
                    "text": candidate["text"],
                    "objects": objects,
                })
            payload = {"hints": hints}
        elif path == f"/api/calliope/sessions/{session_id}/turn":
            body = json.loads(request.post_data or "{}")
            posted.append(body)
            receipt, objects = response_receipt()
            surface = {
                "id": output_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "ordinal": 1,
                "kind": "metric",
                "title": "Enrollment Rate",
                "tool_name": "warehouse__metric_history",
                "tool_call_id": "call-1",
                "lineage_key": "metric:enrollment_rate",
                "payload": {"name": "enrollment_rate", "value": 0.73},
                "source": {},
                "presentation": {},
                "created_at": "2026-08-03T14:01:00Z",
            }
            events = [
                ("calliope.turn.started", {
                    "turn_id": turn_id,
                    "ordinal": 1,
                    "attachments": [],
                    "evidence_refs": [],
                    "object_refs": objects,
                }),
                ("assistant.completed", {
                    "content": "Enrollment Rate is currently 73%. Ada owns the next review."
                }),
                ("calliope.surfaces", {"turn_id": turn_id, "surfaces": [surface]}),
                ("calliope.turn.completed", {
                    "turn_id": turn_id,
                    "assistant_message": "Enrollment Rate is currently 73%. Ada owns the next review.",
                    "surface_count": 1,
                    "response_receipt": receipt,
                }),
            ]
            stream = "".join(
                f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in events
            )
            route.fulfill(status=200, content_type="text/event-stream", body=stream)
            return
        elif path == "/api/calliope/trails":
            payload = {
                "subject": {
                    "kind": "brain_entity",
                    "label": "Ada Lovelace",
                    "handle": {"kind": "brain_entity", "label": "Ada Lovelace"},
                },
                "facts": [],
                "connections": [],
                "route_summary": {"resolved": 0, "bounded": False, "sections": {}},
            }
        else:
            route.fulfill(status=404, content_type="application/json", body='{"error":{"message":"missing fixture"}}')
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                headless=True,
                executable_path="/usr/bin/chromium",
            )
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", api_route)
            page.goto(
                f"http://127.0.0.1:{server.server_port}/calliope/index.html?session={session_id}",
                wait_until="networkidle",
            )
            editor = page.locator("#message-editor .cm-content")
            editor.click()
            editor.type(
                "Can we find where the bigfoot data might be? There are tables "
                "somewhere. Might be _location or not, whatever one is richer. Traffic?"
            )
            bigfoot_hint = page.locator("#message-editor .cm-object-hint", has_text="bigfoot")
            bigfoot_hint.wait_for()
            expect = playwright.expect
            expect(bigfoot_hint).to_have_class(re.compile("cm-object-hint-ambiguous"))
            page.screenshot(path=str(tmp_path / "calliope-lowercase-object-hint.png"), full_page=True)
            bigfoot_hint.click()
            expect(page.locator(".cm-tooltip-autocomplete li")).to_have_count(2)
            page.keyboard.press("Escape")
            editor.press("Control+A")
            editor.press("Backspace")
            editor.type("Please ask Ada Lovelace")
            soft_hint = page.locator("#message-editor .cm-object-hint", has_text="Ada Lovelace")
            soft_hint.wait_for()
            expect(editor).not_to_contain_text("[[")
            page.screenshot(path=str(tmp_path / "calliope-soft-object-hint.png"), full_page=True)
            soft_hint.click()
            hint_choice = page.locator(".cm-tooltip-autocomplete li", has_text="Ada Lovelace")
            hint_choice.wait_for()
            hint_choice.click()
            expect(editor).to_contain_text("[[person:42|Ada Lovelace]]")
            editor.press("Control+A")
            editor.press("Backspace")
            editor.type("Compare [[Enroll")
            page.locator(".cm-tooltip-autocomplete li").first.wait_for()
            editor.press("Enter")
            expect(editor).to_contain_text("[[metric:enrollment_rate|Enrollment Rate]]")
            editor.type(" with [[Ada")
            page.locator(".cm-tooltip-autocomplete li").first.wait_for()
            editor.press("Enter")
            expect(editor).to_contain_text("[[person:42|Ada Lovelace]]")
            editor.press("Enter")
            expect(page.locator(".message.user .message-body")).to_contain_text(
                "Compare Enrollment Rate with Ada Lovelace"
            )
            expect(page.locator(".message.user .message-body")).not_to_contain_text("[[")
            expect(page.locator(".message-receipt summary")).to_contain_text(
                "Used 2 · Ran 1 · Made 1"
            )
            expect(page.locator(".message-objects button")).to_have_count(2)
            page.locator(".message-receipt summary").click()
            expect(page.locator(".message-receipt-body section")).to_have_count(3)
            expect(page.locator(".message-receipt-body [data-focus-surface]")).to_have_count(1)
            page.screenshot(path=str(tmp_path / "calliope-composer-receipt.png"), full_page=True)
            page.locator(".message-objects button").nth(1).click()
            expect(page.locator("#surface-viewer-title")).to_have_text("Ada Lovelace")
            page.screenshot(path=str(tmp_path / "calliope-object-trail.png"), full_page=True)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert errors == []
    assert any(
        candidate.get("text") == "Ada Lovelace"
        for request in hint_requests
        for candidate in request.get("candidates", [])
    )
    assert len(posted) == 1
    assert posted[0]["message"] == (
        "Compare [[metric:enrollment_rate|Enrollment Rate]] with "
        "[[person:42|Ada Lovelace]]"
    )
    assert posted[0]["object_refs"] == [
        {"kind": "metric", "ref_id": "enrollment_rate"},
        {"kind": "person", "ref_id": "42"},
    ]


def test_rendered_artifact_markup_uses_folded_exact_version_capture(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(QuietHandler, directory=str(HERE)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session_id = "9e4e61fb-abd1-4289-8e04-76c604f4d0ee"
    turn_id = "d6dfb4ad-598f-401b-8baf-c6b467225f43"
    artifact_id = "35dcfd8d-375f-41f2-99dd-4d9f3139f18c"
    capture_id = "51b33d22-631f-49a0-a93c-349bdf707acd"
    capture_requests = []
    errors = []

    artifact = {
        "id": artifact_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "ordinal": 1,
        "kind": "artifact",
        "title": "Growth Brief",
        "tool_name": "mcp__warehouse__create_live_app",
        "tool_call_id": "artifact-1",
        "lineage_key": "artifact:growth-brief",
        "artifact_slug": "growth-brief",
        "artifact_version": 4,
        "payload": {
            "slug": "growth-brief",
            "version": 4,
            "display_url": "/calliope/artifacts/growth-brief/versions/4",
        },
        "source": {},
        "presentation": {},
        "created_at": "2026-08-03T14:01:00Z",
    }
    capture = {
        "id": capture_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "ordinal": 2,
        "kind": "image",
        "title": "Capture · Growth Brief",
        "tool_name": "calliope_artifact_capture",
        "tool_call_id": "capture-1",
        "lineage_key": "capture:growth-brief",
        "parent_surface_id": artifact_id,
        "artifact_slug": "growth-brief",
        "artifact_version": 4,
        "payload": {
            "slug": "growth-brief",
            "version": 4,
            "image_url": f"/api/calliope/surfaces/{capture_id}/image",
            "width": 1200,
            "height": 800,
        },
        "source": {"origin": "calliope_markup_capture"},
        "presentation": {"companion": True, "purpose": "markup"},
        "created_at": "2026-08-03T14:01:03Z",
    }
    captured = False

    def api_route(route):
        nonlocal captured
        request = route.request
        path = urlparse(request.url).path
        if path == "/api/calliope/config":
            payload = {
                "healthy": True,
                "evidence_search": True,
                "personal_briefs": False,
                "google_calendar": False,
                "action_library": True,
                "speech_to_text": {"enabled": False},
                "max_image_bytes": 8 * 1024 * 1024,
            }
        elif path == "/api/calliope/inbox":
            payload = {"items": [], "counts": {"unread": 0, "open": 0, "shown": 0, "by_kind": {}}}
        elif path == "/api/calliope/styles":
            payload = {"profiles": []}
        elif path == "/api/calliope/instruments":
            payload = {"instruments": []}
        elif path == "/api/calliope/workflows":
            payload = {"workflows": []}
        elif path == "/api/calliope/sessions" and request.method == "GET":
            payload = {"sessions": [{
                "id": session_id,
                "title": "Artifact markup check",
                "updated_at": "2026-08-03T14:00:00Z",
                "surface_count": 2 if captured else 1,
            }]}
        elif path == f"/api/calliope/sessions/{session_id}" and request.method == "GET":
            payload = {
                "session": {
                    "id": session_id,
                    "title": "Artifact markup check",
                    "updated_at": "2026-08-03T14:00:00Z",
                },
                "turns": [{
                    "id": turn_id,
                    "session_id": session_id,
                    "ordinal": 1,
                    "user_message": "Build the growth brief",
                    "assistant_message": "The live brief is ready.",
                    "attachments": [],
                    "status": "complete",
                    "turn_kind": "chat",
                    "created_at": "2026-08-03T14:00:00Z",
                    "completed_at": "2026-08-03T14:01:04Z",
                }],
                "surfaces": [capture, artifact] if captured else [artifact],
            }
        elif path == (
            f"/api/calliope/sessions/{session_id}/surfaces/{artifact_id}/capture"
        ):
            capture_requests.append(json.loads(request.post_data or "{}"))
            captured = True
            route.fulfill(status=201, content_type="application/json", body=json.dumps({
                "surface": capture,
                "reused": False,
            }))
            return
        elif path == f"/api/calliope/surfaces/{capture_id}/image":
            svg = """<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800">
              <defs><linearGradient id="g"><stop stop-color="#112b38"/><stop offset="1" stop-color="#071018"/></linearGradient></defs>
              <rect width="1200" height="800" fill="url(#g)"/>
              <rect x="80" y="120" width="1040" height="560" rx="22" fill="#102c39" stroke="#6dc5db"/>
              <text x="120" y="190" fill="#eaf7fa" font-size="42">Growth Brief</text>
              <rect x="120" y="250" width="300" height="300" fill="#59a9bd"/>
            </svg>"""
            route.fulfill(status=200, content_type="image/svg+xml", body=svg)
            return
        else:
            route.fulfill(status=404, content_type="application/json", body='{"error":{"message":"missing fixture"}}')
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    def artifact_route(route):
        html = """<!doctype html><html><body style="margin:0;background:#071018;color:white">
          <button id="revenue" style="margin:80px;width:300px;height:120px">Revenue +18%</button>
          <script>
          addEventListener('message', event => {
            if (event.data?.type !== 'calliope.artifact.inspect.start') return;
            parent.postMessage({type:'calliope.artifact.inspect.selected',target:{
              selection_id:'cda76b1c-e1b7-4b4f-b5ea-902fbe6e7f26',label:'Revenue +18%',
              selector:'#revenue',tag:'button',role:'',text:'Revenue +18%',data:{metric:'revenue'},
              bounds:{x:80,y:80,width:300,height:120},viewport:{width:1200,height:800},
              click:{x:200,y:130},table:null
            }},'*');
          });
          parent.postMessage({type:'calliope.artifact.resize',height:500},'*');
          </script></body></html>"""
        route.fulfill(status=200, content_type="text/html", body=html)

    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True, executable_path="/usr/bin/chromium")
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", api_route)
            page.route("**/calliope/artifacts/**", artifact_route)
            page.goto(
                f"http://127.0.0.1:{server.server_port}/calliope/index.html?session={session_id}",
                wait_until="networkidle",
            )
            expect = playwright.expect
            expect(page.locator(".surface.kind-artifact")).to_have_count(1)
            expect(page.locator(".surface.kind-image")).to_have_count(0)
            expect(page.locator("#surface-count")).to_have_text("1 surface")

            page.locator("[data-markup-artifact]").click()
            expect(page.locator("#markup-dialog")).to_have_attribute("open", "")
            expect(page.locator("#markup-canvas")).to_have_class("ready")
            page.locator("#markup-cancel").click()
            assert len(capture_requests) == 1

            page.locator("[data-inspect-artifact]").click()
            expect(page.locator(".spatial-selection-chip")).to_contain_text("Revenue +18%")
            expect(page.locator("[data-draw-selection]")).to_have_count(1)
            page.locator("[data-draw-selection]").click()
            expect(page.locator("#markup-dialog")).to_have_attribute("open", "")
            expect(page.locator("#markup-undo")).to_be_enabled()
            page.screenshot(path=str(tmp_path / "calliope-artifact-markup.png"), full_page=True)
            page.locator("#markup-attach").click()
            expect(page.locator("#attachment-tray .attachment-preview")).to_have_count(1)
            expect(page.locator(".spatial-selection-chip")).to_have_count(2)
            assert len(capture_requests) == 1
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert errors == []
