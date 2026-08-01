"""Focused tests for Calliope's federated evidence resolver."""
from __future__ import annotations

import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import server  # noqa: E402


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _statement, _params=None):
        return _Result(self.rows)


def test_artifact_ranker_searches_semantic_objects_and_keeps_parent_artifact(monkeypatch):
    rows = [{
        "id": 7,
        "slug": "pipeline-health",
        "name": "Pipeline Health",
        "description": "Sales pipeline health and conversion risk.",
        "owner_email": "pilot@example.com",
        "team": "Sales",
        "status": "live",
        "latest_version": 3,
        "updated_at": "2026-07-31T12:00:00Z",
        "runtime_kind": "html",
        "app_kind": "dashboard",
        "manifest": {
            "semantic_map": {
                "description": "Pipeline monitoring",
                "objects": [{
                    "id": "weighted_pipeline",
                    "kind": "scalar",
                    "meaning": {
                        "label": "Weighted pipeline",
                        "description": "Open pipeline weighted by stage probability.",
                        "formula": "sum(amount * probability)",
                    },
                    "evaluator": {"sql": "select 1 as value"},
                }],
            }
        },
        "semantic_status": None,
        "semantic_map": {},
        "verification": {},
        "prompt_version": None,
        "model": None,
        "semantic_updated_at": None,
        "lineage": [{"kind": "table", "ref": "sales.opportunities"}],
    }]
    monkeypatch.setattr(server, "_conn", lambda: _Connection(rows))
    items = server._calliope_artifact_evidence("weighted pipeline", 8)

    assert items[0]["kind"] == "dashboard-object"
    assert items[0]["title"].startswith("Weighted pipeline")
    assert items[0]["provenance"]["replayable"] is True
    assert items[0]["thumbnail_url"] == "/thumbs/dashboard/pipeline-health.png"
    assert any(item["kind"] == "artifact" and item["url"] == "/d/pipeline-health" for item in items)
    assert all(item["thumbnail_url"] == "/thumbs/dashboard/pipeline-health.png" for item in items)


def test_business_search_suppresses_system_learning_noise(monkeypatch):
    rows = [
        {
            "doc_id": 1,
            "chunk_idx": 0,
            "title": "Route shape native_cap=0",
            "folder": "/system",
            "source": "RVBBIT System Learning",
            "doc_type": "system_learning",
            "occurred_at": None,
            "chunk": "routing details",
            "score": 0.8,
            "entities": [],
        },
        {
            "doc_id": 2,
            "chunk_idx": 0,
            "title": "Pipeline review",
            "folder": "/meetings",
            "source": "Fireflies",
            "doc_type": "meeting",
            "occurred_at": "2026-07-30",
            "chunk": "The sales team reviewed pipeline coverage.",
            "score": 0.6,
            "entities": ["Pipeline"],
        },
    ]
    monkeypatch.setattr(server, "_conn", lambda: _Connection(rows))

    business = server._calliope_brain_evidence("sales pipeline", "pilot@example.com", 8)
    assert [item["title"] for item in business] == ["Pipeline review"]

    engine = server._calliope_brain_evidence("rvbbit routing", "pilot@example.com", 8)
    assert {item["title"] for item in engine} == {
        "Route shape native_cap=0",
        "Pipeline review",
    }


