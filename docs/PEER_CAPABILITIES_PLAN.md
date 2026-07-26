# Colony — the database as a shared arsenal

> "Treat your Postgres as an FPS game where guns and ammo are things that
> get acquired and added to the database arsenal, and that arsenal is
> equipment for getting shit done." — the conversation that started this.

A **Colony** is who a shared capability is shared with — the group of
people, scoped by burrow role, who can reach it. **Warren** is the
infrastructure (a deliberate, durable model deployment); **Colony** is the
social layer on top — the group that shares access to whatever's
currently up, whether that's a real Warren, a stray Ollama instance, or a
locally-run MCP server. Engineering nouns (tables, functions, the
`peer_queue` transport) stay plain; "Colony" is the product-facing name
for the feature and its UI.

**STATUS: P0/P1 built, proven end to end, and committed in both repos
(2026-07-26).** Schema (migrations 0212-0214), the `peer_queue` Rust
Transport, and burrow-role scoping are live and verified through the real
dispatch path (`rvbbit.call_specialist`, and via a `specialist`/`llm` flow
step), not just raw SQL: a scoped caller reaches a real local Ollama
(`llama3.1:latest`, ollama_native shape) and a real Gradio-hosted
specialist (a lexicon sentiment app, matching Warren's own
BGE-reranker-via-Gradio pattern), while an out-of-scope caller is correctly
rejected. P1's client runner is real now, not a prototype: `colony-runner.ts`
in rvbbit-lens is a server-lifetime claim-loop (the `openai_chat` shape —
covers Ollama; Gradio isn't ported yet), paired with a `colony-window.tsx`
desktop window combining P2's "Share a Capability" and P3's "My Shared
Capabilities" (manual-URL register form + live roster + pause/resume/
detach), browser-verified against a real Ollama end to end.

Caught and fixed three real bugs along the way: (1) the Rust transport's
side connection lands as superuser via unix-socket peer auth, and
Postgres's `pg_has_role()` unconditionally bypasses for superusers — so
every call through the transport silently ignored scoping until fixed via
`SET SESSION AUTHORIZATION` to the real caller (captured through the same
non-SPI FFI read `route_log.rs` already uses); (2) a `postgres::Client`
parameter cast straight to a non-text type (`$2::jsonb`) makes
`ToSql::accepts()` reject a bound `String`/`&str` — fixed via a `::text`
double-cast; (3) detaching any backend that had ever completed a real call
hit a foreign-key violation — fixed by dropping the FK on
`peer_capability_requests.backend_name`, matching the existing
`rvbbit.ml_models.backend_name` convention (audit survives deletion).

Still open: the Gradio shape in the real (TypeScript) runner, the
system-wide roster reusing Finder's live-vitals rendering, detection/
auto-scan (the other half of P2), and P4 (MCP extension) / P5 (clustering
polish).

## §1 The idea

RVBBIT already has an accidental version of this feature. `register_backend`
writes a name + URL into `rvbbit.backends`; `capability_catalog` does the
same for MCP servers. Neither table cares who created the row or from what
machine — any session that can query the same Postgres can call anything
registered in it. The "shared arsenal" isn't a metaphor for something
hypothetical; it's a literal, accurate description of two tables that
already exist. Nobody designed that as shared infrastructure — it fell out
of "capabilities are catalog rows," which is the load-bearing fact this
whole plan leans on.

