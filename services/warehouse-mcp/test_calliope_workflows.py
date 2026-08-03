"""Contracts for Calliope's bounded, agent-driven Workflow layer."""
from __future__ import annotations

import inspect
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import calliope  # noqa: E402
import server  # noqa: E402


def test_workflow_schema_is_versioned_auditable_and_self_healing():
    migration = (
        ROOT
        / "crates"
        / "pg_rvbbit"
        / "sql"
        / "migrations"
        / "0233_calliope_workflows.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )
    diagnostics = (
        ROOT
        / "crates"
        / "pg_rvbbit"
        / "sql"
        / "migrations"
        / "0234_calliope_workflow_diagnostics.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_workflows" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_workflow_versions" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_workflow_runs" in migration
    assert "UNIQUE (workflow_id,version)" in migration
    assert "REFERENCES rvbbit.calliope_workflow_versions(workflow_id,version)" in migration
    assert "WHERE trigger_kind='scheduled'" in migration
    assert "0233_calliope_workflows" in registry
    assert "0234_calliope_workflow_diagnostics" in registry
    assert "ADD COLUMN IF NOT EXISTS steps jsonb" in diagnostics
    assert "excludes hidden model reasoning" in diagnostics
    assert "ADD COLUMN IF NOT EXISTS steps jsonb" in calliope._WORKFLOW_DDL
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_workflows" in calliope._WORKFLOW_DDL
    assert "conn.execute(_WORKFLOW_DDL)" in inspect.getsource(calliope.ensure_tables)


def test_startup_reconciles_only_orphaned_manual_runs(monkeypatch):
    run_id = str(uuid.uuid4())
    queries = []
    finished = []

    class Result:
        def __init__(self, rows=None):
            self.rows = rows or []

        def fetchall(self):
            return self.rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None):
            queries.append(query)
            if "SELECT id FROM rvbbit.calliope_workflow_runs" in query:
                return Result([{"id": run_id}])
            return Result()

    monkeypatch.setattr(calliope, "_backfill_artifact_attribution", lambda _conn: None)
    monkeypatch.setattr(
        calliope,
        "finish_workflow_run",
        lambda _factory, received_run_id, status, summary, details, **kwargs: finished.append({
            "run_id": received_run_id,
            "status": status,
            "summary": summary,
            "details": details,
            **kwargs,
        }),
    )

    calliope.ensure_tables(Connection)

    assert len(finished) == 1
    assert finished[0]["run_id"] == run_id
    assert finished[0]["status"] == "failed"
    assert finished[0]["details"] == {"reason": "warehouse_service_restarted"}
    orphan_query = next(
        query for query in queries
        if "SELECT id FROM rvbbit.calliope_workflow_runs" in query
    )
    assert "trigger_kind='manual'" in orphan_query
    assert "trigger_kind='scheduled'" not in orphan_query
    interrupted_item_query = next(
        query for query in queries if "'Calliope work was interrupted'" in query
    )
    assert "wr.trigger_kind='manual'" in interrupted_item_query


def test_workflow_graph_is_small_readable_and_governed():
    goal, graph = calliope._normalize_workflow_graph(
        "Review current pipeline health and explain actionable anomalies.",
        {
            "kind": "schedule",
            "schedule": "30 8 * * 1-5",
            "timezone": "Hermes installation",
            "shell": "do-not-keep",
        },
        [
            {
                "kind": "artifact",
                "ref": "sales-pipeline",
                "version": 7,
                "label": "Approved pipeline view",
                "sql": "do-not-keep",
            },
            {
                "kind": "instruction",
                "description": "Escalate only material changes.",
            },
        ],
        ["stage", "work_inbox", "stage"],
        ["Compare against the previous governed period."] * 20,
    )

    assert goal.startswith("Review current")
    assert graph["schema"] == "calliope.workflow/v1"
    assert graph["trigger"] == {
        "id": "trigger",
        "kind": "schedule",
        "label": "On schedule",
        "schedule": "30 8 * * 1-5",
        "timezone": "Hermes installation",
    }
    assert graph["contexts"][0]["version"] == 7
    assert "sql" not in graph["contexts"][0]
    assert graph["agent"]["tool_policy"] == "governed_dynamic"
    assert len(graph["agent"]["decision_rules"]) == 12
    assert [item["kind"] for item in graph["outputs"]] == ["stage", "work_inbox"]
    assert sum(node["kind"] == "agent" for node in graph["nodes"]) == 1
    assert {edge["kind"] for edge in graph["edges"]} == {
        "starts", "context", "produces",
    }


@pytest.mark.parametrize(
    ("trigger", "contexts", "outputs", "message"),
    [
        ({"kind": "webhook"}, [], ["stage"], "manual or schedule"),
        (
            {"kind": "schedule", "schedule": "every 1d", "timezone": "America/New_York"},
            [],
            ["stage"],
            "per-Workflow timezones are not supported",
        ),
        (
            {"kind": "schedule", "schedule": "every weekday at nine"},
            [],
            ["stage"],
            "must use Hermes syntax",
        ),
        (
            {"kind": "manual"},
            [{"kind": "tool", "ref": "execute_sql"}],
            ["stage"],
            "context.kind must be",
        ),
        ({"kind": "manual"}, [], ["shell"], "output.kind must be"),
    ],
)
def test_workflow_graph_rejects_low_level_or_unsupported_nodes(
    trigger, contexts, outputs, message
):
    with pytest.raises(ValueError, match=message):
        calliope._normalize_workflow_graph(
            "Produce a useful result.", trigger, contexts, outputs
        )

    with pytest.raises(ValueError, match="at most"):
        calliope._normalize_workflow_graph(
            "Bound the context.",
            "manual",
            [
                {"kind": "knowledge", "description": f"Context {index}"}
                for index in range(calliope._MAX_WORKFLOW_CONTEXTS + 1)
            ],
            ["stage"],
        )


def test_semantic_object_context_resolves_an_exact_versioned_manifest_handle():
    class Result:
        def fetchone(self):
            return {
                "name": "Pipeline dashboard",
                "manifest": {
                    "semantic_map": {
                        "objects": [{
                            "id": "open_pipeline",
                            "kind": "metric",
                            "meaning": {"label": "Open pipeline", "unit": "USD"},
                            "definition_hash": "sha256:approved",
                            "evaluator": {
                                "shape": "scalar",
                                "sql": "SELECT sum(amount) AS value FROM sales.pipeline",
                            },
                        }]
                    }
                },
            }

    class Connection:
        def execute(self, query, params):
            assert "JOIN rvbbit.dashboard_versions" in query
            assert params == ("pipeline-dashboard", 4)
            return Result()

    context = {
        "id": "context-1",
        "kind": "semantic_object",
        "label": "Open pipeline",
        "ref": "artifact-object:pipeline-dashboard:v4:open_pipeline",
        "version": 4,
        "payload": {"definition_hash": "sha256:approved"},
    }
    resolved = calliope._resolve_workflow_contexts(
        Connection(), "owner@example.com", {"contexts": [context]}
    )[0]["resolved"]

    assert resolved["found"] is True
    assert resolved["artifact"] == {
        "slug": "pipeline-dashboard", "version": 4, "title": "Pipeline dashboard",
    }
    assert resolved["semantic_object"]["definition_hash"] == "sha256:approved"
    assert "SELECT sum(amount)" in resolved["semantic_object"]["evaluator"]["sql"]

    changed = calliope._resolve_workflow_semantic_object(
        Connection(),
        {**context, "payload": {"definition_hash": "sha256:stale"}},
    )
    assert changed["found"] is False
    assert changed["reason"] == "Semantic object definition changed"


def test_workflow_publication_and_schedule_state_remain_human_visible():
    now = datetime.now(timezone.utc)
    workflow_id = str(uuid.uuid4())
    base = {
        "id": workflow_id,
        "owner_email": "owner@example.com",
        "slug": "morning-review",
        "visibility": "company",
        "latest_version": 2,
        "published_version": 1,
        "version": 2,
        "name": "Morning review",
        "description": "Review important changes.",
        "goal": "Explain material changes.",
        "graph": {"schema": "calliope.workflow/v1"},
        "schedule_enabled": True,
        "scheduled_version": 1,
        "hermes_job_id": "job-123",
        "schedule_state": "error",
        "schedule_last_status": "error",
        "schedule_error": "Provider authentication failed",
        "created_at": now,
        "updated_at": now,
    }
    owner = calliope._workflow_row_json(base, "owner@example.com")
    reader = calliope._workflow_row_json(
        {**base, "version": 1, "name": "Approved morning review"},
        "reader@example.com",
    )

    assert owner["status"] == "update_ready" and owner["version"] == 2
    assert owner["schedule"] == {
        "enabled": True,
        "version": 1,
        "job_id": "job-123",
        "state": "error",
        "next_run_at": None,
        "last_run_at": None,
        "last_status": "error",
        "error": "Provider authentication failed",
    }
    assert reader["can_edit"] is False and reader["version"] == 1


def test_scheduler_prompt_enforces_the_run_lifecycle_and_immutable_pointer():
    workflow_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    prompt = calliope._workflow_scheduler_prompt(workflow_id, session_id, 4)

    assert f"Workflow id: {workflow_id}" in prompt
    assert f"Originating Calliope session: {session_id}" in prompt
    assert "Approved version: 4" in prompt
    assert "First call begin_calliope_workflow_run" in prompt
    assert "finish_calliope_workflow_run exactly once" in prompt
    assert "immutable graph, resolved contexts, agent goal, and decision rules" in prompt
    assert "do not create a duplicate generic work item" in prompt


def test_schedule_stays_on_its_pinned_version_until_explicitly_rewired(monkeypatch):
    workflow_id = str(uuid.uuid4())
    source_session_id = str(uuid.uuid4())
    row = {
        "id": workflow_id,
        "owner_email": "owner@example.com",
        "slug": "morning-review",
        "visibility": "private",
        "published_version": 2,
        "scheduled_version": 1,
        "schedule_enabled": True,
        "hermes_job_id": "job-v1",
        "version_id": str(uuid.uuid4()),
        "version_source_session_id": source_session_id,
        "name": "Morning review v1",
        "description": "Approved scheduled revision",
        "goal": "Review material changes.",
        "graph": {"schema": "calliope.workflow/v1", "contexts": []},
    }

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

        def execute(self, query, _params=None):
            if "FROM rvbbit.calliope_workflows w" in query:
                return Result(dict(row))
            if "FROM rvbbit.calliope_sessions" in query:
                return Result({"id": source_session_id, "owner_email": "owner@example.com"})
            raise AssertionError(query)

    monkeypatch.setattr(
        calliope.CalliopeConfig,
        "from_env",
        classmethod(lambda _cls: SimpleNamespace(enabled=False)),
    )
    # Advancing the publication pointer to v2 must not silently advance or
    # invalidate the separately approved v1 schedule.
    with pytest.raises(RuntimeError, match="not configured"):
        calliope.begin_workflow_run(
            Connection, workflow_id, source_session_id, 1, trigger_kind="scheduled"
        )

    row["schedule_enabled"] = False
    with pytest.raises(ValueError, match="schedule is not enabled"):
        calliope.begin_workflow_run(
            Connection, workflow_id, source_session_id, 1, trigger_kind="scheduled"
        )


def test_manual_workflow_launch_has_no_running_seed_and_auto_submits(monkeypatch):
    workflow_id = str(uuid.uuid4())
    source_session_id = str(uuid.uuid4())
    run_session_id = str(uuid.uuid4())
    inserted_turns = []
    row = {
        "id": workflow_id,
        "owner_email": "owner@example.com",
        "slug": "morning-review",
        "visibility": "private",
        "published_version": 1,
        "scheduled_version": None,
        "schedule_enabled": False,
        "hermes_job_id": None,
        "version_id": str(uuid.uuid4()),
        "version_source_session_id": source_session_id,
        "name": "Morning review",
        "description": "Review important changes.",
        "goal": "Explain material changes.",
        "graph": {"schema": "calliope.workflow/v1", "contexts": []},
    }

    class Result:
        def __init__(self, value=None):
            self.value = value

        def fetchone(self):
            return self.value

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

        def execute(self, query, params=None):
            if "FROM rvbbit.calliope_workflows w" in query:
                return Result(dict(row))
            if "FROM rvbbit.calliope_sessions" in query:
                return Result({"id": source_session_id, "owner_email": "owner@example.com"})
            if "INSERT INTO rvbbit.calliope_turns" in query:
                inserted_turns.append((query, params))
                return Result()
            if "INSERT INTO rvbbit.calliope_workflow_runs" in query:
                return Result()
            if "INSERT INTO rvbbit.calliope_surfaces" in query:
                return Result({
                    "id": params[0],
                    "session_id": params[1],
                    "turn_id": params[2],
                    "kind": "workflow",
                    "title": params[3],
                    "payload": {},
                    "source": {},
                    "presentation": {"view": "workflow_graph"},
                })
            raise AssertionError(query)

    monkeypatch.setattr(
        calliope.CalliopeConfig,
        "from_env",
        classmethod(lambda _cls: SimpleNamespace(enabled=True)),
    )
    monkeypatch.setattr(
        calliope,
        "_create_session_record_sync",
        lambda *_args, **_kwargs: {
            "id": run_session_id,
            "hermes_session_id": "workflow-manual-hermes-session",
            "title": "Run · Morning review",
            "title_source": "system",
        },
    )

    result = calliope.begin_workflow_run(
        Connection,
        workflow_id,
        source_session_id,
        1,
        trigger_kind="manual",
        expected_owner="owner@example.com",
    )

    assert len(inserted_turns) == 1
    seed_query, seed_params = inserted_turns[0]
    assert "assistant_message,status,completed_at" in seed_query
    assert seed_params[3].startswith("The immutable Workflow graph is pinned")
    assert seed_params[4:] == ("complete", True)
    assert "autorun=workflow" in result["url"]
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    assert 'const autoRunWorkflow = launchAutorun === "workflow"' in script
    assert "if (autoRunWorkflow) sendTurn();" in script
    assert "await loadSessions(state.current.id, true);" in script
    assert "preserveActivity: refreshCurrent" in script


def test_manual_workflow_stream_fails_closed_when_agent_never_finishes(monkeypatch):
    session_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    finished = {}

    class Result:
        def fetchone(self):
            return {"id": run_id}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            assert "trigger_kind='manual'" in query
            assert "status='running'" in query
            assert params == (session_id,)
            return Result()

    def finish(_factory, received_run_id, status, summary, details, **kwargs):
        finished.update({
            "run_id": received_run_id,
            "status": status,
            "summary": summary,
            "details": details,
            **kwargs,
        })
        return {"run_id": received_run_id, "status": status, "surfaces": []}

    monkeypatch.setattr(calliope, "finish_workflow_run", finish)
    result = calliope._finalize_unfinished_manual_workflow_run(
        Connection,
        session_id,
        "⚠️ Provider authentication failed: No usable credentials found.",
        turn_status="complete",
    )

    assert result == {"run_id": run_id, "status": "failed", "surfaces": []}
    assert finished["run_id"] == run_id
    assert finished["status"] == "failed"
    assert "Provider authentication failed" in finished["summary"]
    assert finished["details"]["reason"] == "provider_authentication_failed"
    assert "Configure a usable Hermes model" in finished["action_prompt"]


def test_workflow_tools_routes_and_graph_ui_ship_as_one_contract():
    workflow_id = str(uuid.uuid4())
    surfaces = calliope._project_tool_result(
        "draft_calliope_workflow",
        {
            "workflow": {
                "id": workflow_id,
                "name": "Morning review",
                "version": 1,
                "graph": {"schema": "calliope.workflow/v1"},
            }
        },
        {"slug": "morning-review"},
        "tool-call-1",
    )
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    server_source = (HERE / "server.py").read_text(encoding="utf-8")
    page = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")

    assert surfaces[0]["kind"] == "workflow"
    assert surfaces[0]["lineage_key"] == f"workflow:{workflow_id}"
    assert surfaces[0]["presentation"] == {"view": "workflow_graph"}
    assert calliope._project_tool_result(
        "begin_calliope_workflow_run", {"run_id": str(uuid.uuid4())}, {}, "call"
    ) == []
    for function in (
        server._mcp_draft_calliope_workflow,
        server._mcp_begin_calliope_workflow_run,
        server._mcp_get_calliope_personal_context,
        server._mcp_finish_calliope_workflow_run,
    ):
        assert "owner" not in inspect.signature(function).parameters
        assert "email" not in inspect.signature(function).parameters
    assert 'mcp.tool(name="draft_calliope_workflow")' in server_source
    assert 'mcp.tool(name="begin_calliope_workflow_run")' in server_source
    assert 'mcp.tool(name="get_calliope_personal_context")' in server_source
    assert 'mcp.tool(name="finish_calliope_workflow_run")' in server_source
    assert '@mcp.custom_route("/api/calliope/workflows", methods=["GET"])' in backend
    assert '"/api/calliope/workflows/{workflow_id}/preflight"' in backend
    assert '"/api/calliope/workflows/{workflow_id}/schedule"' in backend
    assert '"/api/jobs?include_disabled=true"' in backend
    assert "call draft_calliope_workflow with this session_id" in backend
    assert 'id="workflow-library-dialog"' in page
    assert 'id="workflow-graph"' in page
    assert 'id="workflow-lifecycle"' in page
    assert 'id="workflow-preflight"' in page
    assert 'class="workflow-preflight-head"' in page
    assert "function workflowGraphMarkup" in script
    assert "function calliopeTooltipSourceMarkup" in script
    assert "function setupWorkflowNodeTooltips" in script
    assert 'data-workflow-node aria-label=' in script
    assert "What it does" in script
    assert "What it did" in script
    assert "resolvedContexts: payload.resolved_contexts" in script
    assert "function renderWorkflowLifecycle" in script
    assert "function loadWorkflowPreflight" in script
    assert "function workflowPhaseMarkup" in script
    assert "data-workflow-revise-run" in script
    assert "Revision source run" in script
    assert "body: JSON.stringify(runId ? { run_id: runId } : {})" in script
    assert "acknowledge_warnings: Boolean(state.workflowPreflight.requires_warning_ack)" in script
    graph_markup = script.split("function workflowGraphMarkup", 1)[1].split(
        "function renderWorkflowList", 1
    )[0]
    assert graph_markup.count('class="workflow-graph-arrow"') == 3
    assert 'class="workflow-graph-column outputs"' in graph_markup
    assert 'return future ? `in ${amount}` : `${amount} ago`' in script
    assert ".workflow-node.agent" in css
    assert ".workflow-node.output" in css
    assert "--workflow-node-border:var(--instrument-border,var(--block-border,var(--line)))" in css
    assert ".workflow-node-tooltip" in css
    assert ".workflow-node-tooltip-section.did" in css
    assert ".workflow-lifecycle-item.ready" in css
    assert '.workflow-preflight[data-status="blocked"]' in css
    assert ".workflow-phase-timeline" in css
    assert ".workflow-run-actions" in css
    assert ".workflow-revision-run" in css
    assert '"workflow_revision_from_run"' in backend
    assert 'key != "runs"' in backend


def test_run_sessions_have_stable_metadata_and_a_session_tab():
    session_id = uuid.uuid4()
    workflow_id = uuid.uuid4()
    run_id = uuid.uuid4()
    instrument_id = uuid.uuid4()
    instrument_surface_id = uuid.uuid4()
    summary = calliope._session_json({
        "id": session_id,
        "title": "Run · Morning review",
        "workflow_run_id": run_id,
        "workflow_id": workflow_id,
        "workflow_version": 2,
        "workflow_name": "Morning review",
        "workflow_run_status": "complete",
    })
    instrument_summary = calliope._session_json({
        "id": uuid.uuid4(),
        "title": "Run · Field brief",
        "instrument_run_surface_id": instrument_surface_id,
        "instrument_id": str(instrument_id),
        "instrument_version": "1",
        "instrument_name": "Field brief",
    })
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")

    assert summary["id"] == str(session_id)
    assert summary["workflow_run_id"] == str(run_id)
    assert summary["workflow_id"] == str(workflow_id)
    assert summary["workflow_version"] == 2
    assert summary["workflow_name"] == "Morning review"
    assert summary["workflow_run_status"] == "complete"
    assert instrument_summary["instrument_run_surface_id"] == str(instrument_surface_id)
    assert instrument_summary["instrument_id"] == str(instrument_id)
    assert instrument_summary["instrument_name"] == "Field brief"
    assert "wr.workflow_run_id,wr.workflow_id,wr.workflow_version" in backend
    assert "v.name AS workflow_name,r.status AS workflow_run_status" in backend
    assert "r.session_id=s.id AND lower(r.owner_email)=lower(s.owner_email)" in backend
    assert "sf.source->>'origin'='calliope_instrument_run'" in backend
    assert "AS instrument_run_surface_id" in backend
    assert "AS instrument_id" in backend
    assert 'SESSION_TAB_KEY = "rvbbit-calliope-session-tab-v1"' in script
    assert "function isWorkflowRunSession" in script
    assert "function isInstrumentRunSession" in script
    assert "function isRunSession" in script
    assert 'if (isRunSession(session)) return "runs"' in script
    assert "function sessionTabsMarkup" in script
    assert "data-session-tab" in script
    assert ".session-tabs" in css
    assert ".run-session-card" in css


def test_finish_workflow_tool_advertises_structured_details_and_artifacts():
    from mcp.server.fastmcp.tools import Tool

    schema = Tool.from_function(
        fn=server._mcp_finish_calliope_workflow_run
    ).parameters
    properties = schema["properties"]
    detail_options = properties["details"].get(
        "anyOf", [properties["details"]]
    )
    artifact_options = properties["artifacts"].get(
        "anyOf", [properties["artifacts"]]
    )

    assert {option.get("type") for option in detail_options} >= {
        "object", "array", "null"
    }
    artifact_array = next(
        option for option in artifact_options if option.get("type") == "array"
    )
    item_options = artifact_array["items"].get(
        "anyOf", [artifact_array["items"]]
    )
    assert {option.get("type") for option in item_options} >= {"object", "string"}
    assert calliope._normalize_workflow_artifacts(None, "[]") == []
    empty_artifacts = SimpleNamespace(
        execute=lambda *_args, **_kwargs: SimpleNamespace(fetchone=lambda: None)
    )
    assert calliope._normalize_workflow_artifacts(
        empty_artifacts, "unpublished-example"
    ) == [{"ref": "unpublished-example", "version": None, "verified": False}]


def test_manual_workflow_prompt_carries_the_frozen_execution_contract():
    run_id = str(uuid.uuid4())
    prompt = calliope._workflow_run_prompt(
        run_id,
        {
            "id": str(uuid.uuid4()),
            "name": "Data quality watch",
            "version": 1,
            "goal": "Inspect only the governed quality targets.",
            "graph": {
                "trigger": {"kind": "manual"},
                "agent": {"decision_rules": ["Never invent a target."]},
                "outputs": [{"kind": "stage"}, {"kind": "work_inbox"}],
            },
        },
        [{
            "id": "context-1",
            "kind": "instruction",
            "label": "Run context",
            "resolved": {
                "found": True,
                "description": "Use read-only governed warehouse diagnostics.",
            },
        }],
    )

    assert len(prompt) <= 6_000
    assert "FROZEN EXECUTION CONTRACT" in prompt
    assert run_id in prompt
    assert "Inspect only the governed quality targets." in prompt
    assert "Never invent a target." in prompt
    assert "Use read-only governed warehouse diagnostics." in prompt
    assert "get_calliope_personal_context" in prompt
    assert "an artifacts array (use [] when none)" in prompt
    assert "do not call calliope_work_item" in prompt


def test_workflow_personal_context_resolves_owner_from_run_capability(monkeypatch):
    run_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    workflow_id = str(uuid.uuid4())
    brief_id = str(uuid.uuid4())
    observed = {}

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            if "FROM rvbbit.calliope_workflow_runs" in query:
                assert params == (run_id,)
                return Result({
                    "id": run_id,
                    "owner_email": "person@example.com",
                    "session_id": session_id,
                    "workflow_id": workflow_id,
                    "workflow_version": 2,
                    "status": "running",
                })
            assert "FROM rvbbit.calliope_briefs" in query
            assert params == ("person@example.com",)
            return Result({
                "id": brief_id,
                "brief_date": "2026-08-02",
                "timezone": "America/New_York",
                "session_id": str(uuid.uuid4()),
                "latest_surface_id": str(uuid.uuid4()),
                "item_count": 3,
                "source_count": 2,
                "refreshed_at": datetime.now(timezone.utc),
                "payload": {"mode": "personal_brief", "items": [{"title": "One"}]},
            })

    monkeypatch.setattr(
        calliope,
        "_brief_note_rows",
        lambda _conn, owner, received_brief_id: [{
            "id": str(uuid.uuid4()),
            "note_date": "2026-08-02",
            "body": "Follow up with the owner",
            "links": [],
            "created_at": datetime.now(timezone.utc),
        }] if (owner, received_brief_id) == ("person@example.com", brief_id) else [],
    )

    def inbox(_factory, owner, *, include_resolved, limit):
        observed.update({
            "owner": owner,
            "include_resolved": include_resolved,
            "limit": limit,
        })
        return {"items": [{"title": "Open item", "state": "unread"}], "counts": {"open": 1}}

    monkeypatch.setattr(calliope, "_inbox_snapshot", inbox)
    result = calliope.workflow_personal_context(Connection, run_id, True, 18)

    assert observed == {
        "owner": "person@example.com",
        "include_resolved": True,
        "limit": 18,
    }
    assert result["scope"]["run_id"] == run_id
    assert "owner_email" not in result["scope"]
    assert result["brief"]["snapshot"]["mode"] == "personal_brief"
    assert result["notes"][0]["body"] == "Follow up with the owner"
    assert result["inbox"]["items"][0]["state"] == "unread"


def test_native_new_workflow_is_a_no_model_builder_not_a_calliope_shortcut():
    spec = calliope._native_workflow_spec({
        "name": "Daily follow-up",
        "description": "Turn the brief into useful follow-up.",
        "goal": "Review the current Daily Brief and publish material follow-up.",
        "trigger": {"kind": "schedule", "schedule": "0 9 * * 1-5"},
        "context": "Use the signed-in user's brief and notes.",
        "requirements": "personal_context, mcp:linear",
        "decision_rules": "Cite evidence.\nAvoid duplicates.",
        "outputs": ["stage", "work_inbox"],
    })
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    page = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")

    assert spec["trigger"]["kind"] == "schedule"
    assert spec["contexts"] == [{
        "id": "context-1",
        "kind": "instruction",
        "label": "Run context",
        "description": "Use the signed-in user's brief and notes.",
    }]
    assert spec["decision_rules"] == ["Cite evidence.", "Avoid duplicates."]
    assert [item["ref"] for item in spec["requirements"]] == [
        "personal_context", "mcp:linear"
    ]
    assert spec["graph"]["requirements"] == spec["requirements"]
    assert '@mcp.custom_route("/api/calliope/workflows", methods=["POST"])' in backend
    assert '"model_invoked": False' in backend
    assert 'id="workflow-native-form"' in page
    assert 'id="workflow-create-native"' in page
    assert 'id="workflow-native-requirements"' in page
    assert 'els.workflowNew.addEventListener("click", () => showNativeWorkflowBuilder("blank"))' in script
    assert "function createNativeWorkflow" in script
    assert 'api("/api/calliope/workflows", {' in script


def test_preflight_is_side_effect_free_and_blocks_explicit_missing_sources():
    workflow_id = str(uuid.uuid4())

    class Result:
        def __init__(self, rows=None, row=None):
            self.rows = rows or []
            self.row = row

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.row

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None):
            if "FROM rvbbit.mcp_servers" in query:
                return Result(rows=[])
            if "FROM rvbbit.brain_sources" in query:
                return Result(rows=[])
            if "AS briefs" in query:
                return Result(row={"briefs": 1, "notes": 0, "inbox_open": 2})
            if "FROM rvbbit.capability_catalog" in query:
                return Result(rows=[])
            if "JOIN rvbbit.calliope_sessions s ON s.id=v.source_session_id" in query:
                return Result(row={"exists": 1})
            raise AssertionError(query)

    health = {
        "gateway_state": "running",
        "readiness": {"checks": {
            "model": {"status": "ok"},
            "config": {"status": "ok"},
            "gateway": {"status": "ok", "state": "running"},
        }},
    }
    workflow = {
        "id": workflow_id,
        "name": "Project pulse",
        "description": "Project and ticket movement",
        "goal": "Find material project and ticket changes.",
        "version": 1,
        "graph": {
            "trigger": {"kind": "manual"},
            "contexts": [{
                "id": "context-1",
                "kind": "instruction",
                "label": "Scope",
                "description": "Use governed project and ticket sources.",
            }],
            "requirements": [{
                "id": "requirement-1",
                "ref": "project_ticket",
                "label": "Governed project and ticket source",
                "optional": False,
            }],
            "outputs": [{"kind": "stage"}, {"kind": "work_inbox"}],
        },
    }

    preflight = calliope._workflow_preflight(Connection, "person@example.com", workflow, health)

    assert preflight["status"] == "blocked"
    assert preflight["can_run"] is False
    assert preflight["side_effects"] == {
        "model_invoked": False,
        "session_created": False,
        "inbox_written": False,
        "schedule_changed": False,
    }
    requirement = next(
        item for item in preflight["checks"]
        if item["id"] == "requirement:project_ticket"
    )
    assert requirement["status"] == "blocked"
    assert "not registered" in requirement["summary"]
    assert preflight["contract_preview"]["requirements"][0]["ref"] == "project_ticket"


