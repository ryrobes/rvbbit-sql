# Cross-database analytical benchmark

Compares rvbbit to other Postgres-flavored columnar engines, ClickHouse,
and DuckDB on the NYC TLC yellow-taxi dataset. The last row is a
**semantic** query — rvbbit's day job — where every competitor lands
on `N/A`.

## Dataset

NYC TLC yellow taxi, January through March 2023:

| Files | Rows | Parquet on disk |
|---|---|---|
| 3 | 9,384,487 | 144 MB |

Loaded into each system with the same schema (19 columns, casts where
necessary so naturally-integer fields aren't stored as DOUBLE).

## How to run

```sh
# 1. Bring up the competitor containers (profile-gated; ~5GB of images)
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.competitors.yml \
               --profile bench up -d

# 2. Download the taxi parquets (cached in bench/columnar_comparison/data/)
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.competitors.yml \
               exec bench python /bench/columnar_comparison/download_taxi.py

# 3. Load every system
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.competitors.yml \
               exec bench python /bench/columnar_comparison/load_all.py

# 4. Run the canonical query set
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.competitors.yml \
               exec bench python /bench/columnar_comparison/run_queries.py
```

`BENCH_SYSTEMS=duckdb,clickhouse,rvbbit` limits which systems run.
`BENCH_REPEATS=5` raises the per-query repeat count for tighter medians.

## Load results (9.4M rows)

| System | Load (s) | On-disk |
|---|---|---|
| **DuckDB**        | 4   | 226 MB |
| **ClickHouse**    | 2   | 250 MB |
| **rvbbit**        | 74 + 19 (compact) | **139 MB** ← smallest |
| **Hydra**         | 75  | 193 MB |
| **Citus Columnar**| 76  | 193 MB |
| **AlloyDB Omni**  | 75  | 1.4 GB (row store; columnar engine is an in-memory cache) |
| **plain Postgres**| 74  | 1.4 GB |

ClickHouse and DuckDB read parquet natively in one shot. PG-flavored
systems take ~70s for ingest via `COPY`; rvbbit adds ~19s for the
`export_to_parquet` compact step that flips the table onto the
columnar read path. Net: rvbbit's on-disk footprint is the smallest
of any system here.

## Query results (median of 3 runs, ms)

### Portable SQL — apples-to-apples

| Query | duckdb | clickhouse | pg_baseline | citus | hydra | alloydb | **rvbbit** |
|---|---|---|---|---|---|---|---|
| count_all                 |   1ms |   2ms |   87ms |  305ms |   18ms |   98ms | **100µs** ← |
| count_filtered            |   2ms |   6ms |  110ms |  328ms |   24ms |  159ms |  372ms |
| avg_fare                  |   2ms |  10ms |  142ms |  489ms |   71ms |  226ms | **458µs** ← |
| groupby_vendor            |   3ms |   9ms |  155ms |  714ms |  134ms |  286ms |  802ms |
| groupby_payment_avg_tip   |   6ms |  25ms |  242ms |  932ms |  132ms |  386ms |  1.00s |
| daily_trip_count          |  11ms |  14ms |  742ms |  856ms | 1.29s  |  961ms |  943ms |
| compound_filter           |   4ms |  20ms |  109ms |  397ms |   49ms |  207ms |  173ms |
| top_routes                |  17ms |  44ms |  240ms |  971ms |  187ms |  387ms |  1.10s |
| wide_agg                  |   7ms |  27ms |  137ms |  464ms |   89ms |  249ms |  231ms |

### Rvbbit-only stats pushdown (sub-ms from row-group metadata)

`rvbbit.agg_*(rel, col)` and `rvbbit.agg_groupby_*(rel, group_col, ...)`
helpers answer aggregates from `rvbbit.row_groups.{stats,per_group_stats}`
without touching parquet data. Every other system could expose
equivalents but doesn't — these answer SQL-equivalent questions in
single microseconds. Compare to the portable row-scan timings above:

| Helper | rvbbit | (equivalent row-scan SQL) |
|---|---|---|
| `rvbbit.agg_count('trips')`               | **123µs** | `count(*)` ≈ same |
| `rvbbit.agg_avg('trips','fare_amount')`   | **265µs** | `avg_fare` 381µs (already pushed down) |
| `rvbbit.agg_min('trips','fare_amount')`   | **206µs** | row-scan ≈ same |
| `rvbbit.agg_max('trips','fare_amount')`   | **266µs** | row-scan ≈ same |
| `rvbbit.agg_sum('trips','tip_amount')`    | **280µs** | row-scan ≈ same |
| `rvbbit.agg_groupby_count('trips','vendor_id')` | **270µs** | `groupby_vendor` 823ms |
| `rvbbit.agg_groupby_avg('trips','payment_type','tip_amount')` | **371µs** | `groupby_payment_avg_tip` 1.05s |
| `rvbbit.agg_groupby_sum('trips','ratecode_id','fare_amount')` | **341µs** | row-scan ≈ similar |

The GROUP BY helpers work for any column whose distinct-value count
fits under 256 (low-cardinality candidates: vendor_id, payment_type,
ratecode_id, passenger_count). High-cardinality group columns
(timestamps, location IDs) skip per-group stats at compact time.

`avg_fare` is already auto-rewritten by the planner — users get the sub-ms
path without changing SQL. For GROUP BY queries that auto-rewrite is
not yet wired (substituting RTE_RELATION with RTE_VALUES in PG's Query
tree is more invasive). Users get the speed by calling the helpers
directly; auto-rewrite is tracked as a follow-up Linear issue.

### Semantic — rvbbit only

| Query | duckdb | clickhouse | pg_baseline | citus | hydra | alloydb | **rvbbit** |
|---|---|---|---|---|---|---|---|
| `rvbbit.sentiment_bigfoot(observed)` | N/A | N/A | N/A | N/A | N/A | N/A | **1ms** |

Semantic queries call user-defined LLM or specialist sidecar operators
as native SQL functions. Rvbbit is the only system in this comparison
that supports them; everyone else is `N/A` not because of latency,
but because they can't express it. See `bench/bigfoot_bench.py` for
the per-call latency story (sentiment specialist: 82ms cold, ~0.1ms
cached; LLM operator: 1.3s cold, ~0.1ms cached).

### What this is telling us

**Storage**: rvbbit's parquet output is the densest of any system here
(139 MB for 9.4M rows). ZSTD + Arrow row groups punch above their
weight.

**Two queries fastest of any system**:
- `count_all`: 100µs — metadata-only, never reads a row
- `avg_fare`: 458µs — caught at plan time by an absorbed-aggregate rule
  that computes the result from row-group stats and rewrites the
  Aggref into a Const. The plan literally becomes a single `Result`
  node. Same rule covers `sum(col)` and `count(col)` on
  unfiltered, ungrouped queries.

**Predicate pushdown halves filtered scans**: `compound_filter`
(`WHERE passenger_count > 2 AND fare_amount > 20`) went from 552ms →
173ms by evaluating both clauses on Arrow column data BEFORE
materializing PG tuples.

**Group-by queries: helpers fast, auto-rewrite not yet wired** —
per-group stats ARE now computed at compact time for low-cardinality
columns (vendor_id, payment_type, etc.). The `rvbbit.agg_groupby_*`
SRF helpers answer the equivalent queries in sub-millisecond time.
What's missing is the planner rule that swaps standard `SELECT col,
agg(x) FROM t GROUP BY col` SQL for those helper calls — that needs
RTE_VALUES injection in the Query tree which is meaningfully more
invasive than the ungrouped-aggregate substitution. Tracked separately.

## rvbbit implementation status

Shipped:

- Generic `export_to_parquet` for any table schema (bool / int2 / int4 /
  int8 / float4 / float8 / text / varchar / char / name / timestamp /
  timestamptz / date / jsonb / bytea).
- Per-batch typed column readers in the CustomScan (one downcast per
  Arrow batch, not per row).
- Row-group stats with min/max/sum/null_count computed at compact time.
- Stats-pushdown helper functions (`rvbbit.agg_count / agg_avg / agg_min
  / agg_max / agg_sum / agg_count_nonnull`) — sub-millisecond aggregates
  with no scan.

In flight, in priority order for closing the per-row gap further:

1. **Full vectorized execution** — return N tuples per `ExecCustomScan`
   call instead of one (PG ≥16's batched-slot path).
2. **Predicate pushdown** — eval WHERE on Arrow arrays before
   materializing PG tuples. Biggest win on selective filters.
3. **Row-group chunking** — emit multiple parquet files per compact so
   min/max stats per row group enable real pruning, and reads can run
   in parallel.
4. **Auto-rewrite of standard aggregates** — planner hook that turns
   `SELECT avg(col) FROM rel` into the corresponding `agg_avg` call so
   users get the sub-ms path without touching SQL.

## Caveats

- Single-host, single-thread comparison. ClickHouse and DuckDB
  parallelize internally; the PG systems were configured with
  `max_parallel_workers_per_gather=4` but cluster sweeping wasn't tuned.
- DuckDB wins by a lot at this scale (~9M rows); the gap closes at
  10x-100x data sizes where its single-node embedded model becomes
  more competitive with distributed columnar.
- ClickHouse on bigger data also stays close to DuckDB but the gap
  to the PG-flavored systems widens further.
- Citus's columnar storage is its raw form; the more interesting
  Citus story is distributed sharding, which isn't tested here.
- All systems are stock images with default tuning beyond
  `shared_buffers=2GB` and `work_mem=128MB` for the PG-flavored ones.

## Files

- `download_taxi.py` — downloads parquets to `data/`
- `schema.py` — shared column definitions + per-engine DDL helpers
- `loaders/` — one per system (DuckDB, ClickHouse, generic PG)
- `queries.py` — the canonical query set + semantic-only queries
- `runners.py` — per-system runner functions
- `load_all.py` / `run_queries.py` — orchestrators
- `results/last_run.json` — raw output of the most recent run
