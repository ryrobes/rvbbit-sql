#!/usr/bin/env python3
"""Load DoomQL source Parquet into vanilla PostgreSQL and ClickHouse."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import clickhouse_connect
import duckdb
import psycopg

try:
    from .load import WORLD_COLUMNS, WORLD_POSTGRES_COLUMNS, simple_identifier
except ImportError:
    from load import WORLD_COLUMNS, WORLD_POSTGRES_COLUMNS, simple_identifier


HERE = Path(__file__).resolve().parent
DEFAULT_POSTGRES_DSN = os.environ.get(
    "DOOMQL_POSTGRES_DSN",
    "postgresql://postgres:bench@localhost:5440/bench",
)
DEFAULT_CLICKHOUSE_HOST = os.environ.get("DOOMQL_CLICKHOUSE_HOST", "localhost")
DEFAULT_CLICKHOUSE_PORT = int(os.environ.get("DOOMQL_CLICKHOUSE_PORT", "8123"))


def load_postgres(
    dsn: str,
    table: str,
    parquet: Path,
    batch_rows: int,
    world: str = "synthetic",
) -> dict[str, object]:
    columns = ", ".join(WORLD_COLUMNS[world])
    ddl = f"""
CREATE TABLE {table} (
    {WORLD_POSTGRES_COLUMNS[world]}
)
"""
    source = duckdb.connect(":memory:")
    source.execute(f"SELECT {columns} FROM read_parquet(?)", [str(parquet)])
    copied = 0
    next_progress = 1_000_000
    started = time.perf_counter()
    try:
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
                        print(f"  postgres copied {copied:,} rows", flush=True)
                        while next_progress <= copied:
                            next_progress += 1_000_000
            conn.execute(f"ANALYZE {table}")
            size_bytes = conn.execute(
                "SELECT pg_total_relation_size(%s::regclass)::bigint",
                (table,),
            ).fetchone()[0]
    finally:
        source.close()
    return {
        "system": "postgres",
        "rows": copied,
        "load_seconds": round(time.perf_counter() - started, 3),
        "size_bytes": size_bytes,
    }


def load_clickhouse(
    host: str,
    port: int,
    table: str,
    parquet: Path,
    world: str = "synthetic",
) -> dict[str, object]:
    client = clickhouse_connect.get_client(host=host, port=port)
    client.command(f"DROP TABLE IF EXISTS {table}")
    clickhouse_columns = {
        "synthetic": """
            CREATE TABLE {table} (
                sample_id Int64,
                scan_id Int32,
                world_x Int16,
                world_y Int16,
                world_z Int16,
                solid Int16,
                material Int16,
                light Int16
            ) ENGINE = MergeTree ORDER BY sample_id
        """,
        "e1m1": """
            CREATE TABLE {table} (
                sample_id Int64,
                scan_id Int32,
                surface_id Int32,
                world_x Int16,
                world_y Int16,
                z_bottom Int16,
                z_top Int16,
                surface_kind Int16,
                material Int16,
                light Int16,
                sector_id Int16
            ) ENGINE = MergeTree ORDER BY sample_id
        """,
    }
    client.command(clickhouse_columns[world].format(table=table))
    started = time.perf_counter()
    with parquet.open("rb") as source:
        client.raw_insert(table, insert_block=source, fmt="Parquet")
    client.command(f"OPTIMIZE TABLE {table} FINAL")
    load_seconds = time.perf_counter() - started
    rows = client.query(f"SELECT count(*) FROM {table}").result_rows[0][0]
    size_bytes = client.query(
        "SELECT coalesce(sum(bytes_on_disk), 0) FROM system.parts "
        f"WHERE active AND database = currentDatabase() AND table = '{table}'"
    ).result_rows[0][0]
    return {
        "system": "clickhouse",
        "rows": int(rows),
        "load_seconds": round(load_seconds, 3),
        "size_bytes": int(size_bytes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=Path, required=True)
    parser.add_argument("--table", type=simple_identifier, default="doomql_world")
    parser.add_argument("--world", choices=sorted(WORLD_COLUMNS), default="synthetic")
    parser.add_argument("--targets", default="postgres,clickhouse")
    parser.add_argument("--postgres-dsn", default=DEFAULT_POSTGRES_DSN)
    parser.add_argument("--clickhouse-host", default=DEFAULT_CLICKHOUSE_HOST)
    parser.add_argument("--clickhouse-port", type=int, default=DEFAULT_CLICKHOUSE_PORT)
    parser.add_argument("--copy-batch-rows", type=int, default=100_000)
    parser.add_argument("--output", type=Path, default=HERE / "results" / "competitor-load.json")
    args = parser.parse_args()
    if not args.parquet.exists():
        parser.error(f"missing source Parquet: {args.parquet}")
    actual_columns = tuple(
        row[0]
        for row in duckdb.sql(
            "DESCRIBE SELECT * FROM read_parquet(?)",
            params=[str(args.parquet)],
        ).fetchall()
    )
    if actual_columns != WORLD_COLUMNS[args.world]:
        parser.error(
            f"{args.parquet} has columns for a different world: "
            f"{', '.join(actual_columns)}"
        )
    targets = [target.strip() for target in args.targets.split(",") if target.strip()]
    unknown = sorted(set(targets) - {"postgres", "clickhouse"})
    if unknown:
        parser.error(f"unknown targets: {', '.join(unknown)}")

    results: list[dict[str, object]] = []
    for target in targets:
        print(f"Loading {target} from {args.parquet}", flush=True)
        if target == "postgres":
            result = load_postgres(
                args.postgres_dsn,
                args.table,
                args.parquet,
                args.copy_batch_rows,
                args.world,
            )
        else:
            result = load_clickhouse(
                args.clickhouse_host,
                args.clickhouse_port,
                args.table,
                args.parquet,
                args.world,
            )
        results.append(result)
        print(json.dumps(result, indent=2))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "source_parquet": str(args.parquet),
                "source_parquet_bytes": args.parquet.stat().st_size,
                "world": args.world,
                "results": results,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
