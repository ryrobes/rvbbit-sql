"""Focused contracts for the permission-aware, progressive evidence trail."""
from __future__ import annotations

import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import calliope  # noqa: E402
import server  # noqa: E402


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def test_trail_handles_are_small_locators_not_copied_evidence():
    assert server._trail_handle({
        "kind": "dashboard-object",
        "slug": "regional-brief",
        "version": "4",
        "object_id": "regional_revenue",
        "definition_hash": "abc123",
        "context": {"region": "North"},
        "sql": "select secret_payload from somewhere",
        "rendered_value": 42_000,
    }) == {
        "kind": "artifact_object",
        "slug": "regional-brief",
        "version": 4,
        "object_id": "regional_revenue",
        "definition_hash": "abc123",
        "context": {"region": "North"},
    }
    assert server._trail_handle({
        "kind": "table", "table": "sales.orders", "doc": "large copied catalog prose",
    }) == {
        "kind": "db_table", "schema": "sales", "relation": "orders",
    }


def test_document_trail_uses_acl_brain_functions_and_explains_shared_context(monkeypatch):
    calls = []

    def get_doc(doc_id, owner):
        calls.append(("get", doc_id, owner))
        return {
            "doc_id": doc_id,
            "title": "Q3 launch review",
            "source": "Fireflies",
            "mime": "text/markdown",
            "occurred_at": "2026-07-31",
        }

    def related(doc_id, owner):
        calls.append(("related", doc_id, owner))
        return {
            "visible": True,
            "entities": [{"kind": "project", "label": "Atlas"}],
            "related": [{
                "doc_id": 12,
                "title": "Atlas delivery plan",
                "score": 9.5,
                "shared_entities": ["Atlas", "Q3"],
            }],
            "relations": [{
                "subject": "Q3 launch review #11",
                "predicate": "has_status",
                "object": "at risk",
            }],
        }

    monkeypatch.setattr(server, "tool_brain_get_doc", get_doc)
    monkeypatch.setattr(server, "tool_brain_related", related)
    result = server._calliope_follow_trail(
        {"kind": "document", "doc_id": 11}, "analyst@example.com", 12,
    )

    assert calls == [
        ("get", 11, "analyst@example.com"),
        ("related", 11, "analyst@example.com"),
    ]
    assert result["subject"]["label"] == "Q3 launch review"
    assert {item["relationship"] for item in result["connections"]} == {
        "mentions", "also discusses",
    }
    related_doc = next(
        item for item in result["connections"] if item["relationship"] == "also discusses"
    )
    assert related_doc["handle"] == {"kind": "document", "doc_id": 12}
    assert related_doc["shared"] == ["Atlas", "Q3"]
    assert {fact["label"]: fact["value"] for fact in result["facts"]}["Has Status"] == "at risk"


def test_catalog_trail_turns_edges_into_ranked_plain_language_hops(monkeypatch):
    table = {
        "node_id": 7,
        "kind": "db_table",
        "label": "sales.orders",
        "properties": {
            "schema": "sales",
            "table": "orders",
            "n_rows": "150000",
            "n_columns": "2",
            "comment": "One row per booked order.",
        },
        "confidence": 1,
    }
    neighbors = [{
        "direction": "out",
        "predicate": "has_column",
        "node_id": 8,
        "kind": "db_column",
        "label": "sales.orders.net_value",
        "properties": {
            "schema": "sales",
            "table": "orders",
            "column": "net_value",
            "comment": "Booked value after discounts.",
        },
        "confidence": .99,
    }]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, _params=None):
            return _Result(neighbors if "SELECT direction" in statement else [table])

    monkeypatch.setattr(server, "_conn", lambda: Connection())
    monkeypatch.setattr(server, "_trail_artifact_neighbors", lambda *_args: [])
    monkeypatch.setattr(server, "tool_brain_entity", lambda *_args: {"found": False})
    result = server._calliope_follow_trail(
        {"kind": "db_table", "node_id": 7}, "analyst@example.com", 12,
    )

    assert result["subject"]["detail"] == "One row per booked order."
    assert result["facts"][:2] == [
        {"label": "Rows", "value": "150000"},
        {"label": "Fields", "value": "2"},
    ]
    assert result["connections"][0]["relationship"] == "has field"
    assert result["connections"][0]["handle"] == {
        "kind": "db_column",
        "node_id": 8,
        "schema": "sales",
        "relation": "orders",
        "column": "net_value",
    }


def test_metric_trail_uses_governed_source_without_requiring_a_kg_node(monkeypatch):
    monkeypatch.setattr(server, "_metric_detail_snapshot", lambda *args, **kwargs: {
        "name": "net_revenue",
        "title": "Net Revenue",
        "description": "Recognized revenue after credits.",
        "version": 3,
        "grain": "day",
        "params": {"region": "North"},
        "snapshot": {
            "value": 42500,
            "status": "healthy",
            "data_as_of": "2026-08-01T00:00:00Z",
        },
        "trend": {"direction": "up", "percent": 6.25},
        "dependencies": [{
            "table_name": "sales.orders", "age": "01:00:00", "stale": False,
        }],
        "source_tables": ["sales.orders"],
        "artifacts": [{
            "slug": "revenue-room", "name": "Revenue Room",
            "description": "Finance operating dashboard", "app_kind": "dashboard",
            "latest_version": 4,
        }],
    })
    monkeypatch.setattr(server, "_calliope_brain_evidence", lambda *_args: [])
    monkeypatch.setattr(
        server, "_trail_catalog_node",
        lambda *_args: (_ for _ in ()).throw(AssertionError("metric must not require kg_nodes")),
    )

    result = server._calliope_follow_trail({
        "kind": "metric", "name": "net_revenue", "params": {"region": "North"},
    }, "analyst@example.com", 12)

    assert result["subject"]["label"] == "Net Revenue"
    assert result["subject"]["handle"] == {
        "kind": "metric", "relation": "net_revenue", "name": "net_revenue",
        "params": {"region": "North"},
    }
    assert {item["relationship"] for item in result["connections"]} == {
        "derived from", "used by",
    }
    assert result["connections"][0]["handle"] == {
        "kind": "db_table", "schema": "sales", "relation": "orders",
    }
    assert result["connections"][1]["handle"] == {
        "kind": "artifact", "slug": "revenue-room", "version": 4,
    }
    assert "governed metric" in result["searched"]


def test_search_normalizer_preserves_rehydratable_trail_handle():
    result = calliope._normalize_evidence_search_result({
        "items": [{
            "id": "brain:11:0",
            "group": "knowledge",
            "kind": "document",
            "title": "Q3 launch review",
            "handle": {"kind": "document", "doc_id": "11", "body": "must not be present"},
        }],
    }, "launch")

    assert result["items"][0]["handle"] == {"kind": "document", "doc_id": "11"}


def test_trail_affordance_is_shared_by_home_search_documents_and_lens():
    landing = (HERE / "server.py").read_text(encoding="utf-8")
    calliope_js = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    lens_js = (HERE / "theme" / "artifact-lens.js").read_text(encoding="utf-8")

    assert '@m.custom_route("/api/calliope/trails", methods=["POST"])' in landing
    assert 'id="trail-dialog"' in landing
    assert "data-home-trail" in landing
    assert "data-follow-evidence" in calliope_js
    assert "data-viewer-document-trail" in calliope_js
    assert "openTrailViewer(connection.handle)" in calliope_js
    assert '<button type="button" class="trail-follow">Follow trail</button>' in lens_js
    assert "data-lens-trail-hop" in lens_js
