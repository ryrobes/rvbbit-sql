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
    assert any(item["kind"] == "artifact" and item["url"] == "/d/pipeline-health" for item in items)


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
