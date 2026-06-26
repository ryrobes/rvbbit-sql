"""Guarded DuckDB hot path over Rvbbit-owned parquet row groups.

This is intentionally a benchmark/POC runner, not the production executor.
It only attempts DuckDB for read-only SELECTs over fully compacted Rvbbit
tables whose parquet files are visible in the bench container. Anything else
falls back to normal Rvbbit/Postgres execution.
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import atexit
import json
import select
import shutil
import statistics
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import duckdb
import psycopg

from rvbbit_route_model import (
    RouteDecision,
    append_route_log,
    build_route_features,
    extract_table_refs,
    route_trace_enabled,
)

RVBBIT_DSN = "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
PGDATA_PREFIX = "/var/lib/postgresql"
BENCH_PGDATA_PREFIX = "/rvbbit_pgdata"
_LAST_STATUS = "not-run"
_LAST_ROUTE_DECISION: RouteDecision | None = None
_LAST_ROUTE_FEATURES: dict | None = None
DUCK_HOT_MODE_ENV = "RVBBIT_DUCK_HOT_MODE"
NATIVE_ROUTER_ENV = "RVBBIT_NATIVE_ROUTER"
ROUTE_OBSERVE_ENV = "RVBBIT_ROUTE_OBSERVE"
ROUTE_EXPLORE_PCT_ENV = "RVBBIT_ROUTE_EXPLORE_PCT"
DUCK_EXECUTOR_ENV = "RVBBIT_DUCK_EXECUTOR"
DUCK_RUST_BIN_ENV = "RVBBIT_DUCK_RUST_BIN"
HIVE_LAYOUT_ENV = "RVBBIT_HIVE_LAYOUT"
DUCK_RUST_PERSISTENT_ENV = "RVBBIT_DUCK_RUST_PERSISTENT"
_NATIVE_ROUTER_AVAILABLE: bool | None = None
_RUST_SESSIONS: dict[tuple[str, str, str], "RustEngineSession"] = {}
_LAST_RUST_DETAIL: dict = {}


def _close_rust_sessions() -> None:
    for session in list(_RUST_SESSIONS.values()):
        session.close()
    _RUST_SESSIONS.clear()


atexit.register(_close_rust_sessions)


class DuckHotPathFallback(Exception):
    pass


@dataclass
class RvbbitDuckTable:
    schema: str
    relname: str
    paths: list[str]
    columns: list[tuple[str, str]]
    row_group_rows: int
    row_group_bytes: int
    layout: str | None = None


@dataclass
class RustEngineSession:
    binary: str
    engine: str
    layout: str
    proc: subprocess.Popen

    @classmethod
    def start(cls, binary: str, engine: str, layout: str) -> "RustEngineSession":
        proc = subprocess.Popen(
            [
                binary,
                "--serve",
                "--engine",
                engine,
                "--layout",
                layout,
            ],
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return cls(binary=binary, engine=engine, layout=layout, proc=proc)

    def request(self, payload: dict, timeout_s: int) -> dict:
        if self.proc.poll() is not None:
            raise DuckHotPathFallback("persistent Rust executor exited")
        if not self.proc.stdin or not self.proc.stdout:
            raise DuckHotPathFallback("persistent Rust executor pipes unavailable")
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout_s + 30)
        if not ready:
            self.close()
            raise DuckHotPathFallback("persistent Rust executor timed out")
        line = self.proc.stdout.readline()
        if not line:
            self.close()
            raise DuckHotPathFallback("persistent Rust executor returned no response")
        return json.loads(line)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


SUPPORTED_PG_TYPES = {
    "boolean",
    "smallint",
    "integer",
    "bigint",
    "real",
    "double precision",
    "numeric",
    "text",
    "character",
    "character varying",
    "date",
    "timestamp without time zone",
    "timestamp with time zone",
}


def _median_ms(times: list[float]) -> float:
    return statistics.median(times) * 1000.0


def _set_status(status: str) -> None:
    global _LAST_STATUS
    _LAST_STATUS = status


def _set_route_decision(
    path: str,
    reason: str,
    source: str,
    features: dict | None = None,
    confidence: float | None = None,
    entry: dict | None = None,
) -> None:
    global _LAST_ROUTE_DECISION, _LAST_ROUTE_FEATURES
    _LAST_ROUTE_DECISION = RouteDecision(
        path=path,
        reason=reason,
        source=source,
        confidence=confidence,
        entry=entry,
    )
    _LAST_ROUTE_FEATURES = features


def _reset_route_decision() -> None:
    global _LAST_ROUTE_DECISION, _LAST_ROUTE_FEATURES
    _LAST_ROUTE_DECISION = None
    _LAST_ROUTE_FEATURES = None


def _route_status(path: str) -> str:
    if _LAST_ROUTE_DECISION and _LAST_ROUTE_DECISION.path == path:
        reason = _LAST_ROUTE_DECISION.reason
        if len(reason) > 96:
            reason = reason[:93] + "..."
        return f"{path}: {reason}"
    return path


def _write_route_log(
    sql: str,
    mode: str,
    status: str,
    elapsed_ms: float | None,
    label: str | None = None,
    suite: str | None = None,
) -> None:
    if not os.environ.get("RVBBIT_ROUTE_LOG"):
        return
    decision = _LAST_ROUTE_DECISION
    append_route_log(
        {
            "label": label,
            "suite": suite,
            "mode": mode,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "decision": {
                "path": decision.path if decision else None,
                "reason": decision.reason if decision else None,
                "source": decision.source if decision else None,
                "confidence": decision.confidence if decision else None,
            },
            "features": _LAST_ROUTE_FEATURES,
            "sql": sql if route_trace_enabled() else None,
        }
    )


def rvbbit_duck_hot_status() -> str:
    return _LAST_STATUS


def clear_rvbbit_duck_hot_detail() -> None:
    _LAST_RUST_DETAIL.clear()


def rvbbit_duck_hot_detail() -> dict:
    return dict(_LAST_RUST_DETAIL)


def _record_rust_detail(payload: dict, request_wall_ms: float, engine: str, layout: str) -> None:
    _LAST_RUST_DETAIL.clear()
    elapsed = payload.get("elapsed_ms")
    if isinstance(elapsed, (int, float)):
        _LAST_RUST_DETAIL["engine_elapsed_ms"] = float(elapsed)
    row_count = payload.get("row_count")
    if isinstance(row_count, int):
        _LAST_RUST_DETAIL["row_count"] = row_count
    rows = payload.get("rows")
    if isinstance(rows, list) and (rows or row_count == 0):
        payload_json = json.dumps(rows, sort_keys=True, separators=(",", ":"))
        _LAST_RUST_DETAIL["result_digest"] = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    _LAST_RUST_DETAIL["request_wall_ms"] = request_wall_ms
    if isinstance(elapsed, (int, float)):
        _LAST_RUST_DETAIL["sidecar_overhead_ms"] = max(0.0, request_wall_ms - float(elapsed))
    _LAST_RUST_DETAIL["engine"] = engine
    _LAST_RUST_DETAIL["layout"] = layout
    cache = payload.get("cache")
    if isinstance(cache, dict):
        for key, value in cache.items():
            _LAST_RUST_DETAIL[f"cache_{key}"] = value


def _rust_cache_status() -> str:
    if not _LAST_RUST_DETAIL:
        return ""
    catalog_hit = int(bool(_LAST_RUST_DETAIL.get("cache_catalog_cache_hit")))
    executor_hit = int(bool(_LAST_RUST_DETAIL.get("cache_executor_cache_hit")))
    safety_hit = int(bool(_LAST_RUST_DETAIL.get("cache_route_safety_cache_hit")))
    local_hit = int(bool(_LAST_RUST_DETAIL.get("cache_route_safety_local_hit")))
    files = _LAST_RUST_DETAIL.get("cache_parquet_footer_files", 0)
    misses = _LAST_RUST_DETAIL.get("cache_parquet_footer_misses", 0)
    prewarm_ms = _LAST_RUST_DETAIL.get("cache_parquet_prewarm_ms", 0.0)
    try:
        prewarm = f"{float(prewarm_ms):.1f}ms"
    except (TypeError, ValueError):
        prewarm = "?"
    return (
        f" cache(c={catalog_hit},e={executor_hit},s={safety_hit},l={local_hit},"
        f"files={files},misses={misses},prewarm={prewarm})"
    )


def _duck_hot_mode(mode: str | None = None) -> str:
    return (mode or os.environ.get(DUCK_HOT_MODE_ENV, "auto")).strip().lower()


def _duck_executor_mode() -> str:
    return os.environ.get(DUCK_EXECUTOR_ENV, "auto").strip().lower()


def _duck_rust_bin() -> str | None:
    configured = os.environ.get(DUCK_RUST_BIN_ENV)
    if configured:
        return configured
    return shutil.which("rvbbit-duck") or ("/usr/local/bin/rvbbit-duck" if os.path.exists("/usr/local/bin/rvbbit-duck") else None)


def _rust_persistent_enabled() -> bool:
    value = os.environ.get(DUCK_RUST_PERSISTENT_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def _persistent_rust_payload(sql: str, repeat: int, timeout_s: int) -> dict:
    return {
        "sql": sql,
        "repeat": repeat,
        "timeout_s": timeout_s,
        "max_rows": 0,
    }


def _run_rust_engine_persistent(
    binary: str,
    sql: str,
    repeat: int,
    timeout_s: int,
    engine: str,
    layout: str,
) -> dict:
    key = (binary, engine, layout)
    session = _RUST_SESSIONS.get(key)
    if session is None:
        session = RustEngineSession.start(binary, engine, layout)
        _RUST_SESSIONS[key] = session
    try:
        return session.request(_persistent_rust_payload(sql, repeat, timeout_s), timeout_s)
    except Exception:
        old = _RUST_SESSIONS.pop(key, None)
        if old:
            old.close()
        session = RustEngineSession.start(binary, engine, layout)
        _RUST_SESSIONS[key] = session
        return session.request(_persistent_rust_payload(sql, repeat, timeout_s), timeout_s)


def _native_router_mode() -> str:
    return os.environ.get(NATIVE_ROUTER_ENV, "auto").strip().lower()


def _native_router_enabled() -> bool:
    return _native_router_mode() not in {"0", "false", "no", "off", "disabled"}


def _route_explore_pct() -> float:
    raw = os.environ.get(ROUTE_EXPLORE_PCT_ENV, "0").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.0


def _sql_stringless(sql: str) -> str:
    out: list[str] = []
    i = 0
    in_line_comment = False
    in_block_comment = False
    in_string = False
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            else:
                out.append(" ")
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                out.extend("  ")
                i += 2
            else:
                out.append(" ")
                i += 1
            continue
        if in_string:
            if ch == "'":
                if nxt == "'":
                    out.extend("  ")
                    i += 2
                    continue
                in_string = False
            out.append(" ")
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            out.extend("  ")
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            out.extend("  ")
            i += 2
            continue
        if ch == "'":
            in_string = True
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _duck_safe_select(sql: str) -> None:
    stripped = sql.strip()
    lowered = _sql_stringless(stripped).lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise DuckHotPathFallback("not a read-only SELECT")
    if ";" in lowered.rstrip(";"):
        raise DuckHotPathFallback("multiple statements")
    blacklist = [
        "insert",
        "update",
        "delete",
        "merge",
        "copy",
        "create",
        "alter",
        "drop",
        "truncate",
        "vacuum",
        "grant",
        "revoke",
        "call",
        "do",
        "refresh",
        "listen",
        "notify",
        "rvbbit.",
        "pg_",
        "nextval",
        "setval",
        "currval",
        "set_config",
        "current_setting",
        "random",
        " means ",
        " about ",
        "::json",
        "::jsonb",
        "->",
        "$$",
    ]
    for token in blacklist:
        if re.search(rf"\b{re.escape(token)}\b", lowered) if token.isalpha() else token in lowered:
            raise DuckHotPathFallback(f"unsupported token: {token}")


def _rvbbit_row_group_catalog(pg_conn: psycopg.Connection) -> dict[str, RvbbitDuckTable]:
    sql = """
        SELECT n.nspname,
               c.relname,
               c.oid,
               array_agg(rg.path ORDER BY rg.rg_id) AS paths,
               sum(rg.n_rows)::bigint AS row_group_rows,
               sum(rg.n_bytes)::bigint AS row_group_bytes,
               pg_relation_size(c.oid)::bigint AS heap_bytes,
               coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
               coalesce(t.shadow_heap_dirty, false) AS shadow_heap_dirty,
               (SELECT count(*) FROM rvbbit.delete_log dl WHERE dl.table_oid = c.oid) AS deletes
	        FROM rvbbit.row_groups rg
	        JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
	        JOIN pg_class c ON c.oid = rg.table_oid
	        JOIN pg_namespace n ON n.oid = c.relnamespace
	        WHERE coalesce(t.acceleration_enabled, true)
        GROUP BY n.nspname, c.oid, c.relname, t.shadow_heap_retained, t.shadow_heap_dirty
    """
    catalog: dict[str, RvbbitDuckTable] = {}
    with pg_conn.cursor() as cur:
        cur.execute(sql)
        for (
            schema,
            relname,
            _oid,
            paths,
            rows,
            bytes_,
            heap_bytes,
            shadow_heap_retained,
            shadow_heap_dirty,
            deletes,
        ) in cur.fetchall():
            # Duck can only be authoritative when parquet is authoritative.
            # Deletes and dirty heap tail rows require Postgres/Rvbbit MVCC.
            clean_shadow_heap = bool(shadow_heap_retained) and not bool(shadow_heap_dirty)
            if deletes or (heap_bytes and not clean_shadow_heap):
                continue
            mapped = []
            visible = True
            for path in paths:
                if not path.startswith(PGDATA_PREFIX + "/"):
                    visible = False
                    break
                bench_path = BENCH_PGDATA_PREFIX + path[len(PGDATA_PREFIX) :]
                if not os.path.exists(bench_path):
                    visible = False
                    break
                mapped.append(bench_path)
            if mapped and visible:
                key = f"{schema}.{relname}"
                catalog[key] = RvbbitDuckTable(
                    schema=schema,
                    relname=relname,
                    paths=mapped,
                    columns=[],
                    row_group_rows=int(rows or 0),
                    row_group_bytes=int(bytes_ or 0),
                )
        cur.execute(
            """
            SELECT n.nspname, c.relname, a.attname, a.atttypid::regtype::text
	            FROM rvbbit.tables t
	            JOIN pg_class c ON c.oid = t.table_oid
	            JOIN pg_namespace n ON n.oid = c.relnamespace
	            JOIN pg_attribute a ON a.attrelid = c.oid
	            WHERE coalesce(t.acceleration_enabled, true)
	              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY n.nspname, c.relname, a.attnum
            """
        )
        unsupported: set[str] = set()
        for schema, relname, attname, typname in cur.fetchall():
            key = f"{schema}.{relname}"
            if key not in catalog:
                continue
            if typname not in SUPPORTED_PG_TYPES:
                unsupported.add(key)
                continue
            catalog[key].columns.append((attname, typname))
        for key in unsupported:
            catalog.pop(key, None)
    return catalog


def _rvbbit_variant_catalog(pg_conn: psycopg.Connection, layout: str) -> dict[str, RvbbitDuckTable]:
    layout = layout.strip()
    if not re.match(r"^[A-Za-z0-9_:-]+$", layout):
        raise DuckHotPathFallback(f"invalid rvbbit layout: {layout}")
    sql = """
        SELECT n.nspname,
               c.relname,
               rg.layout,
               array_agg(rg.path ORDER BY rg.rg_id) AS paths,
               sum(rg.n_rows)::bigint AS row_group_rows,
               sum(rg.n_bytes)::bigint AS row_group_bytes,
               pg_relation_size(c.oid)::bigint AS heap_bytes,
               coalesce(t.shadow_heap_retained, false) AS shadow_heap_retained,
               coalesce(t.shadow_heap_dirty, false) AS shadow_heap_dirty,
               (SELECT count(*) FROM rvbbit.delete_log dl WHERE dl.table_oid = c.oid) AS deletes
	        FROM rvbbit.row_group_variants rg
	        JOIN rvbbit.layout_variant_status s
	          ON s.table_oid = rg.table_oid AND s.layout = rg.layout
	        JOIN rvbbit.tables t ON t.table_oid = rg.table_oid
	        JOIN pg_class c ON c.oid = rg.table_oid
	        JOIN pg_namespace n ON n.oid = c.relnamespace
	        WHERE coalesce(t.acceleration_enabled, true)
	          AND rg.layout = %s
          AND s.status = 'ready'
        GROUP BY n.nspname, c.oid, c.relname, rg.layout, t.shadow_heap_retained, t.shadow_heap_dirty
    """
    catalog: dict[str, RvbbitDuckTable] = {}
    with pg_conn.cursor() as cur:
        cur.execute(sql, (layout,))
        for (
            schema,
            relname,
            variant_layout,
            paths,
            rows,
            bytes_,
            heap_bytes,
            shadow_heap_retained,
            shadow_heap_dirty,
            deletes,
        ) in cur.fetchall():
            clean_shadow_heap = bool(shadow_heap_retained) and not bool(shadow_heap_dirty)
            if deletes or (heap_bytes and not clean_shadow_heap):
                continue
            mapped = []
            visible = True
            for path in paths:
                if not path.startswith(PGDATA_PREFIX + "/"):
                    visible = False
                    break
                bench_path = BENCH_PGDATA_PREFIX + path[len(PGDATA_PREFIX) :]
                if not os.path.exists(bench_path):
                    visible = False
                    break
                mapped.append(bench_path)
            if mapped and visible:
                key = f"{schema}.{relname}"
                catalog[key] = RvbbitDuckTable(
                    schema=schema,
                    relname=relname,
                    paths=mapped,
                    columns=[],
                    row_group_rows=int(rows or 0),
                    row_group_bytes=int(bytes_ or 0),
                    layout=str(variant_layout or layout),
                )
        cur.execute(
            """
            SELECT n.nspname, c.relname, a.attname, a.atttypid::regtype::text
	            FROM rvbbit.tables t
	            JOIN pg_class c ON c.oid = t.table_oid
	            JOIN pg_namespace n ON n.oid = c.relnamespace
	            JOIN pg_attribute a ON a.attrelid = c.oid
	            WHERE coalesce(t.acceleration_enabled, true)
	              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY n.nspname, c.relname, a.attnum
            """
        )
        unsupported: set[str] = set()
        for schema, relname, attname, typname in cur.fetchall():
            key = f"{schema}.{relname}"
            if key not in catalog:
                continue
            if typname not in SUPPORTED_PG_TYPES:
                unsupported.add(key)
                continue
            catalog[key].columns.append((attname, typname))
        for key in unsupported:
            catalog.pop(key, None)
    return catalog


def _rvbbit_query_table_metrics(pg_conn: psycopg.Connection, sql: str) -> dict:
    refs = extract_table_refs(sql)
    if not refs:
        return {}
    metrics: dict[str, int] = {"rows": 0, "bytes": 0, "row_groups": 0}
    matched = False
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT lower(n.nspname), lower(c.relname),
                   count(rg.*)::bigint,
                   coalesce(sum(rg.n_rows), 0)::bigint,
                   coalesce(sum(rg.n_bytes), 0)::bigint
	            FROM rvbbit.tables t
	            JOIN pg_class c ON c.oid = t.table_oid
	            JOIN pg_namespace n ON n.oid = c.relnamespace
	            LEFT JOIN rvbbit.row_groups rg ON rg.table_oid = c.oid
	            WHERE coalesce(t.acceleration_enabled, true)
            GROUP BY n.nspname, c.relname
            """.encode()
        )  # type: ignore[arg-type]
        for schema, relname, row_groups, rows, bytes_ in cur.fetchall():
            if relname not in refs and f"{schema}.{relname}" not in refs:
                continue
            matched = True
            metrics["row_groups"] += int(row_groups or 0)
            metrics["rows"] += int(rows or 0)
            metrics["bytes"] += int(bytes_ or 0)
    return metrics if matched else {}


