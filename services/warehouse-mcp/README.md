# rvbbit Warehouse MCP — Phase 0 prototype

A governed, semantic, time-travel data interface for Claude (Cowork & Code).
Design: [`docs/WAREHOUSE_MCP_PLAN.md`](../../docs/WAREHOUSE_MCP_PLAN.md) ·
tool spec: [`docs/WAREHOUSE_MCP_PHASE0.md`](../../docs/WAREHOUSE_MCP_PHASE0.md).

Standalone for now (foldable into `rvbbit-mcp-gateway` later). **Phase 0 uses one
read-only connection** — per-user role scoping is Phase 1.

## Tools
| tool | what | backing |
|---|---|---|
| `search_data(query, limit?, schema?)` | semantic search → ranked tables/cols, each grounded with **live samples + per-column stats + freshness/drift** | `data_search` + `pg_stats` + `accel_freshness` |
| `describe_table(table)` | columns + samples + per-column stats + freshness | information_schema + `pg_stats` + `accel_freshness` |
| `list_metrics(category?, search?)` / `get_metric(name)` | the blessed metric catalog | `metric_defs` |
| `metric(name, params?, as_of?, def_as_of?)` | a governed number (bitemporal) | `rvbbit.metric()` |
| `validate_sql(sql, as_of?)` | plan, **don't execute** (self-correct loop) | `route_explain` |
| `run_sql(sql, as_of?, limit?)` | **read-only** execute (validate → safe_select gate → run) | engine |

`as_of` (data-time) flows in as the engine's `-- rvbbit: as_of <ts>` directive; the
read-only guard rejects anything that isn't a `safe_select`.

## What's exposed — databases & schemas
The warehouse and rvbbit's own internals live in **one database, different schemas**
(the Temporal Mirror syncs external sources into dest schemas right next to the
`rvbbit.*` catalog). So scoping is by **schema**, not database: `search_data` and
`describe_table` always hide `rvbbit` / `pg_*` / `information_schema`, surfacing only
the data schemas. Set `WAREHOUSE_SCHEMAS` (CSV) to restrict to an explicit allowlist
(e.g. `mirror_sales,mirror_ops,analytics`). To expose data that lives in a *separate*
database, mirror it in (Temporal Mirror) — then it's covered, time-travel and all.
The hard backstop is still the DB role: don't grant `warehouse_reader` SELECT on the
`rvbbit` schema and the internals are unreadable even via `run_sql`.

