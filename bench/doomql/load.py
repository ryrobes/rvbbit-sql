#!/usr/bin/env python3
"""Generate and load a synthetic or WAD-derived DoomQL observation dataset."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import duckdb
import psycopg

try:
    from .wad_world import (
        DEFAULT_GRID_SCALE,
        EPISODE_MAPS,
        EPISODE_SURFACE_COLUMNS,
        SURFACE_COLUMNS,
        create_episode_parquet,
        create_wad_parquet,
    )
    from .workload import TABLE_COLUMNS, create_parquet
except ImportError:
    from wad_world import (
        DEFAULT_GRID_SCALE,
        EPISODE_MAPS,
        EPISODE_SURFACE_COLUMNS,
        SURFACE_COLUMNS,
        create_episode_parquet,
        create_wad_parquet,
    )
    from workload import TABLE_COLUMNS, create_parquet


HERE = Path(__file__).resolve().parent
DEFAULT_DSN = os.environ.get(
    "RVBBIT_DSN",
    "postgresql://postgres:rvbbit@localhost:55433/bench",
)
DEFAULT_WAD = Path(
    os.environ.get("DOOMQL_WAD", "~/repos2026/diffoom/assets/DOOM1.WAD")
).expanduser()
WORLD_COLUMNS = {
    "synthetic": TABLE_COLUMNS,
    "e1m1": SURFACE_COLUMNS,
    "episode1": EPISODE_SURFACE_COLUMNS,
}
WORLD_POSTGRES_COLUMNS = {
    "synthetic": """
        sample_id bigint,
        scan_id integer,
        world_x smallint,
        world_y smallint,
        world_z smallint,
        solid smallint,
        material smallint,
        light smallint
    """,
    "e1m1": """
        sample_id bigint,
        scan_id integer,
        surface_id integer,
        world_x smallint,
        world_y smallint,
        z_bottom smallint,
        z_top smallint,
        surface_kind smallint,
        material smallint,
        light smallint,
        sector_id smallint
    """,
    "episode1": """
        sample_id bigint,
        scan_id integer,
        map_name text,
        surface_id integer,
        world_x smallint,
        world_y smallint,
        z_bottom smallint,
        z_top smallint,
        surface_kind smallint,
        material smallint,
        light smallint,
        sector_id smallint,
        linedef_id smallint,
        texture_u integer,
        texture_v integer,
        face_light smallint,
        door_id smallint
    """,
}


def simple_identifier(value: str) -> str:
    if not value or not value.replace("_", "a").isalnum() or value[0].isdigit():
        raise argparse.ArgumentTypeError("table must be an unqualified SQL identifier")
    return value


def load_rows(
    dsn: str,
    table: str,
    parquet: Path,
    batch_rows: int,
    refresh_variants: bool,
    world: str = "synthetic",
) -> dict[str, object]:
    columns = ", ".join(WORLD_COLUMNS[world])
    ddl = f"""
