"""DuckDB TPC-H loader."""
from __future__ import annotations

import os
import sys
import time

import duckdb

sys.path.insert(0, "/bench/tpch")
from schema import duckdb_path_for_scale, duckdb_select_list, table_names  # noqa: E402


def load(data_dir: str, scale: str) -> dict:
    db_path = duckdb_path_for_scale(scale)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    con = duckdb.connect(db_path)
    t0 = time.perf_counter()
    rows = 0
    for table in table_names():
        con.execute(
            f"CREATE TABLE {table} AS "
            f"SELECT {duckdb_select_list(table)} "
            f"FROM read_parquet('{data_dir}/{table}.parquet')"
        )
        rows += con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    elapsed = time.perf_counter() - t0
    con.close()
    return {
        "rows": rows,
        "load_seconds": elapsed,
        "size_bytes": os.path.getsize(db_path),
    }
