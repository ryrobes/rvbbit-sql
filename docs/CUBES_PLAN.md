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

`refresh_cube(name)` → **in-place snapshot reload, not a rename-swap.** Reload the SQL result
into the *same* `cubes.<name>` (truncate-and-load, table identity preserved) + `compact()` =
**a new generation/snapshot**. Crucially this **reuses the Temporal Mirror's snapshot-load
machinery** (`snapshot_load` + `compact(keep_heap)`): each refresh is a snapshot, old
generations retained (with a reaper/retention policy), so **the refresh history *is* the
cube's AS-OF timeline** — *"the sales cube as of last Tuesday's refresh."* A rebuild-then-
rename would orphan the generation chain and kill time-travel; that's why we don't swap.
Then re-fingerprint + re-embed. `enrich_cube(name)` → LLM-draft `cube_columns` + grain from
the SQL + samples + source docs, then embed. `drop_cube(name)` → drop the table + def + node.

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

**Cube packs (known schemas ship as templates).** Grounding a `propose_cube` in a 2,000-table
custom schema is hard — but *known* SaaS schemas aren't custom. Salesforce, HubSpot, Stripe,
etc. have a fixed object model (Account ← Opportunity ← OpportunityLineItem, AccountId/
OpportunityId FKs, standard fields). So ship **packs**: parameterized cube templates (the
opportunities cube, the accounts cube…) that bind to the user's actual table/column names
(Salesforce exports prefix/rename) + come with the docs pre-written. For known sources this is
a near-one-click curated layer; for custom schemas, fall back to FK-graph + KG + sampling
reasoning. **Salesforce is the obvious first pack** — highest pain, fully known patterns.

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
- **V1 — the primitive. ✅ LANDED** (`sql/migrations/0004_cubes.sql`). `define_cube` /
  `refresh_cube` / `drop_cube` → materialized `cubes.<name>` (`USING rvbbit`, shape inferred
  via CTAS `WITH NO DATA`) + `cube_defs`/`cube_control` + a `kind='cube'` catalog node
  (`register_cube_node`, embed best-effort). `refresh_cube` **reuses `rvbbit.snapshot_load`**
  (TRUNCATE+load+compact+`set_visible_floor`) so each refresh REPLACES the current view *and*
  retains an AS-OF generation. `rvbbit.cubes()` / `rvbbit.describe_cube()`; warehouse-MCP
  `list_cubes`/`describe_cube` + `search_data` tier-boost (metrics→cubes→raw, verified). Manual
  docs; metrics can target `cubes.<name>`. (pg_cron auto-refresh wiring + numeric-column cubes
  — the latter needs the text-surrogate build, commit 202f985 — are the small follow-ons.)
- **V2 — the semantic layer. ✅ LANDED** (`sql/migrations/0005_cubes_enrich.sql`).
  `rvbbit.cube_columns` (per-column doc/semantics/source_ref/confidence/edited_by, human-
  editable) + `enrich_cube(name, sample_rows, overwrite_edited)` — ONE LLM call (the new
  `cube_enrich` jsonb operator, same `create_operator`/`_exec_op_jsonb` path + retry-validator
  as `triples`) drafts a description, grain, and per-column docs from the cube SQL + a clamped
  row sample + the **source tables' catalog docs**. Lineage via `_cube_source_tables` (walks
  `EXPLAIN (FORMAT JSON, VERBOSE)` for every base relation — raw + rvbbit; **VOLATILE** since
  EXPLAIN is barred in non-volatile fns). The enriched docs fold back into a far richer catalog
  embedding (`register_cube_node` now embeds the curated column docs, ~1.5KB vs V1's terse
  types → sharper KNN). `describe_cube` now returns columns-with-docs + lineage + auto/human
  description + freshness + a 5-row sample (so the MCP `describe_cube` tool carries the
  semantics automatically — no server change). `set_cube_column_doc` for human corrections;
  `drop_cube` cleans `cube_columns`. Verified live: a 7-column `sales_orders` cube enriched with
  lineage-traced `source_ref`s (incl. `derived:` exprs), cube ranks **1.000** (top) on semantic
  `data_search`. Enrich stays SQL/Studio-side (a write + LLM cost); MCP enrich is a V3 item.
