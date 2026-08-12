"""Contracts for Calliope's trusted-organization Action Library."""
from __future__ import annotations

import inspect
import asyncio
import hashlib
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

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


def _linear_brain_action() -> dict:
    return next(
        action for action in calliope._ACTION_LIBRARY_SEED
        if action["id"] == "knowledge.linear_brain"
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
    assert '"/api/calliope/actions/{action_id}/execute"' in backend
    assert '"/api/calliope/actions/{action_id}/plan"' in backend
    assert '"/api/calliope/action-runs/{run_id}/execute"' in backend
    assert 'mcp.tool(name="search_calliope_actions")' in server_source
    assert 'mcp.tool(name="plan_calliope_action")' not in server_source
    assert 'mcp.tool(name="execute_calliope_action")' not in server_source
    assert 'mcp.tool(name="administer_calliope_action")' in server_source
    assert 'mcp.tool(name="administer_local_sql")' in server_source
    assert 'mcp.tool(name="mirror_status")' in server_source
    assert 'id="action-library-dialog"' in page
    assert 'id="action-create-plan"' in page
    assert 'id="action-apply"' in page
    assert "function openActionLibrary" in script
    assert "function executeActionDirect" in script
    assert "/execute`" in script
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

    assert "plan_calliope_action" not in tools
    assert "execute_calliope_action" not in tools

    administer = tools["administer_calliope_action"].parameters
    assert set(administer["properties"]) == {"session_id", "action_id", "inputs"}
    assert administer["required"] == ["session_id", "action_id"]

    local_sql = tools["administer_local_sql"].parameters
    assert set(local_sql["properties"]) == {"session_id", "sql", "approved_run_id"}
    assert local_sql["required"] == ["session_id"]

    mirror_status = tools["mirror_status"].parameters
    assert set(mirror_status["properties"]) == {"session_id"}
    assert mirror_status["required"] == ["session_id"]


def test_direct_admin_reuses_saved_mcp_secret_and_executes_without_approval(
    monkeypatch,
):
    _capability, inventory = _linear_capability()
    inventory["company_admin"] = True
    inventory["mcp_credentials"] = [{
        "server_name": "linear",
        "secret_name": "LINEAR_API_KEY",
        "status": "active",
    }]
    action = calliope._dynamic_capability_actions(inventory)[0]
    run_id = str(uuid.uuid4())
    observed = {}

    monkeypatch.setattr(calliope, "_owner_for_calliope_session", lambda *_args: "admin@example.com")
    monkeypatch.setattr(calliope.calliope_dreams, "is_company_admin", lambda *_args: True)
    monkeypatch.setattr(calliope, "_action_by_id", lambda *_args: action)

    async def saved(*_args):
        return {"LINEAR_API_KEY"}, True

    async def execute(_factory, owner, received_run_id, secure_inputs):
        observed.update({
            "owner": owner,
            "run_id": received_run_id,
            "secure_inputs": secure_inputs,
        })
        return {"run": {"id": received_run_id, "status": "complete"}}

    monkeypatch.setattr(calliope, "_mcp_gateway_secret_names", saved)
    monkeypatch.setattr(calliope, "plan_action", lambda *_args: {"id": run_id})
    monkeypatch.setattr(calliope, "execute_action_with_secure_inputs", execute)

    result = asyncio.run(calliope.administer_action_for_session(
        object, str(uuid.uuid4()), action["id"], {"server_name": "linear"}
    ))

    assert result["execution_mode"] == "direct_admin"
    assert result["approval_required"] is False
    assert observed == {
        "owner": "admin@example.com",
        "run_id": run_id,
        "secure_inputs": {},
    }


def test_direct_admin_requests_only_missing_secure_value(monkeypatch):
    _capability, inventory = _linear_capability()
    inventory["company_admin"] = True
    action = calliope._dynamic_capability_actions(inventory)[0]
    monkeypatch.setattr(calliope, "_owner_for_calliope_session", lambda *_args: "admin@example.com")
    monkeypatch.setattr(calliope.calliope_dreams, "is_company_admin", lambda *_args: True)
    monkeypatch.setattr(calliope, "_action_by_id", lambda *_args: action)

    async def missing(*_args):
        return set(), True

    monkeypatch.setattr(calliope, "_mcp_gateway_secret_names", missing)
    result = asyncio.run(calliope.administer_action_for_session(
        object, str(uuid.uuid4()), action["id"], {"server_name": "linear"}
    ))

    assert result["secure_input_required"] is True
    assert result["missing_secret_names"] == ["LINEAR_API_KEY"]
    assert "plan" not in result


def test_direct_admin_derives_custom_mcp_secret_names_before_planning(monkeypatch):
    action = _custom_mcp_action()
    monkeypatch.setattr(calliope, "_owner_for_calliope_session", lambda *_args: "admin@example.com")
    monkeypatch.setattr(calliope.calliope_dreams, "is_company_admin", lambda *_args: True)
    monkeypatch.setattr(calliope, "_action_by_id", lambda *_args: action)
    monkeypatch.setattr(
        calliope,
        "plan_action",
        lambda *_args: pytest.fail("a missing secret must not create a run"),
    )

    async def missing(*_args):
        return set(), True

    monkeypatch.setattr(calliope, "_mcp_gateway_secret_names", missing)
    result = asyncio.run(calliope.administer_action_for_session(
        object,
        str(uuid.uuid4()),
        action["id"],
        {
            "server_name": "client_mcp",
            "transport": "http",
            "url": "https://mcp.client.example/mcp",
            "auth_token_name": "CLIENT_TOKEN",
        },
    ))

    assert result["secure_input_required"] is True
    assert result["missing_secret_names"] == ["CLIENT_TOKEN"]
    assert result["approval_required"] is False
    assert "submit once" in result["message"]


def test_native_secure_submit_plans_applies_and_verifies_in_one_request(monkeypatch):
    _capability, inventory = _linear_capability()
    inventory["company_admin"] = True
    action = calliope._dynamic_capability_actions(inventory)[0]
    run_id = str(uuid.uuid4())
    secret = "lin_api_native-envelope"
    observed = {}
    monkeypatch.setattr(calliope.calliope_dreams, "is_company_admin", lambda *_args: True)
    monkeypatch.setattr(calliope, "_action_by_id", lambda *_args: action)

    def plan(_factory, owner, action_id, inputs, session_id):
        observed["plan"] = {
            "owner": owner,
            "action_id": action_id,
            "inputs": inputs,
            "session_id": session_id,
        }
        return {"id": run_id}

    async def execute(_factory, owner, received_run_id, inputs):
        observed["execute"] = {
            "owner": owner,
            "run_id": received_run_id,
            "inputs": inputs,
        }
        return {"run": {"id": received_run_id, "status": "complete"}}

    monkeypatch.setattr(calliope, "plan_action", plan)
    monkeypatch.setattr(calliope, "execute_action_with_secure_inputs", execute)
    result = asyncio.run(calliope.administer_action_with_secure_inputs(
        object,
        "admin@example.com",
        action["id"],
        {"server_name": "linear", "secret:LINEAR_API_KEY": secret},
        None,
    ))

    assert observed["execute"]["inputs"]["secret:LINEAR_API_KEY"] == secret
    assert result["run"]["status"] == "complete"
    assert result["execution_mode"] == "direct_native"
    assert result["approval_required"] is False


@pytest.mark.parametrize(
    ("statement", "direct", "statement_type"),
    [
        ("SELECT rvbbit.capability_crawl()", True, "select"),
        ("WITH settings AS (SELECT 1 AS ok) SELECT * FROM settings", True, "select"),
        ("VALUES (1), (2)", True, "values"),
        ("CREATE TABLE public.calliope_test (id bigint)", False, "create"),
        ("INSERT INTO public.calliope_test VALUES (1)", False, "insert"),
        ("CALL rvbbit.some_admin_procedure()", False, "call"),
        ("WITH removed AS (DELETE FROM public.calliope_test RETURNING *) SELECT * FROM removed", False, "select"),
        ("SELECT * INTO public.calliope_copy FROM public.calliope_test", False, "select"),
    ],
)
def test_local_admin_sql_contract_distinguishes_select_functions_from_true_sql_mutations(
    statement, direct, statement_type,
):
    contract = calliope._local_admin_sql_contract(statement)

    assert contract["direct_select"] is direct
    assert contract["approval_required"] is (not direct)
    assert contract["statement_type"] == statement_type
    assert contract["sql_sha256"] == hashlib.sha256(
        statement.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT 1; SELECT 2",
        "SELECT dblink('postgresql://source.example/prod', 'SELECT 1')",
        "COPY public.orders TO PROGRAM 'id'",
        "ALTER SYSTEM SET shared_preload_libraries = 'x'",
        "CREATE ROLE extra_admin LOGIN PASSWORD 'plaintext'",
        "SELECT 'password=plaintext'",
    ],
)
def test_local_admin_sql_rejects_multi_statement_remote_host_and_secret_paths(statement):
    with pytest.raises(ValueError):
        calliope._local_admin_sql_contract(statement)


