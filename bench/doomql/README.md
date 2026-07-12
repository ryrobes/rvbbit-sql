# DoomQL: an analytical SQL raycaster

DoomQL turns either a deterministic voxel world or the real Doom E1M1 geometry
into a 120x40 terminal frame using one scan/filter/project/group query per
frame. It is deliberately a visual benchmark rather than a claim that
databases should be game engines.

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
them to the nearest visible surface for each lateral ray. Fixed-point camera
vectors keep arbitrary-degree rotation and perspective projection deterministic
across engines. The terminal layer expands those SQL-computed depths into
shaded wall slices.

## Requirements

- Python packages already used by the benchmark tree: `duckdb`, `psycopg`, and
  `clickhouse-connect`
- A PostgreSQL database with `pg_rvbbit`
- Optional GQE runtime for the `gpu_gqe` system
- Optional vanilla PostgreSQL and ClickHouse services for the cross-database
  targets
- The Doom shareware IWAD for `--world e1m1`; the default lookup is
  `~/repos2026/diffoom/assets/DOOM1.WAD`, overrideable with `DOOMQL_WAD`

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

### E1M1 WAD surfaces

The E1M1 mode reads the classic WAD lumps directly without another parser
dependency. `VERTEXES`, `LINEDEFS`, `SIDEDEFS`, and `SECTORS` provide authored
geometry; the BSP `NODES`, `SSECTORS`, and `SEGS` resolve sector ownership while
rasterizing floors and ceilings. The source WAD is never copied into this repo.

```bash
python3 bench/doomql/load.py \
  --world e1m1 \
  --wad ~/repos2026/diffoom/assets/DOOM1.WAD \
  --map-name E1M1 --grid-scale 16 \
  --dsn postgresql://postgres:rvbbit@localhost:55433/bench \
  --table doomql_e1m1 \
  --rows 5000000
```

This produces `bench/doomql/data/doomql_e1m1_5000000.parquet` and a generated
metadata JSON with the player start and material dictionary. E1M1 contributes
40,241 unique base surfaces at scale 16: 18,051 floors, 11,754 solid ceilings,
6,297 sky samples, 4,101 solid wall spans, and 38 masked wall samples. They are
repeated to the requested analytical row count.

Scale 16 is the balanced default used for the results below. Use
`--grid-scale 8` to retain E1M1's narrowest 8-unit detail and increase the base
surface set from 40,241 to 149,845 rows. Double `--draw-distance` from 96 to 192
to preserve the same physical view distance at that finer scale.

One-sided linedefs become full wall spans. Two-sided linedefs independently
emit lower walls, upper walls, and open portal space, so stairs and windows keep
their real sector heights. Masked middle textures use a perforated ASCII surface
instead of incorrectly closing the portal. Outdoor sky samples participate in
the SQL workload but remain visually open.

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

For the WAD surface schema, add `--world e1m1` and use the E1M1 table and
Parquet names:

```bash
python3 bench/doomql/load_competitors.py \
  --world e1m1 \
  --table doomql_e1m1 \
  --parquet bench/doomql/data/doomql_e1m1_5000000.parquet
```

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
  --interactive --system auto --turn-degrees 15
```

Use `W`/`S` to move forward/backward, `A`/`D` to turn, `Z`/`C` to strafe
left/right, and `Q` to exit. Turns default to 15 degrees; pass
`--turn-degrees 5` for finer movement or any value from 1 to 90. The camera
moves only through cells classified as open by the same deterministic world
function used to generate the dataset.

To walk the actual E1M1 start room and staircase:

```bash
python3 bench/doomql/run.py \
  --world e1m1 \
  --wad ~/repos2026/diffoom/assets/DOOM1.WAD \
  --dsn postgresql://postgres:rvbbit@localhost:55433/bench \
  --table doomql_e1m1 \
  --parquet bench/doomql/data/doomql_e1m1_5000000.parquet \
  --draw-distance 96 --turn-degrees 5 \
  --interactive --system auto
