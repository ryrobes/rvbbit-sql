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
  WAREHOUSE_ALLOWED_EMAILS   optional CSV email allowlist (else any email + the pw)
  WAREHOUSE_JWT_SECRET       REQUIRED in OAuth mode; token-signing secret — MUST be
                             independent of WAREHOUSE_MCP_KEY (users hold that one)
"""
from __future__ import annotations
# psycopg's dict_row factory + sql.SQL composition trip Pyright's strict overloads
# (DictRow vs TupleRow covariance); the code is correct at runtime (see --selftest).
# pyright: reportArgumentType=false, reportCallIssue=false, reportIndexIssue=false
# pyright: reportReturnType=false, reportOptionalSubscript=false, reportMissingImports=false
import hmac, json, os, re, sys, time

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
            # lean: drop the (potentially long) top-values, keep null%/distinct
            out["column_stats"] = (
                [{"attname": s["attname"], "n_distinct": s["n_distinct"], "null_pct": s["null_pct"]} for s in st]
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
    """A blessed, governed number — bitemporal (as_of = data-time, def_as_of = def-time). Pass
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
                rows = c.execute("SELECT rvbbit.metric(%s, %s::jsonb) AS m",
                                 (name, json.dumps(params))).fetchall()
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
    if tool == "metric":
        return {"result": res.get("result")}
    if tool == "validate_sql":
        return {"safe_select": res.get("safe_select"), "engine": res.get("engine")}
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
        err = {"code": "EXCEPTION", "message": str(e)}
        raise
    finally:
        _record(tool, args, res, err, int((time.time() - t0) * 1000))


# ── dashboards registry (Phase 0: publish → store → serve live, outside Claude) ──
# Claude publishes an artifact; it's stored versioned in rvbbit.dashboards and served at
# <public>/d/<slug> behind the same login. The artifact fetches live data via the injected
# `rvbbitQuery(sql)` client → /api/d/<slug>/q, which runs read-only on the MIRROR
# (safe_select-gated) and logs to mcp_activity. The dashboard outlives the chat.

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
CREATE TABLE IF NOT EXISTS rvbbit.dashboard_versions (
  dashboard_id bigint NOT NULL REFERENCES rvbbit.dashboards(id) ON DELETE CASCADE,
  version      int NOT NULL,
  html         text NOT NULL,
  kind         text DEFAULT 'live',
  created_by   text, created_at timestamptz DEFAULT now(), notes text,
  PRIMARY KEY (dashboard_id, version)
);
CREATE INDEX IF NOT EXISTS dashboards_team_idx ON rvbbit.dashboards (team, updated_at DESC);
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


def tool_publish_dashboard(name, html, team=None, description=None, kind="live"):
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
    return {"slug": slug, "version": 1, "url": _dash_url(slug), "owner": caller, "kind": kind, "deps": crawl}


def tool_update_dashboard(slug, html, notes=None):
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
    return {"slug": slug, "version": nv, "url": _dash_url(slug), "deps": crawl}


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
    with _conn() as c:
        d = c.execute("SELECT id, latest_version FROM rvbbit.dashboards WHERE slug=%s", (slug,)).fetchone()
        if not d:
            return {"error": {"code": "NOT_FOUND", "message": slug}}
        did, ver = d["id"], d["latest_version"]
        hrow = c.execute("SELECT html FROM rvbbit.dashboard_versions WHERE dashboard_id=%s AND version=%s",
                         (did, ver)).fetchone()
        html = hrow["html"] if hrow else ""
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
def _mcp_publish_dashboard(name, html, team=None, description=None, kind="live"):
    """Persist a dashboard so it lives + works OUTSIDE Cowork (a shareable URL + the lens app).
    Build `html` from the `dashboard_template` boilerplate (call that tool FIRST): it gets LIVE
    data through Cowork's callMcpTool→run_sql bridge in-app, and the host's injected rvbbitQuery
    when served — the SAME artifact works both places, no login. Compose each view into ONE
    run_sql (composePayload). NEVER bake query results into the HTML — that's a 'dead tree' with
    no live data or inspectability."""
    return _logged("publish_dashboard", {"name": name, "team": team, "kind": kind, "html_bytes": len(html or "")},
                   lambda: tool_publish_dashboard(name, html, team, description, kind))


def _mcp_update_dashboard(slug, html, notes=None):
    """Publish a new version of an existing dashboard (by slug)."""
    return _logged("update_dashboard", {"slug": slug, "html_bytes": len(html or ""), "notes": notes},
                   lambda: tool_update_dashboard(slug, html, notes))


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
            "Compose ALL of a view's data into ONE run_sql via composePayload() — each callMcpTool "
            "adds ~1.5s host overhead; the DB aggregates the whole payload in ~100ms.",
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
        res = tool_run_sql(sql, as_of)
        _record("dashboard_query", {"dashboard": slug, "sql": sql, "as_of": as_of},
                res, res.get("error"), int((time.time() - t0) * 1000), caller_override=email)
        return _json(res, 400 if res.get("error") else 200)

    return _view, _data


# ── MCP server ───────────────────────────────────────────────────────────────

def _register(mcp):
    mcp.tool(name="search_data")(lambda query, limit=8, schema=None: _logged(
        "search_data", {"query": query, "limit": limit, "schema": schema},
        lambda: tool_search_data(query, limit, schema)))
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
    mcp.tool(name="validate_sql")(lambda sql, as_of=None: _logged(
        "validate_sql", {"sql": sql, "as_of": as_of}, lambda: tool_validate_sql(sql, as_of)))
    mcp.tool(name="run_sql")(lambda sql, as_of=None, limit=None: _logged(
        "run_sql", {"sql": sql, "as_of": as_of, "limit": limit},
        lambda: tool_run_sql(sql, as_of, limit)))
    mcp.tool(name="publish_dashboard")(_mcp_publish_dashboard)
    mcp.tool(name="update_dashboard")(_mcp_update_dashboard)
    mcp.tool(name="list_dashboards")(_mcp_list_dashboards)
    mcp.tool(name="get_dashboard")(_mcp_get_dashboard)
    mcp.tool(name="dashboard_crawl")(_mcp_dashboard_crawl)
    mcp.tool(name="dashboard_dependents")(_mcp_dashboard_dependents)
    mcp.tool(name="dashboard_template")(_mcp_dashboard_template)


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
    "with validate_sql then run_sql (read-only). "
    "TO BUILD A DASHBOARD: call `dashboard_template` FIRST for the proven boilerplate, set its "
    "SERVER_ID to the <id> in your `mcp__<id>__run_sql` tool, and edit only its CONFIG + RENDER "
    "blocks. Live data flows through Cowork's callMcpTool→run_sql bridge (authed by the connector "
    "you already granted — no fetch, no login); compose each view into ONE run_sql. To persist or "
    "share a dashboard outside Cowork, also call publish_dashboard."
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
    port = int(os.environ.get("WAREHOUSE_MCP_PORT", "8765"))
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