def test_local_admin_sql_executes_select_functions_directly_and_freezes_ddl_for_approval(
    monkeypatch,
):
    session_id = str(uuid.uuid4())
    select_run = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "action_id": calliope._LOCAL_ADMIN_SQL_ACTION_ID,
        "status": "planned",
    }
    ddl_run = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "action_id": calliope._LOCAL_ADMIN_SQL_ACTION_ID,
        "status": "planned",
        "input_values": {"sql": "CREATE TABLE public.dba_test (id bigint)"},
    }
    created = []
    executed = []
    monkeypatch.setattr(calliope, "_owner_for_calliope_session", lambda *_args: "admin@example.com")
    monkeypatch.setattr(calliope.calliope_dreams, "is_company_admin", lambda *_args: True)

    def create(_factory, _owner, received_session_id, contract):
        created.append(contract)
        return select_run if contract["direct_select"] else ddl_run

    def execute(_factory, owner, run, *, explicitly_approved):
        executed.append((owner, run["id"], explicitly_approved))
        return {
            "run": {**run, "status": "complete"},
            "approval_required": False,
        }

    monkeypatch.setattr(calliope, "_create_local_admin_sql_run", create)
    monkeypatch.setattr(calliope, "_execute_local_admin_sql_run", execute)

    direct = calliope.administer_local_sql_for_session(
        object,
        session_id,
        "SELECT rvbbit.capability_crawl()",
        authorized_owner="admin@example.com",
    )
    assert direct["run"]["status"] == "complete"
    assert executed == [("admin@example.com", select_run["id"], False)]

    planned = calliope.administer_local_sql_for_session(
        object,
        session_id,
        "CREATE TABLE public.dba_test (id bigint)",
        authorized_owner="admin@example.com",
    )
    assert planned["approval_required"] is True
    assert planned["run"]["id"] == ddl_run["id"]
    assert len(executed) == 1

    monkeypatch.setattr(calliope, "_action_run_for_owner", lambda *_args: ddl_run)
    approved = calliope.administer_local_sql_for_session(
        object,
        session_id,
        approved_run_id=ddl_run["id"],
        authorized_owner="admin@example.com",
    )
    assert approved["run"]["status"] == "complete"
    assert executed[-1] == ("admin@example.com", ddl_run["id"], True)