def test_preflight_marks_legacy_inference_as_a_warning_not_a_false_blocker():
    inferred = calliope._workflow_inferred_requirements({
        "name": "Ticket pulse",
        "goal": "Review project tickets and explain ownership gaps.",
        "graph": {"contexts": [], "requirements": []},
    })

    assert [item["ref"] for item in inferred] == ["project_ticket"]
    assert inferred[0]["inferred"] is True


def test_workflow_run_phases_are_readable_and_keep_technical_events_nested():
    steps = [
        {"label": "skill_view", "status": "complete", "source": "runtime"},
        {
            "label": "mcp__rvbbit_warehouse__ask_brain",
            "status": "complete",
            "source": "runtime",
            "duration_ms": 25,
        },
        {
            "label": "mcp__rvbbit_warehouse__run_operator",
            "status": "complete",
            "source": "runtime",
        },
        {
            "label": "mcp__rvbbit_warehouse__finish_calliope_workflow_run",
            "status": "complete",
            "source": "runtime",
        },
    ]

    phases = calliope._workflow_run_phases(steps, "complete")

    assert [phase["id"] for phase in phases] == [
        "prepare", "context", "work", "publish"
    ]
    assert [phase["status"] for phase in phases] == [
        "complete", "complete", "complete", "complete"
    ]
    assert phases[1]["technical_event_count"] == 1
    assert phases[1]["duration_ms"] == 25
    assert phases[1]["steps"][0]["label"].endswith("ask_brain")
    assert phases[2]["steps"][0]["label"].endswith("run_operator")
    assert phases[3]["steps"][0]["label"].endswith(
        "finish_calliope_workflow_run"
    )


