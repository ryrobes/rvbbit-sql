# Phase 1 Follow-ups

Items deliberately scoped out of the initial in-process DataFusion landing
(commits `8a91ec6` → `b1410ce` → current main) so the substrate could ship
and bench cleanly. Each is independent; pick any in any order.

## Already landed in Phase 1

- `crates/pg_rvbbit/src/df.rs` — `df::query_engine` substrate
  (SPI catalog discovery, ListingTable registration, sidecar-shaped JSON).
- `rvbbit.df_inprocess` GUC (default **on**) dispatches the
  `datafusion_*` routes through the in-process engine; transparent
  fallback to the rvbbit-duck sidecar on any error.
- Per-backend `REG_CACHE` keyed by `(qualified_name → path_signature)`
  skips the deregister/infer_schema/register dance when the file set
  hasn't changed since the last query.

## Tier 3 — bigger lifts, deferred

### Lance datasets — substrate landed, integration TODO

Commit 334ddc4 shipped the embedding-validation spike: lance 6.0.1
compiles cleanly into pg_rvbbit alongside DF53/arrow58, and three
direct SQL functions exercise the read+write+KNN paths. Verified
KNN at 629µs/query on a 1000×16 demo dataset.

Operator-explicit path landed in commit f854e72:
- rvbbit.lance_import_column(reloid, pk, vec, dim, path) exports a
  vector column to a Lance dataset
- rvbbit.lance_build_index(path, column, num_partitions, num_sub_vectors)
  creates an IVF-PQ index on the dataset
- rvbbit.lance_knn(path, query, k) queries (already shipped in the
  substrate commit)
- 12.8x speedup over brute force on 100k x 128-dim (2.3 ms vs 29.5 ms
  per query). Bigger gap at higher scales.

Still pending (the transparent-integration slices that build on this):
1. lance_url column on rvbbit.row_groups, mirroring cold_url. Vector
   columns of an rvbbit table live in a sibling Lance dataset; scalar
   columns stay in parquet.
2. compact() detects vector-typed columns (real[], or a registered
   embedding marker) and writes them to Lance alongside the parquet
   row group.
3. Read-path integration: when a query touches a vector column,
   join parquet + lance per row group. DataFusion can do this if we
   register both as separate ListingTables and the query has an
   identifying key column. (Or: a custom TableProvider that maps
   row-group ordinal across the two.)
4. knn_text auto-routing: when the table has a Lance-indexed embedding
   column, rvbbit.knn_text rewrites to a Lance vector search instead
   of brute-force parquet scan.

### A. Rewriter has metadata-only fast paths that bypass tombstones + AS OF

Discovered while writing the Phase 2 slice 4 (tombstones) probe.
`SELECT label, count(*) FROM t GROUP BY 1` (no predicate) gets rewritten
into `Function Scan on vector_float_agg` — a metadata-aggregation path
that reads pre-computed per-group counts from `rvbbit.row_groups.stats`
and never touches the parquet, never applies tombstones, never honors
`rvbbit.as_of_generation`. Queries with any predicate (e.g.
`WHERE id > 0`) bypass this fast path and behave correctly.

Fix sketch: the rewriter's metadata-aggregate code paths need a check
for "are there tombstones for this table?" (cheap — one SPI row from
delete_log) and for "is `rvbbit.as_of_generation` set?" (cheap — direct
GetConfigOption). If either is true, fall through to the normal scan.

Same gap likely exists for `count(*)` metadata fast path. Audit
candidates: grep for "vector_float_agg" and any other rewriter SRFs
that read stats directly.

### B. df::query_engine eligibility check is too strict for AS OF

`crates/pg_rvbbit/src/df.rs::discover_catalog_scan` rejects tables with
any pending tombstones (`r.deletes != 0`) when AS OF is unset. That
makes the in-process DF path refuse all queries the moment ANY delete
exists, instead of just applying the tombstone bitmap. Phase 4 reader-
side work for df.rs needs the same machinery as custom_scan.rs: load
the per-rg delete bitmap, apply it during result rendering.

When AS OF IS set, the existing code already skips the eligibility
check. The fix for the unset case is symmetrical: also skip the
deletes-block, but load tombstones into the catalog/registration step.