def test_local_admin_sql_rechecks_session_owner_and_frozen_sql(monkeypatch):
    session_id = str(uuid.uuid4())
    run = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "action_id": calliope._LOCAL_ADMIN_SQL_ACTION_ID,
        "status": "planned",
        "input_values": {"sql": "DROP TABLE public.old_table"},
    }
    monkeypatch.setattr(calliope, "_owner_for_calliope_session", lambda *_args: "admin@example.com")
    monkeypatch.setattr(calliope.calliope_dreams, "is_company_admin", lambda *_args: True)
    monkeypatch.setattr(calliope, "_action_run_for_owner", lambda *_args: run)

    with pytest.raises(PermissionError, match="another user"):
        calliope.administer_local_sql_for_session(
            object,
            session_id,
            "SELECT 1",
            authorized_owner="other@example.com",
        )
    with pytest.raises(ValueError, match="exact frozen SQL"):
        calliope.administer_local_sql_for_session(
            object,
            session_id,
            "DROP TABLE public.different_table",
            approved_run_id=run["id"],
            authorized_owner="admin@example.com",
        )


def test_local_admin_select_executor_uses_writable_connection_and_durable_receipt(monkeypatch):
    statement = "SELECT rvbbit.capability_crawl() AS crawled"
    contract = calliope._local_admin_sql_contract(statement)
    run = {
        "id": str(uuid.uuid4()),
        "status": "planned",
        "input_values": {
            "sql": statement,
            "sql_sha256": contract["sql_sha256"],
            "statement_type": "select",
            "direct_select": True,
        },
    }
    calls = []
    steps = []

    class Description:
        name = "crawled"
        type_code = 23

    class Result:
        def __init__(self, *, rows=None, one=None, description=None, status=""):
            self.rows = list(rows or [])
            self.one = one
            self.description = description
            self.statusmessage = status
            self.rowcount = len(self.rows) if self.rows else -1

        def fetchmany(self, limit):
            return self.rows[:limit]

        def fetchone(self):
            return self.one

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))
            if str(query).startswith("SELECT set_config"):
                return Result(one={"set_config": str(calliope._LOCAL_ADMIN_SQL_TIMEOUT_MS)})
            if query == statement:
                return Result(
                    rows=[{"crawled": 42}],
                    description=[Description()],
                    status="SELECT 1",
                )
            if str(query).startswith("SELECT current_database"):
                return Result(one={
                    "database": "rvbbit",
                    "database_user": "postgres",
                    "in_recovery": False,
                })
            raise AssertionError(query)

    running = []
    monkeypatch.setattr(
        calliope,
        "_mark_action_running",
        lambda *_args, **kwargs: running.append(kwargs),
    )
    monkeypatch.setattr(
        calliope,
        "_set_action_step",
        lambda _factory, _run_id, step_id, status, detail=None: steps.append(
            (step_id, status, detail)
        ),
    )
    monkeypatch.setattr(calliope, "_fail_active_action_step", lambda *_args: None)

    def finish(_factory, _run_id, status, **kwargs):
        run.update({"status": status, **kwargs})

    monkeypatch.setattr(calliope, "_finish_action_run", finish)
    monkeypatch.setattr(calliope, "_action_run_for_owner", lambda *_args: run)

    result = calliope._execute_local_admin_sql_run(
        Connection,
        "admin@example.com",
        run,
        explicitly_approved=False,
    )

    assert statement in [query for query, _params in calls]
    assert not any("default_transaction_read_only" in str(query) for query, _ in calls)
    assert result["execution_mode"] == "direct_local_select_sql"
    assert result["run"]["status"] == "complete"
    assert result["run"]["result"]["rows"] == [{"crawled": 42}]
    assert result["run"]["verification"]["committed"] is True
    assert running == [{"mark_approved": False}]
    assert [(step, status) for step, status, _detail in steps] == [
        ("inspect", "complete"),
        ("execute", "running"),
        ("execute", "complete"),
        ("verify", "running"),
        ("verify", "complete"),
    ]


