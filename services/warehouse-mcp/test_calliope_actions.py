"""Contracts for Calliope's trusted-organization Action Library."""
from __future__ import annotations

import inspect
import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import calliope  # noqa: E402
import server  # noqa: E402


def _linear_capability(*, connected: bool = False) -> tuple[dict, dict]:
    capability = {
        "id": "mcp/linear",
        "name": "linear",
        "title": "Linear",
        "description": "Issues, projects, teams, and comments.",
        "kind": "mcp",
        "tags": ["mcp", "issues", "projects"],
        "operators": ["linear_get_issue", "linear_create_issue"],
        "manifest": {
            "secrets": [{
                "name": "LINEAR_API_KEY",
                "env_var": "LINEAR_API_KEY",
                "label": "Linear API key",
                "required": True,
                "secret": True,
            }],
        },
        "runtime_ready": connected,
    }
    inventory = {
        "mcp_servers": ([{"name": "linear"}] if connected else []),
        "brain_sources": [],
        "capabilities": [capability],
        "personal": {"briefs": 0, "notes": 0, "inbox_open": 0},
        "warehouse_ready": True,
    }
    return capability, inventory


def test_action_schema_routes_tools_and_ui_ship_as_one_contract():
    migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0235_calliope_action_library.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    server_source = (HERE / "server.py").read_text(encoding="utf-8")
    page = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")
    compose = (ROOT / "docker" / "docker-compose.uber.yml").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_action_catalog" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_action_runs" in migration
    assert "input_redacted jsonb" in migration
    assert "status IN ('planned','running','complete','failed')" in migration
    assert "0235_calliope_action_library" in registry
    assert "conn.execute(_ACTION_DDL)" in inspect.getsource(calliope.ensure_tables)
    assert '@mcp.custom_route("/api/calliope/actions", methods=["GET"])' in backend
    assert '"/api/calliope/actions/{action_id}/plan"' in backend
    assert '"/api/calliope/action-runs/{run_id}/execute"' in backend
    assert 'mcp.tool(name="search_calliope_actions")' in server_source
    assert 'mcp.tool(name="plan_calliope_action")' in server_source
    assert 'mcp.tool(name="execute_calliope_action")' in server_source
    assert 'id="action-library-dialog"' in page
    assert 'id="action-create-plan"' in page
    assert 'id="action-apply"' in page
    assert "function openActionLibrary" in script
    assert "function pollActionRun" in script
    assert 'data-resolve-action="' in script
    assert ".surface.kind-action" in css
    assert compose.count("RVBBIT_GATEWAY_TOKEN: ${RVBBIT_GATEWAY_TOKEN:-}") >= 3


def test_action_mcp_tools_register_with_typed_schemas():
    # FastMCP inspects raw annotations while looking for Context parameters.
    # The server module postpones annotations, so this catches both startup
    # failures and accidental loss of the argument contracts agents receive.
    mcp = server._build_mcp()
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    search = tools["search_calliope_actions"].parameters
    assert set(search["properties"]) == {"session_id", "query", "category", "limit"}
    assert search["required"] == ["session_id", "query"]

    plan = tools["plan_calliope_action"].parameters
    assert set(plan["properties"]) == {"session_id", "action_id", "inputs"}
    assert plan["required"] == ["session_id", "action_id"]

    execute = tools["execute_calliope_action"].parameters
    assert set(execute["properties"]) == {"session_id", "run_id"}
    assert execute["required"] == ["session_id", "run_id"]


