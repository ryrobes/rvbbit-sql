"""Explain Rvbbit route decisions without executing the query.

This is a prototype for the information we would eventually expose through a
backend `EXPLAIN (RVBBIT)` option. It intentionally runs outside Postgres for
now because the guarded Duck path and adaptive route profile still live in the
benchmark-side Python layer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import duckdb
import psycopg

import rvbbit_duck_hot
from rvbbit_duck_hot import (
    RVBBIT_DSN,
    DuckHotPathFallback,
    _rvbbit_query_table_metrics,
    _rvbbit_row_group_catalog,
    _create_duck_views,
    _duck_safe_select,
    _native_route_reason,
)
from rvbbit_route_model import PROFILE_ENV, extract_table_refs


def _candidate_for_route(route: str | None) -> str | None:
    if route == "duck":
        return "duck_vector"
    if route == "datafusion":
        return "datafusion_vector"
    if route == "native":
        return "rvbbit_native"
    return None


def _read_query(args: argparse.Namespace) -> str:
    if args.query:
        return args.query
    if args.file:
        return Path(args.file).read_text()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("pass --query, --file, or pipe SQL on stdin")


def _pg_explain(conn: psycopg.Connection, sql: str) -> tuple[str | None, str | None]:
    try:
        with conn.cursor() as cur:
            cur.execute(("EXPLAIN " + sql).encode())  # type: ignore[arg-type]
            return "\n".join(row[0] for row in cur.fetchall()), None
    except Exception as exc:
        conn.rollback()
        return None, str(exc).splitlines()[0]


def _referenced_rvbbit_tables(conn: psycopg.Connection, sql: str) -> list[dict[str, Any]]:
    refs = extract_table_refs(sql)
    if not refs:
        return []
    tables: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT lower(n.nspname), lower(c.relname),
                   c.oid::bigint,
                   count(rg.*)::bigint,
                   coalesce(sum(rg.n_rows), 0)::bigint,
                   coalesce(sum(rg.n_bytes), 0)::bigint
            FROM rvbbit.tables t
            JOIN pg_class c ON c.oid = t.table_oid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = c.oid
            WHERE coalesce(t.acceleration_enabled, true)
            GROUP BY n.nspname, c.relname, c.oid
            ORDER BY n.nspname, c.relname
            """.encode()
        )  # type: ignore[arg-type]
        for schema, relname, oid, row_groups, rows, bytes_ in cur.fetchall():
            if relname not in refs and f"{schema}.{relname}" not in refs:
                continue
            tables.append(
                {
                    "schema": schema,
                    "table": relname,
                    "oid": oid,
                    "row_groups": int(row_groups or 0),
                    "rows": int(rows or 0),
                    "bytes": int(bytes_ or 0),
                }
            )
    return tables


def _duck_explain(conn: psycopg.Connection, sql: str) -> tuple[str | None, str | None]:
    con = duckdb.connect(":memory:")
    try:
        con.execute("PRAGMA threads=4")
        _create_duck_views(con, sql, _rvbbit_row_group_catalog(conn))
        rows = con.execute("EXPLAIN " + sql).fetchall()
        return "\n".join(" ".join(str(part) for part in row) for row in rows), None
    except Exception as exc:
        return None, str(exc).splitlines()[0]
    finally:
        con.close()


def _set_candidates(result: dict[str, Any]) -> None:
    route = result.get("route")
    chosen = _candidate_for_route(route)
    result["chosen_candidate"] = chosen

    if not result.get("rvbbit_tables"):
        result["candidates"] = []
        return

    duck_supported = result.get("duck_supported")
    duck_available = duck_supported is not False
    duck_reason = result.get("duck_error")
    if duck_supported is None:
        duck_reason = "not checked"
    elif duck_supported:
        duck_reason = "DuckDB can plan the translated parquet views"

    candidates = [
        {
            "name": "duck_vector",
            "route": "duck",
            "available": duck_available,
            "selected": chosen == "duck_vector",
            "reason": duck_reason,
        },
        {
            "name": "datafusion_vector",
            "route": "datafusion",
            "available": duck_available,
            "selected": chosen == "datafusion_vector",
            "reason": (
                "DataFusion can plan the translated parquet views"
                if duck_available
                else duck_reason
            ),
        },
        {
            "name": "rvbbit_native",
            "route": "native",
            "available": True,
            "selected": chosen == "rvbbit_native",
            "reason": "PostgreSQL executor over Rvbbit native rewrites/custom scans",
        },
        {
            "name": "pg_rowstore",
            "route": "postgres_rowstore",
            "available": False,
            "selected": False,
            "reason": "no shadow heap or rowstore sidecar configured",
        },
    ]
    result["candidates"] = candidates


