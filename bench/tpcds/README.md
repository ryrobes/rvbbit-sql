# TPC-DS-Derived Benchmark

This suite runs DuckDB-generated TPC-DS data and DuckDB's bundled TPC-DS query
templates across Rvbbit, Postgres-family competitors, and DuckDB.

It is not an audited TPC result. Use it as a local engineering signal and
compare only same hardware, same scale, same settings runs.

The harness stores TPC-DS decimal columns as `DOUBLE PRECISION`/`Float64` so
Rvbbit can refresh acceleration into parquet today. That keeps the suite useful
for engine profiling while avoiding the current PG `numeric` export gap.

```bash
./bench/tpcds/run_offline.sh
TPCDS_SCALE=1 BENCH_SYSTEMS=rvbbit,duckdb,pg_baseline,alloydb ./bench/tpcds/run_offline.sh
TPCDS_SCALE=1 BENCH_SYSTEMS=rvbbit,duckdb,clickhouse ./bench/tpcds/run_offline.sh
SKIP_LOAD=1 BENCH_QUERIES=Q1,Q3,Q14 ./bench/tpcds/run_offline.sh
RVBBIT_RESET_EXTENSION=1 ./bench/tpcds/run_offline.sh
./bench/tpcds/run_offline.sh --reset-rvbbit-extension
```

Environment:

- `TPCDS_SCALE`: DuckDB `dsdgen` scale factor. Default `0.1`.
- `BENCH_SYSTEMS`: comma list. Default `rvbbit,duckdb,pg_baseline,citus,hydra,alloydb`.
  Rvbbit aliases match ClickBench/TPC-H: `rvbbit_native_forced`,
  `rvbbit_duck_forced`, `rvbbit_datafusion_forced`,
  `rvbbit_datafusion_hive_forced`, `rvbbit_duck_hive_forced`,
  `rvbbit_datafusion_vortex_forced`, `rvbbit_duck_vortex_forced`,
  `rvbbit_datafusion_mem_forced`, and `rvbbit_pg_heap_forced`.
  `clickhouse` is available as an explicit opt-in, but DuckDB's bundled
  TPC-DS templates contain some query forms ClickHouse does not currently
  accept without deeper query rewrites.
- `BENCH_QUERIES`: optional query list such as `Q1,Q3,Q14`.
- `BENCH_REPEATS`: runs per query. Default `3`.
- `BENCH_TIMEOUT`: per-query timeout seconds for Postgres-family systems and
  the wall-clock watchdog for DuckDB/ClickHouse. Default `300`.
- `BENCH_WALL_TIMEOUT`: optional wall-clock watchdog override for DuckDB and
  ClickHouse. Defaults to `BENCH_TIMEOUT`.
- `BENCH_WALL_TIMEOUT_GRACE`: extra seconds before a stuck DuckDB/ClickHouse
  runner process is terminated. Default `5`.
- `SKIP_LOAD=1`: reuse existing loaded tables.
- `TPCDS_FORCE_REGEN=1`: regenerate parquet for the selected scale.
- `RVBBIT_RESET_EXTENSION=1`: destructive Rvbbit extension reset. This wipes
  extension-owned system data such as router profiles/observations and KG
  tables. The default is to preserve system data and run `ALTER EXTENSION
  UPDATE`.
- `BENCH_PERSIST_RESULTS=0`: skip recording the completed run into
  `bench_history.runs` and `bench_history.query_results`.
- `BENCH_RUN_ID` and `--test-name <name>` / `--name <name>` or
  `BENCH_TEST_NAME`: override the persisted run id or group related scale
  sweeps. See `bench/BENCHMARK_HISTORY.md` for SQL examples.

Generated parquet lives under `bench/columnar_comparison/data/tpcds/`, mounted
into the benchmark container as `/data/tpcds/`.

When loading is enabled, the TPC-DS benchmark tables are replaced for a clean
suite run. That only clears benchmark test data; extension-owned Rvbbit system
state is preserved unless `RVBBIT_RESET_EXTENSION=1` is set.
