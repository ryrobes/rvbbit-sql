"""Identity and compatibility contracts for capability discovery."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import server  # noqa: E402


class _Result:
    def __init__(self, *, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if "capability_search_stale" in query:
            return _Result(row={"ok": False})
        if "capability_search_for" in query:
            return _Result(rows=[{
                "kind": "cap_playbook",
                "name": "weekly-enrollment-review",
                "score": 0.87,
                "doc": "A reusable enrollment review.",
            }])
        raise AssertionError(f"unexpected query: {query}")


def _authorization(**changes):
    values = {
        "actor": "person@example.com",
        "subject": "person@example.com",
        "attributed_subject": "person@example.com",
        "client_id": "direct-oauth-client",
        "mode": "direct_oauth",
        "assurance": "oauth_access_token",
    }
    values.update(changes)
    return server.ApplicationAuthorizationContext(**values)


def test_capability_search_inherits_the_frozen_subject_without_a_public_argument(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr(server, "_conn", lambda: connection)
    token = server._AUTHORIZATION_CONTEXT.set(_authorization())
    try:
        result = server.tool_capability_search("review enrollment", 6, ["cap_playbook"])
    finally:
        server._AUTHORIZATION_CONTEXT.reset(token)

    search_query, search_params = next(
        (query, params) for query, params in connection.calls
        if "capability_search_for" in query
    )
    assert "capability_search_for(%s,%s,%s,%s)" in search_query
    assert search_params == (
        "person@example.com",
        "review enrollment",
        6,
        ["cap_playbook"],
    )
    assert result["matches"][0]["name"] == "weekly-enrollment-review"
    assert "filtered for this caller" in result["hint"]

    source = (HERE / "server.py").read_text(encoding="utf-8")
    assert (
        'mcp.tool(name="capability_search")(lambda query, limit=8, kinds=None:'
        in source
    )
    assert "caller_email" not in source[
        source.index('mcp.tool(name="capability_search")'):
        source.index('mcp.tool(name="render_pdf")')
    ]


def test_operator_discovery_returns_an_executable_schema_qualified_name():
    match = server._capability_search_match({
        "kind": "cap_operator",
        "name": "clover_forecast",
        "score": 0.4921,
        "doc": (
            "capability clover_forecast\n"
            "kind: cap_operator\n"
            "signature: clover_forecast(series text, horizon text) → jsonb"
        ),
    })

    assert match["name"] == "clover_forecast"
    assert match["sql_name"] == "rvbbit.clover_forecast"
    assert "signature: rvbbit.clover_forecast(" in match["doc"]
    assert match["score"] == 0.492


def test_tool_visibility_batches_names_under_the_frozen_subject(monkeypatch):
    class VisibilityConnection(_Connection):
        def execute(self, query, params=None):
            self.calls.append((query, params))
            if "capability_can_use" in query:
                return _Result(rows=[{"name": "describe_table"}])
            raise AssertionError(f"unexpected query: {query}")

    connection = VisibilityConnection()
    monkeypatch.setattr(server, "_conn", lambda: connection)
    token = server._AUTHORIZATION_CONTEXT.set(_authorization())
    try:
        visible = server._capability_visible_names(
            "cap_mcp_tool", ["run_sql", "describe_table", "run_sql"]
        )
    finally:
        server._AUTHORIZATION_CONTEXT.reset(token)

    assert visible == {"describe_table"}
    assert len(connection.calls) == 1
    query, params = connection.calls[0]
    assert "unnest(%s::text[])" in query
    assert "rvbbit.capability_can_use(%s,candidate.name,%s)" in query
    assert params == (
        ["run_sql", "describe_table"],
        "cap_mcp_tool",
        "person@example.com",
    )


def test_search_tools_and_get_tool_help_hide_names_without_enumerating_them(monkeypatch):
    observed = []

    def visible(kind, names, **_kwargs):
        observed.append((kind, set(names)))
        return set(names) - {"run_sql"}

    monkeypatch.setattr(server, "_capability_visible_names", visible)
    monkeypatch.setattr(server, "_record", lambda *_args, **_kwargs: None)
    mcp = FastMCP("identity-aware-tool-discovery")
    server._register(mcp)

    token = server._AUTHORIZATION_CONTEXT.set(_authorization())
    try:
        search = asyncio.run(
            mcp._tool_manager._tools["search_tools"].fn("", 8)
        )
        help_result = asyncio.run(
            mcp._tool_manager._tools["get_tool_help"].fn(
                ["run_sql", "describe_table"]
            )
        )
    finally:
        server._AUTHORIZATION_CONTEXT.reset(token)

    assert "run_sql" not in search["tools"]
    assert "describe_table" in search["tools"]
    assert [tool["name"] for tool in help_result["tools"]] == [
        "describe_table"
    ]
    # A hidden exact name is deliberately indistinguishable from a typo or an
    # absent tool. Discovery must not expose a separate forbidden category.
    assert help_result["missing"] == ["run_sql"]
    assert len(observed) == 2
    assert all(kind == "cap_mcp_tool" for kind, _names in observed)

    search_schema = mcp._tool_manager._tools["search_tools"].parameters
    help_schema = mcp._tool_manager._tools["get_tool_help"].parameters
    assert set(search_schema["properties"]) == {"query", "limit"}
    assert set(help_schema["properties"]) == {"names"}


def test_managed_cron_uses_warehouse_ownership_only_for_discovery(monkeypatch):
    cron = _authorization(
        actor="calliope@example.com",
        subject=None,
        attributed_subject=None,
        client_id=server._HERMES_SERVICE_CLIENT_ID,
        mode="hermes_automation",
        assurance="hermes_service_credential",
        delegated=False,
        platform="cron",
        session_ref="cron_job-123_20260811T090000Z",
    )
    observed = {}

    def linked(_conn, session_ref):
        observed["session_ref"] = session_ref
        return {"kind": "work_order", "owner": "Owner@Example.com"}

    monkeypatch.setattr(server, "_calliope_activity_for_hermes_session", linked)
    token = server._AUTHORIZATION_CONTEXT.set(cron)
    try:
        assert server._capability_discovery_subject(object()) == "owner@example.com"
        # Discovery recovery does not turn the request into an application
        # mutation subject.
        assert server._application_authorization_context().subject is None
    finally:
        server._AUTHORIZATION_CONTEXT.reset(token)
    assert observed["session_ref"] == "cron_job-123_20260811T090000Z"


def test_rolling_upgrade_falls_back_only_when_the_new_sql_function_is_absent(monkeypatch):
    class RollingConnection(_Connection):
        def execute(self, query, params=None):
            self.calls.append((query, params))
            if "capability_search_stale" in query:
                return _Result(row={"ok": False})
            if "capability_search_for" in query:
                raise server.psycopg.errors.UndefinedFunction(
                    "capability_search_for is not installed yet"
                )
            if "rvbbit.capability_search(%s,%s,%s)" in query:
                return _Result(rows=[])
            raise AssertionError(f"unexpected query: {query}")

    connection = RollingConnection()
    monkeypatch.setattr(server, "_conn", lambda: connection)
    token = server._AUTHORIZATION_CONTEXT.set(_authorization())
    try:
        result = server.tool_capability_search("anything")
    finally:
        server._AUTHORIZATION_CONTEXT.reset(token)

    assert result["matches"] == []
    assert any("capability_search_for" in query for query, _ in connection.calls)
    assert any(
        "rvbbit.capability_search(%s,%s,%s)" in query
        for query, _ in connection.calls
    )


def test_unmanaged_or_legacy_automation_cannot_claim_a_discovery_subject(monkeypatch):
    monkeypatch.setattr(
        server,
        "_calliope_activity_for_hermes_session",
        lambda *_args: {"kind": "session", "owner": "forged@example.com"},
    )
    cron = _authorization(
        actor="calliope@example.com",
        subject=None,
        attributed_subject=None,
        client_id=server._HERMES_SERVICE_CLIENT_ID,
        mode="hermes_automation",
        assurance="hermes_service_credential",
        platform="cron",
        session_ref="unmanaged-cron",
    )
    token = server._AUTHORIZATION_CONTEXT.set(cron)
    try:
        assert server._capability_discovery_subject(object()) is None
    finally:
        server._AUTHORIZATION_CONTEXT.reset(token)

    legacy = _authorization(
        actor="calliope@example.com",
        subject=None,
        attributed_subject="forged@example.com",
        client_id="static-key",
        mode="legacy_hermes_attribution",
        assurance="legacy_shared_key",
        platform="cron",
        session_ref="cron_job-123_20260811T090000Z",
    )
    token = server._AUTHORIZATION_CONTEXT.set(legacy)
    try:
        assert server._capability_discovery_subject(object()) is None
    finally:
        server._AUTHORIZATION_CONTEXT.reset(token)


def test_migration_defaults_old_capabilities_open_and_filters_only_explicit_policies():
    migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations" /
        "0281_identity_scoped_capabilities.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS rvbbit.capability_access_policies" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.capability_access_grants" in migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.capability_access_events" in migration
    assert "WHEN NOT EXISTS" in migration
    assert "THEN true" in migration
    assert "t.system_key='everyone'" in migration
    assert "rvbbit.capability_search_for(NULL,q,k,kinds)" in migration
    assert "node_id,kind,schema_name,rel_name,col_name,match_score,doc,source_rank" in migration
    assert "ORDER BY n.source_rank" in migration
    assert "Capability access events are append-only" in migration
    assert '"0281_identity_scoped_capabilities"' in registry
