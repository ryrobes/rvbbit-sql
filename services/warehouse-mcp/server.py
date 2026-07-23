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
"""
from __future__ import annotations
# psycopg's dict_row factory + sql.SQL composition trip Pyright's strict overloads
# (DictRow vs TupleRow covariance); the code is correct at runtime (see --selftest).
# pyright: reportArgumentType=false, reportCallIssue=false, reportIndexIssue=false
# pyright: reportReturnType=false, reportOptionalSubscript=false, reportMissingImports=false
import asyncio, hashlib, hmac, json, os, re, secrets, shutil, socket, subprocess, sys, tempfile, threading, time
from decimal import Decimal
from pathlib import Path

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


_BURROW_ROLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_$]{0,62}$")
# Session-cookie surfaces (the app data bridge) carry identity out-of-band of
# the OAuth token context — they park the subject here around tool calls so
# an extra role arg never leaks into the MCP tool schemas.
import contextvars
_SESSION_SUB = contextvars.ContextVar("rvbbit_session_sub", default=None)


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
    with _conn() as c:
        d = c.execute("SELECT rvbbit.describe_cube(%s) AS d", (name,)).fetchone()
    return d["d"] if (d and d["d"] is not None) else {"error": {"code": "CUBE_NOT_FOUND", "message": name}}


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
    with _conn(read_only=True, role=_session_pg_role()) as c, c.cursor() as cur:
        cur.execute(_with_as_of(sql, as_of))
        cols = ([{"name": d.name, "type": _TYPE.get(d.type_code, str(d.type_code))}
                 for d in cur.description] if cur.description else [])
        rows = cur.fetchmany(limit)
        truncated = cur.fetchone() is not None
    return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated,
            "engine": v.get("engine"), "elapsed_ms": int((time.time() - t0) * 1000),
            "as_of_applied": as_of}


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
        preview_rows = max(0, min(int(preview_rows), 25))
    except (TypeError, ValueError):
        preview_rows = 3
    t0 = time.time()
    results = {str(name): tool_run_sql(sql, as_of, limit) for name, sql in queries.items()}
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
            "as_of_applied": as_of}


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


def _caller():
    """The authenticated caller (email, client_id) from the OAuth token, if any."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        t = get_access_token()
        if t is not None:
            return getattr(t, "email", None) or getattr(t, "client_id", None), getattr(t, "client_id", None)
    except Exception:  # noqa: BLE001 — no auth context (stdio / shared-key) → anonymous
        pass
    return None, None


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
  kind         text NOT NULL,        -- 'query' | 'table' | 'metric'
  object_ref   text,                 -- schema.table | metric name
  base_sql     text,                 -- the panel query (kind='query')
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
         coalesce(dep.metrics, 0)::int AS metrics
  FROM rvbbit.dashboards d
  LEFT JOIN (
    SELECT dashboard_id,
           count(*) FILTER (WHERE kind = 'query') AS queries,
           count(*) FILTER (WHERE kind = 'table') AS tables,
           count(*) FILTER (WHERE kind = 'metric') AS metrics
    FROM rvbbit.dashboard_deps
    GROUP BY dashboard_id
  ) dep ON dep.dashboard_id = d.id;
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


def _thumb_path(kind, slug):
    return _live_app_capture_root() / "thumbs" / kind / f"{slug}.png"


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
                tmp = path.with_suffix(".tmp.png")
                _capture_html_with_playwright((row or {}).get("html") or "", tmp,
                                              width=1200, height=750, full_page=False, wait_ms=1200)
                tmp.replace(path)
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
                       source_files=None, description=None):
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
    return base


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


def _with_as_of(sql: str, as_of: str | None = None) -> str:
    return f"-- rvbbit: as_of {as_of}\n{sql}" if as_of else sql


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
                           source_artifact_id=None):
    html, aerr = _resolve_source(html, source_artifact_id)
    if aerr:
        return aerr
    if not html:
        return {"error": {"code": "EMPTY_HTML",
                          "message": "pass html, or upload_artifact + source_artifact_id"}}
    caller, _ = _caller()
    base = _slugify(name)
    with _conn() as c:
        slug, n = base, 1
        while c.execute("SELECT 1 FROM rvbbit.dashboards WHERE slug=%s", (slug,)).fetchone():
            n += 1
            slug = f"{base}-{n}"
        d = c.execute(
            "INSERT INTO rvbbit.dashboards (slug,name,description,owner_email,team,status,latest_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,1) RETURNING id", (slug, name, description, caller, team, kind)).fetchone()
        c.execute("INSERT INTO rvbbit.dashboard_versions (dashboard_id,version,html,kind,created_by) "
                  "VALUES (%s,1,%s,%s,%s)", (d["id"], html, kind, caller))
    crawl = _crawl_safe(slug, use_llm=False)   # fast deterministic deps at publish
    _auto_thumb("dashboard", slug)
    return {"slug": slug, "version": 1, "url": _dash_url(slug), "hub_url": _hub_url("dashboard", slug),
            "owner": caller, "kind": kind, "deps": crawl}


