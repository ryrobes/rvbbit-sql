"""Focused contracts for the private, handle-based Semantic Home."""
from __future__ import annotations

import sys
from pathlib import Path


_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import calliope  # noqa: E402
import server  # noqa: E402


def _fixture_manifest():
    return server._normalize_semantic_manifest({
        "semantic_map": {
            "objects": [{
                "id": "regional_revenue",
                "kind": "scalar",
                "meaning": {
                    "label": "Regional revenue",
                    "description": "Recognized revenue for one dashboard region.",
                    "formula": "Sum recognized revenue for the selected region.",
                    "unit": "USD",
                },
                "parameters": {
                    "region": {"type": "text", "default": "North"},
                },
                "bindings": [{"selector": "#regional-revenue"}],
                "evaluator": {
                    "sql": "select sum(revenue) as value from sales.orders where region={{region}}",
                    "shape": "scalar",
                    "value_column": "value",
                },
                "display": {"prefix": "$", "decimals": 0},
            }],
        },
    })


def _artifact_fixture(slug, version=None):
    assert slug == "regional-brief"
    selected = int(version or 6)
    return (
        {
            "id": 42,
            "slug": slug,
            "name": "Regional Brief",
            "description": "A live regional operating brief.",
            "runtime_kind": "html",
            "app_kind": "dashboard",
            "latest_version": 6,
        },
        {"version": selected, "manifest": _fixture_manifest()},
        _fixture_manifest(),
        [{"kind": "table", "object_ref": "sales.orders"}],
    )


def test_artifact_pins_follow_latest_while_object_pins_keep_exact_meaning(monkeypatch):
    monkeypatch.setattr(server, "_semantic_home_artifact_row", _artifact_fixture)
    monkeypatch.setattr(
        server,
        "_semantic_home_source_trail",
        lambda tables: [{
            "kind": "table",
            "relationship": "recreated from",
            "label": table,
            "detail": "Governed orders",
            "handle": {"kind": "db_table", "table": table},
        } for table in tables],
    )
    monkeypatch.setattr(
        server,
        "tool_validate_sql",
        lambda *_args, **_kwargs: {"valid": True, "safe_select": True},
    )

    artifact = server._semantic_home_resolve_handle({
        "kind": "artifact",
        "slug": "regional-brief",
    })
    semantic_object = _fixture_manifest()["semantic_map"]["objects"][0]
    north = server._semantic_home_resolve_handle({
        "kind": "artifact_object",
        "slug": "regional-brief",
        "version": 4,
        "object_id": "regional_revenue",
        "definition_hash": semantic_object["definition_hash"],
        "context": {"region": "North"},
        "rendered_value": "$42,000",
    }, validate_sql=True)
    south = server._semantic_home_resolve_handle({
        **north["source"],
        "context": {"region": "South"},
    })

    assert artifact["canonical_key"] == "artifact:regional-brief"
    assert artifact["source"]["tracking"] == "latest"
    assert artifact["version"] == 6
    assert north["source"]["version"] == 4
    assert north["newer_version_available"] is True
    assert north["open_url"] == "/d/regional-brief/versions/4"
    assert north["canonical_key"] != south["canonical_key"]
    assert [crumb["kind"] for crumb in north["trail"]] == [
        "artifact_object", "artifact", "table",
    ]
    assert north["presentation"]["last_rendered_value"] == "$42,000"


def test_object_preview_replays_under_the_authenticated_execution_subject(monkeypatch):
    monkeypatch.setattr(server, "_semantic_home_artifact_row", _artifact_fixture)
    monkeypatch.setattr(server, "_semantic_home_source_trail", lambda _tables: [])
    monkeypatch.setattr(
        server,
        "tool_validate_sql",
        lambda *_args, **_kwargs: {"valid": True, "safe_select": True},
    )
    observed = {}

    def run(sql, as_of=None, limit=None):
        observed.update({
            "sql": sql,
            "as_of": as_of,
            "limit": limit,
            "subject": server._SESSION_SUB.get(),
        })
        return {
            "columns": [{"name": "value", "type": "numeric"}],
            "rows": [{"value": 42000}],
            "row_count": 1,
            "engine": "rvbbit_native",
            "elapsed_ms": 4,
        }

    monkeypatch.setattr(server, "tool_run_sql", run)
    semantic_object = _fixture_manifest()["semantic_map"]["objects"][0]
    source = {
        "kind": "artifact_object",
        "slug": "regional-brief",
        "version": 4,
        "object_id": "regional_revenue",
        "definition_hash": semantic_object["definition_hash"],
        "context": {"region": "North"},
    }
    preview = server._semantic_home_preview(source, "analyst_execution_role")

    assert "region='North'" in observed["sql"]
    assert observed["limit"] == 2
    assert observed["subject"] == "analyst_execution_role"
    assert preview["value"] == 42000
    assert preview["display"] == {"prefix": "$", "decimals": 0}


def test_pinned_rendered_value_survives_as_a_non_authoritative_replay_fallback(monkeypatch):
    monkeypatch.setattr(
        server,
        "_semantic_home_resolve_handle",
        lambda _source: {
            "kind": "artifact_object",
            "presentation": {"title": "Regional revenue", "last_rendered_value": None},
            "title": "Regional revenue",
            "trail": [],
            "status": "ready",
        },
    )
    item = server._semantic_home_public_item({
        "id": "018f3d10-6e84-7d51-b8bd-07c75a67c2a1",
        "item_kind": "artifact_object",
        "source": {"kind": "artifact_object"},
        "presentation": {"last_rendered_value": "$42,000"},
        "sort_order": 1000,
    })

    assert item["presentation"]["last_rendered_value"] == "$42,000"


def test_home_schema_and_surfaces_preserve_private_composition_contract():
    migration = (
        _HERE.parent.parent / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0226_calliope_semantic_home.sql"
    ).read_text(encoding="utf-8")
    landing = (_HERE / "server.py").read_text(encoding="utf-8")
    lens = (_HERE / "theme" / "artifact-lens.js").read_text(encoding="utf-8")

    assert "owner_email text NOT NULL" in migration
    assert "UNIQUE (owner_email, slug)" in migration
    assert "UNIQUE (board_id, canonical_key)" in migration
    assert "artifact_object" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_boards" in calliope._HOME_DDL
    assert 'id="semantic-home"' in landing
    assert "data-home-pin" in landing
    assert "/api/calliope/home/items" in landing
    assert "function galleryTooltipSourceMarkup" in landing
    assert "function homeValueTooltip" in landing
    assert "data-gallery-tooltip" in landing
    assert "data-home-value-text" in landing
    assert "setGalleryTooltipSource(node,homeValueTooltip" in landing
    assert ".gallery-tooltip" in landing
    assert '<button type="button" class="home-pin">Pin to Home</button>' in lens
    assert "definition_hash: semanticObject.definition_hash" in lens
    assert "context: semanticObject.context || {}" in lens