def test_blocked_workflow_phase_explains_the_decision_without_hidden_reasoning():
    phases = calliope._workflow_run_phases([
        {
            "label": "mcp__rvbbit_warehouse__finish_calliope_workflow_run",
            "status": "complete",
            "source": "runtime",
        }
    ], "blocked")

    assert phases[2]["id"] == "work"
    assert phases[2]["status"] == "blocked"
    assert "could not reach a supported result" in phases[2]["summary"]
    assert phases[3]["status"] == "complete"
    assert "durable blocked result" in phases[3]["summary"]


def test_revision_from_run_freezes_bounded_outcome_not_prompts_or_tool_payloads():
    run_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    snapshot = calliope._workflow_revision_run_snapshot({
        "id": run_id,
        "workflow_version": 2,
        "session_id": session_id,
        "trigger_kind": "manual",
        "status": "blocked",
        "result_summary": "Missing governed ticket evidence",
        "result_details": {
            "details": {
                "reason": "source_missing",
                "next_step": "Connect Linear; password=hunter2",
                "prompt": "raw private prompt",
                "transcript": ["hidden exchange"],
                "nested": {"tool_payload": {"query": "secret SQL"}},
            },
            "artifacts": [{"ref": "pulse", "version": 3, "verified": True}],
        },
        "steps": [{
            "label": "mcp__rvbbit_warehouse__ask_brain",
            "status": "complete",
            "source": "runtime",
        }],
        "started_at": "2026-08-02T12:00:00+00:00",
        "completed_at": "2026-08-02T12:01:00+00:00",
    })
    encoded = json.dumps(snapshot)

    assert snapshot["run_id"] == run_id
    assert snapshot["workflow_version"] == 2
    assert snapshot["status"] == "blocked"
    assert snapshot["session"] == {
        "id": session_id,
        "url": f"/calliope?session={session_id}",
    }
    assert snapshot["details"]["reason"] == "source_missing"
    assert snapshot["details"]["next_step"] == "Connect Linear; password=[redacted]"
    assert snapshot["artifacts"] == [{
        "ref": "pulse", "version": 3, "verified": True
    }]
    assert all("steps" not in phase for phase in snapshot["phases"])
    for secret in ("raw private prompt", "hidden exchange", "secret SQL", "hunter2"):
        assert secret not in encoded


