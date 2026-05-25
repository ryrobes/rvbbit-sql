# MCP (Model Context Protocol) — Complete Reference

This is everything a UI builder needs to introspect, manage, observe, and
compose MCP servers in rvbbit. It assumes you already speak SQL and have
read (or can refer to) [`OPERATORS.md`](./OPERATORS.md) for the broader
operator/flow story — this doc focuses on the MCP-specific surfaces and
how MCP integrates with operators.

Everything below is plain SQL against an rvbbit-equipped Postgres. There
is no separate REST API; the UI generates and runs SQL.

---

## Table of contents

1. [Overview & architecture](#1-overview--architecture)
2. [Quick start](#2-quick-start)
3. [Catalog reference — tables & views](#3-catalog-reference)
4. [Mutating DDL — the helper functions](#4-mutating-ddl)
5. [Reading patterns — UI panel recipes](#5-reading-patterns)
6. [Active operations](#6-active-operations)
7. [JSON shapes returned by UDFs](#7-json-shapes)
8. [Operator integration — `mcp` as a node kind](#8-operator-integration)
9. [Caching strategy](#9-caching-strategy)
10. [Typed wrappers — auto-generated SQL per tool](#10-typed-wrappers)
11. [Gotchas & edge cases](#11-gotchas--edge-cases)
12. [SQL generation cheatsheet](#12-sql-generation-cheatsheet)
13. [What's deliberately not implemented](#13-what-isnt-implemented)

---

## 1. Overview & architecture

An **MCP server** is an external process — a stdio subprocess or an HTTP
service — that exposes a set of typed tools (callable functions) and,
optionally, resources (URI-addressable read-only data). Anthropic's
[Model Context Protocol](https://modelcontextprotocol.io/) is the wire
standard; rvbbit brings that ecosystem into SQL.

Architecture:

```
┌─────────────────┐  HTTP   ┌──────────────────┐  stdio/HTTP   ┌──────────────────┐
│  PG backend     │ ─────►  │  mcp-gateway     │ ──────────►   │  MCP server      │
│  (rvbbit ext)   │  ◄────  │  (Python sidecar)│ ◄──────────   │  (github, fs, …) │
└─────────────────┘         └──────────────────┘               └──────────────────┘
       │                            │
       └─────── libpq ──────────────┘  (gateway reads rvbbit.mcp_servers)
```

- **PG never forks subprocesses.** Lifecycle is the sidecar's job.
- **One subprocess per server**, shared across every PG backend, lazy-spawned.
- **Per-server `asyncio.Lock`** serializes JSON-RPC calls to one server.
- **Crash recovery**: a subprocess that dies between calls is reset; the
  next call respawns it.
- **The gateway reads `rvbbit.mcp_servers` via libpq** on first call to
  each server. The catalog is the source of truth.

The UI never talks to the gateway directly — it always goes through SQL.

---

## 2. Quick start

Register a server, discover its tools, call one, look at the audit:

```sql
-- 1. Register the official GitHub MCP server.
SELECT rvbbit.register_mcp_server(
    server_name      => 'github',
    server_transport => 'stdio',
    server_command   => 'npx',
    server_args      => ARRAY['-y', '@modelcontextprotocol/server-github'],
    server_env       => '{"GITHUB_PERSONAL_ACCESS_TOKEN":"${GITHUB_TOKEN}"}'::jsonb,
    server_timeout_ms => 60000);

-- 2. Discover its tools + resources.
SELECT rvbbit.refresh_mcp_server('github');                 -- returns n_tools

-- 3. See what was discovered.
SELECT name, description FROM rvbbit.mcp_tools
WHERE server = 'github' ORDER BY name;

-- 4. Call a tool.
SELECT rvbbit.mcp_call('github', 'search_repositories',
                       '{"query":"rust","perPage":3}'::jsonb);

-- 5. Or call it as a relational source (auto-unwraps array shapes).
SELECT r->>'full_name', r->>'stargazers_count'
FROM rvbbit.mcp_rows('github', 'search_repositories',
                     '{"query":"rust","perPage":3}'::jsonb) r;

-- 6. Audit.
SELECT tool, latency_ms, error
FROM rvbbit.mcp_invocations
WHERE server = 'github' ORDER BY invocation_at DESC LIMIT 10;
```

---

## 3. Catalog reference

Every UI panel maps to one or more of these. All live in the `rvbbit`
schema.

### `rvbbit.mcp_servers` — the registry

Source of truth. One row per registered MCP server.

| column | type | meaning |
|---|---|---|
| `name` | text (PK) | server identifier — also the schema name for typed wrappers |
| `transport` | text | `stdio` or `http` |
| `command` | text | stdio only: executable (e.g. `npx`, `python`) |
| `args` | text[] | stdio only: argv tail |
| `env` | jsonb | stdio only: env vars; `${VAR}` refs resolved at spawn from gateway's env |
| `url` | text | http only: full MCP endpoint URL |
| `auth_header_env` | text | http only: env var name (not the token) for bearer auth |
| `timeout_ms` | int | per-call timeout, default 30000 |
| `description` | text | human-readable docs |
| `created_at` | timestamptz | registration time |

Constraints:
- `transport IN ('stdio','http')`
- stdio rows require `command`; http rows require `url`.

```sql
SELECT * FROM rvbbit.mcp_servers ORDER BY name;
```

### `rvbbit.mcp_tools` — discovered tools per server

Populated by `rvbbit.refresh_mcp_server(name)`. One row per
`(server, tool)`. The `cacheable`/`ttl_seconds` flags are preserved across
re-discoveries (UPSERT on conflict).

| column | type | meaning |
|---|---|---|
| `server` | text (FK → mcp_servers, CASCADE) | |
| `name` | text (PK with server) | tool name |
| `description` | text | tool's own description from the server |
| `input_schema` | jsonb | the tool's JSON Schema for its args |
| `discovered_at` | timestamptz | last `refresh_mcp_server` time |
| `cacheable` | bool | opt-in result caching (default false) — see §9 |
| `ttl_seconds` | int | NULL = forever, else expiry seconds |

### `rvbbit.mcp_resources` — discovered resources per server

Populated by `refresh_mcp_server`. Servers that don't expose resources
just produce zero rows. URIs are server-defined (`file:///…`,
`postgres://…`, `github://…`, etc.).

| column | type | meaning |
|---|---|---|
| `server` | text (FK → mcp_servers, CASCADE) | |
| `uri` | text (PK with server) | the resource URI |
| `name` | text | display name from the server |
| `description` | text | |
| `mime_type` | text | content type hint |
| `discovered_at` | timestamptz | last refresh time |

### `rvbbit.mcp_invocations` — per-call audit log

Every `mcp_call` / `mcp_rows` / operator-context mcp-node call lands
here. The dashboard's "what happened" surface.

| column | type | meaning |
|---|---|---|
| `id` | bigserial PK | |
| `server` | text | NOT a FK — audit survives `drop_mcp_server` |
| `tool` | text | |
| `args` | jsonb | the rendered args sent to the server |
| `output` | jsonb | full envelope `{content, isError}` |
| `error` | text | text of an `isError=true` result, else NULL |
| `latency_ms` | int | wall time of the call (0 for cache hits) |
| `cache_hit` | bool | served from `mcp_cache` |
| `query_id` | uuid | correlation with `rvbbit.receipts.query_id` when called in an operator |
| `invocation_at` | timestamptz | clock_timestamp |

Indexes: `(server, invocation_at DESC)` and `(query_id)`.

**Important limitation:** *transport-level* failures (gateway unreachable,
HTTP 5xx) raise a SQL error and roll back the transaction; their row is
LOST. Only successful calls and tool-level `isError=true` results
persist. Plan UI fallback accordingly (show the SQL error too).

### `rvbbit.mcp_cache` — opted-in tool results

Keyed by `(server, tool, args_hash)` where `args_hash` is blake3 over the
canonical (sorted-key) JSON args. Only populated for tools with
`cacheable=true`. Failed calls are NEVER cached.

| column | type | meaning |
|---|---|---|
| `server` | text (PK part) | |
| `tool` | text (PK part) | |
| `args_hash` | text (PK part) | 32-char hex (128 bits of blake3) |
| `args` | jsonb | kept for human inspection — not load-bearing |
| `output` | jsonb | the cached envelope `{content, isError:false}` |
| `cached_at` | timestamptz | for TTL math |

### `rvbbit.mcp_usage` (view) — per-(server, tool) rollup

Real-time aggregate over `mcp_invocations`. Parallel to
`rvbbit.specialist_usage` / `rvbbit.llm_usage`.

| column | type | meaning |
|---|---|---|
| `server`, `tool` | text | grouping keys |
| `n_calls`, `n_errors` | bigint | |
| `total_latency_ms`, `avg_latency_ms`, `p50_latency_ms`, `p95_latency_ms` | int | |
| `first_call_at`, `last_call_at` | timestamptz | |

```sql
SELECT * FROM rvbbit.mcp_usage ORDER BY n_calls DESC;
```

For cache hit rate (not in the view — easy to compute):

```sql
SELECT server, tool,
       count(*) AS n_calls,
       count(*) FILTER (WHERE cache_hit) AS n_hits,
       round(100.0 * count(*) FILTER (WHERE cache_hit) / count(*), 1) AS hit_pct
FROM rvbbit.mcp_invocations
GROUP BY server, tool
ORDER BY n_calls DESC;
```

### `rvbbit.mcp_health` (view) — per-server status snapshot

Passive snapshot — does NOT probe servers. For an active round-trip use
`rvbbit.mcp_probe(server)` (§6).

| column | type | meaning |
|---|---|---|
| `name` | text | server name |
| `transport` | text | |
| `n_tools`, `n_resources` | int | from mcp_tools / mcp_resources |
| `last_discovered_at` | timestamptz | last refresh time |
| `last_call_at` | timestamptz | last successful invocation |
| `last_error_at` | timestamptz | last error invocation |
| `created_at` | timestamptz | when the server was registered |

```sql
SELECT * FROM rvbbit.mcp_health ORDER BY name;
```

A simple "status pill" for the UI:

```sql
SELECT name,
       CASE
           WHEN last_call_at IS NULL                                        THEN 'untested'
           WHEN last_error_at IS NOT NULL AND last_error_at > last_call_at  THEN 'failing'
           WHEN last_call_at > now() - interval '1 hour'                    THEN 'active'
           ELSE                                                                  'idle'
       END AS status,
       n_tools, n_resources, last_call_at, last_error_at
FROM rvbbit.mcp_health;
```

---

## 4. Mutating DDL

These are the SQL functions the UI generates. All are idempotent /
re-runnable.

### `register_mcp_server(...)` — register or upsert

```sql
SELECT rvbbit.register_mcp_server(
    server_name        => 'github',           -- required
    server_transport   => 'stdio',            -- 'stdio' | 'http', default 'stdio'
    server_command     => 'npx',              -- stdio: executable
    server_args        => ARRAY['-y', '@modelcontextprotocol/server-github'],
    server_env         => '{"GITHUB_PERSONAL_ACCESS_TOKEN":"${GITHUB_TOKEN}"}'::jsonb,
    server_url         => NULL,               -- http only
    server_auth_env    => NULL,               -- http only
    server_timeout_ms  => 30000,
    server_description => 'GitHub repos/issues/PRs via npx');
```

Calling with the same `server_name` updates in place (UPSERT).

**`${VAR}` env substitution** happens in the gateway at subprocess spawn
time — values are NEVER persisted in the catalog. The gateway only reads
its own runtime env, so any var you reference must be passed through in
`docker-compose.yml`'s `environment:` block for the gateway.

### `drop_mcp_server(name)` — deregister

```sql
SELECT rvbbit.drop_mcp_server('github');
```

Cascades to `mcp_tools` + `mcp_resources`. Audit (`mcp_invocations`) and
cache (`mcp_cache`) are preserved (no FK), so a drop+re-register doesn't
lose history. Wrapper schemas (§10) are NOT auto-dropped; clean those
with `DROP SCHEMA "<name>" CASCADE` if you regenerated wrappers.

### `refresh_mcp_server(name)` → int

```sql
SELECT rvbbit.refresh_mcp_server('github');                  -- returns n_tools
```

Asks the gateway to re-run `tools/list` + `resources/list`. **Preserves**
`mcp_tools.cacheable` / `ttl_seconds` across re-discovery (so a periodic
"refresh schemas" task won't wipe caching policy). Tools the server no
longer reports get DELETEd; resources are fully replaced.

### `set_mcp_tool_caching(server, tool, ttl?)` — opt in

```sql
SELECT rvbbit.set_mcp_tool_caching('github', 'search_repositories', 300);
-- 5-minute TTL on identical-args results
SELECT rvbbit.set_mcp_tool_caching('github', 'get_me', NULL);
-- NULL TTL → cache forever (no expiry)
```

Raises if the tool isn't in `mcp_tools` (run `refresh_mcp_server` first).

To turn caching off:

```sql
UPDATE rvbbit.mcp_tools
SET cacheable = false
WHERE server = 'github' AND name = 'search_repositories';
```

### `purge_mcp_cache(server, tool?)` → int

```sql
SELECT rvbbit.purge_mcp_cache('github', 'search_repositories');  -- one tool
SELECT rvbbit.purge_mcp_cache('github');                         -- all of github
```

Returns the row count removed.

### `generate_mcp_wrappers(server)` → int

See §10. Creates a per-server schema with one typed SQL function per tool.

```sql
SELECT rvbbit.generate_mcp_wrappers('github');  -- returns n_wrappers
```

---

## 5. Reading patterns

Concrete SQL for the panels you'll likely build.

### 5a. Servers dashboard (top-level list)

```sql
SELECT h.name,
       h.transport,
       h.n_tools,
       h.n_resources,
       h.last_call_at,
       h.last_error_at,
       coalesce(u.n_calls, 0) AS total_calls,
       coalesce(u.n_errors, 0) AS total_errors,
       h.created_at
FROM rvbbit.mcp_health h
LEFT JOIN (
    SELECT server, sum(n_calls) AS n_calls, sum(n_errors) AS n_errors
    FROM rvbbit.mcp_usage GROUP BY server
) u ON u.server = h.name
ORDER BY h.name;
```

### 5b. Server detail — tools list

```sql
SELECT t.name,
       t.description,
       t.cacheable,
       t.ttl_seconds,
       t.discovered_at,
       u.n_calls,
       u.n_errors,
       u.avg_latency_ms,
       u.p95_latency_ms,
       u.last_call_at
FROM rvbbit.mcp_tools t
LEFT JOIN rvbbit.mcp_usage u
       ON u.server = t.server AND u.tool = t.name
WHERE t.server = $1
ORDER BY t.name;
```

### 5c. Server detail — resources list

```sql
SELECT uri, name, description, mime_type, discovered_at
FROM rvbbit.mcp_resources
WHERE server = $1 ORDER BY uri;
```

### 5d. Tool detail page

Includes input schema so the UI can render a typed form:

```sql
SELECT t.*,
       u.n_calls, u.n_errors, u.avg_latency_ms, u.p95_latency_ms,
       u.first_call_at, u.last_call_at,
       (SELECT count(*) FROM rvbbit.mcp_cache c
         WHERE c.server = t.server AND c.tool = t.name) AS n_cached
FROM rvbbit.mcp_tools t
LEFT JOIN rvbbit.mcp_usage u
       ON u.server = t.server AND u.tool = t.name
WHERE t.server = $1 AND t.name = $2;
```

The `input_schema` is JSON Schema — see §7 for typical shapes.

### 5e. Invocation log viewer (paginated, filterable)

```sql
SELECT id, server, tool, args, output, error, latency_ms,
       cache_hit, query_id, invocation_at
FROM rvbbit.mcp_invocations
WHERE ($1::text IS NULL OR server = $1)
  AND ($2::text IS NULL OR tool   = $2)
  AND ($3::bool IS NULL OR (error IS NOT NULL) = $3)   -- error filter
  AND ($4::bool IS NULL OR cache_hit = $4)             -- cache-hit filter
  AND invocation_at < coalesce($5::timestamptz, 'infinity')
ORDER BY invocation_at DESC
LIMIT 100;
```

Cursor by `invocation_at` for "load more". The audit table can grow large;
have a UI control to purge old rows:

```sql
DELETE FROM rvbbit.mcp_invocations WHERE invocation_at < now() - interval '30 days';
```

### 5f. Usage time-series (charts)

Calls-per-hour for a tool over the last N days:

```sql
SELECT date_trunc('hour', invocation_at) AS bucket,
       count(*)                          AS n,
       count(*) FILTER (WHERE error IS NOT NULL) AS n_errors,
       count(*) FILTER (WHERE cache_hit)         AS n_hits,
       round(avg(latency_ms))::int       AS avg_latency_ms
FROM rvbbit.mcp_invocations
WHERE server = $1 AND tool = $2
  AND invocation_at > now() - interval '7 days'
GROUP BY 1 ORDER BY 1;
```

### 5g. Cache viewer

```sql
SELECT c.server, c.tool, c.args_hash, c.args, c.cached_at,
       t.ttl_seconds,
       CASE
           WHEN t.ttl_seconds IS NULL THEN false
           ELSE c.cached_at + (t.ttl_seconds || ' seconds')::interval < now()
       END AS expired,
       octet_length(c.output::text) AS output_bytes
FROM rvbbit.mcp_cache c
LEFT JOIN rvbbit.mcp_tools t
       ON t.server = c.server AND t.name = c.tool
WHERE c.server = $1
ORDER BY c.cached_at DESC;
```

### 5h. Find operators that use MCP nodes

The operator catalog is `rvbbit.operators` (see [OPERATORS.md](./OPERATORS.md)).
A node with `kind:"mcp"` is what to look for:

```sql
-- All operators referencing an mcp node somewhere in steps or takes.nodes
SELECT o.name, o.return_type,
       jsonb_path_query_array(coalesce(o.steps, '[]'::jsonb),
           '$[*] ? (@.kind == "mcp")') AS mcp_steps,
       jsonb_path_query_array(coalesce(o.takes->'nodes', '[]'::jsonb),
           '$[*] ? (@.kind == "mcp")') AS mcp_take_nodes
FROM rvbbit.operators o
WHERE o.steps  @? '$[*] ? (@.kind == "mcp")'
   OR o.takes->'nodes' @? '$[*] ? (@.kind == "mcp")';
```

Or "find every operator that calls a given (server, tool)":

```sql
SELECT o.name
FROM rvbbit.operators o,
     jsonb_array_elements(coalesce(o.steps, '[]'::jsonb)) AS step
WHERE step->>'kind' = 'mcp'
  AND step->>'server' = $1
  AND step->>'tool'   = $2;
```

---

## 6. Active operations

These actually hit the gateway. Use them for "test this tool" REPL
panels, "refresh now" buttons, "probe health" buttons.

### `mcp_call(server, tool, args jsonb) → jsonb` — invoke

```sql
SELECT rvbbit.mcp_call('github', 'search_repositories',
                       '{"query":"rust","perPage":3}'::jsonb);
```

Returns the full MCP envelope. Errors:
- `isError=true` in the response → tool-level error, surfaced gracefully
  (no exception). Audit row written with `error` set.
- Transport failure → SQL exception raised. NO audit row.

For the UI "test this tool" panel: catch SQL exceptions and surface them
distinctly from `output.isError == true`.

### `mcp_rows(server, tool, args jsonb) → SETOF jsonb` — invoke as relation

Auto-unwraps array-shaped responses. Same auth/logging as `mcp_call`.

```sql
SELECT r->>'full_name', r->>'language', r->>'stargazers_count'
FROM rvbbit.mcp_rows('github', 'search_repositories',
                     '{"query":"vector db","perPage":10}'::jsonb) r
WHERE r->>'language' = 'Rust';
```

Unwrap rules: multiple text content blocks → one row each; top-level JSON
array → one row per element; JSON object with key `items` / `results` /
`data` / `entries` / `rows` → one row per element of that array; anything
else → one row containing the whole thing.

### `mcp_text(envelope jsonb) → text` — convenience extractor

```sql
SELECT rvbbit.mcp_text(
    rvbbit.mcp_call('echo', 'echo', '{"text":"hi"}'::jsonb)
);
-- 'hi'
```

### `mcp_resource(server, uri) → jsonb` — read a resource

```sql
SELECT rvbbit.mcp_resource('fs', 'file:///etc/hostname');
-- {"contents": [{"uri":"file:///etc/hostname", "mimeType":"text/plain", "text":"…"}]}
```

### `mcp_resource_text(server, uri) → text` — first text block

```sql
SELECT rvbbit.mcp_resource_text('fs', 'file:///etc/hostname');
```

### `mcp_probe(server) → jsonb` — active health round-trip

```sql
SELECT rvbbit.mcp_probe('github');
-- {"reachable": true, "latency_ms": 245, "n_tools": 26, "error": null}
```

Spawns the subprocess if not loaded — `reachable=true` means callable
RIGHT NOW. Use behind a button (the cost is real for stdio servers,
especially first-time `npx` downloads).

To probe everything for a dashboard refresh:

```sql
SELECT s.name, p.*
FROM rvbbit.mcp_servers s,
     LATERAL (SELECT (rvbbit.mcp_probe(s.name))) AS x(j),
     LATERAL jsonb_to_record(x.j) AS p(reachable bool, latency_ms int, n_tools int, error text);
```

(This serializes the probes — expensive. Prefer probing one at a time on
user demand.)

### `refresh_mcp_server(name)` and `generate_mcp_wrappers(name)` — see §4 and §10.

---

## 7. JSON shapes

What the UDFs actually return — write your TypeScript/Rust UI types
against these.

### `mcp_call` / `mcp_rows` (per element) — the envelope

```json
{
  "isError": false,
  "content": [
    { "type": "text", "text": "the tool's text response (often JSON inside)" }
    // Or:
    // { "type": "image", "data": "<base64>", "mimeType": "image/png" }
    // { "type": "resource", "uri": "...", "text": "...", "mimeType": "..." }
  ]
}
```

`isError=true` envelopes have the error text in `content[0].text`.

### `mcp_resource` — read envelope

```json
{
  "contents": [
    { "uri": "file:///path", "mimeType": "text/plain", "text": "…" }
    // or { "uri": "...", "mimeType": "...", "blob": "<base64>" } for binary
  ]
}
```

### `mcp_probe` — probe result

```json
{
  "reachable": true,
  "latency_ms": 245,
  "n_tools": 26,
  "error": null            // or "ConnectionError: …" / "TimeoutError"
}
```

### `mcp_tools.input_schema` — JSON Schema per tool

Standard JSON Schema. Most tools look like:

```json
{
  "type": "object",
  "properties": {
    "query":   { "type": "string",  "description": "search string" },
    "perPage": { "type": "integer", "description": "results per page" }
  },
  "required": ["query"]
}
```

`type` may be an array like `["string", "null"]` (we treat any non-null
entry as the type). `array` and `object` properties may have `items` /
`properties` schemas — for form-rendering purposes you can usually
collapse them to a textarea that the user fills with JSON.

### `rvbbit.mcp_servers.env` — env-var template

```json
{
  "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}",
  "DEBUG": "1"
}
```

`${VAR}` references are resolved against the gateway's runtime env at
spawn time. Literal values (no `${...}`) are passed through. To rotate a
token: change the gateway's env, then `rvbbit.refresh_mcp_server(...)` to
force a respawn.

---

## 8. Operator integration

MCP is a node kind alongside `llm`, `specialist`, `code`, `sql`. See
[OPERATORS.md](./OPERATORS.md) for the full operator model; this section
is the MCP-specific delta.

### The `mcp` step shape

```json
{
  "name":   "fetch",          // step name (referenced downstream as steps.fetch.…)
  "kind":   "mcp",
  "server": "github",         // a row in rvbbit.mcp_servers
  "tool":   "search_repositories",  // a row in rvbbit.mcp_tools under that server
  "inputs": {                 // templated per row, sent as the tool's args
      "query":   "{{ inputs.q }}",
      "perPage": 1
  }
}
```

- `inputs` values are templated like every other node — see OPERATORS.md
  §12. Notably: `{{ steps.X.output.items.0.name }}` works (array indexing
  via numeric path segments).
- **Output of an mcp node** = the tool's text content, **parsed as JSON
  when possible**. So if a tool returns `{"items":[…]}`, downstream nodes
  read `{{ steps.fetch.output.items }}`; if it returns plain text,
  `{{ steps.fetch.output }}` is that string.
- **`isError=true` from the tool** becomes a step error — caught by the
  operator's flow control (`wards`, `retry`).
- **In bulk via the warm path**, per-call MCP audit rows are skipped
  (pool threads can't do SPI); the operator's own `sub_calls` receipt
  still captures every call.

### Example: MCP → LLM chain (the differentiator vs lars)

```sql
SELECT rvbbit.create_operator(
    op_name        => 'summarize_repo',
    op_arg_names   => ARRAY['q'],
    op_return_type => 'text',
    op_steps       => $j$[
        {
          "name":"fetch", "kind":"mcp",
          "server":"github", "tool":"search_repositories",
          "inputs": {"query":"{{ inputs.q }}", "perPage":1}
        },
        {
          "name":"summarize", "kind":"llm",
          "model":"openai/gpt-5.4-mini",
          "system":"Summarize in ONE sentence.",
          "user":"Repo: {{ steps.fetch.output.items.0.full_name }}\nDescription: {{ steps.fetch.output.items.0.description }}"
        }
    ]$j$::jsonb);

SELECT rvbbit.summarize_repo('anthropic-ai/claude-code');
```

### UI affordances for the operator editor

A nice operator editor should let the user:

1. **Pick "mcp" as a node kind** (the dropdown alongside llm/specialist/code/sql).
2. **Pick a server** — autocomplete from:
   ```sql
   SELECT name FROM rvbbit.mcp_servers ORDER BY name;
   ```
3. **Pick a tool from that server** — autocomplete from:
   ```sql
   SELECT name, description FROM rvbbit.mcp_tools WHERE server = $1 ORDER BY name;
   ```
4. **Render an inputs form** from the tool's `input_schema` (a typed form
   per property, with templating hints — `{{ inputs.x }}` autocompletes
   from the operator's `arg_names`, `{{ steps.X.output.… }}` from prior
   steps).
5. **Show output addressability**: after picking a tool, hint to the user
   "if this tool returns `{items:[…]}` you can reference
   `{{ steps.<name>.output.items.0.… }}` downstream."

To find downstream uses of a tool (for an "uses" panel on the tool detail):

```sql
-- Operators that call (server, tool) anywhere in their pipeline.
SELECT DISTINCT o.name
FROM rvbbit.operators o, jsonb_array_elements(coalesce(o.steps, '[]'::jsonb)) AS step
WHERE step->>'kind' = 'mcp' AND step->>'server' = $1 AND step->>'tool' = $2;
```

### Modifying an operator's steps (UI generates this)

Operators are jsonb in `rvbbit.operators.steps`. To add an mcp step to an
existing operator:

```sql
UPDATE rvbbit.operators
SET steps = coalesce(steps, '[]'::jsonb) || jsonb_build_array(
    jsonb_build_object(
        'name', 'enrich',
        'kind', 'mcp',
        'server', 'github',
        'tool', 'get_repository',
        'inputs', jsonb_build_object(
            'owner', '{{ inputs.owner }}',
            'repo',  '{{ inputs.repo }}')
    )
)
WHERE name = 'my_operator';
```

To remove a step by name:

```sql
UPDATE rvbbit.operators
SET steps = (
    SELECT jsonb_agg(s) FROM jsonb_array_elements(steps) s
    WHERE s->>'name' <> 'enrich'
)
WHERE name = 'my_operator';
```

(See OPERATORS.md for the full step-editing patterns.)

---

## 9. Caching strategy

Caching is **opt-in per (server, tool)** — never automatic. The point:
idempotent tools (`get_*`, `list_*`, `search_*`) benefit greatly; tools
with side effects (`create_issue`, `delete_*`) must stay un-cached or
they'll silently break.

### How keys are computed

`args_hash = blake3(canonical_json(args))[..16 bytes].hex()`

Canonical JSON = serde_json's default (sorted-key) output. **Key order
doesn't matter** — `{"a":1,"b":2}` and `{"b":2,"a":1}` hash identically.

### What's cached

- Only the OUTPUT envelope (the full `{content, isError:false}` jsonb).
- Only for tools with `mcp_tools.cacheable = true`.
- Only for SUCCESSFUL calls (`isError=false`). Tool errors are NEVER
  cached — no poisoning.
- The TTL check: a row is fresh if
  `cached_at + ttl_seconds > clock_timestamp()` (or `ttl_seconds IS NULL`).

### What's NOT cached

- Operator-context MCP calls (the operator's own `sub_calls` audit is
  the source of truth there; the per-call SPI lookup would crash on pool
  threads).
- Calls via `mcp_rows` (the SETOF form bypasses the cache — same call
  path; could be added but isn't today).

### UI controls

For a tool detail page:

- **Toggle** "Cache results" → `set_mcp_tool_caching(server, tool, ttl)` or
  `UPDATE … SET cacheable=false`.
- **TTL input** (NULL = forever) → second arg to `set_mcp_tool_caching`.
- **Stats**: hit rate from §3's snippet.
- **Purge** button → `purge_mcp_cache(server, tool)`.
- **Cache size** indicator: `COUNT(*)` + `pg_column_size(output)` rollup.

### Bulk enable suggestions

Common pattern — auto-cache obviously-idempotent tools after registration:

```sql
UPDATE rvbbit.mcp_tools
SET cacheable = true, ttl_seconds = 300
WHERE server = 'github'
  AND (name LIKE 'get_%' OR name LIKE 'list_%' OR name LIKE 'search_%');
```

The UI could offer this as a "smart defaults" button on a server detail
page (with a preview of which tools would be affected).

---

## 10. Typed wrappers

```sql
SELECT rvbbit.generate_mcp_wrappers('github');  -- returns n_wrappers
```

For each row in `rvbbit.mcp_tools WHERE server='github'`, this creates a
**typed SETOF-jsonb SQL function** in a per-server schema (here `github`):

```sql
SELECT r->>'full_name'
FROM github.search_repositories(query => 'rust', perpage => 5) r;
```

- Schema name = server name. **Collision risk**: if a user-created schema
  shares the name, generation will `CREATE SCHEMA` fail — pick an unused
  name when registering the server, OR use the generic
  `rvbbit.mcp_call(...)` instead.
- **Idempotent**: drops the schema and re-creates each call.
- **Required args** have no default; **optional args default NULL** and
  are OMITTED from the JSON sent to the tool (so an unset arg truly stays
  unset — never serialized as `null`).
- **SQL arg names are lowercased** so callers don't have to quote
  `camelCase` identifiers; the **original JSON key is preserved** in the
  body.
- JSON Schema → SQL types: `string→text`, `integer→bigint`,
  `number→double precision`, `boolean→boolean`, `array|object→jsonb`,
  anything else → `text`. `["string","null"]` → `text` (first non-null).
- Every wrapper returns `SETOF jsonb` via `rvbbit.mcp_rows` — so list
  shapes compose with `JOIN` / `WHERE` / `GROUP BY` immediately.

### Listing generated wrappers

```sql
SELECT proname AS function, pg_get_function_arguments(p.oid) AS args
FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid
WHERE n.nspname = $1                              -- the server name
ORDER BY proname;
```

### Cleaning up after `drop_mcp_server`

The wrappers are NOT auto-dropped (the schema might contain user objects
too). To clean:

```sql
DROP SCHEMA IF EXISTS "github" CASCADE;
```

---

## 11. Gotchas & edge cases

### Transport-level errors aren't logged

If the gateway is down, or the network blows up, `mcp_call` raises a SQL
error and rolls back — including the audit row. The UI should catch SQL
errors and surface them as "transport unreachable: …", separately from
tool-level `isError=true`.

### `${VAR}` env resolution is at SPAWN time

If you change the gateway's env (e.g., rotate `GITHUB_TOKEN`), already-
spawned servers keep the old value. `rvbbit.refresh_mcp_server(name)`
forces a respawn (it evicts the cached subprocess from the gateway).

### Multi-block tool responses

FastMCP-style servers emit one text content block PER list element when
a tool returns a Python list. `mcp_call` returns all blocks intact;
`mcp_rows` returns one row per block. This is invisible in single-result
calls but very visible in `mcp_call(…)->'content'` for list-returning
tools.

### Tool isError vs transport error — they look different

- **Transport error**: SQL `psycopg.errors.InternalError` (or similar) at
  `rvbbit.mcp_call(...)`. No row in `mcp_invocations`.
- **Tool error**: `rvbbit.mcp_call(...)` returns successfully with
  `output.isError = true` and `output.content[0].text = "<error>"`. Row
  in `mcp_invocations` with `error` set.

For the operator pipeline, both surface as `sub_calls[i].error`.

### `mcp_health` is passive

It does NOT probe servers. A server that crashed mid-day shows fine in
`mcp_health` until you call it (and an error row appears) or
`mcp_probe(name)`. The UI should call `mcp_probe` behind an explicit
button (don't auto-probe-all on dashboard load — too expensive).

### The audit log can get big

`mcp_invocations` grows monotonically. Plan for retention:

```sql
DELETE FROM rvbbit.mcp_invocations WHERE invocation_at < now() - interval '30 days';
```

(Optional: convert to a partitioned table for high-volume installations.)

### `refresh_mcp_server` preserves the caching flag

`UPDATE`-style upsert. So your user's caching choices survive periodic
re-discovery. Drop and re-register loses them (drop cascades to mcp_tools).

### Per-server `asyncio.Lock` serializes calls per server

If you fire 50 concurrent `mcp_call('github', ...)`s from SQL, the
gateway serializes them through one subprocess. This is intentional
(MCP stdio is inherently serial). For high concurrency to one tool,
register multiple instances of the same server with different names
(`github_1`, `github_2`) and round-robin in your SQL.

### Tool schemas can drift

If the upstream server adds/removes tools or changes argument shapes,
your wrappers go stale. The UI should:
- Detect drift: compare `mcp_tools.input_schema` before/after a
  `refresh_mcp_server` (or surface a "Schema changed" badge).
- Suggest regenerating wrappers: `rvbbit.generate_mcp_wrappers(server)`.

---

## 12. SQL generation cheatsheet

Copy-paste templates for the UI to fill in.

### Register stdio server

```sql
SELECT rvbbit.register_mcp_server(
    server_name       => $1,
    server_transport  => 'stdio',
    server_command    => $2,                -- e.g. 'npx', 'python', '/usr/bin/foo'
    server_args       => $3::text[],        -- e.g. ARRAY['-y','@mcp/server-x']
    server_env        => $4::jsonb,         -- e.g. '{"TOKEN":"${MY_TOKEN}"}'
    server_timeout_ms => $5,                -- nullable
    server_description=> $6);
```

### Register HTTP server

```sql
SELECT rvbbit.register_mcp_server(
    server_name       => $1,
    server_transport  => 'http',
    server_url        => $2,
    server_auth_env   => $3,                -- env var name, nullable
    server_timeout_ms => $4,
    server_description=> $5);
```

### Toggle a tool's caching

```sql
SELECT rvbbit.set_mcp_tool_caching($1, $2, $3);          -- ttl_seconds nullable
-- OR:
UPDATE rvbbit.mcp_tools
SET cacheable = false, ttl_seconds = NULL
WHERE server = $1 AND name = $2;
```

### Update an operator's mcp step inputs

```sql
UPDATE rvbbit.operators
SET steps = jsonb_set(
    steps,
    array[(idx - 1)::text, 'inputs'],     -- $1 = 1-based step index
    $2::jsonb
)
WHERE name = $3;
```

(`idx` is the 1-based step index the UI tracks.)

### Append an mcp step to an operator

```sql
UPDATE rvbbit.operators
SET steps = coalesce(steps, '[]'::jsonb) ||
    jsonb_build_array(jsonb_build_object(
        'name',   $1,
        'kind',   'mcp',
        'server', $2,
        'tool',   $3,
        'inputs', $4::jsonb))
WHERE name = $5;
```

---

## 13. What isn't implemented

Deliberately out of scope (see commit history for rationale):

- **MCP prompts** — server-defined prompt templates. Rvbbit operators
  already cover this better.
- **Streaming / Progress notifications** — SQL is synchronous; no fit.
- **MCP sampling** (server requesting an LLM call from the client) — not
  wired. Possible future, but adds bidirectional plumbing.
- **OAuth** — every common MCP server uses a static bearer token via env
  var. OAuth's interactive flow doesn't fit a backend-side Postgres
  extension.
- **URI templates** for resources — only static URIs are discovered;
  templated `resources/templates/list` not consulted.
- **FDW for resources** — `rvbbit.mcp_resource(...)` covers read; no
  `SELECT * FROM mcp_fs.files` foreign-table surface.

If a user need surfaces for any of these, the natural extensions are
straightforward — they're deferred, not blocked.

---

## Cross-references

- **Operators** — full operator/flow reference: [`OPERATORS.md`](./OPERATORS.md)
- **Bigfoot demo** — example operator pipelines:
  [`BIGFOOT-DEMO.md`](./BIGFOOT-DEMO.md) (note: no MCP nodes in the demo
  today; the canonical MCP examples live in `tests/test_mcp.py`)
- **Tests** — exhaustive ground-truth on every surface:
  `tests/test_mcp.py`