def test_local_admin_sql_mcp_binds_the_verified_human_subject(monkeypatch):
    observed = {}
    monkeypatch.setattr(server, "_logged", lambda _tool, _args, thunk: thunk())
    monkeypatch.setattr(
        server,
        "_require_application_subject",
        lambda: SimpleNamespace(subject="admin@example.com"),
    )
    monkeypatch.setattr(calliope, "is_enabled", lambda: True)

    def administer(_factory, session_id, sql, approved_run_id, *, authorized_owner):
        observed.update({
            "session_id": session_id,
            "sql": sql,
            "approved_run_id": approved_run_id,
            "authorized_owner": authorized_owner,
        })
        return {"approval_required": False}

    monkeypatch.setattr(calliope, "administer_local_sql_for_session", administer)
    result = server._mcp_administer_local_sql(
        "225ddde1-1c86-4cb1-bf41-6b7b70b439d3",
        "SELECT rvbbit.capability_crawl()",
    )

    assert result == {"approval_required": False}
    assert observed == {
        "session_id": "225ddde1-1c86-4cb1-bf41-6b7b70b439d3",
        "sql": "SELECT rvbbit.capability_crawl()",
        "approved_run_id": None,
        "authorized_owner": "admin@example.com",
    }


def test_linear_brain_action_reuses_packaged_provider_without_guided_choices():
    action = _linear_brain_action()

    assert action["executor"] == "brain_query_source"
    assert action["executor"] in calliope._DIRECT_ADMIN_EXECUTORS
    assert action["risk"] == "reversible"
    assert action["fields"] == []
    assert action["config"] == {
        "provider": "linear-issues",
        "label": "Linear Issues",
        "reuse_provider": True,
        "source_config": {"tombstone_missing": False},
    }
    calliope._validate_administration_action_inputs(action, {})
    plan = calliope._action_plan_document(action, {})

    assert [step["id"] for step in plan["steps"]] == [
        "inspect", "define", "source", "sync", "verify",
    ]
    assert "packaged provider remains available" in plan["rollback"]
    assert "team_scope" not in json.dumps(action)
    assert "history_window" not in json.dumps(action)


def test_linear_brain_executor_binds_syncs_and_verifies_packaged_provider(
    monkeypatch,
):
    queries = []
    steps = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            queries.append((query, params))
            if "FROM rvbbit.brain_doc_providers" in query:
                return Result({
                    "provider": "linear-issues",
                    "label": "Linear Issues",
                    "doc_type": "ticket",
                })
            if "SELECT source_id FROM rvbbit.brain_sources" in query:
                assert params == ("linear-issues",)
                return Result()
            if "brain_add_query_source" in query:
                assert params == (
                    "Linear Issues",
                    "linear-issues",
                    json.dumps({"tombstone_missing": False}),
                )
                return Result({"source_id": 42})
            if "brain_sync_dispatch" in query:
                assert params == (42,)
                return Result({"result": {"ok": True, "fetched": 17}})
            if "FROM rvbbit.brain_sources s WHERE s.source_id" in query:
                assert params == (42,)
                return Result({
                    "source_id": 42,
                    "label": "Linear Issues",
                    "kind": "query",
                    "enabled": True,
                    "last_synced_at": "2026-08-12T05:00:00+00:00",
                    "provider": "linear-issues",
                    "documents": 17,
                })
            raise AssertionError(query)

    monkeypatch.setattr(
        calliope,
        "_set_action_step",
        lambda _factory, _run_id, step, status, detail=None: steps.append(
            (step, status, detail)
        ),
    )
    run = {
        "id": str(uuid.uuid4()),
        "action_snapshot": _linear_brain_action(),
        "input_values": {},
    }

    result, verification = calliope._execute_brain_query_source(Connection, run)

    assert result == {
        "provider": "linear-issues",
        "source_id": 42,
        "sync": {"ok": True, "fetched": 17},
    }
    assert verification["documents"] == 17
    assert verification["enabled"] is True
    assert not any("brain_define_provider" in query for query, _params in queries)
    assert [
        (step, status) for step, status, _detail in steps
    ] == [
        ("inspect", "running"),
        ("inspect", "complete"),
        ("define", "running"),
        ("define", "complete"),
        ("source", "running"),
        ("source", "complete"),
        ("sync", "running"),
        ("sync", "complete"),
        ("verify", "running"),
        ("verify", "complete"),
    ]