def test_runtime_steps_are_bounded_redacted_and_collapsed_to_one_tool_lifecycle():
    run_id = str(uuid.uuid4())
    stored = {"steps": []}

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
            if query.startswith("SELECT steps"):
                return Result({"steps": stored["steps"]})
            if query.startswith("UPDATE rvbbit.calliope_workflow_runs SET steps"):
                stored["steps"] = json.loads(params[0])
                return Result()
            raise AssertionError(query)

    calliope._record_workflow_runtime_step(
        Connection,
        run_id,
        "tool.started",
        "execute_sql",
        "api_key=super-secret SELECT count(*)",
    )
    calliope._record_workflow_runtime_step(
        Connection, run_id, "tool.completed", "execute_sql"
    )

    assert len(stored["steps"]) == 1
    assert stored["steps"][0]["status"] == "complete"
    assert stored["steps"][0]["source"] == "runtime"
    assert "super-secret" not in stored["steps"][0]["preview"]
    assert stored["steps"][0]["duration_ms"] >= 0


def test_hermes_operations_snapshot_excludes_prompts_and_delivery_secrets():
    snapshot = calliope._workflow_operations_json(
        {
            "jobs": [{
                "id": "job-1",
                "name": "Morning pulse",
                "prompt": "password=hunter2 inspect everything",
                "deliver": {"webhook": "https://secret.invalid/token"},
                "provider": "openai-codex",
                "schedule_display": "0 9 * * 1-5",
                "enabled": True,
                "state": "scheduled",
                "next_run_at": "2026-08-03T13:00:00+00:00",
                "latest_execution": {
                    "status": "failed",
                    "error": "Bearer abcdefghijklmnop provider unavailable",
                },
            }]
        },
        {
            "status": "ok",
            "gateway_state": "running",
            "active_agents": 0,
            "readiness": {"checks": {"background_queues": {
                "active_api_runs": 1,
                "process_completions": 2,
                "active_delegations": 0,
            }}},
        },
        {"job-1": {"id": "workflow-1", "name": "Morning pulse", "owned": True}},
    )
    encoded = json.dumps(snapshot)

    assert snapshot["summary"] == {"jobs": 1, "enabled": 1, "running": 0, "attention": 1}
    assert snapshot["queue"] == {
        "active_runs": 1,
        "process_completions": 2,
        "active_delegations": 0,
    }
    assert snapshot["jobs"][0]["error"] == "Bearer [redacted] provider unavailable"
    for secret in ("hunter2", "webhook", "secret.invalid", "openai-codex", "prompt", "deliver"):
        assert secret not in encoded


