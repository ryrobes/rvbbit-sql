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


def _custom_mcp_action() -> dict:
    return next(
        action for action in calliope._ACTION_LIBRARY_SEED
        if action["id"] == "mcp.connect_custom"
    )


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
    assert "function syncActionFieldVisibility" in script
    assert "field.visible_when" in script
    assert 'data-resolve-action="' in script
    assert ".surface.kind-action" in css
    assert ".action-field[hidden]" in css
    assert ".action-library-empty[hidden],.action-library-selected[hidden]" in css
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


def test_custom_mcp_action_is_seeded_with_transport_aware_secure_contract():
    action = _custom_mcp_action()
    captured = []

    class Connection:
        def execute(self, _query, params):
            captured.append(params)

    calliope._seed_action_catalog(Connection())
    seeded = next(params for params in captured if params[0] == action["id"])
    config = json.loads(seeded[9])

    assert action["category"] == "connect"
    assert action["executor"] == "mcp_connect"
    assert action["risk"] == "organization_change"
    assert config["generic_mcp"] is True
    assert config["requirements"] == []
    assert any(field.get("visible_when") for field in action["fields"])
    assert next(
        field for field in action["fields"] if field["key"] == "secret:MCP_SECRET_VALUES"
    )["secret_group"] is True


def test_custom_http_mcp_plan_freezes_config_and_redacts_named_secure_values():
    action = _custom_mcp_action()
    secret_value = "client-token-must-never-persist"
    values, redacted, envelope = calliope._normalize_action_inputs(action, {
        "server_name": "client_ops",
        "transport": "http",
        "url": "https://mcp.client.example/mcp",
        "auth_token_name": "CLIENT_MCP_TOKEN",
        "secret:MCP_SECRET_VALUES": json.dumps({"CLIENT_MCP_TOKEN": secret_value}),
    })
    spec, secure_values = calliope._custom_mcp_spec(values, envelope)
    plan = calliope._action_plan_document(action, values)

    assert values["transport"] == "http"
    assert values["url"] == "https://mcp.client.example/mcp"
    assert "command" not in values
    assert "args" not in values
    assert redacted["secret:MCP_SECRET_VALUES"] == "••••••"
    assert secure_values == {"CLIENT_MCP_TOKEN": secret_value}
    assert spec["declared_secret_names"] == {"CLIENT_MCP_TOKEN"}
    assert secret_value not in json.dumps(spec, default=list)
    assert secret_value not in json.dumps(plan)
    assert "custom http MCP server client_ops" in plan["summary"]


def test_custom_stdio_mcp_declares_refs_and_rejects_literal_or_undeclared_secrets():
    action = _custom_mcp_action()
    values, _redacted, envelope = calliope._normalize_action_inputs(action, {
        "server_name": "client_stdio",
        "transport": "stdio",
        "command": "npx",
        "args": json.dumps(["-y", "@client/custom-mcp", "--token=${CLIENT_TOKEN}"]),
        "environment": json.dumps({"REGION": "us-east-2"}),
        "secret_names": "CLIENT_SECRET",
        "secret:MCP_SECRET_VALUES": json.dumps({
            "CLIENT_TOKEN": "token-value",
            "CLIENT_SECRET": "secret-value",
        }),
    })
    spec, secure_values = calliope._custom_mcp_spec(values, envelope)

    assert "url" not in values
    assert spec["args"] == ["-y", "@client/custom-mcp", "--token=${CLIENT_TOKEN}"]
    assert spec["declared_secret_names"] == {"CLIENT_TOKEN", "CLIENT_SECRET"}
    assert spec["environment"]["CLIENT_TOKEN"] == "${CLIENT_TOKEN}"
    assert spec["environment"]["CLIENT_SECRET"] == "${CLIENT_SECRET}"
    assert set(secure_values) == {"CLIENT_TOKEN", "CLIENT_SECRET"}
    assert "token-value" not in json.dumps(spec, default=list)

    bad_values = dict(values)
    bad_values["environment"] = json.dumps({"API_TOKEN": "literal-secret"})
    with pytest.raises(ValueError, match="looks secret"):
        calliope._custom_mcp_spec(bad_values, {})
    with pytest.raises(ValueError, match="not declared"):
        calliope._custom_mcp_spec(
            values,
            {"MCP_SECRET_VALUES": json.dumps({"SURPRISE_TOKEN": "not-reviewed"})},
        )


