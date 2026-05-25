# Witness System — Design Note

Status: synthesis / pre-build. Not a spec.
Date captured: 2026-05-19.

## One-sentence pitch

A Postgres extension that maintains structured, versioned, queryable knowledge of its own tables, and synthesizes evidence across them when asked.

## Tagline

> Each table knows itself. Queries convene the right tables. The answer includes the evidence.

## Frame

- **Steward** — resident role per table. Lives between councils, profiles its table, accretes observations, owns the current deposition. Institutional.
- **Witness** — the role a steward plays when called to testify in a council. Bounded to what it directly observed.
- **Deposition** — the steward's structured, versioned, content-hashed statement of what it knows. Produced by the dream cycle.
- **Council** — a convened deliberation across N witnesses to answer a higher-order question. Single-pass synthesis in v1.
- **Receipt** — the audit artifact of a council: witnesses, depositions used, SQL probes, judgments consulted, answer, dissent, caveats, confidence, costs.

Register is legal/institutional, not chatroom. Avoid "agent", "chatbot", "AI assistant". Avoid cute terms like "schema senate". Keep "audit" out — collides with pgaudit.

The orchestrator is a **journalist, not a judge** — it organizes and reports witness testimony including dissent; it does not adjudicate truth.

## Bugs and failure modes to design around

1. **The `ask_table` trap.** Users will route SQL-shaped questions through the LLM path and burn budget. Witnesses must triage: (a) plain SQL, (b) cached deposition, (c) single-witness synthesis, (d) cross-witness council. Cheapest tier wins.
2. **Unbounded council cost.** Multi-agent debate kills these systems. Ship single-pass synthesis only in v1: one prompt, all relevant witness depositions in context, structured output. No rounds, no agent-to-agent chatter.
3. **Witness selection.** The orchestrator's actual brain. v1: embed the question, knn against deposition embeddings, take top 3–4, expand by named natural joins.
4. **Reproducibility.** Receipts must be content-hashed, deposition-versioned, replayable. Identical inputs → identical receipts (modulo model nondeterminism on the live path; cached path is deterministic).
5. **Cold start.** Lazy stewardship — nothing auto-stewards. `rvbbit.steward('orders')` registers a table. v0 deposition in minutes, not hours. Progressive enrichment after.
6. **Memory rot.** Observations are dated, schema-version-bound, half-lifed. Schema changes invalidate (not delete) prior depositions. Compaction promotes recurring observations to patterns and prunes one-off events.
7. **Recursion.** Receipts can be queryable artifacts, but council membership is determined statically and stratified. No witness can be its own ancestor in a council's call graph.
8. **Witness contamination.** A weird week shouldn't poison memory forever. Patterns over events; provenance preserved; adversarial pruning when hard SQL contradicts a witness claim.
9. **Disagreement UX.** Surface dissent in the receipt, headline the answer. The receipt is the trust artifact. Don't hide the model's uncertainty.

## Substrate — three boring pieces

- **pg_rvbbit (extension, Rust/pgrx)** — SQL surface, catalog tables, work queue, cache lookups, NOTIFY emission, event triggers. Zero LLM calls. Synchronous, deterministic, cheap.
- **pg_cron** — coarse scheduling only. Runs SQL that marks stale stewards as due and inserts work queue rows. That is its entire job.
- **rvbbit-sidecar (Rust binary, separate process)** — `tokio` + `deadpool-postgres` + `reqwest`. LISTENs on a channel, processes the work queue, makes LLM calls, writes back. Hand-written prompt templates per job kind. ~1000–1500 LOC. No agent framework. No provider abstraction. No tool-calling abstraction. No streaming. A `match` on `job_kind` with one function per arm.

Discipline: **each new capability is a new match arm, not a new trait.** Generalize only when three concrete cases pull in the same direction.

User-facing call shape:
- `rvbbit.ask(text)` inserts a `RUN_COUNCIL` row, NOTIFYs, returns `pending_receipt_id`. Always async.
- `rvbbit.await_receipt(uuid, timeout)` blocks server-side for sync ergonomics.
- Backend sessions never hold an HTTP connection to a model provider.

Why not pgrx bgworkers for the loop: they crash and cascade, they couple agent lifecycle to postgres restarts, and they make iteration painful. Out-of-process sidecar fails better — if the sidecar is down, SELECTs work, cached receipts return, ask returns pending.

Why not own scheduling in the sidecar: pg_cron is operational reality and its schedules are SQL-queryable.

## Memory architecture — six tables, no more

- `rvbbit.stewards` — registry. One row per stewarded table: regclass, schema_version, current_deposition_id, last_dreamed_at, status, refresh_policy.
- `rvbbit.depositions` — versioned. id, table, schema_version, deposition_version, body (jsonb), content_hash (generated), created_at, supersedes_id. Content-addressable.
- `rvbbit.observations` — append-only log. id, table, observed_at, kind, body, source, embedding. Kinds: schema_change, data_drift, anomaly, query_pattern, manual_note.
- `rvbbit.councils` — id, question, question_embedding, witnesses (regclass[]), deposition_versions_used, started_at, completed_at, answer, dissent, caveats, confidence, status.
- `rvbbit.receipts` — id, council_id, body (jsonb with SQL probes, judgments consulted, prompt hash, model id, costs). Optionally embedded.
- `rvbbit.work_queue` — id, kind, payload, scheduled_at, started_at, completed_at, status, attempts, last_error.

