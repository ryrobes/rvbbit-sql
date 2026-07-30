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
| `metric(name, params?, as_of?, def_as_of?)` | a governed scalar number (bitemporal) | `rvbbit.metric_scalar()` |
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

### Google Sign-In (optional, recommended)
Adds a **Sign in with Google** button to the login page. We stay the OAuth *server*
for Claude and additionally become an OAuth *client* to Google; both paths converge on
one place (`_finish_login`), so a Google-verified address reaches the access token's
`sub` — and therefore `mcp_activity.caller` — exactly like a typed one. It gates the
MCP endpoint **and** everything served behind the same session (`/`, `/d/<slug>`,
`/apps/<slug>`).

This is an identity upgrade, not just convenience: with a shared password the email is
*self-asserted*, so any password-holder can claim any name in your audit log. Google's
`email_verified` claim ends that.

```bash
export WAREHOUSE_GOOGLE_CLIENT_ID="....apps.googleusercontent.com"
export WAREHOUSE_GOOGLE_CLIENT_SECRET="..."
export WAREHOUSE_GOOGLE_HD="acme.com"    # restrict to one Workspace domain
# export WAREHOUSE_GOOGLE_ONLY=1         # later: retire the shared password
```
In the Google Cloud console create an **OAuth client ID → Web application** and register
the redirect URI **exactly**: `<WAREHOUSE_PUBLIC_URL>/auth/google/callback`. (That's a
*web* client — not a service-account JSON, which is a different credential with no human
sign-in. It needs a stable origin, so the ephemeral `warehouse-tunnel-up` quick-tunnel
URL can't be used with Google.)

> **Restricting to one domain — read this.** Passing `hd=` on the authorization request
> is only an account-chooser *hint*; a user can edit it out of the URL, so it is **not**
> a security boundary. The gate is the signed **`hd` claim** on the returned ID token,
> which `WAREHOUSE_GOOGLE_HD` verifies server-side. It's strictly stronger than matching
> the email's suffix — a consumer Google account cannot present an `hd` at all.
> `WAREHOUSE_ALLOWED_EMAILS` still applies on top (exact addresses or `@domain`).
> The server **refuses to start** with Google enabled and *neither* `WAREHOUSE_GOOGLE_HD`
> nor `WAREHOUSE_ALLOWED_EMAILS` set — that combination would let any Google account on
> earth sign in.

The shared password keeps working alongside it (existing users are unaffected) until you
set `WAREHOUSE_GOOGLE_ONLY=1`, which stops `POST /login` from accepting a password at all
— not merely hiding the form. ID tokens are verified, never just decoded: RS256 against
Google's JWKS, audience, issuer, expiry, and a server-planted single-use `nonce`.

### Burrow + Google: one door, Postgres still decides
With `WAREHOUSE_AUTH=pg`, Google proves **who** you are and Postgres still decides **what**
you may touch. A verified identity is resolved to a role by `rvbbit.resolve_identity()`
(migration 0221), in this order:

1. an enabled `rvbbit.identity_map` row (`identity` → `role_name`) — the DBA's escape hatch;
2. **the email IS a role** — Azure Entra and Cloud SQL IAM both name database roles after the
   principal, so `CREATE ROLE "ryan@acme.com" LOGIN` needs no mapping at all;
3. nothing matches → **`rvbbit_guest`**.

That third case is a state neither system expresses alone: *OAuth says yes, the database says
it can't place this user.* It isn't an error — it's someone who needs provisioning. They get
the artifact index's **access-pending** page (no artifacts, no titles, no DataRabbit link — an
unmapped session would fail every query it made), and they're recorded in
**`rvbbit.identity_pending`** so there's a queue to work from:

```sql
SELECT * FROM rvbbit.identity_pending;                 -- who's waiting (role_now_exists = ready)
SELECT rvbbit.burrow_enroll('ryan@acme.com');          -- once their role exists
INSERT INTO rvbbit.identity_map(identity, role_name)   -- or map to a differently-named role
     VALUES ('ryan@acme.com', 'analyst_ryan');
```

`rvbbit_guest` is created `NOLOGIN` **with no grants at all** — it cannot be connected as, and
it can read nothing. (`NOLOGIN` costs nothing: `SET ROLE` into a NOLOGIN role works, and guest
is only ever reached that way.) To give it a real read-only tier, that's a deliberate act:
`SELECT rvbbit.burrow_grant_guest('analytics');`

