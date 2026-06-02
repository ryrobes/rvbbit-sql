# Self-Introspecting Catalog KG + Data Search

Status: **design + Phase 1/2 landed (SQL)**. Date: 2026-06-02.

A crawler that reads a live Postgres database, **fingerprints** every user table
(structural stats + example distinct values), and materializes a **knowledge
graph about the database itself** into the existing rvbbit KG — then exposes a
**free-text KNN "data search"** surface over the fingerprints so the rvbbit-lens
SQL desktop can answer "which tables/columns are about X?".

It reuses the KG system we already shipped (`docs/KNOWLEDGE_GRAPH.md`) rather
than inventing a new store. The catalog lives in its own graph
(`graph_id = 'db_catalog'`), so it never collides with user KG data and renders
in the existing KG Explorer for free.

---

## 1. Goals & non-goals

**Goals**

- One call (`rvbbit.catalog_crawl()`) builds a KG of `schema → table → column`
  (+ FK edges) with a rich JSONB property bag per object: types, null fraction,
  NDV, min/max, **example distinct values**, comments, PK/FK, size, row count.
- A deterministic **fingerprint document** per table/column, embedded for
  **free-text KNN search** (`rvbbit.data_search('customer contact info', 20)`).
- A **Data Search** window in rvbbit-lens; the catalog graph is browsable in the
  existing KG Explorer.
- Works on **ordinary heap tables** (not just rvbbit-managed columnar tables) and
  with **zero LLM calls** in the base build.

**Non-goals (for the base build)**

- LLM/NL table descriptions, column semantic-type classification, PII tagging —
  all deferred to Phase 4 (opt-in, cached).
- Lance ANN indexing of fingerprints — deferred to Phase 5; MVP uses brute-force
  cosine, which is fine to tens of thousands of objects.
- Multi-database / cross-server crawling — single connected database for now
  (the graph id is parameterizable for later multi-DB namespacing).

---

## 2. What already exists (reused as-is)

| Capability | Primitive (file:line) |
|---|---|
| KG node/edge upsert | `rvbbit.kg_assert_node` / `kg_assert_edge` (`kg.rs:448 / 592`) |
| KG provenance | `rvbbit.kg_link_evidence` — `source_table regclass` links to live objects (`kg.rs:510`) |
| KG read/traverse | `kg_context / kg_neighbors / kg_paths` (`kg.rs:1192/1017/1109`) |
| Embeddings (cached) | `rvbbit.embed(text, specialist) → real[]` (`embeddings.rs:226`); `rvbbit.embedding_cache` |
| Per-column stats (fast path) | `rvbbit.approx_distinct(oid, col)`, `rvbbit.row_groups.stats` jsonb (`sketches.rs:20`) — **rvbbit-AM tables only** |
| rvbbit-table check | `rvbbit.is_rvbbit_table(regclass)` (`catalog.rs:4527`) |
| Lens SQL seam | `POST /api/db/query` → `executeQuery`; typed fetchers in `src/lib/rvbbit/*.ts` |
| Lens schema inventory | `loadSchema` (`rvbbit-lens/src/lib/db/schema.ts:73`) — excludes system schemas |
| Lens KG UI | `kg-explorer / kg-browser / kg-entity-detail` windows + `lib/rvbbit/kg.ts` |
| Lens search box pattern | `SeedPicker` (`kg-explorer-window.tsx:577`); `openTableFromFinder` (`desktop-shell.tsx:970`) |

**Missing (built here):** the catalog crawler, the fingerprint document store +
search function, and the lens Data Search window.

---

## 3. Two corrections to early recon (important)

1. **`rvbbit.cosine(real[],real[])` is NOT SQL-callable.** It is a private Rust
   fn (`embeddings.rs:233`); only `embed` and `similarity` are `#[pg_extern]`.
   → MVP search computes cosine **in pure SQL** over a stored `embedding real[]`
   (`unnest(a,b)` dot/norm). A one-line `#[pg_extern]` wrapper for `cosine`
   is an easy future fast-path but is intentionally avoided so Phase 1/2 are
   pure SQL and load with `psql -f` against an already-installed extension.

2. **Crawl writes must pass `match_threshold => 0.0`, not `1.0`.**
   `kg_assert_node` calls `kg_resolve_node`, whose tiers 2 (Lance) and 3
   (per-row `rvbbit.similarity()` scan over every node of the kind) run **only
   when `threshold > 0`** (`kg.rs:409`). At `1.0`, every column assert triggers
   an O(N) embedding scan → **O(N²) embeddings per crawl**. At `0.0` only the
   exact alias / `label_norm` tier runs: deterministic dedup, **zero
   embeddings**. This is the single most important correctness/perf rule.

