# rvbbit

A columnar storage extension for PostgreSQL 18 that doubles as the
SQL-native substrate for cached, composable semantic operators —
embeddings, LLM judgments, topic clustering, evidence retrieval —
all in plain SQL with persistent caches.

**Status:** v0.8.0. Columnar TAM + custom scan + per-group stats +
predicate pushdown + LIKE/ILIKE pushdown + COUNT/SUM rewriter +
semantic operators with persistent judgment cache + JIT embeddings
with content-addressed cache + k-nearest-neighbor + topic clustering +
EXPLAIN SEMANTIC. 163 deterministic tests pass; ClickBench at 1M
rows beats plain PG, Citus, Hydra, AlloyDB.

## The semantic SQL surface

```sql
-- Register an embedder once (Ollama / OpenAI / vLLM, anything OpenAI-compat)
SELECT rvbbit.register_backend(
  'embed', 'http://localhost:11434/v1/embeddings', 'openai',
  backend_opts => '{"model":"nomic-embed-text"}'
);

-- Top-k semantic retrieval in one SQL call (cached after first run)
SELECT * FROM rvbbit.knn_text('tickets'::regclass::oid, 'body',
                              'angry customer want refund', 10);

-- "What's in my data?" — k-means topic clustering
SELECT cluster_id, count, exemplar,
       rvbbit.about(exemplar, 'one-word topic label') AS label
FROM rvbbit.topics('SELECT body FROM tickets', 5);

-- Per-row similarity, ORDER BY composable
SELECT body, rvbbit.similarity(body, 'angry customer') AS score
FROM tickets
ORDER BY score DESC LIMIT 10;

-- Highlighted snippets — show *why* a row matched
SELECT body, rvbbit.text_evidence(body, 'angry refund')
FROM tickets WHERE rvbbit.means(body, 'unhappy customer');

-- Cost preview before paying for it
SELECT rvbbit.token_count(body, 'cl100k_base') FROM tickets;

SELECT * FROM rvbbit.explain_semantic(
  $q$ SELECT * FROM tickets WHERE rvbbit.means(body, 'cancellation risk') $q$
);
```

Every cached value (embeddings, judgments, predicate bitmaps) is a
first-class catalog object. Backed up by `pg_dump`. Invalidated by
versioned keys. Inspectable via SQL:

```sql
SELECT * FROM rvbbit.embedding_cache_stats();
SELECT * FROM rvbbit.judgment_stats('means');
SELECT * FROM rvbbit.bitmap_stats('tickets'::regclass::oid);
```

## Why another columnar Postgres?

Existing options (Hydra, Citus Columnar, cstore_fdw) all try to be a
heap-replacement: they cram column data into 8KB pages so they can ride
shared buffers, which destroys compression and forces brittle MVCC hacks.
They also rely on TOAST for variable-length data, which is precisely
wrong when your "wide" columns are 4KB LLM responses on every row.

Rvbbit takes a different bet:

- **Postgres is the control plane** (catalog, transactions, planner, snapshots).
- **A separate data plane** holds immutable Parquet row groups outside the
  page cache, plus a small heap "catcher" table that absorbs writes with
  full MVCC.
- **Compaction** drains catcher → new row group; deletes go to a tombstone log
  applied as a bitmap during scans.
- **No TOAST.** Variable-length columns (JSONB, text) use per-row-group
  contiguous buffers with ZSTD shared dictionaries.
- **Cached semantic state is catalog state.** LLM judgments, embeddings,
  predicate bitmaps live in regular PG tables — survives restarts,
  backed up with the rest, invalidated by versioned keys.

See `docs/DESIGN.md` (TODO) for full architectural notes.

## What rvbbit ISN'T trying to be

- **pgvector replacement.** pgvector handles vector indexes (HNSW, IVF);
  rvbbit's `cosine_vec` over `real[]` is sufficient for sub-1M-row
  workloads but doesn't ship an ANN index. Use both.
- **ParadeDB replacement.** ParadeDB owns BM25-in-Postgres. Rvbbit's
  `text_evidence` is a lightweight inline matcher; the Tantivy sidecar
  for production BM25 is a future ticket.
- **A Python framework.** Embedders / LLMs are HTTP endpoints rvbbit
  delegates to. No model files bundled.

## Project layout

```
crates/
  pg_rvbbit/         pgrx extension: TAM, custom scan, semantic operators,
                     embeddings, topics, EXPLAIN SEMANTIC
  rvbbit_storage/    pure Rust: row group read/write, metadata, delete log
docker/
  Dockerfile.rvbbit  builds extension against system PG18
  docker-compose.yml heap baseline + rvbbit + bench harness
bench/
  clickbench/        ClickBench 43-query suite vs 7 systems
  tpch/              TPC-H-derived analytics suite
  tatp/              TATP-style transactional suite
  columnar_comparison/ NYC taxi bench
capabilities/        Hugging Face backend/operator manifests + scaffolding
```

## Quick start

```bash
docker compose -f docker/docker-compose.yml up -d --build
psql postgresql://postgres:rvbbit@localhost:55433/bench \
  -c 'SELECT rvbbit.rvbbit_version();'
```

To use the semantic surface, register a specialist endpoint (Ollama is
the easiest: `ollama pull nomic-embed-text`). All `rvbbit.*` functions
that take a specialist arg fall back to one literally named `embed`
when omitted.

## Caches at a glance

| Catalog table | Caches | Key | Invalidation |
|---|---|---|---|
| `rvbbit.receipts` | LLM operator results | blake3(op + model + inputs + prompt_seed) | model change → auto-miss |
| `rvbbit.embedding_cache` | Vector embeddings | blake3(specialist + text) | `embedding_purge(specialist)` |
| `rvbbit.semantic_bitmaps` | Predicate results per row group | blake3(predicate_name + model_version) | `bitmap_drop(rel, name, ver)` |

## Tests

```bash
docker compose exec bench python -m pytest /tests -q --ignore=test_operators_live.py
# 163 passed
```

Live LLM tests live in `test_operators_live.py` and need an
`OPENROUTER_API_KEY` (skipped otherwise).

## Roadmap

- [x] Phase 0 — scaffolding, Docker, extension loads
- [x] Phase 1 — TAM registration, INSERT/SELECT through catcher
- [x] Phase 2 — compaction, parquet row groups, union scan
- [x] Phase 3 — delete log, UPDATE = delete+insert, zone maps
- [x] Phase 4 — JSONB without TOAST (per-row-group ZSTD dictionary)
- [x] Phase 5 — LLM-synthetic + ClickBench benchmarks vs heap
- [x] Phase 6 — bloom-equivalent (per-group stats), projection pushdown
- [x] Phase 7 — Semantic operator runtime, persistent judgment cache
- [x] Phase 8 — JIT embeddings, knn_text, text_evidence, topics
- [ ] Phase 9 — Bitmap auto-routing (RYR-300), Tantivy sidecar (RYR-293),
                Incremental semantic MVs (RYR-292), HLL sketches (RYR-291),
                DBSP-style real incremental view maintenance (RYR-294)
