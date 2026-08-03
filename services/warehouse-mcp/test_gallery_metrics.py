"""Focused contracts for governed metrics across Gallery, Home, Brief, and Stage."""
from __future__ import annotations

import inspect
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import calliope  # noqa: E402
import server  # noqa: E402


class _Result:
    def __init__(self, rows):
        self.rows = rows if isinstance(rows, list) else ([rows] if rows else [])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def test_metric_identity_is_parameter_aware_order_independent_and_strict_json():
    north_a = server._metric_canonical_key(
        "recognized_revenue", {"region": "North", "window": 30},
    )
    north_b = server._metric_canonical_key(
        "recognized_revenue", {"window": 30, "region": "North"},
    )
    south = server._metric_canonical_key(
        "recognized_revenue", {"region": "South", "window": 30},
    )

    assert north_a == north_b
    assert north_a != south
    assert north_a.startswith("metric:recognized_revenue:")
    assert calliope._metric_lineage_key(
        "recognized_revenue", {"region": "North", "window": 30},
    ) == calliope._metric_lineage_key(
        "recognized_revenue", {"window": 30, "region": "North"},
    )
    with pytest.raises(ValueError, match="JSON object"):
        server._metric_params(["North"])
    with pytest.raises(ValueError, match="JSON values"):
        server._metric_params({"value": math.nan})


def test_metric_display_and_trend_use_governed_direction_not_arrow_color():
    definition = {
        "name": "support_backlog",
        "labels": {
            "display": {
                "title": "Open support backlog",
                "unit": "tickets",
                "decimals": 0,
                "preferred_direction": "lower",
            }
        },
    }
    display = server._metric_display(definition)
    falling = server._metric_trend([
        {"numeric_value": 120, "observation_id": 1},
        {"numeric_value": 90, "observation_id": 2},
    ], display["preferred_direction"])
    rising = server._metric_trend([
        {"numeric_value": 90, "observation_id": 2},
        {"numeric_value": 120, "observation_id": 3},
    ], display["preferred_direction"])

    assert server._metric_title(definition) == "Open support backlog"
    assert display["preferred_direction"] == "lower"
    assert display["decimals"] == 0
    assert falling["direction"] == "down" and falling["meaning"] == "good"
    assert rising["direction"] == "up" and rising["meaning"] == "bad"


def test_metric_home_handle_tracks_latest_observation_without_replaying_sql(monkeypatch):
    monkeypatch.setattr(server, "_metric_detail_snapshot", lambda *args, **kwargs: {
        "name": "recognized_revenue",
        "title": "Recognized revenue",
        "description": "Booked revenue recognized in the selected region.",
        "version": 7,
        "grain": "company",
        "category": "Finance",
        "subcategory": "Revenue",
        "parameter_defaults": {"region": "All"},
        "params": {"region": "North"},
        "display": {"currency": "USD", "preferred_direction": "higher"},
        "canonical_key": server._metric_canonical_key(
            "recognized_revenue", {"region": "North"},
        ),
        "snapshot": {
            "observation_id": 42, "numeric_value": 125000, "value": 125000,
            "data_as_of": "2026-08-02T12:00:00+00:00",
        },
        "series": [{"observation_id": 41, "numeric_value": 120000}],
        "trend": {"direction": "up", "meaning": "good", "percent": 4.16},
        "source_tables": ["finance.ledger"],
    })

    item = server._semantic_home_resolve_handle({
        "kind": "metric",
        "name": "recognized_revenue",
        "params": {"region": "North"},
    })

    assert item["kind"] == "metric"
    assert item["source"]["tracking"] == "latest"
    assert item["source"]["pinned_version"] == 7
    assert item["snapshot"]["observation_id"] == 42
    assert item["replayable"] is False
    assert item["trail"][1]["handle"] == {
        "kind": "db_table", "table": "finance.ledger",
    }
    query = parse_qs(urlparse(item["open_url"]).query)
    assert query["view"] == ["metrics"]
    assert query["metric"] == ["recognized_revenue"]
    assert '"region":"North"' in query["params"][0]


