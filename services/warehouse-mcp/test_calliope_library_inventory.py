"""Contracts for the centralized, principal-aware Calliope Library inventory."""
from __future__ import annotations

import datetime as dt
import inspect
import json
import sys
import uuid
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import calliope  # noqa: E402


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _McpConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, query, _params=None):
        assert "FROM rvbbit.mcp_servers" in query
        return _Rows(self.rows)


class _BrainConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, query, _params=None):
        assert "FROM rvbbit.brain_sources" in query
        return _Rows(self.rows)


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _TeamResult:
    def __init__(self, *, rows=None, row=None):
        self.rows = rows or []
        self.row = row

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


class _TeamConnection:
    def __init__(self, teams, *, allowed=False):
        self.teams = teams
        self.allowed = allowed

    def execute(self, query, _params=None):
        if "FROM rvbbit.teams t LEFT JOIN rvbbit.team_members" in query:
            return _TeamResult(rows=self.teams)
        if "WHERE t.system_key='admins'" in query:
            return _TeamResult(row={"allowed": self.allowed})
        raise AssertionError(query)


def _item(ref, *, section="tools", state="healthy", label=None):
    return calliope._library_inventory_item(
        ref=ref,
        kind="mcp_server" if section == "tools" else "metric",
        section=section,
        label=label or ref,
        summary=f"Configured {ref}",
        state=state,
        health=f"Observed {state}",
    )


def test_inventory_contract_ships_backend_modes_stage_and_typed_handoff():
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    page = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")

    assert '@mcp.custom_route("/api/calliope/inventory", methods=["GET"])' in backend
    assert '@mcp.custom_route("/api/calliope/inventory/handoff", methods=["POST"])' in backend
    assert '"kind": "inventory"' in backend
    assert '"inventory_refs": refs' in backend
    assert '"library_origin": "system_inventory"' in backend
    assert 'id="library-modes"' in page
    assert 'data-library-mode="inventory"' in page
    assert 'data-library-mode="discover"' in page
    assert 'data-library-mode="changes"' in page
    assert 'id="inventory-detail-context"' in page
    assert "function loadInventory" in script
    assert "function handoffInventory" in script
    assert "function renderInventory(surface)" in script
    assert "inventory: renderInventory" in script
    assert "data-inventory-focus" in script
    assert ".surface.kind-inventory" in css
    assert ".inventory-detail-contract" in css


def test_teams_ship_as_a_native_library_surface_with_admin_gated_editing():
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    page = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")

    assert '@mcp.custom_route("/api/calliope/teams", methods=["GET"])' in backend
    assert '@mcp.custom_route("/api/calliope/team-people", methods=["GET"])' in backend
    assert '@mcp.custom_route("/api/calliope/teams", methods=["POST"])' in backend
    assert '@mcp.custom_route("/api/calliope/teams/{team_id}", methods=["PATCH"])' in backend
    assert '"teams": "Teams"' in backend
    assert 'id="team-create"' in page
    assert 'id="team-management"' in page
    assert 'id="action-library-bootstrap"' in page
    assert 'id="action-library-discovery" class="action-library-discovery" hidden' in page
    assert 'id="action-library-workspace" class="action-library-workspace" hidden' in page
    assert "function beginTeamCreation" in script
    assert 'state.inventorySection === "teams"' in script
    assert "Only members of Admins can create Teams" in script
    assert "function renderTeamManagement" in script
    assert "function renderTeamManagementPreservingPicker" in script
    assert "nextList.scrollTop = listScrollTop" in script
    assert 'data-team-member-pill=' in script
    assert "function searchTeamPeople" in script
    assert "function identityMatchesQuery" in script
    assert "function setLibraryBootstrapping" in script
    assert "state.libraryReady = true" in script
    assert "function saveTeamDraft" in script
    assert "Only members of Admins may create Teams." in script
    assert 'membershipRule: team?.membership_rule || "explicit_members"' in script
    assert 'team.systemKey === "everyone"' in script
    assert "Any authenticated user" in script
    assert "Dynamic membership · evaluated at request time" in script
    assert ".team-management" in css
    assert ".team-member-pills" in css
    assert ".team-member-pill.wildcard" in css
    assert ".action-library-bootstrap" in css


