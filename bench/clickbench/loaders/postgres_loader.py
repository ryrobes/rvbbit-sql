"""PG-flavored loader for hits.parquet — same shape as taxi loader.

Streams DuckDB rows through psycopg COPY. The DuckDB SELECT preserves
the upstream column order; PG-side DDL declares matching quoted names.
"""
from __future__ import annotations

import os
import time
from typing import Iterable

import duckdb
import psycopg

import sys
sys.path.insert(0, "/bench/clickbench")
from schema import COLUMNS, ddl_postgres, duckdb_select_list  # noqa: E402


COPY_BATCH = 25_000


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _open_stream(data_path: str, limit: int | None):
    con = duckdb.connect(":memory:")
    # Schema's duckdb_select_list applies epoch→timestamp/date casts on
    # the BIGINT/USMALLINT-stored time columns so PG COPY sees real
    # timestamps and dates.
    where_limit = f" LIMIT {limit}" if limit else ""
    sql = f"SELECT {duckdb_select_list()} FROM read_parquet('{data_path}'){where_limit}"
    return con.execute(sql)


def _format_row(row: tuple) -> str:
    parts = []
    for v in row:
        if v is None:
            parts.append(r"\N")
        else:
            s = str(v)
            s = (
                s.replace("\\", "\\\\")
                .replace("\t", "\\t")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
            )
            parts.append(s)
    return "\t".join(parts)


def _stream_into(cur, table: str, rows: Iterable[tuple]) -> int:
    col_list = ", ".join(f'"{n}"' for n, _ in COLUMNS)
    sql = f"COPY {table} ({col_list}) FROM STDIN"
    n = 0
    with cur.copy(sql) as cp:
        for row in rows:
            cp.write(_format_row(row).encode() + b"\n")
            n += 1
    return n


def load_pg(
    dsn: str,
    data_path: str,
    limit: int | None = None,
    table: str = "hits",
    using: str | None = None,
    pre_sql: list[str] | None = None,
    post_sql: list[str] | None = None,
) -> dict:
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            for s in pre_sql or []:
                cur.execute(s)
            cur.execute(f"DROP TABLE IF EXISTS {table}")
            cur.execute(ddl_postgres(table=table, using=using))
            stream = _open_stream(data_path, limit)

            def row_iter():
                while True:
                    batch = stream.fetchmany(COPY_BATCH)
                    if not batch:
                        return
                    for r in batch:
                        yield r

            t0 = time.perf_counter()
            n = _stream_into(cur, table, row_iter())
            copy_elapsed = time.perf_counter() - t0

            for s in post_sql or []:
                cur.execute(s)
            elapsed = time.perf_counter() - t0

            size_info = _measure_size(cur, table, using)
            return {
                "rows": n,
                "load_seconds": elapsed,
                "copy_seconds": copy_elapsed,
                **size_info,
            }


def _measure_size(cur, table: str, using: str | None) -> dict:
    # Rvbbit's columnar bytes live in parquet files outside PG's relation
    # storage, so pg_total_relation_size only sees the heap residue.
    # Read the registered size from rvbbit.row_groups instead.
    if using == "rvbbit":
        cur.execute(
            """
            SELECT
                (SELECT coalesce(sum(n_bytes), 0)::bigint
                 FROM rvbbit.row_groups
                 WHERE table_oid = %s::regclass::oid)
              + (SELECT coalesce(sum(n_bytes), 0)::bigint
                 FROM rvbbit.row_group_variants
                 WHERE table_oid = %s::regclass::oid)
            """,
            (table, table),
        )
        row = cur.fetchone()
        parquet_size = int(row[0]) if row else 0
        cur.execute(
            """
            SELECT coalesce(sum(n_bytes), 0)::bigint
            FROM rvbbit.row_group_variants
            WHERE table_oid = %s::regclass::oid
            """,
            (table,),
        )
        row = cur.fetchone()
        variant_size = int(row[0]) if row else 0
        cur.execute(f"SELECT pg_relation_size('{table}'), pg_total_relation_size('{table}')")
        row = cur.fetchone()
        heap_bytes = int(row[0]) if row else 0
        heap_total_bytes = int(row[1]) if row else 0
        return {
            "size_bytes": parquet_size + heap_total_bytes,
            "parquet_size_bytes": parquet_size,
            "parquet_variant_size_bytes": variant_size,
            "heap_bytes": heap_bytes,
            "heap_total_bytes": heap_total_bytes,
        }
    cur.execute(f"SELECT pg_total_relation_size('{table}')")
    row = cur.fetchone()
    return {"size_bytes": int(row[0]) if row else None}
