# ClickBench

Standard ClickBench (https://github.com/ClickHouse/ClickBench) — 43
analytical queries against a 100M-row, 105-column hits table — run on
rvbbit alongside the same competitors as the taxi bench.

## Data

`hits.parquet` from
`https://datasets.clickhouse.com/hits_compatible/hits.parquet` (~14 GB
compressed). Stored as `bench/columnar_comparison/data/hits.parquet`
(symlinked from `bench/clickbench/data/` so all the containers'
existing `/data` mount sees it).

Download:

```sh
curl -L -o bench/columnar_comparison/data/hits.parquet \
  https://datasets.clickhouse.com/hits_compatible/hits.parquet
```

## Run — one-shot offline script

```sh
# From repo root. Defaults: 10M rows, all 7 systems, all 43 queries.
./bench/clickbench/run_offline.sh

# Full 100M (takes hours; needs ~150GB free)
BENCH_LIMIT=100000000 ./bench/clickbench/run_offline.sh

# Smaller scale or subset of systems / queries
BENCH_LIMIT=1000000 BENCH_SYSTEMS=rvbbit,duckdb,clickhouse \
  ./bench/clickbench/run_offline.sh
BENCH_SYSTEMS=rvbbit_datafusion_forced,rvbbit_datafusion_vortex_forced,rvbbit_duck_vortex_forced \
  ./bench/clickbench/run_offline.sh

# Reuse already-loaded data, just re-run queries
SKIP_LOAD=1 ./bench/clickbench/run_offline.sh

# Opt into a destructive Rvbbit extension reset. This wipes extension-owned
# system data such as router training observations/profiles and KG tables.
RVBBIT_RESET_EXTENSION=1 ./bench/clickbench/run_offline.sh
./bench/clickbench/run_offline.sh --reset-rvbbit-extension

# Preserve existing Rvbbit system data, but import the repo route profile.
RVBBIT_LOAD_ROUTE_PROFILE=1 ./bench/clickbench/run_offline.sh
./bench/clickbench/run_offline.sh --load-route-profile
```

The script:
1. Brings up the competitor containers
2. Downloads `hits.parquet` if missing (~14 GB, one-time)
3. Ensures/updates `pg_rvbbit` non-destructively by default
4. Re-loads requested benchmark tables unless `SKIP_LOAD=1`
5. Runs all 43 queries (3 medians each, 300s per-query timeout)
6. Prints a colored grid: best per-row in **green**, failures in **red**
7. Writes the uncolored grid to `bench/clickbench/results/clickbench_<limit>_<timestamp>.txt`
8. Raw JSON to `results/last_run.json` (incrementally — survives mid-run crashes)
9. Records the completed run into `bench_history.runs` and
   `bench_history.query_results` unless `BENCH_PERSIST_RESULTS=0`

Env overrides: `BENCH_LIMIT`, `BENCH_SYSTEMS`, `BENCH_QUERIES`,
`BENCH_REPEATS`, `BENCH_TIMEOUT`, `SKIP_LOAD`, `SKIP_DOWNLOAD`,
`RVBBIT_RESET_EXTENSION`, `RVBBIT_LOAD_ROUTE_PROFILE`.

Use `BENCH_RUN_ID=<id>` to control the persisted run id and
`--test-name <name>` / `--name <name>` or `BENCH_TEST_NAME=<name>` to group
related scale sweeps. See `bench/BENCHMARK_HISTORY.md` for SQL examples.

By default, the offline script preserves extension-owned Rvbbit system data:
router profiles, route observations, KG tables, semantic caches, and similar
catalog state. When loading is enabled, benchmark test tables such as `hits`
are still replaced so timing runs stay clean. Use `SKIP_LOAD=1` to keep already
loaded benchmark tables too.

## Run — manual / piecewise

```sh
# Start everything (assumes competitors compose already up)
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.competitors.yml \
               --profile bench up -d

# Load — env BENCH_LIMIT caps row count for ALL systems (default: full table)
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.competitors.yml \
               exec -e BENCH_LIMIT=10000000 bench \
               python /bench/clickbench/load_all.py

# Run the 43 queries
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.competitors.yml \
               exec bench \
               python /bench/clickbench/run_queries.py

# Pretty-print last results
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.competitors.yml \
               exec -e FORCE_COLOR=1 bench \
               python /bench/clickbench/format_report.py
```

Override env vars:
- `BENCH_LIMIT=N` — cap rows per system. Default: full 100M.
- `BENCH_SYSTEMS=rvbbit,duckdb,clickhouse` — limit which systems load/run.
  Rvbbit aliases include `rvbbit_native_forced`, legacy `rvbbit_native`,
  `rvbbit_duck_forced`,
  `rvbbit_datafusion_mem_forced`, `rvbbit_datafusion_forced`,
  `rvbbit_duck_hive_forced`, `rvbbit_duck_vortex_forced`,
  `rvbbit_datafusion_hive_forced`, `rvbbit_datafusion_vortex_forced`,
  `rvbbit_gpu_gqe_forced`, and
  `rvbbit_pg_heap_forced` for executor
  comparison over the same compacted table. `rvbbit_native_forced` uses the
  router's `rvbbit.route_force_candidate=rvbbit_native`; `rvbbit_native` is the
  older `rvbbit.duck_backend=off` baseline.
  `rvbbit_datafusion_mem_forced` also loads `rvbbit.hot_objects` after compact
  so the forced memory route has a hot all-column object to use.
  `rvbbit_gpu_gqe_forced` requires a visible NVIDIA GPU plus NVIDIA GQE tooling
  visible to the `pg-rvbbit` container. The Rvbbit image includes the
  `/usr/local/bin/rvbbit-gqe` launcher and
  `/opt/rvbbit/gqe/bin/rvbbit-gqe-bridge`; the bridge expects `gqe-cli` under
  `/opt/gqe/rust/target/release/gqe-cli` or `RVBBIT_GQE_CLI`, and either a
  reachable `RVBBIT_GQE_SERVER_URL` or local GQE node/task manager binaries.
  Set `RVBBIT_GQE_HOME=/path/to/gqe` to mount a host-built GQE checkout/build at
  `/opt/gqe` during the benchmark. Without `RVBBIT_GQE_HOME`, the runner's
  default `RVBBIT_GPU_GQE_INSTALL=auto` mode selects the optional
  `docker/docker-compose.gqe-image.yml` pg-rvbbit image on hosts where
  `nvidia-smi` reports a GPU and Docker exposes the NVIDIA runtime. Use
  `RVBBIT_GPU_GQE_INSTALL=image` to force that image, or
  `RVBBIT_GPU_GQE_INSTALL=off` to keep the normal image and report the GQE
  pathway as `SKIP`. The GPU compose overlays default
  `RVBBIT_GQE_SHM_SIZE=8gb`; the GQE preflight checks `/dev/shm`, starts the
  node manager once, and skips the pathway before query timing if the server
  cannot become reachable. They also default `NVSHMEM_DISABLE_CUDA_VMM=1`,
  `NVSHMEM_SYMMETRIC_SIZE=6G`, and `GQE_MAX_QUERY_MEMORY=6442450944`, giving
  GQE a 6 GiB query pool under the default 8 GB shared-memory container setting.
  The runner defaults forced GQE to the shared `rvbbit-duck` socket sidecar and
  `RVBBIT_GQE_CLIENT_MODE=flight`, so benchmarked queries reuse a persistent
  Flight client instead of spawning `gqe-cli` per query. Because ClickBench runs
  query repeats serially, forced GQE also defaults
  `RVBBIT_DUCK_BACKEND_SHARED_WORKERS=1`; set it higher when intentionally
  measuring concurrent shared-sidecar behavior. Set
  `RVBBIT_GQE_SHARED_BACKEND=off` to disable the shared sidecar,
  `RVBBIT_GQE_CLIENT_MODE=cli` to restore the legacy per-query CLI path, or
  `RVBBIT_GQE_FLIGHT_FALLBACK=0` to fail instead of falling back to CLI when the
  Flight client rejects a query. The runner also prewarms the GQE catalog after
  loading data and before timing queries; set `RVBBIT_GQE_PREWARM=off` to
  measure cold catalog/setup cost.
  GQE shape gates reject currently risky shapes such as unsupported join forms,
  schema-qualified refs, qualified star projections, multi-table `SELECT *`,
  and wide `SELECT *` + text filter + order/limit row retrieval. Set
  `RVBBIT_GQE_ALLOW_RISKY_SHAPES=1` only for controlled experiments that need
  to reproduce those rejected shapes.
  ClickBench also captures best-effort GQE sidecar telemetry into
  `last_run.json` and prints
  `gqe[mode ... flight ... cli ... read ... mat ...]` beside successful
  forced-GQE rows; set `BENCH_REPORT_GQE_TELEMETRY=0` to hide the console suffix,
  `BENCH_GQE_BREAKDOWN=0` to hide the report footer breakdown, or
  `BENCH_CAPTURE_SIDECAR_TELEMETRY=0` to skip the telemetry read. The report
  footer shows the slowest captured GQE sidecar samples by execution time;
  `BENCH_GQE_BREAKDOWN_ROWS=all` prints every captured query.
  Keep `NVSHMEM_SYMMETRIC_SIZE` at least as large as `GQE_MAX_QUERY_MEMORY`;
  lower both on constrained hosts, or raise both plus `RVBBIT_GQE_SHM_SIZE` when
  the GPU and host have enough free memory.
  For the file-count experiment, set `RVBBIT_GQE_LARGE_ROW_GROUPS=1`; it leaves
  normal defaults alone but fills unset `RVBBIT_DIRECT_ACCEL_CHUNK_ROWS` and
  `RVBBIT_COMPACT_SCAN_CHUNK_ROWS` with `1000000` rows, or with
  `RVBBIT_GQE_ROW_GROUP_CHUNK_ROWS` when provided.
  When the optional GQE image already exists, `--rebuild` defaults to
  `RVBBIT_GPU_GQE_REBUILD_MODE=refresh`: it rebuilds the normal `pg-rvbbit`
  image, then overlays the current extension and `rvbbit-gqe` bridge into the
  existing GQE image without recompiling libcudf, MLIR, or GQE. Refresh mode
  can recover from the preserved `docker-pg-rvbbit-gqe-pre-refresh` base or an
  explicit `RVBBIT_GQE_REFRESH_BASE_IMAGE`, but otherwise refuses to fall back
  to the full toolchain build if the existing GQE image is missing.
  If a reusable GQE base has too many Docker layers from repeated refreshes,
  the runner flattens it into `docker-pg-rvbbit-gqe-flat-refresh-base` before
  overlaying the current RVBBIT artifacts; set
  `RVBBIT_GQE_FLATTEN_LAYER_THRESHOLD` or `RVBBIT_GQE_FLAT_REFRESH_BASE_IMAGE`
  to tune that behavior.
  `docker compose up` is run with `--no-build` for the GQE image in
  default/refresh mode. Use `RVBBIT_GPU_GQE_REBUILD_MODE=full` only when the GQE
  toolchain itself needs rebuilding. Before an explicit full rebuild, the
  script tags any existing GQE image as
  `docker-pg-rvbbit-gqe-backup-<utc-timestamp>` by default; set
  `RVBBIT_GQE_BACKUP_BEFORE_FULL_REBUILD=0` to disable this or
  `RVBBIT_GQE_BACKUP_TAG=<tag>` to choose the backup tag.
  If those pieces are missing it remains in the report as `SKIP` with the probe
  reason.
  Rvbbit benchmark loads refresh hive/cluster layouts synchronously by default,
  so auto routing can consider segmented variants during the measured query
  run. Set `RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD=async` to restore
  background refresh, or `0` to disable refresh for non-Hive comparisons or to
  require already-materialized variants.
- `RVBBIT_PARQUET_META_CACHE=1` / `RVBBIT_PARQUET_PREWARM=1` — default-on
  Rust sidecar metadata cache. The cache keeps the compacted parquet catalog
  and footer/schema metadata warm without reading column data. Set either to
  `0`/`off` for a cold metadata comparison.
- `RVBBIT_ROUTE_SAFETY_CACHE=1` — default-on Rust sidecar cache for exact SQL
  `rvbbit.route_explain(...)` safety checks. Entries are scoped to the current
  parquet catalog fingerprint and capped by `RVBBIT_ROUTE_SAFETY_CACHE_MAX`
  (default `4096`).
- `RVBBIT_ROUTE_SAFETY_LOCAL=1` — default-on conservative local safety approval
  for simple `FROM`/`JOIN` SELECTs over authoritative Rvbbit parquet tables;
  complex SQL still falls back to `rvbbit.route_explain(...)`.
- `RVBBIT_DUCK_RUST_PERSISTENT=1` — default-on for forced Duck/DataFusion
  benchmark systems, so forced runs reuse the same Rust sidecar process and
  exercise metadata cache behavior.
- `BENCH_QUERIES=Q0,Q1,Q7` — run only specific queries.
- `BENCH_REPEATS=3` — runs per query, median reported.
- `BENCH_TIMEOUT=300` — per-query timeout in seconds.

## Disk budget

At 1M rows the measured on-disk sizes are:

| System | On-disk (1M rows) |
|---|---|
| **rvbbit**     | **79 MB** (parquet, ZSTD) |
| Hydra / Citus  | 91 MB each (columnar AM) |
| ClickHouse     | 133 MB |
| DuckDB         | 183 MB |
| pg_baseline / AlloyDB | 706 MB (heap) |

For rvbbit the size reflects only the parquet files registered in
`rvbbit.row_groups` — the heap-catcher residue (~706 MB) is *not*
read from after compaction (tracked: RYR-287).

Plain PG / AlloyDB at full 100M scale need substantial disk. Start
with `BENCH_LIMIT=10000000` (~15 GB across the row-store systems)
and grow.

## Schema

`hits` table: 105 columns. WatchID/UserID/RefererHash/URLHash are BIGINT;
EventTime/ClientEventTime/LocalEventTime are TIMESTAMP; EventDate is
DATE; URL/Title/Referer/etc. are TEXT; everything else is INTEGER or
SMALLINT. See `schema.py`.

## Queries

43 standard queries in `queries.py`, lifted from the upstream
`postgresql/queries.sql`. Column references are double-quoted to
preserve the upstream mixed-case identifiers across PG's identifier
folding.

## Results

### 5M rows (2026-07-14, current engine)

Median of 3 runs, 300s timeout, all seven systems on one desktop
(8-core i7-11700K, RTX 3090 Ti). DuckDB FAILs on Q18/Q36–Q42 are
DuckDB's own dialect issues with the upstream `postgresql/queries.sql`
(UINT16 `EventDate` vs date literals, `date_part` on string), not
rvbbit's. Q-id list at
https://github.com/ClickHouse/ClickBench/blob/main/postgresql/queries.sql.

| Query | rvbbit | duckdb | clickhouse | pg_baseline | citus | hydra | alloydb |
|---|---|---|---|---|---|---|---|
| Q0 | 934µs | **349µs** ← | 2ms | 378ms | 175ms | 17ms | 1ms |
| Q1 | 1ms | **851µs** ← | 5ms | 536ms | 148ms | 17ms | 2ms |
| Q2 | 2ms | **2ms** ← | 7ms | 525ms | 316ms | 42ms | 14ms |
| Q3 | **2ms** ← | 2ms | 7ms | 382ms | 227ms | 33ms | 6ms |
| Q4 | 94ms | **30ms** ← | 64ms | 1.13s | 567ms | 735ms | 979ms |
| Q5 | 98ms | **40ms** ← | 68ms | 4.52s | 3.33s | 3.86s | 2.04s |
| Q6 | 7ms | **457µs** ← | 4ms | 411ms | 243ms | 36ms | 3ms |
| Q7 | 2ms | **2ms** ← | 6ms | 593ms | 161ms | 22ms | 2ms |
| Q8 | 52ms | **38ms** ← | 78ms | 2.90s | 1.94s | 1.08s | 1.34s |
| Q9 | **41ms** ← | 49ms | 81ms | 3.27s | 2.09s | 1.12s | 2.00s |
| Q10 | 16ms | **10ms** ← | 18ms | 547ms | 338ms | 93ms | 113ms |
| Q11 | 18ms | **9ms** ← | 22ms | 632ms | 442ms | 106ms | 159ms |
| Q12 | 115ms | **53ms** ← | 88ms | 1.72s | 655ms | 473ms | 487ms |
| Q13 | 110ms | **83ms** ← | 113ms | 2.24s | 3.44s | 1.66s | 1.21s |
| Q14 | 109ms | **45ms** ← | 93ms | 1.73s | 673ms | 551ms | 552ms |
| Q15 | 39ms | **31ms** ← | 45ms | 1.02s | 857ms | 533ms | 803ms |
| Q16 | 109ms | **80ms** ← | 137ms | 2.24s | 1.25s | 917ms | 1.09s |
| Q17 | 112ms | **56ms** ← | 91ms | 948ms | 951ms | 185ms | 621ms |
| Q18 | **115ms** ← | FAIL | 244ms | 3.45s | 2.94s | 1.63s | 2.36s |
| Q19 | 5ms | 609µs | 8ms | 360ms | 163ms | 20ms | **234µs** ← |
| Q20 | 84ms | **34ms** ← | 103ms | 459ms | 901ms | 159ms | 206ms |
| Q21 | 40ms | **33ms** ← | 137ms | 589ms | 993ms | 147ms | 210ms |
| Q22 | **48ms** ← | 59ms | 365ms | 647ms | 1.64s | 329ms | 439ms |
| Q23 | 575ms | **39ms** ← | 456ms | 521ms | 5.11s | 1.55s | 215ms |
| Q24 | 198ms | **7ms** ← | 36ms | 523ms | 410ms | 70ms | 18ms |
| Q25 | 109ms | **11ms** ← | 48ms | 539ms | 322ms | 60ms | 53ms |
| Q26 | 59ms | **7ms** ← | 34ms | 529ms | 409ms | 72ms | 18ms |
| Q27 | 95ms | **42ms** ← | 132ms | 549ms | 1.44s | 253ms | 434ms |
| Q28 | 1.02s | **684ms** ← | 818ms | 4.98s | 20.83s | 4.77s | 7.90s |
| Q29 | 25ms | **8ms** ← | 259ms | 1.23s | 1.87s | 641ms | 595ms |
| Q30 | 112ms | **37ms** ← | 90ms | 1.97s | 855ms | 534ms | 412ms |
| Q31 | 125ms | **58ms** ← | 117ms | 2.33s | 1.42s | 1.00s | 531ms |
| Q32 | **67ms** ← | 240ms | 362ms | 7.73s | 4.61s | 2.72s | 2.87s |
| Q33 | **180ms** ← | 268ms | 408ms | 2.33s | 2.49s | 7.84s | 2.97s |
| Q34 | **168ms** ← | 322ms | 423ms | 2.28s | 2.38s | 7.82s | 3.03s |
| Q35 | 94ms | **38ms** ← | 47ms | 1.17s | 1.06s | 614ms | 1.18s |
| Q36 | **50ms** ← | FAIL | 58ms | 1.01s | 296ms | 3.97s | 425ms |
| Q37 | 28ms | FAIL | 30ms | 953ms | 156ms | 947ms | **25ms** ← |
| Q38 | 27ms | FAIL | 28ms | 653ms | 108ms | 39ms | **9ms** ← |
| Q39 | 116ms | FAIL | **112ms** ← | 1.01s | 518ms | 484ms | 1.28s |
| Q40 | **14ms** ← | FAIL | 17ms | 864ms | 84ms | 36ms | 23ms |
| Q41 | 23ms | FAIL | 14ms | 818ms | 83ms | 37ms | **11ms** ← |
| Q42 | 16ms | FAIL | **15ms** ← | 660ms | 136ms | 67ms | 72ms |

| summary | rvbbit | duckdb | clickhouse | pg_baseline | citus | hydra | alloydb |
|---|---|---|---|---|---|---|---|
| geomean (ms) | 41 | **19** | 54 | 1052 | 690 | 294 | 157 |
| sum of medians (s) | 4.3 | 2.4 | 5.3 | 63.9 | 69.1 | 47.3 | 36.7 |
| wins (best in row) | 9 | 28 | 2 | 0 | 0 | 0 | 4 |
| failures | 0 | 8 | 0 | 0 | 0 | 0 | 0 |

### Reading these numbers

**Rvbbit vs ClickHouse (the headline)**: geomean 41ms vs 54ms, zero
failures on both sides — faster than ClickHouse on its own benchmark,
from inside a stock Postgres 18. The router's picks across the 43
queries: GPU/GQE 16, native scan 15, Duck/Vortex 12. Nothing was
forced; that mix is the learned router doing its job.

**Rvbbit vs DuckDB**: DuckDB's geomean still leads (19ms vs 41ms) and
it takes the most per-row wins, but the gap that used to be 5–50× is
now ~2× — and 8 of the 43 queries FAIL outright under DuckDB's SQL
dialect. The 2026-07 result-path fix (`rvbbit._engine_rows`, Arrow →
Datum direct, no jsonb intermediate) closed most of the wrapper tax:
Q28 (regex) went from 3.97s to 1.02s, within 1.5× of DuckDB's 684ms.

**Rvbbit vs plain Postgres**: geomean 41ms vs 1.05s — 25× — while
remaining a plain Postgres the whole time. Hydra (294ms) and Citus
(691ms) sit in between.

**Rvbbit vs AlloyDB Omni**: 41ms vs 157ms. AlloyDB's columnar engine
needs its in-memory hot copy populated *and resident* to hit that
number — its columnar pool is a fixed-size arena
(`google_columnar_engine.memory_size_in_mb`, 4GB here), and any table
that doesn't fit silently falls back to the row store (~1.3s geomean
on this suite). When you benchmark it, verify residency in
`g_columnar_relations` first; we do, and we evict our own tables from
its pool before measuring. Sub-ms point lookups on hot tables
(Q0/Q1/Q19) remain AlloyDB's best surface.

### Larger scales

`BENCH_LIMIT=10000000` (10M) or `BENCH_LIMIT=100000000` (full 100M)
work the same way — see the disk budget table above. Column-projection
wins compound with scale (105 cols → reading 3 columns is ~3% of
pg_baseline's I/O), and at 50M on a Blackwell GPU box the gap over
AlloyDB widens to ~15×. Multi-scale history lives in `bench_history`
(see `bench/BENCHMARK_HISTORY.md`) and the interactive browser at
`bench/report/bench_report.html`.