def test_data_search_projects_catalog_enrichment_as_compact_typed_fields(monkeypatch):
    search_rows = [
        {
            "node_id": 1,
            "kind": "db_table",
            "schema_name": "sales",
            "rel_name": "orders",
            "col_name": None,
            "score": 0.7,
            "boosted_score": 0.8,
            "usage_touches": 9,
            "doc": "Table sales.orders — 150000 rows. Columns: order_id, net_value.",
            "properties": {
                "n_rows": "150000",
                "n_columns": "2",
                "relkind": "r",
                "comment": "One row per booked order.",
            },
        },
        {
            "node_id": 2,
            "kind": "db_column",
            "schema_name": "sales",
            "rel_name": "orders",
            "col_name": "net_value",
            "score": 0.9,
            "boosted_score": 0.9,
            "usage_touches": 4,
            "doc": "Column sales.orders.net_value (numeric).",
            "properties": {
                "data_type": "numeric",
                "ndv": 42,
                "null_frac": 0.125,
                "is_fk": True,
                "comment": "Booked value after discounts, in USD.",
            },
        },
        {
            "node_id": 3,
            "kind": "cube",
            "schema_name": "cubes",
            "rel_name": "sales_health",
            "col_name": None,
            "score": 0.6,
            "boosted_score": 0.6,
            "usage_touches": 0,
            "doc": "Cube cubes.sales_health — Sales health by region.",
            "properties": {
                "description": "A curated view of sales health and coverage.",
                "grain": "one row per region and week",
            },
        },
    ]
    catalog_columns = [
        {"table_schema": "sales", "table_name": "orders", "column_name": "order_id", "data_type": "bigint", "is_nullable": "NO", "ordinal_position": 1},
        {"table_schema": "sales", "table_name": "orders", "column_name": "net_value", "data_type": "numeric", "is_nullable": "YES", "ordinal_position": 2},
        {"table_schema": "cubes", "table_name": "sales_health", "column_name": "region", "data_type": "text", "is_nullable": "YES", "ordinal_position": 1},
        {"table_schema": "cubes", "table_name": "sales_health", "column_name": "coverage", "data_type": "numeric", "is_nullable": "YES", "ordinal_position": 2},
    ]

    class SemanticConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, _params=None):
            if "search_data_weighted" in statement:
                assert "n.properties" in statement
                return _Result(search_rows)
            if "information_schema.columns" in statement:
                return _Result(catalog_columns)
            if "cube_catalog" in statement:
                return _Result([{
                    "name": "sales_health",
                    "version": 2,
                    "grain": "one row per region and week",
                    "description": "A curated view of sales health and coverage.",
                    "category": "Revenue",
                    "last_rows": 150000,
                    "refreshed_at": "2026-08-01T12:00:00Z",
                }])
            if "cube_columns" in statement:
                return _Result([{
                    "cube_name": "sales_health",
                    "column_name": "coverage",
                    "data_type": "numeric",
                    "doc": "Share of quota already covered.",
                    "semantics": "A ratio from zero to one.",
                    "source_ref": "derived: booked / quota",
                }])
            raise AssertionError(statement)

    monkeypatch.setattr(server, "_conn", lambda: SemanticConnection())
    items = server._calliope_data_evidence("sales health", 8)
    by_kind = {item["kind"]: item for item in items}

    table = by_kind["db_table"]
    assert table["identity"] == {"schema": "sales", "relation": "orders"}
    assert table["definition"] == "One row per booked order."
    assert table["facts"] == [
        {"label": "Rows", "value": "150K"},
        {"label": "Fields", "value": "2"},
        {"label": "Kind", "value": "Table"},
    ]
    assert table["field_count"] == 2
    assert table["fields"][0] == {"name": "order_id", "type": "bigint", "nullable": False}

    column = by_kind["db_column"]
    assert column["identity"]["column"] == "net_value"
    assert column["definition"] == "Booked value after discounts, in USD."
    assert "fields" not in column
    assert "field_count" not in column
    assert {fact["label"]: fact["value"] for fact in column["facts"]} == {
        "Type": "numeric",
        "Key": "Foreign key",
        "Distinct": "42",
        "Missing": "12.5%",
    }

    cube = by_kind["cube"]
    assert cube["definition"] == "A curated view of sales health and coverage."
    assert {fact["label"]: fact["value"] for fact in cube["facts"]} == {
        "Grain": "one row per region and week",
        "Rows": "150K",
        "Fields": "2",
        "Category": "Revenue",
    }
    assert cube["field_count"] == 2
    coverage = next(field for field in cube["fields"] if field["name"] == "coverage")
    assert coverage["definition"] == "Share of quota already covered."
    assert coverage["semantics"] == "A ratio from zero to one."


def test_federated_search_keeps_working_when_one_corpus_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        server,
        "_calliope_brain_evidence",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("brain offline")),
    )
    monkeypatch.setattr(
        server,
        "_calliope_artifact_evidence",
        lambda *_args: [{
            "id": "artifact:one:v1",
            "group": "artifacts",
            "kind": "artifact",
            "title": "One",
            "score": 0.8,
        }],
    )
    monkeypatch.setattr(
        server,
        "_calliope_data_evidence",
        lambda *_args: [{
            "id": "data:one",
            "group": "data",
            "kind": "db_table",
            "title": "sales.one",
            "score": 0.7,
        }],
    )

    result = server._calliope_evidence_search("one", "pilot@example.com", 12)
    assert [item["id"] for item in result["items"]] == ["artifact:one:v1", "data:one"]
    assert result["searched"][0]["status"] == "unavailable"
    assert "Company memory is temporarily unavailable" in result["warnings"][0]


def test_open_document_rechecks_brain_acl_with_the_signed_owner(monkeypatch):
    calls = []

    def get_doc(doc_id, owner):
        calls.append((doc_id, owner))
        return {
            "doc_id": doc_id,
            "title": "Pipeline review",
            "source": "Fireflies",
            "folder_path": "/meetings",
            "mime": "text/markdown",
            "body": "# Pipeline\nCoverage improved.",
        }

    monkeypatch.setattr(server, "tool_brain_get_doc", get_doc)
    result = server._calliope_evidence_open(
        {
            "kind": "document",
            "title": "Search excerpt",
            "provenance": {"resolver": "brain_search", "doc_id": "77"},
        },
        "pilot_pg_role",
        "pilot@example.com",
    )

    assert calls == [(77, "pilot@example.com")]
    assert result["mode"] == "document"
    assert result["document"]["body"].startswith("# Pipeline")