def test_custom_mcp_rejects_credentials_in_endpoint_url():
    action = _custom_mcp_action()
    values, _redacted, envelope = calliope._normalize_action_inputs(action, {
        "server_name": "unsafe_url",
        "transport": "http",
        "url": "https://mcp.example/mcp?api_key=must-not-persist",
    })
    with pytest.raises(ValueError, match="appears to contain a credential"):
        calliope._custom_mcp_spec(values, envelope)

    reserved = dict(values, server_name="public", url="https://mcp.example/mcp")
    with pytest.raises(ValueError, match="reserved"):
        calliope._custom_mcp_spec(reserved, {})


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


def test_custom_mcp_apply_uses_only_frozen_config_and_one_way_secret_envelope(monkeypatch):
    action = _custom_mcp_action()
    planned_values, _redacted, _secrets = calliope._normalize_action_inputs(action, {
        "server_name": "client_custom",
        "transport": "http",
        "url": "https://mcp.client.example/mcp",
        "auth_token_name": "CLIENT_TOKEN",
    })
    run_id = str(uuid.uuid4())
    run = {
        "id": run_id,
        "status": "planned",
        "action_snapshot": action,
        "input_values": planned_values,
    }
    final_run = {**run}
    captured = {}
    mutations = []

    monkeypatch.setattr(
        calliope,
        "_action_run_for_owner",
        lambda *_args: final_run,
    )

    async def no_saved_secrets(_conn_factory, server):
        assert server == "client_custom"
        return set(), True

    async def execute_steps(_factory, passed_run_id, spec, secrets, saved_names):
        captured.update({
            "run_id": passed_run_id,
            "spec": spec,
            "secrets": secrets,
            "saved_names": saved_names,
        })
        return ({"server": spec["server"]}, {"reachable": True})

    def finish(_factory, _run_id, status, **kwargs):
        final_run.update({"status": status, **kwargs})

    monkeypatch.setattr(calliope, "_mcp_gateway_secret_names", no_saved_secrets)
    monkeypatch.setattr(calliope, "_execute_custom_mcp_steps", execute_steps)
    monkeypatch.setattr(
        calliope,
        "_mark_action_running",
        lambda *_args: mutations.append("running"),
    )
    monkeypatch.setattr(calliope, "_finish_action_run", finish)

    result = asyncio.run(calliope.execute_action_with_secure_inputs(
        object,
        "trusted@example.com",
        run_id,
        {
            # This ordinary value must not be allowed to rewrite the frozen plan.
            "url": "https://attacker.invalid/mcp",
            "secret:MCP_SECRET_VALUES": json.dumps({
                "CLIENT_TOKEN": "transient-client-token",
            }),
        },
    ))

    assert mutations == ["running"]
    assert captured["run_id"] == run_id
    assert captured["spec"]["url"] == "https://mcp.client.example/mcp"
    assert captured["secrets"] == {"CLIENT_TOKEN": "transient-client-token"}
    assert captured["saved_names"] == set()
    assert result["run"]["status"] == "complete"
    assert "transient-client-token" not in json.dumps(result)