def tool_update_dashboard(slug, html=None, notes=None, source_artifact_id=None):
    html, aerr = _resolve_source(html, source_artifact_id)
    if aerr:
        return aerr
    if not html:
        return {"error": {"code": "EMPTY_HTML",
                          "message": "pass html, or upload_artifact + source_artifact_id"}}
    caller, _ = _caller()
    with _conn() as c:
        d = c.execute("SELECT id, latest_version FROM rvbbit.dashboards WHERE slug=%s", (slug,)).fetchone()
        if not d:
            return {"error": {"code": "NOT_FOUND", "message": slug}}
        nv = d["latest_version"] + 1
        c.execute("INSERT INTO rvbbit.dashboard_versions (dashboard_id,version,html,created_by,notes) "
                  "VALUES (%s,%s,%s,%s,%s)", (d["id"], nv, html, caller, notes))
        c.execute("UPDATE rvbbit.dashboards SET latest_version=%s, updated_at=now() WHERE id=%s", (nv, d["id"]))
    crawl = _crawl_safe(slug, use_llm=False)
    _auto_thumb("dashboard", slug)
    return {"slug": slug, "version": nv, "url": _dash_url(slug), "hub_url": _hub_url("dashboard", slug),
            "deps": crawl}


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
        d = c.execute("SELECT id, slug, name, description, owner_email, team, status, latest_version, created_at "
                      "FROM rvbbit.dashboards WHERE slug=%s", (slug,)).fetchone()
        if not d:
            return {"error": {"code": "NOT_FOUND", "message": slug}}
        v = int(version or d["latest_version"])
        d["version"] = c.execute(
            "SELECT version, html, kind, created_by, created_at, notes "
            "FROM rvbbit.dashboard_versions WHERE dashboard_id=%s AND version=%s", (d["id"], v)).fetchone()
        d["sources"] = c.execute(
            "SELECT kind, object_ref, base_sql, source FROM rvbbit.dashboard_deps "
            "WHERE dashboard_id=%s ORDER BY kind, object_ref NULLS LAST", (d["id"],)).fetchall()
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
            "how_to_use": [
                "Build the UI in one HTML artifact and call rvbbitQuery(sql) for live read-only data.",
                "Use create_live_app(name, html, manifest=manifest) to publish and version it.",
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
        manifest_doc = _live_app_manifest(runtime_kind, app_kind, manifest, source_files, description)
    except ValueError as e:
        return {"error": {"code": "INVALID_ARGUMENT", "message": str(e)}}

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
            next_manifest.update(manifest_patch)
            next_manifest = _live_app_manifest(
                next_runtime, next_app_kind, next_manifest, next_sources, d["description"])
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
            "latest_version, manifest, last_health, last_debug_at, queries, tables, metrics, updated_at "
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
        if not include_source:
            html = version_doc.pop("html", "") or ""
            source_files = version_doc.pop("source_files", {}) or {}
            version_doc["html_bytes"] = len(html)
            version_doc["source_files"] = sorted(source_files.keys())
        app["version"] = version_doc
        app["sources"] = c.execute(
            "SELECT kind, object_ref, base_sql, source FROM rvbbit.dashboard_deps "
            "WHERE dashboard_id=%s ORDER BY kind, object_ref NULLS LAST", (app["id"],)).fetchall()
        app["recent_queries"] = c.execute(
            "SELECT ts, ok, error, rows, engine, elapsed_ms, args->>'sql' AS sql "
            f"FROM {ACTIVITY_TABLE} WHERE tool='dashboard_query' AND args->>'dashboard'=%s "
            "ORDER BY ts DESC LIMIT 20", (slug,)).fetchall()
    app["url"] = _live_app_url(slug)
    app["hub_url"] = _hub_url(app.get("app_kind"), slug)
    app["path"] = f"/apps/{slug}"
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


def _capture_html_with_playwright(html, path, width, height, full_page, wait_ms):
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
    doc = html or ""
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
        page.screenshot(path=str(path), full_page=bool(full_page))
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
    doc = html
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
        hrow = c.execute("SELECT html, source_files FROM rvbbit.dashboard_versions WHERE dashboard_id=%s AND version=%s",
                         (did, ver)).fetchone()
        html = ((hrow["html"] if hrow else "") or "") + _source_files_text((hrow or {}).get("source_files"))
        runtime = [r["sql"] for r in c.execute(
            "SELECT DISTINCT args->>'sql' AS sql FROM rvbbit.mcp_activity "
            "WHERE tool='dashboard_query' AND args->>'dashboard'=%s AND args->>'sql' IS NOT NULL", (slug,)).fetchall()]
        known_metrics = {r["name"] for r in c.execute("SELECT DISTINCT name FROM rvbbit.metric_defs").fetchall()}

    sql_src = {}                                  # sql -> where we found it (trusted)
    for q in _extract_queries(html):
        sql_src.setdefault(q, "rvbbitQuery")
    for q in runtime:
        sql_src.setdefault(q, "runtime")
    # SQL-shaped literals are candidates — only kept if EXPLAIN resolves real tables
    candidates = [q for q in _extract_sql_literals(html) if q not in sql_src]
    llm_metrics = []
    if use_llm and not sql_src and not candidates:   # only pay for the LLM when nothing else found
        lq, llm_metrics = _llm_extract(html)
        for q in lq:
            sql_src.setdefault(q, "llm")

    sql_tables = {sql: _referenced_tables(sql) for sql in sql_src}   # resolve trusted queries
    for sql in candidates:                        # promote candidates that validate
        t = _referenced_tables(sql)
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
    rows += [("table", t, None, src) for t, src in tables.items()]
    rows += [("metric", m, None, "parse") for m in metric_names]
    status = "live" if (sql_src or metric_names) else "materialized"

    with _conn() as c:
        c.execute("DELETE FROM rvbbit.dashboard_deps WHERE dashboard_id=%s", (did,))
        for kind, obj, bsql, src in rows:
            c.execute("INSERT INTO rvbbit.dashboard_deps (dashboard_id,version,kind,object_ref,base_sql,source) "
                      "VALUES (%s,%s,%s,%s,%s,%s)", (did, ver, kind, obj, bsql, src))
        c.execute("UPDATE rvbbit.dashboards SET status=%s WHERE id=%s", (status, did))
    return {"slug": slug, "status": status, "queries": len(sql_src),
            "tables": sorted(tables), "metrics": sorted(metric_names)}


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
def _mcp_upload_artifact(content, name=None, artifact_id=None, append=False):
    """Stage a large HTML/source payload server-side and get an artifact_id handle back.
    Then publish WITHOUT re-transmitting the document: pass source_artifact_id to
    publish_dashboard / update_dashboard / create_live_app / update_live_app. For very large
    payloads, send chunks: first call returns the artifact_id, subsequent calls pass it with
    append=true. Returns bytes + sha256 for integrity checking. Artifacts expire after ~48h —
    they are a staging area, not storage."""
    return _logged("upload_artifact",
                   {"name": name, "artifact_id": artifact_id, "append": append,
                    "content_bytes": len(content or "")},
                   lambda: tool_upload_artifact(content, name, artifact_id, append))


def _mcp_publish_dashboard(name, html=None, team=None, description=None, kind="live",
                           source_artifact_id=None):
    """Persist a dashboard so it lives + works OUTSIDE Cowork (a shareable URL + the lens app).
    Build `html` from the `dashboard_template` boilerplate (call that tool FIRST): it gets LIVE
    data through Cowork's callMcpTool→run_sql bridge in-app, and the host's injected rvbbitQuery
    when served — the SAME artifact works both places, no login. Instead of inlining a large
    document, you can upload_artifact once and pass source_artifact_id here. Keep each data
    concern its OWN FLAT query in the composePayload parts map — the framework batches them into
    ONE run_sql_multi round trip. NEVER hand-write a json_build_object payload query (it hides the
    SQL from the catalog and the accelerated engines), and NEVER bake query results into the
    HTML — that's a 'dead tree' with no live data or inspectability."""
    return _logged("publish_dashboard", {"name": name, "team": team, "kind": kind,
                                         "html_bytes": len(html or ""),
                                         "source_artifact_id": source_artifact_id},
                   lambda: tool_publish_dashboard(name, html, team, description, kind, source_artifact_id))


def _mcp_update_dashboard(slug, html=None, notes=None, source_artifact_id=None):
    """Publish a new version of an existing dashboard (by slug). Accepts inline html or an
    upload_artifact handle via source_artifact_id."""
    return _logged("update_dashboard", {"slug": slug, "html_bytes": len(html or ""), "notes": notes,
                                        "source_artifact_id": source_artifact_id},
                   lambda: tool_update_dashboard(slug, html, notes, source_artifact_id))


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
                         source_artifact_id=None):
    """Create a versioned RVBBIT live app. HTML apps are hosted immediately at /d/<slug> and
    call rvbbitQuery(sql) for live, read-only data from raw tables, metrics, or cubes. Accepts
    inline html or an upload_artifact handle via source_artifact_id."""
    return _logged("create_live_app", {
        "name": name,
        "runtime_kind": runtime_kind,
        "app_kind": app_kind,
        "team": team,
        "html_bytes": len(html or ""),
        "source_artifact_id": source_artifact_id,
    }, lambda: tool_create_live_app(name, html, runtime_kind, app_kind, team, description,
                                    manifest, source_files, source_artifact_id))