def _quote_ident(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _quote_sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote_qualified(schema: str, rel: str) -> str:
    return f"{_quote_ident(schema)}.{_quote_ident(rel)}"


def _duck_select_expr(col: str, typname: str, source_format: str = "parquet") -> str:
    ident = _quote_ident(col)
    if source_format in {"parquet", "vortex"} and typname == "date":
        return f"(DATE '1970-01-01' + CAST({ident} AS INTEGER)) AS {ident}"
    if source_format == "vortex" and typname == "timestamp without time zone":
        return f"make_timestamp(CAST({ident} AS BIGINT)) AS {ident}"
    if source_format == "vortex" and typname == "timestamp with time zone":
        return f"make_timestamptz(CAST({ident} AS BIGINT)) AS {ident}"
    return ident


def _ensure_duck_vortex(con: duckdb.DuckDBPyConnection) -> None:
    try:
        con.execute("LOAD vortex")
    except Exception:
        con.execute("INSTALL vortex")
        con.execute("LOAD vortex")


def _create_duck_views(
    con: duckdb.DuckDBPyConnection,
    _sql: str,
    catalog: dict[str, RvbbitDuckTable],
    source_format: str = "parquet",
) -> None:
    if not catalog:
        raise DuckHotPathFallback("no compacted rvbbit tables")
    source_format = source_format.strip().lower()
    if source_format not in {"parquet", "vortex"}:
        raise DuckHotPathFallback(f"unsupported DuckDB source format: {source_format}")
    if source_format == "vortex":
        _ensure_duck_vortex(con)
    relname_counts = Counter(table.relname for table in catalog.values())
    for _key, table in sorted(catalog.items()):
        paths = ", ".join(_quote_sql_string(path) for path in table.paths)
        if source_format == "vortex":
            source = f"read_vortex([{paths}])"
        else:
            source = f"read_parquet([{paths}], union_by_name=true)"
        select_list = ", ".join(
            _duck_select_expr(col, typ, source_format=source_format)
            for col, typ in table.columns
        )
        if not select_list:
            select_list = "*"
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(table.schema)}")
        con.execute(
            f"CREATE VIEW {_quote_qualified(table.schema, table.relname)} AS "
            f"SELECT {select_list} FROM {source}"
        )
        if relname_counts[table.relname] == 1:
            con.execute(
                f"CREATE VIEW {_quote_ident(table.relname)} AS "
                f"SELECT * FROM {_quote_qualified(table.schema, table.relname)}"
            )


