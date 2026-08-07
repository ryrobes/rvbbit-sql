"""Contracts for Artifact Areas, directed artifact links, and private rail synopses."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import server  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "warehouse_calliope_catalog_test_module", HERE / "calliope.py"
)
calliope = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = calliope
SPEC.loader.exec_module(calliope)


def test_artifact_links_are_internal_directed_versioned_and_deduplicated(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_PUBLIC_URL", "https://warehouse.example.com")
    html = """
      <a href="/d/revenue-plan/versions/3">Open the approved plan</a>
      <a href="https://warehouse.example.com/apps/account-lens">Account lens</a>
      <a href="https://outside.example.com/d/not-ours">External</a>
      <a href="/d/source-artifact">Self</a>
      <script>window.location.href='/apps/account-lens'; const next="/d/pipeline-health";</script>
    """

    links = server._extract_artifact_links(html, "source-artifact")

    assert [(item["slug"], item["target_version"]) for item in links] == [
        ("revenue-plan", 3),
        ("account-lens", None),
        ("pipeline-health", None),
    ]
    assert links[0]["link_text"] == "Open the approved plan"
    assert links[1]["source"] == "html-link"
    assert all(item["slug"] not in {"not-ours", "source-artifact"} for item in links)


def test_area_classifier_is_constrained_and_does_not_find_board_inside_dashboard(monkeypatch):
    areas = [
        {"id": "executive", "label": "Executive", "description": "", "keywords": ["board"], "sort_order": 10},
        {"id": "marketing", "label": "Marketing", "description": "", "keywords": ["campaign", "web traffic"], "sort_order": 20},
        {"id": "other", "label": "Other", "description": "", "keywords": [], "sort_order": 999},
    ]
    monkeypatch.setattr(server, "_artifact_area_context", lambda _job: (areas, [], []))
    monkeypatch.setattr(server, "_clover_area_score", lambda _text, _areas: None)

    other = server._classify_artifact_area({
        "name": "Dashboard rendering proof", "description": "Canvas layout", "html": "", "manifest": {}
    })
    marketing = server._classify_artifact_area({
        "name": "Campaign pulse", "description": "Web traffic and campaign performance", "html": "", "manifest": {}
    })

    assert other["area_id"] == "other"
    assert marketing["area_id"] == "marketing"
    assert marketing["context"]["method"] == "keywords"


def test_area_classifier_prefers_consistent_navigation_context(monkeypatch):
    areas = [
        {"id": "revenue", "label": "Revenue", "description": "", "keywords": [], "sort_order": 10},
        {"id": "other", "label": "Other", "description": "", "keywords": [], "sort_order": 999},
    ]
    votes = [{"area_id": "revenue", "weight": 8.0, "routes": 2}]
    monkeypatch.setattr(server, "_artifact_area_context", lambda _job: (areas, [], votes))

    result = server._classify_artifact_area({"name": "Untitled analysis", "html": "", "manifest": {}})

    assert result["area_id"] == "revenue"
    assert result["context"]["method"] == "context"
    assert result["confidence"] >= 0.9


def test_session_synopsis_is_short_plain_and_has_a_local_fallback():
    source = (
        "User: Compare pipeline coverage with the five-year plan and identify the largest gap.\n"
        "Calliope: The west region is below plan because enterprise opportunities slipped.\n\n"
        "User: try again\nCalliope: The provider was temporarily unavailable."
    )
    synopsis = calliope._fallback_session_synopsis(source)

    assert synopsis == "Compare pipeline coverage with the five-year plan and identify the largest gap."
    assert len(synopsis.split()) <= 28
    assert "this thread" not in synopsis.lower()


def test_session_synopsis_enqueue_resets_a_bounded_debounce(monkeypatch):
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params):
            calls.append((statement, params))

    monkeypatch.setenv("WAREHOUSE_CALLIOPE_SYNOPSIS_DEBOUNCE_SECONDS", "45")
    assert calliope._enqueue_session_synopsis(lambda: Connection(), "12345678-1234-4234-9234-123456789abc")
    assert "ON CONFLICT (session_id) DO UPDATE SET status='pending'" in calls[0][0]
    assert calls[0][1][1:] == (45, 45)


@pytest.mark.parametrize(
    ("available", "operator_fragment", "parameter_count", "provider", "model"),
    [
        (
            {"clover3": True, "clover2": False, "summarize2": True, "summarize1": False},
            "rvbbit.clover_llm_apply(%s,%s,%s::jsonb)",
            3,
            "clover",
            "clover_llm_apply",
        ),
        (
            {"clover3": False, "clover2": True, "summarize2": True, "summarize1": False},
            "rvbbit.clover_llm_apply(%s,%s)",
            2,
            "clover",
            "clover_llm_apply",
        ),
        (
            {"clover3": False, "clover2": False, "summarize2": True, "summarize1": False},
            "rvbbit.summarize(%s,%s::jsonb)",
            2,
            "rvbbit",
            "summarize",
        ),
        (
            {"clover3": False, "clover2": False, "summarize2": False, "summarize1": True},
            "rvbbit.summarize(%s)",
            1,
            "rvbbit",
            "summarize",
        ),
    ],
)
def test_session_synopsis_supports_current_and_legacy_semantic_signatures(
    available,
    operator_fragment,
    parameter_count,
    provider,
    model,
):
    calls = []

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

        def execute(self, statement, params=None):
            calls.append((statement, params))
            if "to_regprocedure" in statement:
                return Result(row=available)
            if " AS synopsis" in statement:
                return Result(row={
                    "synopsis": "Pipeline risk is concentrated in delayed enterprise renewals."
                })
            if "UPDATE rvbbit.receipts" in statement:
                return Result(rows=[])
            raise AssertionError(f"Unexpected SQL: {statement}")

    synopsis, actual_provider, actual_model = calliope._generate_session_synopsis(
        lambda: Connection(),
        "User: Why did pipeline coverage fall?\nCalliope: Enterprise renewals moved out.",
        "pilot@example.com",
    )

    semantic_calls = [call for call in calls if operator_fragment in call[0]]
    receipt_calls = [call for call in calls if "UPDATE rvbbit.receipts" in call[0]]
    assert len(semantic_calls) == 1
    assert len(semantic_calls[0][1]) == parameter_count
    assert len(receipt_calls) == 1
    assert "inputs->>'t'=%s OR inputs->>'text'=%s" in receipt_calls[0][0]
    assert receipt_calls[0][1][-2] == receipt_calls[0][1][-1]
    assert synopsis == "Pipeline risk is concentrated in delayed enterprise renewals."
    assert actual_provider == provider
    assert actual_model == model


def test_gallery_and_session_rail_expose_derived_metadata_without_new_primary_ui():
    server_source = (HERE / "server.py").read_text(encoding="utf-8")
    calliope_source = (HERE / "calliope.py").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")
    migration = (
        HERE.parent.parent / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0244_artifact_catalog_and_session_synopses.sql"
    ).read_text(encoding="utf-8")

    assert 'id="artifact-area"' in server_source
    assert "c.dataset.area===area" in server_source
    assert 'class="pill dim card-area"' in server_source
    assert '"links to" if linked.get("direction") == "outbound" else "linked from"' in server_source
    assert "sn.synopsis" in calliope_source
    assert "session.synopsis" in script
    assert ".session-card .session-synopsis" in css
    assert "calliope_session_synopses" in migration
    assert "artifact_catalog_enrichments" in migration
    assert "ADD COLUMN IF NOT EXISTS metadata jsonb" in migration