def _mcp_update_live_app(slug, html=None, notes=None, manifest=None, source_files=None,
                         runtime_kind=None, app_kind=None, source_artifact_id=None):
    """Publish a new version of a live app. Omitted source fields are preserved. Accepts inline
    html or an upload_artifact handle via source_artifact_id."""
    return _logged("update_live_app", {
        "slug": slug,
        "html_bytes": len(html or ""),
        "notes": notes,
        "source_artifact_id": source_artifact_id,
    }, lambda: tool_update_live_app(slug, html, notes, manifest, source_files, runtime_kind,
                                    app_kind, source_artifact_id))


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


def tool_dashboard_template():
    try:
        with open(_TEMPLATE_PATH) as f:
            html = f.read()
    except Exception as e:   # noqa: BLE001
        return {"error": str(e)}
    return {
        "template_html": html,
        "how_to_use": [
            "Set SERVER_ID to the <id> in your `mcp__<id>__run_sql` tool name.",
            "Give composePayload() one FLAT sub-SELECT per data concern — it batches them into ONE "
            "run_sql_multi round trip (each callMcpTool adds ~1.5s host overhead, so ONE call — but "
            "each query stays flat/inspectable on the wire; never hand-write a json_build_object "
            "payload query).",
            "Edit only the two `>>> EDIT` blocks (CONFIG: title + composePayload map; RENDER: KPIs / "
            "chart() / table()). Leave everything between the FRAMEWORK markers as-is.",
            "Live data is the Cowork callMcpTool→run_sql bridge (authed by the connector you already "
            "granted — no fetch, no login); it falls back to the host's rvbbitQuery when published.",
            "SQL gotchas (rvbbit read-only guard): no `::type` casts (use `cast(x as t)` or bare "
            "json_agg/row_to_json); no reserved-word bare aliases (use `ym`, not `month`).",
            "Sandbox CDN allowlist only: Chart.js 4.5.0, Grid.js 5.0.2 (+ theme css), Mermaid 11.10.0. "
            "Anything else is silently blocked.",
        ],
    }


