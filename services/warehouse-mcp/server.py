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
  WAREHOUSE_SCHEMAS          CSV allowlist of exposed schemas (default: all but
                             rvbbit/pg_*/information_schema — i.e. hide internals)
"""
from __future__ import annotations
# psycopg's dict_row factory + sql.SQL composition trip Pyright's strict overloads
# (DictRow vs TupleRow covariance); the code is correct at runtime (see --selftest).
# pyright: reportArgumentType=false, reportCallIssue=false, reportIndexIssue=false
# pyright: reportReturnType=false, reportOptionalSubscript=false
import hmac, json, os, sys, time

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row

DSN = os.environ.get(
    "WAREHOUSE_DSN", "host=localhost port=55433 dbname=bench user=postgres password=rvbbit"
)
GRAPH = os.environ.get("RVBBIT_CATALOG_GRAPH", "db_catalog")
ROW_CAP = int(os.environ.get("WAREHOUSE_ROW_CAP", "1000"))
STMT_TIMEOUT_MS = int(os.environ.get("WAREHOUSE_STMT_TIMEOUT_MS", "30000"))

# Schema scoping — the warehouse and rvbbit's own internals share one database, so we
# expose the data schemas and hide the engine's catalog. _DENY is always hidden;
# WAREHOUSE_SCHEMAS (optional CSV allowlist) further restricts to just those.
_DENY_SCHEMAS = {"rvbbit", "pg_catalog", "information_schema", "pg_toast", "pg_temp"}
_ALLOW_SCHEMAS = {s.strip() for s in os.environ.get("WAREHOUSE_SCHEMAS", "").split(",") if s.strip()}

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


def _ro():
    """An autocommit, read-only connection for grounding lookups (samples/stats/
    freshness) — autocommit so one failed probe can't poison the rest of the loop."""
    c = psycopg.connect(DSN, row_factory=dict_row, autocommit=True)
    c.execute("SET default_transaction_read_only = on")
    c.execute(f"SET statement_timeout = {STMT_TIMEOUT_MS}")
    return c


def _with_as_of(sql: str, as_of):
    """Time-travel: the engine reads a leading `-- rvbbit: as_of <ts>` directive."""
    return f"-- rvbbit: as_of {as_of}\n{sql}" if as_of else sql


def _split(table: str):
    parts = table.split(".", 1)
    return ("public", parts[0]) if len(parts) == 1 else (parts[0], parts[1])


def _schema_allowed(schema: str) -> bool:
    """Hide rvbbit internals (and any pg_* schema); honor the optional allowlist."""
    if schema in _DENY_SCHEMAS or schema.startswith("pg_"):
        return False
    return (not _ALLOW_SCHEMAS) or (schema in _ALLOW_SCHEMAS)


def _samples(cur, schema: str, rel: str, n: int = 5):
    try:
        cur.execute(pgsql.SQL("SELECT * FROM {}.{} LIMIT %s").format(
            pgsql.Identifier(schema), pgsql.Identifier(rel)), (n,))
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _fmt_ndv(nd):
    """pg_stats n_distinct → friendly: positive=absolute count, negative=distinct/row ratio."""
    if nd is None or nd == 0:
        return None
    if nd > 0:
        return int(nd)
    if nd == -1:
        return "unique"
    return f"~{round(-nd * 100)}% distinct"