def _run_duck_rust(sql: str, repeat: int, timeout_s: int) -> float | None:
    mode = _duck_executor_mode()
    if mode in {"python", "py", "off", "0", "false", "no"}:
        return None
    if os.environ.get("RVBBIT_DUCK_HOT_VALIDATE"):
        return None

    return _run_rust_engine(sql, repeat, timeout_s, "duck", required=mode in {"rust", "require-rust", "require"})


def _run_rust_engine(
    sql: str,
    repeat: int,
    timeout_s: int,
    engine: str,
    required: bool,
    layout: str | None = None,
) -> float | None:
    binary = _duck_rust_bin()
    if not binary:
        if required:
            raise DuckHotPathFallback(f"Rust {engine} executor requested but rvbbit-duck is not available")
        return None

    layout = layout or "scan"
    if _rust_persistent_enabled():
        try:
            t0 = time.perf_counter()
            payload = _run_rust_engine_persistent(binary, sql, repeat, timeout_s, engine, layout)
            request_wall_ms = (time.perf_counter() - t0) * 1000.0
        except (DuckHotPathFallback, OSError, json.JSONDecodeError) as exc:
            if required:
                raise DuckHotPathFallback(str(exc)[:160]) from exc
            return None
        if payload.get("status") != "ok":
            error = payload.get("error") or f"Rust {engine} executor failed"
            if required:
                raise DuckHotPathFallback(str(error)[:160])
            return None
        elapsed = payload.get("elapsed_ms")
        if not isinstance(elapsed, (int, float)):
            if required:
                raise DuckHotPathFallback(f"Rust {engine} executor returned no elapsed_ms")
            return None
        _record_rust_detail(payload, request_wall_ms, engine, layout)
        return float(elapsed)

    cmd = [
        binary,
        "--engine",
        engine,
        "--sql",
        sql,
        "--repeat",
        str(repeat),
        "--timeout-s",
        str(timeout_s),
        "--max-rows",
        "0",
    ]
    cmd.extend(["--layout", layout])
    try:
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout_s + 30)
        request_wall_ms = (time.perf_counter() - t0) * 1000.0
    except subprocess.TimeoutExpired as exc:
        if required:
            raise DuckHotPathFallback(f"Rust {engine} executor timed out") from exc
        return None
    except OSError as exc:
        if required:
            raise DuckHotPathFallback(f"Rust {engine} executor failed to start: {exc}") from exc
        return None

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        if required:
            raise DuckHotPathFallback(f"Rust {engine} executor returned invalid JSON") from exc
        return None

    if proc.returncode != 0 or payload.get("status") != "ok":
        error = payload.get("error") or (proc.stderr.strip().splitlines() or [f"Rust {engine} executor failed"])[0]
        if required:
            raise DuckHotPathFallback(str(error)[:160])
        return None

    elapsed = payload.get("elapsed_ms")
    if not isinstance(elapsed, (int, float)):
        if required:
            raise DuckHotPathFallback(f"Rust {engine} executor returned no elapsed_ms")
        return None
    _record_rust_detail(payload, request_wall_ms, engine, layout)
    return float(elapsed)


