# Rvbbit Production Shape Notes

This document captures the first production-shaped storage experiment: keeping
the Postgres heap as source-of-truth while deriving Parquet row groups from it.

## Extension Default

`rvbbit.compact(rel)` writes Parquet row groups and truncates the heap. After
that, Parquet is authoritative and the Duck/DataFusion routes can safely read
only the registered Parquet files.

The benchmark loaders currently default to the shadow-heap mode below so route
training can measure `pg_rowstore` as a fourth candidate. That is a harness
default, not a production storage default.

## Shadow Heap Probe

`rvbbit.compact(rel, true)` writes the same Parquet row groups but preserves the
heap. This is intentionally experimental.

Useful properties:

- INSERT/COPY latency is unchanged from today's Rvbbit table writes because
  writes already go to the heap first.
- Compaction cost should be close to current compaction minus the final
  truncate savings.
- Storage becomes roughly `heap + parquet` until a future generation-aware
  compactor can age out old heap/source data.
- Duck/DataFusion routes may still run against the derived Parquet row groups in
  benchmarks. Correctness-sensitive production routing still needs
  generation/LSN tracking before treating this mode as authoritative.

Important limitation:

Repeated `rvbbit.compact(rel, true)` calls can duplicate the same heap rows into
new Parquet row groups. Use this mode for measurement, not correctness-sensitive
production routing, until generation/LSN tracking lands.

## Status Function

```sql
SELECT * FROM rvbbit.shadow_heap_status('hits'::regclass);
```

Important columns:

- `heap_bytes`: main heap fork size used by the current sidecar authority check.
- `heap_total_bytes`: heap plus indexes/toast.
- `parquet_rows`, `parquet_bytes`, `row_groups`: registered Parquet state.
- `parquet_authoritative`: true only when heap is empty and there are no deletes.
- `shadow_heap_present`: true when heap bytes remain after compaction.

## Benchmark Toggle

ClickBench and TPC-H loaders honor:

```bash
RVBBIT_COMPACT_KEEP_HEAP=1
RVBBIT_COMPACT_VARIANTS_SYNC=0
RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD=0
```

The current route-training harness sets this to `1` by default. With
`RVBBIT_COMPACT_KEEP_HEAP=1`, loaders call `rvbbit.compact(..., true)` and
report `size_bytes` as `parquet_size_bytes + heap_total_bytes`.
Use `RVBBIT_COMPACT_KEEP_HEAP=0` to restore parquet-only compaction.

`rvbbit.compact` now writes only the canonical scan layout by default. Cluster
and hive variants are derived copies; build them explicitly with:

```sql
SELECT rvbbit.refresh_layout_variants('hits'::regclass);
```

Set `RVBBIT_COMPACT_VARIANTS_SYNC=1` only when you want `compact` to block on
variant generation. Set `RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD=1` in the
benchmark loaders when you want route-training runs to build hive/cluster
copies after the canonical layout has loaded. This keeps normal load/write
latency focused on the authoritative scan layout while still allowing route
profiles to measure hive-backed Duck/DataFusion paths.

Current caveat: variant refresh still reads the retained heap through SPI. It is
therefore useful with the benchmark/default shadow-heap mode, but a future
generation-aware compactor should rebuild variants from canonical Parquet
instead of requiring heap rows.

Example small probe:

```bash
RVBBIT_COMPACT_KEEP_HEAP=1 BENCH_LIMIT=500000 BENCH_SYSTEMS=rvbbit BENCH_REPEATS=1 BENCH_QUERIES=Q0 ./bench/clickbench/run_offline.sh
```

For the first measurement, compare:

- default load wall time and `load+post`;
- keep-heap load wall time and `load+post`;
- `SELECT * FROM rvbbit.shadow_heap_status('hits'::regclass);`;
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
