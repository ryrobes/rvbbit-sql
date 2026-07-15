#!/usr/bin/env python3
"""Extract Quake BSP surfaces, write Parquet, and load RVBBIT tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import duckdb
import psycopg

try:
    from .pak_bsp import (
        PakArchive,
        QuakeBsp,
        extract_lightmap_texels,
        extract_material_frames,
        find_default_pak,
        rasterize_map,
    )
    from .workload import (
        BRUSH_COLUMNS,
        CLIPNODE_COLUMNS,
        COLORMAP_COLUMNS,
        LIGHTMAP_COLUMNS,
        MAP_COLUMNS,
        MATERIAL_COLUMNS,
        MODEL_COLUMNS,
        PLANE_COLUMNS,
        TABLE_COLUMNS,
        TEXTURE_COLUMNS,
        combine_geometry_parquets,
        combine_relation_parquets,
        combine_texture_parquets,
        create_colormap_parquet,
        create_lightmap_parquet,
        create_material_parquet,
        create_parquet,
        create_runtime_parquets,
        create_texture_parquet,
    )
except ImportError:
    from pak_bsp import (
        PakArchive,
        QuakeBsp,
        extract_lightmap_texels,
        extract_material_frames,
        find_default_pak,
        rasterize_map,
    )
    from workload import (
        BRUSH_COLUMNS,
        CLIPNODE_COLUMNS,
        COLORMAP_COLUMNS,
        LIGHTMAP_COLUMNS,
        MAP_COLUMNS,
        MATERIAL_COLUMNS,
        MODEL_COLUMNS,
        PLANE_COLUMNS,
        TABLE_COLUMNS,
        TEXTURE_COLUMNS,
        combine_geometry_parquets,
        combine_relation_parquets,
        combine_texture_parquets,
        create_colormap_parquet,
        create_lightmap_parquet,
        create_material_parquet,
        create_parquet,
        create_runtime_parquets,
        create_texture_parquet,
    )


HERE = Path(__file__).resolve().parent
MAP_FORMAT = "quakeql-map-v3"
EPISODE1_MAPS = (
    "START",
    "E1M1",
    "E1M2",
    "E1M3",
    "E1M4",
    "E1M5",
    "E1M6",
    "E1M7",
    "E1M8",
)
DEFAULT_DSN = os.environ.get("RVBBIT_DSN", "postgresql://postgres:rvbbit@localhost:55433/bench")
POSTGRES_COLUMNS = """
    sample_id bigint,
    scan_id integer,
    map_name text,
    surface_id integer,
    model_id smallint,
    world_x integer,
    world_y integer,
    world_z integer,
    normal_x smallint,
    normal_y smallint,
    normal_z smallint,
    surface_kind smallint,
    material_id smallint,
    texture_name text,
    texture_u smallint,
    texture_v smallint,
    red smallint,
    green smallint,
    blue smallint,
    light smallint,
    fullbright smallint,
    plane_distance double precision,
    texture_width smallint,
    texture_height smallint,
    texture_s_x double precision,
    texture_s_y double precision,
    texture_s_z double precision,
    texture_s_offset double precision,
    texture_t_x double precision,
    texture_t_y double precision,
    texture_t_z double precision,
    texture_t_offset double precision
"""
TEXTURE_POSTGRES_COLUMNS = """
    map_name text,
    material_id smallint,
    texture_name text,
    mip_level smallint,
    mip_width smallint,
    mip_height smallint,
    texel_u smallint,
    texel_v smallint,
    palette_index smallint,
    red smallint,
    green smallint,
    blue smallint,
    fullbright smallint
"""
LIGHTMAP_POSTGRES_COLUMNS = """
    map_name text,
    surface_id integer,
    style_slot smallint,
    style_id smallint,
    light_min_s integer,
    light_min_t integer,
    light_width smallint,
    light_height smallint,
    light_u smallint,
    light_v smallint,
    light_value smallint
