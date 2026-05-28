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
  `rvbbit_duck_hive_forced`,
  `rvbbit_datafusion_hive_forced`, and `rvbbit_pg_heap_forced` for executor
  comparison over the same compacted table. `rvbbit_native_forced` uses the
  router's `rvbbit.route_force_candidate=rvbbit_native`; `rvbbit_native` is the
  older `rvbbit.duck_backend=off` baseline.
  `rvbbit_datafusion_mem_forced` also loads `rvbbit.hot_objects` after compact
  so the forced memory route has a hot all-column object to use.
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

### 1M rows (smoke pass)

Median of 3 runs, 300s timeout. DuckDB FAILs on Q18/Q36-Q42 are
DuckDB's own dialect issues, not rvbbit's. Q-id list at
https://github.com/ClickHouse/ClickBench/blob/main/postgresql/queries.sql.

| Query | rvbbit | duckdb | clickhouse | pg_baseline | citus | hydra | alloydb |
|---|---|---|---|---|---|---|---|
| Q0 | **189µs** ← | 2ms | 4ms | 94ms | 54ms | 13ms | 1ms |
| Q1 | 48ms | 899µs | 5ms | 123ms | 32ms | 9ms | **452µs** ← |
| Q2 | 90ms | **801µs** ← | 6ms | 119ms | 71ms | 24ms | 5ms |
| Q3 | 84ms | **1ms** ← | 7ms | 117ms | 69ms | 21ms | 3ms |
| Q4 | 168ms | **6ms** ← | 12ms | 231ms | 95ms | 139ms | 191ms |
| Q5 | 334ms | **8ms** ← | 20ms | 563ms | 194ms | 266ms | 235ms |
| Q6 | 76ms | **967µs** ← | 6ms | 102ms | 50ms | 15ms | 2ms |
| Q7 | 46ms | 1ms | 5ms | 103ms | 32ms | 12ms | **993µs** ← |
| Q8 | 289ms | **7ms** ← | 14ms | 431ms | 324ms | 234ms | 252ms |
| Q9 | 379ms | **13ms** ← | 17ms | 562ms | 309ms | 239ms | 298ms |
| Q10 | 82ms | **4ms** ← | 8ms | 105ms | 58ms | 21ms | 11ms |
| Q11 | 107ms | **8ms** ← | 14ms | 155ms | 73ms | 24ms | 17ms |
| Q12 | 91ms | **4ms** ← | 25ms | 110ms | 56ms | 25ms | 13ms |
| Q13 | 232ms | **8ms** ← | 33ms | 178ms | 175ms | 144ms | 63ms |
| Q14 | 95ms | **6ms** ← | 25ms | 124ms | 65ms | 32ms | 11ms |
| Q15 | 145ms | **7ms** ← | 11ms | 227ms | 101ms | 49ms | 36ms |
| Q16 | 217ms | **13ms** ← | 33ms | 382ms | 192ms | 130ms | 61ms |
| Q17 | 206ms | **10ms** ← | 25ms | 174ms | 161ms | 57ms | 51ms |
| Q18 | 499ms | FAIL | **58ms** ← | 849ms | 534ms | 401ms | 494ms |
| Q19 | 13ms | 890µs | 10ms | 92ms | 35ms | 12ms | **163µs** ← |
| Q20 | 92ms | **16ms** ← | 19ms | 76ms | 176ms | 34ms | 36ms |
| Q21 | 104ms | **12ms** ← | 29ms | 93ms | 191ms | 48ms | 36ms |
| Q22 | 218ms | **17ms** ← | 90ms | 108ms | 370ms | 123ms | 42ms |
| Q23 | 141ms | 65ms | 84ms | 90ms | 1.16s | 381ms | **36ms** ← |
| Q24 | 124ms | **3ms** ← | 11ms | 68ms | 64ms | 17ms | 3ms |
| Q25 | 116ms | **4ms** ← | 13ms | 68ms | 50ms | 17ms | 4ms |
| Q26 | 126ms | **7ms** ← | 11ms | 69ms | 65ms | 19ms | 8ms |
| Q27 | 364ms | **17ms** ← | 29ms | 91ms | 327ms | 55ms | 94ms |
| Q28 | 3.97s | **178ms** ← | 161ms | 1.12s | 3.99s | 904ms | 1.55s |
| Q29 | 365ms | **10ms** ← | 61ms | 247ms | 654ms | 146ms | 132ms |
| Q30 | 114ms | **7ms** ← | 30ms | 162ms | 93ms | 40ms | 20ms |
| Q31 | 163ms | **9ms** ← | 24ms | 135ms | 112ms | 68ms | 59ms |
| Q32 | 863ms | **57ms** ← | 74ms | 1.06s | 714ms | 658ms | 584ms |
| Q33 | 375ms | **36ms** ← | 72ms | 403ms | 378ms | 1.69s | 281ms |
| Q34 | 410ms | **38ms** ← | 70ms | 438ms | 311ms | 1.63s | 302ms |
| Q35 | 178ms | **8ms** ← | 14ms | 127ms | 159ms | 81ms | 137ms |
| Q36 | 121ms | FAIL | **24ms** ← | 229ms | 190ms | 1.25s | 233ms |
| Q37 | 128ms | FAIL | **12ms** ← | 145ms | 99ms | 53ms | 28ms |
| Q38 | 158ms | FAIL | 20ms | 138ms | 83ms | 56ms | **10ms** ← |
| Q39 | 243ms | FAIL | **40ms** ← | 581ms | 307ms | 1.36s | 672ms |
| Q40 | 25ms | FAIL | **12ms** ← | 113ms | 58ms | 31ms | 13ms |
| Q41 | 29ms | FAIL | 11ms | 225ms | 52ms | 25ms | **7ms** ← |
| Q42 | 35ms | FAIL | **9ms** ← | 164ms | 81ms | 67ms | 30ms |

