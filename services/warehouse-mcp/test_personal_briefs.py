"""Focused contracts for Calliope's private, deterministic Personal Brief."""
from __future__ import annotations

import inspect
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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


def test_personal_brief_schema_and_provider_projection_are_migrated_and_self_healing():
    migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0230_calliope_personal_briefs.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )

    assert "ADD COLUMN IF NOT EXISTS observation_map jsonb" in migration
    assert "p_observation_map jsonb DEFAULT '{}'::jsonb" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_briefs" in migration
    assert "UNIQUE (owner_email, brief_date)" in migration
    assert "calliope_briefs_session_idx" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_brief_feedback" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_identity_aliases" in migration
    assert "WHERE provider = 'fireflies-meetings'" in migration
    assert "$.meetingAttendees[*].email" in migration
    assert "$.participants[*]" in migration
    assert "0230_calliope_personal_briefs" in registry
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_briefs" in calliope._BRIEF_DDL
    assert "conn.execute(_BRIEF_DDL)" in inspect.getsource(calliope.ensure_tables)


def test_brief_sessions_have_stable_summary_metadata_and_a_session_tab():
    session_id = uuid.uuid4()
    brief_id = uuid.uuid4()
    summary = calliope._session_json({
        "id": session_id,
        "title": "Brief · August 2",
        "brief_id": brief_id,
        "brief_date": date(2026, 8, 2),
        "brief_timezone": "America/New_York",
    })
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")

    assert summary["id"] == str(session_id)
    assert summary["brief_id"] == str(brief_id)
    assert summary["brief_date"] == "2026-08-02"
    assert "LEFT JOIN LATERAL (" in backend
    assert "b.id AS brief_id,b.brief_date,b.timezone AS brief_timezone" in backend
    assert 'SESSION_TAB_KEY = "rvbbit-calliope-session-tab-v1"' in script
    assert "function sessionTabsMarkup" in script
    assert 'data-session-tab="${escapeHtml(tab.id)}"' in script
    assert 'if (isBriefSession(session)) return "briefs"' in script
    assert "session?.brief_id && session?.brief_date" in script
    assert ".session-tabs" in css
    assert ".session-tab-panel" in css
    assert ".brief-session-card" in css


def test_observation_map_and_identity_resolution_stay_generic_and_explainable():
    props = {
        "state": {"name": "In progress"},
        "assignees": [
            {"name": "Ryan R", "email": "ryan@example.com"},
            {"name": "Other Person", "email": "other@example.com"},
        ],
    }
    assert calliope._brief_path_values(props, "$.state.name") == ["In progress"]
    assert calliope._brief_path_values(props, "$.assignees[*].email") == [
        "ryan@example.com",
        "other@example.com",
    ]
    assert calliope._brief_path_values(props, "$..not-supported") == []

    exact = calliope._brief_viewer_relation(
        "ryan@example.com",
        "Generic tasks",
        aliases={"*": {"ryan@example.com"}},
        assignee_emails=["ryan@example.com"],
    )
    assert exact["confidence"] == "exact"
    assert exact["truth"] == "observed"
    assert "OAuth" in exact["evidence"]

    possible = calliope._brief_viewer_relation(
        "ryan.r@example.com",
        "Generic tasks",
        aliases={"*": {"ryan.r@example.com"}},
        assignee_names=["Ryan R"],
    )
    assert possible["confidence"] == "possible"
    assert possible["truth"] == "resolved"
    assert possible["candidate"] == "Ryan R"

    confirmed = calliope._brief_viewer_relation(
        "ryan.r@example.com",
        "Generic tasks",
        aliases={"*": {"ryan.r@example.com"}, "generic tasks": {"ryan r"}},
        assignee_names=["Ryan R"],
    )
    assert confirmed["confidence"] == "confirmed"


