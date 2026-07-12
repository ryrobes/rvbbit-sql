#!/usr/bin/env python3
"""Generate and load the DoomQL voxel-observation dataset."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import duckdb
import psycopg

try:
    from .workload import TABLE_COLUMNS, create_parquet
except ImportError:
    from workload import TABLE_COLUMNS, create_parquet


HERE = Path(__file__).resolve().parent
DEFAULT_DSN = os.environ.get(
    "RVBBIT_DSN",
    "postgresql://postgres:rvbbit@localhost:55433/bench",
)


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
) -> dict[str, object]:
    columns = ", ".join(TABLE_COLUMNS)
    ddl = f"""
CREATE TABLE {table} (
    sample_id bigint,
    scan_id integer,
    world_x smallint,
    world_y smallint,
    world_z smallint,
    solid smallint,
    material smallint,
    light smallint
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
    parser.add_argument("--rows", type=int, default=5_000_000)
    parser.add_argument("--row-group-size", type=int, default=1_000_000)
    parser.add_argument("--copy-batch-rows", type=int, default=25_000)
    parser.add_argument("--parquet", type=Path)
    parser.add_argument("--reuse-parquet", action="store_true")
    parser.add_argument("--skip-variants", action="store_true")
    args = parser.parse_args()
    if args.rows <= 0:
        parser.error("--rows must be positive")

    parquet = args.parquet or HERE / "data" / f"doomql_world_{args.rows}.parquet"
    if not args.reuse_parquet or not parquet.exists():
        print(f"Generating {args.rows:,} deterministic voxel observations -> {parquet}")
        started = time.perf_counter()
        create_parquet(parquet, args.rows, args.row_group_size)
        print(f"  generated in {time.perf_counter() - started:.2f}s ({parquet.stat().st_size:,} bytes)")
    else:
        actual_rows = duckdb.sql("SELECT count(*) FROM read_parquet(?)", params=[str(parquet)]).fetchone()[0]
        if actual_rows != args.rows:
            parser.error(f"{parquet} has {actual_rows:,} rows, expected {args.rows:,}")

    print(f"Loading {args.table} through ordinary PostgreSQL COPY")
    result = load_rows(
        args.dsn,
        args.table,
        parquet,
        args.copy_batch_rows,
        not args.skip_variants,
    )
    result["source_parquet"] = str(parquet)
    result["source_parquet_bytes"] = parquet.stat().st_size
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
