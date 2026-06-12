# Warehouse MCP — a governed, semantic, time-travel data interface for Claude

**Status:** design / brainstorm-of-record · **Owner:** Ryan · **Date:** 2026-06-12

> One line: *expose the rvbbit warehouse to Claude (Cowork & Code) as an MCP server
> that makes Claude a great **governed** analyst — semantically searchable by its
> actual data, queryable as of any point in time, where every answer is a
> reproducible, promotable artifact.*

---

## 1. The reframe (why this isn't "remote text-to-SQL")

Our UI text-to-SQL was built for a dumb front-end: *user types → one SQL → data*,
one shot. Claude is a **multi-turn agent**: it can search, sample, validate, run,
notice it's wrong, and fix itself. So the product is **not** "expose text-to-SQL
remotely." It's: *give Claude the raw materials to be a great, governed analyst,
and let our text-to-SQL be one of those materials (a strong first-draft generator
it can call and refine).*

Consequence: **don't pick one of {text-to-sql, raw execute, in-between}.** Ship one
MCP with a **layered tool surface**; Claude picks its depth from the task and the
user. The same server serves finance-in-Cowork and app-builders-in-Code.

The two things that make this defensible and safe — and that competitors (a vanilla
Snowflake/Postgres MCP) can't offer — are the two layers we already have:
**(a) semantic grounding** (so Claude doesn't hallucinate columns) and
**(b) governance** (metrics, AS OF, receipts, scoping).

---

## 2. Personas

| Persona | Surface | Wants | Path through the tools |
|---|---|---|---|
| **Finance / marketing** (non-tech) | Claude Cowork | answers, not SQL; trust | `search_data` → `metric`/`ask` → Claude renders a table/chart; never sees SQL |
| **Analyst / app-builder** | Claude Code | discover, test, build, pull on a schedule | `search_data` → `describe_table` → `validate_sql` (loop) → `run_sql(limit)` → build → `define_metric`/`get_connection` |

The MCP does not know the persona. Tool affordances + role scoping route it.

---

## 3. Tool surface

Grouped by depth. "Backs onto" = existing rvbbit function we surface (mostly reuse).

### Discover (our moat — lean in)
- **`search_data(query, limit=8)`** — semantic search over the catalog KG **+ the
  data-KG** (triples from real rows). Returns ranked objects, each grounded:
  ```
  { table, columns:[{name,type,desc}], description,
    samples:[ ... 3-5 rows ... ],
    stats:{ rows, ndv_by_col, null_rate, enum_values, distribution },
    relationships:[ {to_table, join_hint} ],      // from the KG
    freshness:{ last_synced, generation }, drift_flags:[...] }
  ```
  Backs onto `data_search` + `catalog_docs` + `catalog_fingerprint_table`. **This is
  the single most important tool** — samples + stats + relationships are what stop
  Claude inventing `dim_cust_xref_v2.amt`.
- **`describe_table(table)`** — full profile: schema, samples, per-column stats,
  relationships, lineage, drift, freshness. Backs onto `catalog_docs` +
  `catalog_fingerprint_table` + `catalog_object_history`/`catalog_drift`.
- **`list_metrics(category?)` / `get_metric(name)`** — the governed metrics catalog.
  Surfacing these makes them the path of least resistance for numbers that matter.
  Backs onto `metric_versions` / `resolve_metric` / `metric_sql`.

### Answer (fast/safe path; esp. non-tech)
- **`metric(name, params, as_of?)`** — a **blessed, governed number** (not Claude's
  guess at which of five revenue columns to sum). Returns value(s) + resolved
  params + def version + data-as-of + check verdict + provenance. Backs onto
  `rvbbit.metric()` (+ `check_metric`). For finance, this is the whole game.
- **`ask(question, as_of?)`** — convenience one-shot. Composes `search_data` → LLM
  compose → `validate_sql` → `run_sql`; returns **data + the SQL used (for
  audit/refine) + tables_used + confidence + provenance**. Claude gets the SQL; it
  just doesn't *show* the finance user unless asked. *Lower priority:* with the
  discover+validate+run tools, Claude can BE the text-to-sql; `ask` is a shortcut
  and a strong first draft.

### Build (analysts / app-builders)
- **`validate_sql(sql, as_of?)`** — `route_explain` / dry-run, **NO execution**:
  `{ valid, engine, est_rows, est_cost, tables[], safe_select, warnings[] }`. Claude
  iterates on this cheaply before ever scanning data — the self-correction loop a
  vanilla MCP can't offer. Backs onto `route_explain`.
- **`run_sql(sql, as_of?, limit?)`** — governed, **read-only** execute:
  `{ columns, rows, row_count, truncated, engine, elapsed_ms, receipt_id,
  freshness_notes }`. Read-only + cost-capped + audited (see §5).

### Promote / connect (the forward bet)
- **`define_metric(...)` / `schedule_refresh(...)`** — Claude's good analysis becomes
  a first-class rvbbit metric/flow with lineage + checks + time-travel, instead of a
  fragile CSV script. Backs onto `define_metric` + pg_cron.
- **`get_connection(scope)`** — for "Claude Code builds an app that pulls daily":
  hand back a **scoped, read-only, time-limited DSN/role** so the *built app*
  connects directly at runtime (not through Claude/the MCP). MCP brokers the
  least-privilege credential; the app does the pulling. Discover/test happens via
  MCP at build time; runtime is a direct, governed connection.

---

## 4. The "weird / forward" — what rvbbit is uniquely positioned to do

These are the design principles, not just features. A vanilla data MCP gives Claude
schema names + `execute`. We can give it:

1. **The warehouse *as of any point in time.*** `run_sql(sql, as_of=…)` and
   `metric(…, as_of=…)` are native (generations + bitemporal metrics): "reproduce
   last month's report exactly," "diff this snapshot vs last week," and the
   bitemporal twist *"what would Q2 have been under the metric definition we had in
   March?"* (def-time × data-time). Surface `as_of` on every read tool. (Backs onto
   the `-- rvbbit: as_of` directive / `rvbbit.as_of_timestamp`.)
