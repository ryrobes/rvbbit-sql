"""Focused contracts for the private, handle-based Semantic Home."""
from __future__ import annotations

import json
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


def _artifact_fixture(slug, version=None, *, viewer):
    assert slug == "regional-brief"
    assert viewer == "analyst@example.com"
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
    }, viewer="analyst@example.com")
    semantic_object = _fixture_manifest()["semantic_map"]["objects"][0]
    north = server._semantic_home_resolve_handle({
        "kind": "artifact_object",
        "slug": "regional-brief",
        "version": 4,
        "object_id": "regional_revenue",
        "definition_hash": semantic_object["definition_hash"],
        "context": {"region": "North"},
        "rendered_value": "$42,000",
    }, validate_sql=True, viewer="analyst@example.com")
    south = server._semantic_home_resolve_handle({
        **north["source"],
        "context": {"region": "South"},
    }, viewer="analyst@example.com")

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
    preview = server._semantic_home_preview(
        source, "analyst_execution_role", viewer="analyst@example.com"
    )

    assert "region='North'" in observed["sql"]
    assert observed["limit"] == 2
    assert observed["subject"] == "analyst_execution_role"
    assert preview["value"] == 42000
    assert preview["display"] == {"prefix": "$", "decimals": 0}


def test_pinned_rendered_value_survives_as_a_non_authoritative_replay_fallback(monkeypatch):
    monkeypatch.setattr(
        server,
        "_semantic_home_resolve_handle",
        lambda _source, *, viewer: {
            "kind": "artifact_object",
            "presentation": {"title": "Regional revenue", "last_rendered_value": None},
            "title": "Regional revenue",
            "trail": [],
            "status": "ready",
        } if viewer == "analyst@example.com" else None,
    )
    item = server._semantic_home_public_item({
        "id": "018f3d10-6e84-7d51-b8bd-07c75a67c2a1",
        "item_kind": "artifact_object",
        "source": {"kind": "artifact_object"},
        "presentation": {"last_rendered_value": "$42,000"},
        "sort_order": 1000,
    }, viewer="analyst@example.com")

    assert item["presentation"]["last_rendered_value"] == "$42,000"


def test_version_document_reads_the_exact_immutable_html(monkeypatch):
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            calls.append((query, params))
            if "FROM rvbbit.dashboards" in query:
                return type("Result", (), {"fetchone": lambda _self: {
                    "id": 42,
                    "slug": "regional-brief",
                    "latest_version": 6,
                }})()
            return type("Result", (), {"fetchone": lambda _self: {
                "html": "<main>version four</main>",
                "manifest": {"semantic_map": {"objects": []}},
            }})()

    monkeypatch.setattr(server, "_conn", lambda: Connection())
    monkeypatch.setattr(
        server,
        "_dash_shim",
        lambda slug, version, manifest: f"<!-- {slug}:v{version}:{bool(manifest)} -->",
    )

    document = server._dashboard_version_document("regional-brief", 4)

    assert document["version"] == 4
    assert document["latest_version"] == 6
    assert document["html"] == (
        "<!-- regional-brief:v4:True --><main>version four</main>"
    )
    assert calls[1][1] == (42, 4)


def test_versioned_routes_cover_dashboards_and_full_page_apps():
    source = (_HERE / "server.py").read_text(encoding="utf-8")

    assert '@m.custom_route("/d/{slug}/versions/{version}", methods=["GET"])' in source
    app_version = source.index(
        '@m.custom_route("/apps/{slug}/versions/{version}", methods=["GET"])'
    )
    app_catchall = source.index(
        '@m.custom_route("/apps/{slug}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])'
    )
    assert app_version < app_catchall


def test_metric_promotion_draft_freezes_context_and_keeps_sql_server_owned(monkeypatch):
    monkeypatch.setattr(server, "_semantic_home_artifact_row", _artifact_fixture)
    monkeypatch.setattr(server, "_semantic_home_source_trail", lambda _tables: [])
    monkeypatch.setattr(
        server,
        "tool_validate_sql",
        lambda *_args, **_kwargs: {"valid": True, "safe_select": True},
    )
    monkeypatch.setattr(
        server,
        "_semantic_home_preview",
        lambda *_args, **_kwargs: {
            "status": "recreated",
            "value": 42000,
            "value_column": "value",
            "row_count": 1,
        },
    )
    semantic_object = _fixture_manifest()["semantic_map"]["objects"][0]
    source = {
        "kind": "artifact_object",
        "slug": "regional-brief",
        "version": 4,
        "object_id": "regional_revenue",
        "definition_hash": semantic_object["definition_hash"],
        "context": {"region": "North"},
    }

    draft = server._semantic_home_metric_draft(
        source, "analyst-role", viewer="analyst@example.com"
    )
    public = server._semantic_home_metric_draft_public(draft)

    assert "region='North'" in draft["definition_sql"]
    assert draft["title"] == "Regional revenue"
    assert draft["display"]["title"] == "Regional revenue"
    assert draft["display"]["unit"] == "USD"
    assert draft["current"] == 42000
    assert draft["suggested_name"].startswith("regional_brief_regional_revenue_")
    assert "definition_sql" not in public
    assert public["artifact"]["version"] == 4


