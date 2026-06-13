# Cubes — a curated, accelerated, semantic mart layer

**Status:** design / brainstorm-of-record · **Owner:** Ryan · **Date:** 2026-06-12 ·
**Related:** [WAREHOUSE_MCP_PLAN.md](./WAREHOUSE_MCP_PLAN.md) · [CATALOG_KG_PLAN.md](./CATALOG_KG_PLAN.md) ·
[MODEL_STUDIO_PLAN.md](./MODEL_STUDIO_PLAN.md)

> One line: *a **cube** is a wide, reasoned-about, documented join materialized as an
> accelerated rvbbit table — the curated middle between blessed **metrics** and 2,000 raw
> tables, so the agent (and people) look at metrics → cubes → raw, and the join knowledge +
> column semantics live in the warehouse instead of in Claude's head on every query.*

---

## 1. The reframe (the curated middle of a discovery gradient)

Two ends of a spectrum, with a painful gap:
- **Metrics** — precise, blessed, narrow ("the revenue number"). Exact, but only answer
  pre-asked questions, and they're still being built.
- **Raw tables** — everything, but a 2,000-table Salesforce export is 2,000 cryptically-named
  haystacks; semantic search is noisy and the joins are arcane.

A **cube** is the curated thing between them: a documented subject area. Salesforce isn't
2,000 tables — it's ~15 subject areas (accounts, opportunities, leads, activities, campaigns,
quotes, cases…) drowning in junk. One cube per subject area **collapses the agent's search
space from 2,000 tables to ~15 documented cubes.** That's the whole game.

The discovery gradient becomes the agent's **search strategy: metrics → cubes → raw**, high
signal first, raw only as a last resort.

---

## 2. Principles (the decisions)

1. **A cube is a materialized rvbbit table.** `CREATE TABLE cubes.<name> USING rvbbit AS
   <sql>` → it inherits vortex/parquet acceleration, AS-OF, freshness, drift — all for free.
2. **One primitive, many callers.** `define_cube(name, sql, grain, …)` is *the same call*
   whether a human (Cube Studio), the agent (MCP), or SQL invokes it. Authoring isn't forked
   by who does it — a cube is just a wrapped query plus (separately) an enrich pass.
3. **Enrichment is a second pass, not part of creation.** The SQL makes the cube; a later
   `enrich_cube` (LLM-drafted column docs + grain + embeddings) makes it *findable and
   understandable*. The two are decoupled — exactly as Ryan framed it.
4. **The metadata is the moat.** The joins encode arcane schema knowledge once; the per-column
   semantics are what stop Claude inventing `acct_xref_v2_amt`. Mostly LLM-generated, always
   human-editable.
5. **Metrics and dashboards sit on cubes.** A metric's SQL targets `cubes.<name>` (the cube
   does the joins, the metric does the aggregation); dashboards read cubes (one documented
   wide table = the easiest source + cleanest dep extraction).
6. **Curation discipline > coverage.** A few crisp, well-grained cubes beat many mediocre
   ones — too many cubes just recreates the discovery problem.
7. **Reuse the stack.** Cubes are not a new engine; they're a new *kind* of catalog object
   built from primitives already shipped.

---

## 3. What a cube is

- a **definition** (`cube_defs`): name, version, SQL, **grain** (the row meaning), description,
  owner, refresh schedule, labels/category;
- a **materialized table** (`cubes.<name>`, `USING rvbbit`): accelerated, time-travelable,
  fingerprinted, drift-tracked;
- **column semantics** (`cube_columns`): per-column doc / source / meaning / confidence;
- **lineage**: the raw tables/columns it reads (for impact analysis);
- **embeddings**: the cube + its columns in the catalog vector space (a curated layer);
- a **refresh** (pg_cron): re-materialize + re-compact + re-embed on a schedule.

---

## 4. Architecture & data model (mirrors metrics)

```sql
rvbbit.cube_defs (                       -- mirrors metric_defs
  cube_id, name, version, sql, grain text, description, owner,
  refresh_cron text, refresh_mode text,  -- 'full' | 'incremental'
  category, subcategory, labels jsonb, created_at)
rvbbit.cube_columns (                     -- the semantic layer (LLM-drafted, human-edited)
  cube_name, column_name, doc, source_ref, semantics, confidence real, edited_by)

cubes.<name>                              -- CREATE TABLE cubes.<name> USING rvbbit AS <sql>
                                          -- => vortex/parquet, AS OF, accel_freshness, Drift

-- catalog: a kind='cube' node + uses-> edges to source tables, embedded like any object
```