What's missing is the half of the idea that came from looking at
[block/buzz](https://github.com/block/buzz) — not its literal mechanism
(a Nostr relay), but its actual bet: **agents are members of the room, not
haunted cron jobs.** Every participant, human or agent, has an identity,
a presence, and an audit trail. RVBBIT's backends/capability rows today
have none of that — no liveness signal beyond ad hoc health checks, no
attribution, no join/leave lifecycle, no room where you can see who's
standing in it.

**The feature**: let anyone running DataRabbit locally attach an LLM, a
hosted ML model, or a locally-run MCP server to the shared database —
callable by anyone else with access, scoped through burrow-mode roles
(the trust boundary is "people in my org," not "anyone with a login"),
for as long as their client stays up, with **zero network configuration**
(no port-forwarding, no inbound firewall changes) and full attribution via
the receipts system that already exists.

## §2 Doctrine (what keeps this small, and what it explicitly isn't)

1. **A Postgres-native queue, not a relay.** Transport is two tables
   (`peer_capability_requests` / `_responses`) + `LISTEN`/`NOTIFY` +
   `FOR UPDATE SKIP LOCKED`. No new always-on service, no connection
   multiplexing, no bespoke isolation guarantee to get right — Postgres's
   own row/connection security is the isolation boundary, and it's one we
   already trust for everything else. This mirrors the alerts
   sweeper+worker pattern almost exactly; we're pointing an already-proven
   shape at a new kind of row, not inventing a new one.
2. **Request-shaped, not streamed, on purpose.** A scalar SQL function was
   never able to stream a partial value into a result column — every
   `clover_*` operator already blocks and returns whole, local Warren or
   remote API alike. The queue's request/response shape gives up nothing
   the system had. (The one honest exception: MCP progress notifications
   for long-running tool calls flatten away. Narrow, and true of nearly
   none of the MCP servers already in the catalog.)
3. **Just another transport, not a new concept.** A peer-backed backend is
   a new value alongside `openai_chat` on `register_backend` — the
   operator layer, the router, receipts, `capability_search()` don't need
   to know or care. Same move for MCP: a peer-shared server is another
   `capability_catalog` row of `kind='mcp'`, differing only in where the
   process lives.
4. **The DataRabbit client is the runner. No new binary.** The Electron
   app's local Next.js server (and any self-hosted lens instance running
   on the same machine as the capability) already has everything this
   needs: unrestricted local network access for scanning (server-side, so
   none of the browser Private-Network-Access restrictions that would
   block this from renderer JS), a proven `LISTEN`/`NOTIFY` primitive (the
   existing per-block "Subscribe to a Postgres NOTIFY channel" feature —
   the delta is a server-lifetime singleton subscription instead of a
   per-window one), and the same API-route + background-task shape every
   other lens feature is already built from.
5. **Scope is a hard requirement from day one, not a v2 bolt-on.** Every
   shared capability is scoped to a burrow role at creation time. There is
   no "share with anyone who can connect" option in v0.
6. **Localhost-only scanning, LAN as an explicit opt-in, never silent.**
   Scanning is a button the user presses, not a background sweep. This is
   not a port-scanner and must never feel like one.
7. **Fan-out/clustering falls out of the queue, it isn't a separate
   subsystem.** N peers registering under one backend identity already get
   exactly-once-claim fan-out from `FOR UPDATE SKIP LOCKED`. v0 load
   balancing is emergent (a fast, attentively-polling peer claims more);
   smarter selection is future work, mirrored on the router's existing
   candidate-ranking machinery, not a v0 requirement.
8. **Not a Warren replacement.** Warren stays the durable, deliberately
   deployed, centrally-managed path for a model you want always-on. This
   is the casual, ephemeral, "I happen to have this running right now"
   path. Different lifecycles, same operator-facing shape.

## §3 Schema (`rvbbit.peer_*`)

Sketch, not final DDL — Phase 0 firms this up.

```sql
rvbbit.peer_backends (
    backend_name      text PRIMARY KEY,   -- the shared identity ("llama3-8b-shared")
    kind              text NOT NULL,      -- 'llm' | 'embedding' | 'specialist' | 'mcp'
    template          text,               -- detected shape: 'openai_chat', 'ollama_native', ...
    model_digest       text,               -- fingerprint for clustering identity, when available
    scope_role        text NOT NULL,      -- burrow role this is shared with
    shared_by         text NOT NULL,      -- burrow identity of the sharer
    description       text,
    created_at        timestamptz NOT NULL DEFAULT now()
)

rvbbit.peer_backend_presence (
    backend_name      text NOT NULL REFERENCES rvbbit.peer_backends(backend_name) ON DELETE CASCADE,
    instance_id       uuid NOT NULL,       -- one row per LIVE peer instance behind a shared name
    last_heartbeat_at timestamptz NOT NULL,
    queue_depth       int NOT NULL DEFAULT 0,   -- cheap load signal for the roster UI
    PRIMARY KEY (backend_name, instance_id)
)

rvbbit.peer_capability_requests (
    request_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    backend_name text NOT NULL,
    payload      jsonb NOT NULL,
    requested_by text NOT NULL,           -- burrow identity, for receipts/attribution
    status       text NOT NULL DEFAULT 'pending',  -- pending | claimed | done | failed
    claimed_by   uuid,                     -- instance_id that claimed it
    created_at   timestamptz NOT NULL DEFAULT now()
)

rvbbit.peer_capability_responses (
    request_id    uuid PRIMARY KEY REFERENCES rvbbit.peer_capability_requests(request_id),
    response      jsonb,
    error         text,
    completed_at  timestamptz NOT NULL DEFAULT now()
)
```

