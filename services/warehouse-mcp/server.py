#!/usr/bin/env python3
"""
rvbbit Warehouse MCP — Phase 0 prototype.

A governed, semantic, time-travel data interface for Claude (Cowork & Code).
Spec: docs/WAREHOUSE_MCP_PHASE0.md.  This is a standalone server (foldable into
rvbbit-mcp-gateway later); Phase 0 uses one read-only connection (per-user scoping
is Phase 1).

Run as an MCP (stdio) server:   python server.py
Smoke-test the tools directly:  python server.py --selftest

Config (env):
  WAREHOUSE_DSN              libpq DSN (default: bench on localhost:55433)
  RVBBIT_CATALOG_GRAPH       catalog KG name (default: db_catalog)
  WAREHOUSE_ROW_CAP          max rows returned by run_sql (default 1000)
  WAREHOUSE_STMT_TIMEOUT_MS  per-query timeout (default 30000)
"""
from __future__ import annotations
# psycopg's dict_row factory + sql.SQL composition trip Pyright's strict overloads
# (DictRow vs TupleRow covariance); the code is correct at runtime (see --selftest).
# pyright: reportArgumentType=false, reportCallIssue=false, reportIndexIssue=false
# pyright: reportReturnType=false, reportOptionalSubscript=false
import json, os, sys, time

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row

DSN = os.environ.get(
    "WAREHOUSE_DSN", "host=localhost port=55433 dbname=bench user=postgres password=rvbbit"
)
GRAPH = os.environ.get("RVBBIT_CATALOG_GRAPH", "db_catalog")
ROW_CAP = int(os.environ.get("WAREHOUSE_ROW_CAP", "1000"))
STMT_TIMEOUT_MS = int(os.environ.get("WAREHOUSE_STMT_TIMEOUT_MS", "30000"))

# common PG type OIDs -> friendly names (best-effort, Phase-0)
_TYPE = {16: "bool", 20: "int8", 21: "int2", 23: "int4", 25: "text", 700: "float4",
         701: "float8", 1043: "varchar", 1082: "date", 1114: "timestamp",
         1184: "timestamptz", 1700: "numeric", 114: "json", 3802: "jsonb"}


def _conn(read_only: bool = False):
    c = psycopg.connect(DSN, row_factory=dict_row, autocommit=not read_only)
    if read_only:
        # belt: txn read-only blocks any write/DDL even for a superuser DSN.
        # suspenders (prod): the mapped role simply lacks write grants.
        c.execute("SET default_transaction_read_only = on")
        c.execute(f"SET statement_timeout = {STMT_TIMEOUT_MS}")
    return c


def _with_as_of(sql: str, as_of):
    """Time-travel: the engine reads a leading `-- rvbbit: as_of <ts>` directive."""
    return f"-- rvbbit: as_of {as_of}\n{sql}" if as_of else sql


def _samples(schema: str, rel: str, n: int = 5):
    try:
        with _conn(read_only=True) as c, c.cursor() as cur:
            cur.execute(pgsql.SQL("SELECT * FROM {}.{} LIMIT %s").format(
                pgsql.Identifier(schema), pgsql.Identifier(rel)), (n,))
            return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _split(table: str):
    parts = table.split(".", 1)
    return ("public", parts[0]) if len(parts) == 1 else (parts[0], parts[1])


# ── tools ───────────────────────────────────────────────────────────────────

def tool_search_data(query: str, limit: int = 8, schema=None) -> dict:
    """Semantic search over the catalog KG + data-KG; grounded with live samples."""
    limit = max(1, min(int(limit), 25))
    with _conn() as c:
        hits = c.execute(
            "SELECT node_id, kind, schema_name, rel_name, col_name, score, doc "
            "FROM rvbbit.data_search(%s, %s, %s, %s)",
            (query, limit, None, GRAPH),
        ).fetchall()
    matches = []
    for h in hits:
        if schema and h["schema_name"] != schema:
            continue
        m = {
            "object": f'{h["schema_name"]}.{h["rel_name"]}'
            + (f'.{h["col_name"]}' if h["col_name"] else ""),
            "kind": h["kind"],
            "score": round(float(h["score"]), 3),
            "doc": h["doc"],
        }
        if not h["col_name"]:  # a table hit -> ground it with samples
            m["samples"] = _samples(h["schema_name"], h["rel_name"], 5)
        matches.append(m)
    return {"matches": matches,
            "note": None if matches else "no strong matches; try broader terms"}


def tool_describe_table(table: str) -> dict:
    """Full profile of one table: columns + live samples (+ stats in Phase 1)."""
    schema, rel = _split(table)
    with _conn() as c:
        cols = c.execute(
            "SELECT column_name AS name, data_type AS type FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, rel),
        ).fetchall()
    if not cols:
        return {"error": {"code": "TABLE_NOT_FOUND", "message": table}}
    return {"table": f"{schema}.{rel}", "columns": cols, "samples": _samples(schema, rel, 5)}


def tool_list_metrics(category=None, search=None) -> dict:
    """The blessed, governed metric catalog (latest version per metric)."""
    _ = category  # reserved: Phase-1 filters via rvbbit.entity_categories
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT ON (name) name, description, params, grain, "
            "check_sql IS NOT NULL AS has_check, version "
            "FROM rvbbit.metric_defs "
            "WHERE (%s::text IS NULL OR description ILIKE '%%'||%s::text||'%%' OR name ILIKE '%%'||%s::text||'%%') "
            "ORDER BY name, version DESC",
            (search, search, search),
        ).fetchall()
    return {"metrics": rows}