"""
MATERIAL_POSTGRES_COLUMNS = """
    map_name text,
    source_material_id smallint,
    source_texture_name text,
    frame_index smallint,
    frame_count smallint,
    target_material_id smallint,
    target_texture_name text
"""
COLORMAP_POSTGRES_COLUMNS = """
    light_level smallint,
    palette_index smallint,
    mapped_palette_index smallint,
    red smallint,
    green smallint,
    blue smallint
"""
MAP_POSTGRES_COLUMNS = """
    map_name text,
    spawn_x double precision,
    spawn_y double precision,
    spawn_z double precision,
    spawn_yaw double precision,
    min_x double precision,
    min_y double precision,
    min_z double precision,
    max_x double precision,
    max_y double precision,
    max_z double precision
"""
PLANE_POSTGRES_COLUMNS = """
    map_name text,
    plane_id integer,
    normal_x double precision,
    normal_y double precision,
    normal_z double precision,
    distance double precision,
    plane_kind smallint
"""
CLIPNODE_POSTGRES_COLUMNS = """
    map_name text,
    clipnode_id integer,
    plane_id integer,
    child_front integer,
    child_back integer
"""
MODEL_POSTGRES_COLUMNS = """
    map_name text,
    model_id integer,
    min_x double precision,
    min_y double precision,
    min_z double precision,
    max_x double precision,
    max_y double precision,
    max_z double precision,
    origin_x double precision,
    origin_y double precision,
    origin_z double precision,
    headnode_0 integer,
    headnode_1 integer,
    headnode_2 integer,
    headnode_3 integer,
    visleafs integer,
    first_face integer,
    face_count integer
"""
BRUSH_POSTGRES_COLUMNS = """
    map_name text,
    entity_id integer,
    model_id integer,
    classname text,
    origin_x double precision,
    origin_y double precision,
    origin_z double precision,
    solid boolean,
    mover boolean,
    targetname text,
    target text,
    closed_x double precision,
    closed_y double precision,
    closed_z double precision,
    open_x double precision,
    open_y double precision,
    open_z double precision,
    speed double precision
