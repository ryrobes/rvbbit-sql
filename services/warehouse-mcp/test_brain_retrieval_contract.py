"""Regression contracts for Company Brain discovery, retrieval, and ingestion."""
from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import calliope  # noqa: E402
import server  # noqa: E402


def test_brain_tools_publish_descriptions_and_typed_schemas():
    mcp = server._build_mcp()
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    ask = tools["ask_brain"]
    assert "semantic search" in ask.description.lower()
    assert "company brain" in ask.description.lower()
    assert set(ask.parameters["properties"]) == {"query", "k", "filters"}
    assert ask.parameters["properties"]["k"]["type"] == "integer"
    assert ask.parameters["properties"]["filters"]["anyOf"][0]["type"] == "object"

    facets = tools["brain_facets"]
    assert "source labels" in facets.description.lower()

    browse = tools["brain_browse"]
    assert "navigation only" in browse.description.lower()
    assert set(browse.parameters["properties"]) == {"source", "folder", "limit", "offset"}
    assert browse.parameters["properties"]["limit"]["type"] == "integer"

    warehouse_search = tools["search_data"]
    assert "warehouse relations" in warehouse_search.description.lower()
    assert "does not search company brain" in warehouse_search.description.lower()

    crawl = tools["brain_crawl_folder"]
    assert "mutating ingestion tool" in crawl.description.lower()
    assert set(crawl.parameters["required"]) == {"path", "source"}
    assert crawl.parameters["properties"]["recursive"]["type"] == "boolean"


def test_calliope_prompt_routes_document_store_questions_to_brain_search():
    instructions = calliope._instructions([], None)

    assert "COMPANY BRAIN RETRIEVAL" in instructions
    assert "call brain_facets first" in instructions
    assert "call ask_brain with the exact source/type filters" in instructions
    assert "search_data searches warehouse tables/columns/catalog metadata only" in instructions
    assert "brain_crawl_folder MUTATES the corpus" in instructions
    assert "Never tell the user a Brain source" in instructions


def test_brain_browse_is_bounded_and_pageable(monkeypatch):
    rows = [
        {
            "folder_path": "/linear/issues",
            "doc_id": index,
            "title": f"RYR-{index}",
            "source": "Linear Issues",
            "mime": "text/markdown",
            "author": None,
            "occurred_at": None,
            "ingested_at": "2026-08-12T00:00:00Z",
            "chunks": 1,
        }
        for index in range(1, 102)
    ]

    class Cursor:
        def __init__(self):
            self.query = None
            self.params = None

        def execute(self, query, params):
            self.query = query
            self.params = params
            return self

        def fetchall(self):
            return rows

    cursor = Cursor()
    monkeypatch.setattr(server, "_ro", lambda: nullcontext(cursor))

    result = server.tool_brain_browse(
        "ryan@rvbbit.ai",
        source="Linear Issues",
        folder="linear",
        limit=100,
    )

    assert result["count"] == 100
    assert result["has_more"] is True
    assert result["next_offset"] == 100
    assert result["filters"] == {"source": "Linear Issues", "folder": "/linear"}
    assert "ORDER BY source, folder_path, title, doc_id LIMIT %s OFFSET %s" in cursor.query
    assert cursor.params[-2:] == (101, 0)


def test_brain_folder_ingest_rejects_root_and_requires_named_source(monkeypatch, tmp_path):
    import_root = tmp_path / "brain-imports"
    import_root.mkdir()
    monkeypatch.setenv("WAREHOUSE_BRAIN_IMPORT_ROOTS", str(import_root))

    root_result = server.tool_brain_crawl_folder("/", source="bad-root")
    assert root_result["error"]["code"] == "PATH_OUTSIDE_BRAIN_IMPORT_ROOTS"

    unnamed_result = server.tool_brain_crawl_folder(str(import_root))
    assert unnamed_result["error"]["code"] == "SOURCE_REQUIRED"


def test_brain_folder_ingest_accepts_only_files_inside_allowed_root(monkeypatch, tmp_path):
    import_root = tmp_path / "brain-imports"
    import_root.mkdir()
    (import_root / "ticket.md").write_text("A grounded ticket", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("must not be followed", encoding="utf-8")
    (import_root / "outside-link.md").symlink_to(outside)
    monkeypatch.setenv("WAREHOUSE_BRAIN_IMPORT_ROOTS", str(import_root))

    class Cursor:
        def __init__(self):
            self.calls = []

        def execute(self, query, params):
            self.calls.append((query, params))
            return self

        def fetchone(self):
            return {"id": 42}

    cursor = Cursor()
    monkeypatch.setattr(server, "_conn", lambda: nullcontext(cursor))

    result = server.tool_brain_crawl_folder(
        str(import_root), source="Explicit Import", roles=["public"]
    )

    assert result["ingested"] == 1
    assert result["skipped"] == 1
    assert result["docs"] == [
        {"doc_id": 42, "title": "ticket", "folder": "/Explicit Import"}
    ]
    assert cursor.calls[0][1][-1] == str((import_root / "ticket.md").resolve())
