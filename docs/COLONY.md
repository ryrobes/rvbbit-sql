# Colony — peer-shared capabilities

User-facing walkthrough: rvbbit.ai/docs/colony (rvbbit-docs
`content/docs/colony.md`). Design history and build log:
`docs/PEER_CAPABILITIES_PLAN.md`. This file is the engineering
reference: schema, functions, transport, runner, and the sharp edges.

A Colony share lets a DataRabbit client attach something running on its
own machine (Ollama, a Gradio app, a local stdio MCP server) to the
shared warehouse, callable by anyone in a scope role while the client
stays up. The database is the rendezvous — request/response queue rows
plus LISTEN/NOTIFY, no relay, no tunnel, no inbound connection to the
sharing machine.

## Schema (migrations 0212–0218)

- `rvbbit.peer_backends` — the registry: `backend_name` (PK), `kind`
  (`llm | embedding | specialist | mcp`), `template` (the transport
  shape the sharing client runs: `openai_chat | gradio | mcp`, or
  legacy `ollama_native`), `model_digest`, `scope_role`, `shared_by`
  (SESSION_USER at registration), `description`, `enabled` (0213),
  `client_host` (0218 — self-reported `os.hostname()` of the sharing
  client; the Fleet map names peer nodes by it, `@role` fallback).
- `rvbbit.peer_backend_presence` — heartbeat rows per
  (backend_name, instance_id), `last_heartbeat_at`, `queue_depth`.
  `rvbbit.peer_backends_live` view rolls this up (instance_count within
  the liveness window, min_queue_depth).
- `rvbbit.peer_capability_requests` / `peer_capability_responses` — the
  queue and its audit trail. `backend_name` is deliberately NOT an FK
  (0214): history survives backend deletion, matching `ml_models`'
  convention.
- Every share is a PAIR of rows under one name: `rvbbit.backends`
  (transport `peer_queue`, endpoint `peer://<name>`) for the generic
  dispatch machinery, plus `rvbbit.peer_backends` for Colony's scope and
  presence. The lens register route creates both;
  `deregister_peer_backend` removes both (0217 — before that, detach
  orphaned the catalog row forever).

## SQL surface

Caller side:

- `rvbbit.call_specialist(name, payload jsonb) → jsonb` — ad-hoc test
  call through the real transport. Payload contract by shape:
  `{"user": "..."}` for chat/gradio, `{"tool": "...", "args": {...}}`
  for mcp.
- `rvbbit.enqueue_peer_request(name, payload) → uuid` and
  `rvbbit.poll_peer_response(request_id, timeout_ms)` — the raw
  two-statement contract. NEVER combine into one function: a single
  top-level statement is one implicit transaction, so the caller's own
  INSERT stays invisible to the claiming runner (a different backend)
  until the function returns — which can't happen while it's polling.
  Proved by deadlock while building P0; SQL-language wrapper bodies
  inline into the caller's statement and have the identical bug.
- Operators: a peer backend is just another backend. Two verified
  patterns — `kind:"llm"` step with `"provider":"<backend_name>"`
  (chat semantics: system/user templates + model override), or
  `kind:"specialist"` step with `"specialist":"<backend_name>"` and
  `inputs` matching the payload contract (e.g. `{"user":"{{t}}"}`).

Sharer side (all SECURITY DEFINER, matching burrow_enroll's precedent —
callers are scoped roles with no direct table grants):

- `rvbbit.register_peer_backend(name, kind, scope_role, template,
  description, model_digest, client_host)` — upserts; scope_role must
  be an existing PG role. 0218 dropped the old 6-arg overload (a
  defaulted 7th param would leave two ambiguous overloads — the psycopg
  overload trap).
- `rvbbit.set_peer_backend_enabled(name, bool)` — pause/resume.
- `rvbbit.deregister_peer_backend(name)` — fails pending/claimed
  requests with 'peer backend was detached by its sharer', removes
  presence + registration + the paired `rvbbit.backends` row.
