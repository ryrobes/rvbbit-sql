"""Contracts for identity-scoped reusable Calliope Playbooks."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import calliope  # noqa: E402
import playbook_access  # noqa: E402
import server  # noqa: E402


def _contract(**changes):
    value = {
        "outcome": "Produce a reviewed operating brief.",
        "when_to_use": ["A leader asks for the current operating picture"],
        "triggers": ["weekly review"],
        "when_not_to_use": [],
        "context_to_gather": ["Current governed metrics"],
        "method": ["Find current evidence", "Explain material exceptions"],
        "guardrails": ["Do not infer missing values"],
        "deliverable": "A concise evidence-backed brief.",
        "completion_criteria": ["Every material claim has evidence"],
        "fallbacks": [],
        "required_capabilities": ["metric"],
        "preferred_capabilities": ["compare"],
        "optional_capabilities": [],
    }
    value.update(changes)
    return value


def test_contract_is_strict_and_normalizes_every_searchable_array():
    normalized = playbook_access.normalize_contract(_contract(triggers=None))

    assert normalized["triggers"] == []
    assert normalized["method"] == [
        "Find current evidence",
        "Explain material exceptions",
    ]
    assert set(normalized) == {
        "outcome", "when_to_use", "triggers", "when_not_to_use",
        "context_to_gather", "method", "guardrails", "deliverable",
        "completion_criteria", "fallbacks", "required_capabilities",
        "preferred_capabilities", "optional_capabilities",
    }

    with pytest.raises(playbook_access.PlaybookError, match="Unsupported contract fields"):
        playbook_access.normalize_contract({**_contract(), "tool_sequence": ["run_sql"]})
    with pytest.raises(playbook_access.PlaybookError, match="method must contain"):
        playbook_access.normalize_contract(_contract(method=[]))


def test_playbook_tools_are_typed_and_never_accept_caller_identity(monkeypatch):
    monkeypatch.setattr(server, "_record", lambda *_args, **_kwargs: None)
    mcp = FastMCP("playbook-contract")
    server._register(mcp)

    expected = {
        "draft_calliope_playbook",
        "read_calliope_playbook",
        "approve_calliope_playbook",
        "set_calliope_playbook_access",
        "archive_calliope_playbook",
    }
    assert expected <= set(mcp._tool_manager._tools)
    for name in expected:
        properties = mcp._tool_manager._tools[name].parameters["properties"]
        assert "caller_email" not in properties
        assert "owner_email" not in properties
        assert "acting_as" not in properties

    draft = mcp._tool_manager._tools["draft_calliope_playbook"].parameters
    contract = draft["$defs"]["_PlaybookContractInput"]
    assert set(contract["required"]) == {
        "outcome", "when_to_use", "method", "deliverable", "completion_criteria"
    }
    assert contract["additionalProperties"] is False
    assert draft["properties"]["readiness"]["enum"] == [
        "ready", "degraded", "blocked"
    ]
    assert draft["properties"]["source_turn_id"]["anyOf"][0]["type"] == "string"


def test_capability_search_repairs_the_approved_playbook_projection(monkeypatch):
    class Result:
        def __init__(self, row=None, rows=None):
            self.row, self.rows = row, rows or []

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            self.calls.append((query, params))
            if "to_regprocedure('rvbbit.capability_search_stale()')" in query:
                return Result({"ok": False})
            if "to_regprocedure('rvbbit.capability_playbook_search_stale()')" in query:
                return Result({"ok": True})
            if "capability_playbook_search_stale() AS s" in query:
                return Result({"s": True})
            if "sync_calliope_playbook_capabilities" in query:
                return Result({"result": {"status": "ok"}})
            if "capability_search_for" in query:
                return Result(rows=[{
                    "kind": "cap_playbook",
                    "name": "playbook.weekly-operations~f8db183a",
                    "score": 1.0,
                    "doc": "Approved reusable method",
                }])
            raise AssertionError(query)

    connection = Connection()
    monkeypatch.setattr(server, "_conn", lambda: connection)
    authorization = server.ApplicationAuthorizationContext(
        actor="owner@example.com",
        subject="owner@example.com",
        attributed_subject="owner@example.com",
        client_id="direct-oauth-client",
        mode="direct_oauth",
        assurance="oauth_access_token",
    )
    token = server._AUTHORIZATION_CONTEXT.set(authorization)
    try:
        result = server.tool_capability_search(
            "weekly operations", kinds=["cap_playbook"]
        )
    finally:
        server._AUTHORIZATION_CONTEXT.reset(token)

    assert result["index_rebuilt"].startswith("capability index was stale")
    assert result["matches"][0]["kind"] == "cap_playbook"
    calls = [query for query, _params in connection.calls]
    assert next(i for i, query in enumerate(calls) if "sync_calliope" in query) < next(
        i for i, query in enumerate(calls) if "capability_search_for" in query
    )


def test_migration_makes_versions_immutable_private_and_approved_only():
    migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations" /
        "0283_calliope_playbooks.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_playbooks" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_playbook_versions" in migration
    assert "ADD COLUMN IF NOT EXISTS playbook_source_turn_id" in migration
    assert "REFERENCES rvbbit.capability_access_policies" in migration
    assert "Calliope Playbook versions are immutable" in migration
    assert "v.version=p.approved_version" in migration
    assert "p.approved_version IS NOT NULL" in migration
    assert "rvbbit.capability_can_use" in migration
    assert "source_turn_id uuid REFERENCES rvbbit.calliope_turns" in migration
    assert "source_from_ordinal integer" in migration
    assert "source_through_ordinal integer" in migration
    assert "sketch_revision integer" in migration
    search_document = migration[
        migration.index("CREATE OR REPLACE FUNCTION rvbbit.calliope_playbook_capability_doc"):
        migration.index("CREATE OR REPLACE FUNCTION rvbbit.sync_calliope_playbook_capability")
    ]
    assert "source_session_id" not in search_document
    assert "source_turn_id" not in search_document
    assert "evidence_refs" not in search_document
    assert "sketch_id" not in search_document
    assert '"0283_calliope_playbooks"' in registry


class _Rows:
    def __init__(self, *, rows=None):
        self.rows = rows or []

    def fetchall(self):
        return self.rows


class _RoutineConnection:
    def execute(self, query, _params=None):
        if "FROM rvbbit.calliope_workflows" in query:
            return _Rows()
        if "FROM rvbbit.calliope_instruments" in query:
            return _Rows()
        if "FROM rvbbit.calliope_watches" in query:
            return _Rows()
        if "FROM rvbbit.calliope_playbooks" in query:
            return _Rows(rows=[{
                "routine": {
                    "id": "f8db183a-f028-411c-9eb0-c67b81835872",
                    "capability_name": "playbook.weekly-operations~f8db183a",
                    "owner_email": "owner@example.com",
                    "latest_version": 2,
                    "approved_version": 1,
                    "access_revision": 3,
                    "updated_at": "2026-08-11T12:00:00+00:00",
                    "approved_at": "2026-08-10T12:00:00+00:00",
                },
                "version": {
                    "version": 2,
                    "title": "Weekly operations review",
                    "synopsis": "Turn current signals into a reviewed operating brief.",
                    "readiness": "ready",
                    "semantic_contract": _contract(),
                    "source_session_id": "dc924430-f3dc-4b56-b8f2-1e676213e88f",
                    "change_summary": "Tighten exception handling.",
                },
                "is_owner": True,
                "grants": 1,
                "everyone": False,
            }])
        raise AssertionError(query)


def test_library_places_playbooks_in_routines_without_a_builder():
    items = calliope._library_routine_inventory(
        _RoutineConnection(), "owner@example.com"
    )

    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "playbook"
    assert item["section"] == "routines"
    assert item["label"] == "Weekly operations review"
    assert item["detail"]["has_newer_draft"] is True
    assert item["detail"]["method"] == _contract()["method"]
    assert item["open_url"].endswith("dc924430-f3dc-4b56-b8f2-1e676213e88f")
    assert "approve" in item["intents"]


def test_tool_result_projects_a_compact_native_playbook_surface():
    result = {
        "playbook": {
            "id": "f8db183a-f028-411c-9eb0-c67b81835872",
            "title": "Weekly operations review",
            "synopsis": "A reusable operating method.",
            "version": 1,
            "approved": False,
            "contract": _contract(),
        },
        "created": True,
    }
    surfaces = calliope._project_tool_result(
        "draft_calliope_playbook",
        result,
        {"session_id": "dc924430-f3dc-4b56-b8f2-1e676213e88f"},
        "tool-call-1",
    )

    assert len(surfaces) == 1
    assert surfaces[0]["kind"] == "playbook"
    assert surfaces[0]["lineage_key"] == "playbook:f8db183a-f028-411c-9eb0-c67b81835872"
    assert surfaces[0]["payload"]["playbook"]["contract"]["outcome"] == _contract()["outcome"]


def test_calliope_browser_assets_render_playbooks_without_a_builder():
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")
    sketch_source = (
        HERE / "calliope-sketch" / "src" / "main.tsx"
    ).read_text(encoding="utf-8")
    sketch_template = (HERE / "calliope" / "sketch.html").read_text(encoding="utf-8")
    dockerfile = (HERE / "Dockerfile").read_text(encoding="utf-8")

    assert "function renderPlaybook(surface)" in script
    assert "playbook: renderPlaybook" in script
    assert "playbook: \"≋\"" in script
    assert "Save as Playbook" in script
    assert "data-playbook-approve" in script
    assert "playbook_source_turn_id" in script
    assert "/api/calliope/playbooks/" in script
    assert "data-playbook-sketch" in script
    assert "/sketch?version=" in script
    assert 'data-inbox-mode="playbooks"' in (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    assert "function renderPersonalPlaybooks()" in script
    assert "/api/calliope/playbooks?limit=100" in script
    assert "data-personal-playbook-action" in script
    assert "function dreamPlaybookMarkup(dream)" in script
    assert "data-dream-playbook-accept" in script
    assert "/playbook`," in script
    assert ".playbook-surface" in css
    assert ".playbook-plan-frame" in css
    assert ".personal-playbook-card" in css
    assert ".calliope-dream-playbook" in css
    assert ".message-playbook-action" in css
    assert "data-source-url=\"__SKETCH_SOURCE_URL__\"" in sketch_template
    assert "data-presentation=\"__SKETCH_PRESENTATION__\"" in sketch_template
    assert "viewModeEnabled={readOnly}" in sketch_source
    assert "presentation ? undefined : topRight" in sketch_source
    assert "playbook-builder" not in script
    assert "playbook_access.py" in dockerfile


def test_playbook_personal_inventory_and_dream_acceptance_routes_ship_together():
    source = (HERE / "calliope.py").read_text(encoding="utf-8")
    worker = (HERE / "calliope_dreams.py").read_text(encoding="utf-8")

    assert '"/api/calliope/playbooks", methods=["GET"]' in source
    assert '"/api/calliope/dreams/{dream_id}/playbook", methods=["POST"]' in source
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_dream_playbooks" in source
    assert "_dream_playbook_sketch_operations" in source
    assert "accept_calliope_dream_playbook" in source
    assert '"kind": "dream"' in source
    assert "normalize_dream_playbook" in worker


def test_dream_playbook_visual_method_uses_the_supported_sketch_dsl():
    proposal = {
        "title": "Weekly operations review",
        "contract": _contract(),
    }
    operations = calliope._dream_playbook_sketch_operations(proposal)
    elements, summary = calliope._apply_sketch_operations([], operations, 2)

    active = [item for item in elements if not item.get("isDeleted")]
    assert any(item.get("id") == "playbook-outcome" for item in active)
    assert len([item for item in active if item.get("type") == "arrow"]) == len(
        _contract()["method"]
    )
    assert summary["operation_count"] == len(operations)
    step = next(item for item in active if item.get("id") == "playbook-step-1")
    assert step["label"]["text"].startswith("1. ")


class _One:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _PlaybookSketchConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=None):
        if "FROM rvbbit.calliope_playbooks WHERE" in query:
            return _One({
                "id": "f8db183a-f028-411c-9eb0-c67b81835872",
                "capability_kind": "cap_playbook",
                "capability_name": "playbook.weekly-operations~f8db183a",
                "owner_email": "owner@example.com",
                "latest_version": 2,
                "approved_version": 1,
                "access_revision": 1,
                "archived": False,
                "updated_at": "2026-08-11T12:00:00+00:00",
            })
        if "SELECT * FROM rvbbit.calliope_playbook_versions" in query:
            return _One({
                "playbook_id": "f8db183a-f028-411c-9eb0-c67b81835872",
                "version": 2,
                "title": "Weekly operations review",
                "synopsis": "A reusable operating method.",
                "readiness": "ready",
                "contract_hash": "a" * 64,
                "semantic_contract": _contract(),
                "change_summary": "",
                "source_session_id": "dc924430-f3dc-4b56-b8f2-1e676213e88f",
                "source_turn_id": None,
                "source_from_ordinal": 1,
                "source_through_ordinal": 3,
                "sketch_id": "c1bf4e66-161b-4795-a769-e21f2d6a2b7b",
                "sketch_revision": 5,
                "evidence_refs": [],
                "created_at": "2026-08-11T12:00:00+00:00",
            })
        if "SELECT v.sketch_id,v.sketch_revision" in query:
            return _One({
                "sketch_id": "c1bf4e66-161b-4795-a769-e21f2d6a2b7b",
                "sketch_revision": 5,
                "sketch_title": "Shared Sketch",
                "actor": "calliope",
                "actor_email": "owner@example.com",
                "operation_count": 8,
                "change_summary": {"element_ids": ["plan-step"]},
                "elements": [
                    {"id": "plan-step", "type": "rectangle"},
                    {"id": "old-step", "type": "rectangle", "isDeleted": True},
                ],
                "app_state": {"gridModeEnabled": False},
                "created_at": "2026-08-11T12:00:00+00:00",
            })
        raise AssertionError(query)


def test_playbook_sketch_is_an_exact_immutable_read_only_revision():
    result = playbook_access.read_sketch(
        lambda: _PlaybookSketchConnection(),
        "owner@example.com",
        "f8db183a-f028-411c-9eb0-c67b81835872",
        2,
    )

    assert result["read_only"] is True
    assert result["presentation"] is True
    assert result["playbook"]["version"] == 2
    assert result["sketch"]["revision"] == 5
    assert result["sketch"]["element_count"] == 1
    assert result["sketch"]["can_undo_calliope"] is False
    assert result["sketch"]["scene"]["elements"][0]["id"] == "plan-step"


class _ReceiptConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=None):
        assert "FROM rvbbit.calliope_surfaces" in query
        return _Rows(rows=[{
            "id": "31b13075-f5ba-4ea3-ad96-ac99f6ce2e67",
            "kind": "playbook",
            "title": "Weekly operations review",
            "tool_name": "warehouse__read_calliope_playbook",
            "payload": {
                "playbook": {
                    "id": "f8db183a-f028-411c-9eb0-c67b81835872",
                    "capability_name": "playbook.weekly-operations~f8db183a",
                    "title": "Weekly operations review",
                    "version": 4,
                }
            },
            "source": {},
        }])


def test_response_receipt_pins_the_exact_playbook_version_used():
    receipt = calliope._response_receipt(
        lambda: _ReceiptConnection(),
        "90e85c70-0de8-4924-82f9-6fbca5467790",
        [],
        [],
        [],
    )

    assert receipt["playbooks"] == [{
        "id": "f8db183a-f028-411c-9eb0-c67b81835872",
        "capability_name": "playbook.weekly-operations~f8db183a",
        "title": "Weekly operations review",
        "version": 4,
        "action": "used",
        "surface_id": "31b13075-f5ba-4ea3-ad96-ac99f6ce2e67",
    }]
    assert receipt["summary"]["playbooks"] == 1


def test_calliope_prompt_requires_loaded_playbook_disclosure_and_private_distillation():
    source = (HERE / "calliope.py").read_text(encoding="utf-8")

    assert "exact approved version with read_calliope_playbook" in source
    assert "human which Playbook and version you are using" in source
    assert "Playbook you did not load" in source
    assert "CALLIOPE PLAYBOOK DISTILLATION" in source
    assert "Create a private draft only. Do not approve or share it." in source
    assert '"/api/calliope/playbooks/{playbook_id}/approve"' in source
