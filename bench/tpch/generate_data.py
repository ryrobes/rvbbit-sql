"""Generate TPC-H parquet files with DuckDB's bundled TPC-H extension."""
from __future__ import annotations

import os
import sys

import duckdb

sys.path.insert(0, "/bench/tpch")
from schema import data_dir_for_scale, table_names  # noqa: E402


def _load_tpch(con: duckdb.DuckDBPyConnection) -> None:
    try:
        con.execute("LOAD tpch")
    except duckdb.IOException:
        con.execute("INSTALL tpch")
        con.execute("LOAD tpch")


def main() -> int:
    scale = os.environ.get("TPCH_SCALE", "0.1")
    out_dir = data_dir_for_scale(scale)
    marker = os.path.join(out_dir, "_SUCCESS")
    force = os.environ.get("TPCH_FORCE_REGEN")
    if os.path.exists(marker) and not force:
        print(f"TPC-H data already present: {out_dir}")
        return 0

    os.makedirs(out_dir, exist_ok=True)
    con = duckdb.connect(":memory:")
    _load_tpch(con)
    print(f"Generating TPC-H sf={scale} into {out_dir}")
    con.execute(f"CALL dbgen(sf={float(scale)})")
    for table in table_names():
        path = os.path.join(out_dir, f"{table}.parquet")
        quoted_path = path.replace("'", "''")
        con.execute(
            f"COPY (SELECT * FROM {table}) TO '{quoted_path}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table:<10} {rows:>12,} rows -> {path}")
    with open(marker, "w") as f:
        f.write(f"scale={scale}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