def test_quiet_metric_follow_projects_durable_observation_into_personal_brief():
    now = datetime(2026, 8, 2, 15, tzinfo=timezone.utc)
    canonical = server._metric_canonical_key("active_accounts", {"segment": "SMB"})
    row = {
        "id": "2bf4e342-fb2d-4b07-9a0c-20533ccf442c",
        "metric_name": "active_accounts",
        "params": {"segment": "SMB"},
        "canonical_key": canonical,
        "definition_version": 3,
        "description": "Accounts with activity in the last 30 days.",
        "grain": "company",
        "category": "Growth",
        "subcategory": "Accounts",
        "labels": {"display": {"title": "Active accounts"}},
        "observation_id": 12,
        "metric_version": 3,
        "value": {"value": 220},
        "numeric_value": 220,
        "previous_observation_id": 11,
        "previous_value": 200,
        "status": "passing",
        "verdict": {"ok": True},
        "data_as_of": now,
        "observed_at": now,
    }
    queries = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            queries.append((query, params))
            return _Result([row])

    items, coverage, warnings = calliope._brief_followed_metric_observations(
        Connection, "analyst@example.com", now - timedelta(days=1), now + timedelta(days=1),
    )

    assert warnings == []
    assert len(items) == 1
    assert items[0]["kind"] == "metric"
    assert items[0]["title"] == "Active accounts"
    assert items[0]["source"] == "Metrics in Briefs"
    assert "10.0% up" in items[0]["summary"]
    assert items[0]["provenance"]["brief_section"] == "data_moved"
    assert items[0]["provenance"]["viewer_relation"]["kind"] == "metric_follow"
    assert items[0]["handle"]["params"] == {"segment": "SMB"}
    assert coverage[0]["count"] == 1
    assert coverage[0]["label"] == "Metrics in Briefs"
    assert "metric_observations" in queries[0][0]
    assert "NOT EXISTS" in queries[0][0]  # A Home pin owns the Brief projection once pinned.

    row["data_as_of"] = now + timedelta(days=3)
    future_items, _, _ = calliope._brief_followed_metric_observations(
        Connection, "analyst@example.com", now - timedelta(days=1), now + timedelta(days=1),
    )
    assert future_items == []


def test_gallery_metric_schema_routes_and_renderers_ship_as_one_contract():
    migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0236_calliope_metrics.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    landing = (HERE / "server.py").read_text(encoding="utf-8")
    stage_script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    stage_css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")

    assert "'artifact', 'artifact_object', 'metric'" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_metric_follows" in migration
    assert "UNIQUE (owner_email, canonical_key)" in migration
    assert "0236_calliope_metrics" in registry
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_metric_follows" in calliope._METRIC_FOLLOW_DDL
    assert "conn.execute(_METRIC_FOLLOW_DDL)" in inspect.getsource(calliope.ensure_tables)
    assert 'data-gallery-view="metrics"' in landing
    assert "initial.searchParams.get('view')==='metrics'" in landing
    assert 'id="metric-lens"' in landing
    assert "Current instance" in landing
    assert "Definition default" in landing
    assert "function metricViewActive()" in landing
    assert "!metricViewActive()&&queryText().length>=2" in landing
    assert "Brief me includes meaningful changes in future Briefs" in landing
    assert "✓ In Briefs" in landing
    assert '>In Briefs</button>' in landing
    assert "/api/gallery/metrics/{name}" in landing
    assert "/api/calliope/metrics/{name}/follow" in landing
    assert "/api/calliope/gallery/metrics/{name}/ask" in backend
    assert '"frozen": True' in backend
    assert "Do not silently replace the frozen context" in backend
    assert "function renderMetric(surface)" in stage_script
    assert "payload.frozen" in stage_script
    assert ".metric-frozen" in stage_css
    assert ".metric-delta.good" in stage_css
    assert ".metric-delta.bad" in stage_css


def test_metric_browser_remains_available_without_artifacts_or_calliope(monkeypatch):
    monkeypatch.setattr(calliope, "is_enabled", lambda: False)

    page = server._landing_html([], "reader@example.com")

    assert 'data-gallery-view="metrics"' in page
    assert 'id="metrics-browser"' in page
    assert 'id="artifact-browser"' in page
    assert 'data-calliope="false"' in page