CREATE TABLE {table} (
    {WORLD_POSTGRES_COLUMNS[world]}
) USING rvbbit
"""
    source = duckdb.connect(":memory:")
    source.execute(f"SELECT {columns} FROM read_parquet(?)", [str(parquet)])
    copied = 0
    next_progress = 1_000_000
    copy_started = time.perf_counter()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(ddl)
        with conn.cursor().copy(f"COPY {table} ({columns}) FROM STDIN") as copy:
            while True:
                batch = source.fetchmany(batch_rows)
                if not batch:
                    break
                for row in batch:
                    copy.write_row(row)
                copied += len(batch)
                if copied >= next_progress:
                    print(f"  copied {copied:,} rows", flush=True)
                    while next_progress <= copied:
                        next_progress += 1_000_000
        copy_seconds = time.perf_counter() - copy_started
        conn.execute(f"ANALYZE {table}")
        compact_started = time.perf_counter()
        result = conn.execute(
            "SELECT rvbbit.refresh_acceleration(%s::regclass, false)",
            (table,),
        ).fetchone()[0]
        compact_seconds = time.perf_counter() - compact_started
        variant_rows = None
        variant_seconds = None
        if refresh_variants:
            variant_started = time.perf_counter()
            variant_rows = conn.execute(
                "SELECT rvbbit.refresh_layout_variants(%s::regclass)",
                (table,),
            ).fetchone()[0]
            variant_seconds = time.perf_counter() - variant_started
        status = conn.execute(
            """
            SELECT coalesce(sum(n_rows), 0)::bigint,
                   coalesce(sum(n_bytes), 0)::bigint,
                   count(*)::integer
            FROM rvbbit.row_groups_visible
            WHERE table_oid = %s::regclass
            """,
            (table,),
        ).fetchone()
    source.close()
    return {
        "rows": copied,
        "copy_seconds": round(copy_seconds, 3),
        "compact_seconds": round(compact_seconds, 3),
        "variant_seconds": round(variant_seconds, 3) if variant_seconds is not None else None,
        "variant_rows": variant_rows,
        "parquet_rows": status[0],
        "parquet_bytes": status[1],
        "row_groups": status[2],
        "refresh": result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--table", type=simple_identifier, default="doomql_world")
    parser.add_argument("--world", choices=sorted(WORLD_COLUMNS), default="synthetic")
    parser.add_argument("--rows", type=int, default=5_000_000)
    parser.add_argument("--row-group-size", type=int, default=1_000_000)
    parser.add_argument("--copy-batch-rows", type=int, default=25_000)
    parser.add_argument("--parquet", type=Path)
    parser.add_argument("--wad", type=Path, default=DEFAULT_WAD)
    parser.add_argument("--map-name", default="E1M1")
    parser.add_argument("--maps", default=",".join(EPISODE_MAPS))
    parser.add_argument("--grid-scale", type=int, default=DEFAULT_GRID_SCALE)
    parser.add_argument("--reuse-parquet", action="store_true")
    parser.add_argument("--skip-variants", action="store_true")
    args = parser.parse_args()
    if args.rows <= 0:
        parser.error("--rows must be positive")

    if args.world == "synthetic":
        default_name = "doomql_world"
    elif args.world == "episode1":
        default_name = "doomql_episode1"
    else:
        default_name = f"doomql_{args.map_name.lower()}"
    parquet = args.parquet or HERE / "data" / f"{default_name}_{args.rows}.parquet"
    if not args.reuse_parquet or not parquet.exists():
        print(f"Generating {args.rows:,} {args.world} observations -> {parquet}")
        started = time.perf_counter()
        if args.world == "episode1":
            map_names = tuple(
                name.strip().upper() for name in args.maps.split(",") if name.strip()
            )
            metadata = create_episode_parquet(
                parquet,
                args.wad.expanduser(),
                map_names,
                args.rows,
                args.row_group_size,
                args.grid_scale,
            )
            print(json.dumps(metadata, indent=2))
        elif args.world == "e1m1":
            metadata = create_wad_parquet(
                parquet,
                args.wad.expanduser(),
                args.map_name,
                args.rows,
                args.row_group_size,
                args.grid_scale,
            )
            print(json.dumps(metadata, indent=2))
        else:
            create_parquet(parquet, args.rows, args.row_group_size)
        print(f"  generated in {time.perf_counter() - started:.2f}s ({parquet.stat().st_size:,} bytes)")
    else:
        actual_rows = duckdb.sql("SELECT count(*) FROM read_parquet(?)", params=[str(parquet)]).fetchone()[0]
        if actual_rows != args.rows:
            parser.error(f"{parquet} has {actual_rows:,} rows, expected {args.rows:,}")
        actual_columns = tuple(
            row[0]
            for row in duckdb.sql(
                "DESCRIBE SELECT * FROM read_parquet(?)",
                params=[str(parquet)],
            ).fetchall()
        )
        if actual_columns != WORLD_COLUMNS[args.world]:
            parser.error(
                f"{parquet} has columns for a different world: {', '.join(actual_columns)}"
            )

    print(f"Loading {args.table} through ordinary PostgreSQL COPY")
    result = load_rows(
        args.dsn,
        args.table,
        parquet,
        args.copy_batch_rows,
        not args.skip_variants,
        args.world,
    )
    result["world"] = args.world
    result["source_parquet"] = str(parquet)
    result["source_parquet_bytes"] = parquet.stat().st_size
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