def test_time_bounded_snapshot_filters_user_feedback_and_preserves_truth_sections(monkeypatch):
    now = datetime(2026, 8, 1, 15, tzinfo=timezone.utc)
    brief_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    relation = {
        "kind": "assigned_to",
        "confidence": "exact",
        "truth": "observed",
        "evidence": "Exact OAuth email match.",
    }

    def item(key, section, title, occurred):
        return {
            "id": key,
            "group": "knowledge",
            "kind": "document",
            "title": title,
            "summary": f"Observed {title}",
            "source": "Generic tasks",
            "occurred_at": occurred.isoformat(),
            "provenance": {
                "resolver": "brain_search",
                "brief_section": section,
                "observation_key": key,
                "viewer_relation": relation,
            },
        }

    monkeypatch.setattr(
        calliope,
        "_brief_identity_state",
        lambda *_args: ({"*": {"owner@example.com"}}, {"brain:hidden": "not_mine"}),
    )
    monkeypatch.setattr(
        calliope,
        "_brief_brain_observations",
        lambda *_args: (
            [
                item("brain:due", "needs_now", "Renewal due", now - timedelta(hours=1)),
                item("brain:hidden", "changed", "Not my ticket", now - timedelta(hours=2)),
                item("brain:possible", "possible", "Possible owner", now - timedelta(hours=3)),
            ],
            [{"key": "tasks", "label": "Generic tasks", "count": 3, "status": "ready"}],
            [],
        ),
    )
    monkeypatch.setattr(
        calliope,
        "_brief_internal_observations",
        lambda *_args: (
            [item("watch:1", "data_moved", "Revenue moved", now)],
            [{"key": "watches", "label": "Data watches", "count": 1, "status": "ready"}],
            [],
        ),
    )
    monkeypatch.setattr(
        calliope,
        "_brief_note_observations",
        lambda *_args: (
            [],
            [{"key": "daily-notes", "label": "Your notes", "count": 0, "status": "ready"}],
            [],
        ),
    )

    raw = calliope._personal_brief_snapshot(
        lambda: None,
        "owner@example.com",
        {
            "id": brief_id,
            "brief_date": "2026-08-01",
            "timezone": "America/New_York",
            "session_id": session_id,
            "window_start": now - timedelta(days=1),
            "window_end": now + timedelta(days=14),
        },
        now=now,
    )
    normalized = calliope._normalize_personal_brief_result(raw)

    assert normalized["mode"] == "personal_brief"
    assert [entry["id"] for entry in normalized["items"]] == [
        "brain:due",
        "watch:1",
        "brain:possible",
    ]
    assert normalized["brief"]["section_counts"] == {
        "focus": 0,
        "needs_now": 1,
        "coming_up": 0,
        "from_notes": 0,
        "changed": 0,
        "data_moved": 1,
        "continue_work": 0,
        "possible": 1,
    }
    assert normalized["brief"]["truth_levels"]["interpreted"].startswith("Reserved")
    assert {source["label"] for source in normalized["coverage"]} == {
        "Generic tasks",
        "Data watches",
        "Your notes",
    }


def test_brain_observer_accepts_source_level_maps_without_provider_specific_code():
    now = datetime(2026, 8, 1, 15, tzinfo=timezone.utc)
    doc_id = 42
    row = {
        "doc_id": doc_id,
        "uri": "generic:42",
        "title": "Renewal preparation",
        "author": None,
        "occurred_at": now - timedelta(hours=2),
        "ingested_at": now - timedelta(hours=1),
        "raw_meta": {},
        "props": {
            "workflow": {"state": "Open", "deadline": "2026-08-02"},
            "owner": {"mail": "owner@example.com"},
            "visibility": {"scope": "owner"},
            "permalink": "https://tasks.example.test/42",
        },
        "source": "Generic tasks",
        "config": {
            "provider": "generic-provider",
            "observation_map": {"assignee_emails": ["$.owner.mail"]},
        },
        "doc_type": "task",
        # The SQL expression merges this provider map with the source override.
        "observation_map": {
            "status": ["$.workflow.state"],
            "due_at": ["$.workflow.deadline"],
            "assignee_emails": ["$.owner.mail"],
            "viewer_scope": ["$.visibility.scope"],
            "url": ["$.permalink"],
        },
        "excerpt": "Prepare the renewal evidence before the customer call.",
    }

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None):
            if "information_schema.columns" in query:
                return _Result({"ok": True})
            if "FROM rvbbit.brain_documents" in query:
                assert "s.config->'observation_map'" in query
                assert "brain_visible_docs" in query
                assert "sources AS" in query
                assert "WHERE s.enabled" in query
                assert "JOIN LATERAL" in query
                assert "row_number() OVER (PARTITION BY s.source_id" in query
                assert "d.source_rank<=%s" in query
                assert "brain_doc_type(s.config)<>'system_learning'" in query
                return _Result(row)
            raise AssertionError(query)

    items, coverage, warnings = calliope._brief_brain_observations(
        Connection,
        "owner@example.com",
        now - timedelta(days=1),
        now + timedelta(days=14),
        now,
        ZoneInfo("America/New_York"),
        {"*": {"owner@example.com"}},
    )

    assert warnings == []
    assert len(items) == 1
    assert items[0]["title"] == "Renewal preparation"
    assert items[0]["url"] == "https://tasks.example.test/42"
    assert items[0]["provenance"]["brief_section"] == "needs_now"
    assert items[0]["provenance"]["viewer_relation"]["confidence"] == "exact"
    assert items[0]["provenance"]["viewer_relation"]["kind"] == "owner"
    assert coverage[0]["identity_status"] == "mapped"
    assert coverage[0]["available"] == 1


