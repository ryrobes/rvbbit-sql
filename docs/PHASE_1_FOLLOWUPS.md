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

### 1. DataFusion 49 → 53 bump

The rvbbit-duck sidecar uses DataFusion 53 (its own workspace pin); we
embed 49 because the rvbbit workspace pins `arrow = 55` and DF 53 needs
arrow 58. Bumping pg_rvbbit's DataFusion alone is impossible without a
cascading arrow bump across:

- `crates/rvbbit_storage` (row_group writer, metadata, HLL serialization)
- `crates/pg_rvbbit/src/compact.rs` (arrow schema construction in
  `export_to_parquet`, type oid mappings)
- `crates/pg_rvbbit/src/scan.rs` (hand-rolled arrow kernels)
- `crates/pg_rvbbit/src/sketches.rs`, `bitmap.rs`, `row_group.rs`

Worth it: closes the engine-quality gap on heavy 1M+ queries where the
sidecar persistent path still has a slight edge, plus picks up
~6 minor releases of DataFusion query-planner improvements.

Estimated effort: 1-2 days. Should be its own branch with a clean
arrow-58 commit and the DF bump on top.

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

### 5. ObjectStore tiered storage

DataFusion exposes the `ObjectStore` trait so cold row groups can live in
S3/GCS while hot stays on local NVMe. Per-table or per-row-group policy
via a new `rvbbit.tables` column. Pairs well with Phase 2 generation
tracking (cold tier = old generation).

Estimated effort: 1 week (S3 client wiring, IAM story, eviction policy,
tests).

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
