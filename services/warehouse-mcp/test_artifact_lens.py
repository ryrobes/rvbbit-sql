"""Focused contracts for native published-artifact data-time controls."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import server  # noqa: E402
import warehouse_theme  # noqa: E402


def _at(hour: int) -> datetime:
    return datetime(2026, 7, 30, hour, 0, tzinfo=timezone.utc)


def _semantic_manifest():
    return {
        "semantic_map": {
            "description": "Business values visible on the dashboard.",
            "objects": [{
                "id": "regional_revenue",
                "kind": "scalar",
                "meaning": {
                    "label": "Regional revenue",
                    "description": "Recognized revenue in the selected region.",
                    "unit": "USD",
                    "formula": "Sum of recognized order revenue for the current region.",
                },
                "parameters": {
                    "region": {
                        "type": "text",
                        "default": "North",
                        "source": "#region-filter",
                    },
                    "minimum_orders": {"type": "integer", "default": 2},
                },
                "bindings": [{"selector": "#regional-revenue"}],
                "evaluator": {
                    "sql": (
                        "select sum(revenue) as value from public.orders "
                        "where region={{region}} and orders >= {{minimum_orders}}"
                    ),
                    "shape": "scalar",
                    "value_column": "value",
                },
                "display": {"prefix": "$", "decimals": 0},
                "source_queries": ["orders_by_region"],
            }],
        },
    }


def test_semantic_map_normalizes_binds_and_safely_renders_context():
    manifest = server._normalize_semantic_manifest(_semantic_manifest())
    semantic_map = manifest["semantic_map"]
    semantic_object = semantic_map["objects"][0]

    assert semantic_map["schema_version"] == "rvbbit.semantic-map.v1"
    assert semantic_object["id"] == "regional_revenue"
    assert semantic_object["bindings"] == [{"selector": "#regional-revenue"}]
    assert len(semantic_object["definition_hash"]) == 20
    sql, context = server._render_semantic_sql(
        semantic_object,
        {"region": "O'Reilly", "minimum_orders": 7},
    )
    assert "region='O''Reilly'" in sql
    assert "orders >= 7" in sql
    assert context == {"region": "O'Reilly", "minimum_orders": 7}


def test_semantic_map_requires_declared_replay_parameters_and_executes_at_publish(monkeypatch):
    wrong_schema = _semantic_manifest()
    wrong_schema["semantic_map"]["schema_version"] = "rvbbit.semantic-map.v99"
    with pytest.raises(ValueError, match="unsupported semantic map schema"):
        server._normalize_semantic_manifest(wrong_schema)

    invalid = _semantic_manifest()
    invalid["semantic_map"]["objects"][0]["evaluator"]["sql"] += " and team={{team}}"
    with pytest.raises(ValueError, match="undeclared SQL parameters: team"):
        server._normalize_semantic_manifest(invalid)

    calls = []
    monkeypatch.setattr(
        server,
        "tool_validate_sql",
        lambda sql, as_of=None: {
            "valid": True,
            "safe_select": True,
            "engine": "rvbbit_native",
        },
    )

    def run(sql, as_of=None, limit=None):
        calls.append((sql, as_of, limit))
        return {
            "columns": [{"name": "value", "type": "numeric"}],
            "rows": [{"value": 42000}],
            "row_count": 1,
            "elapsed_ms": 3,
        }

    monkeypatch.setattr(server, "tool_run_sql", run)
    manifest, report = server._prepare_artifact_manifest(
        _semantic_manifest(), execute=True,
    )
    assert manifest["semantic_map"]["objects"][0]["meaning"]["label"] == "Regional revenue"
    assert report["object_count"] == 1
    assert report["objects"][0]["verified"] is True
    assert report["objects"][0]["value"] == 42000
    assert calls[0][2] == 2


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


def test_read_only_connection_is_frozen_before_any_statement(monkeypatch):
    events = []

    class _Connection:
        def __init__(self):
            self._read_only = False

        @property
        def read_only(self):
            return self._read_only

        @read_only.setter
        def read_only(self, value):
            self._read_only = value
            events.append(("read_only", value))

        def execute(self, statement):
            events.append(("execute", statement))

    connection = _Connection()
    monkeypatch.setattr(server.psycopg, "connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(
        server,
        "_set_request_tracking_user",
        lambda conn: conn.execute("request tracking"),
    )

    assert server._conn(read_only=True) is connection
    assert events[0] == ("read_only", True)
    assert events[1] == ("execute", "request tracking")
    assert events[2] == (
        "execute",
        f"SET statement_timeout = {server.STMT_TIMEOUT_MS}",
    )


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


def test_validate_sql_separates_postgres_fallback_from_router_eligibility(monkeypatch):
    class _Result:
        @staticmethod
        def fetchone():
            return {
                "e": {
                    "safe_select": False,
                    "read_only_select": True,
                    "chosen_candidate": None,
                    "route_source": "none",
                    "reason": "unsupported token: rvbbit.",
                    "candidates": [],
                }
            }

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def execute(_statement, _params):
            return _Result()

    monkeypatch.setattr(server, "_conn", lambda *_args, **_kwargs: _Connection())
    result = server.tool_validate_sql(
        "SELECT rvbbit.clover_forecast('[1,2,3]'::text, '2'::text)"
    )

    assert result["safe_select"] is True
    assert result["read_only_select"] is True
    assert result["router_eligible"] is False
    assert result["engine"] == "postgres"
    assert result["route_source"] == "postgres_fallback"
    assert result["router_reason"] == "unsupported token: rvbbit."


def test_old_router_fallback_is_exact_and_does_not_admit_mutations():
    assert server._route_explain_read_only_select({
        "safe_select": False,
        "reason": "unsupported token: rvbbit.",
    }) is True
    assert server._route_explain_read_only_select({
        "safe_select": False,
        "reason": "unsupported token: delete",
    }) is False


def test_run_sql_returns_planner_resolved_warehouse_lineage(monkeypatch):
    class _Description:
        name = "customer_id"
        type_code = 23

    class _Cursor:
        description = [_Description()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return None

        def fetchmany(self, _limit):
            return [{"customer_id": 7}]

        def fetchone(self):
            return None

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def cursor():
            return _Cursor()

    monkeypatch.setattr(
        server,
        "tool_validate_sql",
        lambda _sql, _as_of=None: {
            "valid": True,
            "safe_select": True,
            "engine": "rvbbit_native",
            "rvbbit_tables": ["commerce.customers"],
        },
    )
    monkeypatch.setattr(server, "_session_pg_role", lambda: None)
    monkeypatch.setattr(server, "_conn", lambda **_kwargs: _Connection())
    monkeypatch.setattr(
        server,
        "_referenced_tables",
        lambda _sql: ["commerce.customers", "commerce.orders"],
    )

    result = server.tool_run_sql(
        "SELECT c.customer_id FROM commerce.customers c "
        "LEFT JOIN commerce.orders o USING (customer_id)"
    )

    assert result["warehouse_objects"] == [
        "commerce.customers",
        "commerce.orders",
    ]
    assert result["rvbbit_tables"] == ["commerce.customers"]
    assert result["lineage"] == {
        "warehouse_objects": ["commerce.customers", "commerce.orders"]
    }
    assert server._objects("run_sql", {}, result) == [
        "commerce.customers",
        "commerce.orders",
    ]
    assert server._summary("run_sql", result)["warehouse_objects"] == [
        "commerce.customers",
        "commerce.orders",
    ]


def test_postgres_fallback_runs_read_only_and_flushes_delayed_receipts(monkeypatch):
    connection_args = []

    class _Description:
        name = "forecast"
        type_code = 3802

    class _Cursor:
        description = [_Description()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return None

        def fetchmany(self, _limit):
            return [{"forecast": {"median": [4, 5]}}]

        def fetchone(self):
            return None

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(server, "tool_validate_sql", lambda *_args: {
        "valid": True,
        "safe_select": True,
        "router_eligible": False,
        "engine": "postgres",
    })
    monkeypatch.setattr(server, "_session_pg_role", lambda: None)
    monkeypatch.setattr(server, "_referenced_tables", lambda _sql: [])
    monkeypatch.setattr(
        server,
        "_conn",
        lambda **kwargs: connection_args.append(kwargs) or _Connection(),
    )
    monkeypatch.setattr(server, "_flush_delayed_semantic_receipts", lambda: 1)

    result = server.tool_run_sql(
        "SELECT rvbbit.clover_forecast('[1,2,3]'::text, '2'::text)"
    )

    assert connection_args == [{"read_only": True, "role": None}]
    assert result["engine"] == "postgres"
    assert result["semantic_receipts_flushed"] == 1


def test_publish_quarantines_bad_optional_semantics_instead_of_rejecting_artifact():
    invalid = {
        "app_kind": "dashboard",
        "semantic_map": {
            "schema_version": "rvbbit.semantic-map.v99",
            "objects": [{"id": "broken"}],
        },
    }

    manifest, report = server._prepare_artifact_manifest_for_publish(invalid)

    assert manifest["app_kind"] == "dashboard"
    assert "semantic_map" not in manifest
    assert manifest["semantic_map_warning"]["code"] == "SEMANTIC_MAP_QUARANTINED"
    assert len(manifest["semantic_map_warning"]["source_hash"]) == 20
    assert report["object_count"] == 0
    assert report["warning"] == manifest["semantic_map_warning"]


def test_native_dashboard_shim_inherits_url_data_time_without_artifact_changes():
    manifest = _semantic_manifest()
    manifest["note"] = "</script><script>window.pwned=true</script>"
    shim = server._dash_shim("revenue-map", 7, manifest)
    assert 'window.RVBBIT_DASHBOARD={slug:"revenue-map",version:7,calliope_enabled:' in shim
    assert "__CALLIOPE_ENABLED__" not in shim
    assert "window.RVBBIT_DASHBOARD.manifest=" in shim
    assert "root.semanticObjects" in shim
    assert "root.semanticObject" in shim
    assert "root.bindSemanticObject" in shim
    assert "elementRuntime=new WeakMap()" in shim
    assert "elementRuntime.set(node,snapshot)" in shim
    assert "root.semanticObject=(id,target)" in shim
    assert "data-rvbbit-object" in shim
    assert "rvbbit:semantic-map-ready" in shim
    assert "\\u003c/script\\u003e" in shim
    assert "<script>window.pwned" not in shim
    assert "rvbbit_as_of" in shim
    assert "window.RVBBIT_DASHBOARD.as_of" in shim
    assert "body:JSON.stringify({sql:sql,as_of:asOf})" in shim
    assert "window.RVBBIT_DASHBOARD.queryTrace" in shim
    assert "rvbbit:query-trace" in shim
    assert "trace.length>24" in shim
    assert "rows.slice(0,200)" in shim
    assert '<link rel="preload" href="/theme/artifact-lens.css" as="style">' in shim
    assert "/theme/artifact-lens.js" in shim
    assert "args.as_of||null" in shim
    assert "<iframe" not in shim


def test_dashboard_templates_teach_the_same_semantic_publish_contract():
    dashboard = server.tool_dashboard_template()
    live_app = server.tool_live_app_template("html", "dashboard")
    example = dashboard["semantic_map_example"]

    assert example["semantic_map"]["schema_version"] == "rvbbit.semantic-map.v1"
    assert example["semantic_map"]["objects"][0]["bindings"][0]["selector"] == "#kpi-revenue"
    assert " as value " in example["semantic_map"]["objects"][0]["evaluator"]["sql"]
    assert live_app["semantic_map_example"] == example
    assert any("bindBusinessObject" in instruction for instruction in dashboard["how_to_use"])
    assert any("semantic_map_example" in instruction for instruction in live_app["how_to_use"])


def test_tanstack_chart_template_is_opt_in_native_and_semantically_bound():
    ordinary = server.tool_dashboard_template()
    experiment = server.tool_tanstack_chart_template()

    assert "/charts/rvbbit-tanstack-charts-0.3.1.js" not in ordinary["template_html"]
    assert "chart.js@4.5.0" in ordinary["template_html"]
    assert experiment["status"] == "experimental"
    assert experiment["tanstack_charts_version"] == "0.3.1"
    assert experiment["runtime_asset"] == "/charts/rvbbit-tanstack-charts-0.3.1.js"
    assert experiment["runtime_asset"] in experiment["template_html"]
    assert "mountRvbbitChart" in experiment["template_html"]
    assert "rvbbit:chart-select" in experiment["template_html"]
    assert "chart.js@" not in experiment["template_html"]

    objects = experiment["manifest"]["semantic_map"]["objects"]
    assert {item["id"] for item in objects} == {
        "revenue_by_channel",
        "roas_by_channel",
        "monthly_revenue",
        "monthly_spend",
    }
    assert all(item["definition_hash"] for item in objects)
    assert all(item["evaluator"]["shape"] == "scalar" for item in objects)
    assert any("default" in instruction.lower() and "chart.js" in instruction.lower()
               for instruction in experiment["how_to_use"])


def test_tanstack_scene_evidence_keeps_svg_points_and_capture_bridge_batches_queries():
    evidence_script = server._SEMANTIC_EVIDENCE_JS
    capture_source = Path(server.__file__).read_text(encoding="utf-8")

    assert "renderer:'tanstack-svg'" in evidence_script
    assert "[data-rvbbit-key][data-row-index]" in evidence_script
    assert "data-rvbbit-object-ref" in evidence_script
    assert "svgGeometry" in evidence_script
    assert "String(tool || '').endsWith('run_sql_multi')" in capture_source


def test_tanstack_runtime_selects_the_exact_svg_datum_at_a_bar_edge():
    """Large bars must select by their keyed element, not a nearest-point radius."""
    from playwright.sync_api import sync_playwright

    html = warehouse_theme.inline_chart_runtime("""<!doctype html>
    <html><head>
      <script defer src="/charts/rvbbit-tanstack-charts-0.3.1.js"></script>
      <style>#chart { width: 720px; }</style>
    </head><body>
      <div id="chart"></div>
      <script>
        window.RVBBIT_DASHBOARD = {
          bindSemanticObject(id, target) {
            target.setAttribute('data-rvbbit-object', id);
          }
        };
        const C = window.RVBBIT_CHARTS;
        const rows = [
          {channel: 'Search', revenue: 990000},
          {channel: 'Social', revenue: 670000}
        ];
        window.selectedByUser = null;
        window.selectionEvents = [];
        window.addEventListener('rvbbit:chart-select', event => {
          window.selectionEvents.push(event.detail);
        });
        C.mountRvbbitChart('#chart', {
          definition: C.defineChart({
            marks: [C.barX(rows, {
              id: 'revenue-bars', x: 'revenue', y: 'channel', key: 'channel',
              fill: '#d69d2e'
            })],
            x: {scale: C.scales.linear},
            y: {scale: () => C.scales.band().padding(.2)}
          }),
          height: 260,
          initialWidth: 720,
          ariaLabel: 'Revenue by channel',
          onSelect(point) { window.selectedByUser = point?.datum || null; }
        }, {
          id: 'channel-chart',
          query: 'channel',
          marks: {
            'revenue-bars': {
              x: 'revenue', y: 'channel', value: 'revenue',
              context: ['channel'], semanticObject: 'revenue_by_channel'
            }
          }
        });
      </script>
    </body></html>""")

    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 900, "height": 500})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html, wait_until="load")
        page.wait_for_selector("#chart [data-rvbbit-key][data-value]", timeout=3_000)
        facts = page.evaluate("""() => {
          const point = document.querySelector('#chart [data-rvbbit-key][data-value]');
          const box = point.getBoundingClientRect();
          // Deliberately click the far edge of a wide bar. TanStack's native
          // nearest-point hit radius does not resolve this location by itself.
          point.dispatchEvent(new MouseEvent('click', {
            bubbles: true, composed: true, clientX: box.left + 2, clientY: box.top + 2
          }));
          return {
            attributes: {
              query: point.dataset.rvbbitQuery,
              row: point.dataset.rowIndex,
              field: point.dataset.field,
              object: point.dataset.rvbbitObject,
              objectRef: point.dataset.rvbbitObjectRef
            },
            selection: RVBBIT_CHARTS.selection('channel-chart'),
            selectedByUser: window.selectedByUser,
            events: window.selectionEvents
          };
        }""")
        browser.close()

    assert errors == []
    assert facts["attributes"] == {
        "query": "channel",
        "row": "0",
        "field": "revenue,channel",
        "object": "revenue_by_channel",
        "objectRef": "revenue_by_channel",
    }
    assert facts["selection"]["datum"] == {"channel": "Search", "revenue": 990000}
    assert facts["selection"]["value"] == 990000
    assert facts["selectedByUser"] == facts["selection"]["datum"]
    assert facts["events"][-1]["semanticObject"] == "revenue_by_channel"


def test_verified_semantic_overlay_uses_captured_binding_and_replays_sql(monkeypatch):
    evidence = {
        "authored_semantic_map": {},
        "dom": {
            "candidate_elements": 1,
            "candidates": [{
                "candidate_id": "candidate_001",
                "selector": "main > section#summary > strong.kpi-value",
                "value": "$42K",
                "represented_elements": 1,
            }],
        },
    }
    candidate = {
        "candidate_id": "candidate_001",
        "id": "recognized_revenue",
        "kind": "scalar",
        "meaning": {
            "label": "Recognized revenue",
            "description": "Recognized revenue represented by the summary callout.",
            "unit": "USD",
            "formula": "Sum of recognized revenue for the current scope.",
        },
        # A model-supplied selector is never trusted; captured evidence owns it.
        "bindings": [{"selector": "#invented-by-model"}],
        "parameters": {},
        "evaluator": {
            "sql": "select 42000::numeric as value",
            "shape": "scalar",
            "value_column": "value",
        },
        "display": {"prefix": "$", "compact": True},
    }
    monkeypatch.setattr(
        server,
        "tool_validate_sql",
        lambda sql, as_of=None: {"valid": True, "safe_select": True, "engine": "postgres"},
    )
    monkeypatch.setattr(
        server,
        "tool_run_sql",
        lambda sql, as_of=None, limit=None: {
            "columns": [{"name": "value", "type": "numeric"}],
            "rows": [{"value": 42000}],
            "row_count": 1,
            "elapsed_ms": 3,
        },
    )

    semantic_map, report = server._verified_semantic_overlay(
        {"objects": [candidate]}, evidence, "analyst@example.com"
    )

    assert report["verified_count"] == 1
    assert report["rejected_count"] == 0
    assert report["coverage"] == 1.0
    semantic_object = semantic_map["objects"][0]
    assert semantic_object["id"] == "recognized_revenue"
    assert semantic_object["bindings"] == [{
        "selector": "main > section#summary > strong.kpi-value"
    }]
    assert semantic_object["evaluator"]["sql"] == "select 42000::numeric as value"


def test_semantic_operator_payload_unwraps_sql_and_agent_result_envelopes():
    payload, run_id = server._semantic_operator_payload({
        "result": {
            "agent_run_id": "run-123",
            "status": "done",
            "result": {
                "description": "Compiled values",
                "objects": [{"candidate_id": "candidate_001"}],
            },
        },
    })

    assert run_id == "run-123"
    assert payload["description"] == "Compiled values"
    assert payload["objects"] == [{"candidate_id": "candidate_001"}]


def test_semantic_overlay_is_additive_and_authored_definitions_win():
    authored = server._normalize_semantic_manifest(_semantic_manifest())

    def generated(object_id, selector):
        raw = dict(_semantic_manifest()["semantic_map"]["objects"][0])
        raw["id"] = object_id
        raw["bindings"] = [{"selector": selector}]
        return server._normalize_semantic_object(raw)

    enrichment = {
        "status": "ready",
        "semantic_map": {
            "schema_version": "rvbbit.semantic-map.v1",
            "objects": [
                generated("regional_revenue", "#model-duplicate-id"),
                generated("overlapping_revenue", "#regional-revenue"),
                generated("order_count", "#order-count"),
            ],
        },
        "verification": {"verified_count": 3, "rejected_count": 0, "coverage": 1.0},
        "prompt_version": "artifact-semantic-enricher.v1",
        "model": "openai/gpt-5.6-sol",
    }

    effective = server._merge_semantic_overlay(authored, enrichment)

    objects = effective["semantic_map"]["objects"]
    assert [item["id"] for item in objects] == ["regional_revenue", "order_count"]
    assert objects[0]["bindings"] == [{"selector": "#regional-revenue"}]
    assert effective["semantic_enrichment"]["status"] == "ready"


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
    assert 'host.style.setProperty("visibility", "hidden", "important")' in script
    assert 'stylesheet.addEventListener("load"' in script
    assert 'stylesheet.addEventListener("error"' in script
    assert 'host.style.removeProperty("visibility")' in script
    assert script.index('stylesheet.addEventListener("load"') < script.index("root.appendChild(stylesheet)")
    assert "window.requestAnimationFrame(() => window.requestAnimationFrame(initializePosition))" not in script
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
    assert "[panelHeader, trigger].forEach" in script
    assert "suppressTriggerClickUntil" in script
    assert "Pick from dashboard" not in script
    assert "Selection active" in script
    assert 'shell.dataset.open === "true" && !pickerActive' in script
    assert "Browse dashboard query data" in script
    assert "dashboardQueryEntries" in script
    assert "openDashboardQuery" in script
    assert "captured rows" in script
    assert "query-source" in script
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
    assert "semanticEntries" in script
    assert "semanticObjectForElement" in script
    assert "semanticSelectionForElement" in script
    assert "semanticSourceValue" in script
    assert "$element.data." in script
    assert "pollSemanticEnrichment" in script
    assert "/semantic-enrichment" in script
    assert "Named business object" in script
    assert "Reproducible warehouse definition" in script
    assert "Open recreated result" in script
    assert "Watch this value" in script
    assert "loadSemanticWatches" in script
    assert 'handle?.kind === "artifact_object"' in script
    assert ".watch-panel" in css
    assert 'semantic_object: semanticSelection' in script
    assert '"semantic-lens"' in script
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
    assert "Analyze with Calliope" not in script
    assert "Ask Calliope" in script
    assert "queryResultPreview" in script
    assert "highlightSql" in script
    assert "formatSql" in script
    assert "SQL_FORMAT_PHRASES" in script
    assert "SQL_KEYWORDS" in script
    assert "query-sql-toggle" in script
    assert "query-sql-view" in script
    assert "query-sql-copy" in script
    assert 'setQuerySqlVisible(false)' in script
    assert 'aria-pressed="false"' in script
    assert "dashboardQueryInspection" in script
    assert 'source: "dashboard runtime query trace"' in script
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
    assert "width: max-content;" in css
    assert "height: max-content;" in css
    assert "right: max(18px" in css
    assert "bottom: max(18px" in css
    assert ".lens[data-open=\"true\"] .panel" in css
    assert ".lens[data-open=\"true\"] .trigger" in css
    assert ".query-analyze" in css
    assert ".target-outline" in css
    assert ".candidate-box" in css
    assert ".candidate-box.semantic" in css
    assert ".semantic-replay" in css
    assert ".semantic-context" in css
    assert ".lens[data-dragging=\"true\"]" in css
    assert "grid-template-rows: auto auto minmax(0, 1fr) auto;" in css
    assert "height: min(720px, calc(100dvh - 96px));" in css
    assert "overscroll-behavior: contain;" in css
    assert ".view::-webkit-scrollbar" in css
    assert ".lens[data-drawer-open=\"true\"] .query-drawer" in css
    assert ".query-table" in css
    assert ".query-sql-toggle[aria-pressed=\"true\"]" in css
    assert ".query-sql-code .sql-keyword" in css
    assert ".query-sql-code .sql-comment" in css
    assert ".query-sql-view pre::-webkit-scrollbar-thumb" in css
    assert "cursor: grab;" in css
    assert ".panel {" in css and "z-index: 20;" in css
    assert ".target-outline {" in css and "z-index: 4;" in css
    assert ".evidence-card" in css
    assert "/theme/artifact-lens.js" in theme_server
    assert "/theme/artifact-lens.css" in theme_server
    assert "COPY services/warehouse-mcp/theme ./theme" in dockerfile


def test_closing_artifact_lens_disables_all_dashboard_selection_behavior():
    from base64 import b64encode

    from playwright.sync_api import sync_playwright

    lens_script = (_HERE / "theme" / "artifact-lens.js").read_text(encoding="utf-8")
    lens_css = b64encode((_HERE / "theme" / "artifact-lens.css").read_bytes()).decode()
    lens_script = lens_script.replace(
        'stylesheet.href = "/theme/artifact-lens.css";',
        f'stylesheet.href = "data:text/css;base64,{lens_css}";',
    ).replace("</script", "<\\/script")
    html = """<!doctype html><html><head><style>
      body { min-height: 1800px; margin: 40px; background: #181818; color: white; }
      .value { display: inline-block; margin: 20px; padding: 24px; border: 1px solid #333; }
    </style></head><body>
      <div id="first" class="value" data-field="amount">$42</div>
      <div id="second" class="value" data-field="amount">$84</div>
      <script>
        window.dashboardClicks = 0;
        document.addEventListener('click', event => {
          if (event.target.id === 'second') window.dashboardClicks += 1;
        });
        window.RVBBIT_DASHBOARD = {
          slug: 'lens-close-test', version: 1, historical: false, manifest: {},
          queryTrace: () => [{
            sql: 'select amount from sample', columns: [{name: 'amount'}],
            rows: [{amount: 42}, {amount: 84}], row_count: 2
          }],
          semanticObjects: () => []
        };
        window.fetch = async url => {
          const href = String(url);
          const data = href.includes('/time-travel')
            ? {eligible: false, code: 'PARTIAL_COVERAGE', message: 'Trace only'}
            : href.includes('/inspect')
              ? {
                  selection: {label: 'Amount', tag: 'div'},
                  binding: {confidence: 'exact', field: 'amount', value: '42'},
                  provenance: {}, sources: []
                }
              : {status: 'disabled'};
          return new Response(JSON.stringify(data), {
            status: 200, headers: {'content-type': 'application/json'}
          });
        };
      </script>
      <script>__LENS_SCRIPT__</script>
    </body></html>""".replace("__LENS_SCRIPT__", lens_script)

    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 700})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html, wait_until="load")
        page.wait_for_selector("rvbbit-artifact-lens")
        page.evaluate("""() => {
          const root = document.querySelector('rvbbit-artifact-lens').shadowRoot;
          root.querySelector('.trigger').click();
          root.querySelector('[data-view="trace"]').click();
        }""")
        page.wait_for_timeout(120)
        page.evaluate("""() => {
          const element = document.querySelector('#first');
          const box = element.getBoundingClientRect();
          const init = {bubbles: true, clientX: box.left + 4, clientY: box.top + 4};
          element.dispatchEvent(new PointerEvent('pointermove', init));
          element.dispatchEvent(new MouseEvent('click', init));
        }""")
        page.wait_for_function("""() => {
          const root = document.querySelector('rvbbit-artifact-lens').shadowRoot;
          return root.querySelector('.target-outline').dataset.selected === 'true';
        }""")
        page.evaluate("""() => {
          document.querySelector('rvbbit-artifact-lens').shadowRoot.querySelector('.close').click();
          const element = document.querySelector('#second');
          const box = element.getBoundingClientRect();
          const init = {bubbles: true, clientX: box.left + 4, clientY: box.top + 4};
          element.dispatchEvent(new PointerEvent('pointermove', init));
          element.dispatchEvent(new MouseEvent('click', init));
          window.dispatchEvent(new Event('scroll'));
          window.dispatchEvent(new Event('resize'));
        }""")
        page.wait_for_timeout(120)
        state = page.evaluate("""() => {
          const root = document.querySelector('rvbbit-artifact-lens').shadowRoot;
          const shell = root.querySelector('.lens');
          const outline = root.querySelector('.target-outline');
          return {
            open: shell.dataset.open,
            picking: shell.dataset.picking,
            documentPicking: document.documentElement.classList.contains(
              'rvbbit-artifact-lens-picking'
            ),
            outlineHidden: outline.hidden,
            outlineSelected: outline.dataset.selected,
            outlineDisplay: getComputedStyle(outline).display,
            candidateCount: root.querySelectorAll('.candidate-box').length,
            hintHidden: root.querySelector('.picker-hint').hidden,
            dashboardClicks: window.dashboardClicks,
          };
        }""")
        browser.close()

    assert errors == []
    assert state == {
        "open": "false",
        "picking": "false",
        "documentPicking": False,
        "outlineHidden": True,
        "outlineSelected": "false",
        "outlineDisplay": "none",
        "candidateCount": 0,
        "hintHidden": True,
        # The post-close click reaches the dashboard instead of the Lens.
        "dashboardClicks": 1,
    }


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


def test_semantic_inspection_uses_versioned_definition_not_browser_sql(monkeypatch):
    manifest = server._normalize_semantic_manifest(_semantic_manifest())
    semantic_object = manifest["semantic_map"]["objects"][0]

    class Result:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = rows or []

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def execute(statement, _params=None):
            if "FROM rvbbit.dashboards WHERE slug" in statement:
                return Result({
                    "id": 9,
                    "slug": "semantic-demo",
                    "name": "Semantic demo",
                    "description": "A demo",
                    "latest_version": 3,
                    "runtime_kind": "html",
                    "app_kind": "dashboard",
                })
            if "SELECT manifest FROM rvbbit.dashboard_versions" in statement:
                return Result({"manifest": manifest})
            if "FROM rvbbit.dashboard_deps" in statement:
                return Result(rows=[])
            raise AssertionError(statement)

    class ReadOnly:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return self

    calls = []

    def run(sql, as_of=None, limit=None):
        calls.append((sql, as_of, limit))
        return {
            "columns": [{"name": "value", "type": "numeric"}],
            "rows": [{"value": 42000}],
            "row_count": 1,
            "engine": "rvbbit_native",
            "elapsed_ms": 2,
        }

    monkeypatch.setattr(server, "_conn", lambda *_args, **_kwargs: Connection())
    monkeypatch.setattr(server, "_ro", lambda *_args, **_kwargs: ReadOnly())
    monkeypatch.setattr(server, "_referenced_tables", lambda _sql: [])
    monkeypatch.setattr(
        server,
        "tool_validate_sql",
        lambda _sql, _as_of=None: {
            "valid": True,
            "safe_select": True,
            "engine": "rvbbit_native",
        },
    )
    monkeypatch.setattr(server, "tool_run_sql", run)

    result = server._dashboard_inspection(
        "semantic-demo",
        3,
        {
            "label": "$42K",
            "selector": "#regional-revenue",
            "tag": "strong",
            "text": "$42K",
            "bounds": {"x": 20, "y": 30, "width": 100, "height": 40},
            "viewport": {"width": 1440, "height": 900},
        },
        {"kind": "value", "confidence": "visual"},
        {
            "id": "browser-forged",
            "sql": "select secret_value from private.credentials",
        },
        {
            "id": "regional_revenue",
            "definition_hash": semantic_object["definition_hash"],
            "context": {"region": "North", "minimum_orders": 2},
            "rendered_value": "$42K",
        },
    )

    assert result["semantic_object"]["meaning"]["label"] == "Regional revenue"
    assert result["semantic_object"]["context"]["region"] == "North"
    assert result["binding"]["confidence"] == "semantic"
    assert result["provenance"]["source"] == "semantic_map"
    assert "private.credentials" not in result["provenance"]["sql"]
    assert "region='North'" in result["provenance"]["sql"]
    assert result["replay"]["status"] == "verified"
    assert result["replay"]["value"] == 42000
    assert calls == [(result["provenance"]["sql"], None, 2)]


def test_dependency_crawl_indexes_semantic_evaluators_as_versioned_sources(monkeypatch):
    manifest = server._normalize_semantic_manifest(_semantic_manifest())
    writes = []

    class Result:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = rows or []

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def execute(statement, params=None):
            if "SELECT id, latest_version FROM rvbbit.dashboards" in statement:
                return Result({"id": 14, "latest_version": 4})
            if "SELECT html, source_files, manifest" in statement:
                return Result({
                    "html": "<html><body>Runtime-built dashboard</body></html>",
                    "source_files": {},
                    "manifest": manifest,
                })
            if "SELECT DISTINCT args->>'sql'" in statement:
                return Result(rows=[])
            if "SELECT DISTINCT name FROM rvbbit.metric_defs" in statement:
                return Result(rows=[])
            writes.append((statement, params))
            return Result()

    monkeypatch.setattr(server, "_ensure_dashboard_tables", lambda: None)
    monkeypatch.setattr(server, "_ensure_activity_table", lambda: None)
    monkeypatch.setattr(server, "_conn", lambda *_args, **_kwargs: Connection())
    monkeypatch.setattr(
        server,
        "_referenced_tables",
        lambda sql: ["public.orders"] if "public.orders" in sql else [],
    )

    result = server.dashboard_crawl("semantic-demo", use_llm=False)
    assert result["status"] == "live"
    assert result["queries"] == 0
    assert result["semantic_objects"] == 1
    assert result["tables"] == ["public.orders"]
    dependency_rows = [
        params
        for statement, params in writes
        if "INSERT INTO rvbbit.dashboard_deps" in statement
    ]
    assert any(
        params[2] == "semantic"
        and params[3] == "regional_revenue"
        and params[5] == "semantic-map:regional_revenue"
        for params in dependency_rows
    )
    assert any(
        params[2] == "table"
        and params[3] == "public.orders"
        and params[5] == "semantic-map:regional_revenue"
        for params in dependency_rows
    )


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
