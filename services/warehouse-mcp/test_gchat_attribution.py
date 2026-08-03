"""Verified Google Chat identity is attribution, never authorization."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP


_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import server  # noqa: E402


def _context(email="analyst@example.com", *, platform="google_chat"):
    metadata = {
        server._HERMES_CALLER_META_KEY: {
            "source": "hermes",
            "platform": platform,
            "user_id": email,
            "session_id": "gchat:spaces/abc:analyst@example.com",
        }
    }
    return SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(root=SimpleNamespace(params={"_meta": metadata}))
        )
    )


def test_static_hermes_publication_uses_verified_gchat_email(monkeypatch):
    monkeypatch.setattr(server, "_authenticated_caller", lambda: ("calliope@example.com", "static-key"))
    monkeypatch.setattr(server, "_logged", lambda _tool, _args, fn: fn())
    monkeypatch.setattr(
        server,
        "tool_publish_dashboard",
        lambda *_args, **_kwargs: {"caller": server._caller()},
    )

    result = server._mcp_publish_dashboard(
        "Chat-created dashboard", html="<main>ok</main>", ctx=_context("Person@Example.com")
    )

    assert result["caller"] == ("person@example.com", "static-key")
    assert server._FORWARDED_CALLER.get() is None


def test_forwarded_identity_never_overrides_oauth_caller(monkeypatch):
    monkeypatch.setattr(server, "_authenticated_caller", lambda: ("oauth@example.com", "oauth-client"))

    observed = server._with_forwarded_mcp_caller(
        _context("other@example.com"), server._caller
    )

    assert observed == ("oauth@example.com", "oauth-client")


def test_non_gchat_or_non_email_metadata_is_ignored(monkeypatch):
    monkeypatch.setattr(server, "_authenticated_caller", lambda: ("calliope@example.com", "static-key"))

    assert server._forwarded_mcp_caller(_context("not-an-email")) is None
    assert server._forwarded_mcp_caller(
        _context("person@example.com", platform="slack")
    ) is None


def test_real_mcp_meta_shape_is_read_and_context_stays_out_of_tool_schemas(monkeypatch):
    metadata = {
        server._HERMES_CALLER_META_KEY: {
            "source": "hermes",
            "platform": "google_chat",
            "user_id": "person@example.com",
        }
    }
    params = mcp_types.CallToolRequestParams(
        name="publish_dashboard", arguments={"name": "Daily"}, **{"_meta": metadata}
    )
    context = SimpleNamespace(request_context=SimpleNamespace(
        meta=params.meta,
        request=mcp_types.ClientRequest(
            mcp_types.CallToolRequest(method="tools/call", params=params)
        ),
    ))
    monkeypatch.setattr(server, "_authenticated_caller", lambda: ("calliope@example.com", "static-key"))

    assert server._forwarded_mcp_caller(context) == "person@example.com"

    mcp = FastMCP("attribution-schema")
    server._register(mcp)
    tools = asyncio.run(mcp.list_tools())
    schemas = {
        tool.name: set((tool.inputSchema.get("properties") or {}).keys())
        for tool in tools
    }
    for name in (
        "upload_artifact", "publish_dashboard", "update_dashboard",
        "create_live_app", "update_live_app",
    ):
        assert "ctx" not in schemas[name]