def _mcp_dashboard_template():
    """Return the proven drop-in boilerplate for a LIVE dashboard (Cowork artifact + hosted).
    ALWAYS start a dashboard from this — it has the data bridge, single-round-trip query
    pattern (composePayload), formatters, and chart/table wrappers already solved. Adapt only
    its two `>>> EDIT` blocks. Then optionally publish_dashboard to persist/share it."""
    return _logged("dashboard_template", {}, tool_dashboard_template)


# Data clients injected into every served dashboard. We provide BOTH the hosted
# rvbbitQuery AND a cowork.callMcpTool shim (routing to the same read-only endpoint), so a
# Cowork-built artifact (callMcpTool) and a hosted-built one (rvbbitQuery) both run here
# unchanged — no codemod of the artifact needed.
_DASH_SHIM = (
    "<script>\n"
    "window.RVBBIT_DASHBOARD={slug:__SLUG__};\n"
    "window.rvbbitQuery=async function(sql,opts){opts=opts||{};"
    "const r=await fetch('/api/d/'+__SLUG__+'/q',{method:'POST',headers:{'content-type':'application/json'},"
    "body:JSON.stringify({sql:sql,as_of:opts.as_of||null})});const d=await r.json();"
    "if(!r.ok||d.error){throw new Error((d.error&&d.error.message)||('query failed '+r.status));}return d;};\n"
    "window.cowork=window.cowork||{};"
    "if(!window.cowork.callMcpTool){window.cowork.callMcpTool=async function(tool,args){"
    "const d=await window.rvbbitQuery((args&&args.sql)||'');return{structuredContent:{rows:(d&&d.rows)||[]}};};}\n"
    "</script>\n")


