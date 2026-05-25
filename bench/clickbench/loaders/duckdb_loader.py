"""DuckDB loader — native parquet, lands in /data/hits_duckdb.db."""
from __future__ import annotations

import os
import time

import duckdb

DB_PATH = "/data/hits_duckdb.db"


def load(data_path: str, limit: int | None = None) -> dict:
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = duckdb.connect(DB_PATH)
    t0 = time.perf_counter()
    if limit:
        con.execute(
            f"CREATE TABLE hits AS SELECT * FROM read_parquet('{data_path}') LIMIT {limit}"
        )
    else:
        con.execute(
            f"CREATE TABLE hits AS SELECT * FROM read_parquet('{data_path}')"
        )
    elapsed = time.perf_counter() - t0
    n = con.execute("SELECT count(*) FROM hits").fetchone()[0]
    size_b = os.path.getsize(DB_PATH)
    con.close()
    return {"rows": int(n), "load_seconds": elapsed, "size_bytes": size_b}