def run_rvbbit_datafusion_forced(
    sql: str,
    repeat: int = 3,
    timeout_s: int = 300,
    label: str | None = None,
    suite: str | None = None,
) -> float:
    _set_status("not-run")
    _reset_route_decision()
    _duck_safe_select(sql)
    ms = _run_rust_engine(sql, repeat, timeout_s, "datafusion", required=True)
    if ms is None:
        raise DuckHotPathFallback("Rust DataFusion executor returned no elapsed_ms")
    status = "datafusion:forced+rust" + _rust_cache_status()
    _set_status(status)
    _write_route_log(sql, "force-datafusion", status, ms, label=label, suite=suite)
    record_rvbbit_route_observation(
        sql,
        "datafusion_vector",
        ms,
        status="ok",
        source=f"benchmark:{suite or 'unknown'}:datafusion_forced",
    )
    return ms


def run_rvbbit_duck_hive_forced(
    sql: str,
    repeat: int = 3,
    timeout_s: int = 300,
    label: str | None = None,
    suite: str | None = None,
) -> float:
    _set_status("not-run")
    _reset_route_decision()
    _duck_safe_select(sql)
    layout = os.environ.get(HIVE_LAYOUT_ENV, "hive")
    ms = _run_rust_engine(sql, repeat, timeout_s, "duck", required=True, layout=layout)
    if ms is None:
        raise DuckHotPathFallback("Rust DuckDB hive executor returned no elapsed_ms")
    status = f"duck_hive:{layout}:forced+rust" + _rust_cache_status()
    _set_status(status)
    _write_route_log(sql, "force-duck-hive", status, ms, label=label, suite=suite)
    record_rvbbit_route_observation(
        sql,
        "duck_hive",
        ms,
        status="ok",
        source=f"benchmark:{suite or 'unknown'}:duck_hive_forced",
    )
    return ms