def test_team_inventory_exposes_flat_membership_without_granting_observed_people_access():
    rows = [
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "slug": "admins",
            "name": "Admins",
            "description": "Application administrators",
            "system_key": "admins",
            "archived": False,
            "revision": 2,
            "created_by": "system",
            "created_at": "2026-08-07T10:00:00+00:00",
            "updated_by": "admin@example.com",
            "updated_at": "2026-08-07T11:00:00+00:00",
            "last_changed_at": "2026-08-07T11:00:00+00:00",
            "members": ["admin@example.com"],
        },
        {
            "id": "00000000-0000-4000-8000-000000000002",
            "slug": "everyone",
            "name": "Everyone",
            "description": "Automatically includes every user with a verified application sign-in.",
            "system_key": "everyone",
            "archived": False,
            "revision": 1,
            "created_by": "system",
            "created_at": "2026-08-07T10:00:00+00:00",
            "updated_by": "system",
            "updated_at": "2026-08-07T10:00:00+00:00",
            "last_changed_at": None,
            "members": [],
        },
        {
            "id": "c487af4a-fe1a-475b-b813-0393fe1bf0c1",
            "slug": "finance",
            "name": "Finance",
            "description": "Finance collaborators",
            "system_key": None,
            "archived": False,
            "revision": 1,
            "created_by": "admin@example.com",
            "created_at": "2026-08-07T11:00:00+00:00",
            "updated_by": "admin@example.com",
            "updated_at": "2026-08-07T11:00:00+00:00",
            "last_changed_at": "2026-08-07T11:00:00+00:00",
            "members": ["finance@example.com"],
        },
    ]
    items = calliope._library_team_inventory(
        _TeamConnection(rows, allowed=True),
        "admin@example.com",
    )

    admins, everyone, finance = items
    assert [item["ref"] for item in items] == [
        "team:00000000-0000-4000-8000-000000000001",
        "team:00000000-0000-4000-8000-000000000002",
        "team:c487af4a-fe1a-475b-b813-0393fe1bf0c1",
    ]
    assert all(item["section"] == "teams" for item in items)
    assert all(item["section_label"] == "Teams" for item in items)
    assert "people:directory" not in {
        item["ref"] for item in items
    }
    assert admins["ref"] == "team:00000000-0000-4000-8000-000000000001"
    assert admins["detail"]["protected"] is True
    assert everyone["state"] == "healthy"
    assert everyone["detail"]["protected"] is True
    assert everyone["detail"]["dynamic_membership"] is True
    assert everyone["detail"]["membership_rule"] == "authenticated_users"
    assert everyone["detail"]["member_count"] is None
    assert everyone["detail"]["members"] == []
    assert {fact["label"]: fact["value"] for fact in everyone["facts"]}["Members"] == "Any authenticated user"
    assert finance["detail"]["members"] == ["finance@example.com"]
    assert finance["visibility"] == "organization"


def test_inventory_item_is_json_stable_and_does_not_infer_unknown_state():
    now = dt.datetime(2026, 8, 4, 12, 30, tzinfo=dt.timezone.utc)
    identifier = uuid.uuid4()
    item = calliope._library_inventory_item(
        ref=f"example:{identifier}",
        kind="example",
        section="meaning",
        label="Example",
        summary="Stable payload",
        state="mystery",
        updated_at=now,
        facts=[calliope._library_inventory_fact("Identifier", identifier)],
        detail={"identifier": identifier, "observed_at": now},
    )

    assert item["state"] == "ready"
    assert item["state_label"] == "Ready"
    assert item["facts"][0]["value"] == str(identifier)
    assert item["detail"]["identifier"] == str(identifier)
    assert item["detail"]["observed_at"] == str(now)
    json.dumps(item)


def test_mcp_inventory_marks_read_surfaces_without_exposing_server_environment():
    secret = "must-not-cross-the-inventory-boundary"
    rows = [{
        "name": "linear",
        "transport": "stdio",
        "url": None,
        "command": "linear-mcp",
        "timeout_ms": 30_000,
        "description": "Linear issues and projects",
        "created_at": dt.datetime(2026, 8, 4, tzinfo=dt.timezone.utc),
        "tools": 3,
        "resources": 1,
        "tool_names": ["linear_search_issues", "linear_create_issue", "linear_get_project"],
        "last_call": {"at": "2026-08-04T10:00:00+00:00", "failed": False, "tool": "linear_search_issues"},
        # Real collectors never select env/auth columns. This catches a future
        # accidental pass-through of arbitrary row fields.
        "env": {"LINEAR_API_KEY": secret},
    }]

    item = calliope._library_mcp_inventory(_McpConnection(rows), "person@example.com")[0]
    serialized = json.dumps(item)

    assert item["ref"] == "mcp:linear"
    assert item["state"] == "healthy"
    assert item["detail"]["knowledge_source_candidate"] is True
    assert item["detail"]["read_candidate_tools"] == [
        "linear_search_issues", "linear_get_project",
    ]
    assert "knowledge_source" in item["intents"]
    assert calliope._library_inventory_matches(item, "linear_search_issues")
    assert secret not in serialized
    assert "LINEAR_API_KEY" not in serialized
    assert calliope._library_safe_endpoint(
        "https://user:password@mcp.example.test/path?token=secret#fragment"
    ) == "https://mcp.example.test/path"