- All three best-effort re-run `capability_crawl()` so
  `capability_search()` reflects the change immediately.

Runner side (internal): `claim_peer_request(name, instance_id)` (FOR
UPDATE SKIP LOCKED — N runners under one name get exactly-once claim
fan-out for free), `complete_peer_request(id, response, error)`,
`peer_heartbeat(name, instance_id, queue_depth)`.

## Scope enforcement

`enqueue_peer_request` gates on `pg_has_role(session_user, scope_role,
'MEMBER')`. Two hard-won facts:

1. **Superuser bypasses pg_has_role() unconditionally** (documented
   Postgres semantics). The Rust transport's side connection lands via
   unix-socket peer auth as superuser, which would have silently
   defeated scoping for every dispatched call. `peer_queue.rs` reads
   the REAL caller via `GetUserId()`/`GetUserNameFromId()` (non-SPI
   FFI, same pattern as route_log.rs) and runs
   `SET SESSION AUTHORIZATION <caller>` immediately after connecting —
   session_user changes, the bypass drops, checks evaluate against the
   real caller.
2. Verified live both ways: a role outside the scope is rejected; a
   member gets a real answer.

## The transport (crates/pg_rvbbit/src/specialists/peer_queue.rs)

Registered as transport `peer_queue` alongside openai_chat/gradio/etc.
SPI is illegal on prewarm pool threads, so it opens its own
postgres::Client over the unix socket (route_log.rs precedent).
Parameters travel as text with SQL-side double casts
(`$2::text::jsonb`, `$1::text::uuid`) — String's `ToSql::accepts()`
only claims text OIDs, and a direct `$2::jsonb` cast makes Postgres
infer a jsonb wire type ("error serializing parameter").

Responses: the runner's chat/gradio reply is an envelope
(`{"content": ..., "model": ...}`); `extract_content()` unwraps
`content` when it's a string so operator output is the answer text —
matching every other transport's bare-scalar contract. MCP replies keep
their content-block arrays (per the MCP spec) untouched.

## The runner (rvbbit-lens)

`src/lib/server/colony-runner.ts` — a server-lifetime claim loop per
connection (1s tick, heartbeat + claim + execute + complete). Shapes:
`openai_chat` (fetch to the local endpoint), `gradio`
(`@gradio/client`, per-endpoint client cache), `mcp`
(`@modelcontextprotocol/sdk`, spawns and HOLDS the stdio subprocess;
detach kills it). `colony-scan.ts` = explicit-only probing (Ollama
`/api/tags`, Gradio ports 7860–7865). Local share config lives in
`~/.config/<app>/colony.json` — NOT the database.

GOTCHAS:
- The runner is lazily armed by hits to `/api/db/colony/status` or
  `/register`. Restarting the lens dev server (or Postgres) does NOT
  resurrect it on its own — a live Colony/Fleet window does.
- Sharing requires a client whose local server can see localhost ports
  (desktop app / self-hosted lens). Browser-to-remote-lens cannot
  share; calling works from anywhere.
- Dev-mode Fast Refresh can stale the module-scope MCP client map
  (same class as db/listen.ts's documented quirk); restart `next dev`.
  Harmless in production.

## Failure semantics

- No live instance → `enqueue_peer_request` raises immediately
  ("has no live instance right now") — fast failure, not a hang.
- `poll_peer_response` timeout → request marked failed.
- Detach → in-flight requests failed with an explicit error.
- Requests/responses are retained rows: auditable by anyone who can
  read the tables. Share with groups you'd be comfortable seeing the
  prompts.

## Discovery

`capability_crawl()` block 9 emits `cap_peer_backend` KG entries
(LIVE / OFFLINE / PAUSED status in the doc text) — so
`rvbbit.capability_search('...')` finds peers next to operators and
packs. Surfaces: Colony window (roster/share/scan/try-it), Capability
Explorer (Peer Capabilities folder), Fleet map (peer machines as
cluster nodes, hostname-primary).
