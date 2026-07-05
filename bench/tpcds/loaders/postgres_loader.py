"""Postgres-family TPC-DS loader."""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import Iterable

import duckdb
import psycopg

sys.path.insert(0, "/bench/tpcds")
from schema import copy_columns, ddl_postgres, duckdb_select_list, table_names  # noqa: E402


COPY_BATCH = 25_000
DIRECT_ACCEL_DIR = Path(os.environ.get("RVBBIT_DIRECT_ACCEL_DIR", "/rvbbit_import"))


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _direct_accel_staging_mode() -> str:
    return os.environ.get("RVBBIT_DIRECT_ACCEL_STAGING_MODE", "single_pass").strip().lower()


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
    stream = con.execute(
        f"SELECT {duckdb_select_list(data_dir, table)} "
        f"FROM read_parquet({_sql_literal(f'{data_dir}/{table}.parquet')})"
    )
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


def _write_direct_accel_chunks(data_dir: str, table: str) -> tuple[list[str], float, Path | None]:
    data_path = f"{data_dir}/{table}.parquet"
    staging_mode = _direct_accel_staging_mode()
    if staging_mode in {"source", "raw_source", "original", "none"}:
        return [data_path], 0.0, None

    chunk_rows = _env_int(
        "RVBBIT_DIRECT_ACCEL_CHUNK_ROWS",
        _env_int("RVBBIT_COMPACT_SCAN_CHUNK_ROWS", 250_000),
    )
    run_dir = DIRECT_ACCEL_DIR / f"tpcds-{table}-{os.getpid()}-{int(time.time() * 1000)}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    source_sql = (
        f"SELECT {duckdb_select_list(data_dir, table)} "
        f"FROM read_parquet({_sql_literal(data_path)})"
    )
    t0 = time.perf_counter()
    con = duckdb.connect(":memory:")
    try:
        if staging_mode not in {"offset_chunks", "legacy_offset", "offset"}:
            chunk_path = run_dir / f"{table}.parquet"
            con.execute(
                "COPY ("
                + source_sql
                + ") TO "
                + _sql_literal(str(chunk_path))
                + f" (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {chunk_rows})"
            )
            return [str(chunk_path)], time.perf_counter() - t0, run_dir

        total_rows = con.execute(
            f"SELECT count(*) FROM read_parquet({_sql_literal(data_path)})"
        ).fetchone()[0]
        paths: list[str] = []
        for offset in range(0, total_rows, chunk_rows):
            chunk_path = run_dir / f"{table}-{offset // chunk_rows:05d}.parquet"
            chunk_sql = (
                f"SELECT * FROM ({source_sql}) AS rvbbit_chunk_source "
                f"LIMIT {chunk_rows} OFFSET {offset}"
            )
            con.execute(
                "COPY ("
                + chunk_sql
                + ") TO "
                + _sql_literal(str(chunk_path))
                + " (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            paths.append(str(chunk_path))
        return paths, time.perf_counter() - t0, run_dir
    finally:
        con.close()


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
    final_sql: list[str] | None = None,
    direct_accel: bool = False,
    direct_accel_refresh_variants: bool = True,
) -> dict:
    direct_tables: list[dict] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            # TPC-DS lives in its own schema: TPC-H and TPC-DS both define a
            # `customer` table (with different columns), so loading into public
            # would clobber TPC-H's copy in the shared bench DB. search_path is
            # set to ONLY tpcds so every unqualified DDL/COPY/ANALYZE — and the
            # rvbbit refresh/hot_load statements passed in via post_sql —
            # resolves strictly inside the tpcds schema.
            cur.execute("CREATE SCHEMA IF NOT EXISTS tpcds")
            cur.execute("SET search_path = tpcds")
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
            if direct_accel:
                for table in table_names(data_dir):
                    chunk_paths, chunk_seconds, cleanup_dir = _write_direct_accel_chunks(
                        data_dir, table
                    )
                    import_t0 = time.perf_counter()
                    cur.execute(
                        "SELECT rvbbit.import_canonical_parquet_chunks(%s::regclass, %s::text[], %s)",
                        (table, chunk_paths, direct_accel_refresh_variants),
                    )
                    direct_doc = cur.fetchone()[0]
                    import_seconds = time.perf_counter() - import_t0
                    if cleanup_dir is not None and not _env_enabled(
                        "RVBBIT_DIRECT_ACCEL_KEEP_CHUNKS"
                    ):
                        shutil.rmtree(cleanup_dir, ignore_errors=True)
                    direct_tables.append(
                        {
                            "table": table,
                            "source_files": len(chunk_paths),
                            "chunk_seconds": chunk_seconds,
                            "import_seconds": import_seconds,
                            "doc": direct_doc,
                        }
                    )
            for s in final_sql or []:
                cur.execute(s)
            elapsed = time.perf_counter() - t0
            size_info = _measure_size(cur, data_dir, using)
            result = {
                "rows": rows,
                "load_seconds": elapsed,
                "copy_seconds": copy_elapsed,
                **size_info,
            }
            if direct_accel:
                result.update(
                    {
                        "direct_accel": True,
                        "direct_accel_staging_mode": _direct_accel_staging_mode() or "single_pass",
                        "direct_accel_tables": direct_tables,
                        "direct_accel_source_files": sum(t["source_files"] for t in direct_tables),
                        "direct_accel_chunk_seconds": sum(
                            float(t["chunk_seconds"] or 0.0) for t in direct_tables
                        ),
                        "direct_accel_import_seconds": sum(
                            float(t["import_seconds"] or 0.0) for t in direct_tables
                        ),
                    }
                )
            return result
