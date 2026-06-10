"""MCP gateway — bridges the rvbbit pg extension to MCP servers.

Why a sidecar: PG backends can't safely fork long-lived subprocesses (one
per backend wastes memory; orphans on backend crash). This gateway holds a
singleton subprocess per registered MCP server, shared across every PG
backend. PG backends only ever talk HTTP to this process.

Architecture:
- Server configs live in `rvbbit.mcp_servers` (the source of truth). The
  gateway reads them via libpq on demand.
- One `MCPServerProcess` per server, lazy-spawned on first call, held in a
  pool. A per-server asyncio.Lock serializes JSON-RPC calls.
- On crash / timeout / refresh, the subprocess is reset; next call respawns.

Endpoints:
  POST /call            — body {server, tool, arguments}     -> tool result
  POST /refresh/{name}  — re-introspect tools, drop cached client
  POST /drop/{name}     — drop cached client (config stays in DB)
  GET  /tools/{name}    — list tools (cached after first list)
  GET  /health          — gateway liveness
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from typing import Any

import asyncpg
from fastapi import FastAPI, Header, HTTPException
from pydantic import AnyUrl, BaseModel

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

try:
    from mcp.client.streamable_http import streamablehttp_client

    HAS_HTTP_TRANSPORT = True
except ImportError:  # SDK without streamable-http
    HAS_HTTP_TRANSPORT = False


log = logging.getLogger("mcp-gateway")
logging.basicConfig(level=logging.INFO)

app = FastAPI()

DSN = os.environ.get(
    "RVBBIT_DSN", "postgresql://postgres:rvbbit@postgres:5432/rvbbit"
)

pool: dict[str, "MCPServerProcess"] = {}
pool_lock = asyncio.Lock()
db_pool: asyncpg.Pool | None = None


# ---- env substitution -----------------------------------------------------


_ENV_REF = re.compile(r"\$\{(\w+)\}")


# ---- secret store ---------------------------------------------------------
#
# Install-time secrets (API keys entered in the UI) are POSTed to /secrets and
# held HERE, by the gateway — never persisted in Postgres, which only stores
# ${VAR} references in mcp_servers.env. resolve_env() checks this store
# (scoped per server) BEFORE the gateway's own process env, so a server
# registered with env {"GITHUB_TOKEN": "${GITHUB_TOKEN}"} picks up whatever the
# installer entered. Encrypted at rest with Fernet when `cryptography` + a key
# are available; degrades to plaintext-in-file (gateway is already an isolation
# boundary) with a warning otherwise.

SECRETS_PATH = os.environ.get("RVBBIT_GATEWAY_SECRETS_PATH", "/app/data/mcp-secrets.bin")
GATEWAY_TOKEN = os.environ.get("RVBBIT_GATEWAY_TOKEN") or None

try:
    from cryptography.fernet import Fernet
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


def _load_fernet():
    if not _HAS_CRYPTO:
        log.warning("mcp-gateway: `cryptography` not installed; secret store is UNENCRYPTED")
        return None
    key = os.environ.get("RVBBIT_GATEWAY_SECRET_KEY")
    if key:
        return Fernet(key.encode() if isinstance(key, str) else key)
    # No explicit key: generate + persist one beside the store so restarts can
    # still decrypt. A mounted/explicit RVBBIT_GATEWAY_SECRET_KEY is recommended
    # for production (a rebuilt container without a volume loses the key file).
    key_path = SECRETS_PATH + ".key"
    try:
        if os.path.exists(key_path):
            with open(key_path, "rb") as f:
                return Fernet(f.read().strip())
        os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)
        k = Fernet.generate_key()
        with open(key_path, "wb") as f:
            f.write(k)
        log.warning("mcp-gateway: generated a secret-store key at %s; set "
                    "RVBBIT_GATEWAY_SECRET_KEY (mounted) for durable production use", key_path)
        return Fernet(k)
    except Exception as e:
        log.warning("mcp-gateway: encryption setup failed (%s); secrets UNENCRYPTED", e)
        return None


class SecretStore:
    def __init__(self):
        self._data: dict[str, dict[str, str]] = {}
        self._fernet = _load_fernet()
        self._load()

    def _load(self):
        try:
            if not os.path.exists(SECRETS_PATH):
                return
            with open(SECRETS_PATH, "rb") as f:
                raw = f.read()
            if not raw:
                return
            if self._fernet is not None:
                raw = self._fernet.decrypt(raw)
            self._data = json.loads(raw.decode("utf-8"))
            log.info("mcp-gateway: loaded secrets for %d server(s)", len(self._data))
        except Exception as e:
            log.warning("mcp-gateway: could not load secret store (%s); starting empty", e)
            self._data = {}

    def _persist(self):
        try:
            os.makedirs(os.path.dirname(SECRETS_PATH) or ".", exist_ok=True)
            raw = json.dumps(self._data).encode("utf-8")
            if self._fernet is not None:
                raw = self._fernet.encrypt(raw)
            tmp = SECRETS_PATH + ".tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, SECRETS_PATH)
        except Exception as e:
            log.warning("mcp-gateway: could not persist secret store: %s", e)

    def for_server(self, server: str) -> dict[str, str]:
        return dict(self._data.get(server, {}))

    def set(self, server: str, name: str, value: str):
        self._data.setdefault(server, {})[name] = value
        self._persist()

    def delete(self, server: str, name: str):
        if name in self._data.get(server, {}):
            del self._data[server][name]
            if not self._data[server]:
                del self._data[server]
            self._persist()

    def names(self, server: str) -> list[str]:
        return sorted(self._data.get(server, {}).keys())


secrets = SecretStore()


def resolve_env(env_template: dict[str, Any] | None, server_name: str | None = None) -> dict[str, str]:
    """Expand ${VAR} refs in env values. Resolution order, highest first:
    the per-server secret store (UI-entered keys), then the gateway's own
    process env. Keys never round-trip through Postgres.
    """
    if not env_template:
        return {}
    store = secrets.for_server(server_name) if server_name else {}

    def _sub(m):
        var = m.group(1)
        return store[var] if var in store else os.environ.get(var, "")

    out: dict[str, str] = {}
    for k, v in env_template.items():
        if isinstance(v, str):
            v = _ENV_REF.sub(_sub, v)
        out[k] = str(v)
    return out


# ---- DB config fetch ------------------------------------------------------


class MCPServerConfig:
    __slots__ = (
        "name",
        "transport",
        "command",
        "args",
        "env",
        "url",
        "auth_header_env",
        "timeout_ms",
    )

    def __init__(self, row):
        self.name = row["name"]
        self.transport = row["transport"]
        self.command = row["command"]
        self.args = list(row["args"] or [])
        self.env = row["env"] or {}
        if isinstance(self.env, str):
            self.env = json.loads(self.env)
        self.url = row["url"]
        self.auth_header_env = row["auth_header_env"]
        self.timeout_ms = row["timeout_ms"]


async def fetch_config(server_name: str) -> MCPServerConfig:
    if db_pool is None:
        raise HTTPException(503, "gateway db pool not initialized")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, transport, command, args, env, url, "
            "       auth_header_env, timeout_ms "
            "FROM rvbbit.mcp_servers WHERE name = $1",
            server_name,
        )
    if not row:
        raise HTTPException(404, f"mcp server '{server_name}' not registered")
    return MCPServerConfig(row)


# ---- the per-server client ------------------------------------------------


class MCPServerProcess:
    """Wraps one MCP server's client session + per-server lock + lifecycle."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.lock = asyncio.Lock()
        self.session: ClientSession | None = None
        self._tools_cache: list | None = None
        # Lifecycle of the session's async-context stack is owned by a single
        # dedicated task (`_run`); see the comment there for why.
        self._runner: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._shutdown: asyncio.Event | None = None
        self._error: BaseException | None = None

    async def _run(self) -> None:
        # Own the session's async-context lifecycle ENTIRELY within this one
        # task — enter AND exit here, never from a caller's task.
        #
        # The MCP SDK builds on anyio cancel scopes, which may only be exited
        # from the same task that entered them. The previous design entered the
        # stdio_client/ClientSession stack inside whichever HTTP-request task
        # first touched the server, then closed it from a *different* request
        # task during /refresh or an error reset. That cross-task teardown
        # fails to cancel the subprocess reader task, which is then orphaned and
        # busy-loops on the EOF pipe forever — pinning a full vCPU even while
        # the gateway is otherwise idle. (Reproduced: a single /refresh ->
        # permanent 100% CPU.) Doing enter+exit in this one task makes the
        # cancel scopes unwind cleanly.
        stack = AsyncExitStack()
        try:
            if self.config.transport == "stdio":
                params = StdioServerParameters(
                    command=self.config.command,
                    args=self.config.args,
                    env={**os.environ, **resolve_env(self.config.env, self.config.name)},
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            elif self.config.transport == "http":
                if not HAS_HTTP_TRANSPORT:
                    raise RuntimeError(
                        "HTTP MCP transport not available; install a newer mcp SDK"
                    )
                ctx = await stack.enter_async_context(
                    streamablehttp_client(self.config.url)
                )
                # streamablehttp_client yields (read, write, get_session_id)
                read, write = ctx[0], ctx[1]
            else:
                raise ValueError(f"unknown transport: {self.config.transport!r}")
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.session = session
            log.info("started mcp server %r (transport=%s)",
                     self.config.name, self.config.transport)
        except BaseException as e:
            # Startup failed — report it to ensure_started() and unwind here.
            self._error = e
            if self._ready is not None:
                self._ready.set()
            try:
                await stack.aclose()
            except Exception:
                pass
            return
        # Ready; park until reset()/shutdown asks us to tear down. The aclose()
        # in the finally runs in THIS task, so teardown is clean.
        if self._ready is not None:
            self._ready.set()
        try:
            if self._shutdown is not None:
                await self._shutdown.wait()
        finally:
            self.session = None
            try:
                await stack.aclose()
            except Exception as e:
                log.warning("error closing mcp server %r: %s",
                            self.config.name, e)

    async def ensure_started(self) -> None:
        if self.session is not None:
            return
        self._error = None
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._runner = asyncio.create_task(self._run())
        await self._ready.wait()
        if self._error is not None:
            err = self._error
            await self._reset_locked()
            raise err

    async def call_tool(self, name: str, arguments: dict) -> Any:
        async with self.lock:
            await self.ensure_started()
            try:
                return await asyncio.wait_for(
                    self.session.call_tool(name, arguments),
                    timeout=self.config.timeout_ms / 1000.0,
                )
            except Exception:
                # subprocess may be in a bad state — reset so the next
                # caller respawns it cleanly.
                await self._reset_locked()
                raise

    async def list_tools(self) -> list:
        async with self.lock:
            await self.ensure_started()
            if self._tools_cache is None:
                result = await self.session.list_tools()
                self._tools_cache = list(result.tools)
            return self._tools_cache

    async def list_resources(self) -> list:
        """List static resources. Servers that don't support resources
        return an empty list (we silently swallow the error)."""
        async with self.lock:
            await self.ensure_started()
            try:
                result = await self.session.list_resources()
                return list(getattr(result, "resources", []))
            except Exception:
                return []

    async def read_resource(self, uri: str) -> Any:
        async with self.lock:
            await self.ensure_started()
            try:
                return await asyncio.wait_for(
                    self.session.read_resource(AnyUrl(uri)),
                    timeout=self.config.timeout_ms / 1000.0,
                )
            except Exception:
                await self._reset_locked()
                raise

    async def reset(self) -> None:
        async with self.lock:
            await self._reset_locked()

    async def _reset_locked(self) -> None:
        self._tools_cache = None
        self.session = None
        runner = self._runner
        if runner is not None and not runner.done():
            # Ask _run to unwind, then wait for it to tear down in its own task.
            if self._shutdown is not None:
                self._shutdown.set()
            try:
                await asyncio.wait_for(runner, timeout=15)
            except asyncio.TimeoutError:
                # wait_for already cancelled the task; log and move on.
                log.warning("mcp server %r teardown timed out", self.config.name)
            except BaseException:
                pass
        self._runner = None
        self._ready = None
        self._shutdown = None


async def get_server(name: str) -> MCPServerProcess:
    async with pool_lock:
        if name in pool:
            return pool[name]
        cfg = await fetch_config(name)
        proc = MCPServerProcess(cfg)
        pool[name] = proc
        return proc


async def evict_server(name: str) -> None:
    async with pool_lock:
        proc = pool.pop(name, None)
    if proc is not None:
        await proc.reset()


# ---- HTTP API -------------------------------------------------------------


class CallRequest(BaseModel):
    server: str
    tool: str
    arguments: dict = {}


def _serialize_content(c: Any) -> dict:
    """Convert an MCP content block to a JSON-friendly dict."""
    out: dict[str, Any] = {}
    t = getattr(c, "type", None)
    if t is not None:
        out["type"] = t
    text = getattr(c, "text", None)
    if text is not None:
        out["text"] = text
    data = getattr(c, "data", None)
    if data is not None:
        out["data"] = data
    mime = getattr(c, "mimeType", None)
    if mime is not None:
        out["mimeType"] = mime
    if not out:
        # Fallback: best-effort dump.
        out = {"type": "unknown", "repr": repr(c)}
    return out


@app.post("/call")
async def call(req: CallRequest):
    proc = await get_server(req.server)
    try:
        result = await proc.call_tool(req.tool, req.arguments)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"tool '{req.tool}' on '{req.server}' timed out")
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    return {
        "content": [_serialize_content(c) for c in result.content],
        "isError": bool(getattr(result, "isError", False)),
    }