def test_linear_brain_executor_reuses_existing_source_with_typed_provider(
    monkeypatch,
):
    updated = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            if "FROM rvbbit.brain_doc_providers" in query:
                return Result({
                    "provider": "linear-issues",
                    "label": "Linear Issues",
                    "doc_type": "ticket",
                })
            if "SELECT source_id FROM rvbbit.brain_sources" in query:
                return Result({"source_id": 42})
            if query.startswith("UPDATE rvbbit.brain_sources SET enabled=true"):
                assert "jsonb_build_object('provider',%s::text)" in query
                assert params == (
                    json.dumps({"tombstone_missing": False}),
                    "linear-issues",
                    42,
                )
                updated.append(42)
                return Result()
            if "FROM rvbbit.brain_sources s WHERE s.source_id" in query:
                return Result({
                    "source_id": 42,
                    "label": "Linear Issues",
                    "kind": "query",
                    "enabled": True,
                    "provider": "linear-issues",
                    "documents": 204,
                })
            raise AssertionError(query)

    monkeypatch.setattr(calliope, "_set_action_step", lambda *_args, **_kwargs: None)
    result, verification = calliope._execute_brain_query_source(Connection, {
        "id": str(uuid.uuid4()),
        "action_snapshot": _linear_brain_action(),
        "input_values": {"sync_now": False},
    })

    assert updated == [42]
    assert result["source_id"] == 42
    assert result["sync"] == {}
    assert verification["documents"] == 204


def test_mirror_status_prefers_active_controller_auth_over_stale_registry_health(
    monkeypatch,
):
    monkeypatch.setattr(
        calliope,
        "_owner_for_calliope_session",
        lambda *_args: "admin@example.com",
    )
    monkeypatch.setattr(
        calliope,
        "_setup_mirror_snapshot",
        lambda *_args: {
            "available": True,
            "credential_store_ready": True,
            "worker": {
                "registered": True,
                "warehouse_authenticated": True,
                "worker_authenticated": None,
                "authenticated": True,
            },
            "connections": [],
        },
    )

    async def active_health(*_args, **_kwargs):
        return {
            "ok": True,
            "controller_auth_configured": True,
            "database_name": "rvbbit",
        }

    monkeypatch.setattr(calliope, "_setup_mirror_worker_request", active_health)

    result = asyncio.run(
        calliope.mirror_status_for_session(object, str(uuid.uuid4()))
    )

    assert result["controller"]["worker_authenticated"] is True
    assert result["controller"]["authenticated"] is True
    assert result["controller"]["available"] is True
    assert result["controller"]["active_health"]["checked_at"]


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
    monkeypatch.setattr(
        calliope.calliope_dreams, "is_company_admin", lambda *_args: True
    )
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
    monkeypatch.setattr(calliope.calliope_dreams, "is_company_admin", lambda *_args: True)

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
    monkeypatch.setattr(calliope.calliope_dreams, "is_company_admin", lambda *_args: True)

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


def _admin_inventory() -> dict:
    _capability, inventory = _linear_capability(connected=True)
    inventory.update({
        "company_admin": True,
        "mirror_jobs": [{
            "job_name": "erp_sales",
            "connection_name": "erp",
            "source_schema": "sales",
            "destination_schema": "erp_sales",
            "enabled": True,
            "schedule_seconds": 3600,
            "next_run_at": "2026-08-11T15:00:00+00:00",
            "updated_at": "2026-08-11T14:00:00+00:00",
            "table_count": 12,
            "latest_run_id": "57cb5daa-3a0b-4fa4-888d-b8bcb44b8c61",
            "latest_run_status": "failed",
            "latest_run_trigger": "schedule",
            "latest_run_requested_at": "2026-08-11T13:00:00+00:00",
            "latest_run_finished_at": "2026-08-11T13:02:00+00:00",
            "latest_tables_succeeded": 10,
            "latest_tables_failed": 2,
            "latest_rows_loaded": 1200,
            "latest_error_code": "SOURCE_TIMEOUT",
            "latest_error_message": "source timed out",
        }],
        "mcp_credentials": [{
            "server_name": "linear",
            "secret_name": "LINEAR_API_KEY",
            "version": 3,
            "status": "active",
            "updated_at": "2026-08-11T12:00:00+00:00",
            "rotated_at": "2026-08-11T12:00:00+00:00",
        }],
    })
    return inventory


