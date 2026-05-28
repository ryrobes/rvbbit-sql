# Rvbbit Production Shape Notes

This document captures the first production-shaped storage experiment: keeping
the Postgres heap as source-of-truth while deriving Parquet row groups from it.

## Extension Default

The production-shaped path is now heap-as-source plus disposable acceleration
layers. Ordinary `INSERT`/`COPY` still land in the PostgreSQL heap. The
accelerator is refreshed with:

```sql
SELECT rvbbit.refresh_acceleration('hits'::regclass);
```

`refresh_acceleration` writes only heap rows whose inserting transaction is
past the table's acceleration watermark, so repeated refreshes do not duplicate
rows. The heap remains the gold source for normal PostgreSQL reads,
`pg_dump`/`pg_restore`, and rebuilds.

`rvbbit.compact(rel)` is now a compatibility wrapper around
`refresh_acceleration`; it no longer truncates the heap. The explicit
`rvbbit.compact(rel, keep_heap := false)` form remains as a legacy physical
rebuild path for older experiments that intentionally moved rows out of heap.

## Heap Source Layer

Useful properties:

- INSERT/COPY latency is unchanged from today's Rvbbit table writes because
  writes already go to the heap first.
- The heap can always rebuild Parquet, Hive variants, Lance, dictionaries, and
  hot memory objects.
- Storage is intentionally `heap + acceleration files`.
- If acceleration files are absent or stale, routing can fall back to heap.
- Refresh/rebuild operations take `LOCK TABLE ... IN SHARE MODE` while they
  derive files. Reads continue; concurrent writes wait so the clean/dirty
  accelerator marker is correct when the operation commits.

Important limitation:

The first watermark implementation uses completed heap `xmin` ranges. This is a
good first step for append-heavy analytic tables, but a future custom table AM
or hidden row-id trigger should replace it with an explicit stable
`rvbbit_row_id` before we call highly transactional update-heavy tables fully
production-ready.

## Observability

Acceleration operations are visible in SQL:

```sql
SELECT * FROM rvbbit.acceleration_status;
SELECT * FROM rvbbit.acceleration_operations ORDER BY started_at DESC LIMIT 20;
```

`rvbbit.acceleration_state` holds the per-table watermark.
`rvbbit.acceleration_operations` records refresh/rebuild/maintenance actions,
row counts, row-group counts, watermarks, timings, settings, and errors.

## Status Function

```sql
SELECT * FROM rvbbit.shadow_heap_status('hits'::regclass);
```

Important columns:

- `heap_bytes`: main heap fork size used by the current sidecar authority check.
- `heap_total_bytes`: heap plus indexes/toast.
- `parquet_rows`, `parquet_bytes`, `row_groups`: registered Parquet state.
- `parquet_authoritative`: true when the heap is empty or the retained heap is
  marked clean by the acceleration refresh machinery.
- `shadow_heap_present`: true when heap bytes remain after compaction.

## Benchmark Toggle

ClickBench and TPC-H loaders honor:

```bash
RVBBIT_COMPACT_VARIANTS_SYNC=0
RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD=sync
```

The current benchmark harness refreshes variants synchronously by default for
Rvbbit loads so auto routing can consider hive/cluster layouts during the
measured query run. Loaders call `rvbbit.refresh_acceleration(...)` and report
`size_bytes` as `parquet_size_bytes + heap_total_bytes`.

`rvbbit.compact` now writes only the canonical scan layout by default. Cluster
and hive variants are derived copies; build them explicitly with:

```sql
SELECT rvbbit.refresh_layout_variants('hits'::regclass);
```

Set `RVBBIT_COMPACT_VARIANTS_SYNC=1` only when you want legacy `compact` itself
to block on variant generation. In benchmark loaders,
`RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD=async` starts a detached `psql`
refresh after the canonical layout has loaded, `sync`/`1` blocks until
variants are ready, and `0` disables the post-load refresh. This keeps normal
load/write latency focused on the authoritative scan layout while still letting
hive-backed Duck/DataFusion paths become available as the background refresh
finishes.

Current caveat: variant refresh still reads the retained heap through SPI. It is
therefore useful with the heap-source model, but a future fused refresh should
write canonical and Hive layouts in one pass.

Example small probe:

```bash
BENCH_LIMIT=500000 BENCH_SYSTEMS=rvbbit BENCH_REPEATS=1 BENCH_QUERIES=Q0 ./bench/clickbench/run_offline.sh
```

For the first measurement, compare:

- default load wall time and `load+post`;
- `SELECT * FROM rvbbit.shadow_heap_status('hits'::regclass);`;
- `SELECT * FROM rvbbit.acceleration_status;`;
- recent rows from `rvbbit.acceleration_operations`;
- `rvbbit_pg_heap_forced` timings versus native/Duck/DataFusion timings.

## Forced Heap Route Probe

`rvbbit.force_heap_scan=on` is a benchmark/probe GUC that tells the planner hook
to leave normal Postgres heap paths intact for Rvbbit tables instead of
replacing them with the Rvbbit Parquet custom scan.

The benchmark target `rvbbit_pg_heap_forced` uses:

```text
options=-c rvbbit.duck_backend=off -c rvbbit.force_heap_scan=on
```

Training profiles include `pg_rowstore` timings and `pg_ms`/`pg_ms_median`
fields. By default, heap is observation-only: it can win the oracle ranking in
reports, but generated profile choices are still limited to native, Duck, and
DataFusion until the transparent router has an enforceable per-query heap route.

## Next Storage Steps

1. Add generation/LSN metadata to row groups so derived Parquet can be tied to a
   heap snapshot.
2. Add an enforceable transparent `pg_rowstore` route for preserved-heap tables.
3. Add delta/merge execution: Parquet base plus heap tail for eligible queries.
4. Decide whether preserved heap is table-level policy, compaction policy, or
   adaptive maintenance policy.