---

## 4. Data model — the `db_catalog` graph

All writes target `graph => 'db_catalog'` (parameterizable; use
`db_catalog:{dbname}` if you later crawl multiple databases).

### Node kinds (lowercase/snake — `kind` is normalized by `kg_normalize_label`)

| kind | **label (fully-qualified!)** | key properties |
|---|---|---|
| `db_schema` | `public` | `n_tables`, `comment` |
| `db_table` | `public.orders` | `oid`, `relkind`, `n_rows`, `n_sampled`, `size_bytes`, `comment`, `is_rvbbit`, `n_columns`, `profiled_at`, `search_doc` |
| `db_column` | `public.orders.status` | `data_type`, `ordinal`, `nullable`, `default`, `is_pk`, `is_fk`, `fk_target`, `n_rows`, `n_nulls`, `null_frac`, `ndv`, `ndv_method`, `min`, `max`, `example_values`, `search_doc` |

> ⚠️ **Fully-qualified labels are mandatory.** `label_norm` is unique per
> `(graph, kind)`. Two columns named `id` both normalize to `id` under
> `db_column` and would **merge into one node**. Labels are
> `schema` / `schema.table` / `schema.table.column`. Unqualified names live in
> the property bag.

### Edge predicates (snake, normalized by `kg_normalize_predicate`)

- `db_schema -[has_table]-> db_table`
- `db_table -[has_column]-> db_column`
- `db_column -[references]-> db_column` (FK target column)

Phase 4 adds: `db_column -[has_semantic_type]-> semantic_type`,
`db_column -[contains_pii]-> pii_category`, `db_table -[about_topic]-> topic`.

### Provenance

Each node links evidence via `kg_link_evidence(target_node_id, source_table =>
'<the real relation>'::regclass, source_pk => '<oid>', evidence_text =>
comment-or-doc, graph => 'db_catalog')`. Because `source_table` is a `regclass`,
the lens "open source row / open in catalog" affordances work natively.

### Write-ordering gotchas (baked into the crawler)

- `kg_assert_edge` asserts endpoints with **empty `{}` properties** → assert
  every node **with** its bag first, then assert edges.
- All asserts use `match_threshold => 0.0` (see §3).
- Re-crawl **merges, never deletes** → dropped objects orphan → Phase 5 prune.

---

## 5. Fingerprinting (heap-first, plain SQL)

Ordinary heap tables have **no** rvbbit stats (those live in `row_groups.stats`
only for rvbbit-AM tables after `compact()`), and there is **no distinct-value
sampling anywhere** in rvbbit. So the crawler fingerprints with plain SQL,
robust to arbitrary column types:

`rvbbit.catalog_fingerprint_table(rel regclass, sample_rows int, examples_k int)
→ jsonb`

Per table: schema/table/relkind/comment/size/oid from `pg_catalog`, exact
`n_rows = count(*)`. If `n_rows > sample_rows` and the relkind is sampleable,
columns are profiled over a `TABLESAMPLE SYSTEM` sample (page-level, cheap);
otherwise over the whole relation.

Per column (each aggregate guarded by a plpgsql `EXCEPTION` block so exotic types
— `json`, arrays, `bytea`, geometric — degrade to `NULL` instead of failing):

- `n_nulls`, `null_frac` (from `count(*)` vs `count(col)`),
- `ndv` via `count(distinct col)` over the sample (`ndv_method = exact|sampled`),
- `min`/`max` cast to `text`,
- `example_values`: top-`k` most frequent non-null values as text
  (`GROUP BY col ORDER BY count(*) DESC LIMIT k`) — the strongest "what is this
  about" signal.

Fast path (Phase 5): if `is_rvbbit_table(rel)` and compacted, read
`row_groups.stats` + `approx_distinct` instead of scanning.

---

## 6. Search — fingerprint documents + brute-force cosine

The embedded unit is a deterministic **fingerprint document** assembled from
structure + example values. Even with no LLM, this carries strong semantic
signal because column names and sample values embed meaningfully:

```
db_column doc:  "Column public.orders.status (text). 4 distinct, 0% null.
                 Examples: paid, pending, refunded, cancelled.
                 In table public.orders."
db_table doc:   "Table public.orders — 1.24M rows. Columns: id (integer),
                 customer_id (integer), email (text; e.g. a@acme.com),
                 total_cents (integer), status (text; paid, pending, refunded),
                 created_at (timestamp)."
```

Stored in `rvbbit.catalog_docs(node_id, graph_id, kind, schema, rel, col, doc,
embedding real[], embedded_at)`. `doc` also lands in the node's
`properties.search_doc`.

