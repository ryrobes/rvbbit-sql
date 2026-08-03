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


def test_brain_work_projection_schema_is_registered_and_runtime_self_healing():
    migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0239_calliope_brain_work_inventory.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )
    upgrade = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "pg_rvbbit--4.2.8--4.2.9.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_brain_work_profiles" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_brain_work_items" in migration
    assert "owner_email" not in migration  # ownership is deliberately read-time only
    assert "assignee_ids" in migration
    assert "$.state.type" in migration
    assert "0239_calliope_brain_work_inventory" in registry
    assert "identity_directory" in migration
    assert "linear_getUsers" in migration
    assert "linear_getUsers" in upgrade
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_brain_work_profiles" in calliope._BRAIN_WORK_DDL
    assert "linear_getUsers" in calliope._BRAIN_WORK_DDL
    ensure_source = inspect.getsource(calliope.ensure_tables)
    assert "conn.execute(_BRAIN_WORK_DDL)" in ensure_source
    assert "SELECT rvbbit.migrate()" not in ensure_source


@pytest.mark.parametrize(
    ("source", "props", "expected_kind", "expected_lifecycle"),
    [
        (
            {"source_id": 1, "doc_type": "document", "document_count": 12},
            {
                "key": "OPS-42",
                "self": "https://jira.example.test/rest/api/issue/OPS-42",
                "fields": {
                    "status": {
                        "name": "In Progress",
                        "statusCategory": {"key": "indeterminate"},
                    },
                    "assignee": {
                        "displayName": "Ryan R",
                        "emailAddress": "ryan@example.com",
                        "accountId": "jira-user-1",
                    },
                    "updated": "2026-08-01T12:00:00Z",
                    "priority": {"name": "High"},
                },
            },
            "issue",
            "in_progress",
        ),
        (
            {"source_id": 2, "doc_type": "document", "document_count": 8},
            {
                "number": 81,
                "state": "open",
                "html_url": "https://github.example.test/acme/repo/pull/81",
                "updated_at": "2026-08-01T12:00:00Z",
                "requested_reviewers": [{"login": "ryanr", "id": 77}],
                "user": {"login": "author", "id": 88},
                "pull_request": {"url": "https://api.github.example.test/pulls/81"},
            },
            "pull_request",
            "open",
        ),
        (
            {"source_id": 3, "doc_type": "document", "document_count": 25},
            {
                "number": "INC0010042",
                "sys_id": "snow-42",
                "active": True,
                "state": {"display_value": "New", "value": "1"},
                "assigned_to": {"display_value": "Ryan R", "value": "snow-user-1"},
                "sys_updated_on": "2026-08-01 12:00:00",
                "priority": {"display_value": "2 - High"},
            },
            "ticket",
            "open",
        ),
    ],
)
def test_work_shape_profiler_recognizes_common_systems_without_provider_code(
    source, props, expected_kind, expected_lifecycle
):
    profile = calliope._brief_work_profile(source, [{"props": props}])
    item = calliope._brief_work_index_item(
        {
            "doc_id": source["source_id"],
            "source_id": source["source_id"],
            "title": "Structured work record",
            "author": None,
            "occurred_at": None,
            "ingested_at": "2026-08-01T12:00:00Z",
            "props": props,
            "raw_meta": {},
        },
        profile,
        ZoneInfo("UTC"),
    )

    assert profile["status"] == "active"
    assert profile["confidence"] >= 0.7
    assert profile["work_kind"] == expected_kind
    assert profile["profile_source"] == "inferred"
    assert item["lifecycle"] == expected_lifecycle
    assert item["relations"]


def test_work_profiler_does_not_mistake_meetings_for_assigned_work():
    profile = calliope._brief_work_profile(
        {"source_id": 4, "doc_type": "meeting", "document_count": 50},
        [{"props": {
            "dateString": "2026-08-04T15:00:00Z",
            "meetingLink": "https://meet.example.test/4",
            "participants": ["owner@example.com"],
            "organizerEmail": "owner@example.com",
        }}],
    )

    assert profile["status"] == "ignored"
    assert profile["qualification"]["lifecycle"] is False


