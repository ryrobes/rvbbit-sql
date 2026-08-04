"""Private Daily Brief note, object-link, and graph-overlay contracts."""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import calliope  # noqa: E402


class _Result:
    def __init__(self, rows):
        self.rows = rows if isinstance(rows, list) else ([rows] if rows else [])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_daily_notes_migration_is_private_append_only_and_self_healing():
    migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0231_calliope_daily_notes.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_daily_notes" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_daily_note_links" in migration
    assert "CREATE OR REPLACE VIEW rvbbit.calliope_private_note_edges" in migration
    assert "FOREIGN KEY (brief_id,owner_email)" in migration
    assert "owner_email" in migration
    assert "body" not in migration.split("CREATE OR REPLACE VIEW", 1)[1].split("COMMENT", 1)[0]
    assert "0231_calliope_daily_notes" in registry
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_daily_notes" in calliope._BRIEF_DDL
    assert "calliope_private_note_edges" in calliope._BRIEF_DDL


def test_note_markers_are_bounded_deduplicated_and_keep_plain_prose_readable():
    body = (
        "Met [[person:42|Ada Lovelace]] about [[project:77|Apollo]].\n"
        "Follow up with [[person:42|Ada]]."
    )
    assert calliope._brief_note_markers(body) == [
        {"entity_kind": "person", "node_id": 42, "mention": "Ada Lovelace"},
        {"entity_kind": "project", "node_id": 77, "mention": "Apollo"},
    ]
    assert calliope._brief_note_plain_text(body) == (
        "Met Ada Lovelace about Apollo.\nFollow up with Ada."
    )
    assert calliope._brief_note_entity_kind("PERSON") == "person"
    assert calliope._brief_note_entity_kind("document", "issue") == "ticket"
    assert calliope._brief_note_entity_kind("location") == "place"
    assert calliope._brief_note_entity_kind("organization") == "thing"

    too_many = " ".join(f"[[thing:{index}|Thing {index}]]" for index in range(1, 26))
    with pytest.raises(ValueError, match="at most 24"):
        calliope._brief_note_markers(too_many)
    with pytest.raises(ValueError, match="outside the supported range"):
        calliope._brief_note_markers("[[thing:99999999999999999999|Too large]]")


def test_object_lookup_is_acl_gated_and_projects_open_graph_kinds():
    rows = [
        {
            "node_id": 10,
            "graph_id": "brain",
            "kind": "person",
            "label": "Ada Lovelace",
            "doc_type": None,
            "source": None,
        },
        {
            "node_id": 11,
            "graph_id": "brain",
            "kind": "document",
            "label": "ENG-42 · Apollo follow-up",
            "doc_type": "ticket",
            "source": "Linear Issues",
        },
    ]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            assert "brain_visible_docs(%s)" in query
            assert "me.predicate_norm='mentions'" in query
            assert "rvbbit.brain_doc_type(s.config)" in query
            assert params[0] == "owner@example.com"
            return _Result(rows)

    objects = calliope._brief_note_objects(
        Connection, "owner@example.com", "apollo", limit=12
    )
    assert [item["kind"] for item in objects] == ["person", "ticket"]
    assert objects[1]["source"] == "Linear Issues"
    tickets = calliope._brief_note_objects(
        Connection, "owner@example.com", "apollo", kind="ticket", limit=12
    )
    assert [item["node_id"] for item in tickets] == ["11"]


