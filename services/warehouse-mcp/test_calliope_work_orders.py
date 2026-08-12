"""Contracts for Calliope's private delegated-work layer."""
from __future__ import annotations

import inspect
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import calliope  # noqa: E402
import server  # noqa: E402


class _Result:
    def __init__(self, value=None):
        self.rows = value if isinstance(value, list) else ([value] if value else [])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def test_work_order_schema_is_migrated_auditable_and_self_healing():
    migration = (
        ROOT
        / "crates"
        / "pg_rvbbit"
        / "sql"
        / "migrations"
        / "0280_calliope_delegated_work.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_work_orders" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_work_order_runs" in migration
    assert "UNIQUE (work_order_id,execution_key)" in migration
    assert "notification_policy IN ('always','attention','failure','never')" in migration
    assert "overlap_policy='skip'" in migration
    assert "0280_calliope_delegated_work" in registry
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_work_orders" in calliope._WORK_ORDER_DDL
    assert "conn.execute(_WORK_ORDER_DDL)" in inspect.getsource(calliope.ensure_tables)


def test_work_order_compiler_keeps_schedule_and_safety_contract_small():
    recurring = calliope._normalize_work_order_spec(
        "Morning risk scan",
        "Review new operational risks and explain only material changes.",
        "0 8 * * 1-5",
        timezone_name="Hermes installation",
        context={"project": "Northstar", "secret": "not-an-instruction"},
    )
    assert recurring["trigger_kind"] == "recurring"
    assert recurring["schedule"] == "0 8 * * 1-5"
    assert recurring["timezone"] == "Hermes installation"
    assert recurring["approval_policy"] == "read_only"
    assert recurring["notification_policy"] == "attention"

    once = calliope._normalize_work_order_spec(
        "Prepare the renewal brief",
        "Collect the current evidence for review.",
        "2026-08-12T09:30:00",
        trigger_kind="once",
        timezone_name="America/New_York",
    )
    assert once["trigger_kind"] == "once"
    assert once["schedule"].endswith("-04:00")

    with pytest.raises(ValueError, match="Hermes installation"):
        calliope._normalize_work_order_spec(
            "Clock-local scan", "Check it.", "0 8 * * *", timezone_name="America/New_York"
        )
    with pytest.raises(ValueError, match="read_only or propose_changes"):
        calliope._normalize_work_order_spec(
            "Unsafe mutation", "Change it.", "2h", approval_policy="autonomous"
        )


@pytest.mark.parametrize(
    ("policy", "status", "attention", "changed", "expected"),
    [
        ("attention", "complete", False, False, False),
        ("attention", "complete", False, True, True),
        ("attention", "blocked", False, False, True),
        ("failure", "complete", True, True, False),
        ("failure", "failed", False, False, True),
        ("always", "complete", False, False, True),
        ("never", "failed", True, True, False),
    ],
)
def test_work_order_notification_policy_suppresses_healthy_noise(
    policy, status, attention, changed, expected
):
    assert calliope._work_order_should_notify(
        policy, status, attention, changed
    ) is expected


def test_draft_resolves_owner_from_the_calliope_session_and_never_activates():
    session_id = str(uuid.uuid4())
    captured = {}
    now = datetime.now(timezone.utc)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            if query.startswith("SELECT id,owner_email FROM rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "owner_email": "owner@example.com"})
            if query.startswith("INSERT INTO rvbbit.calliope_work_orders"):
                captured["params"] = params
                return _Result({
                    "id": params[0],
                    "owner_email": params[1],
                    "source_session_id": params[2],
                    "title": params[3],
                    "instruction": params[4],
                    "trigger_kind": params[5],
                    "schedule": params[6],
                    "schedule_display": params[7],
                    "timezone": params[8],
                    "context": json.loads(params[9]),
                    "approval_policy": params[10],
                    "notification_policy": params[11],
                    "assignee": "calliope",
                    "execution_kind": "agent",
                    "overlap_policy": "skip",
                    "definition_version": 1,
                    "status": "draft",
                    "created_at": now,
                    "updated_at": now,
                })
            raise AssertionError(query)

    work_order = calliope.draft_work_order(
        Connection,
        session_id,
        "Review weekly exceptions",
        "Find material exceptions and prepare a short evidence-backed note.",
        "every 1d",
    )

    assert captured["params"][1] == "owner@example.com"
    assert work_order["owner"] == "owner@example.com"
    assert work_order["status"] == "draft"
    assert work_order["schedule"]["job_id"] is None
    assert "owner" not in inspect.signature(calliope.draft_work_order).parameters


def test_scheduler_prompt_binds_the_managed_job_and_requires_a_durable_finish():
    work_order_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    prompt = calliope._work_order_scheduler_prompt(
        work_order_id, session_id, 3, "hermes-job-7"
    )

    assert work_order_id in prompt
    assert session_id in prompt
    assert "hermes-job-7" in prompt
    assert "begin_calliope_work_order_run" in prompt
    assert "finish_calliope_work_order_run" in prompt
    assert "both false" in prompt


def test_occurrences_use_the_forwarded_cron_session_as_the_deduplication_key():
    source = inspect.getsource(calliope.begin_work_order_run)

    assert "occurrence_key = clean_scheduler_session" in source
    assert 'execution_key = f"{clean_job or clean_trigger}:{occurrence_key}"' in source


def test_managed_routes_tools_stage_and_grouped_run_ui_ship_together():
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    server_source = (HERE / "server.py").read_text(encoding="utf-8")
    page = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")

    assert '@mcp.custom_route("/api/calliope/work-orders", methods=["GET"])' in backend
    assert '"/api/calliope/work-orders/{work_order_id}/actions"' in backend
    assert "Never create a raw Hermes cron job" in backend
    assert 'mcp.tool(name="draft_calliope_work_order")' in server_source
    assert 'mcp.tool(name="begin_calliope_work_order_run")' in server_source
    assert 'mcp.tool(name="finish_calliope_work_order_run")' in server_source
    assert 'id="work-inbox-modes"' in page
    assert "Assigned to Callie" in page
    assert "function renderAssignedWork" in script
    assert "function renderWorkOrder" in script
    assert "work_order: renderWorkOrder" in script
    assert "data-work-order-run-group" in script
    assert 'if "(404)" not in str(exc)' in backend
    assert 'if (status === "paused") controls.push(["resume", "Resume"]);' in script
    assert ".work-order-card" in css
    assert ".session-run-group" in css


def test_cron_transport_is_attributed_to_the_assignment_owner():
    source = inspect.getsource(server._calliope_activity_for_hermes_session)
    resolver = inspect.getsource(server._resolve_activity_context)

    assert "calliope_work_orders" in source
    assert 're.match(r"^cron_([^_]+)_"' in source
    assert '"kind": "work_order"' in source
    assert '"calliope_work_order"' in resolver
    assert "begin_calliope_work_order_run" in resolver