def test_open_column_generates_quoted_sql_and_uses_the_execution_subject(monkeypatch):
    calls = []
    records = []

    def run_sql(sql, as_of=None, limit=None):
        calls.append((sql, as_of, limit, server._SESSION_SUB.get()))
        return {
            "columns": [{"name": 'Net "Value', "type": "numeric"}],
            "rows": [[42]],
            "row_count": 1,
            "truncated": False,
            "engine": "postgres",
            "elapsed_ms": 2,
        }

    monkeypatch.setattr(server, "tool_run_sql", run_sql)
    monkeypatch.setattr(server, "_record", lambda *args, **kwargs: records.append((args, kwargs)))
    result = server._calliope_evidence_open(
        {
            "kind": "db_column",
            "title": 'public.Order Facts.Net "Value',
            "provenance": {
                "resolver": "search_data_weighted",
                "schema": "public",
                "relation": "Order Facts",
                "column": 'Net "Value',
            },
        },
        "pilot_pg_role",
        "pilot@example.com",
    )

    expected = 'SELECT "Net ""Value" FROM "public"."Order Facts" LIMIT 500'
    assert calls == [(expected, None, 500, "pilot_pg_role")]
    assert result["mode"] == "query"
    assert result["query"]["sql"] == expected
    assert result["query"]["default_view"] == "table"
    assert records[0][1]["caller_override"] == "pilot@example.com"


def test_open_cube_uses_its_materialized_relation_as_a_table(monkeypatch):
    opened = []
    monkeypatch.setattr(
        server,
        "_calliope_evidence_query",
        lambda sql, subject, owner, **kwargs: opened.append((sql, subject, owner, kwargs)) or {
            "columns": [{"name": "map_name"}, {"name": "brushes"}],
            "rows": [["e1m1", 486]],
            "row_count": 1,
        },
    )
    result = server._calliope_evidence_open(
        {
            "kind": "cube",
            "title": "cubes.engine_perf",
            "provenance": {
                "resolver": "search_data_weighted",
                "schema": "untrusted_override",
                "relation": "engine_perf",
            },
        },
        "pilot_pg_role",
        "pilot@example.com",
    )

    expected = 'SELECT * FROM "cubes"."engine_perf" LIMIT 500'
    assert opened[0][:3] == (expected, "pilot_pg_role", "pilot@example.com")
    assert result["mode"] == "query"
    assert result["kind"] == "cube"
    assert result["query"]["sql"] == expected
    assert result["query"]["default_view"] == "table"


def test_open_dashboard_object_reloads_exact_enriched_manifest(monkeypatch):
    rows = [{
        "id": 7,
        "name": "Pipeline Health",
        "app_kind": "dashboard",
        "manifest": {"semantic_map": {"objects": []}},
        "semantic_status": "ready",
        "semantic_map": {
            "objects": [{
                "id": "weighted_pipeline",
                "kind": "chart",
                "meaning": {"label": "Weighted pipeline", "description": "Pipeline by stage."},
                "evaluator": {"sql": "select stage, amount from sales.pipeline"},
            }],
        },
        "verification": {"verified_count": 1},
        "prompt_version": "test",
        "model": "test",
        "semantic_updated_at": "2026-08-01T12:00:00Z",
    }]
    opened = []
    monkeypatch.setattr(server, "_conn", lambda: _Connection(rows))
    monkeypatch.setattr(
        server,
        "_calliope_evidence_query",
        lambda sql, subject, owner, **kwargs: opened.append((sql, subject, owner, kwargs)) or {
            "columns": [{"name": "stage"}, {"name": "amount"}],
            "rows": [["Won", 10]],
            "row_count": 1,
        },
    )
    result = server._calliope_evidence_open(
        {
            "kind": "dashboard-object",
            "title": "Weighted pipeline · Pipeline Health",
            "provenance": {
                "resolver": "artifact_semantic_map",
                "slug": "pipeline-health",
                "version": 3,
                "object_id": "weighted_pipeline",
            },
        },
        "pilot_pg_role",
        "pilot@example.com",
    )

    assert opened[0][:3] == (
        "select stage, amount from sales.pipeline",
        "pilot_pg_role",
        "pilot@example.com",
    )
    assert result["mode"] == "query"
    assert result["query"]["default_view"] == "chart"
    assert result["external_url"] == "/d/pipeline-health/versions/3"
