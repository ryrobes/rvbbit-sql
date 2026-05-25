"""Generic PG-flavored loader — works for plain Postgres, Citus, Hydra,
AlloyDB, and rvbbit (with a `using=...` override).

Strategy:
  1. Reads parquet via DuckDB in the bench container.
  2. Pipes rows through psycopg COPY (text mode) into the target.
  3. Optional `pre_sql` runs DDL specific to the target's columnar engine
     (e.g. SELECT alter_table_set_access_method('trips', 'columnar')
     for Citus, ALTER TABLE trips SET ACCESS METHOD columnar for Hydra).

No intermediate CSV file — streaming keeps disk pressure low and lets us
tune row batching from one place.
"""
from __future__ import annotations

import glob
import os
import time
from typing import Iterable

import duckdb
import psycopg

import sys
sys.path.insert(0, "/bench/columnar_comparison")
from schema import COLUMNS, PARQUET_TO_OURS, ddl_postgres  # noqa: E402


COPY_BATCH = 50_000


def _open_parquet_stream(data_dir: str):
    """Returns a DuckDB cursor over the renamed/casted columns.

    Parquet stores naturally-integer fields (passenger_count, RatecodeID)
    as DOUBLE; we cast to our target SMALLINT here so Postgres COPY
    doesn't choke on '1.0'."""
    parquet_glob = os.path.join(data_dir, "yellow_tripdata_*.parquet")
    files = sorted(glob.glob(parquet_glob))
    if not files:
        raise FileNotFoundError(f"no parquet files in {data_dir}")
    duck_type = {
        "smallint": "SMALLINT",
        "double precision": "DOUBLE",
        "text": "VARCHAR",
        "timestamp": "TIMESTAMP",
    }
    # Build SELECT that renames + casts each col.
    pg_type_by_name = {name: pg for name, _, pg in COLUMNS}
    parts = []
    for src, dst in PARQUET_TO_OURS.items():
        cast_to = duck_type[pg_type_by_name[dst]]
        parts.append(f'CAST("{src}" AS {cast_to}) AS {dst}')
    sel_cols = ", ".join(parts)
    con = duckdb.connect(":memory:")
    return con.execute(f"SELECT {sel_cols} FROM read_parquet('{parquet_glob}')")


def _format_row(row: tuple) -> str:
    """Tab-separated text-format COPY row. Nulls as \\N. Quote nothing
    here — PG text COPY interprets tabs as separators, newlines as
    row separators, backslash sequences are interpreted."""
    parts = []
    for v in row:
        if v is None:
            parts.append(r"\N")
        else:
            s = str(v)
            # Minimal escaping for PG COPY text format.
            s = s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
            parts.append(s)
    return "\t".join(parts)


def _stream_into(cur, table: str, rows: Iterable[tuple]) -> int:
    """Push rows through COPY ... FROM STDIN in chunks. Returns row count."""
    col_list = ", ".join(name for name, _, _ in COLUMNS)
    sql = f"COPY {table} ({col_list}) FROM STDIN"
    n = 0
    with cur.copy(sql) as cp:
        for row in rows:
            cp.write(_format_row(row).encode() + b"\n")
            n += 1
    return n


def load_pg(
    dsn: str,
    data_dir: str,
    table: str = "trips",
    using: str | None = None,
    pre_sql: list[str] | None = None,
    post_sql: list[str] | None = None,
    extra_ddl: list[str] | None = None,
) -> dict:
    """Load `data_dir`/yellow_tripdata_*.parquet into `dsn`."`using` adds
    a USING clause to CREATE TABLE (e.g. 'rvbbit'). `pre_sql` runs
    before DDL (extension installs); `post_sql` after data load
    (alter-access-method, analyze)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            for s in pre_sql or []:
                cur.execute(s)
            cur.execute(f"DROP TABLE IF EXISTS {table}")
            cur.execute(ddl_postgres(table=table, using=using))
            for s in extra_ddl or []:
                cur.execute(s)

            stream = _open_parquet_stream(data_dir)

            def row_iter():
                while True:
                    batch = stream.fetchmany(COPY_BATCH)
                    if not batch:
                        return
                    for r in batch:
                        yield r

            t0 = time.perf_counter()
            n = _stream_into(cur, table, row_iter())
            elapsed = time.perf_counter() - t0

            for s in post_sql or []:
                cur.execute(s)

            size_b = _measure_size(cur, table, using)
            return {"rows": n, "load_seconds": elapsed, "size_bytes": size_b}


def _measure_size(cur, table: str, using: str | None) -> int | None:
    # Rvbbit's columnar bytes live in parquet files outside PG's relation
    # storage, so pg_total_relation_size only sees the heap residue.
    # Read the registered size from rvbbit.row_groups instead.
    if using == "rvbbit":
        cur.execute(
            "SELECT coalesce(sum(n_bytes), 0)::bigint FROM rvbbit.row_groups "
            "WHERE table_oid = %s::regclass::oid",
            (table,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else None
    cur.execute(f"SELECT pg_total_relation_size('{table}')")
    row = cur.fetchone()
    return int(row[0]) if row else None