def test_dynamic_admin_actions_freeze_mirror_and_credential_metadata_only():
    actions = calliope._dynamic_administration_actions(_admin_inventory())
    mirror = next(action for action in actions if action["executor"] == "mirror_control")
    credential = next(action for action in actions if action["executor"] == "mcp_credentials")

    assert mirror["id"] == "mirror.manage:erp_sales"
    assert mirror["config"]["expected_updated_at"] == "2026-08-11T14:00:00+00:00"
    assert mirror["config"]["latest_run"]["status"] == "failed"
    assert next(field for field in mirror["fields"] if field["key"] == "operation")["default"] == "retry_last_failed"
    assert credential["id"] == "mcp.credentials:linear"
    assert credential["executor"] in calliope._DIRECT_ADMIN_EXECUTORS
    assert credential["config"]["active_names"] == ["LINEAR_API_KEY"]
    assert credential["config"]["credential_revisions"]["LINEAR_API_KEY"]["version"] == 3
    assert "ciphertext" not in json.dumps(credential)
    assert "value" not in credential["config"]["credential_revisions"]["LINEAR_API_KEY"]

    non_admin = dict(_admin_inventory(), company_admin=False)
    assert calliope._dynamic_administration_actions(non_admin) == []


def test_agent_rotates_via_secure_handoff_but_revokes_credentials_directly(monkeypatch):
    action = next(
        item for item in calliope._dynamic_administration_actions(_admin_inventory())
        if item["executor"] == "mcp_credentials"
    )
    run_id = str(uuid.uuid4())
    executions = []
    monkeypatch.setattr(calliope, "_owner_for_calliope_session", lambda *_args: "admin@example.com")
    monkeypatch.setattr(calliope.calliope_dreams, "is_company_admin", lambda *_args: True)
    monkeypatch.setattr(calliope, "_action_by_id", lambda *_args: action)

    def plan(*_args):
        return {"id": run_id}

    async def execute(_factory, _owner, received_run_id, secure_inputs):
        executions.append((received_run_id, secure_inputs))
        return {"run": {"id": received_run_id, "status": "complete"}}

    monkeypatch.setattr(calliope, "plan_action", plan)
    monkeypatch.setattr(calliope, "execute_action_with_secure_inputs", execute)

    rotate = asyncio.run(calliope.administer_action_for_session(
        object,
        str(uuid.uuid4()),
        action["id"],
        {"operation": "rotate", "credential_name": "LINEAR_API_KEY"},
    ))
    assert rotate["secure_input_required"] is True
    assert rotate["approval_required"] is False
    assert rotate["missing_secret_names"] == ["LINEAR_API_KEY"]
    assert executions == []

    revoke = asyncio.run(calliope.administer_action_for_session(
        object,
        str(uuid.uuid4()),
        action["id"],
        {"operation": "revoke", "credential_name": "LINEAR_API_KEY"},
    ))
    assert revoke["execution_mode"] == "direct_admin"
    assert revoke["approval_required"] is False
    assert executions == [(run_id, {})]


def test_credential_plan_is_redacted_and_revoke_requires_an_active_name():
    action = next(
        item for item in calliope._dynamic_administration_actions(_admin_inventory())
        if item["executor"] == "mcp_credentials"
    )
    secret_value = "must-never-enter-the-plan"
    values, redacted, secrets = calliope._normalize_action_inputs(action, {
        "operation": "rotate",
        "credential_name": "LINEAR_API_KEY",
        "secret:CREDENTIAL_VALUE": secret_value,
    })
    calliope._validate_administration_action_inputs(action, values)
    plan = calliope._action_plan_document(action, values)

    assert values == {"operation": "rotate", "credential_name": "LINEAR_API_KEY"}
    assert redacted["secret:CREDENTIAL_VALUE"] == "••••••"
    assert secrets == {"CREDENTIAL_VALUE": secret_value}
    assert secret_value not in json.dumps(plan)
    assert [step["id"] for step in plan["steps"]] == ["inspect", "apply", "verify"]

    with pytest.raises(ValueError, match="No active UNKNOWN"):
        calliope._validate_administration_action_inputs(
            action, {"operation": "revoke", "credential_name": "UNKNOWN"}
        )


def test_admin_action_plan_rechecks_admin_membership(monkeypatch):
    action = next(
        item for item in calliope._dynamic_administration_actions(_admin_inventory())
        if item["executor"] == "mirror_control"
    )
    monkeypatch.setattr(calliope, "_action_by_id", lambda *_args: action)
    monkeypatch.setattr(
        calliope.calliope_dreams, "is_company_admin", lambda *_args: False
    )

    with pytest.raises(PermissionError, match="Admins Team"):
        calliope.plan_action(
            object, "former-admin@example.com", action["id"],
            {"operation": "retry_last_failed"},
        )