"""


def simple_identifier(value: str) -> str:
    if not value or not value.replace("_", "a").isalnum() or value[0].isdigit():
        raise argparse.ArgumentTypeError("table must be an unqualified SQL identifier")
    return value


def build_world(
    pak_path: Path,
    map_name: str,
    sample_step: float,
    brush_sample_step: float,
    include_brush_models: bool,
    pak: PakArchive | None = None,
):
    pak = pak or PakArchive(pak_path)
    bsp_bytes = pak.read(f"maps/{map_name.lower()}.bsp")
    bsp = QuakeBsp(bsp_bytes, pak.read("gfx/palette.lmp"), map_name)
    started = time.perf_counter()
    world = rasterize_map(bsp, sample_step, include_brush_models, brush_sample_step)
    elapsed = time.perf_counter() - started
    metadata = {
        "format": MAP_FORMAT,
        "map_name": world.map_name,
        "pak": pak.hashes(),
        "bsp_sha256": hashlib.sha256(bsp_bytes).hexdigest(),
        "bsp_bytes": len(bsp_bytes),
        "sample_step": sample_step,
        "brush_sample_step": brush_sample_step,
        "coordinate_scale": 8,
        "base_samples": len(world.samples),
        "raster_seconds": round(elapsed, 3),
        "spawn_origin": list(world.spawn_origin),
        "spawn_yaw": world.spawn_yaw,
        "bounds": [list(world.bounds[0]), list(world.bounds[1])],
        "faces": world.face_count,
        "sampled_faces": world.sampled_faces,
        "models": world.model_count,
        "include_brush_models": include_brush_models,
        "brush_models": len(world.brush_models),
        "brush_samples": world.brush_sample_count,
        "brush_classes": dict(
            sorted(
                {
                    classname: sum(brush.classname == classname for brush in world.brush_models)
                    for classname in {brush.classname for brush in world.brush_models}
                }.items()
            )
        ),
        "textures": list(world.texture_names),
        "surface_samples": world.surface_counts,
    }
    return pak, bsp, world, metadata


def load_rows(
    dsn: str,
    table: str,
    parquet: Path,
    batch_rows: int,
    refresh_variants: bool,
    access_method: str,
    table_columns: tuple[str, ...] = TABLE_COLUMNS,
    postgres_columns: str = POSTGRES_COLUMNS,
) -> dict[str, object]:
    columns = ", ".join(table_columns)
    source = duckdb.connect(":memory:")
    source.execute(f"SELECT {columns} FROM read_parquet(?)", [str(parquet)])
    copied = 0
    next_progress = 1_000_000
    copy_started = time.perf_counter()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(f"CREATE TABLE {table} ({postgres_columns}) USING {access_method}")
        with conn.cursor().copy(f"COPY {table} ({columns}) FROM STDIN") as copy:
            while batch := source.fetchmany(batch_rows):
                for row in batch:
                    copy.write_row(row)
                copied += len(batch)
                if copied >= next_progress:
                    print(f"  copied {copied:,} rows", flush=True)
                    while next_progress <= copied:
                        next_progress += 1_000_000
        copy_seconds = time.perf_counter() - copy_started
        conn.execute(f"ANALYZE {table}")
        refresh = None
        compact_seconds = None
        variants = None
        variant_seconds = None
        if access_method == "rvbbit":
            compact_started = time.perf_counter()
            refresh = conn.execute(
                "SELECT rvbbit.refresh_acceleration(%s::regclass, false)", (table,)
            ).fetchone()[0]
            compact_seconds = time.perf_counter() - compact_started
            if refresh_variants:
                variant_started = time.perf_counter()
                variants = conn.execute(
                    "SELECT rvbbit.refresh_layout_variants(%s::regclass)", (table,)
                ).fetchone()[0]
                variant_seconds = time.perf_counter() - variant_started
            visible = conn.execute(
                """
                SELECT coalesce(sum(n_rows), 0)::bigint,
                       coalesce(sum(n_bytes), 0)::bigint,
                       count(*)::integer
                FROM rvbbit.row_groups_visible
                WHERE table_oid = %s::regclass
                """,
                (table,),
            ).fetchone()
        else:
            visible = (
                copied,
                conn.execute("SELECT pg_total_relation_size(%s::regclass)", (table,)).fetchone()[0],
                0,
            )
    source.close()
    return {
        "rows": copied,
        "access_method": access_method,
        "copy_seconds": round(copy_seconds, 3),
        "compact_seconds": round(compact_seconds, 3) if compact_seconds is not None else None,
        "variant_seconds": round(variant_seconds, 3) if variant_seconds is not None else None,
        "variant_rows": variants,
        "storage_rows": visible[0],
        "storage_bytes": visible[1],
        "row_groups": visible[2],
        "refresh": refresh,
    }


def generate_render_support(
    pak_path: Path,
    map_names: tuple[str, ...],
    lightmap_parquet: Path,
    material_parquet: Path,
    colormap_parquet: Path,
    runtime_parquets: dict[str, Path],
) -> dict[str, object]:
    pak = PakArchive(pak_path)
    lightmap_parts: list[Path] = []
    material_parts: list[Path] = []
    runtime_parts: dict[str, list[Path]] = {name: [] for name in runtime_parquets}
    with tempfile.TemporaryDirectory(prefix="quakeql-render-") as temp_directory:
        temp = Path(temp_directory)
        for index, map_name in enumerate(map_names):
            bsp = QuakeBsp.from_pak(pak, map_name)
            lightmap_part = temp / f"{index:02d}_{map_name.lower()}_lightmaps.parquet"
            material_part = temp / f"{index:02d}_{map_name.lower()}_materials.parquet"
            map_runtime_paths = {
                name: temp / f"{index:02d}_{map_name.lower()}_{name}.parquet"
                for name in runtime_parquets
            }
            create_lightmap_parquet(
                lightmap_part,
                map_name,
                extract_lightmap_texels(bsp),
            )
            create_material_parquet(
                material_part,
                map_name,
                extract_material_frames(bsp),
            )
            lightmap_parts.append(lightmap_part)
            material_parts.append(material_part)
            create_runtime_parquets(bsp, map_runtime_paths)
            for name, path in map_runtime_paths.items():
                runtime_parts[name].append(path)
        lightmap_rows = combine_relation_parquets(
            lightmap_parquet,
            lightmap_parts,
            LIGHTMAP_COLUMNS,
        )
        material_rows = combine_relation_parquets(
            material_parquet,
            material_parts,
            MATERIAL_COLUMNS,
            10_000,
        )
        runtime_columns = {
            "maps": MAP_COLUMNS,
            "planes": PLANE_COLUMNS,
            "clipnodes": CLIPNODE_COLUMNS,
            "models": MODEL_COLUMNS,
            "brushes": BRUSH_COLUMNS,
        }
        runtime_rows = {
            name: combine_relation_parquets(
                runtime_parquets[name],
                runtime_parts[name],
                runtime_columns[name],
            )
            for name in runtime_parquets
        }
    colormap_rows = create_colormap_parquet(
        colormap_parquet,
        pak.read("gfx/palette.lmp"),
        pak.read("gfx/colormap.lmp"),
    )
    result: dict[str, object] = {
        "lightmap_rows": lightmap_rows,
        "material_rows": material_rows,
        "colormap_rows": colormap_rows,
        "lightmap_parquet": str(lightmap_parquet.resolve()),
        "lightmap_parquet_bytes": lightmap_parquet.stat().st_size,
        "material_parquet": str(material_parquet.resolve()),
        "material_parquet_bytes": material_parquet.stat().st_size,
        "colormap_parquet": str(colormap_parquet.resolve()),
        "colormap_parquet_bytes": colormap_parquet.stat().st_size,
    }
    for name, path in runtime_parquets.items():
        result[f"runtime_{name}_rows"] = runtime_rows[name]
        result[f"runtime_{name}_parquet"] = str(path.resolve())
        result[f"runtime_{name}_parquet_bytes"] = path.stat().st_size
    return result


def generate_dataset(
    pak_path: Path,
    map_names: tuple[str, ...],
    parquet: Path,
    texture_parquet: Path,
    lightmap_parquet: Path,
    material_parquet: Path,
    colormap_parquet: Path,
    runtime_parquets: dict[str, Path],
    requested_rows: int | None,
    sample_step: float,
    brush_sample_step: float,
    include_brush_models: bool,
    row_group_size: int,
) -> dict[str, object]:
    started = time.perf_counter()
    map_metadata: list[dict[str, object]] = []
    geometry_parts: list[Path] = []
    texture_parts: list[Path] = []
    archive = PakArchive(pak_path)
    with tempfile.TemporaryDirectory(prefix="quakeql-episode-") as temp_directory:
        temp = Path(temp_directory)
        for index, map_name in enumerate(map_names):
            print(f"Reading {map_name} from {pak_path}", flush=True)
            pak, bsp, world, item = build_world(
                pak_path,
                map_name,
                sample_step,
                brush_sample_step,
                include_brush_models,
                archive,
            )
            geometry_part = temp / f"{index:02d}_{map_name.lower()}_geometry.parquet"
            texture_part = temp / f"{index:02d}_{map_name.lower()}_texels.parquet"
            create_parquet(geometry_part, world, len(world.samples), row_group_size)
            texture_rows = create_texture_parquet(texture_part, bsp)
            item["texture_rows"] = texture_rows
            map_metadata.append(item)
            geometry_parts.append(geometry_part)
            texture_parts.append(texture_part)
            print(
                f"  {len(world.samples):,} natural geometry rows, {texture_rows:,} mip texels",
                flush=True,
            )

        base_rows = sum(int(item["base_samples"]) for item in map_metadata)
        rows = requested_rows if requested_rows is not None else base_rows
        if rows < base_rows:
            raise ValueError(f"--rows {rows:,} is below the {base_rows:,} natural geometry samples")
        combine_geometry_parquets(
            parquet,
            geometry_parts,
            base_rows,
            rows,
            row_group_size,
        )
        texture_rows = combine_texture_parquets(texture_parquet, texture_parts)

    render_support = generate_render_support(
        pak_path,
        map_names,
        lightmap_parquet,
        material_parquet,
        colormap_parquet,
        runtime_parquets,
    )

    surface_names = ("wall", "floor", "ceiling", "sky", "liquid")
    metadata: dict[str, object] = {
        "format": MAP_FORMAT,
        "dataset": "episode1" if map_names == EPISODE1_MAPS else "maps",
        "map_name": map_names[0] if len(map_names) == 1 else None,
        "map_names": list(map_names),
        "pak": pak.hashes(),
        "sample_step": sample_step,
        "brush_sample_step": brush_sample_step,
        "coordinate_scale": 8,
        "include_brush_models": include_brush_models,
        "base_samples": base_rows,
        "rows": rows,
        "natural_cardinality": rows == base_rows,
        "texture_rows": texture_rows,
        "sampled_faces": sum(int(item["sampled_faces"]) for item in map_metadata),
        "brush_samples": sum(int(item["brush_samples"]) for item in map_metadata),
        "surface_samples": {
            name: sum(int(item["surface_samples"][name]) for item in map_metadata)  # type: ignore[index]
            for name in surface_names
        },
        "maps": map_metadata,
        "parquet": str(parquet.resolve()),
        "parquet_bytes": parquet.stat().st_size,
        "texture_parquet": str(texture_parquet.resolve()),
        "texture_parquet_bytes": texture_parquet.stat().st_size,
        "parquet_seconds": round(time.perf_counter() - started, 3),
        **render_support,
    }
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--pak", type=Path, default=find_default_pak())
    map_group = parser.add_mutually_exclusive_group()
    map_group.add_argument("--map-name", default="E1M1")
    map_group.add_argument(
        "--episode1",
        action="store_true",
        help="load START and E1M1 through E1M8 as one map-partitioned dataset",
    )
    map_group.add_argument("--maps", help="comma-separated BSP map names")
    parser.add_argument("--table", type=simple_identifier)
    parser.add_argument("--texture-table", type=simple_identifier)
    parser.add_argument("--lightmap-table", type=simple_identifier)
    parser.add_argument("--material-table", type=simple_identifier)
    parser.add_argument("--colormap-table", type=simple_identifier, default="quakeql_colormap")
    parser.add_argument("--map-table", type=simple_identifier)
    parser.add_argument("--plane-table", type=simple_identifier)
    parser.add_argument("--clipnode-table", type=simple_identifier)
    parser.add_argument("--model-table", type=simple_identifier)
    parser.add_argument("--brush-table", type=simple_identifier)
    cardinality_group = parser.add_mutually_exclusive_group()
    cardinality_group.add_argument("--rows", type=int)
    cardinality_group.add_argument(
        "--natural-rows",
        action="store_true",
        help="write each sampled geometry row exactly once",
    )
    parser.add_argument("--sample-step", type=float, default=16.0)
    parser.add_argument("--brush-sample-step", type=float, default=4.0)
    parser.add_argument("--row-group-size", type=int, default=1_000_000)
    parser.add_argument("--copy-batch-rows", type=int, default=25_000)
    parser.add_argument("--parquet", type=Path)
    parser.add_argument("--texture-parquet", type=Path)
    parser.add_argument("--lightmap-parquet", type=Path)
    parser.add_argument("--material-parquet", type=Path)
    parser.add_argument("--colormap-parquet", type=Path)
    parser.add_argument("--map-parquet", type=Path)
    parser.add_argument("--plane-parquet", type=Path)
    parser.add_argument("--clipnode-parquet", type=Path)
    parser.add_argument("--model-parquet", type=Path)
    parser.add_argument("--brush-parquet", type=Path)
    parser.add_argument("--reuse-parquet", action="store_true")
    parser.add_argument(
        "--support-only",
        action="store_true",
        help="generate/load render and SQL runtime relations without reloading geometry",
    )
    brush_group = parser.add_mutually_exclusive_group()
    brush_group.add_argument(
        "--include-brush-models",
        dest="include_brush_models",
        action="store_true",
        help="include visible inline BSP models (default)",
    )
    brush_group.add_argument(
        "--world-only",
        dest="include_brush_models",
        action="store_false",
        help="exclude doors, lifts, buttons, and other visible brush models",
    )
    parser.set_defaults(include_brush_models=True)
    parser.add_argument("--skip-load", action="store_true")
    parser.add_argument("--skip-variants", action="store_true")
    parser.add_argument("--access-method", choices=("rvbbit", "heap"), default="rvbbit")
    args = parser.parse_args()
    if args.rows is not None and args.rows <= 0:
        parser.error("--rows must be positive")
    if args.sample_step <= 0:
        parser.error("--sample-step must be positive")
    if args.brush_sample_step <= 0:
        parser.error("--brush-sample-step must be positive")
    pak_path = args.pak.expanduser()

    def require_pak() -> None:
        if not pak_path.exists():
            parser.error(
                f"missing PAK: {pak_path}; generation requires a Quake shareware/full pak0.pak"
            )

    if args.episode1:
        map_names = EPISODE1_MAPS
        dataset_slug = "episode1"
    elif args.maps:
        map_names = tuple(item.strip().upper() for item in args.maps.split(",") if item.strip())
        if not map_names:
            parser.error("--maps must contain at least one map name")
        dataset_slug = "_".join(name.lower() for name in map_names)
    else:
        map_names = (args.map_name.upper(),)
        dataset_slug = map_names[0].lower()
    if any(not name.replace("_", "").isalnum() for name in map_names):
        parser.error("map names must be alphanumeric")

    natural_rows = args.natural_rows or (len(map_names) > 1 and args.rows is None)
    requested_rows = args.rows if args.rows is not None else (None if natural_rows else 5_000_000)
    table = args.table or f"quakeql_{dataset_slug}"
    try:
        table = simple_identifier(table)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    args.table = table
    cardinality_slug = str(requested_rows) if requested_rows is not None else "natural"
    default_stem = f"quakeql_{dataset_slug}_{cardinality_slug}"
    parquet = (args.parquet or HERE / "data" / f"{default_stem}.parquet").expanduser()
    texture_table = args.texture_table or f"{table}_texels"
    try:
        texture_table = simple_identifier(texture_table)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    texture_parquet = (
        args.texture_parquet or parquet.with_name(f"{parquet.stem}_texels.parquet")
    ).expanduser()
    lightmap_table = args.lightmap_table or f"{table}_lightmaps"
    material_table = args.material_table or f"{table}_materials"
    lightmap_parquet = (
        args.lightmap_parquet or parquet.with_name(f"{parquet.stem}_lightmaps.parquet")
    ).expanduser()
    material_parquet = (
        args.material_parquet or parquet.with_name(f"{parquet.stem}_materials.parquet")
    ).expanduser()
    colormap_parquet = (
        args.colormap_parquet or HERE / "data" / "quakeql_colormap.parquet"
    ).expanduser()
    runtime_tables = {
        "maps": args.map_table or f"{table}_maps",
        "planes": args.plane_table or f"{table}_planes",
        "clipnodes": args.clipnode_table or f"{table}_clipnodes",
        "models": args.model_table or f"{table}_models",
        "brushes": args.brush_table or f"{table}_brushes",
    }
    runtime_parquets = {
        "maps": (
            args.map_parquet or parquet.with_name(f"{parquet.stem}_maps.parquet")
        ).expanduser(),
        "planes": (
            args.plane_parquet or parquet.with_name(f"{parquet.stem}_planes.parquet")
        ).expanduser(),
        "clipnodes": (
            args.clipnode_parquet or parquet.with_name(f"{parquet.stem}_clipnodes.parquet")
        ).expanduser(),
        "models": (
            args.model_parquet or parquet.with_name(f"{parquet.stem}_models.parquet")
        ).expanduser(),
        "brushes": (
            args.brush_parquet or parquet.with_name(f"{parquet.stem}_brushes.parquet")
        ).expanduser(),
    }
    for support_table in (
        lightmap_table,
        material_table,
        args.colormap_table,
        *runtime_tables.values(),
    ):
        try:
            simple_identifier(support_table)
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    metadata_path = parquet.with_suffix(".json")

    if args.support_only and not parquet.exists():
        parser.error("--support-only requires an existing geometry parquet")

    metadata: dict[str, object]
    if not args.support_only and (not args.reuse_parquet or not parquet.exists()):
        require_pak()
        try:
            metadata = generate_dataset(
                pak_path,
                map_names,
                parquet,
                texture_parquet,
                lightmap_parquet,
                material_parquet,
                colormap_parquet,
                runtime_parquets,
                requested_rows,
                args.sample_step,
                args.brush_sample_step,
                args.include_brush_models,
                args.row_group_size,
            )
        except (KeyError, ValueError) as exc:
            parser.error(str(exc))
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        print(
            f"Wrote {int(metadata['rows']):,} rows to {parquet} ({parquet.stat().st_size:,} bytes)"
        )
        print(
            f"Wrote {int(metadata['texture_rows']):,} mip texels to {texture_parquet} "
            f"({texture_parquet.stat().st_size:,} bytes)"
        )
    else:
        if not metadata_path.exists():
            parser.error(f"missing metadata sidecar: {metadata_path}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("format") != MAP_FORMAT:
            parser.error(
                f"{parquet} uses an older dataset format; regenerate without --reuse-parquet"
            )
        metadata_maps = tuple(
            str(name).upper()
            for name in metadata.get("map_names", [metadata.get("map_name")])
            if name
        )
        if metadata_maps != map_names:
            parser.error(
                f"{parquet} contains {','.join(metadata_maps)}, expected {','.join(map_names)}"
            )
        if bool(metadata.get("include_brush_models")) != args.include_brush_models:
            parser.error(
                f"{parquet} brush-model setting does not match; regenerate without --reuse-parquet"
            )
        if float(metadata.get("sample_step", 0)) != args.sample_step:
            parser.error(
                f"{parquet} sample step does not match; regenerate without --reuse-parquet"
            )
        if float(metadata.get("brush_sample_step", 0)) != args.brush_sample_step:
            parser.error(
                f"{parquet} brush sample step does not match; regenerate without --reuse-parquet"
            )
        expected_rows = (
            requested_rows if requested_rows is not None else int(metadata.get("base_samples", -1))
        )
        actual = duckdb.sql(
            "SELECT count(*) FROM read_parquet(?)", params=[str(parquet)]
        ).fetchone()[0]
        if actual != expected_rows:
            parser.error(f"{parquet} has {actual:,} rows, expected {expected_rows:,}")
        if not texture_parquet.exists():
            parser.error(f"missing texture parquet: {texture_parquet}")
        texture_actual = duckdb.sql(
            "SELECT count(*) FROM read_parquet(?)", params=[str(texture_parquet)]
        ).fetchone()[0]
        expected_texture_rows = int(metadata.get("texture_rows", -1))
        if texture_actual != expected_texture_rows:
            parser.error(
                f"{texture_parquet} has {texture_actual:,} rows, expected {expected_texture_rows:,}"
            )
        support_paths = (
            lightmap_parquet,
            material_parquet,
            colormap_parquet,
            *runtime_parquets.values(),
        )
        support_keys = (
            "lightmap_rows",
            "material_rows",
            "colormap_rows",
            *(f"runtime_{name}_rows" for name in runtime_parquets),
        )
        support_is_current = all(path.exists() for path in support_paths) and all(
            key in metadata for key in support_keys
        )
        if support_is_current:
            for path, key in zip(support_paths, support_keys):
                actual_rows = duckdb.sql(
                    "SELECT count(*) FROM read_parquet(?)", params=[str(path)]
                ).fetchone()[0]
                if actual_rows != int(metadata[key]):
                    support_is_current = False
                    break
        if not support_is_current:
            require_pak()
            print("Generating SQL render and runtime relations")
            try:
                metadata.update(
                    generate_render_support(
                        pak_path,
                        map_names,
                        lightmap_parquet,
                        material_parquet,
                        colormap_parquet,
                        runtime_parquets,
                    )
                )
            except (KeyError, ValueError) as exc:
                parser.error(str(exc))
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    result: dict[str, object] = {
        "world": metadata,
        "source_parquet": str(parquet),
        "source_parquet_bytes": parquet.stat().st_size,
        "texture_parquet": str(texture_parquet),
        "texture_parquet_bytes": texture_parquet.stat().st_size,
        "lightmap_parquet": str(lightmap_parquet),
        "lightmap_parquet_bytes": lightmap_parquet.stat().st_size,
        "material_parquet": str(material_parquet),
        "material_parquet_bytes": material_parquet.stat().st_size,
        "colormap_parquet": str(colormap_parquet),
        "colormap_parquet_bytes": colormap_parquet.stat().st_size,
    }
    for name, path in runtime_parquets.items():
        result[f"runtime_{name}_parquet"] = str(path)
        result[f"runtime_{name}_parquet_bytes"] = path.stat().st_size
    if not args.skip_load:
        if not args.support_only:
            print(f"Loading {table} as {args.access_method} through ordinary PostgreSQL COPY")
            result["load"] = load_rows(
                args.dsn,
                table,
                parquet,
                args.copy_batch_rows,
                not args.skip_variants,
                args.access_method,
            )
            print(
                f"Loading {texture_table} as {args.access_method} through ordinary PostgreSQL COPY"
            )
            result["texture_load"] = load_rows(
                args.dsn,
                texture_table,
                texture_parquet,
                args.copy_batch_rows,
                not args.skip_variants,
                args.access_method,
                TEXTURE_COLUMNS,
                TEXTURE_POSTGRES_COLUMNS,
            )
        support_relations = (
            (
                "lightmap_load",
                lightmap_table,
                lightmap_parquet,
                LIGHTMAP_COLUMNS,
                LIGHTMAP_POSTGRES_COLUMNS,
            ),
            (
                "material_load",
                material_table,
                material_parquet,
                MATERIAL_COLUMNS,
                MATERIAL_POSTGRES_COLUMNS,
            ),
            (
                "colormap_load",
                args.colormap_table,
                colormap_parquet,
                COLORMAP_COLUMNS,
                COLORMAP_POSTGRES_COLUMNS,
            ),
        )
        runtime_relation_specs = {
            "maps": (MAP_COLUMNS, MAP_POSTGRES_COLUMNS),
            "planes": (PLANE_COLUMNS, PLANE_POSTGRES_COLUMNS),
            "clipnodes": (CLIPNODE_COLUMNS, CLIPNODE_POSTGRES_COLUMNS),
            "models": (MODEL_COLUMNS, MODEL_POSTGRES_COLUMNS),
            "brushes": (BRUSH_COLUMNS, BRUSH_POSTGRES_COLUMNS),
        }
        support_relations += tuple(
            (
                f"runtime_{name}_load",
                runtime_tables[name],
                runtime_parquets[name],
                runtime_relation_specs[name][0],
                runtime_relation_specs[name][1],
            )
            for name in runtime_tables
        )
        for result_key, support_table, support_path, columns, postgres_columns in support_relations:
            print(
                f"Loading {support_table} as {args.access_method} through ordinary PostgreSQL COPY"
            )
            result[result_key] = load_rows(
                args.dsn,
                support_table,
                support_path,
                args.copy_batch_rows,
                not args.skip_variants,
                args.access_method,
                columns,
                postgres_columns,
            )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