def test_work_inventory_reapplies_brain_acl_and_keeps_old_open_assignments(monkeypatch):
    now = datetime(2026, 8, 3, 15, tzinfo=timezone.utc)
    rows = [
        {
            "doc_id": 101,
            "source_id": 8,
            "profile_doc_type": "ticket",
            "profile_shape_hash": "linear-shape",
            "work_kind": "ticket",
            "identifier": "ENG-101",
            "title": "Old but still open",
            "url": "https://linear.example.test/ENG-101",
            "status_label": "In Progress",
            "lifecycle": "in_progress",
            "due_at": now - timedelta(days=2),
            "priority_label": "High",
            "source_updated_at": now - timedelta(days=90),
            "relations": {
                "assignee": {
                    "ids": ["linear-owner-1"],
                    "names": ["Owner Person"],
                }
            },
            "project": {"name": "Warehouse"},
            "facts": {},
            "source": "Linear",
            "last_synced_at": now - timedelta(hours=1),
            "last_document_ingested_at": now - timedelta(hours=1),
            "profile_error": None,
        },
        {
            "doc_id": 102,
            "source_id": 8,
            "profile_doc_type": "ticket",
            "profile_shape_hash": "linear-shape",
            "work_kind": "ticket",
            "identifier": "ENG-102",
            "title": "Possible display-name assignment",
            "url": "https://linear.example.test/ENG-102",
            "status_label": "Todo",
            "lifecycle": "open",
            "due_at": None,
            "priority_label": None,
            "source_updated_at": now - timedelta(days=45),
            "relations": {"assignee": {"names": ["Owner"]}},
            "project": {},
            "facts": {},
            "source": "Linear",
            "last_synced_at": now - timedelta(hours=1),
            "last_document_ingested_at": now - timedelta(hours=1),
            "profile_error": None,
        },
    ]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            if "FROM rvbbit.calliope_brain_work_profiles" in query and "FILTER" in query:
                return _Result({
                    "active": 1, "possible": 0, "errors": 0,
                    "last_indexed_at": now - timedelta(hours=1),
                })
            if "WITH visible AS MATERIALIZED" in query:
                assert "rvbbit.brain_visible_docs(%s)" in query
                assert "wi.lifecycle IN ('open','in_progress','blocked','review')" in query
                assert params[0] == "owner@example.com"
                assert "linear-owner-1" in params[1]
                return _Result(rows)
            raise AssertionError(query)

    monkeypatch.setattr(calliope, "_brief_refresh_brain_work_index", lambda *_args: [])
    monkeypatch.setattr(
        calliope,
        "_brief_directory_identity_aliases",
        lambda *_args: ({"linear": {"linear-owner-1", "owner person"}}, []),
    )
    observations, inventory, coverage, warnings = calliope._brief_work_inventory(
        Connection,
        "owner@example.com",
        now,
        ZoneInfo("UTC"),
        {"*": {"owner@example.com"}},
        {},
    )

    assert warnings == []
    assert [item["id"] for item in observations] == ["brain:101", "brain:102"]
    assert inventory["summary"] == {
        "open": 1,
        "overdue": 1,
        "due_soon": 0,
        "blocked": 0,
        "review": 0,
        "possible": 1,
        "stale": 0,
        "shown": 2,
        "total": 2,
    }
    assert inventory["groups"]["overdue"] == 1
    assert inventory["groups"]["possible"] == 1
    assert observations[0]["provenance"]["brief_section"] == "needs_now"
    assert observations[0]["provenance"]["viewer_relation"]["resolver"] == "identity_directory"
    assert observations[1]["provenance"]["viewer_relation"]["confidence"] == "possible"
    assert coverage[0]["identity_status"] == "mapped"


def test_expanded_work_inventory_survives_normalization_and_is_openable():
    raw_items = [{
        "id": f"brain:{index}",
        "group": "knowledge",
        "kind": "document",
        "handle": {"kind": "document", "doc_id": str(index), "chunk_idx": 0},
        "title": f"Work {index}",
        "source": "Generic work",
        "provenance": {"work_bucket": "open", "observation_key": f"brain:{index}"},
    } for index in range(100)]
    normalized = calliope._normalize_brief_work_inventory({
        "available": True,
        "summary": {"open": 100, "shown": 100, "total": 100},
        "groups": {"open": 100},
        "items": raw_items,
    })
    payload = {"items": raw_items[:24], "work_inventory": normalized}

    assert len(normalized["items"]) == 100
    assert len(calliope._brief_payload_items(payload)) == 100
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")
    assert "function renderBriefWork(surface)" in script
    assert "function surfaceEvidenceItems(surface)" in script
    assert 'data-brief-feedback="relevant"' in script
    assert ".brief-work-inventory" in css


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


def test_daily_brief_stage_renders_sections_once_and_hides_superseded_snapshots():
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")

    assert "function visibleStageSurfaces()" in script
    assert 'surface.payload?.mode !== "personal_brief" || surface.id === latest.id' in script
    assert "const visibleSurfaces = visibleStageSurfaces();" in script
    assert "${sections}" in script
    assert "Work index healthy · identity not mapped" in script


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

    external_username = calliope._brief_viewer_relation(
        "ryan.r@example.com",
        "GitHub pull requests",
        aliases={"*": {"ryan.r@example.com"}},
        reviewer_ids=["ryanr"],
    )
    assert external_username["confidence"] == "possible"
    assert external_username["alias_kind"] == "external_id"