def test_mirror_pause_executes_against_frozen_revision_and_verifies(monkeypatch):
    state = {
        "job": {
            "job_name": "erp_sales", "connection_name": "erp",
            "source_schema": "sales", "destination_schema": "erp_sales",
            "enabled": True, "schedule_seconds": 3600,
            "next_run_at": "2026-08-11T15:00:00+00:00", "last_run_at": None,
            "updated_at": "2026-08-11T14:00:00+00:00", "updated_by": "system",
            "table_count": 12,
        },
        "latest": {
            "run_id": "57cb5daa-3a0b-4fa4-888d-b8bcb44b8c61",
            "trigger": "schedule", "status": "succeeded", "attempt": 1,
            "requested_at": "2026-08-11T13:00:00+00:00",
            "requested_by": "scheduler", "started_at": "2026-08-11T13:00:01+00:00",
            "finished_at": "2026-08-11T13:02:00+00:00",
            "tables_succeeded": 12, "tables_failed": 0, "rows_loaded": 1200,
            "error_code": None, "error_message": None,
        },
    }

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def transaction(self):
            return Transaction()

        def execute(self, query, params):
            if query.startswith("SELECT job_name"):
                return Result(dict(state["job"]))
            if query.startswith("SELECT run_id"):
                return Result(dict(state["latest"]))
            if query.startswith("UPDATE rvbbit.mirror_jobs SET enabled=false"):
                state["job"].update({
                    "enabled": False,
                    "next_run_at": None,
                    "updated_at": "2026-08-11T14:05:00+00:00",
                    "updated_by": params[0],
                })
                return Result()
            raise AssertionError(query)

    action = {
        "executor": "mirror_control",
        "config": {
            "job_name": "erp_sales",
            "expected_updated_at": "2026-08-11T14:00:00+00:00",
            "latest_run": {"run_id": state["latest"]["run_id"], "status": "succeeded"},
        },
    }
    run = {
        "id": str(uuid.uuid4()), "owner_email": "admin@example.com",
        "action_snapshot": action, "input_values": {"operation": "pause"},
    }
    steps = []
    monkeypatch.setattr(
        calliope, "_set_action_step",
        lambda _factory, _run_id, step, status, detail=None: steps.append((step, status, detail)),
    )

    result, verification = calliope._execute_mirror_control(Connection, run)

    assert result == {"job_name": "erp_sales", "operation": "pause", "queued_run_id": None}
    assert verification["enabled"] is False
    assert verification["updated_by"] == "admin@example.com"
    assert [(step, status) for step, status, _detail in steps] == [
        ("inspect", "running"), ("inspect", "complete"),
        ("apply", "running"), ("apply", "complete"),
        ("verify", "running"), ("verify", "complete"),
    ]


def test_mirror_plan_that_observed_no_run_rejects_a_newer_run():
    action = {
        "config": {
            "expected_updated_at": "2026-08-11T14:00:00+00:00",
            "latest_run": {"run_id": None, "status": None},
        }
    }
    current = {
        "updated_at": "2026-08-11T14:00:00+00:00",
        "latest_run": {"run_id": str(uuid.uuid4()), "status": "queued"},
    }

    with pytest.raises(ValueError, match="latest mirror run changed"):
        calliope._assert_mirror_plan_current(action, current)


def test_mcp_credential_rotation_uses_canonical_cas_and_returns_no_value(monkeypatch):
    action = next(
        item for item in calliope._dynamic_administration_actions(_admin_inventory())
        if item["executor"] == "mcp_credentials"
    )
    current = {
        "credential_ref": "mcp/linear/LINEAR_API_KEY", "server_name": "linear",
        "secret_name": "LINEAR_API_KEY", "version": 3, "status": "active",
        "updated_at": "2026-08-11T12:00:00+00:00", "updated_by": "system",
        "rotated_at": "2026-08-11T12:00:00+00:00",
    }
    secret_value = "transient-rotation-value"
    pushed = {}
    steps = []

    async def health(_factory):
        return {"status": "ok", "credential_store": "canonical"}

    async def push(_factory, server, name, value, **expectation):
        pushed.update({"server": server, "name": name, "value": value, **expectation})
        current.update({
            "version": 4,
            "updated_at": "2026-08-11T12:05:00+00:00",
            "updated_by": "gateway",
            "rotated_at": "2026-08-11T12:05:00+00:00",
        })

    async def names(_factory, server):
        assert server == "linear"
        return {"LINEAR_API_KEY"}, True

    class Result:
        def fetchone(self):
            return {"probe": {"reachable": True, "latency_ms": 9}}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            assert "mcp_probe" in query and params == ("linear",)
            return Result()

    monkeypatch.setattr(calliope, "_mcp_gateway_health", health)
    monkeypatch.setattr(calliope, "_push_mcp_gateway_secret", push)
    monkeypatch.setattr(calliope, "_mcp_gateway_secret_names", names)
    monkeypatch.setattr(calliope, "_mcp_credential_metadata", lambda *_args: dict(current))
    monkeypatch.setattr(
        calliope, "_set_action_step",
        lambda _factory, _run_id, step, status, detail=None: steps.append((step, status, detail)),
    )
    run = {"id": str(uuid.uuid4()), "action_snapshot": action}

    result, verification = asyncio.run(calliope._execute_mcp_credentials(
        Connection,
        run,
        {"operation": "rotate", "credential_name": "LINEAR_API_KEY"},
        {"CREDENTIAL_VALUE": secret_value},
    ))

    assert pushed == {
        "server": "linear", "name": "LINEAR_API_KEY", "value": secret_value,
        "expected_version": 3, "expected_status": "active", "expect_absent": False,
    }
    assert verification["version"] == 4
    assert verification["reachable"] is True
    assert secret_value not in json.dumps({"result": result, "verification": verification, "steps": steps})


