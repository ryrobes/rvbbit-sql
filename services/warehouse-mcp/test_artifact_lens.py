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
    shim = server._dash_shim("revenue-map", 7)
    assert 'window.RVBBIT_DASHBOARD={slug:"revenue-map",version:7}' in shim
    assert "rvbbit_as_of" in shim
    assert "window.RVBBIT_DASHBOARD.as_of" in shim
    assert "body:JSON.stringify({sql:sql,as_of:asOf})" in shim
    assert "window.RVBBIT_DASHBOARD.queryTrace" in shim
    assert "rvbbit:query-trace" in shim
    assert "trace.length>24" in shim
    assert "rows.slice(0,200)" in shim
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


def test_artifact_lens_is_shadow_isolated_trace_capable_and_heavily_debounced():
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
    assert "localStorage" in script
    assert "positionKey" in script
    assert "setPointerCapture" in script
    assert "persistLensPosition" in script
    assert "Pick from dashboard" in script
    assert "Ask Calliope" in script
    assert "Continue in Calliope" not in script
    assert "Pick another" not in script
    assert "ChartApi?.getChart" in script
    assert "getElementsAtEventForMode" in script
    assert "svgContext" in script
    assert "visualBindingEvidence" in script
    assert "indexedCoverage >= 0.8" in script
    assert "indexed_mark_count" in script
    assert "resolveBinding" in script
    assert "renderCandidateHighlights" in script
    assert "candidateConfidence" in script
    assert "isValueLikeText" in script
    assert "best.score < 3" in script
    assert 'setView("trace")' in script
    assert "viewExplicitlyChosen" in script
    assert "/inspect" in script
    assert "runInspectionQuery" in script
    assert "query-drawer" in script
    assert "RESULT_BATCH_SIZE = 100" in script
    assert "Analyze with Calliope" in script
    assert "queryResultPreview" in script
    assert 'mode: analyzeResult ? "query_result" : "selection"' in script
    assert ".slice(0, 12)" in script
    assert "/q" in script
    assert "/api/calliope/investigations" in script
    assert "exact" in script
    assert "likely" in script
    assert "visual" in script
    assert "/theme/artifact-lens.css" in script
    assert ":host {" in css
    assert "position: fixed;" in css
    assert "right: max(18px" in css
    assert "bottom: max(18px" in css
    assert ".lens[data-open=\"true\"] .panel" in css
    assert ".lens[data-open=\"true\"] .trigger" in css
    assert ".query-analyze" in css
    assert ".target-outline" in css
    assert ".candidate-box" in css
    assert ".lens[data-dragging=\"true\"]" in css
    assert ".lens[data-drawer-open=\"true\"] .query-drawer" in css
    assert ".query-table" in css
    assert "cursor: grab;" in css
    assert ".panel {" in css and "z-index: 20;" in css
    assert ".target-outline {" in css and "z-index: 4;" in css
    assert ".evidence-card" in css
    assert "/theme/artifact-lens.js" in theme_server
    assert "/theme/artifact-lens.css" in theme_server
    assert "COPY theme ./theme" in dockerfile


def test_inspection_target_and_binding_are_bounded_and_drop_credentials():
    target = server._sanitize_inspection_target({
        "label": "Revenue for North",
        "selector": "#revenue > strong",
        "tag": "strong",
        "text": "$42,000",
        "data": {
            "data-metric": "revenue",
            "data-api-key": "do-not-store",
        },
        "bounds": {"x": 12.5, "y": 20, "width": 90, "height": 28},
        "viewport": {"width": 1440, "height": 900, "scroll_y": 120},
        "table": {
            "row_index": 2,
            "column_index": 3,
            "column_header": "Revenue",
            "cell_text": "$42,000",
        },
        "visual": {
            "row_index": 2,
            "indexed_mark_count": 42,
            "mark_tag": "circle",
            "mark_text": "North",
            "container_label": "Regional map",
            "text_values": ["North", "South"],
            "data": {"data-i": "2", "data-token": "do-not-store"},
        },
    })
    assert target["data"] == {"data-metric": "revenue"}
    assert target["table"]["row_index"] == 2
    assert target["visual"]["indexed_mark_count"] == 42
    assert target["visual"]["text_values"] == ["North", "South"]
    assert target["visual"]["data"] == {"data-i": "2"}
    assert target["bounds"]["width"] == 90

    binding = server._sanitize_inspection_binding({
        "kind": "table",
        "confidence": "exact",
        "field": "revenue",
        "trace_row_index": 2,
        "row": {"region": "North", "revenue": 42000},
    })
    assert binding["confidence"] == "exact"
    assert binding["trace_row_index"] == 2
    assert binding["row"]["region"] == "North"

    with pytest.raises(ValueError, match="visible"):
        server._sanitize_inspection_target({
            "bounds": {"x": 0, "y": 0, "width": 0, "height": 10},
            "viewport": {"width": 1440, "height": 900},
        })


def test_visual_inspection_cannot_claim_an_ambient_query_trace():
    trace = {
        "id": "query-1",
        "query_hash": "abc123",
        "sql": "select count(*) from public.events",
    }
    assert server._bound_inspection_trace(
        {"kind": "value", "confidence": "visual"},
        trace,
    ) == {}
    assert server._bound_inspection_trace(
        {"kind": "value", "confidence": "likely"},
        trace,
    ) == trace
    assert server._bound_inspection_trace(
        {"kind": "chart", "confidence": "exact"},
        trace,
    ) == trace


def test_inspection_comparison_matches_dimensions_and_calculates_delta():
    current = {"region": "North", "revenue": 42000, "orders": 12}
    latest = [
        {"region": "South", "revenue": 39000, "orders": 11},
        {"region": "North", "revenue": 46200, "orders": 13},
    ]
    matched = server._matching_latest_row(current, latest, 0)
    assert matched["region"] == "North"
    assert server._row_value(current, "Revenue") == ("revenue", 42000)
    delta = server._numeric_delta(42000, 46200)
    assert delta["absolute"] == 4200
    assert delta["percent"] == pytest.approx(10)