## Run on the uber stack (Docker)
The image ships in the release set (`ghcr.io/<ns>/rvbbit-warehouse-mcp`) and is wired
into `docker-compose.uber.yml` behind an **opt-in `warehouse` profile** — so a plain
`make release-uber-up` / `docker compose up -d` does **not** start it (by design: it's
an internet-facing endpoint). Bring it up explicitly:
```bash
export WAREHOUSE_MCP_KEY="$(openssl rand -hex 24)"   # required — endpoint won't start without it
make warehouse-up RELEASE_VERSION=<the version you pushed>   # pulls the image, starts MCP + tunnel
make warehouse-url                                          # the public https://<…>.trycloudflare.com URL
make warehouse-down                                         # stop just these two
```
Equivalently, raw compose: `… --profile warehouse up -d` (the `--profile` flag is the
thing that's easy to forget — without it the two services are silently skipped).

## Run standalone (no Docker)
```bash
pip install -r requirements.txt
export WAREHOUSE_DSN="host=... port=5432 dbname=... user=warehouse_reader password=..."

# remote (Cowork + Code): streamable-HTTP, single shared key
export WAREHOUSE_MCP_KEY="$(openssl rand -hex 24)"   # share this with users
python server.py --http        # serves http://0.0.0.0:8765/mcp  (/health is open)

python server.py --selftest    # exercise every tool against the warehouse
python server.py               # stdio (local Claude Code only)
```

### Make it remotely reachable (no open ports, no exposed Postgres)
Run `--http` **next to the warehouse** (DB over localhost) and expose only the MCP
endpoint via a tunnel:
```bash
cloudflared tunnel --url http://localhost:8765      # → https://<random>.trycloudflare.com
# (or a named Cloudflare Tunnel / Tailscale for a stable URL)
```

## Two auth modes
The server picks the mode from `WAREHOUSE_PUBLIC_URL`:

**OAuth (recommended — Claude Desktop/Cowork's native connector).** A self-contained
OAuth 2.1 AS (`auth.py`): the SDK mounts `/authorize`/`/token`/`/register` + the
`.well-known` metadata and verifies PKCE; we supply the `/login` page (a shared
`WAREHOUSE_LOGIN_PASSWORD` + optional `WAREHOUSE_ALLOWED_EMAILS`) and HS256 JWTs. Users
just **paste the URL → log in → Allow** — no header to configure. Needs a **stable
HTTPS URL** (OAuth redirects), so terminate TLS at a proxy.
```bash
export WAREHOUSE_PUBLIC_URL="https://dwmcp.example.com"   # your stable domain
export WAREHOUSE_LOGIN_PASSWORD="$(openssl rand -hex 16)" # the shared login password
export WAREHOUSE_JWT_SECRET="$(openssl rand -hex 32)"     # MUST differ from WAREHOUSE_MCP_KEY
export WAREHOUSE_ALLOWED_EMAILS="a@co.com,b@co.com"       # optional allowlist
python server.py --http     # serves :8765; behind your proxy at WAREHOUSE_PUBLIC_URL
```
> **Security:** `WAREHOUSE_JWT_SECRET` must be independent of `WAREHOUSE_MCP_KEY` — that
> key is handed to users, and reusing it to sign would let any holder forge a token for
> any email. The server **refuses to start** if they match or if either secret/password
> is missing. Login is rate-limited (per-IP lockout + serialized checks).

**Shared key (Claude Code / scripts).** No `WAREHOUSE_PUBLIC_URL`; gate on a static
bearer. Still accepted in OAuth mode too, so Code keeps working alongside the UI flow.

### nginx (terminate TLS, forward all paths to `127.0.0.1:8765`)
```nginx
server {
  listen 443 ssl;
  server_name dwmcp.example.com;
  # ssl_certificate ... (e.g. certbot)
  location / {                       # /mcp, /authorize, /token, /register, /.well-known/*, /login
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;     # the server rate-limits per this IP
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;             # streamable-HTTP / SSE
    proxy_read_timeout 3600s;
  }
}
```

### Connect Claude
- **Claude Desktop / Cowork (OAuth):** Settings → Connectors → **Add custom connector** →
  URL `https://dwmcp.example.com/mcp` → it opens the login page → enter email + the shared
  password → **Allow**. No header.
- **Claude Code (either mode):** `claude mcp add --transport http rvbbit-warehouse <url>/mcp --header "Authorization: Bearer $WAREHOUSE_MCP_KEY"`

## Config (env)
`WAREHOUSE_DSN` · `RVBBIT_CATALOG_GRAPH` (default `db_catalog`) ·
`WAREHOUSE_SCHEMAS` (CSV allowlist; default = all but rvbbit/pg_*) ·
`WAREHOUSE_ROW_CAP` (1000) · `WAREHOUSE_STMT_TIMEOUT_MS` (30000) ·
`WAREHOUSE_MCP_HOST` (0.0.0.0) · `WAREHOUSE_MCP_PORT` (8765)
**OAuth mode:** `WAREHOUSE_PUBLIC_URL` (enables it) · `WAREHOUSE_LOGIN_PASSWORD` (req) ·
`WAREHOUSE_JWT_SECRET` (req, ≠ MCP_KEY) · `WAREHOUSE_ALLOWED_EMAILS` (opt) ·
`WAREHOUSE_ACCESS_TTL` (3600) · `WAREHOUSE_REFRESH_TTL` (30d).
**Shared-key mode:** `WAREHOUSE_MCP_KEY` (bearer; unset = auth OFF, dev only).

## Deferred to Phase 1+
Per-user identity → scoped role (tools run as the *caller's* scope), PII masking in
samples, `ask` (compose text-to-SQL), per-role cost caps, receipts table,
`define_metric`/`get_connection` (promote + scoped runtime DSN).