def test_guided_action_sessions_have_metadata_and_a_session_tab():
    session_id = uuid.uuid4()
    surface_id = uuid.uuid4()
    summary = calliope._session_json({
        "id": session_id,
        "title": "Action · Watch an important metric",
        "action_handoff_surface_id": surface_id,
        "action_id": "monitor.metric_watch",
        "action_title": "Watch an important metric",
    })
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")

    assert summary["id"] == str(session_id)
    assert summary["action_handoff_surface_id"] == str(surface_id)
    assert summary["action_id"] == "monitor.metric_watch"
    assert "AS action_handoff_surface_id" in backend
    assert "sf.source->>'origin'='calliope_action_library'" in backend
    assert "ac.action_handoff_surface_id,ac.action_id,ac.action_title" in backend
    assert 'LAST_SESSION_KEY = "rvbbit-calliope-last-session-v1"' in script
    assert 'SESSION_TAB_KEY = "rvbbit-calliope-session-tab-v1"' in script
    assert 'TAB_SESSIONS_KEY = "rvbbit-calliope-tab-sessions-v1"' in script
    assert "function isActionSession" in script
    assert 'if (isActionSession(session)) return "actions"' in script
    assert "function sessionTabsMarkup" in script
    assert "function rememberSession" in script
    assert "function restoreSessionRailState" in script
    assert "data-session-action-id" in script
    assert 'role="tablist"' in script
    assert ".session-tabs" in css
    assert ".action-session-card" in css


def test_dynamic_linear_action_is_outcome_oriented_and_resolves_workflow_edges():
    _capability, inventory = _linear_capability()
    actions = calliope._dynamic_capability_actions(inventory)

    assert len(actions) == 1
    action = actions[0]
    assert action["id"] == "mcp.connect:mcp~linear"
    assert action["category"] == "connect"
    assert action["executor"] == "mcp_connect"
    assert action["state"] == "connect"
    assert "mcp:linear" in action["resolves"]
    assert "project_ticket" in action["resolves"]
    secret = next(field for field in action["fields"] if field["type"] == "secret")
    assert secret["secret_name"] == "LINEAR_API_KEY"
    assert secret["required"] is True

    requirement = calliope._action_requirement_state("project_ticket", inventory)
    assert requirement == {
        "ref": "project_ticket",
        "available": False,
        "remediation_action_id": "mcp.connect:mcp~linear",
    }


def test_action_input_normalization_never_places_secret_values_in_plan_inputs():
    _capability, inventory = _linear_capability()
    action = calliope._dynamic_capability_actions(inventory)[0]
    values, redacted, secrets = calliope._normalize_action_inputs(action, {
        "server_name": "linear",
        "secret:LINEAR_API_KEY": "lin_api_super-secret",
    })

    assert values == {"server_name": "linear"}
    assert redacted == {
        "server_name": "linear",
        "secret:LINEAR_API_KEY": "••••••",
    }
    assert secrets == {"LINEAR_API_KEY": "lin_api_super-secret"}
    plan = calliope._action_plan_document(action, values)
    assert "super-secret" not in json.dumps(plan)
    assert [step["id"] for step in plan["steps"]] == [
        "inspect", "register", "secrets", "discover", "verify",
    ]


def test_plan_receipt_is_immutable_redacted_and_requires_a_separate_apply(monkeypatch):
    _capability, inventory = _linear_capability()
    action = calliope._dynamic_capability_actions(inventory)[0]
    inserted = {}

    class Result:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            assert "INSERT INTO rvbbit.calliope_action_runs" in query
            inserted["params"] = params
            return Result({
                "id": params[0],
                "owner_email": params[1],
                "session_id": params[2],
                "action_id": params[3],
                "action_version": params[4],
                "action_snapshot": json.loads(params[5]),
                "status": "planned",
                "input_values": json.loads(params[6]),
                "input_redacted": json.loads(params[7]),
                "plan": json.loads(params[8]),
                "steps": json.loads(params[9]),
                "result": {},
                "verification": {},
            })

    monkeypatch.setattr(calliope, "_action_by_id", lambda *_args: action)
    run = calliope.plan_action(
        Connection,
        "trusted@example.com",
        action["id"],
        {
            "server_name": "linear",
            "secret:LINEAR_API_KEY": "lin_api_super-secret",
        },
    )

    assert uuid.UUID(run["id"])
    assert run["status"] == "planned"
    assert run["input_values"] == {"server_name": "linear"}
    assert run["input_redacted"]["secret:LINEAR_API_KEY"] == "••••••"
    assert "super-secret" not in json.dumps(run)
    assert inserted["params"][1] == "trusted@example.com"


