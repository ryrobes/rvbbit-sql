"""Contracts for Calliope's evidence-backed living Pages."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "warehouse_calliope_pages_test_module", _HERE / "calliope_pages.py"
)
pages = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pages)


def test_page_definition_is_personal_business_evidence_by_default():
    title, question, anchor, filters = pages._definition(
        "  Acme pulse  ",
        " What changed, what did we promise, and what needs attention? ",
        {"kind": "entity", "label": "Acme Corp", "ignored": "nope"},
    )
    assert title == "Acme pulse"
    assert question.startswith("What changed")
    assert anchor == {"kind": "entity", "label": "Acme Corp"}
    assert filters["type"] == list(pages.DEFAULT_TYPES)
    assert "system_learning" not in filters["type"]


def test_page_definition_accepts_an_explicit_non_business_corpus_for_diagnostics():
    _title, _question, _anchor, filters = pages._definition(
        "RVBBIT pulse", "What has this database learned?", {},
        {"type": ["system_learning", "system_learning", "bad type!"]},
    )
    assert filters == {"type": ["system_learning"]}


def test_page_fingerprint_is_stable_but_tracks_source_and_definition_changes():
    page = {
        "question": "What changed?",
        "anchor": {"label": "Acme"},
        "source_filters": {"type": ["ticket"]},
    }
    evidence = [{"doc_id": 7, "chunk_id": 11, "content_hash": "abc"}]
    first = pages._fingerprint(page, evidence)
    assert first == pages._fingerprint(dict(page), list(evidence))
    assert first != pages._fingerprint(page, [
        {"doc_id": 7, "chunk_id": 11, "content_hash": "changed"}
    ])
    assert first != pages._fingerprint({**page, "question": "What changed today?"}, evidence)


def test_page_writer_removes_a_redundant_generated_title_but_keeps_real_sections():
    body = pages._clean_model_body(
        "## Acme relationship pulse\n\n## Current state\n\n"
        "The renewal is on track and the next review is Friday [1].",
        "Acme relationship pulse",
    )
    assert body.startswith("## Current state")
    assert "next review is Friday [1]." in body


def test_grounded_fallback_is_still_a_cited_revision():
    body = pages._fallback_body(
        {"question": "What should we know?"},
        [{"title": "Meeting notes", "ordinal": 1, "excerpt": "The renewal is due Friday."}],
    )
    assert "Meeting notes [1]" in body
    assert "The renewal is due Friday." in body
    assert "What should we know?" in body


def test_refresh_generation_failure_preserves_an_existing_revision(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql, _params=None):
            return self

        def fetchone(self):
            return {"available": False}

    with pytest.raises(pages.PageGenerationUnavailable):
        pages._generate_body(
            lambda: Connection(),
            {"owner_email": "ada@example.com", "title": "Pulse", "question": "What changed?"},
            [{
                "ordinal": 1, "title": "Notes", "source": "Docs",
                "doc_type": "document", "occurred_at": None,
                "excerpt": "A supported fact.",
            }],
            previous_body="## Current state\n\nThe prior good revision.",
        )


def test_invalid_page_inputs_fail_before_database_work():
    with pytest.raises(pages.PageError):
        pages._definition("", "A real question")
    with pytest.raises(pages.PageError):
        pages._definition("A page", "x")
    with pytest.raises(pages.PageError):
        pages._definition("A page", "A real question", {"doc_id": "not-a-number"})


def test_page_schema_and_ui_preserve_acl_receipts_and_future_object_anchors():
    source = (_HERE / "calliope_pages.py").read_text(encoding="utf-8")
    migration = (
        _HERE.parent.parent / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0278_calliope_living_pages.sql"
    ).read_text(encoding="utf-8")
    page = (_HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    script = (_HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")

    for table in (
        "calliope_pages", "calliope_page_revisions",
        "calliope_page_evidence", "calliope_page_runs",
    ):
        assert f"rvbbit.{table}" in migration
    assert "brain_visible_docs(p.owner_email)" in migration
    assert "calliope_page_revision_visible" in source
    assert "body\": None" not in source  # body is conditionally withheld, not destroyed
    assert 'id="calliope-pages-dialog"' in page
    assert 'id="calliope-page-anchor"' in page
    assert "async function createPage()" in script
    assert "pageRevisionHtml(revision.body, page.title)" in script