### 1. DataFusion 49 → 53 bump — LANDED

Commit c4e4502. Took under an hour wall-time and ZERO code changes —
only Cargo.toml version pins + one feature add (`sql`). Our use of
Arrow/parquet/DataFusion is conservative enough that the API changes
across versions didn't touch our surface.

Aligns pg_rvbbit with the rvbbit_duck sidecar (which already used DF
53.1.0 + parquet 58.3.0). In-process DataFusion now wins or ties every
query vs persistent sidecar at 1M with 5 row groups (1.26x-1.60x wins,
including flipping the previous lone Q2 loss into a 60% win).

### 2. Hive/cluster layout support in `query_engine`

`df::query_engine` currently early-returns on layouts other than
canonical "scan":

```rust
if !matches!(layout.trim().to_ascii_lowercase().as_str(),
             "" | "scan" | "canonical" | "default") {
    return Err(format!(
        "in-process datafusion currently only supports scan layout, got {layout}"
    ));
}
```

When the layout is hive or cluster, the router falls back to the sidecar.
Adding support means:

- Mirroring `rvbbit_duck::main::variant_catalog_sql` via SPI in
  `discover_catalog_*`.
- Building DataFusion `ListingTable` with `with_partition_columns` to
  signal hive partitioning so the planner can prune.
- Tests against a hive variant.

Estimated effort: half a day.

### 3. Arrow `compute` kernels in `crates/pg_rvbbit/src/scan.rs`

`scan.rs` hand-rolls Rust loops for aggregate, filter, take operations on
Arrow arrays. Replacing those with `arrow::compute::kernels::{aggregate,
filter, take, cmp}` gives SIMD for free. Specific call sites:

- `scan.rs:413-492` `scan_numeric_sum_count`
- `scan.rs:1107-1231` `top_count_1col`
- `scan.rs:1235-1276` `count_distinct_int`

Estimated effort: 1-2 days, mechanical.

### 4. Vectorized tuple emission (PG18 batched slot API)

Custom scan in `crates/pg_rvbbit/src/custom_scan.rs` emits one
`TupleTableSlot` at a time from Arrow batches. PG18's batched-slot API
lets us emit ~1024 rows per call. Biggest expected win is on text-heavy
queries (Q28 regex in ClickBench, Q20-Q26 LIKE/ORDER BY).

Estimated effort: 1 week. Touches the custom_scan exec hook in a
load-bearing way.

### 5. ObjectStore tiered storage — LANDED, FULL VANILLA SQL

Commits 3f1e30f (initial plumbing) + 7f1d792 (close the loop). Per-row-
group tier: rvbbit.row_groups.cold_url IS NULL = local hot, non-NULL =
ObjectStore URL. rvbbit.migrate_to_cold(reloid, cold_url_prefix) copies
+ relabels each row group.

The native CustomScan node reads cold-tier row groups transparently
through DataFusion's ObjectStore path — same plan node, same hot loop,
the bytes just come from a different source. Plain SELECT/COUNT/GROUP
BY/AS OF/ORDER BY/LIMIT/min/max/avg all work on cold data without any
operator workaround.

MVP supports file:// only (single-machine demo). s3:// + gs:// land
when we add credential helpers — the URL-based plumbing is already
ObjectStore-scheme-agnostic.

Limitations carried as separate followups:
- Mixed-tier tables (some local rg's, some cold) not yet handled.
  migrate_to_cold is all-or-nothing per table, so this only arises
  from direct catalog UPDATE.
- tombstones + cold rejects in df.rs (Phase 4 work to make in-process
  DF apply per-rg bitmaps to ObjectStore reads).

### 6. Generation tracking primitive — LANDED

Landed as Phase 2's first slice. See `catalog.rs` (`next_generation` on
`rvbbit.tables`, `generation` on `rvbbit.row_groups`, `current_generation`
SQL function), `compact.rs::export_to_parquet` (advisory-lock-protected
increment + plumb to `register_primary_chunks`). Verified end-to-end:
each compact() call atomically allocates a monotonic generation; old row
groups keep theirs; `current_generation()` returns the max.

