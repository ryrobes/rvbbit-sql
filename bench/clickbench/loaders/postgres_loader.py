"""PG-flavored loader for hits.parquet — same shape as taxi loader.

Streams DuckDB rows through psycopg COPY. The DuckDB SELECT preserves
the upstream column order; PG-side DDL declares matching quoted names.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Iterable

import duckdb
import psycopg

import sys
sys.path.insert(0, "/bench/clickbench")
from schema import COLUMNS, ddl_postgres, duckdb_select_list  # noqa: E402


COPY_BATCH = 25_000
DIRECT_ACCEL_DIR = Path(os.environ.get("RVBBIT_DIRECT_ACCEL_DIR", "/rvbbit_import"))
CLICKBENCH_EPOCH_SECOND_COLUMNS = "EventTime,ClientEventTime,LocalEventTime"


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


def _write_direct_accel_chunks(
    data_path: str, limit: int | None, table: str
) -> tuple[list[str], float, Path | None]:
    chunk_rows = _env_int("RVBBIT_DIRECT_ACCEL_CHUNK_ROWS", _env_int("RVBBIT_COMPACT_SCAN_CHUNK_ROWS", 250_000))
    staging_mode = _direct_accel_staging_mode()
    if staging_mode in {"source", "raw_source", "original", "none"}:
        return [data_path], 0.0, None

    run_dir = DIRECT_ACCEL_DIR / f"{table}-{os.getpid()}-{int(time.time() * 1000)}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    data_path_sql = _sql_literal(data_path)
    source_sql = f"SELECT {duckdb_select_list()} FROM read_parquet({data_path_sql})"
    bounded_source_sql = f"SELECT * FROM ({source_sql}) AS rvbbit_source"
    if limit:
        bounded_source_sql = f"{bounded_source_sql} LIMIT {int(limit)}"

    t0 = time.perf_counter()
    con = duckdb.connect(":memory:")
    try:
        if staging_mode not in {"offset_chunks", "legacy_offset", "offset"}:
            chunk_path = run_dir / "source.parquet"
            con.execute(
                "COPY (" + bounded_source_sql + ") TO "
                + _sql_literal(str(chunk_path))
                + f" (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {chunk_rows})"
            )
            return [str(chunk_path)], time.perf_counter() - t0, run_dir

        if limit:
            total_rows = int(limit)
        else:
            total_rows = con.execute(
                f"SELECT count(*) FROM read_parquet({data_path_sql})"
            ).fetchone()[0]
        paths: list[str] = []
        for offset in range(0, total_rows, chunk_rows):
            chunk_path = run_dir / f"chunk-{offset // chunk_rows:05d}.parquet"
            chunk_sql = (
                f"SELECT * FROM ({bounded_source_sql}) AS rvbbit_chunk_source "
                f"LIMIT {chunk_rows} OFFSET {offset}"
            )
            con.execute(
                "COPY (" + chunk_sql + ") TO "
                + _sql_literal(str(chunk_path))
                + " (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            paths.append(str(chunk_path))
    finally:
        con.close()
    return paths, time.perf_counter() - t0, run_dir


def load_pg(
    dsn: str,
    data_path: str,
    limit: int | None = None,
    table: str = "hits",
    using: str | None = None,
    pre_sql: list[str] | None = None,
    post_sql: list[str] | None = None,
    final_sql: list[str] | None = None,
    direct_accel: bool = False,
    direct_accel_refresh_variants: bool = True,
) -> dict:
    direct_chunk_paths: list[str] = []
    direct_chunk_seconds: float | None = None
    direct_import_seconds: float | None = None
    direct_cleanup_dir: Path | None = None
    direct_import_doc = None
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
            if direct_accel:
                staging_mode = _direct_accel_staging_mode()
                if staging_mode in {"source", "raw_source", "original", "none"}:
                    epoch_columns = os.environ.get(
                        "RVBBIT_IMPORT_EPOCH_SECONDS_COLUMNS",
                        CLICKBENCH_EPOCH_SECOND_COLUMNS,
                    )
                    if epoch_columns.strip():
                        cur.execute(
                            "SELECT set_config('rvbbit.import_epoch_seconds_columns', %s, false)",
                            (epoch_columns,),
                        )
                    if limit:
                        cur.execute(
                            "SELECT set_config('rvbbit.import_row_limit', %s, false)",
                            (str(int(limit)),),
                        )
                direct_chunk_paths, direct_chunk_seconds, direct_cleanup_dir = _write_direct_accel_chunks(
                    data_path, limit, table
                )
                import_t0 = time.perf_counter()
                cur.execute(
                    "SELECT rvbbit.import_canonical_parquet_chunks(%s::regclass, %s::text[], %s)",
                    (table, direct_chunk_paths, direct_accel_refresh_variants),
                )
                direct_import_doc = cur.fetchone()[0]
                direct_import_seconds = time.perf_counter() - import_t0
                if direct_cleanup_dir is not None and not _env_enabled("RVBBIT_DIRECT_ACCEL_KEEP_CHUNKS"):
                    shutil.rmtree(direct_cleanup_dir, ignore_errors=True)
            for s in final_sql or []:
                cur.execute(s)
            elapsed = time.perf_counter() - t0

            size_info = _measure_size(cur, table, using)
            result = {
                "rows": n,
                "load_seconds": elapsed,
                "copy_seconds": copy_elapsed,
                **size_info,
            }
            if direct_accel:
                result.update(
                    {
                        "direct_accel": True,
                        "direct_accel_staging_mode": _direct_accel_staging_mode() or "single_pass",
                        "direct_accel_source_files": len(direct_chunk_paths),
                        "direct_accel_chunk_seconds": direct_chunk_seconds,
                        "direct_accel_import_seconds": direct_import_seconds,
                        "direct_accel_doc": direct_import_doc,
                    }
                )
            return result


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
