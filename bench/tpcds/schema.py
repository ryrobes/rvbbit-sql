"""TPC-DS table metadata used by the benchmark harness.

This is a reproducible TPC-DS-derived harness, not an audited TPC result.
DuckDB's bundled TPC-DS generator produces the data and canonical query text.
"""
from __future__ import annotations

import json
import os
import re


STANDARD_TABLES = [
    "call_center",
    "catalog_page",
    "catalog_returns",
    "catalog_sales",
    "customer",
    "customer_address",
    "customer_demographics",
    "date_dim",
    "household_demographics",
    "income_band",
    "inventory",
    "item",
    "promotion",
    "reason",
    "ship_mode",
    "store",
    "store_returns",
    "store_sales",
    "time_dim",
    "warehouse",
    "web_page",
    "web_returns",
    "web_sales",
    "web_site",
]


def data_dir_for_scale(scale: str | float) -> str:
    label = str(scale).replace(".", "_")
    return f"/data/tpcds/sf_{label}"


def duckdb_path_for_scale(scale: str | float) -> str:
    return f"{data_dir_for_scale(scale)}/tpcds.duckdb"


def schema_path_for_scale(scale: str | float) -> str:
    return f"{data_dir_for_scale(scale)}/schema.json"


def _schema_path_from_data_dir(data_dir: str) -> str:
    return os.path.join(data_dir, "schema.json")


def _load_schema(path: str) -> dict[str, list[tuple[str, str]]]:
    with open(path) as f:
        raw = json.load(f)
    return {
        table: [(str(col["name"]), str(col["pg_type"])) for col in cols]
        for table, cols in raw["tables"].items()
    }


def schema_for_data_dir(data_dir: str) -> dict[str, list[tuple[str, str]]]:
    return _load_schema(_schema_path_from_data_dir(data_dir))


def table_names(data_dir: str | None = None) -> list[str]:
    if data_dir is None:
        return list(STANDARD_TABLES)
    path = _schema_path_from_data_dir(data_dir)
    if not os.path.exists(path):
        return list(STANDARD_TABLES)
    schema = _load_schema(path)
    return [t for t in STANDARD_TABLES if t in schema]


def duckdb_to_pg_type(duck_type: str) -> str:
    t = duck_type.upper()
    if t in {"TINYINT", "SMALLINT", "INTEGER"}:
        return "INTEGER"
    if t == "BIGINT":
        return "BIGINT"
    if t in {"FLOAT", "REAL", "DOUBLE"} or t.startswith("DECIMAL"):
        return "DOUBLE PRECISION"
    if t == "DATE":
        return "DATE"
    if t.startswith("VARCHAR") or t.startswith("CHAR"):
        m = re.search(r"\((\d+)\)", t)
        return f"VARCHAR({m.group(1)})" if m else "TEXT"
    raise ValueError(f"unmapped DuckDB type: {duck_type}")


def pg_type_to_duck_cast(pg_type: str) -> str | None:
    if pg_type == "DOUBLE PRECISION":
        return "DOUBLE"
    return None


def ddl_postgres(data_dir: str, table: str, using: str | None = None) -> str:
    schema = schema_for_data_dir(data_dir)
    cols = ",\n  ".join(f"{name} {typ}" for name, typ in schema[table])
    suffix = f" USING {using}" if using else ""
    return f"CREATE TABLE {table} (\n  {cols}\n){suffix}"


def _clickhouse_type(pg_type: str) -> str:
    if pg_type == "INTEGER":
        return "Int32"
    if pg_type == "BIGINT":
        return "Int64"
    if pg_type == "DATE":
        return "Date"
    if pg_type == "DOUBLE PRECISION":
        return "Float64"
    if pg_type.startswith("VARCHAR") or pg_type == "TEXT":
        return "String"
    raise ValueError(f"unmapped type: {pg_type}")


def ddl_clickhouse(data_dir: str, table: str) -> str:
    schema = schema_for_data_dir(data_dir)
    cols = ",\n  ".join(f"{name} {_clickhouse_type(typ)}" for name, typ in schema[table])
    return f"CREATE TABLE {table} (\n  {cols}\n) ENGINE = MergeTree ORDER BY tuple()"


def copy_columns(data_dir: str, table: str) -> str:
    schema = schema_for_data_dir(data_dir)
    return ", ".join(name for name, _ in schema[table])


def duckdb_select_list(data_dir: str, table: str) -> str:
    schema = schema_for_data_dir(data_dir)
    parts = []
    for name, typ in schema[table]:
        cast_type = pg_type_to_duck_cast(typ)
        if cast_type:
            parts.append(f"CAST({name} AS {cast_type}) AS {name}")
        else:
            parts.append(name)
    return ", ".join(parts)

