"""TPC-H query text sourced from DuckDB's bundled TPC-H extension."""
from __future__ import annotations

import re

import duckdb


def base_queries() -> list[tuple[str, str, str]]:
    con = duckdb.connect(":memory:")
    con.execute("LOAD tpch")
    rows = con.execute(
        "SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr"
    ).fetchall()
    con.close()
    return [(f"Q{nr}", f"TPC-H query {nr}", sql.strip().rstrip(";")) for nr, sql in rows]


def sql_for_system(sql: str, system: str, qid: str) -> str:
    if system != "clickhouse":
        return sql
    out = sql
    out = re.sub(r"CAST\('([0-9-]+)' AS date\)", r"toDate('\1')", out, flags=re.I)
    out = re.sub(r"date '([0-9-]+)'", r"toDate('\1')", out, flags=re.I)
    out = re.sub(
        r"extract\(year FROM ([^)]+)\)", r"toYear(\1)", out, flags=re.I
    )
    out = re.sub(
        r"substring\(c_phone FROM 1 FOR 2\)",
        "substring(c_phone, 1, 2)",
        out,
        flags=re.I,
    )
    if qid == "Q13":
        out = """
SELECT
    c_count,
    count(*) AS custdist
FROM (
    SELECT
        c_custkey,
        count(o_orderkey) AS c_count
    FROM
        customer
    LEFT OUTER JOIN orders ON c_custkey = o_custkey
        AND o_comment NOT LIKE '%special%requests%'
    GROUP BY
        c_custkey
) AS c_orders
GROUP BY
    c_count
ORDER BY
    custdist DESC,
    c_count DESC
""".strip()
    return out