### 7. Compact ignores new heap rows when parquet is authoritative — FIXED

Fix landed alongside Phase 2 slice 2 (commit 2026-05-25). `write_layout_chunks`
and `write_hive_layout_chunks` now bracket their SPI scan with
`set_config('rvbbit.force_heap_scan', 'on', is_local=true)` and restore
the prior value after, so the rewriter does NOT route the read through
the parquet custom scan during compact(). Newly-INSERTed heap rows are
preserved into the next generation correctly.

Original symptom (kept for the audit log):

```sql
CREATE TABLE t (id bigserial, label text) USING rvbbit;
INSERT INTO t (label) SELECT 'a' FROM generate_series(1,100);
SELECT rvbbit.compact('t'::regclass);  -- parquet has 100 'a' (correct)
INSERT INTO t (label) SELECT 'b' FROM generate_series(1,100);
SELECT rvbbit.compact('t'::regclass);
-- second compact's parquet has 100 'a' again — the 'b' rows were
-- silently dropped from the heap during truncation
```

Root cause: `compact()` reads rows via `SELECT * FROM {qualified}` through
SPI. That goes through the rewriter, which redirects authoritative-parquet
reads to the custom scan, which reads parquet only (not heap). So the
second compact sees the existing parquet contents and re-writes them as
a new row group, then truncates the heap, losing the new INSERTs.

This is a load-bearing bug for Phase 2 AS OF semantics: time travel
across generations needs each generation to reflect the actual delta
since the previous one, not a re-write of the prior snapshot.

Fix sketch: `export_to_parquet` should read directly from the heap
(`SELECT * FROM <table> WHERE ctid IS NOT NULL` against the underlying
heap, bypassing the rewriter — or use SPI with a GUC that suppresses the
rewriter) and only emit row groups for those rows. Subsequent compacts
would then produce true incremental row groups.

Estimated effort: 1 day, including a regression test that catches this
pattern.

## Bench results that motivated some of these

Real `USING rvbbit` table, real `compact()`, 20 iters per cell, single
psql session per path (warm). DataFusion-49 in-process, DataFusion-53
sidecar. `RVBBIT_DF_THREADS=16`, `RVBBIT_COMPACT_SCAN_CHUNK_ROWS=200_000`
(5 row groups at 1M):

**100k, hot p50 ms:**

| Query        | Sidecar persistent | Sidecar one-shot | In-process |
|---           |---:|---:|---:|
| count(*)     | 2.44 | 18.70 | **1.38** |
| filter+count | 3.10 | 21.20 | **1.93** |
| group by     | 3.87 | 22.06 | **3.20** |
| top-k        | 5.45 | 25.63 | **4.52** |
| count(distinct) | 4.47 | 22.87 | **3.53** |

**1M with 5 row groups, hot p50 ms:**

| Query        | Sidecar persistent | Sidecar one-shot | In-process |
|---           |---:|---:|---:|
| count(*)     | 2.17 | 19.63 | **1.71** |
| filter+count | **4.02** | 23.15 | 4.29 |
| group by     | 8.22 | 27.45 | **7.83** |
| top-k        | 17.86 | 42.44 | **15.93** |
| count(distinct) | 15.85 | 37.85 | **13.07** |

In-process wins or ties every query at 100k; wins 4 of 5 at 1M (the lone
loss is by 6%). Worth pursuing item 1 (DF 49 → 53) primarily to close the
6% gap on Q2 and pick up planner improvements that may move Q3/Q4
further; everything else is gravy.

## Bench harness location

Ad-hoc scripts written to `/tmp/df_spike/` during the spike are
ephemeral (lost on reboot):

- `phase1_setup.sql`     — 100k events, no jsonb
- `phase1_setup_1m.sql`  — 1M events, no jsonb (set
  `RVBBIT_COMPACT_SCAN_CHUNK_ROWS=200000` for multi-RG)
- `phase1_bench.py`      — psycopg2, 3-way A/B/C, parameterized by
  `PHASE1_TABLE` env var

If we revisit any of these items, the bench harness should move into
`bench/` as a first-class script with a Makefile target.
