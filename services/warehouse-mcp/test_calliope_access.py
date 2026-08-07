"""Contracts for read-only Calliope notebook sharing and named callers."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import application_teams  # noqa: E402
import calliope_access  # noqa: E402
import server  # noqa: E402


def test_sharing_migration_and_service_contract_are_additive_and_audited():
    migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations" /
        "0257_calliope_session_sharing.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )
    combined = migration + calliope_access.DDL

    assert '"0257_calliope_session_sharing"' in registry
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_session_view_grants" in combined
    assert "num_nonnulls(team_id,principal_email)=1" in combined
    assert "Calliope session access events are append-only" in combined
    assert "CREATE OR REPLACE FUNCTION rvbbit.calliope_session_can_view" in combined
    assert "t.system_key='everyone'" in combined
    assert "ALTER COLUMN author_email SET NOT NULL" in combined
    assert "calliope_turn_author_default" in combined
    assert "contributor" not in combined.lower()


def test_sharing_inputs_are_exact_normalized_lists():
    assert calliope_access._email_list([
        " Person@Example.com ", "person@example.com", "other@example.com"
    ]) == ["person@example.com", "other@example.com"]
    assert calliope_access._uuid_list([
        "d2956549-8201-4654-a3ea-d8dacaef1cc9",
        "d2956549-8201-4654-a3ea-d8dacaef1cc9",
    ]) == ["d2956549-8201-4654-a3ea-d8dacaef1cc9"]

    with pytest.raises(calliope_access.CalliopeAccessError) as invalid:
        calliope_access._email_list(["not an email"])
    assert invalid.value.code == "INVALID_GRANTS"


def test_avatar_sources_remain_provider_bounded_and_have_initials_fallback():
    valid = "https://lh3.googleusercontent.com/a/example"
    assert application_teams._google_avatar_url(valid) == valid
    assert application_teams._google_avatar_url("http://lh3.googleusercontent.com/a/example") is None
    assert application_teams._google_avatar_url("https://googleusercontent.com.evil.test/a") is None
    assert calliope_access.avatar_url("d2956549-8201-4654-a3ea-d8dacaef1cc9") == (
        "/api/calliope/avatars/d2956549-8201-4654-a3ea-d8dacaef1cc9"
    )
    assert calliope_access.avatar_url("bad") is None


def test_callie_and_browser_use_the_same_access_mutators():
    server_source = (HERE / "server.py").read_text(encoding="utf-8")
    calliope_source = (HERE / "calliope.py").read_text(encoding="utf-8")
    browser_source = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    dockerfile = (HERE / "Dockerfile").read_text(encoding="utf-8")

    for tool in ("calliope_session_access_get", "calliope_session_access_update"):
        assert f'mcp.tool(name="{tool}")' in server_source
        assert tool in calliope_source
    assert "calliope_access.get_access" in inspect.getsource(server.tool_calliope_session_access_get)
    assert "calliope_access.replace_access" in inspect.getsource(
        server.tool_calliope_session_access_update
    )
    assert "/api/calliope/session-events" in browser_source
    assert 'id: "shared"' in browser_source
    assert "currentReadOnly()" in browser_source
    assert "calliope_access.py" in dockerfile


def test_activity_receipts_name_the_notebook_without_copying_the_audience():
    session_id = "d2956549-8201-4654-a3ea-d8dacaef1cc9"
    result = {
        "session": {"id": session_id, "access_revision": 4},
        "grants": {
            "teams": [{"id": "00000000-0000-4000-8000-000000000002"}],
            "people": [{"email": "not-copied@example.com"}],
        },
        "private": False,
        "changed": True,
    }
    args = {"session_id": session_id}

    assert server._objects("calliope_session_access_update", args, result) == [
        f"calliope_session:{session_id}"
    ]
    assert server._summary("calliope_session_access_update", result) == {
        "session_id": session_id,
        "revision": 4,
        "teams": 1,
        "people": 1,
        "private": False,
        "changed": True,
    }
