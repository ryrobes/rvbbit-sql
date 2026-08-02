"""Focused contracts for dashboard-native semantic watches."""
from __future__ import annotations

import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import calliope  # noqa: E402
import server  # noqa: E402


def _semantic_object():
    return server._normalize_semantic_manifest({
        "semantic_map": {
            "objects": [{
                "id": "regional_revenue",
                "kind": "scalar",
                "meaning": {
                    "label": "Regional revenue",
                    "description": "Recognized revenue for the selected region.",
                    "formula": "Sum recognized revenue in the current dashboard context.",
                    "unit": "USD",
                },
                "parameters": {"region": {"type": "text", "default": "North"}},
                "bindings": [{"selector": "#regional-revenue"}],
                "evaluator": {
                    "sql": "select sum(revenue) as value from sales.orders where region={{region}}",
                    "shape": "scalar",
                    "value_column": "value",
                },
                "display": {"prefix": "$", "decimals": 0},
            }],
        },
    })["semantic_map"]["objects"][0]


def test_watch_resolves_an_exact_scalar_and_replays_as_saved_subject(monkeypatch):
    semantic_object = _semantic_object()
    source = {
        "kind": "artifact_object",
        "slug": "regional-brief",
        "version": 4,
        "object_id": semantic_object["id"],
        "definition_hash": semantic_object["definition_hash"],
        "context": {"region": "North"},
    }
    observed = {}
    monkeypatch.setattr(server, "_semantic_home_resolve_handle", lambda *_args, **_kwargs: {
        "kind": "artifact_object",
        "source": source,
        "title": "Regional revenue",
        "description": "Recognized revenue for the selected region.",
        "formula": "Sum recognized revenue in the current dashboard context.",
        "unit": "USD",
        "display": {"prefix": "$", "decimals": 0},
        "open_url": "/d/regional-brief/versions/4",
        "thumbnail_url": "/thumbs/dashboard/regional-brief.png",
    })
    monkeypatch.setattr(server, "_semantic_home_artifact_row", lambda *_args: (
        {"name": "Regional Brief"}, {}, {"semantic_map": {"objects": [semantic_object]}}, [],
    ))

    def preview(_source, subject):
        observed["subject"] = subject
        return {"status": "recreated", "value": "42,000"}

    monkeypatch.setattr(server, "_semantic_home_preview", preview)
    resolved, presentation, current = server._watch_semantic_definition(
        source, "analyst@example.com", preview=True,
    )

    assert resolved == source
    assert presentation["title"] == "Regional revenue"
    assert current == Decimal("42000")
    assert observed["subject"] == "analyst@example.com"


def test_alert_rule_only_reads_the_private_observation_row():
    watch_id = uuid.uuid4()
    condition, policy = server._watch_alert_definition(
        watch_id, "below", Decimal("125.5"), 3,
    )

    assert condition["kind"] == "sql"
    assert condition["compare"] == "lte"
    assert condition["threshold"] == "125.5"
    assert "rvbbit.calliope_watches" in condition["query"]
    assert str(watch_id) in condition["query"]
    assert "sales.orders" not in condition["query"]
    assert policy == {"consecutive_n": 3, "cooldown_secs": 0}
    assert server._watch_rule_tier(watch_id) == f"calliope:{watch_id.hex}"


def test_watch_inputs_are_bounded_and_boolean_updates_are_unambiguous():
    assert server._watch_inputs({
        "comparator": "above",
        "threshold": "10.25",
        "cadence": "fast",
        "consecutive_n": "2",
    }) == ("above", Decimal("10.25"), "fast", 2)
    assert server._watch_bool("false", default=True) is False
    assert server._watch_bool("true", default=False) is True
    with pytest.raises(ValueError, match="numeric"):
        server._watch_inputs({"threshold": "not a number"})
    with pytest.raises(ValueError, match="between 1 and 12"):
        server._watch_inputs({"threshold": 1, "consecutive_n": 13})
    with pytest.raises(ValueError, match="true or false"):
        server._watch_bool("maybe", default=False)


def test_watch_schema_is_migrated_self_healing_and_subject_aware():
    migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0227_calliope_semantic_watches.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_watches" in migration
    assert "execution_subject text NOT NULL" in migration
    assert "owner_email text NOT NULL" in migration
    assert "calliope_watch_events" in migration
    assert "0227_calliope_semantic_watches" in registry
    assert "execution_subject text NOT NULL" in calliope._WATCH_DDL


def test_watch_routes_and_lens_affordance_stay_on_exact_semantic_objects():
    backend = (HERE / "server.py").read_text(encoding="utf-8")
    script = (HERE / "theme" / "artifact-lens.js").read_text(encoding="utf-8")
    css = (HERE / "theme" / "artifact-lens.css").read_text(encoding="utf-8")

    assert '@m.custom_route("/api/calliope/watches", methods=["GET", "POST"])' in backend
    assert '"/api/calliope/watches/{watch_id}/check"' in backend
    assert '"/api/calliope/watch-events"' in backend
    assert "session.get(\"sub\") or owner" in backend
    assert "WHERE id=%s::uuid AND owner_email=%s" in backend
    assert "Watch this value" in script
    assert "watchHandle()?.kind" not in script
    assert 'handle?.kind === "artifact_object"' in script
    assert "/api/calliope/watches" in script
    assert "Every 15 minutes" in script
    assert ".watch-panel" in css
    assert ".watch-card.fail" in css