def _serialize_tool(t: Any) -> dict:
    return {
        "name": t.name,
        "description": getattr(t, "description", None),
        "input_schema": getattr(t, "inputSchema", {}) or {},
    }


def _serialize_resource(r: Any) -> dict:
    return {
        "uri": str(getattr(r, "uri", "")),
        "name": getattr(r, "name", None),
        "description": getattr(r, "description", None),
        "mime_type": getattr(r, "mimeType", None),
    }


def _serialize_resource_content(c: Any) -> dict:
    out: dict[str, Any] = {}
    uri = getattr(c, "uri", None)
    if uri is not None:
        out["uri"] = str(uri)
    mime = getattr(c, "mimeType", None)
    if mime is not None:
        out["mimeType"] = mime
    text = getattr(c, "text", None)
    if text is not None:
        out["text"] = text
    blob = getattr(c, "blob", None)
    if blob is not None:
        out["blob"] = blob
    return out


class ReadResourceRequest(BaseModel):
    server: str
    uri: str


@app.post("/refresh/{server}")
async def refresh(server: str):
    # Drop any cached subprocess + tools cache, then re-list everything.
    await evict_server(server)
    proc = await get_server(server)
    try:
        tools = await proc.list_tools()
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    # Resources may not be supported — silent fall-through to [].
    resources = await proc.list_resources()
    return {
        "tools": [_serialize_tool(t) for t in tools],
        "resources": [_serialize_resource(r) for r in resources],
    }


