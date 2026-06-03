# Catalog KG — Phase 4+ Roadmap

Deferred ideas for the self-introspecting catalog KG + data search + drift
system. Phases 1–3 (crawl → KG → free-text search) and the Drift layer (snapshot
diffing) are shipped; see `CATALOG_KG_PLAN.md` (§1–11). This file is the parking
lot for what comes next.

---

## Phase 4 — Semantic enrichment (opt-in, LLM/specialist)

Everything here is additive and cached (content-addressed via `rvbbit.receipts`),
so re-crawling unchanged tables is near-free. All of it degrades gracefully when
no model/specialist backend is available.

- **`describe_table` / `describe_column` operators** — define via
  `rvbbit.create_operator(...)` so the prompt is editable from SQL. Feed the
  fingerprint (+ a few sampled rows / example values) → a natural-language
  summary. Fold the result into `properties.description` and into `search_doc`
  before embedding, so search matches on meaning, not just structure.
- **Column semantic-type classification** — `email | currency | date | id |
  name | phone | url | free_text | category | …`. Prefer the embedding-argmax
  path (`rvbbit.semantic_case`) when a specialist is available; fall back to
  `rvbbit.classify` (LLM). Emit `db_column -[has_semantic_type]-> semantic_type`
  nodes/edges.
- **PII detection** — GLiNER pack (`rvbbit.has_pii`, `rvbbit.extract_entities`)
  over sampled values → `db_column -[contains_pii]-> pii_category`. Gate on
  backend health. **Also a drift signal:** "PII appeared in `notes` between
  runs" is a compliance alarm.
- **Topic extraction for free-text columns** — `rvbbit.topics(query_sql, k)`
  cluster exemplars → `db_table -[about_topic]-> topic`. Enriches both search
  and the graph.
- **Re-embed** enriched docs after enrichment; nothing else changes downstream.

## Phase 5 — Scale & freshness

- **Lance ANN for `data_search`** — today it's brute-force cosine over stored
  embeddings (fine to tens of thousands of objects). At larger scale, either
  extend `kg_lance` to embed a `search_doc` text source, or `lance_enable` the
  `catalog_docs` table; swap the cosine scan for ANN. `data_search`'s signature
  stays the same.
- **rvbbit-stats fast path** — when `is_rvbbit_table(rel)` and the table is
  compacted, read `row_groups.stats` (+ `approx_distinct`) instead of scanning
  the heap. Treat as an optimization, never the only path.
- **Incremental re-crawl** — only re-fingerprint tables whose data changed.
  Drive off the compaction generation bump (`rvbbit.tables.next_generation` /
  `row_groups.generation`) for rvbbit tables; for heap tables use a cheap change
  proxy (reltuples delta, last-analyze, or a trigger-maintained dirty flag).
- **Reconcile / prune** — re-crawl currently merges, never deletes, so dropped
  tables/columns leave orphan nodes. Add a prune pass (mark nodes whose `oid` no
  longer resolves) and optionally a DDL event trigger to mark the catalog dirty.
- **Scheduling** — wire `catalog_crawl()` to `pg_cron` (or the harness/cron) so
  the catalog + drift history accrue automatically. Drift quality tracks crawl
  cadence.

## Drift-specific follow-ups

- **Numeric distribution drift** — we now capture `quantiles` (p05…p95). Use
  them for PSI on binned numerics / quantile-shift detection, complementing the
  categorical PSI already shipped.
- **Sampling-confidence surfacing** — snapshots record `sampled` / `n_sampled`.
  The Drift UI should de-emphasize low-confidence diffs (sampling noise vs real
  drift). Offer an exact-mode crawl as the clean baseline.
- **Retention / thinning** — one snapshot row per object per run; embeddings
  dominate size. Add a retention policy (keep last N runs in full, downsample
  older ones; dedupe identical embeddings via `embedding_cache`).
- **Drift as KG evidence** — optionally record notable drift events as
  timestamped evidence on the catalog nodes, so the graph itself tells the
  change story.

## Cross-cutting / bigger bets

- **Multi-database namespacing** — `graph_id = 'db_catalog:{dbname}'` to crawl
  and compare multiple databases / environments (prod vs staging schema drift).
- **"Describe my database" RAG** — `kg_context` over `db_catalog` to answer
  natural-language questions about the schema ("which tables hold customer
  contact info and how are they joined?"), reusing the KG retrieval surface.
- **Packaging** — cut a version bump + `sql/pg_rvbbit--X--Y.sql` migration delta
  so the catalog/drift SQL ships via a clean `CREATE EXTENSION` /
  `ALTER EXTENSION UPDATE` instead of `psql -f`.