def test_append_note_validates_surface_owner_and_persists_confirmed_private_edges():
    brief_id = str(uuid.uuid4())
    surface_id = str(uuid.uuid4())
    inserted_links = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def transaction(self):
            return _Transaction()

        def execute(self, query, params=None):
            if "JOIN rvbbit.calliope_briefs" in query:
                assert params == (surface_id, "owner@example.com", "owner@example.com")
                return _Result({
                    "brief_id": brief_id,
                    "brief_date": "2026-08-02",
                    "surface_id": surface_id,
                })
            if "WITH visible_docs AS MATERIALIZED" in query:
                assert params == ("owner@example.com", [42])
                return _Result({
                    "node_id": 42,
                    "graph_id": "brain",
                    "kind": "person",
                    "label": "Ada Lovelace",
                    "doc_type": None,
                    "source": "Company knowledge",
                })
            if "INSERT INTO rvbbit.calliope_daily_notes" in query:
                assert params[1:4] == (brief_id, "owner@example.com", "2026-08-02")
                return _Result({
                    "id": params[0],
                    "note_date": params[3],
                    "body": params[4],
                    "created_at": datetime(2026, 8, 2, 16, tzinfo=timezone.utc),
                })
            if "INSERT INTO rvbbit.calliope_daily_note_links" in query:
                inserted_links.append(params)
                return _Result(None)
            raise AssertionError(query)

    note = calliope._append_brief_note(
        Connection,
        "owner@example.com",
        surface_id,
        "Decision with [[person:42|Ada]].",
    )
    assert note["body"] == "Decision with [[person:42|Ada]]."
    assert note["links"][0]["label"] == "Ada Lovelace"
    assert len(inserted_links) == 1
    assert inserted_links[0][5:7] == ("person", "Ada Lovelace")
    properties = json.loads(inserted_links[0][7])
    assert properties["confirmed_by"] == "user_lookup"
    assert properties["mention"] == "Ada"


def test_prior_notes_become_noted_truth_in_future_briefs_only():
    note_id = str(uuid.uuid4())
    row = {
        "id": note_id,
        "note_date": "2026-08-01",
        "body": "Ask [[person:42|Ada]] about the Apollo decision.",
        "created_at": datetime(2026, 8, 1, 20, tzinfo=timezone.utc),
        "links": [{
            "node_id": 42,
            "graph_id": "brain",
            "node_kind": "person",
            "kind": "person",
            "label": "Ada Lovelace",
            "properties": {"confirmed_by": "user_lookup"},
        }],
    }

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            assert "n.note_date<%s::date" in query
            assert params == ("owner@example.com", "2026-08-02", "2026-08-02", 30)
            return _Result(row)

    items, coverage, warnings = calliope._brief_note_observations(
        Connection, "owner@example.com", "2026-08-02"
    )
    assert warnings == []
    assert coverage[0]["label"] == "Your notes"
    assert items[0]["id"] == f"note:{note_id}"
    assert items[0]["summary"] == "Ask Ada about the Apollo decision."
    assert items[0]["provenance"]["brief_section"] == "from_notes"
    assert items[0]["provenance"]["viewer_relation"]["truth"] == "noted"
    assert items[0]["provenance"]["entity_refs"][0]["node_id"] == 42


def test_notes_ship_with_codemirror_lookup_ui_and_authenticated_routes():
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    page = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    editor_source = (HERE / "calliope-editor" / "editor.js").read_text(encoding="utf-8")
    editor_bundle = HERE / "calliope" / "daily-notes-editor.js"
    css = (HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")

    assert '@mcp.custom_route("/api/calliope/briefs/notes", methods=["GET"])' in backend
    assert '@mcp.custom_route("/api/calliope/briefs/notes", methods=["POST"])' in backend
    assert '@mcp.custom_route("/api/calliope/briefs/note-objects", methods=["GET"])' in backend
    assert '"personal_notes": True' in backend
    assert '/calliope/daily-notes-editor.js' in page
    assert editor_bundle.stat().st_size > 100_000
    assert "@codemirror/autocomplete" in editor_source
    assert "objectCompletionSource" in editor_source
    assert "[[${kind}:${refId}|${label}]]" in editor_source
    assert "window.CalliopeObjectEditor" in editor_source
    assert "function renderBriefNotes" in script
    assert "function appendBriefNote" in script
    assert "private graph edge" in script
    assert ".brief-note-editor" in css
    assert ".brief-notes-private" in css