def test_custom_mcp_steps_register_discover_generate_and_verify_without_sql_secrets(monkeypatch):
    calls = []
    pushed = []
    step_updates = []
    secret_value = "opaque-client-token"

    class Result:
        def __init__(self, rows=None):
            self.rows = rows if isinstance(rows, list) else ([] if rows is None else [rows])

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return self.rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            calls.append((query, params))
            if query.startswith("SELECT name,transport"):
                return Result()
            if query.startswith("SELECT EXISTS"):
                return Result({
                    "schema_exists": False,
                    "schema_relations": 0,
                    "foreign_functions": 0,
                })
            if "register_mcp_server" in query:
                return Result({})
            if "refresh_mcp_server" in query:
                return Result({"tools": 2})
            if "generate_mcp_wrappers" in query:
                return Result({"sql_functions": 2})
            if "generate_mcp_operators" in query:
                return Result({"operators": 2})
            if query.startswith("SELECT name,description"):
                return Result([
                    {"name": "lookup_customer", "description": "Find a customer"},
                    {"name": "list_projects", "description": "List projects"},
                ])
            if query.startswith("SELECT coalesce(array_agg"):
                return Result({"names": []})
            if "mcp_probe" in query:
                return Result({
                    "probe": {"reachable": True, "latency_ms": 11, "n_tools": 2},
                })
            if query.startswith("SELECT (SELECT count"):
                return Result({
                    "tools": 2,
                    "resources": 1,
                    "sql_functions": 2,
                    "operators": 2,
                })
            if query.startswith("SELECT p.proname"):
                return Result([
                    {"name": "lookup_customer", "arguments": "customer_id text"},
                    {"name": "list_projects", "arguments": ""},
                ])
            raise AssertionError(query)

    async def push_secret(_factory, server, name, value):
        pushed.append((server, name, value))

    monkeypatch.setattr(calliope, "_push_mcp_gateway_secret", push_secret)
    monkeypatch.setattr(
        calliope,
        "_set_action_step",
        lambda _factory, _run_id, step, status, result=None: step_updates.append(
            (step, status, result or {})
        ),
    )
    spec = {
        "server": "client_custom",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@client/custom-mcp"],
        "environment": {"CLIENT_TOKEN": "${CLIENT_TOKEN}"},
        "url": None,
        "auth_token_name": None,
        "timeout_ms": 30_000,
        "description": "Client tools",
        "create_sql_functions": True,
        "create_operators": True,
        "declared_secret_names": {"CLIENT_TOKEN"},
    }

    result, verification = asyncio.run(calliope._execute_custom_mcp_steps(
        Connection,
        str(uuid.uuid4()),
        spec,
        {"CLIENT_TOKEN": secret_value},
        set(),
    ))

    register_call = next(call for call in calls if "register_mcp_server" in call[0])
    assert register_call[1][0:4] == (
        "client_custom", "stdio", "npx", ["-y", "@client/custom-mcp"],
    )
    assert json.loads(register_call[1][4]) == {"CLIENT_TOKEN": "${CLIENT_TOKEN}"}
    assert pushed == [("client_custom", "CLIENT_TOKEN", secret_value)]
    assert any("refresh_mcp_server" in query for query, _params in calls)
    assert any("generate_mcp_wrappers" in query for query, _params in calls)
    assert any("generate_mcp_operators" in query for query, _params in calls)
    assert verification == {
        "server": "client_custom",
        "transport": "stdio",
        "reachable": True,
        "latency_ms": 11,
        "tools": 2,
        "resources": 1,
        "sql_functions": 2,
        "operators": 2,
    }
    assert result["base_sql_function"] == "rvbbit.mcp_call(server, tool, args jsonb)"
    assert "rvbbit.mcp_call('client_custom', 'lookup_customer'" in result["direct_sql_example"]
    assert '"client_custom"."lookup_customer"' in result["typed_sql_example"]
    assert [update[:2] for update in step_updates] == [
        ("inspect", "running"), ("inspect", "complete"),
        ("register", "running"), ("register", "complete"),
        ("secrets", "running"), ("secrets", "complete"),
        ("discover", "running"), ("discover", "complete"),
        ("verify", "running"), ("verify", "complete"),
    ]
    assert secret_value not in json.dumps(calls)
    assert secret_value not in json.dumps(result)


def test_custom_mcp_refuses_to_replace_an_unrelated_sql_schema(monkeypatch):
    queries = []

    class Result:
        def __init__(self, value=None):
            self.value = value

        def fetchone(self):
            return self.value

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params):
            queries.append(query)
            if query.startswith("SELECT name,transport"):
                return Result()
            if query.startswith("SELECT EXISTS"):
                return Result({
                    "schema_exists": True,
                    "schema_relations": 3,
                    "foreign_functions": 0,
                })
            raise AssertionError("registration must not run after a schema collision")

    monkeypatch.setattr(calliope, "_set_action_step", lambda *_args, **_kwargs: None)
    spec = {
        "server": "client_data",
        "transport": "http",
        "command": None,
        "args": [],
        "environment": {},
        "url": "https://mcp.client.example/mcp",
        "auth_token_name": None,
        "timeout_ms": 30_000,
        "description": None,
        "create_sql_functions": True,
        "create_operators": True,
        "declared_secret_names": set(),
    }

    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(calliope._execute_custom_mcp_steps(
            Connection, str(uuid.uuid4()), spec, {}, set()
        ))
    assert not any("register_mcp_server" in query for query in queries)


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
