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

### Connect Claude
- **Claude Code:** `claude mcp add --transport http rvbbit-warehouse <url>/mcp --header "Authorization: Bearer $WAREHOUSE_MCP_KEY"`
- **Claude Cowork / claude.ai:** add a **custom connector** → URL `<url>/mcp`, header `Authorization: Bearer <key>`.

Non-tech users just paste the URL + key once. Revoke = rotate `WAREHOUSE_MCP_KEY`
(per-user keys via an `mcp_api_keys` table are Phase 1).

## Config (env)
`WAREHOUSE_DSN` · `RVBBIT_CATALOG_GRAPH` (default `db_catalog`) ·
`WAREHOUSE_SCHEMAS` (CSV allowlist; default = all but rvbbit/pg_*) ·
`WAREHOUSE_ROW_CAP` (1000) · `WAREHOUSE_STMT_TIMEOUT_MS` (30000) ·
`WAREHOUSE_MCP_KEY` (shared bearer key; unset = auth OFF, dev only) ·
`WAREHOUSE_MCP_HOST` (0.0.0.0) · `WAREHOUSE_MCP_PORT` (8765)

## Deferred to Phase 1+
Per-user identity → scoped role (tools run as the *caller's* scope), PII masking in
samples, `ask` (compose text-to-SQL), per-role cost caps, receipts table,
`define_metric`/`get_connection` (promote + scoped runtime DSN).