def test_real_fireflies_shape_maps_time_url_and_participants_without_new_extension_column():
    now = datetime(2026, 8, 1, 15, tzinfo=timezone.utc)
    row = {
        "doc_id": 77,
        "uri": "fireflies:77",
        "title": "Customer operating review",
        "author": None,
        "occurred_at": now - timedelta(hours=1),
        "ingested_at": now,
        "raw_meta": {},
        "props": {
            "dateString": (now + timedelta(days=1)).isoformat(),
            "meetingLink": "https://meet.example.test/77",
            "meetingAttendees": [
                {"email": "owner@example.com", "displayName": "Owner Person"},
            ],
            "participants": ["owner@example.com", "customer@example.com"],
            "organizerEmail": "facilitator@example.com",
        },
        "source": "Fireflies · meetings",
        "config": {"provider": "fireflies-meetings"},
        "doc_type": "meeting",
        "observation_map": {},
        "source_available": 50,
        "excerpt": "Review current risks and decisions.",
    }

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None):
            if "information_schema.columns" in query:
                return _Result({"ok": False})
            if "FROM rvbbit.brain_documents" in query:
                return _Result(row)
            raise AssertionError(query)

    items, coverage, warnings = calliope._brief_brain_observations(
        Connection,
        "owner@example.com",
        now - timedelta(days=1),
        now + timedelta(days=14),
        now,
        ZoneInfo("America/New_York"),
        {"*": {"owner@example.com"}},
    )

    assert warnings == []
    assert len(items) == 1
    assert items[0]["url"] == "https://meet.example.test/77"
    assert items[0]["provenance"]["brief_section"] == "coming_up"
    assert items[0]["provenance"]["viewer_relation"]["confidence"] == "exact"
    assert coverage[0]["available"] == 50
    assert coverage[0]["identity_status"] == "mapped"


def test_semantic_home_pins_are_private_focus_even_when_someone_else_owns_the_artifact():
    pin_id = str(uuid.uuid4())
    row = {
        "id": pin_id,
        "item_kind": "artifact",
        "source": {
            "kind": "artifact",
            "slug": "shared-operations",
            "tracking": "latest",
            "pinned_version": 2,
        },
        "presentation": {
            "title": "Shared operations",
            "description": "A company dashboard in my working set.",
            "app_kind": "dashboard",
        },
        "created_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "slug": "shared-operations",
        "name": "Shared operations",
        "description": "A company dashboard in my working set.",
        "runtime_kind": "html",
        "app_kind": "dashboard",
        "latest_version": 4,
        "artifact_updated_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
    }

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            assert "FROM rvbbit.calliope_boards" in query
            assert params == ("owner@example.com",)
            return _Result(row)

    items, coverage, warnings = calliope._brief_home_observations(
        Connection, "owner@example.com"
    )

    assert warnings == []
    assert len(items) == 1
    assert items[0]["handle"] == {
        "kind": "artifact", "slug": "shared-operations", "version": 4,
    }
    assert items[0]["provenance"]["brief_section"] == "focus"
    assert items[0]["provenance"]["viewer_relation"]["kind"] == "home_pin"
    assert items[0]["url"] == "/d/shared-operations"
    assert coverage == [{
        "key": "semantic-home",
        "label": "Semantic Home",
        "count": 1,
        "available": 1,
        "unavailable": 0,
        "status": "ready",
        "scope": "personal",
    }]


def test_snapshot_delta_is_exact_and_unchanged_generic_activity_is_not_called_changed():
    def item(key, section, *, status=None, version=None, title=None):
        provenance = {
            "observation_key": key,
            "brief_section": section,
            "status": status,
        }
        if version is not None:
            provenance["version"] = version
        return {
            "id": key,
            "title": title or key,
            "summary": "Stable observed detail",
            "source": "Test source",
            "provenance": provenance,
        }

    previous_items = [
        item("brain:stable", "changed", status="open"),
        item("home:1", "focus", version=2),
        item("brain:status", "needs_now", status="open"),
    ]
    current_items = [
        item("brain:stable", "changed", status="open"),
        item("home:1", "focus", version=4),
        item("brain:status", "needs_now", status="closed"),
        item("brain:new", "changed", status="open"),
    ]
    previous = {
        "surface_id": str(uuid.uuid4()),
        "date": "2026-08-01",
        "as_of": "2026-08-01T12:00:00+00:00",
        "payload": {"mode": "personal_brief", "items": previous_items},
    }

    visible, comparison = calliope._brief_apply_deltas(current_items, previous)

    assert [entry["id"] for entry in visible] == [
        "home:1", "brain:status", "brain:new",
    ]
    assert visible[0]["provenance"]["delta"] == {
        "kind": "changed",
        "fields": ["artifact version"],
        "compared_to_surface_id": previous["surface_id"],
    }
    assert visible[1]["provenance"]["delta"]["fields"] == ["status"]
    assert visible[2]["provenance"]["delta"]["kind"] == "new"
    assert comparison["counts"] == {
        "baseline": 0, "new": 1, "changed": 2, "unchanged": 1,
    }
    assert comparison["omitted_unchanged"] == 1


