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
from cryptography.fernet import Fernet


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


def test_mcp_gateway_secret_request_masks_its_value(monkeypatch, tmp_path):
    gateway = _load_gateway(monkeypatch, tmp_path)

    request = gateway.SecretRequest(
        server="linear", name="LINEAR_API_KEY", value="secret-token"
    )

    assert "secret-token" not in repr(request)
    assert request.value.get_secret_value() == "secret-token"


def test_mcp_gateway_repairs_existing_legacy_key_permissions(monkeypatch, tmp_path):
    key_path = tmp_path / "mcp-secrets.bin.key"
    key_path.write_bytes(Fernet.generate_key())
    key_path.chmod(0o644)

    _load_gateway(monkeypatch, tmp_path)

    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_mcp_gateway_migrates_legacy_cache_without_overwriting_canonical(monkeypatch, tmp_path):
    gateway = _load_gateway(monkeypatch, tmp_path)
    gateway.secrets.replace({
        "linear": {"LINEAR_API_KEY": "legacy-linear"},
        "fireflies": {"FIREFLIES_API_KEY": "legacy-fireflies"},
        "github": {"GITHUB_TOKEN": "must-stay-revoked"},
    })

    class Connection:
        def __init__(self):
            self.values = {
                ("linear", "LINEAR_API_KEY"): "canonical-linear",
            }
            self.revoked = {("github", "GITHUB_TOKEN")}

        async def fetchval(self, sql, *args):
            if "to_regprocedure" in sql or "credential_key_available" in sql:
                return True
            if "set_mcp_credential" in sql:
                server, name, value = args
                self.values[(server, name)] = value
                return f"mcp/{server}/{name}"
            if "resolve_mcp_credential" in sql:
                return self.values.get((args[0], args[1]))
            raise AssertionError(f"unexpected query: {sql}")

        async def fetch(self, sql, *args):
            assert "list_credentials('mcp',NULL)" in sql
            return [
                {"server_name": server, "secret_name": name, "status": "active"}
                for server, name in self.values
            ] + [
                {"server_name": server, "secret_name": name, "status": "revoked"}
                for server, name in self.revoked
            ]

    class Acquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *_args):
            return None

    class Pool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return Acquire(self.conn)

    connection = Connection()
    gateway.db_pool = Pool(connection)

    asyncio.run(gateway._initialize_canonical_credentials())

    assert gateway.canonical_credentials is True
    assert gateway.secrets.for_server("linear") == {
        "LINEAR_API_KEY": "canonical-linear"
    }
    assert gateway.secrets.for_server("fireflies") == {
        "FIREFLIES_API_KEY": "legacy-fireflies"
    }
    assert gateway.secrets.for_server("github") == {}
    assert gateway.revoked_credentials == {("github", "GITHUB_TOKEN")}
    assert connection.values[("linear", "LINEAR_API_KEY")] == "canonical-linear"


def test_mcp_gateway_revoked_tombstone_blocks_environment_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("LINEAR_API_KEY", "environment-fallback")
    gateway = _load_gateway(monkeypatch, tmp_path)
    gateway.canonical_credentials = True
    gateway.revoked_credentials = {("linear", "LINEAR_API_KEY")}

    cfg = _config(gateway)
    cfg.name = "linear"
    cfg.transport = "http"
    cfg.auth_header_env = "LINEAR_API_KEY"

    assert gateway.resolve_auth_headers(cfg) == {}
    assert gateway.resolve_env(
        {"LINEAR_API_KEY": "${LINEAR_API_KEY}"}, "linear"
    ) == {"LINEAR_API_KEY": ""}


def test_mcp_gateway_credential_revision_check_is_atomic_and_fails_stale_plans(
    monkeypatch, tmp_path
):
    gateway = _load_gateway(monkeypatch, tmp_path)

    class Connection:
        def __init__(self, row):
            self.row = row
            self.queries = []

        async def fetchrow(self, query, *args):
            self.queries.append((query, args))
            return self.row

    exact = gateway.SecretRequest(
        server="linear",
        name="LINEAR_API_KEY",
        value="new-value",
        expected_version=3,
        expected_status="active",
    )
    connection = Connection({"version": 3, "status": "active"})
    asyncio.run(gateway._check_credential_revision(connection, exact))
    assert "FOR UPDATE" in connection.queries[0][0]

    stale = Connection({"version": 4, "status": "active"})
    with pytest.raises(gateway.HTTPException) as error:
        asyncio.run(gateway._check_credential_revision(stale, exact))
    assert error.value.status_code == 409

    create = gateway.SecretRequest(
        server="linear",
        name="NEW_TOKEN",
        value="new-value",
        expect_absent=True,
    )
    with pytest.raises(gateway.HTTPException) as error:
        asyncio.run(gateway._check_credential_revision(
            Connection({"version": 1, "status": "active"}), create
        ))
    assert error.value.status_code == 409


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