def test_google_meet_inventory_surfaces_transcript_coverage():
    rows = [{
        "source": {
            "source_id": 42,
            "label": "Google Meet",
            "kind": "google_meet",
            "enabled": True,
            "config": {"provider": "google-meet", "connector": "gmeet_connector"},
            "last_synced_at": "2026-08-06T08:00:00+00:00",
        },
        "docs": 18,
        "meeting_briefs": 7,
        "chunks": 93,
        "pending_grants": 0,
        "last_sync": {
            "trigger": "auto",
            "finished_at": "2026-08-06T08:00:00+00:00",
            "added": 3,
            "errors": 0,
            "detail": {
                "connector_stats": {
                    "transcript_files": 3,
                    "meetings_without_transcript": 2,
                    "subjects_polled": 64,
                    "warnings": 0,
                    "auto_transcription": {"enabled": 4, "errors": 0},
                },
                "meeting_summaries": {
                    "available": True,
                    "created": 2,
                    "updated": 1,
                    "skipped": 0,
                },
            },
        },
    }]

    item = calliope._library_brain_inventory(_BrainConnection(rows), "person@example.com")[0]

    assert item["state"] == "attention"
    assert "2 recent meetings" in item["health"]
    facts = {fact["label"]: fact["value"] for fact in item["facts"]}
    assert facts["Transcripts found"] == 3
    assert facts["Meeting briefs"] == 7
    assert facts["Summary status"] == "Available"
    assert facts["Missing transcripts"] == 2
    assert facts["Workspace users polled"] == 64
    assert facts["Auto-transcripts enabled"] == 4
    assert item["detail"]["connector_stats"]["transcript_files"] == 3
    assert item["detail"]["meeting_summaries"]["created"] == 2


def test_inventory_snapshot_isolates_collectors_and_filters_exact_refs(monkeypatch):
    collectors = {
        "_library_brain_inventory": lambda _conn, _owner: [
            _item("metric:revenue", section="meaning", state="attention", label="Revenue"),
        ],
        "_library_mcp_inventory": lambda _conn, _owner: [
            _item("mcp:linear", label="Linear"),
            _item("mcp:github", label="GitHub"),
        ],
        "_library_cube_inventory": lambda _conn, _owner: (_ for _ in ()).throw(RuntimeError("old install")),
        "_library_metric_inventory": lambda _conn, _owner: [],
        "_library_routine_inventory": lambda _conn, _owner: [],
        "_library_team_inventory": lambda _conn, _owner: [],
        "_library_personal_inventory": lambda _conn, _owner: [],
    }
    for name, collector in collectors.items():
        monkeypatch.setattr(calliope, name, collector)

    snapshot = calliope._library_inventory_snapshot(
        lambda: _Connection(),
        "person@example.com",
        query="linear",
        section="tools",
        refs=["mcp:linear"],
    )

    assert [item["ref"] for item in snapshot["items"]] == ["mcp:linear"]
    assert snapshot["total"] == 1
    assert snapshot["available_total"] == 3
    assert snapshot["summary"] == {
        "total": 3,
        "needs_attention": 1,
        "healthy": 2,
        "ready": 0,
        "working": 0,
        "inactive": 0,
    }
    assert snapshot["warnings"] == [
        "Cubes are temporarily unavailable (RuntimeError).",
    ]


@pytest.mark.parametrize("field,value", [("section", "secrets"), ("state", "broken")])
def test_inventory_snapshot_rejects_unknown_filters(field, value):
    with pytest.raises(ValueError):
        calliope._library_inventory_snapshot(
            lambda: _Connection(),
            "person@example.com",
            **{field: value},
        )


def test_personal_collectors_are_scoped_to_the_signed_in_owner():
    source = inspect.getsource(calliope._library_personal_inventory)

    assert source.count("lower(%s)") >= 3
    assert "WHERE lower(owner_email)=lower(%s)" in source
    assert "WHERE lower(rm.principal)=lower(%s)" in source
    assert "FROM rvbbit.brain_role_members" in source
