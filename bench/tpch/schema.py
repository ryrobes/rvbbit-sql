"""TPC-H table definitions used by the benchmark harness.

This is a reproducible TPC-H-derived harness, not an audited TPC result.
DuckDB's bundled TPC-H generator produces the data and canonical query text.
"""
from __future__ import annotations

from collections import OrderedDict


TABLES: "OrderedDict[str, list[tuple[str, str]]]" = OrderedDict(
    [
        (
            "region",
            [
                ("r_regionkey", "INTEGER"),
                ("r_name", "VARCHAR(25)"),
                ("r_comment", "VARCHAR(152)"),
            ],
        ),
        (
            "nation",
            [
                ("n_nationkey", "INTEGER"),
                ("n_name", "VARCHAR(25)"),
                ("n_regionkey", "INTEGER"),
                ("n_comment", "VARCHAR(152)"),
            ],
        ),
        (
            "part",
            [
                ("p_partkey", "INTEGER"),
                ("p_name", "VARCHAR(55)"),
                ("p_mfgr", "VARCHAR(25)"),
                ("p_brand", "VARCHAR(10)"),
                ("p_type", "VARCHAR(25)"),
                ("p_size", "INTEGER"),
                ("p_container", "VARCHAR(10)"),
                ("p_retailprice", "DOUBLE PRECISION"),
                ("p_comment", "VARCHAR(23)"),
            ],
        ),
        (
            "supplier",
            [
                ("s_suppkey", "INTEGER"),
                ("s_name", "VARCHAR(25)"),
                ("s_address", "VARCHAR(40)"),
                ("s_nationkey", "INTEGER"),
                ("s_phone", "VARCHAR(15)"),
                ("s_acctbal", "DOUBLE PRECISION"),
                ("s_comment", "VARCHAR(101)"),
            ],
        ),
        (
            "partsupp",
            [
                ("ps_partkey", "INTEGER"),
                ("ps_suppkey", "INTEGER"),
                ("ps_availqty", "INTEGER"),
                ("ps_supplycost", "DOUBLE PRECISION"),
                ("ps_comment", "VARCHAR(199)"),
            ],
        ),
        (
            "customer",
            [
                ("c_custkey", "INTEGER"),
                ("c_name", "VARCHAR(25)"),
                ("c_address", "VARCHAR(40)"),
                ("c_nationkey", "INTEGER"),
                ("c_phone", "VARCHAR(15)"),
                ("c_acctbal", "DOUBLE PRECISION"),
                ("c_mktsegment", "VARCHAR(10)"),
                ("c_comment", "VARCHAR(117)"),
            ],
        ),
        (
            "orders",
            [
                ("o_orderkey", "BIGINT"),
                ("o_custkey", "INTEGER"),
                ("o_orderstatus", "VARCHAR(1)"),
                ("o_totalprice", "DOUBLE PRECISION"),
                ("o_orderdate", "DATE"),
                ("o_orderpriority", "VARCHAR(15)"),
                ("o_clerk", "VARCHAR(15)"),
                ("o_shippriority", "INTEGER"),
                ("o_comment", "VARCHAR(79)"),
            ],
        ),
        (
            "lineitem",
            [
                ("l_orderkey", "BIGINT"),
                ("l_partkey", "INTEGER"),
                ("l_suppkey", "INTEGER"),
                ("l_linenumber", "INTEGER"),
                ("l_quantity", "DOUBLE PRECISION"),
                ("l_extendedprice", "DOUBLE PRECISION"),
                ("l_discount", "DOUBLE PRECISION"),
                ("l_tax", "DOUBLE PRECISION"),
                ("l_returnflag", "VARCHAR(1)"),
                ("l_linestatus", "VARCHAR(1)"),
                ("l_shipdate", "DATE"),
                ("l_commitdate", "DATE"),
                ("l_receiptdate", "DATE"),
                ("l_shipinstruct", "VARCHAR(25)"),
                ("l_shipmode", "VARCHAR(10)"),
                ("l_comment", "VARCHAR(44)"),
            ],
        ),
    ]
)


def table_names() -> list[str]:
    return list(TABLES.keys())


def data_dir_for_scale(scale: str | float) -> str:
    label = str(scale).replace(".", "_")
    return f"/data/tpch/sf_{label}"


def duckdb_path_for_scale(scale: str | float) -> str:
    return f"{data_dir_for_scale(scale)}/tpch.duckdb"


def ddl_postgres(table: str, using: str | None = None) -> str:
    cols = ",\n  ".join(f"{name} {typ}" for name, typ in TABLES[table])
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
    if pg_type.startswith("DECIMAL"):
        return pg_type.replace("DECIMAL", "Decimal")
    if pg_type.startswith("VARCHAR"):
        return "String"
    raise ValueError(f"unmapped type: {pg_type}")


def ddl_clickhouse(table: str) -> str:
    cols = ",\n  ".join(
        f"{name} {_clickhouse_type(typ)}" for name, typ in TABLES[table]
    )
    return f"CREATE TABLE {table} (\n  {cols}\n) ENGINE = MergeTree ORDER BY tuple()"


def copy_columns(table: str) -> str:
    return ", ".join(name for name, _ in TABLES[table])


def duckdb_select_list(table: str) -> str:
    parts = []
    for name, typ in TABLES[table]:
        if typ == "DOUBLE PRECISION":
            parts.append(f"CAST({name} AS DOUBLE) AS {name}")
        else:
            parts.append(name)
    return ", ".join(parts)