`rvbbit.data_search(query text, k int, kinds text[], graph text)
→ TABLE(node_id, kind, schema, rel, col, score, doc)` embeds the query once
(cached) and ranks by cosine computed in SQL:

```sql
score = sum(d*q) / (sqrt(sum(d*d)) * sqrt(sum(q*q)))   -- over unnest(embedding, qvec)
```

If no embed specialist is configured (query embedding empty), it **falls back to
an ILIKE rank over `doc`**, so search still works on a bare install.

Scale path (Phase 5): extend `kg_lance` to embed a `search_doc` text source (or
`lance_enable` `catalog_docs`); swap the cosine scan for ANN — `data_search`'s
signature is unchanged.

---

## 7. Lens UI — Data Search window

Pure-additive, riding the existing SQL seam:

- `src/lib/rvbbit/data-search.ts` — `searchData(connId, query, k, kinds)`,
  `crawlCatalog(connId, opts)`, `fetchCatalogRun(connId)`. Mirrors `lib/rvbbit/kg.ts`.
- `src/components/desktop/data-search-window.tsx` — debounced search box (clone
  `SeedPicker`), results grouped by table with score bars, matched-doc snippet,
  and (Phase 4) semantic-type/PII chips. Hit actions: **Open table**
  (`openTableFromFinder`) and **Open in catalog graph** (existing KG Explorer at
  `graph='db_catalog'`).
- Wire-up (mechanical): `DesktopWindowKind` + payload (`types.ts`),
  `renderWindowBody` case + `iconForKind` + open helper (`desktop-shell.tsx`),
  menu item near Knowledge Graph (`desktop-menu-bar.tsx:201`); gate on
  `schema.hasRvbbit`. A "Crawl / Refresh catalog" admin action calls
  `rvbbit.catalog_crawl()` and shows progress via a run view.

Bonus: because the crawler emits the structural graph into `db_catalog`, the
**existing KG Explorer already visualizes the database as a graph** — one menu
item ("Browse Database Graph") gets it.

---

## 8. Phased plan

| Phase | Deliverable | LLM? | Where |
|---|---|---|---|
| **1. Structural crawler** | `catalog_fingerprint_table`, `catalog_crawl`, `catalog_docs`/`catalog_runs`, KG materialization | none | `crates/pg_rvbbit/sql/catalog_kg.sql` (+ `catalog_kg.rs` `extension_sql_file!`) |
| **2. Search fn** | `rvbbit.data_search()` (brute-force cosine + ILIKE fallback) | none | same SQL file |
| **3. Lens UI** | Data Search window + `data-search.ts`; KG Explorer over `db_catalog`; crawl button | none | `rvbbit-lens/src` |
| **4. Semantic enrichment** | `create_operator('describe_table'/'describe_column')`, semantic-type via `semantic_case`/`classify`, PII via GLiNER; fold into `search_doc`, re-embed | opt-in, cached | SQL + operators |
| **5. Scale & freshness** | Lance ANN swap; rvbbit-stats fast path; incremental re-crawl via generation bumps; prune dropped objects; pg_cron | none | Rust `kg_lance` + SQL |

Phases 1–3 deliver a fully working, zero-LLM data-search experience.

---

## 9. Build / run

The implementation lives in `crates/pg_rvbbit/sql/catalog_kg.sql` and is compiled
into the extension via `crates/pg_rvbbit/src/catalog_kg.rs`
(`extension_sql_file!`). Two ways to get it into a database:

- **Dev (no rebuild):** it only depends on already-shipped functions, so load it
  directly: `psql "$RVBBIT_DSN" -f crates/pg_rvbbit/sql/catalog_kg.sql`.
- **Packaged:** rebuild + `make reload-extension` (`ALTER EXTENSION UPDATE`)
  once a version bump + migration delta is cut.

Smoke test:

```sql
SELECT rvbbit.catalog_crawl();                       -- crawl all user schemas
SELECT * FROM rvbbit.data_search('customer email', 20);
SELECT count(*) FROM rvbbit.kg_nodes WHERE graph_id = 'db_catalog';
```

---

## 10. Open items / decisions taken

- **Crawler impl:** SQL/plpgsql in-DB (chosen) — editable, no recompile to load.
- **Search index:** brute-force cosine now, Lance later (chosen).
- **Semantic layer:** deterministic first, enrich later (chosen).
- **Sampling bias:** `TABLESAMPLE SYSTEM` is page-level (clustered) — acceptable
  for fingerprints; revisit if NDV/example quality suffers on clustered tables.
- **Embedding dependency:** search requires an `embed` specialist; crawler and
  `data_search` degrade gracefully (NULL embeddings / ILIKE fallback) without one.
- **Freshness:** re-crawl merges, never deletes; Phase 5 adds prune + incremental.