def run_rvbbit_duck_vortex_forced(
    sql: str,
    repeat: int = 3,
    timeout_s: int = 300,
    label: str | None = None,
    suite: str | None = None,
) -> float:
    _set_status("not-run")
    _reset_route_decision()
    _duck_safe_select(sql)
    if os.environ.get("RVBBIT_DUCK_VORTEX_FORCE_PYTHON", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        ms = _run_rust_engine(sql, repeat, timeout_s, "duck", required=True, layout="vortex")
        if ms is None:
            raise DuckHotPathFallback("Rust DuckDB Vortex executor returned no elapsed_ms")
        status = "duck_vortex:vortex:forced+rust" + _rust_cache_status()
        _set_status(status)
        _write_route_log(sql, "force-duck-vortex", status, ms, label=label, suite=suite)
        record_rvbbit_route_observation(
            sql,
            "duck_vortex",
            ms,
            status="ok",
            source=f"benchmark:{suite or 'unknown'}:duck_vortex_forced",
        )
        return ms

    with psycopg.connect(RVBBIT_DSN) as pg_conn:
        catalog = _rvbbit_variant_catalog(pg_conn, "vortex_scan")
    if not catalog:
        raise DuckHotPathFallback("no ready rvbbit vortex_scan accelerator files")
    repeat = max(1, repeat)

    con = duckdb.connect(":memory:")
    try:
        con.execute("PRAGMA threads=4")
        _create_duck_views(con, sql, catalog, source_format="vortex")
        con.execute("EXPLAIN " + sql).fetchall()
        times: list[float] = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            con.execute(sql).fetchall()
            times.append(time.perf_counter() - t0)
        ms = _median_ms(times)
    finally:
        con.close()

    status = "duck_vortex:vortex_scan:forced+python"
    _set_status(status)
    _write_route_log(sql, "force-duck-vortex", status, ms, label=label, suite=suite)
    record_rvbbit_route_observation(
        sql,
        "duck_vortex",
        ms,
        status="ok",
        source=f"benchmark:{suite or 'unknown'}:duck_vortex_forced_python",
    )
    return ms


def run_rvbbit_datafusion_hive_forced(
    sql: str,
    repeat: int = 3,
    timeout_s: int = 300,
    label: str | None = None,
    suite: str | None = None,
) -> float:
    _set_status("not-run")
    _reset_route_decision()
    _duck_safe_select(sql)
    layout = os.environ.get(HIVE_LAYOUT_ENV, "hive")
    ms = _run_rust_engine(sql, repeat, timeout_s, "datafusion", required=True, layout=layout)
    if ms is None:
        raise DuckHotPathFallback("Rust DataFusion hive executor returned no elapsed_ms")
    status = f"datafusion_hive:{layout}:forced+rust" + _rust_cache_status()
    _set_status(status)
    _write_route_log(sql, "force-datafusion-hive", status, ms, label=label, suite=suite)
    record_rvbbit_route_observation(
        sql,
        "datafusion_hive",
        ms,
        status="ok",
        source=f"benchmark:{suite or 'unknown'}:datafusion_hive_forced",
    )
    return ms


def _fetch_pg_rows(sql: str, timeout_s: int) -> list[tuple]:
    with psycopg.connect(RVBBIT_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {timeout_s * 1000}".encode())  # type: ignore[arg-type]
            cur.execute(sql.encode())  # type: ignore[arg-type]
            return cur.fetchall()


def _normalize_value(value):
    if isinstance(value, Decimal):
        return format(float(value), ".12g")
    if isinstance(value, float):
        return format(value, ".12g")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _rows_fingerprint(rows: list[tuple]) -> Counter:
    return Counter(tuple(_normalize_value(v) for v in row) for row in rows)


def _validate_duck_rows(sql: str, duck_rows: list[tuple], timeout_s: int) -> None:
    pg_rows = _fetch_pg_rows(sql, timeout_s)
    if len(pg_rows) != len(duck_rows):
        raise DuckHotPathFallback(
            f"duck validation row-count mismatch: duck={len(duck_rows)} pg={len(pg_rows)}"
        )
    if _rows_fingerprint(pg_rows) != _rows_fingerprint(duck_rows):
        raise DuckHotPathFallback("duck validation result mismatch")


def _native_route_reason(pg_conn: psycopg.Connection, sql: str, mode: str) -> str | None:
    if mode in {"always", "duck", "force-duck"}:
        _set_route_decision("duck", f"mode={mode}", "mode")
        return None
    if mode in {"native", "off", "force-native"}:
        _set_route_decision("native", f"mode={mode}", "mode")
        return f"mode={mode}"
    if mode != "auto":
        raise DuckHotPathFallback(f"unsupported {DUCK_HOT_MODE_ENV}: {mode}")

    native_decision = _native_router_decision(pg_conn, sql)
    if native_decision:
        _set_route_decision(
            native_decision.path,
            native_decision.reason,
            native_decision.source,
            native_decision.entry.get("features") if native_decision.entry else None,
            native_decision.confidence,
            native_decision.entry,
        )
        if native_decision.path in {"native", "pg_heap"}:
            return f"native router: {native_decision.reason}"
        if native_decision.path in {"duck", "duck_hive", "duck_vortex", "datafusion", "datafusion_hive"}:
            return None

    table_metrics = _rvbbit_query_table_metrics(pg_conn, sql)
    _set_route_decision(
        "duck",
        "native router unavailable; default duck candidate",
        "fallback",
        build_route_features(sql, table_metrics=table_metrics),
    )
    return None


def _native_router_decision(pg_conn: psycopg.Connection, sql: str) -> RouteDecision | None:
    global _NATIVE_ROUTER_AVAILABLE
    if not _native_router_enabled():
        return None
    if _NATIVE_ROUTER_AVAILABLE is False and _native_router_mode() != "require":
        return None

    try:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT rvbbit.route_explain(%s)", (sql,))
            row = cur.fetchone()
    except Exception as exc:
        pg_conn.rollback()
        _NATIVE_ROUTER_AVAILABLE = False
        if _native_router_mode() == "require":
            raise DuckHotPathFallback(f"native router unavailable: {exc}") from exc
        return None

    _NATIVE_ROUTER_AVAILABLE = True
    if not row:
        return None
    route_doc = row[0]
    if isinstance(route_doc, str):
        import json

        route_doc = json.loads(route_doc)
    if not isinstance(route_doc, dict):
        return None
    route = route_doc.get("route")
    if route in {None, "none"} or not route_doc.get("safe_select", False):
        return None
    candidate = route_doc.get("chosen_candidate")
    if candidate == "rvbbit_native":
        path = "native"
    elif candidate == "duck_vector":
        path = "duck"
    elif candidate == "duck_hive":
        path = "duck_hive"
    elif candidate == "duck_vortex":
        path = "duck_vortex"
    elif candidate == "datafusion_vector":
        path = "datafusion"
    elif candidate == "datafusion_hive":
        path = "datafusion_hive"
    elif candidate == "pg_rowstore":
        path = "pg_heap"
    else:
        path = route if route in {"duck_hive", "duck_vortex", "datafusion_hive"} else ("duck" if route == "duck" else "native")

    reason = route_doc.get("reason") or "native router decision"
    source = route_doc.get("route_source") or "native-router"
    confidence = route_doc.get("confidence")
    if not isinstance(confidence, (int, float)):
        confidence = None
    return RouteDecision(
        path=path,
        reason=str(reason),
        source=f"native-router:{source}",
        confidence=float(confidence) if confidence is not None else None,
        entry=route_doc,
    )


def _route_candidate_available(entry: dict | None, path: str) -> bool:
    if not isinstance(entry, dict):
        return False
    candidate_name = {
        "duck": "duck_vector",
        "duck_hive": "duck_hive",
        "duck_vortex": "duck_vortex",
        "datafusion": "datafusion_vector",
        "datafusion_hive": "datafusion_hive",
        "native": "rvbbit_native",
        "pg_heap": "pg_rowstore",
    }.get(path, "rvbbit_native")
    for candidate in entry.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("name") == candidate_name:
            return bool(candidate.get("available", False))
    return False


def _maybe_apply_route_exploration(mode: str, native_reason: str | None) -> str | None:
    if mode != "auto":
        return native_reason
    pct = _route_explore_pct()
    if pct <= 0.0 or random.random() >= pct:
        return native_reason
    decision = _LAST_ROUTE_DECISION
    if not decision or decision.path not in {"duck", "duck_hive", "duck_vortex", "datafusion", "datafusion_hive", "native", "pg_heap"}:
        return native_reason
    if decision.source.startswith("explore:"):
        return native_reason

    alternates = [
        path
        for path in ("native", "duck", "duck_hive", "duck_vortex", "datafusion", "datafusion_hive", "pg_heap")
        if path != decision.path and _route_candidate_available(decision.entry, path)
    ]
    if not alternates:
        return native_reason
    alternate = random.choice(alternates)

    original = decision
    entry = dict(original.entry or {})
    entry["explore_original"] = {
        "path": original.path,
        "reason": original.reason,
        "source": original.source,
        "confidence": original.confidence,
    }
    _set_route_decision(
        alternate,
        f"explore alternate to {original.path}: {original.reason}",
        f"explore:{original.source}",
        _LAST_ROUTE_FEATURES,
        None,
        entry,
    )
    if alternate in {"native", "pg_heap"}:
        return f"explore alternate to {original.path}: {original.reason}"
    return None


def _record_route_observation(sql: str, elapsed_ms: float, status: str) -> None:
    observe = os.environ.get(ROUTE_OBSERVE_ENV, "1").strip().lower()
    if observe in {"0", "false", "no", "off", "disabled"}:
        return
    decision = _LAST_ROUTE_DECISION
    if not decision or decision.path not in {"duck", "duck_hive", "duck_vortex", "datafusion", "datafusion_hive", "native", "pg_heap"}:
        return
    candidate = {
        "duck": "duck_vector",
        "duck_hive": "duck_hive",
        "duck_vortex": "duck_vortex",
        "datafusion": "datafusion_vector",
        "datafusion_hive": "datafusion_hive",
        "native": "rvbbit_native",
        "pg_heap": "pg_rowstore",
    }[decision.path]
    obs_status = (
        "ok"
        if status in {"duck", "duck_hive", "duck_vortex", "datafusion", "datafusion_hive", "native", "pg_heap"}
        or status.startswith(f"{decision.path}:")
        else status
    )
    source = decision.source
    try:
        with psycopg.connect(RVBBIT_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rvbbit.route_record_observation(%s, %s, %s, %s, %s)",
                    (sql, candidate, elapsed_ms, obs_status, source),
                )
            conn.commit()
    except Exception as exc:
        if os.environ.get("RVBBIT_DUCK_HOT_DEBUG"):
            print(f"[rvbbit_route_observe fallback] {str(exc).splitlines()[0][:120]}")


def record_rvbbit_route_observation(
    sql: str,
    candidate: str,
    elapsed_ms: float,
    status: str = "ok",
    source: str = "benchmark",
) -> None:
    observe = os.environ.get(ROUTE_OBSERVE_ENV, "1").strip().lower()
    if observe in {"0", "false", "no", "off", "disabled"}:
        return
    try:
        with psycopg.connect(RVBBIT_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rvbbit.route_record_observation(%s, %s, %s, %s, %s)",
                    (sql, candidate, elapsed_ms, status, source),
                )
            conn.commit()
    except Exception as exc:
        if os.environ.get("RVBBIT_DUCK_HOT_DEBUG"):
            print(f"[rvbbit_route_observe fallback] {str(exc).splitlines()[0][:120]}")


def _fixed_contains_like_count(sql: str, lowered_stringless: str) -> bool:
    if not re.match(r"\s*select\s+count\s*\(\s*\*\s*\)\s+from\s+", lowered_stringless):
        return False
    if any(
        token in lowered_stringless
        for token in (" group by ", " order by ", " having ", " limit ", " offset ")
    ):
        return False
    if " where " not in lowered_stringless or " not like " in lowered_stringless:
        return False

    # `_sql_stringless` intentionally blanks string literals, so inspect the
    # raw SQL for the fixed contains pattern that Rvbbit rewrites natively.
    raw = sql.lower()
    return re.search(r"\blike\s+'%([^%'_]|'')+%'", raw) is not None


def _run_pg(sql: str, repeat: int, timeout_s: int) -> float:
    times: list[float] = []
    with psycopg.connect(RVBBIT_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {timeout_s * 1000}".encode())  # type: ignore[arg-type]
            for _ in range(repeat):
                t0 = time.perf_counter()
                cur.execute(sql.encode())  # type: ignore[arg-type]
                cur.fetchall()
                times.append(time.perf_counter() - t0)
    return _median_ms(times)


def run_rvbbit_duck_hot(
    sql: str,
    repeat: int = 3,
    timeout_s: int = 300,
    mode: str | None = None,
    label: str | None = None,
    suite: str | None = None,
) -> float:
    _set_status("not-run")
    _reset_route_decision()
    mode = _duck_hot_mode(mode)
    try:
        _duck_safe_select(sql)
        with psycopg.connect(RVBBIT_DSN) as pg_conn:
            native_reason = _native_route_reason(pg_conn, sql, mode)
            native_reason = _maybe_apply_route_exploration(mode, native_reason)
            if native_reason:
                status = _route_status(
                    _LAST_ROUTE_DECISION.path if _LAST_ROUTE_DECISION else "native"
                )
                _set_status(status)
                ms = _run_pg(sql, repeat, timeout_s)
                _write_route_log(sql, mode, status, ms, label=label, suite=suite)
                _record_route_observation(sql, ms, status)
                return ms
            catalog = _rvbbit_row_group_catalog(pg_conn)
        route_path = _LAST_ROUTE_DECISION.path if _LAST_ROUTE_DECISION else "duck"
        engine = "datafusion" if route_path in {"datafusion", "datafusion_hive"} else "duck"
        if route_path in {"duck_hive", "datafusion_hive"}:
            layout = os.environ.get(HIVE_LAYOUT_ENV, "hive")
        elif route_path == "duck_vortex":
            layout = "vortex"
        else:
            layout = "scan"
        rust_ms = _run_rust_engine(sql, repeat, timeout_s, engine, required=False, layout=layout)
        if rust_ms is not None:
            status_path = route_path if route_path in {"duck_hive", "duck_vortex", "datafusion_hive"} else engine
            status = _route_status(status_path) if mode == "auto" else f"{status_path}:{mode}"
            status = f"{status}+rust" + _rust_cache_status()
            _set_status(status)
            _write_route_log(sql, mode, status, rust_ms, label=label, suite=suite)
            _record_route_observation(sql, rust_ms, status)
            return rust_ms
        if layout != "scan":
            raise DuckHotPathFallback("Rust layout executor unavailable")
        if engine == "datafusion":
            raise DuckHotPathFallback("Rust DataFusion executor unavailable")
        con = duckdb.connect(":memory:")
        try:
            con.execute("PRAGMA threads=4")
            _create_duck_views(con, sql, catalog)
            con.execute("EXPLAIN " + sql).fetchall()
            duck_status = _route_status("duck") if mode == "auto" else f"duck:{mode}"
            if os.environ.get("RVBBIT_DUCK_HOT_VALIDATE"):
                _validate_duck_rows(sql, con.execute(sql).fetchall(), timeout_s)
                _set_status(f"{duck_status}+validated")
            else:
                _set_status(duck_status)
            times: list[float] = []
            for _ in range(repeat):
                t0 = time.perf_counter()
                con.execute(sql).fetchall()
                times.append(time.perf_counter() - t0)
            ms = _median_ms(times)
            _write_route_log(sql, mode, rvbbit_duck_hot_status(), ms, label=label, suite=suite)
            _record_route_observation(sql, ms, rvbbit_duck_hot_status())
            return ms
        finally:
            con.close()
    except Exception as exc:
        reason = str(exc).splitlines()[0][:120] or exc.__class__.__name__
        _set_status(f"fallback: {reason}")
        if _LAST_ROUTE_DECISION and _LAST_ROUTE_DECISION.path == "duck":
            _set_route_decision(
                "native",
                f"fallback after duck candidate: {reason}",
                "fallback-after-duck",
                _LAST_ROUTE_FEATURES,
                _LAST_ROUTE_DECISION.confidence,
                _LAST_ROUTE_DECISION.entry,
            )
        if os.environ.get("RVBBIT_DUCK_HOT_DEBUG"):
            print(f"[rvbbit_duck_hot fallback] {reason}")
        ms = _run_pg(sql, repeat, timeout_s)
        _write_route_log(sql, mode, rvbbit_duck_hot_status(), ms, label=label, suite=suite)
        _record_route_observation(sql, ms, rvbbit_duck_hot_status())
        return ms