`define_cube(name, sql, grain, description, refresh_cron?, category?)`:
1. upsert `cube_defs` (versioned, like `define_metric`);
2. `CREATE TABLE cubes.<name> USING rvbbit AS <sql>` (or refresh if it exists) → `compact()`
   to accelerate;
3. register a `kind='cube'` catalog node + lineage edges (the source tables from `route_explain`);
4. schedule `refresh_cube(name)` on `refresh_cron` via pg_cron.

`refresh_cube(name)` → re-run the SQL (full rebuild v1; swap-in), `compact()`, re-fingerprint,
re-embed. `enrich_cube(name)` → LLM-draft `cube_columns` + grain from the SQL + samples +
source docs, then embed. `drop_cube(name)` → drop the table + def + catalog node.

---

## 5. The discovery gradient (the agent's new search)

`search_data` ranks by tier, not just similarity: **metric → cube → table**. A hit carries its
kind + tier so Claude prefers the curated thing:

- *"EU revenue last quarter"* → a **metric** (exact, blessed).
- *"which enterprise accounts are slipping"* → the **opportunities cube** (documented, the
  right grain to explore) — Claude reads `describe_cube`, then `run_sql` over the accelerated
  cube.
- only when no metric/cube fits does it fall back to raw tables.

This is the governance gradient from WAREHOUSE_MCP_PLAN §4.3, now with a real curated middle.

---

## 6. Authoring — one primitive, three front doors

Per Ryan: *"if it's just a SQL function with a wrapped query, and then some other query to
enrich it later, there isn't a ton of difference"* — so authoring is uniform:

- **Human** (Cube Studio): write SQL + grain, live-preview, save → `define_cube`.
- **Agent** (MCP): `define_cube` / `propose_cube` (Claude drafts the join from the raw schema +
  FK/catalog hints — one-time reasoning captured forever — and you bless it).
- **SQL**: call `rvbbit.define_cube(...)` directly.

All three land in `cube_defs` + the materialized table. The human-vs-agent question dissolves:
start **human-authored** (V1), add **propose_cube** (agent-drafted → human-blessed) and
direct MCP creation as the loop matures. Enrichment (`enrich_cube`) is the same regardless of
who authored the SQL.

---

## 7. Metadata & semantics (the moat, mostly auto)

`enrich_cube` feeds an LLM the cube SQL + a sample of rows + the **source tables' existing
catalog docs** → drafts: a grain statement, a cube description, and per-column docs (what it
is, where it's from, what it means). Reuses the catalog-LLM / `data_crawl` infra (CATALOG_KG
Phase 4). Output lands in `cube_columns`, **human-editable** in the Inspector. Confidence is
tracked; low-confidence columns are flagged for review. This is what makes a cube *understood*,
not just *fast*.

---

## 8. Embeddings (the curated layer — sketch, details later)

Cubes get nodes in the **same vector space** as the catalog (`embed`/`cosine`/KNN already
exist), embedding the *curated docs* — which are far richer than the inferred fingerprints raw
tables get, so matches are sharper. A "curated" layer alongside structure / data / usage in the
KG; `search_data` runs one KNN across all and applies the metric→cube→table tier boost. **No new
vector system** — a new, higher-quality layer in the one we have. Re-embed on refresh/enrich.

---

## 9. Composition (what falls out for free)

| A cube needs… | …already shipped |
|---|---|
| fast scans | **vortex/parquet** (auto on `compact()`) |
| scheduled refresh + freshness | **pg_cron** + `accel_freshness` ("cube 2 days stale" warnings) |
| "what changed" | **catalog Drift** over the cube's fingerprints |
| time-travel | **AS OF / generations** — *"the sales cube as of Q1"* |
| semantic discovery | **catalog KG + embed/cosine/KNN** |
| lineage + impact | the **dep/impact** machinery the dashboards use (source drifts → which cubes/metrics/dashboards break) |
| learns from use | **`mcp_activity`** ("this cube answers churn questions") |
| metrics on top | a metric's SQL targets `cubes.<name>` |
| dashboards on top | one documented wide table = cleanest source + dep extraction |

A cube ≈ **dbt model + LookML semantic layer + a vector index + time-travel + drift — as one
rvbbit object.**

---

## 10. The MCP surface

