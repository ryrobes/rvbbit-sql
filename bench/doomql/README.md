# DoomQL: an analytical SQL raycaster

DoomQL turns either a deterministic voxel world or real Doom Episode 1 geometry
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
- The Doom shareware IWAD for `--world e1m1` or `--world episode1`; the default lookup is
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

### Full Episode 1

The additive `episode1` world leaves every existing E1M1 table and Parquet file
alone. It imports E1M1 through E1M9 into a separate schema with `map_name`,
linedef and texture coordinates, directional face light, and door identity:

```bash
python3 bench/doomql/load.py \
  --world episode1 \
  --wad ~/repos2026/diffoom/assets/DOOM1.WAD \
  --dsn postgresql://postgres:rvbbit@localhost:55433/bench \
  --table doomql_episode1 \
  --rows 5000000
```

At grid scale 16 the enriched nine-map schema contributes 709,818 unique
surfaces per complete episode scan. Map sizes remain naturally uneven, from
40,416 surfaces in E1M1 to 218,227 in E1M8. Requested row counts repeat the
complete episode stream,
so 5M and 50M exercise the same geometry at different analytical depth while
retaining map-size variability. Use `--maps E1M1,E1M2,E1M3` to generate a
subset. The default headless camera sequence changes maps between frames, which
changes both the map predicate and the amount of qualifying data.

This is a read-only state model: an open door is a camera-side set of door IDs,
and SQL excludes those tagged surfaces. No table is mutated when a door opens.
Entity billboards are intentionally not part of this schema yet.

Run or record a multi-map session against the separate table:

```bash
python3 bench/doomql/run.py \
  --world episode1 \
  --wad ~/repos2026/diffoom/assets/DOOM1.WAD \
  --dsn postgresql://postgres:rvbbit@localhost:55433/bench \
  --table doomql_episode1 \
  --parquet bench/doomql/data/doomql_episode1_5000000.parquet \
  --draw-distance 96 --turn-degrees 5 \
  --render-type ansi-half --interactive --system auto \
  --record-session bench/doomql/scripts/episode1-tour.json
```

### Vanilla database targets

The existing competitor compose provides suitable adjacent services:

```bash
docker compose -f docker/docker-compose.yml \
  -f docker/docker-compose.competitors.yml \
  --profile bench up -d pg-baseline citus hydra alloydb-omni clickhouse

python3 bench/doomql/load_competitors.py \
  --table doomql_world \
  --parquet bench/doomql/data/doomql_world_5000000.parquet \
  --targets postgres,citus,hydra,alloydb,clickhouse
```

Defaults are vanilla PostgreSQL on `localhost:5440`, Citus on `5441`, Hydra on
`5442`, AlloyDB Omni on `5443`, and ClickHouse HTTP on `8123`. Each has a
matching CLI flag and `DOOMQL_*` environment variable. PostgreSQL receives an
unindexed heap; Citus and Hydra receive `USING columnar` tables; AlloyDB's heap
is registered and force-refreshed into `google_columnar_engine`; ClickHouse
receives a `MergeTree ORDER BY sample_id`. All loaders use the identical source
Parquet and avoid benchmark-specific clustering keys.

For the WAD surface schema, add `--world e1m1` and use the E1M1 table and
Parquet names:

```bash
python3 bench/doomql/load_competitors.py \
  --world e1m1 \
  --table doomql_e1m1 \
  --parquet bench/doomql/data/doomql_e1m1_5000000.parquet
```

For the full episode use the separate dataset and table:

