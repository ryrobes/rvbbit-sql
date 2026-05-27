# Rvbbit as a lakehouse engine

This document covers the analytic-storage features rvbbit gained in
late May 2026: in-process DataFusion, time travel, merge-on-read
deletes, ObjectStore tiering, and Lance-backed vector search. All of
these are exposed through ordinary SQL — no special syntax, no
operator-only DSL, no porting effort for application code.

The five things rvbbit can do that no other Postgres extension can:

1. Read parquet row groups through an in-process Arrow/DataFusion
   stack with **no sidecar process** — eliminates the
   fork-exec-IPC tax on every query.
2. Time-travel reads (`AS OF GENERATION N` and `AS OF TIMESTAMP ts`)
   on the analytic columnar layer.
3. Merge-on-read tombstones with generation-aware visibility.
4. Per-row-group ObjectStore tiering (file:// today, s3:// + gs://
   on the same code path) with **transparent SQL semantics** —
   `SELECT * FROM cold_table` keeps working.
5. Lance-format vector columns with IVF-PQ vector indices,
   auto-refreshed on `compact()`, queryable via a single
   catalog-driven `rvbbit.knn()` call.

## In-process DataFusion

A `SET rvbbit.df_inprocess` GUC controls whether the `datafusion_*`
routes go through an in-backend `SessionContext` or the legacy
`rvbbit-duck` sidecar. **Default is `on`** as of the polish commit:

```sql
-- Default: in-process DataFusion (current behavior)
SHOW rvbbit.df_inprocess;             -- (unset, so the default 'on' applies)
SET LOCAL rvbbit.df_inprocess = off;  -- force back to the sidecar route

-- Direct entry point without the router dispatch glue
SELECT rvbbit.df_inprocess_query(
    'SELECT count(*) FROM my_rvbbit_table',
    100);
-- → {"status":"ok","row_count":1,"columns":["count"],"rows":[[42]]}
```

Per-backend behavior is controlled by these env vars:

- `RVBBIT_DF_THREADS=N` — multi-thread tokio runtime workers
  and DataFusion `target_partitions` (default `min(num_cpus, 8)`;
  `0` = current_thread).
- `RVBBIT_NATIVE_THREADS=N` — native row-group worker threads for
  CustomScan fast paths that can split independent row groups, currently
  dictionary-backed text top-count paths (default `min(num_cpus, 8)`,
  clamped to row-group count).
- `RVBBIT_DUCK_BIN` / `RVBBIT_DUCK_DSN` — sidecar fallback config
  (kept as a route option for A/B and to handle anything in-process
  DF refuses to plan).

## Time travel: `AS OF GENERATION`

Every `compact()` call atomically allocates a new monotonic
`generation` per table (advisory-lock-protected so concurrent
compacts on the same table serialize). Every row group is stamped
with the generation that wrote it.

```sql
-- Inspect the timeline
SELECT * FROM rvbbit.list_generations('orders'::regclass);
-- generation | committed_at                  | n_rows | n_row_groups
-- 4          | 2026-05-25 23:18:14.449-04    | 100    | 1
-- 3          | 2026-05-25 23:18:13.129-04    | 100    | 1
-- 1          | 2026-05-25 23:18:11.792-04    | 100    | 1

-- Read at a specific generation
SET LOCAL rvbbit.as_of_generation = 3;
SELECT customer_id, sum(amount) FROM orders GROUP BY 1;

-- Latest generation present
SELECT rvbbit.current_generation('orders'::regclass);
```

Row groups with `generation > asof` are excluded from the scan.
Tombstones with `deleted_generation > asof` are NOT applied. Together
those give you the exact state at that point in time.

## Time travel: `AS OF TIMESTAMP`

Resolves a wall-clock time to a generation, then sets the GUC:

```sql
-- Read the orders table as of 24 hours ago
SELECT rvbbit.set_as_of('orders'::regclass, now() - interval '24 hours');
-- → returns the generation it resolved to

SELECT count(*), avg(amount) FROM orders WHERE status='ok';

-- Reset to latest
SELECT rvbbit.set_as_of_reset();
```

The timestamp is resolved against the `rvbbit.generations` index
table that `compact()` populates with `committed_at = clock_timestamp()`
per non-empty compaction. Timestamps earlier than the first generation
resolve to 0 (no filter, returns the latest view — documented
quirk).

## Merge-on-read deletes

DELETE semantics on a parquet-backed relation, without a parquet
rewrite. Tombstones land in `rvbbit.delete_log` with a `deleted_xid`
and a `deleted_generation`; the custom scan node loads them into a
per-row-group roaring bitmap at scan begin and skips matching
ordinals in the hot loop.

```sql
-- Tombstone specific (row_group, ordinal) tuples — all in one generation
SELECT rvbbit.tombstone_batch(
    'orders'::regclass,
    '[{"rg":0,"ord":17}, {"rg":1,"ord":4}]'::jsonb
);

-- Tombstone a single row
SELECT rvbbit.tombstone('orders'::regclass, rg_id => 0, ordinal => 5);

-- How many tombstones are visible at a given AS OF?
SELECT rvbbit.tombstone_count('orders'::regclass, 5);

-- Allocate a generation without writing tombstones (advanced)
SELECT rvbbit.allocate_generation('orders'::regclass);
```