def _col_stats(cur, schema: str, rel: str, max_cols: int = 16):
    """Cheap per-column profile from the planner's ANALYZE stats (pg_stats): distinct
    count, null %, most-common values — what keeps Claude from inventing columns."""
    try:
        rows = cur.execute(
            "SELECT attname, n_distinct, round((null_frac*100)::numeric, 1) AS null_pct, "
            "(most_common_vals::text::text[])[1:6] AS top_vals "
            "FROM pg_stats WHERE schemaname=%s AND tablename=%s ORDER BY attname LIMIT %s",
            (schema, rel, max_cols),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return None
    out = {}
    for r in rows:
        col = {}
        ndv = _fmt_ndv(r["n_distinct"])
        if ndv is not None:
            col["ndv"] = ndv
        if r["null_pct"] is not None and float(r["null_pct"]) > 0:
            col["null_pct"] = float(r["null_pct"])
        if r["top_vals"]:
            col["top"] = r["top_vals"]
        if col:
            out[r["attname"]] = col
    return out or None


def _freshness(cur, schema: str, rel: str):
    """rvbbit's superpower, surfaced in the grounding: rows, last sync, staleness/drift."""
    try:
        r = cur.execute(
            "SELECT parquet_rows, row_groups, parquet_bytes, last_refresh_at, "
            "round(seconds_since_refresh) AS secs, drift_rows, shadow_heap_dirty "
            "FROM rvbbit.accel_freshness WHERE table_oid = to_regclass(%s)::oid LIMIT 1",
            (f"{schema}.{rel}",),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not r:
        return None
    drift = int(r["drift_rows"] or 0)
    return {
        "rows": r["parquet_rows"],
        "row_groups": r["row_groups"],
        "bytes": r["parquet_bytes"],
        "last_synced": r["last_refresh_at"],
        "seconds_since_refresh": float(r["secs"]) if r["secs"] is not None else None,
        "drift_rows": drift,
        "stale": bool(r["shadow_heap_dirty"]) or drift > 0,
    }


# ── tools ───────────────────────────────────────────────────────────────────

def tool_search_data(query: str, limit: int = 8, schema=None) -> dict:
    """Semantic search over the catalog KG + data-KG, each table hit grounded with live
    samples, cheap per-column stats, and freshness/drift. Internal (rvbbit/pg_*)
    schemas are hidden, so users only ever see the data they're meant to."""
    limit = max(1, min(int(limit), 25))
    with _conn() as c:
        hits = c.execute(
            "SELECT node_id, kind, schema_name, rel_name, col_name, score, doc "
            "FROM rvbbit.data_search(%s, %s, %s, %s)",
            (query, min(limit * 4, 100), None, GRAPH),   # over-fetch; internals get filtered out
        ).fetchall()
    matches = []
    with _ro() as rc, rc.cursor() as cur:
        for h in hits:
            if len(matches) >= limit:
                break
            if not _schema_allowed(h["schema_name"]):
                continue
            if schema and h["schema_name"] != schema:
                continue
            m = {
                "object": f'{h["schema_name"]}.{h["rel_name"]}'
                + (f'.{h["col_name"]}' if h["col_name"] else ""),
                "kind": h["kind"],
                "score": round(float(h["score"]), 3),
                "doc": h["doc"],
            }
            if not h["col_name"]:  # a table hit -> ground it (samples + stats + freshness)
                m["samples"] = _samples(cur, h["schema_name"], h["rel_name"], 5)
                st = _col_stats(cur, h["schema_name"], h["rel_name"])
                if st:
                    m["column_stats"] = st
                fr = _freshness(cur, h["schema_name"], h["rel_name"])
                if fr:
                    m["freshness"] = fr
            matches.append(m)
    return {"matches": matches,
            "note": None if matches else "no strong matches; try broader terms"}


def tool_describe_table(table: str) -> dict:
    """Full profile of one table: columns, live samples, per-column stats, freshness."""
    schema, rel = _split(table)
    if not _schema_allowed(schema):
        return {"error": {"code": "NOT_AUTHORIZED",
                          "message": f"schema '{schema}' is not exposed"}}
    with _ro() as rc, rc.cursor() as cur:
        cols = cur.execute(
            "SELECT column_name AS name, data_type AS type FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, rel),
        ).fetchall()
        if not cols:
            return {"error": {"code": "TABLE_NOT_FOUND", "message": table}}
        out = {"table": f"{schema}.{rel}", "columns": cols,
               "samples": _samples(cur, schema, rel, 5)}
        st = _col_stats(cur, schema, rel, max_cols=128)
        if st:
            out["column_stats"] = st
        fr = _freshness(cur, schema, rel)
        if fr:
            out["freshness"] = fr
    return out


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
    show("describe_table('public._demo_revenue')", tool_describe_table("public._demo_revenue"))
    show("describe_table('rvbbit.row_groups') — internal, must be hidden",
         tool_describe_table("rvbbit.row_groups"))
    show("list_metrics(search='error')", tool_list_metrics(search="error"))
    show("metric('demo_error_rate')", tool_metric("demo_error_rate", {}))
    show("validate_sql(good SELECT)", tool_validate_sql("SELECT region, drop_pct FROM public._demo_revenue"))
    show("validate_sql(a write — must be unsafe)", tool_validate_sql("DELETE FROM public._demo_revenue"))
    show("run_sql(good SELECT)", tool_run_sql("SELECT region, drop_pct FROM public._demo_revenue", limit=3))
    show("run_sql(a write — must be blocked)", tool_run_sql("DELETE FROM public._demo_revenue"))
    print("\nselftest done")


def _build_mcp():
    from mcp.server.fastmcp import FastMCP
    m = FastMCP("rvbbit-warehouse")
    _register(m)
    return m


def _with_api_key(app, key: str):
    """ASGI gate: require `Authorization: Bearer <key>` on HTTP requests (single
    shared key for now; lifespan + a /health probe pass through). Per-user keys
    are Phase 1 — swap this lookup for the mcp_api_keys table."""
    async def wrapper(scope, receive, send):
        if scope["type"] != "http" or not key:
            return await app(scope, receive, send)
        if scope.get("path", "").rstrip("/") == "/health":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok"})
            return
        auth = dict(scope.get("headers") or {}).get(b"authorization", b"").decode()
        if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], key)):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        return await app(scope, receive, send)
    return wrapper


def _serve_http():
    import uvicorn
    m = _build_mcp()
    app = m.streamable_http_app()
    key = os.environ.get("WAREHOUSE_MCP_KEY", "")
    host = os.environ.get("WAREHOUSE_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("WAREHOUSE_MCP_PORT", "8765"))
    path = getattr(m.settings, "streamable_http_path", "/mcp")
    if not key:
        print("WARNING: WAREHOUSE_MCP_KEY unset — auth DISABLED (dev only)", file=sys.stderr)
    print(f"rvbbit-warehouse MCP → http://{host}:{port}{path}  (auth: {'on' if key else 'OFF'})",
          file=sys.stderr)
    uvicorn.run(_with_api_key(app, key), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif "--http" in sys.argv:
        _serve_http()       # remote: streamable-HTTP + shared-key gate
    else:
        _build_mcp().run()  # local: stdio (Claude Code)