```bash
python3 bench/doomql/load_competitors.py \
  --world episode1 \
  --table doomql_episode1 \
  --parquet bench/doomql/data/doomql_episode1_5000000.parquet \
  --targets postgres,citus,hydra,alloydb,clickhouse
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
  --systems auto,datafusion_vortex,duck_vortex,duckdb,postgres,citus,hydra,alloydb,clickhouse
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
left/right, and `Q` to exit. In Episode 1 mode, `[`/`]` selects the previous or
next map and space opens or closes the nearest door. Turns default to 15 degrees; pass
`--turn-degrees 5` for finer movement or any value from 1 to 90. The camera
moves only through cells classified as open by the same deterministic world
function used to generate the dataset.

Record an interactive route as a reusable headless benchmark:

```bash
python3 bench/doomql/run.py \
  --world e1m1 \
  --wad ~/repos2026/diffoom/assets/DOOM1.WAD \
  --dsn postgresql://postgres:rvbbit@localhost:55433/bench \
  --table doomql_e1m1 \
  --parquet bench/doomql/data/doomql_e1m1_5000000.parquet \
  --render-type ansi-half --interactive --system auto \
  --record-session bench/doomql/scripts/e1m1-tour.json
```

The session JSON contains the rendering/world settings, every key and resolved
before/after camera, blocked movement flags, the initial frame, and one frame
for every navigation command. It intentionally excludes connection strings.
Replay restores the recorded world and rendering settings while leaving engine
selection, database endpoints, warmups, timeout, and output configurable:

```bash
python3 bench/doomql/run.py \
  --replay-session bench/doomql/scripts/e1m1-tour.json \
  --dsn postgresql://postgres:rvbbit@localhost:55433/bench \
  --systems auto,duck_vortex,datafusion_vector,duckdb,postgres,clickhouse
```

Replay uses the exact recorded camera frames instead of recalculating movement,
and writes the session path and SHA-256 hash into the benchmark result JSON.
Use `--replay-table` and `--replay-parquet` to run the same recorded tour at a
different data scale without editing or re-signing the session file.

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
Episode 1 colors are sampled from the authored wall textures and 64x64 flats;
sector light, directional face light, the original `COLORMAP`, and distance
attenuation provide the depth gradient. E1M1 compatibility mode retains its
existing representative material colors.

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
truecolor terminal is required for the intended appearance. In Episode 1 mode,
wall textures and flats are decoded through the original `PLAYPAL` and 32 normal
`COLORMAP` tables. Texture coordinates and directional face-light values remain
SQL columns and grouping inputs rather than renderer-only metadata.

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
  --scales 5m,50m --keep-loaded \
  --replay-session bench/doomql/scripts/e1m1-tour1.json \
  --systems auto,duck_vortex,duckdb,postgres,citus,hydra,alloydb,clickhouse
```

Scale values accept raw integers or `k`/`m`/`b` suffixes. Without a replay
session, add `--world e1m1 --table doomql_e1m1` for E1M1, or
`--world episode1 --table doomql_episode1` for the full episode, plus the WAD
path.

The sweep reuses generated source Parquet files, but it reloads, compacts, and
benchmarks every selected target at each scale. `--keep-loaded` gives every
scale its own suffixed table; use `--skip-load` for later back-to-back reruns.
Individual run documents and a combined warm-median matrix are written under
`bench/doomql/results/`.

## Scale curve viewer

Build a self-contained HTML/JavaScript chart from compatible individual scale
results:

```bash
python3 bench/doomql/viz.py \
  bench/doomql/results/scale-episode1-5000000.json \
  bench/doomql/results/scale-episode1-15000000.json \
  bench/doomql/results/scale-episode1-50000000.json \
  bench/doomql/results/scale-episode1-100000000.json \
  bench/doomql/results/scale-episode1-200000000.json
```

With no positional inputs, the viewer uses every
`scale-episode1-*.json` document. It rejects runs whose frame count, viewport,
draw distance, render type, map set, warmup policy, or other benchmark-shape
settings differ, so unrelated timings cannot silently become one curve.

The command writes `bench/doomql/results/episode1-scale-curves.html`, which
opens directly from disk without a web server or external JavaScript, plus
`episode1-scale-curves.json`, a compact combined data document. The viewer can
switch between median, p95, cold latency, and FPS, use linear or logarithmic
axes, toggle individual engines, and display the exact values in a table.