def test_metric_promotion_defines_materializes_and_replaces_the_home_pin(monkeypatch):
    item_id = "018f3d10-6e84-7d51-b8bd-07c75a67c2a1"
    board_id = "018f3d10-6e84-7d51-b8bd-07c75a67c2a2"
    calls = []
    draft = {
        "suggested_name": "regional_brief_regional_revenue",
        "title": "Regional revenue",
        "description": "Recognized revenue for one dashboard region.",
        "grain": "One scalar value from Regional Brief version 4.",
        "display": {"title": "Regional revenue", "prefix": "$", "decimals": 0},
        "formula": "Sum recognized revenue for the selected region.",
        "definition_sql": "select sum(revenue) as value from sales.orders where region='North'",
        "value_column": "value",
        "parameters": {"region": {"type": "text", "default": "North"}},
        "source_canonical_key": "artifact-object:regional-brief:v4:regional_revenue:abc",
        "artifact": {
            "slug": "regional-brief",
            "name": "Regional Brief",
            "version": 4,
            "latest_version": 6,
            "object_id": "regional_revenue",
            "definition_hash": "deadbeef",
            "context": {"region": "North"},
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
            calls.append((query, params))
            if "SELECT i.*,b.id AS owned_board_id" in query:
                return Result({
                    "id": item_id,
                    "board_id": board_id,
                    "owned_board_id": board_id,
                    "item_kind": "artifact_object",
                    "source": {"kind": "artifact_object"},
                })
            if "FROM rvbbit.metric_catalog WHERE name" in query:
                return Result(None)
            if "rvbbit.define_metric" in query:
                return Result({"version": 1})
            if "rvbbit.materialize_metric" in query:
                return Result({"id": 99})
            if "canonical_key=%s AND id<>" in query:
                return Result(None)
            if "UPDATE rvbbit.calliope_board_items SET item_kind='metric',canonical_key" in query:
                return Result({
                    "id": item_id,
                    "board_id": board_id,
                    "item_kind": "metric",
                    "source": json.loads(params[1]),
                    "presentation": json.loads(params[2]),
                    "sort_order": 1000,
                })
            return Result(None)

    monkeypatch.setattr(server, "_conn", lambda: Connection())
    monkeypatch.setattr(server, "_semantic_home_metric_draft", lambda *_args, **_kwargs: draft)
    monkeypatch.setattr(
        server,
        "_semantic_home_public_item",
        lambda row, *, viewer: dict(row) if viewer == "analyst@example.com" else None,
    )

    result = server._promote_semantic_home_metric(
        "analyst@example.com", "analyst-role", item_id, {}
    )

    assert result["created"] is True
    assert result["metric"] == "regional_brief_regional_revenue"
    assert result["observation_id"] == 99
    define = next(params for query, params in calls if "rvbbit.define_metric" in query)
    assert define[1] == draft["definition_sql"]
    assert define[5] == "analyst@example.com"
    labels = json.loads(define[6])
    assert labels["metric_value_column"] == "value"
    assert labels["promoted_from"]["version"] == 4
    assert labels["promoted_from"]["context"] == {"region": "North"}
    assert result["item"]["source"]["kind"] == "metric"
    assert result["item"]["source"]["pinned_version"] == 1


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
    assert 'class="home-thumb pending" data-home-thumbnail' in landing
    assert 'class="home-thumb-fallback"' in landing
    assert "function hydrateHomeThumbnails" in landing
    assert "hydrateHomeThumbnails(homeGrid)" in landing
    assert ".home-thumb.ready img" in landing
    assert "data-home-promote" in landing
    assert 'id="metric-promote-dialog"' in landing
    assert "/metric-promotion" in landing
    assert "rvbbit.define_metric" in landing
    assert "rvbbit.materialize_metric" in landing
    assert '<button type="button" class="home-pin">Pin to Home</button>' in lens
    assert "definition_hash: semanticObject.definition_hash" in lens
    assert "context: semanticObject.context || {}" in lens
