"""Focused contracts for native published-artifact data-time controls."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import server  # noqa: E402


def _at(hour: int) -> datetime:
    return datetime(2026, 7, 30, hour, 0, tzinfo=timezone.utc)


def test_as_of_accepts_only_one_normalized_iso_timestamp():
    assert server._normalize_as_of("2026-07-30") == "2026-07-30T00:00:00Z"
    assert (
        server._normalize_as_of("2026-07-30T08:15:30.123456-04:00")
        == "2026-07-30T12:15:30.123456Z"
    )
    assert server._with_as_of("select 1", "2026-07-30T12:00:00Z").startswith(
        "-- rvbbit: as_of 2026-07-30T12:00:00Z\n"
    )
    with pytest.raises(ValueError, match="ISO-8601"):
        server._normalize_as_of("2026-07-30\nselect pg_sleep(10)")
    assert server.tool_run_sql("select 1", "not a timestamp") == {
        "error": {
            "code": "BAD_AS_OF",
            "message": "as_of must be one ISO-8601 timestamp",
        }
    }


def test_validate_sql_keeps_as_of_directive_out_of_read_only_gate(monkeypatch):
    calls = []

    class _Result:
        @staticmethod
        def fetchone():
            return {
                "e": {
                    "safe_select": True,
                    "chosen_candidate": "rvbbit_native",
                    "route_source": "planner",
                    "rvbbit_tables": ["public.orders"],
                    "reason": "eligible",
                    "candidates": [{"name": "rvbbit_native"}],
                }
            }

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def execute(statement, params):
            calls.append((statement, params))
            return _Result()

    monkeypatch.setattr(server, "_conn", lambda *_args, **_kwargs: _Connection())
    result = server.tool_validate_sql("select * from public.orders", "2026-07-30T12:00:00Z")

    assert result["safe_select"] is True
    assert result["as_of_applied"] == "2026-07-30T12:00:00Z"
    assert calls == [
        (
            "SELECT rvbbit.route_explain(%s) AS e",
            ("select * from public.orders",),
        )
    ]


def test_native_dashboard_shim_inherits_url_data_time_without_artifact_changes():
    shim = server._dash_shim("revenue-map")
    assert 'window.RVBBIT_DASHBOARD={slug:"revenue-map"}' in shim
    assert "rvbbit_as_of" in shim
    assert "window.RVBBIT_DASHBOARD.as_of" in shim
    assert "body:JSON.stringify({sql:sql,as_of:asOf})" in shim
    assert "/theme/artifact-lens.js" in shim
    assert "args.as_of||null" in shim
    assert "<iframe" not in shim


def test_time_travel_summary_requires_complete_rvbbit_history():
    covered = [
        {
            "rvbbit_managed": True,
            "generations": 4,
            "earliest": _at(8),
            "latest": _at(12),
        },
        {
            "rvbbit_managed": True,
            "generations": 3,
            "earliest": _at(9),
            "latest": _at(11),
        },
    ]
    points = [{"committed_at": _at(hour)} for hour in (8, 9, 10, 11, 12)]
    summary = server._summarize_dashboard_time_travel(covered, points, 7)
    assert summary["eligible"] is True
    assert summary["version"] == 7
    assert summary["table_count"] == 2
    assert summary["earliest"] == "2026-07-30T09:00:00Z"
    assert summary["points"] == [
        "2026-07-30T09:00:00Z",
        "2026-07-30T10:00:00Z",
        "2026-07-30T11:00:00Z",
        "2026-07-30T12:00:00Z",
    ]

    partial = server._summarize_dashboard_time_travel(
        covered + [{
            "rvbbit_managed": False,
            "generations": 0,
            "earliest": None,
            "latest": None,
        }],
        [],
        7,
    )
    assert partial["eligible"] is False
    assert partial["code"] == "PARTIAL_COVERAGE"
    assert partial["unsupported_count"] == 1

    one_point = server._summarize_dashboard_time_travel(
        covered,
        [{"committed_at": _at(10)}],
        7,
    )
    assert one_point["eligible"] is False
    assert one_point["code"] == "ONE_RETAINED_POINT"


def test_artifact_lens_is_shadow_isolated_native_only_and_heavily_debounced():
    script = (_HERE / "theme" / "artifact-lens.js").read_text(encoding="utf-8")
    css = (_HERE / "theme" / "artifact-lens.css").read_text(encoding="utf-8")
    theme_server = (_HERE / "warehouse_theme.py").read_text(encoding="utf-8")
    dockerfile = (_HERE / "Dockerfile").read_text(encoding="utf-8")

    assert "attachShadow({ mode: \"open\" })" in script
    assert "window.self !== window.top" in script
    assert "dashboard.historical" in script
    assert "SCRUB_DEBOUNCE_MS = 1100" in script
    assert "rvbbit_as_of" in script
    assert "/time-travel" in script
    assert "window.location.assign" in script
    assert "window.RVBBIT_DASHBOARD?.refresh" in script
    assert "sessionStorage" in script
    assert "/theme/artifact-lens.css" in script
    assert ":host {" in css
    assert "position: fixed;" in css
    assert "right: max(18px" in css
    assert "bottom: max(18px" in css
    assert ".lens[data-open=\"true\"] .panel" in css
    assert "/theme/artifact-lens.js" in theme_server
    assert "/theme/artifact-lens.css" in theme_server
    assert "COPY theme ./theme" in dockerfile
