"""TPC-H query text sourced from DuckDB's bundled TPC-H extension."""
from __future__ import annotations

import re

import duckdb


Q2_DECORRELATED = """
SELECT
    s_acctbal,
    s_name,
    n_name,
    p_partkey,
    p_mfgr,
    s_address,
    s_phone,
    s_comment
FROM
    part
JOIN partsupp ON p_partkey = ps_partkey
JOIN supplier ON s_suppkey = ps_suppkey
JOIN nation ON s_nationkey = n_nationkey
JOIN region ON n_regionkey = r_regionkey
JOIN (
    SELECT
        ps_partkey,
        min(ps_supplycost) AS min_supplycost
    FROM
        partsupp
    JOIN supplier ON s_suppkey = ps_suppkey
    JOIN nation ON s_nationkey = n_nationkey
    JOIN region ON n_regionkey = r_regionkey
    WHERE
        r_name = 'EUROPE'
    GROUP BY
        ps_partkey
) AS min_supply ON min_supply.ps_partkey = p_partkey
    AND min_supply.min_supplycost = ps_supplycost
WHERE
    p_size = 15
    AND p_type LIKE '%BRASS'
    AND r_name = 'EUROPE'
ORDER BY
    s_acctbal DESC,
    n_name,
    s_name,
    p_partkey
LIMIT 100
""".strip()


Q4_DECORRELATED = """
SELECT
    o_orderpriority,
    count(*) AS order_count
FROM
    orders
JOIN (
    SELECT DISTINCT
        l_orderkey
    FROM
        lineitem
    WHERE
        l_commitdate < l_receiptdate
) AS late_lineitem ON late_lineitem.l_orderkey = o_orderkey
WHERE
    o_orderdate >= CAST('1993-07-01' AS date)
    AND o_orderdate < CAST('1993-10-01' AS date)
GROUP BY
    o_orderpriority
ORDER BY
    o_orderpriority
""".strip()


Q17_DECORRELATED = """
SELECT
    sum(l_extendedprice) / 7.0 AS avg_yearly
FROM
    lineitem
JOIN part ON p_partkey = l_partkey
JOIN (
    SELECT
        l_partkey,
        0.2 * avg(l_quantity) AS quantity_threshold
    FROM
        lineitem
    GROUP BY
        l_partkey
) AS part_quantity ON part_quantity.l_partkey = p_partkey
WHERE
    p_brand = 'Brand#23'
    AND p_container = 'MED BOX'
    AND l_quantity < part_quantity.quantity_threshold
""".strip()


Q19_JOIN_FACTORED = """
SELECT
    sum(l_extendedprice * (1 - l_discount)) AS revenue
FROM
    lineitem
JOIN part ON p_partkey = l_partkey
WHERE
    l_shipmode IN ('AIR', 'AIR REG')
    AND l_shipinstruct = 'DELIVER IN PERSON'
    AND (
        (p_brand = 'Brand#12'
            AND p_container IN ('SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
            AND l_quantity >= 1
            AND l_quantity <= 1 + 10
            AND p_size BETWEEN 1 AND 5)
        OR (p_brand = 'Brand#23'
            AND p_container IN ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
            AND l_quantity >= 10
            AND l_quantity <= 10 + 10
            AND p_size BETWEEN 1 AND 10)
        OR (p_brand = 'Brand#34'
            AND p_container IN ('LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
            AND l_quantity >= 20
            AND l_quantity <= 20 + 10
            AND p_size BETWEEN 1 AND 15)
    )
""".strip()


