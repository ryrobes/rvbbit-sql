"""TPC-DS query text sourced from DuckDB's bundled TPC-DS extension."""
from __future__ import annotations

import re

import duckdb


def base_queries() -> list[tuple[str, str, str]]:
    con = duckdb.connect(":memory:")
    con.execute("LOAD tpcds")
    rows = con.execute(
        "SELECT query_nr, query FROM tpcds_queries() ORDER BY query_nr"
    ).fetchall()
    con.close()
    return [(f"Q{nr}", f"TPC-DS query {nr}", sql.strip().rstrip(";")) for nr, sql in rows]


def _uses_postgres_parser(system: str) -> bool:
    return (
        system in {"pg_baseline", "citus", "hydra", "alloydb", "pg_heap"}
        or system.startswith("rvbbit")
    )


def _matching_paren(sql: str, open_idx: int) -> int | None:
    depth = 0
    i = open_idx
    in_single = False
    in_double = False
    while i < len(sql):
        ch = sql[i]
        if in_single:
            if ch == "'" and i + 1 < len(sql) and sql[i + 1] == "'":
                i += 2
                continue
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return None


def _split_top_level_args(args_sql: str) -> list[str]:
    args: list[str] = []
    start = 0
    depth = 0
    in_single = False
    in_double = False
    i = 0
    while i < len(args_sql):
        ch = args_sql[i]
        if in_single:
            if ch == "'" and i + 1 < len(args_sql) and args_sql[i + 1] == "'":
                i += 2
                continue
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                args.append(args_sql[start:i].strip())
                start = i + 1
        i += 1
    args.append(args_sql[start:].strip())
    return args


def _rewrite_pg_round2(sql: str) -> str:
    out: list[str] = []
    pos = 0
    pattern = re.compile(r"\bround\s*\(", flags=re.I)
    while True:
        match = pattern.search(sql, pos)
        if match is None:
            out.append(sql[pos:])
            return "".join(out)
        open_idx = match.end() - 1
        close_idx = _matching_paren(sql, open_idx)
        if close_idx is None:
            out.append(sql[pos:])
            return "".join(out)

        args = _split_top_level_args(sql[open_idx + 1 : close_idx])
        out.append(sql[pos : match.start()])
        if len(args) == 2:
            out.append(f"round(({args[0]})::numeric, {args[1]})::double precision")
        else:
            out.append(sql[match.start() : close_idx + 1])
        pos = close_idx + 1


def sql_for_system(sql: str, system: str, qid: str) -> str:
    if _uses_postgres_parser(system):
        return _rewrite_pg_round2(sql)
    if system != "clickhouse":
        return sql
    out = sql
    out = re.sub(r"CAST\('([0-9-]+)' AS date\)", r"toDate('\1')", out, flags=re.I)
    out = re.sub(r"date '([0-9-]+)'", r"toDate('\1')", out, flags=re.I)
    out = re.sub(
        r"extract\(year FROM ([^)]+)\)", r"toYear(\1)", out, flags=re.I
    )
    out = re.sub(
        r"substring\(([^()]+?) FROM ([0-9]+) FOR ([0-9]+)\)",
        r"substring(\1, \2, \3)",
        out,
        flags=re.I,
    )
    out = re.sub(r"\s+NULLS\s+(FIRST|LAST)", "", out, flags=re.I)
    return out