@app.post("/drop/{server}")
async def drop(server: str):
    await evict_server(server)
    return {"dropped": server}


@app.get("/tools/{server}")
async def get_tools(server: str):
    proc = await get_server(server)
    try:
        tools = await proc.list_tools()
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    return {"tools": [_serialize_tool(t) for t in tools]}


@app.post("/resource")
async def read_resource(req: ReadResourceRequest):
    proc = await get_server(req.server)
    try:
        result = await proc.read_resource(req.uri)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"resource '{req.uri}' on '{req.server}' timed out")
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    return {"contents": [_serialize_resource_content(c) for c in result.contents]}


@app.post("/probe/{server}")
async def probe(server: str):
    """Active health probe — forces tools/list, returns latency + result.
    A reachable server returns reachable=true even if no tools (degenerate
    but valid)."""
    import time as _time
    try:
        proc = await get_server(server)
    except HTTPException:
        raise
    t0 = _time.monotonic()
    try:
        tools = await proc.list_tools()
        latency_ms = int((_time.monotonic() - t0) * 1000)
        return {
            "reachable": True,
            "latency_ms": latency_ms,
            "n_tools": len(tools),
            "error": None,
        }
    except Exception as e:
        latency_ms = int((_time.monotonic() - t0) * 1000)
        return {
            "reachable": False,
            "latency_ms": latency_ms,
            "n_tools": 0,
            "error": f"{type(e).__name__}: {e}",
        }