```

The E1M1 camera starts at the WAD player-one position `(1056, -3616)`, mapped to
grid `(114, 78)`, facing 90 degrees. Movement uses a swept 16-Doom-unit player
radius against the authoritative linedefs, plus BSP-resolved sector floors,
portal clearance, and Doom's 24-unit step limit. Eye height therefore changes
while descending and climbing the real stairs without allowing the camera to
clip through one-sided walls, blocking windows, or closed portals. Each SQL row
contains projected depth/lateral coordinates and vertical bounds; a half-cell
near plane and per-character depth buffer prevent close surfaces from exposing
geometry behind walls.

### Rendering modes

`--render-type ascii` is the portable default. `--render-type ansi-half` uses
24-bit ANSI foreground/background colors and a Unicode upper-half block (`▀`)
to encode two independently shaded vertical pixels in every terminal cell.
Colors are stable material families rather than extracted Doom textures; sector
light and distance attenuation provide the depth gradient.

```bash
python3 bench/doomql/run.py \
  --world e1m1 \
  --wad ~/repos2026/diffoom/assets/DOOM1.WAD \
  --dsn postgresql://postgres:rvbbit@localhost:55433/bench \
  --table doomql_e1m1 \
  --parquet bench/doomql/data/doomql_e1m1_5000000.parquet \
  --width 120 --height 40 \
  --draw-distance 96 --turn-degrees 5 \
  --render-type ansi-half --interactive --system auto
```

Width and height still describe terminal columns and rows. ANSI half-block mode
renders an internal `width x (height * 2)` color buffer, so `120x40` becomes
120x80 addressable color pixels without regenerating or reloading any data. A
truecolor terminal is required for the intended appearance. In WAD mode, wall
textures and flats are decoded through the original `PLAYPAL` and 32 normal
`COLORMAP` tables. Their representative colors follow the actual Doom assets,
while sector brightness and view distance select the light band for each
surface.

## Reading the result

Small row counts may favor standalone DuckDB or a CPU route because GQE pays
dispatch and result-transfer costs. That is expected. The interesting result is
the scale and view shape where each engine crosses over, and whether `auto`
tracks the best forced route across a scripted fly-through.

### Synthetic snapshot

This is a reference run from 2026-07-12, not a canonical leaderboard: 8-core
i7-11700K, 120x40 frames, 12 scripted cameras spanning 0 through 345 degrees,
two warmups, and exact frame-hash parity across every available system. Values
are warm SQL medians.

| System | 5M rows | 50M rows |
|---|---:|---:|
| standalone DuckDB | 14.3 ms | 79.9 ms |
| ClickHouse 24.10 MergeTree | 15.7 ms | 65.5 ms |
| DuckDB/Vortex through RVBBIT | 40.1 ms | 219.9 ms |
| RVBBIT auto | 43.1 ms | 227.1 ms |
| vanilla PostgreSQL 18 heap | 78.3 ms | 664.6 ms |
| DataFusion/Vortex through RVBBIT | 601.5 ms | 5.66 s |

Oblique headings increase rotated-ray group cardinality, making this a much
harder aggregation shape than the original four cardinal views. Auto chooses
Duck/Vortex in both runs. DataFusion/Vortex is especially sensitive to the new
grouping shape, reaching a 9.54 s p95 at 50M, which makes this useful both as a
router-training case and as a high-cardinality regression target. GQE was
unavailable in this validation environment and is reported as `skip`, rather
than being assigned an unverified timing.

### E1M1 surface snapshot

This 2026-07-12 run used the same host, 5M rows, grid scale 16, 120x40 output,
96-cell draw distance, 12 moving/turning cameras, and two warmups. Every
available engine produced the same 12 rendered frame hashes.

| System | 5M warm median | p95 |
|---|---:|---:|
| standalone DuckDB | 40.7 ms | 54.5 ms |
| ClickHouse 24.10 MergeTree | 95.2 ms | 145.7 ms |
| DataFusion/canonical through RVBBIT | 223.5 ms | 274.1 ms |
| DuckDB/Vortex through RVBBIT | 232.0 ms | 295.4 ms |
| RVBBIT auto | 234.1 ms | 305.9 ms |
| DuckDB/canonical through RVBBIT | 241.1 ms | 311.9 ms |
| vanilla PostgreSQL 18 heap | 340.4 ms | 482.4 ms |
| DataFusion/Vortex through RVBBIT | 431.9 ms | 518.6 ms |
| RVBBIT native scan | 652.0 ms | 802.3 ms |

The SQL groups transformed surface candidates by lateral position, depth,
vertical bounds, surface kind, material, sector, and surface identity. The
scripted cameras also traverse the first staircase, changing camera height.
This is intentionally more expensive than the synthetic nearest-wall query.
GQE was unavailable in this validation environment and is reported as `skip`.

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

Add `--world e1m1 --table doomql_e1m1 --wad ~/repos2026/diffoom/assets/DOOM1.WAD`
to run the same scale sweep over WAD surfaces.

The sweep reuses generated source Parquet files, but it reloads, compacts, and
benchmarks the RVBBIT table at every scale. When `postgres` or `clickhouse`
appears in `--systems`, their loader is run at every scale too. Individual run
documents and a combined warm-median matrix are written under
`bench/doomql/results/`.
