# Capability KG — the system that knows what it can do

Status: design converged 2026-07-14, not yet built.
Sibling of `docs/CATALOG_KG_PLAN.md` (same KG substrate, same search surface,
pointed at VERBS instead of NOUNS). Primary consumer: the Desktop Assistant
(`rvbbit-lens/docs/DESKTOP_ASSISTANT_PLAN.md`); secondary consumers: Scry
(third layer), warehouse-mcp (`search_tools` could later read this instead of
its hand-rolled Python ranker), any future agent surface.

## Thesis

The "one execute tool vs 100 named tools" debate resolves as: **one execute
surface plus a queryable map**. RVBBIT's execute surface already exists — the
enriched SQL dialect (semantic operators, metrics, cubes, AS OF, flows, KNN).
What's missing is the map: an agent can *run* `means()` but doesn't know it
exists, doesn't know a blessed `revenue` metric supersedes its hand-rolled
SUM, doesn't know the user created a `classify_ticket` operator on Tuesday.

The map must be **data, not prompt**:

- **Fixed prompt, growing capability space.** User-created operators, metrics,
  cubes, brains become discoverable the moment they exist — no prompt edits,
  no redeploys. The agent's competence is a view over the catalog
  ("derive, don't declare", applied to the assistant herself).
- **JIT, problem-shaped context.** Capability knowledge arrives as tool
  results — top-k entries shaped to the actual question — instead of an
  80-function context dump. Evictable, re-queryable, cache-friendly.