def test_secure_mcp_apply_fails_before_mutation_when_gateway_has_no_key(monkeypatch):
    _capability, inventory = _linear_capability()
    action = calliope._dynamic_capability_actions(inventory)[0]
    run_id = str(uuid.uuid4())
    run = {
        "id": run_id,
        "status": "planned",
        "action_snapshot": action,
        "input_values": {"server_name": "linear"},
    }
    mutations = []
    monkeypatch.setattr(calliope, "_action_run_for_owner", lambda *_args: run)

    inspected_servers = []

    async def no_saved_secrets(_conn_factory, server):
        inspected_servers.append(server)
        return set(), True

    monkeypatch.setattr(calliope, "_mcp_gateway_secret_names", no_saved_secrets)
    monkeypatch.setattr(calliope, "_mark_action_running", lambda *_args: mutations.append("running"))

    with pytest.raises(ValueError, match="LINEAR_API_KEY"):
        asyncio.run(calliope.execute_action_with_secure_inputs(
            object, "trusted@example.com", run_id, {"server_name": "unapproved-name"}
        ))
    assert inspected_servers == ["linear"]
    assert mutations == []


def test_secure_mcp_apply_requires_gateway_preflight_before_registration(monkeypatch):
    _capability, inventory = _linear_capability()
    action = calliope._dynamic_capability_actions(inventory)[0]
    run_id = str(uuid.uuid4())
    run = {
        "id": run_id,
        "status": "planned",
        "action_snapshot": action,
        "input_values": {"server_name": "linear"},
    }
    mutations = []
    monkeypatch.setattr(calliope, "_action_run_for_owner", lambda *_args: run)

    async def unavailable_gateway(*_args):
        return set(), False

    monkeypatch.setattr(calliope, "_mcp_gateway_secret_names", unavailable_gateway)
    monkeypatch.setattr(calliope, "_mark_action_running", lambda *_args: mutations.append("running"))

    with pytest.raises(ValueError, match="secure MCP gateway"):
        asyncio.run(calliope.execute_action_with_secure_inputs(
            object,
            "trusted@example.com",
            run_id,
            {"secret:LINEAR_API_KEY": "lin_api_transient-only"},
        ))
    assert mutations == []


def test_gateway_rejection_never_echoes_response_body_or_secret(monkeypatch):
    leaked_value = "lin_api_must-not-escape"

    class Response:
        status_code = 500
        text = f"failed request contained {leaked_value}"

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, *, headers, json):
            assert headers["content-type"] == "application/json"
            assert json["value"] == leaked_value
            return Response()

    monkeypatch.setattr(calliope, "_mcp_gateway_url", lambda _factory: "http://gateway")
    monkeypatch.setattr(calliope.httpx, "AsyncClient", Client)

    with pytest.raises(RuntimeError) as error:
        asyncio.run(calliope._push_mcp_gateway_secret(
            object, "linear", "LINEAR_API_KEY", leaked_value
        ))
    assert leaked_value not in str(error.value)
    assert "failed request" not in str(error.value)
    receipt_error = calliope._action_error_text(
        f"probe failed with {leaked_value}; bearer abcdefghijklmnop; password=hunter2",
        [leaked_value],
    )
    assert leaked_value not in receipt_error
    assert "abcdefghijklmnop" not in receipt_error
    assert "hunter2" not in receipt_error


def test_action_receipts_project_to_the_stage_without_secret_values():
    run_id = str(uuid.uuid4())
    surfaces = calliope._project_tool_result(
        "plan_calliope_action",
        {
            "run": {
                "id": run_id,
                "action_snapshot": {
                    "id": "mcp.connect:mcp~linear",
                    "title": "Connect Linear",
                    "risk": "organization_change",
                },
                "status": "planned",
                "input_values": {"server_name": "linear"},
                "input_redacted": {"secret:LINEAR_API_KEY": "••••••"},
                "plan": {"summary": "Connect Linear"},
                "steps": [],
            }
        },
        {"action_id": "mcp.connect:mcp~linear"},
        "tool-call-1",
    )

    assert surfaces[0]["kind"] == "action"
    assert surfaces[0]["lineage_key"] == f"action-run:{run_id}"
    assert surfaces[0]["presentation"] == {"view": "action_receipt"}
    assert "super-secret" not in json.dumps(surfaces)