def test_provider_directory_resolves_oauth_email_to_external_assignment_identity():
    owner = "owner@example.com"
    external_id = "linear-owner-1"
    directory = {
        "server": "linear",
        "tool": "linear_getUsers",
        "args": {},
        "email_paths": ["$.email"],
        "aliases": {
            "external_id": ["$.id"],
            "name": ["$.name", "$.displayName"],
        },
        "ttl_seconds": 900,
    }
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            if "FROM rvbbit.calliope_brain_work_profiles wp" in query:
                assert params == (owner,)
                return _Result({
                    "source_id": 8,
                    "label": "Linear · all",
                    "identity_directory": directory,
                })
            if "FROM rvbbit.mcp_rows" in query:
                calls.append((query, params))
                assert params[:3] == ("linear", "linear_getUsers", "{}")
                return _Result([
                    {"identity_record": {
                        "id": external_id,
                        "name": "Owner Person",
                        "displayName": "owner",
                        "email": owner,
                    }},
                    {"identity_record": {
                        "id": "linear-other-1",
                        "name": "Other Person",
                        "email": "other@example.com",
                    }},
                ])
            raise AssertionError(query)

    calliope._BRIEF_IDENTITY_DIRECTORY_CACHE.clear()
    aliases, warnings = calliope._brief_directory_identity_aliases(Connection, owner)
    aliases_again, warnings_again = calliope._brief_directory_identity_aliases(
        Connection, owner
    )

    assert warnings == warnings_again == []
    assert aliases == aliases_again == {
        "linear · all": {external_id, "owner person", "owner"}
    }
    assert len(calls) == 1  # the bounded directory result is cached, not refetched
    relation = calliope._brief_viewer_relation(
        owner,
        "Linear · all",
        aliases={"*": {owner}},
        directory_aliases=aliases,
        assignee_ids=[external_id],
    )
    assert relation["confidence"] == "exact"
    assert relation["truth"] == "resolved"
    assert relation["resolver"] == "identity_directory"
    assert "OAuth email" in relation["evidence"]


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


def test_calendar_section_keeps_nearest_events_and_uses_dense_view_allowance(monkeypatch):
    now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    events = []
    for index in range(50):
        starts_at = now + timedelta(hours=index + 1)
        events.append({
            "id": f"calendar:primary:event-{index}",
            "group": "knowledge",
            "kind": "calendar-event",
            "subtype": "calendar",
            "title": f"Calendar event {index}",
            "summary": "Scheduled on the connected primary calendar.",
            "source": "Google Calendar",
            "occurred_at": starts_at.isoformat(),
            "provenance": {
                "resolver": "private_google_calendar",
                "coverage_key": "google-calendar",
                "brief_section": "coming_up",
                "observation_key": f"calendar:primary:event-{index}",
                "starts_at": starts_at.isoformat(),
                "viewer_relation": {
                    "kind": "calendar_owner",
                    "confidence": "exact",
                    "truth": "observed",
                },
            },
        })

    monkeypatch.setattr(calliope, "_brief_previous_snapshot", lambda *_args: None)
    monkeypatch.setattr(calliope, "_brief_identity_state", lambda *_args: ({}, {}))
    monkeypatch.setattr(
        calliope,
        "_brief_calendar_observations",
        lambda *_args: (
            list(reversed(events)),
            [{
                "key": "google-calendar",
                "label": "Google Calendar",
                "count": len(events),
                "available": len(events),
                "status": "ready",
                "scope": "personal",
            }],
            [],
        ),
    )
    empty_observer = lambda *_args: ([], [], [])
    monkeypatch.setattr(calliope, "_brief_brain_observations", empty_observer)
    monkeypatch.setattr(calliope, "_brief_internal_observations", empty_observer)
    monkeypatch.setattr(calliope, "_brief_note_observations", empty_observer)

    raw = calliope._personal_brief_snapshot(
        lambda: None,
        "owner@example.com",
        {
            "id": str(uuid.uuid4()),
            "brief_date": "2026-08-03",
            "timezone": "UTC",
            "session_id": str(uuid.uuid4()),
            "window_start": now - timedelta(days=1),
            "window_end": now + timedelta(days=14),
        },
        now=now,
        include_google_calendar=True,
    )
    normalized = calliope._normalize_personal_brief_result(raw)

    assert len(normalized["items"]) == 42
    assert normalized["items"][0]["id"] == "calendar:primary:event-0"
    assert normalized["items"][-1]["id"] == "calendar:primary:event-41"
    assert normalized["brief"]["section_counts"]["coming_up"] == 42
    assert normalized["brief"]["section_available_counts"]["coming_up"] == 50
    assert normalized["brief"]["section_omitted_counts"]["coming_up"] == 8
    assert normalized["coverage"][0]["count"] == 42
    assert normalized["coverage"][0]["matched_count"] == 50


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


def test_coming_up_is_a_sunday_first_two_week_calendar_without_losing_overflow():
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")

    assert '"Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"' in script
    assert "function renderBriefComingUp" in script
    assert "Array.from({ length: 14 }" in script
    assert 'key === "coming_up"' in script
    assert "Later in the Brief horizon" in script
    assert 'data-brief-action="prepare"' in script
    assert "briefCalendarEventTooltip" in script
    assert ".brief-calendar-weekdays,.brief-calendar-days" in css
    assert "grid-template-columns:repeat(7,minmax(0,1fr))" in css
    assert ".brief-calendar-overflow" in css


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
