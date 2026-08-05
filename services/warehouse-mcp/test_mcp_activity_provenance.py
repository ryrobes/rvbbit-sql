"""Activity receipts distinguish first-party surfaces from direct MCP clients."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import auth  # noqa: E402
import server  # noqa: E402


def test_google_chat_and_direct_static_clients_have_distinct_channels(monkeypatch):
    monkeypatch.setattr(server, "_mcp_client_implementation", lambda: {
        "name": "hermes-agent", "version": "0.19.0"
    })
    monkeypatch.setattr(server, "_hermes_mcp_envelope", lambda: {
        "source": "hermes",
        "platform": "google_chat",
        "session_id": "spaces/AAA:thread:person@example.com",
        "user_id": "person@example.com",
    })

    chat = server._initial_activity_context(
        "run_sql", {"sql": "select 1"}, "static-key"
    )

    assert chat["channel"] == "google_chat"
    assert chat["client_app"] == "hermes"
    assert chat["session_ref"] == "spaces/AAA:thread:person@example.com"
    assert chat["provenance"]["identity_forwarded"] is True

    monkeypatch.setattr(server, "_hermes_mcp_envelope", lambda: None)
    monkeypatch.setattr(server, "_mcp_client_implementation", lambda: {
        "name": "claude-code", "title": "Claude Code", "version": "2.1.0"
    })

    direct = server._initial_activity_context(
        "run_sql", {"sql": "select 1"}, "static-key"
    )

    assert direct["channel"] == "direct_mcp"
    assert direct["client_app"] == "Claude Code"
    assert direct["provenance"]["auth"] == "static_key"


def test_native_browser_activity_is_labeled_by_surface(monkeypatch):
    monkeypatch.setattr(server, "_mcp_client_implementation", lambda: {})

    gallery = server._initial_activity_context(
        "artifact_view",
        {"surface": "gallery", "slug": "five-year-plan"},
        None,
        caller_override="person@example.com",
    )

    assert gallery == {
        "channel": "web",
        "client_app": "gallery",
        "session_ref": None,
        "provenance": {"source": "browser_session", "surface": "gallery"},
    }

    embedded_query = server._initial_activity_context(
        "dashboard_query",
        {"dashboard": "five-year-plan", "origin": "calliope"},
        None,
        caller_override="person@example.com",
    )
    native_pivot = server._initial_activity_context(
        "cube_pivot",
        {"cube": "sales", "origin": "calliope_cube"},
        None,
        caller_override="person@example.com",
    )

    assert embedded_query["channel"] == "web"
    assert embedded_query["client_app"] == "calliope"
    assert embedded_query["provenance"]["surface"] == "calliope"
    assert native_pivot["client_app"] == "calliope"


def test_dashboard_query_origin_accepts_calliope_but_rejects_unknown_values():
    assert server._dashboard_query_origin("calliope") == "calliope"
    assert server._dashboard_query_origin("  ARTIFACT-LENS ") == "artifact-lens"
    assert server._dashboard_query_origin("untrusted-client") == "dashboard"


def test_calliope_cube_pivot_receipt_keeps_its_native_surface(monkeypatch):
    recorded = {}
    monkeypatch.setattr(
        server,
        "tool_cube_pivot",
        lambda *_args, **_kwargs: {"cube": "sales", "matrix": []},
    )
    monkeypatch.setattr(
        server,
        "_record",
        lambda tool, args, *_rest, **kwargs: recorded.update(
            tool=tool,
            args=args,
            caller=kwargs.get("caller_override"),
        ),
    )

    result = server._calliope_cube_pivot(
        "sales", ["region"], [], "revenue", "sum", [],
        "person@example.com", "person@example.com",
    )

    assert result["cube"] == "sales"
    assert recorded["tool"] == "cube_pivot"
    assert recorded["args"]["origin"] == "calliope_cube"
    assert recorded["caller"] == "person@example.com"


def test_calliope_session_mapping_restores_owner_and_automation_kind(monkeypatch):
    linked = {
        "owner": "owner@example.com",
        "calliope_session_id": "755303d5-7f91-42c3-bbea-b989e34a56c9",
        "kind": "workflow",
        "trigger_kind": "scheduled",
        "status": "running",
    }
    monkeypatch.setattr(
        server,
        "_calliope_activity_for_hermes_session",
        lambda _conn, session_ref: linked if session_ref == "calliope_run_123" else None,
    )
    context = {
        "channel": "hermes",
        "client_app": "hermes_api",
        "session_ref": "calliope_run_123",
        "provenance": {"source": "hermes", "platform": "api_server"},
    }

    caller, resolved = server._resolve_activity_context(
        object(), context, "calliope@example.com", "static-key", tool="run_sql"
    )

    assert caller == "owner@example.com"
    assert resolved["channel"] == "automation"
    assert resolved["client_app"] == "calliope_workflow"
    assert resolved["provenance"]["trigger_kind"] == "scheduled"

    oauth_caller, _resolved = server._resolve_activity_context(
        object(), context, "signed@example.com", "oauth-client", tool="run_sql"
    )
    assert oauth_caller == "signed@example.com"

    linked["status"] = "complete"
    _caller, follow_up = server._resolve_activity_context(
        object(), context, "calliope@example.com", "static-key", tool="run_sql"
    )
    assert follow_up["channel"] == "web"
    _caller, finishing_receipt = server._resolve_activity_context(
        object(), context, "calliope@example.com", "static-key",
        tool="finish_calliope_workflow_run",
    )
    assert finishing_receipt["channel"] == "automation"


def test_oauth_registration_enriches_direct_client_without_exposing_secrets(monkeypatch):
    provider = object.__new__(auth.WarehouseAuthProvider)
    provider._clients = {
        "oauth-client": SimpleNamespace(
            client_name="Codex",
            software_id="https://openai.com/codex",
            software_version="1.2.3",
            client_secret="must-not-leak",
            redirect_uris=["https://secret.example/callback"],
        )
    }
    monkeypatch.setattr(server, "_ACTIVITY_AUTH_PROVIDER", provider)
    monkeypatch.setattr(server, "_hermes_mcp_envelope", lambda: None)
    monkeypatch.setattr(server, "_mcp_client_implementation", lambda: {
        "name": "codex-mcp-client", "version": "1.2.3"
    })

    context = server._initial_activity_context("search_data", {}, "oauth-client")
    caller, resolved = server._resolve_activity_context(
        object(), context, "person@example.com", "oauth-client"
    )

    assert caller == "person@example.com"
    assert resolved["channel"] == "direct_mcp"
    assert resolved["client_app"] == "Codex"
    encoded = json.dumps(resolved["provenance"])
    assert "must-not-leak" not in encoded
    assert "secret.example" not in encoded
    assert resolved["provenance"]["oauth_client"] == {
        "client_name": "Codex",
        "software_id": "https://openai.com/codex",
        "software_version": "1.2.3",
    }


def test_legacy_generic_static_client_remains_explicitly_ambiguous(monkeypatch):
    monkeypatch.setattr(server, "_hermes_mcp_envelope", lambda: None)
    monkeypatch.setattr(server, "_mcp_client_implementation", lambda: {
        "name": "mcp", "version": "0.1.0"
    })

    context = server._initial_activity_context("list_dashboards", {}, "static-key")

    assert context["channel"] == "unknown"
    assert context["client_app"] == "static_key"
    assert "ambiguous" in context["provenance"]["note"]


def test_activity_insert_falls_back_when_additive_columns_are_not_available(monkeypatch):
    class Connection:
        def __init__(self):
            self.inserts = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            if query.startswith("INSERT INTO"):
                self.inserts.append((query, params))
                if "channel,client_app" in query:
                    raise server.psycopg.errors.UndefinedColumn("legacy table")
            return SimpleNamespace()

    connection = Connection()
    monkeypatch.setattr(server, "_conn", lambda: connection)
    monkeypatch.setattr(
        server, "_caller", lambda: ("person@example.com", "oauth-client")
    )
    monkeypatch.setattr(
        server,
        "_initial_activity_context",
        lambda *_args, **_kwargs: {
            "channel": "direct_mcp",
            "client_app": "Codex",
            "session_ref": None,
            "provenance": {"source": "direct_mcp"},
        },
    )
    monkeypatch.setattr(
        server,
        "_resolve_activity_context",
        lambda _conn, context, caller, _client_id, **_kwargs: (caller, context),
    )

    server._record("list_dashboards", {}, {"dashboards": []}, None, 4)

    assert len(connection.inserts) == 2
    assert "channel,client_app" in connection.inserts[0][0]
    assert "channel,client_app" not in connection.inserts[1][0]