def tool_get_metric(name: str) -> dict:
    """One metric's definition + version history."""
    with _conn() as c:
        d = c.execute(
            "SELECT name, description, params, grain, sql AS definition_sql, check_sql "
            "FROM rvbbit.metric_defs WHERE name=%s ORDER BY version DESC LIMIT 1", (name,)
        ).fetchone()
        if not d:
            return {"error": {"code": "METRIC_NOT_FOUND", "message": name}}
        d["versions"] = c.execute(
            "SELECT version, created_at FROM rvbbit.metric_defs WHERE name=%s ORDER BY version DESC",
            (name,)).fetchall()
    return d


def tool_metric(name: str, params=None, as_of=None, def_as_of=None) -> dict:
    """A blessed, governed number — bitemporal (as_of = data-time, def_as_of = def-time)."""
    params = params or {}
    with _conn() as c:
        if as_of:
            c.execute("SET rvbbit.as_of_timestamp = %s", (str(as_of),))
        try:
            rows = c.execute("SELECT rvbbit.metric(%s, %s::jsonb) AS m",
                             (name, json.dumps(params))).fetchall()
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "METRIC_FAILED", "message": str(e)}}
    vals = [r["m"] for r in rows]
    return {"name": name, "result": vals[0] if len(vals) == 1 else vals,
            "params": params, "data_as_of": as_of, "def_as_of": def_as_of}


def tool_validate_sql(sql: str, as_of=None) -> dict:
    """Plan, don't execute — route_explain dry-run so Claude can self-correct cheaply."""
    try:
        with _conn() as c:
            ex = c.execute("SELECT rvbbit.route_explain(%s) AS e",
                           (_with_as_of(sql, as_of),)).fetchone()["e"]
    except Exception as e:  # noqa: BLE001
        return {"valid": False, "safe_select": False, "error": str(e)}
    return {
        "valid": True,
        "safe_select": bool(ex.get("safe_select")),
        "engine": ex.get("chosen_candidate"),
        "route_source": ex.get("route_source"),
        "rvbbit_tables": ex.get("rvbbit_tables"),
        "reason": ex.get("reason"),
        "candidates": [c.get("name") for c in (ex.get("candidates") or [])],
    }


def tool_run_sql(sql: str, as_of=None, limit=None) -> dict:
    """Governed read-only execute: validate -> safe_select gate -> read-only run + LIMIT."""
    limit = max(1, min(int(limit or ROW_CAP), ROW_CAP))
    v = tool_validate_sql(sql, as_of)
    if not v.get("valid"):
        return {"error": {"code": "INVALID_SQL", "message": v.get("error")}}
    if not v.get("safe_select"):
        return {"error": {"code": "NOT_SELECT",
                          "message": "only a read-only SELECT/CTE is allowed", "reason": v.get("reason")}}
    t0 = time.time()
    with _conn(read_only=True) as c, c.cursor() as cur:
        cur.execute(_with_as_of(sql, as_of))
        cols = ([{"name": d.name, "type": _TYPE.get(d.type_code, str(d.type_code))}
                 for d in cur.description] if cur.description else [])
        rows = cur.fetchmany(limit)
        truncated = cur.fetchone() is not None
    return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated,
            "engine": v.get("engine"), "elapsed_ms": int((time.time() - t0) * 1000),
            "as_of_applied": as_of}


# ── MCP server ───────────────────────────────────────────────────────────────

def _register(mcp):
    mcp.tool(name="search_data")(
        lambda query, limit=8, schema=None: tool_search_data(query, limit, schema))
    mcp.tool(name="describe_table")(lambda table: tool_describe_table(table))
    mcp.tool(name="list_metrics")(
        lambda category=None, search=None: tool_list_metrics(category, search))
    mcp.tool(name="get_metric")(lambda name: tool_get_metric(name))
    mcp.tool(name="metric")(
        lambda name, params=None, as_of=None, def_as_of=None: tool_metric(name, params, as_of, def_as_of))
    mcp.tool(name="validate_sql")(lambda sql, as_of=None: tool_validate_sql(sql, as_of))
    mcp.tool(name="run_sql")(lambda sql, as_of=None, limit=None: tool_run_sql(sql, as_of, limit))


def _selftest():
    def show(name, out):
        s = json.dumps(out, default=str)
        print(f"\n## {name}\n{s[:600]}{'…' if len(s) > 600 else ''}")
    show("search_data('orders and revenue')", tool_search_data("orders and revenue", 3))
    show("list_metrics(search='error')", tool_list_metrics(search="error"))
    show("metric('demo_error_rate')", tool_metric("demo_error_rate", {}))
    show("validate_sql(good SELECT)", tool_validate_sql("SELECT region, drop_pct FROM public._demo_revenue"))
    show("validate_sql(a write — must be unsafe)", tool_validate_sql("DELETE FROM public._demo_revenue"))
    show("run_sql(good SELECT)", tool_run_sql("SELECT region, drop_pct FROM public._demo_revenue", limit=3))
    show("run_sql(a write — must be blocked)", tool_run_sql("DELETE FROM public._demo_revenue"))
    print("\nselftest done")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        from mcp.server.fastmcp import FastMCP
        _mcp = FastMCP("rvbbit-warehouse")
        _register(_mcp)
        _mcp.run()