2. **Semantic search over the *data*, not the schema.** The data-KG finds the right
   cryptically-named table by what its data is *about* — the difference between
   Claude guessing and Claude knowing.
3. **A governance gradient: free to explore, blessed to report.** Roam with
   `search_data`/`run_sql` for discovery; `metric()` is the only path to the
   *official* sensitive number. Analysts get a sandbox; finance gets the audited
   number.
4. **Freshness/drift-aware answers.** Every result carries "this table is 3 days
   stale" / "this enum gained a value last week" (`catalog_drift`). Claude *warns*
   instead of confidently returning rotten data.
5. **Validate-before-run.** `route_explain` lets Claude reason about
   cost/correctness/safety without scanning the warehouse.
6. **Promote analysis into the governed system.** The daily-refresh dashboard becomes
   a real metric/flow with lineage, checks, and time-travel — the ad-hoc gets
   *captured*, not lost.
7. **Semantic operators inside SQL.** `about()`/`classify` in-query → Claude writes
   one statement that joins data *and* calls an LLM ("classify these tickets, then
   aggregate"). Claude writing SQL that runs LLMs.
8. **Every answer reproducible + audit-traced** (receipts + bitemporal): "here's the
   exact query, version, snapshot, and lineage behind this number" — a compliance
   superpower for finance.
9. **The catalog learns from usage.** Claude's successful explorations annotate the
   KG ("this table answers churn questions") → the warehouse gets *more* navigable
   the more it's used.

---

## 5. Governance (non-negotiable — this is "let finance's Claude hit prod")

- **Read-only.** No writes/DDL via `run_sql`/`ask`. Enforce at the role + a SQL
  guard (reject non-SELECT; `BEGIN READ ONLY`).
- **Per-user scoping.** Map the MCP caller's identity → an rvbbit role with
  row/column/schema scope (finance sees finance). Policy table + GRANTs; the catalog
  already knows the objects.
- **Cost / row caps.** `validate_sql` rejects > N est-rows / > cost budget;
  `run_sql` hard-caps returned rows + statement_timeout.
- **PII.** Use the catalog's PII tagging (CATALOG_KG Phase 4) to mask/deny sensitive
  columns per role; `search_data` samples must respect masking.
- **Audit.** Every `run_sql`/`ask`/`metric` writes a receipt (who/what/when/engine/
  rows/SQL). Reproducible by construction.
- **Metrics-first for reporting.** Free SQL for *exploration*; blessed metrics for
  *reporting*. Claude+SQL will occasionally join wrong and present it confidently;
  a finance user can't catch it. Gate `run_sql` to analyst+ roles; non-tech roles
  get `search_data` + `metric` + `ask` only.

---

## 6. Architecture

**New direction.** Existing MCP plumbing = rvbbit as an MCP **client** (it *calls*
external tools via `mcp_call`/`register_mcp_server`). This is rvbbit as an MCP
**server** — Claude calls *in*. So we need a serve path:

- A small **MCP server** (extend `rvbbit-mcp-gateway` with a "serve" mode, or a thin
  sibling service) that speaks MCP to Claude and, per tool call:
  1. resolves the caller's identity → a **scoped read-only PG connection** (role),
  2. calls the backing rvbbit SQL function(s),
  3. attaches freshness/provenance + writes a receipt,
  4. returns structured JSON (Claude renders tables/charts).
- **Secrets** (DSNs, role creds) stay in the gateway's Fernet store — never in PG,
  never in the tool output (except the deliberately-scoped `get_connection`).
- **Transport:** HTTP/streamable-MCP for Cowork (remote); the same server is
  reachable by Claude Code.
- Optionally register the warehouse MCP itself as a `kind='mcp'` capability so it
  shows up in the catalog (dogfooding), but its *job* is serving, not calling.

---

## 7. Phased plan

- **Phase 0 — the safe analyst MVP (highest value, lowest risk).**
  `search_data`, `describe_table`, `list_metrics`/`metric`, `validate_sql`,
  `run_sql` (read-only + caps + receipts). Single scoped role to start. Stand up the
  serve path. This is ~80% of the value and it's the *safe* 80%.
- **Phase 1 — make it trustworthy + temporal.** `ask` (compose text-to-sql),
  `as_of` on all reads, freshness/drift in every output, per-user row/col scoping +
  PII masking, audit dashboard (reuse receipts/Query lens).
- **Phase 2 — forward bets.** `define_metric`/`schedule_refresh` (promote),
  `get_connection` (scoped runtime DSN for built apps), catalog-learns-from-usage,
  semantic-operators-in-SQL exposure.

---

## 8. Open questions

1. **Identity → role mapping.** How does a Cowork/Code user's identity reach the MCP,
   and how do we map it to an rvbbit scoped role? (Per-team tokens? OAuth? Static
   role per MCP instance to start?)
2. **Serve host.** Extend `rvbbit-mcp-gateway` to serve, or a dedicated tiny service?
3. **Non-tech raw-SQL.** Recommendation: **deny `run_sql` to non-tech roles**
   entirely — `search_data` + `metric` + `ask` only. Confirm.
4. **`ask` build vs Claude-native.** Do we invest in a strong server-side `ask`, or
   lean on Claude doing search→validate→run itself (cheaper, more transparent)?
5. **Cost model.** Where do the row/cost caps live, and what are the defaults?

---

## 9. The pitch (what you can't buy elsewhere)

> A data warehouse that's **semantically searchable by its actual data**, queryable
> **as of any point in time**, where **every answer is a reproducible, governed,
> promotable artifact** — exposed to Claude so finance asks in English and gets a
> blessed number, and engineers build apps against a live, introspectable, governed
> connection.
