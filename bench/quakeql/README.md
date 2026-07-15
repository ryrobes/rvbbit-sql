# QuakeQL

QuakeQL turns Quake's E1M1 BSP into a SQL-heavy 3D rendering workload. It is
both an interactive ANSI demo and a repeatable analytical benchmark for RVBBIT,
standalone DuckDB, and an optional PostgreSQL heap table.

This is not a port of the Quake renderer. The loader reads the original BSP
geometry, mip textures, palette, colormap, and baked lightmaps, then samples the
world faces into columnar relations. Each frame is one SQL statement over those
relations.

## Asset

Quake's source release and the game data have different distribution terms.
The benchmark code does not include `pak0.pak`. Put a legally obtained Quake
shareware or retail PAK here:

```text
bench/quakeql/assets/pak0.pak
```

The PAK is a loader-only seed. Set `QUAKEQL_PAK` or pass `--pak` to `load.py`;
`run.py` has no PAK argument and never opens the archive. The canonical
shareware PAK is 18,689,235 bytes and has these identities:

```text
MD5   5906e5998fc3d896ddaf5e6a62e03abb
SHA1  36b42dc7b6313fd9cabc0be8b9e9864840929735
```

The parser follows the Quake BSP 29 structures and lightmap calculations from
the [GPL source release](https://github.com/id-Software/Quake). It rejects
invalid PAK directories, unsupported BSP versions, truncated lumps, and invalid
face references.

## Load

The default load creates 5,000,000 rows in `quakeql_e1m1`, using ordinary
PostgreSQL `COPY`, then refreshes RVBBIT acceleration and layout variants:

```bash
python3 bench/quakeql/load.py
```

The default E1M1 working set contains 1,098,853 unique surface samples from
5,342 faces. The static world uses a 16-unit sampling step; doors, lifts,
buttons, and other visible brush models use a denser 4-unit step so their close
surfaces remain coherent. `--rows` repeats that full working set with a
`scan_id`; this preserves exact output while controlling scan volume. The
generated Parquet and metadata live under `bench/quakeql/data/` and are ignored
by Git.

The default load also creates four compact render-support relations. None are
repeated with `--rows`:

| Relation | E1M1 rows | Purpose |
| --- | ---: | --- |
| `quakeql_e1m1_texels` | 540,600 | All four PAK texture mip levels |
| `quakeql_e1m1_lightmaps` | 163,096 | Per-face baked light samples and style IDs |
| `quakeql_e1m1_materials` | 137 | Static and animated material-frame mappings |
| `quakeql_colormap` | 16,384 | Quake's 64 light levels by 256 palette indexes |

It also normalizes the remaining engine startup state into SQL runtime
relations:

| Relation | E1M1 rows | Purpose |
| --- | ---: | --- |
| `quakeql_e1m1_maps` | 1 | Spawn, yaw, and world bounds |
| `quakeql_e1m1_planes` | 1,810 | BSP collision planes |
| `quakeql_e1m1_clipnodes` | 5,408 | Player and brush collision trees |
| `quakeql_e1m1_models` | 58 | Model bounds and hull roots |
| `quakeql_e1m1_brushes` | 31 | Motion, solidity, targets, and endpoints |

Load the complete shareware episode at its natural sampled cardinality:

```bash
python3 bench/quakeql/load.py --episode1
```

This creates `quakeql_episode1` plus its render and runtime relations.
The Episode 1 lightmap relation has 1,373,770 rows and its material relation has
843 rows. The runtime adds 17,403 planes, 46,170 clipnodes, 623 models, and 335
brushes across nine maps. It includes the `START` portal map plus `E1M1` through
`E1M8`, with every geometry sample written once (`scan_id = 0`). `--episode1`
defaults to natural cardinality; passing an explicit `--rows` larger than the
natural total is the opt-in inflation path. `--natural-rows` provides the same
one-to-one behavior for a single map, and `--maps START,E1M1,E1M2` builds an
arbitrary map set.

| Map | Natural geometry rows |
| --- | ---: |
| `START` | 1,195,229 |
| `E1M1` | 1,098,853 |
| `E1M2` | 1,372,019 |
| `E1M3` | 2,016,657 |
| `E1M4` | 1,547,071 |
| `E1M5` | 1,294,817 |
| `E1M6` | 1,519,693 |
| `E1M7` | 607,073 |
| `E1M8` | 1,198,631 |
| **Episode total** | **11,850,043** |

Useful loader options:

```bash
# Generate Parquet without touching PostgreSQL.
python3 bench/quakeql/load.py --rows 5000000 --skip-load

# Reuse an existing Parquet and load it without layout variants.
python3 bench/quakeql/load.py --rows 5000000 --reuse-parquet --skip-variants

# Add render support and normalized SQL runtime state. Existing geometry and
# texture tables are not dropped or recopied.
python3 bench/quakeql/load.py --support-only --reuse-parquet
python3 bench/quakeql/load.py --episode1 --support-only --reuse-parquet

# Denser geometry. This raises the unique working set substantially.
python3 bench/quakeql/load.py --sample-step 8 --rows 10000000

# Reproduce the original static-world-only workload.
python3 bench/quakeql/load.py --world-only --rows 5000000
```

## Run

Render one frame through RVBBIT Auto:

```bash
python3 bench/quakeql/run.py --render --system auto
```

Run the SQL-native texture renderer, where fragment expansion, ray/plane
intersection, perspective UV reconstruction, mip selection, texel lookup,
animated material selection, lightmap interpolation, colormap lookup, shading,
and Z ranking all execute inside the frame query:

```bash
python3 bench/quakeql/run.py --interactive \
  --renderer sql-texture --system duck_vector --splat-cap 2
```

Explore a map from the natural Episode 1 dataset:

```bash
python3 bench/quakeql/run.py --interactive --episode1 --map-name START \
  --renderer sql-texture --system duck_vortex --splat-cap 2

python3 bench/quakeql/run.py --interactive --episode1 --map-name E1M6 \
  --renderer sql-texture --system duck_vortex --splat-cap 2
```

`--episode1` selects the combined tables and Parquet files; `--map-name`
selects the geometry partition and SQL-backed spawn/collision world.

Runtime map state is hydrated from the selected engine automatically: RVBBIT
sessions query RVBBIT tables, standalone DuckDB queries the Parquets, and
vanilla PostgreSQL queries its heap tables. Override that selection with:

```bash
python3 bench/quakeql/run.py --interactive --runtime-source rvbbit
python3 bench/quakeql/run.py --interactive --runtime-source postgres --system postgres
```

The `--map-table`, `--plane-table`, `--clipnode-table`, `--model-table`, and
`--brush-table` options select custom runtime tables. Matching `--*-parquet`
options select custom embedded-DuckDB sources.

`--mip-bias -1` retains a finer mip for more texture detail; positive values
trade detail for stability at distance. `--texture-table` and
`--texture-parquet` select non-default texture relations. The corresponding
`--lightmap-*`, `--material-*`, and `--colormap-*` options select the other SQL
render inputs.

Animated PAK materials advance at Quake's five-frame-per-second cadence. Liquid
and sky UVs move continuously, and light styles advance at ten samples per
second. Headless benchmarks use deterministic `--animation-step` increments
(default `0.1` seconds), so every engine receives identical frame SQL.
Interactive rendering uses elapsed wall time.

Limit close-up sample footprints independently of dataset density:

```bash
python3 bench/quakeql/run.py --interactive --system auto --splat-cap 2
```

`--splat-cap` is a screen-space radius. `0` draws one pixel per geometry anchor,
`1` allows up to 3x3, and the default `6` allows up to 13x13. In the default
`samples` renderer Python expands the SQL result. In `sql-texture`, SQL expands
candidate fragments and emits final pixels; Python performs no splatting.
Lowering the cap exposes more texture detail but can reveal holes when the
loaded surface sampling is too sparse. Match `--sample-step` and
`--brush-sample-step` to the loader values when changing reconstruction.

Explore E1M1 in the terminal:

```bash
python3 bench/quakeql/run.py --interactive --system auto
```

Set the SQL prefilter and far clipping distance in Quake world units with
`--render-distance` (default `768`). Larger values expose more of the map but
scan and project more candidate geometry:

```bash
python3 bench/quakeql/run.py --interactive --episode1 --map-name E1M1 \
  --renderer sql-texture --render-distance 1536
```

`--draw-distance` remains available as a compatibility alias.

Controls:

| Key | Action |
| --- | --- |
| `W` / `S` | Move forward / backward |
| `A` / `D` | Turn left / right |
| `Z` / `C` | Strafe left / right |
| `R` / `F` | Look up / down |
| `Space` | Activate the nearest door, button, or lift |
| `X` | Reset to the player start |
| `Q`, `Esc` | Quit |

Run the default 16-frame benchmark:

```bash
python3 bench/quakeql/run.py
```

The default systems are RVBBIT Auto, Native, Duck Vector, Duck Vortex,
DataFusion Vector, DataFusion Vortex, and standalone DuckDB. Add vanilla
PostgreSQL after loading the same Parquet into its heap:

```bash
python3 bench/quakeql/load.py --rows 5000000 --reuse-parquet \
  --access-method heap \
  --dsn postgresql://postgres:rvbbit@localhost:55432/bench

python3 bench/quakeql/run.py \
  --systems auto,duck_vortex,datafusion_vector,duckdb,postgres \
  --postgres-dsn postgresql://postgres:rvbbit@localhost:55432/bench
```

Results include cold latency, median, p95, FPS, chosen route, and rendered-frame
parity. The machine-readable result is written to
`bench/quakeql/results/last_run.json` by default.

## What SQL Does

One frame query performs:

1. E1M1 and axis-aligned draw-distance filtering.
2. Fixed-point yaw and pitch transforms into 3D camera space.
3. Near/far clipping and perspective projection.
4. Duplicate collapse across repeated scans.
5. Per-pixel `ROW_NUMBER()` depth ranking.
6. Baked-lightmap, fullbright, and distance shading.

With `--renderer sql-texture`, the query additionally performs:

1. Screen-space fragment expansion around unique geometry anchors.
2. Per-fragment camera ray construction and BSP plane intersection.
3. Perspective-correct Quake S/T reconstruction, including moved brush models.
4. Projected-density mip selection and wrapped texel coordinates.
5. Time-based animated-material selection and liquid/sky UV mutation.
6. A relational join to the selected PAK mip texel.
7. Bilinear sampling of all face lightmap styles at animated intensities.
8. Palette-index transformation through Quake's 64-level colormap.
9. Dynamic brush translation of anchors, planes, and texture bases.
10. Fragment de-duplication and final per-pixel depth ranking.

At runtime, Python is limited to keyboard input, ANSI encoding, optional sparse
sample reconstruction, brush-state timing, and interpreting collision rows that
were fetched through SQL. It does not read or parse the PAK or BSP. Texture
indexes, full lightmaps, light styles, animated material mappings, colormap,
spawn metadata, collision trees, models, and brushes all come from loaded
relations. Only `load.py` performs PAK/BSP ingestion while seeding them.

## Data Shape

Each geometry row includes fixed-point world XYZ, face/model/material IDs,
quantized face normal, translated plane distance, Quake S/T vectors and offsets,
texture dimensions and anchor UV/RGB, baked light, and fullbright state. Each
texel row includes its material, mip level, coordinates, palette index, RGB, and
fullbright state. The default base sample distribution is:

| Surface | Samples |
| --- | ---: |
| Walls | 617,380 |
| Floors | 192,915 |
| Ceilings | 189,498 |
| Sky | 21,878 |
| Liquids | 77,182 |

The default dataset includes 31 visible inline models: 14 doors, seven secret
doors, six buttons, two lifts, and two static `func_wall` models. Invisible
trigger volumes are excluded. Start-open doors and lowered lifts are placed at
their Quake map-start positions, and their individual collision hulls participate
in interactive movement.

In interactive SQL-texture mode, Space toggles the nearest movable brush. Paired
door leaves and button target links activate together, SQL translates the model
geometry continuously at its map-defined speed, and collision uses the same
interpolated pose. General QuakeC trigger choreography, path-driven
`func_train` models, automatic door timers, non-brush entities, particles,
weapon sprites, and Quake's visibility/PVS traversal remain out of scope.

## Tests

```bash
python3 -m pytest bench/quakeql/test_quakeql.py -q
```

The PAK-independent parser and projection tests always run. When `pak0.pak` is
available, the suite also verifies E1M1 extraction and a complete
PAK-to-Parquet-to-SQL collision-world round trip.