- `define_cube` / `propose_cube` / `refresh_cube` / `enrich_cube` / `drop_cube`
- `list_cubes(category?, search?)` · `describe_cube(name)` → grain, column docs, lineage,
  freshness, drift, samples (the agent's grounding)
- `search_data` tiered so cubes outrank raw tables
- the agent's flow: search_data → describe_cube → run_sql over the accelerated cube → (promote
  a recurring question to a metric *on the cube*)

---

## 11. The lens — Cube Studio (mirrors Model Studio / Metrics)

A 3-app folder, same shape as the Metrics apps:
- **Catalog** — browse cubes by category, freshness/drift badges, usage heat (from `mcp_activity`).
- **Creator** — SQL + grain editor with live preview; "materialize", refresh-cron picker;
  `propose_cube` to have Claude draft a join.
- **Inspector** — the cube as an instrument: column docs (view/**edit** the LLM-drafted
  semantics), lineage graph (Scry), freshness/drift, samples, "open in Finder", and a
  "define a metric on this cube" action. Reuses `sql-editor.tsx` / `chart-view.tsx` /
  `data-grid-window.tsx` and the present-mode substrate.

---

## 12. The "weird" / differentiated angles
Time-travelable cubes (reproduce any analysis as-of), drift-aware cubes (Salesforce changes a
field → flagged + impact-traced), searchable-by-*data* (run `data_crawl` over a cube → "find the
cube with European enterprise accounts"), and a catalog that learns which cube answers which
question. None of which dbt/Cube.dev/LookML do natively.

---

## 13. Honest hard parts
- **Refresh cost** — a wide join is expensive to rebuild. V1 = scheduled full rebuilds (pg_cron);
  incremental/CDC is the follow-on (the Temporal Mirror's *compaction-is-the-diff-engine* is a
  natural fit).
- **Grain discipline** — a cube is only useful if its grain is crisp + documented; mixed grains
  are a footgun. Declare + enforce it.
- **Cube sprawl** — curation is the constraint; a few great cubes beat many.
- **Recursive discovery** — Claude drafting a cube still has to *find* the right raw tables in
  the mess; mitigate with FK/catalog hints + iterative refinement (one-time cost, not per-query).
- **Embedding/doc freshness** — re-enrich + re-embed when a cube's data/schema changes.

---

## 14. Phased plan
- **V1 — the primitive.** `define_cube` / `refresh_cube` / `drop_cube` → materialized
  `cubes.<name>` (`USING rvbbit`) + `cube_defs` + a `kind='cube'` catalog node + pg_cron refresh.
  `list_cubes` / `describe_cube`. `search_data` tier-boosts cubes. Manual docs. Metrics can be
  defined over a cube.
- **V2 — the semantic layer.** `enrich_cube` (LLM column docs + grain) + cube embeddings +
  metric→cube→raw ranking + `describe_cube` returns docs/lineage/freshness/drift/samples.
- **V3 — curation + studio + learning.** The lens **Cube Studio** (Catalog/Creator/Inspector
  w/ metadata editing), `propose_cube` (agent-drafted → human-blessed), cube→metric promotion,
  usage-learning, drift→cube-health, incremental refresh.

---

## 15. Open questions
1. **Schema home** — a dedicated `cubes` schema (clean, easy to scope/expose) vs. `rvbbit`? Lean
   `cubes`.
2. **Refresh granularity** — full rebuild vs. incremental, and the swap mechanism (rebuild-then-
   rename vs. truncate+insert) to avoid serving a half-built cube.
3. **Versioning a materialized thing** — `cube_defs` is versioned like metrics, but the table is
   singular; do we keep old versions queryable (AS OF already gives data-time; def-time history
   is the def)?
4. **Embedding model/space** — confirm cubes share the catalog's embedding model so KNN is
   comparable (the "works with the existing vector system" requirement).
5. **Propose-cube grounding** — how Claude finds the right tables to join in a 2,000-table
   schema (FK graph + catalog KG + sampling); how much human review the draft needs.

---

## 16. The pitch
> Tame the 2,000-table warehouse with a handful of **cubes**: documented, accelerated,
> time-travelable subject-area tables that encode the join knowledge once and carry the column
> semantics the agent needs — so Claude looks at metrics, then cubes, and almost never has to
> reason over raw Salesforce again. Metrics and dashboards stand on them; the catalog learns
> from them; and they fall out almost entirely from primitives rvbbit already ships.