def test_gallery_ask_pins_an_exact_snapshot_and_capsule_has_more_voice():
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    gallery = (HERE / "server.py").read_text(encoding="utf-8")

    assert '"/api/calliope/gallery/artifacts/{slug}/ask"' in backend
    assert "AND kind IN ('app','dashboard') AND path IS NOT NULL" in backend
    assert '"display_url": (\n                        f"/calliope/artifacts/' in backend
    assert '"published_url": artifact.get("path")' in backend
    assert '"source": {"origin": "gallery", "artifact_ref": slug, "version": version}' in backend
    assert 'data-gallery-ask=' in gallery
    assert 'data-home-pin=' in gallery
    assert "Gallery artifact · {title} · v{version}" in backend
    assert "font-size:22px;font-weight:650" in gallery
    assert "text-shadow:.35px 0 currentColor" in gallery


def test_generated_titles_and_hermes_costs_use_the_semantic_ledger():
    migration = (
        ROOT
        / "crates"
        / "pg_rvbbit"
        / "sql"
        / "migrations"
        / "0232_calliope_titles_and_cost_callers.sql"
    ).read_text(encoding="utf-8")
    source = (HERE / "calliope.py").read_text(encoding="utf-8")

    assert "title_source text NOT NULL DEFAULT 'system'" in migration
    assert "ADD COLUMN IF NOT EXISTS caller text" in migration
    assert calliope._generated_session_title(
        'Summary: "A long morning review of the sales pipeline"'
    ) == "A long morning review of the sales pipeline"
    assert calliope._hermes_reported_cost({"cost": {"usd": "0.0125"}}) == 0.0125
    assert calliope._hermes_is_subscription_runtime(
        {}, {"provider": "copilot", "auth_type": "oauth_subscription"}, "copilot"
    ) is True
    assert "SELECT rvbbit.summarize(%s) AS title" in source
    assert "AND title_source='provisional'" in source
    assert "'hermes.calliope_turn'" in source
    assert "cost_source = \"subscription\"" in source
    assert "caller" in inspect.getsource(calliope._record_hermes_receipt)
