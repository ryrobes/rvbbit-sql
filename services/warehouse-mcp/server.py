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

OAuth mode (for Claude Desktop/Cowork's native connector) — set a public URL:
  WAREHOUSE_PUBLIC_URL       e.g. https://dwmcp.example.com (enables the OAuth AS;
                             unset = legacy shared-key gate via WAREHOUSE_MCP_KEY)
  WAREHOUSE_LOGIN_PASSWORD   REQUIRED in OAuth mode; shared login password
  WAREHOUSE_ALLOWED_EMAILS   optional CSV allowlist; entries match exactly OR as a domain when prefixed
                             with '@' (e.g. "@acme.com" allows anyone @acme.com). Empty = any email + pw.
  WAREHOUSE_JWT_SECRET       REQUIRED in OAuth mode; token-signing secret — MUST be
                             independent of WAREHOUSE_MCP_KEY (users hold that one)
  WAREHOUSE_MCP_STATIC_CALLER optional caller label/email for the legacy shared bearer;
                             client_id remains "static-key" (default caller: "static-key")
"""
from __future__ import annotations
# psycopg's dict_row factory + sql.SQL composition trip Pyright's strict overloads
# (DictRow vs TupleRow covariance); the code is correct at runtime (see --selftest).
# pyright: reportArgumentType=false, reportCallIssue=false, reportIndexIssue=false
# pyright: reportReturnType=false, reportOptionalSubscript=false, reportMissingImports=false
import asyncio, hashlib, hmac, json, math, os, re, secrets, shutil, socket, subprocess, sys, tempfile, threading, time, uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row

DSN = os.environ.get(
    "WAREHOUSE_DSN", "host=localhost port=55433 dbname=bench user=postgres password=rvbbit"
)
GRAPH = os.environ.get("RVBBIT_CATALOG_GRAPH", "db_catalog")


def _env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


ROW_CAP = _env_int("WAREHOUSE_ROW_CAP", 1000, maximum=100_000)
STMT_TIMEOUT_MS = _env_int("WAREHOUSE_STMT_TIMEOUT_MS", 30_000, maximum=600_000)
CUBE_PIVOT_CELL_CAP = _env_int("WAREHOUSE_CUBE_PIVOT_CELL_CAP", 2500, maximum=25_000)

# Schema scoping — the warehouse and rvbbit's own internals share one database, so we
# expose the data schemas and hide the engine's catalog. _DENY is always hidden;
# WAREHOUSE_SCHEMAS (optional CSV allowlist) further restricts to just those.
_DENY_SCHEMAS = {"rvbbit", "pg_catalog", "information_schema", "pg_toast", "pg_temp"}
_ALLOW_SCHEMAS = {s.strip() for s in os.environ.get("WAREHOUSE_SCHEMAS", "").split(",") if s.strip()}

# common PG type OIDs -> friendly names (best-effort, Phase-0)
_TYPE = {16: "bool", 20: "int8", 21: "int2", 23: "int4", 25: "text", 700: "float4",
         701: "float8", 1043: "varchar", 1082: "date", 1114: "timestamp",
         1184: "timestamptz", 1700: "numeric", 114: "json", 3802: "jsonb"}


def _conn(read_only: bool = False, role: str | None = None):
    c = psycopg.connect(DSN, row_factory=dict_row, autocommit=not read_only)
    if role:
        # Burrow mode (docs/BURROW_PLAN.md): execute as the caller's PG role —
        # their GRANTs/RLS govern the query. Connection is per-call, so plain
        # SET ROLE is safe (no pool to leak into).
        c.execute('SET ROLE "%s"' % role.replace('"', '""'))
    if read_only:
        # belt: txn read-only blocks any write/DDL even for a superuser DSN.
        # suspenders (prod): the mapped role simply lacks write grants.
        c.execute("SET default_transaction_read_only = on")
        c.execute(f"SET statement_timeout = {STMT_TIMEOUT_MS}")
    return c


_BURROW_ROLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_$@.\-]{0,62}$")
# Session-cookie surfaces (the app data bridge) carry identity out-of-band of
# the OAuth token context — they park the subject here around tool calls so
# an extra role arg never leaks into the MCP tool schemas.
import contextvars
_SESSION_SUB = contextvars.ContextVar("rvbbit_session_sub", default=None)
# A native messaging adapter can call Warehouse through one long-lived shared
# MCP connection even though each turn belongs to a different human.  Hermes
# forwards that already-verified turn identity in request metadata.  Keep it in
# a task-local override so it can improve attribution without ever changing the
# access token, PG role, or authorization decision for the call.
_FORWARDED_CALLER = contextvars.ContextVar("rvbbit_forwarded_caller", default=None)


def _session_pg_role(sub=None):
    """In pg auth mode (Burrow) the authenticated subject IS a Postgres role —
    return it for SET ROLE execution; None means service identity (shared/
    stdio modes, or a malformed subject)."""
    try:
        import auth
        if getattr(auth, "AUTH_MODE", "shared") != "pg":
            return None
    except Exception:  # noqa: BLE001 — auth module absent in some harnesses
        return None
    s = sub if sub is not None else (_SESSION_SUB.get() or _caller()[0])
    return s if s and _BURROW_ROLE_RE.fullmatch(str(s)) else None


def _ro():
    """An autocommit, read-only connection for grounding lookups (samples/stats/
    freshness) — autocommit so one failed probe can't poison the rest of the loop."""
    c = psycopg.connect(DSN, row_factory=dict_row, autocommit=True)
    c.execute("SET default_transaction_read_only = on")
    c.execute(f"SET statement_timeout = {STMT_TIMEOUT_MS}")
    return c


def _normalize_as_of(as_of):
    """Return one safe, UTC timestamp for the statement directive.

    The value ultimately appears in a leading SQL comment, so accepting arbitrary
    text here would let a newline escape the directive.  Keep the public contract
    deliberately narrow: ISO-8601 timestamps (a date alone means midnight UTC).
    """
    if as_of is None:
        return None
    if isinstance(as_of, datetime):
        parsed = as_of
    else:
        raw = str(as_of).strip()
        if not raw:
            return None
        if len(raw) > 80 or "\n" in raw or "\r" in raw or "\x00" in raw:
            raise ValueError("as_of must be one ISO-8601 timestamp")
        candidate = raw[:-1] + "+00:00" if raw.lower().endswith("z") else raw
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError("as_of must be one ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _with_as_of(sql: str, as_of):
    """Time-travel: the engine reads a leading `-- rvbbit: as_of <ts>` directive."""
    normalized = _normalize_as_of(as_of)
    return f"-- rvbbit: as_of {normalized}\n{sql}" if normalized else sql


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

def tool_capability_search(query: str, limit: int = 8, kinds=None) -> dict:
    """Search WHAT THIS WAREHOUSE CAN DO — the same just-in-time discovery the
    built-in assistant uses: semantic SQL operators (means()/about()/extract/
    classify/forecast/...), installed MCP servers and their tools, installable
    capability packs, SQL syntax patterns, models and providers. Ask in plain
    language ("extract entities from text", "search the web", "forecast this
    series") and get callable names + signatures back — operators are directly
    usable inside run_sql. Complements search_data (which finds DATA: tables,
    metrics, cubes); this finds ABILITIES. kinds filter (optional):
    cap_operator | cap_mcp_tool | cap_pack | cap_syntax | model | provider."""
    limit = max(1, min(int(limit or 8), 25))
    ks = None
    if kinds:
        ks = [str(k).strip() for k in (kinds if isinstance(kinds, list) else str(kinds).split(",")) if str(k).strip()]
        ks = ks or None
    rebuilt = False
    with _conn() as c:
        # Self-heal FIRST: the capabilities graph only updates when
        # capability_crawl() runs (e.g. after installing an MCP server), and
        # a stale-but-populated index returns confident results that simply
        # omit new tools — invisible to a zero-match check. The staleness
        # probe is one cheap query and a full re-crawl measures ~2s, so when
        # stale we just rebuild before searching.
        try:
            probe = c.execute(
                "SELECT to_regprocedure('rvbbit.capability_search_stale()') IS NOT NULL AS ok"
            ).fetchone()
            if probe and probe["ok"] and bool(
                c.execute("SELECT rvbbit.capability_search_stale() AS s").fetchone()["s"]
            ):
                c.execute("SELECT rvbbit.capability_crawl()")
                rebuilt = True
        except Exception:  # noqa: BLE001
            pass  # search whatever index exists; never fail discovery on upkeep
        rows = c.execute(
            "SELECT kind, name, score, doc FROM rvbbit.capability_search(%s, %s, %s)",
            (query, limit, ks),
        ).fetchall()
    out = {
        "query": query,
        "matches": [
            {"kind": r["kind"], "name": r["name"], "score": round(float(r["score"] or 0), 3), "doc": r["doc"]}
            for r in rows
        ],
        "hint": "cap_operator results are SQL functions (use via run_sql); cap_pack results are installable capabilities; cap_mcp_tool results are tools on MCP servers already installed in the warehouse.",
    }
    if rebuilt:
        out["index_rebuilt"] = "capability index was stale — rebuilt automatically before this search"
    return out


def tool_search_data(query: str, limit: int = 8, schema=None) -> dict:
    """Semantic search over the catalog KG + data-KG, each table hit grounded with live
    samples, cheap per-column stats, and freshness/drift. Internal (rvbbit/pg_*)
    schemas are hidden, so users only ever see the data they're meant to."""
    limit = max(1, min(int(limit), 25))
    with _conn() as c:
        # usage-weighted: objects employees actually query climb (boosted_score folds in
        # mcp_popular_objects). Falls back to pure relevance when nothing is logged yet.
        hits = c.execute(
            "SELECT node_id, kind, schema_name, rel_name, col_name, score, boosted_score, doc, usage_touches "
            "FROM rvbbit.search_data_weighted(%s, %s, %s, %s, 0.5)",
            (query, min(limit * 4, 100), None, GRAPH),   # over-fetch; internals get filtered out
        ).fetchall()
    # discovery gradient: curated metrics/cubes outrank raw tables, then by usage-weighted score
    _tier = {"metric": 0, "cube": 1, "db_table": 2, "db_column": 2}
    hits.sort(key=lambda h: (_tier.get(h["kind"], 3), -float(h["boosted_score"] or h["score"] or 0)))
    matches = []
    with _ro() as rc, rc.cursor() as cur:
        for h in hits:
            if len(matches) >= limit:
                break
            curated = h["kind"] in ("metric", "cube")   # always allowed (not raw schema)
            if not curated and not _schema_allowed(h["schema_name"]):
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
            if h["usage_touches"]:
                m["usage_touches"] = int(h["usage_touches"])   # how often employees query it
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


def tool_describe_table(table: str, lean: bool = False) -> dict:
    """Full profile of one table: columns, live samples, AND per-column stats — null %, distinct
    count, and the actual most-common values (the enum/value dictionary, so you never guess a
    status/type literal) — plus freshness. Pass lean=true for a compact view (columns + null%/distinct
    + freshness, no samples or top-values) on wide tables to stay under the token budget."""
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
        out = {"table": f"{schema}.{rel}", "columns": cols}
        if not lean:
            out["samples"] = _samples(cur, schema, rel, 5)
        st = _col_stats(cur, schema, rel, max_cols=128)
        if st:
            # lean: drop the (potentially long) top-values, keep ndv/null%.
            # NB: _col_stats returns a dict keyed by column name (not a row list) —
            # iterating it as rows was the "string indices must be integers" crash.
            out["column_stats"] = (
                {name: {k: v for k, v in s.items() if k != "top"} for name, s in st.items()}
                if lean else st
            )
        fr = _freshness(cur, schema, rel)
        if fr:
            out["freshness"] = fr
    return out


def tool_profile_schema(schema=None) -> dict:
    """A fast overview of every (allowed) table: estimated row count + column count — to see which
    tables are populated WITHOUT running count(*) probes. Optionally scope to one schema. Row counts
    are planner estimates (pg_class.reltuples, ~0 if never analyzed); use describe_table for a full
    per-column profile of one table."""
    with _ro() as rc, rc.cursor() as cur:
        rows = cur.execute(
            "SELECT n.nspname AS schema, c.relname AS \"table\", "
            "       greatest(c.reltuples, 0)::bigint AS est_rows, "
            "       (SELECT count(*) FROM information_schema.columns ic "
            "          WHERE ic.table_schema = n.nspname AND ic.table_name = c.relname) AS columns "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r','p','f') "
            "  AND n.nspname NOT LIKE 'pg_%%' AND n.nspname <> 'information_schema' "
            "  AND (%s::text IS NULL OR n.nspname = %s::text) "
            "ORDER BY n.nspname, c.relname",
            (schema, schema),
        ).fetchall()
    rows = [r for r in rows if _schema_allowed(r["schema"])]
    return {"tables": rows, "note": "est_rows are planner estimates; 0 may mean empty OR never-analyzed"}


def tool_list_metrics(category=None, search=None) -> dict:
    """The blessed, governed metric catalog (latest version per metric), with its category. Read
    from metric_catalog (not metric_defs) so the shared category/subcategory taxonomy is included —
    use the optional `category` filter to scope to one subject area."""
    with _conn() as c:
        rows = c.execute(
            "SELECT name, description, params, grain, check_sql IS NOT NULL AS has_check, version, "
            "category, subcategory "
            "FROM rvbbit.metric_catalog "
            "WHERE (%s::text IS NULL OR description ILIKE '%%'||%s::text||'%%' OR name ILIKE '%%'||%s::text||'%%') "
            "  AND (%s::text IS NULL OR category = %s::text) "
            "ORDER BY name",
            (search, search, search, category, category),
        ).fetchall()
    return {"metrics": rows}


def tool_get_metric(name: str) -> dict:
    """One metric's definition (with category/subcategory) + version history."""
    with _conn() as c:
        d = c.execute(
            "SELECT name, description, params, grain, sql AS definition_sql, check_sql, "
            "category, subcategory "
            "FROM rvbbit.metric_catalog WHERE name=%s", (name,)
        ).fetchone()
        if not d:
            return {"error": {"code": "METRIC_NOT_FOUND", "message": name}}
        d["versions"] = c.execute(
            "SELECT version, created_at FROM rvbbit.metric_defs WHERE name=%s ORDER BY version DESC",
            (name,)).fetchall()
    return d


def tool_list_cubes(category=None) -> dict:
    """Curated subject-area tables (cubes) — wide, documented, accelerated, with their category. The
    agent's entry point: look here (and at metrics) before raw tables. Optional `category` filter."""
    with _conn() as c:
        rows = c.execute(
            "SELECT name, grain, description, category, version, refreshed_at::text AS refreshed_at, rows "
            "FROM rvbbit.cubes() WHERE (%s::text IS NULL OR category = %s::text)",
            (category, category)).fetchall()
    return {"cubes": rows}


def tool_set_category(kind, name, category=None, subcategory=None) -> dict:
    """Categorize a cube or metric (kind = 'cube' | 'metric') in the shared taxonomy — lightweight
    and mutable (no new version). Pass category=null to clear it. Use this to organize the catalog;
    read it back via list_cubes / list_metrics."""
    with _conn() as c:
        try:
            c.execute("SELECT rvbbit.set_category(%s, %s, %s, %s)", (kind, name, category, subcategory))
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "SET_CATEGORY_FAILED", "message": str(e)}}
    return {"kind": kind, "name": name, "category": category, "subcategory": subcategory}


def tool_describe_cube(name: str) -> dict:
    """A cube's grain, columns, freshness + definition SQL (the agent's grounding to query it)."""
    with _conn(read_only=True, role=_session_pg_role()) as c:
        d = c.execute("SELECT rvbbit.describe_cube(%s) AS d", (name,)).fetchone()
        value = d["d"] if (d and d["d"] is not None) else None
        if value:
            dimensions = c.execute(
                "SELECT column_name,data_type,kind,groupable,distinct_est,semantics "
                "FROM rvbbit.cube_dimensions(%s)",
                (name,),
            ).fetchall()
            by_name = {str(row["column_name"]): dict(row) for row in dimensions}
            columns = value.get("columns")
            if isinstance(columns, list):
                enriched = []
                for column in columns:
                    if not isinstance(column, dict):
                        enriched.append(column)
                        continue
                    column_name = str(column.get("name") or "")
                    data_type = str(
                        column.get("type") or column.get("data_type") or ""
                    ).lower()
                    detail = by_name.get(column_name, {})
                    numeric = data_type in _CUBE_NUMERIC_TYPES
                    fallback_kind = _cube_fallback_kind(column_name, data_type)
                    enriched.append({
                        **column,
                        **detail,
                        "kind": detail.get("kind") or fallback_kind,
                        "groupable": detail.get("groupable")
                        if detail.get("groupable") is not None
                        else fallback_kind != "measure",
                        "numeric": numeric,
                    })
                value["columns"] = enriched
    return value if value is not None else {"error": {"code": "CUBE_NOT_FOUND", "message": name}}


def tool_propose_cube(subject: str, seed_tables=None, schema=None) -> dict:
    """Draft a candidate cube for a subject — a documented join over your tables. Returns a DRAFT
    only (name, sql, grain, description, source_tables, join_rationale, confidence + the FK edges
    it reasoned from); NOTHING is created. The draft is LOGGED to a review queue (returns its
    proposal_id) so a human can bless it in the lens Cube Proposals inbox (or define_cube on the
    primary). Propose freely — good ideas are captured for review, not lost. Pass seed_tables
    (schema.table list) to pin the join, or a schema to scope discovery."""
    with _conn() as c:
        try:
            row = c.execute(
                "SELECT rvbbit.propose_cube(%s, %s::text[], %s) AS d",
                (subject, seed_tables, schema)).fetchone()
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "PROPOSE_FAILED", "message": str(e)}}
        draft = row["d"] if row else None
        if not draft:
            return {"error": {"code": "PROPOSE_FAILED", "message": "no draft"}}
        # self-validate (dry-run) so a hallucinated column never reaches the user
        ok_v, samp, verr = _validate_draft(c, draft["sql"])
        draft = {**draft, "subject": subject, "validated": ok_v}
        if ok_v:
            draft["sample"] = samp
        else:
            draft["validation_error"] = verr
        # Log the draft to the review queue (best-effort: a read-only mirror just skips it).
        try:
            pid = c.execute(
                "SELECT rvbbit.record_proposal('cube', %s::jsonb, 'mcp', 'mcp') AS id",
                (json.dumps(draft),)).fetchone()
            if pid and pid["id"] is not None:
                draft = {**draft, "proposal_id": pid["id"]}
        except Exception:  # noqa: BLE001
            pass
    return draft


def _validate_draft(c, sql) -> tuple:
    """Dry-run a draft SELECT (LIMIT 3): proves it executes against the real schema (catching
    hallucinated columns) and returns a small sample. Autocommit conn → a failure doesn't poison it."""
    try:
        rows = c.execute(f"SELECT to_jsonb(_v) AS r FROM ({sql}) _v LIMIT 3").fetchall()
        return True, [r["r"] for r in rows], None
    except Exception as e:  # noqa: BLE001
        return False, None, str(e)


def tool_propose_metric(subject: str, seed_sources=None, schema=None) -> dict:
    """Draft a candidate metric for a subject — a small, governed aggregation, PREFERRING a cube as
    its source. Returns a DRAFT only (name, sql, grain, description, params, optional KPI check_sql,
    source, confidence) plus validated/sample from a dry-run; NOTHING is created. The draft is LOGGED
    to the review queue (returns its proposal_id) so a human can bless it in the lens Proposals inbox
    (→ define_metric). Propose freely. Pass seed_sources (cubes.x / schema.table list) or a schema."""
    with _conn() as c:
        try:
            row = c.execute(
                "SELECT rvbbit.propose_metric(%s, %s::text[], %s) AS d",
                (subject, seed_sources, schema)).fetchone()
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "PROPOSE_FAILED", "message": str(e)}}
        draft = row["d"] if row else None
        if not draft:
            return {"error": {"code": "PROPOSE_FAILED", "message": "no draft"}}
        # self-validate: resolve {param} then dry-run, so a hallucinated column is caught here
        try:
            resolved = c.execute("SELECT rvbbit.preview_metric_sql(%s, %s::jsonb) AS s",
                                 (draft["sql"], json.dumps(draft.get("params") or {}))).fetchone()["s"]
        except Exception:  # noqa: BLE001
            resolved = draft["sql"]
        ok_v, samp, verr = _validate_draft(c, resolved or draft["sql"])
        draft = {**draft, "subject": subject, "validated": ok_v}
        if ok_v:
            draft["sample"] = samp
        else:
            draft["validation_error"] = verr
        try:
            pid = c.execute(
                "SELECT rvbbit.record_proposal('metric', %s::jsonb, 'mcp', 'mcp') AS id",
                (json.dumps(draft),)).fetchone()
            if pid and pid["id"] is not None:
                draft = {**draft, "proposal_id": pid["id"]}
        except Exception:  # noqa: BLE001
            pass
    return draft


# ── proposal queue: see your drafts' fate + iterate on pending ones ──────────

def tool_list_proposals(status=None, kind=None, proposed_by=None, limit=20) -> dict:
    """See the proposal queue — drafts (yours or others') and their fate. Filter by status
    (pending/accepted/rejected/withdrawn), kind (cube/metric), or proposed_by. ACCEPTED proposals
    carry result_name (the object created); REJECTED/WITHDRAWN carry notes (the reason). Use this to
    LEARN from feedback before proposing again — don't re-propose something already rejected, and
    refine_proposal a pending draft instead of submitting a duplicate."""
    lim = max(1, min(int(limit or 20), 100))
    with _conn() as c:
        rows = c.execute(
            "SELECT proposal_id, kind, name, subject, status, confidence, "
            "       created_at::text AS created_at, reviewed_at::text AS reviewed_at, result_name, notes "
            "FROM rvbbit.proposals(%s, %s) "
            "WHERE (%s IS NULL OR proposed_by = %s) LIMIT %s",
            (status, kind, proposed_by, proposed_by, lim)).fetchall()
    return {"proposals": rows}


def tool_get_proposal(proposal_id) -> dict:
    """Full detail of one proposal — sql, grain, source_tables, params, check_sql, join_rationale,
    confidence, status, result_name. Use after list_proposals to inspect a specific draft."""
    with _conn() as c:
        row = c.execute(
            "SELECT proposal_id, kind, status, name, subject, sql, grain, description, source_tables, "
            "       fk_edges, join_rationale, confidence, params, check_sql, proposed_by, proposed_via, "
            "       result_name, notes, created_at::text AS created_at, reviewed_at::text AS reviewed_at "
            "FROM rvbbit.proposals WHERE proposal_id = %s", (proposal_id,)).fetchone()
    return row or {"error": {"code": "PROPOSAL_NOT_FOUND", "message": str(proposal_id)}}


def tool_refine_proposal(proposal_id, name=None, sql=None, grain=None, description=None,
                         params=None, check_sql=None, join_rationale=None, confidence=None,
                         category=None, subcategory=None) -> dict:
    """Edit a PENDING proposal in place after seeing feedback — instead of submitting a duplicate.
    Only the fields you pass change. (Cube SQL is plain; metric SQL may use {param} tokens.) Pass
    category/subcategory to (re)file it under a folder before review."""
    # EVERY arg is cast explicitly: psycopg sends a Python int as the narrowest int type (smallint)
    # and a Python float as double precision — neither implicitly casts to the declared bigint/real,
    # which otherwise yields AmbiguousFunction / UndefinedFunction at resolution time.
    with _conn() as c:
        try:
            row = c.execute(
                "SELECT rvbbit.refine_proposal("
                "%s::bigint, %s::text, %s::text, %s::text, %s::text, %s::jsonb, "
                "%s::text, %s::text, %s::real, %s::text, %s::text) AS r",
                (proposal_id, name, sql, grain, description,
                 json.dumps(params) if params is not None else None,
                 check_sql, join_rationale, confidence, category, subcategory)).fetchone()
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "REFINE_FAILED", "message": str(e)}}
    return row["r"] if (row and row["r"] is not None) else {"error": {"code": "REFINE_FAILED", "message": "no result"}}


def tool_withdraw_proposal(proposal_id, reason=None) -> dict:
    """Retract a PENDING proposal you no longer want reviewed (status -> withdrawn)."""
    with _conn() as c:
        try:
            c.execute("SELECT rvbbit.withdraw_proposal(%s, %s)", (proposal_id, reason))
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "WITHDRAW_FAILED", "message": str(e)}}
    return {"status": "withdrawn", "proposal_id": proposal_id}


# ── direct edits (versioned, so reversible) ──────────────────────────────────

def tool_edit_metric(name, sql=None, grain=None, description=None, params=None,
                     check_sql=None, category=None, subcategory=None) -> dict:
    """Edit an existing metric IN PLACE — appends a new version (old versions are kept, so it's
    reversible) that goes LIVE immediately. Only the fields you pass change. check_sql: omit to keep
    the current check, pass "" to remove it. Use this to fix/improve a metric you (or someone) defined."""
    with _conn() as c:
        try:
            row = c.execute(
                "SELECT rvbbit.revise_metric(%s, p_sql=>%s, p_grain=>%s, p_description=>%s, "
                "p_params=>%s::jsonb, p_check_sql=>%s, p_category=>%s, p_subcategory=>%s) AS v",
                (name, sql, grain, description,
                 json.dumps(params) if params is not None else None,
                 check_sql, category, subcategory)).fetchone()
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "EDIT_FAILED", "message": str(e)}}
    return {"metric": name, "version": row["v"] if row else None}


def tool_edit_cube(name, sql, grain=None, description=None, category=None, subcategory=None) -> dict:
    """Edit an existing cube's DEFINITION in place — appends a new version (revert via the prior
    version) that goes LIVE immediately. Shape-aware: a column change rebuilds the cube table, a
    filter-only change preserves its AS-OF history. sql is required (the full new SELECT)."""
    with _conn() as c:
        try:
            row = c.execute(
                "SELECT rvbbit.redefine_cube(%s, %s, p_grain=>%s, p_description=>%s, "
                "p_category=>%s, p_subcategory=>%s) AS v",
                (name, sql, grain, description, category, subcategory)).fetchone()
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "EDIT_FAILED", "message": str(e)}}
    return {"cube": name, "version": row["v"] if row else None}


def tool_metric(name: str, params=None, as_of=None, def_as_of=None, group_by=None) -> dict:
    """A blessed, governed scalar number — bitemporal (as_of = data-time, def_as_of = def-time). Pass
    group_by (a list of cube dimension columns) to slice a DIMENSIONAL metric — one defined over a
    cube (labels.cube_source) — into a breakdown row per group (e.g. group_by=['stage_name']). The
    metric's measures are reused verbatim; dimensions are validated against the cube's real columns.
    Call metric_dimensions(name) to discover which columns are sliceable."""
    params = params or {}
    dims = [d for d in (group_by or []) if d]
    with _conn() as c:
        if as_of:
            c.execute("SET rvbbit.as_of_timestamp = %s", (str(as_of),))
        try:
            if dims:
                rows = c.execute("SELECT rvbbit.metric_by(%s, %s::text[], %s::jsonb) AS m",
                                 (name, dims, json.dumps(params))).fetchall()
            else:
                rows = c.execute(
                    "SELECT rvbbit.metric_scalar(%s, %s::jsonb, coalesce(%s::timestamptz, now()), %s::timestamptz) AS m",
                    (name, json.dumps(params), def_as_of, as_of)).fetchall()
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "METRIC_FAILED", "message": str(e)}}
    vals = [r["m"] for r in rows]
    out = {"name": name, "result": vals[0] if (len(vals) == 1 and not dims) else vals,
           "params": params, "data_as_of": as_of, "def_as_of": def_as_of}
    if dims:
        out["group_by"] = dims
    return out


def tool_metric_dimensions(name: str) -> dict:
    """The cube columns a DIMENSIONAL metric can be sliced by (empty unless it declares labels.cube_source).
    Each entry: column, type, kind (dimension/time/key/measure), groupable. Feed groupable columns to
    metric(name, group_by=[...]) for a breakdown."""
    with _conn() as c:
        rows = c.execute(
            "SELECT column_name, data_type, kind, groupable, distinct_est, semantics "
            "FROM rvbbit.metric_dimensions(%s)", (name,)).fetchall()
    return {"metric": name, "dimensions": rows,
            "groupable": [r["column_name"] for r in rows if r["groupable"]]}


# ── monitoring surface: snapshot, history, breaches, lineage ─────────────────

def tool_materialize_metric(name: str, params=None, as_of=None, def_as_of=None) -> dict:
    """Snapshot a metric NOW into the durable observation log (value + KPI verdict at this instant) —
    the basis for trend history and breach monitoring. Returns the observation id."""
    with _conn() as c:
        try:
            row = c.execute(
                "SELECT rvbbit.materialize_metric(%s, %s::jsonb, coalesce(%s::timestamptz, now()), "
                "%s::timestamptz, NULL, 'mcp') AS id",
                (name, json.dumps(params or {}), def_as_of, as_of)).fetchone()
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "MATERIALIZE_FAILED", "message": str(e)}}
    return {"metric": name, "observation_id": row["id"] if row else None}


def tool_metric_history(name: str, limit: int = 50) -> dict:
    """The durable observation series for a metric (newest first): value, KPI verdict/status, the
    data-time it was taken at, and how it was triggered. Turns a definition into a trend."""
    with _conn() as c:
        rows = c.execute(
            "SELECT observation_id, metric_version, value, verdict, status, trigger, "
            "       data_as_of::text AS data_as_of, observed_at::text AS observed_at "
            "FROM rvbbit.metric_history(%s, %s)", (name, max(1, min(int(limit or 50), 500)))).fetchall()
    return {"metric": name, "observations": rows}


def tool_breaching_kpis() -> dict:
    """Which KPIs are FAILING their target right now — the latest observation per metric where the
    check verdict is false. A monitoring dashboard in one call (materialize metrics first to populate)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT metric_name, status, value, verdict, observed_at::text AS observed_at "
            "FROM rvbbit.breaching_kpis()").fetchall()
    return {"breaching": rows, "count": len(rows)}


def tool_metric_lineage(name: str) -> dict:
    """The base tables a metric reads (for impact analysis) — resolved from its SQL via the planner.
    The metric-side mirror of dashboard_dependents."""
    with _conn() as c:
        row = c.execute("SELECT rvbbit.metric_lineage(%s) AS t", (name,)).fetchone()
    return {"metric": name, "source_tables": (row["t"] if row else None) or []}


# ── alerts: observe + operate + author-conditions (T0+T1) ────────────────────
# Read + control over the durable alert engine (edge-triggered condition->action rules, pg_cron
# sweep+worker). v1 lets an agent see what's firing, operate the controls (enable/mute/cadence/
# kill-switch, manual sweep+worker), and DRY-RUN conditions. Authoring whole rules (define_alert) is
# deferred: open-ended actions (mcp_call/flow) go through the human bless path in the lens, where the
# action form + manifest validation lives.

def tool_list_alerts(category=None, enabled=None, muted=None, tier=None, search=None, limit=50) -> dict:
    """Alert rules with live vitals: each rule's condition/action shape, on/off + mute + cadence tier,
    and current breach/entity/pending counts + last-fired. The agent's entry point for "what's firing
    and why". Filters: category, enabled (bool), muted (bool), tier (fast|normal|slow), search
    (name/description). Also returns the global alerts_enabled kill-switch state."""
    lim = max(1, min(int(limit or 50), 500))
    with _conn() as c:
        rows = c.execute(
            "SELECT c.name, c.condition_spec, c.fire_policy, c.action_spec, c.cardinality, c.fan_out_cap, "
            "c.description, c.enabled, c.muted, c.cadence_tier, c.category, c.subcategory, "
            "c.created_at::text AS created_at, "
            "(SELECT count(*) FROM rvbbit.alert_state s WHERE s.rule_name=c.name AND s.last_status='fail') AS breaching, "
            "(SELECT count(*) FROM rvbbit.alert_state s WHERE s.rule_name=c.name) AS entities, "
            "(SELECT count(*) FROM rvbbit.alert_queue q WHERE q.rule_name=c.name AND q.status='pending') AS pending, "
            "(SELECT max(s.last_fired_at)::text FROM rvbbit.alert_state s WHERE s.rule_name=c.name) AS last_fired "
            "FROM rvbbit.alert_catalog c "
            "WHERE (%s::text IS NULL OR c.category=%s::text) "
            "  AND (%s::bool IS NULL OR c.enabled=%s::bool) "
            "  AND (%s::bool IS NULL OR c.muted=%s::bool) "
            "  AND (%s::text IS NULL OR c.cadence_tier=%s::text) "
            "  AND (%s::text IS NULL OR c.name ILIKE '%%'||%s::text||'%%' OR c.description ILIKE '%%'||%s::text||'%%') "
            "ORDER BY c.name LIMIT %s",
            (category, category, enabled, enabled, muted, muted, tier, tier, search, search, search, lim)).fetchall()
        on = c.execute("SELECT rvbbit.alerts_enabled() AS on").fetchone()["on"]
    return {"alerts": rows, "alerts_enabled": on, "count": len(rows)}


def tool_get_alert(name) -> dict:
    """One alert rule in full: latest-version condition_spec/action_spec/fire_policy, cardinality,
    fan-out cap, control state (enabled/muted/cadence), category, version history, a live state
    summary (breaching/entities/pending, last fired), and its most recent firing events."""
    with _conn() as c:
        d = c.execute(
            "SELECT name, version, condition_spec, fire_policy, action_spec, cardinality, fan_out_cap, "
            "description, owner, labels, enabled, muted, muted_until::text AS muted_until, cadence_tier, "
            "category, subcategory, created_at::text AS created_at FROM rvbbit.alert_catalog WHERE name=%s",
            (name,)).fetchone()
        if not d:
            return {"error": {"code": "ALERT_NOT_FOUND", "message": name}}
        d["state"] = c.execute(
            "SELECT count(*) FILTER (WHERE last_status='fail') AS breaching, count(*) AS entities, "
            "max(last_fired_at)::text AS last_fired FROM rvbbit.alert_state WHERE rule_name=%s",
            (name,)).fetchone()
        d["pending"] = c.execute(
            "SELECT count(*) AS n FROM rvbbit.alert_queue WHERE rule_name=%s AND status='pending'",
            (name,)).fetchone()["n"]
        d["recent_events"] = c.execute(
            "SELECT entity_key, transition, status, error, ts::text AS ts FROM rvbbit.alert_events "
            "WHERE rule_name=%s ORDER BY ts DESC LIMIT 5", (name,)).fetchall()
        d["versions"] = c.execute(
            "SELECT version, created_at::text AS created_at FROM rvbbit.alert_rules WHERE name=%s "
            "ORDER BY version DESC", (name,)).fetchall()
    return d


def tool_alert_state(name, limit=200) -> dict:
    """Per-entity reconciler state for a rule: entity_key, last_status (pass|fail), score, consecutive
    fail count, and when it last changed/fired — the breakdown behind a rule's breach count."""
    lim = max(1, min(int(limit or 200), 1000))
    with _conn() as c:
        rows = c.execute(
            "SELECT entity_key, last_status, score, consecutive, last_changed_at::text AS last_changed_at, "
            "last_fired_at::text AS last_fired_at FROM rvbbit.alert_state WHERE rule_name=%s "
            "ORDER BY (score IS NULL), score DESC NULLS LAST, entity_key LIMIT %s", (name, lim)).fetchall()
    return {"alert": name, "entities": rows, "count": len(rows)}


def tool_alert_events(name=None, limit=50) -> dict:
    """The firing audit log (newest first): which rule+entity fired, the transition, fired/failed
    status, the action output or error, and when. Pass name to scope to one rule."""
    lim = max(1, min(int(limit or 50), 500))
    with _conn() as c:
        rows = c.execute(
            "SELECT rule_name, entity_key, transition, status, action_output, error, ts::text AS ts "
            "FROM rvbbit.alert_events WHERE (%s::text IS NULL OR rule_name=%s::text) "
            "ORDER BY ts DESC LIMIT %s", (name, name, lim)).fetchall()
    return {"events": rows, "count": len(rows)}


def tool_alert_sweep_runs(limit=40) -> dict:
    """The sweep heartbeat (newest first): per tick — tier, start/finish, rules evaluated,
    transitions, enqueued, errors. Use it to confirm the reconciler is alive and see its rate."""
    lim = max(1, min(int(limit or 40), 500))
    with _conn() as c:
        rows = c.execute(
            "SELECT sweep_id, tier, started_at::text AS started_at, finished_at::text AS finished_at, "
            "rules_evaluated, transitions, enqueued, errors FROM rvbbit.alert_sweep_runs "
            "ORDER BY started_at DESC LIMIT %s", (lim,)).fetchall()
    return {"sweeps": rows, "count": len(rows)}


def tool_breaching_alerts() -> dict:
    """Which alerts are FAILING right now — the scalar analog of breaching_kpis. Per rule currently in
    fail state: how many entities are breaching, the worst (max) score, when it last fired, plus its
    enabled/muted/tier so you can tell real fires from silenced ones."""
    with _conn() as c:
        rows = c.execute(
            "SELECT c.name, c.cadence_tier, c.enabled, c.muted, c.category, "
            "count(*) AS breaching_entities, max(s.score) AS worst_score, "
            "max(s.last_fired_at)::text AS last_fired "
            "FROM rvbbit.alert_state s JOIN rvbbit.alert_catalog c ON c.name=s.rule_name "
            "WHERE s.last_status='fail' "
            "GROUP BY c.name, c.cadence_tier, c.enabled, c.muted, c.category "
            "ORDER BY breaching_entities DESC, c.name").fetchall()
    return {"breaching": rows, "count": len(rows)}


def tool_set_alert_enabled(name, enabled) -> dict:
    """Enable or disable a rule (control flag; survives re-definition). Disabled rules are skipped by
    the sweep. The non-destructive on/off — use this (or mute) to silence a noisy alert, not delete."""
    fn = "enable_alert" if enabled else "disable_alert"   # fixed literals, not user input
    with _conn() as c:
        try:
            c.execute(f"SELECT rvbbit.{fn}(%s)", (name,))
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "ALERT_CONTROL_FAILED", "message": str(e)}}
    return {"alert": name, "enabled": bool(enabled)}


def tool_mute_alert(name, minutes=None) -> dict:
    """Temporarily silence a rule's ACTIONS without stopping evaluation. minutes=None mutes
    indefinitely (until unmuted); otherwise for that many minutes. Returns the muted_until."""
    with _conn() as c:
        try:
            if minutes is None:
                row = c.execute("SELECT rvbbit.mute_alert(%s)::text AS until", (name,)).fetchone()
            else:
                row = c.execute("SELECT rvbbit.mute_alert(%s, make_interval(mins => %s))::text AS until",
                                (name, int(minutes))).fetchone()
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "MUTE_FAILED", "message": str(e)}}
    return {"alert": name, "muted_until": row["until"] if row else None}


def tool_unmute_alert(name) -> dict:
    """Clear a rule's mute (resume its actions)."""
    with _conn() as c:
        try:
            c.execute("SELECT rvbbit.unmute_alert(%s)", (name,))
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "UNMUTE_FAILED", "message": str(e)}}
    return {"alert": name, "muted": False}


def tool_set_alert_cadence(name, tier) -> dict:
    """Move a rule to a sweep tier: 'fast' (~1m), 'normal' (~15m), or 'slow' (~hourly)."""
    if tier not in ("fast", "normal", "slow"):
        return {"error": {"code": "BAD_TIER", "message": "tier must be fast|normal|slow"}}
    with _conn() as c:
        try:
            c.execute("SELECT rvbbit.set_alert_cadence(%s, %s)", (name, tier))
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "CADENCE_FAILED", "message": str(e)}}
    return {"alert": name, "cadence_tier": tier}


def tool_set_alerts_enabled(on) -> dict:
    """The GLOBAL alerts kill-switch. on=false pauses ALL sweeps + actions at once (the circuit
    breaker); on=true resumes. Returns the new state. Pairs with the alerts_enabled flag in list_alerts."""
    with _conn() as c:
        try:
            row = c.execute("SELECT rvbbit.set_alerts_enabled(%s) AS on", (bool(on),)).fetchone()
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "KILLSWITCH_FAILED", "message": str(e)}}
    return {"alerts_enabled": row["on"] if row else None}


def tool_run_alert_sweep(tier="normal") -> dict:
    """Run one reconciler sweep NOW for a tier (fast|normal|slow) instead of waiting for cron —
    evaluates conditions, diffs state, enqueues transitions. Returns the sweep summary. Pair with
    run_alert_worker to actually dispatch what it enqueues."""
    if tier not in ("fast", "normal", "slow"):
        return {"error": {"code": "BAD_TIER", "message": "tier must be fast|normal|slow"}}
    with _conn() as c:
        try:
            row = c.execute("SELECT rvbbit.alert_sweep(%s) AS j", (tier,)).fetchone()
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "SWEEP_FAILED", "message": str(e)}}
    return {"tier": tier, "summary": row["j"] if row else None}


def tool_run_alert_worker(max_items=50) -> dict:
    """Drain up to max_items from the action queue NOW (dispatch pending alert actions) instead of
    waiting for cron. Fire-and-forget; results land in alert_events. Returns the drain summary."""
    n = max(1, min(int(max_items or 50), 500))
    with _conn() as c:
        try:
            row = c.execute("SELECT rvbbit.alert_worker_tick(%s) AS j", (n,)).fetchone()
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "WORKER_FAILED", "message": str(e)}}
    return {"max": n, "summary": row["j"] if row else None}


def tool_preview_alert_condition(query, expr=None) -> dict:
    """Dry-run an alert CONDITION read-only — the observable feedback for authoring a rule before any
    rule exists. Runs the query (LIMIT 500) and returns its (entity_key, score, status) rows + counts.
    If expr is given, wraps the query in the same CASE the sweep uses (status='fail' when expr true)
    so a bad boolean expr surfaces as an error here. A condition query should return an entity_key
    column plus EITHER a status ('pass'/'fail') OR a numeric score. Read-only: writes are blocked."""
    trimmed = (query or "").strip().rstrip(";").strip()
    if not trimmed:
        return {"error": {"code": "EMPTY_QUERY", "message": "query is required"}}
    e = (expr or "").strip()
    inner = (f"SELECT q2.*, CASE WHEN ({e}) THEN 'fail' ELSE 'pass' END AS _alert_status FROM ({trimmed}) q2"
             if e else trimmed)
    try:
        with _ro() as c:   # read-only txn + statement timeout: agent-supplied SQL cannot write
            rows = c.execute(f"SELECT to_jsonb(q) AS j FROM ({inner}) q LIMIT 500").fetchall()
    except Exception as ex:  # noqa: BLE001
        return {"valid": False, "error": str(ex)}
    out = []
    for r in rows:
        j = r["j"] or {}
        out.append({
            "entity_key": j["entity_key"] if j.get("entity_key") is not None else "",
            "score": j.get("score"),
            "status": (j.get("_alert_status") if e else j.get("status")),
        })
    breaching = sum(1 for x in out if str(x["status"]).lower() == "fail")
    return {"valid": True, "rows": out, "count": len(out), "breaching": breaching,
            "columns": list((rows[0]["j"] or {}).keys()) if rows else []}


def tool_preview_metric_observation(metric) -> dict:
    """The latest materialized observation for a metric — exactly what a metric-kind condition reads
    (status pass/fail, value, verdict, data-time). Check this before wiring an alert onto a metric."""
    with _ro() as c:
        row = c.execute(
            "SELECT status, data_as_of::text AS data_as_of, value, verdict FROM rvbbit.metric_observations "
            "WHERE metric_name=%s ORDER BY data_as_of DESC NULLS LAST, observed_at DESC LIMIT 1",
            (metric,)).fetchone()
    if not row:
        return {"metric": metric, "observation": None,
                "note": "no materialized observation yet — run materialize_metric first"}
    return {"metric": metric, "observation": row}


# ── consumer verbs: opinionated, pre-shaped business views ────────────────────
# The MCP can't render UI, so these return data ALREADY shaped (rows/cols/totals) plus an explicit
# `render` instruction the agent follows — a "pre-baked opinion" on how to look at the numbers, not a
# raw crosstab. All thin compositions over the blessed metric / metric_by / observation log.

_GRAINS = {"day", "week", "month", "quarter", "year"}


def _isnum(v):
    """A real number (int/float/Decimal) — NOT a bool (which is an int subclass in Python)."""
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def _metric_scalar(v, prefer=None):
    """Pull a representative number out of a metric row (dict), a stored value ([{...}] list), or a
    bare number. Prefers `prefer`, then a 'value' key, then the first numeric field."""
    if v is None or isinstance(v, bool):
        return None
    if _isnum(v):
        return float(v)
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        for k in ([prefer] if prefer else []) + ["value"]:
            if k and _isnum(v.get(k)):
                return float(v[k])
        for val in v.values():
            if _isnum(val):
                return float(val)
    return None


def _measure_order(c, metric):
    """The metric's measure aliases in DEFINITION order (its select-list) — the first is the headline
    measure. jsonb reorders keys by (length, alpha), so without this the 'first numeric' would be
    arbitrary; this restores the author's intended order."""
    row = c.execute(
        "SELECT sql FROM rvbbit.metric_defs WHERE name=%s ORDER BY created_at DESC, version DESC LIMIT 1",
        (metric,)).fetchone()
    if not row or not row.get("sql"):
        return []
    mobj = re.match(r"(?is)^\s*select\s+(.*?)\s+from\s", row["sql"])
    if not mobj:
        return []
    parts, depth, cur = [], 0, ""
    for ch in mobj.group(1):
        if ch == "(":
            depth += 1; cur += ch
        elif ch == ")":
            depth -= 1; cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur); cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    out = []
    for p in parts:
        p = p.strip()
        am = re.search(r'(?is)\bas\s+"?([a-z_][a-z0-9_]*)"?\s*$', p) or re.search(r"([a-z_][a-z0-9_]*)\s*$", p, re.I)
        if am:
            out.append(am.group(1).lower())
    return out


def _pick_measures(c, metric, sample, exclude):
    """Numeric measure columns of a metric row, ranked by the metric's definition order (headline first)."""
    numeric = [k for k, v in sample.items() if k not in exclude and _isnum(v)]
    order = _measure_order(c, metric)
    return sorted(numeric, key=lambda k: (order.index(k) if k in order else 999, k))


def tool_scoreboard(category=None, grain="month", periods=6, as_of=None) -> dict:
    """The executive KPI matrix: every blessed metric laid out as category › subcategory (left axis) ×
    time periods (top axis), each cell = the metric's value that period, with its latest target verdict
    and a trend. Reads the materialized observation log (the governed history — no recompute). Filter by
    category; grain = day|week|month|quarter|year; periods = how many columns back. The 'how are we
    doing?' view — render it as one opinionated matrix, not a flat list."""
    if grain not in _GRAINS:
        return {"error": {"code": "BAD_GRAIN", "message": f"grain must be one of {sorted(_GRAINS)}"}}
    n = max(1, min(int(periods or 6), 36))
    step = f"1 {grain}"
    with _ro() as c:
        axis = [r["period"] for r in c.execute(
            "SELECT to_char(b,'YYYY-MM-DD') AS period FROM generate_series("
            "date_trunc(%s, coalesce(%s::timestamptz, now())) - ((%s - 1) * (%s)::interval), "
            "date_trunc(%s, coalesce(%s::timestamptz, now())), (%s)::interval) b ORDER BY b",
            (grain, as_of, n, step, grain, as_of, step)).fetchall()]
        long = c.execute(
            "WITH obs AS (SELECT o.metric_name, "
            "  to_char(date_trunc(%s, coalesce(o.data_as_of,o.observed_at)),'YYYY-MM-DD') AS bucket, "
            "  o.value, o.status, row_number() OVER (PARTITION BY o.metric_name, "
            "    date_trunc(%s, coalesce(o.data_as_of,o.observed_at)) ORDER BY o.observed_at DESC) AS rn "
            "  FROM rvbbit.metric_observations o) "
            "SELECT m.name, m.category, m.subcategory, m.description, m.grain AS metric_grain, "
            "       obs.bucket, obs.value, obs.status "
            "FROM rvbbit.metric_catalog m LEFT JOIN obs ON obs.metric_name=m.name AND obs.rn=1 "
            "WHERE (%s::text IS NULL OR m.category=%s::text) "
            "ORDER BY m.category NULLS LAST, m.subcategory NULLS LAST, m.name",
            (grain, grain, category, category)).fetchall()
    axis_set = set(axis)
    metrics, order = {}, []
    for r in long:
        nm = r["name"]
        if nm not in metrics:
            metrics[nm] = {"category": r["category"], "subcategory": r["subcategory"],
                           "description": r["description"], "metric_grain": r["metric_grain"], "cells": {}}
            order.append(nm)
        b = r["bucket"]
        if b and b in axis_set:
            metrics[nm]["cells"][b] = {"value": _metric_scalar(r["value"]), "status": r["status"]}
    groups = {}
    for nm in order:
        md = metrics[nm]
        series = [(md["cells"].get(p) or {}).get("value") for p in axis]
        present = [md["cells"][p] for p in axis if p in md["cells"]]
        latest = present[-1] if present else {}
        nn = [v for v in series if v is not None]
        trend = None
        if len(nn) >= 2:
            trend = "up" if nn[-1] > nn[0] else ("down" if nn[-1] < nn[0] else "flat")
        key = (md["category"] or "Uncategorized", md["subcategory"] or "")
        groups.setdefault(key, []).append({
            "name": nm, "description": md["description"], "metric_grain": md["metric_grain"],
            "cells": series, "latest": latest.get("value"), "status": latest.get("status"), "trend": trend})
    out_groups = [{"category": k[0], "subcategory": k[1], "metrics": v}
                  for k, v in sorted(groups.items())]
    return {"grain": grain, "periods": axis, "groups": out_groups,
            "render": {"as": "kpi_matrix",
                       "note": "Render as ONE matrix: left axis = category › subcategory › metric (grouped with "
                               "subcategory subheaders, indented), top axis = the periods (oldest left → newest "
                               "right), each cell = the value. On the latest cell append a ▲/▼/– from trend and a "
                               "✓/✗ from status. Right-align numbers. This is an executive scoreboard, not a list."}}


def tool_pivot(metric, rows, cols, measure=None, params=None, as_of=None) -> dict:
    """A governed crosstab of a DIMENSIONAL metric: rows (a cube dimension) × cols (a cube dimension) ×
    one measure, with row/column/grand totals. Reshapes metric_by into a matrix — values are the blessed
    metric's, dimensions are validated against the cube — so it's a repeatable pivot table, not hand-rolled
    SQL. measure defaults to the metric's first numeric measure. Call metric_dimensions(metric) to see
    the sliceable columns. Render as a matrix."""
    if not rows or not cols:
        return {"error": {"code": "BAD_AXES", "message": "rows and cols are required cube dimensions"}}
    available = []
    with _conn() as c:
        try:
            recs = c.execute(
                "SELECT rvbbit.metric_by(%s, %s::text[], %s::jsonb, now(), %s::timestamptz) AS m",
                (metric, [rows, cols], json.dumps(params or {}), as_of)).fetchall()
            longrows = [r["m"] for r in recs if r["m"] is not None]
            if longrows:
                available = _pick_measures(c, metric, longrows[0], {rows, cols})
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "PIVOT_FAILED", "message": str(e)}}
    if not longrows:
        return {"metric": metric, "rows_dim": rows, "cols_dim": cols, "matrix": [], "note": "no rows"}
    if measure is None:
        measure = available[0] if available else None        # headline measure (definition order)
    elif measure not in available:
        return {"error": {"code": "BAD_MEASURE",
                          "message": f"'{measure}' is not a numeric measure; available: {available}"}}
    if measure is None:
        return {"error": {"code": "NO_MEASURE",
                          "message": f"no numeric measure found in {sorted(longrows[0].keys())}; pass measure="}}
    cells, row_vals, col_vals = {}, [], []
    for rec in longrows:
        rv = "" if rec.get(rows) is None else str(rec.get(rows))
        cv = "" if rec.get(cols) is None else str(rec.get(cols))
        if rv not in cells:
            cells[rv] = {}
            row_vals.append(rv)
        if cv not in col_vals:
            col_vals.append(cv)
        cells[rv][cv] = rec.get(measure)
    row_vals.sort()
    col_vals.sort()
    col_tot = {cv: 0.0 for cv in col_vals}
    grand = 0.0
    matrix = []
    for rv in row_vals:
        rc = {cv: cells[rv].get(cv) for cv in col_vals}
        rtot = 0.0
        for cv in col_vals:
            x = rc[cv]
            if _isnum(x):
                rtot += x
                col_tot[cv] += x
        grand += rtot
        matrix.append({"row": rv, "cells": rc, "total": rtot})
    return {"metric": metric, "rows_dim": rows, "cols_dim": cols, "measure": measure,
            "available_measures": available, "columns": col_vals, "matrix": matrix,
            "col_totals": col_tot, "grand_total": grand,
            "render": {"as": "pivot",
                       "note": f"Render as a matrix: '{rows}' down the left, '{cols}' across the top, cell = "
                               f"{measure}. Right-align numbers; add the per-row 'total' column and the col_totals "
                               f"row (with grand_total in the corner); bold totals. Other measures available: "
                               f"{[a for a in available if a != measure]} (re-call pivot with measure= to switch)."}}


_CUBE_NUMERIC_TYPES = {
    "bigint",
    "decimal",
    "double precision",
    "integer",
    "money",
    "numeric",
    "real",
    "smallint",
}
_CUBE_PIVOT_AGGREGATES = ("sum", "avg", "min", "max", "count", "count_distinct")


def _cube_fallback_kind(name: str, data_type: str) -> str:
    lowered = str(name or "").lower()
    if lowered == "id" or lowered.endswith("_id"):
        return "key"
    if data_type in {
        "date",
        "time without time zone",
        "timestamp with time zone",
        "timestamp without time zone",
    }:
        return "time"
    return "measure" if data_type in _CUBE_NUMERIC_TYPES else "dimension"


def _cube_pivot_aggregate(aggregate: str, measure: str | None):
    """Build an aggregate expression from a fixed verb allowlist + validated identifier."""
    if aggregate == "count" and measure is None:
        return pgsql.SQL("count(*)")
    if measure is None:
        raise ValueError(f"{aggregate} requires a numeric measure")
    ident = pgsql.Identifier(measure)
    if aggregate == "count_distinct":
        return pgsql.SQL("count(DISTINCT {})").format(ident)
    return pgsql.SQL("{}({})").format(pgsql.SQL(aggregate), ident)


def _cube_pivot_label(value) -> str:
    return "(blank)" if value is None else str(value)


def _cube_field_list(value, maximum: int = 8) -> list[str]:
    raw = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for item in raw:
        name = str(item or "").strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= maximum:
            break
    return out


def _cube_measure_list(measures, measure, aggregate) -> tuple[list[dict], dict | None]:
    if measures is None:
        raw = [{"field": measure, "aggregate": aggregate}]
    elif isinstance(measures, (list, tuple)):
        raw = list(measures)
    else:
        raw = [measures]
    out = []
    for item in raw[:8]:
        if isinstance(item, dict):
            field = item.get("field", item.get("measure", item.get("name")))
            agg = item.get("aggregate", item.get("agg", "sum"))
        else:
            field, agg = item, "sum"
        field = str(field or "").strip() or None
        if field in {"*", "__rows__"}:
            field = None
        agg = str(agg or "sum").strip().lower()
        if agg not in _CUBE_PIVOT_AGGREGATES:
            return [], {
                "code": "BAD_AGGREGATE",
                "message": (
                    f"'{agg}' is not supported; available: "
                    f"{list(_CUBE_PIVOT_AGGREGATES)}"
                ),
            }
        if field is None and agg != "count":
            return [], {
                "code": "BAD_MEASURE",
                "message": f"{agg} requires a numeric cube field",
            }
        key = f"{agg}:{field or '__rows__'}"
        if any(spec["key"] == key for spec in out):
            continue
        out.append({
            "key": key,
            "field": field,
            "aggregate": agg,
            "label": f"{agg.replace('_', ' ').upper()} {field or 'rows'}",
            "alias": f"__m{len(out)}",
        })
    if not out:
        return [], {
            "code": "BAD_MEASURE",
            "message": "choose at least one numeric value or row count",
        }
    return out, None


def _cube_group_query(relation, dimensions: list[str], measures: list[dict], limit: bool):
    select_parts = [pgsql.Identifier(name) for name in dimensions]
    select_parts.extend(
        pgsql.SQL("{} AS {}").format(
            _cube_pivot_aggregate(spec["aggregate"], spec["field"]),
            pgsql.Identifier(spec["alias"]),
        )
        for spec in measures
    )
    query = pgsql.SQL("SELECT {} FROM {}").format(
        pgsql.SQL(",").join(select_parts),
        relation,
    )
    if dimensions:
        ordinals = [pgsql.SQL(str(index + 1)) for index in range(len(dimensions))]
        ordered = [
            pgsql.SQL("{} NULLS LAST").format(ordinal)
            for ordinal in ordinals
        ]
        query += pgsql.SQL(" GROUP BY {} ORDER BY {}").format(
            pgsql.SQL(",").join(ordinals),
            pgsql.SQL(",").join(ordered),
        )
    if limit:
        query += pgsql.SQL(" LIMIT %s")
    return query


def _cube_dimension_values(row, dimensions: list[str]) -> dict[str, str]:
    return {name: _cube_pivot_label(row.get(name)) for name in dimensions}


def _cube_dimension_key(row, dimensions: list[str]) -> str:
    return json.dumps(
        [_cube_pivot_label(row.get(name)) for name in dimensions],
        separators=(",", ":"),
    )


def tool_cube_pivot(
    cube,
    rows,
    cols=None,
    measure=None,
    aggregate="sum",
    measures=None,
) -> dict:
    """Explore a cube directly as a grouped table or crosstab, without requiring a metric.

    ``rows`` and optional ``cols`` accept one field or a list of fields.
    ``measures`` accepts ``[{field, aggregate}, ...]`` and supersedes the legacy
    singular ``measure``/``aggregate`` pair. With no column dimensions the
    result is an ordinary grouped table. Adding column dimensions produces a
    crosstab. Every relation and field is validated and quoted.
    """
    cube = str(cube or "").strip()
    row_dimensions = _cube_field_list(rows)
    column_dimensions = _cube_field_list(cols)
    measure_specs, measure_error = _cube_measure_list(
        measures,
        measure,
        aggregate,
    )
    if not cube:
        return {
            "error": {
                "code": "BAD_AXES",
                "message": "cube is required",
            }
        }
    if measure_error:
        return {"error": measure_error}
    overlap = sorted(set(row_dimensions) & set(column_dimensions))
    if overlap:
        return {
            "error": {
                "code": "BAD_AXES",
                "message": f"fields cannot be on both Rows and Columns: {overlap}",
            }
        }

    try:
        with _conn(read_only=True, role=_session_pg_role()) as c:
            found = c.execute(
                "SELECT name FROM rvbbit.cube_catalog WHERE name=%s",
                (cube,),
            ).fetchone()
            if not found:
                return {"error": {"code": "CUBE_NOT_FOUND", "message": cube}}

            column_rows = c.execute(
                "SELECT column_name,data_type FROM information_schema.columns "
                "WHERE table_schema='cubes' AND table_name=%s ORDER BY ordinal_position",
                (cube,),
            ).fetchall()
            if not column_rows:
                return {
                    "error": {
                        "code": "CUBE_UNAVAILABLE",
                        "message": f"cubes.{cube} is not materialized or is not visible",
                    }
                }
            dimension_rows = c.execute(
                "SELECT column_name,data_type,kind,groupable,distinct_est,semantics "
                "FROM rvbbit.cube_dimensions(%s)",
                (cube,),
            ).fetchall()
            dimensions = {str(row["column_name"]): dict(row) for row in dimension_rows}
            fields = []
            for column in column_rows:
                name = str(column["column_name"])
                data_type = str(column["data_type"] or "").lower()
                detail = dimensions.get(name, {})
                fallback_kind = _cube_fallback_kind(name, data_type)
                fields.append({
                    "name": name,
                    "type": data_type,
                    "kind": detail.get("kind") or fallback_kind,
                    "groupable": bool(detail.get(
                        "groupable",
                        fallback_kind != "measure",
                    )),
                    "numeric": data_type in _CUBE_NUMERIC_TYPES,
                    "distinct_est": detail.get("distinct_est"),
                    "semantics": detail.get("semantics"),
                })
            by_name = {field["name"]: field for field in fields}
            available_dimensions = [
                field["name"] for field in fields if field["groupable"]
            ]
            available_measures = [
                field["name"] for field in fields if field["numeric"]
            ]
            bad_axes = [
                axis for axis in row_dimensions + column_dimensions
                if axis not in available_dimensions
            ]
            if bad_axes:
                return {
                    "error": {
                        "code": "BAD_AXES",
                        "message": (
                            f"not groupable: {bad_axes}; available: "
                            f"{available_dimensions}"
                        ),
                    }
                }
            bad_measures = sorted({
                spec["field"]
                for spec in measure_specs
                if spec["field"] is not None
                and (
                    spec["field"] not in by_name
                    or not by_name[spec["field"]]["numeric"]
                )
            })
            if bad_measures:
                return {
                    "error": {
                        "code": "BAD_MEASURE",
                        "message": (
                            f"not numeric: {bad_measures}; available: "
                            f"{available_measures}"
                        ),
                    }
                }

            relation = pgsql.SQL("{}.{}").format(
                pgsql.Identifier("cubes"),
                pgsql.Identifier(cube),
            )
            cell_dimensions = row_dimensions + column_dimensions
            grouped_row_cap = max(
                1,
                CUBE_PIVOT_CELL_CAP // max(1, len(measure_specs)),
            )
            cell_query = _cube_group_query(
                relation,
                cell_dimensions,
                measure_specs,
                limit=bool(cell_dimensions),
            )
            cell_rows = c.execute(
                cell_query,
                (grouped_row_cap + 1,) if cell_dimensions else None,
            ).fetchall()
            if len(cell_rows) > grouped_row_cap:
                return {
                    "error": {
                        "code": "PIVOT_TOO_LARGE",
                        "message": (
                            f"this pivot exceeds {CUBE_PIVOT_CELL_CAP:,} cells; "
                            "choose lower-cardinality axes"
                        ),
                    }
                }
            if column_dimensions:
                row_total_rows = c.execute(
                    _cube_group_query(
                        relation,
                        row_dimensions,
                        measure_specs,
                        limit=False,
                    )
                ).fetchall()
                col_total_rows = c.execute(
                    _cube_group_query(
                        relation,
                        column_dimensions,
                        measure_specs,
                        limit=False,
                    )
                ).fetchall()
            else:
                row_total_rows = []
                col_total_rows = []
            grand_row = c.execute(
                _cube_group_query(
                    relation,
                    [],
                    measure_specs,
                    limit=False,
                )
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return {
            "error": {
                "code": "CUBE_PIVOT_FAILED",
                "message": str(exc),
            }
        }

    public_measures = [
        {key: spec[key] for key in ("key", "field", "aggregate", "label")}
        for spec in measure_specs
    ]
    grand_totals = {
        spec["key"]: grand_row.get(spec["alias"]) if grand_row else None
        for spec in measure_specs
    }
    common = {
        "cube": cube,
        "rows_dim": row_dimensions[0] if len(row_dimensions) == 1 else " › ".join(row_dimensions),
        "cols_dim": (
            column_dimensions[0]
            if len(column_dimensions) == 1
            else " › ".join(column_dimensions)
        ) or None,
        "row_dimensions": row_dimensions,
        "column_dimensions": column_dimensions,
        "measure": measure_specs[0]["field"],
        "value_label": measure_specs[0]["field"] or "rows",
        "aggregate": measure_specs[0]["aggregate"],
        "measures": public_measures,
        "available_dimensions": available_dimensions,
        "available_measures": available_measures,
        "available_aggregates": list(_CUBE_PIVOT_AGGREGATES),
        "fields": fields,
        "grand_totals": grand_totals,
        "grand_total": grand_totals.get(measure_specs[0]["key"]),
        "cell_count": len(cell_rows) * len(measure_specs),
    }

    if not column_dimensions:
        table_columns = [
            {"key": name, "label": name, "kind": "dimension"}
            for name in row_dimensions
        ] + [
            {
                "key": spec["key"],
                "label": spec["label"],
                "kind": "measure",
                "measure_key": spec["key"],
            }
            for spec in measure_specs
        ]
        table_rows = [
            {
                "dimensions": _cube_dimension_values(row, row_dimensions),
                "values": {
                    spec["key"]: row.get(spec["alias"])
                    for spec in measure_specs
                },
            }
            for row in cell_rows
        ]
        return {
            **common,
            "display_mode": "table",
            "table_columns": table_columns,
            "table_rows": table_rows,
            "row_count": len(table_rows),
            "render": {
                "as": "grouped_table",
                "note": (
                    f"Render {row_dimensions or ['overall']} as ordinary columns "
                    f"with values {[spec['label'] for spec in measure_specs]}."
                ),
            },
        }

    column_groups = []
    column_group_keys = {}
    row_items = {}
    for record in cell_rows:
        row_key = _cube_dimension_key(record, row_dimensions)
        col_key = _cube_dimension_key(record, column_dimensions)
        if col_key not in column_group_keys:
            short_key = f"c{len(column_groups)}"
            column_group_keys[col_key] = short_key
            column_groups.append({
                "key": short_key,
                "values": _cube_dimension_values(record, column_dimensions),
            })
        short_key = column_group_keys[col_key]
        if row_key not in row_items:
            row_items[row_key] = {
                "row": " › ".join(_cube_dimension_values(record, row_dimensions).values()) or "Overall",
                "dimensions": _cube_dimension_values(record, row_dimensions),
                "cells": {},
                "totals": {},
            }
        for spec in measure_specs:
            row_items[row_key]["cells"][f"{short_key}::{spec['key']}"] = record.get(
                spec["alias"]
            )

    row_totals = {
        _cube_dimension_key(record, row_dimensions): {
            spec["key"]: record.get(spec["alias"])
            for spec in measure_specs
        }
        for record in row_total_rows
    }
    for row_key, item in row_items.items():
        item["totals"] = row_totals.get(row_key, {})
        item["total"] = item["totals"].get(measure_specs[0]["key"])

    col_total_groups = {
        _cube_dimension_key(record, column_dimensions): record
        for record in col_total_rows
    }
    value_columns = []
    col_totals = {}
    for group in column_groups:
        raw_key = json.dumps(list(group["values"].values()), separators=(",", ":"))
        total_record = col_total_groups.get(raw_key, {})
        group_label = " › ".join(group["values"].values()) or "Overall"
        for spec in measure_specs:
            key = f"{group['key']}::{spec['key']}"
            value_columns.append({
                "key": key,
                "label": (
                    f"{group_label} · {spec['label']}"
                    if len(measure_specs) > 1
                    else group_label
                ),
                "column_values": group["values"],
                "measure_key": spec["key"],
            })
            col_totals[key] = total_record.get(spec["alias"])

    matrix = list(row_items.values())
    return {
        **common,
        "display_mode": "crosstab",
        "column_groups": column_groups,
        "value_columns": value_columns,
        "columns": [column["key"] for column in value_columns],
        "matrix": matrix,
        "col_totals": col_totals,
        "row_count": len(matrix),
        "render": {
            "as": "pivot",
            "note": (
                f"Render {row_dimensions or ['overall']} down the left, "
                f"{column_dimensions} across the top, with "
                f"{[spec['label'] for spec in measure_specs]} as values."
            ),
        },
    }


def tool_compare(metric, period_a, period_b, by=None, params=None) -> dict:
    """Period-over-period / variance for a metric: its value at period_a vs period_b with Δ and %Δ. Pass
    `by` (a cube dimension) to break it down per segment (a variance table). Periods are data-time
    instants (e.g. '2026-03-31' vs '2026-06-30') — each side is the metric AS OF that instant via the
    bitemporal engine. Render the breakdown as a table sorted by |Δ|."""
    pj = json.dumps(params or {})

    def delta(a, b):
        if a is None or b is None:
            return {"a": a, "b": b, "delta": None, "pct": None}
        d = b - a
        return {"a": a, "b": b, "delta": d, "pct": (d / a * 100.0) if a else None}

    measure = None
    with _conn() as c:
        try:
            if by:
                ra = [r["m"] for r in c.execute(
                    "SELECT rvbbit.metric_by(%s,%s::text[],%s::jsonb,now(),%s::timestamptz) AS m",
                    (metric, [by], pj, period_a)).fetchall() if r["m"] is not None]
                rb = [r["m"] for r in c.execute(
                    "SELECT rvbbit.metric_by(%s,%s::text[],%s::jsonb,now(),%s::timestamptz) AS m",
                    (metric, [by], pj, period_b)).fetchall() if r["m"] is not None]
                sample = (ra or rb or [{}])[0]
                picks = _pick_measures(c, metric, sample, {by})
                measure = picks[0] if picks else None
            else:
                ra = [r["m"] for r in c.execute(
                    "SELECT rvbbit.metric(%s,%s::jsonb,now(),%s::timestamptz) AS m",
                    (metric, pj, period_a)).fetchall() if r["m"] is not None]
                rb = [r["m"] for r in c.execute(
                    "SELECT rvbbit.metric(%s,%s::jsonb,now(),%s::timestamptz) AS m",
                    (metric, pj, period_b)).fetchall() if r["m"] is not None]
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "COMPARE_FAILED", "message": str(e)}}
    if by:
        amap = {str(r.get(by)): _metric_scalar(r, measure) for r in ra}
        bmap = {str(r.get(by)): _metric_scalar(r, measure) for r in rb}
        rows = [{"segment": s, **delta(amap.get(s), bmap.get(s))} for s in sorted(set(amap) | set(bmap))]
        rows.sort(key=lambda x: abs(x["delta"]) if x["delta"] is not None else -1, reverse=True)
        ta = sum(v for v in amap.values() if v is not None)
        tb = sum(v for v in bmap.values() if v is not None)
        return {"metric": metric, "by": by, "measure": measure, "period_a": period_a, "period_b": period_b,
                "total": delta(ta, tb), "rows": rows,
                "render": {"as": "variance",
                           "note": f"Variance table: one row per '{by}', columns value@{period_a}, value@{period_b}, "
                                   "Δ, %Δ; sorted by |Δ| (biggest movers first); show the total row; "
                                   "color Δ red(neg)/green(pos)."}}
    a = _metric_scalar(ra[0]) if ra else None
    b = _metric_scalar(rb[0]) if rb else None
    return {"metric": metric, "period_a": period_a, "period_b": period_b, **delta(a, b),
            "render": {"as": "delta", "note": "Show value@A, value@B, Δ and %Δ as a compact stat line."}}


# ── document brain — role-gated, semantically-searchable docs ─────────────────
# Access is enforced server-side from the AUTHENTICATED caller email (never a tool argument): the
# retrieval filters to the caller's permitted docs BEFORE the vector search, so a restricted doc never
# enters the result set and can't be paraphrased into an answer. caller_email is injected by the
# registration lambda from _caller(); read tools require it (default-deny on no identity).

def tool_ask_brain(query, k=8, filters=None, caller_email=None) -> dict:
    """Ask the document brain — semantic search over the docs YOU are permitted to see, returned as
    grounded, citeable context (NOT a synthesized answer — compose the answer from these chunks and
    cite title/folder). Access is enforced from your authenticated identity: docs you lack a role for,
    or that exclude you, never appear. The ABSENCE of a doc means you're not cleared for it — never
    speculate about what you can't see.

    PRE-FILTER to avoid mixing object classes: pass `filters` to narrow BEFORE the search —
      {"type": "ticket"}  ·  {"type": ["document","meeting"]}  ·  {"source": "Linear · all"}  ·
      {"folder": "/sops", "since": "2026-01-01"}.
    Every hit is tagged with `doc_type` (e.g. document, ticket, meeting) and `source`, and `types` /
    `sources` summarize what came back. Don't know what's filterable? Call brain_facets first.

    This is the ENTRY POINT, not the whole story. Each hit carries breadcrumbs — its doc's key
    `entities` (knowledge-graph handles) — and a doc-level `documents` rollup lists, per doc, its
    entities + `related` docs (other docs you can see that share its concepts). Pull threads on demand
    rather than over-fetching: brain_context(doc_id, chunk_idx) for the chunks around a hit,
    brain_get_doc(doc_id) for the full document, brain_related(doc_id) to walk the graph from a doc,
    brain_entity(name) to ask 'what do we know about X?'."""
    if not caller_email:
        return {"error": {"code": "NO_IDENTITY", "message": "brain access requires an authenticated caller (OAuth email)"}}
    k = max(1, min(int(k or 8), 50))
    flt = json.dumps(filters if isinstance(filters, dict) else {})
    with _conn() as c:   # _conn (writable): rvbbit.embed may populate its embedding cache
        try:
            hits = c.execute(
                "SELECT doc_id, chunk_idx, title, folder_path AS folder, source, doc_type, "
                "occurred_at::text AS occurred_at, chunk, round(score::numeric, 4) AS score, entities "
                "FROM rvbbit.brain_search(%s, %s, %s, %s::jsonb)", (caller_email, query, k, flt)).fetchall()
            # Doc-level rollup: dedupe the hit docs; attach DOC-LEVEL entities + related threads from
            # brain_related (same store as the relatedness `shared` counts, so they reconcile). Each
            # related doc carries `shared_entities` — the exact overlap that explains its `shared`.
            # (Per-hit `entities` stay CHUNK-scoped for local signal; the rollup is the doc-level view.)
            docs: dict = {}
            types: dict = {}
            for h in hits:
                d = docs.setdefault(h["doc_id"], {"doc_id": h["doc_id"], "title": h["title"],
                                                  "source": h["source"], "doc_type": h["doc_type"], "n_hits": 0})
                d["n_hits"] += 1
                types[h["doc_type"]] = types.get(h["doc_type"], 0) + 1
            for did, d in docs.items():
                rel = c.execute("SELECT rvbbit.brain_related(%s, %s::bigint, 15) AS r", (caller_email, did)).fetchone()
                rr = (rel["r"] if rel else {}) or {}
                d["entities"] = [e.get("label") for e in rr.get("entities", []) if e.get("label")]
                d["related"] = rr.get("related", [])
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "ASK_BRAIN_FAILED", "message": str(e)}}
    return {"query": query, "as": caller_email, "filters": filters or {}, "hits": hits,
            "documents": list(docs.values()), "count": len(hits), "types": types,
            "note": "Grounded context, not an answer — cite title/folder. Each hit is tagged `doc_type` — "
                    "don't conflate classes (a ticket ≠ an SOP). Pre-filter with `filters` (see brain_facets) "
                    "to narrow by type/source. Go deeper with brain_context / brain_get_doc / brain_related / "
                    "brain_entity. Absence = not cleared, not 'nothing exists'."}


def tool_brain_facets(caller_email=None) -> dict:
    """Discover what you can FILTER by: the document TYPES (document, ticket, meeting, …) and SOURCES you
    are cleared to see, each with a doc count. Call this before ask_brain when you want to narrow — then
    pass filters={"type": "ticket"} or {"source": "..."} to ask_brain. ACL-enforced: only your visible
    corpus is counted."""
    if not caller_email:
        return {"error": {"code": "NO_IDENTITY", "message": "brain access requires an authenticated caller"}}
    with _ro() as c:
        rows = c.execute("SELECT facet, value, docs FROM rvbbit.brain_facets(%s)", (caller_email,)).fetchall()
    return {"as": caller_email,
            "types":   {r["value"]: r["docs"] for r in rows if r["facet"] == "type"},
            "sources": {r["value"]: r["docs"] for r in rows if r["facet"] == "source"},
            "note": "Pass to ask_brain as filters={\"type\": …, \"source\": …} to pre-narrow the search."}


def tool_brain_browse(caller_email=None) -> dict:
    """The document brain as a file tree — every folder + doc YOU may see (ACL-enforced). Powers a
    file-explorer view and lets you navigate before asking. Returns folders + docs with folder_path,
    title, source, mime, occurred_at, chunk count."""
    if not caller_email:
        return {"error": {"code": "NO_IDENTITY", "message": "brain access requires an authenticated caller"}}
    with _ro() as c:
        rows = c.execute(
            "SELECT folder_path, doc_id, title, source, mime, author, occurred_at::text AS occurred_at, "
            "ingested_at::text AS ingested_at, chunks FROM rvbbit.brain_tree(%s)", (caller_email,)).fetchall()
    return {"as": caller_email, "folders": sorted({r["folder_path"] for r in rows}),
            "documents": rows, "count": len(rows)}


def tool_brain_get_doc(doc_id, caller_email=None) -> dict:
    """Open one document's full body + metadata — only if you're cleared for it (else NOT_VISIBLE)."""
    if not caller_email:
        return {"error": {"code": "NO_IDENTITY", "message": "brain access requires an authenticated caller"}}
    with _ro() as c:
        row = c.execute("SELECT rvbbit.brain_get_doc(%s, %s::bigint) AS d", (caller_email, doc_id)).fetchone()
    d = row["d"] if row else None
    if not d:
        return {"error": {"code": "NOT_VISIBLE", "message": f"doc {doc_id} not found or not permitted"}}
    return d


def tool_brain_context(doc_id, chunk_idx, window=2, caller_email=None) -> dict:
    """VERTICAL expand: the chunks immediately AROUND a search hit (window on each side) — cheaper than
    pulling a whole long document when you just need a hit's local context. Pass the doc_id + chunk_idx
    from an ask_brain hit. ACL-gated (empty if you're not cleared for the doc)."""
    if not caller_email:
        return {"error": {"code": "NO_IDENTITY", "message": "brain access requires an authenticated caller"}}
    with _ro() as c:
        rows = c.execute(
            "SELECT idx, chunk FROM rvbbit.brain_context(%s, %s::bigint, %s::int, %s::int)",
            (caller_email, doc_id, chunk_idx, window)).fetchall()
    return {"doc_id": doc_id, "chunk_idx": chunk_idx, "window": window, "chunks": rows, "count": len(rows)}


def tool_brain_related(doc_id, caller_email=None) -> dict:
    """LATERAL expand: a document's knowledge-graph neighborhood — the entities it names, the typed
    relations among them (e.g. X -acquired-> Y), and OTHER docs you can see that share its entities.
    Follow a thread from a doc instead of re-searching. ACL-gated."""
    if not caller_email:
        return {"error": {"code": "NO_IDENTITY", "message": "brain access requires an authenticated caller"}}
    with _ro() as c:
        row = c.execute("SELECT rvbbit.brain_related(%s, %s::bigint) AS r", (caller_email, doc_id)).fetchone()
    return (row["r"] if row else {}) or {}


def tool_brain_entity(name, caller_email=None) -> dict:
    """LATERAL expand, entity-centric: given a concept/person/org/metric (e.g. 'NPS', 'refund policy'),
    return its typed relations and the visible documents that mention it — 'what do we know about X?'.
    Resolves by exact then fuzzy name match. ACL-gated (docs list is filtered to what you can see)."""
    if not caller_email:
        return {"error": {"code": "NO_IDENTITY", "message": "brain access requires an authenticated caller"}}
    with _ro() as c:
        row = c.execute("SELECT rvbbit.brain_entity(%s, %s) AS r", (caller_email, name)).fetchone()
    return (row["r"] if row else {}) or {}


_SYSTEM_LEARNING_SUGGESTED_PROMPTS = [
    {
        "label": "Acceleration next steps",
        "query": "Which regular heap tables should I accelerate next, and why?",
        "use_when": "Start here when a database feels slow but you do not know which tables deserve acceleration.",
    },
    {
        "label": "Slow query explanation",
        "query": "What did RVBBIT learn about my slowest query shapes and routing choices?",
        "use_when": "Use after a workload run or benchmark to explain why some shapes prefer specific engines.",
    },
    {
        "label": "Layout payoff",
        "query": "Which accepted workload layouts are built, which are still pending, and what evidence supports them?",
        "use_when": "Use when deciding whether a workload layout should be built, rebuilt, or rejected.",
    },
    {
        "label": "Operator trust",
        "query": "Which SQL operators have enough receipts to trust, and which need more observation?",
        "use_when": "Use before leaning on semantic SQL operators in dashboards or agent workflows.",
    },
    {
        "label": "What changed",
        "query": "What has RVBBIT learned recently about acceleration, routing, layouts, and operators?",
        "use_when": "Use as a weekly or post-deploy briefing for humans and agents.",
    },
]


def _system_learning_answer_contract() -> dict:
    return {
        "style": "grounded_context_not_synthesis",
        "required_citations": ["hit.title", "artifact.uri"],
        "follow_the_breadcrumbs": [
            "Use hit.artifact.handles for the table/layout/shape/operator handle.",
            "Use hit.artifact.followups where tool='run_sql' for the exact learned row.",
            "Use run_sql on rvbbit.system_learning_items when you need the full body/props.",
        ],
        "do_not": [
            "Do not claim a table is accelerated unless the artifact status says so.",
            "Do not treat absence of a hit as absence of evidence; sync first or broaden the query.",
        ],
    }


def _system_learning_readiness(status: dict | None) -> dict:
    if not status or not status.get("installed"):
        return {
            "ready": False,
            "state": "missing",
            "why": "RVBBIT System Learning is not installed in this database.",
            "actions": [
                {"tool": "run_sql", "sql": "SELECT rvbbit.migrate()"},
            ],
        }
    if not status.get("enabled"):
        return {
            "ready": False,
            "state": "paused",
            "why": "The RVBBIT System Learning Brain source is disabled.",
            "actions": [
                {
                    "tool": "run_sql",
                    "sql": "UPDATE rvbbit.brain_sources SET enabled = true WHERE source_id = "
                           f"{int(status.get('source_id') or 0)}",
                },
            ],
        }
    indexed = int(status.get("indexed_items") or 0)
    docs = int(status.get("docs") or 0)
    last_run = status.get("last_run") or {}
    errors = int(last_run.get("errors") or 0)
    if indexed <= 0:
        return {
            "ready": False,
            "state": "empty",
            "why": "The provider is installed, but RVBBIT has not observed learning artifacts yet.",
            "actions": [
                {"tool": "run_sql", "sql": "SELECT * FROM rvbbit.system_learning_item_summary ORDER BY items DESC"},
            ],
        }
    if docs <= 0:
        return {
            "ready": False,
            "state": "needs_sync",
            "why": f"{indexed} learned artifact(s) exist, but none are indexed into Brain yet.",
            "actions": [{"tool": "sync_system_learning"}],
        }
    if errors > 0:
        return {
            "ready": False,
            "state": "degraded",
            "why": f"The last sync recorded {errors} error(s). Search may be incomplete.",
            "actions": [{"tool": "sync_system_learning"}, {"tool": "system_learning_status"}],
        }
    if docs < indexed:
        return {
            "ready": True,
            "state": "partial",
            "why": f"{docs} of {indexed} learned artifact(s) are indexed. Search works, but a sync may catch up.",
            "actions": [{"tool": "sync_system_learning"}],
        }
    return {
        "ready": True,
        "state": "ready",
        "why": f"{docs} learned artifact(s) are indexed and searchable.",
        "actions": [
            {"tool": "ask_system_learning", "query": _SYSTEM_LEARNING_SUGGESTED_PROMPTS[0]["query"]},
        ],
    }


def _system_learning_status_sql_followups() -> list[dict]:
    return [
        {
            "tool": "run_sql",
            "label": "Learning item summary",
            "sql": (
                "SELECT object_type, items, last_seen_at "
                "FROM rvbbit.system_learning_item_summary ORDER BY items DESC, object_type"
            ),
        },
        {
            "tool": "run_sql",
            "label": "Recent learned artifacts",
            "sql": (
                "SELECT uri, title, occurred_at, props "
                "FROM rvbbit.system_learning_items ORDER BY occurred_at DESC, title LIMIT 20"
            ),
        },
        {
            "tool": "run_sql",
            "label": "Brain sync state",
            "sql": "SELECT * FROM rvbbit.system_learning_brain_status",
        },
    ]


def tool_system_learning_status() -> dict:
    """What RVBBIT has learned about its own workload and agent corpus: artifact counts, sync state,
    graph edge handles, concrete breadcrumb examples, and the doc_type/source an MCP caller can
    search. This is the MCP-friendly mirror of the SQL Desktop's System Learning strip."""
    with _ro() as c:
        installed = c.execute(
            "SELECT to_regclass('rvbbit.system_learning_brain_status') IS NOT NULL AS ok"
        ).fetchone()["ok"]
        summary_installed = c.execute(
            "SELECT to_regclass('rvbbit.system_learning_item_summary') IS NOT NULL AS ok"
        ).fetchone()["ok"]
        if not installed:
            response = {
                "installed": False,
                "source": "RVBBIT System Learning",
                "doc_type": "system_learning",
                "summary": [],
                "breadcrumbs": [],
                "graph_edges": [],
                "agent_tools": ["system_learning_status", "sync_system_learning", "ask_system_learning"],
                "note": "Run rvbbit.migrate() to install the system-learning Brain provider.",
            }
            response["readiness"] = _system_learning_readiness(response)
            response["suggested_prompts"] = _SYSTEM_LEARNING_SUGGESTED_PROMPTS
            response["answer_contract"] = _system_learning_answer_contract()
            response["followups"] = [{"tool": "run_sql", "sql": "SELECT rvbbit.migrate()"}]
            return response
        status = c.execute(
            "SELECT installed, source_id, enabled, indexed_items, docs, "
            "last_synced_at::text AS last_synced_at, last_run_at::text AS last_run_at, "
            "last_run_added, last_run_changed, last_run_removed, last_run_skipped, "
            "last_run_errors, last_run_elapsed_sec "
            "FROM rvbbit.system_learning_brain_status"
        ).fetchone()
        summary = []
        if summary_installed:
            summary = c.execute(
                "SELECT object_type, items, last_seen_at::text AS last_seen_at "
                "FROM rvbbit.system_learning_item_summary ORDER BY items DESC, object_type"
            ).fetchall()
        provider = c.execute(
            "SELECT edge_map FROM rvbbit.brain_doc_providers WHERE provider = 'rvbbit-system-learning'"
        ).fetchone()
        breadcrumbs = c.execute(
            """
            WITH ranked AS (
                SELECT uri, title, occurred_at, body, props,
                       coalesce(props->>'object_type', 'unknown') AS object_type,
                       row_number() OVER (
                           PARTITION BY coalesce(props->>'object_type', 'unknown')
                           ORDER BY occurred_at DESC, title
                       ) AS rn
                FROM rvbbit.system_learning_items
            )
            SELECT uri, title, object_type, occurred_at::text AS occurred_at,
                   left(body, 700) AS preview,
                   jsonb_strip_nulls(jsonb_build_object(
                       'table', props->>'table',
                       'column', props->>'column',
                       'layout', props->>'layout',
                       'layout_kind', props->>'layout_kind',
                       'layout_status', props->>'layout_status',
                       'shape_key', props->>'shape_key',
                       'shape_family', props->>'shape_family',
                       'engine', props->>'engine',
                       'operator', props->>'operator',
                       'status', props->>'status',
                       'score', props->>'score',
                       'observations', props->>'observations'
                   )) AS handles
            FROM ranked
            WHERE rn <= 2
            ORDER BY object_type, occurred_at DESC, title
            LIMIT 12
            """
        ).fetchall()
    breadcrumbs = [_system_learning_breadcrumb(row) for row in breadcrumbs]
    response = {
        "installed": bool(status["installed"]) if status else False,
        "source_id": status["source_id"] if status else None,
        "enabled": bool(status["enabled"]) if status else False,
        "source": "RVBBIT System Learning",
        "doc_type": "system_learning",
        "indexed_items": status["indexed_items"] if status else 0,
        "docs": status["docs"] if status else 0,
        "last_synced_at": status["last_synced_at"] if status else None,
        "last_run": {
            "at": status["last_run_at"] if status else None,
            "added": status["last_run_added"] if status else 0,
            "changed": status["last_run_changed"] if status else 0,
            "removed": status["last_run_removed"] if status else 0,
            "skipped": status["last_run_skipped"] if status else 0,
            "errors": status["last_run_errors"] if status else 0,
            "elapsed_sec": status["last_run_elapsed_sec"] if status else None,
        },
        "summary": summary,
        "breadcrumbs": breadcrumbs,
        "graph_edges": (provider["edge_map"] if provider else []) or [],
        "agent_tools": ["system_learning_status", "sync_system_learning", "ask_system_learning", "run_sql"],
        "next_tools": ["ask_system_learning", "sync_system_learning", "run_sql"],
        "note": "Use breadcrumbs as handles: ask_system_learning for fuzzy context, run_sql for exact rvbbit.system_learning_items rows.",
    }
    response["readiness"] = _system_learning_readiness(response)
    response["suggested_prompts"] = _SYSTEM_LEARNING_SUGGESTED_PROMPTS
    response["answer_contract"] = _system_learning_answer_contract()
    response["followups"] = _system_learning_status_sql_followups()
    return response


def _system_learning_breadcrumb(row: dict) -> dict:
    handles = row.get("handles") or {}
    queries = []
    table = handles.get("table")
    column = handles.get("column")
    layout = handles.get("layout")
    shape_key = handles.get("shape_key")
    operator = handles.get("operator")
    engine = handles.get("engine")
    if table:
        queries.append(f"{table} acceleration workload")
    if table and column:
        queries.append(f"{table} {column} layout recommendation")
    if layout:
        queries.append(f"workload layout {layout}")
    if shape_key:
        queries.append(f"route shape {shape_key}")
    if engine:
        queries.append(f"{engine} routing performance")
    if operator:
        queries.append(f"operator {operator} trust receipts")
    if not queries:
        queries.append(str(row.get("title") or row.get("object_type") or "RVBBIT system learning"))
    uri = str(row.get("uri") or "")
    sql_uri = uri.replace("'", "''")
    inspect_sql = (
        "SELECT uri, title, occurred_at, body, props "
        f"FROM rvbbit.system_learning_items WHERE uri = '{sql_uri}'"
    )
    return {
        "uri": uri,
        "title": row.get("title"),
        "object_type": row.get("object_type"),
        "occurred_at": row.get("occurred_at"),
        "handles": handles,
        "preview": row.get("preview"),
        "inspect_sql": inspect_sql,
        "followups": [
            {"tool": "ask_system_learning", "query": queries[0]},
            {
                "tool": "run_sql",
                "sql": inspect_sql,
            },
        ],
    }


_SYSTEM_LEARNING_HANDLES_SQL = """
jsonb_strip_nulls(jsonb_build_object(
    'table', i.props->>'table',
    'column', i.props->>'column',
    'layout', i.props->>'layout',
    'layout_kind', i.props->>'layout_kind',
    'layout_status', i.props->>'layout_status',
    'shape_key', i.props->>'shape_key',
    'shape_family', i.props->>'shape_family',
    'engine', i.props->>'engine',
    'operator', i.props->>'operator',
    'status', i.props->>'status',
    'score', i.props->>'score',
    'observations', i.props->>'observations'
)) AS handles
"""


def _system_learning_breadcrumbs_for_docs(doc_ids: list[int]) -> dict[int, dict]:
    ids = sorted({int(doc_id) for doc_id in doc_ids if doc_id is not None})
    if not ids:
        return {}
    with _ro() as c:
        rows = c.execute(
            f"""
            SELECT d.doc_id, i.uri, i.title,
                   coalesce(i.props->>'object_type', 'unknown') AS object_type,
                   i.occurred_at::text AS occurred_at,
                   left(i.body, 700) AS preview,
                   {_SYSTEM_LEARNING_HANDLES_SQL}
            FROM rvbbit.brain_documents d
            JOIN rvbbit.system_learning_items i ON i.uri = d.uri
            WHERE d.doc_id = ANY(%s::bigint[])
            """,
            (ids,),
        ).fetchall()
    return {int(row["doc_id"]): _system_learning_breadcrumb(row) for row in rows}


def _attach_system_learning_breadcrumbs(result: dict) -> dict:
    if not isinstance(result, dict) or result.get("error"):
        return result

    doc_ids: list[int] = []
    for hit in result.get("hits", []):
        if isinstance(hit, dict) and hit.get("doc_id") is not None:
            doc_ids.append(hit["doc_id"])
    for doc in result.get("documents", []):
        if isinstance(doc, dict) and doc.get("doc_id") is not None:
            doc_ids.append(doc["doc_id"])

    try:
        breadcrumbs_by_doc = _system_learning_breadcrumbs_for_docs(doc_ids)
    except Exception as e:  # noqa: BLE001
        result["breadcrumb_error"] = str(e)
        return result

    seen = set()
    breadcrumbs = []
    for hit in result.get("hits", []):
        if not isinstance(hit, dict):
            continue
        artifact = breadcrumbs_by_doc.get(int(hit["doc_id"])) if hit.get("doc_id") is not None else None
        if artifact:
            hit["artifact"] = artifact
            if artifact["uri"] not in seen:
                seen.add(artifact["uri"])
                breadcrumbs.append(artifact)

    for doc in result.get("documents", []):
        if not isinstance(doc, dict):
            continue
        artifact = breadcrumbs_by_doc.get(int(doc["doc_id"])) if doc.get("doc_id") is not None else None
        if artifact:
            doc["artifact"] = artifact
            if artifact["uri"] not in seen:
                seen.add(artifact["uri"])
                breadcrumbs.append(artifact)

    result["breadcrumbs"] = breadcrumbs
    result["followups"] = [
        followup
        for breadcrumb in breadcrumbs[:5]
        for followup in breadcrumb.get("followups", [])
        if followup.get("tool") == "run_sql"
    ] or _system_learning_status_sql_followups()
    result["next_tools"] = ["ask_system_learning", "run_sql", "system_learning_status", "sync_system_learning"]
    result["suggested_prompts"] = _SYSTEM_LEARNING_SUGGESTED_PROMPTS
    result["answer_contract"] = _system_learning_answer_contract()
    try:
        result["readiness"] = tool_system_learning_status().get("readiness")
    except Exception as e:  # noqa: BLE001
        result["readiness_error"] = str(e)
    result["note"] = (
        "Grounded system-learning context, not an answer. Each hit/document may include an `artifact` "
        "with handles and followups; use run_sql followups for exact rvbbit.system_learning_items rows."
    )
    return result


def tool_sync_system_learning() -> dict:
    """Refresh RVBBIT System Learning into the Brain. This syncs learned workload layouts, route
    shapes, acceleration state/candidates, and operator trust artifacts so MCP agents search the same
    breadcrumbs the SQL Desktop shows."""
    with _conn() as c:
        try:
            source = c.execute(
                "SELECT source_id FROM rvbbit.brain_sources WHERE label = 'RVBBIT System Learning'"
            ).fetchone()
            if not source:
                return {"error": {"code": "NOT_INSTALLED", "message": "RVBBIT System Learning source is not installed; run rvbbit.migrate()"}}
            result = c.execute(
                "SELECT rvbbit.brain_sync_dispatch(%s, 'mcp') AS r", (source["source_id"],)
            ).fetchone()["r"]
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "SYNC_SYSTEM_LEARNING_FAILED", "message": str(e)}}
    status = tool_system_learning_status()
    return {"source": "RVBBIT System Learning", "result": result or {}, "status": status}


def tool_ask_system_learning(query, k=8, caller_email=None) -> dict:
    """Ask what RVBBIT has learned about this database. This is the agent-safe shortcut over
    ask_brain(filters={"type":["system_learning"]}) so callers do not need to remember the doc_type
    name. Results include workload/layout/routing/acceleration/operator breadcrumbs, not a synthesized
    answer. Compose an answer from the returned chunks and cite titles."""
    effective_email = caller_email or "mcp-system-learning@rvbbit.local"
    result = tool_ask_brain(
        query,
        k,
        {"type": ["system_learning"]},
        effective_email,
    )
    return _attach_system_learning_breadcrumbs(result)


def tool_brain_ingest(source, title, body, roles=None, folder=None, uri=None,
                      author=None, occurred_at=None) -> dict:
    """Ingest a document into the brain (operator action): chunks + embeds it and assigns access role(s).
    roles = the roles allowed to see it (omit → the source's default roles → if none, DEFAULT-DENY:
    nobody can see it until granted a role). folder = its file-explorer path. Returns the doc_id."""
    with _conn() as c:
        try:
            row = c.execute(
                "SELECT rvbbit.brain_ingest(%s, %s, %s, %s::text[], %s, %s, %s, %s::timestamptz) AS id",
                (source, title, body, roles, folder, uri, author, occurred_at)).fetchone()
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "INGEST_FAILED", "message": str(e)}}
    return {"doc_id": row["id"] if row else None, "source": source, "title": title, "roles": roles}


def tool_brain_grant(role, principal, on=True) -> dict:
    """Grant (on=true) or revoke (on=false) a brain ROLE to a principal (email). Roles→emails are just
    rows — this IS the access model; who holds what determines what each person's brain can see.
    Revocation takes effect on the next query (no re-index)."""
    fn = "brain_grant" if on else "brain_revoke"   # fixed literals
    with _conn() as c:
        try:
            c.execute(f"SELECT rvbbit.{fn}(%s, %s)", (role, principal))
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "GRANT_FAILED", "message": str(e)}}
    return {"role": role, "principal": principal, "granted": bool(on)}


def tool_brain_exclude(doc_id, principal, reason=None) -> dict:
    """The subject-exclusion belt: hide a specific doc from a specific person even if their role would
    allow it (the meeting that's ABOUT them). Returns the exclusion."""
    with _conn() as c:
        try:
            c.execute("SELECT rvbbit.brain_exclude(%s::bigint, %s, %s)", (doc_id, principal, reason))
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "EXCLUDE_FAILED", "message": str(e)}}
    return {"doc_id": doc_id, "principal": principal, "excluded": True}


def tool_brain_set_doc_roles(doc_id, roles=None) -> dict:
    """Set the access role(s) on a document — the docs a role grants are visible to anyone holding it.
    Pass [] to make it private again (default-deny). A freshly-ingested doc with no roles is invisible
    to everyone (incl. the explorer's own listing); this is how you make it visible."""
    with _conn() as c:
        try:
            c.execute("SELECT rvbbit.brain_set_doc_roles(%s::bigint, %s::text[])", (doc_id, roles))
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": "SET_DOC_ROLES_FAILED", "message": str(e)}}
    return {"doc_id": doc_id, "roles": roles or []}


_BRAIN_TEXT_EXT = {".md", ".markdown", ".mdx", ".txt", ".text", ".rst", ".org", ".log"}


def tool_brain_crawl_folder(path, source=None, roles=None, base_folder=None,
                            recursive=True, max_files=500, max_bytes=1_000_000) -> dict:
    """Crawl a SERVER-LOCAL folder and ingest its text documents into the brain — the on-disk folder
    structure becomes the brain's folder tree (e.g. <root>/HR/policy.md → folder /<source>/HR). `path`
    must be readable by the MCP process (mount it into the container). roles = access roles applied to
    EVERY ingested doc (omit → the source's defaults → DEFAULT-DENY: nobody sees them). Handles
    .md/.markdown/.mdx/.txt/.text/.rst/.org/.log; skips binaries + files over max_bytes. Re-crawl is
    idempotent (keyed on each file's path), so it doubles as a sync."""
    root = os.path.abspath(os.path.expanduser(path or ""))
    if not os.path.isdir(root):
        return {"error": {"code": "BAD_PATH", "message": f"not a readable directory: {root}"}}
    src = source or (os.path.basename(root.rstrip("/")) or "crawl")
    base = (base_folder or ("/" + src)).rstrip("/") or "/"
    cap = max(1, min(int(max_files or 500), 5000))
    ingested, skipped, errors = [], 0, []
    n = 0
    with _conn() as c:
        for dirpath, dirnames, filenames in os.walk(root):
            if not recursive:
                dirnames[:] = []
            for fn in sorted(filenames):
                if n >= cap:
                    break
                fp = os.path.join(dirpath, fn)
                if os.path.splitext(fn)[1].lower() not in _BRAIN_TEXT_EXT:
                    skipped += 1
                    continue
                try:
                    if os.path.getsize(fp) > max_bytes:
                        skipped += 1
                        continue
                    with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                        body = fh.read()
                except Exception as e:  # noqa: BLE001
                    errors.append({"file": fp, "error": str(e)})
                    continue
                subdir = os.path.dirname(os.path.relpath(fp, root)).replace(os.sep, "/")
                folder = base + ("/" + subdir if subdir else "")
                title = os.path.splitext(os.path.basename(fp))[0]
                try:
                    row = c.execute(
                        "SELECT rvbbit.brain_ingest(%s, %s, %s, %s::text[], %s, %s) AS id",
                        (src, title, body, roles, folder, fp)).fetchone()
                    ingested.append({"doc_id": row["id"] if row else None, "title": title, "folder": folder})
                    n += 1
                except Exception as e:  # noqa: BLE001
                    errors.append({"file": fp, "error": str(e)})
            if n >= cap:
                break
    return {"source": src, "root": root, "ingested": len(ingested), "skipped": skipped,
            "errors": errors[:10], "docs": ingested[:50],
            "note": (None if roles else "no roles given → docs are DEFAULT-DENY (visible to no one) until a role is granted")}


def tool_validate_sql(sql: str, as_of=None) -> dict:
    """Plan, don't execute — route_explain dry-run so Claude can self-correct cheaply."""
    try:
        normalized_as_of = _normalize_as_of(as_of)
        with _conn() as c:
            # route_explain's read-only gate expects SELECT/WITH at byte zero and
            # does not currently strip the documented leading AS-OF directive.
            # Validate the underlying statement; the execution below receives
            # the directive after the same statement has passed this gate.
            ex = c.execute("SELECT rvbbit.route_explain(%s) AS e", (sql,)).fetchone()["e"]
    except ValueError as e:
        return {"valid": False, "safe_select": False, "error": str(e)}
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
        "as_of_applied": normalized_as_of,
    }


def tool_run_sql(sql: str, as_of=None, limit=None) -> dict:
    """Governed read-only execute: validate -> safe_select gate -> read-only run + LIMIT."""
    limit = max(1, min(int(limit or ROW_CAP), ROW_CAP))
    try:
        normalized_as_of = _normalize_as_of(as_of)
    except ValueError as exc:
        return {"error": {"code": "BAD_AS_OF", "message": str(exc)}}
    v = tool_validate_sql(sql, normalized_as_of)
    if not v.get("valid"):
        return {"error": {"code": "INVALID_SQL", "message": v.get("error")}}
    if not v.get("safe_select"):
        return {"error": {"code": "NOT_SELECT",
                          "message": "only a read-only SELECT/CTE is allowed", "reason": v.get("reason")}}
    t0 = time.time()
    with _conn(read_only=True, role=_session_pg_role()) as c, c.cursor() as cur:
        cur.execute(_with_as_of(sql, normalized_as_of))
        cols = ([{"name": d.name, "type": _TYPE.get(d.type_code, str(d.type_code))}
                 for d in cur.description] if cur.description else [])
        rows = cur.fetchmany(limit)
        truncated = cur.fetchone() is not None
    return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated,
            "engine": v.get("engine"), "elapsed_ms": int((time.time() - t0) * 1000),
            "as_of_applied": normalized_as_of}


def tool_run_sql_multi(queries, as_of=None, limit=None, result_mode="full", preview_rows=3) -> dict:
    """Governed read-only BATCH: many named FLAT queries, one round trip.
    This exists so dashboards/apps never glue multi-concern payloads together
    inside SQL (top-level json_build_object) just to save bridge calls — each
    concern stays a flat rowset the router can accelerate, the catalog can
    mine, and Promote can later lift into a metric/cube. Per-query errors are
    isolated under their name; one bad query doesn't sink the batch.

    result_mode='summary' returns per-query row_count/columns/truncated/
    elapsed/error plus the first preview_rows rows — use it to VALIDATE a
    dashboard's query set without hauling hundreds of KB of rows back through
    the conversation. Re-run individual queries in full mode when you need
    the data itself."""
    if not isinstance(queries, dict) or not queries:
        return {"error": {"code": "BAD_QUERIES",
                          "message": "queries must be a non-empty {name: sql} object"}}
    if len(queries) > 24:
        return {"error": {"code": "TOO_MANY_QUERIES", "message": "max 24 queries per batch"}}
    if result_mode not in ("full", "summary"):
        return {"error": {"code": "BAD_RESULT_MODE", "message": "result_mode must be 'full' or 'summary'"}}
    try:
        normalized_as_of = _normalize_as_of(as_of)
    except ValueError as exc:
        return {"error": {"code": "BAD_AS_OF", "message": str(exc)}}
    try:
        preview_rows = max(0, min(int(preview_rows), 25))
    except (TypeError, ValueError):
        preview_rows = 3
    t0 = time.time()
    results = {
        str(name): tool_run_sql(sql, normalized_as_of, limit)
        for name, sql in queries.items()
    }
    if result_mode == "summary":
        compact = {}
        for name, r in results.items():
            if r.get("error"):
                compact[name] = {"error": r["error"]}
                continue
            compact[name] = {
                "row_count": r.get("row_count"),
                "columns": [c["name"] for c in r.get("columns", [])],
                "truncated": r.get("truncated"),
                "engine": r.get("engine"),
                "elapsed_ms": r.get("elapsed_ms"),
                "preview": (r.get("rows") or [])[:preview_rows],
            }
        results = compact
    return {"results": results, "result_mode": result_mode,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "as_of_applied": normalized_as_of}


# ── activity log (audit + a substrate for usage-learning) ────────────────────
# Every tool call is recorded to a system table: who (the token's email), the tool,
# the args (incl. the SQL/query), outcome, the objects it touched, rows, engine, ms.
# It answers "who is doing what" now, and is the raw material for a feedback loop
# later (popular tables → search-rank boosts, common questions → suggested metrics,
# repeated errors → catalog gaps). Best-effort: a logging failure never breaks a call.

ACTIVITY_TABLE = os.environ.get("WAREHOUSE_ACTIVITY_TABLE", "rvbbit.mcp_activity")
_ACTIVITY_DDL = f"""
CREATE TABLE IF NOT EXISTS {ACTIVITY_TABLE} (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts             timestamptz NOT NULL DEFAULT now(),
  caller         text,                 -- email from the OAuth token (or null for the static key)
  client_id      text,
  tool           text NOT NULL,
  args           jsonb,                -- the tool input, including the SQL / search query
  ok             boolean,
  error          jsonb,
  objects        text[],               -- schema.table objects searched / described / queried
  rows           integer,
  engine         text,
  elapsed_ms     integer,
  as_of          text,
  result_summary jsonb                 -- compact: match scores, columns+row_count, metric value
);
CREATE INDEX IF NOT EXISTS mcp_activity_ts_idx      ON {ACTIVITY_TABLE} (ts DESC);
CREATE INDEX IF NOT EXISTS mcp_activity_caller_idx  ON {ACTIVITY_TABLE} (caller, ts DESC);
CREATE INDEX IF NOT EXISTS mcp_activity_tool_idx    ON {ACTIVITY_TABLE} (tool, ts DESC);
CREATE INDEX IF NOT EXISTS mcp_activity_objects_idx ON {ACTIVITY_TABLE} USING gin (objects);
CREATE OR REPLACE VIEW rvbbit.mcp_activity_summary AS
  SELECT tool, caller, count(*) AS calls, count(*) FILTER (WHERE NOT ok) AS errors,
         round(avg(elapsed_ms)) AS avg_ms, max(ts) AS last_seen
  FROM {ACTIVITY_TABLE} GROUP BY tool, caller;
CREATE OR REPLACE VIEW rvbbit.mcp_popular_objects AS
  SELECT obj AS object, count(*) AS touches, count(DISTINCT caller) AS users, max(ts) AS last_touch
  FROM {ACTIVITY_TABLE}, unnest(objects) AS obj GROUP BY obj ORDER BY touches DESC;
"""


def _ensure_activity_table():
    try:
        with _conn() as c:
            c.execute(_ACTIVITY_DDL)
    except Exception as e:  # noqa: BLE001 — logging is best-effort (e.g. a read-only role)
        print(f"WARNING: activity logging disabled (could not ensure {ACTIVITY_TABLE}): {e}", file=sys.stderr)


def _authenticated_caller():
    """The caller carried by the actual Warehouse access token."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        t = get_access_token()
        if t is not None:
            return getattr(t, "email", None) or getattr(t, "client_id", None), getattr(t, "client_id", None)
    except Exception:  # noqa: BLE001 — no auth context (stdio / shared-key) → anonymous
        pass
    return None, None


def _caller():
    """The audited caller, retaining the access token's client identity.

    A forwarded human identity is deliberately attribution-only and is honored
    only behind the legacy/static Hermes connection.  Request metadata is read
    from the SDK's task-local request context so every MCP tool gets the same
    attribution behavior, including tools that do not inject a FastMCP Context
    parameter.  OAuth callers always remain whatever their own signed token
    says they are.
    """
    caller, client_id = _authenticated_caller()
    forwarded = _FORWARDED_CALLER.get()
    if client_id == "static-key" and not forwarded:
        forwarded = _forwarded_mcp_caller(
            None, authenticated=(caller, client_id)
        )
    if client_id == "static-key" and forwarded:
        return forwarded, client_id
    return caller, client_id


_FORWARDED_EMAIL_RE = re.compile(r"^[^@\s]{1,128}@[^@\s]{1,190}$")
_HERMES_CALLER_META_KEY = "rvbbit.ai/hermes-caller"


def _mcp_request_metadata(ctx):
    """Return MCP request metadata across supported SDK/Pydantic shapes."""
    request_context = getattr(ctx, "request_context", None) if ctx is not None else None
    if request_context is None:
        # FastMCP's injected Context is only needed when a tool wants to use it
        # directly.  The low-level server keeps the same RequestContext in a
        # ContextVar for the lifetime of every tool invocation, including sync
        # handlers dispatched through AnyIO's worker thread.  Reading it here
        # makes forwarded attribution consistent across the whole MCP surface
        # without adding a public-looking ``ctx`` argument to every tool.
        try:
            from mcp.server.lowlevel.server import request_ctx

            request_context = request_ctx.get(None)
        except Exception:  # noqa: BLE001 — older optional MCP SDKs
            request_context = None
    # Current FastMCP lifts request.params._meta onto RequestContext.meta.
    # Retain the raw-request fallback for older SDKs and lightweight harnesses.
    metadata = getattr(request_context, "meta", None)
    if metadata is None:
        request = getattr(request_context, "request", None)
        request = getattr(request, "root", request)
        if isinstance(request, dict):
            params = request.get("params") or {}
        else:
            params = getattr(request, "params", None)
        if isinstance(params, dict):
            metadata = params.get("_meta") or params.get("meta") or {}
        else:
            metadata = getattr(params, "meta", None)
    if metadata is None:
        return {}
    if isinstance(metadata, dict):
        return metadata
    dump = getattr(metadata, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except TypeError:
            return dump()
    extra = getattr(metadata, "model_extra", None)
    return dict(extra) if isinstance(extra, dict) else {}


def _forwarded_mcp_caller(ctx=None, *, authenticated=None):
    """Resolve a verified Hermes/Google Chat email for display attribution.

    This does not confer permissions.  The shared key remains the authenticated
    principal and database access still follows that connection's normal rules.
    """
    _token_caller, client_id = authenticated or _authenticated_caller()
    if client_id != "static-key":
        return None
    envelope = _mcp_request_metadata(ctx).get(_HERMES_CALLER_META_KEY)
    if not isinstance(envelope, dict):
        return None
    if str(envelope.get("source") or "").strip().lower() != "hermes":
        return None
    platform = str(envelope.get("platform") or "").strip().lower().replace("-", "_")
    if platform not in {"google_chat", "gchat"}:
        return None
    email = str(envelope.get("user_id") or "").strip().lower()
    if len(email) > 254 or not _FORWARDED_EMAIL_RE.fullmatch(email):
        return None
    return email


def _with_forwarded_mcp_caller(ctx, fn):
    email = _forwarded_mcp_caller(ctx)
    if not email:
        return fn()
    token = _FORWARDED_CALLER.set(email)
    try:
        return fn()
    finally:
        _FORWARDED_CALLER.reset(token)


def _objects(tool, args, res):
    if not isinstance(res, dict):
        return None
    if tool in ("system_learning_status", "sync_system_learning", "ask_system_learning"):
        return ["rvbbit.system_learning_items"]
    if tool in (
        "create_live_app", "update_live_app", "get_live_app", "debug_live_app", "live_app_logs",
        "start_live_app", "stop_live_app", "live_app_status", "capture_live_app",
    ):
        slug = res.get("slug") or args.get("slug")
        return [f"live_app:{slug}"] if slug else None
    if tool == "artifact_view":
        slug = res.get("slug") or args.get("slug")
        return [f"artifact:{slug}"] if slug else None
    if tool == "dashboard_query" and args.get("dashboard"):
        return [f"artifact:{args['dashboard']}"]
    if tool == "search_data":
        return [m.get("object") for m in res.get("matches", []) if m.get("object")] or None
    if tool == "describe_table":
        return [res.get("table") or args.get("table")]
    if tool == "metric" and args.get("name"):
        return [args["name"]]
    if tool in ("validate_sql", "run_sql"):
        return res.get("rvbbit_tables") or None
    return None


def _summary(tool, res):
    if not isinstance(res, dict):
        return None
    if tool == "search_data":
        return {"matches": [{"object": m.get("object"), "score": m.get("score")} for m in res.get("matches", [])]}
    if tool == "run_sql":
        return {"columns": [c.get("name") for c in res.get("columns", [])],
                "row_count": res.get("row_count"), "truncated": res.get("truncated")}
    if tool == "run_sql_multi":
        rs = res.get("results") or {}
        return {"queries": {k: {"row_count": (v or {}).get("row_count"),
                                "error": bool((v or {}).get("error"))}
                            for k, v in rs.items()}}
    if tool == "metric":
        return {"result": res.get("result")}
    if tool == "validate_sql":
        return {"safe_select": res.get("safe_select"), "engine": res.get("engine")}
    if tool in (
        "create_live_app", "update_live_app", "get_live_app", "debug_live_app",
        "start_live_app", "stop_live_app", "live_app_status", "capture_live_app",
    ):
        return {
            "slug": res.get("slug"),
            "runtime_kind": res.get("runtime_kind"),
            "app_kind": res.get("app_kind"),
            "url": res.get("url"),
            "state": res.get("state") or (res.get("health") or {}).get("state"),
            "path": res.get("path"),
        }
    if tool == "list_live_apps":
        return {"count": len(res.get("live_apps", []))}
    if tool == "live_app_logs":
        return {"slug": res.get("slug"), "events": len(res.get("events", []))}
    if tool == "artifact_view":
        return {
            "slug": res.get("slug"),
            "version": res.get("version"),
            "app_kind": res.get("app_kind"),
        }
    if tool == "system_learning_status":
        return {
            "indexed_items": res.get("indexed_items"),
            "docs": res.get("docs"),
            "groups": [
                {"object_type": g.get("object_type"), "items": g.get("items")}
                for g in res.get("summary", [])
                if isinstance(g, dict)
            ],
            "breadcrumbs": [
                {"object_type": b.get("object_type"), "title": b.get("title")}
                for b in res.get("breadcrumbs", [])
                if isinstance(b, dict)
            ],
        }
    if tool == "sync_system_learning":
        status = res.get("status") or {}
        return {
            "indexed_items": status.get("indexed_items"),
            "docs": status.get("docs"),
            "last_run": status.get("last_run"),
        }
    if tool == "ask_system_learning":
        return {
            "count": res.get("count"),
            "hits": [
                {"doc_id": h.get("doc_id"), "title": h.get("title"), "score": h.get("score")}
                for h in res.get("hits", [])
                if isinstance(h, dict)
            ],
        }
    return None


def _record(tool, args, res, err, elapsed_ms, caller_override=None):
    caller, client_id = _caller()
    if caller_override:                  # browser/dashboard sessions aren't OAuth-token calls
        caller = caller_override
    rows = res.get("row_count") if isinstance(res, dict) else None
    engine = res.get("engine") if isinstance(res, dict) else None
    as_of = args.get("as_of") if isinstance(args, dict) else None
    try:
        with _conn() as c:
            c.execute(
                f"INSERT INTO {ACTIVITY_TABLE} "
                "(caller, client_id, tool, args, ok, error, objects, rows, engine, elapsed_ms, as_of, result_summary) "
                "VALUES (%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb)",
                (caller, client_id, tool, json.dumps(args, default=str), err is None,
                 json.dumps(err, default=str) if err is not None else None,
                 _objects(tool, args, res), rows, engine, elapsed_ms, as_of,
                 json.dumps(_summary(tool, res), default=str)))
    except Exception:  # noqa: BLE001 — never let logging break a tool call
        pass


def _logged(tool, args, thunk):
    t0 = time.time()
    res = err = None
    try:
        res = thunk()
        if isinstance(res, dict) and res.get("error"):
            err = res["error"]
        return res
    except Exception as e:  # noqa: BLE001
        # Degrade to the same structured {"error": ...} shape every tool already
        # returns, instead of raising a protocol-level tool error. Client
        # harnesses circuit-break on repeated protocol errors and mark the WHOLE
        # server unreachable — a bug in one tool must not take down the other
        # forty (field report: a describe_table crash benched the entire
        # connector mid-build).
        err = {"code": "EXCEPTION", "message": f"{type(e).__name__}: {e}",
               "hint": "unexpected server-side failure in this one tool; other tools are unaffected"}
        res = {"error": err}
        return res
    finally:
        _record(tool, args, res, err, int((time.time() - t0) * 1000))


def _identity_key(value):
    return str(value or "").strip().casefold()


def _artifact_activity_days(value):
    try:
        return max(7, min(int(value or 30), 90))
    except (TypeError, ValueError):
        return 30


def _artifact_activity_timezone(value):
    name = str(value or "UTC").strip()[:100] or "UTC"
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC", ZoneInfo("UTC")


def _artifact_activity_snapshot(slug, viewer, days=30, timezone_name="UTC"):
    """Shared, authenticated human page-view activity backed by mcp_activity.

    Query execution is intentionally not treated as a page view: one dashboard
    can issue ten bridge queries while another issues none.  The dedicated
    ``artifact_view`` activity event is emitted once after a real document has
    been resolved, so totals remain comparable across dashboards and apps.
    Gallery authentication is the access boundary: anyone who can see the
    artifact can inspect its audience, just as they can inspect its lineage.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", str(slug or ""), re.I):
        raise ValueError("artifact slug is invalid")
    days = _artifact_activity_days(days)
    timezone_name, local_zone = _artifact_activity_timezone(timezone_name)
    viewer_key = _identity_key(viewer)
    if not viewer_key:
        raise PermissionError("signed-in identity is unavailable")

    with _conn() as conn:
        artifact = conn.execute(
            "SELECT slug,name,owner_email,app_kind,latest_version "
            "FROM rvbbit.dashboards WHERE slug=%s",
            (slug,),
        ).fetchone()
        if not artifact:
            raise LookupError("artifact not found")
        owner = str(artifact.get("owner_email") or "").strip()

        summary = conn.execute(
            "SELECT count(*)::int AS total_views,"
            "count(DISTINCT lower(caller))::int AS unique_viewers,"
            "count(*) FILTER (WHERE ts>=now()-(%s::int*interval '1 day'))::int "
            "AS window_views,min(ts) AS tracked_since,max(ts) AS last_viewed_at "
            f"FROM {ACTIVITY_TABLE} WHERE tool='artifact_view' AND ok IS TRUE "
            "AND caller IS NOT NULL AND args->>'slug'=%s",
            (days, slug),
        ).fetchone() or {}
        daily_rows = conn.execute(
            "SELECT timezone(%s,ts)::date AS day,count(*)::int AS views,"
            "count(DISTINCT lower(caller))::int AS viewers "
            f"FROM {ACTIVITY_TABLE} WHERE tool='artifact_view' AND ok IS TRUE "
            "AND caller IS NOT NULL AND args->>'slug'=%s "
            "AND ts>=now()-(%s::int*interval '1 day') "
            "GROUP BY 1 ORDER BY 1",
            (timezone_name, slug, days),
        ).fetchall()
        viewer_rows = conn.execute(
            "SELECT lower(caller) AS viewer,count(*)::int AS views,"
            "count(DISTINCT timezone(%s,ts)::date)::int AS active_days,"
            "min(ts) AS first_viewed_at,max(ts) AS last_viewed_at "
            f"FROM {ACTIVITY_TABLE} WHERE tool='artifact_view' AND ok IS TRUE "
            "AND caller IS NOT NULL AND args->>'slug'=%s "
            "GROUP BY lower(caller) ORDER BY max(ts) DESC,count(*) DESC LIMIT 50",
            (timezone_name, slug),
        ).fetchall()
        version_rows = conn.execute(
            "SELECT coalesce(nullif(args->>'version',''),'unknown') AS version,"
            "count(*)::int AS views "
            f"FROM {ACTIVITY_TABLE} WHERE tool='artifact_view' AND ok IS TRUE "
            "AND caller IS NOT NULL AND args->>'slug'=%s "
            "GROUP BY 1 ORDER BY count(*) DESC,1",
            (slug,),
        ).fetchall()

    by_day = {str(row["day"]): dict(row) for row in daily_rows}
    today = datetime.now(local_zone).date()
    series = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        observed = by_day.get(day.isoformat()) or {}
        series.append({
            "day": day.isoformat(),
            "views": int(observed.get("views") or 0),
            "viewers": int(observed.get("viewers") or 0),
        })

    total_views = int(summary.get("total_views") or 0)
    unique_viewers = int(summary.get("unique_viewers") or 0)
    return {
        "artifact": {
            "slug": str(artifact["slug"]),
            "name": str(artifact.get("name") or artifact["slug"]),
            "app_kind": str(artifact.get("app_kind") or "dashboard"),
            "owner": owner,
            "latest_version": int(artifact.get("latest_version") or 1),
        },
        "window_days": days,
        "timezone": timezone_name,
        "summary": {
            "total_views": total_views,
            "unique_viewers": unique_viewers,
            "repeat_views": max(0, total_views - unique_viewers),
            "window_views": int(summary.get("window_views") or 0),
            "tracked_since": summary.get("tracked_since"),
            "last_viewed_at": summary.get("last_viewed_at"),
        },
        "series": series,
        "viewers": [
            {
                **dict(row),
                "is_you": _identity_key(row.get("viewer")) == viewer_key,
            }
            for row in viewer_rows
        ],
        "versions": [dict(row) for row in version_rows],
    }


def _artifact_view_surface(request):
    referer = str(request.headers.get("referer") or "")
    try:
        from urllib.parse import urlsplit
        path = urlsplit(referer).path
    except ValueError:
        path = ""
    if path in {"/", "/gallery"}:
        return "gallery"
    if path.startswith("/calliope"):
        return "calliope"
    return "internal" if path.startswith("/") else "direct"


def _record_artifact_view(request, document, viewer, auth_via=None):
    """Best-effort one-event receipt for an actual artifact document open."""
    if str(getattr(request, "method", "GET")).upper() != "GET":
        return
    purpose = " ".join(str(request.headers.get(name) or "").lower()
                       for name in ("purpose", "sec-purpose"))
    if "prefetch" in purpose:
        return
    slug = str(document.get("slug") or request.path_params.get("slug") or "")
    if not slug or not viewer:
        return
    args = {
        "slug": slug,
        "version": document.get("version"),
        "app_kind": document.get("app_kind") or "dashboard",
        "surface": _artifact_view_surface(request),
        "auth_via": str(auth_via or "session")[:32],
    }
    result = {
        "slug": slug,
        "version": document.get("version"),
        "app_kind": document.get("app_kind") or "dashboard",
    }
    _record("artifact_view", args, result, None, 0, caller_override=str(viewer))


# ── dashboards registry (Phase 0: publish → store → serve live, outside Claude) ──
# Claude publishes an artifact; it's stored versioned in rvbbit.dashboards and served at
# <public>/d/<slug> behind the same login. The artifact fetches live data via the injected
# `rvbbitQuery(sql)` client → /api/d/<slug>/q, which runs read-only on the MIRROR
# (safe_select-gated) and logs to mcp_activity. The dashboard outlives the chat.

# NOTE: extension migration 0200_hub_front_door.sql carries a shape-identical
# copy of the dashboards/dashboard_versions/dashboard_deps/live_apps DDL —
# fresh installs migrate before this service ever connects, and 0200's
# artifact_index view needs the tables to exist. Change one, change both.
_DASHBOARDS_DDL = """
CREATE TABLE IF NOT EXISTS rvbbit.dashboards (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug        text UNIQUE NOT NULL,
  name        text NOT NULL,
  description text,
  owner_email text,
  team        text,
  status      text DEFAULT 'live',            -- 'live' | 'materialized' (dead tree)
  latest_version int DEFAULT 1,
  created_at  timestamptz DEFAULT now(),
  updated_at  timestamptz DEFAULT now()
);
ALTER TABLE rvbbit.dashboards ADD COLUMN IF NOT EXISTS runtime_kind text NOT NULL DEFAULT 'html';
ALTER TABLE rvbbit.dashboards ADD COLUMN IF NOT EXISTS app_kind text NOT NULL DEFAULT 'dashboard';
ALTER TABLE rvbbit.dashboards ADD COLUMN IF NOT EXISTS manifest jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE rvbbit.dashboards ADD COLUMN IF NOT EXISTS last_health jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE rvbbit.dashboards ADD COLUMN IF NOT EXISTS last_debug_at timestamptz;
CREATE TABLE IF NOT EXISTS rvbbit.dashboard_versions (
  dashboard_id bigint NOT NULL REFERENCES rvbbit.dashboards(id) ON DELETE CASCADE,
  version      int NOT NULL,
  html         text NOT NULL,
  kind         text DEFAULT 'live',
  created_by   text, created_at timestamptz DEFAULT now(), notes text,
  PRIMARY KEY (dashboard_id, version)
);
ALTER TABLE rvbbit.dashboard_versions ADD COLUMN IF NOT EXISTS manifest jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE rvbbit.dashboard_versions ADD COLUMN IF NOT EXISTS source_files jsonb NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX IF NOT EXISTS dashboards_team_idx ON rvbbit.dashboards (team, updated_at DESC);
-- Agent-generated semantic metadata is a derived, regenerable overlay keyed to
-- one immutable artifact version.  Publication never waits for or depends on
-- this row; the effective manifest merges it at read time.
CREATE TABLE IF NOT EXISTS rvbbit.artifact_semantic_enrichments (
  dashboard_id bigint NOT NULL,
  version int NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  input_hash text NOT NULL,
  semantic_map jsonb NOT NULL DEFAULT '{"schema_version":"rvbbit.semantic-map.v1","objects":[]}'::jsonb,
  verification jsonb NOT NULL DEFAULT '{}'::jsonb,
  agent_run_id text,
  model text,
  prompt_version text NOT NULL,
  attempts int NOT NULL DEFAULT 0,
  last_error text,
  not_before timestamptz NOT NULL DEFAULT now(),
  enqueued_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  completed_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (dashboard_id, version),
  FOREIGN KEY (dashboard_id, version)
    REFERENCES rvbbit.dashboard_versions(dashboard_id, version) ON DELETE CASCADE,
  CONSTRAINT artifact_semantic_enrichments_status_check CHECK (
    status IN ('pending','running','ready','partial','failed','disabled')
  )
);
CREATE INDEX IF NOT EXISTS artifact_semantic_enrichments_queue_idx
  ON rvbbit.artifact_semantic_enrichments (status, not_before, enqueued_at);
-- staged artifact uploads: lets an agent ship a large HTML/source payload once
-- (optionally in chunks) and then publish by handle, instead of re-transmitting
-- the whole document through every publish/update call. Short-lived by design.
CREATE TABLE IF NOT EXISTS rvbbit.mcp_artifacts (
  artifact_id text PRIMARY KEY,
  name        text,
  content     text NOT NULL,
  sha256      text NOT NULL,
  bytes       int NOT NULL,
  created_by  text,
  created_at  timestamptz DEFAULT now()
);
-- the derived dependency index (regenerated by dashboard_crawl; safe to truncate + rebuild)
CREATE TABLE IF NOT EXISTS rvbbit.dashboard_deps (
  dashboard_id bigint NOT NULL REFERENCES rvbbit.dashboards(id) ON DELETE CASCADE,
  version      int NOT NULL,
  kind         text NOT NULL,        -- 'query' | 'semantic' | 'table' | 'metric'
  object_ref   text,                 -- schema.table | metric name | semantic object id
  base_sql     text,                 -- the panel/evaluator SQL
  source       text,                 -- 'parse' | 'runtime' | 'llm'
  confidence   real DEFAULT 1.0,
  created_at   timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dashboard_deps_did_idx ON rvbbit.dashboard_deps (dashboard_id);
CREATE INDEX IF NOT EXISTS dashboard_deps_obj_idx ON rvbbit.dashboard_deps (object_ref);
CREATE OR REPLACE VIEW rvbbit.dashboard_sources AS   -- forward: a dashboard's data edges
  SELECT d.slug, d.name, d.team, dd.kind, dd.object_ref, dd.base_sql, dd.source
  FROM rvbbit.dashboard_deps dd JOIN rvbbit.dashboards d
    ON d.id = dd.dashboard_id AND d.latest_version = dd.version;
CREATE OR REPLACE VIEW rvbbit.dashboard_dependents AS  -- reverse: object -> dashboards (impact)
  SELECT dd.object_ref AS object, dd.kind, count(DISTINCT d.id) AS dashboards,
         array_agg(DISTINCT d.slug) AS slugs
  FROM rvbbit.dashboard_deps dd JOIN rvbbit.dashboards d
    ON d.id = dd.dashboard_id AND d.latest_version = dd.version
  WHERE dd.kind IN ('table', 'metric') AND dd.object_ref IS NOT NULL
  GROUP BY dd.object_ref, dd.kind;
CREATE OR REPLACE VIEW rvbbit.live_apps AS
  SELECT d.id, d.slug, d.name, d.description, d.owner_email, d.team, d.status,
         d.runtime_kind, d.app_kind, d.latest_version, d.manifest, d.last_health,
         d.last_debug_at, d.created_at, d.updated_at,
         coalesce(dep.queries, 0)::int AS queries,
         coalesce(dep.tables, 0)::int AS tables,
         coalesce(dep.metrics, 0)::int AS metrics,
         coalesce(dep.semantic_objects, 0)::int AS semantic_objects
  FROM rvbbit.dashboards d
  LEFT JOIN LATERAL (
    SELECT
           count(*) FILTER (WHERE kind = 'query') AS queries,
           count(*) FILTER (WHERE kind = 'table') AS tables,
           count(*) FILTER (WHERE kind = 'metric') AS metrics,
           count(*) FILTER (WHERE kind = 'semantic') AS semantic_objects
    FROM rvbbit.dashboard_deps
    WHERE dashboard_id = d.id AND version = d.latest_version
  ) dep ON true;
"""


def _ensure_dashboard_tables():
    try:
        with _conn() as c:
            c.execute(_DASHBOARDS_DDL)
    except Exception as e:   # noqa: BLE001
        print(f"WARNING: dashboards disabled (could not ensure tables): {e}", file=sys.stderr)


def _slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "dashboard"


def _dash_url(slug):
    public = os.environ.get("WAREHOUSE_PUBLIC_URL", "").rstrip("/")
    return f"{public}/d/{slug}" if public else None


def _live_app_url(slug):
    public = os.environ.get("WAREHOUSE_PUBLIC_URL", "").rstrip("/")
    return f"{public}/apps/{slug}" if public else None


# ── The Hub (docs/HUB_PLAN.md) ───────────────────────────────────────────
# The DataRabbit front door for chat-first users: /?hub on the LENS host is
# a browsable index of everything made through this server. Tools return
# hub_url alongside url so agents hand users the gallery link, not just the
# bare artifact — distribution through the transcript.

def _artifact_kind(app_kind):
    return "dashboard" if (app_kind or "") == "dashboard" else "app"


def _hub_url(app_kind, slug):
    lens = os.environ.get("LENS_PUBLIC_URL", "").rstrip("/")
    return f"{lens}/hub?sel={_artifact_kind(app_kind)}:{slug}" if lens else None


# Captures are JPEG as of 4.1.9 — a gallery card renders ~290px wide, and the
# old 1200x750 PNGs averaged ~190KB (worst case 414KB), so a 40-artifact wall
# was multiple megabytes of first load. The URL keeps its .png spelling for
# compatibility (lens's rv-shot proxy hardcodes it); the response's
# content-type is what actually tells the browser, and the proxy forwards ours.
# PNG is still READ so pre-4.1.9 captures keep serving until they're refreshed.
_THUMB_EXTS = (("jpg", "image/jpeg"), ("png", "image/png"))
_THUMB_W, _THUMB_H, _THUMB_Q = 800, 500, 72


def _thumb_path(kind, slug, ext="jpg"):
    return _live_app_capture_root() / "thumbs" / kind / f"{slug}.{ext}"


def _thumb_existing(kind, slug):
    """(path, media_type) of the capture to serve, preferring the current
    format; (None, None) when nothing has been rendered yet."""
    for ext, mime in _THUMB_EXTS:
        p = _thumb_path(kind, slug, ext)
        if p.is_file():
            return p, mime
    return None, None


def _thumb_stale(kind, slug, updated_at) -> bool:
    """Missing, or older than the artifact's last publish. Republishing an
    artifact is exactly when its picture stops being true."""
    p, _ = _thumb_existing(kind, slug)
    if p is None:
        return True
    try:
        return p.stat().st_mtime < updated_at.timestamp()
    except Exception:   # noqa: BLE001 — unreadable stat: leave what we have
        return False


_THUMBS_IN_FLIGHT = set()
# Captures are playwright renders — cheap enough singly, a stampede when a
# gallery of 40 uncaptured artifacts loads. Two at a time, rest queue.
_THUMBS_GATE = threading.Semaphore(2)


def _auto_thumb(app_kind, slug):
    """Best-effort background thumbnail for the Hub gallery: render the stored
    HTML through the same bridge-injected capture the capture tool uses, into
    a stable path (<capture_root>/thumbs/<kind>/<slug>.png) that /thumbs
    serves and the lens gallery proxies. Never blocks or fails a publish;
    also fired lazily by /thumbs on miss/stale, so thumbnails need no
    manual step anywhere."""
    kind = _artifact_kind(app_kind)
    key = f"{kind}:{slug}"
    if key in _THUMBS_IN_FLIGHT:
        return

    def _work():
        try:
            with _THUMBS_GATE:
                app, row = _load_live_app_version(slug)
                if not app or (app.get("runtime_kind") or "html") != "html":
                    return
                path = _thumb_path(kind, slug)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp.jpg")
                _capture_html_with_playwright((row or {}).get("html") or "", tmp,
                                              width=_THUMB_W, height=_THUMB_H, full_page=False,
                                              wait_ms=1200, quality=_THUMB_Q)
                tmp.replace(path)
                # Retire the pre-4.1.9 PNG so _thumb_existing stops having to
                # choose and the volume doesn't keep both forever.
                legacy = _thumb_path(kind, slug, "png")
                if legacy.is_file():
                    legacy.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            print(f"auto-thumb {key}: {e}", file=sys.stderr)
        finally:
            _THUMBS_IN_FLIGHT.discard(key)

    _THUMBS_IN_FLIGHT.add(key)
    threading.Thread(target=_work, name=f"thumb-{slug}", daemon=True).start()


def _coerce_json_object(value, field):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(f"{field} must be a JSON object: {e}") from e
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"{field} must be a JSON object")


def _json_default(obj):
    return json.dumps(obj or {}, default=str)


_SEMANTIC_MAP_SCHEMA = "rvbbit.semantic-map.v1"
_SEMANTIC_OBJECT_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,127}$")
_SEMANTIC_PARAM_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
_SEMANTIC_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z][a-zA-Z0-9_]*)\s*\}\}")
_SEMANTIC_PARAM_TYPES = {
    "text", "number", "integer", "boolean", "date", "timestamp", "text_array", "number_array",
}
_SEMANTIC_KINDS = {"scalar", "cell", "status"}
_SEMANTIC_SHAPES = {"scalar"}
_SEMANTIC_SENSITIVE_RE = re.compile(
    r"(?:secret|token|password|passwd|auth|cookie|session|api[-_]?key)",
    re.I,
)


def _semantic_text(value, limit, *, required=False):
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()[:limit]
    if required and not text:
        raise ValueError("semantic object text is required")
    return text


def _semantic_json_value(value, depth=0):
    """Bound inert manifest/context JSON without preserving credential-shaped keys."""
    if depth > 5:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        # Context values are query inputs, not prose: preserve meaningful
        # whitespace exactly while removing control characters and bounding size.
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", value)[:1000]
    if isinstance(value, (list, tuple)):
        return [_semantic_json_value(item, depth + 1) for item in list(value)[:80]]
    if isinstance(value, dict):
        clean = {}
        for raw_key, raw_value in list(value.items())[:80]:
            key = re.sub(r"[^a-zA-Z0-9_.:-]", "", str(raw_key))[:80]
            if not key or _SEMANTIC_SENSITIVE_RE.search(key):
                continue
            clean[key] = _semantic_json_value(raw_value, depth + 1)
        return clean
    return _semantic_text(value, 1000)


def _normalize_semantic_parameter(name, value):
    if not _SEMANTIC_PARAM_RE.fullmatch(str(name or "")):
        raise ValueError(f"invalid semantic parameter name: {name}")
    if _SEMANTIC_SENSITIVE_RE.search(str(name)):
        raise ValueError(f"semantic parameter cannot contain credentials: {name}")
    raw = value if isinstance(value, dict) else {"default": value}
    kind = str(raw.get("type") or "").strip().lower()
    default = _semantic_json_value(raw.get("default"))
    if not kind:
        if isinstance(default, bool):
            kind = "boolean"
        elif isinstance(default, int):
            kind = "integer"
        elif isinstance(default, float):
            kind = "number"
        elif isinstance(default, list):
            kind = "number_array" if all(isinstance(item, (int, float)) for item in default) else "text_array"
        else:
            kind = "text"
    if kind not in _SEMANTIC_PARAM_TYPES:
        raise ValueError(
            f"semantic parameter {name} type must be one of: "
            + ", ".join(sorted(_SEMANTIC_PARAM_TYPES))
        )
    out = {
        "type": kind,
        "default": default,
        "label": _semantic_text(raw.get("label") or str(name).replace("_", " ").title(), 160),
    }
    source = _semantic_text(raw.get("source"), 240)
    if source:
        out["source"] = source
    if raw.get("rolling") is not None:
        out["rolling"] = bool(raw.get("rolling"))
    return out


def _normalize_semantic_binding(value):
    raw = {"selector": value} if isinstance(value, str) else value
    if not isinstance(raw, dict):
        raise ValueError("semantic object bindings must be selectors or JSON objects")
    out = {}
    for key, limit in (
        ("selector", 800),
        ("element_id", 240),
        ("name", 240),
        ("role", 120),
        ("chart_dataset", 240),
        ("table_column", 240),
        ("value_source", 320),
    ):
        text = _semantic_text(raw.get(key), limit)
        if text:
            out[key] = text
    if raw.get("dataset_index") is not None:
        try:
            out["dataset_index"] = max(0, min(int(raw["dataset_index"]), 100_000))
        except (TypeError, ValueError) as exc:
            raise ValueError("semantic binding dataset_index must be an integer") from exc
    context = _semantic_json_value(raw.get("context"))
    if isinstance(context, dict) and context:
        out["context"] = context
    if not any(out.get(key) for key in ("selector", "element_id", "name")):
        raise ValueError("semantic object binding requires selector, element_id, or name")
    return out


def _normalize_semantic_object(value):
    if not isinstance(value, dict):
        raise ValueError("semantic map objects must be JSON objects")
    object_id = str(value.get("id") or "").strip()
    if not _SEMANTIC_OBJECT_ID_RE.fullmatch(object_id):
        raise ValueError(
            "semantic object id must start with a letter and contain only letters, "
            f"numbers, `_`, `.`, `:`, or `-`: {object_id or '(missing)'}"
        )
    if _SEMANTIC_SENSITIVE_RE.search(object_id):
        raise ValueError(f"semantic object id cannot contain credentials: {object_id}")
    kind = str(value.get("kind") or "scalar").strip().lower()
    kind = {"value": "scalar", "number": "scalar", "kpi": "scalar"}.get(kind, kind)
    if kind not in _SEMANTIC_KINDS:
        raise ValueError(f"semantic object {object_id} has unsupported kind: {kind}")

    raw_meaning = value.get("meaning") if isinstance(value.get("meaning"), dict) else {}
    label = _semantic_text(
        raw_meaning.get("label") or value.get("label") or object_id.replace("_", " ").replace("-", " ").title(),
        240,
        required=True,
    )
    meaning = {
        "label": label,
        "description": _semantic_text(
            raw_meaning.get("description") or value.get("description"), 1400
        ),
        "unit": _semantic_text(raw_meaning.get("unit") or value.get("unit"), 120),
        "formula": _semantic_text(raw_meaning.get("formula") or value.get("formula"), 1000),
    }
    meaning = {key: item for key, item in meaning.items() if item}

    raw_bindings = value.get("bindings")
    if raw_bindings is None and value.get("binding") is not None:
        raw_bindings = [value.get("binding")]
    if raw_bindings is None:
        raw_bindings = []
    if not isinstance(raw_bindings, list):
        raw_bindings = [raw_bindings]
    bindings = [_normalize_semantic_binding(binding) for binding in raw_bindings[:16]]

    raw_parameters = value.get("parameters") or {}
    if not isinstance(raw_parameters, dict):
        raise ValueError(f"semantic object {object_id} parameters must be a JSON object")
    parameters = {
        str(name): _normalize_semantic_parameter(name, spec)
        for name, spec in list(raw_parameters.items())[:32]
    }

    raw_evaluator = value.get("evaluator") or value.get("replay")
    if not isinstance(raw_evaluator, dict):
        raise ValueError(f"semantic object {object_id} requires an evaluator")
    evaluator_sql = str(raw_evaluator.get("sql") or "").strip()
    if not evaluator_sql:
        raise ValueError(f"semantic object {object_id} evaluator.sql is required")
    if len(evaluator_sql) > 50_000:
        raise ValueError(f"semantic object {object_id} evaluator.sql exceeds 50000 characters")
    shape = str(raw_evaluator.get("shape") or "scalar").lower()
    if shape not in _SEMANTIC_SHAPES:
        raise ValueError(f"semantic object {object_id} evaluator shape is unsupported: {shape}")
    evaluator = {
        "sql": evaluator_sql,
        "shape": shape,
        "value_column": _semantic_text(raw_evaluator.get("value_column") or "value", 160),
    }
    if raw_evaluator.get("row_index") is not None:
        try:
            evaluator["row_index"] = max(0, min(int(raw_evaluator["row_index"]), 100_000))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"semantic object {object_id} row_index must be an integer") from exc

    placeholders = set(_SEMANTIC_PLACEHOLDER_RE.findall(evaluator_sql))
    undeclared = sorted(placeholders - set(parameters))
    if undeclared:
        raise ValueError(
            f"semantic object {object_id} has undeclared SQL parameters: {', '.join(undeclared)}"
        )
    missing_defaults = sorted(
        name for name in placeholders if parameters[name].get("default") is None
    )
    if missing_defaults:
        raise ValueError(
            f"semantic object {object_id} parameters need defaults for publication: "
            + ", ".join(missing_defaults)
        )

    display = _semantic_json_value(value.get("display") or {})
    source_queries = [
        _semantic_text(item, 160)
        for item in list(value.get("source_queries") or [])[:24]
        if _semantic_text(item, 160)
    ]
    definition_payload = {
        "kind": kind,
        "meaning": meaning,
        "parameters": parameters,
        "evaluator": evaluator,
    }
    definition_hash = hashlib.sha256(
        json.dumps(definition_payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:20]
    out = {
        "id": object_id,
        "kind": kind,
        "meaning": meaning,
        "parameters": parameters,
        "bindings": bindings,
        "evaluator": evaluator,
        "definition_hash": definition_hash,
    }
    if isinstance(display, dict) and display:
        out["display"] = display
    if source_queries:
        out["source_queries"] = source_queries
    return out


def _normalize_semantic_manifest(manifest):
    """Canonicalize the optional versioned semantic source map in an artifact manifest."""
    doc = _coerce_json_object(manifest, "manifest") if manifest is not None else {}
    doc = dict(doc)
    raw_map = doc.get("semantic_map")
    if raw_map is None:
        return doc
    if isinstance(raw_map, list):
        raw_map = {"objects": raw_map}
    if not isinstance(raw_map, dict):
        raise ValueError("manifest.semantic_map must be a JSON object")
    requested_schema = str(raw_map.get("schema_version") or _SEMANTIC_MAP_SCHEMA)
    if requested_schema != _SEMANTIC_MAP_SCHEMA:
        raise ValueError(
            f"unsupported semantic map schema {requested_schema!r}; "
            f"expected {_SEMANTIC_MAP_SCHEMA!r}"
        )
    raw_objects = raw_map.get("objects") or []
    if not isinstance(raw_objects, list):
        raise ValueError("manifest.semantic_map.objects must be a list")
    if len(raw_objects) > 160:
        raise ValueError("manifest.semantic_map supports at most 160 objects")
    objects = [_normalize_semantic_object(item) for item in raw_objects]
    ids = [item["id"] for item in objects]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        raise ValueError("duplicate semantic object ids: " + ", ".join(duplicates))
    doc["semantic_map"] = {
        "schema_version": _SEMANTIC_MAP_SCHEMA,
        "objects": objects,
        "description": _semantic_text(raw_map.get("description"), 1000),
    }
    if not doc["semantic_map"]["description"]:
        doc["semantic_map"].pop("description")
    return doc


def _semantic_sql_literal(value, kind):
    if value is None:
        return "NULL"
    if kind == "boolean":
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered not in {"true", "false", "1", "0", "yes", "no"}:
                raise ValueError(f"cannot coerce {value!r} to boolean")
            value = lowered in {"true", "1", "yes"}
        return "TRUE" if bool(value) else "FALSE"
    if kind in {"number", "integer"}:
        try:
            number = int(value) if kind == "integer" else float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"cannot coerce {value!r} to {kind}") from exc
        if isinstance(number, float) and not math.isfinite(number):
            raise ValueError(f"cannot coerce {value!r} to finite number")
        return str(number)
    if kind in {"date", "timestamp"}:
        raw = str(value).strip()
        try:
            if kind == "date":
                datetime.fromisoformat(raw).date()
            else:
                _normalize_as_of(raw)
        except ValueError as exc:
            raise ValueError(f"cannot coerce {value!r} to {kind}") from exc
        return "'" + raw.replace("'", "''") + "'"
    if kind in {"text_array", "number_array"}:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"cannot coerce {value!r} to {kind}")
        child_kind = "text" if kind == "text_array" else "number"
        items = [_semantic_sql_literal(item, child_kind) for item in list(value)[:500]]
        if items:
            return "ARRAY[" + ",".join(items) + "]"
        target = "text[]" if kind == "text_array" else "numeric[]"
        return f"cast(ARRAY[] as {target})"
    bounded = str(value).replace("\x00", "")[:4000]
    return "'" + bounded.replace("'", "''") + "'"


def _render_semantic_sql(semantic_object, context=None):
    parameters = semantic_object.get("parameters") or {}
    supplied = context if isinstance(context, dict) else {}
    resolved = {}
    for name, spec in parameters.items():
        raw = supplied.get(name, spec.get("default"))
        resolved[name] = _semantic_json_value(raw)
    sql_text = str((semantic_object.get("evaluator") or {}).get("sql") or "")

    def replace(match):
        name = match.group(1)
        spec = parameters.get(name)
        if not spec:
            raise ValueError(f"undeclared semantic SQL parameter: {name}")
        return _semantic_sql_literal(resolved.get(name), spec.get("type") or "text")

    return _SEMANTIC_PLACEHOLDER_RE.sub(replace, sql_text), resolved


def _semantic_result_value(semantic_object, result):
    if not isinstance(result, dict) or result.get("error"):
        return None, None
    rows = result.get("rows") or []
    evaluator = semantic_object.get("evaluator") or {}
    row_index = int(evaluator.get("row_index") or 0)
    if not (0 <= row_index < len(rows)):
        return None, None
    row = rows[row_index]
    columns = [
        column if isinstance(column, str) else column.get("name")
        for column in (result.get("columns") or [])
    ]
    if isinstance(row, (list, tuple)):
        row = {
            name: row[index]
            for index, name in enumerate(columns)
            if name and index < len(row)
        }
    if not isinstance(row, dict):
        return None, None
    value_column = str(evaluator.get("value_column") or "value")
    if value_column in row:
        return row[value_column], value_column
    if len(row) == 1:
        key = next(iter(row))
        return row[key], str(key)
    return None, None


def _validate_semantic_manifest(manifest, *, execute=True):
    semantic_map = (manifest or {}).get("semantic_map") or {}
    objects = semantic_map.get("objects") or []
    report = {
        "schema_version": semantic_map.get("schema_version") or _SEMANTIC_MAP_SCHEMA,
        "object_count": len(objects),
        "objects": [],
    }
    for semantic_object in objects:
        sql_text, context = _render_semantic_sql(semantic_object)
        validation = tool_validate_sql(sql_text)
        if not validation.get("valid") or not validation.get("safe_select"):
            raise ValueError(
                f"semantic object {semantic_object['id']} replay SQL is not a safe read-only query: "
                + str(validation.get("error") or validation.get("reason") or "validation failed")
            )
        item = {
            "id": semantic_object["id"],
            "definition_hash": semantic_object["definition_hash"],
            "engine": validation.get("engine"),
            "context": context,
            "verified": not execute,
        }
        if execute:
            result = tool_run_sql(sql_text, limit=2)
            if result.get("error"):
                raise ValueError(
                    f"semantic object {semantic_object['id']} replay failed: "
                    + str((result.get("error") or {}).get("message") or result["error"])
                )
            shape = (semantic_object.get("evaluator") or {}).get("shape")
            value, value_column = _semantic_result_value(semantic_object, result)
            if shape == "scalar":
                if int(result.get("row_count") or 0) != 1:
                    raise ValueError(
                        f"semantic object {semantic_object['id']} scalar replay must return exactly one row"
                    )
                if value_column is None:
                    raise ValueError(
                        f"semantic object {semantic_object['id']} replay did not return "
                        f"value column {(semantic_object.get('evaluator') or {}).get('value_column')!r}"
                    )
            item.update({
                "verified": True,
                "value": value,
                "value_column": value_column,
                "row_count": result.get("row_count"),
                "elapsed_ms": result.get("elapsed_ms"),
            })
        report["objects"].append(item)
    return report


def _prepare_artifact_manifest(manifest, *, execute=True):
    doc = _normalize_semantic_manifest(manifest)
    return doc, _validate_semantic_manifest(doc, execute=execute)


def _prepare_artifact_manifest_for_publish(
    manifest,
    *,
    fallback_semantic_map=None,
    execute=True,
):
    """Keep semantic metadata advisory: quarantine it instead of blocking HTML.

    The strict helper above remains useful for validation tools and tests. Publish
    paths use this wrapper because an optional debug-symbol layer must never make
    an otherwise valid artifact fail to version. On update, a previously verified
    authored map may be retained when the replacement is malformed.
    """
    raw = dict(_coerce_json_object(manifest, "manifest"))
    raw.pop("semantic_map_warning", None)
    try:
        return _prepare_artifact_manifest(raw, execute=execute)
    except ValueError as exc:
        if "semantic_map" not in raw:
            raise
        rejected_map = raw.pop("semantic_map")
        if fallback_semantic_map is not None:
            raw["semantic_map"] = fallback_semantic_map
        try:
            doc, report = _prepare_artifact_manifest(raw, execute=execute)
        except ValueError:
            raw.pop("semantic_map", None)
            doc, report = _prepare_artifact_manifest(raw, execute=execute)
        warning = {
            "code": "SEMANTIC_MAP_QUARANTINED",
            "message": _semantic_text(exc, 1200),
            "source_hash": hashlib.sha256(
                json.dumps(rejected_map, sort_keys=True, default=str).encode()
            ).hexdigest()[:20],
        }
        doc["semantic_map_warning"] = warning
        report["warning"] = warning
        return doc, report


_SEMANTIC_ENRICH_PROMPT_VERSION = "artifact-semantic-enricher.v2"
_SEMANTIC_ENRICH_MODEL = os.environ.get(
    "WAREHOUSE_SEMANTIC_ENRICH_MODEL", "openai/gpt-5.6-sol"
).strip() or "openai/gpt-5.6-sol"
_SEMANTIC_ENRICH_WAKE = threading.Event()
_SEMANTIC_ENRICH_THREAD = None
_SEMANTIC_ENRICH_THREAD_LOCK = threading.Lock()


def _semantic_enrichment_enabled():
    return os.environ.get("WAREHOUSE_SEMANTIC_ENRICHMENT", "1").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


def _semantic_binding_keys(semantic_object):
    keys = set()
    for binding in (semantic_object or {}).get("bindings") or []:
        if not isinstance(binding, dict):
            continue
        for name in ("selector", "element_id", "name"):
            value = str(binding.get(name) or "").strip()
            if value:
                keys.add((name, value))
    return keys


def _merge_semantic_overlay(manifest, enrichment):
    """Merge verified derived objects without rewriting the authored manifest.

    Authored ids and DOM bindings win.  That lets a human or builder provide a
    precise definition while the publication-side compiler fills only the gaps.
    """
    doc = dict(manifest or {})
    authored_map = doc.get("semantic_map") if isinstance(doc.get("semantic_map"), dict) else {}
    authored_objects = [
        item for item in (authored_map.get("objects") or []) if isinstance(item, dict)
    ]
    status = str((enrichment or {}).get("status") or "none")
    overlay_map = (
        (enrichment or {}).get("semantic_map")
        if status in {"ready", "partial"}
        and isinstance((enrichment or {}).get("semantic_map"), dict)
        else {}
    )
    authored_ids = {str(item.get("id") or "") for item in authored_objects}
    authored_bindings = set().union(*(
        _semantic_binding_keys(item) for item in authored_objects
    )) if authored_objects else set()
    generated = []
    for item in overlay_map.get("objects") or []:
        if not isinstance(item, dict) or str(item.get("id") or "") in authored_ids:
            continue
        item_bindings = _semantic_binding_keys(item)
        if item_bindings and item_bindings & authored_bindings:
            continue
        generated.append(item)
    objects = authored_objects + generated
    if objects or authored_map or overlay_map:
        merged_map = {
            "schema_version": _SEMANTIC_MAP_SCHEMA,
            "objects": objects,
        }
        description = _semantic_text(
            authored_map.get("description")
            or overlay_map.get("description")
            or "Business meanings and replayable SQL attached by the RVBBIT artifact compiler.",
            1000,
        )
        if description:
            merged_map["description"] = description
        doc["semantic_map"] = merged_map
    verification = (enrichment or {}).get("verification") or {}
    doc["semantic_enrichment"] = {
        "status": status,
        "source": "rvbbit-agent-overlay",
        "prompt_version": (enrichment or {}).get("prompt_version"),
        "model": (enrichment or {}).get("model"),
        "verified_objects": int(verification.get("verified_count") or len(generated)),
        "rejected_objects": int(verification.get("rejected_count") or 0),
        "coverage": verification.get("coverage"),
        "updated_at": _iso_utc((enrichment or {}).get("updated_at")),
    }
    return doc


def _semantic_enrichment_row(dashboard_id, version):
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT * FROM rvbbit.artifact_semantic_enrichments "
                "WHERE dashboard_id=%s AND version=%s",
                (dashboard_id, version),
            ).fetchone()
        return dict(row) if row else None
    except Exception:  # noqa: BLE001 — old installs simply have no overlay yet
        return None


def _effective_artifact_manifest(dashboard_id, version, manifest, enrichment=None):
    row = enrichment if enrichment is not None else _semantic_enrichment_row(dashboard_id, version)
    return _merge_semantic_overlay(manifest or {}, row)


def _semantic_enrichment_public(row):
    if not row:
        return {"status": "none", "eligible": False}
    verification = row.get("verification") or {}
    return {
        "status": row.get("status"),
        "eligible": row.get("status") != "disabled",
        "version": int(row.get("version") or 0),
        "attempts": int(row.get("attempts") or 0),
        "verified_objects": int(verification.get("verified_count") or 0),
        "rejected_objects": int(verification.get("rejected_count") or 0),
        "coverage": verification.get("coverage"),
        "model": row.get("model"),
        "prompt_version": row.get("prompt_version"),
        "last_error": _semantic_text(row.get("last_error"), 600) or None,
        "enqueued_at": _iso_utc(row.get("enqueued_at")),
        "started_at": _iso_utc(row.get("started_at")),
        "completed_at": _iso_utc(row.get("completed_at")),
        "updated_at": _iso_utc(row.get("updated_at")),
    }


def _semantic_enrichment_input_hash(html, manifest, source_files):
    payload = json.dumps(
        {
            "html": html or "",
            "manifest": manifest or {},
            "source_files": source_files or {},
            "prompt_version": _SEMANTIC_ENRICH_PROMPT_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _enqueue_semantic_enrichment(dashboard_id, version, *, force=False):
    """Durably schedule a post-commit compiler pass; never fail publication."""
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT d.runtime_kind,d.app_kind,v.html,v.manifest,v.source_files "
                "FROM rvbbit.dashboards d JOIN rvbbit.dashboard_versions v ON v.dashboard_id=d.id "
                "WHERE d.id=%s AND v.version=%s",
                (dashboard_id, version),
            ).fetchone()
            if not row:
                return {"status": "none", "eligible": False}
            if str(row.get("runtime_kind") or "html") != "html":
                return {"status": "disabled", "eligible": False, "reason": "non-html runtime"}
            input_hash = _semantic_enrichment_input_hash(
                row.get("html"), row.get("manifest"), row.get("source_files")
            )
            existing = c.execute(
                "SELECT * FROM rvbbit.artifact_semantic_enrichments "
                "WHERE dashboard_id=%s AND version=%s",
                (dashboard_id, version),
            ).fetchone()
            enabled = _semantic_enrichment_enabled()
            if (
                existing
                and not force
                and existing.get("input_hash") == input_hash
                and existing.get("status") in {"pending", "running", "ready", "partial"}
            ):
                return _semantic_enrichment_public(dict(existing))
            status = "pending" if enabled else "disabled"
            c.execute(
                "INSERT INTO rvbbit.artifact_semantic_enrichments "
                "(dashboard_id,version,status,input_hash,prompt_version,model,not_before,enqueued_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,now(),now(),now()) "
                "ON CONFLICT (dashboard_id,version) DO UPDATE SET "
                "status=EXCLUDED.status,input_hash=EXCLUDED.input_hash,prompt_version=EXCLUDED.prompt_version,"
                "model=EXCLUDED.model,semantic_map='{\"schema_version\":\"rvbbit.semantic-map.v1\",\"objects\":[]}'::jsonb,"
                "verification='{}'::jsonb,agent_run_id=NULL,attempts=0,last_error=NULL,"
                "not_before=now(),enqueued_at=now(),started_at=NULL,completed_at=NULL,updated_at=now()",
                (
                    dashboard_id,
                    version,
                    status,
                    input_hash,
                    _SEMANTIC_ENRICH_PROMPT_VERSION,
                    _SEMANTIC_ENRICH_MODEL,
                ),
            )
            saved = c.execute(
                "SELECT * FROM rvbbit.artifact_semantic_enrichments "
                "WHERE dashboard_id=%s AND version=%s",
                (dashboard_id, version),
            ).fetchone()
        if status == "pending":
            _SEMANTIC_ENRICH_WAKE.set()
        return _semantic_enrichment_public(dict(saved))
    except Exception as exc:  # noqa: BLE001 — semantic compilation is never a publish gate
        print(f"WARNING: could not enqueue semantic enrichment: {exc}", file=sys.stderr)
        return {
            "status": "unavailable",
            "eligible": False,
            "last_error": _semantic_text(exc, 600),
        }


def _normalize_runtime_kind(runtime_kind):
    kind = (runtime_kind or "html").strip().lower().replace("_", "-")
    aliases = {
        "html-dashboard": "html",
        "dashboard-html": "html",
        "static-html": "html",
        "python": "python-fastapi",
        "fastapi": "python-fastapi",
    }
    kind = aliases.get(kind, kind)
    if kind not in {"html", "python-fastapi"}:
        raise ValueError("runtime_kind must be one of: html, python-fastapi")
    return kind


def _normalize_app_kind(app_kind):
    kind = (app_kind or "dashboard").strip().lower().replace("_", "-")
    return kind or "dashboard"


def _source_files_text(source_files):
    if not isinstance(source_files, dict):
        return ""
    parts = []
    for name, body in source_files.items():
        if isinstance(body, str):
            parts.append(f"\n\n/* file: {name} */\n{body}")
    return "".join(parts)


def _python_placeholder_html(name, slug=None):
    title = (name or slug or "RVBBIT live app").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="icon" href="/theme/datarabbit.svg" type="image/svg+xml">
  <style>
    body {{ margin: 0; font-family: Inter, system-ui, sans-serif; background: #111827; color: #e5e7eb; }}
    main {{ max-width: 840px; margin: 10vh auto; padding: 32px; }}
    h1 {{ font-size: 28px; margin: 0 0 12px; }}
    p {{ color: #a5b4fc; line-height: 1.55; }}
    code {{ color: #f9fafb; background: #1f2937; padding: 2px 5px; border-radius: 4px; }}
  </style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <p>This Python FastAPI live app is stored and versioned in RVBBIT. Call <code>start_live_app</code> to run it under the local uvicorn runner.</p>
    <p>Use <code>get_live_app</code>, <code>debug_live_app</code>, or <code>live_app_status</code> to inspect source, dependencies, and health.</p>
  </main>
</body>
</html>"""


def _python_fastapi_files():
    return {
        "app.py": """from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from rvbbit_live import rvbbit_query

app = FastAPI(title=os.environ.get("RVBBIT_APP_NAME", "RVBBIT Live App"))
templates = Jinja2Templates(directory="templates")


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    result = await rvbbit_query("select now() as generated_at")
    return templates.TemplateResponse("index.html", {"request": request, "result": result})
""",
        "templates/index.html": """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RVBBIT Live App</title>
  <link rel="icon" href="/theme/datarabbit.svg" type="image/svg+xml">
</head>
<body>
  <main>
    <h1>RVBBIT Live App</h1>
    <pre>{{ result | tojson(indent=2) }}</pre>
  </main>
</body>
</html>
""",
        "requirements.txt": "fastapi\nuvicorn[standard]\njinja2\npsycopg[binary]\npandas\nplotly\n",
    }


def _html_from_live_app(runtime_kind, name, slug, html, source_files):
    if runtime_kind == "html":
        source_html = None
        if isinstance(source_files, dict):
            source_html = source_files.get("index.html") or source_files.get("dashboard.html")
        final_html = html or source_html
        if not final_html:
            raise ValueError("html live apps require `html` or source_files['index.html']")
        return final_html
    return html or _python_placeholder_html(name, slug)


def _live_app_manifest(runtime_kind="html", app_kind="dashboard", manifest=None,
                       source_files=None, description=None, *, normalize=True):
    runtime_kind = _normalize_runtime_kind(runtime_kind)
    app_kind = _normalize_app_kind(app_kind)
    user = _coerce_json_object(manifest, "manifest") if manifest is not None else {}
    base = {
        "schema_version": "live_app.v0",
        "runtime_kind": runtime_kind,
        "app_kind": app_kind,
        "description": description,
        "entrypoint": "index.html" if runtime_kind == "html" else "app.py",
        "capabilities": {
            "read_only_sql": True,
            "rvbbit_query": True,
            "metrics": True,
            "cubes": True,
            "screenshots": True,
        },
        "lifecycle": {
            "versioned_in": "rvbbit.dashboard_versions",
            "served_by": "/apps/{slug}",
            "python_runner": "local-uvicorn" if runtime_kind == "python-fastapi" else None,
        },
    }
    if source_files:
        base["source_files"] = sorted(source_files.keys())
    base.update(user)
    base["runtime_kind"] = runtime_kind
    base["app_kind"] = app_kind
    return _normalize_semantic_manifest(base) if normalize else base


def _live_app_runtime_health(runtime_kind, status="unknown", issues=None):
    issues = issues or []
    state = status if status in {"runnable", "running", "stored", "stopped", "exited"} else None
    runnable = runtime_kind == "html" or state == "running"
    return {
        "ok": not any(i.get("severity") == "error" for i in issues) and runnable,
        "state": state or ("runnable" if runtime_kind == "html" else "stored"),
        "runtime_kind": runtime_kind,
        "status": status,
        "issues": issues,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


_LIVE_APP_PROCS = {}
_PLAYWRIGHT_INSTALL_ATTEMPTED = False


def _live_app_root():
    root = os.environ.get("WAREHOUSE_LIVE_APP_ROOT")
    return Path(root) if root else Path(tempfile.gettempdir()) / "rvbbit-live-apps"


def _live_app_capture_root():
    root = os.environ.get("WAREHOUSE_LIVE_APP_CAPTURE_DIR")
    return Path(root) if root else Path(tempfile.gettempdir()) / "rvbbit-live-app-captures"


def _safe_source_path(name):
    rel = Path(str(name))
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError(f"unsafe source file path: {name}")
    return rel


def _tail_file(path, max_bytes=4000):
    try:
        p = Path(path)
        if not p.exists():
            return ""
        with p.open("rb") as f:
            if p.stat().st_size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            return f.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _runner_helper_source():
    return r'''from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

DSN = os.environ["RVBBIT_APP_DSN"]
ROW_CAP = int(os.environ.get("RVBBIT_APP_ROW_CAP", "10000"))
STMT_TIMEOUT_MS = int(os.environ.get("RVBBIT_APP_STMT_TIMEOUT_MS", "30000"))
_SAFE_HEAD = re.compile(r"^\s*(?:/\*.*?\*/\s*)*(?:--[^\n]*\n\s*)*(select|with)\b", re.IGNORECASE | re.DOTALL)
_BLOCKED = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|copy|call|do|grant|revoke|vacuum|merge)\b",
    re.IGNORECASE,
)


def _safe_select(sql: str) -> bool:
    text = (sql or "").strip()
    return bool(_SAFE_HEAD.search(text)) and not _BLOCKED.search(text)


def _normalize_as_of(as_of: str | None = None) -> str | None:
    if as_of is None:
        return None
    raw = str(as_of).strip()
    if not raw:
        return None
    if len(raw) > 80 or "\n" in raw or "\r" in raw or "\x00" in raw:
        raise ValueError("as_of must be one ISO-8601 timestamp")
    candidate = raw[:-1] + "+00:00" if raw.lower().endswith("z") else raw
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _with_as_of(sql: str, as_of: str | None = None) -> str:
    normalized = _normalize_as_of(as_of)
    return f"-- rvbbit: as_of {normalized}\n{sql}" if normalized else sql


async def rvbbit_query(sql: str, as_of: str | None = None, limit: int | None = None) -> dict:
    if not _safe_select(sql):
        return {"error": {"code": "UNSAFE_SQL", "message": "Only read-only SELECT/WITH queries are allowed."}}
    row_cap = max(1, min(int(limit or ROW_CAP), 100000))
    with psycopg.connect(DSN, row_factory=dict_row, autocommit=False) as c:
        c.execute("SET default_transaction_read_only = on")
        c.execute(f"SET statement_timeout = {STMT_TIMEOUT_MS}")
        cur = c.execute(_with_as_of(sql, as_of))
        rows = cur.fetchmany(row_cap + 1)
        truncated = len(rows) > row_cap
        rows = rows[:row_cap]
        columns = [
            {"name": col.name, "type": str(col.type_code)}
            for col in (cur.description or [])
        ]
        c.rollback()
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}
'''


def _materialize_live_app_sources(slug, version, source_files):
    root = _live_app_root()
    root.mkdir(parents=True, exist_ok=True)
    work_dir = root / slug / f"v{version}"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (source_files or {}).items():
        if not isinstance(body, str):
            continue
        rel = _safe_source_path(name)
        target = work_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    (work_dir / "rvbbit_live.py").write_text(_runner_helper_source(), encoding="utf-8")
    return work_dir


def _load_live_app_version(slug, version=None):
    _ensure_dashboard_tables()
    with _conn() as c:
        app = c.execute(
            "SELECT id, slug, name, description, owner_email, team, status, runtime_kind, app_kind, "
            "latest_version, manifest, last_health, created_at, updated_at "
            "FROM rvbbit.dashboards WHERE slug=%s", (slug,)).fetchone()
        if not app:
            return None, None
        app = dict(app)
        v = int(version or app["latest_version"])
        row = c.execute(
            "SELECT version, html, kind, created_by, created_at, notes, manifest, source_files "
            "FROM rvbbit.dashboard_versions WHERE dashboard_id=%s AND version=%s",
            (app["id"], v)).fetchone()
        if not row:
            return app, None
        return app, dict(row)


def _runner_entrypoint(manifest, source_files):
    manifest = manifest or {}
    entrypoint = str(manifest.get("entrypoint") or "app.py")
    if entrypoint not in (source_files or {}) and "app.py" in (source_files or {}):
        entrypoint = "app.py"
    rel = _safe_source_path(entrypoint)
    module = rel.with_suffix("").as_posix().replace("/", ".")
    return str(manifest.get("uvicorn_app") or f"{module}:app")


def _live_app_runner_status(slug, probe=True):
    entry = _LIVE_APP_PROCS.get(slug)
    if not entry:
        return {"slug": slug, "state": "stopped", "running": False}
    proc = entry["process"]
    rc = proc.poll()
    state = "running" if rc is None else "exited"
    status = {
        "slug": slug,
        "state": state,
        "running": rc is None,
        "pid": proc.pid,
        "port": entry["port"],
        "endpoint_url": entry["endpoint_url"],
        "version": entry["version"],
        "runtime_kind": entry["runtime_kind"],
        "started_at": entry["started_at"],
        "work_dir": entry["work_dir"],
        "log_path": entry["log_path"],
        "returncode": rc,
        "log_tail": _tail_file(entry["log_path"]),
    }
    if probe and rc is None:
        try:
            import httpx
            r = httpx.get(f'{entry["endpoint_url"].rstrip("/")}/health', timeout=1.5)
            status["health_http_status"] = r.status_code
            status["health_ok"] = 200 <= r.status_code < 500
        except Exception as e:  # noqa: BLE001
            status["health_ok"] = False
            status["health_error"] = str(e)
    return status


def _wait_live_app_runner(slug, timeout_s=8.0):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        status = _live_app_runner_status(slug, probe=True)
        last = status
        if status.get("health_ok"):
            return status
        if not status.get("running"):
            return status
        time.sleep(0.15)
    return last or _live_app_runner_status(slug, probe=True)


def _close_runner_log(entry):
    try:
        entry.get("log_handle").close()
    except Exception:  # noqa: BLE001
        pass


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


_ARTIFACT_MAX_BYTES = _env_int("WAREHOUSE_ARTIFACT_MAX_BYTES", 8_000_000, maximum=64_000_000)
_ARTIFACT_TTL_HOURS = _env_int("WAREHOUSE_ARTIFACT_TTL_HOURS", 48, maximum=24 * 14)


def tool_upload_artifact(content, name=None, artifact_id=None, append=False):
    """Stage content server-side and get back a handle. One upload (or several
    append chunks for very large payloads), then publish/update by
    source_artifact_id — no re-transmitting a 33KB+ document through every call."""
    if not isinstance(content, str) or not content:
        return {"error": {"code": "EMPTY_CONTENT", "message": "content must be a non-empty string"}}
    _ensure_dashboard_tables()
    caller, _ = _caller()
    with _conn() as c:
        # opportunistic TTL sweep — artifacts are a staging area, not storage
        c.execute("DELETE FROM rvbbit.mcp_artifacts WHERE created_at < now() - make_interval(hours => %s)",
                  (_ARTIFACT_TTL_HOURS,))
        if append:
            if not artifact_id:
                return {"error": {"code": "MISSING_ARTIFACT_ID", "message": "append=true requires artifact_id"}}
            row = c.execute("SELECT content FROM rvbbit.mcp_artifacts WHERE artifact_id=%s",
                            (artifact_id,)).fetchone()
            if not row:
                return {"error": {"code": "ARTIFACT_NOT_FOUND",
                                  "message": f"{artifact_id} (expired after {_ARTIFACT_TTL_HOURS}h?)"}}
            content = row["content"] + content
        else:
            artifact_id = artifact_id or secrets.token_urlsafe(9)
        nbytes = len(content.encode("utf-8"))
        if nbytes > _ARTIFACT_MAX_BYTES:
            return {"error": {"code": "ARTIFACT_TOO_LARGE",
                              "message": f"{nbytes} bytes exceeds cap of {_ARTIFACT_MAX_BYTES}"}}
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        c.execute(
            "INSERT INTO rvbbit.mcp_artifacts (artifact_id, name, content, sha256, bytes, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (artifact_id) DO UPDATE SET content=EXCLUDED.content, sha256=EXCLUDED.sha256, "
            "bytes=EXCLUDED.bytes, name=coalesce(EXCLUDED.name, rvbbit.mcp_artifacts.name)",
            (artifact_id, name, content, sha, nbytes, caller))
    return {"artifact_id": artifact_id, "bytes": nbytes, "sha256": sha,
            "expires_after_hours": _ARTIFACT_TTL_HOURS,
            "next": "pass source_artifact_id to publish_dashboard / update_dashboard / "
                    "create_live_app / update_live_app"}


def _resolve_source(html, source_artifact_id):
    """html-or-handle: a provided source_artifact_id wins; returns (html, error)."""
    if not source_artifact_id:
        return html, None
    _ensure_dashboard_tables()
    with _conn() as c:
        row = c.execute("SELECT content FROM rvbbit.mcp_artifacts WHERE artifact_id=%s",
                        (source_artifact_id,)).fetchone()
    if not row:
        return None, {"error": {"code": "ARTIFACT_NOT_FOUND",
                                "message": f"{source_artifact_id} — upload_artifact first "
                                           f"(artifacts expire after {_ARTIFACT_TTL_HOURS}h)"}}
    return row["content"], None


def tool_publish_dashboard(name, html=None, team=None, description=None, kind="live",
                           source_artifact_id=None, manifest=None):
    html, aerr = _resolve_source(html, source_artifact_id)
    if aerr:
        return aerr
    if not html:
        return {"error": {"code": "EMPTY_HTML",
                          "message": "pass html, or upload_artifact + source_artifact_id"}}
    try:
        manifest_doc, semantic_validation = _prepare_artifact_manifest_for_publish(
            manifest, execute=True
        )
    except ValueError as e:
        return {"error": {"code": "INVALID_SEMANTIC_MAP", "message": str(e)}}
    caller, _ = _caller()
    base = _slugify(name)
    with _conn() as c:
        slug, n = base, 1
        while c.execute("SELECT 1 FROM rvbbit.dashboards WHERE slug=%s", (slug,)).fetchone():
            n += 1
            slug = f"{base}-{n}"
        d = c.execute(
            "INSERT INTO rvbbit.dashboards "
            "(slug,name,description,owner_email,team,status,latest_version,manifest) "
            "VALUES (%s,%s,%s,%s,%s,%s,1,%s::jsonb) RETURNING id",
            (slug, name, description, caller, team, kind, _json_default(manifest_doc)),
        ).fetchone()
        c.execute(
            "INSERT INTO rvbbit.dashboard_versions "
            "(dashboard_id,version,html,kind,created_by,manifest) "
            "VALUES (%s,1,%s,%s,%s,%s::jsonb)",
            (d["id"], html, kind, caller, _json_default(manifest_doc)),
        )
    crawl = _crawl_safe(slug, use_llm=False)   # fast deterministic deps at publish
    enrichment = _enqueue_semantic_enrichment(d["id"], 1)
    _auto_thumb("dashboard", slug)
    return {"slug": slug, "version": 1, "url": _dash_url(slug), "hub_url": _hub_url("dashboard", slug),
            "owner": caller, "kind": kind, "manifest": manifest_doc,
            "semantic_validation": semantic_validation, "semantic_enrichment": enrichment,
            "deps": crawl}


def tool_update_dashboard(slug, html=None, notes=None, source_artifact_id=None, manifest=None):
    html, aerr = _resolve_source(html, source_artifact_id)
    if aerr:
        return aerr
    if not html:
        return {"error": {"code": "EMPTY_HTML",
                          "message": "pass html, or upload_artifact + source_artifact_id"}}
    caller, _ = _caller()
    try:
        manifest_patch = (
            _coerce_json_object(manifest, "manifest") if manifest is not None else None
        )
    except ValueError as e:
        return {"error": {"code": "INVALID_SEMANTIC_MAP", "message": str(e)}}
    with _conn() as c:
        d = c.execute(
            "SELECT id, latest_version, manifest FROM rvbbit.dashboards WHERE slug=%s",
            (slug,),
        ).fetchone()
        if not d:
            return {"error": {"code": "NOT_FOUND", "message": slug}}
        current = c.execute(
            "SELECT manifest FROM rvbbit.dashboard_versions "
            "WHERE dashboard_id=%s AND version=%s",
            (d["id"], d["latest_version"]),
        ).fetchone()
        next_manifest = dict(d.get("manifest") or {})
        next_manifest.update((current or {}).get("manifest") or {})
        previous_semantic_map = next_manifest.get("semantic_map")
        if manifest_patch is not None:
            next_manifest.update(manifest_patch)
        try:
            next_manifest, semantic_validation = _prepare_artifact_manifest_for_publish(
                next_manifest,
                fallback_semantic_map=(
                    previous_semantic_map
                    if manifest_patch is not None and "semantic_map" in manifest_patch
                    else None
                ),
                execute=True,
            )
        except ValueError as e:
            return {"error": {"code": "INVALID_SEMANTIC_MAP", "message": str(e)}}
        nv = d["latest_version"] + 1
        c.execute(
            "INSERT INTO rvbbit.dashboard_versions "
            "(dashboard_id,version,html,created_by,notes,manifest) "
            "VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
            (d["id"], nv, html, caller, notes, _json_default(next_manifest)),
        )
        c.execute(
            "UPDATE rvbbit.dashboards SET latest_version=%s, updated_at=now(), manifest=%s::jsonb "
            "WHERE id=%s",
            (nv, _json_default(next_manifest), d["id"]),
        )
    crawl = _crawl_safe(slug, use_llm=False)
    enrichment = _enqueue_semantic_enrichment(d["id"], nv)
    _auto_thumb("dashboard", slug)
    return {"slug": slug, "version": nv, "url": _dash_url(slug), "hub_url": _hub_url("dashboard", slug),
            "manifest": next_manifest, "semantic_validation": semantic_validation,
            "semantic_enrichment": enrichment, "deps": crawl}


def tool_list_dashboards(team=None, search=None):
    with _conn() as c:
        rows = c.execute(
            "SELECT slug, name, description, owner_email, team, status, latest_version, updated_at "
            "FROM rvbbit.dashboards "
            "WHERE (%s::text IS NULL OR team=%s::text) "
            "AND (%s::text IS NULL OR name ILIKE '%%'||%s::text||'%%' OR description ILIKE '%%'||%s::text||'%%') "
            "ORDER BY updated_at DESC LIMIT 100", (team, team, search, search, search)).fetchall()
    return {"dashboards": rows}


def tool_get_dashboard(slug, version=None):
    with _conn() as c:
        d = c.execute("SELECT id, slug, name, description, owner_email, team, status, "
                      "latest_version, manifest, created_at "
                      "FROM rvbbit.dashboards WHERE slug=%s", (slug,)).fetchone()
        if not d:
            return {"error": {"code": "NOT_FOUND", "message": slug}}
        v = int(version or d["latest_version"])
        version_row = c.execute(
            "SELECT version, html, kind, created_by, created_at, notes, manifest "
            "FROM rvbbit.dashboard_versions WHERE dashboard_id=%s AND version=%s", (d["id"], v)).fetchone()
        if version_row:
            version_row = dict(version_row)
            version_row["authored_manifest"] = version_row.get("manifest") or {}
            version_row["manifest"] = _effective_artifact_manifest(
                d["id"], v, version_row.get("manifest")
            )
        d["version"] = version_row
        d["sources"] = c.execute(
            "SELECT kind, object_ref, base_sql, source FROM rvbbit.dashboard_deps "
            "WHERE dashboard_id=%s AND version=%s ORDER BY kind, object_ref NULLS LAST",
            (d["id"], v),
        ).fetchall()
    d["semantic_enrichment"] = _semantic_enrichment_public(
        _semantic_enrichment_row(d["id"], v)
    )
    d["url"] = _dash_url(slug)
    return d


def tool_live_app_template(runtime_kind="html", app_kind="dashboard"):
    """Return a starter artifact for an agent-authored live app."""
    try:
        runtime_kind = _normalize_runtime_kind(runtime_kind)
        app_kind = _normalize_app_kind(app_kind)
    except ValueError as e:
        return {"error": {"code": "INVALID_ARGUMENT", "message": str(e)}}

    if runtime_kind == "html":
        dashboard = tool_dashboard_template()
        if dashboard.get("error"):
            return dashboard
        manifest = _live_app_manifest(runtime_kind, app_kind)
        return {
            "runtime_kind": runtime_kind,
            "app_kind": app_kind,
            "manifest": manifest,
            "template_html": dashboard["template_html"],
            "semantic_map_example": dashboard["semantic_map_example"],
            "how_to_use": [
                "Build the UI in one HTML artifact and call rvbbitQuery(sql) for live read-only data.",
                "Publish normally with create_live_app; RVBBIT automatically compiles a verified "
                "semantic overlay after publication without changing the artifact.",
                "semantic_map_example is an optional precision hint when you already have an exact "
                "business definition; authored objects override generated ones.",
                "Use debug_live_app(slug) after it runs to reconcile parsed and runtime dependencies.",
            ] + dashboard.get("how_to_use", []),
        }

    files = _python_fastapi_files()
    manifest = _live_app_manifest(runtime_kind, app_kind, source_files=files)
    return {
        "runtime_kind": runtime_kind,
        "app_kind": app_kind,
        "manifest": manifest,
        "source_files": files,
        "how_to_use": [
            "Python FastAPI apps are stored, versioned, dependency-indexed, and runnable under local uvicorn.",
            "Call create_live_app(..., runtime_kind='python-fastapi', source_files=source_files), then start_live_app(slug).",
            "Keep read-only data access behind `from rvbbit_live import rvbbit_query`; the runner injects that helper.",
            "requirements.txt documents dependencies, but this v1 runner uses the MCP service's current Python environment.",
        ],
    }


def tool_create_live_app(name, html=None, runtime_kind="html", app_kind="dashboard",
                         team=None, description=None, manifest=None, source_files=None,
                         source_artifact_id=None):
    """Create a versioned live app. HTML apps are served immediately at /apps/<slug>.
    Share BOTH links with the user: url (the bare app) and hub_url (the DataRabbit
    Hub — the browsable gallery of everything they've made, with this app focused)."""
    html, aerr = _resolve_source(html, source_artifact_id)
    if aerr:
        return aerr
    try:
        runtime_kind = _normalize_runtime_kind(runtime_kind)
        app_kind = _normalize_app_kind(app_kind)
        source_files = _coerce_json_object(source_files, "source_files") if source_files is not None else {}
        if runtime_kind == "python-fastapi" and not source_files:
            source_files = _python_fastapi_files()
        html = _html_from_live_app(runtime_kind, name, None, html, source_files)
    except ValueError as e:
        return {"error": {"code": "INVALID_ARGUMENT", "message": str(e)}}
    try:
        manifest_doc = _live_app_manifest(
            runtime_kind,
            app_kind,
            manifest,
            source_files,
            description,
            normalize=False,
        )
        manifest_doc, semantic_validation = _prepare_artifact_manifest_for_publish(
            manifest_doc, execute=True
        )
    except ValueError as e:
        return {"error": {"code": "INVALID_SEMANTIC_MAP", "message": str(e)}}

    _ensure_dashboard_tables()
    caller, _ = _caller()
    base = _slugify(name)
    health = _live_app_runtime_health(runtime_kind, "created")
    with _conn() as c:
        slug, n = base, 1
        while c.execute("SELECT 1 FROM rvbbit.dashboards WHERE slug=%s", (slug,)).fetchone():
            n += 1
            slug = f"{base}-{n}"
        html = _html_from_live_app(runtime_kind, name, slug, html, source_files)
        d = c.execute(
            "INSERT INTO rvbbit.dashboards "
            "(slug,name,description,owner_email,team,status,latest_version,runtime_kind,app_kind,manifest,last_health) "
            "VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s::jsonb,%s::jsonb) RETURNING id",
            (slug, name, description, caller, team, "live" if runtime_kind == "html" else "stored",
             runtime_kind, app_kind, _json_default(manifest_doc), _json_default(health))).fetchone()
        c.execute(
            "INSERT INTO rvbbit.dashboard_versions "
            "(dashboard_id,version,html,kind,created_by,manifest,source_files) "
            "VALUES (%s,1,%s,%s,%s,%s::jsonb,%s::jsonb)",
            (d["id"], html, runtime_kind, caller, _json_default(manifest_doc), _json_default(source_files)))
    crawl = _crawl_safe(slug, use_llm=False)
    enrichment = _enqueue_semantic_enrichment(d["id"], 1)
    if runtime_kind == "html":
        _auto_thumb(app_kind, slug)
    return {
        "slug": slug,
        "version": 1,
        "url": _live_app_url(slug),
        "hub_url": _hub_url(app_kind, slug),
        "owner": caller,
        "runtime_kind": runtime_kind,
        "app_kind": app_kind,
        "manifest": manifest_doc,
        "semantic_validation": semantic_validation,
        "semantic_enrichment": enrichment,
        "health": health,
        "deps": crawl,
    }


def tool_update_live_app(slug, html=None, notes=None, manifest=None, source_files=None,
                         runtime_kind=None, app_kind=None, source_artifact_id=None):
    """Publish a new version of a live app, preserving omitted source fields.
    Share hub_url with the user alongside url — the Hub gallery link."""
    html, aerr = _resolve_source(html, source_artifact_id)
    if aerr:
        return aerr
    _ensure_dashboard_tables()
    caller, _ = _caller()
    try:
        manifest_patch = _coerce_json_object(manifest, "manifest") if manifest is not None else {}
        source_patch = _coerce_json_object(source_files, "source_files") if source_files is not None else None
        with _conn() as c:
            d = c.execute(
                "SELECT id, name, description, latest_version, runtime_kind, app_kind, manifest "
                "FROM rvbbit.dashboards WHERE slug=%s", (slug,)).fetchone()
            if not d:
                return {"error": {"code": "NOT_FOUND", "message": slug}}
            cur = c.execute(
                "SELECT html, manifest, source_files FROM rvbbit.dashboard_versions "
                "WHERE dashboard_id=%s AND version=%s", (d["id"], d["latest_version"])).fetchone()
            next_runtime = _normalize_runtime_kind(runtime_kind or d["runtime_kind"])
            next_app_kind = _normalize_app_kind(app_kind or d["app_kind"])
            next_sources = source_patch if source_patch is not None else (cur["source_files"] or {})
            next_html = html if html is not None else cur["html"]
            next_html = _html_from_live_app(next_runtime, d["name"], slug, next_html, next_sources)
            next_manifest = dict(d["manifest"] or {})
            next_manifest.update(cur["manifest"] or {})
            previous_semantic_map = next_manifest.get("semantic_map")
            next_manifest.update(manifest_patch)
            try:
                next_manifest = _live_app_manifest(
                    next_runtime,
                    next_app_kind,
                    next_manifest,
                    next_sources,
                    d["description"],
                    normalize=False,
                )
                next_manifest, semantic_validation = _prepare_artifact_manifest_for_publish(
                    next_manifest,
                    fallback_semantic_map=(
                        previous_semantic_map if "semantic_map" in manifest_patch else None
                    ),
                    execute=True,
                )
            except ValueError as e:
                return {"error": {"code": "INVALID_SEMANTIC_MAP", "message": str(e)}}
            nv = d["latest_version"] + 1
            health = _live_app_runtime_health(next_runtime, "updated")
            c.execute(
                "INSERT INTO rvbbit.dashboard_versions "
                "(dashboard_id,version,html,kind,created_by,notes,manifest,source_files) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
                (d["id"], nv, next_html, next_runtime, caller, notes,
                 _json_default(next_manifest), _json_default(next_sources)))
            c.execute(
                "UPDATE rvbbit.dashboards SET latest_version=%s, updated_at=now(), runtime_kind=%s, "
                "app_kind=%s, manifest=%s::jsonb, last_health=%s::jsonb WHERE id=%s",
                (nv, next_runtime, next_app_kind, _json_default(next_manifest), _json_default(health), d["id"]))
    except ValueError as e:
        return {"error": {"code": "INVALID_ARGUMENT", "message": str(e)}}
    crawl = _crawl_safe(slug, use_llm=False)
    enrichment = _enqueue_semantic_enrichment(d["id"], nv)
    if next_runtime == "html":
        _auto_thumb(next_app_kind, slug)
    return {
        "slug": slug,
        "version": nv,
        "url": _live_app_url(slug),
        "hub_url": _hub_url(next_app_kind, slug),
        "runtime_kind": next_runtime,
        "app_kind": next_app_kind,
        "manifest": next_manifest,
        "semantic_validation": semantic_validation,
        "semantic_enrichment": enrichment,
        "health": health,
        "deps": crawl,
    }


def tool_list_live_apps(team=None, search=None, runtime_kind=None, app_kind=None):
    _ensure_dashboard_tables()
    try:
        runtime_kind = _normalize_runtime_kind(runtime_kind) if runtime_kind else None
        app_kind = _normalize_app_kind(app_kind) if app_kind else None
    except ValueError as e:
        return {"error": {"code": "INVALID_ARGUMENT", "message": str(e)}}
    with _conn() as c:
        rows = c.execute(
            "SELECT slug, name, description, owner_email, team, status, runtime_kind, app_kind, "
            "latest_version, manifest, last_health, last_debug_at, queries, tables, metrics, "
            "semantic_objects, updated_at "
            "FROM rvbbit.live_apps "
            "WHERE (%s::text IS NULL OR team=%s::text) "
            "AND (%s::text IS NULL OR runtime_kind=%s::text) "
            "AND (%s::text IS NULL OR app_kind=%s::text) "
            "AND (%s::text IS NULL OR name ILIKE '%%'||%s::text||'%%' "
            "     OR coalesce(description,'') ILIKE '%%'||%s::text||'%%') "
            "ORDER BY updated_at DESC LIMIT 100",
            (team, team, runtime_kind, runtime_kind, app_kind, app_kind, search, search, search)).fetchall()
    apps = []
    for row in rows:
        item = dict(row)
        item["url"] = _live_app_url(item["slug"])
        item["hub_url"] = _hub_url(item.get("app_kind"), item["slug"])
        apps.append(item)
    return {"live_apps": apps, "hub_url": (os.environ.get("LENS_PUBLIC_URL", "").rstrip("/") + "/hub")
            if os.environ.get("LENS_PUBLIC_URL") else None}


def tool_get_live_app(slug, version=None, include_source=True):
    _ensure_dashboard_tables()
    _ensure_activity_table()
    with _conn() as c:
        app = c.execute(
            "SELECT id, slug, name, description, owner_email, team, status, runtime_kind, app_kind, "
            "latest_version, manifest, last_health, last_debug_at, created_at, updated_at "
            "FROM rvbbit.dashboards WHERE slug=%s", (slug,)).fetchone()
        if not app:
            return {"error": {"code": "NOT_FOUND", "message": slug}}
        app = dict(app)
        v = int(version or app["latest_version"])
        version_row = c.execute(
            "SELECT version, html, kind, created_by, created_at, notes, manifest, source_files "
            "FROM rvbbit.dashboard_versions WHERE dashboard_id=%s AND version=%s",
            (app["id"], v)).fetchone()
        if not version_row:
            return {"error": {"code": "NOT_FOUND", "message": f"{slug}@v{v}"}}
        version_doc = dict(version_row)
        version_doc["authored_manifest"] = version_doc.get("manifest") or {}
        version_doc["manifest"] = _effective_artifact_manifest(
            app["id"], v, version_doc.get("manifest")
        )
        if not include_source:
            html = version_doc.pop("html", "") or ""
            source_files = version_doc.pop("source_files", {}) or {}
            version_doc["html_bytes"] = len(html)
            version_doc["source_files"] = sorted(source_files.keys())
        app["version"] = version_doc
        app["sources"] = c.execute(
            "SELECT kind, object_ref, base_sql, source FROM rvbbit.dashboard_deps "
            "WHERE dashboard_id=%s AND version=%s ORDER BY kind, object_ref NULLS LAST",
            (app["id"], v),
        ).fetchall()
        app["recent_queries"] = c.execute(
            "SELECT ts, ok, error, rows, engine, elapsed_ms, args->>'sql' AS sql "
            f"FROM {ACTIVITY_TABLE} WHERE tool='dashboard_query' AND args->>'dashboard'=%s "
            "ORDER BY ts DESC LIMIT 20", (slug,)).fetchall()
    app["url"] = _live_app_url(slug)
    app["hub_url"] = _hub_url(app.get("app_kind"), slug)
    app["path"] = f"/apps/{slug}"
    app["semantic_enrichment"] = _semantic_enrichment_public(
        _semantic_enrichment_row(app["id"], v)
    )
    app["runner"] = _live_app_runner_status(slug, probe=False)
    return app


def tool_live_app_logs(slug, limit=50):
    _ensure_activity_table()
    try:
        limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        limit = 50
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, caller, ok, error, rows, engine, elapsed_ms, args->>'sql' AS sql "
            f"FROM {ACTIVITY_TABLE} WHERE tool='dashboard_query' AND args->>'dashboard'=%s "
            "ORDER BY ts DESC LIMIT %s", (slug, limit)).fetchall()
    return {"slug": slug, "events": rows}


def tool_debug_live_app(slug, run_crawl=True, include_activity=True):
    _ensure_dashboard_tables()
    app = tool_get_live_app(slug, include_source=False)
    if app.get("error"):
        return app
    crawl = _crawl_safe(slug, use_llm=False) if run_crawl else None
    logs = tool_live_app_logs(slug, 50) if include_activity else {"events": []}
    issues = []
    runner = _live_app_runner_status(slug, probe=True)
    if app.get("runtime_kind") != "html" and not runner.get("running"):
        issues.append({
            "severity": "warning",
            "code": "PYTHON_RUNNER_STOPPED" if runner.get("state") == "stopped" else "PYTHON_RUNNER_EXITED",
            "message": "Python FastAPI source is versioned, but the local runner is not running.",
        })
    if runner.get("running") and runner.get("health_ok") is False:
        issues.append({
            "severity": "warning",
            "code": "PYTHON_RUNNER_HEALTH_UNKNOWN",
            "message": runner.get("health_error") or "The runner process is up, but /health did not respond cleanly.",
        })
    deps = crawl or {"queries": app.get("queries", 0), "tables": [], "metrics": []}
    if not deps.get("queries") and not deps.get("metrics"):
        issues.append({
            "severity": "warning",
            "code": "NO_LIVE_DEPENDENCIES",
            "message": "No rvbbitQuery/sql literals/metric calls were detected yet.",
        })
    error_events = [e for e in logs.get("events", []) if e.get("ok") is False]
    if error_events:
        issues.append({
            "severity": "error",
            "code": "RECENT_QUERY_ERRORS",
            "message": f"{len(error_events)} recent live-app query calls failed.",
        })
    health_state = "running" if runner.get("running") else app.get("status")
    health = _live_app_runtime_health(app.get("runtime_kind"), health_state, issues)
    with _conn() as c:
        c.execute(
            "UPDATE rvbbit.dashboards SET last_health=%s::jsonb, last_debug_at=now() WHERE slug=%s",
            (_json_default(health), slug))
    return {
        "slug": slug,
        "url": app.get("url"),
        "runtime_kind": app.get("runtime_kind"),
        "app_kind": app.get("app_kind"),
        "health": health,
        "deps": deps,
        "runner": runner,
        "recent_activity": logs.get("events", [])[:10],
        "next_actions": [
            "For Python apps, call start_live_app before opening or capturing the app.",
            "Open the URL and exercise the app once so runtime SQL calls are logged.",
            "Run debug_live_app again after edits to refresh dependencies and health.",
            "Use update_live_app for source or manifest changes; every update creates a new version.",
        ],
    }


def tool_start_live_app(slug, version=None, restart=False, port=None):
    """Start a Python FastAPI live app under local uvicorn. HTML apps are already hosted."""
    app, row = _load_live_app_version(slug, version)
    if not app:
        return {"error": {"code": "NOT_FOUND", "message": slug}}
    if not row:
        return {"error": {"code": "NOT_FOUND", "message": f"{slug}@v{version or app['latest_version']}"}}
    runtime_kind = _normalize_runtime_kind(app.get("runtime_kind"))
    if runtime_kind == "html":
        return {
            "slug": slug,
            "runtime_kind": runtime_kind,
            "state": "hosted",
            "running": True,
            "url": _live_app_url(slug),
            "path": f"/apps/{slug}",
            "version": row["version"],
        }

    current = _live_app_runner_status(slug, probe=True)
    if current.get("running") and int(current.get("version") or 0) == int(row["version"]) and not restart:
        return current | {"url": _live_app_url(slug), "path": f"/apps/{slug}"}
    if current.get("running"):
        tool_stop_live_app(slug)
    elif slug in _LIVE_APP_PROCS:
        _close_runner_log(_LIVE_APP_PROCS[slug])
        _LIVE_APP_PROCS.pop(slug, None)

    source_files = row.get("source_files") or {}
    if not source_files:
        source_files = _python_fastapi_files()
    try:
        work_dir = _materialize_live_app_sources(slug, row["version"], source_files)
        runner_port = int(port or _free_port())
        manifest = dict(app.get("manifest") or {})
        manifest.update(row.get("manifest") or {})
        uvicorn_app = _runner_entrypoint(manifest, source_files)
    except Exception as e:  # noqa: BLE001
        return {"error": {"code": "RUNNER_PREP_FAILED", "message": str(e)}}

    log_path = work_dir / "runner.log"
    log_handle = log_path.open("a", encoding="utf-8", buffering=1)
    env = os.environ.copy()
    env.update({
        "RVBBIT_APP_NAME": app.get("name") or slug,
        "RVBBIT_APP_SLUG": slug,
        "RVBBIT_APP_VERSION": str(row["version"]),
        "RVBBIT_APP_DSN": os.environ.get("WAREHOUSE_LIVE_APP_DSN", DSN),
        "RVBBIT_APP_ROW_CAP": os.environ.get("WAREHOUSE_LIVE_APP_ROW_CAP", "10000"),
        "RVBBIT_APP_STMT_TIMEOUT_MS": str(STMT_TIMEOUT_MS),
        "PYTHONPATH": str(work_dir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
    })
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        uvicorn_app,
        "--host",
        "127.0.0.1",
        "--port",
        str(runner_port),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(work_dir),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:  # noqa: BLE001
        _close_runner_log({"log_handle": log_handle})
        return {"error": {"code": "RUNNER_START_FAILED", "message": str(e)}}

    endpoint = f"http://127.0.0.1:{runner_port}"
    _LIVE_APP_PROCS[slug] = {
        "process": proc,
        "port": runner_port,
        "endpoint_url": endpoint,
        "version": row["version"],
        "runtime_kind": runtime_kind,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "work_dir": str(work_dir),
        "log_path": str(log_path),
        "log_handle": log_handle,
        "command": cmd,
    }
    status = _wait_live_app_runner(slug, float(os.environ.get("WAREHOUSE_LIVE_APP_START_TIMEOUT", "8")))
    issues = []
    if not status.get("running"):
        issues.append({
            "severity": "error",
            "code": "RUNNER_EXITED",
            "message": status.get("log_tail") or "The live app process exited before it became healthy.",
        })
    elif status.get("health_ok") is False:
        issues.append({
            "severity": "warning",
            "code": "RUNNER_HEALTH_UNKNOWN",
            "message": status.get("health_error") or "The runner process is up, but /health did not respond cleanly.",
        })
    health = _live_app_runtime_health(runtime_kind, status.get("state") or "running", issues)
    with _conn() as c:
        c.execute(
            "UPDATE rvbbit.dashboards SET last_health=%s::jsonb, last_debug_at=now() WHERE slug=%s",
            (_json_default(health), slug))
    return status | {"url": _live_app_url(slug), "path": f"/apps/{slug}", "command": cmd}


def tool_stop_live_app(slug):
    entry = _LIVE_APP_PROCS.get(slug)
    if not entry:
        return {"slug": slug, "state": "stopped", "running": False}
    proc = entry["process"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    status = _live_app_runner_status(slug, probe=False)
    status["state"] = "stopped"
    status["running"] = False
    _close_runner_log(entry)
    _LIVE_APP_PROCS.pop(slug, None)
    try:
        with _conn() as c:
            c.execute(
                "UPDATE rvbbit.dashboards SET last_health=%s::jsonb, last_debug_at=now() WHERE slug=%s",
                (_json_default(_live_app_runtime_health(status.get("runtime_kind"), "stopped")), slug))
    except Exception:  # noqa: BLE001
        pass
    return status


def tool_live_app_status(slug=None):
    if slug:
        return _live_app_runner_status(slug, probe=True)
    return {"live_apps": [_live_app_runner_status(s, probe=False) for s in sorted(_LIVE_APP_PROCS)]}


def _default_capture_path(slug, version):
    root = _live_app_capture_root()
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return root / f"{slug}-v{version}-{stamp}.png"


def _looks_like_missing_playwright_browser(exc):
    msg = str(exc).lower()
    return (
        "executable doesn't exist" in msg
        or "playwright install" in msg
        or "browser has not been installed" in msg
    )


def _install_playwright_chromium():
    global _PLAYWRIGHT_INSTALL_ATTEMPTED
    if _PLAYWRIGHT_INSTALL_ATTEMPTED:
        return {"ok": False, "error": "install already attempted in this process"}
    _PLAYWRIGHT_INSTALL_ATTEMPTED = True
    cmd = [sys.executable, "-m", "playwright", "install"]
    if _env_bool("WAREHOUSE_PLAYWRIGHT_INSTALL_WITH_DEPS", False):
        cmd.append("--with-deps")
    cmd.append("chromium")
    timeout = _env_int("WAREHOUSE_PLAYWRIGHT_INSTALL_TIMEOUT_SEC", 600, minimum=30, maximum=3600)
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "cmd": cmd, "error": str(e)}
    return {
        "ok": proc.returncode == 0,
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-4000:],
    }


def _launch_playwright_chromium(playwright):
    try:
        return playwright.chromium.launch()
    except Exception as e:  # noqa: BLE001
        if not _env_bool("WAREHOUSE_PLAYWRIGHT_AUTO_INSTALL", True) or not _looks_like_missing_playwright_browser(e):
            raise
        install = _install_playwright_chromium()
        if not install.get("ok"):
            raise RuntimeError(
                "Playwright Chromium is missing and automatic install failed: "
                + json.dumps(install, default=str)
            ) from e
        return playwright.chromium.launch()


def _inline_artifact_system_assets(html):
    """Make first-party opt-in assets available to ``about:blank`` renders."""
    try:
        import warehouse_theme
        return warehouse_theme.inline_chart_runtime(html or "")
    except Exception:  # noqa: BLE001 — an optional renderer must not break ordinary captures
        return html or ""


def _capture_html_with_playwright(html, path, width, height, full_page, wait_ms, quality=None):
    """Render + screenshot the stored HTML with the LIVE rvbbitQuery bridge injected.
    Returns a telemetry dict — every bridge query that ran (ok/rows/ms) plus console
    and page errors — so a capture doubles as a health check of the data bridge,
    not just a picture of whatever happened to render."""
    from playwright.sync_api import sync_playwright

    telemetry = {"queries": [], "console_errors": [], "page_errors": []}

    def _query(sql, opts=None):
        opts = opts or {}
        t0 = time.time()
        res = tool_run_sql(str(sql), opts.get("as_of"))
        entry = {"sql": str(sql)[:200], "ms": int((time.time() - t0) * 1000)}
        if isinstance(res, dict) and res.get("error"):
            entry["error"] = res["error"]
        else:
            entry["rows"] = res.get("row_count")
        telemetry["queries"].append(entry)
        return json.loads(json.dumps(res, default=str))

    init = """<script>
window.rvbbitQuery = async function(sql, opts) { return await window.__rvbbitQuery(sql, opts || {}); };
window.cowork = window.cowork || {};
window.cowork.callMcpTool = async function(tool, args) {
  args = args || {};
  if (String(tool || '').endsWith('run_sql_multi')) {
    const results = {};
    for (const [name, sql] of Object.entries(args.queries || {})) {
      results[name] = await window.rvbbitQuery(sql, {as_of: args.as_of || null});
    }
    return {structuredContent: {results}};
  }
  const d = await window.rvbbitQuery((args && args.sql) || "");
  return {structuredContent: {rows: (d && d.rows) || []}};
};
</script>"""
    # The bridge shim must be INLINED into the document, not add_init_script'd:
    # Playwright init scripts do not fire for set_content() documents (verified
    # empirically — the wrapper was undefined and every parse-time rvbbitQuery
    # call threw, which is why captures used to report no query activity).
    # Exposed bindings (__rvbbitQuery) ARE installed for set_content, so only
    # the wrapper definition needs to ride inside the HTML, ahead of any
    # content script.
    doc = _inline_artifact_system_assets(html)
    m = re.search(r"<head[^>]*>", doc, re.IGNORECASE)
    if m:
        doc = doc[:m.end()] + init + doc[m.end():]
    else:
        doc = init + doc
    with sync_playwright() as p:
        browser = _launch_playwright_chromium(p)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.expose_function("__rvbbitQuery", _query)
        page.on("console", lambda msg: telemetry["console_errors"].append(msg.text[:500])
                if msg.type in ("error", "warning") and len(telemetry["console_errors"]) < 20 else None)
        page.on("pageerror", lambda exc: telemetry["page_errors"].append(str(exc)[:500])
                if len(telemetry["page_errors"]) < 20 else None)
        page.set_content(doc, wait_until="networkidle", timeout=30_000)
        if wait_ms:
            page.wait_for_timeout(wait_ms)
        # quality only applies to JPEG; playwright infers the codec from the
        # path suffix, so callers passing .png keep lossless output untouched.
        shot = {"path": str(path), "full_page": bool(full_page)}
        if quality is not None and str(path).lower().endswith((".jpg", ".jpeg")):
            shot["quality"] = int(quality)
        page.screenshot(**shot)
        browser.close()
    return telemetry


def _capture_url_with_playwright(url, path, width, height, full_page, wait_ms):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = _launch_playwright_chromium(p)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(url, wait_until="networkidle", timeout=30_000)
        if wait_ms:
            page.wait_for_timeout(wait_ms)
        page.screenshot(path=str(path), full_page=bool(full_page))
        browser.close()


def _workflow_file_root():
    """Where workflow artifacts live: the capture root (durable volume in
    compose) — PDFs under pdfs/, and inbound images are expected under the
    shared /staging volume or this root. Path reads are JAILED to these."""
    return _live_app_capture_root()


def _jailed_path(p):
    """Resolve a user-supplied path inside the allowed roots (staging +
    capture root) or raise — workflow tools must never read arbitrary disk."""
    import pathlib
    roots = [pathlib.Path("/staging"), _workflow_file_root()]
    extra = os.environ.get("WAREHOUSE_FILE_ROOTS", "")
    roots += [pathlib.Path(x) for x in extra.split(":") if x.strip()]
    rp = pathlib.Path(p).resolve()
    for root in roots:
        try:
            rp.relative_to(root.resolve())
            return rp
        except ValueError:
            continue
    raise ValueError(f"path {p} is outside the allowed file roots")


def tool_render_pdf(name, html=None, slug=None, source_artifact_id=None,
                    width=816, height=1056, landscape=False, wait_ms=900):
    """Render HTML (or a stored live app by slug) to a PDF — the official-
    document leg of intake->extract->validate->document workflows (certs,
    permits, invoices). Rides the same bridge-injected playwright renderer
    as captures, so rvbbitQuery works inside the template: the PDF can pull
    LIVE rows at render time. Returns the served path (/pdfs/<name>.pdf)."""
    html, aerr = _resolve_source(html, source_artifact_id)
    if aerr:
        return aerr
    if slug and not html:
        app, row = _load_live_app_version(slug)
        if not app:
            return {"error": {"code": "NOT_FOUND", "message": slug}}
        html = (row or {}).get("html") or ""
    if not html:
        return {"error": {"code": "EMPTY_HTML", "message": "pass html, slug, or source_artifact_id"}}
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(name or "document")).strip("-")[:80] or "document"
    out_dir = _workflow_file_root() / "pdfs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{safe}.pdf"

    from playwright.sync_api import sync_playwright
    telemetry = {"queries": [], "console_errors": [], "page_errors": []}

    def _query(sql, opts=None):
        opts = opts or {}
        res = tool_run_sql(str(sql), opts.get("as_of"))
        entry = {"sql": str(sql)[:200]}
        if isinstance(res, dict) and res.get("error"):
            entry["error"] = res["error"]
        else:
            entry["rows"] = res.get("row_count")
        telemetry["queries"].append(entry)
        return json.loads(json.dumps(res, default=str))

    init = ("<script>window.rvbbitQuery = async function(sql, opts) "
            "{ return await window.__rvbbitQuery(sql, opts || {}); };</script>")
    doc = _inline_artifact_system_assets(html)
    mhead = re.search(r"<head[^>]*>", doc, re.IGNORECASE)
    doc = doc[:mhead.end()] + init + doc[mhead.end():] if mhead else init + doc
    with sync_playwright() as pw:
        browser = _launch_playwright_chromium(pw)
        page = browser.new_page(viewport={"width": int(width), "height": int(height)})
        page.expose_function("__rvbbitQuery", _query)
        page.on("pageerror", lambda exc: telemetry["page_errors"].append(str(exc)[:400])
                if len(telemetry["page_errors"]) < 10 else None)
        page.set_content(doc, wait_until="networkidle", timeout=30_000)
        if wait_ms:
            page.wait_for_timeout(int(wait_ms))
        page.pdf(path=str(path), landscape=bool(landscape), print_background=True)
        browser.close()
    return {"name": safe, "path": f"/pdfs/{safe}.pdf", "bytes": path.stat().st_size,
            "bridge": telemetry}


def _llm_chat(messages, model=None, max_tokens=1600):
    """One openai-compatible chat call using the box's vision/chat envs
    (WAREHOUSE_VISION_BASE/KEY/MODEL; OpenRouter/OpenAI keys as fallback).
    Returns (text, model) or raises."""
    import httpx
    base = os.environ.get("WAREHOUSE_VISION_BASE", "https://openrouter.ai/api/v1").rstrip("/")
    key = (os.environ.get("WAREHOUSE_VISION_KEY") or os.environ.get("OPENROUTER_API_KEY")
           or os.environ.get("OPENAI_API_KEY") or "")
    mdl = model or os.environ.get("WAREHOUSE_VISION_MODEL", "google/gemini-2.5-flash")
    if not key:
        raise RuntimeError("set WAREHOUSE_VISION_KEY (or OPENROUTER_API_KEY / OPENAI_API_KEY)")
    r = httpx.post(f"{base}/chat/completions",
                   headers={"Authorization": f"Bearer {key}"},
                   json={"model": mdl, "max_tokens": max_tokens, "messages": messages},
                   timeout=90.0)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or ""), mdl


def tool_kit_rehearsal(kit, scenario=None, model=None):
    """The rehearsal (a LINT, never the source): compile the kit's
    deterministic briefing (rvbbit.kit_brief), hand it to a model with a
    scenario, and get back (a) the step-by-step runstream it would follow
    and (b) every AMBIGUITY or MISSING VERB it had to guess around. The
    gap list is the payload — each finding is either an edit to a logic
    plate's explanation or an action the kit still needs."""
    with _conn(read_only=True) as c:
        row = c.execute("SELECT rvbbit.kit_brief(%s) AS b", (kit,)).fetchone()
    brief = (row or {}).get("b") or ""
    if not brief or "(no such kit)" in brief:
        return {"error": {"code": "NOT_FOUND", "message": f"no kit named {kit}"}}
    scen = scenario or "A typical new item of work arrives via chat for this kit."
    ask = (
        "You are an autonomous agent that has just been directed at the following kit. "
        "The briefing below is your ONLY context (it is exactly what you would receive "
        "in production).\n\n--- BRIEFING ---\n" + brief + "\n--- END BRIEFING ---\n\n"
        "Scenario: " + scen + "\n\n"
        "Reply in two markdown sections:\n"
        "## Runstream — numbered, concrete steps you would take (name the exact actions/"
        "functions from the briefing at each step; include what you would SAY to the human "
        "when a check is red).\n"
        "## Gaps & ambiguities — every point where the briefing forced you to guess: "
        "unclear rules, missing actions/verbs, undefined vocabulary, identity ambiguity. "
        "Be specific and adversarial; an empty list is a failure of imagination.")
    try:
        text, mdl = _llm_chat([{"role": "user", "content": ask}], model=model)
    except Exception as e:  # noqa: BLE001
        return {"error": {"code": "REHEARSAL_CALL_FAILED", "message": str(e)[:300]}}
    return {"kit": kit, "scenario": scen, "model": mdl, "rehearsal": text,
            "note": "The briefing is ground truth; this narration is one model's traversal — a lint, not a spec."}


def tool_extract_image(path, fields, model=None, prompt=None):
    """Vision extraction for intake workflows: read an image (staging or
    capture volume — texted photos land there via the agent) and pull the
    named fields with a multimodal model. Returns strict JSON per field plus
    _confidence 0-1 each; low confidence is the caller's cue to ask for a
    better photo ("can't read the serial — shoot it closer")."""
    import base64
    try:
        rp = _jailed_path(path)
    except ValueError as e:
        return {"error": {"code": "BAD_PATH", "message": str(e)}}
    if not rp.is_file():
        return {"error": {"code": "NOT_FOUND", "message": str(path)}}
    ext = rp.suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "gif": "image/gif"}.get(ext)
    if not mime:
        return {"error": {"code": "BAD_TYPE", "message": f"unsupported image type .{ext}"}}
    b64 = base64.b64encode(rp.read_bytes()).decode()

    want = [f.strip() for f in str(fields).split(",") if f.strip()]
    ask = prompt or (
        "Extract these fields from the image: " + ", ".join(want) + ". "
        "Reply with ONLY a JSON object: one key per field (string value, or null if absent/unreadable) "
        "plus a _confidence object mapping each field to 0..1. No prose.")
    try:
        text, mdl = _llm_chat([{
            "role": "user",
            "content": [{"type": "text", "text": ask},
                        {"type": "image_url",
                         "image_url": {"url": f"data:{mime};base64,{b64}"}}]}],
            model=model, max_tokens=800)
    except Exception as e:  # noqa: BLE001
        return {"error": {"code": "VISION_CALL_FAILED", "message": str(e)[:300]}}
    mjson = re.search(r"\{.*\}", text, re.DOTALL)
    if not mjson:
        return {"error": {"code": "UNPARSEABLE", "message": text[:300]}}
    try:
        out = json.loads(mjson.group(0))
    except Exception:  # noqa: BLE001
        return {"error": {"code": "UNPARSEABLE", "message": text[:300]}}
    return {"model": mdl, "fields": {k: v for k, v in out.items() if k != "_confidence"},
            "confidence": out.get("_confidence", {}), "image": str(rp)}


def tool_capture_live_app(slug, path=None, width=1440, height=900, full_page=True, start=True, wait_ms=750):
    """Capture a PNG screenshot. HTML apps get an injected live rvbbitQuery bridge; Python apps
    are captured from their running local endpoint and can be auto-started."""
    app, row = _load_live_app_version(slug)
    if not app:
        return {"error": {"code": "NOT_FOUND", "message": slug}}
    if not row:
        return {"error": {"code": "NOT_FOUND", "message": f"{slug}@v{app['latest_version']}"}}
    try:
        width = max(320, min(int(width or 1440), 3840))
        height = max(240, min(int(height or 900), 2160))
        wait_ms = max(0, min(int(wait_ms or 0), 10_000))
    except (TypeError, ValueError):
        return {"error": {"code": "INVALID_ARGUMENT", "message": "width, height, and wait_ms must be integers"}}
    out = Path(path) if path else _default_capture_path(slug, row["version"])
    out.parent.mkdir(parents=True, exist_ok=True)
    runtime_kind = _normalize_runtime_kind(app.get("runtime_kind"))
    telemetry = None
    try:
        if runtime_kind == "html":
            telemetry = _capture_html_with_playwright(
                row.get("html") or "", out, width, height, full_page, wait_ms)
            source = "stored-html"
        else:
            status = _live_app_runner_status(slug, probe=True)
            if not status.get("running") and start:
                status = tool_start_live_app(slug)
            if not status.get("running"):
                return {"error": {"code": "RUNNER_NOT_RUNNING", "message": status}, "status": status}
            _capture_url_with_playwright(status["endpoint_url"], out, width, height, full_page, wait_ms)
            source = status["endpoint_url"]
    except Exception as e:  # noqa: BLE001
        return {
            "error": {
                "code": "CAPTURE_FAILED",
                "message": str(e),
                "hint": (
                    "The warehouse-mcp image installs Chromium at build time. Runtime fallback runs "
                    "`python -m playwright install chromium` once when the browser is missing; set "
                    "WAREHOUSE_PLAYWRIGHT_INSTALL_WITH_DEPS=1 if OS dependencies must also be installed, "
                    "or WAREHOUSE_PLAYWRIGHT_AUTO_INSTALL=0 to disable self-install."
                ),
            }
        }
    res = {
        "slug": slug,
        "version": row["version"],
        "runtime_kind": runtime_kind,
        "path": str(out),
        "bytes": out.stat().st_size if out.exists() else None,
        "width": width,
        "height": height,
        "full_page": bool(full_page),
        "source": source,
    }
    if telemetry is not None:
        q = telemetry["queries"]
        res["bridge"] = {
            "queries_ran": len(q),
            "queries_failed": sum(1 for e in q if e.get("error")),
            "queries": q[:24],
            "console_errors": telemetry["console_errors"],
            "page_errors": telemetry["page_errors"],
            "healthy": not any(e.get("error") for e in q) and not telemetry["page_errors"],
        }
    return res


# ── post-publication semantic compiler ───────────────────────────────────────

_SEMANTIC_EVIDENCE_JS = r"""
() => {
  const SENSITIVE = /(?:secret|token|password|passwd|auth|cookie|session|api[-_]?key)/i;
  const bounded = (value, limit=500) => String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, limit);
  const quoteAttr = (value) => String(value).replaceAll('\\', '\\\\').replaceAll('"', '\\"');
  const css = (value) => window.CSS?.escape ? window.CSS.escape(String(value)) : String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  const visible = (node) => {
    if (!(node instanceof Element)) return false;
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0
      && rect.width >= 2 && rect.height >= 2 && rect.bottom > 0 && rect.right > 0;
  };
  const safeAttributes = (node) => {
    const out = {};
    [...(node?.attributes || [])].slice(0, 32).forEach((attr) => {
      if (!SENSITIVE.test(attr.name) && (attr.name.startsWith('data-') || ['aria-label','title','name'].includes(attr.name))) {
        out[attr.name] = bounded(attr.value, 240);
      }
    });
    return out;
  };
  const contextData = (node) => {
    const out = {};
    let current = node;
    for (let depth=0; current && current !== document.body && depth<4; depth++, current=current.parentElement) {
      Object.entries(safeAttributes(current)).forEach(([key,value]) => {
        if (key.startsWith('data-') && key !== 'data-value' && !SENSITIVE.test(key)) {
          const short = key.slice(5).replace(/[^a-zA-Z0-9_]/g, '_');
          if (!(short in out)) out[short] = value;
        }
        if (key === 'aria-label' && !('aria_label' in out)) out.aria_label = value;
      });
    }
    return out;
  };
  const uniqueDataPart = (node) => {
    const attrs = Object.entries(safeAttributes(node)).filter(([key,value]) => key.startsWith('data-') && value);
    for (let count=1; count<=Math.min(3, attrs.length); count++) {
      const part = (node.localName || '*') + attrs.slice(0,count)
        .map(([key,value]) => `[${key}="${quoteAttr(value)}"]`).join('');
      try { if (document.querySelectorAll(part).length === 1) return part; } catch {}
    }
    return '';
  };
  const selectorFor = (node) => {
    if (!(node instanceof Element)) return '';
    const parts = [];
    let current = node;
    while (current && current !== document.body && parts.length < 8) {
      let part = current.localName || 'div';
      if (current.id && !SENSITIVE.test(current.id)) {
        parts.unshift(`${part}#${css(current.id)}`);
        break;
      }
      const dataPart = uniqueDataPart(current);
      if (dataPart) {
        parts.unshift(dataPart);
        break;
      }
      const testId = current.getAttribute('data-testid');
      if (testId && !SENSITIVE.test(testId)) part += `[data-testid="${quoteAttr(testId)}"]`;
      else if (current.parentElement) {
        const siblings = [...current.parentElement.children].filter((item) => item.localName === current.localName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(current)+1})`;
      }
      parts.unshift(part);
      current = current.parentElement;
    }
    return parts.join(' > ').slice(0,800);
  };
  const valueFrom = (node) => {
    for (const attr of ['data-value','data-v']) {
      const value = node.getAttribute?.(attr);
      if (value !== null && value !== '') return {
        value: bounded(value,120), value_attribute: attr,
        value_source:`$element.data.${attr.slice(5).replace(/[^a-zA-Z0-9_]/g,'_')}`,
      };
    }
    const text = bounded(node.textContent,160);
    if (!text) return {value:'', value_attribute:null, value_source:null};
    const matches = text.match(/[~≈<>≤≥]?\s*[+-]?\s*(?:[$€£¥]\s*)?\(?\d[\d,]*(?:\.\d+)?\)?(?:\s*%)?(?:\s*[kmbt])?/ig) || [];
    const comparable = (value) => bounded(value,120).toLowerCase().replace(/[^0-9.eE+-]/g,'');
    const contextual = new Set(Object.values(contextData(node)).map(comparable).filter(Boolean));
    let valueIndex = matches.findIndex((item) => bounded(item,120) && !contextual.has(comparable(item)));
    if (valueIndex < 0) valueIndex = matches.findIndex((item) => bounded(item,120));
    const value = valueIndex >= 0 ? bounded(matches[valueIndex],120) : '';
    return {
      value, value_attribute:null,
      value_source:valueIndex >= 0 ? `$element.text_number.${valueIndex}` : null,
    };
  };
  const containerFor = (node) => node.closest('section,article,.card,[role="figure"],[role="group"]') || node.parentElement;
  const labelFor = (node) => {
    const container = containerFor(node);
    const local = node.parentElement?.querySelector?.('.lab,.label,[data-label]');
    const heading = container?.querySelector?.('h1,h2,h3,h4,h5,h6,.lab,.label,[aria-label]');
    return bounded(local?.textContent || local?.getAttribute?.('data-label') || heading?.textContent
      || node.getAttribute?.('aria-label') || node.getAttribute?.('title'), 300);
  };
  const raw = [];
  [...document.body.querySelectorAll('*')].slice(0,6000).forEach((node) => {
    const svgGeometry = ['path','line','polyline','circle'].includes(node.localName);
    if (!visible(node) || ['script','style','option'].includes(node.localName)
        || (svgGeometry && !node.matches('[data-rvbbit-key],[data-row-index],[data-index],[data-i]'))) return;
    const {value,value_attribute,value_source} = valueFrom(node);
    const hinted = node.matches('.val,.value,.metric,.kpi-value,.rowval,.detailbox b,[data-value],[data-v],[data-metric]');
    const svgMark = node.matches('[data-rvbbit-key][data-value],rect[data-year],rect[data-value],[data-row-index],[data-index]');
    if (!value || (!hinted && !svgMark && node.childElementCount > 0)) return;
    const rect = node.getBoundingClientRect();
    raw.push({
      node,
      selector: selectorFor(node),
      tag: node.localName,
      classes: bounded(node.className?.baseVal || node.className,180),
      label: labelFor(node),
      rendered_text: bounded(node.textContent || node.getAttribute('title'),240),
      value, value_source,
      value_attribute,
      attributes: safeAttributes(node),
      element_context: contextData(node),
      bounds: {x:Math.round(rect.left),y:Math.round(rect.top),width:Math.round(rect.width),height:Math.round(rect.height)},
    });
  });
  const groupSelectorFor = (item) => {
    if (!item.classes) return '';
    const className = item.classes.split(/\s+/).filter(Boolean)[0];
    if (!className) return '';
    const container = item.node.closest('[id]');
    if (!container?.id || SENSITIVE.test(container.id)) return '';
    const selector = `#${css(container.id)} .${css(className)}`;
    try {
      const nodes = [...document.querySelectorAll(selector)].filter(visible);
      if (nodes.length >= 3 && nodes.length <= 250 && nodes.every((node) => Object.keys(contextData(node)).length)) return selector;
    } catch {}
    return '';
  };
  const groups = new Map();
  raw.forEach((item) => {
    const selector = groupSelectorFor(item);
    if (!selector) return;
    if (!groups.has(selector)) groups.set(selector,[]);
    groups.get(selector).push(item);
  });
  const groupedNodes = new Set();
  const candidates = [];
  [...groups.entries()].forEach(([selector,items]) => {
    if (items.length < 3) return;
    items.forEach((item) => groupedNodes.add(item.node));
    candidates.push({
      kind:'repeated', selector, label:items[0].label, tag:items[0].tag,
      rendered_text:items[0].rendered_text, value:items[0].value,
      value_attribute:items[0].value_attribute, value_source:items[0].value_source,
      element_context:items[0].element_context,
      represented_elements:items.length,
      samples:items.slice(0,16).map((item) => ({selector:item.selector,label:item.label,
        rendered_text:item.rendered_text,value:item.value,value_attribute:item.value_attribute,
        element_context:item.element_context,attributes:item.attributes})),
    });
  });
  raw.filter((item) => !groupedNodes.has(item.node)).forEach((item) => candidates.push({
    kind:'value', selector:item.selector, label:item.label, tag:item.tag, classes:item.classes,
    rendered_text:item.rendered_text, value:item.value, value_attribute:item.value_attribute,
    value_source:item.value_source, attributes:item.attributes,
    element_context:item.element_context, bounds:item.bounds,
    represented_elements:1,
  }));
  candidates.sort((left,right) => {
    const rank = (item) => item.kind === 'value' && /(?:val|value|metric|kpi)/i.test(item.classes || '') ? 0 : item.kind === 'repeated' ? 1 : 2;
    return rank(left)-rank(right) || (left.bounds?.y || 0)-(right.bounds?.y || 0) || (left.bounds?.x || 0)-(right.bounds?.x || 0);
  });
  const finalCandidates = candidates.slice(0,140).map((item,index) => ({...item,candidate_id:`candidate_${String(index+1).padStart(3,'0')}`}));
  const controls = [...document.querySelectorAll('input,select,textarea,[role="slider"],[role="switch"]')]
    .filter(visible).slice(0,80).map((node) => ({
      selector:selectorFor(node), tag:node.localName, type:node.type || node.getAttribute('role') || '',
      name:bounded(node.name || node.id,120), label:labelFor(node),
      value:node.type === 'checkbox' ? Boolean(node.checked) : bounded(node.value ?? node.textContent,500),
      options:node.matches('select') ? [...node.options].slice(0,80).map((option) => ({value:bounded(option.value,160),label:bounded(option.textContent,160)})) : [],
    }));
  const tables = [...document.querySelectorAll('table')].filter(visible).slice(0,24).map((table) => ({
    selector:selectorFor(table), label:labelFor(table),
    columns:[...table.querySelectorAll('thead tr:last-child th')].slice(0,40).map((node) => bounded(node.textContent,160)),
    sample_rows:[...table.querySelectorAll('tbody tr')].slice(0,8).map((row) => [...row.querySelectorAll('td,th')].slice(0,40).map((node) => bounded(node.textContent,240))),
  }));
  const canvasCharts = [...document.querySelectorAll('canvas')].filter(visible).slice(0,24).map((canvas) => {
    const chart = window.Chart?.getChart?.(canvas);
    return {renderer:'chartjs-canvas',selector:selectorFor(canvas),label:labelFor(canvas),
      labels:(chart?.data?.labels || []).slice(0,40),datasets:(chart?.data?.datasets || []).slice(0,12).map((dataset,index) => ({
        dataset_index:index,label:bounded(dataset.label,160),values:(dataset.data || []).slice(0,40),
      }))};
  });
  const tanstackCharts = [...document.querySelectorAll('[data-rvbbit-chart]')]
    .filter((node,index,all) => visible(node) && all.indexOf(node) === index && node.querySelector('svg.ts-chart'))
    .slice(0,24).map((container) => {
      const points = [...container.querySelectorAll('[data-rvbbit-key][data-row-index]')];
      const byMark = new Map();
      points.forEach((point) => {
        const mark = bounded(point.getAttribute('data-rvbbit-mark'),160) || 'unknown';
        byMark.set(mark,(byMark.get(mark)||0)+1);
      });
      return {
        renderer:'tanstack-svg',selector:selectorFor(container),label:labelFor(container),
        chart_id:bounded(container.getAttribute('data-rvbbit-chart'),160),
        query:bounded(container.getAttribute('data-rvbbit-query'),160),
        point_count:points.length,
        marks:[...byMark].slice(0,30).map(([id,count]) => ({id,count})),
        sample_points:points.slice(0,40).map((point) => ({
          key:bounded(point.getAttribute('data-rvbbit-key'),240),
          mark:bounded(point.getAttribute('data-rvbbit-mark'),160),
          row_index:Number(point.getAttribute('data-row-index')),
          field:bounded(point.getAttribute('data-field'),160),
          series:bounded(point.getAttribute('data-series'),160),
          value:bounded(point.getAttribute('data-value'),160),
          semantic_object:bounded(point.getAttribute('data-rvbbit-object-ref'),160),
        })),
      };
    });
  const charts = [...canvasCharts,...tanstackCharts].slice(0,36);
  const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(visible).slice(0,80).map((node) => bounded(node.textContent,240));
  return {title:document.title || headings[0] || '',headings,controls,tables,charts,candidates:finalCandidates,
    candidate_elements:finalCandidates.reduce((total,item) => total + Number(item.represented_elements || 1),0),
    viewport:{width:innerWidth,height:innerHeight,document_width:document.documentElement.scrollWidth,document_height:document.documentElement.scrollHeight}};
}
"""


def _semantic_capture_document(html):
    bridge = """<script>
window.rvbbitQuery=async function(sql,opts){return await window.__rvbbitSemanticQuery(sql,opts||{});};
window.cowork=window.cowork||{};
window.cowork.callMcpTool=async function(tool,args){
  args=args||{};
  if(String(tool||'').endsWith('run_sql_multi')){
    const results={};
    for(const [name,sql] of Object.entries(args.queries||{})){
      results[name]=await window.rvbbitQuery(sql,{as_of:args.as_of||null});
    }
    return {structuredContent:{results}};
  }
  const data=await window.rvbbitQuery(args.sql||'',{as_of:args.as_of||null});
  return {structuredContent:{...data,rows:(data&&data.rows)||[]}};
};
</script>"""
    doc = _inline_artifact_system_assets(html)
    match = re.search(r"<head[^>]*>", doc, re.IGNORECASE)
    return doc[:match.end()] + bridge + doc[match.end():] if match else bridge + doc


def _semantic_packet_query(sql_text, result, ordinal):
    result = result if isinstance(result, dict) else {}
    return {
        "query_id": f"runtime_{ordinal}",
        "sql": str(sql_text or "")[:50_000],
        "columns": list(result.get("columns") or [])[:160],
        "sample_rows": list(result.get("rows") or [])[:12],
        "row_count": int(result.get("row_count") or 0),
        "truncated": bool(result.get("truncated")),
        "engine": result.get("engine"),
        "elapsed_ms": result.get("elapsed_ms"),
        "error": result.get("error"),
    }


def _capture_semantic_evidence(job):
    """Render one immutable version and return bounded code + runtime evidence."""
    from playwright.sync_api import sync_playwright

    app, row = _load_live_app_version(job["slug"], int(job["version"]))
    if not app or not row:
        raise ValueError(f"artifact version disappeared: {job['slug']}@v{job['version']}")
    html = row.get("html") or ""
    queries = []
    console_errors = []
    page_errors = []
    owner = job.get("owner_email")

    def run_query(sql_text, opts=None):
        token = _SESSION_SUB.set(owner)
        try:
            result = tool_run_sql(str(sql_text or ""), (opts or {}).get("as_of"))
        finally:
            _SESSION_SUB.reset(token)
        queries.append(_semantic_packet_query(sql_text, result, len(queries) + 1))
        return json.loads(json.dumps(result, default=str))

    capture_root = _live_app_capture_root() / "semantic"
    capture_root.mkdir(parents=True, exist_ok=True)
    screenshot = capture_root / f"{job['slug']}-v{int(job['version'])}.jpg"
    width = _env_int("WAREHOUSE_SEMANTIC_ENRICH_WIDTH", 1200, minimum=640, maximum=1920)
    height = _env_int("WAREHOUSE_SEMANTIC_ENRICH_HEIGHT", 800, minimum=480, maximum=1440)
    wait_ms = _env_int("WAREHOUSE_SEMANTIC_ENRICH_RENDER_WAIT_MS", 3000, minimum=250, maximum=15_000)
    with sync_playwright() as playwright:
        browser = _launch_playwright_chromium(playwright)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.expose_function("__rvbbitSemanticQuery", run_query)
        page.on(
            "console",
            lambda message: console_errors.append(message.text[:500])
            if message.type in {"error", "warning"} and len(console_errors) < 20 else None,
        )
        page.on(
            "pageerror",
            lambda error: page_errors.append(str(error)[:500]) if len(page_errors) < 20 else None,
        )
        page.set_content(_semantic_capture_document(html), wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(wait_ms)
        dom = page.evaluate(_SEMANTIC_EVIDENCE_JS)
        page.screenshot(path=str(screenshot), full_page=False, type="jpeg", quality=68)
        browser.close()

    source_limit = _env_int(
        "WAREHOUSE_SEMANTIC_ENRICH_SOURCE_CHARS", 180_000, minimum=20_000, maximum=800_000
    )
    source = (html + _source_files_text(row.get("source_files")))[:source_limit]
    packet = {
        "schema_version": "rvbbit.artifact-evidence.v1",
        "artifact": {
            "slug": app["slug"],
            "name": app["name"],
            "description": app.get("description"),
            "version": int(row["version"]),
            "app_kind": app.get("app_kind"),
            "runtime_kind": app.get("runtime_kind"),
        },
        "authored_semantic_map": ((row.get("manifest") or {}).get("semantic_map") or {}),
        "dom": dom,
        "runtime_queries": queries[:24],
        "source": source,
        "render_health": {
            "queries_ran": len(queries),
            "queries_failed": sum(1 for item in queries if item.get("error")),
            "console_errors": console_errors,
            "page_errors": page_errors,
        },
        "screenshot": {"path": str(screenshot), "width": width, "height": height},
    }
    vision = []
    if screenshot.is_file() and screenshot.stat().st_size <= 3 * 1024 * 1024:
        import base64
        vision.append({
            "dataUrl": "data:image/jpeg;base64," + base64.b64encode(screenshot.read_bytes()).decode(),
        })
    return packet, vision


def _semantic_operator_available():
    try:
        with _conn() as c:
            c.execute(
                "UPDATE rvbbit.operators SET model=%s,"
                "steps=jsonb_set(steps,'{0,model}',to_jsonb(%s::text),true),updated_at=now() "
                "WHERE name='artifact_semantic_enrich' AND "
                "(model IS DISTINCT FROM %s OR steps#>>'{0,model}' IS DISTINCT FROM %s)",
                (
                    _SEMANTIC_ENRICH_MODEL,
                    _SEMANTIC_ENRICH_MODEL,
                    _SEMANTIC_ENRICH_MODEL,
                    _SEMANTIC_ENRICH_MODEL,
                ),
            )
            row = c.execute(
                "SELECT to_regprocedure('rvbbit.artifact_semantic_enrich(jsonb,jsonb,jsonb)') IS NOT NULL AS ok"
            ).fetchone()
        return bool((row or {}).get("ok"))
    except Exception:
        return False


def _semantic_operator_payload(envelope):
    if isinstance(envelope, str):
        envelope = json.loads(envelope)
    if not isinstance(envelope, dict):
        raise ValueError("semantic enrichment operator returned no JSON object")
    result = envelope
    agent_run_id = None
    # SQL operators return their selected `result` column inside the scalar
    # function envelope. Agent workflows then intentionally add a second
    # result envelope with the run receipt. Tolerate both that shape and a
    # direct object so the publication worker is not coupled to transport
    # serialization details.
    for _depth in range(4):
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            break
        agent_run_id = result.get("agent_run_id") or agent_run_id
        if isinstance(result.get("semantic_map"), dict) or isinstance(result.get("objects"), list):
            return result, agent_run_id
        if "result" not in result:
            break
        result = result["result"]
    raise ValueError("semantic enrichment operator result did not contain semantic objects")


def _run_semantic_operator(packet, vision):
    if not _semantic_operator_available():
        raise ValueError("rvbbit.artifact_semantic_enrich is not installed")
    with _conn() as c:
        c.execute("SET statement_timeout = 600000")
        row = c.execute(
            "SELECT rvbbit.artifact_semantic_enrich(%s::jsonb,%s::jsonb) AS result",
            (_json_default(packet), _json_default(vision)),
        ).fetchone()
    return _semantic_operator_payload((row or {}).get("result"))


def _semantic_candidate_id(raw):
    if not isinstance(raw, dict):
        return ""
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    return str(raw.get("candidate_id") or evidence.get("candidate_id") or "").strip()


def _semantic_source_key(source):
    raw = str(source or "")
    match = re.fullmatch(r"\$(?:element|selection)\.data\.([a-zA-Z][a-zA-Z0-9_]*)", raw)
    if not match:
        match = re.fullmatch(
            r"\$element\.attr\.data-([a-zA-Z][a-zA-Z0-9_-]*)", raw
        )
    return match.group(1).replace("-", "_") if match else None


def _candidate_sample_for_defaults(candidate, raw_object):
    samples = candidate.get("samples") or [candidate]
    parameters = raw_object.get("parameters") if isinstance(raw_object.get("parameters"), dict) else {}
    for sample in samples:
        context = sample.get("element_context") or {}
        matched = True
        for spec in parameters.values():
            if not isinstance(spec, dict):
                continue
            key = _semantic_source_key(spec.get("source"))
            if key and str(context.get(key)) != str(spec.get("default")):
                matched = False
                break
        if matched:
            return sample
    return samples[0] if samples else candidate


def _verified_semantic_overlay(agent_result, evidence, owner=None):
    """Accept agent candidates one at a time; bad guesses cannot poison good objects."""
    raw_map = agent_result.get("semantic_map") if isinstance(agent_result.get("semantic_map"), dict) else {}
    raw_objects = agent_result.get("objects") or raw_map.get("objects") or []
    if not isinstance(raw_objects, list):
        raise ValueError("semantic enrichment objects must be a list")
    candidates = {
        item.get("candidate_id"): item
        for item in ((evidence.get("dom") or {}).get("candidates") or [])
        if isinstance(item, dict) and item.get("candidate_id")
    }
    authored_objects = (
        (evidence.get("authored_semantic_map") or {}).get("objects") or []
    )
    authored_ids = {str(item.get("id") or "") for item in authored_objects if isinstance(item, dict)}
    authored_bindings = set().union(*(
        _semantic_binding_keys(item) for item in authored_objects if isinstance(item, dict)
    )) if authored_objects else set()
    verified = []
    rejected = []
    seen_ids = set(authored_ids)
    represented = 0
    token = _SESSION_SUB.set(owner)
    try:
        for raw in raw_objects[:160]:
            object_id = str(raw.get("id") or "") if isinstance(raw, dict) else ""
            candidate_id = _semantic_candidate_id(raw)
            candidate = candidates.get(candidate_id)
            try:
                if not isinstance(raw, dict):
                    raise ValueError("candidate is not an object")
                if not candidate:
                    raise ValueError(f"unknown or missing candidate_id {candidate_id or '(missing)'}")
                if object_id in seen_ids:
                    raise ValueError(f"duplicate or authored object id {object_id}")
                prepared = dict(raw)
                raw_bindings = raw.get("bindings") or raw.get("binding") or [{}]
                if not isinstance(raw_bindings, list):
                    raw_bindings = [raw_bindings]
                supplied = raw_bindings[0] if raw_bindings and isinstance(raw_bindings[0], dict) else {}
                binding = {
                    key: supplied[key]
                    for key in ("role", "chart_dataset", "table_column", "dataset_index", "context", "value_source")
                    if key in supplied
                }
                binding["selector"] = candidate["selector"]
                if candidate.get("value_source"):
                    binding["value_source"] = candidate["value_source"]
                elif not binding.get("value_source") and candidate.get("value_attribute", "").startswith("data-"):
                    binding["value_source"] = "$element.data." + candidate["value_attribute"][5:].replace("-", "_")
                prepared["bindings"] = [binding]
                prepared.pop("binding", None)
                normalized = _normalize_semantic_object(prepared)
                if _semantic_binding_keys(normalized) & authored_bindings:
                    raise ValueError("candidate overlaps an authored DOM binding")
                validation = _validate_semantic_manifest({
                    "semantic_map": {"schema_version": _SEMANTIC_MAP_SCHEMA, "objects": [normalized]}
                }, execute=True)
                validation_item = validation["objects"][0]
                sample = _candidate_sample_for_defaults(candidate, raw)
                rendered_value = sample.get("value")
                if rendered_value in (None, ""):
                    raise ValueError("candidate has no captured rendered value")
                matches = _semantic_values_match(
                    rendered_value, validation_item.get("value"), normalized.get("display")
                )
                if matches is not True:
                    raise ValueError(
                        f"replay value {validation_item.get('value')!r} did not match rendered value {rendered_value!r}"
                    )
                verified.append(normalized)
                seen_ids.add(normalized["id"])
                represented += int(candidate.get("represented_elements") or 1)
            except Exception as exc:  # noqa: BLE001 — retain other independently verified objects
                rejected.append({
                    "id": object_id or None,
                    "candidate_id": candidate_id or None,
                    "error": _semantic_text(exc, 700),
                })
    finally:
        _SESSION_SUB.reset(token)
    total_elements = int((evidence.get("dom") or {}).get("candidate_elements") or 0)
    coverage = (represented / total_elements) if total_elements else 1.0
    semantic_map = {
        "schema_version": _SEMANTIC_MAP_SCHEMA,
        "description": _semantic_text(
            raw_map.get("description") or agent_result.get("description")
            or "Automatically compiled business meanings and independently replayable SQL.",
            1000,
        ),
        "objects": verified,
    }
    report = {
        "verified_count": len(verified),
        "rejected_count": len(rejected),
        "candidate_count": len(candidates),
        "candidate_elements": total_elements,
        "covered_elements": represented,
        "coverage": round(coverage, 4),
        "rejected": rejected[:80],
        "unmapped": list(agent_result.get("unmapped") or [])[:80],
    }
    return semantic_map, report


def _claim_semantic_enrichment_job():
    max_attempts = _env_int("WAREHOUSE_SEMANTIC_ENRICH_MAX_ATTEMPTS", 3, minimum=1, maximum=8)
    with psycopg.connect(DSN, row_factory=dict_row, autocommit=False) as c:
        c.execute(
            "UPDATE rvbbit.artifact_semantic_enrichments SET status='pending',not_before=now(),"
            "last_error=coalesce(last_error,'') || CASE WHEN coalesce(last_error,'')='' THEN '' ELSE E'\\n' END || "
            "'Recovered stale worker claim',updated_at=now() "
            "WHERE status='running' AND started_at < now() - interval '20 minutes' AND attempts < %s",
            (max_attempts,),
        )
        c.execute(
            "UPDATE rvbbit.artifact_semantic_enrichments SET status='failed',completed_at=now(),"
            "last_error=coalesce(last_error,'Semantic enrichment exhausted its retry budget'),updated_at=now() "
            "WHERE status='running' AND started_at < now() - interval '20 minutes' AND attempts >= %s",
            (max_attempts,),
        )
        row = c.execute(
            "SELECT e.*,d.slug,d.name,d.owner_email,d.runtime_kind,d.app_kind "
            "FROM rvbbit.artifact_semantic_enrichments e "
            "JOIN rvbbit.dashboards d ON d.id=e.dashboard_id "
            "WHERE e.status='pending' AND e.not_before<=now() "
            "ORDER BY e.enqueued_at FOR UPDATE OF e SKIP LOCKED LIMIT 1"
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE rvbbit.artifact_semantic_enrichments SET status='running',attempts=attempts+1,"
            "started_at=now(),last_error=NULL,updated_at=now() WHERE dashboard_id=%s AND version=%s",
            (row["dashboard_id"], row["version"]),
        )
        job = dict(row)
        job["attempts"] = int(row.get("attempts") or 0) + 1
    return job


def _complete_semantic_enrichment(job, semantic_map, verification, agent_run_id):
    status = "partial" if verification.get("rejected_count") else "ready"
    with _conn() as c:
        c.execute(
            "UPDATE rvbbit.artifact_semantic_enrichments SET status=%s,semantic_map=%s::jsonb,"
            "verification=%s::jsonb,agent_run_id=%s,model=%s,completed_at=now(),last_error=NULL,updated_at=now() "
            "WHERE dashboard_id=%s AND version=%s",
            (
                status,
                _json_default(semantic_map),
                _json_default(verification),
                agent_run_id,
                _SEMANTIC_ENRICH_MODEL,
                job["dashboard_id"],
                job["version"],
            ),
        )
    _crawl_safe(job["slug"], use_llm=False)


def _fail_semantic_enrichment(job, exc):
    max_attempts = _env_int("WAREHOUSE_SEMANTIC_ENRICH_MAX_ATTEMPTS", 3, minimum=1, maximum=8)
    attempts = int(job.get("attempts") or 1)
    retry = attempts < max_attempts
    delay = min(300, 15 * (2 ** max(0, attempts - 1)))
    with _conn() as c:
        c.execute(
            "UPDATE rvbbit.artifact_semantic_enrichments SET status=%s,last_error=%s,"
            "not_before=now()+(%s * interval '1 second'),completed_at=%s,updated_at=now() "
            "WHERE dashboard_id=%s AND version=%s",
            (
                "pending" if retry else "failed",
                _semantic_text(f"{type(exc).__name__}: {exc}", 2000),
                delay if retry else 0,
                None if retry else datetime.now(timezone.utc),
                job["dashboard_id"],
                job["version"],
            ),
        )
    if retry:
        _SEMANTIC_ENRICH_WAKE.set()


def _process_semantic_enrichment_job(job):
    packet, vision = _capture_semantic_evidence(job)
    candidate_count = len((packet.get("dom") or {}).get("candidates") or [])
    if not candidate_count:
        _complete_semantic_enrichment(
            job,
            {"schema_version": _SEMANTIC_MAP_SCHEMA, "objects": []},
            {"verified_count": 0, "rejected_count": 0, "candidate_count": 0, "coverage": 1.0},
            None,
        )
        return
    agent_result, agent_run_id = _run_semantic_operator(packet, vision)
    semantic_map, verification = _verified_semantic_overlay(
        agent_result, packet, job.get("owner_email")
    )
    if not semantic_map.get("objects"):
        errors = "; ".join(item.get("error") or "" for item in verification.get("rejected") or [])
        raise ValueError("agent produced no verified semantic objects" + (f": {errors[:1000]}" if errors else ""))
    _complete_semantic_enrichment(job, semantic_map, verification, agent_run_id)


def _semantic_enrichment_worker():
    unavailable_logged = False
    while True:
        if not _semantic_enrichment_enabled():
            _SEMANTIC_ENRICH_WAKE.wait(30)
            _SEMANTIC_ENRICH_WAKE.clear()
            continue
        if not _semantic_operator_available():
            if not unavailable_logged:
                print(
                    "Semantic enrichment queued but rvbbit.artifact_semantic_enrich is not installed yet",
                    file=sys.stderr,
                )
                unavailable_logged = True
            _SEMANTIC_ENRICH_WAKE.wait(30)
            _SEMANTIC_ENRICH_WAKE.clear()
            continue
        unavailable_logged = False
        try:
            job = _claim_semantic_enrichment_job()
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: semantic enrichment queue unavailable: {exc}", file=sys.stderr)
            _SEMANTIC_ENRICH_WAKE.wait(15)
            _SEMANTIC_ENRICH_WAKE.clear()
            continue
        if not job:
            _SEMANTIC_ENRICH_WAKE.wait(10)
            _SEMANTIC_ENRICH_WAKE.clear()
            continue
        try:
            _process_semantic_enrichment_job(job)
            print(
                f"Semantic enrichment complete: {job['slug']}@v{job['version']}",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001 — publication stays live; queue owns retries
            _fail_semantic_enrichment(job, exc)
            print(
                f"WARNING: semantic enrichment failed for {job['slug']}@v{job['version']}: {exc}",
                file=sys.stderr,
            )


def _start_semantic_enrichment_worker():
    global _SEMANTIC_ENRICH_THREAD
    if not _semantic_enrichment_enabled():
        return False
    with _SEMANTIC_ENRICH_THREAD_LOCK:
        if _SEMANTIC_ENRICH_THREAD and _SEMANTIC_ENRICH_THREAD.is_alive():
            return True
        _SEMANTIC_ENRICH_THREAD = threading.Thread(
            target=_semantic_enrichment_worker,
            name="artifact-semantic-enricher",
            daemon=True,
        )
        _SEMANTIC_ENRICH_THREAD.start()
    return True


def tool_semantic_enrichment_status(slug, version=None):
    _ensure_dashboard_tables()
    with _conn() as c:
        dashboard = c.execute(
            "SELECT id,latest_version FROM rvbbit.dashboards WHERE slug=%s", (slug,)
        ).fetchone()
    if not dashboard:
        return {"error": {"code": "NOT_FOUND", "message": slug}}
    selected = int(version or dashboard["latest_version"])
    row = _semantic_enrichment_row(dashboard["id"], selected)
    return {"slug": slug, **_semantic_enrichment_public(row)}


def tool_enrich_live_app(slug, version=None, force=False):
    """Queue a missing/failed historical version; normal publish paths call this automatically."""
    _ensure_dashboard_tables()
    with _conn() as c:
        dashboard = c.execute(
            "SELECT id,latest_version FROM rvbbit.dashboards WHERE slug=%s", (slug,)
        ).fetchone()
    if not dashboard:
        return {"error": {"code": "NOT_FOUND", "message": slug}}
    selected = int(version or dashboard["latest_version"])
    queued = _enqueue_semantic_enrichment(dashboard["id"], selected, force=bool(force))
    _start_semantic_enrichment_worker()
    return {"slug": slug, **queued}


# ── dependency extraction (Phase 1: queries → tables/metrics, the derived index) ──

_RVBBIT_QUERY_RE = re.compile(r"rvbbitQuery\(\s*([`'\"])(.*?)\1", re.DOTALL)
# Any quoted string that looks like SQL — catches SQL assigned to a variable and passed
# as `client(sql)` (Claude rarely inlines the literal in the rvbbitQuery() call). EXPLAIN
# is the filter: a candidate that resolves to real tables is a real query; junk is dropped.
_SQL_LIT_RE = re.compile(r"([`'\"])\s*((?:select|with)\b.*?\bfrom\b.*?)\1", re.IGNORECASE | re.DOTALL)
_METRIC_RE = re.compile(r"""(?:rvbbitMetric|rvbbit\.metric|\bmetric)\(\s*['"]([a-zA-Z0-9_]+)['"]""")
EXTRACT_MODEL = os.environ.get("WAREHOUSE_EXTRACT_MODEL", "anthropic/claude-3.5-sonnet")


def _extract_queries(html):
    """SQL passed literally to the injected client (the cleanest case)."""
    return [m.group(2).strip() for m in _RVBBIT_QUERY_RE.finditer(html or "") if m.group(2).strip()]


def _extract_sql_literals(html):
    """Candidate SQL-shaped string literals anywhere in the artifact (validated by EXPLAIN)."""
    out, seen = [], set()
    for m in _SQL_LIT_RE.finditer(html or ""):
        s = m.group(2).strip()
        if s and len(s) < 8000 and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _referenced_tables(sql):
    """Every relation a query touches — planner-resolved (EXPLAIN does NOT execute), so it
    catches plain heap tables too, not just rvbbit-managed ones."""
    try:
        with _conn(read_only=True) as c, c.cursor() as cur:
            cur.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + sql)
            raw = cur.fetchone()["QUERY PLAN"]
        plan = json.loads(raw) if isinstance(raw, str) else raw
        tables = set()

        def walk(node):
            if isinstance(node, dict):
                if node.get("Relation Name"):
                    sch = node.get("Schema")
                    tables.add(f'{sch}.{node["Relation Name"]}' if sch else node["Relation Name"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(plan)
        return sorted(tables)
    except Exception:   # noqa: BLE001 — unparseable SQL → no tables
        return []


def _llm_extract(html):
    """Fallback for artifacts that don't follow the rvbbitQuery contract (dynamic SQL).
    Best-effort: needs OPENROUTER_API_KEY; the LLM only *finds* the SQL — route_explain/
    EXPLAIN still resolve the tables deterministically."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key or not html:
        return [], []
    import httpx
    prompt = ('Extract the data dependencies from this dashboard source. Return ONLY JSON: '
              '{"queries":["<each full SQL the page runs>"],"metrics":["<metric names>"]}. '
              'Give a representative form for dynamically-built SQL. No prose.\n\n--- SOURCE ---\n' + html[:24000])
    try:
        with httpx.Client(timeout=45) as cli:
            r = cli.post("https://openrouter.ai/api/v1/chat/completions",
                         headers={"Authorization": f"Bearer {key}"},
                         json={"model": EXTRACT_MODEL, "temperature": 0,
                               "messages": [{"role": "user", "content": prompt}]})
            txt = r.json()["choices"][0]["message"]["content"]
        d = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
        return [q for q in d.get("queries", []) if q], [m for m in d.get("metrics", []) if m]
    except Exception as e:   # noqa: BLE001
        print(f"WARNING: dashboard LLM extraction failed: {e}", file=sys.stderr)
        return [], []


def dashboard_crawl(slug, use_llm=True):
    """Rebuild a dashboard's dependency index: gather its SQL (parse the rvbbitQuery calls +
    the queries it actually ran + an LLM fallback), resolve each to tables via EXPLAIN,
    detect metric() usage, and store the edges. Regenerable — replaces prior deps."""
    _ensure_dashboard_tables()
    _ensure_activity_table()
    with _conn() as c:
        d = c.execute("SELECT id, latest_version FROM rvbbit.dashboards WHERE slug=%s", (slug,)).fetchone()
        if not d:
            return {"error": {"code": "NOT_FOUND", "message": slug}}
        did, ver = d["id"], d["latest_version"]
        hrow = c.execute(
            "SELECT html, source_files, manifest FROM rvbbit.dashboard_versions "
            "WHERE dashboard_id=%s AND version=%s",
            (did, ver),
        ).fetchone()
        html = ((hrow["html"] if hrow else "") or "") + _source_files_text((hrow or {}).get("source_files"))
        effective_manifest = _effective_artifact_manifest(
            did, ver, (hrow or {}).get("manifest") or {}
        )
        semantic_objects = (
            (effective_manifest.get("semantic_map") or {}).get("objects")
            or []
        )
        runtime = [r["sql"] for r in c.execute(
            "SELECT DISTINCT args->>'sql' AS sql FROM rvbbit.mcp_activity "
            "WHERE tool='dashboard_query' AND args->>'dashboard'=%s "
            "AND coalesce(args->>'origin','dashboard')='dashboard' "
            "AND args->>'sql' IS NOT NULL",
            (slug,),
        ).fetchall()]
        known_metrics = {r["name"] for r in c.execute("SELECT DISTINCT name FROM rvbbit.metric_defs").fetchall()}

    sql_src = {}                                  # sql -> where we found it (trusted)
    for q in _extract_queries(html):
        sql_src.setdefault(q, "rvbbitQuery")
    for q in runtime:
        sql_src.setdefault(q, "runtime")
    semantic_sql = []
    for semantic_object in semantic_objects:
        try:
            q, _context = _render_semantic_sql(semantic_object)
        except ValueError:
            continue
        if q:
            semantic_sql.append((q, str(semantic_object.get("id") or "")))
    for evaluator_sql, _object_id in semantic_sql:
        # Older Lens executions predate the explicit activity origin tag. If a
        # runtime-only activity row is byte-for-byte an evaluator, represent it
        # as the semantic edge instead of inflating the dashboard query count.
        if sql_src.get(evaluator_sql) == "runtime":
            sql_src.pop(evaluator_sql, None)
    # SQL-shaped literals are candidates — only kept if EXPLAIN resolves real tables
    known_sql = set(sql_src) | {sql for sql, _source in semantic_sql}
    candidates = [q for q in _extract_sql_literals(html) if q not in known_sql]
    llm_metrics = []
    if use_llm and not sql_src and not semantic_sql and not candidates:
        # Only pay for the LLM when deterministic source + manifest extraction found nothing.
        lq, llm_metrics = _llm_extract(html)
        for q in lq:
            sql_src.setdefault(q, "llm")

    table_cache = {}

    def resolved_tables(sql):
        if sql not in table_cache:
            table_cache[sql] = _referenced_tables(sql)
        return table_cache[sql]

    sql_tables = {sql: resolved_tables(sql) for sql in sql_src}
    semantic_tables = [
        (sql, object_id, resolved_tables(sql))
        for sql, object_id in semantic_sql
    ]
    for sql in candidates:                        # promote candidates that validate
        t = resolved_tables(sql)
        if t:
            sql_src.setdefault(sql, "sql-literal")
            sql_tables[sql] = t

    metric_names = ({m for m in _METRIC_RE.findall(html or "")} | set(llm_metrics)) & known_metrics
    tables = {}                                   # table -> source
    rows = []
    for sql, src in sql_src.items():
        rows.append(("query", None, sql, src))
        for t in sql_tables.get(sql, []):
            tables.setdefault(t, src)
    for sql, object_id, resolved in semantic_tables:
        # Keep one query edge per named object even when two objects happen to
        # share identical evaluator SQL. The manifest is a semantic map, not
        # merely a deduplicated list of query strings.
        source = f"semantic-map:{object_id}"
        rows.append(("semantic", object_id, sql, source))
        for table in resolved:
            tables.setdefault(table, source)
    rows += [("table", t, None, src) for t, src in tables.items()]
    rows += [("metric", m, None, "parse") for m in metric_names]
    status = "live" if (sql_src or semantic_sql or metric_names) else "materialized"

    with _conn() as c:
        # Dependency rows are versioned evidence. Re-crawling one published
        # version should replace only that version, not erase the lineage that
        # immutable Calliope surfaces and Artifact Lens investigations rely on.
        c.execute(
            "DELETE FROM rvbbit.dashboard_deps WHERE dashboard_id=%s AND version=%s",
            (did, ver),
        )
        for kind, obj, bsql, src in rows:
            c.execute("INSERT INTO rvbbit.dashboard_deps (dashboard_id,version,kind,object_ref,base_sql,source) "
                      "VALUES (%s,%s,%s,%s,%s,%s)", (did, ver, kind, obj, bsql, src))
        c.execute("UPDATE rvbbit.dashboards SET status=%s WHERE id=%s", (status, did))
    return {
        "slug": slug,
        "status": status,
        "queries": len(sql_src),
        "semantic_objects": len(semantic_sql),
        "tables": sorted(tables),
        "metrics": sorted(metric_names),
    }


def _crawl_safe(slug, use_llm=False):
    try:
        return dashboard_crawl(slug, use_llm=use_llm)
    except Exception as e:   # noqa: BLE001 — never let a crawl failure break publish
        return {"error": str(e)}


def tool_dashboard_dependents(object_ref):
    """Impact analysis: which dashboards depend on a table or metric."""
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT slug, name, team, kind FROM rvbbit.dashboard_sources WHERE object_ref=%s",
            (object_ref,)).fetchall()
    return {"object": object_ref, "dashboards": rows}


# MCP wrappers (named, so their docstring becomes the tool description Claude reads)
def _mcp_upload_artifact(content, name=None, artifact_id=None, append=False, ctx=None):
    """Stage a large HTML/source payload server-side and get an artifact_id handle back.
    Then publish WITHOUT re-transmitting the document: pass source_artifact_id to
    publish_dashboard / update_dashboard / create_live_app / update_live_app. For very large
    payloads, send chunks: first call returns the artifact_id, subsequent calls pass it with
    append=true. Returns bytes + sha256 for integrity checking. Artifacts expire after ~48h —
    they are a staging area, not storage."""
    return _with_forwarded_mcp_caller(ctx, lambda: _logged(
        "upload_artifact",
        {"name": name, "artifact_id": artifact_id, "append": append,
         "content_bytes": len(content or "")},
        lambda: tool_upload_artifact(content, name, artifact_id, append),
    ))


def _mcp_publish_dashboard(name, html=None, team=None, description=None, kind="live",
                           source_artifact_id=None, manifest=None, ctx=None):
    """Persist a dashboard so it lives + works OUTSIDE Cowork (a shareable URL + the lens app).
    Build `html` from the `dashboard_template` boilerplate (call that tool FIRST): it gets LIVE
    data through Cowork's callMcpTool→run_sql bridge in-app, and the host's injected rvbbitQuery
    when served — the SAME artifact works both places, no login. Instead of inlining a large
    document, you can upload_artifact once and pass source_artifact_id here. Keep each data
    concern its OWN FLAT query in the composePayload parts map — the framework batches them into
    ONE run_sql_multi round trip. NEVER hand-write a json_build_object payload query (it hides the
    SQL from the catalog and the accelerated engines), and NEVER bake query results into the
    HTML — that's a 'dead tree' with no live data or inspectability. Publication automatically
    queues RVBBIT's semantic compiler, which derives and verifies business-object bindings without
    changing the HTML. An authored manifest.semantic_map is optional and takes precedence when the
    builder already knows an especially precise meaning or evaluator."""
    return _with_forwarded_mcp_caller(ctx, lambda: _logged(
        "publish_dashboard", {"name": name, "team": team, "kind": kind,
                              "html_bytes": len(html or ""),
                              "semantic_objects": len(
                                  ((manifest or {}).get("semantic_map") or {}).get("objects") or []
                              ) if isinstance(manifest, dict) else None,
                              "source_artifact_id": source_artifact_id},
        lambda: tool_publish_dashboard(
            name, html, team, description, kind, source_artifact_id, manifest
        ),
    ))


def _mcp_update_dashboard(slug, html=None, notes=None, source_artifact_id=None, manifest=None,
                          ctx=None):
    """Publish a new version of an existing dashboard (by slug). Accepts inline html or an
    upload_artifact handle via source_artifact_id. Omit manifest to retain the current semantic
    map; pass a manifest patch to revise its versioned business-object definitions."""
    return _with_forwarded_mcp_caller(ctx, lambda: _logged(
        "update_dashboard", {"slug": slug, "html_bytes": len(html or ""), "notes": notes,
                             "source_artifact_id": source_artifact_id},
        lambda: tool_update_dashboard(slug, html, notes, source_artifact_id, manifest),
    ))


def _mcp_list_dashboards(team=None, search=None):
    """List published dashboards (optionally filter by team or a name/description search)."""
    return _logged("list_dashboards", {"team": team, "search": search},
                   lambda: tool_list_dashboards(team, search))


def _mcp_get_dashboard(slug, version=None):
    """Fetch a dashboard's metadata, source, and data dependencies (to inspect or fork it)."""
    return _logged("get_dashboard", {"slug": slug, "version": version},
                   lambda: tool_get_dashboard(slug, version))


def _mcp_dashboard_crawl(slug):
    """Re-extract a dashboard's data dependencies (queries → tables, metrics) into the
    catalog index — runs the LLM pass + reconciles the queries it actually ran."""
    return _logged("dashboard_crawl", {"slug": slug}, lambda: dashboard_crawl(slug, use_llm=True))


def _mcp_dashboard_dependents(object):
    """Impact analysis: which dashboards depend on a given table or metric."""
    return _logged("dashboard_dependents", {"object": object},
                   lambda: tool_dashboard_dependents(object))


def _mcp_live_app_template(runtime_kind="html", app_kind="dashboard"):
    """Return a starter live-app contract. Use html for immediately hosted apps; use
    python-fastapi to scaffold source that is stored, versioned, and runnable under local uvicorn."""
    return _logged("live_app_template", {"runtime_kind": runtime_kind, "app_kind": app_kind},
                   lambda: tool_live_app_template(runtime_kind, app_kind))


def _mcp_create_live_app(name, html=None, runtime_kind="html", app_kind="dashboard",
                         team=None, description=None, manifest=None, source_files=None,
                         source_artifact_id=None, ctx=None):
    """Create a versioned RVBBIT live app. HTML apps are hosted immediately at /d/<slug> and
    call rvbbitQuery(sql) for live, read-only data. Publication automatically queues a separate
    semantic compiler pass; an authored manifest.semantic_map remains an optional precise hint.
    Accepts inline html or an upload_artifact handle via source_artifact_id."""
    return _with_forwarded_mcp_caller(ctx, lambda: _logged(
        "create_live_app", {
            "name": name,
            "runtime_kind": runtime_kind,
            "app_kind": app_kind,
            "team": team,
            "html_bytes": len(html or ""),
            "source_artifact_id": source_artifact_id,
        }, lambda: tool_create_live_app(name, html, runtime_kind, app_kind, team, description,
                                        manifest, source_files, source_artifact_id),
    ))


def _mcp_update_live_app(slug, html=None, notes=None, manifest=None, source_files=None,
                         runtime_kind=None, app_kind=None, source_artifact_id=None, ctx=None):
    """Publish a new version of a live app. Omitted source fields are preserved. Accepts inline
    html or an upload_artifact handle via source_artifact_id. Omit manifest to retain the current
    semantic map; pass a manifest patch whenever visible meanings, bindings, filters, or replay
    SQL change."""
    return _with_forwarded_mcp_caller(ctx, lambda: _logged(
        "update_live_app", {
            "slug": slug,
            "html_bytes": len(html or ""),
            "notes": notes,
            "source_artifact_id": source_artifact_id,
        }, lambda: tool_update_live_app(slug, html, notes, manifest, source_files, runtime_kind,
                                        app_kind, source_artifact_id),
    ))


def _mcp_semantic_enrichment_status(slug, version=None):
    """Inspect the non-blocking semantic compiler state for one immutable artifact version."""
    return _logged(
        "semantic_enrichment_status",
        {"slug": slug, "version": version},
        lambda: tool_semantic_enrichment_status(slug, version),
    )


def _mcp_enrich_live_app(slug, version=None, force=False):
    """Queue semantic enrichment for an older or failed artifact version. New HTML versions are
    queued automatically; this tool is for backfills and explicit retries, not normal publishing."""
    return _logged(
        "enrich_live_app",
        {"slug": slug, "version": version, "force": force},
        lambda: tool_enrich_live_app(slug, version, force),
    )


def _mcp_list_live_apps(team=None, search=None, runtime_kind=None, app_kind=None):
    """List versioned live apps with runtime kind, health, dependency counts, and URLs."""
    return _logged("list_live_apps", {
        "team": team,
        "search": search,
        "runtime_kind": runtime_kind,
        "app_kind": app_kind,
    }, lambda: tool_list_live_apps(team, search, runtime_kind, app_kind))


def _mcp_get_live_app(slug, version=None, include_source=True):
    """Fetch a live app's metadata, manifest, versioned source, dependencies, and recent query calls."""
    return _logged("get_live_app", {"slug": slug, "version": version, "include_source": include_source},
                   lambda: tool_get_live_app(slug, version, include_source))


def _mcp_debug_live_app(slug, run_crawl=True, include_activity=True):
    """Inspect and refresh a live app's health: dependency crawl, recent query errors, runtime status,
    and recommended next actions."""
    return _logged("debug_live_app", {
        "slug": slug,
        "run_crawl": run_crawl,
        "include_activity": include_activity,
    }, lambda: tool_debug_live_app(slug, run_crawl, include_activity))


def _mcp_live_app_logs(slug, limit=50):
    """Return recent live-app query events from mcp_activity for debugging."""
    return _logged("live_app_logs", {"slug": slug, "limit": limit},
                   lambda: tool_live_app_logs(slug, limit))


def _mcp_start_live_app(slug, version=None, restart=False, port=None):
    """Start a Python FastAPI live app locally under uvicorn. HTML apps are already hosted."""
    return _logged("start_live_app", {"slug": slug, "version": version, "restart": restart, "port": port},
                   lambda: tool_start_live_app(slug, version, restart, port))


def _mcp_stop_live_app(slug):
    """Stop a locally running Python live app process."""
    return _logged("stop_live_app", {"slug": slug}, lambda: tool_stop_live_app(slug))


def _mcp_live_app_status(slug=None):
    """Inspect local live-app runner state for one app or every running app."""
    return _logged("live_app_status", {"slug": slug}, lambda: tool_live_app_status(slug))


async def _mcp_capture_live_app(slug, path=None, width=1440, height=900, full_page=True, start=True,
                                wait_ms=750, return_image=False):
    """Capture a PNG screenshot of a live app. HTML captures inject the live rvbbitQuery bridge
    and report per-query bridge health (queries run/failed, console + page errors) in the result;
    Python captures auto-start the local runner by default. return_image=true additionally returns
    the PNG itself as image content for direct visual inspection (the saved path is on the MCP
    host, so remote agents should use return_image; keep the viewport modest — a full-page
    1440px capture can be megabytes)."""
    res = await asyncio.to_thread(
        lambda: _logged("capture_live_app", {
            "slug": slug,
            "path": path,
            "width": width,
            "height": height,
            "full_page": full_page,
            "start": start,
            "wait_ms": wait_ms,
            "return_image": return_image,
        }, lambda: tool_capture_live_app(slug, path, width, height, full_page, start, wait_ms))
    )
    if return_image and isinstance(res, dict) and not res.get("error") and res.get("path"):
        try:
            from mcp.server.fastmcp import Image
            return [res, Image(path=res["path"])]
        except Exception as e:  # noqa: BLE001 — the capture itself succeeded; degrade gracefully
            res["image_error"] = str(e)
    return res


_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_template.html")
_TANSTACK_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "examples",
    "tanstack-charts-dashboard.html",
)


def _dashboard_semantic_map_example():
    """Agent-facing example; callers adapt it to the values they actually render."""
    return {
        "semantic_map": {
            "schema_version": _SEMANTIC_MAP_SCHEMA,
            "description": "Exact meanings and independent SQL recreations for visible values.",
            "objects": [{
                "id": "revenue",
                "kind": "scalar",
                "meaning": {
                    "label": "Booked revenue",
                    "description": "Revenue booked across the currently selected channel.",
                    "unit": "USD",
                    "formula": "Sum of revenue_booked after applying the dashboard channel filter.",
                },
                "parameters": {
                    "channel": {
                        "type": "text",
                        "default": "All",
                        "label": "Channel",
                        "source": "#channel-filter",
                    },
                },
                "bindings": [{"selector": "#kpi-revenue"}],
                "evaluator": {
                    "sql": (
                        "select sum(revenue_booked) as value "
                        "from marts.mart_blended_cac_by_channel "
                        "where ({{channel}} = 'All' or channel = {{channel}})"
                    ),
                    "shape": "scalar",
                    "value_column": "value",
                },
                "display": {"prefix": "$", "decimals": 0},
                "source_queries": ["kpi"],
            }],
        },
    }


def _tanstack_chart_semantic_map_example():
    """Repeated chart objects: one definition, exact runtime state per SVG mark."""
    return _normalize_semantic_manifest({
        "schema_version": "live_app.v0",
        "runtime_kind": "html",
        "app_kind": "dashboard",
        "semantic_map": {
            "schema_version": _SEMANTIC_MAP_SCHEMA,
            "description": (
                "Business meanings used by the optional TanStack chart adapter. "
                "Each keyed SVG point binds its own value and dimension context."
            ),
            "objects": [
                {
                    "id": "revenue_by_channel",
                    "kind": "scalar",
                    "meaning": {
                        "label": "Booked revenue by channel",
                        "description": "Revenue booked for the selected attribution channel.",
                        "unit": "USD",
                        "formula": "Sum of revenue_booked for one channel.",
                    },
                    "parameters": {
                        "channel": {"type": "text", "default": "Direct", "label": "Channel"},
                    },
                    "evaluator": {
                        "sql": (
                            "select sum(revenue_booked) as value "
                            "from marts.mart_blended_cac_by_channel "
                            "where channel = {{channel}}"
                        ),
                        "shape": "scalar",
                        "value_column": "value",
                    },
                    "display": {"prefix": "$", "decimals": 0},
                    "source_queries": ["channel"],
                },
                {
                    "id": "roas_by_channel",
                    "kind": "scalar",
                    "meaning": {
                        "label": "Revenue-to-spend ratio by channel",
                        "description": "Booked revenue divided by media spend for one channel.",
                        "unit": "ratio",
                        "formula": "Sum of revenue_booked divided by sum of spend for one channel.",
                    },
                    "parameters": {
                        "channel": {"type": "text", "default": "Direct", "label": "Channel"},
                    },
                    "evaluator": {
                        "sql": (
                            "select round(sum(revenue_booked) / nullif(sum(spend),0), 2) as value "
                            "from marts.mart_blended_cac_by_channel "
                            "where channel = {{channel}}"
                        ),
                        "shape": "scalar",
                        "value_column": "value",
                    },
                    "display": {"suffix": "x", "decimals": 2},
                    "source_queries": ["channel"],
                },
                {
                    "id": "monthly_revenue",
                    "kind": "scalar",
                    "meaning": {
                        "label": "Monthly booked revenue",
                        "description": "Revenue booked in the selected calendar month.",
                        "unit": "USD",
                        "formula": "Sum of revenue_booked for one calendar month.",
                    },
                    "parameters": {
                        "ym": {"type": "text", "default": "2026-01", "label": "Month"},
                    },
                    "evaluator": {
                        "sql": (
                            "select sum(revenue_booked) as value "
                            "from marts.mart_blended_cac_by_channel "
                            "where to_char(date_trunc('month',date_day),'YYYY-MM') = {{ym}}"
                        ),
                        "shape": "scalar",
                        "value_column": "value",
                    },
                    "display": {"prefix": "$", "decimals": 0},
                    "source_queries": ["trend"],
                },
                {
                    "id": "monthly_spend",
                    "kind": "scalar",
                    "meaning": {
                        "label": "Monthly media spend",
                        "description": "Media spend in the selected calendar month.",
                        "unit": "USD",
                        "formula": "Sum of spend for one calendar month.",
                    },
                    "parameters": {
                        "ym": {"type": "text", "default": "2026-01", "label": "Month"},
                    },
                    "evaluator": {
                        "sql": (
                            "select sum(spend) as value "
                            "from marts.mart_blended_cac_by_channel "
                            "where to_char(date_trunc('month',date_day),'YYYY-MM') = {{ym}}"
                        ),
                        "shape": "scalar",
                        "value_column": "value",
                    },
                    "display": {"prefix": "$", "decimals": 0},
                    "source_queries": ["trend"],
                },
            ],
        },
    })


def tool_dashboard_template():
    try:
        with open(_TEMPLATE_PATH) as f:
            html = f.read()
    except Exception as e:   # noqa: BLE001
        return {"error": str(e)}
    return {
        "template_html": html,
        "semantic_map_example": _dashboard_semantic_map_example(),
        "how_to_use": [
            "Set SERVER_ID to the <id> in your `mcp__<id>__run_sql` tool name.",
            "Give composePayload() one FLAT sub-SELECT per data concern — it batches them into ONE "
            "run_sql_multi round trip (each callMcpTool adds ~1.5s host overhead, so ONE call — but "
            "each query stays flat/inspectable on the wire; never hand-write a json_build_object "
            "payload query).",
            "Edit only the two `>>> EDIT` blocks (CONFIG: title + composePayload map; RENDER: KPIs / "
            "chart() / table()). Leave everything between the FRAMEWORK markers as-is.",
            "Publish normally: RVBBIT renders the immutable version and compiles a verified semantic "
            "overlay from its DOM, filters, query traces, source, and screenshot.",
            "Optional: use stable DOM ids plus bindBusinessObject and adapt semantic_map_example when "
            "you already know a particularly precise business definition. Authored objects win; the "
            "compiler fills remaining gaps.",
            "Live data is the Cowork callMcpTool→run_sql bridge (authed by the connector you already "
            "granted — no fetch, no login); it falls back to the host's rvbbitQuery when published.",
            "SQL gotchas (rvbbit read-only guard): no `::type` casts (use `cast(x as t)` or bare "
            "json_agg/row_to_json); no reserved-word bare aliases (use `ym`, not `month`).",
            "Sandbox CDN allowlist only: Chart.js 4.5.0, Grid.js 5.0.2 (+ theme css), Mermaid 11.10.0. "
            "Anything else is silently blocked.",
        ],
    }


def tool_tanstack_chart_template():
    """Return the optional TanStack Charts experiment without changing the default template."""
    try:
        with open(_TANSTACK_TEMPLATE_PATH, encoding="utf-8") as file:
            html = file.read()
    except Exception as error:  # noqa: BLE001
        return {"error": str(error)}
    manifest = _tanstack_chart_semantic_map_example()
    return {
        "status": "experimental",
        "tanstack_charts_version": "0.3.1",
        "runtime_asset": "/charts/rvbbit-tanstack-charts-0.3.1.js",
        "template_html": html,
        "manifest": manifest,
        "how_to_use": [
            "This is opt-in. The default dashboard_template and live_app_template remain on "
            "Chart.js 4.5.0 and arbitrary HTML/JS; do not migrate an existing artifact unless asked.",
            "Keep the exact versioned /charts/rvbbit-tanstack-charts-0.3.1.js script tag. The "
            "Warehouse self-hosts it, and capture/PDF/semantic renders inline that same asset.",
            "Author native TanStack definitions with window.RVBBIT_CHARTS marks, scales, tooltip, "
            "transforms, and createMark escape hatches. mountRvbbitChart only owns lifecycle plus "
            "RVBBIT metadata; it does not replace the TanStack grammar.",
            "Give every mark a stable id and pass the tiny metadata map (query, x/y fields, value, "
            "context fields, semanticObject). Keyed SVG marks then become exact Artifact Lens targets.",
            "Pass the returned manifest to create_live_app so repeated SVG marks bind to replayable "
            "business definitions. Omit it only when the post-publication compiler should infer them.",
            "Call capture_live_app after publication and inspect bridge.page_errors plus the image. "
            "TanStack Charts is pre-alpha, so keep this runtime pinned while evaluating it.",
        ],
    }


def _mcp_dashboard_template():
    """Return the proven drop-in boilerplate for a LIVE dashboard (Cowork artifact + hosted).
    ALWAYS start a dashboard from this — it has the data bridge, single-round-trip query
    pattern (composePayload), semantic-object binding helper, formatters, and chart/table
    wrappers already solved. Adapt its two `>>> EDIT` blocks and publish normally; the semantic
    compiler runs after publication. semantic_map_example remains an optional precision hint."""
    return _logged("dashboard_template", {}, tool_dashboard_template)


def _mcp_tanstack_chart_template():
    """Get RVBBIT's EXPERIMENTAL, opt-in TanStack Charts starter. It preserves the normal
    standalone HTML/JS dashboard model while replacing only selected visualization surfaces with
    native, responsive, keyed SVG chart definitions. The default Chart.js template is unchanged.
    Use this tool only when the user explicitly wants to try TanStack Charts or the semantic SVG
    primitive; publish with create_live_app and the returned manifest, then capture it."""
    return _logged("tanstack_chart_template", {}, tool_tanstack_chart_template)


def _mcp_calliope_work_item(
    session_id,
    kind,
    title,
    summary="",
    urgency="normal",
    action_prompt=None,
    context=None,
    dedupe_key=None,
    due_at=None,
    source_ref=None,
):
    """Publish a meaningful handoff to the originating user's Calliope Work Inbox.

    `session_id` must be the exact Calliope session UUID supplied in the current
    turn's internal work-routing context; it resolves the owner, so this tool
    intentionally accepts no email or recipient. Use `kind` suggestion,
    scheduled, goal, blocked, or result after creating scheduled/persistent
    work, reaching a useful async result, becoming blocked, or finding a truly
    useful proactive suggestion. Do not publish routine tool progress. For a
    Hermes cron job or persistent goal, copy the same session UUID and a future
    call to this tool into the saved job prompt. Reuse `dedupe_key` when later
    updates should reopen or replace the same logical item.
    """
    args = {
        "session_id": session_id,
        "kind": kind,
        "title": title,
        "summary": summary,
        "urgency": urgency,
        "action_prompt": action_prompt,
        "context": context,
        "dedupe_key": dedupe_key,
        "due_at": due_at,
        "source_ref": source_ref,
    }

    def publish():
        import calliope

        if not calliope.is_enabled():
            raise RuntimeError("Calliope is not configured on this Warehouse")
        return {"item": calliope.publish_work_item(
            _conn,
            session_id,
            kind,
            title,
            summary,
            urgency,
            action_prompt,
            context,
            dedupe_key,
            due_at,
            source_ref=source_ref,
        )}

    return _logged("calliope_work_item", args, publish)


def _mcp_search_calliope_actions(
    session_id: str,
    query: str,
    category: str | None = None,
    limit: int = 12,
):
    """Search outcome-oriented actions available to a Calliope user.

    Use the exact `session_id` from Calliope's internal work-routing context;
    it resolves the owning human and current runtime state. Search in ordinary
    language such as "connect Linear", "add tickets to company knowledge", or
    "monitor a metric". Results distinguish ready, connect/install, and blocked
    actions and include structured question fields. For typed changes, call
    `plan_calliope_action` next. Never ask the user to put a secret in chat.
    """
    args = {
        "session_id": session_id,
        "query": query,
        "category": category,
        "limit": limit,
    }

    def search():
        import calliope

        if not calliope.is_enabled():
            raise RuntimeError("Calliope is not configured on this Warehouse")
        return calliope.search_actions_for_session(
            _conn, session_id, query, category, limit
        )

    return _logged("search_calliope_actions", args, search)


def _mcp_plan_calliope_action(
    session_id: str,
    action_id: str,
    inputs: dict | None = None,
):
    """Create a durable, redacted plan for one typed Calliope action.

    Use an exact action id returned by `search_calliope_actions` and the exact
    originating Calliope session id. The plan records human-readable steps,
    change scope, verification, and rollback guidance, but does not apply the
    change. Explain it and wait for explicit user approval before calling
    `execute_calliope_action`. Secret-required MCP connections are deliberately
    completed only in the native Library form; never request or pass secrets.
    """
    args = {"session_id": session_id, "action_id": action_id, "inputs": inputs}

    def plan():
        import calliope

        if not calliope.is_enabled():
            raise RuntimeError("Calliope is not configured on this Warehouse")
        return calliope.plan_action_for_session(
            _conn, session_id, action_id, inputs
        )

    return _logged("plan_calliope_action", args, plan)


def _mcp_execute_calliope_action(session_id: str, run_id: str):
    """Apply one explicitly approved, previously planned Calliope action.

    This accepts only the opaque plan UUID returned by `plan_calliope_action`;
    the action id and non-secret inputs are frozen in that plan. It runs typed
    SQL operations, verification, and a durable receipt. If the action needs a
    credential, the result directs the user to the secure native Library flow
    instead of accepting a secret through the model.
    """
    args = {"session_id": session_id, "run_id": run_id}

    def execute():
        import calliope

        if not calliope.is_enabled():
            raise RuntimeError("Calliope is not configured on this Warehouse")
        return calliope.execute_action_for_session(_conn, session_id, run_id)

    return _logged("execute_calliope_action", args, execute)


# Warehouse currently ships a FastMCP release that inspects raw annotations
# while discovering Context parameters.  This module postpones annotations, so
# expose concrete runtime types for directly registered typed tools (matching
# the Workflow helpers below) instead of leaving strings for FastMCP to inspect.
_mcp_search_calliope_actions.__annotations__ = {
    "session_id": str,
    "query": str,
    "category": str | None,
    "limit": int,
}
_mcp_plan_calliope_action.__annotations__ = {
    "session_id": str,
    "action_id": str,
    "inputs": dict | None,
}
_mcp_execute_calliope_action.__annotations__ = {
    "session_id": str,
    "run_id": str,
}


def _mcp_draft_calliope_instrument(
    session_id,
    name,
    description,
    prompt_template,
    fields,
    slug=None,
    revision_notes=None,
):
    """Draft or revise a safe declarative UI skill in the user's Instrument library.

    Use this only after a user asks to make a repeatable workflow reusable, or a
    proactive task has strong evidence that a small form would remove repeated
    work. `session_id` must be the exact originating Calliope session UUID from
    the internal work-routing context; it resolves ownership and this tool
    accepts no recipient. Fields are declarative objects using text, textarea,
    number, select, boolean, or date. `prompt_template` may reference them as
    `{{field_key}}`. No HTML, JavaScript, SQL execution, or auto-publication is
    accepted here. The result is a private draft revision: its human owner must
    explicitly publish it privately or share it with the company in Calliope.
    Reuse `slug` to create the next immutable revision of an existing Instrument.
    """
    args = {
        "session_id": session_id,
        "name": name,
        "description": description,
        "prompt_template": prompt_template,
        "fields": fields,
        "slug": slug,
        "revision_notes": revision_notes,
    }

    def draft():
        import calliope

        if not calliope.is_enabled():
            raise RuntimeError("Calliope is not configured on this Warehouse")
        return {"instrument": calliope.draft_instrument(
            _conn,
            session_id,
            name,
            description,
            prompt_template,
            fields,
            slug,
            revision_notes,
        )}

    return _logged("draft_calliope_instrument", args, draft)


def _mcp_draft_calliope_workflow(
    session_id,
    name,
    description,
    goal,
    trigger=None,
    contexts=None,
    outputs=None,
    slug=None,
    revision_notes=None,
    decision_rules=None,
    requirements=None,
):
    """Draft or revise a bounded, agent-driven Calliope Workflow.

    Use this after the user wants repeatable work to run headlessly or on a
    schedule. `session_id` is the exact originating Calliope session UUID and
    resolves ownership. A Workflow has one manual/schedule trigger, up to eight
    governed context nodes (`artifact`, `semantic_object`, `evidence`,
    `knowledge`, or `instruction`), one agent goal with optional decision rules,
    and `stage`, `work_inbox`, or `artifact` outputs. Hermes dynamically chooses
    tools at run time; do not encode SQL, JavaScript, shell commands, or low-level
    tool nodes. Optional `requirements` name preflightable runtime dependencies:
    `personal_context`, `project_ticket`, `warehouse`, `mcp:<server>`,
    `brain:<provider>`, or `capability:<id>`. Scheduled triggers use Hermes syntax (`every 30m`, `every 2h`, a
    five-field cron expression, or an ISO timestamp) in the Hermes installation
    timezone; per-Workflow timezone overrides are not supported. The result is
    always a private immutable draft revision. A human
    must review and publish it before any schedule can be enabled. Reuse `slug`
    to create the next revision.
    """
    args = {
        "session_id": session_id,
        "name": name,
        "description": description,
        "goal": goal,
        "trigger": trigger,
        "contexts": contexts,
        "outputs": outputs,
        "slug": slug,
        "revision_notes": revision_notes,
        "decision_rules": decision_rules,
        "requirements": requirements,
    }

    def draft():
        import calliope

        if not calliope.is_enabled():
            raise RuntimeError("Calliope is not configured on this Warehouse")
        return {"workflow": calliope.draft_workflow(
            _conn,
            session_id,
            name,
            description,
            goal,
            trigger,
            contexts,
            outputs,
            slug,
            revision_notes,
            decision_rules,
            requirements,
        )}

    return _logged("draft_calliope_workflow", args, draft)


def _mcp_begin_calliope_workflow_run(workflow_id, source_session_id, version):
    """Begin one approved headless Calliope Workflow run.

    This lifecycle tool is for the Hermes schedule prompt produced by Calliope.
    Pass the exact workflow UUID, originating Calliope session UUID, and approved
    version embedded in that prompt. It validates the publication pointer,
    creates a fresh paired run notebook, freezes the graph and governed context,
    and returns the sole execution contract. Do not improvise IDs or call it for
    an unpublished draft. Finish every successful begin with exactly one
    `finish_calliope_workflow_run` call.
    """
    args = {
        "workflow_id": workflow_id,
        "source_session_id": source_session_id,
        "version": version,
    }

    def begin():
        import calliope

        if not calliope.is_enabled():
            raise RuntimeError("Calliope is not configured on this Warehouse")
        return calliope.begin_workflow_run(
            _conn,
            workflow_id,
            source_session_id,
            version,
            trigger_kind="scheduled",
        )

    return _logged("begin_calliope_workflow_run", args, begin)


def _mcp_get_calliope_personal_context(
    run_id: str,
    include_resolved: bool = True,
    limit: int = 30,
):
    """Read the Workflow owner's latest private Brief, notes, and Work Inbox.

    The exact Workflow run UUID is an opaque capability: ownership is resolved
    server-side and there is intentionally no email or user parameter. Use this
    only when the frozen Workflow goal requires the owner's personal Calliope
    context. Resolved Inbox items may be included to avoid duplicate follow-up.
    """
    args = {
        "run_id": run_id,
        "include_resolved": include_resolved,
        "limit": limit,
    }

    def resolve():
        import calliope

        if not calliope.is_enabled():
            raise RuntimeError("Calliope is not configured on this Warehouse")
        return calliope.workflow_personal_context(
            _conn,
            run_id,
            include_resolved,
            limit,
        )

    return _logged("get_calliope_personal_context", args, resolve)


_mcp_get_calliope_personal_context.__annotations__ = {
    "run_id": str,
    "include_resolved": bool,
    "limit": int,
}


def _mcp_finish_calliope_workflow_run(
    run_id: str,
    status: str,
    summary: str,
    details: dict | list | None = None,
    artifacts: list[dict | str] | None = None,
    action_prompt: str | None = None,
):
    """Finish one Calliope Workflow run and publish its durable result.

    Call exactly once after a successful `begin_calliope_workflow_run`, or when
    a manually opened Workflow supplies an exact run UUID. Status is `complete`,
    `blocked`, or `failed`. Include concise evidence-backed details and exact
    artifact refs/versions where applicable; unverified artifact claims remain
    visibly unverified. This closes the run notebook, adds result surfaces,
    records caller-attributed cost state, and publishes the user's Work Inbox
    result or blocker. It is idempotent for an already-finished run.
    """
    args = {
        "run_id": run_id,
        "status": status,
        "summary": summary,
        "details": details,
        "artifacts": artifacts,
        "action_prompt": action_prompt,
    }

    def finish():
        import calliope

        if not calliope.is_enabled():
            raise RuntimeError("Calliope is not configured on this Warehouse")
        return calliope.finish_workflow_run(
            _conn,
            run_id,
            status,
            summary,
            details,
            artifacts,
            action_prompt,
        )

    return _logged("finish_calliope_workflow_run", args, finish)


# This module uses postponed annotations, while the FastMCP version deployed by
# Warehouse inspects ``__annotations__`` directly instead of resolving them.
# Give it concrete runtime types so the emitted JSON schema preserves objects
# and arrays rather than falling back to strings.
_mcp_finish_calliope_workflow_run.__annotations__ = {
    "run_id": str,
    "status": str,
    "summary": str,
    "details": dict | list | None,
    "artifacts": list[dict | str] | None,
    "action_prompt": str | None,
}


# Data clients injected into every served dashboard. We provide BOTH the hosted
# rvbbitQuery AND a cowork.callMcpTool shim (routing to the same read-only endpoint), so a
# Cowork-built artifact (callMcpTool) and a hosted-built one (rvbbitQuery) both run here
# unchanged — no codemod of the artifact needed.
_DASH_SHIM = (
    '<link rel="icon" href="/theme/datarabbit.svg" type="image/svg+xml">\n'
    '<link rel="preload" href="/theme/artifact-lens.css" as="style">\n'
    "<script>\n"
    "window.RVBBIT_DASHBOARD={slug:__SLUG__,version:__VERSION__,calliope_enabled:__CALLIOPE_ENABLED__};"
    "window.RVBBIT_DASHBOARD.manifest=__MANIFEST__;"
    "(()=>{const root=window.RVBBIT_DASHBOARD;"
    "let semantic=(root.manifest&&root.manifest.semantic_map)||{};"
    "let defs=Array.isArray(semantic.objects)?semantic.objects:[];const runtime=new Map();"
    "const elementRuntime=new WeakMap();"
    "const find=(id)=>defs.find((item)=>item&&item.id===String(id||''))||null;"
    "const nodes=(binding)=>{const found=[];try{"
    "if(binding&&binding.selector)found.push(...document.querySelectorAll(binding.selector));"
    "if(binding&&binding.element_id){const node=document.getElementById(binding.element_id);if(node)found.push(node);}"
    "if(binding&&binding.name)found.push(...document.getElementsByName(binding.name));"
    "}catch(_error){}return[...new Set(found)];};"
    "const attach=()=>{defs.forEach((item)=>(item.bindings||[]).forEach((binding)=>"
    "nodes(binding).forEach((node)=>node.setAttribute('data-rvbbit-object',item.id))));"
    "window.dispatchEvent(new CustomEvent('rvbbit:semantic-map-ready',{detail:{count:defs.length}}));};"
    "root.semanticMap=()=>semantic;"
    "root.replaceSemanticManifest=(manifest)=>{root.manifest=manifest||{};"
    "semantic=(root.manifest&&root.manifest.semantic_map)||{};"
    "defs=Array.isArray(semantic.objects)?semantic.objects:[];runtime.clear();attach();return defs.length;};"
    "root.semanticObjects=()=>defs.map((item)=>({...item,runtime:runtime.get(item.id)||null}));"
    "root.semanticObject=(id,target)=>{const item=find(id);"
    "const node=target&&target.nodeType===1?target:null;"
    "return item?({...item,runtime:(node&&elementRuntime.get(node))||runtime.get(item.id)||null}):null;};"
    "root.bindSemanticObject=(id,target,state)=>{const item=find(id);if(!item)return null;"
    "const next=(state&&typeof state==='object'&&!Array.isArray(state))?state:{value:state};"
    "let selected=[];if(target&&target.nodeType===1)selected=[target];"
    "else if(typeof target==='string'){try{selected=[...document.querySelectorAll(target)];}catch(_error){selected=[];}}"
    "if(!selected.length)(item.bindings||[]).forEach((binding)=>selected.push(...nodes(binding)));"
    "selected=[...new Set(selected)];"
    "const snapshot={value:next.value,"
    "context:(next.context&&typeof next.context==='object')?next.context:{},"
    "rendered_at:new Date().toISOString(),selector:typeof target==='string'?target:null};"
    "runtime.set(item.id,snapshot);"
    "selected.forEach((node)=>{node.setAttribute('data-rvbbit-object',item.id);"
    "elementRuntime.set(node,snapshot);});"
    "window.dispatchEvent(new CustomEvent('rvbbit:semantic-object',{detail:{id:item.id}}));"
    "return root.semanticObject(item.id,selected[0]);};"
    "if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',attach,{once:true});"
    "else queueMicrotask(attach);})();"
    "(()=>{const v=new URLSearchParams(location.search).get('rvbbit_as_of');"
    "if(v)window.RVBBIT_DASHBOARD.as_of=v;"
    "const trace=[];let sequence=0;"
    "const hash=(value)=>{let h=2166136261;for(let i=0;i<value.length;i++){"
    "h^=value.charCodeAt(i);h=Math.imul(h,16777619);}return(h>>>0).toString(16).padStart(8,'0');};"
    "window.RVBBIT_DASHBOARD.queryTrace=()=>trace.map((entry)=>({...entry,"
    "columns:[...(entry.columns||[])],rows:[...(entry.rows||[])]}));"
    "window.RVBBIT_DASHBOARD.clearQueryTrace=()=>{trace.length=0;};"
    "window.RVBBIT_DASHBOARD._recordQueryTrace=(sql,asOf,result,error)=>{"
    "const text=String(sql||'');const entry={id:'q-'+Date.now().toString(36)+'-'+(++sequence).toString(36),"
    "query_hash:hash(text.replace(/\\s+/g,' ').trim().toLowerCase()),sql:text,as_of:asOf||null,"
    "columns:Array.isArray(result&&result.columns)?result.columns.slice(0,160):[],"
    "rows:Array.isArray(result&&result.rows)?result.rows.slice(0,200):[],"
    "row_count:Number(result&&result.row_count)||0,truncated:Boolean(result&&result.truncated),"
    "engine:(result&&result.engine)||null,elapsed_ms:Number(result&&result.elapsed_ms)||null,"
    "error:error?String(error.message||error).slice(0,500):null,at:new Date().toISOString()};"
    "trace.unshift(entry);if(trace.length>24)trace.length=24;"
    "window.dispatchEvent(new CustomEvent('rvbbit:query-trace',{detail:entry}));return entry;};})();\n"
    "window.rvbbitQuery=async function(sql,opts){opts=opts||{};"
    "const asOf=opts.as_of||window.RVBBIT_DASHBOARD.as_of||null;"
    "try{const r=await fetch('/api/d/'+__SLUG__+'/q',{method:'POST',headers:{'content-type':'application/json'},"
    "body:JSON.stringify({sql:sql,as_of:asOf})});const d=await r.json();"
    "if(!r.ok||d.error){throw new Error((d.error&&d.error.message)||('query failed '+r.status));}"
    "window.RVBBIT_DASHBOARD._recordQueryTrace(sql,asOf,d,null);return d;"
    "}catch(error){window.RVBBIT_DASHBOARD._recordQueryTrace(sql,asOf,null,error);throw error;}};\n"
    "window.cowork=window.cowork||{};"
    "if(!window.cowork.callMcpTool){window.cowork.callMcpTool=async function(tool,args){"
    "args=args||{};if(String(tool||'').endsWith('run_sql_multi')){"
    "const pairs=Object.entries(args.queries||{});const results={};"
    "for(const [name,sql] of pairs){results[name]=await window.rvbbitQuery(sql,{as_of:args.as_of||null});}"
    "return{structuredContent:{results:results}};}"
    "const d=await window.rvbbitQuery(args.sql||'',{as_of:args.as_of||null});"
    "return{structuredContent:{...d,rows:(d&&d.rows)||[]}};};}\n"
    "</script>\n"
    '<script src="/theme/artifact-lens.js" defer></script>\n')


def _script_json(value):
    return (
        json.dumps(value or {}, default=str, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _dash_shim(slug, version=None, manifest=None):
    try:
        import calliope
        calliope_enabled = calliope.is_enabled()
    except Exception:  # noqa: BLE001 — the Lens simply omits its optional handoff
        calliope_enabled = False
    if version is not None:
        try:
            with _conn() as c:
                row = c.execute(
                    "SELECT d.id,v.manifest FROM rvbbit.dashboards d "
                    "JOIN rvbbit.dashboard_versions v ON v.dashboard_id=d.id "
                    "WHERE d.slug=%s AND v.version=%s",
                    (slug, int(version)),
                ).fetchone()
            if row:
                manifest = _effective_artifact_manifest(
                    row["id"], int(version), manifest if manifest is not None else row["manifest"]
                )
        except Exception as exc:  # noqa: BLE001 — serving the artifact always wins
            print(f"WARNING: semantic overlay unavailable for {slug}@v{version}: {exc}", file=sys.stderr)
    return (
        _DASH_SHIM
        .replace("__SLUG__", json.dumps(slug))
        .replace("__VERSION__", json.dumps(int(version)) if version is not None else "null")
        .replace("__MANIFEST__", _script_json(manifest))
        .replace("__CALLIOPE_ENABLED__", "true" if calliope_enabled else "false")
    )


def _iso_utc(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return _normalize_as_of(value)


def _summarize_dashboard_time_travel(rows, point_rows, version):
    """Turn dependency/generation metadata into the small public lens contract."""
    base = {
        "eligible": False,
        "version": int(version),
        "table_count": len(rows),
        "points": [],
    }
    if not rows:
        return base | {
            "code": "NO_QUERY_SOURCES",
            "message": "No live SQL sources have been discovered for this artifact yet.",
        }
    unmanaged = [row for row in rows if not row.get("rvbbit_managed")]
    if unmanaged:
        return base | {
            "code": "PARTIAL_COVERAGE",
            "message": (
                "Data time is available only when every dashboard source is "
                "an RVBBIT-managed table or cube."
            ),
            "unsupported_count": len(unmanaged),
        }
    without_history = [row for row in rows if int(row.get("generations") or 0) < 1]
    if without_history:
        return base | {
            "code": "NO_RETAINED_HISTORY",
            "message": "One or more dashboard sources have no retained generations yet.",
            "unsupported_count": len(without_history),
        }

    common_earliest = max(row["earliest"] for row in rows if row.get("earliest"))
    latest_refresh = max(row["latest"] for row in rows if row.get("latest"))
    raw_points = [
        row.get("committed_at") if isinstance(row, dict) else row
        for row in point_rows
    ]
    points = sorted({
        point for point in raw_points
        if point is not None and point >= common_earliest
    })
    if len(points) < 2:
        return base | {
            "code": "ONE_RETAINED_POINT",
            "message": "This dashboard does not have two common retained data points yet.",
            "earliest": _iso_utc(common_earliest),
            "latest_refresh": _iso_utc(latest_refresh),
        }
    return base | {
        "eligible": True,
        "code": "READY",
        "message": "Every discovered source supports RVBBIT data-time travel.",
        "earliest": _iso_utc(common_earliest),
        "latest_refresh": _iso_utc(latest_refresh),
        "points": [_iso_utc(point) for point in points],
        "point_count": len(points),
    }


def _dashboard_time_travel(slug, version=None):
    """Discover one published artifact's common RVBBIT generation timeline.

    Dependency rows are the fast path.  For dynamically-authored SQL that was
    not visible during publication, recent dashboard-query telemetry supplies
    a read-only fallback after the page has run once.
    """
    _ensure_dashboard_tables()
    _ensure_activity_table()
    with _conn() as c:
        dashboard = c.execute(
            "SELECT id, latest_version, runtime_kind FROM rvbbit.dashboards WHERE slug=%s",
            (slug,),
        ).fetchone()
        if not dashboard:
            return {
                "eligible": False,
                "code": "NOT_FOUND",
                "message": "No such published artifact.",
                "points": [],
            }
        selected_version = int(version or dashboard["latest_version"])
        if selected_version < 1:
            raise ValueError("version must be a positive integer")
        refs = [
            row["object_ref"]
            for row in c.execute(
                "SELECT DISTINCT object_ref FROM rvbbit.dashboard_deps "
                "WHERE dashboard_id=%s AND version=%s AND kind='table' "
                "AND object_ref IS NOT NULL ORDER BY object_ref",
                (dashboard["id"], selected_version),
            ).fetchall()
        ]
        runtime_sql = []
        if not refs and selected_version == int(dashboard["latest_version"]):
            runtime_sql = [
                row["sql"]
                for row in c.execute(
                    f"SELECT DISTINCT args->>'sql' AS sql FROM {ACTIVITY_TABLE} "
                    "WHERE tool='dashboard_query' AND args->>'dashboard'=%s "
                    "AND args->>'sql' IS NOT NULL ORDER BY sql LIMIT 24",
                    (slug,),
                ).fetchall()
            ]

    if not refs:
        refs = sorted({
            relation
            for query in runtime_sql
            for relation in _referenced_tables(query)
        })
    if not refs:
        return _summarize_dashboard_time_travel([], [], selected_version)

    coverage_sql = """
        WITH requested AS (
          SELECT ref, to_regclass(ref)::oid AS table_oid
          FROM unnest(%s::text[]) AS requested_refs(ref)
        )
        SELECT requested.ref,
               requested.table_oid,
               (managed.table_oid IS NOT NULL) AS rvbbit_managed,
               count(generation.generation)::int AS generations,
               min(generation.committed_at) AS earliest,
               max(generation.committed_at) AS latest
        FROM requested
        LEFT JOIN rvbbit.tables managed ON managed.table_oid=requested.table_oid
        LEFT JOIN rvbbit.generations generation ON generation.table_oid=managed.table_oid
        GROUP BY requested.ref, requested.table_oid, managed.table_oid
        ORDER BY requested.ref
    """
    with _conn() as c:
        coverage = c.execute(coverage_sql, (refs,)).fetchall()
    if (
        not coverage
        or any(not row.get("rvbbit_managed") for row in coverage)
        or any(int(row.get("generations") or 0) < 1 for row in coverage)
    ):
        return _summarize_dashboard_time_travel(coverage, [], selected_version)

    common_earliest = max(row["earliest"] for row in coverage)
    points_sql = """
        WITH requested AS (
          SELECT to_regclass(ref)::oid AS table_oid
          FROM unnest(%s::text[]) AS requested_refs(ref)
        )
        SELECT DISTINCT generation.committed_at
        FROM requested
        JOIN rvbbit.generations generation ON generation.table_oid=requested.table_oid
        WHERE generation.committed_at >= %s
        ORDER BY generation.committed_at DESC
        LIMIT 240
    """
    with _conn() as c:
        points = c.execute(points_sql, (refs, common_earliest)).fetchall()
    return _summarize_dashboard_time_travel(coverage, points, selected_version)


_INSPECTION_SENSITIVE_RE = re.compile(
    r"(?:secret|token|password|passwd|auth|cookie|session|api[-_]?key)",
    re.I,
)


def _inspection_text(value, limit):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", str(value or "")).strip()[:limit]


def _inspection_number(value, minimum=-1_000_000, maximum=1_000_000):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (minimum <= number <= maximum):
        return None
    return round(number, 3)


def _sanitize_inspection_target(value):
    """Bound browser DOM evidence before it can become durable investigation context."""
    if not isinstance(value, dict):
        raise ValueError("target must be an object")
    bounds = value.get("bounds")
    viewport = value.get("viewport")
    if not isinstance(bounds, dict) or not isinstance(viewport, dict):
        raise ValueError("target needs bounds and viewport")
    clean_bounds = {
        key: _inspection_number(bounds.get(key), 0, 100_000)
        for key in ("x", "y", "width", "height")
    }
    clean_viewport = {
        key: _inspection_number(viewport.get(key), 0, 100_000)
        for key in ("width", "height", "scroll_x", "scroll_y", "document_width", "document_height")
        if viewport.get(key) is not None
    }
    if (
        clean_bounds["width"] is None
        or clean_bounds["height"] is None
        or clean_bounds["width"] < 1
        or clean_bounds["height"] < 1
        or clean_viewport.get("width") is None
        or clean_viewport.get("height") is None
    ):
        raise ValueError("target must describe one visible dashboard object")
    target = {
        "label": _inspection_text(value.get("label") or "Selected target", 400),
        "selector": _inspection_text(value.get("selector"), 800),
        "tag": _inspection_text(value.get("tag"), 80),
        "role": _inspection_text(value.get("role"), 80),
        "text": _inspection_text(value.get("text"), 600),
        "bounds": clean_bounds,
        "viewport": clean_viewport,
    }
    raw_data = value.get("data")
    if isinstance(raw_data, dict):
        safe_data = {}
        for raw_key, raw_value in list(raw_data.items())[:20]:
            key = re.sub(r"[^a-zA-Z0-9_.:-]", "", str(raw_key))[:64]
            if key and not _INSPECTION_SENSITIVE_RE.search(key):
                safe_data[key] = _inspection_text(raw_value, 240)
        if safe_data:
            target["data"] = safe_data
    for nested, limits in (
        ("table", {"row_index": 100_000, "column_index": 10_000}),
        ("chart", {"dataset_index": 10_000, "data_index": 100_000}),
    ):
        raw = value.get(nested)
        if not isinstance(raw, dict):
            continue
        cleaned = {
            key: int(number)
            for key, maximum in limits.items()
            if raw.get(key) is not None
            if (number := _inspection_number(raw.get(key), 0, maximum)) is not None
        }
        for key in (
            "column_header",
            "cell_text",
            "dataset_label",
            "data_label",
            "value",
        ):
            if raw.get(key) is not None:
                cleaned[key] = _inspection_text(raw.get(key), 400)
        target[nested] = cleaned
    raw_visual = value.get("visual")
    if isinstance(raw_visual, dict):
        visual = {}
        for key, maximum in (("row_index", 100_000), ("indexed_mark_count", 100_000)):
            if raw_visual.get(key) is not None:
                number = _inspection_number(raw_visual.get(key), 0, maximum)
                if number is not None:
                    visual[key] = int(number)
        for key in ("mark_tag", "mark_text", "container_label"):
            if raw_visual.get(key) is not None:
                visual[key] = _inspection_text(raw_visual.get(key), 240)
        values = raw_visual.get("text_values")
        if isinstance(values, list):
            visual["text_values"] = [
                _inspection_text(item, 120)
                for item in values[:160]
                if _inspection_text(item, 120)
            ]
        raw_visual_data = raw_visual.get("data")
        if isinstance(raw_visual_data, dict):
            visual["data"] = {
                key: _inspection_text(item, 240)
                for raw_key, item in list(raw_visual_data.items())[:20]
                if (key := re.sub(r"[^a-zA-Z0-9_.:-]", "", str(raw_key))[:64])
                and not _INSPECTION_SENSITIVE_RE.search(key)
            }
        target["visual"] = visual
    return target


def _sanitize_inspection_binding(value):
    if not isinstance(value, dict):
        return {"kind": "element", "confidence": "visual"}
    kind = str(value.get("kind") or "element").lower()
    if kind not in {"chart", "table", "value", "element"}:
        kind = "element"
    confidence = str(value.get("confidence") or "visual").lower()
    if confidence not in {"semantic", "exact", "likely", "visual"}:
        confidence = "visual"
    binding = {
        "kind": kind,
        "confidence": confidence,
        "field": _inspection_text(value.get("field"), 160),
        "label": _inspection_text(value.get("label"), 300),
        "value": _inspection_text(value.get("value"), 400),
    }
    for key in ("trace_row_index", "row_index", "column_index", "dataset_index", "data_index"):
        if value.get(key) is not None:
            number = _inspection_number(value.get(key), 0, 100_000)
            if number is not None:
                binding[key] = int(number)
    raw_row = value.get("row")
    if isinstance(raw_row, dict):
        binding["row"] = {
            _inspection_text(key, 160): _inspection_text(raw_value, 500)
            for key, raw_value in list(raw_row.items())[:80]
            if _inspection_text(key, 160)
        }
    return binding


def _sanitize_semantic_selection(value):
    if not isinstance(value, dict):
        return {}
    object_id = str(value.get("id") or "").strip()
    if not object_id:
        return {}
    if not _SEMANTIC_OBJECT_ID_RE.fullmatch(object_id):
        raise ValueError("semantic selection has an invalid object id")
    context = _semantic_json_value(value.get("context") or {})
    if not isinstance(context, dict):
        context = {}
    rendered_value = _semantic_json_value(value.get("rendered_value"))
    return {
        "id": object_id,
        "definition_hash": _inspection_text(value.get("definition_hash"), 80),
        "context": context,
        "rendered_value": rendered_value,
    }


def _bound_inspection_trace(binding, trace):
    """Only keep query provenance when the selected object actually matched it."""
    if not isinstance(trace, dict):
        return {}
    if binding.get("confidence") not in {"semantic", "exact", "likely"}:
        return {}
    return trace


def _semantic_object_from_manifest(manifest, selection):
    semantic_map = (manifest or {}).get("semantic_map") or {}
    for semantic_object in semantic_map.get("objects") or []:
        if semantic_object.get("id") != selection.get("id"):
            continue
        selected_hash = selection.get("definition_hash")
        if selected_hash and selected_hash != semantic_object.get("definition_hash"):
            raise ValueError(
                f"semantic object {selection['id']} changed; reload the artifact before inspecting it"
            )
        return semantic_object
    return None


def _semantic_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        return number if math.isfinite(number) else None
    raw = str(value).strip().lower()
    negative = raw.startswith("(") and raw.endswith(")")
    multiplier = 1.0
    if re.search(r"[kmbt]\s*(?:%|x|×)?$", raw):
        suffix = re.search(r"([kmbt])\s*(?:%|x|×)?$", raw).group(1)
        multiplier = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[suffix]
    cleaned = re.sub(r"[^0-9.eE+-]", "", raw)
    try:
        number = float(cleaned) * multiplier
    except (TypeError, ValueError):
        return None
    return -number if negative else number


def _semantic_values_match(rendered, replayed, display=None):
    if rendered is None:
        return None
    left = _semantic_number(rendered)
    right = _semantic_number(replayed)
    if left is not None and right is not None:
        display = display if isinstance(display, dict) else {}
        try:
            tolerance = max(0.0, float(display.get("tolerance") or 0.000001))
        except (TypeError, ValueError):
            tolerance = 0.000001
        return abs(left - right) <= max(tolerance, abs(right) * tolerance)
    return re.sub(r"\s+", " ", str(rendered)).strip().lower() == re.sub(
        r"\s+", " ", str(replayed)
    ).strip().lower()


def _normalized_field(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _row_value(row, field_hint=None, value_hint=None):
    if not isinstance(row, dict) or not row:
        return None, None
    if field_hint:
        wanted = _normalized_field(field_hint)
        for key, value in row.items():
            if _normalized_field(key) == wanted:
                return str(key), value
    if value_hint not in (None, ""):
        wanted_value = _inspection_text(value_hint, 400).replace(",", "").replace("$", "")
        for key, value in row.items():
            if _inspection_text(value, 400).replace(",", "").replace("$", "") == wanted_value:
                return str(key), value
    numeric = [
        (str(key), value)
        for key, value in row.items()
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    ]
    return numeric[0] if len(numeric) == 1 else (None, None)


def _matching_latest_row(current_row, latest_rows, row_index):
    if not isinstance(current_row, dict):
        return None
    dimensions = [
        (key, value)
        for key, value in current_row.items()
        if value not in (None, "")
        and not isinstance(value, (int, float, Decimal, bool))
    ][:4]
    if dimensions:
        matches = [
            row for row in latest_rows
            if isinstance(row, dict)
            and all(str(row.get(key)) == str(value) for key, value in dimensions)
        ]
        if len(matches) == 1:
            return matches[0]
    if isinstance(row_index, (int, float)):
        index = int(row_index)
        if 0 <= index < len(latest_rows) and isinstance(latest_rows[index], dict):
            return latest_rows[index]
    return latest_rows[0] if len(latest_rows) == 1 and isinstance(latest_rows[0], dict) else None


def _numeric_delta(current, latest):
    def parse(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float, Decimal)):
            return float(value)
        raw = str(value or "").strip()
        negative = raw.startswith("(") and raw.endswith(")")
        raw = re.sub(r"[^0-9.eE+-]", "", raw)
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            return None
        return -parsed if negative else parsed

    left, right = parse(current), parse(latest)
    if left is None or right is None:
        return None
    delta = right - left
    return {
        "absolute": delta,
        "percent": (delta / abs(left) * 100) if left else None,
    }


def _dashboard_inspection(
    slug,
    version,
    target,
    binding,
    trace,
    semantic_selection=None,
    as_of=None,
):
    """Build deterministic breadcrumbs for one rendered business object.

    A versioned semantic-map object is authoritative when present. Query/DOM
    matching remains the compatibility path for older artifacts.
    """
    selected_target = _sanitize_inspection_target(target)
    selected_binding = _sanitize_inspection_binding(binding)
    selected_semantic = _sanitize_semantic_selection(semantic_selection)
    # Defense in depth for stale or hand-written clients: artifact-level source
    # edges remain available, but an unbound visual object cannot claim a query.
    trace = _bound_inspection_trace(selected_binding, trace)
    query_sql = str(trace.get("sql") or "").strip()
    if len(query_sql) > 50_000:
        raise ValueError("query trace is too large")
    requested_as_of = as_of if as_of is not None else trace.get("as_of")
    normalized_as_of = _normalize_as_of(requested_as_of) if requested_as_of else None

    with _conn() as c:
        dashboard = c.execute(
            "SELECT id,slug,name,description,latest_version,runtime_kind,app_kind "
            "FROM rvbbit.dashboards WHERE slug=%s",
            (slug,),
        ).fetchone()
        if not dashboard:
            return {"error": {"code": "NOT_FOUND", "message": "No such published artifact."}}
        selected_version = int(version or dashboard["latest_version"])
        if selected_version < 1:
            raise ValueError("version must be a positive integer")
        version_row = c.execute(
            "SELECT manifest FROM rvbbit.dashboard_versions "
            "WHERE dashboard_id=%s AND version=%s",
            (dashboard["id"], selected_version),
        ).fetchone()
        if not version_row:
            return {"error": {"code": "VERSION_NOT_FOUND", "message": "No such artifact version."}}
        deps = c.execute(
            "SELECT kind,object_ref,base_sql,source,confidence "
            "FROM rvbbit.dashboard_deps WHERE dashboard_id=%s AND version=%s "
            "ORDER BY kind,object_ref NULLS LAST,base_sql NULLS LAST",
            (dashboard["id"], selected_version),
        ).fetchall()

    effective_manifest = _effective_artifact_manifest(
        dashboard["id"], selected_version, version_row.get("manifest") or {}
    )
    semantic_object = None
    semantic_context = {}
    if selected_semantic:
        semantic_object = _semantic_object_from_manifest(
            effective_manifest, selected_semantic
        )
        if not semantic_object:
            raise ValueError(
                f"semantic object {selected_semantic['id']} is not defined in artifact version "
                f"{selected_version}"
            )
        query_sql, semantic_context = _render_semantic_sql(
            semantic_object, selected_semantic.get("context")
        )
        meaning = semantic_object.get("meaning") or {}
        evaluator = semantic_object.get("evaluator") or {}
        selected_binding = {
            "kind": "value",
            "confidence": "semantic",
            "field": _inspection_text(evaluator.get("value_column"), 160),
            "label": _inspection_text(meaning.get("label"), 300),
            "value": _inspection_text(selected_semantic.get("rendered_value"), 400),
            "semantic_id": semantic_object["id"],
        }

    validation = None
    if query_sql:
        validation = tool_validate_sql(query_sql, normalized_as_of)
        if not validation.get("valid") or not validation.get("safe_select"):
            raise ValueError("selected evaluator is not a safe read-only query")
    tables = _referenced_tables(query_sql) if query_sql else []
    if not tables:
        tables = sorted({
            str(row["object_ref"])
            for row in deps
            if row.get("kind") == "table" and row.get("object_ref")
        })

    source_cards = []
    with _ro() as rc, rc.cursor() as cur:
        for table in tables[:16]:
            schema, rel = _split(table)
            if not _schema_allowed(schema):
                continue
            freshness = _freshness(cur, schema, rel)
            doc = None
            try:
                row = cur.execute(
                    "SELECT doc FROM rvbbit.catalog_docs "
                    "WHERE graph_id=%s AND kind='db_table' "
                    "AND schema_name=%s AND rel_name=%s "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (GRAPH, schema, rel),
                ).fetchone()
                doc = _inspection_text((row or {}).get("doc"), 900) or None
            except Exception:  # noqa: BLE001 — catalog prose is optional evidence
                pass
            source_cards.append({
                "table": f"{schema}.{rel}",
                "doc": doc,
                "freshness": freshness,
            })

    related = []
    if tables:
        with _conn() as c:
            related = c.execute(
                "SELECT DISTINCT d.slug,d.name,d.app_kind,d.latest_version "
                "FROM rvbbit.dashboard_deps dd "
                "JOIN rvbbit.dashboards d ON d.id=dd.dashboard_id "
                "AND d.latest_version=dd.version "
                "WHERE dd.kind='table' AND dd.object_ref=ANY(%s::text[]) "
                "AND d.slug<>%s ORDER BY d.name LIMIT 8",
                (tables, slug),
            ).fetchall()

    replay = None
    semantic_public = None
    replay_result = None
    if semantic_object and query_sql:
        evaluator = semantic_object.get("evaluator") or {}
        limit = 2 if evaluator.get("shape") == "scalar" else 200
        replay_result = tool_run_sql(query_sql, normalized_as_of, limit)
        rendered_value = selected_semantic.get("rendered_value")
        if replay_result.get("error"):
            replay = {
                "status": "error",
                "rendered_value": rendered_value,
                "error": _inspection_text(
                    (replay_result.get("error") or {}).get("message")
                    or replay_result.get("error"),
                    800,
                ),
            }
        else:
            replay_value, value_column = _semantic_result_value(
                semantic_object, replay_result
            )
            matches = _semantic_values_match(
                rendered_value, replay_value, semantic_object.get("display")
            )
            replay = {
                "status": (
                    "verified" if matches is True
                    else "mismatch" if matches is False
                    else "recreated"
                ),
                "value": replay_value,
                "rendered_value": rendered_value,
                "matches_rendered": matches,
                "value_column": value_column,
                "row_count": replay_result.get("row_count"),
                "engine": replay_result.get("engine"),
                "elapsed_ms": replay_result.get("elapsed_ms"),
                "as_of": normalized_as_of,
            }
        semantic_public = {
            "id": semantic_object["id"],
            "kind": semantic_object["kind"],
            "meaning": semantic_object.get("meaning") or {},
            "context": semantic_context,
            "display": semantic_object.get("display") or {},
            "definition_hash": semantic_object.get("definition_hash"),
            "evaluator": {
                "shape": evaluator.get("shape"),
                "value_column": evaluator.get("value_column"),
            },
            "source_queries": semantic_object.get("source_queries") or [],
        }

    comparison = None
    if semantic_object and query_sql and normalized_as_of and replay_result and not replay_result.get("error"):
        latest = tool_run_sql(query_sql, None, 2 if (semantic_object.get("evaluator") or {}).get("shape") == "scalar" else 200)
        if not latest.get("error"):
            current_value, field = _semantic_result_value(semantic_object, replay_result)
            latest_value, _ = _semantic_result_value(semantic_object, latest)
            comparison = {
                "as_of": normalized_as_of,
                "field": field,
                "current": current_value,
                "latest": latest_value,
                "delta": _numeric_delta(current_value, latest_value),
                "matched_latest_row": latest_value is not None,
            }
    elif query_sql and normalized_as_of:
        historical = tool_run_sql(query_sql, normalized_as_of, 200)
        latest = tool_run_sql(query_sql, None, 200)
        if not historical.get("error") and not latest.get("error"):
            row_index = selected_binding.get("trace_row_index")
            if row_index is None:
                row_index = selected_binding.get("row_index")
            index = int(row_index) if isinstance(row_index, (int, float)) else 0
            historical_rows = historical.get("rows") or []
            current_row = (
                historical_rows[index]
                if 0 <= index < len(historical_rows) and isinstance(historical_rows[index], dict)
                else (historical_rows[0] if len(historical_rows) == 1 else None)
            )
            latest_row = _matching_latest_row(current_row, latest.get("rows") or [], row_index)
            field, current_value = _row_value(
                current_row,
                selected_binding.get("field"),
                selected_binding.get("value"),
            )
            latest_value = latest_row.get(field) if field and latest_row else None
            if current_row is not None:
                comparison = {
                    "as_of": normalized_as_of,
                    "field": field,
                    "current": current_value,
                    "latest": latest_value,
                    "delta": _numeric_delta(current_value, latest_value),
                    "matched_latest_row": latest_row is not None,
                }

    query_hash = _inspection_text(trace.get("query_hash"), 80) or None
    query_id = _inspection_text(trace.get("id"), 120) or None
    if semantic_object:
        query_hash = hashlib.sha256(
            re.sub(r"\s+", " ", query_sql).strip().lower().encode()
        ).hexdigest()[:16]
        query_id = f"semantic:{semantic_object['id']}"
    result = {
        "artifact": {
            "slug": dashboard["slug"],
            "name": dashboard["name"],
            "description": dashboard.get("description"),
            "version": selected_version,
            "latest_version": int(dashboard["latest_version"]),
            "runtime_kind": dashboard.get("runtime_kind"),
            "app_kind": dashboard.get("app_kind"),
        },
        "selection": selected_target,
        "binding": selected_binding,
        "provenance": {
            "query_id": query_id,
            "query_hash": query_hash,
            "sql": query_sql or None,
            "as_of": normalized_as_of,
            "engine": (validation or {}).get("engine") or _inspection_text(trace.get("engine"), 120) or None,
            "tables": tables,
            "confidence": selected_binding["confidence"],
            "source": "semantic_map" if semantic_object else "query_trace",
        },
        "sources": source_cards,
        "related_artifacts": [dict(row) for row in related],
        "comparison": comparison,
        "dependency_count": len(deps),
    }
    if semantic_public:
        result["semantic_object"] = semantic_public
    if replay:
        result["replay"] = replay
    return result


# ── Gallery metrics ─────────────────────────────────────────────────────────
#
# The catalog, observation log, Home, Brief, and Calliope stage all project the
# same parameter-aware metric identity.  Gallery reads materialized
# observations only: opening the page must never execute every metric.
_GALLERY_METRIC_LIMIT = 600
_GALLERY_METRIC_SERIES_LIMIT = 32
_METRIC_BUCKETS = {"raw", "day", "week", "month", "quarter", "year"}


def _metric_name(value):
    name = re.sub(r"\s+", " ", str(value or "")).strip()
    if not name or len(name) > 240 or any(ord(char) < 32 for char in name):
        raise ValueError("metric name is invalid")
    return name


def _metric_params(value=None):
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        if len(value) > 20_000:
            raise ValueError("metric parameters are too large")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("metric parameters must be a JSON object") from exc
    if not isinstance(value, dict) or len(value) > 48:
        raise ValueError("metric parameters must be a bounded JSON object")
    if any(not isinstance(key, str) or not key or len(key) > 160 for key in value):
        raise ValueError("metric parameter names are invalid")
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("metric parameters must contain JSON values") from exc
    if len(encoded.encode("utf-8")) > 20_000:
        raise ValueError("metric parameters are too large")
    return json.loads(encoded)


def _metric_canonical_key(name, params=None):
    normalized_name = _metric_name(name)
    normalized_params = _metric_params(params)
    encoded = json.dumps(
        normalized_params, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
    return f"metric:{normalized_name}:{digest}"


def _metric_display(definition):
    definition = definition if isinstance(definition, dict) else {}
    labels = definition.get("labels") if isinstance(definition.get("labels"), dict) else {}
    nested = labels.get("display") if isinstance(labels.get("display"), dict) else {}
    display = dict(nested)
    for key in (
        "unit", "format", "currency", "prefix", "suffix", "decimals",
        "precision", "preferred_direction", "good_direction", "label", "title",
    ):
        if key not in display and labels.get(key) not in (None, ""):
            display[key] = labels[key]
    preferred = str(
        display.get("preferred_direction") or display.get("good_direction") or "neutral"
    ).strip().lower()
    display["preferred_direction"] = {
        "up": "higher", "increase": "higher", "higher": "higher",
        "down": "lower", "decrease": "lower", "lower": "lower",
    }.get(preferred, "neutral")
    try:
        decimals = display.get("decimals", display.get("precision"))
        if decimals not in (None, ""):
            display["decimals"] = max(0, min(int(decimals), 8))
    except (TypeError, ValueError):
        display.pop("decimals", None)
    return {
        key: value for key, value in display.items()
        if value not in (None, "") and key != "good_direction"
    }


def _metric_title(definition):
    display = _metric_display(definition)
    return str(
        display.get("title") or display.get("label")
        or str((definition or {}).get("name") or "Metric").replace("_", " ").title()
    )[:240]


def _metric_observation_public(row):
    if not row:
        return None
    raw = dict(row)
    value = raw.get("value")
    numeric = _metric_scalar(value)
    verdict = raw.get("verdict") if isinstance(raw.get("verdict"), dict) else {}
    ok = verdict.get("ok") if isinstance(verdict, dict) else None
    if ok is None:
        status = str(raw.get("status") or "").lower()
        ok = True if status in {"pass", "passing", "ok", "healthy"} else (
            False if status in {"fail", "failing", "breach", "breaching"} else None
        )
    return {
        "observation_id": int(raw["observation_id"]) if raw.get("observation_id") is not None else None,
        "metric_version": int(raw["metric_version"]) if raw.get("metric_version") is not None else None,
        "value": numeric if numeric is not None else value,
        "numeric_value": numeric,
        "verdict": verdict,
        "status": raw.get("status"),
        "ok": ok,
        "trigger": raw.get("trigger"),
        "params": raw.get("params") if isinstance(raw.get("params"), dict) else {},
        "data_as_of": _iso_utc(raw.get("data_as_of")),
        "observed_at": _iso_utc(raw.get("observed_at")),
    }


def _metric_trend(series, preferred_direction="neutral"):
    numeric = [
        point for point in series
        if isinstance(point, dict) and point.get("numeric_value") is not None
    ]
    if len(numeric) < 2:
        return None
    first = float(numeric[0]["numeric_value"])
    last = float(numeric[-1]["numeric_value"])
    absolute = last - first
    percent = (absolute / abs(first) * 100) if first else None
    direction = "up" if absolute > 0 else "down" if absolute < 0 else "flat"
    preferred = str(preferred_direction or "neutral")
    meaning = "neutral"
    if direction != "flat" and preferred in {"higher", "lower"}:
        meaning = "good" if (
            (preferred == "higher" and direction == "up")
            or (preferred == "lower" and direction == "down")
        ) else "bad"
    return {
        "absolute": absolute,
        "percent": percent,
        "direction": direction,
        "meaning": meaning,
        "from_observation_id": numeric[0].get("observation_id"),
        "to_observation_id": numeric[-1].get("observation_id"),
    }


def _metric_user_relations(owner):
    followed, pinned = set(), set()
    if not owner:
        return followed, pinned
    try:
        with _conn() as conn:
            followed = {
                str(row["canonical_key"])
                for row in conn.execute(
                    "SELECT canonical_key FROM rvbbit.calliope_metric_follows "
                    "WHERE lower(owner_email)=lower(%s)", (owner,),
                ).fetchall()
            }
            pinned = {
                str(row["canonical_key"])
                for row in conn.execute(
                    "SELECT i.canonical_key FROM rvbbit.calliope_board_items i "
                    "JOIN rvbbit.calliope_boards b ON b.id=i.board_id "
                    "WHERE lower(b.owner_email)=lower(%s) AND b.slug='home' "
                    "AND i.item_kind='metric'", (owner,),
                ).fetchall()
            }
    except Exception:  # noqa: BLE001 — Gallery metrics still browse without Calliope tables
        pass
    return followed, pinned


def _metric_catalog_snapshot(owner=None, search=None, category=None, limit=None):
    limit = max(1, min(int(limit or _GALLERY_METRIC_LIMIT), _GALLERY_METRIC_LIMIT))
    search = re.sub(r"\s+", " ", str(search or "")).strip()[:300] or None
    category = re.sub(r"\s+", " ", str(category or "")).strip()[:160] or None
    with _conn() as conn:
        definitions = conn.execute(
            "SELECT name,version,description,params,grain,labels,check_sql IS NOT NULL AS has_check,"
            "category,subcategory,created_at FROM rvbbit.metric_catalog "
            "WHERE (%s::text IS NULL OR name ILIKE '%%'||%s||'%%' "
            "OR description ILIKE '%%'||%s||'%%') "
            "AND (%s::text IS NULL OR category=%s) "
            "ORDER BY category NULLS LAST,subcategory NULLS LAST,name LIMIT %s",
            (search, search, search, category, category, limit),
        ).fetchall()
        names = [str(row["name"]) for row in definitions]
        observations = []
        if names:
            observations = conn.execute(
                "SELECT * FROM (SELECT o.observation_id,o.metric_name,o.metric_version,"
                "o.value,o.verdict,o.status,o.trigger,o.params,o.data_as_of,o.observed_at,"
                "row_number() OVER (PARTITION BY o.metric_name ORDER BY "
                "coalesce(o.data_as_of,o.observed_at) DESC,o.observed_at DESC,o.observation_id DESC) AS rn "
                "FROM rvbbit.metric_observations o WHERE o.metric_name=ANY(%s::text[]) "
                "AND o.params='{}'::jsonb "
                "AND coalesce(o.data_as_of,o.observed_at)<=now()) ranked WHERE rn<=%s "
                "ORDER BY metric_name,coalesce(data_as_of,observed_at),observed_at,observation_id",
                (names, _GALLERY_METRIC_SERIES_LIMIT),
            ).fetchall()
        category_rows = conn.execute(
            "SELECT coalesce(category,'Uncategorized') AS category,count(*)::int AS count "
            "FROM rvbbit.metric_catalog GROUP BY category ORDER BY category NULLS LAST"
        ).fetchall()
    by_name = {name: [] for name in names}
    for raw in observations:
        point = _metric_observation_public(raw)
        if point:
            by_name.setdefault(str(raw["metric_name"]), []).append(point)
    followed, pinned = _metric_user_relations(owner)
    metrics = []
    for raw in definitions:
        definition = dict(raw)
        params = {}
        canonical_key = _metric_canonical_key(definition["name"], params)
        series = by_name.get(str(definition["name"]), [])
        display = _metric_display(definition)
        latest = series[-1] if series else None
        metrics.append({
            "name": definition["name"],
            "title": _metric_title(definition),
            "description": definition.get("description") or "",
            "version": int(definition.get("version") or 1),
            "grain": definition.get("grain"),
            "category": definition.get("category") or "Uncategorized",
            "subcategory": definition.get("subcategory") or "",
            "parameter_defaults": definition.get("params") or {},
            "parameter_count": len(definition.get("params") or {}),
            "params": params,
            "display": display,
            "has_check": bool(definition.get("has_check")),
            "defined_at": _iso_utc(definition.get("created_at")),
            "canonical_key": canonical_key,
            "snapshot": latest,
            "series": series,
            "trend": _metric_trend(series, display.get("preferred_direction")),
            "followed": canonical_key in followed,
            "pinned": canonical_key in pinned,
        })
    return {
        "metrics": metrics,
        "count": len(metrics),
        "categories": [dict(row) for row in category_rows],
        "observation_contract": "materialized",
    }


def _metric_detail_snapshot(name, params=None, *, owner=None, days=90, bucket="raw"):
    name = _metric_name(name)
    params = _metric_params(params)
    try:
        days = max(1, min(int(days or 90), 3650))
    except (TypeError, ValueError) as exc:
        raise ValueError("days must be an integer") from exc
    bucket = str(bucket or "raw").strip().lower()
    if bucket not in _METRIC_BUCKETS:
        raise ValueError(f"bucket must be one of {sorted(_METRIC_BUCKETS)}")
    with _conn() as conn:
        definition = conn.execute(
            "SELECT name,version,description,params,grain,labels,sql AS definition_sql,"
            "check_sql,category,subcategory,owner,created_at "
            "FROM rvbbit.metric_catalog WHERE name=%s", (name,),
        ).fetchone()
        if not definition:
            raise LookupError("No such governed metric")
        definition = dict(definition)
        expression = "coalesce(data_as_of,observed_at)" if bucket == "raw" else (
            f"date_trunc('{bucket}',coalesce(data_as_of,observed_at))"
        )
        series_rows = conn.execute(
            "SELECT observation_id,metric_version,value,verdict,status,trigger,params,"
            "data_as_of,observed_at FROM (SELECT * FROM ("
            "SELECT DISTINCT ON (bucket_value) * FROM ("
            f"SELECT o.*,{expression} AS bucket_value FROM rvbbit.metric_observations o "
            "WHERE metric_name=%s AND params=%s::jsonb "
            "AND coalesce(data_as_of,observed_at)>=now()-(%s * interval '1 day') "
            "AND coalesce(data_as_of,observed_at)<=now()"
            ") scoped ORDER BY bucket_value DESC,coalesce(data_as_of,observed_at) DESC,"
            "observed_at DESC,observation_id DESC) picked LIMIT 500) bounded "
            "ORDER BY coalesce(data_as_of,observed_at),observed_at,observation_id",
            (name, json.dumps(params), days),
        ).fetchall()
        latest_row = conn.execute(
            "SELECT observation_id,metric_version,value,verdict,status,trigger,params,"
            "data_as_of,observed_at FROM rvbbit.metric_observations "
            "WHERE metric_name=%s AND params=%s::jsonb "
            "AND coalesce(data_as_of,observed_at)<=now() ORDER BY "
            "coalesce(data_as_of,observed_at) DESC,observed_at DESC,observation_id DESC LIMIT 1",
            (name, json.dumps(params)),
        ).fetchone()
        versions = conn.execute(
            "SELECT version,created_at FROM rvbbit.metric_defs WHERE name=%s "
            "ORDER BY version DESC", (name,),
        ).fetchall()
        artifacts = conn.execute(
            "SELECT DISTINCT d.slug,d.name,d.description,d.app_kind,d.latest_version "
            "FROM rvbbit.dashboard_deps x JOIN rvbbit.dashboards d ON d.id=x.dashboard_id "
            "WHERE x.kind='metric' AND x.object_ref=%s AND x.version=d.latest_version "
            "ORDER BY d.name LIMIT 12", (name,),
        ).fetchall()
        try:
            freshness = conn.execute(
                "SELECT table_schema||'.'||table_name AS table_name,freshness_column,"
                "max_freshness,age,stale FROM rvbbit.metric_dependency_freshness("
                "ARRAY[%s]::text[],interval '2 days') ORDER BY table_schema,table_name",
                (name,),
            ).fetchall()
        except Exception:  # noqa: BLE001 — older installs still have definition lineage
            freshness = []
        try:
            lineage_row = conn.execute(
                "SELECT rvbbit.metric_lineage(%s) AS source_tables", (name,),
            ).fetchone()
            lineage = (lineage_row or {}).get("source_tables") or []
        except Exception:  # noqa: BLE001
            lineage = []
    series = [point for row in series_rows if (point := _metric_observation_public(row))]
    latest = _metric_observation_public(latest_row)
    if latest and not any(
        point.get("observation_id") == latest.get("observation_id") for point in series
    ):
        series.append(latest)
        series.sort(key=lambda point: point.get("data_as_of") or point.get("observed_at") or "")
    display = _metric_display(definition)
    canonical_key = _metric_canonical_key(name, params)
    followed, pinned = _metric_user_relations(owner)
    dependency_names = list(dict.fromkeys([
        str(row.get("table_name")) for row in freshness if row.get("table_name")
    ] + [str(value) for value in lineage if value]))
    return {
        "name": name,
        "title": _metric_title(definition),
        "description": definition.get("description") or "",
        "version": int(definition.get("version") or 1),
        "grain": definition.get("grain"),
        "category": definition.get("category") or "Uncategorized",
        "subcategory": definition.get("subcategory") or "",
        "owner": definition.get("owner"),
        "parameter_defaults": definition.get("params") or {},
        "params": params,
        "display": display,
        "definition_sql": definition.get("definition_sql") or "",
        "has_check": bool(definition.get("check_sql")),
        "defined_at": _iso_utc(definition.get("created_at")),
        "versions": [{**dict(row), "created_at": _iso_utc(row.get("created_at"))} for row in versions],
        "canonical_key": canonical_key,
        "snapshot": latest,
        "series": series,
        "trend": _metric_trend(series, display.get("preferred_direction")),
        "range": {"days": days, "bucket": bucket},
        "dependencies": [{
            **dict(row),
            "max_freshness": _iso_utc(row.get("max_freshness")),
            "age": str(row.get("age")) if row.get("age") is not None else None,
        } for row in freshness],
        "source_tables": dependency_names,
        "artifacts": [dict(row) for row in artifacts],
        "followed": canonical_key in followed,
        "pinned": canonical_key in pinned,
    }


def _metric_follow_snapshot(owner):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id,metric_name,params,canonical_key,created_at,updated_at "
            "FROM rvbbit.calliope_metric_follows WHERE lower(owner_email)=lower(%s) "
            "ORDER BY updated_at DESC", (owner,),
        ).fetchall()
    return {"follows": [{
        **dict(row),
        "id": str(row["id"]),
        "created_at": _iso_utc(row.get("created_at")),
        "updated_at": _iso_utc(row.get("updated_at")),
    } for row in rows]}


def _follow_metric(owner, execution_subject, name, params=None):
    name = _metric_name(name)
    params = _metric_params(params)
    canonical_key = _metric_canonical_key(name, params)
    with _conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM rvbbit.metric_catalog WHERE name=%s", (name,),
        ).fetchone():
            raise LookupError("No such governed metric")
        row = conn.execute(
            "INSERT INTO rvbbit.calliope_metric_follows "
            "(id,owner_email,execution_subject,metric_name,params,canonical_key) "
            "VALUES (%s::uuid,%s,%s,%s,%s::jsonb,%s) "
            "ON CONFLICT (owner_email,canonical_key) DO UPDATE SET "
            "execution_subject=excluded.execution_subject,updated_at=now() RETURNING *",
            (
                str(uuid.uuid4()), owner, execution_subject or owner, name,
                json.dumps(params), canonical_key,
            ),
        ).fetchone()
    return {
        "follow": {
            "id": str(row["id"]), "metric_name": name, "params": params,
            "canonical_key": canonical_key, "followed": True,
        }
    }


def _unfollow_metric(owner, name, params=None):
    name = _metric_name(name)
    params = _metric_params(params)
    canonical_key = _metric_canonical_key(name, params)
    with _conn() as conn:
        row = conn.execute(
            "DELETE FROM rvbbit.calliope_metric_follows "
            "WHERE lower(owner_email)=lower(%s) AND canonical_key=%s RETURNING id",
            (owner, canonical_key),
        ).fetchone()
    return {
        "metric_name": name, "params": params, "canonical_key": canonical_key,
        "followed": False, "removed": bool(row),
    }


# ── Semantic Home ───────────────────────────────────────────────────────────
#
# Home is deliberately a composition of stable warehouse handles, not copied
# dashboard DOM.  Whole artifacts follow latest. Named business objects pin an
# exact artifact version + definition hash + resolved dashboard context, which
# makes them replayable and protects their meaning when an artifact evolves.
_SEMANTIC_HOME_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$", re.I)
_SEMANTIC_HOME_ITEM_KINDS = {"artifact", "artifact_object", "metric"}


def _semantic_home_enabled():
    try:
        import calliope
        return calliope.is_enabled()
    except Exception:  # noqa: BLE001 — the gallery remains independently useful
        return False


def _semantic_home_artifact_href(slug, app_kind, version=None):
    prefix = "/d" if (app_kind or "dashboard").lower() == "dashboard" else "/apps"
    suffix = f"/versions/{int(version)}" if version else ""
    return f"{prefix}/{slug}{suffix}"


def _semantic_home_metric_href(name, params=None):
    from urllib.parse import quote, urlencode

    query = {"view": "metrics", "metric": _metric_name(name)}
    normalized_params = _metric_params(params)
    if normalized_params:
        query["params"] = json.dumps(
            normalized_params, separators=(",", ":"), ensure_ascii=False
        )
    return "/gallery?" + urlencode(query, quote_via=quote)


def _semantic_home_artifact_row(slug, version=None):
    if not _SEMANTIC_HOME_SLUG_RE.fullmatch(str(slug or "")):
        raise ValueError("artifact slug is invalid")
    with _conn() as conn:
        dashboard = conn.execute(
            "SELECT id,slug,name,description,owner_email,team,runtime_kind,app_kind,"
            "latest_version,updated_at FROM rvbbit.dashboards WHERE slug=%s",
            (slug,),
        ).fetchone()
        if not dashboard:
            raise LookupError("No such published artifact.")
        selected_version = int(version or dashboard["latest_version"])
        if selected_version < 1:
            raise ValueError("version must be a positive integer")
        row = conn.execute(
            "SELECT v.version,v.manifest,v.created_by,v.created_at,"
            "e.status AS semantic_status,e.semantic_map,e.verification,"
            "e.prompt_version,e.model,e.updated_at AS semantic_updated_at "
            "FROM rvbbit.dashboard_versions v "
            "LEFT JOIN rvbbit.artifact_semantic_enrichments e "
            "ON e.dashboard_id=v.dashboard_id AND e.version=v.version "
            "WHERE v.dashboard_id=%s AND v.version=%s",
            (dashboard["id"], selected_version),
        ).fetchone()
        if not row:
            raise LookupError("No such artifact version.")
        deps = conn.execute(
            "SELECT kind,object_ref FROM rvbbit.dashboard_deps "
            "WHERE dashboard_id=%s AND version=%s ORDER BY kind,object_ref NULLS LAST",
            (dashboard["id"], selected_version),
        ).fetchall()
    enrichment = {
        "status": row.get("semantic_status"),
        "semantic_map": row.get("semantic_map") or {},
        "verification": row.get("verification") or {},
        "prompt_version": row.get("prompt_version"),
        "model": row.get("model"),
        "updated_at": row.get("semantic_updated_at"),
    }
    manifest = _effective_artifact_manifest(
        dashboard["id"], selected_version, row.get("manifest") or {}, enrichment
    )
    return dict(dashboard), dict(row), manifest, [dict(dep) for dep in deps]


def _semantic_home_source_trail(tables):
    """Resolve a few friendly catalog breadcrumbs, never expose a graph UI."""
    normalized = []
    for table in tables or []:
        raw = str(table or "").strip()
        if not raw or raw in normalized:
            continue
        schema, relation = _split(raw)
        if _schema_allowed(schema):
            normalized.append(f"{schema}.{relation}")
        if len(normalized) >= 4:
            break
    docs = {}
    try:
        with _conn() as conn:
            for table in normalized:
                schema, relation = _split(table)
                row = conn.execute(
                    "SELECT doc FROM rvbbit.catalog_docs "
                    "WHERE graph_id=%s AND kind='db_table' AND schema_name=%s AND rel_name=%s "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (GRAPH, schema, relation),
                ).fetchone()
                docs[table] = _semantic_text((row or {}).get("doc"), 260)
    except Exception:  # noqa: BLE001 — catalog prose enriches, never gates, a pin
        docs = {}
    return [{
        "kind": "table",
        "relationship": "recreated from",
        "label": table,
        "detail": docs.get(table) or "Warehouse source",
        "handle": {"kind": "db_table", "table": table},
    } for table in normalized]


def _semantic_home_context_key(context):
    encoded = json.dumps(
        context or {}, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _semantic_home_resolve_handle(value, *, validate_sql=False):
    """Resolve one client or persisted locator into a current, bounded tile."""
    body = value if isinstance(value, dict) else {}
    kind = str(body.get("kind") or body.get("item_kind") or "").strip().lower()
    if kind not in _SEMANTIC_HOME_ITEM_KINDS:
        raise ValueError("Home items must be artifacts, named artifact objects, or metrics")
    if kind == "metric":
        name = _metric_name(body.get("name") or body.get("metric") or body.get("relation"))
        params = _metric_params(body.get("params"))
        detail = _metric_detail_snapshot(name, params, days=90, bucket="raw")
        source = {
            "kind": "metric",
            "name": name,
            "params": params,
            "tracking": "latest",
            "pinned_version": detail["version"],
        }
        presentation = {
            "title": detail["title"],
            "description": detail["description"],
            "grain": detail.get("grain"),
            "category": detail.get("category"),
            "subcategory": detail.get("subcategory"),
            "display": detail.get("display") or {},
        }
        trail = [{
            "kind": "metric",
            "relationship": "governed measure",
            "label": detail["title"],
            "detail": detail.get("description") or detail.get("grain") or "Warehouse metric",
            "handle": {"kind": "metric", "name": name, "relation": name, "params": params},
        }]
        trail.extend({
            "kind": "table",
            "relationship": "derived from",
            "label": table,
            "detail": "Metric source",
            "handle": {"kind": "db_table", "table": table},
        } for table in (detail.get("source_tables") or [])[:3])
        return {
            "kind": "metric",
            "canonical_key": detail["canonical_key"],
            "source": source,
            "presentation": presentation,
            "title": detail["title"],
            "description": detail["description"],
            "grain": detail.get("grain"),
            "category": detail.get("category"),
            "subcategory": detail.get("subcategory"),
            "display": detail.get("display") or {},
            "params": params,
            "parameter_defaults": detail.get("parameter_defaults") or {},
            "version": detail["version"],
            "snapshot": detail.get("snapshot"),
            "series": (detail.get("series") or [])[-_GALLERY_METRIC_SERIES_LIMIT:],
            "trend": detail.get("trend"),
            "open_url": _semantic_home_metric_href(name, params),
            "thumbnail_url": None,
            "trail": trail,
            "status": "ready" if detail.get("snapshot") else "unobserved",
            "replayable": False,
        }
    slug = str(body.get("slug") or "").strip()
    requested_version = body.get("version") if kind == "artifact_object" else None
    try:
        version = int(requested_version) if requested_version not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ValueError("version must be a positive integer") from exc
    dashboard, version_row, manifest, deps = _semantic_home_artifact_row(slug, version)
    selected_version = int(version_row["version"])
    app_kind = str(dashboard.get("app_kind") or "dashboard").lower()
    object_count = len(((manifest.get("semantic_map") or {}).get("objects") or []))
    dependency_tables = sorted({
        str(dep.get("object_ref"))
        for dep in deps
        if dep.get("kind") == "table" and dep.get("object_ref")
    })
    artifact_handle = {
        "kind": "artifact",
        "slug": slug,
        "version": selected_version,
    }
    artifact_trail = {
        "kind": "artifact",
        "relationship": "defined in" if kind == "artifact_object" else "working set",
        "label": dashboard.get("name") or slug,
        "detail": f"{app_kind.replace('_', ' ')} · version {selected_version}",
        "handle": artifact_handle,
    }
    if kind == "artifact":
        source = {
            "kind": "artifact",
            "slug": slug,
            "tracking": "latest",
            "pinned_version": selected_version,
        }
        presentation = {
            "title": dashboard.get("name") or slug,
            "description": dashboard.get("description") or "",
            "app_kind": app_kind,
            "runtime_kind": dashboard.get("runtime_kind") or "html",
        }
        trail = [artifact_trail]
        if object_count:
            trail.append({
                "kind": "semantic_map",
                "relationship": "contains",
                "label": f"{object_count} named business object{'' if object_count == 1 else 's'}",
                "detail": "Values this artifact can explain and recreate",
                "handle": {"kind": "semantic_map", "slug": slug, "version": selected_version},
            })
        trail.extend(_semantic_home_source_trail(dependency_tables)[:2])
        return {
            "kind": kind,
            "canonical_key": f"artifact:{slug}",
            "source": source,
            "presentation": presentation,
            "title": presentation["title"],
            "description": presentation["description"],
            "app_kind": app_kind,
            "version": selected_version,
            "latest_version": int(dashboard["latest_version"]),
            "open_url": _semantic_home_artifact_href(slug, app_kind),
            "thumbnail_url": f"/thumbs/{_artifact_kind(app_kind)}/{slug}.png",
            "trail": trail,
            "status": "ready",
        }

    object_id = str(body.get("object_id") or "").strip()
    if not _SEMANTIC_OBJECT_ID_RE.fullmatch(object_id):
        raise ValueError("semantic object id is invalid")
    requested_hash = _inspection_text(body.get("definition_hash"), 80)
    semantic_object = _semantic_object_from_manifest(
        manifest, {"id": object_id, "definition_hash": requested_hash}
    )
    if not semantic_object:
        raise LookupError("That named business object is not defined in this artifact version.")
    context = _semantic_json_value(body.get("context") or {})
    if not isinstance(context, dict):
        context = {}
    rendered_sql, resolved_context = _render_semantic_sql(semantic_object, context)
    if validate_sql:
        validation = tool_validate_sql(rendered_sql)
        if not validation.get("valid") or not validation.get("safe_select"):
            raise ValueError("That business object does not have a safe replayable definition")
    meaning = semantic_object.get("meaning") or {}
    evaluator = semantic_object.get("evaluator") or {}
    tables = _referenced_tables(rendered_sql) or dependency_tables
    definition_hash = str(semantic_object.get("definition_hash") or "")
    context_key = _semantic_home_context_key(resolved_context)
    source = {
        "kind": "artifact_object",
        "slug": slug,
        "version": selected_version,
        "object_id": object_id,
        "definition_hash": definition_hash,
        "context": resolved_context,
    }
    presentation = {
        "title": meaning.get("label") or object_id.replace("_", " ").title(),
        "description": meaning.get("description") or "",
        "formula": meaning.get("formula") or "",
        "unit": meaning.get("unit") or "",
        "display": semantic_object.get("display") or {},
        "artifact_name": dashboard.get("name") or slug,
        "last_rendered_value": _semantic_json_value(body.get("rendered_value")),
    }
    trail = [{
        "kind": "artifact_object",
        "relationship": "named value",
        "label": presentation["title"],
        "detail": presentation["formula"] or presentation["description"] or "Replayable business meaning",
        "handle": source,
    }, artifact_trail]
    trail.extend(_semantic_home_source_trail(tables))
    return {
        "kind": kind,
        "canonical_key": f"artifact-object:{slug}:v{selected_version}:{object_id}:{context_key}",
        "source": source,
        "presentation": presentation,
        "title": presentation["title"],
        "description": presentation["description"],
        "formula": presentation["formula"],
        "unit": presentation["unit"],
        "display": presentation["display"],
        "context": resolved_context,
        "artifact_name": presentation["artifact_name"],
        "app_kind": app_kind,
        "version": selected_version,
        "latest_version": int(dashboard["latest_version"]),
        "newer_version_available": int(dashboard["latest_version"]) > selected_version,
        "open_url": _semantic_home_artifact_href(slug, app_kind, selected_version),
        "thumbnail_url": f"/thumbs/{_artifact_kind(app_kind)}/{slug}.png",
        "trail": trail,
        "status": "ready",
        "replayable": bool(rendered_sql.strip()),
        "evaluator_shape": evaluator.get("shape") or "scalar",
    }


def _semantic_home_board(conn, owner, *, create=False):
    row = conn.execute(
        "SELECT * FROM rvbbit.calliope_boards WHERE owner_email=%s AND slug='home'",
        (owner,),
    ).fetchone()
    if row or not create:
        return dict(row) if row else None
    return dict(conn.execute(
        "INSERT INTO rvbbit.calliope_boards (id,owner_email,slug,title,kind) "
        "VALUES (%s::uuid,%s,'home','My Home','home') "
        "ON CONFLICT (owner_email,slug) DO UPDATE SET owner_email=excluded.owner_email "
        "RETURNING *",
        (str(uuid.uuid4()), owner),
    ).fetchone())


def _semantic_home_public_item(row):
    raw = dict(row or {})
    try:
        resolved = _semantic_home_resolve_handle(raw.get("source") or {})
        # Keep only the inert, non-authoritative value observed when the user
        # pinned the object as a graceful fallback while its live replay runs.
        stored_presentation = raw.get("presentation") or {}
        if (
            resolved.get("kind") == "artifact_object"
            and stored_presentation.get("last_rendered_value") is not None
        ):
            resolved["presentation"]["last_rendered_value"] = (
                stored_presentation["last_rendered_value"]
            )
        resolved.update({
            "id": str(raw.get("id")),
            "sort_order": int(raw.get("sort_order") or 0),
            "created_at": _iso_utc(raw.get("created_at")),
            "updated_at": _iso_utc(raw.get("updated_at")),
        })
        return resolved
    except Exception as exc:  # noqa: BLE001 — a removed source must not break Home
        presentation = raw.get("presentation") or {}
        return {
            "id": str(raw.get("id")),
            "kind": raw.get("item_kind") or "artifact",
            "title": presentation.get("title") or "Unavailable Home item",
            "description": presentation.get("description") or "",
            "source": raw.get("source") or {},
            "presentation": presentation,
            "sort_order": int(raw.get("sort_order") or 0),
            "status": "unavailable",
            "message": _semantic_text(exc, 300),
            "trail": [],
            "created_at": _iso_utc(raw.get("created_at")),
            "updated_at": _iso_utc(raw.get("updated_at")),
        }


def _semantic_home_snapshot(owner):
    with _conn() as conn:
        board = _semantic_home_board(conn, owner, create=True)
        rows = conn.execute(
            "SELECT * FROM rvbbit.calliope_board_items WHERE board_id=%s::uuid "
            "ORDER BY sort_order,created_at LIMIT 96",
            (board["id"],),
        ).fetchall()
    return {
        "home": {
            "id": str(board["id"]),
            "title": board.get("title") or "My Home",
            "layout": board.get("layout") or {},
            "updated_at": _iso_utc(board.get("updated_at")),
        },
        "items": [_semantic_home_public_item(row) for row in rows],
    }


def _semantic_home_preview(source, execution_subject=None):
    resolved = _semantic_home_resolve_handle(source, validate_sql=True)
    if resolved["kind"] == "metric":
        snapshot = resolved.get("snapshot")
        if not snapshot:
            return {
                "status": "unobserved",
                "value": None,
                "display": resolved.get("display") or {},
                "metric": resolved["source"]["name"],
                "params": resolved["source"].get("params") or {},
            }
        return {
            "status": "observed",
            "value": snapshot.get("value"),
            "display": resolved.get("display") or {},
            "metric": resolved["source"]["name"],
            "params": resolved["source"].get("params") or {},
            "observation_id": snapshot.get("observation_id"),
            "metric_version": snapshot.get("metric_version"),
            "data_as_of": snapshot.get("data_as_of"),
            "observed_at": snapshot.get("observed_at"),
        }
    if resolved["kind"] != "artifact_object":
        raise ValueError("Only named business objects have a live value preview")
    dashboard, _, manifest, _ = _semantic_home_artifact_row(
        resolved["source"]["slug"], resolved["source"]["version"]
    )
    semantic_object = _semantic_object_from_manifest(manifest, {
        "id": resolved["source"]["object_id"],
        "definition_hash": resolved["source"]["definition_hash"],
    })
    rendered_sql, context = _render_semantic_sql(
        semantic_object, resolved["source"].get("context") or {}
    )
    token = _SESSION_SUB.set(execution_subject)
    try:
        evaluator = semantic_object.get("evaluator") or {}
        result = tool_run_sql(
            rendered_sql,
            None,
            2 if evaluator.get("shape") == "scalar" else 200,
        )
    finally:
        _SESSION_SUB.reset(token)
    if result.get("error"):
        return {
            "status": "error",
            "error": (result.get("error") or {}).get("message") or result.get("error"),
        }
    value, value_column = _semantic_result_value(semantic_object, result)
    return {
        "status": "recreated",
        "value": value,
        "value_column": value_column,
        "context": context,
        "display": resolved.get("display") or {},
        "unit": resolved.get("unit") or "",
        "row_count": result.get("row_count"),
        "engine": result.get("engine"),
        "elapsed_ms": result.get("elapsed_ms"),
        "artifact": {
            "slug": dashboard["slug"],
            "version": resolved["version"],
            "latest_version": resolved["latest_version"],
        },
    }


_PROMOTED_METRIC_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _semantic_home_metric_name(source):
    """Suggest one stable SQL-friendly metric name for an immutable object."""
    source = source if isinstance(source, dict) else {}
    raw = f"{source.get('slug') or 'artifact'}_{source.get('object_id') or 'value'}"
    base = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_") or "artifact_metric"
    context = source.get("context") if isinstance(source.get("context"), dict) else {}
    if context:
        base += "_" + _semantic_home_context_key(context)[:8]
    if not base[0].isalpha():
        base = "metric_" + base
    if len(base) > 63:
        digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
        base = base[:54].rstrip("_") + "_" + digest
    return base


def _semantic_home_metric_draft(source, execution_subject=None, *, preview=True):
    """Rehydrate a pin into a server-owned governed-metric definition draft."""
    resolved = _semantic_home_resolve_handle(source, validate_sql=True)
    if resolved.get("kind") != "artifact_object":
        raise ValueError("Only named business objects can become governed metrics")
    dashboard, _, manifest, _ = _semantic_home_artifact_row(
        resolved["source"]["slug"], resolved["source"]["version"]
    )
    semantic_object = _semantic_object_from_manifest(manifest, {
        "id": resolved["source"]["object_id"],
        "definition_hash": resolved["source"]["definition_hash"],
    })
    evaluator = (semantic_object or {}).get("evaluator") or {}
    if not semantic_object or evaluator.get("shape") != "scalar":
        raise ValueError("Only a single scalar business value can become a metric")
    if int(evaluator.get("row_index") or 0) != 0:
        raise ValueError("This object selects one row from a dataset and is not a scalar metric")
    definition_sql, context = _render_semantic_sql(
        semantic_object, resolved["source"].get("context") or {}
    )
    observed = None
    if preview:
        observed = _semantic_home_preview(resolved["source"], execution_subject)
        if observed.get("status") == "error":
            raise ValueError(str(observed.get("error") or "The current value could not be read"))
        if observed.get("row_count") not in (None, 1):
            raise ValueError("A governed metric must return exactly one row")
        if not observed.get("value_column"):
            raise ValueError("The metric value column could not be identified")
    meaning = semantic_object.get("meaning") or {}
    title = _semantic_text(meaning.get("label") or resolved.get("title"), 240, required=True)
    description = _semantic_text(
        meaning.get("description") or resolved.get("description")
        or f"Promoted from {dashboard.get('name') or dashboard['slug']}.",
        1400,
        required=True,
    )
    display = dict(semantic_object.get("display") or {})
    display.setdefault("title", title)
    if meaning.get("unit") and not display.get("unit"):
        display["unit"] = meaning["unit"]
    context_copy = _semantic_json_value(context) if context else {}
    context_phrase = ", ".join(
        f"{str(key).replace('_', ' ')}={value}"
        for key, value in list((context_copy or {}).items())[:8]
    )
    grain = (
        f"One scalar value from {dashboard.get('name') or dashboard['slug']} "
        f"version {resolved['version']}"
        + (f", fixed at {context_phrase}" if context_phrase else "")
        + "."
    )
    source_tables = _referenced_tables(definition_sql)
    return {
        "suggested_name": _semantic_home_metric_name(resolved["source"]),
        "title": title,
        "description": description,
        "grain": grain,
        "display": display,
        "formula": meaning.get("formula") or "",
        "unit": meaning.get("unit") or "",
        "definition_sql": definition_sql,
        "value_column": (observed or {}).get("value_column")
        or evaluator.get("value_column") or "value",
        "current": (observed or {}).get("value"),
        "source_tables": source_tables,
        "parameters": semantic_object.get("parameters") or {},
        "source": resolved["source"],
        "source_canonical_key": resolved["canonical_key"],
        "artifact": {
            "slug": dashboard["slug"],
            "name": dashboard.get("name") or dashboard["slug"],
            "version": resolved["version"],
            "latest_version": resolved["latest_version"],
            "object_id": resolved["source"]["object_id"],
            "definition_hash": resolved["source"]["definition_hash"],
            "context": context_copy or {},
        },
    }


def _semantic_home_metric_draft_public(draft):
    """The confirmation payload: descriptive metadata, never client-authored SQL."""
    return {
        key: draft.get(key)
        for key in (
            "suggested_name", "title", "description", "grain", "display", "formula",
            "unit", "current", "source_tables", "artifact",
        )
    }


def _promote_semantic_home_metric(owner, execution_subject, item_id, values=None):
    """Create an append-versioned metric and replace its source Home pin atomically."""
    item_id = str(uuid.UUID(str(item_id)))
    values = values if isinstance(values, dict) else {}
    with _conn() as conn:
        item = conn.execute(
            "SELECT i.*,b.id AS owned_board_id FROM rvbbit.calliope_board_items i "
            "JOIN rvbbit.calliope_boards b ON b.id=i.board_id "
            "WHERE i.id=%s::uuid AND lower(b.owner_email)=lower(%s) AND b.slug='home'",
            (item_id, owner),
        ).fetchone()
    if not item:
        raise LookupError("That Home item is no longer available")
    if item.get("item_kind") != "artifact_object":
        raise ValueError("Only named business objects can become governed metrics")

    # Re-run the immutable definition under the mapped viewer before creating
    # anything. The browser supplies only the desired name/description; SQL,
    # provenance, display metadata, and value-column selection stay server-owned.
    draft = _semantic_home_metric_draft(
        item.get("source") or {}, execution_subject, preview=True
    )
    requested_name = str(values.get("name") or draft["suggested_name"]).strip().lower()
    if not _PROMOTED_METRIC_NAME_RE.fullmatch(requested_name):
        raise ValueError(
            "Metric name must start with a letter and use at most 63 lowercase letters, numbers, or underscores"
        )
    description = _semantic_text(
        values.get("description") or draft["description"], 1400, required=True
    )
    promoted_from = {
        "kind": "artifact_object",
        "canonical_key": draft["source_canonical_key"],
        **draft["artifact"],
        "parameters": draft["parameters"],
    }
    labels = {
        "display": draft["display"],
        "metric_value_column": draft["value_column"],
        "formula": draft["formula"],
        "semantic_kind": "scalar",
        "promoted_from": promoted_from,
    }
    metric_source = {
        "kind": "metric",
        "name": requested_name,
        "params": {},
        "tracking": "latest",
    }
    metric_key = _metric_canonical_key(requested_name, {})
    metric_presentation = {
        "title": draft["title"],
        "description": description,
        "grain": draft["grain"],
        "display": draft["display"],
    }
    created = False
    observation_id = None
    with _conn() as conn:
        with conn.transaction():
            locked_item = conn.execute(
                "SELECT i.*,b.id AS owned_board_id FROM rvbbit.calliope_board_items i "
                "JOIN rvbbit.calliope_boards b ON b.id=i.board_id "
                "WHERE i.id=%s::uuid AND lower(b.owner_email)=lower(%s) AND b.slug='home' "
                "FOR UPDATE OF i",
                (item_id, owner),
            ).fetchone()
            if not locked_item:
                raise LookupError("That Home item is no longer available")
            if (
                locked_item.get("item_kind") != "artifact_object"
                or (locked_item.get("source") or {}) != (item.get("source") or {})
            ):
                raise ValueError("That Home item changed while its metric draft was open; review it again")
            item = locked_item
            # Serialize the name check with define_metric's own lock. Without
            # this pre-check lock, two different Home objects promoted to the
            # same new name could both observe "missing" and append unrelated
            # v1/v2 definitions in a race.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('rvbbit.metric:' || %s,0))",
                (requested_name,),
            )
            existing = conn.execute(
                "SELECT name,version,labels FROM rvbbit.metric_catalog WHERE name=%s",
                (requested_name,),
            ).fetchone()
            if existing:
                existing_origin = (existing.get("labels") or {}).get("promoted_from") or {}
                if existing_origin.get("canonical_key") != draft["source_canonical_key"]:
                    raise FileExistsError(
                        f"A different governed metric already uses the name {requested_name}"
                    )
                metric_version = int(existing["version"])
            else:
                metric_version = int(conn.execute(
                    "SELECT rvbbit.define_metric(%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,NULL) AS version",
                    (
                        requested_name,
                        draft["definition_sql"],
                        json.dumps({}),
                        draft["grain"],
                        description,
                        owner,
                        json.dumps(labels, default=str),
                    ),
                ).fetchone()["version"])
                metric_source["pinned_version"] = metric_version
                observation = conn.execute(
                    "SELECT rvbbit.materialize_metric(%s,%s::jsonb,now(),NULL,NULL,%s) AS id",
                    (requested_name, json.dumps({}), "semantic_home_promotion"),
                ).fetchone()
                observation_id = (observation or {}).get("id")
                created = True
            metric_source["pinned_version"] = metric_version

            board_id = item["owned_board_id"]
            existing_pin = conn.execute(
                "SELECT * FROM rvbbit.calliope_board_items "
                "WHERE board_id=%s::uuid AND canonical_key=%s AND id<>%s::uuid",
                (board_id, metric_key, item_id),
            ).fetchone()
            if existing_pin:
                conn.execute(
                    "DELETE FROM rvbbit.calliope_board_items WHERE id=%s::uuid",
                    (item_id,),
                )
                promoted_item = conn.execute(
                    "UPDATE rvbbit.calliope_board_items SET item_kind='metric',source=%s::jsonb,"
                    "presentation=%s::jsonb,updated_at=now() WHERE id=%s::uuid RETURNING *",
                    (
                        json.dumps(metric_source),
                        json.dumps(metric_presentation, default=str),
                        existing_pin["id"],
                    ),
                ).fetchone()
            else:
                promoted_item = conn.execute(
                    "UPDATE rvbbit.calliope_board_items SET item_kind='metric',canonical_key=%s,"
                    "source=%s::jsonb,presentation=%s::jsonb,updated_at=now() "
                    "WHERE id=%s::uuid RETURNING *",
                    (
                        metric_key,
                        json.dumps(metric_source),
                        json.dumps(metric_presentation, default=str),
                        item_id,
                    ),
                ).fetchone()
            conn.execute(
                "UPDATE rvbbit.calliope_boards SET updated_at=now() WHERE id=%s::uuid",
                (board_id,),
            )
    return {
        "created": created,
        "metric": requested_name,
        "version": metric_version,
        "observation_id": observation_id,
        "open_url": _semantic_home_metric_href(requested_name, {}),
        "item": _semantic_home_public_item(promoted_item),
    }


# ── Semantic Watches ────────────────────────────────────────────────────────
#
# A watch replays the exact semantic-object handle under the authenticated
# database subject, then hands only the observed scalar to an isolated RVBBIT
# alert rule. That preserves Burrow/RLS while reusing the alert reconciler's
# edge detection, consecutive checks, state, queue, and audit trail. Hermes is
# intentionally not in this polling loop; the Work Inbox consumes the events.
_WATCH_CADENCE_SECONDS = {"fast": 60, "normal": 15 * 60, "slow": 60 * 60}
_WATCH_WAKE = threading.Event()
_WATCH_THREAD = None
_WATCH_THREAD_LOCK = threading.Lock()


def _watch_number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("That dashboard value is not numeric")
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except Exception as exc:  # noqa: BLE001 — Decimal has several parse errors
        raise ValueError("That dashboard value is not numeric") from exc
    if not number.is_finite():
        raise ValueError("That dashboard value is not a finite number")
    return number


def _watch_rule_name(watch_id):
    return f"calliope_watch_{uuid.UUID(str(watch_id)).hex}"


def _watch_rule_tier(watch_id):
    return f"calliope:{uuid.UUID(str(watch_id)).hex}"


def _watch_comparison_copy(comparator):
    return "rises to or above" if comparator == "above" else "falls to or below"


def _watch_public(row):
    raw = dict(row or {})
    source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    presentation = raw.get("presentation") if isinstance(raw.get("presentation"), dict) else {}
    threshold = raw.get("threshold")
    last_value = raw.get("last_value")
    return {
        "id": str(raw.get("id")),
        "name": raw.get("name") or presentation.get("title") or "Semantic watch",
        "source": source,
        "presentation": presentation,
        "condition": {
            "comparator": raw.get("comparator"),
            "threshold": float(threshold) if threshold is not None else None,
            "cadence": raw.get("cadence") or "normal",
            "consecutive_n": int(raw.get("consecutive_n") or 1),
            "copy": _watch_comparison_copy(raw.get("comparator")),
        },
        "active": bool(raw.get("active")),
        "current": {
            "value": float(last_value) if last_value is not None else None,
            "status": raw.get("last_status"),
            "evaluated_at": _iso_utc(raw.get("last_evaluated_at")),
            "triggered_at": _iso_utc(raw.get("last_triggered_at")),
            "error": raw.get("last_error"),
        },
        "unread_count": int(raw.get("unread_count") or 0),
        "event_count": int(raw.get("event_count") or 0),
        "created_at": _iso_utc(raw.get("created_at")),
        "updated_at": _iso_utc(raw.get("updated_at")),
    }


def _watch_snapshot(owner, *, slug=None, version=None, object_id=None, limit=100):
    clauses = ["w.owner_email=%s"]
    params = [owner]
    if slug:
        clauses.append("w.source->>'slug'=%s")
        params.append(str(slug))
    if version not in (None, ""):
        clauses.append("w.source->>'version'=%s")
        params.append(str(int(version)))
    if object_id:
        clauses.append("w.source->>'object_id'=%s")
        params.append(str(object_id))
    params.append(max(1, min(int(limit or 100), 200)))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT w.*,coalesce(ev.event_count,0) AS event_count,"
            "coalesce(ev.unread_count,0) AS unread_count "
            "FROM rvbbit.calliope_watches w LEFT JOIN LATERAL ("
            " SELECT count(*) AS event_count,count(*) FILTER (WHERE acknowledged_at IS NULL) AS unread_count "
            " FROM rvbbit.calliope_watch_events e WHERE e.watch_id=w.id"
            ") ev ON true WHERE " + " AND ".join(clauses)
            + " ORDER BY w.active DESC,w.updated_at DESC LIMIT %s",
            tuple(params),
        ).fetchall()
    return {"watches": [_watch_public(row) for row in rows]}


def _watch_semantic_definition(source, execution_subject, *, preview=True):
    resolved = _semantic_home_resolve_handle(source, validate_sql=True)
    if resolved.get("kind") != "artifact_object":
        raise ValueError("Only named, replayable dashboard values can be watched")
    dashboard, _, manifest, _ = _semantic_home_artifact_row(
        resolved["source"]["slug"], resolved["source"]["version"]
    )
    semantic_object = _semantic_object_from_manifest(manifest, {
        "id": resolved["source"]["object_id"],
        "definition_hash": resolved["source"]["definition_hash"],
    })
    evaluator = (semantic_object or {}).get("evaluator") or {}
    if not semantic_object or evaluator.get("shape") != "scalar":
        raise ValueError("This dashboard object is not a single watchable value")
    current = None
    if preview:
        result = _semantic_home_preview(resolved["source"], execution_subject)
        if result.get("status") == "error":
            raise ValueError(str(result.get("error") or "The current value could not be read"))
        current = _watch_number(result.get("value"))
    presentation = {
        "title": resolved.get("title") or "Dashboard value",
        "description": resolved.get("description") or "",
        "formula": resolved.get("formula") or "",
        "unit": resolved.get("unit") or "",
        "display": resolved.get("display") or {},
        "artifact_name": resolved.get("artifact_name") or dashboard.get("name") or resolved["source"]["slug"],
        "open_url": resolved.get("open_url"),
        "thumbnail_url": resolved.get("thumbnail_url"),
    }
    return resolved["source"], presentation, current


def _watch_alert_definition(watch_id, comparator, threshold, consecutive_n):
    watch_uuid = str(uuid.UUID(str(watch_id)))
    # The alert reads a single service-owned observation, never authored SQL.
    # Semantic SQL stays in the versioned artifact and is replayed separately
    # under the mapped user's database role.
    query = (
        "SELECT ''::text AS entity_key,last_value::numeric AS score "
        "FROM rvbbit.calliope_watches "
        f"WHERE id='{watch_uuid}'::uuid AND active AND last_value IS NOT NULL"
    )
    condition = {
        "kind": "sql",
        "query": query,
        "threshold": str(_watch_number(threshold)),
        "compare": "gte" if comparator == "above" else "lte",
    }
    fire_policy = {"consecutive_n": int(consecutive_n), "cooldown_secs": 0}
    return condition, fire_policy


def _define_watch_alert(conn, row):
    row = dict(row)
    condition, fire_policy = _watch_alert_definition(
        row["id"], row["comparator"], row["threshold"], row["consecutive_n"]
    )
    labels = {
        "surface": "calliope",
        "kind": "semantic_watch",
        "watch_id": str(row["id"]),
        "owner_email": row["owner_email"],
    }
    conn.execute(
        "SELECT rvbbit.define_alert(%s,%s::jsonb,%s::jsonb,%s::jsonb,"
        "'aggregate',1,%s,%s,%s,%s::jsonb)",
        (
            row["rule_name"],
            json.dumps(condition),
            json.dumps({"operator": "noop", "watch_id": str(row["id"])}),
            json.dumps(fire_policy),
            _watch_rule_tier(row["id"]),
            f"Calliope watch: {row['name']}",
            row["owner_email"],
            json.dumps(labels),
        ),
    )
    conn.execute(
        "UPDATE rvbbit.alert_control SET enabled=%s,cadence_tier=%s,updated_at=now() "
        "WHERE name=%s",
        (bool(row["active"]), _watch_rule_tier(row["id"]), row["rule_name"]),
    )


def _watch_event_message(row, value, event_kind):
    presentation = row.get("presentation") if isinstance(row.get("presentation"), dict) else {}
    label = presentation.get("title") or row.get("name") or "Dashboard value"
    unit = presentation.get("unit") or ""
    number = _watch_number(value)
    value_text = f"{number.normalize():f}"
    threshold = _watch_number(row.get("threshold"))
    threshold_text = f"{threshold.normalize():f}"
    if event_kind == "recovered":
        return f"{label} is back outside the watched range at {value_text}{(' ' + unit) if unit else ''}."
    return (
        f"{label} {_watch_comparison_copy(row.get('comparator'))} "
        f"{threshold_text}{(' ' + unit) if unit else ''}; current value is {value_text}."
    )


def _watch_inputs(body, *, current=None):
    body = body if isinstance(body, dict) else {}
    comparator = str(body.get("comparator") or "below").strip().lower()
    if comparator not in {"above", "below"}:
        raise ValueError("comparator must be above or below")
    threshold_raw = body.get("threshold", current)
    if threshold_raw in (None, ""):
        raise ValueError("A numeric threshold is required")
    threshold = _watch_number(threshold_raw)
    cadence = str(body.get("cadence") or "normal").strip().lower()
    if cadence not in _WATCH_CADENCE_SECONDS:
        raise ValueError("cadence must be fast, normal, or slow")
    try:
        consecutive_n = int(body.get("consecutive_n") or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("consecutive_n must be an integer") from exc
    if not 1 <= consecutive_n <= 12:
        raise ValueError("consecutive_n must be between 1 and 12")
    return comparator, threshold, cadence, consecutive_n


def _watch_bool(value, *, default):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError("active must be true or false")


def _create_calliope_watch(owner, execution_subject, body):
    body = body if isinstance(body, dict) else {}
    source, presentation, current = _watch_semantic_definition(
        body.get("source") or body, execution_subject, preview=True
    )
    comparator, threshold, cadence, consecutive_n = _watch_inputs(body, current=current)
    name = _semantic_text(body.get("name"), 120) or (
        f"{presentation['title']} {_watch_comparison_copy(comparator)} {threshold.normalize():f}"
    )
    watch_id = str(uuid.uuid4())
    rule_name = _watch_rule_name(watch_id)
    with _conn() as conn:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO rvbbit.calliope_watches "
                "(id,owner_email,execution_subject,name,source,presentation,rule_name,"
                "comparator,threshold,cadence,consecutive_n,last_value) "
                "VALUES (%s::uuid,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    watch_id, owner, execution_subject or owner, name,
                    json.dumps(source, default=str), json.dumps(presentation, default=str),
                    rule_name, comparator, threshold, cadence, consecutive_n, current,
                ),
            ).fetchone()
            _define_watch_alert(conn, row)
    result = _calliope_watch_tick(force_watch_id=watch_id)
    snapshot = _watch_snapshot(owner, slug=source["slug"], version=source["version"], object_id=source["object_id"])
    watch = next(item for item in snapshot["watches"] if item["id"] == watch_id)
    return {"watch": watch, "check": result}


def _update_calliope_watch(owner, watch_id, body):
    watch_id = str(uuid.UUID(str(watch_id)))
    body = body if isinstance(body, dict) else {}
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM rvbbit.calliope_watches WHERE id=%s::uuid AND owner_email=%s",
            (watch_id, owner),
        ).fetchone()
    if not row:
        raise LookupError("No such watch")
    row = dict(row)
    comparator, threshold, cadence, consecutive_n = _watch_inputs({
        "comparator": body.get("comparator", row["comparator"]),
        "threshold": body.get("threshold", row["threshold"]),
        "cadence": body.get("cadence", row["cadence"]),
        "consecutive_n": body.get("consecutive_n", row["consecutive_n"]),
    })
    active = _watch_bool(body.get("active"), default=row["active"])
    name = _semantic_text(body.get("name"), 120) or row["name"]
    changed_condition = (
        comparator != row["comparator"]
        or threshold != row["threshold"]
        or consecutive_n != int(row["consecutive_n"])
    )
    with _conn() as conn:
        with conn.transaction():
            updated = conn.execute(
                "UPDATE rvbbit.calliope_watches SET name=%s,comparator=%s,threshold=%s,"
                "cadence=%s,consecutive_n=%s,active=%s,updated_at=now() "
                "WHERE id=%s::uuid AND owner_email=%s RETURNING *",
                (
                    name, comparator, threshold, cadence, consecutive_n, active,
                    watch_id, owner,
                ),
            ).fetchone()
            if changed_condition:
                _define_watch_alert(conn, updated)
            conn.execute(
                "UPDATE rvbbit.alert_control SET enabled=%s,updated_at=now() WHERE name=%s",
                (active, row["rule_name"]),
            )
    check = _calliope_watch_tick(force_watch_id=watch_id) if active else {"results": []}
    snapshot = _watch_snapshot(owner)
    watch = next(item for item in snapshot["watches"] if item["id"] == watch_id)
    return {"watch": watch, "check": check}


def _delete_calliope_watch(owner, watch_id):
    watch_id = str(uuid.UUID(str(watch_id)))
    with _conn() as conn:
        with conn.transaction():
            row = conn.execute(
                "DELETE FROM rvbbit.calliope_watches WHERE id=%s::uuid AND owner_email=%s "
                "RETURNING rule_name",
                (watch_id, owner),
            ).fetchone()
            if not row:
                raise LookupError("No such watch")
            conn.execute("SELECT rvbbit.delete_alert(%s)", (row["rule_name"],))
    return {"removed": watch_id}


def _calliope_watch_events(owner, *, watch_id=None, unread_only=False, limit=100):
    clauses = ["w.owner_email=%s"]
    params = [owner]
    if watch_id:
        clauses.append("e.watch_id=%s::uuid")
        params.append(str(uuid.UUID(str(watch_id))))
    if unread_only:
        clauses.append("e.acknowledged_at IS NULL")
    params.append(max(1, min(int(limit or 100), 300)))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT e.*,w.name,w.source,w.presentation,w.comparator,w.cadence "
            "FROM rvbbit.calliope_watch_events e "
            "JOIN rvbbit.calliope_watches w ON w.id=e.watch_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY e.created_at DESC,e.event_id DESC LIMIT %s",
            tuple(params),
        ).fetchall()
    return {
        "events": [{
            "id": str(row["event_id"]),
            "watch_id": str(row["watch_id"]),
            "watch_name": row.get("name"),
            "kind": row.get("event_kind"),
            "message": row.get("message"),
            "value": float(row["value"]) if row.get("value") is not None else None,
            "threshold": float(row["threshold"]) if row.get("threshold") is not None else None,
            "source": row.get("source") or {},
            "presentation": row.get("presentation") or {},
            "payload": row.get("payload") or {},
            "created_at": _iso_utc(row.get("created_at")),
            "acknowledged_at": _iso_utc(row.get("acknowledged_at")),
        } for row in rows]
    }


def _reconcile_calliope_watch(watch_id):
    watch_id = str(uuid.UUID(str(watch_id)))
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM rvbbit.calliope_watches WHERE id=%s::uuid AND active",
            (watch_id,),
        ).fetchone()
    if not row:
        return {"watch_id": watch_id, "skipped": True, "reason": "inactive_or_missing"}
    row = dict(row)
    try:
        _source, _presentation, current = _watch_semantic_definition(
            row["source"], row["execution_subject"], preview=True
        )
    except Exception as exc:  # noqa: BLE001 — a failed read is a user-visible watch state
        message = _semantic_text(exc, 800) or "The watched value could not be read"
        with _conn() as conn:
            with conn.transaction():
                locked = conn.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS ok",
                    (row["rule_name"],),
                ).fetchone()["ok"]
                if not locked:
                    return {"watch_id": watch_id, "skipped": True, "reason": "busy"}
                previous = conn.execute(
                    "SELECT last_error FROM rvbbit.calliope_watches WHERE id=%s::uuid FOR UPDATE",
                    (watch_id,),
                ).fetchone()
                conn.execute(
                    "UPDATE rvbbit.calliope_watches SET last_error=%s,last_evaluated_at=now(),updated_at=now() "
                    "WHERE id=%s::uuid",
                    (message, watch_id),
                )
                if not previous or previous.get("last_error") != message:
                    conn.execute(
                        "INSERT INTO rvbbit.calliope_watch_events "
                        "(watch_id,event_kind,message,payload) VALUES (%s::uuid,'error',%s,%s::jsonb)",
                        (watch_id, message, json.dumps({"code": "WATCH_REPLAY_FAILED"})),
                    )
        return {"watch_id": watch_id, "status": "error", "error": message}

    with _conn() as conn:
        with conn.transaction():
            locked = conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS ok",
                (row["rule_name"],),
            ).fetchone()["ok"]
            if not locked:
                return {"watch_id": watch_id, "skipped": True, "reason": "busy"}
            current_row = conn.execute(
                "SELECT * FROM rvbbit.calliope_watches WHERE id=%s::uuid AND active FOR UPDATE",
                (watch_id,),
            ).fetchone()
            if not current_row:
                return {"watch_id": watch_id, "skipped": True, "reason": "inactive_or_missing"}
            current_row = dict(current_row)
            previous_status = current_row.get("last_status")
            conn.execute(
                "UPDATE rvbbit.calliope_watches SET last_value=%s,last_error=NULL,"
                "last_evaluated_at=now(),updated_at=now() WHERE id=%s::uuid",
                (current, watch_id),
            )
            sweep = conn.execute(
                "SELECT rvbbit.alert_sweep(%s) AS result",
                (_watch_rule_tier(watch_id),),
            ).fetchone()["result"]
            pending = conn.execute(
                "SELECT q.queue_id,q.entity_key,q.transition FROM rvbbit.alert_queue q "
                "WHERE q.rule_name=%s AND q.status='pending' ORDER BY q.enqueued_at "
                "FOR UPDATE SKIP LOCKED",
                (current_row["rule_name"],),
            ).fetchall()
            newest_alert_event = int(current_row.get("last_alert_event_id") or 0)
            triggered = 0
            for queued in pending:
                output = {
                    "ok": True,
                    "operator": "calliope_inbox",
                    "watch_id": watch_id,
                }
                alert_event = conn.execute(
                    "INSERT INTO rvbbit.alert_events "
                    "(rule_name,entity_key,transition,action_output,status) "
                    "VALUES (%s,%s,%s,%s::jsonb,'fired') RETURNING event_id,ts",
                    (
                        current_row["rule_name"], queued.get("entity_key") or "",
                        queued.get("transition") or "enter_fail", json.dumps(output),
                    ),
                ).fetchone()
                conn.execute(
                    "UPDATE rvbbit.alert_queue SET status='done',attempts=attempts+1 "
                    "WHERE queue_id=%s",
                    (queued["queue_id"],),
                )
                message = _watch_event_message(current_row, current, "triggered")
                conn.execute(
                    "INSERT INTO rvbbit.calliope_watch_events "
                    "(watch_id,alert_event_id,event_kind,value,threshold,message,payload) "
                    "VALUES (%s::uuid,%s,'triggered',%s,%s,%s,%s::jsonb) "
                    "ON CONFLICT (watch_id,alert_event_id) WHERE alert_event_id IS NOT NULL DO NOTHING",
                    (
                        watch_id, alert_event["event_id"], current, current_row["threshold"], message,
                        json.dumps({
                            "source": current_row["source"],
                            "comparator": current_row["comparator"],
                            "consecutive_n": current_row["consecutive_n"],
                        }, default=str),
                    ),
                )
                newest_alert_event = max(newest_alert_event, int(alert_event["event_id"]))
                triggered += 1
            state = conn.execute(
                "SELECT last_status,score,consecutive,last_changed_at,last_fired_at,updated_at "
                "FROM rvbbit.alert_state WHERE rule_name=%s AND entity_key=''",
                (current_row["rule_name"],),
            ).fetchone()
            status = (state or {}).get("last_status")
            if previous_status == "fail" and status == "pass":
                message = _watch_event_message(current_row, current, "recovered")
                conn.execute(
                    "INSERT INTO rvbbit.calliope_watch_events "
                    "(watch_id,event_kind,value,threshold,message,payload) "
                    "VALUES (%s::uuid,'recovered',%s,%s,%s,%s::jsonb)",
                    (
                        watch_id, current, current_row["threshold"], message,
                        json.dumps({"source": current_row["source"]}, default=str),
                    ),
                )
            conn.execute(
                "UPDATE rvbbit.calliope_watches SET last_status=%s,last_alert_event_id=%s,"
                "last_triggered_at=CASE WHEN %s>0 THEN now() ELSE last_triggered_at END,updated_at=now() "
                "WHERE id=%s::uuid",
                (status, newest_alert_event, triggered, watch_id),
            )
    return {
        "watch_id": watch_id,
        "status": status,
        "value": float(current),
        "triggered": triggered,
        "sweep": sweep,
    }


def _calliope_watch_tick(force_watch_id=None, budget=50):
    if force_watch_id:
        return {"results": [_reconcile_calliope_watch(force_watch_id)]}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id FROM rvbbit.calliope_watches WHERE active AND ("
            "last_evaluated_at IS NULL OR last_evaluated_at <= now() - "
            "CASE cadence WHEN 'fast' THEN interval '60 seconds' "
            "WHEN 'slow' THEN interval '60 minutes' ELSE interval '15 minutes' END"
            ") ORDER BY last_evaluated_at NULLS FIRST,created_at LIMIT %s",
            (max(1, min(int(budget or 50), 200)),),
        ).fetchall()
    return {"results": [_reconcile_calliope_watch(row["id"]) for row in rows]}


def _calliope_watch_worker():
    interval = _env_int("WAREHOUSE_CALLIOPE_WATCH_TICK_SECONDS", 20, minimum=5, maximum=300)
    while True:
        try:
            _calliope_watch_tick(budget=50)
        except Exception as exc:  # noqa: BLE001 — a bad watch cannot stop future checks
            print(f"WARNING: Calliope semantic watch tick failed: {exc}", file=sys.stderr)
        _WATCH_WAKE.wait(interval)
        _WATCH_WAKE.clear()


def _start_calliope_watch_worker():
    global _WATCH_THREAD
    if not _semantic_home_enabled():
        return False
    with _WATCH_THREAD_LOCK:
        if _WATCH_THREAD and _WATCH_THREAD.is_alive():
            return True
        _WATCH_THREAD = threading.Thread(
            target=_calliope_watch_worker,
            name="calliope-semantic-watches",
            daemon=True,
        )
        _WATCH_THREAD.start()
    return True


def _dashboard_version_document(slug, version=None):
    """Load one exact stored dashboard/app document, or the current version.

    Named Home objects intentionally retain the artifact version whose semantic
    definition they came from.  Rendering that URL must therefore read the
    immutable ``dashboard_versions`` row instead of falling through to the
    current live-app runner.
    """
    if not _SEMANTIC_HOME_SLUG_RE.fullmatch(str(slug or "")):
        raise ValueError("artifact slug is invalid")
    try:
        selected = int(version) if version not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ValueError("version must be a positive integer") from exc
    if selected is not None and selected < 1:
        raise ValueError("version must be a positive integer")
    with _conn() as conn:
        dashboard = conn.execute(
            "SELECT id,slug,latest_version,app_kind FROM rvbbit.dashboards WHERE slug=%s",
            (slug,),
        ).fetchone()
        if not dashboard:
            return None
        selected = selected or int(dashboard["latest_version"])
        version_row = conn.execute(
            "SELECT html,manifest FROM rvbbit.dashboard_versions "
            "WHERE dashboard_id=%s AND version=%s",
            (dashboard["id"], selected),
        ).fetchone()
    if not version_row:
        return None
    return {
        "slug": str(dashboard["slug"]),
        "version": selected,
        "latest_version": int(dashboard["latest_version"]),
        "app_kind": str(dashboard.get("app_kind") or "dashboard"),
        "html": _dash_shim(slug, selected, version_row.get("manifest"))
        + (version_row.get("html") or ""),
    }


# ── the landing page: this server's own front door ───────────────────────────
# For the install shape where nobody opens DataRabbit at all — people talk to
# the warehouse through Claude, artifacts get published here, and the links go
# out in chat. Without an index, "find last week's dashboard" means scrolling a
# transcript. The Hub (docs/HUB_PLAN.md) answers the same need but is a
# DataRabbit plate wall and needs lens running; this is the lens-free version,
# served by the same process that serves the artifacts, behind the same session
# cookie that already gates /d/<slug>. A cold link lands on /login and comes
# back here.
#
# Deliberately one server-rendered page: no build step, no framework, no new
# dependency, and no new table — the whole index is one SELECT over
# rvbbit.live_apps plus <img> tags pointed at the self-healing /thumbs route.

_LANDING_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --void:#100d0b; --panel:#171310; --panel-2:#1c1712; --panel-raised:#1d1813;
  --bone:#e8ddcc; --bone-bright:#fff7e9; --fog:rgba(232,221,204,.55); --dim:rgba(232,221,204,.32);
  --line:rgba(232,221,204,.13); --line-hot:rgba(245,180,70,.42);
  --amber:#f5b446; --amber-soft:rgba(245,180,70,.12);
  --jade:#68c7b2; --jade-soft:rgba(104,199,178,.10);
  --gallery-rail-bg:color-mix(in oklch,var(--void) 85%,transparent);
  --mono:ui-monospace,"JetBrains Mono",SFMono-Regular,Menlo,monospace;
  --sans:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --serif:"Iowan Old Style",Baskerville,"Times New Roman",serif;
}
/* The base colour lives on <html> and NOWHERE else. body must stay
   transparent: a negative-z-index layer paints BEFORE in-flow backgrounds, so
   an opaque body background hides .bg completely (it did — measured at zero
   contributed pixels). The warm gradient that used to live here moved into
   .veil, which sits ABOVE the photo. */
html{background:var(--void)}
body{
  min-height:100vh; background:transparent;
  color:var(--bone); font-family:var(--sans); -webkit-font-smoothing:antialiased;
}
a{color:inherit;text-decoration:none}
::selection{background:var(--amber);color:#1a1206}

/* Backdrop, pushed much further back than the login page's — this page is full
   of content and the scene is atmosphere, not subject. */
.bg{position:fixed;inset:0;z-index:-2;background-position:center;background-size:cover;
  background-repeat:no-repeat;filter:saturate(.7) contrast(1.03);pointer-events:none}
.veil{position:fixed;inset:0;z-index:-1;pointer-events:none}

/* the faint grid wash, faded out down the page */
.wash{position:fixed;inset:56px 0 0;z-index:0;opacity:.5;pointer-events:none;
  background-image:linear-gradient(var(--line) 1px,transparent 1px),
                   linear-gradient(90deg,var(--line) 1px,transparent 1px);
  background-size:72px 72px;
  -webkit-mask-image:linear-gradient(to bottom,#000 0%,transparent 70%);
          mask-image:linear-gradient(to bottom,#000 0%,transparent 70%)}

nav{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:12px;
  height:56px;padding:0 max(20px,4vw);border-bottom:1px solid var(--line);
  background:var(--gallery-rail-bg);backdrop-filter:blur(18px)}
.mark{display:block;height:15px;width:auto;color:var(--amber);flex:none}
.wordmark{font:700 12px/1 var(--mono);letter-spacing:.14em}
.wordmark small{margin-left:10px;padding-left:10px;border-left:1px solid var(--line);
  font-weight:400;font-size:9px;letter-spacing:.16em;color:var(--dim)}
.who{margin-left:auto;display:flex;align-items:center;gap:14px;
  font:10px/1 var(--mono);letter-spacing:.1em;color:var(--fog)}
.who a{color:var(--dim)}
.who a:hover{color:var(--amber)}
.inbox-rail-link{
  position:relative;display:inline-flex;align-items:center;gap:7px;height:30px;padding:0 9px;
  border:1px solid var(--line);border-radius:999px;color:var(--fog)!important;
  font:600 7px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;
  transition:border-color .18s,background .18s,color .18s}
.inbox-rail-link::before{
  content:"";width:6px;height:6px;flex:none;border:1px solid var(--dim);border-radius:50%;
  background:transparent;transition:border-color .18s,background .18s,box-shadow .18s}
.inbox-rail-link:hover{border-color:color-mix(in oklch,var(--jade) 42%,var(--line));color:var(--bone-bright)!important}
.inbox-rail-link.has-open{border-color:color-mix(in oklch,var(--jade) 34%,var(--line));background:color-mix(in oklch,var(--jade) 5%,transparent)}
.inbox-rail-link.has-open::before{border-color:var(--jade);background:var(--jade);box-shadow:0 0 9px color-mix(in oklch,var(--jade) 56%,transparent)}
.inbox-rail-link.has-unread{border-color:var(--line-hot);color:var(--bone-bright)!important;background:var(--amber-soft)}
.inbox-rail-link.has-unread::before{border-color:var(--amber);background:var(--amber);box-shadow:0 0 10px color-mix(in oklch,var(--amber) 62%,transparent)}
.inbox-rail-count{min-width:17px;padding:3px 5px;border-radius:999px;background:var(--jade);color:var(--void);
  font:800 7px/1 var(--mono);letter-spacing:0;text-align:center}
.inbox-rail-link.has-unread .inbox-rail-count{background:var(--amber)}
.inbox-rail-count[hidden]{display:none}
.brief-rail-link{
  position:relative;display:inline-flex;align-items:center;gap:7px;height:30px;padding:0 9px;
  border:1px solid color-mix(in oklch,var(--amber) 34%,var(--line));border-radius:999px;
  background:color-mix(in oklch,var(--amber) 5%,transparent);color:var(--fog)!important;
  font:600 7px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;
  transition:border-color .18s,background .18s,color .18s}
.brief-rail-link::before{content:"◐";color:var(--amber);font:10px/1 var(--serif)}
.brief-rail-link:hover,.brief-rail-link.has-brief{border-color:var(--amber);color:var(--bone-bright)!important;background:var(--amber-soft)}
.brief-rail-count{min-width:17px;padding:3px 5px;border-radius:999px;background:var(--amber);color:var(--void);font:800 7px/1 var(--mono);letter-spacing:0;text-align:center}
.brief-rail-count[hidden]{display:none}
.applink{padding:6px 11px;border:1px solid var(--line-hot);color:var(--amber)!important;
  letter-spacing:.12em}
.applink:hover{background:var(--amber);color:#1a1206!important}

.calliope-float{
  --calliope-edge:clamp(18px,2vw,28px);
  --calliope-capsule-bg:color-mix(in oklch,#fffaf1 86%,var(--amber));
  --calliope-capsule-ink:color-mix(in oklch,#100d0b 91%,var(--amber));
  --calliope-capsule-muted:color-mix(in oklch,var(--calliope-capsule-ink) 68%,transparent);
  position:fixed;right:var(--calliope-edge);bottom:var(--calliope-edge);z-index:19;
  display:inline-flex;align-items:center;gap:13px;min-height:64px;
  padding:6px 20px 6px 6px;border:1px solid color-mix(in oklch,var(--amber) 68%,#fff);border-radius:999px;
  background:linear-gradient(135deg,color-mix(in oklch,#fff 62%,var(--calliope-capsule-bg)),var(--calliope-capsule-bg));
  -webkit-backdrop-filter:blur(20px) saturate(1.24);
  backdrop-filter:blur(20px) saturate(1.24);
  box-shadow:0 16px 46px rgba(0,0,0,.46),0 0 0 4px color-mix(in oklch,var(--amber) 9%,transparent),inset 0 1px 0 rgba(255,255,255,.72);
  color:var(--calliope-capsule-ink);transition:transform .2s,border-color .2s,background .2s,box-shadow .2s}
.calliope-float:hover{
  transform:translateY(-3px);border-color:var(--amber);
  background:linear-gradient(135deg,#fff,var(--calliope-capsule-bg));
  box-shadow:0 20px 58px rgba(0,0,0,.52),0 0 0 5px color-mix(in oklch,var(--amber) 14%,transparent),inset 0 1px 0 #fff}
.calliope-float:focus-visible{outline:2px solid var(--amber);outline-offset:3px}
.calliope-float-avatar{
  width:44px;height:44px;flex:none;overflow:hidden;border:1px solid var(--line-hot);
  border-radius:50%;background:var(--panel-raised);
  box-shadow:0 0 0 3px var(--amber-soft),0 7px 20px rgba(0,0,0,.38)}
.calliope-float-avatar[data-period=night]{
  border-color:color-mix(in oklch,var(--jade) 58%,transparent);
  box-shadow:0 0 0 3px var(--jade-soft),0 7px 20px rgba(0,0,0,.42)}
.calliope-float-avatar img{
  display:block;width:100%;height:100%;object-fit:cover;
  transform:scale(1.22);transform-origin:57% 39%}
.calliope-float-name{
  color:var(--calliope-capsule-ink);font-family:"Homemade Apple",cursive;
  font-size:22px;font-weight:650;line-height:1;letter-spacing:-.015em;white-space:nowrap;
  text-shadow:.35px 0 currentColor,-.2px .2px currentColor}
.calliope-float-copy{display:flex;flex-direction:column;align-items:flex-start;gap:5px;padding-top:3px}
.calliope-float-action{color:var(--calliope-capsule-muted);font:7px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;white-space:nowrap}
.calliope-float-action b{margin-left:4px;color:color-mix(in oklch,var(--calliope-capsule-ink) 62%,var(--amber));font-size:10px;font-weight:700}

.gallery-presence{
  position:fixed;z-index:19;top:70px;right:max(20px,4vw);width:min(344px,calc(100vw - 40px));
  overflow:visible;isolation:isolate;border:0;background:transparent;box-shadow:none;
  transition:width .24s ease,transform .24s ease,background .24s ease,box-shadow .24s ease}
.gallery-presence::before{content:"";position:absolute;z-index:-1;inset:-42% -12% auto 24%;height:118px;pointer-events:none;background:radial-gradient(ellipse,color-mix(in oklch,var(--amber) 9%,transparent),transparent 68%);opacity:.8}
.presence-clock{position:relative;z-index:1;display:flex;align-items:center;justify-content:space-between;gap:14px;min-height:70px;padding:11px 16px 10px 18px;border-radius:21px;background:linear-gradient(135deg,color-mix(in oklch,var(--panel) 38%,transparent),color-mix(in oklch,var(--void) 13%,transparent));-webkit-backdrop-filter:blur(28px) saturate(1.28);backdrop-filter:blur(28px) saturate(1.28);box-shadow:0 17px 45px rgba(0,0,0,.18),0 3px 12px rgba(0,0,0,.10),inset 0 1px 0 color-mix(in oklch,var(--bone) 7%,transparent)}
.presence-time-row{display:flex;align-items:baseline;min-width:0;color:var(--bone-bright)}
.presence-time{font:400 35px/.88 var(--serif);font-variant-numeric:tabular-nums lining-nums;letter-spacing:-.025em;text-shadow:0 3px 18px color-mix(in oklch,var(--void) 62%,transparent)}
.presence-time .colon{display:inline-block;margin:0 .035em;color:color-mix(in oklch,var(--amber) 82%,var(--bone));font-weight:300;animation:clock-breathe 2s ease-in-out infinite}
.presence-seconds{margin-left:8px;color:var(--fog);font:650 8px/1 var(--mono);font-variant-numeric:tabular-nums;letter-spacing:.08em}
.presence-period{margin-left:4px;color:var(--dim);font:650 6px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase}
.presence-calendar{min-width:0;text-align:right}
.presence-date{display:block;overflow:hidden;color:var(--fog);font:italic 400 13px/1.15 var(--serif);text-overflow:ellipsis;white-space:nowrap}
.presence-zone{display:block;margin-top:6px;color:var(--dim);font:650 6px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase}
.presence-meeting{position:relative;display:grid;grid-template-columns:7px minmax(0,1fr) auto;align-items:center;gap:10px;min-height:49px;margin-top:6px;padding:9px 15px 10px 18px;border:0;border-radius:16px;background:linear-gradient(100deg,color-mix(in oklch,var(--jade) 6%,transparent),color-mix(in oklch,var(--panel) 27%,transparent) 42%,color-mix(in oklch,var(--void) 9%,transparent));-webkit-backdrop-filter:blur(25px) saturate(1.22);backdrop-filter:blur(25px) saturate(1.22);color:var(--bone);box-shadow:0 12px 34px rgba(0,0,0,.15),inset 0 1px 0 color-mix(in oklch,var(--bone) 5%,transparent);transition:background .18s,box-shadow .18s,transform .18s}
.presence-meeting[hidden]{display:none}.presence-meeting:hover{transform:translateY(-1px);background:linear-gradient(100deg,color-mix(in oklch,var(--jade) 11%,transparent),color-mix(in oklch,var(--panel) 34%,transparent) 48%,color-mix(in oklch,var(--void) 12%,transparent));box-shadow:0 15px 38px rgba(0,0,0,.18),inset 0 1px 0 color-mix(in oklch,var(--jade) 12%,transparent)}
.presence-meeting-dot{width:6px;height:6px;border:1px solid var(--jade);border-radius:50%;box-shadow:0 0 0 3px color-mix(in oklch,var(--jade) 7%,transparent)}
.presence-meeting-copy{min-width:0;display:flex;flex-direction:column;gap:5px}.presence-meeting-copy small{overflow:hidden;color:var(--jade);font:700 6px/1 var(--mono);letter-spacing:.11em;text-overflow:ellipsis;text-transform:uppercase;white-space:nowrap}.presence-meeting-copy strong{overflow:hidden;color:var(--bone-bright);font:italic 400 14px/1.12 var(--serif);text-overflow:ellipsis;white-space:nowrap}
.presence-meeting-time{display:flex;align-items:flex-end;flex-direction:column;gap:5px;color:var(--fog);font:650 8px/1 var(--mono);font-variant-numeric:tabular-nums}.presence-meeting-time small{color:var(--dim);font-size:5px;letter-spacing:.06em;text-transform:uppercase}
.gallery-presence.soon .presence-meeting-dot{border-color:var(--amber);background:var(--amber);box-shadow:0 0 11px color-mix(in oklch,var(--amber) 60%,transparent)}.gallery-presence.soon .presence-meeting-copy small{color:var(--amber)}
.gallery-presence.live .presence-meeting-dot{border-color:var(--jade);background:var(--jade);animation:meeting-pulse 1.7s ease-in-out infinite}.gallery-presence.live .presence-meeting-copy small{color:var(--jade)}
.gallery-presence.compact{width:183px;background:transparent;box-shadow:none}.gallery-presence.compact .presence-calendar,.gallery-presence.compact .presence-meeting{display:none}.gallery-presence.compact .presence-clock{min-height:52px;padding-block:9px;background:linear-gradient(135deg,color-mix(in oklch,var(--panel) 34%,transparent),color-mix(in oklch,var(--void) 10%,transparent));box-shadow:0 12px 34px rgba(0,0,0,.15),inset 0 1px 0 color-mix(in oklch,var(--bone) 6%,transparent)}.gallery-presence.compact .presence-time{font-size:28px}.gallery-presence.compact .presence-seconds{font-size:7px}
@keyframes clock-breathe{0%,100%{opacity:.45}50%{opacity:1}}@keyframes meeting-pulse{50%{box-shadow:0 0 0 5px color-mix(in oklch,var(--jade) 8%,transparent),0 0 12px color-mix(in oklch,var(--jade) 55%,transparent)}}

main{position:relative;z-index:1;padding:0 max(20px,4vw) 90px}
header.hero{padding:66px 0 30px;border-bottom:1px solid var(--line)}
.kicker{color:var(--amber);font:10px/1.3 var(--mono);letter-spacing:.19em;text-transform:uppercase}
h1{margin-top:16px;font-size:clamp(38px,5.4vw,68px);line-height:.92;
  letter-spacing:-.045em;font-weight:600}
h1 em{color:var(--amber);font-family:var(--serif);font-weight:400;font-style:italic;letter-spacing:-.02em}
.tally{margin-top:18px;color:var(--fog);font:10px/1 var(--mono);letter-spacing:.13em;text-transform:uppercase}

.semantic-home{padding:19px 0 20px;border-bottom:1px solid var(--line)}
.semantic-home[hidden]{display:none}
.home-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:12px}
.home-title{display:flex;align-items:center;gap:11px;min-width:0}
.home-title-mark{width:31px;height:31px;display:grid;place-items:center;flex:none;border:1px solid color-mix(in oklch,var(--jade) 38%,var(--line));
  border-radius:50%;background:color-mix(in oklch,var(--jade) 7%,transparent);color:var(--jade);font:16px/1 var(--serif)}
.home-title-copy{display:flex;flex-direction:column;gap:3px;min-width:0}
.home-title-copy strong{color:var(--bone-bright);font:italic 400 19px/1 var(--serif)}
.home-title-copy small{overflow:hidden;color:var(--dim);font:7px/1.35 var(--mono);letter-spacing:.1em;text-overflow:ellipsis;text-transform:uppercase;white-space:nowrap}
.home-status{min-height:11px;color:var(--dim);font:7px/1.35 var(--mono);letter-spacing:.08em;text-align:right;text-transform:uppercase}
.home-status.error{color:#f2a28f}
.home-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:8px}
.home-empty{display:flex;align-items:center;gap:11px;min-height:62px;padding:11px 13px;border:1px dashed color-mix(in oklch,var(--jade) 25%,var(--line));
  background:color-mix(in oklch,var(--void) 60%,transparent);color:var(--fog)}
.home-empty[hidden]{display:none}
.home-empty b{color:var(--jade);font:italic 400 16px/1.2 var(--serif)}
.home-empty span{font-size:9px;line-height:1.45}
.home-tile{position:relative;min-width:0;overflow:hidden;border:1px solid color-mix(in oklch,var(--bone) 11%,transparent);
  background:linear-gradient(145deg,color-mix(in oklch,var(--panel-raised) 92%,transparent),color-mix(in oklch,var(--void) 88%,transparent));
  box-shadow:inset 0 1px 0 rgba(255,255,255,.025);transition:border-color .18s,transform .18s,background .18s}
.home-tile:hover{transform:translateY(-1px);border-color:color-mix(in oklch,var(--jade) 34%,var(--line));background:linear-gradient(145deg,color-mix(in oklch,var(--panel-raised) 96%,var(--jade) 4%),var(--void))}
.home-tile.object::before{content:"";position:absolute;z-index:2;inset:0 auto 0 0;width:2px;background:var(--jade);opacity:.72}
.home-tile.metric{--home-metric-trend:var(--jade);--home-metric-floor:var(--void)}
.home-tile.metric::before{content:"";position:absolute;z-index:2;inset:0 auto 0 0;width:2px;background:var(--amber);opacity:.78}
.home-tile.unavailable{opacity:.66}
.home-tile-content{display:grid;grid-template-columns:74px minmax(0,1fr);min-height:132px}
.home-tile.object .home-tile-content,.home-tile.metric .home-tile-content{grid-template-columns:1fr}
.home-thumb{position:relative;display:block;min-height:100%;overflow:hidden;isolation:isolate;background:#0d0b09;color:var(--amber)}
.home-thumb-fallback{position:absolute;z-index:0;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:7px;
  background:radial-gradient(circle at 42% 42%,color-mix(in oklch,var(--amber) 7%,transparent),transparent 64%);transition:opacity .25s}
.home-thumb-fallback b{color:var(--amber);font:italic 400 25px/1 var(--serif);opacity:.28}
.home-thumb-fallback small{color:var(--dim);font:600 5px/1 var(--mono);letter-spacing:.09em;text-transform:uppercase}
.home-thumb.pending .home-thumb-fallback b{animation:breathe 1.9s ease-in-out infinite}
.home-thumb img{position:absolute;z-index:1;inset:0;width:100%;height:100%;object-fit:cover;object-position:top center;visibility:hidden;opacity:0;transition:opacity .2s,transform .35s}
.home-thumb.ready img{visibility:visible;opacity:.67}
.home-thumb.ready .home-thumb-fallback{opacity:0}
.home-tile:hover .home-thumb.ready img{opacity:.88;transform:scale(1.035)}
.home-thumb::after{content:"";position:absolute;z-index:2;inset:0;background:linear-gradient(90deg,transparent 45%,var(--panel-raised) 100%);pointer-events:none}
.home-tile-main{display:flex;flex-direction:column;min-width:0;padding:12px 12px 10px}
.home-tile.object .home-tile-main,.home-tile.metric .home-tile-main{position:relative;z-index:1;padding-left:14px}
.home-kicker{display:flex;align-items:center;gap:7px;margin-bottom:6px;color:var(--jade);font:650 6px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase}
.home-kicker i{width:5px;height:5px;border-radius:50%;background:currentColor;box-shadow:0 0 9px color-mix(in oklch,currentColor 55%,transparent)}
.home-tile.artifact .home-kicker{color:var(--amber)}
.home-tile.metric .home-kicker{color:var(--amber)}
.home-metric-spark{position:absolute;z-index:0;inset:28px 0 26px;opacity:.52;pointer-events:none}.home-metric-spark svg{display:block;width:100%;height:100%}.home-metric-area-top{stop-color:var(--home-metric-trend);stop-opacity:.24}.home-metric-area-mid{stop-color:var(--home-metric-trend);stop-opacity:.07}.home-metric-area-clear{stop-color:var(--home-metric-floor);stop-opacity:0}.home-metric-area-floor{stop-color:var(--home-metric-floor);stop-opacity:.96}.home-metric-line{fill:none;stroke:var(--home-metric-trend);stroke-width:2;vector-effect:non-scaling-stroke;filter:drop-shadow(0 0 6px color-mix(in oklch,var(--home-metric-trend) 38%,transparent))}.home-tile.metric .home-tile-main{text-shadow:0 1px 1px color-mix(in oklch,var(--void) 98%,transparent),0 2px 6px color-mix(in oklch,var(--void) 94%,transparent),0 7px 18px color-mix(in oklch,var(--void) 80%,transparent)}.home-tile.metric .home-value{font-size:28px}
.home-tile h3{overflow:hidden;color:var(--bone-bright);font:italic 400 16px/1.17 var(--serif);text-overflow:ellipsis;white-space:nowrap}
.home-tile-desc{display:-webkit-box;overflow:hidden;margin-top:5px;color:var(--fog);font-size:8px;line-height:1.45;-webkit-box-orient:vertical;-webkit-line-clamp:2}
.home-value{min-height:25px;margin:4px 0 1px;color:var(--bone-bright);font:600 22px/1.05 var(--sans);letter-spacing:-.025em}
.home-value.loading{color:var(--dim);font:8px/25px var(--mono);letter-spacing:.08em;text-transform:uppercase}
.home-value.error{color:#f2a28f;font:8px/1.35 var(--mono);letter-spacing:.04em}
.home-context{display:flex;gap:4px;overflow:hidden;margin-top:6px}
.home-context span{overflow:hidden;padding:3px 5px;border:1px solid color-mix(in oklch,var(--jade) 20%,var(--line));border-radius:999px;
  color:var(--fog);font:6px/1 var(--mono);text-overflow:ellipsis;white-space:nowrap}
.home-trail{display:flex;align-items:center;gap:4px;overflow:hidden;margin-top:auto;padding-top:8px}
.home-trail::before{content:"TRAIL";flex:none;margin-right:2px;color:var(--dim);font:600 5px/1 var(--mono);letter-spacing:.12em}
.home-crumb{display:inline-flex;align-items:center;gap:4px;min-width:0;max-width:128px;padding:3px 5px;border:1px solid var(--line);border-radius:999px;
  color:var(--fog);font:6px/1 var(--mono);white-space:nowrap}
.home-crumb:not(:last-child)::after{content:"›";position:relative;right:-9px;color:var(--jade)}
.home-crumb span{overflow:hidden;text-overflow:ellipsis}
.home-tile-actions{display:flex;align-items:center;justify-content:flex-end;gap:6px;flex-wrap:wrap;padding:7px 9px;border-top:1px solid var(--line);background:rgba(0,0,0,.12)}
.home-tile-actions a,.home-tile-actions button{padding:5px 8px;border:1px solid var(--line);background:transparent;color:var(--fog);
  font:6px/1 var(--mono);letter-spacing:.09em;text-transform:uppercase;cursor:pointer}
.home-tile-actions a:hover{border-color:var(--line-hot);color:var(--amber)}
.home-tile-actions button[data-home-trail]:hover{border-color:color-mix(in oklch,var(--jade) 52%,var(--line));color:var(--jade)}
.home-tile-actions button:hover{border-color:color-mix(in oklch,#ef8178 52%,var(--line));color:#ef9b91}
.home-tile-actions button[data-home-promote]:hover{border-color:var(--amber);background:var(--amber-soft);color:var(--amber)}
.home-version-note{margin-right:auto;color:var(--amber);font:6px/1.2 var(--mono);letter-spacing:.06em;text-transform:uppercase}

[data-gallery-tooltip]{cursor:help}[data-gallery-tooltip]:focus-visible{outline:1px solid color-mix(in oklch,var(--amber) 68%,var(--jade));outline-offset:2px}.home-crumb[data-gallery-tooltip],.home-context span[data-gallery-tooltip],.home-value[data-gallery-tooltip]{transition:border-color .15s,box-shadow .15s,filter .15s}.home-crumb[data-gallery-tooltip]:hover,.home-context span[data-gallery-tooltip]:hover{border-color:color-mix(in oklch,var(--jade) 48%,var(--line));box-shadow:inset 0 -1px 0 color-mix(in oklch,var(--jade) 46%,transparent)}.home-value[data-gallery-tooltip]:hover{filter:brightness(1.08)}
.gallery-tooltip{--gallery-tooltip-accent:color-mix(in oklch,var(--amber) 55%,var(--jade));--gallery-tooltip-bg:color-mix(in oklch,var(--block-bg,var(--panel)) 96%,var(--background,var(--void)));position:fixed;z-index:100000;width:min(342px,calc(100vw - 24px));padding:13px 14px 14px;border:1px solid color-mix(in oklch,var(--gallery-tooltip-accent) 48%,var(--block-border,var(--line)));background:var(--gallery-tooltip-bg);color:var(--foreground,var(--bone));box-shadow:0 18px 50px color-mix(in oklch,var(--overlay,#000) 42%,transparent),inset 0 0 0 1px color-mix(in oklch,var(--foreground,var(--bone)) 4%,transparent);opacity:0;pointer-events:none;transform:translateY(4px);transition:opacity .13s ease,transform .13s ease}.gallery-tooltip[hidden]{display:none}.gallery-tooltip.visible{opacity:1;transform:translateY(0)}.gallery-tooltip[data-placement="bottom"]{transform:translateY(-4px)}.gallery-tooltip[data-placement="bottom"].visible{transform:translateY(0)}.gallery-tooltip[data-tooltip-kind="trail"],.gallery-tooltip[data-tooltip-kind="value"]{--gallery-tooltip-accent:var(--jade)}.gallery-tooltip[data-tooltip-kind="context"]{--gallery-tooltip-accent:var(--amber)}.gallery-tooltip::after{content:"";position:absolute;left:var(--gallery-tooltip-anchor,calc(50% - 5px));bottom:-5px;width:8px;height:8px;border-right:1px solid color-mix(in oklch,var(--gallery-tooltip-accent) 48%,var(--block-border,var(--line)));border-bottom:1px solid color-mix(in oklch,var(--gallery-tooltip-accent) 48%,var(--block-border,var(--line)));background:var(--gallery-tooltip-bg);transform:rotate(45deg)}.gallery-tooltip[data-placement="bottom"]::after{top:-5px;bottom:auto;border:0;border-top:1px solid color-mix(in oklch,var(--gallery-tooltip-accent) 48%,var(--block-border,var(--line)));border-left:1px solid color-mix(in oklch,var(--gallery-tooltip-accent) 48%,var(--block-border,var(--line)))}.gallery-tooltip-head{display:flex;align-items:center;gap:9px}.gallery-tooltip-head span{overflow:hidden;color:var(--gallery-tooltip-accent);font:750 6px/1 var(--mono);letter-spacing:.11em;text-overflow:ellipsis;text-transform:uppercase;white-space:nowrap}.gallery-tooltip-head b{margin-left:auto;padding:4px 6px;border:1px solid color-mix(in oklch,var(--gallery-tooltip-accent) 34%,var(--block-border,var(--line)));color:var(--gallery-tooltip-accent);font:700 6px/1 var(--mono);letter-spacing:.07em;text-transform:uppercase}.gallery-tooltip-head b.error,.gallery-tooltip-head b.unavailable{border-color:color-mix(in oklch,#df765f 44%,var(--block-border,var(--line)));color:#df765f}.gallery-tooltip-title{display:block;margin-top:9px;color:var(--foreground,var(--bone));font:italic 500 19px/1.15 var(--serif)}.gallery-tooltip-section{margin-top:10px}.gallery-tooltip-section>span{display:block;margin-bottom:4px;color:var(--chrome-text,var(--dim));font:700 6px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase}.gallery-tooltip-section>p{margin:0;color:var(--chrome-text,var(--fog));font:9px/1.5 var(--sans)}.gallery-tooltip-section.evidence{margin:11px -14px -14px;padding:10px 14px 12px;border-top:1px solid color-mix(in oklch,var(--gallery-tooltip-accent) 24%,var(--block-border,var(--line)));background:color-mix(in oklch,var(--gallery-tooltip-accent) 5%,transparent)}.gallery-tooltip-section.evidence>span{color:var(--gallery-tooltip-accent)}.gallery-tooltip-facts{display:flex;gap:5px;flex-wrap:wrap;margin:10px 0 0}.gallery-tooltip-facts div{min-width:0;display:flex;align-items:center;gap:5px;padding:5px 6px;border:1px solid var(--block-border,var(--line));background:color-mix(in oklch,var(--doc-bg,var(--void)) 52%,transparent);font:6px/1.2 var(--mono)}.gallery-tooltip-facts dt{color:var(--chrome-text,var(--dim));text-transform:uppercase}.gallery-tooltip-facts dd{overflow:hidden;max-width:220px;margin:0;color:var(--foreground,var(--fog));text-overflow:ellipsis;white-space:nowrap}

.trail-dialog{width:min(860px,calc(100vw - 32px));max-width:none;height:min(760px,calc(100dvh - 32px));max-height:none;margin:auto;padding:0;
  border:1px solid var(--line-hot);background:color-mix(in oklch,var(--panel) 92%,transparent);color:var(--bone);
  box-shadow:0 34px 120px color-mix(in oklch,var(--void) 88%,transparent);color-scheme:dark}
.trail-dialog::backdrop{background:color-mix(in oklch,var(--void) 74%,transparent);backdrop-filter:blur(8px)}
.trail-shell{height:100%;display:grid;grid-template-rows:auto minmax(0,1fr)}
.trail-head{display:flex;align-items:center;gap:12px;min-height:64px;padding:10px 13px 10px 17px;border-bottom:1px solid var(--line);
  background:color-mix(in oklch,var(--gallery-rail-bg) 92%,transparent);backdrop-filter:blur(20px)}
.trail-back,.trail-close{width:34px;height:34px;display:grid;place-items:center;flex:none;border:1px solid var(--line);background:transparent;color:var(--fog);cursor:pointer}
.trail-back:hover,.trail-close:hover{border-color:var(--line-hot);color:var(--amber)}
.trail-back[hidden]{display:none}
.trail-head-copy{min-width:0;display:flex;flex:1;flex-direction:column;gap:4px}
.trail-head-copy strong{overflow:hidden;color:var(--bone-bright);font:italic 400 20px/1.1 var(--serif);text-overflow:ellipsis;white-space:nowrap}
.trail-head-copy small{overflow:hidden;color:var(--dim);font:7px/1.3 var(--mono);letter-spacing:.08em;text-overflow:ellipsis;text-transform:uppercase;white-space:nowrap}
.trail-content{min-height:0;overflow:auto;padding:18px;scrollbar-color:var(--line-hot) color-mix(in oklch,var(--void) 65%,transparent)}
.trail-loom{position:static;z-index:auto;display:block;height:auto;margin-bottom:14px;padding:12px;border:1px solid color-mix(in oklch,var(--amber) 28%,var(--line));background:color-mix(in oklch,var(--void) 32%,transparent);backdrop-filter:none}
.trail-loom>header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:9px}.trail-loom>header span{color:var(--amber);font:700 7px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase}.trail-loom>header b{color:var(--dim);font:500 6px/1.2 var(--mono);letter-spacing:.06em;text-align:right;text-transform:uppercase}
.trail-loom-track{display:flex;align-items:stretch;overflow-x:auto;padding-bottom:3px;scrollbar-color:var(--line-hot) transparent;scroll-snap-type:x proximity}
.trail-loom-step{position:relative;width:184px;min-height:104px;display:flex;align-items:flex-start;flex:0 0 184px;flex-direction:column;padding:10px;border:1px solid var(--line);background:color-mix(in oklch,var(--panel) 70%,transparent);color:var(--fog);text-align:left;cursor:pointer;scroll-snap-align:start}.trail-loom-step:hover{border-color:color-mix(in oklch,var(--jade) 42%,var(--line))}.trail-loom-step.current{border-color:color-mix(in oklch,var(--jade) 62%,var(--line));background:color-mix(in oklch,var(--jade) 8%,var(--panel))}
.trail-loom-step small{color:var(--jade);font:700 6px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase}.trail-loom-step strong{display:-webkit-box;overflow:hidden;margin-top:7px;color:var(--bone-bright);font:italic 400 15px/1.15 var(--serif);-webkit-box-orient:vertical;-webkit-line-clamp:2}.trail-loom-step>span{margin-top:6px;color:var(--dim);font:6px/1.2 var(--mono);text-transform:uppercase}.trail-loom-step em{display:flex;flex-wrap:wrap;gap:3px;margin-top:7px;font-style:normal}.trail-loom-step em i{padding:3px 4px;border:1px solid var(--line);color:var(--fog);font:5px/1 var(--mono);font-style:normal}.trail-loom-step u{position:absolute;right:7px;bottom:6px;color:var(--jade);font:700 5px/1 var(--mono);letter-spacing:.08em;text-decoration:none;text-transform:uppercase}
.trail-loom-link{width:88px;min-width:88px;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:6px;padding:8px;color:var(--dim);text-align:center}.trail-loom-link b{font:600 6px/1.25 var(--mono);letter-spacing:.05em;text-transform:uppercase}.trail-loom-link i{color:var(--amber);font:400 15px/1 var(--serif);font-style:normal}
.trail-loading,.trail-error,.trail-empty{min-height:220px;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:10px;color:var(--dim);font:9px/1.5 var(--mono);text-align:center}
.trail-loading i{width:22px;height:22px;border:1px solid var(--jade);border-right-color:transparent;border-radius:50%;animation:semantic-spin .8s linear infinite}
.trail-subject{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:14px;padding:15px;border:1px solid color-mix(in oklch,var(--jade) 34%,var(--line));background:color-mix(in oklch,var(--jade) 5%,transparent)}
.trail-subject span,.trail-section>h3{color:var(--jade);font:650 7px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase}
.trail-subject h2{margin:7px 0 4px;color:var(--bone-bright);font:italic 400 25px/1.08 var(--serif)}
.trail-subject p{max-width:680px;color:var(--fog);font-size:11px;line-height:1.5}
.trail-subject a{align-self:start;padding:7px 9px;border:1px solid var(--line-hot);color:var(--amber);font:7px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase}
.trail-facts{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.trail-facts span{max-width:100%;padding:5px 7px;border:1px solid var(--line);color:var(--fog);font:7px/1.3 var(--mono);text-transform:none;letter-spacing:0}
.trail-facts b{margin-right:6px;color:var(--dim);font-weight:500;text-transform:uppercase;letter-spacing:.06em}
.trail-section{margin-top:19px}
.trail-section>h3{display:flex;align-items:center;gap:8px;margin-bottom:8px}.trail-section>h3::after{content:"";height:1px;flex:1;background:var(--line)}
.trail-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:7px}
.trail-card{min-width:0;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;padding:11px;border:1px solid var(--line);background:color-mix(in oklch,var(--void) 38%,transparent)}
.trail-card:hover{border-color:color-mix(in oklch,var(--jade) 30%,var(--line))}
.trail-card-copy{min-width:0}.trail-card-copy>span{display:block;color:var(--jade);font:650 6px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase}
.trail-card-copy strong{display:block;overflow:hidden;margin:6px 0 3px;color:var(--bone-bright);font:italic 400 16px/1.15 var(--serif);text-overflow:ellipsis;white-space:nowrap}
.trail-card-copy p{display:-webkit-box;overflow:hidden;color:var(--dim);font-size:8px;line-height:1.4;-webkit-box-orient:vertical;-webkit-line-clamp:2}
.trail-route-context{display:inline-block;margin-top:7px;padding:4px 5px;border:1px solid color-mix(in oklch,var(--amber) 36%,var(--line));color:var(--amber);font:600 5px/1 var(--mono);font-style:normal;letter-spacing:.06em;text-transform:uppercase}.trail-route-context.return{border-color:color-mix(in oklch,var(--jade) 35%,var(--line));color:var(--jade)}
.trail-shared{display:flex;gap:3px;overflow:hidden;margin-top:7px}.trail-shared i{overflow:hidden;padding:3px 5px;border:1px solid var(--line);border-radius:999px;color:var(--fog);font:5px/1 var(--mono);font-style:normal;text-overflow:ellipsis;white-space:nowrap}
.trail-card-actions{display:flex;align-items:flex-end;flex-direction:column;gap:5px}.trail-card-actions button,.trail-card-actions a{padding:5px 7px;border:1px solid var(--line);background:transparent;color:var(--fog);font:6px/1 var(--mono);cursor:pointer;text-transform:uppercase;white-space:nowrap}
.trail-card-actions button:hover{border-color:var(--jade);color:var(--jade)}.trail-card-actions a:hover{border-color:var(--amber);color:var(--amber)}

.metric-promote-dialog{width:min(640px,calc(100vw - 32px));max-width:none;margin:auto;padding:0;border:1px solid color-mix(in oklch,var(--amber) 58%,var(--line));
  background:color-mix(in oklch,var(--panel) 95%,transparent);color:var(--bone);box-shadow:0 34px 120px color-mix(in oklch,var(--void) 88%,transparent);color-scheme:dark}
.metric-promote-dialog::backdrop{background:color-mix(in oklch,var(--void) 74%,transparent);backdrop-filter:blur(8px)}
.metric-promote-shell{display:flex;flex-direction:column;max-height:calc(100dvh - 32px)}
.metric-promote-head{display:flex;align-items:center;gap:13px;min-height:68px;padding:12px 13px 12px 18px;border-bottom:1px solid var(--line);background:color-mix(in oklch,var(--gallery-rail-bg) 94%,transparent);backdrop-filter:blur(20px)}
.metric-promote-mark{width:34px;height:34px;display:grid;place-items:center;flex:none;border:1px solid color-mix(in oklch,var(--amber) 48%,var(--line));border-radius:50%;color:var(--amber);font:italic 400 18px/1 var(--serif)}
.metric-promote-heading{min-width:0;display:flex;flex:1;flex-direction:column;gap:4px}.metric-promote-heading strong{overflow:hidden;color:var(--bone-bright);font:italic 400 20px/1.1 var(--serif);text-overflow:ellipsis;white-space:nowrap}.metric-promote-heading small{color:var(--dim);font:7px/1.3 var(--mono);letter-spacing:.08em;text-transform:uppercase}
.metric-promote-close{width:34px;height:34px;display:grid;place-items:center;flex:none;border:1px solid var(--line);background:transparent;color:var(--fog);cursor:pointer}.metric-promote-close:hover{border-color:var(--amber);color:var(--amber)}
.metric-promote-body{min-height:220px;overflow:auto;padding:18px}.metric-promote-loading{min-height:210px;display:grid;place-items:center;color:var(--dim);font:8px/1.4 var(--mono);letter-spacing:.08em;text-transform:uppercase}.metric-promote-loading[hidden],.metric-promote-form[hidden]{display:none}
.metric-promote-source{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;margin-bottom:16px;padding:12px;border:1px solid color-mix(in oklch,var(--jade) 28%,var(--line));background:color-mix(in oklch,var(--jade) 4%,transparent)}.metric-promote-source span{color:var(--jade);font:650 6px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase}.metric-promote-source strong{display:block;margin-top:6px;color:var(--bone-bright);font:italic 400 18px/1.15 var(--serif)}.metric-promote-source p{margin-top:5px;color:var(--fog);font-size:9px;line-height:1.45}.metric-promote-current{align-self:start;color:var(--bone-bright);font:650 20px/1 var(--sans);letter-spacing:-.03em;text-align:right}
.metric-promote-field{display:flex;flex-direction:column;gap:6px;margin-top:12px}.metric-promote-field>span{color:var(--dim);font:650 6px/1 var(--mono);letter-spacing:.11em;text-transform:uppercase}.metric-promote-field input,.metric-promote-field textarea{width:100%;padding:10px 11px;border:1px solid var(--line);background:color-mix(in oklch,var(--void) 58%,transparent);color:var(--bone-bright);font:10px/1.4 var(--mono);outline:none;resize:vertical}.metric-promote-field textarea{min-height:82px;font-family:var(--sans);font-size:11px}.metric-promote-field input:focus,.metric-promote-field textarea:focus{border-color:var(--amber)}
.metric-promote-facts{display:flex;flex-wrap:wrap;gap:5px;margin-top:13px}.metric-promote-facts span{max-width:100%;overflow:hidden;padding:5px 7px;border:1px solid var(--line);color:var(--fog);font:6px/1.2 var(--mono);text-overflow:ellipsis;white-space:nowrap}.metric-promote-error{min-height:12px;margin-top:11px;color:#f2a28f;font:8px/1.4 var(--mono)}
.metric-promote-actions{display:flex;justify-content:flex-end;gap:7px;margin-top:15px}.metric-promote-actions button{min-height:32px;padding:0 12px;border:1px solid var(--line);background:transparent;color:var(--fog);font:650 7px/1 var(--mono);letter-spacing:.09em;text-transform:uppercase;cursor:pointer}.metric-promote-actions button[type=submit]{border-color:var(--amber);background:var(--amber);color:#1a1206}.metric-promote-actions button:disabled{opacity:.5;cursor:wait}

.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  padding:16px 0;border-bottom:1px solid var(--line)}
#q{flex:1;min-width:220px;padding:9px 12px;border:1px solid var(--line);
  background:rgba(232,221,204,.04);color:var(--bone);
  font:12px/1 var(--mono);letter-spacing:.04em;outline:none}
#q:focus{border-color:var(--line-hot)}
#q::placeholder{color:var(--dim)}
.chip{padding:8px 13px;border:1px solid var(--line);background:transparent;color:var(--fog);
  font:9px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;cursor:pointer}
.chip:hover{color:var(--bone);border-color:var(--line-hot)}
.chip[aria-pressed=true]{color:#1a1206;background:var(--amber);border-color:var(--amber);font-weight:700}

.semantic-launch{padding:10px 0 12px;border-bottom:1px solid var(--line)}
.semantic-launch[hidden]{display:none}
.semantic-launch-button{width:100%;min-height:68px;display:grid;grid-template-columns:42px minmax(0,1fr) auto;
  align-items:center;gap:14px;padding:10px 13px;border:1px solid color-mix(in oklch,var(--jade) 30%,var(--line));
  background:color-mix(in oklch,var(--void) 72%,transparent);color:var(--bone);text-align:left;cursor:pointer;
  box-shadow:inset 0 1px 0 color-mix(in oklch,var(--bone) 3%,transparent);
  transition:border-color .18s,background .18s,transform .18s,box-shadow .18s}
.semantic-launch-button:hover,.semantic-launch-button:focus-visible{border-color:color-mix(in oklch,var(--jade) 72%,var(--amber));
  outline:0;background:color-mix(in oklch,var(--jade) 8%,var(--void));transform:translateY(-1px);
  box-shadow:0 11px 30px rgba(0,0,0,.24),inset 0 1px 0 color-mix(in oklch,var(--bone) 5%,transparent)}
.semantic-launch-mark{width:42px;height:42px;display:grid;place-items:center;border:1px solid color-mix(in oklch,var(--jade) 48%,transparent);
  border-radius:50%;background:color-mix(in oklch,var(--jade) 8%,transparent);color:var(--jade);
  font:italic 400 22px/1 var(--serif);box-shadow:0 0 0 4px color-mix(in oklch,var(--jade) 4%,transparent)}
.semantic-launch-copy{min-width:0;display:flex;flex-direction:column;gap:6px}
.semantic-launch-copy strong{overflow:hidden;color:var(--bone-bright);font:italic 400 18px/1.15 var(--serif);text-overflow:ellipsis;white-space:nowrap}
.semantic-launch-copy small{color:var(--dim);font:8px/1.4 var(--mono);letter-spacing:.08em;text-transform:uppercase}
.semantic-launch-copy small b{color:var(--fog);font-weight:500}
.semantic-launch-cta{display:flex;align-items:center;gap:10px;color:var(--jade);font:7px/1.25 var(--mono);letter-spacing:.1em;text-align:right;text-transform:uppercase}
.semantic-launch-cta b{width:25px;height:25px;display:grid;place-items:center;border:1px solid color-mix(in oklch,var(--jade) 42%,transparent);font-size:11px;font-weight:400}
.semantic-launch-error{display:block;min-height:0;padding:0;color:#f2a28f;font:8px/1.4 var(--mono)}
.semantic-launch.has-error .semantic-launch-error{padding:8px 4px 0}
.semantic-launch.launching .semantic-launch-button{pointer-events:none;border-color:var(--jade);background:color-mix(in oklch,var(--jade) 10%,var(--void))}
.semantic-launch.launching .semantic-launch-mark{animation:semantic-pulse 1.15s ease-in-out infinite}
.semantic-launch.launching .semantic-launch-cta b{font-size:0}
.semantic-launch.launching .semantic-launch-cta b::after{content:"";width:10px;height:10px;border:1px solid var(--jade);border-right-color:transparent;border-radius:50%;animation:semantic-spin .75s linear infinite}
@keyframes semantic-pulse{50%{box-shadow:0 0 0 8px color-mix(in oklch,var(--jade) 8%,transparent)}}
@keyframes semantic-spin{to{transform:rotate(360deg)}}

/* hairline grid: 1px gaps over a line-colored bed, so rules stay perfect at any wrap */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));
  gap:1px;margin-top:1px;background:var(--line);border:1px solid var(--line)}
.card{position:relative;display:flex;flex-direction:column;background:var(--panel);transition:background .25s}
.card:hover{background:var(--panel-2)}
.card-link{display:flex;flex:1;flex-direction:column;min-height:100%}
.card-actions{position:absolute;top:10px;right:10px;z-index:5;display:flex;align-items:center;gap:5px}
.card-pin,.card-ask,.card-activity{display:flex;align-items:center;gap:6px;min-height:27px;padding:6px 8px;
  border:1px solid color-mix(in oklch,var(--bone) 18%,transparent);border-radius:999px;background:color-mix(in oklch,var(--void) 76%,transparent);
  -webkit-backdrop-filter:blur(13px);backdrop-filter:blur(13px);color:var(--fog);font:650 6px/1 var(--mono);letter-spacing:.09em;text-transform:uppercase;
  box-shadow:0 5px 16px rgba(0,0,0,.28);cursor:pointer;transition:border-color .18s,background .18s,color .18s,transform .18s}
.card-pin:hover,.card-pin:focus-visible{outline:0;transform:translateY(-1px);border-color:var(--amber);background:color-mix(in oklch,var(--void) 86%,var(--amber) 4%);color:var(--amber)}
.card-ask{border-color:color-mix(in oklch,var(--jade) 36%,var(--line));color:var(--jade)}
.card-ask:hover,.card-ask:focus-visible{outline:0;transform:translateY(-1px);border-color:var(--jade);background:color-mix(in oklch,var(--jade) 12%,var(--void));color:color-mix(in oklch,var(--jade) 82%,#fff)}
.card-activity{border-color:color-mix(in oklch,var(--bone) 22%,var(--line));color:var(--fog)}
.card-activity:hover,.card-activity:focus-visible{outline:0;transform:translateY(-1px);border-color:color-mix(in oklch,var(--amber) 68%,var(--jade));background:color-mix(in oklch,var(--amber) 7%,var(--void));color:var(--bone-bright)}
.card-activity b{color:var(--amber);font-size:8px;font-weight:500}
.card-ask:disabled{cursor:wait;opacity:.72}.card-ask.loading span{font-size:0}.card-ask.loading span::after{content:"…";font-size:6px}
.card-pin[aria-pressed=true]{border-color:color-mix(in oklch,var(--jade) 62%,transparent);background:color-mix(in oklch,var(--jade) 15%,var(--void));color:var(--jade)}
.card-pin:disabled{cursor:wait;opacity:.62}
.card-pin b{font-size:10px;font-weight:400;line-height:.6}
.shot{position:relative;aspect-ratio:16/10;overflow:hidden;background:#0d0b09}
/* The glyph sits underneath permanently; the shot fades in ON TOP once it
   actually loads. That way a thumbnail still being rendered shows the
   stand-in rather than a broken image, and a retry can swap it in later
   without the page having thrown the <img> away. */
.shot img{position:absolute;inset:0;z-index:1;width:100%;height:100%;
  object-fit:cover;object-position:top center;display:block;
  opacity:0;transition:transform .7s,opacity .45s}
.shot img.ok{opacity:.84}
.card:hover .shot img.ok{transform:scale(1.035);opacity:1}
.shot::after{content:"";position:absolute;inset:0;z-index:2;pointer-events:none;
  background:linear-gradient(to top,var(--panel) 2%,transparent 46%)}
.glyph{position:absolute;inset:0;z-index:0;display:grid;place-items:center;
  font-size:38px;color:var(--amber);opacity:.16}
/* Once the shot is up it sits at 84% opacity, so an untouched stand-in would
   ghost through the dark areas of the image. Retire it on load — but only on
   load, so the pending state still has something to show. */
.shot img.ok+.glyph{display:none}
.shot.pending .glyph{animation:breathe 1.9s ease-in-out infinite}
@keyframes breathe{0%,100%{opacity:.16}50%{opacity:.34}}
.body{display:flex;flex-direction:column;gap:8px;flex:1;padding:16px 18px 18px}
.meta{display:flex;align-items:center;gap:9px;flex-wrap:wrap;
  font:9px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase}
.pill{padding:3px 8px;border:1px solid var(--line-hot);color:var(--amber)}
.pill.dim{border-color:var(--line);color:var(--dim)}
.when{color:var(--dim)}
.card h2{font:italic 400 21px/1.2 var(--serif);letter-spacing:-.01em}
.desc{color:var(--fog);font-size:12.5px;line-height:1.55;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.foot{margin-top:auto;padding-top:12px;display:flex;gap:14px;
  color:var(--dim);font:9px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase}
.foot b{color:var(--fog);font-weight:400}

.artifact-activity-dialog{width:min(820px,calc(100vw - 32px));max-width:none;margin:auto;padding:0;border:1px solid color-mix(in oklch,var(--amber) 45%,var(--line));background:color-mix(in oklch,var(--panel) 96%,transparent);color:var(--bone);box-shadow:0 34px 120px color-mix(in oklch,var(--void) 88%,transparent);color-scheme:dark}
.artifact-activity-dialog::backdrop{background:color-mix(in oklch,var(--void) 72%,transparent);backdrop-filter:blur(8px)}
.activity-shell{display:flex;max-height:calc(100dvh - 32px);flex-direction:column}.activity-head{display:flex;align-items:center;gap:13px;min-height:72px;padding:12px 13px 12px 18px;border-bottom:1px solid var(--line);background:color-mix(in oklch,var(--gallery-rail-bg) 94%,transparent);backdrop-filter:blur(20px)}
.activity-head-mark{width:36px;height:36px;display:grid;place-items:center;flex:none;border:1px solid color-mix(in oklch,var(--amber) 45%,var(--line));border-radius:50%;color:var(--amber);font:15px/1 var(--mono)}
.activity-heading{min-width:0;display:flex;flex:1;flex-direction:column;gap:4px}.activity-heading>span{color:var(--amber);font:700 6px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase}.activity-heading strong{overflow:hidden;color:var(--bone-bright);font:italic 400 21px/1.08 var(--serif);text-overflow:ellipsis;white-space:nowrap}.activity-heading small{color:var(--dim);font:7px/1.3 var(--mono);letter-spacing:.07em;text-transform:uppercase}
.activity-range{display:flex;align-items:center;border:1px solid var(--line)}.activity-range button{min-height:29px;padding:0 9px;border:0;border-right:1px solid var(--line);background:transparent;color:var(--dim);font:700 6px/1 var(--mono);letter-spacing:.08em;cursor:pointer}.activity-range button:last-child{border-right:0}.activity-range button:hover{color:var(--bone)}.activity-range button[aria-pressed=true]{background:var(--amber);color:#1a1206}
.activity-close{width:36px;height:36px;display:grid;place-items:center;flex:none;border:1px solid var(--line);background:transparent;color:var(--fog);font-size:18px;cursor:pointer}.activity-close:hover{border-color:var(--amber);color:var(--amber)}
.activity-content{overflow:auto;min-height:330px;padding:18px}.activity-loading{min-height:300px;display:grid;place-items:center;color:var(--dim);font:8px/1.5 var(--mono);letter-spacing:.09em;text-transform:uppercase}.activity-loading i{width:22px;height:22px;margin-bottom:-100px;border:1px solid var(--line-hot);border-right-color:transparent;border-radius:50%;animation:semantic-spin .8s linear infinite}
.activity-stats{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--line);background:color-mix(in oklch,var(--void) 38%,transparent)}.activity-stat{min-width:0;padding:13px 14px;border-right:1px solid var(--line)}.activity-stat:last-child{border-right:0}.activity-stat span{display:block;color:var(--dim);font:700 6px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase}.activity-stat strong{display:block;overflow:hidden;margin-top:8px;color:var(--bone-bright);font:600 25px/1 var(--sans);font-variant-numeric:tabular-nums;letter-spacing:-.04em;text-overflow:ellipsis;white-space:nowrap}.activity-stat small{display:block;margin-top:6px;color:var(--fog);font:7px/1.25 var(--mono)}
.activity-chart-panel,.activity-viewers-panel{margin-top:12px;padding:14px;border:1px solid var(--line);background:color-mix(in oklch,var(--void) 30%,transparent)}.activity-panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.activity-panel-head h3{color:var(--bone-bright);font:italic 400 17px/1 var(--serif)}.activity-panel-head span{color:var(--dim);font:650 6px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase}
.activity-chart{height:126px;display:flex;align-items:flex-end;gap:3px;margin-top:15px;padding-top:8px;border-bottom:1px solid var(--line);background-image:linear-gradient(to bottom,var(--line) 1px,transparent 1px);background-size:100% 33.333%}.activity-bar{position:relative;min-width:2px;flex:1;height:max(2px,var(--bar-height));background:linear-gradient(to top,color-mix(in oklch,var(--jade) 30%,transparent),var(--jade));box-shadow:0 -3px 11px color-mix(in oklch,var(--jade) 12%,transparent);transition:filter .15s,opacity .15s}.activity-bar:hover{filter:brightness(1.32)}.activity-bar.zero{opacity:.14}.activity-chart-axis{display:flex;justify-content:space-between;margin-top:7px;color:var(--dim);font:6px/1 var(--mono);letter-spacing:.05em;text-transform:uppercase}
.activity-viewer-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin-top:11px}.activity-viewer{display:grid;grid-template-columns:29px minmax(0,1fr) auto;align-items:center;gap:9px;padding:8px;border:1px solid var(--line);background:color-mix(in oklch,var(--panel-raised) 64%,transparent)}.activity-viewer-avatar{width:29px;height:29px;display:grid;place-items:center;border:1px solid color-mix(in oklch,var(--jade) 35%,var(--line));border-radius:50%;color:var(--jade);font:700 7px/1 var(--mono)}.activity-viewer-copy{min-width:0}.activity-viewer-copy strong{display:block;overflow:hidden;color:var(--bone-bright);font:9px/1.2 var(--mono);text-overflow:ellipsis;white-space:nowrap}.activity-viewer-copy small{display:block;margin-top:4px;color:var(--dim);font:6px/1.2 var(--mono)}.activity-viewer-count{color:var(--fog);font:700 7px/1 var(--mono);text-align:right}.activity-viewer-count b{display:block;color:var(--amber);font-size:12px}.activity-empty{min-height:250px;display:grid;place-items:center;padding:35px;text-align:center}.activity-empty div{max-width:430px}.activity-empty b{display:block;color:var(--bone-bright);font:italic 400 24px/1.1 var(--serif)}.activity-empty p{margin-top:10px;color:var(--fog);font-size:11px;line-height:1.6}.activity-error{min-height:260px;display:grid;place-items:center;color:#f2a28f;font:9px/1.5 var(--mono);text-align:center}

.empty{padding:80px 0;text-align:center;color:var(--fog);font:12px/1.7 var(--mono);letter-spacing:.06em}
.empty code{color:var(--amber)}
#none{display:none;padding:60px 0;text-align:center;color:var(--dim);
  font:10px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase}
@media (max-width:760px){
  header.hero{padding:44px 0 24px}
  .who{gap:8px}
  .who .viewer{display:none}
  .applink{padding:6px 8px}
  .semantic-launch-button{grid-template-columns:38px minmax(0,1fr)}
  .semantic-launch-mark{width:38px;height:38px}
  .semantic-launch-cta{grid-column:2;justify-content:flex-start;text-align:left}
  .home-head{align-items:flex-start;flex-direction:column;gap:8px}
  .home-status{text-align:left}
  .gallery-presence{top:64px;right:12px;width:205px}.gallery-presence .presence-calendar,.gallery-presence .presence-meeting{display:none}.gallery-presence .presence-clock{min-height:54px;padding-block:9px}.gallery-presence .presence-time{font-size:26px}
  .activity-stats{grid-template-columns:repeat(2,1fr)}.activity-stat:nth-child(2){border-right:0}.activity-stat:nth-child(-n+2){border-bottom:1px solid var(--line)}.activity-viewer-list{grid-template-columns:1fr}
}
@media (max-width:520px){
  nav{padding-inline:14px}
  .wordmark small{display:none}
  .inbox-rail-label,.brief-rail-label{display:none}
  .inbox-rail-link,.brief-rail-link{gap:5px;padding-inline:8px}
  .applink{display:none}
  .calliope-float{gap:11px;min-height:60px;padding:5px 17px 5px 5px}
  .calliope-float-avatar{width:42px;height:42px}
  .calliope-float-name{font-size:20px}
  .calliope-float-copy{padding-top:1px}
  .calliope-float-action{display:none}
  .trail-dialog{width:100vw;height:100dvh;margin:0;border:0}
  .trail-content{padding:12px}
  .trail-loom>header{align-items:flex-start;flex-direction:column}.trail-loom>header b{text-align:left}
  .trail-loom-track{align-items:stretch;flex-direction:column}.trail-loom-step{width:100%;min-height:92px;flex-basis:auto}.trail-loom-link{width:auto;min-width:0;min-height:34px;flex-direction:row}.trail-loom-link i{transform:rotate(90deg)}
  .artifact-activity-dialog{width:100vw;height:100dvh;margin:0;border:0}.activity-head{gap:8px;padding-left:11px}.activity-head-mark,.activity-heading small{display:none}.activity-range button{padding-inline:7px}.activity-content{padding:10px}
}
@media (min-width:900px){header.hero{padding-right:min(390px,32vw)}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

_GALLERY_METRICS_CSS = """
.gallery-view-switch{display:inline-flex;align-items:center;flex:none;border:1px solid var(--line);background:color-mix(in oklch,var(--void) 62%,transparent)}
.gallery-view-switch button{min-height:34px;padding:0 12px;border:0;border-right:1px solid var(--line);background:transparent;color:var(--dim);font:700 8px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;cursor:pointer}
.gallery-view-switch button:last-child{border-right:0}.gallery-view-switch button:hover{color:var(--bone)}
.gallery-view-switch button[aria-selected=true]{background:var(--amber);color:#1a1206}
.gallery-view-switch b{display:inline-grid;min-width:17px;margin-left:6px;padding:3px 4px;border-radius:999px;background:color-mix(in oklch,currentColor 14%,transparent);font-size:6px;place-items:center}.gallery-view-switch b[hidden]{display:none}
.artifact-kind-chips{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.artifact-kind-chips[hidden]{display:none}
#artifact-browser[hidden],.metrics-browser[hidden]{display:none}
.metrics-browser{padding-top:16px}
.metrics-browser-head{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;padding:0 0 14px;border-bottom:1px solid var(--line)}
.metrics-browser-head>div:first-child{display:flex;flex-direction:column;gap:6px}.metrics-browser-head>div:first-child>span{color:var(--jade);font:700 7px/1 var(--mono);letter-spacing:.15em;text-transform:uppercase}
.metrics-browser-head>div:first-child>strong{color:var(--fog);font:italic 400 16px/1.2 var(--serif)}.metrics-browser-head>div:first-child>small{color:var(--dim);font:6px/1.45 var(--mono);letter-spacing:.05em}
.metric-browser-filters{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}
#metric-category{height:30px;max-width:210px;padding:0 25px 0 9px;border:1px solid var(--line);border-radius:0;background:var(--panel);color:var(--fog);font:7px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;outline:none}
#metric-category:focus{border-color:var(--jade)}
.metric-relation-filter{display:inline-flex;border:1px solid var(--line)}.metric-relation-filter button{height:28px;padding:0 9px;border:0;border-right:1px solid var(--line);background:transparent;color:var(--dim);font:650 6px/1 var(--mono);letter-spacing:.09em;text-transform:uppercase;cursor:pointer}.metric-relation-filter button:last-child{border:0}.metric-relation-filter button:hover{color:var(--jade)}.metric-relation-filter button[aria-pressed=true]{background:var(--jade-soft);color:var(--jade)}
.metric-browser-status,.metric-gallery-empty{min-height:190px;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:10px;color:var(--dim);font:8px/1.5 var(--mono);letter-spacing:.08em;text-align:center;text-transform:uppercase}.metric-browser-status[hidden],.metric-gallery-empty[hidden]{display:none}.metric-browser-status i{width:21px;height:21px;border:1px solid var(--jade);border-right-color:transparent;border-radius:50%;animation:semantic-spin .75s linear infinite}.metric-browser-status.error{color:#f2a28f}.metric-browser-status.error i{display:none}.metric-gallery-empty strong{color:var(--fog);font:italic 400 18px/1.2 var(--serif);letter-spacing:0;text-transform:none}.metric-gallery-empty span{font-size:7px}
.metric-gallery-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:1px;margin-top:1px;border:1px solid var(--line);background:var(--line)}.metric-gallery-grid:empty{display:none}
.metric-gallery-card{--metric-card-trend:var(--jade);--metric-card-floor:var(--panel);position:relative;min-height:280px;overflow:hidden;background:linear-gradient(145deg,color-mix(in oklch,var(--panel) 95%,var(--jade) 5%),var(--panel));cursor:pointer;isolation:isolate;transition:background .22s,transform .2s}.metric-gallery-card:hover{--metric-card-floor:var(--panel-2);z-index:2;background:linear-gradient(145deg,color-mix(in oklch,var(--panel-2) 90%,var(--jade) 10%),var(--panel-2))}.metric-gallery-card:focus-within{z-index:3;outline:1px solid var(--jade);outline-offset:-1px}.metric-gallery-card.bad{--metric-card-trend:#df765f}
.metric-card-grid{position:absolute;z-index:-1;inset:0;opacity:.62;background-image:linear-gradient(color-mix(in oklch,var(--bone) 4%,transparent) 1px,transparent 1px),linear-gradient(90deg,color-mix(in oklch,var(--bone) 3%,transparent) 1px,transparent 1px);background-size:100% 25%,16.66% 100%;mask-image:linear-gradient(to top,#000,transparent 92%)}
.metric-card-chart{position:absolute;z-index:-1;inset:50px 0 0;opacity:.72;pointer-events:none}.metric-card-chart svg{width:100%;height:100%;display:block;overflow:visible}.metric-card-area-top{stop-color:var(--metric-card-trend);stop-opacity:.24}.metric-card-area-mid{stop-color:var(--metric-card-trend);stop-opacity:.07}.metric-card-area-clear{stop-color:var(--metric-card-floor);stop-opacity:0}.metric-card-area-floor{stop-color:var(--metric-card-floor);stop-opacity:.96}.metric-card-line{fill:none;stroke:var(--metric-card-trend);stroke-width:1.8;vector-effect:non-scaling-stroke;filter:drop-shadow(0 0 7px color-mix(in oklch,var(--metric-card-trend) 42%,transparent))}
.metric-card-open{position:absolute;z-index:1;inset:0;width:100%;border:0;background:transparent;color:inherit;cursor:pointer}.metric-card-open:focus-visible{outline:2px solid var(--jade);outline-offset:-3px}
.metric-card-actions{position:absolute;z-index:3;top:12px;right:12px;display:flex;gap:5px}.metric-card-actions button,.metric-lens-actions button{display:inline-flex;align-items:center;gap:5px;min-height:27px;padding:5px 8px;border:1px solid color-mix(in oklch,var(--bone) 17%,transparent);border-radius:999px;background:color-mix(in oklch,var(--void) 72%,transparent);color:var(--fog);font:700 6px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;cursor:pointer;backdrop-filter:blur(12px);transition:border-color .16s,color .16s,background .16s,transform .16s}.metric-card-actions button:hover,.metric-lens-actions button:hover{transform:translateY(-1px);border-color:var(--jade);color:var(--jade)}.metric-card-actions button.active,.metric-lens-actions button.active{border-color:color-mix(in oklch,var(--jade) 58%,var(--line));background:color-mix(in oklch,var(--jade) 13%,var(--void));color:var(--jade)}.metric-card-actions button.ask,.metric-lens-actions button.ask{border-color:color-mix(in oklch,var(--amber) 46%,var(--line));color:var(--amber)}.metric-card-actions button:disabled,.metric-lens-actions button:disabled{opacity:.55;cursor:wait;transform:none}
.metric-card-content{position:relative;z-index:2;min-height:280px;display:flex;flex-direction:column;justify-content:flex-end;padding:18px;pointer-events:none;text-shadow:0 1px 1px color-mix(in oklch,var(--void) 98%,transparent),0 2px 7px color-mix(in oklch,var(--void) 96%,transparent),0 8px 22px color-mix(in oklch,var(--void) 84%,transparent)}
.metric-card-kicker{position:absolute;top:18px;left:18px;right:168px;display:flex;align-items:center;gap:7px;overflow:hidden;color:var(--jade);font:700 6px/1 var(--mono);letter-spacing:.12em;text-overflow:ellipsis;text-transform:uppercase;white-space:nowrap}.metric-card-kicker::before{content:"";width:13px;height:1px;flex:none;background:currentColor}.metric-gallery-card.bad .metric-card-kicker{color:#df765f}
.metric-card-status{align-self:flex-start;margin-bottom:auto;padding:4px 6px;border:1px solid var(--line);color:var(--dim);font:650 6px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase}.metric-card-status.good{border-color:color-mix(in oklch,var(--jade) 38%,var(--line));color:var(--jade)}.metric-card-status.bad{border-color:color-mix(in oklch,#df765f 48%,var(--line));color:#ef9b91}
.metric-card-value{display:flex;align-items:baseline;gap:10px;color:var(--bone-bright);font:600 clamp(34px,4vw,57px)/.92 var(--sans);letter-spacing:-.06em}.metric-card-value small{color:var(--jade);font:650 8px/1 var(--mono);letter-spacing:0}.metric-card-value small.bad{color:#ef9b91}.metric-card-value small.neutral{color:var(--fog)}
.metric-card-title{margin-top:9px;color:var(--bone-bright);font:italic 400 20px/1.15 var(--serif)}.metric-card-description{display:-webkit-box;overflow:hidden;margin-top:6px;color:var(--fog);font-size:9px;line-height:1.45;-webkit-box-orient:vertical;-webkit-line-clamp:2}.metric-card-foot{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:12px;padding-top:9px;border-top:1px solid var(--line);color:var(--dim);font:6px/1.3 var(--mono);letter-spacing:.09em;text-transform:uppercase}.metric-card-foot span:last-child{text-align:right}
.metric-lens{width:min(1120px,calc(100vw - 28px));max-width:none;height:min(850px,calc(100dvh - 28px));max-height:none;margin:auto;padding:0;border:1px solid color-mix(in oklch,var(--jade) 46%,var(--line));background:color-mix(in oklch,var(--panel) 96%,transparent);color:var(--bone);box-shadow:0 38px 140px color-mix(in oklch,var(--void) 90%,transparent)}.metric-lens::backdrop{background:color-mix(in oklch,var(--void) 76%,transparent);backdrop-filter:blur(9px)}.metric-lens-shell{height:100%;display:grid;grid-template-rows:auto minmax(0,1fr)}
.metric-lens-head{display:flex;align-items:center;gap:16px;min-height:72px;padding:11px 13px 11px 19px;border-bottom:1px solid var(--line);background:color-mix(in oklch,var(--gallery-rail-bg) 92%,transparent);backdrop-filter:blur(18px)}.metric-lens-heading{min-width:0;display:flex;flex:1;flex-direction:column;gap:4px}.metric-lens-heading>span{color:var(--jade);font:750 6px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase}.metric-lens-heading>strong{overflow:hidden;color:var(--bone-bright);font:italic 400 22px/1.05 var(--serif);text-overflow:ellipsis;white-space:nowrap}.metric-lens-heading>small{overflow:hidden;color:var(--dim);font:7px/1.25 var(--mono);letter-spacing:.08em;text-overflow:ellipsis;text-transform:uppercase;white-space:nowrap}.metric-lens-actions{display:flex;align-items:center;gap:5px}.metric-lens-close{width:36px;height:36px;display:grid;place-items:center;flex:none;border:1px solid var(--line);background:transparent;color:var(--fog);font:19px/1 var(--sans);cursor:pointer}.metric-lens-close:hover{border-color:var(--amber);color:var(--amber)}
.metric-lens-content{min-height:0;overflow:auto;padding:18px;scrollbar-color:color-mix(in oklch,var(--jade) 48%,var(--line)) color-mix(in oklch,var(--void) 60%,transparent)}.metric-lens-loading,.metric-lens-error{min-height:300px;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:11px;color:var(--dim);font:8px/1.5 var(--mono);text-align:center}.metric-lens-loading i{width:25px;height:25px;border:1px solid var(--jade);border-right-color:transparent;border-radius:50%;animation:semantic-spin .75s linear infinite}.metric-lens-error strong{color:#ef9b91;font:italic 400 20px/1.2 var(--serif)}
.metric-lens-hero{position:relative;min-height:200px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:20px;overflow:hidden;padding:20px;border:1px solid color-mix(in oklch,var(--jade) 28%,var(--line));background:linear-gradient(145deg,color-mix(in oklch,var(--jade) 7%,var(--panel)),var(--panel))}.metric-lens-hero::before{content:"";position:absolute;inset:0;opacity:.5;background-image:linear-gradient(color-mix(in oklch,var(--bone) 4%,transparent) 1px,transparent 1px),linear-gradient(90deg,color-mix(in oklch,var(--bone) 3%,transparent) 1px,transparent 1px);background-size:100% 25%,12.5% 100%;pointer-events:none}.metric-lens-hero-copy{position:relative;z-index:1;align-self:end}.metric-lens-hero-copy>span{color:var(--jade);font:700 7px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase}.metric-lens-hero-copy>strong{display:block;margin-top:14px;color:var(--bone-bright);font:600 clamp(45px,7vw,80px)/.86 var(--sans);letter-spacing:-.07em}.metric-lens-hero-copy>p{max-width:720px;margin-top:13px;color:var(--fog);font-size:11px;line-height:1.5}.metric-lens-hero-facts{position:relative;z-index:1;display:flex;align-items:flex-end;flex-direction:column;justify-content:flex-end;gap:6px}.metric-lens-hero-facts span{padding:5px 7px;border:1px solid var(--line);color:var(--fog);font:7px/1.25 var(--mono);text-transform:uppercase}.metric-lens-hero-facts b{margin-right:6px;color:var(--dim);font-weight:500}
.metric-lens-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:14px;padding:9px 10px;border:1px solid var(--line);background:color-mix(in oklch,var(--void) 35%,transparent)}.metric-range-buttons{display:flex;gap:4px}.metric-range-buttons button{height:27px;padding:0 8px;border:1px solid var(--line);background:transparent;color:var(--dim);font:650 6px/1 var(--mono);text-transform:uppercase;cursor:pointer}.metric-range-buttons button:hover,.metric-range-buttons button.active{border-color:var(--jade);color:var(--jade);background:var(--jade-soft)}.metric-range-selection{color:var(--dim);font:7px/1.35 var(--mono);letter-spacing:.06em;text-align:right}.metric-range-selection.active{color:var(--jade)}
.metric-lens-chart{position:relative;margin-top:1px;min-height:310px;border:1px solid var(--line);background:color-mix(in oklch,var(--void) 38%,transparent)}.metric-lens-chart svg{display:block;width:100%;height:310px}.metric-lens-chart .grid-line{stroke:color-mix(in oklch,var(--bone) 7%,transparent);stroke-width:1;vector-effect:non-scaling-stroke}.metric-lens-chart .axis-label{fill:var(--dim);font:7px var(--mono)}.metric-lens-chart .area{fill:color-mix(in oklch,var(--jade) 10%,transparent)}.metric-lens-chart .line{fill:none;stroke:var(--jade);stroke-width:2;vector-effect:non-scaling-stroke}.metric-lens-chart .point{fill:var(--panel);stroke:var(--jade);stroke-width:2;vector-effect:non-scaling-stroke;cursor:pointer}.metric-lens-chart .point:hover,.metric-lens-chart .point.selected{fill:var(--amber);stroke:var(--amber);r:5}.metric-lens-chart .version-mark{stroke:color-mix(in oklch,var(--amber) 45%,transparent);stroke-dasharray:3 3;vector-effect:non-scaling-stroke}.metric-chart-tooltip{position:absolute;z-index:3;max-width:240px;padding:8px 9px;border:1px solid color-mix(in oklch,var(--jade) 45%,var(--line));background:color-mix(in oklch,var(--panel) 97%,transparent);box-shadow:0 10px 35px rgba(0,0,0,.35);color:var(--fog);font:7px/1.4 var(--mono);pointer-events:none;transform:translate(-50%,-112%)}.metric-chart-tooltip[hidden]{display:none}.metric-chart-tooltip strong{display:block;margin-bottom:3px;color:var(--bone-bright);font-size:9px}.metric-chart-tooltip b{color:var(--jade);font-weight:500}
.metric-lens-grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(260px,.75fr);gap:12px;margin-top:14px}.metric-lens-panel{min-width:0;padding:15px;border:1px solid var(--line);background:color-mix(in oklch,var(--panel-raised) 72%,transparent)}.metric-lens-panel>h3{display:flex;align-items:center;gap:8px;margin-bottom:11px;color:var(--jade);font:700 7px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase}.metric-lens-panel>h3::after{content:"";height:1px;flex:1;background:var(--line)}.metric-lens-panel>p{color:var(--fog);font-size:11px;line-height:1.6}.metric-definition-facts,.metric-source-list,.metric-artifact-list{display:flex;flex-wrap:wrap;gap:6px}.metric-definition-facts span,.metric-source-list span,.metric-artifact-list a{padding:6px 8px;border:1px solid var(--line);color:var(--fog);font:7px/1.3 var(--mono)}.metric-definition-facts b{margin-right:6px;color:var(--dim);font-weight:500;text-transform:uppercase}.metric-source-list span.stale{border-color:color-mix(in oklch,#df765f 45%,var(--line));color:#ef9b91}.metric-artifact-list a:hover{border-color:var(--amber);color:var(--amber)}.metric-sql{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}.metric-sql summary{color:var(--dim);font:7px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;cursor:pointer}.metric-sql pre{max-height:260px;overflow:auto;margin-top:10px;padding:11px;border:1px solid var(--line);background:var(--void);color:var(--fog);font:8px/1.55 var(--mono);white-space:pre-wrap}.metric-param-list{display:flex;flex-direction:column;gap:5px}.metric-param-list div{display:grid;grid-template-columns:minmax(90px,.55fr) minmax(0,1fr);gap:8px;padding:7px 8px;border:1px solid var(--line);font:7px/1.35 var(--mono)}.metric-param-list dt{color:var(--dim)}.metric-param-list dt small{display:block;margin-top:3px;color:var(--jade);font:5px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase}.metric-param-list dd{overflow-wrap:anywhere;color:var(--fog)}
@media(max-width:760px){.gallery-view-switch{width:100%}.gallery-view-switch button{flex:1}.artifact-kind-chips{width:100%;overflow:auto;flex-wrap:nowrap}.metrics-browser-head{align-items:flex-start;flex-direction:column}.metric-browser-filters{width:100%;justify-content:flex-start}.metric-lens{width:100vw;height:100dvh;margin:0;border:0}.metric-lens-head{gap:9px}.metric-lens-heading>small{display:none}.metric-lens-actions button span{display:none}.metric-lens-content{padding:11px}.metric-lens-hero{grid-template-columns:1fr}.metric-lens-hero-facts{align-items:flex-start}.metric-lens-toolbar{align-items:flex-start;flex-direction:column}.metric-range-selection{text-align:left}.metric-lens-grid{grid-template-columns:1fr}}
"""

_LANDING_JS = """
(function(){
 // Thumbnails render on the server the first time an index is viewed, so a
 // cold gallery has cards whose shot does not exist YET. Retry with backoff
 // instead of settling for the monogram until someone thinks to refresh.
 // Cache-buster only on retries: the first hit must stay cacheable.
 [].forEach.call(document.querySelectorAll('.shot img'), function(img){
   var shot=img.parentNode, tries=0, base=img.getAttribute('src');
   shot.classList.add('pending');
   img.addEventListener('load', function(){
     img.classList.add('ok'); shot.classList.remove('pending');
   });
   img.addEventListener('error', function(){
     if(++tries>5){ shot.classList.remove('pending'); return; }
     setTimeout(function(){ img.src = base + '?r=' + tries; }, tries*2500);
   });
   if(img.complete && img.naturalWidth>0){
     img.classList.add('ok'); shot.classList.remove('pending');
   }
 });

 var calliopeFrame=document.querySelector('.calliope-float-avatar'),
     calliopeTimer=null;
 function updateCalliopeAvatar(){
   if(!calliopeFrame)return;
   var hour=new Date().getHours(),
       period=hour>=7&&hour<19?'day':'night',
       image=calliopeFrame.querySelector('img'),
       src=period==='day'?calliopeFrame.dataset.daySrc:calliopeFrame.dataset.nightSrc;
   if(src&&image.getAttribute('src')!==src)image.src=src;
   calliopeFrame.dataset.period=period;
 }
 function scheduleCalliopeAvatar(){
   updateCalliopeAvatar();
   clearTimeout(calliopeTimer);
   calliopeTimer=setTimeout(scheduleCalliopeAvatar,60050-(Date.now()%60000));
 }
 if(calliopeFrame)scheduleCalliopeAvatar();

 var galleryPresence=document.getElementById('gallery-presence'),
     presenceHours=document.getElementById('presence-hours'),
     presenceMinutes=document.getElementById('presence-minutes'),
     presenceSeconds=document.getElementById('presence-seconds'),
     presencePeriod=document.getElementById('presence-period'),
     presenceDate=document.getElementById('presence-date'),
     presenceZone=document.getElementById('presence-zone'),
     presenceMeeting=document.getElementById('presence-meeting'),
     presenceMeetingLabel=document.getElementById('presence-meeting-label'),
     presenceMeetingTitle=document.getElementById('presence-meeting-title'),
     presenceMeetingTime=document.getElementById('presence-meeting-time'),
     presenceEvents=[],presenceClockTimer=null,presenceCalendarTimer=null;
 function presencePart(parts,type,fallback){var match=parts.find(function(part){return part.type===type;});return match?match.value:fallback;}
 function presenceTimeText(value){
   try{return new Intl.DateTimeFormat(undefined,{hour:'numeric',minute:'2-digit'}).format(value);}catch(ignore){return value.toLocaleTimeString();}
 }
 function presenceMeetingMarkup(now){
   if(!galleryPresence||!presenceMeeting)return;
   var activeEvents=presenceEvents.filter(function(item){var end=item.ends_at?new Date(item.ends_at).getTime():new Date(item.starts_at).getTime()+3600000;return Number.isFinite(end)&&end>now.getTime();}),event=activeEvents[0];
   if(!event){presenceMeeting.hidden=true;galleryPresence.classList.remove('soon','live');return;}
   var start=new Date(event.starts_at),end=event.ends_at?new Date(event.ends_at):new Date(start.getTime()+3600000),delta=start.getTime()-now.getTime(),live=delta<=0&&end.getTime()>now.getTime(),minutes=Math.max(0,Math.round(delta/60000)),label='Coming up';
   if(live)label='Happening now';
   else if(minutes<1)label='Starting now';
   else if(minutes<60)label='Next · in '+minutes+' min';
   else if(minutes<1440){var hours=Math.floor(minutes/60),rest=minutes%60;label='Next · in '+hours+' hr'+(rest?' '+rest+' min':'');}
   else label=start.toDateString()===new Date(now.getFullYear(),now.getMonth(),now.getDate()+1).toDateString()?'Tomorrow':'Coming up';
   galleryPresence.classList.toggle('live',live);galleryPresence.classList.toggle('soon',!live&&minutes<=15);
   presenceMeetingLabel.textContent=label;presenceMeetingTitle.textContent=event.title||'Busy';
   presenceMeetingTime.textContent=presenceTimeText(start);
   if(activeEvents.length>1){var later=document.createElement('small');later.textContent='+'+(activeEvents.length-1)+' later';presenceMeetingTime.appendChild(later);}
   var url=event.meeting_url||event.calendar_url||'';
   try{var parsed=new URL(url,window.location.origin);if(parsed.protocol!=='https:')throw new Error('unsafe');presenceMeeting.href=parsed.href;presenceMeeting.removeAttribute('aria-disabled');}
   catch(ignore){presenceMeeting.removeAttribute('href');presenceMeeting.setAttribute('aria-disabled','true');}
   presenceMeeting.title=(live?'Open current meeting':'Open upcoming meeting')+' · '+(event.title||'Busy');presenceMeeting.hidden=false;
 }
 function updatePresenceClock(){
   if(!galleryPresence)return;
   var now=new Date(),parts=[];
   try{parts=new Intl.DateTimeFormat(undefined,{hour:'2-digit',minute:'2-digit',second:'2-digit'}).formatToParts(now);}catch(ignore){}
   presenceHours.textContent=presencePart(parts,'hour',String(now.getHours()).padStart(2,'0'));
   presenceMinutes.textContent=presencePart(parts,'minute',String(now.getMinutes()).padStart(2,'0'));
   presenceSeconds.textContent=presencePart(parts,'second',String(now.getSeconds()).padStart(2,'0'));
   presencePeriod.textContent=presencePart(parts,'dayPeriod','');
   try{presenceDate.textContent=new Intl.DateTimeFormat(undefined,{weekday:'long',month:'short',day:'numeric'}).format(now);var zoneParts=new Intl.DateTimeFormat(undefined,{timeZoneName:'short'}).formatToParts(now);presenceZone.textContent=presencePart(zoneParts,'timeZoneName',Intl.DateTimeFormat().resolvedOptions().timeZone||'Local time');}catch(ignore){}
   presenceMeetingMarkup(now);
   clearTimeout(presenceClockTimer);presenceClockTimer=setTimeout(updatePresenceClock,1005-(Date.now()%1000));
 }
 function compactPresence(){if(!galleryPresence)return;galleryPresence.classList.toggle('compact',window.scrollY>180&&!galleryPresence.classList.contains('soon')&&!galleryPresence.classList.contains('live'));}
 async function loadPresenceMeetings(){
   if(!galleryPresence)return;
   try{var response=await fetch('/api/gallery/meetings',{headers:{accept:'application/json'}}),data={};try{data=await response.json();}catch(ignore){}if(!response.ok)throw new Error('Calendar unavailable');presenceEvents=Array.isArray(data.events)?data.events:[];presenceMeetingMarkup(new Date());compactPresence();}
   catch(ignore){presenceEvents=[];presenceMeeting.hidden=true;galleryPresence.classList.remove('soon','live');}
 }
 if(galleryPresence){updatePresenceClock();loadPresenceMeetings();window.addEventListener('scroll',compactPresence,{passive:true});compactPresence();presenceCalendarTimer=setInterval(loadPresenceMeetings,120000);document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible'){updatePresenceClock();loadPresenceMeetings();}});}

 var activityDialog=document.getElementById('artifact-activity-dialog'),
     activityTitle=document.getElementById('activity-title'),
     activityMeta=document.getElementById('activity-meta'),
     activityContent=document.getElementById('activity-content'),
     activityRange=document.getElementById('activity-range'),
     activityClose=document.getElementById('activity-close'),
     activitySlug='',activityDays=30,activityRequest=0;
 function escapeActivity(value){return String(value===null||value===undefined?'':value).replace(/[&<>"']/g,function(char){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char];});}
 function activityNumber(value){try{return new Intl.NumberFormat(undefined,{notation:Number(value)>=10000?'compact':'standard',maximumFractionDigits:1}).format(Number(value)||0);}catch(ignore){return String(Number(value)||0);}}
 function activityRelative(value){var stamp=new Date(value),seconds=Math.round((Date.now()-stamp.getTime())/1000);if(!Number.isFinite(seconds))return 'never';if(seconds<60)return 'just now';if(seconds<3600)return Math.floor(seconds/60)+'m ago';if(seconds<86400)return Math.floor(seconds/3600)+'h ago';if(seconds<604800)return Math.floor(seconds/86400)+'d ago';try{return new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric',year:stamp.getFullYear()===new Date().getFullYear()?undefined:'numeric'}).format(stamp);}catch(ignore){return stamp.toLocaleDateString();}}
 function activityDay(value){var stamp=new Date(value+'T12:00:00');try{return new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric'}).format(stamp);}catch(ignore){return value;}}
 function activityInitials(value){var local=String(value||'').split('@')[0].replace(/[^a-z0-9]+/ig,' ').trim().split(/\\s+/).filter(Boolean);return (local.length>1?local[0][0]+local[1][0]:(local[0]||'?').slice(0,2)).toUpperCase();}
 function activityLoading(){if(activityContent)activityContent.innerHTML='<div class="activity-loading"><i></i><span>Reading authenticated views…</span></div>';}
 function renderActivity(data){
   var summary=data.summary||{},series=Array.isArray(data.series)?data.series:[],viewers=Array.isArray(data.viewers)?data.viewers:[],artifact=data.artifact||{};
   activityTitle.textContent=artifact.name||activitySlug;activityMeta.textContent=(artifact.app_kind||'artifact')+' · shared authenticated audience · tracking since '+(summary.tracked_since?activityRelative(summary.tracked_since):'this release');
   if(!Number(summary.total_views||0)){activityContent.innerHTML='<div class="activity-empty"><div><b>The room is quiet—for now.</b><p>Authenticated view tracking is live. The first real dashboard or app open will appear here; thumbnails, prefetches, and background queries are not counted.</p></div></div>';return;}
   var max=Math.max.apply(null,series.map(function(item){return Number(item.views)||0;}).concat([1])),bars=series.map(function(item){var views=Number(item.views)||0,height=views?Math.max(4,(views/max)*100):2;return '<i class="activity-bar '+(views?'':'zero')+'" style="--bar-height:'+height.toFixed(2)+'%" title="'+escapeActivity(activityDay(item.day)+' · '+views+' view'+(views===1?'':'s')+' · '+(Number(item.viewers)||0)+' '+((Number(item.viewers)||0)===1?'person':'people'))+'"></i>';}).join(''),axis=series.length?'<div class="activity-chart-axis"><span>'+escapeActivity(activityDay(series[0].day))+'</span><span>'+escapeActivity(activityDay(series[Math.floor((series.length-1)/2)].day))+'</span><span>'+escapeActivity(activityDay(series[series.length-1].day))+'</span></div>':'';
   var people=viewers.map(function(item){return '<article class="activity-viewer"><span class="activity-viewer-avatar" aria-hidden="true">'+escapeActivity(activityInitials(item.viewer))+'</span><span class="activity-viewer-copy"><strong>'+escapeActivity(item.viewer)+(item.is_you?' · you':'')+'</strong><small>Last opened '+escapeActivity(activityRelative(item.last_viewed_at))+' · '+escapeActivity(item.active_days||1)+' active day'+(Number(item.active_days)===1?'':'s')+'</small></span><span class="activity-viewer-count"><b>'+escapeActivity(activityNumber(item.views))+'</b>view'+(Number(item.views)===1?'':'s')+'</span></article>';}).join('');
   activityContent.innerHTML='<section class="activity-stats"><div class="activity-stat"><span>All views</span><strong>'+escapeActivity(activityNumber(summary.total_views))+'</strong><small>real document opens</small></div><div class="activity-stat"><span>People</span><strong>'+escapeActivity(activityNumber(summary.unique_viewers))+'</strong><small>authenticated viewers</small></div><div class="activity-stat"><span>'+escapeActivity(data.window_days)+' day views</span><strong>'+escapeActivity(activityNumber(summary.window_views))+'</strong><small>selected window</small></div><div class="activity-stat"><span>Last opened</span><strong>'+escapeActivity(activityRelative(summary.last_viewed_at))+'</strong><small>'+escapeActivity(activityNumber(summary.repeat_views))+' return view'+(Number(summary.repeat_views)===1?'':'s')+'</small></div></section><section class="activity-chart-panel"><header class="activity-panel-head"><h3>Viewing rhythm</h3><span>Hover a day for people + views</span></header><div class="activity-chart">'+bars+'</div>'+axis+'</section><section class="activity-viewers-panel"><header class="activity-panel-head"><h3>Who came through</h3><span>'+escapeActivity(viewers.length)+' authenticated '+(viewers.length===1?'person':'people')+'</span></header><div class="activity-viewer-list">'+people+'</div></section>';
 }
 async function loadActivity(){
   if(!activitySlug||!activityContent)return;var request=++activityRequest;activityLoading();
   [].forEach.call(activityRange.querySelectorAll('[data-activity-days]'),function(button){button.setAttribute('aria-pressed',String(Number(button.dataset.activityDays)===activityDays));});
   var zone='UTC';try{zone=Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';}catch(ignore){}
   try{var response=await fetch('/api/gallery/artifacts/'+encodeURIComponent(activitySlug)+'/activity?days='+activityDays+'&timezone='+encodeURIComponent(zone),{headers:{accept:'application/json'}}),data={};try{data=await response.json();}catch(ignore){}if(request!==activityRequest)return;if(!response.ok)throw new Error(data.error&&data.error.message||'Activity is unavailable');renderActivity(data);}
   catch(error){if(request!==activityRequest)return;activityContent.innerHTML='<div class="activity-error">'+escapeActivity(error&&error.message||'Activity is unavailable')+'</div>';}
 }
 function openActivity(slug){if(!activityDialog)return;activitySlug=slug;activityDays=30;activityTitle.textContent='Artifact activity';activityMeta.textContent='Human document opens · shared Gallery insight';if(!activityDialog.open)activityDialog.showModal();loadActivity();}
 document.addEventListener('click',function(event){var button=event.target.closest&&event.target.closest('[data-gallery-activity]');if(button){event.preventDefault();event.stopPropagation();openActivity(button.dataset.galleryActivity);}});
 if(activityRange)activityRange.addEventListener('click',function(event){var button=event.target.closest&&event.target.closest('[data-activity-days]');if(!button)return;activityDays=Number(button.dataset.activityDays)||30;loadActivity();});
 if(activityClose)activityClose.addEventListener('click',function(){activityDialog.close();});if(activityDialog)activityDialog.addEventListener('cancel',function(event){event.preventDefault();activityDialog.close();});

 var galleryInbox=document.getElementById('gallery-work-inbox'),
     galleryInboxCount=document.getElementById('gallery-work-inbox-count'),
     galleryInboxTimer=null;
 async function loadGalleryInbox(){
   if(!galleryInbox||!galleryInboxCount)return;
   try{
     var response=await fetch('/api/calliope/inbox?limit=1',{headers:{accept:'application/json'}}),data={};
     try{data=await response.json();}catch(ignore){}
     if(!response.ok)throw new Error('Inbox unavailable');
     var counts=data.counts||{},open=Math.max(0,Number(counts.open)||0),
         unread=Math.max(0,Number(counts.unread)||0);
     galleryInbox.classList.toggle('has-open',open>0);
     galleryInbox.classList.toggle('has-unread',unread>0);
     galleryInboxCount.hidden=open===0;
     galleryInboxCount.textContent=open>99?'99+':String(open);
     galleryInbox.title=open
       ? open+' open work item'+(open===1?'':'s')+(unread?' · '+unread+' unread':'')+' · Open in Calliope'
       : 'Work Inbox clear · Open in Calliope';
     galleryInbox.setAttribute('aria-label',open
       ? 'Open Work Inbox in Calliope · '+open+' open'+(unread?' · '+unread+' unread':'')
       : 'Open Work Inbox in Calliope · no open items');
   }catch(ignore){
     galleryInbox.classList.remove('has-open','has-unread');
     galleryInboxCount.hidden=true;
     galleryInbox.title='Open Work Inbox in Calliope';
   }
 }
 if(galleryInbox){
   loadGalleryInbox();
   galleryInboxTimer=setInterval(loadGalleryInbox,45000);
   document.addEventListener('visibilitychange',function(){
     if(document.visibilityState==='visible')loadGalleryInbox();
   });
 }

 var galleryBrief=document.getElementById('gallery-personal-brief'),
     galleryBriefCount=document.getElementById('gallery-personal-brief-count'),
     galleryBriefTimer=null;
 async function loadGalleryBrief(){
   if(!galleryBrief||!galleryBriefCount)return;
   try{
     var timezone='UTC';
     try{timezone=Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';}catch(ignore){}
     var response=await fetch('/api/calliope/briefs/status?timezone='+encodeURIComponent(timezone),{headers:{accept:'application/json'}}),data={};
     try{data=await response.json();}catch(ignore){}
     if(!response.ok)throw new Error('Brief unavailable');
     var brief=data.brief||{},exists=Boolean(brief.exists),count=Math.max(0,Number(brief.item_count)||0);
     galleryBrief.classList.toggle('has-brief',exists);
     galleryBriefCount.hidden=!exists;
     galleryBriefCount.textContent=count>99?'99+':String(count);
     galleryBrief.title=exists
       ? "Open today's Personal Brief · "+count+' grounded item'+(count===1?'':'s')
       : "Create today's private Personal Brief";
     galleryBrief.setAttribute('aria-label',galleryBrief.title);
   }catch(ignore){
     galleryBrief.classList.remove('has-brief');
     galleryBriefCount.hidden=true;
     galleryBrief.title="Open today's private Personal Brief";
   }
 }
 if(galleryBrief){
   loadGalleryBrief();
   galleryBriefTimer=setInterval(loadGalleryBrief,60000);
   document.addEventListener('visibilitychange',function(){
     if(document.visibilityState==='visible')loadGalleryBrief();
   });
 }

 var homeSection=document.getElementById('semantic-home'),
     homeGrid=document.getElementById('home-grid'),
     homeEmpty=document.getElementById('home-empty'),
     homeTitle=document.getElementById('home-title'),
     homeStatus=document.getElementById('home-status'),
     homeItems=[],homeSparkGradientSequence=0,
     trailDialog=document.getElementById('trail-dialog'),
     trailTitle=document.getElementById('trail-title'),
     trailMeta=document.getElementById('trail-meta'),
     trailContent=document.getElementById('trail-content'),
     trailBack=document.getElementById('trail-back'),
     trailHistory=[],trailData=null,trailRequest=0;
 var metricPromoteDialog=document.getElementById('metric-promote-dialog'),
     metricPromoteForm=document.getElementById('metric-promote-form'),
     metricPromoteLoading=document.getElementById('metric-promote-loading'),
     metricPromoteName=document.getElementById('metric-promote-name'),
     metricPromoteDescription=document.getElementById('metric-promote-description'),
     metricPromoteError=document.getElementById('metric-promote-error'),
     metricPromotionItemId='',metricPromotionRequest=0;
 function escapeHome(value){
   return String(value==null?'':value).replace(/[&<>"']/g,function(char){
     return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[char];
   });
 }
 function galleryShortRef(value){
   var text=String(value||'');
   return text.length>18?text.slice(0,8)+'…'+text.slice(-4):text;
 }
 function galleryTooltipSourceMarkup(options){
   options=options||{};
   var facts=(options.facts||[]).filter(function(fact){
     return fact&&fact[0]&&fact[1]!==null&&fact[1]!==undefined&&fact[1]!=='';
   });
   var status=String(options.status||'context'),statusClass=status.toLowerCase().replace(/[^a-z0-9_-]+/g,'-');
   return '<template class="gallery-tooltip-source">'
     +'<header class="gallery-tooltip-head"><span>'+escapeHome(options.eyebrow||'Explain this')+'</span><b class="'+escapeHome(statusClass)+'">'+escapeHome(status)+'</b></header>'
     +'<strong class="gallery-tooltip-title">'+escapeHome(options.title||'Context')+'</strong>'
     +(options.meaning?'<section class="gallery-tooltip-section"><span>'+escapeHome(options.meaningLabel||'What it means')+'</span><p>'+escapeHome(options.meaning)+'</p></section>':'')
     +(facts.length?'<dl class="gallery-tooltip-facts">'+facts.map(function(fact){return '<div><dt>'+escapeHome(fact[0])+'</dt><dd>'+escapeHome(fact[1])+'</dd></div>';}).join('')+'</dl>':'')
     +(options.evidence?'<section class="gallery-tooltip-section evidence"><span>'+escapeHome(options.evidenceLabel||'Why it is here')+'</span><p>'+escapeHome(options.evidence)+'</p></section>':'')
     +'</template>';
 }
 function setGalleryTooltipSource(target,markup){
   if(!target)return;
   var source=target.querySelector('.gallery-tooltip-source');
   if(source)source.remove();
   target.insertAdjacentHTML('beforeend',markup||'');
   if(galleryTooltipTarget===target)showGalleryTooltip(target);
 }
 var galleryTooltipTarget=null,galleryTooltipFrame=null,galleryTooltipHideTimer=null;
 function galleryTooltipElement(target){
   var tooltip=document.getElementById('gallery-explain-tooltip');
   if(!tooltip){
     tooltip=document.createElement('div');tooltip.id='gallery-explain-tooltip';tooltip.className='gallery-tooltip';
     tooltip.setAttribute('role','tooltip');tooltip.hidden=true;
   }
   var owner=target&&target.closest('dialog[open]')||document.body;
   if(tooltip.parentNode!==owner)owner.appendChild(tooltip);
   return tooltip;
 }
 function positionGalleryTooltip(){
   galleryTooltipFrame=null;
   var target=galleryTooltipTarget;
   if(!target||!target.isConnected){hideGalleryTooltip();return;}
   var tooltip=galleryTooltipElement(target);if(tooltip.hidden)return;
   var targetRect=target.getBoundingClientRect(),tooltipRect=tooltip.getBoundingClientRect(),dialog=target.closest('dialog[open]');
   var bounds=dialog?dialog.getBoundingClientRect():{top:0,right:window.innerWidth,bottom:window.innerHeight,left:0};
   var inset=11,gap=10,minLeft=bounds.left+inset,maxLeft=Math.max(minLeft,bounds.right-tooltipRect.width-inset);
   var left=Math.min(maxLeft,Math.max(minLeft,targetRect.left+(targetRect.width-tooltipRect.width)/2));
   var placement='top',top=targetRect.top-tooltipRect.height-gap;
   if(top<bounds.top+inset){placement='bottom';top=targetRect.bottom+gap;}
   var maxTop=Math.max(bounds.top+inset,bounds.bottom-tooltipRect.height-inset);
   top=Math.min(maxTop,Math.max(bounds.top+inset,top));
   var anchor=Math.min(tooltipRect.width-18,Math.max(10,targetRect.left+targetRect.width/2-left-4));
   tooltip.dataset.placement=placement;tooltip.style.setProperty('--gallery-tooltip-anchor',Math.round(anchor)+'px');
   tooltip.style.left=Math.round(left)+'px';tooltip.style.top=Math.round(top)+'px';
 }
 function scheduleGalleryTooltipPosition(){
   if(!galleryTooltipTarget||galleryTooltipFrame!==null)return;
   galleryTooltipFrame=requestAnimationFrame(positionGalleryTooltip);
 }
 function showGalleryTooltip(target){
   var source=target&&target.querySelector('.gallery-tooltip-source');if(!source)return;
   clearTimeout(galleryTooltipHideTimer);
   if(galleryTooltipTarget&&galleryTooltipTarget!==target)galleryTooltipTarget.removeAttribute('aria-describedby');
   galleryTooltipTarget=target;
   var tooltip=galleryTooltipElement(target);tooltip.dataset.tooltipKind=target.dataset.tooltipKind||'context';
   tooltip.innerHTML=source.innerHTML;tooltip.hidden=false;tooltip.classList.remove('visible');
   target.setAttribute('aria-describedby',tooltip.id);positionGalleryTooltip();
   requestAnimationFrame(function(){if(galleryTooltipTarget===target)tooltip.classList.add('visible');});
 }
 function hideGalleryTooltip(){
   var target=galleryTooltipTarget;galleryTooltipTarget=null;if(target)target.removeAttribute('aria-describedby');
   if(galleryTooltipFrame!==null)cancelAnimationFrame(galleryTooltipFrame);galleryTooltipFrame=null;
   var tooltip=document.getElementById('gallery-explain-tooltip');if(!tooltip)return;
   tooltip.classList.remove('visible');clearTimeout(galleryTooltipHideTimer);
   galleryTooltipHideTimer=setTimeout(function(){if(galleryTooltipTarget)return;tooltip.hidden=true;tooltip.innerHTML='';},140);
 }
 document.addEventListener('pointerover',function(event){
   var target=event.target.closest&&event.target.closest('[data-gallery-tooltip]');
   if(!target||target.contains(event.relatedTarget))return;showGalleryTooltip(target);
 });
 document.addEventListener('pointerout',function(event){
   var target=event.target.closest&&event.target.closest('[data-gallery-tooltip]');
   if(!target||target!==galleryTooltipTarget||target.contains(event.relatedTarget)||target.contains(document.activeElement))return;
   hideGalleryTooltip();
 });
 document.addEventListener('focusin',function(event){
   var target=event.target.closest&&event.target.closest('[data-gallery-tooltip]');if(target)showGalleryTooltip(target);
 });
 document.addEventListener('focusout',function(event){
   var target=event.target.closest&&event.target.closest('[data-gallery-tooltip]');if(!target)return;
   requestAnimationFrame(function(){if(target===galleryTooltipTarget&&!target.matches(':hover')&&!target.contains(document.activeElement))hideGalleryTooltip();});
 });
 document.addEventListener('scroll',scheduleGalleryTooltipPosition,true);
 window.addEventListener('resize',scheduleGalleryTooltipPosition);
 document.addEventListener('keydown',function(event){if(event.key==='Escape'&&galleryTooltipTarget)hideGalleryTooltip();});
 function formatHomeValue(value,display,unit){
   if(value===null||value===undefined||value==='')return '—';
   display=display||{};
   var rendered=value,number=typeof value==='number'?value:null;
   if(number===null&&typeof value==='string'&&/^[+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)$/.test(value.trim())){
     number=Number(value);
   }
   if(number!==null&&Number.isFinite(number)){
     var decimals=Number(display.decimals);
     var options=Number.isInteger(decimals)?{
       maximumFractionDigits:Math.max(0,Math.min(decimals,12)),
       minimumFractionDigits:Math.max(0,Math.min(decimals,12))
     }:{maximumFractionDigits:2};
     rendered=new Intl.NumberFormat(undefined,options).format(number);
   }else if(typeof value==='object'){
     try{rendered=JSON.stringify(value);}catch(ignore){rendered=String(value);}
   }
   var prefix=String(display.prefix||''),suffix=String(display.suffix||'');
   return prefix+String(rendered)+suffix+(!prefix&&!suffix&&unit?' '+String(unit):'');
 }
 function trailSectionLabel(section){
   return {meaning:'What it means',artifacts:'Where it lives',knowledge:'What the company knows',data:'What it is built from'}[section]||'Related evidence';
 }
 function trailStableValue(value){
   if(Array.isArray(value))return '['+value.map(trailStableValue).join(',')+']';
   if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(function(key){
     return JSON.stringify(key)+':'+trailStableValue(value[key]);
   }).join(',')+'}';
   return JSON.stringify(value);
 }
 function trailHandleKey(handle){return trailStableValue(handle||{});}
 function trailFrameIndex(handle){
   var key=trailHandleKey(handle);
   return trailHistory.findIndex(function(frame){return trailHandleKey(frame.handle)===key;});
 }
 function trailRouteSummary(data){
   data=data||{};
   var connections=data.connections||[],raw=data.route_summary||{},sections={meaning:0,artifacts:0,knowledge:0,data:0};
   connections.forEach(function(connection){
     var section=connection.section||'knowledge';sections[section]=(sections[section]||0)+1;
   });
   if(raw.sections)Object.keys(sections).forEach(function(section){
     sections[section]=Number(raw.sections[section]||0);
   });
   return {resolved:Number(raw.resolved==null?connections.length:raw.resolved),bounded:!!raw.bounded,sections:sections};
 }
 function trailConnectionContext(handle){
   var key=trailHandleKey(handle),pathStep=trailFrameIndex(handle);
   if(pathStep>=0)return {kind:'return',step:pathStep,label:'Returns to step '+(pathStep+1)};
   for(var step=0;step<trailHistory.length-1;step+=1){
     var found=((trailHistory[step].data||{}).connections||[]).some(function(connection){
       return trailHandleKey(connection.handle)===key;
     });
     if(found)return {kind:'converges',step:step,label:'Also reachable from step '+(step+1)};
   }
   return null;
 }
 function trailLoomMarkup(){
   if(!trailHistory.length)return '';
   var retained=trailHistory.reduce(function(total,frame){return total+trailRouteSummary(frame.data).resolved;},0);
   var steps=trailHistory.map(function(frame,index){
     var subject=(frame.data||{}).subject||{},summary=trailRouteSummary(frame.data),current=index===trailHistory.length-1;
     var mixes=[['meaning','Meaning'],['artifacts','Places'],['knowledge','Knowledge'],['data','Data']].map(function(pair){
       var count=summary.sections[pair[0]]||0;return count?'<i>'+pair[1]+' '+count+'</i>':'';
     }).join('');
     var link=index&&frame.via?'<span class="trail-loom-link"><b>'+escapeHome(frame.via.relationship||'related to')+'</b><i>→</i></span>':'';
     return link+'<button type="button" class="trail-loom-step'+(current?' current':'')+'" data-trail-step="'+index+'"'+(current?' aria-current="step"':'')+'>'
       +'<small>'+escapeHome(String(subject.kind||'evidence').replaceAll('_',' '))+'</small>'
       +'<strong>'+escapeHome(subject.label||'Evidence')+'</strong>'
       +'<span>'+(summary.resolved||0)+(summary.bounded?'+':'')+' nearby route'+(summary.resolved===1?'':'s')+'</span>'
       +(mixes?'<em>'+mixes+'</em>':'')+(current?'<u>You are here</u>':'')+'</button>';
   }).join('');
   return '<nav class="trail-loom" aria-label="How you got here"><header><span>How you got here</span><b>'
     +trailHistory.length+' step'+(trailHistory.length===1?'':'s')+' · '+retained+' route choice'+(retained===1?'':'s')+' retained</b></header>'
     +'<div class="trail-loom-track">'+steps+'</div></nav>';
 }
 function trailConnectionMarkup(connection,index){
   var shared=(connection.shared||[]).slice(0,3).map(function(item){return '<i>'+escapeHome(item)+'</i>';}).join(''),context=trailConnectionContext(connection.handle);
   return '<article class="trail-card"><div class="trail-card-copy">'
     +'<span>'+escapeHome(connection.relationship||'related to')+'</span>'
     +'<strong title="'+escapeHome(connection.label||'Related evidence')+'">'+escapeHome(connection.label||'Related evidence')+'</strong>'
     +(connection.detail?'<p>'+escapeHome(connection.detail)+'</p>':'')
     +(context?'<em class="trail-route-context '+context.kind+'">'+escapeHome(context.label)+'</em>':'')
     +(shared?'<div class="trail-shared">'+shared+'</div>':'')+'</div>'
     +'<div class="trail-card-actions"><button type="button" data-trail-follow="'+index+'">'+(context&&context.kind==='return'?'Return':'Follow')+'</button>'
     +(connection.url?'<a href="'+escapeHome(connection.url)+'" target="_blank" rel="noopener">Open ↗</a>':'')
     +'</div></article>';
 }
 function renderTrail(data){
   trailData=data||{};
   var subject=trailData.subject||{},facts=(trailData.facts||[]).slice(0,8);
   trailTitle.textContent=subject.label||'Follow the trail';
   var routeSummary=trailRouteSummary(trailData);
   trailMeta.textContent=(subject.kind||'evidence').replaceAll('_',' ')+' · '+routeSummary.resolved+(routeSummary.bounded?'+':'')+' nearby route'+(routeSummary.resolved===1?'':'s');
   trailBack.hidden=trailHistory.length<2;
   var factMarkup=facts.length?'<div class="trail-facts">'+facts.map(function(fact){
     return '<span><b>'+escapeHome(fact.label)+'</b>'+escapeHome(fact.value)+'</span>';
   }).join('')+'</div>':'';
   var groups={};
   (trailData.connections||[]).forEach(function(connection,index){
     var section=connection.section||'knowledge';
     (groups[section]||(groups[section]=[])).push({connection:connection,index:index});
   });
   var sections=['meaning','artifacts','knowledge','data'].map(function(section){
     var items=groups[section]||[];if(!items.length)return '';
     return '<section class="trail-section"><h3>'+trailSectionLabel(section)+'</h3><div class="trail-list">'
       +items.map(function(item){return trailConnectionMarkup(item.connection,item.index);}).join('')+'</div></section>';
   }).join('');
   trailContent.innerHTML=trailLoomMarkup()+'<section class="trail-subject"><div><span>Current evidence</span><h2>'+escapeHome(subject.label||'Evidence')+'</h2>'
     +(subject.detail?'<p>'+escapeHome(subject.detail)+'</p>':'')+factMarkup+'</div>'
     +(subject.url?'<a href="'+escapeHome(subject.url)+'" target="_blank" rel="noopener">Open source ↗</a>':'')
     +'</section>'+(sections||'<div class="trail-empty"><b>No further breadcrumbs surfaced.</b><span>This object is still a valid endpoint.</span></div>');
 }
 function showTrailStep(index){
   if(index<0||index>=trailHistory.length)return;
   trailHistory=trailHistory.slice(0,index+1);
   renderTrail(trailHistory[index].data);
 }
 function followTrailConnection(connection){
   if(!connection||!connection.handle)return;
   var earlier=trailFrameIndex(connection.handle);
   if(earlier>=0){showTrailStep(earlier);return;}
   openTrail(connection.handle,true,connection);
 }
 async function openTrail(handle,push,via){
   if(!trailDialog||!handle)return;
   if(!trailDialog.open)trailDialog.showModal();
   var request=++trailRequest;
   trailContent.innerHTML='<div class="trail-loading"><i></i><strong>Following the evidence…</strong><span>Resolving only what your account can see</span></div>';
   try{
     var response=await fetch('/api/calliope/trails',{method:'POST',headers:{'content-type':'application/json',accept:'application/json'},body:JSON.stringify({handle:handle,limit:24})}),data={};
     try{data=await response.json();}catch(ignore){}
     if(request!==trailRequest)return;
     if(!response.ok)throw new Error(data.error&&data.error.message||'Could not follow that trail');
     var canonical=(data.subject||{}).handle||handle;
     if(push!==false){
       var earlier=trailFrameIndex(canonical);
       if(earlier>=0){trailHistory=trailHistory.slice(0,earlier+1);trailHistory[earlier].data=data;}
       else trailHistory.push({handle:canonical,data:data,via:via||null});
     }else if(trailHistory.length){
       trailHistory[trailHistory.length-1].handle=canonical;
       trailHistory[trailHistory.length-1].data=data;
     }else trailHistory.push({handle:canonical,data:data,via:via||null});
     renderTrail(data);
   }catch(error){
     if(request!==trailRequest)return;
     trailTitle.textContent='Trail unavailable';trailMeta.textContent='Evidence resolver';
     trailContent.innerHTML='<div class="trail-error"><b>Those breadcrumbs could not be resolved.</b><span>'+escapeHome(error&&error.message||'Try again shortly.')+'</span></div>';
   }
 }
 function galleryTrailReference(crumb){
   var handle=crumb&&crumb.handle||{};
   return handle.object_id||handle.slug||handle.table||handle.column||handle.id||'';
 }
 function homeTrailTooltip(crumb){
   crumb=crumb||{};
   var kind=String(crumb.kind||'evidence').replaceAll('_',' '),relationship=String(crumb.relationship||'related to');
   var reference=galleryTrailReference(crumb),handle=crumb.handle||{};
   return galleryTooltipSourceMarkup({
     eyebrow:'Semantic Home · evidence trail',status:relationship,title:crumb.label||kind,
     meaning:'This '+kind+' is connected through the “'+relationship+'” edge.',
     evidence:crumb.detail||'Follow this breadcrumb to inspect the governed evidence behind the connection.',
     evidenceLabel:'Resolved detail',
     facts:[['Kind',kind],['Reference',galleryShortRef(reference)],['Version',handle.version||handle.pinned_version]]
   });
 }
 function homeContextTooltip(key,value){
   return galleryTooltipSourceMarkup({
     eyebrow:'Semantic Home · context',status:'frozen',title:String(key||'context').replaceAll('_',' '),
     meaning:'This frozen parameter scopes the named business object; replay uses the same value.',
     evidence:value,evidenceLabel:'Bound value'
   });
 }
 function homeValueTooltip(item,preview,error){
   item=item||{};preview=preview||null;
   var source=item.source||{},presented=item.presentation||{},hasLast=presented.last_rendered_value!==null&&presented.last_rendered_value!==undefined;
   var status=error?'unavailable':preview&&preview.status||(!hasLast?'recreating':'pinned');
   var rawValue=preview?preview.value:presented.last_rendered_value;
   var display=preview&&preview.display||item.display||{},unit=item.unit||preview&&preview.unit||'';
   var artifactSlug=preview&&preview.artifact&&preview.artifact.slug||source.slug||'';
   var artifactVersion=preview&&preview.artifact&&preview.artifact.version||item.version||source.pinned_version||'';
   var context=item.context&&typeof item.context==='object'?JSON.stringify(item.context):item.context||'';
   var evidence=error?'Live recreation failed: '+String(error&&error.message||error):preview
     ?'Recreated now under the signed-in warehouse identity'+(artifactSlug?' from '+artifactSlug+(artifactVersion?' v'+artifactVersion:''):'')+'.'
     :hasLast?'Showing the preserved rendered value while the governed definition is replayed.'
     :'The governed definition is being replayed under the signed-in warehouse identity.';
   return galleryTooltipSourceMarkup({
     eyebrow:'Semantic Home · governed value',status:status,title:item.title||'Named business object',
     meaning:item.definition||item.description||'Live reconstruction of this named business object from its frozen semantic definition.',
     evidence:evidence,evidenceLabel:'Current observation',
     facts:[
       ['Value',rawValue!==null&&rawValue!==undefined?formatHomeValue(rawValue,display,unit):''],
       ['Unit',unit],['Engine',preview&&preview.engine],
       ['Runtime',preview&&preview.elapsed_ms!=null?preview.elapsed_ms+' ms':''],
       ['Rows',preview&&preview.row_count],
       ['Artifact',artifactSlug+(artifactVersion?' · v'+artifactVersion:'')],
       ['Object',galleryShortRef(source.object_id||item.id)],['Context',context]
     ]
   });
 }
 function homeTrailMarkup(trail){
   var items=(Array.isArray(trail)?trail:[]).slice(0,4);
   if(!items.length)return '';
   return '<div class="home-trail" aria-label="Evidence trail">'+items.map(function(crumb){
     var relationship=crumb.relationship||'',label=crumb.label||crumb.kind||'evidence';
     return '<span class="home-crumb" tabindex="0" data-gallery-tooltip data-tooltip-kind="trail" aria-label="'
       +escapeHome(label+'. Explain this Semantic Home relationship.')+'"><span>'+escapeHome(label)+'</span>'
       +homeTrailTooltip(crumb)+'</span>';
   }).join('')+'</div>';
 }
 function homeContextMarkup(context){
   if(!context||typeof context!=='object')return '';
   var entries=Object.entries(context).slice(0,3);
   if(!entries.length)return '';
   return '<div class="home-context">'+entries.map(function(entry){
     var value=typeof entry[1]==='object'?JSON.stringify(entry[1]):String(entry[1]);
     return '<span tabindex="0" data-gallery-tooltip data-tooltip-kind="context" aria-label="'
       +escapeHome(entry[0].replaceAll('_',' ')+'. Explain this frozen context.')+'">'
       +escapeHome(entry[0].replaceAll('_',' '))+': '+escapeHome(value)+homeContextTooltip(entry[0],value)+'</span>';
   }).join('')+'</div>';
 }
 function homeMetricSparkline(item){
   var points=(item.series||[]).map(function(point){
     var value=point&&point.numeric_value!==null&&point.numeric_value!==undefined?Number(point.numeric_value):Number(point&&point.value);
     return Number.isFinite(value)?value:null;
   }).filter(function(value){return value!==null;});
   if(points.length<2)return '';
   var w=420,h=115,min=Math.min.apply(null,points),max=Math.max.apply(null,points);if(max===min){var homeFlatPad=Math.abs(max)*.08||1;min-=homeFlatPad;max+=homeFlatPad;}var range=max-min;
   var x=function(index){return index/Math.max(1,points.length-1)*w;},y=function(value){return 8+(max-value)/range*(h-16);};
   var path=points.map(function(value,index){return (index?'L':'M')+' '+x(index).toFixed(2)+' '+y(value).toFixed(2);}).join(' '),gradientId='home-metric-area-gradient-'+(++homeSparkGradientSequence);
   return '<div class="home-metric-spark" aria-hidden="true"><svg viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none"><defs><linearGradient id="'+gradientId+'" x1="0" y1="0" x2="0" y2="1"><stop class="home-metric-area-top" offset="0%"></stop><stop class="home-metric-area-mid" offset="54%"></stop><stop class="home-metric-area-clear" offset="82%"></stop><stop class="home-metric-area-floor" offset="100%"></stop></linearGradient></defs><path class="home-metric-area" fill="url(#'+gradientId+')" d="'+path+' L '+w+' '+h+' L 0 '+h+' Z"></path><path class="home-metric-line" d="'+path+'"></path></svg></div>';
 }
 function homeMetricTooltip(item){
   var snapshot=item.snapshot||{},source=item.source||{},stamp=snapshot.data_as_of||snapshot.observed_at||'',params=source.params||{};
   return galleryTooltipSourceMarkup({
     eyebrow:'Semantic Home · governed metric',status:snapshot.status||item.status||'observed',title:item.title||source.name||'Metric',
     meaning:item.description||'Latest durable observation of this governed metric instance.',
     evidence:snapshot.observation_id?'Materialized observation '+snapshot.observation_id+(stamp?' at '+stamp:'')+'.':'No materialized observation exists for this parameter scope yet.',
     evidenceLabel:'Current observation',facts:[
       ['Value',snapshot.value!==undefined?formatHomeValue(snapshot.value,item.display):''],
       ['Metric',source.name],['Params',Object.keys(params).length?JSON.stringify(params):'defaults'],
       ['Definition','v'+(item.version||source.pinned_version||'?')],['Observed',stamp]
     ]
   });
 }
 function homeArtifactGlyph(item){
   var kind=String(item&&item.app_kind||'artifact').toLowerCase();
   if(kind.indexOf('dashboard')>=0)return '▦';
   if(kind.indexOf('deck')>=0||kind.indexOf('slide')>=0)return '▷';
   if(kind.indexOf('report')>=0)return '▤';
   return '◈';
 }
 function homeTileMarkup(item){
   var object=item.kind==='artifact_object',metric=item.kind==='metric',unavailable=item.status==='unavailable';
   var presented=item.presentation||{},last=presented.last_rendered_value;
   var metricSnapshot=item.snapshot||{},metricValue=metricSnapshot.value;
   var valueText=metric?(metricValue===null||metricValue===undefined?'No observation':formatHomeValue(metricValue,item.display,item.unit)):(last===null||last===undefined?'Reading current value…':formatHomeValue(last,item.display,item.unit));
   var value=object?'<div class="home-value '+(last===null||last===undefined?'loading':'')+'" data-home-value="'
     +escapeHome(item.id)+'" tabindex="0" data-gallery-tooltip data-tooltip-kind="value" aria-label="Live value for '
     +escapeHome(item.title||'named business object')+'. Explain this governed value."><span data-home-value-text>'
     +escapeHome(valueText)+'</span>'+homeValueTooltip(item,null,null)+'</div>':metric?'<div class="home-value '+(metricValue===null||metricValue===undefined?'loading':'')+'" tabindex="0" data-gallery-tooltip data-tooltip-kind="value" aria-label="Current observed value for '+escapeHome(item.title||'metric')+'"><span data-home-value-text>'+escapeHome(valueText)+'</span>'+homeMetricTooltip(item)+'</div>':'';
   var thumbnail=!object&&!metric&&item.thumbnail_url?'<a class="home-thumb pending" data-home-thumbnail href="'+escapeHome(item.open_url||'#')
     +'" target="_blank" rel="noopener"><span class="home-thumb-fallback" aria-hidden="true"><b>'+homeArtifactGlyph(item)+'</b><small>Preview pending</small></span>'
     +'<img src="'+escapeHome(item.thumbnail_url)+'" alt="" loading="lazy" decoding="async"></a>':'';
   var context=object?homeContextMarkup(item.context):metric?homeContextMarkup(item.params):'';
   var versionNote=metric?'<span class="home-version-note">Definition v'+escapeHome(item.version||'?')+'</span>':item.newer_version_available?'<span class="home-version-note">Pinned v'+escapeHome(item.version)
     +' · v'+escapeHome(item.latest_version)+' exists</span>':'<span class="home-version-note"></span>';
   var open=item.open_url?'<a href="'+escapeHome(item.open_url)+'"'+(metric?'':' target="_blank" rel="noopener"')+'>Open</a>':'';
   var ask=metric?'<button type="button" data-home-metric-ask="'+escapeHome((item.source||{}).name||item.title)+'" data-home-metric-params="'+escapeHome(JSON.stringify((item.source||{}).params||{}))+'">✦ Ask Calliope</button>':'';
   var promote=object&&item.status==='ready'&&item.evaluator_shape==='scalar'?'<button type="button" data-home-promote="'+escapeHome(item.id)+'">Promote to metric</button>':'';
   return '<article class="home-tile '+(metric?'metric':object?'object':'artifact')+(unavailable?' unavailable':'')+'" data-home-id="'+escapeHome(item.id)+'">'
     +(metric?homeMetricSparkline(item):'')+'<div class="home-tile-content">'+thumbnail+'<div class="home-tile-main">'
     +'<span class="home-kicker"><i></i>'+(metric?'Governed metric':object?'Named business object':escapeHome(item.app_kind||'artifact'))+'</span>'
     +'<h3 title="'+escapeHome(item.title||'Home item')+'">'+escapeHome(item.title||'Home item')+'</h3>'
     +value
     +(item.description?'<p class="home-tile-desc">'+escapeHome(item.description)+'</p>':'')
     +context+homeTrailMarkup(item.trail)+'</div></div>'
     +'<div class="home-tile-actions">'+versionNote+open+ask+promote
     +'<button type="button" data-home-trail="'+escapeHome(item.id)+'">Follow trail</button>'
     +'<button type="button" data-home-remove="'+escapeHome(item.id)+'">Remove</button></div></article>';
 }
 function hydrateHomeThumbnails(root){
   if(!root)return;
   [].forEach.call(root.querySelectorAll('[data-home-thumbnail]'),function(frame){
     if(frame.dataset.hydrated==='true')return;
     frame.dataset.hydrated='true';
     var image=frame.querySelector('img'),tries=0,retryTimer=null;
     if(!image)return;
     var base=image.getAttribute('src');
     function ready(){
       if(retryTimer){clearTimeout(retryTimer);retryTimer=null;}
       frame.classList.add('ready');
       frame.classList.remove('pending','unavailable');
     }
     function retry(){
       if(retryTimer)return;
       frame.classList.remove('ready');
       if(++tries>5){frame.classList.remove('pending');frame.classList.add('unavailable');return;}
       frame.classList.add('pending');
       retryTimer=setTimeout(function(){
         retryTimer=null;
         if(!frame.isConnected)return;
         image.src=base+(base.indexOf('?')>=0?'&':'?')+'r='+tries;
       },tries*2500);
     }
     image.addEventListener('load',ready);
     image.addEventListener('error',retry);
     if(image.complete){
       if(image.naturalWidth>0)ready();else setTimeout(retry,0);
     }
   });
 }
 function syncGalleryPins(){
   var pinned={};
   homeItems.forEach(function(item){
     if(item.kind==='artifact'&&item.source&&item.source.slug)pinned[item.source.slug]=item.id;
   });
   [].forEach.call(document.querySelectorAll('[data-home-pin]'),function(button){
     var itemId=pinned[button.dataset.homePin]||'';
     button.dataset.homeItemId=itemId;
     button.setAttribute('aria-pressed',String(Boolean(itemId)));
     button.innerHTML=itemId?'<b aria-hidden="true">✓</b><span>Home</span>':'<b aria-hidden="true">＋</b><span>Pin</span>';
     button.title=itemId?'Remove this artifact from your private Home':'Pin this artifact to your private Home';
   });
 }
 async function previewHomeItem(item){
   if(item.kind!=='artifact_object'||item.status!=='ready')return;
   var node=homeGrid&&homeGrid.querySelector('[data-home-value="'+CSS.escape(item.id)+'"]');
   if(!node)return;
   try{
     var response=await fetch('/api/calliope/home/items/'+encodeURIComponent(item.id)+'/preview',{headers:{accept:'application/json'}});
     var data={};try{data=await response.json();}catch(ignore){}
     if(!response.ok||!data.preview)throw new Error(data.error&&data.error.message||'Current value unavailable');
     if(data.preview.status==='error')throw new Error(data.preview.error||'Current value unavailable');
     node.className='home-value';
     var valueText=node.querySelector('[data-home-value-text]');
     if(valueText)valueText.textContent=formatHomeValue(data.preview.value,data.preview.display,item.unit||data.preview.unit);
     setGalleryTooltipSource(node,homeValueTooltip(item,data.preview,null));
   }catch(error){
     node.className='home-value error';
     var errorText=node.querySelector('[data-home-value-text]');
     if(errorText)errorText.textContent=error&&error.message?error.message:'Current value unavailable';
     setGalleryTooltipSource(node,homeValueTooltip(item,null,error));
   }
 }
 function renderHome(data){
   if(!homeSection||!homeGrid)return;
   homeItems=Array.isArray(data&&data.items)?data.items:[];
   homeSection.hidden=false;
   if(homeTitle&&data&&data.home&&data.home.title)homeTitle.textContent=data.home.title;
   homeSparkGradientSequence=0;
   homeGrid.innerHTML=homeItems.map(homeTileMarkup).join('');
   homeEmpty.hidden=Boolean(homeItems.length);
   homeStatus.classList.remove('error');
   homeStatus.textContent=homeItems.length?homeItems.length+' pinned object'+(homeItems.length===1?'':'s'):'Private to your signed-in account';
   hydrateHomeThumbnails(homeGrid);
   syncGalleryPins();
   homeItems.forEach(previewHomeItem);
 }
 async function loadHome(){
   if(!homeSection)return;
   try{
     var response=await fetch('/api/calliope/home',{headers:{accept:'application/json'}}),data={};
     try{data=await response.json();}catch(ignore){}
     if(!response.ok)throw new Error(data.error&&data.error.message||'Could not load your Home');
     renderHome(data);
   }catch(error){
     homeSection.hidden=false;
     homeStatus.classList.add('error');
     homeStatus.textContent=error&&error.message?error.message:'Could not load your Home';
     homeEmpty.hidden=false;
   }
 }
 async function removeHomeItem(itemId,button){
   if(!itemId||button.disabled)return;
   button.disabled=true;
   try{
     var response=await fetch('/api/calliope/home/items/'+encodeURIComponent(itemId),{method:'DELETE',headers:{accept:'application/json'}}),data={};
     try{data=await response.json();}catch(ignore){}
     if(!response.ok)throw new Error(data.error&&data.error.message||'Could not remove the pin');
     await loadHome();
   }catch(error){
     button.disabled=false;
     homeStatus.classList.add('error');
     homeStatus.textContent=error&&error.message?error.message:'Could not remove the pin';
   }
 }
 async function openMetricPromotion(itemId,button){
   if(!metricPromoteDialog||!itemId)return;
   metricPromotionItemId=itemId;var requestId=++metricPromotionRequest;
   metricPromoteForm.hidden=true;metricPromoteLoading.hidden=false;
   metricPromoteLoading.textContent='Checking the immutable definition…';metricPromoteError.textContent='';
   if(!metricPromoteDialog.open)metricPromoteDialog.showModal();
   if(button)button.disabled=true;
   try{
     var response=await fetch('/api/calliope/home/items/'+encodeURIComponent(itemId)+'/metric-promotion',{headers:{accept:'application/json'}}),data={};
     try{data=await response.json();}catch(ignore){}
     if(requestId!==metricPromotionRequest)return;
     if(!response.ok||!data.draft)throw new Error(data.error&&data.error.message||'This object cannot become a metric');
     var draft=data.draft||{},artifact=draft.artifact||{},facts=[];
     document.getElementById('metric-promote-title').textContent=draft.title||'Promote to metric';
     document.getElementById('metric-promote-source-title').textContent=draft.title||artifact.object_id||'Named value';
     document.getElementById('metric-promote-source-copy').textContent=(artifact.name||artifact.slug||'Published artifact')+' · version '+(artifact.version||'?')+' · '+(artifact.object_id||'semantic object');
     document.getElementById('metric-promote-current').textContent=draft.current===null||draft.current===undefined?'':formatHomeValue(draft.current,draft.display,draft.unit);
     metricPromoteName.value=draft.suggested_name||'';metricPromoteDescription.value=draft.description||'';
     if(draft.formula)facts.push(['Formula',draft.formula]);
     if(draft.grain)facts.push(['Grain',draft.grain]);
     if((draft.source_tables||[]).length)facts.push(['Sources',draft.source_tables.join(', ')]);
     if(artifact.context&&Object.keys(artifact.context).length)facts.push(['Frozen context',JSON.stringify(artifact.context)]);
     document.getElementById('metric-promote-facts').innerHTML=facts.map(function(fact){return '<span title="'+escapeHome(fact[1])+'"><b>'+escapeHome(fact[0])+':</b> '+escapeHome(fact[1])+'</span>';}).join('');
     metricPromoteLoading.hidden=true;metricPromoteForm.hidden=false;metricPromoteName.focus();metricPromoteName.select();
   }catch(error){
     if(requestId!==metricPromotionRequest)return;
     metricPromoteLoading.textContent=error&&error.message?error.message:'This object cannot become a metric';
   }finally{if(button)button.disabled=false;}
 }
 async function submitMetricPromotion(event){
   event.preventDefault();if(!metricPromotionItemId||!metricPromoteForm.reportValidity())return;
   var submit=document.getElementById('metric-promote-submit');submit.disabled=true;
   metricPromoteError.textContent='';
   try{
     var response=await fetch('/api/calliope/home/items/'+encodeURIComponent(metricPromotionItemId)+'/metric-promotion',{
       method:'POST',headers:{'content-type':'application/json',accept:'application/json'},
       body:JSON.stringify({name:metricPromoteName.value,description:metricPromoteDescription.value})
     }),data={};
     try{data=await response.json();}catch(ignore){}
     if(!response.ok)throw new Error(data.error&&data.error.message||'The metric could not be created');
     var metricName=data.metric||metricPromoteName.value;metricPromoteDialog.close();await loadHome();
     homeStatus.classList.remove('error');homeStatus.textContent=metricName+' is now a governed metric';
   }catch(error){metricPromoteError.textContent=error&&error.message?error.message:'The metric could not be created';}
   finally{submit.disabled=false;}
 }
 async function toggleArtifactPin(button){
   if(button.disabled)return;
   button.disabled=true;
   var itemId=button.dataset.homeItemId||'',slug=button.dataset.homePin;
   try{
     var response=await fetch(itemId?'/api/calliope/home/items/'+encodeURIComponent(itemId):'/api/calliope/home/items',{
       method:itemId?'DELETE':'POST',headers:{'content-type':'application/json',accept:'application/json'},
       body:itemId?undefined:JSON.stringify({kind:'artifact',slug:slug})
     }),data={};
     try{data=await response.json();}catch(ignore){}
     if(!response.ok)throw new Error(data.error&&data.error.message||'Could not update your Home');
     await loadHome();
   }catch(error){
     button.disabled=false;
     if(homeStatus){homeStatus.classList.add('error');homeStatus.textContent=error&&error.message?error.message:'Could not update your Home';}
   }
 }
 async function askCalliopeAboutArtifact(button){
   if(!button||button.disabled)return;
   var slug=button.dataset.galleryAsk;
   if(!slug)return;
   button.disabled=true;button.classList.add('loading');
   try{
     var response=await fetch('/api/calliope/gallery/artifacts/'+encodeURIComponent(slug)+'/ask',{
       method:'POST',headers:{'content-type':'application/json',accept:'application/json'},body:'{}'
     }),data={};
     try{data=await response.json();}catch(ignore){}
     if(!response.ok)throw new Error(data.error&&data.error.message||'Could not open this artifact in Calliope');
     if(!data.new_session||!data.url)throw new Error('Calliope did not create a fresh artifact notebook');
     window.location.assign(data.url);
   }catch(error){
     button.disabled=false;button.classList.remove('loading');
     if(homeStatus){homeStatus.classList.add('error');homeStatus.textContent=error&&error.message?error.message:'Could not open Calliope';}
     else window.alert(error&&error.message?error.message:'Could not open Calliope');
   }
 }
 if(homeGrid)homeGrid.addEventListener('click',function(event){
   var promoteButton=event.target.closest('[data-home-promote]');
   if(promoteButton){openMetricPromotion(promoteButton.dataset.homePromote,promoteButton);return;}
   var trailButton=event.target.closest('[data-home-trail]');
   if(trailButton){
     var item=homeItems.find(function(candidate){return candidate.id===trailButton.dataset.homeTrail;});
     if(item&&item.source){trailHistory=[];openTrail(item.source,true);}return;
   }
   var button=event.target.closest('[data-home-remove]');
   if(button)removeHomeItem(button.dataset.homeRemove,button);
 });
 if(metricPromoteDialog){
   metricPromoteForm.addEventListener('submit',submitMetricPromotion);
   document.getElementById('metric-promote-close').addEventListener('click',function(){metricPromoteDialog.close();});
   document.getElementById('metric-promote-cancel').addEventListener('click',function(){metricPromoteDialog.close();});
   metricPromoteDialog.addEventListener('close',function(){metricPromotionRequest++;metricPromotionItemId='';metricPromoteError.textContent='';});
 }
 if(trailDialog){
   document.getElementById('trail-close').addEventListener('click',function(){trailRequest++;trailDialog.close();});
   trailBack.addEventListener('click',function(){
     if(trailHistory.length<2)return;showTrailStep(trailHistory.length-2);
   });
   trailContent.addEventListener('click',function(event){
     var step=event.target.closest('[data-trail-step]');
     if(step){showTrailStep(Number(step.dataset.trailStep));return;}
     var button=event.target.closest('[data-trail-follow]');if(!button||!trailData)return;
     var connection=(trailData.connections||[])[Number(button.dataset.trailFollow)];
     followTrailConnection(connection);
   });
   trailDialog.addEventListener('close',function(){trailRequest++;trailHistory=[];trailData=null;});
 }
 [].forEach.call(document.querySelectorAll('[data-home-pin]'),function(button){
   button.addEventListener('click',function(){toggleArtifactPin(button);});
 });
 [].forEach.call(document.querySelectorAll('[data-gallery-ask]'),function(button){
   button.addEventListener('click',function(){askCalliopeAboutArtifact(button);});
 });

 var q=document.getElementById('q'),
     chips=[].slice.call(document.querySelectorAll('.chip')),
     cards=[].slice.call(document.querySelectorAll('.card')),
     none=document.getElementById('none'),
     semantic=document.getElementById('semantic-launch'),
     semanticButton=document.getElementById('semantic-launch-button'),
     semanticQuery=document.getElementById('semantic-launch-query'),
     semanticLocal=document.getElementById('semantic-launch-local'),
     semanticScope=document.getElementById('semantic-launch-scope'),
     semanticError=document.getElementById('semantic-launch-error'),
     kind='',semanticBusy=false;
 function queryText(){return q?(q.value||'').trim().replace(/\\s+/g,' '):'';}
 function metricViewActive(){var metricsBrowser=document.getElementById('metrics-browser');return Boolean(metricsBrowser&&!metricsBrowser.hidden);}
 function syncSemantic(shown){
   if(!semantic)return;
   if(metricViewActive()){semantic.hidden=true;return;}
   var text=queryText(),ready=text.length>=2;
   semantic.hidden=!ready;
   if(!ready)return;
   semanticQuery.textContent='“'+text+'”';
   semanticLocal.textContent=shown+' published match'+(shown===1?'':'es')+' here';
   if(!semanticBusy){
     semantic.classList.remove('has-error');
     semanticError.textContent='';
     semanticScope.textContent='docs · artifacts · warehouse semantics';
   }
 }
 function apply(){
   var t=queryText().toLowerCase(),shown=0;
   cards.forEach(function(c){
     var ok=(!kind||c.dataset.kind===kind)&&(!t||c.dataset.search.indexOf(t)>=0);
     c.style.display=ok?'':'none'; if(ok)shown++;
   });
   if(none)none.style.display=shown?'none':'block';
   syncSemantic(shown);
 }
 async function launchSemantic(){
   var text=queryText();
   if(!semantic||!semanticButton||semanticBusy||text.length<2)return;
   semanticBusy=true;
   semantic.classList.remove('has-error');
   semantic.classList.add('launching');
   semanticButton.disabled=true;
   semanticError.textContent='';
   semanticScope.textContent='resolving a new evidence workspace…';
   document.body.classList.add('calliope-launching');
   try{
     var response=await fetch('/api/calliope/evidence-explorations',{
       method:'POST',headers:{'content-type':'application/json','accept':'application/json'},
       body:JSON.stringify({query:text,limit:24})
     });
     var data={};
     try{data=await response.json();}catch(ignore){}
     if(!response.ok)throw new Error((data.error&&data.error.message)||'Could not open the evidence workspace');
     if(!data.url)throw new Error('Calliope did not return a workspace');
     window.location.assign(data.url);
   }catch(error){
     semanticBusy=false;
     semantic.classList.remove('launching');
     semantic.classList.add('has-error');
     semanticButton.disabled=false;
     semanticScope.textContent='docs · artifacts · warehouse semantics';
     semanticError.textContent=error&&error.message?error.message:'Could not open Calliope';
     document.body.classList.remove('calliope-launching');
   }
 }
 if(q){
   q.addEventListener('input',apply);
   q.addEventListener('keydown',function(e){
     if(e.key==='Enter'&&!e.isComposing&&!metricViewActive()&&queryText().length>=2){e.preventDefault();launchSemantic();}
   });
 }
 if(semanticButton)semanticButton.addEventListener('click',launchSemantic);
 chips.forEach(function(ch){ch.addEventListener('click',function(){
   kind=ch.dataset.kind||'';
   chips.forEach(function(o){o.setAttribute('aria-pressed',String(o===ch))});
   apply();
 })});
 // "/" focuses search, the way every browse page should behave
 document.addEventListener('keydown',function(e){
   if(q&&e.key==='/'&&document.activeElement!==q){e.preventDefault();q.focus();}
   if(q&&e.key==='Escape'&&document.activeElement===q){q.value='';apply();q.blur();}
 });
 if(homeSection){
   loadHome();
   window.addEventListener('gallery-home-changed',loadHome);
 }
 apply();
})();
"""

_GALLERY_METRICS_JS = """
(function(){
 var browser=document.getElementById('metrics-browser'),artifactBrowser=document.getElementById('artifact-browser');
 if(!browser)return;
 var grid=document.getElementById('metric-gallery-grid'),statusNode=document.getElementById('metric-browser-status'),
     empty=document.getElementById('metric-gallery-empty'),category=document.getElementById('metric-category'),
     countNode=document.getElementById('gallery-metric-count'),q=document.getElementById('q'),
     artifactChips=document.getElementById('artifact-kind-chips'),kicker=document.getElementById('gallery-kicker'),
     title=document.getElementById('gallery-title'),tally=document.getElementById('gallery-tally'),
     viewButtons=[].slice.call(document.querySelectorAll('[data-gallery-view]')),
     relationButtons=[].slice.call(document.querySelectorAll('[data-metric-relation]')),
     lens=document.getElementById('metric-lens'),lensTitle=document.getElementById('metric-lens-title'),
     lensMeta=document.getElementById('metric-lens-meta'),lensActions=document.getElementById('metric-lens-actions'),
     lensContent=document.getElementById('metric-lens-content'),lensClose=document.getElementById('metric-lens-close'),
     calliope=browser.dataset.calliope==='true';
 var state={view:'artifacts',loaded:false,loading:false,metrics:[],categories:[],relation:'all',
   pinIds:{},detail:null,detailDays:90,selected:[],request:0,busy:new Set()};
 var sparkGradientSequence=0;
 function esc(value){return String(value==null?'':value).replace(/[&<>"']/g,function(char){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[char];});}
 function text(value){return String(value==null?'':value).replace(/\\s+/g,' ').trim();}
 function queryText(){return q?text(q.value).toLowerCase():'';}
 function relative(value){
   if(!value)return 'not observed';var time=Date.parse(value);if(!Number.isFinite(time))return String(value);
   var seconds=Math.max(0,(Date.now()-time)/1000),steps=[[3600,60,'m'],[86400,3600,'h'],[604800,86400,'d'],[2629800,604800,'w'],[31557600,2629800,'mo']];
   if(seconds<90)return 'just now';for(var i=0;i<steps.length;i++){if(seconds<steps[i][0])return Math.floor(seconds/steps[i][1])+steps[i][2]+' ago';}
   return Math.floor(seconds/31557600)+'y ago';
 }
 function numeric(value){
   if(typeof value==='number'&&Number.isFinite(value))return value;
   if(typeof value==='string'&&value.trim()!==''&&Number.isFinite(Number(value)))return Number(value);
   if(value&&typeof value==='object'){
     if(Number.isFinite(Number(value.value)))return Number(value.value);
     if(value.row&&typeof value.row==='object'){
       var found=Object.values(value.row).find(function(item){return typeof item==='number'&&Number.isFinite(item);});
       if(found!==undefined)return Number(found);
     }
   }return null;
 }
 function formatValue(value,display,name){
   var number=numeric(value);if(number===null)return value==null?'—':typeof value==='object'?JSON.stringify(value):String(value);
   display=display||{};var hint=(String(display.format||'')+' '+String(display.unit||'')+' '+String(name||'')).toLowerCase();
   var decimals=Number.isInteger(Number(display.decimals))?Math.max(0,Math.min(Number(display.decimals),8)):2,rendered='';
   if(display.currency||/(?:currency|revenue|sales|amount|arr|mrr|dollar|usd)/.test(hint)){
     try{rendered=new Intl.NumberFormat(undefined,{style:'currency',currency:display.currency||'USD',notation:Math.abs(number)>=10000?'compact':'standard',maximumFractionDigits:decimals}).format(number);}catch(ignore){}
   }else if(/(?:percent|percentage|pct|rate|%)/.test(hint)){
     var pct=Math.abs(number)<=1.25?number:number/100;
     rendered=new Intl.NumberFormat(undefined,{style:'percent',maximumFractionDigits:decimals}).format(pct);
   }
   if(!rendered)rendered=new Intl.NumberFormat(undefined,{notation:Math.abs(number)>=100000?'compact':'standard',maximumFractionDigits:decimals}).format(number);
   if(display.prefix)rendered=String(display.prefix)+rendered;if(display.suffix)rendered+=String(display.suffix);
   if(display.unit&&!display.prefix&&!display.suffix&&!/(?:%|usd|currency)/i.test(String(display.unit)))rendered+=' '+display.unit;
   return rendered;
 }
 function points(metric){return (metric.series||[]).map(function(point){return {value:numeric(point.numeric_value!=null?point.numeric_value:point.value),point:point};}).filter(function(item){return item.value!==null;});}
 function sparkline(metric){
   var values=points(metric);if(values.length<2)return '';
   var w=600,h=210,pad=0,min=Math.min.apply(null,values.map(function(item){return item.value;})),max=Math.max.apply(null,values.map(function(item){return item.value;}));if(max===min){var cardFlatPad=Math.abs(max)*.08||1;min-=cardFlatPad;max+=cardFlatPad;}var range=max-min;
   var x=function(index){return pad+index/Math.max(1,values.length-1)*(w-pad*2);},y=function(value){return 18+(max-value)/range*(h-36);};
   var path=values.map(function(item,index){return (index?'L':'M')+' '+x(index).toFixed(2)+' '+y(item.value).toFixed(2);}).join(' '),gradientId='metric-card-area-gradient-'+(++sparkGradientSequence);
   return '<div class="metric-card-chart" aria-hidden="true"><svg viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none"><defs><linearGradient id="'+gradientId+'" x1="0" y1="0" x2="0" y2="1"><stop class="metric-card-area-top" offset="0%"></stop><stop class="metric-card-area-mid" offset="54%"></stop><stop class="metric-card-area-clear" offset="82%"></stop><stop class="metric-card-area-floor" offset="100%"></stop></linearGradient></defs><path class="metric-card-area" fill="url(#'+gradientId+')" d="'+path+' L '+x(values.length-1)+' '+h+' L 0 '+h+' Z"></path><path class="metric-card-line" d="'+path+'"></path></svg></div>';
 }
 function statusInfo(metric){
   var snapshot=metric.snapshot||{},raw=String(snapshot.status||'').toLowerCase(),bad=snapshot.ok===false||/(fail|breach|error)/.test(raw),good=snapshot.ok===true||/(pass|healthy|ok)/.test(raw);
   return {label:snapshot.status||(snapshot.observation_id?'observed':'not observed'),className:bad?'bad':good?'good':'neutral',bad:bad};
 }
 function deltaMarkup(metric){
   var trend=metric.trend;if(!trend)return '';
   var sign=trend.direction==='up'?'▲':trend.direction==='down'?'▼':'–',value=trend.percent==null?'':Math.abs(Number(trend.percent)).toFixed(1)+'%';
   return '<small class="'+esc(trend.meaning||'neutral')+'">'+sign+(value?' '+esc(value):'')+'</small>';
 }
 function actionMarkup(metric){
   if(!calliope)return '';
   return '<div class="metric-card-actions">'
     +'<button type="button" data-metric-follow="'+esc(metric.name)+'" class="'+(metric.followed?'active':'')+'" aria-pressed="'+String(Boolean(metric.followed))+'" title="'+(metric.followed?'Included in future Personal Briefs — click to remove':'Include meaningful changes in future Personal Briefs')+'">'+(metric.followed?'✓ In Briefs':'Brief me')+'</button>'
     +'<button type="button" data-metric-pin="'+esc(metric.name)+'" class="'+(metric.pinned?'active':'')+'" aria-pressed="'+String(Boolean(metric.pinned))+'" title="'+(metric.pinned?'Remove from Semantic Home':'Pin to Semantic Home')+'">'+(metric.pinned?'✓ Home':'Pin')+'</button>'
     +'<button type="button" class="ask" data-metric-ask="'+esc(metric.name)+'" title="Freeze this observation on a new Calliope stage">✦ Ask</button></div>';
 }
 function cardMarkup(metric){
   var snapshot=metric.snapshot||{},status=statusInfo(metric),stamp=snapshot.data_as_of||snapshot.observed_at;
   return '<article class="metric-gallery-card '+(status.bad?'bad':'')+'" data-metric-card="'+esc(metric.name)+'">'
     +'<div class="metric-card-grid"></div>'+sparkline(metric)
     +'<button type="button" class="metric-card-open" data-metric-open="'+esc(metric.name)+'" aria-label="Open Metric Lens for '+esc(metric.title)+'"></button>'
     +actionMarkup(metric)+'<div class="metric-card-content"><div class="metric-card-kicker">'+esc(metric.category)+(metric.subcategory?' · '+esc(metric.subcategory):'')+'</div>'
     +'<span class="metric-card-status '+status.className+'">'+esc(status.label)+'</span>'
     +'<div class="metric-card-value">'+esc(formatValue(snapshot.value,metric.display,metric.name))+deltaMarkup(metric)+'</div>'
     +'<h2 class="metric-card-title">'+esc(metric.title)+'</h2>'+(metric.description?'<p class="metric-card-description">'+esc(metric.description)+'</p>':'')
     +'<footer class="metric-card-foot"><span>v'+esc(metric.version)+(metric.grain?' · '+esc(metric.grain):'')+(metric.parameter_count?' · '+metric.parameter_count+' params':'')+'</span><span>'+esc(stamp?'observed '+relative(stamp):'no materialized history')+'</span></footer></div></article>';
 }
 function visibleMetrics(){
   var needle=queryText(),selected=category.value||'';
   return state.metrics.filter(function(metric){
     if(selected&&metric.category!==selected)return false;
     if(state.relation==='followed'&&!metric.followed)return false;if(state.relation==='pinned'&&!metric.pinned)return false;
     if(!needle)return true;return [metric.name,metric.title,metric.description,metric.category,metric.subcategory].join(' ').toLowerCase().indexOf(needle)>=0;
   });
 }
 function renderMetrics(){
   if(!state.loaded)return;var visible=visibleMetrics();grid.innerHTML=visible.map(cardMarkup).join('');
   empty.hidden=Boolean(visible.length);statusNode.hidden=true;
   var relationLabel=state.relation==='followed'?'in briefs':state.relation;tally.textContent=visible.length+' of '+state.metrics.length+' governed metric'+(state.metrics.length===1?'':'s')+(state.relation!=='all'?' · '+relationLabel:'');
 }
 async function loadPinIds(){
   if(!calliope)return;try{var response=await fetch('/api/calliope/home',{headers:{accept:'application/json'}}),data=await response.json();if(!response.ok)return;state.pinIds={};(data.items||[]).forEach(function(item){if(item.kind==='metric'&&item.canonical_key)state.pinIds[item.canonical_key]=item.id;});}catch(ignore){}
 }
 async function loadMetrics(){
   if(state.loaded||state.loading)return;state.loading=true;statusNode.hidden=false;statusNode.classList.remove('error');statusNode.innerHTML='<i></i><span>Loading governed metrics…</span>';
   try{var response=await fetch('/api/gallery/metrics?limit=600',{headers:{accept:'application/json'}}),data=await response.json();if(!response.ok)throw new Error(data.error&&data.error.message||'Metric catalog unavailable');
     state.metrics=data.metrics||[];state.categories=data.categories||[];state.loaded=true;
     category.innerHTML='<option value="">Every category</option>'+state.categories.map(function(item){return '<option value="'+esc(item.category)+'">'+esc(item.category)+' · '+esc(item.count)+'</option>';}).join('');
     countNode.hidden=false;countNode.textContent=state.metrics.length>999?'999+':String(state.metrics.length);await loadPinIds();renderMetrics();
   }catch(error){statusNode.classList.add('error');statusNode.innerHTML='<span>'+esc(error&&error.message||'Metric catalog unavailable')+'</span>';}
   finally{state.loading=false;}
 }
 function setView(view,updateUrl){
   state.view=view==='metrics'?'metrics':'artifacts';var metrics=state.view==='metrics';browser.hidden=!metrics;if(artifactBrowser)artifactBrowser.hidden=metrics;if(artifactChips)artifactChips.hidden=metrics;
   viewButtons.forEach(function(button){button.setAttribute('aria-selected',String(button.dataset.galleryView===state.view));});
   q.placeholder=metrics?'Search metrics…  (press /)':'Search artifacts…  (press /)';kicker.textContent=metrics?'Governed metric catalog':'Published artifacts';title.innerHTML=metrics?'The numbers that <em>matter</em>.':'Your data, <em>live</em>.';
   if(q)q.dispatchEvent(new Event('input'));
   if(metrics){loadMetrics().then(renderMetrics);}else tally.textContent=tally.dataset.artifactTally||'';
   if(updateUrl!==false){var url=new URL(window.location.href);if(metrics)url.searchParams.set('view','metrics');else{url.searchParams.delete('view');url.searchParams.delete('metric');url.searchParams.delete('params');}history.replaceState({},'',url);}
 }
 function metricByName(name){return state.metrics.find(function(metric){return metric.name===name;})||(state.detail&&state.detail.name===name?state.detail:null);}
 function setBusy(button,busy){if(!button)return;button.disabled=busy;button.classList.toggle('loading',busy);}
 async function toggleFollow(metric,button){
   if(!metric||!calliope)return;setBusy(button,true);try{var response=await fetch('/api/calliope/metrics/'+encodeURIComponent(metric.name)+'/follow',{method:metric.followed?'DELETE':'POST',headers:{'content-type':'application/json',accept:'application/json'},body:JSON.stringify({params:metric.params||{}})}),data=await response.json();if(!response.ok)throw new Error(data.error&&data.error.message||'Could not update Briefs');
     var followed=!metric.followed;state.metrics.filter(function(item){return item.canonical_key===metric.canonical_key;}).forEach(function(item){item.followed=followed;});if(state.detail&&state.detail.canonical_key===metric.canonical_key)state.detail.followed=followed;renderMetrics();if(state.detail)renderLens();
   }catch(error){window.alert(error&&error.message||'Could not update Briefs');}finally{setBusy(button,false);}
 }
 async function togglePin(metric,button){
   if(!metric||!calliope)return;setBusy(button,true);try{var itemId=state.pinIds[metric.canonical_key]||'',response=await fetch(itemId?'/api/calliope/home/items/'+encodeURIComponent(itemId):'/api/calliope/home/items',{method:itemId?'DELETE':'POST',headers:{'content-type':'application/json',accept:'application/json'},body:itemId?undefined:JSON.stringify({kind:'metric',name:metric.name,params:metric.params||{}})}),data=await response.json();if(!response.ok)throw new Error(data.error&&data.error.message||'Could not update Home');
     if(itemId)delete state.pinIds[metric.canonical_key];else if(data.item)state.pinIds[metric.canonical_key]=data.item.id;
     var pinned=!metric.pinned;state.metrics.filter(function(item){return item.canonical_key===metric.canonical_key;}).forEach(function(item){item.pinned=pinned;});if(state.detail&&state.detail.canonical_key===metric.canonical_key)state.detail.pinned=pinned;
     window.dispatchEvent(new CustomEvent('gallery-home-changed'));renderMetrics();if(state.detail)renderLens();
   }catch(error){window.alert(error&&error.message||'Could not update Home');}finally{setBusy(button,false);}
 }
 async function askMetric(metric,button){
   if(!metric||!calliope)return;setBusy(button,true);try{var body={params:metric.params||{},days:state.detailDays,bucket:'raw'},selected=state.selected;
     if(state.detail&&selected.length){body.from_observation_id=selected[0];body.to_observation_id=selected[selected.length-1];}
     var response=await fetch('/api/calliope/gallery/metrics/'+encodeURIComponent(metric.name)+'/ask',{method:'POST',headers:{'content-type':'application/json',accept:'application/json'},body:JSON.stringify(body)}),data=await response.json();if(!response.ok)throw new Error(data.error&&data.error.message||'Could not open metric in Calliope');if(!data.url)throw new Error('Calliope did not return a metric notebook');window.location.assign(data.url);
   }catch(error){window.alert(error&&error.message||'Could not open Calliope');setBusy(button,false);}
 }
 function lensActionsMarkup(metric){if(!calliope)return '';return '<button type="button" data-lens-follow class="'+(metric.followed?'active':'')+'" title="'+(metric.followed?'Included in future Personal Briefs — click to remove':'Include meaningful changes in future Personal Briefs')+'"><span>'+(metric.followed?'✓ In Briefs':'Brief me')+'</span></button><button type="button" data-lens-pin class="'+(metric.pinned?'active':'')+'"><span>'+(metric.pinned?'✓ Home':'Pin')+'</span></button><button type="button" class="ask" data-lens-ask>✦ <span>'+(state.selected.length>1?'Ask about range':'Ask Calliope')+'</span></button>';}
 function chartMarkup(metric){
   var list=points(metric);if(list.length<2)return '<div class="metric-lens-error"><strong>No timeline yet.</strong><span>This metric has fewer than two materialized numeric observations.</span></div>';
   var w=980,h=310,left=54,right=18,top=20,bottom=35,values=list.map(function(item){return item.value;}),min=Math.min.apply(null,values),max=Math.max.apply(null,values);if(max===min){var lensFlatPad=Math.abs(max)*.08||1;min-=lensFlatPad;max+=lensFlatPad;}var range=max-min;
   var x=function(index){return left+index/Math.max(1,list.length-1)*(w-left-right);},y=function(value){return top+(max-value)/range*(h-top-bottom);};
   var path=list.map(function(item,index){return (index?'L':'M')+' '+x(index).toFixed(2)+' '+y(item.value).toFixed(2);}).join(' '),gridLines=[0,.25,.5,.75,1].map(function(fraction){var value=max-range*fraction,yy=y(value);return '<line class="grid-line" x1="'+left+'" y1="'+yy+'" x2="'+(w-right)+'" y2="'+yy+'"></line><text class="axis-label" x="'+(left-7)+'" y="'+(yy+3)+'" text-anchor="end">'+esc(formatValue(value,metric.display,metric.name))+'</text>';}).join('');
   var priorVersion=null,marks='',circles='';list.forEach(function(item,index){var point=item.point,selected=state.selected.indexOf(point.observation_id)>=0;
     if(priorVersion!==null&&point.metric_version!==priorVersion)marks+='<line class="version-mark" x1="'+x(index)+'" y1="'+top+'" x2="'+x(index)+'" y2="'+(h-bottom)+'"></line>';priorVersion=point.metric_version;
     circles+='<circle tabindex="0" role="button" aria-label="Observation '+esc(point.observation_id)+' · '+esc(formatValue(item.value,metric.display,metric.name))+'" class="point '+(selected?'selected':'')+'" data-metric-point="'+esc(point.observation_id)+'" data-point-index="'+index+'" cx="'+x(index)+'" cy="'+y(item.value)+'" r="3.2"></circle>';
   });
   return '<div class="metric-lens-chart"><svg viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none" role="img" aria-label="Timeline for '+esc(metric.title)+'">'+gridLines+'<path class="area" d="'+path+' L '+x(list.length-1)+' '+(h-bottom)+' L '+x(0)+' '+(h-bottom)+' Z"></path><path class="line" d="'+path+'"></path>'+marks+circles+'</svg><div class="metric-chart-tooltip" hidden></div></div>';
 }
 function parameterMarkup(metric){
   var defaults=metric.parameter_defaults||{},current=metric.params||{},keys=Array.from(new Set(Object.keys(defaults).concat(Object.keys(current))));
   if(!keys.length)return '<p>This metric has no parameters.</p>';
   return '<dl class="metric-param-list">'+keys.map(function(key){var active=Object.prototype.hasOwnProperty.call(current,key),value=active?current[key]:defaults[key];return '<div><dt>'+esc(key)+'<small>'+(active?'Current instance':'Definition default')+'</small></dt><dd>'+esc(typeof value==='object'?JSON.stringify(value):value)+'</dd></div>';}).join('')+'</dl>';
 }
 function renderLens(){
   var metric=state.detail;if(!metric)return;var snapshot=metric.snapshot||{},info=statusInfo(metric),stamp=snapshot.data_as_of||snapshot.observed_at;
   lensTitle.textContent=metric.title;lensMeta.textContent=[metric.category,metric.subcategory,'definition v'+metric.version].filter(Boolean).join(' · ');lensActions.innerHTML=lensActionsMarkup(metric);
   var selection='Click one point to anchor a question; click a second to select a range.';if(state.selected.length===1)selection='Observation '+state.selected[0]+' selected · choose an endpoint.';if(state.selected.length>1)selection='Observation range '+state.selected[0]+' → '+state.selected[state.selected.length-1]+' selected for Calliope.';
   var dependencies=(metric.dependencies||[]).length?(metric.dependencies||[]).map(function(item){return '<span class="'+(item.stale?'stale':'')+'">'+esc(item.table_name)+(item.stale?' · stale':'')+(item.max_freshness?' · '+esc(relative(item.max_freshness)):'')+'</span>';}).join(''):(metric.source_tables||[]).map(function(item){return '<span>'+esc(item)+'</span>';}).join('');
   var artifacts=(metric.artifacts||[]).map(function(item){var path=(item.app_kind||'dashboard')==='dashboard'?'/d/':'/apps/';return '<a href="'+path+esc(item.slug)+'" target="_blank" rel="noopener">'+esc(item.name||item.slug)+' ↗</a>';}).join('');
   lensContent.innerHTML='<section class="metric-lens-hero"><div class="metric-lens-hero-copy"><span>'+esc(info.label)+'</span><strong>'+esc(formatValue(snapshot.value,metric.display,metric.name))+'</strong><p>'+esc(metric.description||'Governed warehouse metric.')+'</p></div><div class="metric-lens-hero-facts"><span><b>Observed</b>'+esc(stamp?relative(stamp):'never')+'</span><span><b>Grain</b>'+esc(metric.grain||'unspecified')+'</span><span><b>Definition</b>v'+esc(metric.version)+'</span><span><b>History</b>'+esc((metric.series||[]).length)+' points</span></div></section>'
     +'<div class="metric-lens-toolbar"><div class="metric-range-buttons">'+[7,30,90,365].map(function(days){return '<button type="button" data-metric-days="'+days+'" class="'+(state.detailDays===days?'active':'')+'">'+(days===365?'1 year':days+' days')+'</button>';}).join('')+'</div><span class="metric-range-selection '+(state.selected.length?'active':'')+'">'+esc(selection)+'</span></div>'
     +chartMarkup(metric)+'<div class="metric-lens-grid"><section class="metric-lens-panel"><h3>Definition</h3><p>'+esc(metric.description||'No business definition supplied.')+'</p><div class="metric-definition-facts"><span><b>Canonical name</b>'+esc(metric.name)+'</span><span><b>Category</b>'+esc(metric.category)+(metric.subcategory?' · '+esc(metric.subcategory):'')+'</span><span><b>Check</b>'+(metric.has_check?'Governed target':'No target check')+'</span><span><b>Versions</b>'+esc((metric.versions||[]).length)+'</span></div><details class="metric-sql"><summary>Advanced · definition SQL</summary><pre>'+esc(metric.definition_sql||'SQL unavailable')+'</pre></details></section>'
     +'<section class="metric-lens-panel"><h3>Parameters</h3>'+parameterMarkup(metric)+'</section><section class="metric-lens-panel"><h3>Sources & freshness</h3><div class="metric-source-list">'+(dependencies||'<span>No source lineage resolved</span>')+'</div></section><section class="metric-lens-panel"><h3>Used by artifacts</h3><div class="metric-artifact-list">'+(artifacts||'<span>No published artifact dependency recorded</span>')+'</div></section></div>';
 }
 async function openMetric(name,params,days,updateUrl){
   if(!lens)return;state.detail=null;state.selected=[];state.detailDays=days||90;var request=++state.request;if(!lens.open)lens.showModal();lensTitle.textContent=name;lensMeta.textContent='Resolving definition, lineage, and durable observations';lensActions.innerHTML='';lensContent.innerHTML='<div class="metric-lens-loading"><i></i><strong>Opening Metric Lens…</strong><span>Reading the governed definition and materialized timeline</span></div>';
   try{var query=new URLSearchParams({days:String(state.detailDays),bucket:'raw'});if(params&&Object.keys(params).length)query.set('params',JSON.stringify(params));var response=await fetch('/api/gallery/metrics/'+encodeURIComponent(name)+'?'+query.toString(),{headers:{accept:'application/json'}}),data=await response.json();if(request!==state.request)return;if(!response.ok)throw new Error(data.error&&data.error.message||'Could not inspect metric');state.detail=data.metric;renderLens();
     if(updateUrl!==false){var url=new URL(window.location.href);url.searchParams.set('view','metrics');url.searchParams.set('metric',name);if(params&&Object.keys(params).length)url.searchParams.set('params',JSON.stringify(params));else url.searchParams.delete('params');history.replaceState({},'',url);}
   }catch(error){if(request!==state.request)return;lensContent.innerHTML='<div class="metric-lens-error"><strong>Metric Lens unavailable</strong><span>'+esc(error&&error.message||'Try again shortly.')+'</span></div>';}
 }
 function closeLens(updateUrl){state.request++;state.detail=null;state.selected=[];if(lens&&lens.open)lens.close();if(updateUrl!==false){var url=new URL(window.location.href);url.searchParams.delete('metric');url.searchParams.delete('params');history.replaceState({},'',url);}}
 function choosePoint(id){id=Number(id);if(!Number.isFinite(id))return;if(!state.selected.length)state.selected=[id];else if(state.selected.length===1&&state.selected[0]!==id)state.selected=[state.selected[0],id];else state.selected=[id];renderLens();}
 viewButtons.forEach(function(button){button.addEventListener('click',function(){setView(button.dataset.galleryView,true);});});
 relationButtons.forEach(function(button){button.addEventListener('click',function(){state.relation=button.dataset.metricRelation;relationButtons.forEach(function(item){item.setAttribute('aria-pressed',String(item===button));});renderMetrics();});});
 category.addEventListener('change',renderMetrics);if(q)q.addEventListener('input',function(){if(state.view==='metrics')renderMetrics();});
 grid.addEventListener('click',function(event){var open=event.target.closest('[data-metric-open]'),follow=event.target.closest('[data-metric-follow]'),pin=event.target.closest('[data-metric-pin]'),ask=event.target.closest('[data-metric-ask]'),target=open||follow||pin||ask;if(!target)return;var name=target.dataset.metricOpen||target.dataset.metricFollow||target.dataset.metricPin||target.dataset.metricAsk,metric=metricByName(name);if(open)openMetric(name,metric&&metric.params,90,true);else if(follow)toggleFollow(metric,follow);else if(pin)togglePin(metric,pin);else askMetric(metric,ask);});
 lensActions.addEventListener('click',function(event){var metric=state.detail;if(!metric)return;var follow=event.target.closest('[data-lens-follow]'),pin=event.target.closest('[data-lens-pin]'),ask=event.target.closest('[data-lens-ask]');if(follow)toggleFollow(metric,follow);else if(pin)togglePin(metric,pin);else if(ask)askMetric(metric,ask);});
 lensContent.addEventListener('click',function(event){var days=event.target.closest('[data-metric-days]'),point=event.target.closest('[data-metric-point]');if(days&&state.detail)openMetric(state.detail.name,state.detail.params,Number(days.dataset.metricDays),false);else if(point)choosePoint(point.dataset.metricPoint);});
 lensContent.addEventListener('keydown',function(event){var point=event.target.closest&&event.target.closest('[data-metric-point]');if(point&&(event.key==='Enter'||event.key===' ')){event.preventDefault();choosePoint(point.dataset.metricPoint);}});
 lensContent.addEventListener('pointerover',function(event){var point=event.target.closest&&event.target.closest('[data-metric-point]');if(!point||!state.detail)return;var list=points(state.detail),item=list[Number(point.dataset.pointIndex)],tip=lensContent.querySelector('.metric-chart-tooltip');if(!item||!tip)return;var rect=point.getBoundingClientRect(),host=point.closest('.metric-lens-chart').getBoundingClientRect();tip.innerHTML='<strong>'+esc(formatValue(item.value,state.detail.display,state.detail.name))+'</strong><b>'+esc(item.point.data_as_of||item.point.observed_at||'')+'</b><br>Observation '+esc(item.point.observation_id)+' · metric v'+esc(item.point.metric_version||'?');tip.style.left=(rect.left-host.left+rect.width/2)+'px';tip.style.top=(rect.top-host.top)+'px';tip.hidden=false;});
 lensContent.addEventListener('pointerout',function(event){if(!event.target.closest||!event.target.closest('[data-metric-point]'))return;var tip=lensContent.querySelector('.metric-chart-tooltip');if(tip)tip.hidden=true;});
 if(lensClose)lensClose.addEventListener('click',function(){closeLens(true);});if(lens)lens.addEventListener('cancel',function(event){event.preventDefault();closeLens(true);});
 document.addEventListener('click',function(event){var button=event.target.closest&&event.target.closest('[data-home-metric-ask]');if(!button)return;var params={};try{params=JSON.parse(button.dataset.homeMetricParams||'{}');}catch(ignore){}askMetric({name:button.dataset.homeMetricAsk,params:params},button);});
 window.addEventListener('popstate',function(){var url=new URL(window.location.href),view=url.searchParams.get('view'),params={};setView(view==='metrics'?'metrics':'artifacts',false);try{params=url.searchParams.get('params')?JSON.parse(url.searchParams.get('params')):{};}catch(ignore){}if(url.searchParams.get('metric'))openMetric(url.searchParams.get('metric'),params,90,false);else closeLens(false);});
 var initial=new URL(window.location.href),initialView=initial.searchParams.get('view')==='metrics'||Boolean(initial.searchParams.get('metric'))?'metrics':'artifacts';setView(initialView,false);if(initial.searchParams.get('metric')){var initialParams={};try{initialParams=initial.searchParams.get('params')?JSON.parse(initial.searchParams.get('params')):{};}catch(ignore){}loadMetrics().then(function(){openMetric(initial.searchParams.get('metric'),initialParams,90,false);});}
})();
"""

# Species → how it's labelled and what stands in when there's no screenshot.
# app_kind is free-form (agents may invent one), so this is a display hint with
# a sane default, never a gate.
_KIND_GLYPH = {"dashboard": "▦", "app": "◈", "deck": "▷", "report": "▤", "tool": "⌘"}

# The rvbbit rabbit, same traced mark rvbbit-lens uses in its menu bar
# (src/components/desktop/rvbbit-logo.tsx). Paths fill with currentColor, so
# the nav's amber carries straight through — same colour the sparkle had.
_RABBIT_SVG = (
    '<svg class=mark viewBox="0 0 1383 709" fill="none" aria-hidden="true">'
    '<path d="M 0 458 L 0 708 L 36 708 L 36 458 Z" fill="currentColor" fill-rule="evenodd"/>'
    '<path d="M 81 458 L 81 708 L 144 708 L 193 702 L 233 692 L 271 678 L 314 656 L 378 608 L 406 579'
    ' L 435 541 L 623 541 L 654 536 L 681 528 L 717 511 L 746 491 L 773 465 L 789 486 L 814 509 L 849 529'
    ' L 873 537 L 896 541 L 1175 541 L 1271 589 L 1382 365 L 1274 310 L 1288 270 L 1289 232 L 1282 188'
    ' L 1262 135 L 1237 96 L 1204 61 L 1163 32 L 1124 14 L 1097 6 L 1057 0 L 672 0 L 672 50 L 678 96'
    ' L 641 86 L 589 83 L 552 88 L 516 99 L 500 59 L 483 37 L 468 24 L 444 10 L 427 4 L 406 0 L 380 0'
    ' L 359 4 L 332 15 L 313 28 L 296 45 L 277 77 L 268 115 L 272 158 L 285 189 L 298 207 L 331 234'
    ' L 360 246 L 381 250 L 373 291 L 247 292 L 247 345 L 242 370 L 232 393 L 212 420 L 182 443 L 155 454'
    ' L 134 458 Z M 831 364 L 843 331 L 863 309 L 889 295 L 926 292 L 953 301 L 975 318 L 990 340'
    ' L 1002 375 L 1216 375 L 1270 403 L 1233 476 L 1197 458 L 905 458 L 874 448 L 849 427 L 834 399 Z'
    ' M 722 230 L 735 253 L 744 280 L 747 298 L 745 340 L 737 367 L 725 390 L 707 413 L 685 432 L 653 449'
    ' L 614 458 L 387 458 L 345 521 L 295 568 L 264 588 L 231 604 L 191 617 L 164 621 L 164 538 L 205 525'
    ' L 235 509 L 259 491 L 285 464 L 303 438 L 318 406 L 327 375 L 462 375 L 456 327 L 456 297 L 461 272'
    ' L 481 230 L 492 216 L 519 192 L 544 178 L 573 169 L 625 168 L 654 176 L 678 188 L 698 203 Z'
    ' M 763 84 L 1048 83 L 1076 87 L 1104 96 L 1146 122 L 1167 143 L 1185 169 L 1195 190 L 1204 223'
    ' L 1206 254 L 1203 265 L 1188 284 L 1172 291 L 1111 292 L 1073 285 L 1044 268 L 1020 240 L 1008 210'
    ' L 1002 167 L 882 167 L 840 160 L 815 148 L 800 137 L 776 110 Z M 389 83 L 406 85 L 421 94 L 428 102'
    ' L 435 121 L 434 134 L 427 149 L 414 161 L 401 166 L 385 166 L 368 158 L 356 144 L 352 133 L 352 116'
    ' L 359 101 L 372 89 Z" fill="currentColor" fill-rule="evenodd"/></svg>')


def _rel_time(dt):
    """'3d ago' — a browse page reads better in elapsed time than in dates."""
    if not dt:
        return ""
    try:
        secs = time.time() - dt.timestamp()
    except Exception:  # noqa: BLE001
        return ""
    if secs < 90:
        return "just now"
    for span, unit in ((3600, "m"), (86400, "h"), (604800, "d"), (2629800, "w"), (31557600, "mo")):
        if secs < span:
            prev = {3600: 60, 86400: 3600, 604800: 86400, 2629800: 604800, 31557600: 2629800}[span]
            return f"{int(secs // prev)}{unit} ago"
    return f"{int(secs // 31557600)}y ago"


def _landing_rows():
    """Every published, web-addressable artifact. rvbbit.live_apps is the view
    over rvbbit.dashboards (which is ONLY external artifacts — DataRabbit plates
    live in rvbbit.plates and never appear here), so no filtering is needed and
    none is wanted: decks and custom app_kinds have public URLs too."""
    with _conn() as c:
        return c.execute(
            "SELECT slug, name, description, owner_email, team, status, runtime_kind, app_kind, "
            "latest_version, queries, tables, metrics, semantic_objects, updated_at "
            "FROM rvbbit.live_apps ORDER BY updated_at DESC").fetchall()


def _warm_thumbs(rows):
    """Start captures for everything missing or stale when the INDEX renders,
    not when an <img> happens to be requested.

    The browser only fetches thumbnails it decides to load — lazily, and only
    for cards near the viewport — so leaving generation to /thumbs meant an
    artifact below the fold never began rendering until somebody scrolled to
    it, and a first visit showed monograms that only filled in on a later
    manual refresh. Deduped and semaphore-gated inside _auto_thumb, so calling
    it for every row on every page view is cheap once the volume is warm."""
    for r in rows:
        if (r.get("runtime_kind") or "html") != "html":
            continue
        app_kind = (r.get("app_kind") or "dashboard").lower()
        try:
            if _thumb_stale(_artifact_kind(app_kind), r["slug"], r["updated_at"]):
                _auto_thumb(app_kind, r["slug"])
        except Exception as e:   # noqa: BLE001 — warming is best-effort, never fails a page
            print(f"warm thumb {r.get('slug')}: {e}", file=sys.stderr)


def _lens_url():
    """The DataRabbit origin, when there is one. Absent = warehouse-only
    install, so there is no app to offer."""
    return os.environ.get("LENS_PUBLIC_URL", "").rstrip("/")


def _unmapped_html(identity):
    """Signed in, unknown to the database.

    Deliberately a dead end with a next step rather than a 403: the person is
    who they say they are — a verified account in an allowed domain — they
    just have no Postgres role yet. Their arrival is already recorded in
    rvbbit.identity_pending, so this page's promise (someone will grant you
    access) is one a DBA can actually act on.
    """
    import auth
    import warehouse_theme
    from html import escape as e
    bg = auth.background_layer(
        0.34, "radial-gradient(1000px 700px at 50% 42%, rgba(16,13,11,.52) 0%, "
              "rgba(16,13,11,.84) 58%, rgba(16,13,11,.95) 100%)",
        scene_key=identity)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Access pending — Warehouse</title>
<style>{_LANDING_CSS}
.gate{{min-height:100vh;display:grid;place-items:center;padding:24px}}
.gate-card{{max-width:520px;width:100%;border:1px solid var(--line);background:var(--panel);
  padding:38px 40px;backdrop-filter:blur(3px)}}
.gate-card h1{{font-size:clamp(28px,3.4vw,40px);margin-bottom:14px}}
.gate-card p{{color:var(--fog);font-size:13.5px;line-height:1.65;margin-bottom:14px}}
.gate-who{{display:inline-block;margin:2px 0 18px;padding:5px 11px;border:1px solid var(--line-hot);
  color:var(--amber);font:10px/1 var(--mono);letter-spacing:.1em}}
.gate-foot{{margin-top:26px;padding-top:18px;border-top:1px solid var(--line);
  color:var(--dim);font:9px/1.6 var(--mono);letter-spacing:.1em;text-transform:uppercase}}
.gate-foot a{{color:var(--fog)}}
</style>
{warehouse_theme.head_assets()}
</head><body>
{bg}
<div class="wash"></div>
<nav data-warehouse-header>{_RABBIT_SVG}
 <span class="wordmark">DATA RABBIT<small>WAREHOUSE</small></span>
 <span class="who"><span data-warehouse-theme-anchor></span><span class="viewer">{e(identity)}</span><a href="/auth/logout">Sign out</a></span></nav>
<div class="gate"><div class="gate-card">
  <div class="kicker">Access pending</div>
  <h1>You&rsquo;re signed in.</h1>
  <div class="gate-who">{e(identity)}</div>
  <p>Your sign-in was verified, but this warehouse doesn&rsquo;t have an account
     for you yet — so there&rsquo;s nothing here you can read.</p>
  <p>Someone with database access needs to map you to a role. Your request has
     already been recorded, so ask whoever administers this warehouse to grant
     you access; nothing else is needed from you.</p>
  <div class="gate-foot">Once granted, sign in again &mdash; <a href="/auth/logout">sign out</a></div>
</div></div>
</body></html>"""


def _landing_html(rows, viewer):
    import auth
    import warehouse_theme
    from html import escape as e

    # Far dimmer than the login page's 0.42: a wall of cards has to stay the
    # subject. Cards are opaque, so the scene only ever reads in the hero band
    # and the outer gutters — the veil is lightest there and closes down over
    # the grid, which is where legibility actually matters.
    # The veil also carries the warm top-left glow the body background used to
    # provide, so the palette survives while the scene shows through.
    _bg_layer = auth.background_layer(
        0.40,
        "radial-gradient(1200px 800px at 30% -10%, rgba(36,28,20,.50) 0%, rgba(36,28,20,0) 62%),"
        "linear-gradient(to bottom, rgba(16,13,11,.26) 0%, rgba(16,13,11,.50) 30%,"
        " rgba(16,13,11,.84) 62%, rgba(16,13,11,.93) 100%)",
        scene_key=viewer)

    # The personal working set exists only with the Hermes-backed Calliope
    # surface; warehouse-only installs keep the original public gallery.
    import calliope
    calliope_enabled = calliope.is_enabled()

    kinds, cards = {}, []
    for r in rows:
        app_kind = (r.get("app_kind") or "dashboard").lower()
        kinds[app_kind] = kinds.get(app_kind, 0) + 1
        slug = r["slug"]
        # Link where the publish tools already told the user to look, so the
        # cards match the URLs sitting in their chat history.
        href = f"/d/{slug}" if app_kind == "dashboard" else f"/apps/{slug}"
        name = r.get("name") or slug
        desc = r.get("description") or ""
        # No onerror-remove and no lazy: the page warms every capture on render
        # (_warm_thumbs), and the script retries a miss with backoff, so a cold
        # gallery fills itself in while you watch instead of after a refresh.
        thumb = ""
        if (r.get("runtime_kind") or "html") == "html":
            thumb = (f'<img src="/thumbs/{e(_artifact_kind(app_kind))}/{e(slug)}.png" alt="" '
                     f'decoding="async">')
        deps = []
        for label, key in (
            ("queries", "queries"),
            ("tables", "tables"),
            ("objects", "semantic_objects"),
            ("metrics", "metrics"),
        ):
            if r.get(key):
                deps.append(f"<span><b>{r[key]}</b> {label}</span>")
        if r.get("latest_version"):
            deps.append(f"<span>v<b>{r['latest_version']}</b></span>")
        artifact_owner = str(r.get("owner_email") or "").strip()
        owner = artifact_owner or r.get("team") or ""
        haystack = " ".join(str(x) for x in (name, desc, slug, app_kind, owner) if x).lower()
        activity_action = (
            f'<button class="card-activity" type="button" data-gallery-activity="{e(slug)}" '
            f'title="View authenticated activity for this artifact">'
            f'<b aria-hidden="true">▥</b><span>Views</span></button>'
        )
        calliope_actions = (
            f'<button class="card-ask" type="button" data-gallery-ask="{e(slug)}" '
            f'title="Ask Calliope about this exact published version">'
            f'<b aria-hidden="true">✦</b><span>Ask</span></button>'
            f'<button class="card-pin" type="button" data-home-pin="{e(slug)}" '
            f'aria-pressed="false" title="Pin this artifact to your private Home">'
            f'<b aria-hidden="true">＋</b><span>Pin</span></button>'
            if calliope_enabled else ""
        )
        card_actions = activity_action + calliope_actions
        cards.append(
            # New tab: the index is a place you come back to, not a page you
            # navigate away from. The pin is a sibling of the link so both
            # controls keep valid, predictable browser semantics.
            f'<article class="card" data-kind="{e(app_kind)}" data-search="{e(haystack)}" '
            f'data-slug="{e(slug)}">'
            f'<a class="card-link" href="{e(href)}" target="_blank" rel="noopener">'
            f'<div class="shot">{thumb}<div class="glyph">{_KIND_GLYPH.get(app_kind, "◇")}</div></div>'
            f'<div class="body">'
            f'<div class="meta"><span class="pill">{e(app_kind)}</span>'
            + (f'<span class="pill dim">{e(owner)}</span>' if owner else "")
            + f'<span class="when">{e(_rel_time(r.get("updated_at")))}</span></div>'
            f'<h2>{e(name)}</h2>'
            + (f'<p class="desc">{e(desc)}</p>' if desc else "")
            + (f'<div class="foot">{"".join(deps)}</div>' if deps else "")
            + '</div></a>'
            + (f'<div class="card-actions">{card_actions}</div>' if card_actions else "")
            + '</article>')

    # The rung up the ladder. Only offered when there IS an app (LENS_PUBLIC_URL)
    # and only to viewers the database can place — an unmapped session reaching
    # DataRabbit lands in a desktop where every query fails, so withholding the
    # affordance is the honest move, not a lesser one. Everyone else gets a
    # browsable index that works, uncluttered by a surface they can't use.
    lens = _lens_url()
    _app_link = (
        f'<a class=applink href="{e(lens)}/" target="_blank" rel="noopener" '
        f'title="Open Data Desktop in a new window">Open Data Desktop &rarr;</a>'
    ) if lens else ""
    # Calliope is a true opt-in surface: when Hermes is not configured there is
    # no gallery launcher and its routes are not registered.
    _calliope_link = (
        '<a class="calliope-float" href="/calliope" '
        'title="Open the full Calliope workspace" aria-label="Open the full Calliope workspace">'
        '<span class="calliope-float-avatar" aria-hidden="true" '
        'data-day-src="/calliope/callie-avatar-day.jpg" '
        'data-night-src="/calliope/callie-avatar-night.jpg">'
        '<img alt="" width="44" height="44" decoding="async"></span>'
        '<span class="calliope-float-copy"><span class="calliope-float-name">Calliope</span>'
        '<span class="calliope-float-action">Open workspace <b>&rarr;</b></span></span></a>'
        if calliope_enabled else ""
    )
    _inbox_link = (
        '<a id="gallery-work-inbox" class="inbox-rail-link" href="/calliope?inbox=1" '
        'title="Open Work Inbox in Calliope" aria-label="Open Work Inbox in Calliope">'
        '<span class="inbox-rail-label">Work Inbox</span>'
        '<b id="gallery-work-inbox-count" class="inbox-rail-count" hidden>0</b></a>'
        if calliope_enabled else ""
    )
    _brief_link = (
        '<a id="gallery-personal-brief" class="brief-rail-link" href="/calliope?brief=1" '
        'title="Open today\'s private Personal Brief" aria-label="Open today\'s private Personal Brief">'
        '<span class="brief-rail-label">Brief</span>'
        '<b id="gallery-personal-brief-count" class="brief-rail-count" hidden>0</b></a>'
        if calliope_enabled else ""
    )
    _calliope_search = (
        '<div id="semantic-launch" class="semantic-launch" hidden>'
        '<button id="semantic-launch-button" class="semantic-launch-button" type="button">'
        '<span class="semantic-launch-mark" aria-hidden="true">⌕</span>'
        '<span class="semantic-launch-copy">'
        '<strong>Explore <span id="semantic-launch-query"></span> across company knowledge</strong>'
        '<small><b id="semantic-launch-local">0 published matches here</b> · '
        '<span id="semantic-launch-scope">docs · artifacts · warehouse semantics</span></small></span>'
        '<span class="semantic-launch-cta">Open fresh Calliope session <b aria-hidden="true">↵</b></span>'
        '</button><span id="semantic-launch-error" class="semantic-launch-error" role="status"></span></div>'
        if calliope_enabled else ""
    )
    _semantic_home = (
        '<section id="semantic-home" class="semantic-home" aria-label="My Semantic Home" hidden>'
        '<div class="home-head"><div class="home-title">'
        '<span class="home-title-mark" aria-hidden="true">⌂</span>'
        '<span class="home-title-copy"><strong id="home-title">My Home</strong>'
        '<small>Your private working set · metrics, artifacts, and named business objects</small></span></div>'
        '<span id="home-status" class="home-status" role="status">Loading your working set…</span></div>'
        '<div id="home-grid" class="home-grid"></div>'
        '<div id="home-empty" class="home-empty" hidden><b>Make this yours.</b>'
        '<span>Pin a metric or artifact here, or use Artifact Lens to pin a named value.</span></div>'
        '</section>'
        if calliope_enabled else ""
    )
    _trail_dialog = (
        '<dialog id="trail-dialog" class="trail-dialog" aria-labelledby="trail-title">'
        '<div class="trail-shell"><header class="trail-head">'
        '<button id="trail-back" class="trail-back" type="button" aria-label="Previous trail" hidden>←</button>'
        '<div class="trail-head-copy"><strong id="trail-title">Follow the trail</strong>'
        '<small id="trail-meta">Permission-aware company evidence</small></div>'
        '<button id="trail-close" class="trail-close" type="button" aria-label="Close">×</button>'
        '</header><div id="trail-content" class="trail-content"></div></div></dialog>'
        if calliope_enabled else ""
    )
    _metric_promote_dialog = (
        '<dialog id="metric-promote-dialog" class="metric-promote-dialog" '
        'aria-labelledby="metric-promote-title">'
        '<div class="metric-promote-shell"><header class="metric-promote-head">'
        '<span class="metric-promote-mark" aria-hidden="true">↗</span>'
        '<div class="metric-promote-heading"><strong id="metric-promote-title">Promote to metric</strong>'
        '<small id="metric-promote-meta">Turn this pinned meaning into a durable governed measure</small></div>'
        '<button id="metric-promote-close" class="metric-promote-close" type="button" aria-label="Close">×</button>'
        '</header><div class="metric-promote-body">'
        '<div id="metric-promote-loading" class="metric-promote-loading">Checking the immutable definition…</div>'
        '<form id="metric-promote-form" class="metric-promote-form" hidden>'
        '<div class="metric-promote-source"><div><span>Source business object</span>'
        '<strong id="metric-promote-source-title">Named value</strong>'
        '<p id="metric-promote-source-copy"></p></div>'
        '<b id="metric-promote-current" class="metric-promote-current"></b></div>'
        '<label class="metric-promote-field"><span>Metric name</span>'
        '<input id="metric-promote-name" name="name" required maxlength="63" '
        'pattern="[a-z][a-z0-9_]{0,62}" autocomplete="off" spellcheck="false"></label>'
        '<label class="metric-promote-field"><span>Catalog description</span>'
        '<textarea id="metric-promote-description" name="description" required maxlength="1400"></textarea></label>'
        '<div id="metric-promote-facts" class="metric-promote-facts"></div>'
        '<div id="metric-promote-error" class="metric-promote-error" role="status"></div>'
        '<div class="metric-promote-actions"><button id="metric-promote-cancel" type="button">Cancel</button>'
        '<button id="metric-promote-submit" type="submit">Create governed metric</button></div>'
        '</form></div></div></dialog>'
        if calliope_enabled else ""
    )
    _metric_browser = (
        '<section id="metrics-browser" class="metrics-browser" '
        f'data-calliope="{str(calliope_enabled).lower()}" hidden>'
        '<header class="metrics-browser-head"><div><span>Governed measures</span>'
        '<strong>Current observations, not ad-hoc calculations.</strong>'
        '<small>Brief me includes meaningful changes in future Briefs · Pin keeps the latest value on Home</small></div>'
        '<div class="metric-browser-filters">'
        '<select id="metric-category" aria-label="Filter metrics by category">'
        '<option value="">Every category</option></select>'
        '<div class="metric-relation-filter" role="group" aria-label="Filter personal metrics">'
        '<button type="button" data-metric-relation="all" aria-pressed="true">All</button>'
        '<button type="button" data-metric-relation="followed" aria-pressed="false">In Briefs</button>'
        '<button type="button" data-metric-relation="pinned" aria-pressed="false">Pinned</button>'
        '</div></div></header>'
        '<div id="metric-browser-status" class="metric-browser-status" role="status">'
        '<i></i><span>Loading governed metrics…</span></div>'
        '<div id="metric-gallery-grid" class="metric-gallery-grid"></div>'
        '<div id="metric-gallery-empty" class="metric-gallery-empty" hidden>'
        '<strong>No metrics match this view.</strong><span>Try another category or clear the search.</span></div>'
        '</section>'
    )
    _metric_dialog = (
        '<dialog id="metric-lens" class="metric-lens" aria-labelledby="metric-lens-title">'
        '<div class="metric-lens-shell"><header class="metric-lens-head">'
        '<div class="metric-lens-heading"><span>Metric Lens</span>'
        '<strong id="metric-lens-title">Governed metric</strong>'
        '<small id="metric-lens-meta">Inspect definition and observation history</small></div>'
        '<div id="metric-lens-actions" class="metric-lens-actions"></div>'
        '<button id="metric-lens-close" class="metric-lens-close" type="button" aria-label="Close">×</button>'
        '</header><div id="metric-lens-content" class="metric-lens-content"></div></div></dialog>'
    )
    _activity_dialog = (
        '<dialog id="artifact-activity-dialog" class="artifact-activity-dialog" '
        'aria-labelledby="activity-title">'
        '<div class="activity-shell"><header class="activity-head">'
        '<span class="activity-head-mark" aria-hidden="true">▥</span>'
        '<div class="activity-heading"><span>Authenticated audience</span>'
        '<strong id="activity-title">Artifact activity</strong>'
        '<small id="activity-meta">Human document opens · shared Gallery insight</small></div>'
        '<div id="activity-range" class="activity-range" role="group" aria-label="Activity range">'
        '<button type="button" data-activity-days="7" aria-pressed="false">7D</button>'
        '<button type="button" data-activity-days="30" aria-pressed="true">30D</button>'
        '<button type="button" data-activity-days="90" aria-pressed="false">90D</button></div>'
        '<button id="activity-close" class="activity-close" type="button" aria-label="Close">×</button>'
        '</header><div id="activity-content" class="activity-content"></div></div></dialog>'
    )

    total = len(rows)
    tally = " · ".join([f"{total} artifact{'' if total == 1 else 's'}"]
                       + [f"{n} {k}{'' if n == 1 else 's'}" for k, n in
                          sorted(kinds.items(), key=lambda kv: -kv[1])])
    chips = ('<button class="chip" data-kind="" aria-pressed="true">All</button>'
             + "".join(f'<button class="chip" data-kind="{e(k)}" aria-pressed="false">{e(k)}s</button>'
                       for k, _ in sorted(kinds.items(), key=lambda kv: -kv[1])))

    toolbar = (
        f'<div class="toolbar"><div class="gallery-view-switch" role="tablist" aria-label="Gallery view">'
        f'<button type="button" role="tab" data-gallery-view="artifacts" aria-selected="true">Artifacts</button>'
        f'<button type="button" role="tab" data-gallery-view="metrics" aria-selected="false">Metrics'
        f'<b id="gallery-metric-count" hidden>0</b></button></div>'
        f'<input id="q" type="search" maxlength="600" '
        f'placeholder="Search artifacts…  (press /)" autocomplete="off" spellcheck="false">'
        f'<div id="artifact-kind-chips" class="artifact-kind-chips">{chips if rows else ""}</div></div>'
    )
    if rows:
        body = (
            _semantic_home
            + toolbar
            + _metric_browser
            + '<section id="artifact-browser">'
            + _calliope_search
            + f'<div class="grid">{"".join(cards)}</div>'
            + '<div id="none">No published artifacts match. Explore the company evidence above.</div>'
            + '</section>'
        )
    elif calliope_enabled:
        body = (
            _semantic_home
            + toolbar
            + _metric_browser
            + '<section id="artifact-browser">'
            + _calliope_search
            + '<div class="empty">No artifacts published yet.<br><br>'
            'Search company knowledge above, or open Calliope to make the first one.</div>'
            + '</section>'
        )
    else:
        body = (
            toolbar
            + _metric_browser
            + '<section id="artifact-browser"><div class="empty">No artifacts published yet.<br><br>'
            'Ask an RVBBIT-enabled agent to build one — it starts with '
            '<code>live_app_template</code><br>and publishes with '
            '<code>create_live_app</code>.</div></section>'
        )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Warehouse — published artifacts</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Homemade+Apple&display=swap">
<style>{_LANDING_CSS}{_GALLERY_METRICS_CSS}</style>
{warehouse_theme.head_assets()}
</head><body>
{_bg_layer}
<div class="wash"></div>
<nav data-warehouse-header>{_RABBIT_SVG}
 <span class="wordmark">DATA RABBIT<small>an operational answer engine</small></span>
 <span class="who"><span data-warehouse-theme-anchor></span>{_brief_link}{_inbox_link}{_app_link}{f'<span class="viewer">{e(viewer)}</span>' if viewer else ''}<a href="/auth/logout">Sign out</a></span></nav>
<aside id="gallery-presence" class="gallery-presence" aria-label="Local time and upcoming meetings">
 <div class="presence-clock">
  <div class="presence-time-row" aria-label="Local time">
   <time class="presence-time"><span id="presence-hours">--</span><span class="colon">:</span><span id="presence-minutes">--</span></time>
   <span id="presence-seconds" class="presence-seconds">--</span><span id="presence-period" class="presence-period"></span>
  </div>
  <div class="presence-calendar"><time id="presence-date" class="presence-date">Today</time><span id="presence-zone" class="presence-zone">Local time</span></div>
 </div>
 <a id="presence-meeting" class="presence-meeting" href="#" target="_blank" rel="noopener" hidden>
  <i class="presence-meeting-dot" aria-hidden="true"></i><span class="presence-meeting-copy"><small id="presence-meeting-label">Coming up</small><strong id="presence-meeting-title">Next meeting</strong></span>
  <time id="presence-meeting-time" class="presence-meeting-time"></time>
 </a>
</aside>
<main>
 <header class="hero">
  <div id="gallery-kicker" class="kicker">Published artifacts</div>
  <h1 id="gallery-title">Your data, <em>live</em>.</h1>
  <div id="gallery-tally" class="tally" data-artifact-tally="{e(tally)}">{e(tally)}</div>
 </header>
 {body}
</main>
{_calliope_link}
{_trail_dialog}
{_metric_promote_dialog}
{_metric_dialog}
{_activity_dialog}
<script>{_LANDING_JS}</script><script>{_GALLERY_METRICS_JS}</script></body></html>"""


def register_dashboard_routes(m):
    import auth
    from urllib.parse import quote
    from starlette.responses import HTMLResponse, RedirectResponse, Response

    def _json(obj, status=200):   # default=str handles Decimal / datetime in query rows
        return Response(json.dumps(obj, default=str), media_type="application/json", status_code=status)

    def _private_json(obj, status=200):
        return Response(
            json.dumps(obj, default=str),
            media_type="application/json",
            status_code=status,
            headers={"cache-control": "no-store"},
        )

    async def _proxy_runner(request, subpath=""):
        email = auth.read_session(request)
        if not email:
            return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=302)
        slug = request.path_params["slug"]
        status = _live_app_runner_status(slug, probe=False)
        if not status.get("running"):
            return None
        import httpx
        target = status["endpoint_url"].rstrip("/") + "/" + (subpath or "")
        if request.url.query:
            target = f"{target}?{request.url.query}"
        body = await request.body()
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in {"host", "content-length", "connection", "accept-encoding"}
        }
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as cli:
                proxied = await cli.request(request.method, target, content=body, headers=headers)
        except Exception as e:  # noqa: BLE001
            return _json({"error": {"code": "RUNNER_PROXY_FAILED", "message": str(e)}}, 502)
        out_headers = {
            k: v for k, v in proxied.headers.items()
            if k.lower() in {"content-type", "cache-control", "etag", "last-modified"}
        }
        return Response(proxied.content, status_code=proxied.status_code, headers=out_headers)

    def _document_navigation(request):
        destination = str(request.headers.get("sec-fetch-dest") or "").lower()
        return destination in {"document", "iframe"}

    def _record_running_app_view(request):
        session = auth.read_session_full(request)
        if not session:
            return
        try:
            app, version_row = _load_live_app_version(request.path_params["slug"])
            if not app:
                return
            document = {
                "slug": app["slug"],
                "version": (version_row or {}).get("version") or app.get("latest_version"),
                "app_kind": app.get("app_kind") or "app",
            }
            _record_artifact_view(
                request,
                document,
                session.get("identity") or session.get("sub"),
                session.get("via"),
            )
        except Exception as exc:  # noqa: BLE001 - analytics never breaks an app
            print(
                f"artifact view receipt ({request.path_params.get('slug')}): {exc}",
                file=sys.stderr,
            )

    @m.custom_route("/", methods=["GET"])
    async def _landing(request):
        # Same wall as /d/<slug>: no session → login → back here. On a
        # warehouse-only box this is the root of the site; on a unified origin
        # (docker/origin/Caddyfile) DataRabbit owns / and this is reached at
        # /gallery, which the ingress routes here.
        s = auth.read_session_full(request)
        if not s:
            return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=302)
        # Authenticated, but the database has no account for them. Show the
        # request-access state and query NOTHING: they have no grants, so every
        # card would be a dead link, and artifact titles are themselves a
        # disclosure. A closed door beats a broken gallery.
        if not s["mapped"]:
            return HTMLResponse(_unmapped_html(s["identity"]),
                                headers={"cache-control": "no-store"})
        try:
            rows = _landing_rows()
        except Exception as ex:   # noqa: BLE001 — an index that can't query is still a page
            print(f"landing page: {ex}", file=sys.stderr)
            rows = []
        _warm_thumbs(rows)        # background; the page renders immediately
        return HTMLResponse(_landing_html(rows, s["identity"]),
                            headers={"cache-control": "no-store"})

    @m.custom_route("/gallery", methods=["GET"])
    async def _landing_alias(request):
        return await _landing(request)

    @m.custom_route("/thumbs/{kind}/{slug}.png", methods=["GET"])
    async def _thumb(request):
        # Hub gallery thumbnails (docs/HUB_PLAN.md). Viewer auth: a browser
        # session OR the static bearer key — the LENS thumb proxy fetches
        # server-side with WAREHOUSE_MCP_KEY, browsers ride their session.
        authed = bool(auth.read_session(request))
        if not authed and auth.STATIC_KEY:
            hdr = request.headers.get("authorization", "")
            authed = hdr.startswith("Bearer ") and hmac.compare_digest(hdr[7:], auth.STATIC_KEY)
        if not authed and auth.STATIC_KEY:
            return _json({"error": "unauthorized"}, 401)
        kind = request.path_params["kind"]
        slug = request.path_params["slug"]
        if kind not in ("app", "dashboard") or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", slug, re.I):
            return _json({"error": "bad artifact handle"}, 400)
        # Lazy self-heal: a missing or out-of-date capture enqueues itself
        # (throttled, deduped) — pre-Hub artifacts and republished versions
        # get thumbnails just by being LOOKED AT. Stale files still serve
        # (better a last-version shot than a monogram) while the refresh
        # renders in the background. The landing page ALSO warms on render, so
        # this path is the backstop rather than the only trigger.
        try:
            with _conn() as c:
                d = c.execute("SELECT app_kind, runtime_kind, updated_at FROM rvbbit.dashboards "
                              "WHERE slug=%s", (slug,)).fetchone()
            if d and (d.get("runtime_kind") or "html") == "html" \
                    and _thumb_stale(kind, slug, d["updated_at"]):
                _auto_thumb(d.get("app_kind"), slug)
        except Exception as e:  # noqa: BLE001
            print(f"thumbs route ({kind}:{slug}): {e}", file=sys.stderr)
        path, mime = _thumb_existing(kind, slug)
        if path is None:
            return _json({"error": "no thumbnail"}, 404)
        # Conditional requests. Without an ETag every reload re-transferred the
        # whole gallery (a refresh looked like the server was regenerating);
        # now an unchanged capture costs a 304 instead of its full body.
        st = path.stat()
        etag = f'W/"{int(st.st_mtime)}-{st.st_size}"'
        cache = "public, max-age=60"
        if request.headers.get("if-none-match", "") == etag:
            return Response(status_code=304, headers={"etag": etag, "cache-control": cache})
        return Response(path.read_bytes(), media_type=mime,
                        headers={"cache-control": cache, "etag": etag})

    @m.custom_route("/pdfs/{name}.pdf", methods=["GET"])
    async def _pdf(request):
        # Workflow documents (render_pdf output). Same viewer wall as /thumbs.
        authed = bool(auth.read_session(request))
        if not authed and auth.STATIC_KEY:
            hdr = request.headers.get("authorization", "")
            authed = hdr.startswith("Bearer ") and hmac.compare_digest(hdr[7:], auth.STATIC_KEY)
        if not authed and auth.STATIC_KEY:
            return _json({"error": "unauthorized"}, 401)
        nm = request.path_params["name"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", nm):
            return _json({"error": "bad name"}, 400)
        fp = _workflow_file_root() / "pdfs" / f"{nm}.pdf"
        if not fp.is_file():
            return _json({"error": "no such document"}, 404)
        return Response(fp.read_bytes(), media_type="application/pdf",
                        headers={"content-disposition": f'inline; filename="{nm}.pdf"'})

    async def _render_artifact_document(request, version=None, record_view=True):
        session = auth.read_session_full(request)
        if not session:
            return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=302)
        slug = request.path_params["slug"]
        try:
            document = _dashboard_version_document(slug, version)
        except ValueError as exc:
            return HTMLResponse(f"<h1>400 — {str(exc)}</h1>", status_code=400)
        if not document:
            return HTMLResponse("<h1>404 — no such artifact version</h1>", status_code=404)
        if record_view:
            _record_artifact_view(
                request,
                document,
                session.get("identity") or session.get("sub"),
                session.get("via"),
            )
        return HTMLResponse(document["html"], headers={"cache-control": "no-store"})

    @m.custom_route("/d/{slug}/versions/{version}", methods=["GET"])
    async def _view_version(request):
        return await _render_artifact_document(request, request.path_params.get("version"))

    @m.custom_route("/d/{slug}", methods=["GET"])
    async def _view(request):
        return await _render_artifact_document(request)

    @m.custom_route("/api/d/{slug}/time-travel", methods=["GET"])
    async def _time_travel(request):
        if not auth.read_session(request):
            return _json({"error": {"code": "UNAUTHORIZED"}}, 401)
        slug = request.path_params["slug"]
        version = request.query_params.get("version")
        try:
            selected_version = int(version) if version else None
            if selected_version is not None and selected_version < 1:
                raise ValueError("version must be a positive integer")
            result = _dashboard_time_travel(slug, selected_version)
        except (TypeError, ValueError) as exc:
            return _json({"error": {"code": "BAD_VERSION", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001 — keep the dashboard itself usable
            print(f"artifact time travel ({slug}): {exc}", file=sys.stderr)
            return _json(
                {
                    "eligible": False,
                    "code": "TIMELINE_UNAVAILABLE",
                    "message": "The retained data timeline could not be loaded.",
                    "points": [],
                },
                200,
            )
        return _json(result, 404 if result.get("code") == "NOT_FOUND" else 200)

    @m.custom_route("/api/d/{slug}/semantic-enrichment", methods=["GET"])
    async def _semantic_enrichment(request):
        if not auth.read_session(request):
            return _json({"error": {"code": "UNAUTHORIZED"}}, 401)
        slug = request.path_params["slug"]
        try:
            requested = request.query_params.get("version")
            with _conn() as c:
                dashboard = c.execute(
                    "SELECT id,latest_version FROM rvbbit.dashboards WHERE slug=%s", (slug,)
                ).fetchone()
                if not dashboard:
                    return _json({"error": {"code": "NOT_FOUND"}}, 404)
                version = int(requested or dashboard["latest_version"])
                version_row = c.execute(
                    "SELECT manifest FROM rvbbit.dashboard_versions "
                    "WHERE dashboard_id=%s AND version=%s",
                    (dashboard["id"], version),
                ).fetchone()
            if not version_row:
                return _json({"error": {"code": "VERSION_NOT_FOUND"}}, 404)
            enrichment = _semantic_enrichment_row(dashboard["id"], version)
            effective = _effective_artifact_manifest(
                dashboard["id"], version, version_row.get("manifest") or {}, enrichment
            )
            return _json({
                "slug": slug,
                "version": version,
                **_semantic_enrichment_public(enrichment),
                "semantic_map": effective.get("semantic_map") or {
                    "schema_version": _SEMANTIC_MAP_SCHEMA,
                    "objects": [],
                },
                "manifest": effective,
            })
        except (TypeError, ValueError) as exc:
            return _json({"error": {"code": "BAD_VERSION", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001 — artifact rendering remains independent
            print(f"semantic enrichment status ({slug}): {exc}", file=sys.stderr)
            return _json({"error": {"code": "SEMANTIC_STATUS_UNAVAILABLE"}}, 500)

    def _home_owner(request):
        session = auth.read_session_full(request)
        if not session:
            return None, None, _json({"error": {"code": "UNAUTHORIZED"}}, 401)
        if not session.get("mapped", True):
            return None, session, _json({"error": {"code": "ACCESS_PENDING"}}, 403)
        owner = str(session.get("identity") or "").strip().lower()
        if not owner:
            return None, session, _json({"error": {"code": "UNAUTHORIZED"}}, 401)
        return owner, session, None

    @m.custom_route("/api/gallery/meetings", methods=["GET"])
    async def _gallery_meetings(request):
        owner, _, error = _home_owner(request)
        if error:
            return error
        try:
            import calliope
            if not calliope.is_enabled() or not getattr(auth, "google_enabled", lambda: False)():
                return _private_json({
                    "available": False,
                    "connected": False,
                    "events": [],
                })
            snapshot = await calliope.google_calendar_upcoming_snapshot(
                _conn, owner, horizon_hours=48, limit=4
            )
            return _private_json(snapshot)
        except Exception as exc:  # noqa: BLE001 - a clock never depends on Calendar
            print(f"gallery calendar ({owner}): {type(exc).__name__}: {exc}", file=sys.stderr)
            return _private_json({
                "available": True,
                "connected": True,
                "stale": True,
                "events": [],
            })

    @m.custom_route("/api/gallery/artifacts/{slug}/activity", methods=["GET"])
    async def _gallery_artifact_activity(request):
        owner, _, error = _home_owner(request)
        if error:
            return error
        try:
            snapshot = _artifact_activity_snapshot(
                request.path_params["slug"],
                owner,
                request.query_params.get("days") or 30,
                request.query_params.get("timezone") or "UTC",
            )
            return _private_json(snapshot)
        except LookupError as exc:
            return _private_json({
                "error": {"code": "NOT_FOUND", "message": str(exc)}
            }, 404)
        except PermissionError as exc:
            return _private_json({
                "error": {"code": "FORBIDDEN", "message": str(exc)}
            }, 403)
        except ValueError as exc:
            return _private_json({
                "error": {"code": "BAD_ACTIVITY_QUERY", "message": str(exc)}
            }, 400)
        except Exception as exc:  # noqa: BLE001 - Gallery remains browsable
            print(
                f"artifact activity ({request.path_params['slug']}): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return _private_json({
                "error": {
                    "code": "ACTIVITY_UNAVAILABLE",
                    "message": "View activity could not be loaded right now.",
                }
            }, 503)

    @m.custom_route("/api/gallery/metrics", methods=["GET"])
    async def _gallery_metrics(request):
        owner, _, error = _home_owner(request)
        if error:
            return error
        try:
            return _json(_metric_catalog_snapshot(
                owner,
                search=request.query_params.get("search"),
                category=request.query_params.get("category"),
                limit=request.query_params.get("limit") or _GALLERY_METRIC_LIMIT,
            ))
        except (TypeError, ValueError) as exc:
            return _json({"error": {"code": "BAD_METRIC_QUERY", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001 — artifact Gallery must remain usable
            print(f"gallery metrics ({owner}): {type(exc).__name__}: {exc}", file=sys.stderr)
            return _json({
                "error": {
                    "code": "METRICS_UNAVAILABLE",
                    "message": "The governed metric catalog could not be loaded.",
                }
            }, 500)

    @m.custom_route("/api/gallery/metrics/{name}", methods=["GET"])
    async def _gallery_metric_detail(request):
        owner, _, error = _home_owner(request)
        if error:
            return error
        try:
            return _json({"metric": _metric_detail_snapshot(
                request.path_params.get("name"),
                request.query_params.get("params"),
                owner=owner,
                days=request.query_params.get("days") or 90,
                bucket=request.query_params.get("bucket") or "raw",
            )})
        except LookupError as exc:
            return _json({"error": {"code": "NOT_FOUND", "message": str(exc)}}, 404)
        except (TypeError, ValueError) as exc:
            return _json({"error": {"code": "BAD_METRIC_QUERY", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001
            print(f"gallery metric detail ({owner}): {type(exc).__name__}: {exc}", file=sys.stderr)
            return _json({
                "error": {
                    "code": "METRIC_UNAVAILABLE",
                    "message": "That metric could not be inspected right now.",
                }
            }, 500)

    @m.custom_route("/api/calliope/metric-follows", methods=["GET"])
    async def _calliope_metric_follows(request):
        if not _semantic_home_enabled():
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        owner, _, error = _home_owner(request)
        if error:
            return error
        try:
            return _json(_metric_follow_snapshot(owner))
        except Exception as exc:  # noqa: BLE001
            print(f"metric follows ({owner}): {type(exc).__name__}: {exc}", file=sys.stderr)
            return _json({"error": {"code": "FOLLOWS_UNAVAILABLE"}}, 500)

    @m.custom_route(
        "/api/calliope/metrics/{name}/follow", methods=["POST", "DELETE"]
    )
    async def _calliope_metric_follow(request):
        if not _semantic_home_enabled():
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        owner, session, error = _home_owner(request)
        if error:
            return error
        try:
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
            body = body if isinstance(body, dict) else {}
            if request.method == "DELETE":
                result = _unfollow_metric(
                    owner, request.path_params.get("name"), body.get("params")
                )
            else:
                result = _follow_metric(
                    owner,
                    session.get("sub") or owner,
                    request.path_params.get("name"),
                    body.get("params"),
                )
            return _json(result, 201 if request.method == "POST" else 200)
        except LookupError as exc:
            return _json({"error": {"code": "NOT_FOUND", "message": str(exc)}}, 404)
        except (TypeError, ValueError) as exc:
            return _json({"error": {"code": "BAD_METRIC_FOLLOW", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001
            print(f"metric follow ({owner}): {type(exc).__name__}: {exc}", file=sys.stderr)
            return _json({
                "error": {
                    "code": "METRIC_FOLLOW_FAILED",
                    "message": "That metric's Brief setting could not be changed.",
                }
            }, 500)

    @m.custom_route("/api/calliope/trails", methods=["POST"])
    async def _follow_calliope_trail(request):
        if not _semantic_home_enabled():
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        owner, _, error = _home_owner(request)
        if error:
            return error
        try:
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
            body = body if isinstance(body, dict) else {}
            return _json(_calliope_follow_trail(
                body.get("handle") or body,
                owner,
                body.get("limit") or 14,
            ))
        except LookupError as exc:
            return _json({"error": {"code": "NOT_FOUND", "message": str(exc)}}, 404)
        except (TypeError, ValueError) as exc:
            return _json({"error": {"code": "BAD_TRAIL", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001 — a missing graph layer must fail closed
            print(f"follow trail ({owner}): {type(exc).__name__}: {exc}", file=sys.stderr)
            return _json({
                "error": {
                    "code": "TRAIL_UNAVAILABLE",
                    "message": "Those breadcrumbs could not be resolved right now.",
                }
            }, 500)

    @m.custom_route("/api/calliope/home", methods=["GET", "PATCH"])
    async def _calliope_home(request):
        if not _semantic_home_enabled():
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        owner, _, error = _home_owner(request)
        if error:
            return error
        try:
            if request.method == "GET":
                return _json(_semantic_home_snapshot(owner))
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
            body = body if isinstance(body, dict) else {}
            item_ids = body.get("item_ids") or []
            if not isinstance(item_ids, list) or len(item_ids) > 96:
                raise ValueError("item_ids must be a bounded ordered list")
            normalized_ids = []
            for item_id in item_ids:
                try:
                    normalized_ids.append(str(uuid.UUID(str(item_id))))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Home item id is invalid") from exc
            if len(set(normalized_ids)) != len(normalized_ids):
                raise ValueError("Home item order contains duplicates")
            with _conn() as conn:
                with conn.transaction():
                    board = _semantic_home_board(conn, owner, create=True)
                    total = int(conn.execute(
                        "SELECT count(*) AS n FROM rvbbit.calliope_board_items "
                        "WHERE board_id=%s::uuid",
                        (board["id"],),
                    ).fetchone()["n"])
                    count = 0
                    if normalized_ids:
                        count = int(conn.execute(
                            "SELECT count(*) AS n FROM rvbbit.calliope_board_items "
                            "WHERE board_id=%s::uuid AND id=ANY(%s::uuid[])",
                            (board["id"], normalized_ids),
                        ).fetchone()["n"])
                    if count != len(normalized_ids):
                        raise PermissionError("One or more Home items do not belong to this user")
                    if total != len(normalized_ids):
                        raise ValueError("item_ids must include every current Home item")
                    for index, item_id in enumerate(normalized_ids, 1):
                        conn.execute(
                            "UPDATE rvbbit.calliope_board_items SET sort_order=%s,updated_at=now() "
                            "WHERE board_id=%s::uuid AND id=%s::uuid",
                            (index * 1000, board["id"], item_id),
                        )
                    conn.execute(
                        "UPDATE rvbbit.calliope_boards SET updated_at=now() WHERE id=%s::uuid",
                        (board["id"],),
                    )
            return _json(_semantic_home_snapshot(owner))
        except PermissionError as exc:
            return _json({"error": {"code": "FORBIDDEN", "message": str(exc)}}, 403)
        except ValueError as exc:
            return _json({"error": {"code": "BAD_HOME", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001
            print(f"semantic home ({owner}): {exc}", file=sys.stderr)
            return _json({
                "error": {
                    "code": "HOME_UNAVAILABLE",
                    "message": "Your Semantic Home could not be loaded.",
                }
            }, 500)

    @m.custom_route("/api/calliope/home/items", methods=["POST"])
    async def _pin_calliope_home_item(request):
        if not _semantic_home_enabled():
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        owner, _, error = _home_owner(request)
        if error:
            return error
        try:
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
            resolved = _semantic_home_resolve_handle(
                body if isinstance(body, dict) else {}, validate_sql=True
            )
            with _conn() as conn:
                with conn.transaction():
                    board = _semantic_home_board(conn, owner, create=True)
                    next_order = conn.execute(
                        "SELECT coalesce(max(sort_order),0)+1000 AS n "
                        "FROM rvbbit.calliope_board_items WHERE board_id=%s::uuid",
                        (board["id"],),
                    ).fetchone()["n"]
                    row = conn.execute(
                        "INSERT INTO rvbbit.calliope_board_items "
                        "(id,board_id,item_kind,canonical_key,source,presentation,sort_order) "
                        "VALUES (%s::uuid,%s::uuid,%s,%s,%s::jsonb,%s::jsonb,%s) "
                        "ON CONFLICT (board_id,canonical_key) DO UPDATE SET "
                        "source=excluded.source,presentation=excluded.presentation,updated_at=now() "
                        "RETURNING *",
                        (
                            str(uuid.uuid4()),
                            board["id"],
                            resolved["kind"],
                            resolved["canonical_key"],
                            json.dumps(resolved["source"], default=str),
                            json.dumps(resolved["presentation"], default=str),
                            next_order,
                        ),
                    ).fetchone()
                    conn.execute(
                        "UPDATE rvbbit.calliope_boards SET updated_at=now() WHERE id=%s::uuid",
                        (board["id"],),
                    )
            return _json({"item": _semantic_home_public_item(row)}, 201)
        except LookupError as exc:
            return _json({"error": {"code": "NOT_FOUND", "message": str(exc)}}, 404)
        except ValueError as exc:
            return _json({"error": {"code": "BAD_HOME_ITEM", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001
            print(f"semantic home pin ({owner}): {exc}", file=sys.stderr)
            return _json({
                "error": {
                    "code": "HOME_PIN_FAILED",
                    "message": "That item could not be pinned to Home.",
                }
            }, 500)

    @m.custom_route("/api/calliope/home/items/{item_id}", methods=["DELETE"])
    async def _remove_calliope_home_item(request):
        if not _semantic_home_enabled():
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        owner, _, error = _home_owner(request)
        if error:
            return error
        try:
            item_id = str(uuid.UUID(str(request.path_params["item_id"])))
        except (TypeError, ValueError):
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        with _conn() as conn:
            row = conn.execute(
                "DELETE FROM rvbbit.calliope_board_items i USING rvbbit.calliope_boards b "
                "WHERE i.id=%s::uuid AND i.board_id=b.id AND b.owner_email=%s "
                "RETURNING i.id,i.board_id",
                (item_id, owner),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE rvbbit.calliope_boards SET updated_at=now() WHERE id=%s::uuid",
                    (row["board_id"],),
                )
        if not row:
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        return _json({"removed": item_id})

    @m.custom_route("/api/calliope/home/items/{item_id}/preview", methods=["GET"])
    async def _preview_calliope_home_item(request):
        if not _semantic_home_enabled():
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        owner, session, error = _home_owner(request)
        if error:
            return error
        try:
            item_id = str(uuid.UUID(str(request.path_params["item_id"])))
        except (TypeError, ValueError):
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        with _conn() as conn:
            row = conn.execute(
                "SELECT i.source,i.item_kind FROM rvbbit.calliope_board_items i "
                "JOIN rvbbit.calliope_boards b ON b.id=i.board_id "
                "WHERE i.id=%s::uuid AND b.owner_email=%s",
                (item_id, owner),
            ).fetchone()
        if not row:
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        if row.get("item_kind") not in {"artifact_object", "metric"}:
            return _json({"error": {"code": "NOT_REPLAYABLE"}}, 400)
        try:
            preview = _semantic_home_preview(
                row.get("source") or {}, session.get("sub") or owner
            )
        except (LookupError, ValueError) as exc:
            return _json({"error": {"code": "PREVIEW_UNAVAILABLE", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001
            print(f"semantic home preview ({owner}:{item_id}): {exc}", file=sys.stderr)
            return _json({
                "error": {
                    "code": "PREVIEW_UNAVAILABLE",
                    "message": "The current value could not be recreated.",
                }
            }, 500)
        return _json({"preview": preview})

    @m.custom_route(
        "/api/calliope/home/items/{item_id}/metric-promotion",
        methods=["GET", "POST"],
    )
    async def _promote_calliope_home_metric(request):
        if not _semantic_home_enabled():
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        owner, session, error = _home_owner(request)
        if error:
            return error
        try:
            item_id = str(uuid.UUID(str(request.path_params["item_id"])))
        except (TypeError, ValueError):
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        execution_subject = session.get("sub") or owner
        try:
            if request.method == "GET":
                with _conn() as conn:
                    row = conn.execute(
                        "SELECT i.source,i.item_kind FROM rvbbit.calliope_board_items i "
                        "JOIN rvbbit.calliope_boards b ON b.id=i.board_id "
                        "WHERE i.id=%s::uuid AND lower(b.owner_email)=lower(%s) AND b.slug='home'",
                        (item_id, owner),
                    ).fetchone()
                if not row:
                    raise LookupError("That Home item is no longer available")
                if row.get("item_kind") != "artifact_object":
                    raise ValueError("Only named business objects can become governed metrics")
                draft = _semantic_home_metric_draft(
                    row.get("source") or {}, execution_subject, preview=True
                )
                return _json({"draft": _semantic_home_metric_draft_public(draft)})
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
            started = time.time()
            result = _promote_semantic_home_metric(
                owner,
                execution_subject,
                item_id,
                body if isinstance(body, dict) else {},
            )
            _record(
                "promote_home_metric",
                {"item_id": item_id, "metric": result["metric"]},
                result,
                None,
                int((time.time() - started) * 1000),
                caller_override=owner,
            )
            return _json(result, 201 if result.get("created") else 200)
        except LookupError as exc:
            return _json({"error": {"code": "NOT_FOUND", "message": str(exc)}}, 404)
        except FileExistsError as exc:
            return _json({"error": {"code": "METRIC_NAME_TAKEN", "message": str(exc)}}, 409)
        except ValueError as exc:
            return _json({"error": {"code": "NOT_PROMOTABLE", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001 — keep Home usable after a failed promotion
            print(f"semantic home metric promotion ({owner}:{item_id}): {exc}", file=sys.stderr)
            return _json({
                "error": {
                    "code": "METRIC_PROMOTION_FAILED",
                    "message": "That business object could not be promoted safely.",
                }
            }, 500)

    @m.custom_route("/api/calliope/watches", methods=["GET", "POST"])
    async def _calliope_watches(request):
        if not _semantic_home_enabled():
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        owner, session, error = _home_owner(request)
        if error:
            return error
        try:
            if request.method == "GET":
                version = request.query_params.get("version")
                return _json(_watch_snapshot(
                    owner,
                    slug=request.query_params.get("slug"),
                    version=int(version) if version not in (None, "") else None,
                    object_id=request.query_params.get("object_id"),
                    limit=request.query_params.get("limit") or 100,
                ))
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
            result = _create_calliope_watch(
                owner,
                session.get("sub") or owner,
                body if isinstance(body, dict) else {},
            )
            _WATCH_WAKE.set()
            return _json(result, 201)
        except LookupError as exc:
            return _json({"error": {"code": "NOT_FOUND", "message": str(exc)}}, 404)
        except (TypeError, ValueError) as exc:
            return _json({"error": {"code": "BAD_WATCH", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001
            print(f"semantic watch ({owner}): {type(exc).__name__}: {exc}", file=sys.stderr)
            return _json({
                "error": {
                    "code": "WATCH_UNAVAILABLE",
                    "message": "That value could not be watched right now.",
                }
            }, 500)

    @m.custom_route("/api/calliope/watches/{watch_id}", methods=["PATCH", "DELETE"])
    async def _calliope_watch(request):
        if not _semantic_home_enabled():
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        owner, _, error = _home_owner(request)
        if error:
            return error
        try:
            watch_id = str(uuid.UUID(str(request.path_params["watch_id"])))
            if request.method == "DELETE":
                result = _delete_calliope_watch(owner, watch_id)
            else:
                try:
                    body = await request.json()
                except Exception:  # noqa: BLE001
                    body = {}
                result = _update_calliope_watch(
                    owner, watch_id, body if isinstance(body, dict) else {}
                )
            _WATCH_WAKE.set()
            return _json(result)
        except LookupError as exc:
            return _json({"error": {"code": "NOT_FOUND", "message": str(exc)}}, 404)
        except (TypeError, ValueError) as exc:
            return _json({"error": {"code": "BAD_WATCH", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001
            print(f"semantic watch update ({owner}): {type(exc).__name__}: {exc}", file=sys.stderr)
            return _json({
                "error": {
                    "code": "WATCH_UNAVAILABLE",
                    "message": "That watch could not be changed right now.",
                }
            }, 500)

    @m.custom_route("/api/calliope/watches/{watch_id}/check", methods=["POST"])
    async def _check_calliope_watch(request):
        if not _semantic_home_enabled():
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        owner, _, error = _home_owner(request)
        if error:
            return error
        try:
            watch_id = str(uuid.UUID(str(request.path_params["watch_id"])))
            with _conn() as conn:
                owned = conn.execute(
                    "SELECT 1 FROM rvbbit.calliope_watches WHERE id=%s::uuid AND owner_email=%s",
                    (watch_id, owner),
                ).fetchone()
            if not owned:
                raise LookupError("No such watch")
            check = _calliope_watch_tick(force_watch_id=watch_id)
            snapshot = _watch_snapshot(owner)
            watch = next((item for item in snapshot["watches"] if item["id"] == watch_id), None)
            return _json({"watch": watch, "check": check})
        except LookupError as exc:
            return _json({"error": {"code": "NOT_FOUND", "message": str(exc)}}, 404)
        except (TypeError, ValueError) as exc:
            return _json({"error": {"code": "BAD_WATCH", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001
            print(f"semantic watch check ({owner}): {type(exc).__name__}: {exc}", file=sys.stderr)
            return _json({
                "error": {
                    "code": "WATCH_CHECK_FAILED",
                    "message": "That value could not be checked right now.",
                }
            }, 500)

    @m.custom_route("/api/calliope/watch-events", methods=["GET"])
    async def _calliope_watch_event_feed(request):
        if not _semantic_home_enabled():
            return _json({"error": {"code": "NOT_FOUND"}}, 404)
        owner, _, error = _home_owner(request)
        if error:
            return error
        try:
            unread = str(request.query_params.get("unread") or "").lower() in {
                "1", "true", "yes", "on",
            }
            return _json(_calliope_watch_events(
                owner,
                watch_id=request.query_params.get("watch_id"),
                unread_only=unread,
                limit=request.query_params.get("limit") or 100,
            ))
        except (TypeError, ValueError) as exc:
            return _json({"error": {"code": "BAD_WATCH_EVENT_QUERY", "message": str(exc)}}, 400)
        except Exception as exc:  # noqa: BLE001
            print(f"semantic watch events ({owner}): {type(exc).__name__}: {exc}", file=sys.stderr)
            return _json({"error": {"code": "WATCH_EVENTS_UNAVAILABLE"}}, 500)

    @m.custom_route("/api/d/{slug}/inspect", methods=["POST"])
    async def _inspect(request):
        email = auth.read_session(request)
        if not email:
            return _json({"error": {"code": "UNAUTHORIZED"}}, 401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        body = body if isinstance(body, dict) else {}
        try:
            version = body.get("version")
            selected_version = int(version) if version is not None else None
            if selected_version is not None and selected_version < 1:
                raise ValueError("version must be a positive integer")
            tok = _SESSION_SUB.set(email)
            try:
                result = _dashboard_inspection(
                    request.path_params["slug"],
                    selected_version,
                    body.get("target"),
                    body.get("binding"),
                    body.get("trace"),
                    body.get("semantic_object"),
                    body.get("as_of"),
                )
            finally:
                _SESSION_SUB.reset(tok)
        except (TypeError, ValueError) as exc:
            return _json(
                {"error": {"code": "BAD_INSPECTION", "message": str(exc)}},
                400,
            )
        except Exception as exc:  # noqa: BLE001 — the dashboard remains usable
            print(
                f"artifact inspection ({request.path_params['slug']}): {exc}",
                file=sys.stderr,
            )
            return _json(
                {
                    "error": {
                        "code": "INSPECTION_UNAVAILABLE",
                        "message": "The evidence graph could not be assembled.",
                    }
                },
                500,
            )
        if result.get("error"):
            code = result["error"].get("code")
            return _json(result, 404 if code in {"NOT_FOUND", "VERSION_NOT_FOUND"} else 400)
        try:
            import calliope
            result["calliope_enabled"] = calliope.is_enabled()
        except Exception:  # noqa: BLE001
            result["calliope_enabled"] = False
        return _json(result)

    @m.custom_route("/api/d/{slug}/q", methods=["POST"])
    async def _data(request):
        email = auth.read_session(request)
        if not email:
            return _json({"error": {"code": "UNAUTHORIZED"}}, 401)
        slug = request.path_params["slug"]
        try:
            body = await request.json()
        except Exception:   # noqa: BLE001
            body = {}
        sql = (body or {}).get("sql")
        if not sql:
            return _json({"error": {"code": "MISSING_SQL"}}, 400)
        as_of = (body or {}).get("as_of")
        origin = str((body or {}).get("origin") or "dashboard")
        if origin not in {"dashboard", "artifact-lens", "semantic-lens"}:
            origin = "dashboard"
        t0 = time.time()
        # Burrow: the viewer's session identity IS a PG role — app queries run
        # under it (parked in a contextvar; tool schemas stay clean).
        tok = _SESSION_SUB.set(email)
        try:
            res = tool_run_sql(sql, as_of)
        finally:
            _SESSION_SUB.reset(tok)
        _record("dashboard_query", {
            "dashboard": slug,
            "sql": sql,
            "as_of": as_of,
            "origin": origin,
        },
                res, res.get("error"), int((time.time() - t0) * 1000), caller_override=email)
        return _json(res, 400 if res.get("error") else 200)

    @m.custom_route("/apps/{slug}/versions/{version}", methods=["GET"])
    async def _view_app_version(request):
        # Historical app URLs are immutable documents.  Never proxy them to a
        # currently running Python app, whose code may have advanced versions.
        return await _render_artifact_document(request, request.path_params.get("version"))

    @m.custom_route("/apps/{slug}", methods=["GET"])
    async def _view_app(request):
        proxied = await _proxy_runner(request)
        if proxied is not None:
            if 200 <= proxied.status_code < 300:
                _record_running_app_view(request)
            return proxied
        return await _view(request)

    @m.custom_route("/apps/{slug}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def _proxy_app_path(request):
        proxied = await _proxy_runner(request, request.path_params.get("path") or "")
        if proxied is not None:
            if 200 <= proxied.status_code < 300 and _document_navigation(request):
                _record_running_app_view(request)
            return proxied
        return await _render_artifact_document(
            request,
            record_view=_document_navigation(request),
        )

    @m.custom_route("/api/apps/{slug}/q", methods=["POST"])
    async def _data_app(request):
        return await _data(request)

    return _view, _data, _view_app, _proxy_app_path, _data_app, _landing, _landing_alias


# ── MCP server ───────────────────────────────────────────────────────────────

def _register(mcp):
    # FastMCP detects injected Context parameters from the concrete runtime
    # annotation.  This module postpones annotations, so attach it explicitly
    # and keep request metadata out of each public tool schema.
    from mcp.server.fastmcp import Context as FastMCPContext
    for attributed_tool in (
        _mcp_upload_artifact,
        _mcp_publish_dashboard,
        _mcp_update_dashboard,
        _mcp_create_live_app,
        _mcp_update_live_app,
    ):
        attributed_tool.__annotations__["ctx"] = FastMCPContext

    mcp.tool(name="search_data")(lambda query, limit=8, schema=None: _logged(
        "search_data", {"query": query, "limit": limit, "schema": schema},
        lambda: tool_search_data(query, limit, schema)))
    mcp.tool(name="capability_search")(lambda query, limit=8, kinds=None: _logged(
        "capability_search", {"query": query, "limit": limit, "kinds": kinds},
        lambda: tool_capability_search(query, limit, kinds)))
    mcp.tool(name="render_pdf")(lambda name, html=None, slug=None, source_artifact_id=None, width=816, height=1056, landscape=False, wait_ms=900: _logged(
        "render_pdf", {"name": name, "slug": slug},
        lambda: tool_render_pdf(name, html, slug, source_artifact_id, width, height, landscape, wait_ms)))
    mcp.tool(name="extract_image")(lambda path, fields, model=None, prompt=None: _logged(
        "extract_image", {"path": path, "fields": fields, "model": model},
        lambda: tool_extract_image(path, fields, model, prompt)))
    mcp.tool(name="kit_rehearsal")(lambda kit, scenario=None, model=None: _logged(
        "kit_rehearsal", {"kit": kit, "scenario": scenario},
        lambda: tool_kit_rehearsal(kit, scenario, model)))
    mcp.tool(name="describe_table")(lambda table, lean=False: _logged(
        "describe_table", {"table": table, "lean": lean}, lambda: tool_describe_table(table, lean)))
    mcp.tool(name="profile_schema")(lambda schema=None: _logged(
        "profile_schema", {"schema": schema}, lambda: tool_profile_schema(schema)))
    mcp.tool(name="list_metrics")(lambda category=None, search=None: _logged(
        "list_metrics", {"category": category, "search": search},
        lambda: tool_list_metrics(category, search)))
    mcp.tool(name="get_metric")(lambda name: _logged(
        "get_metric", {"name": name}, lambda: tool_get_metric(name)))
    mcp.tool(name="list_cubes")(lambda category=None: _logged(
        "list_cubes", {"category": category}, lambda: tool_list_cubes(category)))
    mcp.tool(name="set_category")(lambda kind, name, category=None, subcategory=None: _logged(
        "set_category", {"kind": kind, "name": name, "category": category, "subcategory": subcategory},
        lambda: tool_set_category(kind, name, category, subcategory)))
    mcp.tool(name="describe_cube")(lambda name: _logged(
        "describe_cube", {"name": name}, lambda: tool_describe_cube(name)))
    mcp.tool(name="propose_cube")(lambda subject, seed_tables=None, schema=None: _logged(
        "propose_cube", {"subject": subject, "seed_tables": seed_tables, "schema": schema},
        lambda: tool_propose_cube(subject, seed_tables, schema)))
    mcp.tool(name="propose_metric")(lambda subject, seed_sources=None, schema=None: _logged(
        "propose_metric", {"subject": subject, "seed_sources": seed_sources, "schema": schema},
        lambda: tool_propose_metric(subject, seed_sources, schema)))
    mcp.tool(name="list_proposals")(lambda status=None, kind=None, proposed_by=None, limit=20: _logged(
        "list_proposals", {"status": status, "kind": kind, "proposed_by": proposed_by, "limit": limit},
        lambda: tool_list_proposals(status, kind, proposed_by, limit)))
    mcp.tool(name="get_proposal")(lambda proposal_id: _logged(
        "get_proposal", {"proposal_id": proposal_id}, lambda: tool_get_proposal(proposal_id)))
    mcp.tool(name="refine_proposal")(lambda proposal_id, name=None, sql=None, grain=None, description=None, params=None, check_sql=None, join_rationale=None, confidence=None, category=None, subcategory=None: _logged(
        "refine_proposal", {"proposal_id": proposal_id},
        lambda: tool_refine_proposal(proposal_id, name, sql, grain, description, params, check_sql, join_rationale, confidence, category, subcategory)))
    mcp.tool(name="withdraw_proposal")(lambda proposal_id, reason=None: _logged(
        "withdraw_proposal", {"proposal_id": proposal_id, "reason": reason},
        lambda: tool_withdraw_proposal(proposal_id, reason)))
    mcp.tool(name="edit_metric")(lambda name, sql=None, grain=None, description=None, params=None, check_sql=None, category=None, subcategory=None: _logged(
        "edit_metric", {"name": name},
        lambda: tool_edit_metric(name, sql, grain, description, params, check_sql, category, subcategory)))
    mcp.tool(name="edit_cube")(lambda name, sql, grain=None, description=None, category=None, subcategory=None: _logged(
        "edit_cube", {"name": name},
        lambda: tool_edit_cube(name, sql, grain, description, category, subcategory)))
    mcp.tool(name="metric")(lambda name, params=None, as_of=None, def_as_of=None, group_by=None: _logged(
        "metric", {"name": name, "params": params, "as_of": as_of, "def_as_of": def_as_of, "group_by": group_by},
        lambda: tool_metric(name, params, as_of, def_as_of, group_by)))
    mcp.tool(name="metric_dimensions")(lambda name: _logged(
        "metric_dimensions", {"name": name}, lambda: tool_metric_dimensions(name)))
    mcp.tool(name="materialize_metric")(lambda name, params=None, as_of=None, def_as_of=None: _logged(
        "materialize_metric", {"name": name},
        lambda: tool_materialize_metric(name, params, as_of, def_as_of)))
    mcp.tool(name="metric_history")(lambda name, limit=50: _logged(
        "metric_history", {"name": name, "limit": limit}, lambda: tool_metric_history(name, limit)))
    mcp.tool(name="breaching_kpis")(lambda: _logged("breaching_kpis", {}, tool_breaching_kpis))
    mcp.tool(name="metric_lineage")(lambda name: _logged(
        "metric_lineage", {"name": name}, lambda: tool_metric_lineage(name)))
    # alerts — observe + operate + author-conditions (T0+T1)
    mcp.tool(name="list_alerts")(lambda category=None, enabled=None, muted=None, tier=None, search=None, limit=50: _logged(
        "list_alerts", {"category": category, "enabled": enabled, "muted": muted, "tier": tier, "search": search, "limit": limit},
        lambda: tool_list_alerts(category, enabled, muted, tier, search, limit)))
    mcp.tool(name="get_alert")(lambda name: _logged("get_alert", {"name": name}, lambda: tool_get_alert(name)))
    mcp.tool(name="alert_state")(lambda name, limit=200: _logged(
        "alert_state", {"name": name, "limit": limit}, lambda: tool_alert_state(name, limit)))
    mcp.tool(name="alert_events")(lambda name=None, limit=50: _logged(
        "alert_events", {"name": name, "limit": limit}, lambda: tool_alert_events(name, limit)))
    mcp.tool(name="alert_sweep_runs")(lambda limit=40: _logged(
        "alert_sweep_runs", {"limit": limit}, lambda: tool_alert_sweep_runs(limit)))
    mcp.tool(name="breaching_alerts")(lambda: _logged("breaching_alerts", {}, tool_breaching_alerts))
    mcp.tool(name="set_alert_enabled")(lambda name, enabled: _logged(
        "set_alert_enabled", {"name": name, "enabled": enabled}, lambda: tool_set_alert_enabled(name, enabled)))
    mcp.tool(name="mute_alert")(lambda name, minutes=None: _logged(
        "mute_alert", {"name": name, "minutes": minutes}, lambda: tool_mute_alert(name, minutes)))
    mcp.tool(name="unmute_alert")(lambda name: _logged("unmute_alert", {"name": name}, lambda: tool_unmute_alert(name)))
    mcp.tool(name="set_alert_cadence")(lambda name, tier: _logged(
        "set_alert_cadence", {"name": name, "tier": tier}, lambda: tool_set_alert_cadence(name, tier)))
    mcp.tool(name="set_alerts_enabled")(lambda on: _logged(
        "set_alerts_enabled", {"on": on}, lambda: tool_set_alerts_enabled(on)))
    mcp.tool(name="run_alert_sweep")(lambda tier="normal": _logged(
        "run_alert_sweep", {"tier": tier}, lambda: tool_run_alert_sweep(tier)))
    mcp.tool(name="run_alert_worker")(lambda max_items=50: _logged(
        "run_alert_worker", {"max_items": max_items}, lambda: tool_run_alert_worker(max_items)))
    mcp.tool(name="preview_alert_condition")(lambda query, expr=None: _logged(
        "preview_alert_condition", {"query": query, "expr": expr}, lambda: tool_preview_alert_condition(query, expr)))
    mcp.tool(name="preview_metric_observation")(lambda metric: _logged(
        "preview_metric_observation", {"metric": metric}, lambda: tool_preview_metric_observation(metric)))
    # consumer verbs — opinionated, pre-shaped business views
    mcp.tool(name="scoreboard")(lambda category=None, grain="month", periods=6, as_of=None: _logged(
        "scoreboard", {"category": category, "grain": grain, "periods": periods, "as_of": as_of},
        lambda: tool_scoreboard(category, grain, periods, as_of)))
    mcp.tool(name="pivot")(lambda metric, rows, cols, measure=None, params=None, as_of=None: _logged(
        "pivot", {"metric": metric, "rows": rows, "cols": cols, "measure": measure, "as_of": as_of},
        lambda: tool_pivot(metric, rows, cols, measure, params, as_of)))
    mcp.tool(name="cube_pivot")(lambda cube, rows=None, cols=None, measure=None, aggregate="sum", measures=None: _logged(
        "cube_pivot",
        {
            "cube": cube,
            "rows": rows,
            "cols": cols,
            "measure": measure,
            "aggregate": aggregate,
            "measures": measures,
        },
        lambda: tool_cube_pivot(cube, rows, cols, measure, aggregate, measures)))
    mcp.tool(name="compare")(lambda metric, period_a, period_b, by=None, params=None: _logged(
        "compare", {"metric": metric, "period_a": period_a, "period_b": period_b, "by": by},
        lambda: tool_compare(metric, period_a, period_b, by, params)))
    # document brain — caller identity comes from the OAuth token (_caller), never a tool argument
    mcp.tool(name="ask_brain")(lambda query, k=8, filters=None: _logged(
        "ask_brain", {"query": query, "k": k, "filters": filters},
        lambda: tool_ask_brain(query, k, filters, _caller()[0])))
    mcp.tool(name="system_learning_status")(lambda: _logged(
        "system_learning_status", {}, tool_system_learning_status))
    mcp.tool(name="sync_system_learning")(lambda: _logged(
        "sync_system_learning", {}, tool_sync_system_learning))
    mcp.tool(name="ask_system_learning")(lambda query, k=8: _logged(
        "ask_system_learning", {"query": query, "k": k},
        lambda: tool_ask_system_learning(query, k, _caller()[0])))
    mcp.tool(name="brain_facets")(lambda: _logged(
        "brain_facets", {}, lambda: tool_brain_facets(_caller()[0])))
    mcp.tool(name="brain_browse")(lambda: _logged(
        "brain_browse", {}, lambda: tool_brain_browse(_caller()[0])))
    mcp.tool(name="brain_get_doc")(lambda doc_id: _logged(
        "brain_get_doc", {"doc_id": doc_id}, lambda: tool_brain_get_doc(doc_id, _caller()[0])))
    mcp.tool(name="brain_context")(lambda doc_id, chunk_idx, window=2: _logged(
        "brain_context", {"doc_id": doc_id, "chunk_idx": chunk_idx, "window": window},
        lambda: tool_brain_context(doc_id, chunk_idx, window, _caller()[0])))
    mcp.tool(name="brain_related")(lambda doc_id: _logged(
        "brain_related", {"doc_id": doc_id}, lambda: tool_brain_related(doc_id, _caller()[0])))
    mcp.tool(name="brain_entity")(lambda name: _logged(
        "brain_entity", {"name": name}, lambda: tool_brain_entity(name, _caller()[0])))
    mcp.tool(name="brain_ingest")(lambda source, title, body, roles=None, folder=None, uri=None, author=None, occurred_at=None: _logged(
        "brain_ingest", {"source": source, "title": title, "roles": roles},
        lambda: tool_brain_ingest(source, title, body, roles, folder, uri, author, occurred_at)))
    mcp.tool(name="brain_grant")(lambda role, principal, on=True: _logged(
        "brain_grant", {"role": role, "principal": principal, "on": on},
        lambda: tool_brain_grant(role, principal, on)))
    mcp.tool(name="brain_exclude")(lambda doc_id, principal, reason=None: _logged(
        "brain_exclude", {"doc_id": doc_id, "principal": principal},
        lambda: tool_brain_exclude(doc_id, principal, reason)))
    mcp.tool(name="brain_crawl_folder")(lambda path, source=None, roles=None, base_folder=None, recursive=True, max_files=500: _logged(
        "brain_crawl_folder", {"path": path, "source": source, "roles": roles, "recursive": recursive},
        lambda: tool_brain_crawl_folder(path, source, roles, base_folder, recursive, max_files)))
    mcp.tool(name="brain_set_doc_roles")(lambda doc_id, roles=None: _logged(
        "brain_set_doc_roles", {"doc_id": doc_id, "roles": roles},
        lambda: tool_brain_set_doc_roles(doc_id, roles)))
    mcp.tool(name="validate_sql")(lambda sql, as_of=None: _logged(
        "validate_sql", {"sql": sql, "as_of": as_of}, lambda: tool_validate_sql(sql, as_of)))
    mcp.tool(name="run_sql")(lambda sql, as_of=None, limit=None: _logged(
        "run_sql", {"sql": sql, "as_of": as_of, "limit": limit},
        lambda: tool_run_sql(sql, as_of, limit)))

    # ── tool discovery: search the catalog instead of "tasting" tools ────────
    # This server exposes ~80 tools; agents burn calls (and context) probing
    # them one by one. search_tools ranks the catalog for a task description;
    # get_tool_help returns full descriptions + schemas for the shortlist.
    # Index is built lazily from the SAME registry agents see (the FastMCP
    # tool manager), so it can never drift from reality.
    def _tool_index():
        out = []
        for t in mcp._tool_manager.list_tools():
            params = []
            try:
                params = list(((t.parameters or {}).get("properties") or {}).keys())
            except Exception:
                pass
            out.append({"name": t.name, "description": t.description or "", "params": params})
        return out

    def tool_search_tools(query, limit=8):
        import re as _re
        limit = max(1, min(int(limit or 8), 25))
        index = [t for t in _tool_index() if t["name"] not in ("search_tools", "get_tool_help")]
        words = [w for w in _re.split(r"[^a-z0-9]+", (query or "").lower()) if len(w) > 1]
        if not words:
            names = sorted(t["name"] for t in index)
            return {"tools": names, "count": len(names),
                    "hint": "pass a task description, e.g. search_tools('build a live dashboard')"}
        scored = []
        for t in index:
            name_toks = set(_re.split(r"[^a-z0-9]+", t["name"].lower()))
            desc = t["description"].lower()
            score = 0
            for w in words:
                if w in name_toks:
                    score += 5
                elif any(w in nt for nt in name_toks):
                    score += 3
                if w in desc:
                    score += 1
                if any(w in p.lower() for p in t["params"]):
                    score += 1
            if score > 0:
                scored.append((score, t))
        scored.sort(key=lambda x: (-x[0], x[1]["name"]))
        return {
            "matches": [{
                "name": t["name"],
                "score": sc,
                "description": t["description"].split("\n")[0][:180],
                "params": t["params"][:10],
            } for sc, t in scored[:limit]],
            "hint": "call get_tool_help(names=[...]) for full descriptions and argument schemas; "
                    "for reads, ONE run_sql (or run_sql_multi) usually beats several small tool calls",
        }

    def tool_get_tool_help(names):
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list) or not names:
            return {"error": {"code": "BAD_NAMES", "message": "names must be a non-empty list of tool names"}}
        by_name = {}
        for t in mcp._tool_manager.list_tools():
            by_name[t.name] = t
        out, missing = [], []
        for n in [str(x) for x in names][:16]:
            t = by_name.get(n)
            if not t:
                missing.append(n)
                continue
            out.append({"name": t.name, "description": t.description or "", "schema": t.parameters})
        res = {"tools": out}
        if missing:
            res["missing"] = missing
        return res

    mcp.tool(name="search_tools")(lambda query, limit=8: _logged(
        "search_tools", {"query": query, "limit": limit},
        lambda: tool_search_tools(query, limit)))
    mcp.tool(name="get_tool_help")(lambda names: _logged(
        "get_tool_help", {"names": names},
        lambda: tool_get_tool_help(names)))
    mcp.tool(name="run_sql_multi")(lambda queries, as_of=None, limit=None, result_mode="full", preview_rows=3: _logged(
        "run_sql_multi", {"queries": queries, "as_of": as_of, "limit": limit, "result_mode": result_mode},
        lambda: tool_run_sql_multi(queries, as_of, limit, result_mode, preview_rows)))
    mcp.tool(name="upload_artifact")(_mcp_upload_artifact)
    mcp.tool(name="publish_dashboard")(_mcp_publish_dashboard)
    mcp.tool(name="update_dashboard")(_mcp_update_dashboard)
    mcp.tool(name="list_dashboards")(_mcp_list_dashboards)
    mcp.tool(name="get_dashboard")(_mcp_get_dashboard)
    mcp.tool(name="dashboard_crawl")(_mcp_dashboard_crawl)
    mcp.tool(name="dashboard_dependents")(_mcp_dashboard_dependents)
    mcp.tool(name="dashboard_template")(_mcp_dashboard_template)
    mcp.tool(name="tanstack_chart_template")(_mcp_tanstack_chart_template)
    mcp.tool(name="calliope_work_item")(_mcp_calliope_work_item)
    mcp.tool(name="draft_calliope_instrument")(_mcp_draft_calliope_instrument)
    mcp.tool(name="draft_calliope_workflow")(_mcp_draft_calliope_workflow)
    mcp.tool(name="search_calliope_actions")(_mcp_search_calliope_actions)
    mcp.tool(name="plan_calliope_action")(_mcp_plan_calliope_action)
    mcp.tool(name="execute_calliope_action")(_mcp_execute_calliope_action)
    mcp.tool(name="begin_calliope_workflow_run")(_mcp_begin_calliope_workflow_run)
    mcp.tool(name="get_calliope_personal_context")(_mcp_get_calliope_personal_context)
    mcp.tool(name="finish_calliope_workflow_run")(_mcp_finish_calliope_workflow_run)
    mcp.tool(name="live_app_template")(_mcp_live_app_template)
    mcp.tool(name="create_live_app")(_mcp_create_live_app)
    mcp.tool(name="update_live_app")(_mcp_update_live_app)
    mcp.tool(name="semantic_enrichment_status")(_mcp_semantic_enrichment_status)
    mcp.tool(name="enrich_live_app")(_mcp_enrich_live_app)
    mcp.tool(name="list_live_apps")(_mcp_list_live_apps)
    mcp.tool(name="get_live_app")(_mcp_get_live_app)
    mcp.tool(name="debug_live_app")(_mcp_debug_live_app)
    mcp.tool(name="live_app_logs")(_mcp_live_app_logs)
    mcp.tool(name="start_live_app")(_mcp_start_live_app)
    mcp.tool(name="stop_live_app")(_mcp_stop_live_app)
    mcp.tool(name="live_app_status")(_mcp_live_app_status)
    mcp.tool(name="capture_live_app")(_mcp_capture_live_app)


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
    # regression: lean=True iterated _col_stats (a dict) as rows → TypeError
    show("describe_table(lean=True) — must not crash", tool_describe_table("public._demo_revenue", lean=True))
    show("run_sql_multi(result_mode='summary')", tool_run_sql_multi(
        {"a": "SELECT region, drop_pct FROM public._demo_revenue",
         "b": "SELECT bogus_col FROM public._demo_revenue"},
        result_mode="summary", preview_rows=2))
    # artifact staging round trip: upload (2 chunks) → publish by handle → read back
    art = tool_upload_artifact("<html><body>selftest", name="selftest-artifact")
    art2 = tool_upload_artifact(" dashboard</body></html>", artifact_id=art.get("artifact_id"), append=True)
    show("upload_artifact (chunked)", art2)
    pub = tool_publish_dashboard("selftest artifact dash", source_artifact_id=art.get("artifact_id"))
    show("publish_dashboard(source_artifact_id=...)", pub)
    if not pub.get("error"):
        got = tool_get_dashboard(pub["slug"])
        v = (got.get("version") or {}) if isinstance(got, dict) else {}
        ok = "selftest dashboard" in (v.get("html") or "")
        show("published html matches staged artifact", {"match": ok, "version": v.get("version")})
        with _conn() as c:   # selftest tidiness — don't leave the fixture dashboard behind
            c.execute("DELETE FROM rvbbit.dashboards WHERE slug=%s", (pub["slug"],))
    show("publish_dashboard(no html, no handle) — must be a structured error",
         tool_publish_dashboard("selftest empty dash"))
    # _logged must degrade exceptions to {"error": ...}, never raise (circuit-breaker fix)
    show("_logged(exception) — structured error, no raise",
         _logged("selftest_boom", {}, lambda: (_ for _ in ()).throw(TypeError("boom"))))
    # activity log: ensure the table, log one call through the wrapper, read it back
    _ensure_activity_table()
    _logged("search_data", {"query": "orders"}, lambda: tool_search_data("orders", 2))
    with _conn() as c:
        row = c.execute(f"SELECT count(*) AS n FROM {ACTIVITY_TABLE}").fetchone()
        recent = c.execute(
            f"SELECT tool, objects, rows, elapsed_ms FROM {ACTIVITY_TABLE} ORDER BY ts DESC LIMIT 1").fetchone()
    show(f"activity log ({ACTIVITY_TABLE})", {"total_rows": row["n"], "most_recent": recent})
    print("\nselftest done")


_INSTRUCTIONS = (
    "rvbbit warehouse — a governed, semantic, time-travel data warehouse. Discover tables/columns "
    "by what their data is about with search_data; get official numbers with metric(); explore SQL "
    "with validate_sql then run_sql (read-only). Use system_learning_status and ask_system_learning "
    "before tuning or diagnosing RVBBIT workloads: they expose learned routing, acceleration, layout, "
    "and operator breadcrumbs from the same Brain corpus the SQL Desktop shows. "
    "TOOL DISCOVERY: this server exposes ~80 tools — when unsure which to use, call "
    "search_tools('what you want to do') and then get_tool_help(names) for the shortlist, instead "
    "of probing tools one by one. For reads, prefer ONE run_sql / run_sql_multi — nearly everything "
    "readable here has a SQL analog. "
    "TO BUILD A LIVE APP: call `live_app_template(runtime_kind='html')` FIRST, edit the template, "
    "and call create_live_app. Hosted HTML apps live at /d/<slug>, are versioned, and call "
    "rvbbitQuery(sql) for live read-only data — one FLAT query per data concern (batch them with "
    "run_sql_multi in-Cowork; never assemble app JSON inside SQL with json_build_object). "
    "After every HTML publication RVBBIT automatically compiles a version-keyed semantic overlay "
    "from rendered DOM, filter controls, query traces, source, and screenshot evidence. The build "
    "must not wait for that pass. An authored manifest.semantic_map or bindBusinessObject call is "
    "optional precision metadata and overrides the generated overlay; do not sacrifice artifact "
    "quality to manufacture it. "
    "Use list_live_apps, get_live_app, update_live_app, live_app_logs, and debug_live_app to "
    "maintain them. For Python FastAPI apps, call start_live_app to run the current version under "
    "local uvicorn, stop_live_app to stop it, live_app_status to inspect runner state, and "
    "capture_live_app to create a PNG screenshot. The legacy dashboard_template/publish_dashboard "
    "tools remain for compatibility. "
    "OPTIONAL TANSTACK CHARTS EXPERIMENT: only when the user explicitly requests TanStack Charts "
    "or semantic keyed SVG marks, call tanstack_chart_template instead. It leaves the default "
    "Chart.js/arbitrary-HTML path untouched and returns a pinned starter plus its semantic manifest. "
    "NO LOCAL GLUE NEEDED: to publish a large document, upload_artifact(content) once and pass "
    "source_artifact_id to publish/update tools (no local file reads, no re-transmission). To "
    "VALIDATE a query set, run_sql_multi(queries, result_mode='summary') returns row counts + "
    "tiny previews instead of full rowsets. capture_live_app(return_image=true) returns the PNG "
    "as viewable image content plus bridge health (queries run/failed, console + page errors). "
    "THE HUB: publish/update tools return hub_url — the DataRabbit gallery of everything made "
    "through this server (search, previews, lineage). When you hand the user their app link, "
    "hand them hub_url too; it is the front door to all their artifacts."
)


def _build_mcp():
    from mcp.server.fastmcp import FastMCP
    m = FastMCP("rvbbit-warehouse", instructions=_INSTRUCTIONS)
    _register(m)
    _ensure_activity_table()
    _ensure_dashboard_tables()
    _start_semantic_enrichment_worker()
    return m


def _calliope_cube_pivot(
    cube,
    rows,
    cols,
    measure,
    aggregate,
    measures,
    execution_subject,
    owner,
):
    """Run a browser pivot under its database subject and log the human owner."""
    args = {
        "cube": cube,
        "rows": rows,
        "cols": cols,
        "measure": measure,
        "aggregate": aggregate,
        "measures": measures,
    }
    t0 = time.time()
    token = _SESSION_SUB.set(execution_subject)
    try:
        result = tool_cube_pivot(cube, rows, cols, measure, aggregate, measures)
    except Exception as exc:  # noqa: BLE001
        result = {
            "error": {
                "code": "EXCEPTION",
                "message": f"{type(exc).__name__}: {exc}",
            }
        }
    finally:
        _SESSION_SUB.reset(token)
    _record(
        "cube_pivot",
        args,
        result,
        result.get("error") if isinstance(result, dict) else None,
        int((time.time() - t0) * 1000),
        caller_override=owner,
    )
    return result


def _calliope_artifact_capture(slug, version, execution_subject, owner):
    """Render one exact HTML artifact version for owner-private Calliope markup."""
    args = {
        "slug": str(slug or ""),
        "version": version,
        "width": 1200,
        "height": 800,
        "full_page": False,
        "wait_ms": 900,
        "origin": "calliope_markup",
    }
    started = time.time()
    token = _SESSION_SUB.set(execution_subject)
    try:
        app, row = _load_live_app_version(args["slug"], version)
        if not app or not row:
            result = {
                "error": {
                    "code": "VERSION_NOT_FOUND",
                    "message": "That artifact version is no longer available.",
                }
            }
        elif _normalize_runtime_kind(app.get("runtime_kind")) != "html":
            result = {
                "error": {
                    "code": "CAPTURE_UNAVAILABLE",
                    "message": "Interactive runtime snapshots are not available for markup yet.",
                }
            }
        else:
            resolved_version = int(row["version"])
            out = _default_capture_path(args["slug"], resolved_version)
            telemetry = _capture_html_with_playwright(
                row.get("html") or "",
                out,
                args["width"],
                args["height"],
                args["full_page"],
                args["wait_ms"],
            )
            result = {
                "slug": args["slug"],
                "version": resolved_version,
                "runtime_kind": "html",
                "path": str(out),
                "bytes": out.stat().st_size if out.is_file() else None,
                "width": args["width"],
                "height": args["height"],
                "full_page": args["full_page"],
                "source": "stored-html",
                "bridge": telemetry,
            }
    except Exception as exc:  # noqa: BLE001
        result = {
            "error": {
                "code": "CAPTURE_FAILED",
                "message": f"{type(exc).__name__}: {exc}"[:600],
            }
        }
    finally:
        _SESSION_SUB.reset(token)
    _record(
        "capture_live_app",
        args,
        result,
        result.get("error") if isinstance(result, dict) else None,
        int((time.time() - started) * 1000),
        caller_override=owner,
    )
    return result


_CALLIOPE_EVIDENCE_STOP_WORDS = {
    "about", "all", "and", "are", "can", "company", "data", "did", "does",
    "find", "for", "from", "have", "into", "its", "me", "our", "show", "that",
    "the", "their", "this", "was", "what", "when", "where", "which", "with",
}


def _calliope_evidence_text(value, limit=2_000):
    if isinstance(value, str):
        raw = value
    elif value in (None, ""):
        raw = ""
    else:
        raw = json.dumps(value, ensure_ascii=False, default=str)
    return re.sub(r"\s+", " ", raw).strip()[:limit]


def _calliope_evidence_terms(query):
    terms = []
    for token in re.findall(r"[a-z0-9][a-z0-9_.-]*", str(query or "").lower()):
        if (len(token) >= 3 or any(ch.isdigit() for ch in token)) \
                and token not in _CALLIOPE_EVIDENCE_STOP_WORDS \
                and token not in terms:
            terms.append(token)
    return terms[:16]


def _calliope_lexical_score(query, title, body=""):
    """Small deterministic ranker for artifacts not yet projected into Brain."""
    needle = _calliope_evidence_text(query, 600).lower()
    heading = _calliope_evidence_text(title, 500).lower()
    haystack = f"{heading} {_calliope_evidence_text(body, 24_000).lower()}"
    terms = _calliope_evidence_terms(needle)
    if not needle or not haystack:
        return 0.0
    score = 0.0
    if needle == heading:
        score += 0.72
    elif needle in heading:
        score += 0.48
    elif needle in haystack:
        score += 0.28
    if terms:
        title_hits = sum(term in heading for term in terms)
        body_hits = sum(term in haystack for term in terms)
        score += 0.34 * (body_hits / len(terms))
        score += 0.18 * (title_hits / len(terms))
    return round(min(score, 1.0), 4)


def _calliope_brain_evidence(query, owner, limit):
    # Brain can contain a large volume of internal system-learning notes. Fetch
    # beyond the requested presentation limit so filtering that operational
    # noise does not crowd ordinary company documents out of the resolver.
    search_limit = min(max(limit * 4, limit), 50)
    with _conn() as c:
        rows = c.execute(
            "SELECT doc_id,chunk_idx,title,folder_path AS folder,source,doc_type,"
            "occurred_at::text AS occurred_at,chunk,score,entities "
            "FROM rvbbit.brain_search(%s,%s,%s,'{}'::jsonb)",
            (owner, query, search_limit),
        ).fetchall()
    items = []
    system_terms = {
        "acceleration", "accelerator", "engine", "layout", "operator", "performance",
        "route", "routing", "rvbbit", "slow", "vortex", "workload",
    }
    include_system_learning = bool(set(_calliope_evidence_terms(query)) & system_terms)
    for row in rows:
        if row.get("doc_type") == "system_learning" and not include_system_learning:
            continue
        if len(items) >= limit:
            break
        entities = row.get("entities") or []
        if isinstance(entities, dict):
            entities = list(entities.values())
        labels = []
        for entity in entities:
            label = entity.get("label") if isinstance(entity, dict) else entity
            if label and str(label) not in labels:
                labels.append(str(label))
        try:
            score = max(0.0, min(1.0, float(row.get("score") or 0)))
        except (TypeError, ValueError):
            score = 0.0
        items.append({
            "id": f"brain:{row['doc_id']}:{row['chunk_idx']}",
            "group": "knowledge",
            "kind": "document",
            "handle": {
                "kind": "document",
                "doc_id": str(row["doc_id"]),
                "chunk_idx": int(row["chunk_idx"]),
            },
            "subtype": row.get("doc_type") or "document",
            "title": row.get("title") or "Company knowledge",
            "summary": _calliope_evidence_text(row.get("chunk"), 1_800),
            "source": row.get("source") or row.get("folder") or "Document Brain",
            "score": score,
            "occurred_at": row.get("occurred_at"),
            "entities": labels[:8],
            "provenance": {
                "resolver": "brain_search",
                "doc_id": str(row["doc_id"]),
                "chunk_idx": int(row["chunk_idx"]),
                "folder": row.get("folder"),
                "doc_type": row.get("doc_type"),
            },
        })
    return items


def _calliope_compact_count(value):
    """Format catalog counts for a small semantic-result card."""
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    magnitude = abs(number)
    for threshold, divisor, suffix in (
        (1_000_000_000, 1_000_000_000, "B"),
        (1_000_000, 1_000_000, "M"),
        (10_000, 1_000, "K"),
    ):
        if magnitude >= threshold:
            scaled = number / divisor
            precision = 0 if abs(scaled) >= 100 else 1
            rendered = f"{scaled:.{precision}f}"
            if "." in rendered:
                rendered = rendered.rstrip("0").rstrip(".")
            return rendered + suffix
    return f"{number:,}"


def _calliope_semantic_text(value, limit=520):
    return _calliope_evidence_text(value, limit) or None


def _calliope_data_supplements(conn, rows):
    """Best-effort typed metadata for data-search hits.

    Search documents deliberately remain useful on older extension installs,
    but current catalogs retain the same information as structured KG/cube
    fields. Pull those fields in bounded batches so the browser does not have
    to reverse-parse one long prose document.
    """
    relation_keys = list(dict.fromkeys(
        (str(row.get("schema_name") or ""), str(row.get("rel_name") or ""))
        for row in rows
        if row.get("schema_name") and row.get("rel_name")
        and row.get("kind") in {"db_table", "cube"}
    ))
    fields = {key: [] for key in relation_keys}
    field_counts = {key: 0 for key in relation_keys}
    if relation_keys:
        try:
            column_rows = conn.execute(
                "WITH wanted(schema_name,rel_name) AS ("
                " SELECT * FROM unnest(%s::text[],%s::text[])"
                "), ranked AS ("
                " SELECT c.table_schema,c.table_name,c.column_name,c.data_type,c.is_nullable,"
                "c.ordinal_position,count(*) OVER (PARTITION BY c.table_schema,c.table_name) AS field_count,"
                "row_number() OVER (PARTITION BY c.table_schema,c.table_name ORDER BY c.ordinal_position) AS field_rank "
                "FROM information_schema.columns c JOIN wanted w "
                "ON w.schema_name=c.table_schema AND w.rel_name=c.table_name"
                ") SELECT * FROM ranked WHERE field_rank<=8 "
                "ORDER BY table_schema,table_name,ordinal_position",
                ([key[0] for key in relation_keys], [key[1] for key in relation_keys]),
            ).fetchall()
            for column in column_rows:
                key = (str(column.get("table_schema") or ""), str(column.get("table_name") or ""))
                if key not in fields:
                    continue
                try:
                    field_counts[key] = max(field_counts[key], int(column.get("field_count")))
                except (TypeError, ValueError):
                    field_counts[key] += 1
                if len(fields[key]) >= 8:
                    continue
                fields[key].append({
                    "name": _calliope_semantic_text(column.get("column_name"), 160),
                    "type": _calliope_semantic_text(column.get("data_type"), 120),
                    "nullable": str(column.get("is_nullable") or "").upper() == "YES",
                })
        except Exception:  # noqa: BLE001 — metadata is an optional presentation upgrade
            pass

    cube_names = list(dict.fromkeys(
        str(row.get("rel_name")) for row in rows
        if row.get("kind") == "cube" and row.get("rel_name")
    ))
    cubes = {}
    if cube_names:
        try:
            cube_rows = conn.execute(
                "SELECT c.name,c.version,c.grain,c.description,c.category,"
                "ctl.last_rows,ctl.refreshed_at::text AS refreshed_at "
                "FROM rvbbit.cube_catalog c LEFT JOIN rvbbit.cube_control ctl "
                "ON ctl.cube_name=c.name WHERE c.name=ANY(%s::text[])",
                (cube_names,),
            ).fetchall()
            cubes = {str(row.get("name")): row for row in cube_rows if row.get("name")}
        except Exception:  # noqa: BLE001
            pass
        try:
            enriched_rows = conn.execute(
                "SELECT cube_name,column_name,data_type,doc,semantics,source_ref FROM ("
                " SELECT cc.*,row_number() OVER (PARTITION BY cube_name ORDER BY column_name) AS rn"
                " FROM rvbbit.cube_columns cc WHERE cube_name=ANY(%s::text[])"
                ") ranked WHERE rn<=8 ORDER BY cube_name,column_name",
                (cube_names,),
            ).fetchall()
            for column in enriched_rows:
                key = ("cubes", str(column.get("cube_name") or ""))
                if key not in fields:
                    fields[key] = []
                field = next(
                    (item for item in fields[key] if item.get("name") == column.get("column_name")),
                    None,
                )
                if field is None and len(fields[key]) < 8:
                    field = {
                        "name": _calliope_semantic_text(column.get("column_name"), 160),
                        "type": _calliope_semantic_text(column.get("data_type"), 120),
                    }
                    fields[key].append(field)
                    field_counts[key] = max(field_counts.get(key, 0), len(fields[key]))
                if field is not None:
                    field["definition"] = _calliope_semantic_text(column.get("doc"), 360)
                    field["semantics"] = _calliope_semantic_text(column.get("semantics"), 360)
                    field["source_ref"] = _calliope_semantic_text(column.get("source_ref"), 240)
        except Exception:  # noqa: BLE001 — V1 cubes predate cube_columns
            pass

    metric_names = list(dict.fromkeys(
        str(row.get("rel_name") or row.get("col_name")) for row in rows
        if row.get("kind") == "metric" and (row.get("rel_name") or row.get("col_name"))
    ))
    metrics = {}
    if metric_names:
        try:
            metric_rows = conn.execute(
                "SELECT name,version,grain,description,params,labels "
                "FROM rvbbit.metric_catalog WHERE name=ANY(%s::text[])",
                (metric_names,),
            ).fetchall()
            metrics = {str(row.get("name")): row for row in metric_rows if row.get("name")}
        except Exception:  # noqa: BLE001
            pass
    return {
        "fields": fields,
        "field_counts": field_counts,
        "cubes": cubes,
        "metrics": metrics,
    }


def _calliope_data_presentation(row, supplements):
    kind = str(row.get("kind") or "data-object")
    schema = _calliope_semantic_text(row.get("schema_name"), 160)
    relation = _calliope_semantic_text(row.get("rel_name"), 160)
    column = _calliope_semantic_text(row.get("col_name"), 160)
    props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
    fields = list(supplements.get("fields", {}).get((schema or "", relation or ""), []))[:8]
    field_count = supplements.get("field_counts", {}).get((schema or "", relation or ""), 0) or len(fields)
    if kind not in {"db_table", "cube"}:
        fields = []
        field_count = 0
    facts = []

    def fact(label, value):
        text = _calliope_semantic_text(value, 120)
        if text and len(facts) < 4:
            facts.append({"label": label, "value": text})

    definition = next((
        _calliope_semantic_text(props.get(key), 520)
        for key in ("description", "business_definition", "semantic_description", "doc", "comment", "semantics")
        if _calliope_semantic_text(props.get(key), 520)
    ), None)

    if kind == "db_column":
        fact("Type", props.get("data_type"))
        if props.get("is_pk"):
            fact("Key", "Primary key")
        elif props.get("is_fk"):
            fact("Key", "Foreign key")
        fact("Distinct", _calliope_compact_count(props.get("ndv")))
        if props.get("null_frac") is not None:
            try:
                fact("Missing", f"{float(props['null_frac']) * 100:.1f}%".replace(".0%", "%"))
            except (TypeError, ValueError):
                pass
        semantic_role = props.get("semantic_role") or props.get("role")
        if semantic_role:
            fact("Role", semantic_role)
    elif kind == "db_table":
        fact("Rows", _calliope_compact_count(props.get("n_rows")))
        fact("Fields", _calliope_compact_count(props.get("n_columns") or field_count))
        relation_kind = {
            "r": "Table", "p": "Partitioned table", "v": "View", "m": "Materialized view",
            "f": "Foreign table",
        }.get(str(props.get("relkind") or ""))
        fact("Kind", relation_kind)
    elif kind == "cube":
        cube = supplements.get("cubes", {}).get(relation or "", {})
        definition = definition or _calliope_semantic_text(cube.get("description"), 520)
        fact("Grain", props.get("grain") or cube.get("grain"))
        fact("Rows", _calliope_compact_count(cube.get("last_rows")))
        fact("Fields", _calliope_compact_count(field_count) if field_count else None)
        fact("Category", cube.get("category"))
    elif kind == "metric":
        metric = supplements.get("metrics", {}).get(relation or column or "", {})
        definition = definition or _calliope_semantic_text(metric.get("description"), 520)
        fact("Grain", props.get("grain") or metric.get("grain"))
        fact("Version", metric.get("version"))
        params = metric.get("params")
        if isinstance(params, dict):
            fact("Parameters", _calliope_compact_count(len(params)))

    return {
        "identity": {
            key: value for key, value in {
                "schema": schema,
                "relation": relation,
                "column": column,
            }.items() if value
        },
        "definition": definition,
        "facts": facts,
        "field_count": field_count or None,
        "fields": [
            {key: value for key, value in field.items() if value not in (None, "")}
            for field in fields
            if field.get("name")
        ],
    }


def _calliope_data_evidence(query, limit):
    tier = {"metric": 0, "cube": 1, "db_table": 2, "db_column": 3}
    with _conn() as c:
        rows = c.execute(
            "SELECT s.node_id,s.kind,s.schema_name,s.rel_name,s.col_name,s.score,"
            "s.boosted_score,s.doc,s.usage_touches,n.properties "
            "FROM rvbbit.search_data_weighted(%s,%s,%s,%s,0.5) s "
            "LEFT JOIN rvbbit.kg_nodes n ON n.node_id=s.node_id AND n.graph_id=%s",
            (query, min(limit * 4, 100), None, GRAPH, GRAPH),
        ).fetchall()
        rows = sorted(
            rows,
            key=lambda row: (
                tier.get(row.get("kind"), 4),
                -float(row.get("boosted_score") or row.get("score") or 0),
            ),
        )
        rows = [
            row for row in rows
            if row.get("kind") in {"metric", "cube"}
            or _schema_allowed(row.get("schema_name") or "")
        ][:limit]
        supplements = _calliope_data_supplements(c, rows)
    items = []
    for row in rows:
        if len(items) >= limit:
            break
        if row.get("kind") not in {"metric", "cube"} and not _schema_allowed(row.get("schema_name") or ""):
            continue
        object_name = f"{row.get('schema_name')}.{row.get('rel_name')}"
        if row.get("col_name"):
            object_name += f".{row['col_name']}"
        try:
            score = max(0.0, min(1.0, float(row.get("boosted_score") or row.get("score") or 0)))
        except (TypeError, ValueError):
            score = 0.0
        usage = int(row.get("usage_touches") or 0)
        presentation = _calliope_data_presentation(row, supplements)
        item = {
            "id": f"data:{row.get('node_id')}",
            "group": "data",
            "kind": row.get("kind") or "data-object",
            "handle": {
                "kind": row.get("kind") or "data_object",
                "node_id": str(row.get("node_id") or ""),
                "schema": row.get("schema_name"),
                "relation": row.get("rel_name"),
                "column": row.get("col_name"),
            },
            "subtype": str(row.get("kind") or "data object").replace("db_", "").replace("_", " "),
            "title": object_name,
            "summary": _calliope_evidence_text(row.get("doc"), 1_500),
            "source": "Warehouse semantic catalog",
            "score": score,
            "provenance": {
                "resolver": "search_data_weighted",
                "node_id": str(row.get("node_id") or ""),
                "kind": row.get("kind"),
                "schema": row.get("schema_name"),
                "relation": row.get("rel_name"),
                "column": row.get("col_name"),
                "usage_touches": usage,
            },
        }
        item.update({key: value for key, value in presentation.items() if value not in (None, "", [], {})})
        items.append(item)
    return items


def _calliope_artifact_evidence(query, limit):
    with _conn() as c:
        rows = c.execute(
            "SELECT d.id,d.slug,d.name,d.description,d.owner_email,d.team,d.status,"
            "d.latest_version,d.updated_at,d.runtime_kind,d.app_kind,v.manifest,"
            "e.status AS semantic_status,e.semantic_map,e.verification,e.prompt_version,e.model,"
            "e.updated_at AS semantic_updated_at,coalesce(dep.lineage,'[]'::jsonb) AS lineage "
            "FROM rvbbit.dashboards d "
            "JOIN rvbbit.dashboard_versions v ON v.dashboard_id=d.id AND v.version=d.latest_version "
            "LEFT JOIN rvbbit.artifact_semantic_enrichments e "
            "ON e.dashboard_id=d.id AND e.version=d.latest_version "
            "LEFT JOIN LATERAL ("
            " SELECT jsonb_agg(DISTINCT jsonb_build_object('kind',x.kind,'ref',x.object_ref)) "
            " FILTER (WHERE x.object_ref IS NOT NULL) AS lineage "
            " FROM rvbbit.dashboard_deps x "
            " WHERE x.dashboard_id=d.id AND x.version=d.latest_version"
            ") dep ON true ORDER BY d.updated_at DESC LIMIT 300"
        ).fetchall()
    candidates = []
    for row in rows:
        enrichment = {
            "status": row.get("semantic_status"),
            "semantic_map": row.get("semantic_map") or {},
            "verification": row.get("verification") or {},
            "prompt_version": row.get("prompt_version"),
            "model": row.get("model"),
            "updated_at": row.get("semantic_updated_at"),
        }
        manifest = _merge_semantic_overlay(row.get("manifest") or {}, enrichment)
        semantic_map = manifest.get("semantic_map") or {}
        objects = [item for item in semantic_map.get("objects") or [] if isinstance(item, dict)]
        lineage = row.get("lineage") or []
        lineage_refs = [
            str(item.get("ref")) for item in lineage
            if isinstance(item, dict) and item.get("ref")
        ]
        artifact_body = " ".join(
            text for value in (
                row.get("description"), row.get("slug"), row.get("team"), row.get("app_kind"),
                semantic_map.get("description"), " ".join(lineage_refs), objects,
            )
            if (text := _calliope_evidence_text(value, 20_000))
        )
        score = _calliope_lexical_score(query, row.get("name"), artifact_body)
        url = f"/d/{row['slug']}" if row.get("app_kind") == "dashboard" else f"/apps/{row['slug']}"
        thumbnail_url = (
            f"/thumbs/{_artifact_kind(row.get('app_kind'))}/{row['slug']}.png"
            if (row.get("runtime_kind") or "html") == "html"
            else None
        )
        if score >= 0.16:
            candidates.append({
                "id": f"artifact:{row['slug']}:v{row['latest_version']}",
                "group": "artifacts",
                "kind": "artifact",
                "handle": {
                    "kind": "artifact",
                    "slug": row.get("slug"),
                    "version": int(row.get("latest_version") or 1),
                },
                "subtype": row.get("app_kind") or "dashboard",
                "title": row.get("name") or row.get("slug"),
                "summary": row.get("description") or semantic_map.get("description") or "Published RVBBIT artifact",
                "source": "Published artifacts",
                "url": url,
                "thumbnail_url": thumbnail_url,
                "score": score,
                "occurred_at": row.get("updated_at"),
                "entities": lineage_refs[:8],
                "provenance": {
                    "resolver": "artifact_index",
                    "slug": row.get("slug"),
                    "version": int(row.get("latest_version") or 1),
                    "app_kind": row.get("app_kind"),
                    "semantic_status": row.get("semantic_status") or "none",
                    "lineage": lineage[:16],
                },
            })
        for semantic_object in objects:
            meaning = semantic_object.get("meaning") or {}
            label = meaning.get("label") or semantic_object.get("id") or "Dashboard object"
            object_body = " ".join(
                text for value in (
                    meaning.get("description"), meaning.get("formula"), meaning.get("unit"),
                    semantic_object.get("id"), row.get("name"),
                )
                if (text := _calliope_evidence_text(value, 2_000))
            )
            object_score = _calliope_lexical_score(query, label, object_body)
            if object_score < 0.18:
                continue
            summary = meaning.get("description") or meaning.get("formula") or (
                f"A mapped business value in {row.get('name') or row.get('slug')}."
            )
            candidates.append({
                "id": f"artifact-object:{row['slug']}:v{row['latest_version']}:{semantic_object.get('id') or 'object'}",
                "group": "artifacts",
                "kind": "dashboard-object",
                "handle": {
                    "kind": "artifact_object",
                    "slug": row.get("slug"),
                    "version": int(row.get("latest_version") or 1),
                    "object_id": semantic_object.get("id"),
                    "definition_hash": semantic_object.get("definition_hash"),
                    "context": {},
                },
                "subtype": semantic_object.get("kind") or "visible value",
                "title": f"{label} · {row.get('name') or row.get('slug')}",
                "summary": summary,
                "source": f"{row.get('name') or row.get('slug')} · semantic map",
                "url": url,
                "thumbnail_url": thumbnail_url,
                "score": object_score,
                "occurred_at": row.get("updated_at"),
                "entities": lineage_refs[:8],
                "provenance": {
                    "resolver": "artifact_semantic_map",
                    "slug": row.get("slug"),
                    "version": int(row.get("latest_version") or 1),
                    "object_id": semantic_object.get("id"),
                    "meaning": meaning,
                    "replayable": bool((semantic_object.get("evaluator") or {}).get("sql")),
                },
            })
    candidates.sort(key=lambda item: (
        -float(item.get("score") or 0),
        0 if item.get("kind") == "artifact" else 1,
        str(item.get("title") or ""),
    ))
    return candidates[:limit]


def _calliope_evidence_search(query, owner, limit=24):
    """Resolve one user question across ACL'd Brain, artifacts, and the data KG."""
    started = time.time()
    limit = max(6, min(int(limit or 24), 36))
    quota = max(4, math.ceil(limit / 3))
    sources = [
        ("knowledge", "Company memory", lambda: _calliope_brain_evidence(query, owner, quota)),
        ("artifacts", "Artifacts & dashboard objects", lambda: _calliope_artifact_evidence(query, quota)),
        ("data", "Warehouse semantics", lambda: _calliope_data_evidence(query, quota)),
    ]
    groups = {}
    searched = []
    warnings = []
    for key, label, resolver in sources:
        try:
            groups[key] = resolver()
            searched.append({"key": key, "label": label, "count": len(groups[key]), "status": "ready"})
        except Exception as exc:  # noqa: BLE001 — one corpus must never blank the other two
            groups[key] = []
            searched.append({"key": key, "label": label, "count": 0, "status": "unavailable"})
            warnings.append(f"{label} is temporarily unavailable: {type(exc).__name__}: {exc}")
    items = []
    for key, _label, _resolver in sources:
        items.extend(groups[key][:quota])
    if len(items) < limit:
        included = {item.get("id") for item in items}
        remainder = [
            item for key, _label, _resolver in sources for item in groups[key]
            if item.get("id") not in included
        ]
        remainder.sort(key=lambda item: -float(item.get("score") or 0))
        items.extend(remainder[:limit - len(items)])
    return {
        "items": items[:limit],
        "searched": searched,
        "warnings": warnings,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


# ── Follow the Trail ────────────────────────────────────────────────────────
#
# The warehouse has real graphs, but a graph viewer is the wrong abstraction
# for most people.  This resolver exposes one bounded, permission-aware step at
# a time: a subject, a few plain-language facts, and ranked next hops.  Handles
# are locators, never copied evidence; every hop is rehydrated under the signed
# in user's Brain ACL and the current catalog/artifact state.
_TRAIL_KINDS = {
    "artifact", "artifact_object", "document", "brain_entity",
    "metric", "cube", "db_table", "db_column", "calendar_event",
}
_TRAIL_SECTIONS = {"meaning", "artifacts", "knowledge", "data"}


def _trail_handle(value):
    raw = value if isinstance(value, dict) else {}
    kind = str(raw.get("kind") or "").strip().lower().replace("-", "_")
    kind = {
        "dashboard_object": "artifact_object",
        "table": "db_table",
        "column": "db_column",
        "entity": "brain_entity",
    }.get(kind, kind)
    if kind not in _TRAIL_KINDS:
        raise ValueError("That kind of evidence does not have a trail yet")

    if kind in {"artifact", "artifact_object"}:
        slug = str(raw.get("slug") or "").strip()
        if not _SEMANTIC_HOME_SLUG_RE.fullmatch(slug):
            raise ValueError("artifact slug is invalid")
        handle = {"kind": kind, "slug": slug}
        version = raw.get("version")
        if version not in (None, ""):
            try:
                version = int(version)
            except (TypeError, ValueError) as exc:
                raise ValueError("version must be a positive integer") from exc
            if version < 1:
                raise ValueError("version must be a positive integer")
            handle["version"] = version
        if kind == "artifact_object":
            object_id = str(raw.get("object_id") or "").strip()
            if not _SEMANTIC_OBJECT_ID_RE.fullmatch(object_id):
                raise ValueError("semantic object id is invalid")
            handle.update({
                "object_id": object_id,
                "definition_hash": _inspection_text(raw.get("definition_hash"), 80) or None,
                "context": _semantic_json_value(raw.get("context") or {}),
            })
            if not isinstance(handle["context"], dict):
                handle["context"] = {}
        return {key: val for key, val in handle.items() if val is not None}

    if kind == "document":
        try:
            doc_id = int(raw.get("doc_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("document locator is invalid") from exc
        if doc_id < 1:
            raise ValueError("document locator is invalid")
        return {"kind": kind, "doc_id": doc_id}

    if kind == "brain_entity":
        label = _semantic_text(raw.get("label") or raw.get("name"), 240)
        if not label:
            raise ValueError("entity name is required")
        return {"kind": kind, "label": label}

    if kind == "calendar_event":
        calendar_id = _semantic_text(raw.get("calendar_id") or "primary", 120)
        event_id = _semantic_text(raw.get("event_id"), 1_024)
        if calendar_id != "primary" or not event_id:
            raise ValueError("calendar event locator is invalid")
        return {
            "kind": kind,
            "calendar_id": calendar_id,
            "event_id": event_id,
        }

    try:
        node_id = int(raw.get("node_id")) if raw.get("node_id") not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ValueError("catalog node locator is invalid") from exc
    schema = _semantic_text(raw.get("schema"), 128)
    relation = _semantic_text(
        raw.get("relation") or (raw.get("name") if kind == "metric" else None), 128
    )
    column = _semantic_text(raw.get("column"), 128)
    table = _semantic_text(raw.get("table"), 260)
    if table and not relation:
        schema, relation = _split(table)
    if kind == "cube" and not schema:
        schema = "cubes"
    if kind in {"db_table", "db_column", "cube"} and schema and not _schema_allowed(schema):
        raise ValueError("that schema is not available in the public catalog")
    if node_id is None and not relation:
        raise ValueError("catalog object locator is incomplete")
    if kind == "db_column" and node_id is None and not column:
        raise ValueError("column locator is incomplete")
    handle = {
        key: val for key, val in {
            "kind": kind,
            "node_id": node_id,
            "schema": schema,
            "relation": relation,
            "column": column,
        }.items() if val not in (None, "")
    }
    if kind == "metric":
        handle["name"] = relation
        handle["params"] = _metric_params(raw.get("params"))
    return handle


def _trail_node_handle(row):
    row = dict(row or {})
    props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
    label = str(row.get("label") or "")
    kind = str(row.get("kind") or "")
    schema = props.get("schema") or props.get("schema_name")
    relation = props.get("table") or props.get("rel_name") or props.get("relation")
    column = props.get("column") or props.get("col_name")
    if kind == "db_table" and (not schema or not relation):
        schema, relation = _split(label)
    elif kind == "db_column" and (not schema or not relation or not column):
        parts = label.split(".")
        if len(parts) >= 3:
            schema, relation, column = parts[0], parts[1], ".".join(parts[2:])
    return {
        key: value for key, value in {
            "kind": kind,
            "node_id": int(row["node_id"]) if row.get("node_id") is not None else None,
            "schema": schema,
            "relation": relation,
            "column": column,
        }.items() if value not in (None, "")
    }


def _trail_connection(relationship, label, *, kind, handle, section,
                      detail=None, url=None, thumbnail_url=None, confidence=None,
                      shared=None):
    clean_handle = _trail_handle(handle)
    payload = {
        "relationship": _semantic_text(relationship, 80) or "related to",
        "label": _semantic_text(label, 240) or "Related evidence",
        "detail": _semantic_text(detail, 520) or None,
        "kind": _semantic_text(kind, 80) or clean_handle["kind"],
        "section": section if section in _TRAIL_SECTIONS else "knowledge",
        "handle": clean_handle,
        "url": url,
        "thumbnail_url": thumbnail_url,
        "shared": [
            _semantic_text(item, 120) for item in (shared or [])[:8]
            if _semantic_text(item, 120)
        ],
    }
    try:
        payload["confidence"] = round(max(0.0, min(1.0, float(confidence))), 3)
    except (TypeError, ValueError):
        payload["confidence"] = None
    identity = json.dumps(clean_handle, sort_keys=True, separators=(",", ":"), default=str)
    payload["id"] = hashlib.sha256(
        f"{identity}|{payload['relationship']}".encode("utf-8")
    ).hexdigest()[:20]
    return {key: value for key, value in payload.items() if value not in (None, "", [])}


def _trail_append(connections, seen, connection):
    identity = json.dumps(
        connection.get("handle") or {}, sort_keys=True, separators=(",", ":"), default=str
    )
    if identity in seen:
        return
    seen.add(identity)
    connections.append(connection)


def _trail_artifact_neighbors(table_refs, exclude_slug, limit=5):
    refs = list(dict.fromkeys(str(ref) for ref in (table_refs or []) if ref))[:16]
    if not refs:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT d.slug,d.name,d.description,d.app_kind,d.runtime_kind,d.latest_version,"
            "count(DISTINCT dep.object_ref) AS shared_count,"
            "array_agg(DISTINCT dep.object_ref ORDER BY dep.object_ref) AS shared_refs "
            "FROM rvbbit.dashboard_deps dep JOIN rvbbit.dashboards d ON d.id=dep.dashboard_id "
            "WHERE dep.version=d.latest_version AND dep.object_ref=ANY(%s::text[]) "
            "AND d.slug<>%s GROUP BY d.id,d.slug,d.name,d.description,d.app_kind,"
            "d.runtime_kind,d.latest_version ORDER BY shared_count DESC,d.name LIMIT %s",
            (refs, exclude_slug or "", int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def _trail_brain_document_connection(doc, relationship="mentioned by", *, shared=None):
    doc_id = doc.get("doc_id") if isinstance(doc, dict) else None
    try:
        raw_score = float((doc or {}).get("score"))
        confidence = min(1.0, raw_score if raw_score <= 1 else raw_score / 20.0)
    except (TypeError, ValueError):
        confidence = None
    return _trail_connection(
        relationship,
        (doc or {}).get("title") or "Company document",
        kind="document",
        handle={"kind": "document", "doc_id": doc_id},
        section="knowledge",
        detail=(doc or {}).get("source") or (doc or {}).get("folder") or "Company memory",
        confidence=confidence,
        shared=shared,
    )


def _trail_artifact(handle, owner, limit):
    resolved = _semantic_home_resolve_handle(handle)
    source = resolved.get("source") or handle
    kind = resolved["kind"]
    slug = source["slug"]
    version = int(resolved.get("version") or source.get("version") or 1)
    dashboard, _row, manifest, deps = _semantic_home_artifact_row(slug, version)
    app_kind = dashboard.get("app_kind") or "dashboard"
    subject = {
        "kind": kind,
        "label": resolved.get("title") or dashboard.get("name") or slug,
        "detail": resolved.get("description") or resolved.get("formula") or dashboard.get("description") or "Published artifact",
        "handle": _trail_handle(source),
        "url": resolved.get("open_url"),
        "thumbnail_url": resolved.get("thumbnail_url"),
    }
    facts = [
        {"label": "Artifact", "value": dashboard.get("name") or slug},
        {"label": "Version", "value": str(version)},
    ]
    if kind == "artifact_object":
        if resolved.get("formula"):
            facts.append({"label": "Definition", "value": resolved["formula"]})
        if resolved.get("unit"):
            facts.append({"label": "Unit", "value": resolved["unit"]})
        for key, value in list((resolved.get("context") or {}).items())[:4]:
            facts.append({"label": str(key).replace("_", " ").title(), "value": _semantic_text(value, 160)})
    else:
        facts.append({"label": "Type", "value": str(app_kind).replace("_", " ").title()})

    connections, seen = [], set()
    tables = sorted({
        str(dep.get("object_ref")) for dep in deps
        if dep.get("kind") == "table" and dep.get("object_ref")
    })
    if kind == "artifact_object":
        _trail_append(connections, seen, _trail_connection(
            "defined in", dashboard.get("name") or slug,
            kind="artifact", handle={"kind": "artifact", "slug": slug, "version": version},
            section="artifacts", detail=f"{str(app_kind).replace('_', ' ')} · version {version}",
            url=_semantic_home_artifact_href(slug, app_kind, version),
            thumbnail_url=resolved.get("thumbnail_url"), confidence=1,
        ))
        semantic_object = _semantic_object_from_manifest(manifest, source)
        if semantic_object:
            rendered_sql, _context = _render_semantic_sql(semantic_object, source.get("context") or {})
            tables = _referenced_tables(rendered_sql) or tables
    else:
        for semantic_object in (manifest.get("semantic_map") or {}).get("objects") or []:
            if not isinstance(semantic_object, dict) or not semantic_object.get("id"):
                continue
            meaning = semantic_object.get("meaning") or {}
            _trail_append(connections, seen, _trail_connection(
                "contains", meaning.get("label") or semantic_object["id"].replace("_", " ").title(),
                kind="artifact_object",
                handle={
                    "kind": "artifact_object", "slug": slug, "version": version,
                    "object_id": semantic_object["id"],
                    "definition_hash": semantic_object.get("definition_hash"), "context": {},
                },
                section="meaning",
                detail=meaning.get("description") or meaning.get("formula") or "Named dashboard value",
                url=_semantic_home_artifact_href(slug, app_kind, version), confidence=1,
            ))
            if len(connections) >= min(6, limit):
                break

    for table in tables[:6]:
        _trail_append(connections, seen, _trail_connection(
            "recreated from" if kind == "artifact_object" else "built from",
            table, kind="db_table", handle={"kind": "db_table", "table": table},
            section="data", detail="Warehouse source", confidence=.98,
        ))
    for neighbor in _trail_artifact_neighbors(tables, slug, 4):
        target_kind = neighbor.get("app_kind") or "dashboard"
        _trail_append(connections, seen, _trail_connection(
            "also uses this evidence", neighbor.get("name") or neighbor.get("slug"),
            kind="artifact",
            handle={"kind": "artifact", "slug": neighbor["slug"], "version": neighbor["latest_version"]},
            section="artifacts",
            detail=f"Shares {neighbor.get('shared_count') or 1} warehouse source(s)",
            url=_semantic_home_artifact_href(neighbor["slug"], target_kind),
            thumbnail_url=f"/thumbs/{_artifact_kind(target_kind)}/{neighbor['slug']}.png",
            confidence=min(1, .55 + .1 * int(neighbor.get("shared_count") or 1)),
            shared=neighbor.get("shared_refs") or [],
        ))
    query = " ".join(filter(None, [subject["label"], resolved.get("formula"), *tables[:2]]))
    if query:
        try:
            for item in _calliope_brain_evidence(query, owner, 3):
                _trail_append(connections, seen, _trail_brain_document_connection(item))
        except Exception:  # noqa: BLE001 — Brain is one optional breadcrumb layer
            pass
    return subject, facts[:8], connections[:limit], ["artifact map", "warehouse lineage", "company memory"]


def _trail_metric(handle, owner, limit):
    """Resolve governed metrics from their source of truth, not the catalog KG.

    Metric definitions and observations live in ``rvbbit.metric_catalog`` and
    ``rvbbit.metric_observations``.  A metric may also be projected into
    ``kg_nodes``, but that projection is optional and must not gate its trail.
    """
    name = handle["name"]
    params = handle.get("params") or {}
    detail = _metric_detail_snapshot(
        name, params, owner=owner, days=90, bucket="raw"
    )
    snapshot = detail.get("snapshot") or {}
    trend = detail.get("trend") or {}
    subject = {
        "kind": "metric",
        "label": detail.get("title") or name.replace("_", " ").title(),
        "detail": detail.get("description") or detail.get("grain") or "Governed metric",
        "handle": _trail_handle({"kind": "metric", "name": name, "params": params}),
        "url": _semantic_home_metric_href(name, params),
    }

    def fact(label, value):
        if value in (None, ""):
            return
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, default=str)
        text = _semantic_text(value, 240)
        if text:
            facts.append({"label": label, "value": text})

    facts = []
    fact("Current", snapshot.get("value"))
    fact("Status", snapshot.get("status") or ("Observed" if snapshot else "Not observed"))
    fact("As Of", snapshot.get("data_as_of") or snapshot.get("observed_at"))
    if trend:
        percent = trend.get("percent")
        if percent is not None:
            fact("Trend", f"{str(trend.get('direction') or 'changed').title()} {abs(float(percent)):.1f}%")
        elif trend.get("absolute") is not None:
            fact("Trend", f"{str(trend.get('direction') or 'changed').title()} {abs(float(trend['absolute'])):g}")
    fact("Grain", detail.get("grain"))
    fact("Definition", f"Version {detail.get('version')}" if detail.get("version") else None)
    fact("Parameters", params if params else None)

    connections, seen = [], set()
    dependencies = {
        str(item.get("table_name")): item
        for item in detail.get("dependencies") or []
        if isinstance(item, dict) and item.get("table_name")
    }
    tables = list(dict.fromkeys(
        str(table) for table in detail.get("source_tables") or [] if table
    ))[:12]
    for table in tables:
        dependency = dependencies.get(table) or {}
        freshness = []
        if dependency.get("age"):
            freshness.append(f"freshness age {dependency['age']}")
        if dependency.get("stale"):
            freshness.append("stale")
        try:
            _trail_append(connections, seen, _trail_connection(
                "derived from", table,
                kind="db_table", handle={"kind": "db_table", "table": table},
                section="data", detail=" · ".join(freshness) or "Warehouse source",
                confidence=1,
            ))
        except ValueError:
            # Hidden/internal schemas never become public trail handles.
            continue

    for artifact in (detail.get("artifacts") or [])[:6]:
        if not isinstance(artifact, dict) or not artifact.get("slug"):
            continue
        app_kind = artifact.get("app_kind") or "dashboard"
        _trail_append(connections, seen, _trail_connection(
            "used by", artifact.get("name") or artifact["slug"],
            kind="artifact",
            handle={
                "kind": "artifact", "slug": artifact["slug"],
                "version": artifact.get("latest_version") or 1,
            },
            section="artifacts", detail=artifact.get("description") or "Published artifact",
            url=_semantic_home_artifact_href(artifact["slug"], app_kind),
            thumbnail_url=f"/thumbs/{_artifact_kind(app_kind)}/{artifact['slug']}.png",
            confidence=1,
        ))

    query = " ".join(filter(None, [
        subject["label"], detail.get("description"), *tables[:2],
    ]))
    if query:
        try:
            for item in _calliope_brain_evidence(query, owner, 3):
                _trail_append(connections, seen, _trail_brain_document_connection(item))
        except Exception:  # noqa: BLE001 — Brain is one optional breadcrumb layer
            pass
    return subject, facts[:8], connections[:limit], [
        "governed metric", "warehouse lineage", "artifact lineage", "company memory",
    ]


def _trail_calendar_event(handle, owner, limit):
    """Follow one owner-private event into ACL-visible company context."""
    calendar_id = handle["calendar_id"]
    event_id = handle["event_id"]
    with _conn() as conn:
        row = conn.execute(
            "SELECT summary,description,location,html_link,meeting_link,organizer,"
            "attendees,starts_at::text AS starts_at,ends_at::text AS ends_at,"
            "all_day,response_status,event_type,visibility,status "
            "FROM rvbbit.calliope_google_calendar_events "
            "WHERE lower(owner_email)=lower(%s) AND calendar_id=%s AND event_id=%s "
            "AND status<>'cancelled'",
            (owner, calendar_id, event_id),
        ).fetchone()
        if not row:
            raise LookupError("That private calendar event is no longer available")
        row = dict(row)
        edges = conn.execute(
            "WITH visible_docs AS MATERIALIZED ("
            " SELECT doc_id FROM rvbbit.brain_visible_docs(%s)),"
            "visible_nodes AS MATERIALIZED ("
            " SELECT DISTINCT n.node_id FROM rvbbit.kg_nodes n "
            " JOIN rvbbit.kg_edges me ON me.graph_id='brain' "
            "  AND me.object_node_id=n.node_id AND me.predicate_norm='mentions' "
            " JOIN rvbbit.kg_nodes dn ON dn.node_id=me.subject_node_id "
            "  AND dn.graph_id='brain' AND dn.kind='document' "
            " JOIN visible_docs vd ON vd.doc_id=CASE "
            "  WHEN dn.properties->>'doc_id' ~ '^[0-9]+$' "
            "  THEN (dn.properties->>'doc_id')::bigint END "
            " WHERE n.graph_id='brain') "
            "SELECT e.predicate,e.object_kind,e.label,e.properties,e.kg_node_id,"
            "e.node_kind,e.canonical_label,e.match_basis,e.match_confidence "
            "FROM rvbbit.calliope_private_calendar_edges e "
            "JOIN visible_nodes visible ON visible.node_id=e.kg_node_id "
            "WHERE lower(e.owner_email)=lower(%s) AND e.calendar_id=%s "
            "AND e.event_id=%s AND e.kg_node_id IS NOT NULL "
            "AND NOT (e.object_kind='person' AND ("
            " lower(e.object_key)=lower(%s) OR coalesce(e.properties->>'self','false')='true')) "
            "ORDER BY CASE e.predicate WHEN 'organized_by' THEN 0 "
            "WHEN 'involves' THEN 1 ELSE 2 END,e.canonical_label",
            (owner, owner, calendar_id, event_id, owner),
        ).fetchall()

    title = _semantic_text(row.get("summary"), 500) or "Untitled event"
    description = _semantic_text(row.get("description"), 1_200)
    subject = {
        "kind": "calendar_event",
        "label": title,
        "detail": description or "Owner-private Google Calendar event",
        "handle": handle,
        "url": row.get("html_link") or row.get("meeting_link"),
    }
    facts = []
    for label, value in (
        ("Starts", row.get("starts_at")),
        ("Ends", row.get("ends_at")),
        ("Location", row.get("location")),
        ("Your response", row.get("response_status")),
        ("Type", "All day" if row.get("all_day") else row.get("event_type")),
    ):
        value = _semantic_text(value, 240)
        if value:
            facts.append({"label": label, "value": value})
    attendee_count = len(row.get("attendees") or []) if isinstance(row.get("attendees"), list) else 0
    if attendee_count:
        facts.append({"label": "Attendees", "value": str(attendee_count)})

    connections, seen, canonical_labels, canonical_entities = [], set(), [], []
    relationship_labels = {
        "organized_by": "organized by",
        "involves": "meeting with",
        "at": "scheduled at",
    }
    # Keep room for a few semantically retrieved preparation documents even
    # when a large meeting has many attendees.
    entity_limit = max(1, min(8, limit - 3))
    for edge in edges:
        edge = dict(edge)
        canonical_label = _semantic_text(edge.get("canonical_label"), 240)
        if not canonical_label:
            continue
        canonical_labels.append(canonical_label)
        canonical_entities.append({
            "label": canonical_label,
            "kind": str(edge.get("object_kind") or edge.get("node_kind") or "entity"),
        })
        _trail_append(connections, seen, _trail_connection(
            relationship_labels.get(
                str(edge.get("predicate") or ""),
                str(edge.get("predicate") or "related to").replace("_", " "),
            ),
            canonical_label,
            kind=edge.get("object_kind") or edge.get("node_kind") or "entity",
            handle={"kind": "brain_entity", "label": canonical_label},
            section="meaning",
            detail=(
                f"{str(edge.get('node_kind') or edge.get('object_kind') or 'entity').replace('_', ' ').title()}"
                f" · exact Calendar {str(edge.get('match_basis') or 'identity').replace('_', ' ')} match"
            ),
            confidence=edge.get("match_confidence"),
        ))
        if len(connections) >= entity_limit:
            break

    if canonical_labels:
        facts.append({
            "label": "Company objects",
            "value": str(len(set(canonical_labels))),
        })

    # The explicit edge should immediately pay off: pull a small number of
    # ACL-filtered documents already attached to the canonical entity.  This is
    # more grounded than hoping a broad event-title search rediscovers the same
    # relationship, and it still leaves the entity hop available for deeper
    # inspection.
    direct_documents = 0
    unique_entities = list({
        (entity["label"], entity["kind"]): entity
        for entity in canonical_entities
    }.values())
    for entity in unique_entities[:3]:
        try:
            context = tool_brain_entity(entity["label"], owner)
        except Exception:  # noqa: BLE001 — one entity cannot blank the event trail
            continue
        if not isinstance(context, dict) or not context.get("found"):
            continue
        relationship = (
            "context for attendee"
            if entity["kind"] == "person"
            else "context for location"
        )
        for doc in (context.get("docs") or [])[:2]:
            if not isinstance(doc, dict) or not doc.get("doc_id"):
                continue
            before = len(connections)
            _trail_append(
                connections,
                seen,
                _trail_brain_document_connection(
                    doc, relationship, shared=[entity["label"]]
                ),
            )
            if len(connections) > before:
                direct_documents += 1
            if direct_documents >= 5 or len(connections) >= limit:
                break
        if direct_documents >= 5 or len(connections) >= limit:
            break
    query = " ".join(filter(None, [
        title,
        description[:600] if description else None,
        *list(dict.fromkeys(canonical_labels))[:6],
    ]))
    if query and title.lower() != "private event" and len(connections) < limit:
        try:
            for doc in _calliope_brain_evidence(query, owner, min(4, limit)):
                _trail_append(
                    connections,
                    seen,
                    _trail_brain_document_connection(doc, "preparation context"),
                )
                if len(connections) >= limit:
                    break
        except Exception:  # noqa: BLE001 — company memory is an optional trail layer
            pass
    return subject, facts[:8], connections[:limit], [
        "private calendar overlay", "visible company entities", "company memory",
    ]


def _trail_document(handle, owner, limit):
    doc_id = int(handle["doc_id"])
    document = tool_brain_get_doc(doc_id, owner)
    if not isinstance(document, dict) or document.get("error") or not document.get("doc_id"):
        raise LookupError("That document is not visible to this user")
    related = tool_brain_related(doc_id, owner)
    if not isinstance(related, dict) or not related.get("visible"):
        raise LookupError("That document is not visible to this user")
    subject = {
        "kind": "document",
        "label": document.get("title") or "Company document",
        "detail": document.get("source") or document.get("folder_path") or "Company memory",
        "handle": handle,
    }
    facts = []
    for label, value in (
        ("Source", document.get("source")),
        ("Author", document.get("author")),
        ("Occurred", document.get("occurred_at")),
        ("Format", document.get("mime")),
    ):
        if value:
            facts.append({"label": label, "value": _semantic_text(value, 240)})
    connections, seen = [], set()
    for entity in (related.get("entities") or [])[:8]:
        if not isinstance(entity, dict) or not entity.get("label"):
            continue
        _trail_append(connections, seen, _trail_connection(
            "mentions", entity["label"], kind=entity.get("kind") or "entity",
            handle={"kind": "brain_entity", "label": entity["label"]},
            section="meaning",
            detail=str(entity.get("kind") or "company concept").replace("_", " "),
            confidence=.9,
        ))
    for item in (related.get("related") or [])[:8]:
        if not isinstance(item, dict) or not item.get("doc_id"):
            continue
        _trail_append(connections, seen, _trail_brain_document_connection(
            item, "also discusses", shared=item.get("shared_entities") or [],
        ))
    doc_marker = f"#{doc_id}"
    title = str(document.get("title") or "")
    for relation in related.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        relation_subject = str(relation.get("subject") or "")
        if doc_marker not in relation_subject and title not in relation_subject:
            continue
        facts.append({
            "label": str(relation.get("predicate") or "related to").replace("_", " ").title(),
            "value": _semantic_text(relation.get("object"), 240),
        })
    return subject, facts[:8], connections[:limit], ["document brain", "shared entities"]


def _trail_brain_entity(handle, owner, limit):
    label = handle["label"]
    result = tool_brain_entity(label, owner)
    if not isinstance(result, dict) or not result.get("found"):
        raise LookupError("That company concept is not available to this user")
    subject = {
        "kind": "brain_entity",
        "label": result.get("entity") or label,
        "detail": str(result.get("kind") or "company concept").replace("_", " ").title(),
        "handle": handle,
    }
    facts, connections, seen = [], [], set()
    for relation in (result.get("relations") or [])[:8]:
        if not isinstance(relation, dict):
            continue
        facts.append({
            "label": str(relation.get("predicate") or "related to").replace("_", " ").title(),
            "value": _semantic_text(
                relation.get("subject") if relation.get("object") == result.get("entity")
                else relation.get("object"), 240
            ),
        })
    for doc in (result.get("docs") or [])[:limit]:
        if isinstance(doc, dict) and doc.get("doc_id"):
            _trail_append(connections, seen, _trail_brain_document_connection(doc, "appears in"))
    # A data-shaped Brain entity can continue seamlessly into the live catalog.
    if result.get("kind") in {"db_table", "db_column", "cube", "metric"}:
        raw_kind = result["kind"]
        raw = {"kind": raw_kind}
        if raw_kind == "db_column":
            parts = str(result.get("entity") or label).split(".")
            if len(parts) >= 3:
                raw.update({"schema": parts[0], "relation": parts[1], "column": ".".join(parts[2:])})
        elif raw_kind in {"db_table", "cube"}:
            raw["table"] = result.get("entity") or label
        else:
            raw["relation"] = result.get("entity") or label
        try:
            _trail_append(connections, seen, _trail_connection(
                "resolved as", result.get("entity") or label,
                kind=raw_kind, handle=raw, section="data",
                detail="Live warehouse catalog object", confidence=1,
            ))
        except ValueError:
            pass
    return subject, facts[:8], connections[:limit], ["document brain", "entity index"]


def _trail_catalog_node(handle, owner, limit):
    kind = handle["kind"]
    with _conn() as conn:
        if handle.get("node_id"):
            row = conn.execute(
                "SELECT node_id,kind,label,properties,confidence FROM rvbbit.kg_nodes "
                "WHERE graph_id=%s AND node_id=%s",
                (GRAPH, int(handle["node_id"])),
            ).fetchone()
        else:
            schema, relation, column = (
                handle.get("schema"), handle.get("relation"), handle.get("column")
            )
            label = ".".join(part for part in (schema, relation, column) if part)
            row = conn.execute(
                "SELECT node_id,kind,label,properties,confidence FROM rvbbit.kg_nodes "
                "WHERE graph_id=%s AND kind=%s AND (label=%s OR label_norm=lower(%s)) "
                "ORDER BY confidence DESC,node_id LIMIT 1",
                (GRAPH, kind, label, label),
            ).fetchone()
        if not row or row.get("kind") not in {"metric", "cube", "db_table", "db_column"}:
            raise LookupError("That catalog object is no longer available")
        row = dict(row)
        clean_handle = _trail_node_handle(row)
        if row["kind"] in {"db_table", "db_column", "cube"}:
            schema = clean_handle.get("schema") or ""
            if not _schema_allowed(schema):
                raise LookupError("That catalog object is not available")
        neighbors = conn.execute(
            "SELECT direction,predicate,node_id,kind,label,properties,confidence FROM ("
            " SELECT 'out'::text AS direction,e.predicate,n.node_id,n.kind,n.label,n.properties,e.confidence "
            " FROM rvbbit.kg_edges e JOIN rvbbit.kg_nodes n "
            " ON n.graph_id=e.graph_id AND n.node_id=e.object_node_id "
            " WHERE e.graph_id=%s AND e.subject_node_id=%s "
            " UNION ALL "
            " SELECT 'in'::text,e.predicate,n.node_id,n.kind,n.label,n.properties,e.confidence "
            " FROM rvbbit.kg_edges e JOIN rvbbit.kg_nodes n "
            " ON n.graph_id=e.graph_id AND n.node_id=e.subject_node_id "
            " WHERE e.graph_id=%s AND e.object_node_id=%s"
            ") edge ORDER BY CASE kind WHEN 'db_table' THEN 0 WHEN 'db_column' THEN 1 ELSE 2 END,"
            "confidence DESC,label LIMIT 24",
            (GRAPH, row["node_id"], GRAPH, row["node_id"]),
        ).fetchall()
    props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
    subject = {
        "kind": row["kind"],
        "label": row.get("label") or handle.get("relation") or "Catalog object",
        "detail": _semantic_text(
            props.get("comment") or props.get("description") or props.get("search_doc"), 520
        ) or str(row["kind"]).replace("db_", "").replace("_", " ").title(),
        "handle": clean_handle,
    }
    facts = []
    fact_values = (
        ("Rows", props.get("n_rows")),
        ("Fields", props.get("n_columns")),
        ("Type", props.get("data_type")),
        ("Distinct", props.get("ndv")),
        ("Grain", props.get("grain")),
    )
    for label, value in fact_values:
        if value not in (None, ""):
            facts.append({"label": label, "value": _semantic_text(value, 200)})
    connections, seen = [], set()
    predicate_labels = {
        "has_column": ("has field", "data"),
        "has_table": ("belongs to schema", "data"),
        "references": ("references", "data"),
    }
    for neighbor in neighbors:
        neighbor = dict(neighbor)
        if neighbor.get("kind") not in {"metric", "cube", "db_table", "db_column"}:
            continue
        neighbor_handle = _trail_node_handle(neighbor)
        if neighbor.get("kind") in {"db_table", "db_column", "cube"} and not _schema_allowed(neighbor_handle.get("schema") or ""):
            continue
        predicate = str(neighbor.get("predicate") or "related_to")
        relationship, section = predicate_labels.get(
            predicate, (predicate.replace("_", " "), "data")
        )
        if neighbor.get("direction") == "in" and predicate == "has_column":
            relationship = "belongs to"
        elif neighbor.get("direction") == "in" and predicate == "references":
            relationship = "referenced by"
        neighbor_props = neighbor.get("properties") if isinstance(neighbor.get("properties"), dict) else {}
        _trail_append(connections, seen, _trail_connection(
            relationship, neighbor.get("label") or "Catalog object",
            kind=neighbor.get("kind"), handle=neighbor_handle, section=section,
            detail=neighbor_props.get("comment") or neighbor_props.get("description")
            or str(neighbor.get("kind")).replace("db_", "").replace("_", " ").title(),
            confidence=neighbor.get("confidence"),
        ))
        if len(connections) >= min(8, limit):
            break
    table_refs = []
    if row["kind"] == "db_table":
        table_refs = [row.get("label")]
    elif row["kind"] == "db_column":
        table_refs = [".".join(filter(None, [clean_handle.get("schema"), clean_handle.get("relation")]))]
    elif row["kind"] == "cube":
        table_refs = [row.get("label")]
    for artifact in _trail_artifact_neighbors(table_refs, "", 4):
        artifact_kind = artifact.get("app_kind") or "dashboard"
        _trail_append(connections, seen, _trail_connection(
            "used by", artifact.get("name") or artifact.get("slug"),
            kind="artifact",
            handle={"kind": "artifact", "slug": artifact["slug"], "version": artifact["latest_version"]},
            section="artifacts", detail=artifact.get("description") or "Published artifact",
            url=_semantic_home_artifact_href(artifact["slug"], artifact_kind),
            thumbnail_url=f"/thumbs/{_artifact_kind(artifact_kind)}/{artifact['slug']}.png",
            confidence=.85,
        ))
    try:
        entity = tool_brain_entity(row.get("label"), owner)
        for doc in (entity.get("docs") or [])[:4] if isinstance(entity, dict) else []:
            if isinstance(doc, dict) and doc.get("doc_id"):
                _trail_append(connections, seen, _trail_brain_document_connection(doc, "discussed in"))
    except Exception:  # noqa: BLE001
        pass
    return subject, facts[:8], connections[:limit], ["warehouse catalog", "artifact lineage", "company memory"]


def _calliope_follow_trail(value, owner, limit=14):
    started = time.time()
    handle = _trail_handle(value)
    limit = max(4, min(int(limit or 14), 24))
    if handle["kind"] in {"artifact", "artifact_object"}:
        subject, facts, connections, searched = _trail_artifact(handle, owner, limit)
    elif handle["kind"] == "document":
        subject, facts, connections, searched = _trail_document(handle, owner, limit)
    elif handle["kind"] == "brain_entity":
        subject, facts, connections, searched = _trail_brain_entity(handle, owner, limit)
    elif handle["kind"] == "calendar_event":
        subject, facts, connections, searched = _trail_calendar_event(handle, owner, limit)
    elif handle["kind"] == "metric":
        subject, facts, connections, searched = _trail_metric(handle, owner, limit)
    else:
        subject, facts, connections, searched = _trail_catalog_node(handle, owner, limit)
    visible_connections = connections[:limit]
    section_counts = {
        section: sum(
            1 for connection in visible_connections
            if connection.get("section") == section
        )
        for section in ("meaning", "artifacts", "knowledge", "data")
    }
    return {
        "subject": subject,
        "facts": [fact for fact in facts if fact.get("label") and fact.get("value")],
        "connections": visible_connections,
        "route_summary": {
            "resolved": len(visible_connections),
            "limit": limit,
            # Resolvers deliberately bound their neighborhoods. Equal-to-limit
            # means additional edges may exist; the UI must not present the
            # returned neighborhood as the complete company graph.
            "bounded": len(visible_connections) >= limit,
            "sections": section_counts,
        },
        "searched": searched,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _calliope_evidence_query(sql, execution_subject, owner, *, origin):
    """Execute one evidence preview through the same governed SQL path as MCP."""
    args = {"sql": sql, "limit": 500, "origin": origin}
    started = time.time()
    token = _SESSION_SUB.set(execution_subject)
    try:
        result = tool_run_sql(sql, limit=500)
    except Exception as exc:  # noqa: BLE001
        result = {
            "error": {
                "code": "EXCEPTION",
                "message": f"{type(exc).__name__}: {exc}",
            }
        }
    finally:
        _SESSION_SUB.reset(token)
    _record(
        "run_sql",
        args,
        result,
        result.get("error") if isinstance(result, dict) else None,
        int((time.time() - started) * 1000),
        caller_override=owner,
    )
    return result


def _calliope_evidence_open(item, execution_subject, owner):
    """Rehydrate a persisted evidence handle for the authenticated viewer."""
    item = item if isinstance(item, dict) else {}
    kind = str(item.get("kind") or "")
    provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}

    if kind == "document" and provenance.get("resolver") == "brain_search":
        try:
            doc_id = int(provenance.get("doc_id"))
        except (TypeError, ValueError):
            return {"error": {"code": "INVALID_EVIDENCE", "message": "The document locator is invalid."}}
        document = tool_brain_get_doc(doc_id, owner)
        if not isinstance(document, dict) or document.get("error"):
            return document if isinstance(document, dict) else {
                "error": {"code": "BRAIN_UNAVAILABLE", "message": "The document could not be loaded."}
            }
        return {
            "mode": "document",
            "kind": kind,
            "title": document.get("title") or item.get("title"),
            "source": document.get("source") or item.get("source"),
            "document": {
                "body": document.get("body") or "",
                "mime": document.get("mime") or "text/plain",
                "author": document.get("author"),
                "folder": document.get("folder_path"),
                "occurred_at": document.get("occurred_at"),
                "ingested_at": document.get("ingested_at"),
                "raw_meta": document.get("raw_meta") or {},
            },
        }

    if kind in {"cube", "db_table", "db_column"} and provenance.get("resolver") == "search_data_weighted":
        schema = str(provenance.get("schema") or "").strip()
        relation = str(provenance.get("relation") or "").strip()
        column = str(provenance.get("column") or "").strip()
        if kind == "cube":
            # Cubes are ordinary materialized relations in the canonical cubes
            # schema. Do not accept a search-index schema override here.
            schema = "cubes"
        if not schema or not relation or not _schema_allowed(schema):
            return {"error": {"code": "INVALID_EVIDENCE", "message": "The data object is not previewable."}}
        if kind == "db_column":
            if not column:
                return {"error": {"code": "INVALID_EVIDENCE", "message": "The column locator is invalid."}}
            sql = pgsql.SQL("SELECT {} FROM {} LIMIT 500").format(
                pgsql.Identifier(column),
                pgsql.Identifier(schema, relation),
            ).as_string()
        else:
            sql = pgsql.SQL("SELECT * FROM {} LIMIT 500").format(
                pgsql.Identifier(schema, relation),
            ).as_string()
        query = _calliope_evidence_query(
            sql,
            execution_subject,
            owner,
            origin=f"calliope_evidence_open:{kind}",
        )
        if isinstance(query, dict) and query.get("error"):
            return query
        query = dict(query or {})
        query.update({"sql": sql, "default_view": "table"})
        return {
            "mode": "query",
            "kind": kind,
            "title": item.get("title") or f"{schema}.{relation}",
            "source": item.get("source") or "Warehouse semantic catalog",
            "query": query,
        }

    if kind == "dashboard-object" and provenance.get("resolver") == "artifact_semantic_map":
        slug = str(provenance.get("slug") or "").strip()
        object_id = str(provenance.get("object_id") or "").strip()
        try:
            version = int(provenance.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        if not slug or not object_id or version < 1:
            return {"error": {"code": "INVALID_EVIDENCE", "message": "The dashboard object locator is invalid."}}
        with _conn() as conn:
            row = conn.execute(
                "SELECT d.id,d.name,d.app_kind,v.manifest,e.status AS semantic_status,"
                "e.semantic_map,e.verification,e.prompt_version,e.model,e.updated_at AS semantic_updated_at "
                "FROM rvbbit.dashboards d "
                "JOIN rvbbit.dashboard_versions v ON v.dashboard_id=d.id AND v.version=%s "
                "LEFT JOIN rvbbit.artifact_semantic_enrichments e "
                "ON e.dashboard_id=d.id AND e.version=v.version "
                "WHERE d.slug=%s",
                (version, slug),
            ).fetchone()
        if not row:
            return {"error": {"code": "VERSION_NOT_FOUND", "message": "That artifact version is no longer available."}}
        manifest = _merge_semantic_overlay(
            row.get("manifest") or {},
            {
                "status": row.get("semantic_status"),
                "semantic_map": row.get("semantic_map") or {},
                "verification": row.get("verification") or {},
                "prompt_version": row.get("prompt_version"),
                "model": row.get("model"),
                "updated_at": row.get("semantic_updated_at"),
            },
        )
        semantic_object = _semantic_object_from_manifest(manifest, {"id": object_id})
        if not semantic_object:
            return {"error": {"code": "NOT_FOUND", "message": "That semantic object is no longer mapped."}}
        meaning = semantic_object.get("meaning") or {}
        sql, resolved_context = _render_semantic_sql(semantic_object, {})
        external_url = f"/d/{slug}/versions/{version}" if row.get("app_kind") == "dashboard" else f"/apps/{slug}/versions/{version}"
        if not sql.strip():
            return {
                "mode": "detail",
                "kind": kind,
                "title": meaning.get("label") or item.get("title"),
                "source": item.get("source"),
                "external_url": external_url,
                "detail": {"meaning": meaning, "replayable": False},
            }
        query = _calliope_evidence_query(
            sql,
            execution_subject,
            owner,
            origin="calliope_evidence_open:dashboard_object",
        )
        if isinstance(query, dict) and query.get("error"):
            return query
        object_kind = str(semantic_object.get("kind") or item.get("subtype") or "")
        query = dict(query or {})
        query.update({
            "sql": sql,
            "default_view": "chart" if object_kind in {"chart", "plot", "visualization"} else "table",
        })
        return {
            "mode": "query",
            "kind": object_kind or kind,
            "title": meaning.get("label") or item.get("title"),
            "source": f"{row.get('name') or slug} · semantic map",
            "external_url": external_url,
            "query": query,
            "detail": {"meaning": meaning, "context": resolved_context},
        }

    return {
        "error": {
            "code": "NOT_PREVIEWABLE",
            "message": "This evidence opens in its native surface instead.",
        }
    }


def _build_mcp_oauth(public: str):
    """FastMCP with our self-contained OAuth AS (auth.py). The SDK mounts /authorize,
    /token, /register + the .well-known metadata and verifies PKCE; auth.py supplies
    the storage, the /login page, and signed tokens. The static WAREHOUSE_MCP_KEY is
    still accepted as a bearer (Claude Code), so both auth paths coexist."""
    from mcp.server.fastmcp import FastMCP
    from starlette.responses import PlainTextResponse
    import auth
    fatal = auth.validate_config()
    if fatal:
        for e in fatal:
            print(f"FATAL (OAuth mode): {e}", file=sys.stderr)
        raise SystemExit(2)
    for w in auth.config_warnings():
        print(f"WARNING: {w}", file=sys.stderr)
    provider = auth.WarehouseAuthProvider(public)
    m = FastMCP("rvbbit-warehouse",
                instructions=_INSTRUCTIONS,
                auth_server_provider=provider,
                auth=auth.make_auth_settings(public))
    _register(m)
    _ensure_activity_table()
    _ensure_dashboard_tables()
    _start_semantic_enrichment_worker()
    import calliope
    calendar_grant = None
    if auth.google_enabled() and calliope.CalliopeConfig.from_env().enabled:
        calendar_grant = calliope.google_calendar_grant_handler(_conn)
    if calendar_grant:
        auth.register_login_route(
            m,
            provider,
            _RABBIT_SVG,
            google_calendar_grant=calendar_grant,
        )
    else:
        auth.register_login_route(m, provider, _RABBIT_SVG)
    import warehouse_theme
    warehouse_theme.register_theme_routes(m)
    register_dashboard_routes(m)
    if calliope.register_calliope_routes(
        m,
        _conn,
        _RABBIT_SVG,
        _dash_shim,
        cube_pivot=_calliope_cube_pivot,
        evidence_search=_calliope_evidence_search,
        evidence_open=_calliope_evidence_open,
        metric_detail=_metric_detail_snapshot,
        artifact_capture=_calliope_artifact_capture,
    ):
        print("Calliope enabled (Hermes-backed living artifact notebook)", file=sys.stderr)
        _start_calliope_watch_worker()

    @m.custom_route("/health", methods=["GET"])
    async def _health(_req):
        return PlainTextResponse("ok")

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
    host = os.environ.get("WAREHOUSE_MCP_HOST", "0.0.0.0")
    port = _env_int("WAREHOUSE_MCP_PORT", 8765, maximum=65_535)
    public = os.environ.get("WAREHOUSE_PUBLIC_URL", "").rstrip("/")
    if public:
        # OAuth mode: Claude Desktop/Cowork's native connector flow works (login at
        # <public>/login). Terminate TLS at your proxy and forward all paths to this port.
        m = _build_mcp_oauth(public)
        print(f"rvbbit-warehouse MCP (OAuth AS) → {public}/mcp  (issuer {public}, login {public}/login)",
              file=sys.stderr)
        uvicorn.run(m.streamable_http_app(), host=host, port=port, log_level="warning",
                    forwarded_allow_ips="*")   # trust X-Forwarded-* from the fronting proxy
        return
    # shared-key mode (local dev / Claude Code only — no public URL configured)
    m = _build_mcp()
    app = m.streamable_http_app()
    key = os.environ.get("WAREHOUSE_MCP_KEY", "")
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