AS OF interplay: tombstones at `deleted_generation > asof` are
treated as future events and NOT applied. Tombstones at
`deleted_generation <= asof` ARE applied. So AS OF before the delete
shows the row; AS OF at-or-after the delete hides it.

## `update_rows` — UPDATE via composition

PG can't lower the SQL-standard `UPDATE` syntax onto a parquet
relation without exposing per-row identity in the scan (a multi-week
change). `rvbbit.update_rows` is the bridge: it composes
`tombstone_batch` + an INSERT + `compact()` into one transaction.

```sql
SELECT rvbbit.update_rows(
    'orders'::regclass,
    '[{"rg":0,"ord":17}, {"rg":1,"ord":4}]'::jsonb,
    $$INSERT INTO orders (status, amount) VALUES ('shipped', 99),
                                                  ('shipped', 145)$$);
-- → {"tombstone_generation": 4, "insert_generation": 5}
```

The returned jsonb lets `AS OF` queries against either point in time
reconstruct the pre-update or post-update view.

## `rebuild_acceleration` — pg_dump/restore safety net

Rvbbit's parquet files live on disk under PGDATA. `pg_dump` captures
the heap + catalog tables, but not the parquet files themselves. On
the restore target the catalog points at parquet that doesn't exist.
`rebuild_acceleration` wipes derived state for one table and
re-runs `compact()` from the heap:

```sql
SELECT rvbbit.rebuild_acceleration('orders'::regclass);
-- → {"dropped_row_groups": 3, "new_row_count": 5000}
```

For this to actually recover real data, the pre-dump compact must
have used `compact(rel, true)` (keep_heap=true) so the heap holds the
source-of-truth rows.

## ObjectStore tiered storage

Per-row-group tier: `rvbbit.row_groups.cold_url IS NULL` = local hot,
non-NULL = ObjectStore URL. `rvbbit.migrate_to_cold` copies parquet
files to the cold location and updates the catalog. Reads route
through DataFusion's `ObjectStore` automatically — **the native
CustomScan node consults DataFusion when its local fetch comes up
empty**, so plain SQL works across the tier boundary.

```sql
-- One-time migration
SELECT rvbbit.migrate_to_cold('orders'::regclass, 'file:///mnt/cold/');
-- → {"migrated_row_groups": 2, "total_bytes": 3090,
--    "cold_url_prefix": "file:///mnt/cold"}

-- Every subsequent query just works
SELECT customer_id, sum(amount) FROM orders WHERE region='EU' GROUP BY 1;
SELECT count(*), avg(amount) FROM orders WHERE status='ok';

-- AS OF still honored on cold-tier data
SET LOCAL rvbbit.as_of_generation = 7;
SELECT * FROM orders WHERE id <= 3;
```

MVP supports `file://` only. `s3://` + `gs://` land via the same
URL-based plumbing once credential helpers are added; the
DataFusion side is already scheme-agnostic.

Limitation: mixed-tier tables (some local row groups, some cold)
aren't yet handled. `migrate_to_cold` is all-or-nothing per table.

## Lance — vector columns

Lance is a DataFusion-native columnar format with first-class
vector search (IVF-PQ, HNSW). Rvbbit integrates Lance as a
catalog-driven acceleration layer: operators opt a table in
once, then `compact()` and `rvbbit.knn()` handle the rest.

```sql
CREATE TABLE articles (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title     text,
    body      text,
    embedding real[]
) USING rvbbit;

-- INSERT however you produce embeddings
INSERT INTO articles (title, body, embedding) VALUES
    ('A Cat Sat',  'The mat held no grudge.', ARRAY[0.1,0.2,...]::real[]),
    ('Quantum FAQ','Yes, it can be in two places.', ARRAY[0.3,0.4,...]::real[]);

-- Compact (real[] is now a first-class type in parquet)
SELECT rvbbit.compact('articles'::regclass);

-- Opt this table in to Lance
SELECT rvbbit.lance_enable(
    'articles'::regclass::oid,
    'embedding',           -- which column holds the vectors
    384,                   -- expected dimension
    'file:///data/articles.lance');

-- Build an IVF-PQ index for fast KNN at scale
SELECT rvbbit.lance_build_index(
    'file:///data/articles.lance',
    'embedding',
    1000,   -- num_partitions ≈ sqrt(n_rows)
    24);    -- num_sub_vectors (must divide dim)

-- Query
SELECT rvbbit.knn(
    'articles'::regclass::oid,
    embedding_for('thermodynamics laws'),
    10);
-- → [{"id": 42, "_distance": 0.04},
--    {"id": 363, "_distance": 0.18}, ...]
```

After `lance_enable`, every subsequent `compact()` auto-refreshes the
Lance dataset to match the current table state. No further operator
action required. The index does need to be rebuilt manually for now;
auto-rebuild is a follow-on slice.

### Operator-explicit Lance (lower-level)

For one-off / ad-hoc workflows, the path primitives are exposed
directly:

```sql
-- Export a vector column to a fresh Lance dataset (no catalog update)
SELECT rvbbit.lance_import_column(
    'articles'::regclass::oid, 'id', 'embedding', 384,
    '/data/snapshot.lance');

-- Query a Lance dataset by path
SELECT rvbbit.lance_knn(
    '/data/snapshot.lance',
    embedding_for('quantum mechanics'),
    5);

-- Inspect a Lance dataset
SELECT rvbbit.lance_count('/data/snapshot.lance');

-- Create one for testing
SELECT rvbbit.lance_create_demo('/tmp/demo', 1000, 32);  -- 1000 rows × 32-dim
```

### Bench numbers

Measured on the test box (Intel, single backend, 16 worker threads):

| Scale                  | Brute-force KNN | IVF-PQ KNN | Speedup |
|---                     |---:             |---:        |---:     |
| 100k × 128-dim         | 29.5 ms/query   | 2.3 ms     | 12.8×   |

At 1M+ scale the gap widens (brute force is O(n), IVF-PQ is ~O(√n)).

## Architecture: how the pieces fit

```
┌────────────────────────────────────────────────────────────┐
│  Postgres backend                                          │
│                                                            │
│  ┌─────────────┐    ┌──────────────────────────────────┐  │
│  │  Planner    │───▶│  Rewriter (metadata fast paths)  │  │
│  │  hook       │    │  gated on tombstones / AS OF     │  │
│  └─────────────┘    └────────────────┬─────────────────┘  │
│                                      │                     │
│                                      ▼                     │
│  ┌────────────────────────────────────────────────────┐   │
│  │  Custom Scan (RvbbitParquetScan)                   │   │
│  │   ├─ local rg's   → RowGroupReader (std::fs)       │   │
│  │   ├─ cold rg's    → df::collect_batches_for_table  │   │
│  │   │                 → DataFusion ObjectStore       │   │
│  │   └─ tombstone bitmap AND'd in hot loop            │   │
│  └────────────────────────┬───────────────────────────┘   │
│                           │                                │
│  ┌────────────────────────┼───────────────────────────┐   │
│  │  Per-backend tokio Runtime + SessionContext        │   │
│  │   ├─ rvbbit.df_inprocess_query                     │   │
│  │   ├─ rvbbit.datafusion_query_json (router dispatch)│   │
│  │   ├─ rvbbit.collect_batches_for_table              │   │
│  │   └─ rvbbit.lance_* (Lance Dataset API)            │   │
│  └────────────────────────────────────────────────────┘   │
│                                                            │
└────────────────────────────────────────────────────────────┘

On disk under PGDATA/rvbbit/<oid>/:
  scan/<rg_id>.parquet            (local hot rg's)
  hive/<key>/...                  (variant layout, optional)
On the operator-chosen cold tier:
  file:// or s3:// or gs:// /path/<oid>/scan/<rg_id>.parquet
On the operator-chosen Lance location:
  file:// or s3:// or gs:// /path/articles.lance/
    data/  _transactions/  _versions/  ...
```

## Catalog reference

| Table                       | What it tracks                            |
|---                          |---                                        |
| `rvbbit.tables`             | Per-table flags: shadow heap, next_generation, lance_url, lance_vector_column, lance_dim |
| `rvbbit.row_groups`         | path, rg_id, generation, n_rows, stats, cold_url |
| `rvbbit.row_group_variants` | Same shape, for hive/cluster layouts      |
| `rvbbit.delete_log`         | (table_oid, rg_id, ordinal, deleted_xid, deleted_generation) |
| `rvbbit.generations`        | (table_oid, generation, committed_at, n_rows, n_row_groups) |

## What this means in practice

A rvbbit table with everything turned on is **a Postgres table that is
also**:

- A columnar OLAP store (parquet)
- A time-travel snapshot system (AS OF)
- A queue with merge-on-read deletes (tombstones)
- A tiered store (hot local + cold S3-class)
- A vector index (Lance IVF-PQ)

…all without changing the SQL semantics any application sees. The
operator turns features on per-table; applications keep writing
standard SQL.

## Limitations carried into the next session

In honest order of operator impact:

1. **Mixed-tier tables** (some local rg's, some cold). `migrate_to_cold`
   is all-or-nothing per table today; partial via direct catalog
   UPDATE is unsupported.
2. **Tombstones + cold** rejects in df.rs (in-process DataFusion can't
   apply per-rg bitmaps to ObjectStore reads). Plain SELECT through
   the CustomScan with both cold + tombstones falls back to local
   scan; if the table is fully cold, the read errors with a clear
   message.
3. **knn_text auto-routing** to Lance — the rewriter doesn't yet
   detect `ORDER BY embedding <-> query LIMIT k` patterns and reroute.
   Operators call `rvbbit.knn()` explicitly.
4. **Lance index auto-rebuild on compact** — auto-refresh of the
   dataset works; the index needs to be rebuilt manually after
   compact for now.
5. **s3:// + gs://** for both cold tier and Lance — URL plumbing is
   ready; credential helpers are a follow-on.