def test_whole_brief_handoff_is_a_compact_handle_index_not_full_document_bodies():
    surface_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    payload = {
        "mode": "personal_brief",
        "query": "Personal brief · 2026-08-01",
        "count": 1,
        "brief": {"date": "2026-08-01", "timezone": "America/New_York"},
        "searched": [{"key": "brain", "label": "Company memory", "count": 1}],
        "items": [{
            "id": "brain:42",
            "group": "knowledge",
            "kind": "document",
            "title": "Renewal ticket",
            "summary": "x" * 1_500,
            "source": "Generic tasks",
            "provenance": {
                "resolver": "brain_search",
                "doc_id": "42",
                "brief_section": "needs_now",
                "observation_key": "brain:42",
                "due_at": "2026-08-02T12:00:00Z",
                "viewer_relation": {"confidence": "exact", "kind": "assigned_to"},
            },
        }],
    }

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None):
            assert "kind='evidence'" in query
            return _Result({"id": surface_id, "payload": payload})

    hydrated = calliope._hydrate_evidence_refs(
        Connection,
        session_id,
        [{"surface_id": surface_id, "evidence_id": calliope._EVIDENCE_SET_HANDLE}],
    )
    snapshot = hydrated[0]
    assert snapshot["kind"] == "personal-brief"
    assert snapshot["title"] == "Personal brief · 2026-08-01"
    assert "x" * 300 not in str(snapshot)
    locator = snapshot["provenance"]["result_handles"][0]["locator"]
    assert locator["doc_id"] == "42"
    assert locator["brief_section"] == "needs_now"
    assert locator["observation_key"] == "brain:42"


def test_brief_routes_and_both_native_entry_points_ship_as_one_feature():
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    page = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")
    server = (HERE / "server.py").read_text(encoding="utf-8")

    assert '@mcp.custom_route("/api/calliope/briefs/status", methods=["GET"])' in backend
    assert '@mcp.custom_route("/api/calliope/briefs/today", methods=["POST"])' in backend
    assert '@mcp.custom_route("/api/calliope/briefs/feedback", methods=["POST"])' in backend
    assert '"personal_briefs": True' in backend
    assert 'turn_kind="brief"' in backend
    assert 'id="personal-brief-open"' in page
    assert "function renderPersonalBrief" in script
    assert "function openPersonalBrief" in script
    assert "function briefProvenanceTooltip" in script
    assert "function briefDeltaTooltip" in script
    assert "function briefEntityTooltip" in script
    assert 'data-tooltip-kind="provenance"' in script
    assert 'launch.get("brief")' in script
    assert "data-brief-feedback" in script
    assert "data-brief-action" in script
    assert "function prepareBriefAction" in script
    assert '["focus", "My focus"' in script
    assert "_brief_home_observations" in backend
    assert "_brief_apply_deltas" in backend
    assert ".personal-brief" in css
    assert ".brief-truth-legend" in css
    assert ".brief-truth[data-calliope-tooltip]" in css
    assert 'id="gallery-personal-brief"' in server
    assert 'href="/calliope?brief=1"' in server
    assert "function loadGalleryBrief" in server


def test_brief_timezone_rejects_ambiguous_names_and_sections_are_deterministic():
    name, zone = calliope._brief_timezone("America/New_York")
    assert name == "America/New_York"
    assert isinstance(zone, ZoneInfo)
    with pytest.raises(ValueError, match="IANA"):
        calliope._brief_timezone("local business time")
    now = datetime(2026, 8, 1, 15, tzinfo=timezone.utc)
    relation = {"confidence": "exact"}
    assert calliope._brief_section(
        now=now,
        doc_type="ticket",
        status="open",
        due_at=now + timedelta(hours=5),
        starts_at=None,
        occurred_at=now - timedelta(days=1),
        relation=relation,
    ) == "needs_now"
    assert calliope._brief_section(
        now=now,
        doc_type="meeting",
        status="",
        due_at=None,
        starts_at=now + timedelta(days=3),
        occurred_at=None,
        relation=relation,
    ) == "coming_up"