- **V3 — curation + authoring + health. ✅ LANDED** (backend `sql/migrations/0006_cubes_v3.sql`
  + the lens **Cube Studio**).
  - **`propose_cube(subject, seed_tables?, schema?, max_tables?)`** — an LLM (new `propose_cube_draft`
    jsonb operator, same path as `cube_enrich`) drafts a wide join SQL + grain + description + name
    from FK edges (`pg_constraint`, oid-matched), `data_search` candidates (→ `information_schema`
    fallback when uncrawled), per-table columns + catalog docs. Returns a DRAFT only (never persists;
    a human blesses via `define_cube`). Verified: drafts a correct FK-joined cube, confidence 0.97,
    draft SQL EXPLAINs, bless-loop produces a live cube.
  - **Cube packs** — `rvbbit.cube_packs`/`cube_packs_latest`/`pack_bindings` + a seeded
    `salesforce.opportunities` pack (canonical join template + per-column docs + binding field specs).
    `fuzzy_suggest_bindings(pack, schema?)` (lexical + type-family match, no crawl needed),
    `apply_cube_pack(pack, bindings)` (substitute + EXPLAIN-validate, dry-run), `define_cube_from_pack`
    (materialize + pre-seed curated docs at `edited_by='pack'` so `enrich_cube` preserves them).
    Verified: synthetic renamed CRM schema → fuzzy-bound → live 200-row documented `sf_opportunities`.
  - **`promote_cube_to_metric(cube, metric, …)`** — zero-copy scalar row-count metric over
    `cubes.<name>` (`labels.cube_source` for reverse lookup); other business scalars are still
    hand-written over the cube.
  - **`cube_health(name)`** (folded into `describe_cube` as `health`) — freshness (keyed off
    `cube_control.refreshed_at`, not the parquet clock) / staleness / drift (`accel_freshness`,
    null-guarded) / usage + a skip/delta/full-rebuild recommendation.
  - **MCP**: `propose_cube` added (read-only-safe draft, audited); all persisting/DDL cube ops stay
    primary-only (the mirror proposes, the human blesses).
  - **Lens Cube Studio** (rvbbit-lens) — three desktop apps mirroring the Metrics studio:
    **Cube Catalog** (browse/search/sort), **Cube Creator** with three modes —
    **Manual** (SQL + live LIMIT-5 preview), **Propose** (subject + optional seed-tables/schema →
    `propose_cube` → pre-fills the Manual form to review & Save) and **From Pack** (pick a SaaS pack
    → auto-suggest bindings → preview resolved SQL → `define_cube_from_pack`) — and **Cube Inspector**
    (Overview/Columns/Health/Lineage; inline column-doc editing via `set_cube_column_doc`,
    Refresh/Enrich/Promote-to-Metric actions). New `lib/rvbbit/cubes.ts` + `cube-shared.tsx`;
    registered in `desktop-shell.tsx`/`types.ts` under a new **Cubes** launcher folder. tsc + eslint
    clean.
  - **Proposal queue** (migration 0008) — `propose_cube` drafts were ephemeral; now the MCP
    `propose_cube` *tool* logs every draft to `rvbbit.proposals` (generic over kind — cubes now,
    metrics later). `record_proposal` (dedup-supersedes a prior pending same-name), `accept_proposal`
    (→ `define_cube` + optional enrich, links `result_name`), `reject_proposal`, `proposals()` list.
    A lens **Cube Proposals** inbox triages them (review subject/lineage/confidence/rationale +
    editable name/SQL → Accept / Accept & Enrich / Reject). The accept/reject signal is the
    substrate for the future learning loop.
  - Deferred to v3.1: incremental/delta refresh (the executor; `cube_health` already emits the
    skip/delta/full signal), the Inspector AS-OF time-travel scrubber, an MCP enrich-preview tool,
    and `propose_metric` (the proposals table is already kind-generic for it).

---

## 15. Decisions & open questions
- **DECIDED — Schema home:** `cubes.<name>`. Clean, easy for the warehouse-mcp schema-scoping
  to expose, keeps the curated layer out of `rvbbit` internals.
- **DECIDED — Refresh:** **in-place truncate-and-load (a snapshot generation), not a rename-
  swap** — rename orphans the time-travel chain; in-place reload preserves table identity so
  each refresh is an AS-OF snapshot. Reuse the Temporal Mirror's `snapshot_load` + `compact`
  machinery + a retention/reaper for old generations. Full rebuild in V1; incremental
  (compaction-is-the-diff-engine) is the follow-on.
- **DECIDED — Propose-cube grounding:** **all three** (FK graph + catalog KG + sampling) for
  custom schemas, **plus cube packs** (parameterized templates) for known SaaS schemas.
  **Salesforce ships first** — known patterns mean templates beat from-scratch reasoning.

Still open:
1. **Versioning a materialized thing** — `cube_defs` is versioned like metrics, but the table is
   singular; AS OF already gives data-time, def-time history is the def — confirm we don't need
   per-version tables.
2. **Embedding model/space** — confirm cubes share the catalog's embedding model so KNN is
   comparable (the "works with the existing vector system" requirement).
3. **Refresh atomicity** — truncate+load briefly locks (ACCESS EXCLUSIVE on TRUNCATE) or leaves
   dead tuples (DELETE); pick the load path that never serves a half-built cube (the Temporal
   Mirror already solved this with per-table COMMIT + keep_heap — inherit it).
4. **Pack binding** — how a Salesforce pack maps its canonical objects to the user's
   actual (prefixed/renamed) table+column names — fuzzy match + confirm, or a small mapping file.

---

## 16. The pitch
> Tame the 2,000-table warehouse with a handful of **cubes**: documented, accelerated,
> time-travelable subject-area tables that encode the join knowledge once and carry the column
> semantics the agent needs — so Claude looks at metrics, then cubes, and almost never has to
> reason over raw Salesforce again. Metrics and dashboards stand on them; the catalog learns
> from them; and they fall out almost entirely from primitives rvbbit already ships.