def explain_route(sql: str, dsn: str, include_plans: bool, check_duck: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "route": "none",
        "chosen_candidate": None,
        "candidates": [],
        "route_source": "none",
        "reason": None,
        "safe_select": False,
        "rvbbit_tables": [],
        "table_metrics": {},
        "profile": os.environ.get(PROFILE_ENV) or "/bench/rvbbit_route_profile.json",
        "features": None,
        "postgres_explain": None,
        "duck_explain": None,
        "duck_supported": None,
        "fallback": "postgres",
    }

    try:
        _duck_safe_select(sql)
        result["safe_select"] = True
    except DuckHotPathFallback as exc:
        result["reason"] = str(exc)
        _set_candidates(result)
        return result

    with psycopg.connect(dsn) as conn:
        pg_plan, pg_error = _pg_explain(conn, sql)
        if include_plans:
            result["postgres_explain"] = pg_plan
        if pg_error:
            result["reason"] = f"postgres EXPLAIN failed: {pg_error}"
            _set_candidates(result)
            return result

        tables = _referenced_rvbbit_tables(conn, sql)
        result["rvbbit_tables"] = tables
        if not tables:
            result["reason"] = "query does not reference Rvbbit tables"
            _set_candidates(result)
            return result

        metrics = _rvbbit_query_table_metrics(conn, sql)
        result["table_metrics"] = metrics

        native_reason = _native_route_reason(conn, sql, "auto")
        decision = rvbbit_duck_hot._LAST_ROUTE_DECISION
        features = rvbbit_duck_hot._LAST_ROUTE_FEATURES
        result["features"] = features
        if decision:
            result["route"] = decision.path
            result["route_source"] = decision.source
            result["reason"] = decision.reason
            result["confidence"] = decision.confidence
            if decision.entry:
                result["route_entry"] = decision.entry
        elif native_reason:
            result["route"] = "native"
            result["route_source"] = "native"
            result["reason"] = native_reason

        if check_duck:
            duck_plan, duck_error = _duck_explain(conn, sql)
            result["duck_supported"] = duck_error is None
            if include_plans:
                result["duck_explain"] = duck_plan
            if duck_error:
                result["duck_error"] = duck_error
                if result["route"] == "duck":
                    result["route"] = "native"
                    result["route_source"] = "fallback"
                    result["reason"] = f"Duck candidate rejected by EXPLAIN: {duck_error}"
        _set_candidates(result)
        return result


def _fmt_bytes(value: int | None) -> str:
    value = int(value or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} B"
        value /= 1024
    return f"{value} B"


def print_text(result: dict[str, Any]) -> None:
    print("Rvbbit Route")
    print(f"  Route       : {result.get('route')}")
    print(f"  Candidate   : {result.get('chosen_candidate') or 'none'}")
    print(f"  Source      : {result.get('route_source')}")
    print(f"  Reason      : {result.get('reason')}")
    if result.get("confidence") is not None:
        print(f"  Confidence  : {result['confidence']:.3f}")
    print(f"  Safe SELECT : {str(result.get('safe_select')).lower()}")
    print(f"  Duck check  : {result.get('duck_supported')}")
    print(f"  Fallback    : {result.get('fallback')}")
    print(f"  Profile     : {result.get('profile')}")

    print("\nCandidates")
    candidates = result.get("candidates") or []
    if not candidates:
        print("  none")
    for candidate in candidates:
        marker = "*" if candidate.get("selected") else "-"
        print(
            "  "
            f"{marker} {candidate['name']} "
            f"available={str(candidate.get('available')).lower()} "
            f"reason={candidate.get('reason')}"
        )

    tables = result.get("rvbbit_tables") or []
    print("\nRvbbit Tables")
    if not tables:
        print("  none")
    for table in tables:
        print(
            "  "
            f"{table['schema']}.{table['table']} "
            f"rows={table['rows']} row_groups={table['row_groups']} "
            f"bytes={_fmt_bytes(table['bytes'])}"
        )

    metrics = result.get("table_metrics") or {}
    if metrics:
        print("\nRoute Metrics")
        print(
            "  "
            f"rows={metrics.get('rows', 0)} "
            f"row_groups={metrics.get('row_groups', 0)} "
            f"bytes={_fmt_bytes(metrics.get('bytes', 0))}"
        )

    features = result.get("features") or {}
    if features:
        print("\nShape")
        for key in [
            "shape_key",
            "native_function",
            "from_count",
            "aggregate_count",
            "group_by",
            "order_by",
            "where",
            "like_count",
            "plan_has_join",
            "plan_has_subplan",
            "table_rows_bucket",
            "plan_width_bucket",
        ]:
            print(f"  {key}: {features.get(key)}")

    if result.get("postgres_explain"):
        print("\nPostgres Plan")
        print(result["postgres_explain"])
    if result.get("duck_explain"):
        print("\nDuck Plan")
        print(result["duck_explain"])
    if result.get("duck_error"):
        print("\nDuck Error")
        print(result["duck_error"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", "-q")
    parser.add_argument("--file", "-f")
    parser.add_argument("--dsn", default=os.environ.get("RVBBIT_DSN", RVBBIT_DSN))
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument("--plans", action="store_true", help="Include PostgreSQL and Duck EXPLAIN text")
    parser.add_argument(
        "--no-duck-check",
        action="store_true",
        help="Skip DuckDB EXPLAIN compatibility check",
    )
    args = parser.parse_args()

    result = explain_route(
        _read_query(args),
        dsn=args.dsn,
        include_plans=args.plans,
        check_duck=not args.no_duck_check,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print_text(result)


if __name__ == "__main__":
    main()