`NOTIFY` payload is the `request_id` only (a doorbell, never the delivery —
Postgres caps `NOTIFY` payloads at 8000 bytes; the real payload lives in
the jsonb column, which doesn't share that limit). Presence is a heartbeat
row, not raw socket liveness, deliberately — sleep/wake cycles and flaky
wifi lie about TCP state; a "last heartbeat within N seconds" check
doesn't, and it's the same shape `accelerator_runtime_status`/
`gqe_warm_state` already use.

## §4 Architecture

- **Claim + execute:** a peer instance's runner does
  `SELECT ... FROM peer_capability_requests WHERE backend_name = $1 AND status = 'pending' FOR UPDATE SKIP LOCKED LIMIT N`,
  marks claimed, does the actual local call (HTTP to its own loopback
  model server, or an in-process MCP tool call), writes the response row.
  Multiple peers behind one `backend_name` get exactly-once delivery for
  free from this locking — clustering is this, not a separate feature.
- **Caller side:** the new transport's execution path inserts a request
  row and polls/waits (bounded by a GUC-configurable timeout, same shape
  as `rvbbit.duck_backend_timeout_s`) for the matching response row, then
  returns it exactly like every other transport's operator call. The
  calling backend holds its connection for the wait — the same resource
  shape as any slow synchronous semantic op today, not a new pressure
  category.
- **MCP extension:** the client-side runner is also where a locally-run
  stdio MCP server's process lives (spawned/held by the DataRabbit client
  itself, the same job `rvbbit-mcp-gateway` already does for centrally
  hosted MCP servers — just relocated to the machine where the process
  has to run anyway, since stdio was never going to work otherwise). The
  runner bridges tool-call/tool-result through the same request/response
  tables. A peer-shared MCP server is a `capability_catalog` row exactly
  like any other; nothing downstream needs to know the difference.
- **Scoping enforcement:** a request against a `peer_backends` row only
  resolves if the caller's burrow role matches `scope_role` — enforced at
  the row level, not the UI level.

## §5 Detection / template library (LLMs *and* hosted ML models)

Templates are organized around the shapes the operator layer already
expects to call — not an open-ended universe of possible ML APIs:

