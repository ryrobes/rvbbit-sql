"""DuckDB loader — keeps a persistent database file under /data so
queries can hit cold cache too.

DuckDB ingests parquet natively (no schema massage needed), but we
project the columns through our PARQUET_TO_OURS rename so the column
names match every other system's `trips` table.
"""
from __future__ import annotations

import glob
import os
import time

import duckdb

import sys
sys.path.insert(0, "/bench/columnar_comparison")
from schema import COLUMNS, PARQUET_TO_OURS  # noqa: E402


DB_PATH = "/data/duckdb.db"


def _select_renamed(parquet_glob: str) -> str:
    cols = ", ".join(f'"{src}" AS {dst}' for src, dst in PARQUET_TO_OURS.items())
    return f"SELECT {cols} FROM read_parquet('{parquet_glob}')"


def load(data_dir: str) -> dict:
    parquet_glob = os.path.join(data_dir, "yellow_tripdata_*.parquet")
    files = sorted(glob.glob(parquet_glob))
    if not files:
        raise FileNotFoundError(f"no parquet files in {data_dir}")

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    con = duckdb.connect(DB_PATH)
    # Construct CREATE TABLE AS so DuckDB picks our normalized types.
    type_map = {pg: ch for _, _, pg in COLUMNS for ch in [{
        "smallint": "SMALLINT",
        "double precision": "DOUBLE",
        "text": "VARCHAR",
        "timestamp": "TIMESTAMP",
    }[pg]]}
    col_defs = ", ".join(f"{name} {type_map[pg]}" for name, _, pg in COLUMNS)
    con.execute(f"CREATE TABLE trips ({col_defs})")

    t0 = time.perf_counter()
    sel = _select_renamed(parquet_glob)
    # Cast each col to the normalized type during insert.
    cast_cols = ", ".join(
        f"CAST({name} AS {type_map[pg]}) AS {name}"
        for name, _, pg in COLUMNS
    )
    con.execute(f"INSERT INTO trips SELECT {cast_cols} FROM ({sel})")
    elapsed = time.perf_counter() - t0

    n = con.execute("SELECT count(*) FROM trips").fetchone()[0]
    size_b = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else None
    con.close()
    return {"rows": int(n), "load_seconds": elapsed, "size_bytes": size_b}