# ---- secrets API ----------------------------------------------------------
#
# The install UI pushes API keys here (not to Postgres). Guarded by a shared
# bearer token when RVBBIT_GATEWAY_TOKEN is set; open (with a log note) in dev.


def _check_token(authorization: str | None) -> None:
    if GATEWAY_TOKEN is None:
        return
    if authorization != f"Bearer {GATEWAY_TOKEN}":
        raise HTTPException(401, "invalid or missing gateway token")


class SecretRequest(BaseModel):
    server: str
    name: str
    value: str


class SecretRef(BaseModel):
    server: str
    name: str


@app.post("/secrets")
async def set_secret(req: SecretRequest, authorization: str | None = Header(default=None)):
    _check_token(authorization)
    secrets.set(req.server, req.name, req.value)
    # Respawn on next call so the new secret is picked up.
    await evict_server(req.server)
    return {"ok": True, "server": req.server, "name": req.name}


@app.delete("/secrets")
async def delete_secret(req: SecretRef, authorization: str | None = Header(default=None)):
    _check_token(authorization)
    secrets.delete(req.server, req.name)
    await evict_server(req.server)
    return {"ok": True, "server": req.server, "name": req.name}


@app.get("/secrets/{server}")
async def secret_status(server: str, authorization: str | None = Header(default=None)):
    """Which secret names are set for a server (values are never returned)."""
    _check_token(authorization)
    return {"server": server, "set": secrets.names(server)}


@app.get("/health")
async def health():
    return {"status": "ok", "servers_loaded": sorted(pool.keys())}


# ---- lifecycle ------------------------------------------------------------


async def _init_codecs(conn):
    # asyncpg returns jsonb as raw bytes/str unless a codec is set; we want
    # native dicts so MCPServerConfig.__init__ can read fields directly.
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


@app.on_event("startup")
async def startup():
    global db_pool
    # pg-rvbbit may not be ready yet when the gateway starts; retry briefly.
    for attempt in range(60):
        try:
            db_pool = await asyncpg.create_pool(
                DSN, min_size=1, max_size=4, init=_init_codecs
            )
            break
        except Exception as e:
            log.info("waiting for rvbbit DB (attempt %d): %s", attempt + 1, e)
            await asyncio.sleep(1)
    if db_pool is None:
        raise RuntimeError(f"could not connect to {DSN} after 60 attempts")
    log.info("mcp-gateway ready on :9180")


@app.on_event("shutdown")
async def shutdown():
    async with pool_lock:
        procs = list(pool.values())
        pool.clear()
    for proc in procs:
        await proc.reset()
    if db_pool is not None:
        await db_pool.close()
