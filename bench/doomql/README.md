# DoomQL: an analytical SQL raycaster

DoomQL turns a deterministic 3D voxel-observation table into a 120x40 terminal
frame using one scan/filter/raycast/group query per frame. It is deliberately a
visual benchmark rather than a claim that databases should be game engines.

The workload is shaped to expose Rvbbit's actual crossover points:

- PostgreSQL receives ordinary SQL through one DSN.
- Rvbbit can route the same frame to native, DuckDB, DataFusion, Vortex layouts,
  or NVIDIA GQE.
- Camera position and draw distance change selectivity; large repeated voxel
  scans give vector and GPU engines enough work to amortize dispatch.
- A standalone DuckDB run reads the exact source Parquet file and provides an
  honest in-process baseline.
- Every engine must produce the same rendered frame hash.

This is not the recursive-CTE implementation used by the original DuckDB-WASM
Doom demo. That 16x16 workload is too small to exercise an OLAP storage layer.
DoomQL stores repeated observations of a 256x256x16 voxel volume and reduces
them to the nearest visible surface for each lateral ray. The terminal layer
expands those SQL-computed depths into shaded wall slices.

## Requirements

- Python packages already used by the benchmark tree: `duckdb`, `psycopg`, and
  `clickhouse-connect`
- A PostgreSQL database with `pg_rvbbit`
- Optional GQE runtime for the `gpu_gqe` system
- Optional vanilla PostgreSQL and ClickHouse services for the cross-database
  targets

The table name must remain unqualified because the current GQE safety gate does
not accept schema-qualified table references.

## Load

From the repository root:

```bash
python3 bench/doomql/load.py \
  --dsn postgresql://postgres:rvbbit@localhost:55433/bench \
  --rows 5000000
```

The loader first writes `bench/doomql/data/doomql_world_5000000.parquet`, then
loads the same rows through ordinary PostgreSQL `COPY` into a `USING rvbbit`
table, compacts it, and builds the available layout variants. Pass
`--skip-variants` to omit that last step. Generated Parquet and JSON result
files are gitignored.

Scale with `--rows`. One complete voxel observation is 1,048,576 rows, so useful
crossovers are normally visible at 5M, 10M, 50M, and 100M rows.

### Vanilla database targets

The existing competitor compose provides suitable adjacent services:

```bash
docker compose -f docker/docker-compose.yml \
  -f docker/docker-compose.competitors.yml \
  --profile bench up -d pg-baseline clickhouse

python3 bench/doomql/load_competitors.py \
  --table doomql_world \
  --parquet bench/doomql/data/doomql_world_5000000.parquet
```

Defaults are vanilla PostgreSQL on `localhost:5440` and ClickHouse HTTP on
`localhost:8123`; both can be overridden with CLI flags or
`DOOMQL_POSTGRES_DSN`, `DOOMQL_CLICKHOUSE_HOST`, and
`DOOMQL_CLICKHOUSE_PORT`. PostgreSQL receives the rows through ordinary `COPY`
into an unindexed heap. ClickHouse receives the exact Parquet stream into a
`MergeTree ORDER BY sample_id`, preserving a neutral source order instead of
choosing a benchmark-specific clustering key.

## Benchmark

```bash
python3 bench/doomql/run.py \
  --dsn postgresql://postgres:rvbbit@localhost:55433/bench \
  --parquet bench/doomql/data/doomql_world_5000000.parquet \
  --frames 12 --render
```

The default comparison is:

```text
auto, rvbbit_native, duck_vector, duck_vortex, datafusion_vector,
datafusion_vortex, gpu_gqe, duckdb
```

After loading the adjacent databases, include the generic SQL targets:

```bash
python3 bench/doomql/run.py \
  --dsn postgresql://postgres:rvbbit@localhost:55433/bench \
  --table doomql_world \
  --parquet bench/doomql/data/doomql_world_5000000.parquet \
  --systems auto,datafusion_vortex,duck_vortex,gpu_gqe,duckdb,postgres,clickhouse
```

A forced candidate whose variant or runtime is unavailable is reported as
`skip`, not silently credited to a fallback. Cold latency, warm median, p95,
effective FPS, route, and frame parity are written to
`bench/doomql/results/last_run.json`.

The JSON also captures the PostgreSQL/Rvbbit versions, runtime availability,
source Parquet size and row count, authoritative row-group size/count, vanilla
PostgreSQL memory/parallel settings, ClickHouse engine/sorting key, and host CPU
count. A frame mismatch is a failing result, not a warning.

## Interactive mode

```bash
python3 bench/doomql/run.py \
  --dsn postgresql://postgres:rvbbit@localhost:55433/bench \
  --parquet bench/doomql/data/doomql_world_5000000.parquet \
  --interactive --system auto
```

Use `W`/`S` to move, `A`/`D` to turn, and `Q` to exit. The camera moves only
through cells classified as open by the same deterministic world function used
to generate the dataset.

## Reading the result

Small row counts may favor standalone DuckDB or a CPU route because GQE pays
dispatch and result-transfer costs. That is expected. The interesting result is
the scale and view shape where each engine crosses over, and whether `auto`
tracks the best forced route across a scripted fly-through.

### Local development snapshot

This is a reference run from 2026-07-12, not a canonical leaderboard: 8-core
i7-11700K, RTX 3090 Ti, 120x40 frames, 12 scripted cameras, two warmups, and
exact frame-hash parity across every system. Values are warm SQL medians.

| System | 5M rows | 50M rows |
|---|---:|---:|
| ClickHouse 24.10 MergeTree | 10.6 ms | 45.6 ms |
| standalone DuckDB | 11.0 ms | 44.9 ms |
| DataFusion/Vortex through RVBBIT | 13.5 ms | 50.9 ms |
| DuckDB/Vortex through RVBBIT | 16.9 ms | 71.1 ms |
| DataFusion/canonical through RVBBIT | 19.4 ms | 102.4 ms |
| RVBBIT auto | 21.2 ms | 78.6 ms |
| DuckDB/canonical through RVBBIT | 37.5 ms | 213.1 ms |
| NVIDIA GQE through RVBBIT | 38.3 ms | 83.8 ms |
| vanilla PostgreSQL 18 heap | 50.9 ms | 488.6 ms |
| RVBBIT native scan | 127.6 ms | 1.17 s |

GQE does not win this query on this hardware, but it grows only about 2.2x as
the dataset grows 10x, closing its relative gap to the fastest CPU route. Auto
chooses Duck/Vortex in both runs; measured DataFusion/Vortex is faster, making
this workload a useful new training shape for the router. The 50M
DataFusion/Vortex run also had a 272 ms p95 outlier, so the median is not the
whole operational story.

Keep hardware, row count, frame size, draw distance, cold/warm policy, and frame
hash parity beside any published timing. GPU-vs-CPU numbers without those facts
are not comparable.

## Scale sweep

To load and test multiple scales in sequence:

```bash
python3 bench/doomql/sweep.py \
  --dsn postgresql://postgres:rvbbit@localhost:55433/bench \
  --scales 1000000,5000000,10000000,50000000 \
  --systems auto,datafusion_vortex,gpu_gqe,duckdb,postgres,clickhouse
```

The sweep reuses generated source Parquet files, but it reloads, compacts, and
benchmarks the RVBBIT table at every scale. When `postgres` or `clickhouse`
appears in `--systems`, their loader is run at every scale too. Individual run
documents and a combined warm-median matrix are written under
`bench/doomql/results/`.
