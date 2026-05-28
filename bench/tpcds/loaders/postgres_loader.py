"""Postgres-family TPC-DS loader."""
from __future__ import annotations

import sys
import time
from typing import Iterable

import duckdb
import psycopg

sys.path.insert(0, "/bench/tpcds")
from schema import copy_columns, ddl_postgres, table_names  # noqa: E402


COPY_BATCH = 25_000


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


def _stream_rows(data_dir: str, table: str) -> Iterable[tuple]:
    con = duckdb.connect(":memory:")
    stream = con.execute(f"SELECT * FROM read_parquet('{data_dir}/{table}.parquet')")
    while True:
        batch = stream.fetchmany(COPY_BATCH)
        if not batch:
            con.close()
            return
        yield from batch


def _copy_table(cur, data_dir: str, table: str) -> int:
    cols = copy_columns(data_dir, table)
    sql = f"COPY {table} ({cols}) FROM STDIN"
    n = 0
    with cur.copy(sql) as cp:
        for row in _stream_rows(data_dir, table):
            cp.write(_format_row(row).encode() + b"\n")
            n += 1
    return n


def _measure_size(cur, data_dir: str, using: str | None) -> dict:
    names = table_names(data_dir)
    if using == "rvbbit":
        cur.execute(
            """
            SELECT
                (SELECT coalesce(sum(n_bytes), 0)::bigint
                 FROM rvbbit.row_groups
                 WHERE table_oid::regclass::text = ANY(%s))
              + (SELECT coalesce(sum(n_bytes), 0)::bigint
                 FROM rvbbit.row_group_variants
                 WHERE table_oid::regclass::text = ANY(%s))
            """,
            (names, names),
        )
        row = cur.fetchone()
        parquet_size = int(row[0]) if row else 0
        cur.execute(
            """
            SELECT coalesce(sum(n_bytes), 0)::bigint
            FROM rvbbit.row_group_variants
            WHERE table_oid::regclass::text = ANY(%s)
            """,
            (names,),
        )
        row = cur.fetchone()
        variant_size = int(row[0]) if row else 0
        heap_bytes = 0
        heap_total_bytes = 0
        for table in names:
            cur.execute(f"SELECT pg_relation_size('{table}'), pg_total_relation_size('{table}')")
            row = cur.fetchone()
            if row:
                heap_bytes += int(row[0])
                heap_total_bytes += int(row[1])
        return {
            "size_bytes": parquet_size + heap_total_bytes,
            "parquet_size_bytes": parquet_size,
            "parquet_variant_size_bytes": variant_size,
            "heap_bytes": heap_bytes,
            "heap_total_bytes": heap_total_bytes,
        }
    total = 0
    for table in names:
        cur.execute(f"SELECT pg_total_relation_size('{table}')")
        row = cur.fetchone()
        total += int(row[0]) if row else 0
    return {"size_bytes": total}


def load_pg(
    dsn: str,
    data_dir: str,
    using: str | None = None,
    pre_sql: list[str] | None = None,
    post_sql: list[str] | None = None,
) -> dict:
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            for s in pre_sql or []:
                cur.execute(s)
            for table in reversed(table_names(data_dir)):
                cur.execute(f"DROP TABLE IF EXISTS {table}")
            for table in table_names(data_dir):
                cur.execute(ddl_postgres(data_dir, table, using=using))

            t0 = time.perf_counter()
            rows = 0
            for table in table_names(data_dir):
                print(f"    copy {table}")
                rows += _copy_table(cur, data_dir, table)
            copy_elapsed = time.perf_counter() - t0

            for s in post_sql or []:
                cur.execute(s)
            elapsed = time.perf_counter() - t0
            size_info = _measure_size(cur, data_dir, using)
            return {
                "rows": rows,
                "load_seconds": elapsed,
                "copy_seconds": copy_elapsed,
                **size_info,
            }