def test_mcp_credential_revocation_verifies_ciphertext_free_tombstone(monkeypatch):
    action = next(
        item for item in calliope._dynamic_administration_actions(_admin_inventory())
        if item["executor"] == "mcp_credentials"
    )
    current = {
        "credential_ref": "mcp/linear/LINEAR_API_KEY", "server_name": "linear",
        "secret_name": "LINEAR_API_KEY", "version": 3, "status": "active",
        "updated_at": "2026-08-11T12:00:00+00:00", "updated_by": "system",
        "rotated_at": "2026-08-11T12:00:00+00:00",
    }
    deleted = {}

    async def health(_factory):
        return {"status": "ok", "credential_store": "canonical"}

    async def delete(_factory, server, name, **expectation):
        deleted.update({"server": server, "name": name, **expectation})
        current.update({
            "version": 4, "status": "revoked",
            "updated_at": "2026-08-11T12:05:00+00:00", "updated_by": "gateway",
        })

    async def names(_factory, _server):
        return set(), True

    monkeypatch.setattr(calliope, "_mcp_gateway_health", health)
    monkeypatch.setattr(calliope, "_delete_mcp_gateway_secret", delete)
    monkeypatch.setattr(calliope, "_mcp_gateway_secret_names", names)
    monkeypatch.setattr(calliope, "_mcp_credential_metadata", lambda *_args: dict(current))
    monkeypatch.setattr(calliope, "_set_action_step", lambda *_args, **_kwargs: None)

    result, verification = asyncio.run(calliope._execute_mcp_credentials(
        object,
        {"id": str(uuid.uuid4()), "action_snapshot": action},
        {"operation": "revoke", "credential_name": "LINEAR_API_KEY"},
        {},
    ))

    assert deleted == {
        "server": "linear", "name": "LINEAR_API_KEY",
        "expected_version": 3, "expected_status": "active",
    }
    assert result["credential_ref"] == "mcp/linear/LINEAR_API_KEY"
    assert verification["status"] == "revoked"
    assert verification["version"] == 4
    assert verification["saved_names"] == []


def test_secure_credential_execution_rethrows_only_redacted_error(monkeypatch):
    action = next(
        item for item in calliope._dynamic_administration_actions(_admin_inventory())
        if item["executor"] == "mcp_credentials"
    )
    run_id = str(uuid.uuid4())
    run = {
        "id": run_id, "status": "planned", "action_snapshot": action,
        "input_values": {"operation": "rotate", "credential_name": "LINEAR_API_KEY"},
    }
    secret_value = "opaque-value-echoed-by-upstream"
    failures = []

    monkeypatch.setattr(calliope, "_action_run_for_owner", lambda *_args: run)
    monkeypatch.setattr(calliope, "_require_action_access", lambda *_args: None)
    monkeypatch.setattr(calliope, "_mark_action_running", lambda *_args: None)
    monkeypatch.setattr(calliope, "_fail_active_action_step", lambda *_args: None)
    monkeypatch.setattr(
        calliope,
        "_finish_action_run",
        lambda _factory, _run_id, status, **kwargs: failures.append((status, kwargs)),
    )

    async def fail(*_args):
        raise RuntimeError(f"upstream echoed {secret_value}")

    monkeypatch.setattr(calliope, "_execute_mcp_credentials", fail)

    with pytest.raises(RuntimeError) as error:
        asyncio.run(calliope.execute_action_with_secure_inputs(
            object,
            "admin@example.com",
            run_id,
            {"secret:CREDENTIAL_VALUE": secret_value},
        ))

    assert secret_value not in str(error.value)
    assert failures[0][0] == "failed"
    assert secret_value not in failures[0][1]["error"]
