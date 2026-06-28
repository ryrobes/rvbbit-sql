"""Sidecar-level tests for MCP gateway lifecycle bounds."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import stat
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_gateway(monkeypatch, tmp_path):
    gateway_path = (
        Path(__file__).resolve().parents[1]
        / "capabilities"
        / "templates"
        / "mcp-gateway"
        / "main.py"
    )
    monkeypatch.setenv(
        "RVBBIT_GATEWAY_SECRETS_PATH",
        str(tmp_path / "mcp-secrets.bin"),
    )
    _install_gateway_import_stubs(monkeypatch)

    module_name = f"rvbbit_mcp_gateway_test_{os.getpid()}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, gateway_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _install_gateway_import_stubs(monkeypatch):
    asyncpg = ModuleType("asyncpg")
    asyncpg.Pool = object
    monkeypatch.setitem(sys.modules, "asyncpg", asyncpg)

    mcp = ModuleType("mcp")

    class ClientSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            return None

    class StdioServerParameters:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    mcp.ClientSession = ClientSession
    mcp.StdioServerParameters = StdioServerParameters
    monkeypatch.setitem(sys.modules, "mcp", mcp)

    mcp_client = ModuleType("mcp.client")
    monkeypatch.setitem(sys.modules, "mcp.client", mcp_client)

    stdio = ModuleType("mcp.client.stdio")

    def stdio_client(_params):
        raise AssertionError("stdio_client should not be entered in unit tests")

    stdio.stdio_client = stdio_client
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio)

    streamable_http = ModuleType("mcp.client.streamable_http")

    def streamablehttp_client(_url):
        raise AssertionError("streamablehttp_client should not be entered in unit tests")

    streamable_http.streamablehttp_client = streamablehttp_client
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", streamable_http)


def _config(gateway, timeout_ms=10):
    return gateway.MCPServerConfig(
        {
            "name": "slow_server",
            "transport": "stdio",
            "command": "python",
            "args": [],
            "env": {},
            "url": None,
            "auth_header_env": None,
            "timeout_ms": timeout_ms,
        }
    )


def test_mcp_gateway_normalizes_timeout_bounds(monkeypatch, tmp_path):
    gateway = _load_gateway(monkeypatch, tmp_path)

    assert gateway._normalize_timeout_ms(None) == 30_000
    assert gateway._normalize_timeout_ms("not an int") == 30_000
    assert gateway._normalize_timeout_ms(0) == 30_000
    assert gateway._normalize_timeout_ms(-5) == 1
    assert gateway._normalize_timeout_ms(900_000) == 600_000


def test_mcp_gateway_resolves_http_auth_from_secret_store(monkeypatch, tmp_path):
    gateway = _load_gateway(monkeypatch, tmp_path)
    gateway.secrets.set("slow_server", "REMOTE_TOKEN", "secret-token")
    cfg = _config(gateway)
    cfg.transport = "http"
    cfg.auth_header_env = "REMOTE_TOKEN"

    assert gateway.resolve_auth_headers(cfg) == {
        "Authorization": "Bearer secret-token"
    }


def test_mcp_gateway_secret_store_is_owner_only(monkeypatch, tmp_path):
    gateway = _load_gateway(monkeypatch, tmp_path)
    gateway.secrets.set("server", "TOKEN", "secret-token")

    mode = stat.S_IMODE((tmp_path / "mcp-secrets.bin").stat().st_mode)

    assert mode == 0o600


def test_mcp_gateway_streamable_kwargs_uses_http_client(monkeypatch, tmp_path):
    gateway = _load_gateway(monkeypatch, tmp_path)

    def streamablehttp_client(_url, http_client=None):
        return http_client

    gateway.streamablehttp_client = streamablehttp_client

    kwargs = gateway.streamable_http_client_kwargs(
        {"Authorization": "Bearer secret-token"}
    )

    assert set(kwargs) == {"http_client"}
    assert kwargs["http_client"].headers["authorization"] == "Bearer secret-token"
    asyncio.run(kwargs["http_client"].aclose())


def test_mcp_gateway_list_tools_timeout_resets_session(monkeypatch, tmp_path):
    gateway = _load_gateway(monkeypatch, tmp_path)

    class SlowSession:
        async def list_tools(self):
            await asyncio.sleep(0.05)
            return SimpleNamespace(tools=[])

    async def run_case():
        proc = gateway.MCPServerProcess(_config(gateway, timeout_ms=1))
        proc.session = SlowSession()

        with pytest.raises(asyncio.TimeoutError):
            await proc.list_tools()

        assert proc.session is None
        assert proc._tools_cache is None

    asyncio.run(run_case())


def test_mcp_gateway_startup_timeout_resets_runner(monkeypatch, tmp_path):
    gateway = _load_gateway(monkeypatch, tmp_path)

    async def run_case():
        proc = gateway.MCPServerProcess(_config(gateway, timeout_ms=1))

        async def never_ready():
            await asyncio.sleep(0.05)

        proc._run = never_ready

        with pytest.raises(asyncio.TimeoutError):
            await proc.ensure_started()

        assert proc.session is None
        assert proc._runner is None
        assert proc._ready is None
        assert proc._shutdown is None

    asyncio.run(run_case())


def test_mcp_gateway_call_request_uses_isolated_arguments(monkeypatch, tmp_path):
    gateway = _load_gateway(monkeypatch, tmp_path)

    first = gateway.CallRequest(server="s", tool="t")
    second = gateway.CallRequest(server="s", tool="t")
    first.arguments["x"] = 1

    assert second.arguments == {}