- **Observed cost, not documented cost.** Receipts exist for every operator
  call; capability entries carry telemetry ("`about()` p50 40ms/row,
  ~$X/1k rows — filter before applying"). The tool list audits itself.
- **Composable + observable.** It's "just a new named KG": Scry/KG Explorer
  render it for free, `data_search` already searches it for free.

## What already exists (reused as-is — verified 2026-07-14)

| Capability | Primitive |
|---|---|
| Graph-namespaced KG store | `rvbbit.kg_nodes/kg_edges/kg_evidence` (`graph_id` column); `kg_assert_node` / `kg_assert_edge` / `kg_link_evidence` (kg.rs) |
| Fingerprint docs + embeddings | `rvbbit.catalog_docs` — **already keyed by `graph_id`** (`node_id, graph_id, kind, …, doc, embedding, embedded_at`) |
| Free-text KNN search | `rvbbit.data_search(query, k, kinds text[], graph text DEFAULT 'db_catalog')` — **already graph-parameterized**; brute-force cosine fine to ~10k docs, `kg_lance_indexes` for later ANN |
| Cached embeddings | `rvbbit.embed(text, specialist)` + `embedding_cache` (re-crawls only pay for changed docs) |
| Durable crawl pattern | `catalog_crawl_run()` PROCEDURE (COMMIT per item; see memory: `extension_sql_file` doesn't re-run on .so rebuild → `reload-extension` re-applies) |
| Capability sources | `rvbbit.operators` (name, arg_names/types, model, **description**, steps), `metric_defs`, `cube_defs`, `alert_rules`, `brain_sources`/`brain_roles`, `capability_catalog` (packs incl. MCP servers), curated system-surface seed (below) |
| Telemetry for v2 | `agent_messages` (cost_usd, latency_ms, tool calls), `query_costs`, `cost_events`, `live_call_counts()` |
| Lens/Scry rendering | KG Explorer + Scry render any named graph; Data Search window pattern |

**Missing (built here):** the capability crawler, the curated system-surface
seed, the `capability_search()` sugar, edges, and (v2) the telemetry rollup.

## Design

### Graph model — `graph_id = 'rvbbit_capabilities'`

Node kinds (KG `kind` strings, also used as `catalog_docs.kind` for search
filtering via the existing `kinds text[]` arg):

- `cap_syntax` — language features not derivable from any table: semantic
  operators as a family (`means`, `about`, `classify`, `extract`, …),
  `AS OF` time travel, `THEN` pipelines, `block.<name>`/`param.*` reactive
  refs, `rvbbit.flow`, KNN/`embed`. Sourced from the curated seed.
- `cap_operator` — every row of `rvbbit.operators` (system-installed AND
  user-created; instances, with their concrete signatures/models).
- `cap_metric` / `cap_cube` — blessed definitions from `metric_defs`/`cube_defs`.
- `cap_brain` — document brains (name, corpus description, ask_brain entry).
- `cap_alert_template` — the alert verbs ("watch this": condition→action).
- `cap_pack` — `capability_catalog` entries; `install_state: installed|available`
  (v3: the adjacent possible — "you could do X if you install Y").
- Shared nodes: `model`, `provider` (edges only; tiny).

Edges: `cap_operator —runs_on→ model —served_by→ provider`;
`cap_metric —derived_from→ table` (via metric lineage, cross-referencing the
`db_catalog` graph's table nodes — same `kg_nodes` table, so cross-graph edges
are just rows); `cap_cube —joins→ table`; `cap_pack —contains→ cap_*`.

Property bag per node: `signature`, `description`, `example` (ONE runnable
example — the highest-value field for an agent), `cost_class`
(`free | cheap | metered_llm | gpu`), `tags`, `install_state`, and (v2)
`observed: {calls_30d, p50_ms, avg_cost_usd, last_used}`.

### Fingerprint doc (per capability → `catalog_docs`)

Deterministic text: kind, name, signature, description, tags, example,
cost class. Same shape as table fingerprints; embedded via the cached
`rvbbit.embed`. Unchanged text ⇒ unchanged embedding ⇒ re-crawls are ~free.

### Crawler

- `rvbbit.capability_crawl()` (fn, one txn — fine at this cardinality; the
  durable PROCEDURE variant only if brains/packs get big).
- Enumerates: seed table + operators + metric_defs + cube_defs + brains +
  alert templates + capability_catalog → asserts nodes/edges → upserts docs →
  embeds changed docs.
- Trigger cadence: manual + pg_cron nightly + (cheap) after `create_operator`
  / metric/cube DDL via their existing write paths (nice-to-have; nightly is
  acceptable v1 — the assistant can also be told "crawl then search" on miss).

### The curated seed — the one real authoring lift

`rvbbit.capability_seed(name, kind, signature, description, example,
cost_class, tags)` shipped as a migration. ~25 rows v1, covering: the semantic
operator family (with the filter-before-semantic cost guidance baked into
descriptions), metrics surface (`metric()`, `metric_sql`, `define_metric`,
metrics-first doctrine), cubes, `data_search`, `ask_brain`, AS OF, flows/THEN,
`train_model`/`predict_*`, `knn`/`embed`. This is documentation-as-data — write
it once, every agent surface inherits it.

### Search

```sql
-- ergonomic wrapper; data_search does the work
CREATE FUNCTION rvbbit.capability_search(q text, k int DEFAULT 8, kinds text[] DEFAULT NULL)
RETURNS ... AS $$ SELECT * FROM rvbbit.data_search(q, k, kinds, 'rvbbit_capabilities') $$;
```

Returns kind, name, and the doc (signature + description + example + cost) —
one call, problem-shaped, ready to paste into a block.

## Assistant integration (rvbbit-lens side)

- **One prompt line replaces the entire planned syllabus** (data_search §,
  metrics §, semantic-SQL §, ask_brain § — all subsumed): *"Before hand-rolling
  something the platform might already do, run
  `SELECT * FROM rvbbit.capability_search('…')` — the system describes itself:
  semantic SQL operators, blessed metrics, cubes, brains, with signatures,
  examples, and observed costs. Prefer blessed metrics/cubes over ad-hoc
  aggregates; heed cost_class (filter first for metered_llm/gpu)."*
- No new agent tool: the existing builtin `query` executes it. JIT capability
  context arrives as an evictable tool result.
- The metrics-first rule stays as one sentence of doctrine; everything else is
  lookup.

### The consent loop (costs quoted before spend — machinery already exists)

`rvbbit.explain_semantic(q)` (explain.rs, RYR-290) projects a query's semantic
execution graph WITHOUT running it — external calls and **dollar cost sketched
from receipt history** via the model-rates table; `explain_semantic_analyze`
reports measured actuals. So the loop is prompt-only:

1. Prompt rule: before creating/updating a block whose SQL applies
   `metered_llm`/`gpu` operators beyond trivial row counts, run
   `explain_semantic`, and if projected cost exceeds the user's threshold,
   reply with the estimate and ZERO commands — the zero-command conversational
   turn (built for vagueness) doubles as the consent primitive. "I'd run
   means() over 1.4M rows — projected ~$1.50. Go?"
2. `explain_semantic` itself becomes a `capability_seed` row — the capability
   that prices capabilities is discoverable through the same map.
3. Spend threshold = an Assistant Settings knob (auto-approve under $X, ask
   above; the operator's `budget.cost_usd` stays the hard ceiling beneath the
   soft gate).
4. Post-run, receipts let her self-reconcile: "estimated $1.50, actual $1.37"
   — pre-receipt and post-receipt are the same system talking to itself.

This is also the crispest argument for SQL-over-Python as the agent substrate:
declarative means plannable, plannable means **estimable before execution**.
There is no EXPLAIN for a Python script.

## Presence heartbeat (sibling deliverable, lens side)

Presence is **shell state, not window state** — the ✦ in the OS bar is the
natural heartbeat: dim (idle) / glow (open) / **pulse + narration** while a
turn runs (poll `rvbbit.agent_messages` / `live_call_counts()` for the active
`agent_run_id`). Because it's global, feedback survives the window being
closed — which is the prerequisite for TTS/STT conversations that never open
a transcript at all. Detail in `rvbbit-lens/docs/DESKTOP_ASSISTANT_PLAN.md`.

## P0 SHIPPED (2026-07-14, uncommitted) — as built

Migration `0147_capability_kg.sql` (registered in migrations.rs, applied live to
bench): `capability_seed` (14 curated rows), `capability_doc()`,
`capability_crawl()` (seed + 67 operators; metrics/cubes loops ready, none on
bench), `capability_search()` wrapper. Embedder for this box: the `embed`
backend repointed to OpenAI `text-embedding-3-small` @1536 (transport `openai`,
`OPENAI_API_KEY` already in-container; GPU-box BGE embedder is stopped);
db_catalog re-crawled for coherence.

Verified acceptance:
- Paraphrase probes land the right capability on the first page: "sounds
  angry"→semantic_sql_operators #1, "how it looked last week"→time_travel #1,
  "cost before running"→explain_semantic #1, "policy documents"→ask_brain #1,
  "notified when sales drop"→alerts_watch #1, "natural language →
  query"→synth_sql #2.
- **create_operator → crawl → discovered → used, zero prompt changes**: a
  `cryptid_codename` operator created minutes earlier was found #1 by the
  assistant (her receipts show capability_search → explain_semantic → build),
  priced at ~$0.0004 (quoted, under threshold, proceeded per consent contract),
  and deployed into a live block. The full stack — map, meter, consent,
  artifact — composed in one turn.
- Not yet eyeballed: KG Explorer render of the graph (nodes/edges exist).

Hard-won P0 lessons:
1. **`embedding_cache` keys by text but NOT by model** (real platform bug,
   observed): switching the embed backend's model silently serves stale
   vectors — docs re-"embedded" via cache hits from the old model, fresh
   queries in the new space, dimension-mismatched dense search returning
   nothing for exactly the never-before-seen strings. Fix procedure until the
   key includes model: `embedding_purge('<specialist>')` after ANY model
   change, then re-crawl. (text-embedding-3-large also ignored the
   `dimensions` opt → 3072-dim vectors; reverted to 3-small.)
2. **Doc quality beats model quality.** The "sounds angry" miss was cured by
   writing use-cases in USER vocabulary into the family doc and synthesizing
   infix usage examples for operator instances — not by the bigger embedder.
   Retrieval docs must speak the way askers ask.
3. **Example strings poison retrieval**: capability_search's own example
   ("compare a table to how it looked last week") outranked time_travel for
   that exact phrasing. Examples are part of the embedded doc — write them
   about the capability's domain, never in generic temporal/comparative
   phrasing that shadows sibling capabilities.
4. Page-recall is the acceptance bar, not rank-1: the agent reads all top-k
   docs and chooses; the occasional lexical-noise #1 (clean_year) is harmless.

## P1 SHIPPED (2026-07-14, uncommitted) — as built

Crawler extended in 0147: model→provider `served_by` edges (split on '/'),
pack loop over `capability_catalog` (52 packs, honest `install_state`:
'installed' when its backend is registered OR any of its operators exist;
docs for 'available' packs carry the never-install-autonomously line),
`cap_pack —contains→ cap_operator` edges (37), brain sources (`cap_brain`),
alert rules loop (`cap_alert`, latest version), and best-effort metric lineage
(`derived_from` → lightweight `db_table_ref` nodes; true cross-graph
unification with db_catalog still an open question). Live graph on bench:
142 nodes / 8 kinds; edges runs_on 67, contains 37, served_by 4,
derived_from 1. Blessed a real test metric (`wa_sightings`).

Verified:
- **Adjacent possible**: "search the web for competitor news" → brave #1 /
  exa #3 (both 'available'). Assistant asked for live news replied: honest
  about her sandbox, named Brave/Exa/Firecrawl as installable, **"I won't
  install one on my own"**, offered to walk the user through it.
- **Governed numbers**: first pass she hand-rolled count(*) for "the official
  count" (right number, wrong pedigree — thread familiarity beat lookup);
  added the GOVERNED NUMBERS prompt rule (trigger words: official/canonical/
  blessed/THE number → check metric_defs first, attribute the metric). Retest:
  "Per the blessed wa_sightings metric… it happens to match, but that's now
  THE governed figure." Owned the earlier hand-count unprompted.
- **KG Explorer renders the graph natively** — `rvbbit_capabilities (142n)`
  appears in the graph picker with zero integration work; WebGL topics view
  clusters by edge structure (runs_on/openai hub, clover/contains, etc.).

P1 lesson: prompt doctrine needs TRIGGER WORDS, not just principles — "prefer
blessed metrics" didn't fire against a table she already knew; "when the user
says official/canonical/THE number, check metric_defs FIRST, even for data you
know well" did.

## P2 + MCP corpus SHIPPED (2026-07-14, uncommitted) — as built

- **Observed costs (P2)**: the crawler's operator loop rolls up
  `rvbbit.receipts` (30d, error-free): calls, avg cost/call, p50 latency —
  appended to the search doc ("Observed (30d): ~2500 calls, ~$0.00053/call,
  p50 625ms") and stored exact in node props.observed. `_cap_sig2()` buckets
  to two significant figures so nightly re-crawls don't churn embeddings over
  noise. First proof: cryptid_codename's doc self-priced from the assistant's
  own five calls the day before. Scheduling: run `capability_crawl()` nightly
  via pg_cron (not auto-registered — cron home is the 'postgres' db, see
  memory; wire per deployment).
- **Warehouse MCP tool corpus**: all 79 connector tools extracted
  (`scripts/gen_capability_mcp_tools.py` — AST docstrings + registration
  mapping from services/warehouse-mcp/server.py) into
  `rvbbit.capability_mcp_tools`, crawled as `cap_mcp_tool` nodes whose docs
  state plainly: NOT a local SQL function, exposed to external agents via the
  connector, SQL-side sibling usually exists. In-corpus even when the
  connector is unused locally — the map should cover both sides of the wall.
  (24 tools lack upstream docstrings — indexed by name; backfill welcome.
  Regenerate + re-apply + re-crawl after tool changes.)
- Live graph after: 214 docs embedded; node kinds now include cap_mcp_tool(79).

P3 status: Scry `[capability]` layer toggle = lens-side polish, deferred (the
graph already renders in KG Explorer's picker); pointing warehouse-mcp's
`search_tools` at this graph = deferred (would unify both agent surfaces on
one map — natural follow-up now that the connector's own tools live here).

## Phases

- **P0 (core loop):** `capability_seed` migration (~25 curated rows) →
  `capability_crawl()` covering seed + operators + metrics + cubes →
  `capability_search()` wrapper → assistant prompt line. Smoke: the acceptance
  list below.
- **P1 (graph richness):** model/provider/lineage edges; brains + alert
  templates + packs (`install_state`); KG Explorer sanity pass.
- **P2 (self-auditing costs):** nightly rollup from `agent_messages` /
  `query_costs` into `observed` props + doc regeneration on material change.
- **P3 (surface):** Scry `[structure | data | capability]` layer toggle; Hutch
  adjacent-possible entries (assistant may *suggest* installs, never perform
  them); consider pointing warehouse-mcp's `search_tools` at this graph so
  both agent surfaces share one map.

## Acceptance (P0)

- `capability_search('filter text by meaning')` → `means()` in top 3 with
  signature + runnable example, warm < 100ms.
- User runs `create_operator(...)` → `capability_crawl()` → the new operator
  is discoverable by free text.
- Assistant turn "find reviews that sound angry" produces a block using
  `means()` — with NO semantic-SQL section in her prompt.
- "what's our revenue" resolves through the blessed metric, and she says so.
- KG Explorer opens `rvbbit_capabilities` and renders nodes/edges.

## Open questions

- `cost_class` taxonomy granularity (start with the 4 above; refine from v2
  telemetry).
- Crawl-on-DDL hooks vs nightly-only (v1: nightly + manual; revisit).
- Whether `capability_seed` descriptions should embed per-example EXPLAIN
  guidance (probably v2, from telemetry, not hand-written).
- Multi-db: operators/metrics are per-database; one capability graph per db is
  correct and free (graph_id is already db-local).