Five rules that keep this elegant:

1. **Deposition body is structured JSON with an enforced shape.** Sections: `summary`, `columns`, `joins`, `temporal`, `regions`, `caveats`, `query_hints`.
2. **Content-hash everything.** Depositions, receipts, prompt templates. Identical input → identical hash → free dedup and replay.
3. **Eat your own embeddings.** Use rvbbit's embedding primitives, not pgvector. Dogfood validation.
4. **Observations are evidence; depositions are knowledge.** Observations append; the dream cycle compacts them into the deposition; old observations get pruned with a long tail.
5. **Schema version binds memory.** `pg_event_trigger` on DDL bumps schema_version; prior depositions become history (readable but non-authoritative); next dream produces a fresh one.

No graph DB, no separate memory service, no LTREE, no logical replication. JSONB + own embeddings + content hashes + append log + schema-version stamps. Reach for more only when there's a real problem these can't solve.

## Primitives — have vs. need

**Already in tree (reuse directly):**
- Embeddings (JIT + bulk) → retrieval primitive
- `knn_text` → "find similar in my table"
- `text_evidence` → witness's testimony return shape
- Semantic predicate bitmap cache (roaring) → "how many rows satisfy judgment X"
- EXPLAIN SEMANTIC → receipt's plan view
- Dreaming → reframe as deposition production
- Clustering (`cluster.rs`) → steward's `regions` section

**Need to add — extension side:**
- Six catalog tables + indexes
- SQL functions: `steward`, `deposition`, `ask`, `await_receipt`, `receipt`, `refresh`
- `pg_event_trigger` for schema-change invalidation
- NOTIFY emit on `work_queue` insert
- pg_cron-driven SQL marking stale stewards as due

**Need to add — sidecar side:**
- LISTEN loop on `rvbbit_jobs`
- Job handlers: `BUILD_V0_DEPOSITION`, `REFRESH_DEPOSITION`, `RUN_COUNCIL`
- Versioned prompt templates (in repo, content-hashed)
- Witness selection (knn over deposition embeddings + join expansion)
- Anthropic HTTP client + structured JSON output parsing
- Per-job budget tracking

The only nontrivial sidecar logic is witness selection. Everything else is plumbing.

## Staged plan

**Stage 0 — Substrate (1–2 weeks).** Catalog tables, work queue with NOTIFY trigger, sidecar skeleton round-tripping a no-op job. Nothing user-visible. Load-bearing.

**Stage 1 — Stewardship without council (2–3 weeks).** `rvbbit.steward(regclass)`, v0 deposition generator (schema + statistics + sample + one LLM pass), `rvbbit.deposition` to inspect, `rvbbit.refresh` to force. Event trigger on DDL → schema version bump → mark for re-dream. pg_cron sweeps stale stewards. **Demo: point rvbbit at a table, get a structured human-readable account of what that table is and means, watch it re-learn after DDL.** Publishable on its own as a self-documenting database.

**Stage 2 — The council (3–4 weeks).** `rvbbit.ask(text)` → witness selection via deposition-embedding knn → triage layer (plain SQL? emit and run, no model call) → single-pass synthesis with all selected witness depositions → structured output (answer + dissent + caveats + confidence). `rvbbit.await_receipt`, `rvbbit.receipt(uuid)` renders the full audit. **Demo: the canned churn question returns a structured answer with a receipt; multi-witness disagreement gets surfaced verbatim.**

**Stage 3 — Observations and accretion (2–3 weeks).** `rvbbit.observations` writes. Dream cycle reads prior observations + prior deposition, compacts events into patterns, prunes archived events. Observation kinds wired: schema_change, data_drift, anomaly, query_pattern, manual_note. `rvbbit.note_table(regclass, text)`. **Demo: deposition changed because of last week's backfill; here's the observation trail.**

**Stage 4 — Cost discipline (2–3 weeks).** Strong "is this SQL?" classifier. Budget contracts on every `rvbbit.ask` (max tokens, max witnesses, max latency, max dollars). Semantic cache on equivalent questions (knn over `councils.question_embedding`; high similarity + unchanged depositions → return prior receipt). Refusal modes. `rvbbit.stats`. **Demo: an ask storm doesn't burn budget — most are SQL or cache hits.**

**Stage 5 — Post-MVP, prioritized.** Council-of-councils with strict acyclicity. Multi-round deliberation (only if a real customer needs it). Witness contamination guards. Join discovery. Bundled demo dataset (fake SaaS). Provider abstraction (when a second model is genuinely needed). Reproducibility CLI (`rvbbit replay <receipt-uuid>`).

## What not to build

- A generic agent loop. Single-pass synthesis with structured JSON output is sufficient through v2.
- A provider abstraction trait before there's a second concrete provider.
- An MCP server in front of any of this. The SQL surface is the protocol.
- Streaming output from the LLM. Wait, parse, write. Add streaming only if a real UX needs it.
- A separate memory store service (Letta/MemGPT/mem0). Deposition + observations + content hashing IS the memory store, and it lives in postgres with the data.
- A bgworker for the orchestration loop. Out-of-process sidecar fails better.

## Test against the pitch

Every architecture decision tested against:

> A Postgres extension that maintains structured, versioned, queryable knowledge of its own tables, and synthesizes evidence across them when asked.

If a proposed feature doesn't serve that sentence, cut it.