Q20_DECORRELATED = """
SELECT
    s_name,
    s_address
FROM (
    SELECT
        s_suppkey,
        s_name,
        s_address
    FROM
        supplier
    JOIN nation ON s_nationkey = n_nationkey
    JOIN partsupp ON ps_suppkey = s_suppkey
    JOIN part ON p_partkey = ps_partkey
    JOIN (
        SELECT
            l_partkey,
            l_suppkey,
            sum(l_quantity) AS total_quantity
        FROM
            lineitem
        WHERE
            l_shipdate >= CAST('1994-01-01' AS date)
            AND l_shipdate < CAST('1995-01-01' AS date)
        GROUP BY
            l_partkey,
            l_suppkey
    ) AS yearly_lineitem ON yearly_lineitem.l_partkey = ps_partkey
        AND yearly_lineitem.l_suppkey = ps_suppkey
    WHERE
        p_name LIKE 'forest%'
        AND ps_availqty > 0.5 * yearly_lineitem.total_quantity
        AND n_name = 'CANADA'
    GROUP BY
        s_suppkey,
        s_name,
        s_address
) AS qualifying_supplier
ORDER BY
    s_name
""".strip()


Q21_DECORRELATED = """
SELECT
    s_name,
    count(*) AS numwait
FROM
    supplier
JOIN nation ON s_nationkey = n_nationkey
JOIN lineitem l1 ON s_suppkey = l1.l_suppkey
JOIN orders ON o_orderkey = l1.l_orderkey
JOIN (
    SELECT
        l_orderkey,
        count(DISTINCT l_suppkey) AS supplier_count
    FROM
        lineitem
    GROUP BY
        l_orderkey
) AS order_suppliers ON order_suppliers.l_orderkey = l1.l_orderkey
JOIN (
    SELECT
        l_orderkey,
        count(DISTINCT l_suppkey) AS late_supplier_count
    FROM
        lineitem
    WHERE
        l_receiptdate > l_commitdate
    GROUP BY
        l_orderkey
) AS late_order_suppliers ON late_order_suppliers.l_orderkey = l1.l_orderkey
WHERE
    o_orderstatus = 'F'
    AND l1.l_receiptdate > l1.l_commitdate
    AND order_suppliers.supplier_count > 1
    AND late_order_suppliers.late_supplier_count = 1
    AND n_name = 'SAUDI ARABIA'
GROUP BY
    s_name
ORDER BY
    numwait DESC,
    s_name
LIMIT 100
""".strip()


Q22_DECORRELATED = """
SELECT
    cntrycode,
    count(*) AS numcust,
    sum(c_acctbal) AS totacctbal
FROM (
    SELECT
        substring(c_phone FROM 1 FOR 2) AS cntrycode,
        c_acctbal
    FROM
        customer
    CROSS JOIN (
        SELECT
            avg(c_acctbal) AS avg_acctbal
        FROM
            customer
        WHERE
            c_acctbal > 0.00
            AND substring(c_phone FROM 1 FOR 2) IN ('13', '31', '23', '29', '30', '18', '17')
    ) AS positive_customer
    WHERE
        substring(c_phone FROM 1 FOR 2) IN ('13', '31', '23', '29', '30', '18', '17')
        AND c_acctbal > positive_customer.avg_acctbal
        AND c_custkey NOT IN (
            SELECT
                o_custkey
            FROM
                orders
        )
) AS custsale
GROUP BY
    cntrycode
ORDER BY
    cntrycode
""".strip()


def _normalize_query(qid: str, sql: str) -> str:
    # These canonical TPC-H templates contain correlated aggregate subqueries.
    # Several engines in this local comparison do not decorrelate them reliably,
    # so keep the benchmark bounded by running equivalent relational forms.
    if qid == "Q2":
        return Q2_DECORRELATED
    if qid == "Q4":
        return Q4_DECORRELATED
    if qid == "Q17":
        return Q17_DECORRELATED
    if qid == "Q19":
        return Q19_JOIN_FACTORED
    if qid == "Q20":
        return Q20_DECORRELATED
    if qid == "Q21":
        return Q21_DECORRELATED
    if qid == "Q22":
        return Q22_DECORRELATED
    return sql


def base_queries() -> list[tuple[str, str, str]]:
    con = duckdb.connect(":memory:")
    con.execute("LOAD tpch")
    rows = con.execute(
        "SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr"
    ).fetchall()
    con.close()
    return [
        (qid, f"TPC-H query {nr}", _normalize_query(qid, sql.strip().rstrip(";")))
        for nr, sql in rows
        for qid in [f"Q{nr}"]
    ]


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
