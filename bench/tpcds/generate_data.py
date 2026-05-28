"""Generate TPC-DS parquet files with DuckDB's bundled TPC-DS extension."""
from __future__ import annotations

import json
import os
import sys

import duckdb

sys.path.insert(0, "/bench/tpcds")
from schema import (  # noqa: E402
    STANDARD_TABLES,
    data_dir_for_scale,
    duckdb_to_pg_type,
    pg_type_to_duck_cast,
)


def _table_schema(con: duckdb.DuckDBPyConnection, table: str) -> list[dict]:
    cols = []
    for name, duck_type, *_ in con.execute(f"DESCRIBE {table}").fetchall():
        pg_type = duckdb_to_pg_type(str(duck_type))
        cols.append({"name": str(name), "duck_type": str(duck_type), "pg_type": pg_type})
    return cols


def _select_list(cols: list[dict]) -> str:
    parts = []
    for col in cols:
        cast_type = pg_type_to_duck_cast(col["pg_type"])
        if cast_type:
            parts.append(f"CAST({col['name']} AS {cast_type}) AS {col['name']}")
        else:
            parts.append(col["name"])
    return ", ".join(parts)


def main() -> int:
    scale = os.environ.get("TPCDS_SCALE", "0.1")
    out_dir = data_dir_for_scale(scale)
    marker = os.path.join(out_dir, "_SUCCESS")
    force = os.environ.get("TPCDS_FORCE_REGEN")
    if os.path.exists(marker) and not force:
        print(f"TPC-DS data already present: {out_dir}")
        return 0

    os.makedirs(out_dir, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute("LOAD tpcds")
    print(f"Generating TPC-DS sf={scale} into {out_dir}")
    con.execute(f"CALL dsdgen(sf={float(scale)})")

    schema: dict[str, list[dict]] = {}
    for table in STANDARD_TABLES:
        schema[table] = _table_schema(con, table)

    with open(os.path.join(out_dir, "schema.json"), "w") as f:
        json.dump({"suite": "TPC-DS", "scale": scale, "tables": schema}, f, indent=2)

    for table in STANDARD_TABLES:
        path = os.path.join(out_dir, f"{table}.parquet")
        quoted_path = path.replace("'", "''")
        select_list = _select_list(schema[table])
        con.execute(
            f"COPY (SELECT {select_list} FROM {table}) TO '{quoted_path}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table:<24} {rows:>12,} rows -> {path}")
    with open(marker, "w") as f:
        f.write(f"scale={scale}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