def _dash_shim(slug):
    return _DASH_SHIM.replace("__SLUG__", json.dumps(slug))


def register_dashboard_routes(m):
    import auth
    from urllib.parse import quote
    from starlette.responses import HTMLResponse, RedirectResponse, Response

    def _json(obj, status=200):   # default=str handles Decimal / datetime in query rows
        return Response(json.dumps(obj, default=str), media_type="application/json", status_code=status)

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
        path = _thumb_path(kind, slug)
        # Lazy self-heal: a missing or out-of-date capture enqueues itself
        # (throttled, deduped) — pre-Hub artifacts and republished versions
        # get thumbnails just by being LOOKED AT. Stale files still serve
        # (better a last-version shot than a monogram) while the refresh
        # renders in the background.
        try:
            with _conn() as c:
                d = c.execute("SELECT app_kind, runtime_kind, updated_at FROM rvbbit.dashboards "
                              "WHERE slug=%s", (slug,)).fetchone()
            if d and (d.get("runtime_kind") or "html") == "html":
                stale = (not path.is_file()
                         or path.stat().st_mtime < d["updated_at"].timestamp())
                if stale:
                    _auto_thumb(d.get("app_kind"), slug)
        except Exception as e:  # noqa: BLE001
            print(f"thumbs route ({kind}:{slug}): {e}", file=sys.stderr)
        if not path.is_file():
            return _json({"error": "no thumbnail"}, 404)
        return Response(path.read_bytes(), media_type="image/png",
                        headers={"cache-control": "public, max-age=60"})

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

    @m.custom_route("/d/{slug}", methods=["GET"])
    async def _view(request):
        if not auth.read_session(request):
            return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=302)
        slug = request.path_params["slug"]
        with _conn() as c:
            d = c.execute("SELECT id, latest_version FROM rvbbit.dashboards WHERE slug=%s", (slug,)).fetchone()
            if not d:
                return HTMLResponse("<h1>404 — no such dashboard</h1>", status_code=404)
            v = c.execute("SELECT html FROM rvbbit.dashboard_versions WHERE dashboard_id=%s AND version=%s",
                          (d["id"], d["latest_version"])).fetchone()
        return HTMLResponse(_dash_shim(slug) + (v["html"] or ""))

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
        t0 = time.time()
        # Burrow: the viewer's session identity IS a PG role — app queries run
        # under it (parked in a contextvar; tool schemas stay clean).
        tok = _SESSION_SUB.set(email)
        try:
            res = tool_run_sql(sql, as_of)
        finally:
            _SESSION_SUB.reset(tok)
        _record("dashboard_query", {"dashboard": slug, "sql": sql, "as_of": as_of},
                res, res.get("error"), int((time.time() - t0) * 1000), caller_override=email)
        return _json(res, 400 if res.get("error") else 200)

    @m.custom_route("/apps/{slug}", methods=["GET"])
    async def _view_app(request):
        proxied = await _proxy_runner(request)
        return proxied if proxied is not None else await _view(request)

    @m.custom_route("/apps/{slug}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def _proxy_app_path(request):
        proxied = await _proxy_runner(request, request.path_params.get("path") or "")
        return proxied if proxied is not None else await _view(request)

    @m.custom_route("/api/apps/{slug}/q", methods=["POST"])
    async def _data_app(request):
        return await _data(request)

    return _view, _data, _view_app, _proxy_app_path, _data_app


# ── MCP server ───────────────────────────────────────────────────────────────

def _register(mcp):
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
    mcp.tool(name="live_app_template")(_mcp_live_app_template)
    mcp.tool(name="create_live_app")(_mcp_create_live_app)
    mcp.tool(name="update_live_app")(_mcp_update_live_app)
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
    "Use list_live_apps, get_live_app, update_live_app, live_app_logs, and debug_live_app to "
    "maintain them. For Python FastAPI apps, call start_live_app to run the current version under "
    "local uvicorn, stop_live_app to stop it, live_app_status to inspect runner state, and "
    "capture_live_app to create a PNG screenshot. The legacy dashboard_template/publish_dashboard "
    "tools remain for compatibility. "
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
    return m


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
    auth.register_login_route(m, provider)
    register_dashboard_routes(m)

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
