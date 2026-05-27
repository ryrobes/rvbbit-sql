# TPC-H-Derived Benchmark

This suite runs DuckDB-generated TPC-H data and the 22 canonical TPC-H query templates across Rvbbit, Postgres-family competitors, DuckDB, and ClickHouse. TPC-H is the TPC decision-support benchmark: https://www.tpc.org/tpch/default5.asp

It is not an audited TPC result. Use it as a local engineering signal and compare only same hardware, same scale, same settings runs.

The harness stores TPC-H decimal columns as `DOUBLE PRECISION`/`Float64` so Rvbbit can compact the tables into parquet today. That keeps the suite useful for engine profiling while avoiding the current PG `numeric` export gap.

```bash
./bench/tpch/run_offline.sh
TPCH_SCALE=1 BENCH_SYSTEMS=rvbbit,duckdb,clickhouse,hydra,citus ./bench/tpch/run_offline.sh
SKIP_LOAD=1 BENCH_QUERIES=Q1,Q6,Q14 ./bench/tpch/run_offline.sh
RVBBIT_RESET_EXTENSION=1 ./bench/tpch/run_offline.sh
RVBBIT_LOAD_ROUTE_PROFILE=1 ./bench/tpch/run_offline.sh
./bench/tpch/run_offline.sh --reset-rvbbit-extension
./bench/tpch/run_offline.sh --load-route-profile
```

Environment:

- `TPCH_SCALE`: DuckDB `dbgen` scale factor. Default `0.1`.
- `BENCH_SYSTEMS`: comma list. Default `rvbbit,duckdb,clickhouse,pg_baseline,citus,hydra,alloydb`.
  Rvbbit aliases include `rvbbit_native_forced`, legacy `rvbbit_native`,
  `rvbbit_duck_forced`,
  `rvbbit_datafusion_mem_forced`, `rvbbit_datafusion_forced`,
  `rvbbit_duck_hive_forced`,
  `rvbbit_datafusion_hive_forced`, and `rvbbit_pg_heap_forced` for executor
  comparison over the same compacted tables. `rvbbit_native_forced` uses the
  router's `rvbbit.route_force_candidate=rvbbit_native`; `rvbbit_native` is the
  older `rvbbit.duck_backend=off` baseline.
  `rvbbit_datafusion_mem_forced` also loads `rvbbit.hot_objects` after compact
  so the forced memory route has hot all-column objects to use.
  Rvbbit benchmark loads refresh hive/cluster layouts synchronously by default,
  so auto routing can consider segmented variants during the measured query
  run. Set `RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD=async` to restore
  background refresh, or `0` to disable refresh for non-Hive comparisons or to
  require already-materialized variants.
- `RVBBIT_PARQUET_META_CACHE=1` / `RVBBIT_PARQUET_PREWARM=1`: default-on Rust
  sidecar metadata cache for compacted parquet catalog and footer/schema
  metadata. Set either to `0`/`off` for a cold metadata comparison.
- `RVBBIT_ROUTE_SAFETY_CACHE=1`: default-on Rust sidecar cache for exact SQL
  `rvbbit.route_explain(...)` safety checks. Entries are scoped to the current
  parquet catalog fingerprint and capped by `RVBBIT_ROUTE_SAFETY_CACHE_MAX`
  (default `4096`).
- `RVBBIT_ROUTE_SAFETY_LOCAL=1`: default-on conservative local safety approval
  for simple `FROM`/`JOIN` SELECTs over authoritative Rvbbit parquet tables;
  complex SQL still falls back to `rvbbit.route_explain(...)`.
- `RVBBIT_DUCK_RUST_PERSISTENT=1`: default-on for forced Duck/DataFusion
  benchmark systems, so forced runs reuse the same Rust sidecar process and
  exercise metadata cache behavior.
- `BENCH_QUERIES`: optional query list such as `Q1,Q6,Q14`.
- `BENCH_REPEATS`: runs per query. Default `3`.
- `BENCH_TIMEOUT`: per-query timeout seconds for Postgres-family systems. Default `300`.
- `SKIP_LOAD=1`: reuse existing loaded tables.
- `TPCH_FORCE_REGEN=1`: regenerate parquet for the selected scale.
- `RVBBIT_RESET_EXTENSION=1`: destructive Rvbbit extension reset. This wipes
  extension-owned system data such as router profiles/observations and KG
  tables. The default is to preserve system data and run `ALTER EXTENSION
  UPDATE`.
- `RVBBIT_LOAD_ROUTE_PROFILE=1`: import `bench/rvbbit_route_profile.json` into
  the native router catalog. The default is to leave the current trained profile
  state alone.

Generated parquet lives under `bench/columnar_comparison/data/tpch/`, mounted into the benchmark container as `/data/tpch/`.

When loading is enabled, the TPC-H benchmark tables are replaced for a clean
suite run. That only clears benchmark test data; extension-owned Rvbbit system
state is preserved unless `RVBBIT_RESET_EXTENSION=1` is set. Use `SKIP_LOAD=1`
for before/after routing-training comparisons over the same already-loaded
tables.