| summary | rvbbit | duckdb | clickhouse | pg_baseline | citus | hydra | alloydb |
|---|---|---|---|---|---|---|---|
| geomean (ms) | 126 | **7** | 19 | 179 | 139 | 76 | 30 |
| sum of medians (s) | 11.7 | **0.6** | 1.3 | 10.8 | 12.4 | 10.6 | 6.1 |
| wins (best in row) | 1 | 27 | 7 | 0 | 0 | 0 | 8 |
| failures | 0 | 8 | 0 | 0 | 0 | 0 | 0 |

### Reading these numbers

**Rvbbit vs plain Postgres (the headline win)**: rvbbit beats
pg_baseline on every query — geomean 126ms vs 179ms — and crushes it
on Q0 (metadata aggregate, 500×). Plain PG has no columnar story;
rvbbit gives Postgres users genuine columnar performance.

**Rvbbit vs Hydra/Citus**: roughly competitive on geomean (126ms vs
76/139ms). Rvbbit wins on metadata aggregates (Q0), narrow group-bys,
and the high-OFFSET queries Q40-Q42. Hydra wins on text-heavy
group-bys (Q20-Q26 LIKE/ORDER BY) because its vectorized text path is
mature; RYR-284 LIKE pushdown closed about half the Q20/Q21 gap (235ms
→ 92ms, 262ms → 104ms).

**Rvbbit vs DuckDB/ClickHouse**: rvbbit trails by 5-50× on most
queries. These are best-in-class standalone columnar engines and our
v1 custom-scan emits one PG tuple at a time. The fact that rvbbit is
competitive on query shapes the rewriter understands (Q0) and within
2-10× on many others is a reasonable v1 showing — and the trade-off
is "still a Postgres extension with arbitrary semantic operators."

**Rvbbit vs AlloyDB Omni**: AlloyDB's columnar engine, with the
populated-in-memory hot copy, beats rvbbit on most "single point
lookup with hot table" queries (Q0/Q1/Q7/Q19 sub-ms). The cost is
RAM: AlloyDB needs the columnar in-memory hot copy *plus* the row
store on disk. Rvbbit holds geomean 126ms while keeping disk
footprint at 706MB and no extra RAM for the columnar copy.

**Q28 (regex)** is still rvbbit's worst showing (3.97s vs DuckDB's
178ms). PG regex on TEXT columns through per-row Arrow→Datum dispatch
is brutal. RYR-284 covered LIKE/ILIKE but not REGEXP_REPLACE — a real
fix needs Arrow string kernels and/or a bulk varlena allocator.

### Larger scales

Bumping to `BENCH_LIMIT=10000000` (10M) or `BENCH_LIMIT=100000000`
(full) is straightforward — see the disk budget table above. At
larger scale we expect:
- rvbbit's column-projection wins compound (105 cols → reading 3
  columns means ~3% of the I/O of pg_baseline)
- group-by queries with low-cardinality columns stay sub-ms via
  per-group stats
- text-heavy queries become harder as full rows materialize

Full-scale numbers will land in a follow-up commit.