- **Chat/LLM** — OpenAI-compatible `/v1/chat/completions` (covers vLLM,
  llama.cpp server, LM Studio, text-generation-webui's OpenAI shim,
  Ollama's OpenAI-compat surface) → maps to the existing `openai_chat`
  transport.
- **Ollama native** — `/api/generate`, `/api/chat`, `/api/tags` (model
  listing), `/api/show` (a digest — the identity signal clustering wants,
  so two peers serving "the same" model don't get silently pooled if
  they're actually different quantizations).
- **Embeddings** — whatever transport already backs the embedding-shaped
  operators (`existing-postgres.md` references "embedding operators point
  at an embeddings API the same way" as `openai_chat` — confirm the exact
  transport name against `providers.rs` in Phase 0, don't assume it here).
- **Specialist/task models** (rerank, NER/extract, classify, OCR,
  transcribe) — v1 scope is Gradio-hosted endpoints specifically, because
  that's the exact shape Warren's own specialist zoo already deploys (the
  BGE reranker is already served this way). A peer-shared specialist and
  a Warren-deployed specialist become the same operator-facing shape,
  just a different registration path — genuine continuity, not a new
  category.
- **Anything unmatched** falls back to manual registration (the existing
  `register_backend` flow) — never a blocker, just no auto-detect
  convenience.

Detection: probe a short, curated list of well-known local ports (11434
Ollama, 1234 LM Studio, 8000/8080 vLLM/llama.cpp, 7860 Gradio, …) and
well-known paths, pattern-match the response shape, surface a
**confidence-scored** match. Never silently register on a low-confidence
match — an unconfirmed shape surfaces as "force-match to X?", not an
automatic share.

## §6 UI — adding, modifying, monitoring

Three distinct surfaces; where exactly each lives (a new tab on the
existing Capabilities window vs. a dedicated panel) is a product call for
Phase 2, not locked here.

1. **Share a Capability (local, adding).** An explicit "Scan for local
   models" button (never silent/background) → confidence-scored results
   with detected template + suggested name (offering to join an existing
   cluster identity when a digest match is found) → pick a burrow scope →
   confirm. Plus a manual "I know the URL" fallback for anything the
   scanner misses.
2. **My Shared Capabilities (local, modifying + monitoring).** Everything
   *this* client is currently sharing: name, scope, live health, current
   queue depth, a recent-calls/receipts summary, pause/resume, rescope,
   rename, and a hard "Stop sharing" detach. This is the sharer's own
   visibility into what they're giving away and how hard it's being used
   — the direct answer to the "quietly becomes an unpaid tax on one
   generous person" risk flagged during design.
3. **Peer Capabilities roster (system-wide, monitoring).** The literal
   "dynamic Finder of peer capabilities" this whole idea started from — a
   Finder-adjacent live view (reusing the same live-vitals rendering
   Finder rows already use) of every capability currently live across the
   database: who's sharing it, what shape, current load, a heartbeat
   freshness indicator, click-through to try it (drop a prefilled
   semantic-op block) or to its receipts/lineage. Extend
   `capability_search()` to include peer-shared entries so "what can
   answer an embedding question right now" is answerable the same way any
   other capability question already is. This is the room where you can
   see who's standing in it — the actual Buzz-derived idea, made
   concrete.

## §7 Phases (each shippable + testable before the next)

- **P0 — Queue foundation.** Schema above; the new transport wired into
  `register_backend` + the operator/router execution path so a
  peer-queue-backed backend is callable exactly like any other backend;
  `FOR UPDATE SKIP LOCKED` claim + GUC-configurable timeout/error path;
  burrow-role scoping enforced at the row level. No UI — provable with
  direct SQL and a curl-style stand-in for "the DataRabbit runner." *Test:*
  insert a request, claim it from a second session, write a response,
  confirm the caller-side wait resolves; confirm a caller outside
  `scope_role` gets nothing.
- **P1 — Client runner.** The actual DataRabbit-side implementation: a
  server-lifetime `LISTEN` + claim-loop module, manual "register a URL as
  a peer backend" (no auto-scan yet), heartbeat presence writes. *Test:*
  two local DataRabbit instances, one shares a stubbed local HTTP
  endpoint, the other calls it through a real semantic op.
- **P2 — Detection.** The scan/template-match library (chat, Ollama
  native, embeddings — confirm the exact existing transport parity
  against `providers.rs` first), confidence scoring, the "Share a
  Capability" UI. *Test:* scan against a real local Ollama + a real local
  Gradio specialist, confirm correct template + confidence.
- **P3 — Monitoring UI.** "My Shared Capabilities" local panel + the
  system-wide Peer Capabilities roster window. *Test:* pause/resume and
  detach actually stop routing; the roster reflects presence within one
  heartbeat interval of a client closing.
- **P4 — MCP extension.** Client-side stdio runner bridging local MCP
  tool-calls through the same queue tables; peer-shared MCP servers appear
  in `capability_catalog`. *Test:* share a locally-running stdio MCP
  server, call one of its tools from a second session end to end.
- **P5 — Clustering polish.** Digest-based identity matching in the scan
  flow (offer to join an existing pool rather than always minting a new
  backend); cluster-aware roster display (N peers behind one name, load
  per peer). The fan-out mechanics themselves already work as of P0 — this
  phase is UX, not queue engineering.

## §8 Open questions (flagged honestly, not resolved here)

- Exact current transport name/shape for embeddings and for Gradio-hosted
  specialists — a real audit against `providers.rs`, not an assumption,
  before P2 locks its template list.
- Should an org admin be able to forcibly detach someone else's
  misbehaving/overloaded share from the system-wide roster? A real
  moderation question, not assumed either way.
- Anything beyond receipts + a pause toggle for cost/consent (quotas,
  "only serve N req/min") is explicitly future work, not v0.
- Whether smarter peer selection within a cluster (mirroring the router's
  candidate-ranking) is ever worth building, or whether emergent
  fast-peer-claims-more is good enough indefinitely.

## §9 Explicitly not this

- Not a general-purpose RPC system — scoped to shapes the operator layer
  already knows how to consume.
- Not real-time token streaming to the caller.
- Not background/silent network scanning, ever.
- Not a Warren replacement.
- Not reachable by "anyone with a Postgres login" without an explicit
  scope choice — burrow roles are the trust boundary, always.
