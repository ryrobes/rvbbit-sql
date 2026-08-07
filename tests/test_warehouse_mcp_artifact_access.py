"""End-to-end contracts for owner-managed artifact viewers and archive."""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from conftest import RVBBIT_DSN


def _load_artifact_access():
    root = Path(__file__).resolve().parents[1] / "services" / "warehouse-mcp"
    # artifact_access imports the sibling application_teams module.
    import sys

    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location(
        f"artifact_access_{uuid.uuid4().hex}", root / "artifact_access.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _BorrowedConnection:
    """Let service helpers share one test transaction without committing it."""

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


def test_artifact_viewers_archive_and_one_time_grandfathering(rvbbit):
    rvbbit.execute("SELECT rvbbit.migrate()")
    access = _load_artifact_access()
    owner = f"owner-{uuid.uuid4().hex[:8]}@example.invalid"
    member = f"member-{uuid.uuid4().hex[:8]}@example.invalid"
    person = f"person-{uuid.uuid4().hex[:8]}@example.invalid"
    slug = f"artifact-access-{uuid.uuid4().hex[:10]}"
    auth = {
        "actor": owner,
        "subject": owner,
        "mode": "pytest",
        "delegated": False,
    }

    conn = psycopg.connect(RVBBIT_DSN, row_factory=dict_row)
    factory = lambda: _BorrowedConnection(conn)
    try:
        for email, name in (
            (owner, "Artifact owner"),
            (member, "Team viewer"),
            (person, "Individual viewer"),
        ):
            conn.execute(
                "INSERT INTO rvbbit.application_principals(email,display_name) "
                "VALUES (%s,%s)",
                (email, name),
            )
        team_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO rvbbit.teams "
            "(id,slug,name,description,created_by,updated_by) "
            "VALUES (%s::uuid,%s,%s,'pytest',%s,%s)",
            (team_id, f"access-{uuid.uuid4().hex[:8]}", f"Access {uuid.uuid4().hex[:8]}", owner, owner),
        )
        conn.execute(
            "INSERT INTO rvbbit.team_members(team_id,principal_email,added_by) "
            "VALUES (%s::uuid,%s,%s)",
            (team_id, member, owner),
        )
        artifact_id = conn.execute(
            "INSERT INTO rvbbit.dashboards(slug,name,owner_email) "
            "VALUES (%s,'Artifact access pytest',%s) RETURNING id",
            (slug, owner),
        ).fetchone()["id"]

        # Re-running the service bootstrap must not grant a newly published row
        # to Everyone. Only rows present at the first 0254 install are public.
        conn.execute(access.DDL)
        assert conn.execute(
            "SELECT count(*) AS n FROM rvbbit.artifact_view_grants WHERE artifact_id=%s",
            (artifact_id,),
        ).fetchone()["n"] == 0

        initial = access.get_access(factory, auth, slug)
        assert initial["private"] and initial["summary"] == "Only you"
        assert access.can_view(conn, slug, owner)
        assert not access.can_view(conn, slug, member)
        people = access.search_people(factory, auth, "viewer", 20)["people"]
        assert owner not in {item["email"] for item in people}
        assert {member, person}.issubset({item["email"] for item in people})

        shared = access.replace_access(
            factory,
            auth,
            slug,
            1,
            team_ids=[team_id],
            people=[person, owner],  # owner is implicit, never a redundant grant
        )
        assert shared["artifact"]["access_revision"] == 2
        assert access.can_view(conn, slug, member)
        assert access.can_view(conn, slug, person)
        assert [item["email"] for item in shared["grants"]["people"]] == [person]

        everyone_id = str(conn.execute(
            "SELECT id FROM rvbbit.teams WHERE system_key='everyone'"
        ).fetchone()["id"])
        try:
            access.replace_access(
                factory, auth, slug, 2, team_ids=[everyone_id], people=[]
            )
            raise AssertionError("Everyone was added without explicit confirmation")
        except access.ArtifactAccessError as exc:
            assert exc.code == "EVERYONE_CONFIRMATION_REQUIRED"
        everyone = access.replace_access(
            factory,
            auth,
            slug,
            2,
            team_ids=[everyone_id],
            people=[],
            confirm_everyone=True,
        )
        assert everyone["everyone"]

        archived = access.set_archived(factory, auth, slug, 3, True)
        assert archived["artifact"]["archived"]
        assert not access.can_view(conn, slug, owner)
        assert access.can_view(conn, slug, owner, include_archived=True)
        assert not access.can_view(conn, slug, member, include_archived=True)

        restored = access.set_archived(factory, auth, slug, 4, False)
        assert not restored["artifact"]["archived"]
        assert restored["everyone"] and access.can_view(conn, slug, member)
        assert len(restored["events"]) == 4

        try:
            access.replace_access(factory, auth, slug, 4, team_ids=[], people=[])
            raise AssertionError("A stale sharing revision was accepted")
        except access.ArtifactAccessError as exc:
            assert exc.code == "ARTIFACT_ACCESS_CONFLICT"
    finally:
        conn.rollback()
        conn.close()
