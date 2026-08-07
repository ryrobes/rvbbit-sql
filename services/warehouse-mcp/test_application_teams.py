"""Contracts for flat application Teams and protected system Teams."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import application_teams  # noqa: E402
import server  # noqa: E402


class _RecordingConnection:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return SimpleNamespace()


class _Result:
    def __init__(self, *, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


def test_historical_people_backfill_uses_only_trusted_identity_ledgers():
    class Connection:
        def __init__(self):
            self.calls = []

        def execute(self, query, params=None):
            self.calls.append((query, params))
            if query.startswith("SELECT to_regclass"):
                return _Result(row={"activity": True, "sessions": True})
            return _Result()

    connection = Connection()
    application_teams._backfill_observed_principals(connection)

    backfill = connection.calls[1][0]
    assert "subject IS NOT NULL" in backfill
    assert "rvbbit.calliope_sessions" in backfill
    assert "subject IS NULL AND channel='web'" in backfill
    assert "client_app IN ('dashboard','gallery','calliope','warehouse_web')" in backfill
    assert "channel='direct_mcp'" not in backfill
    assert "browser_session" in backfill
    assert "WHERE NOT (email=ANY(%s::text[]))" in backfill
    assert connection.calls[1][1] == (["calliope@system"],)


def test_people_search_matches_written_names_across_email_punctuation():
    class Connection:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            self.calls.append((query, params))
            if query.startswith("SELECT p.email"):
                return _Result(rows=[])
            if query.startswith("SELECT EXISTS"):
                return _Result(row={"allowed": False})
            return _Result()

    connection = Connection()
    result = application_teams.people_search(
        lambda: connection,
        SimpleNamespace(subject="admin@example.com", mode="browser_session"),
        query="Jane Smith",
    )

    search_query, params = next(
        (query, params) for query, params in connection.calls
        if query.startswith("SELECT p.email")
    )
    assert "ILIKE ALL" in search_query
    assert params[:2] == (["%jane%", "%smith%"], ["%jane%", "%smith%"])
    assert result["people"] == []


def test_only_authorized_human_subjects_enter_the_people_directory():
    connection = _RecordingConnection()
    automation = SimpleNamespace(
        actor="calliope@example.com",
        subject=None,
        mode="hermes_automation",
        platform="cron",
    )
    assert application_teams.observe_principal_on_connection(
        connection, automation
    ) is None
    assert connection.calls == []

    delegated = SimpleNamespace(
        actor="calliope@example.com",
        subject="Person@Example.com",
        mode="google_chat_delegation",
        delegated=True,
        platform="google_chat",
    )
    assert application_teams.observe_principal_on_connection(
        connection, delegated
    ) == "person@example.com"
    assert len(connection.calls) == 1
    query, params = connection.calls[0]
    assert query.startswith("INSERT INTO rvbbit.application_principals")
    assert params == (
        "person@example.com",
        None,
        "google_chat_delegation",
        "google_chat",
        None,
        None,
    )


def test_team_inputs_are_normalized_and_system_team_names_are_reserved():
    assert application_teams.normalize_email(" Person@Example.com ") == "person@example.com"
    assert application_teams._slug_base("  Customer Success / East  ") == "customer-success-east"

    with pytest.raises(application_teams.TeamError) as invalid_email:
        application_teams.normalize_email("not a person")
    assert invalid_email.value.code == "INVALID_PRINCIPAL"

    with pytest.raises(application_teams.TeamError) as reserved:
        application_teams._name(" admins ")
    assert reserved.value.code == "RESERVED_TEAM_NAME"

    with pytest.raises(application_teams.TeamError) as everyone_reserved:
        application_teams._name(" Everyone ")
    assert everyone_reserved.value.code == "RESERVED_TEAM_NAME"

    authorization = SimpleNamespace(subject="admin@example.com")
    with pytest.raises(application_teams.TeamError) as archived:
        application_teams.update_team(
            lambda: None,
            authorization,
            "d2956549-8201-4654-a3ea-d8dacaef1cc9",
            1,
            archived="false",
        )
    assert archived.value.code == "INVALID_TEAM_CHANGE"


def test_migrations_and_service_ddl_preserve_system_teams_and_append_only_audit():
    teams_migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations" /
        "0252_application_teams.sql"
    ).read_text(encoding="utf-8")
    everyone_migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations" /
        "0253_application_everyone_team.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )
    combined = teams_migration + everyone_migration + application_teams.DDL

    assert '"0252_application_teams"' in registry
    assert '"0253_application_everyone_team"' in registry
    for table in (
        "application_principals", "teams", "team_members", "team_events"
    ):
        assert f"CREATE TABLE IF NOT EXISTS rvbbit.{table}" in combined
    assert "The Admins Team cannot be renamed or archived" in combined
    assert "The Admins Team must retain at least one member" in combined
    assert "The Everyone Team is a protected authenticated-user wildcard" in combined
    assert "The Everyone Team has dynamic membership and cannot contain explicit members" in combined
    assert "system_key='everyone'" in combined
    assert "Team audit events are append-only" in combined
    assert "parent_team" not in combined
    assert "member_role" not in combined


def test_everyone_matches_only_a_verified_subject_without_materialized_members():
    everyone = {
        "system_key": "everyone",
        "archived": False,
        "members": [],
    }
    finance = {
        "system_key": None,
        "archived": False,
        "members": ["person@example.com"],
    }

    verified = SimpleNamespace(subject="Person@Example.com")
    service = SimpleNamespace(subject=None, actor="calliope@example.com")

    assert application_teams.authorization_matches_team(everyone, verified) is True
    assert application_teams.authorization_matches_team(everyone, service) is False
    assert application_teams.authorization_matches_team(finance, verified) is True
    assert application_teams.authorization_matches_team(
        finance, SimpleNamespace(subject="someone-else@example.com")
    ) is False
    assert application_teams.authorization_matches_team(
        {**everyone, "archived": True}, verified
    ) is False

    shaped = application_teams._shape_team({
        "id": application_teams.EVERYONE_TEAM_ID,
        "system_key": "everyone",
        "members": [],
    })
    assert shaped["dynamic_membership"] is True
    assert shaped["membership_rule"] == "authenticated_users"
    assert shaped["member_count"] is None
    assert shaped["protected"] is True


def test_server_registers_read_and_admin_mutation_tools():
    source = (HERE / "server.py").read_text(encoding="utf-8")
    dockerfile = (HERE / "Dockerfile").read_text(encoding="utf-8")

    for tool in (
        "team_people_search", "team_list", "team_get", "team_create", "team_update"
    ):
        assert f'mcp.tool(name="{tool}")' in source
    assert "_require_admin(conn, subject)" in inspect.getsource(application_teams.create_team)
    assert "_require_admin(conn, subject)" in inspect.getsource(application_teams.update_team)
    assert "Only members of the Admins Team may change Teams." in inspect.getsource(
        application_teams._require_admin
    )
    assert "application_teams.py" in dockerfile


def test_team_activity_receipts_keep_compact_objects_and_results():
    result = {
        "changed": True,
        "team": {
            "id": "d2956549-8201-4654-a3ea-d8dacaef1cc9",
            "name": "Customer Success",
            "revision": 4,
            "member_count": 12,
            "members": ["not-copied-into-summary@example.com"],
        },
    }
    args = {"team_id": "d2956549-8201-4654-a3ea-d8dacaef1cc9"}

    assert server._objects("team_update", args, result) == [
        "team:d2956549-8201-4654-a3ea-d8dacaef1cc9"
    ]
    assert server._summary("team_update", result) == {
        "id": "d2956549-8201-4654-a3ea-d8dacaef1cc9",
        "name": "Customer Success",
        "revision": 4,
        "member_count": 12,
        "changed": True,
    }