> Identities longer than **63 bytes** can't be role names — Postgres truncates identifiers at
> that length with only a `NOTICE`, so two long addresses sharing a prefix would silently
> collide into one account. Resolution refuses them; they need an explicit `identity_map` row.

**Non-Burrow installs are untouched by all of this.** `session_subject()` returns before any
resolution when `WAREHOUSE_AUTH` isn't `pg`, and 0221's only footprint on such a box is one
inert row in `pg_authid`.

`GET /auth/config` reports `{mode, google, password}` so a sibling-rendered login page
(`WAREHOUSE_LOGIN_UI=lens`) draws the right buttons without duplicating this config.

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

> **Connector gotcha:** in the "Add custom connector" dialog, leave **OAuth Client ID**
> and **OAuth Client Secret** EMPTY — those are for pre-registered clients and bypass
> auto-registration. If you put your email there you'll get "Client ID not found." Your
> email + password go on the *login page* that appears after, not in the dialog.

## Activity log (audit + usage-learning)
Every tool call is recorded to **`rvbbit.mcp_activity`** (auto-created on startup):
`caller` (the OAuth token's email), `tool`, `args` (incl. the SQL/search query),
`ok`/`error`, `objects` (schema.tables touched), `rows`, `engine`, `elapsed_ms`, `as_of`,
`result_summary`. Two rollup views ship with it: `rvbbit.mcp_activity_summary`
(per tool/caller: calls, errors, avg ms) and `rvbbit.mcp_popular_objects` (most-touched
tables — the seed for "the catalog learns from usage"). It's in the `rvbbit` schema, so
it's hidden from `search_data`. Logging is best-effort; with a read-only data role,
`GRANT INSERT ON rvbbit.mcp_activity` (and the table's privileges) so writes succeed.

### Connect Claude
- **Claude Desktop / Cowork (OAuth):** Settings → Connectors → **Add custom connector** →
  URL `https://dwmcp.example.com/mcp` → it opens the login page → enter email + the shared
  password → **Allow**. No header.
- **Claude Code (either mode):** `claude mcp add --transport http rvbbit-warehouse <url>/mcp --header "Authorization: Bearer $WAREHOUSE_MCP_KEY"`

## The landing page (`/`)
`GET /` is this server's own index of every published artifact — thumbnail, kind,
description, dependency counts, search and kind filters — behind the same session
cookie that already gates `/d/<slug>`, so a cold link bounces through `/login` and
comes back. It's for the install shape where nobody opens DataRabbit at all: people
talk to the warehouse through Claude, artifacts get published here, and without an
index "find last week's dashboard" means scrolling a transcript.

Also served at **`/gallery`** — on a warehouse-only box `/` is free and you get it at
the root; behind the unified origin (`docker/origin/Caddyfile`) DataRabbit owns `/`,
so the ingress routes `/gallery` here instead. Same page either way.

It lists `rvbbit.live_apps` unfiltered — every row there is externally addressable
(DataRabbit *plates* live in `rvbbit.plates` and never appear). Cards open in a new
tab: the index is somewhere you come back to. No new table, no build step, no extra
config. Routes only exist in OAuth mode (same as `/d/<slug>`).

**Thumbnails.** Captures are stored on disk under
`$WAREHOUSE_LIVE_APP_CAPTURE_DIR/thumbs/<kind>/<slug>.jpg` (compose points that at the
durable volume) and are **never regenerated per visit** — a warm request is a ~7ms
file read. Three things keep a cold gallery from looking broken:
- **Warming happens at page render**, not at image request. The browser only fetches
  thumbnails for cards it decides to load, so leaving generation to `/thumbs` meant an
  artifact below the fold never started rendering until somebody scrolled to it.
- **The page retries** a missing shot with backoff (5 tries), so a cold gallery fills
  in while you watch instead of after a manual refresh.
- **`ETag` + `304`**, so a reload revalidates for ~0 bytes instead of re-transferring
  every image.

Captures are JPEG at 800×500/q72 (~28KB each, down from ~190KB PNGs). The URL keeps its
`.png` spelling because lens's `rv-shot` proxy hardcodes it — the `content-type` header
is what the browser reads, and the proxy forwards ours. Pre-existing `.png` captures are
still served until the artifact is republished. The `capture_live_app` MCP tool is
unaffected and still produces full-resolution lossless PNGs.

## Dashboards (artifacts that live + work outside Claude)
**Start from `dashboard_template`** — the proven boilerplate (see [`DASHBOARD_TEMPLATE.md`](DASHBOARD_TEMPLATE.md)).
Its dual-mode data bridge means the *same* artifact runs live in **two places, no login**:
- **In a Cowork artifact** — `window.cowork.callMcpTool('mcp__<id>__run_sql', {sql})`, authed by
  the connector OAuth the user already granted (the sandbox blocks `fetch`, so this is the path).
- **Hosted** — `publish_dashboard(name, html, …)` stores it versioned in `rvbbit.dashboards`,
  serves it at `<WAREHOUSE_PUBLIC_URL>/d/<slug>` behind the login cookie, and injects
  `rvbbitQuery()` (→ `/api/d/<slug>/q`, read-only on the mirror, logged to `mcp_activity`).

Key rule: one **flat** query per data concern in the `composePayload` parts map — the framework
batches them into **one** `run_sql_multi` round trip (each bridge call has ~1.5s overhead), but
each query stays flat on the wire: routable by the accelerated engines, visible to the catalog,
and individually promotable. Never hand-write a `json_build_object` payload query, and never
bake data in — that's a 'dead tree'.
Tools: `dashboard_template` / `publish_dashboard` / `update_dashboard` / `list_dashboards` /
`get_dashboard`. Tables auto-create on startup (no migration). Design: [`docs/DASHBOARDS_PLAN.md`](../../docs/DASHBOARDS_PLAN.md).

**Phase 1 — catalog-linked inspection.** `dashboard_crawl(slug)` extracts each dashboard's
data dependencies — parses literal `rvbbitQuery(...)` calls, **SQL-shaped string literals
anywhere in the artifact** (catches SQL Claude assigns to a variable and passes as
`client(sql)`; `EXPLAIN` validates them so junk like `"select … from the menu"` is dropped),
reconciles the queries it actually ran (from `mcp_activity`), and an OpenRouter LLM fallback
(`OPENROUTER_API_KEY`) — then resolves every query to its tables via `EXPLAIN` (catches
plain heap tables, not just rvbbit-managed). Stored in `rvbbit.dashboard_deps` (a derived,
regenerable index; re-run on publish/update). `get_dashboard` returns the `sources` (the
lens "open base SQL" list); `dashboard_dependents(object)` is impact analysis ("what breaks
if I change this table"); views `rvbbit.dashboard_sources` / `rvbbit.dashboard_dependents`.
No `rvbbitQuery`/metric found ⇒ flagged `materialized` (a "dead tree" — nudge against).

## Config (env)
`WAREHOUSE_DSN` · `RVBBIT_CATALOG_GRAPH` (default `db_catalog`) ·
`WAREHOUSE_SCHEMAS` (CSV allowlist; default = all but rvbbit/pg_*) ·
`WAREHOUSE_ROW_CAP` (1000) · `WAREHOUSE_STMT_TIMEOUT_MS` (30000) ·
`WAREHOUSE_MCP_HOST` (0.0.0.0) · `WAREHOUSE_MCP_PORT` (8765)
**OAuth mode:** `WAREHOUSE_PUBLIC_URL` (enables it) · `WAREHOUSE_LOGIN_PASSWORD` (req) ·
`WAREHOUSE_JWT_SECRET` (req, ≠ MCP_KEY) · `WAREHOUSE_ALLOWED_EMAILS` (opt) ·
`WAREHOUSE_ACCESS_TTL` (3600) · `WAREHOUSE_REFRESH_TTL` (30d) ·
`WAREHOUSE_STATE_FILE` (persist registered clients + refresh tokens across restarts —
put it on a volume, else a restart strands connectors with "client_id not found").
**Shared-key mode:** `WAREHOUSE_MCP_KEY` (bearer; unset = auth OFF, dev only).

## Deferred to Phase 1+
Per-user identity → scoped role (tools run as the *caller's* scope), PII masking in
samples, `ask` (compose text-to-SQL), per-role cost caps, receipts table,
`define_metric`/`get_connection` (promote + scoped runtime DSN).
