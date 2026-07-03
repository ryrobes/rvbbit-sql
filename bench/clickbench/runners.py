"""Per-system query runners for ClickBench. Lifted from columnar_comparison."""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from typing import Callable

import psycopg
import clickhouse_connect
import duckdb

sys.path.insert(0, "/bench")
from rvbbit_duck_hot import (  # noqa: E402
    rvbbit_duck_hot_status,
    rvbbit_duck_hot_detail,
    clear_rvbbit_duck_hot_detail,
    record_rvbbit_route_observation,
    run_rvbbit_duck_hot,
)

PG_DSNS = {
    "pg_baseline": "postgresql://postgres:bench@bench-pg-baseline:5432/bench",
    "citus":       "postgresql://postgres:bench@bench-citus:5432/bench",
    "hydra":       "postgresql://postgres:bench@bench-hydra:5432/bench",
    "alloydb":     "postgresql://postgres:bench@bench-alloydb:5432/postgres",
    "rvbbit":      "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench",
    "rvbbit_native": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.duck_backend%3Doff",
    "rvbbit_native_forced": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.route_force_candidate%3Drvbbit_native%20-c%20rvbbit.native_vortex%3Doff",
    "rvbbit_native_vortex": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.route_force_candidate%3Drvbbit_native%20-c%20rvbbit.native_vortex%3Don",
    "rvbbit_duck_forced": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.route_force_candidate%3Dduck_vector",
    "rvbbit_duck_hive_forced": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.route_force_candidate%3Dduck_hive",
    "rvbbit_duck_vortex_forced": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.route_force_candidate%3Dduck_vortex",
    "rvbbit_datafusion_forced": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.route_force_candidate%3Ddatafusion_vector",
    "rvbbit_datafusion_hive_forced": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.route_force_candidate%3Ddatafusion_hive",
    "rvbbit_datafusion_mem_forced": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.route_force_candidate%3Ddatafusion_mem",
    "rvbbit_datafusion_vortex_forced": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.route_force_candidate%3Ddatafusion_vortex",
    "rvbbit_gpu_gqe_forced": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.route_force_candidate%3Dgpu_gqe%20-c%20rvbbit.route_gpu_gqe%3Don%20-c%20rvbbit.duck_backend_fail_open%3Doff",
    "rvbbit_pg_heap_forced": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.duck_backend%3Doff%20-c%20rvbbit.force_heap_scan%3Don",
    "rvbbit_pg_heap": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.duck_backend%3Doff%20-c%20rvbbit.force_heap_scan%3Don",
    "pg_heap": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.duck_backend%3Doff%20-c%20rvbbit.force_heap_scan%3Don",
}
CH_HOST = "bench-clickhouse"
CH_PORT = 8123
DUCKDB_PATH = "/data/hits_duckdb.db"
FORCED_SQL_CANDIDATES = {
    "rvbbit_duck_forced": "duck_vector",
    "rvbbit_duck_hive_forced": "duck_hive",
    "rvbbit_duck_vortex_forced": "duck_vortex",
    "rvbbit_datafusion_forced": "datafusion_vector",
    "rvbbit_datafusion_hive_forced": "datafusion_hive",
    "rvbbit_datafusion_mem_forced": "datafusion_mem",
    "rvbbit_datafusion_vortex_forced": "datafusion_vortex",
    "rvbbit_gpu_gqe_forced": "gpu_gqe",
    # Native engine, vortex columnar layout. Same router Candidate as
    # rvbbit_native (no separate candidate until Phase 4) — the route assertion
    # confirms native was chosen; the native_vortex=on GUC in the DSN makes it
    # read .vortex. run_one intercepts this name to skip route-observation logging.
    "rvbbit_native_vortex": "rvbbit_native",
}


def _median_ms(times: list[float]) -> float:
    return statistics.median(times) * 1000.0


_LAST_RUN_DETAIL: dict[str, object] = {}
ROUTE_GUCS = {
    "RVBBIT_ROUTE_DUCK_VECTOR": "rvbbit.route_duck_vector",
    "RVBBIT_ROUTE_DUCK_HIVE": "rvbbit.route_duck_hive",
    "RVBBIT_ROUTE_DUCK_VORTEX": "rvbbit.route_duck_vortex",
    "RVBBIT_ROUTE_DATAFUSION_MEM": "rvbbit.route_datafusion_mem",
    "RVBBIT_ROUTE_DATAFUSION_VECTOR": "rvbbit.route_datafusion_vector",
    "RVBBIT_ROUTE_DATAFUSION_HIVE": "rvbbit.route_datafusion_hive",
    "RVBBIT_ROUTE_DATAFUSION_VORTEX": "rvbbit.route_datafusion_vortex",
    "RVBBIT_ROUTE_DATAFUSION_VORTEX_ALLOW_TEMPORAL": "rvbbit.route_datafusion_vortex_allow_temporal",
    "RVBBIT_ROUTE_GPU_GQE": "rvbbit.route_gpu_gqe",
    "RVBBIT_ROUTE_HIVE": "rvbbit.route_hive",
    "RVBBIT_ROUTE_PG_ROWSTORE": "rvbbit.route_pg_rowstore",
    "RVBBIT_ROUTE_RVBBIT_NATIVE": "rvbbit.route_rvbbit_native",
    "RVBBIT_ROUTE_FORCE_CANDIDATE": "rvbbit.route_force_candidate",
    "RVBBIT_GQE_BIN": "rvbbit.gqe_bin",
    "RVBBIT_GQE_ALLOW_RISKY_SHAPES": "rvbbit.gqe_allow_risky_shapes",
    "RVBBIT_ROUTE_HIVE_MIN_CONFIDENCE": "rvbbit.route_hive_min_confidence",
    "RVBBIT_HOT_STORE_BUDGET_MB": "rvbbit.hot_store_budget_mb",
    "RVBBIT_HOT_STORE_ROUTE_MAX_ROWS": "rvbbit.hot_store_route_max_rows",
    # In-process DataFusion (default on as of Phase 1). Set "off" to
    # force the legacy rvbbit-duck sidecar path for A/B benchmarking
    # against the older system. The GUC is read by duck_backend's
    # dispatch hook; SETting it here propagates to every query run.
    "RVBBIT_DF_INPROCESS": "rvbbit.df_inprocess",
}
SIDECAR_ENGINE_BY_CANDIDATE = {
    "gpu_gqe": "gpu_gqe",
}


def clear_run_detail() -> None:
    _LAST_RUN_DETAIL.clear()
    clear_rvbbit_duck_hot_detail()


def record_run_error(error: str) -> None:
    _LAST_RUN_DETAIL["error"] = error


def last_run_detail() -> dict[str, object]:
    detail = dict(_LAST_RUN_DETAIL)
    detail.update(rvbbit_duck_hot_detail())
    return detail


def _record_times(times: list[float]) -> None:
    _LAST_RUN_DETAIL.clear()
    if not times:
        return
    _LAST_RUN_DETAIL["first_ms"] = times[0] * 1000.0
    _LAST_RUN_DETAIL["median_ms"] = _median_ms(times)
    if len(times) > 1:
        _LAST_RUN_DETAIL["warm_median_ms"] = _median_ms(times[1:])


def _apply_route_gucs(cur) -> None:
    for env_name, guc_name in ROUTE_GUCS.items():
        value = os.environ.get(env_name)
        if value is None:
            continue
        safe_value = value.replace("'", "''")
        cur.execute(f"SET {guc_name} = '{safe_value}'".encode())  # type: ignore[arg-type]


def _sidecar_telemetry_enabled() -> bool:
    return os.environ.get("BENCH_CAPTURE_SIDECAR_TELEMETRY", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _sidecar_event_floor(cur, engine: str) -> int | None:
    if not _sidecar_telemetry_enabled():
        return None
    try:
        cur.execute(
            "SELECT coalesce(max(id), 0) FROM rvbbit.duck_sidecar_query_events WHERE engine = %s",
            (engine,),
        )
        value = cur.fetchone()
        return int(value[0] or 0) if value else 0
    except Exception:
        try:
            cur.connection.rollback()
            cur.execute("SET statement_timeout = 300000")
            _apply_route_gucs(cur)
        except Exception:
            pass
        return None


def _median_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _capture_sidecar_telemetry(cur, engine: str, floor_id: int) -> None:
    wait_s = float(os.environ.get("BENCH_SIDECAR_TELEMETRY_WAIT_S", "1.0"))
    deadline = time.perf_counter() + max(0.0, wait_s)
    rows = []
    while True:
        try:
            cur.execute(
                """
                SELECT id, elapsed_ms, execute_ms, row_count, result_format, arrow_ipc_bytes,
                       repeat_count, cache::text
                FROM rvbbit.duck_sidecar_query_events
                WHERE engine = %s AND id > %s
                ORDER BY id
                """,
                (engine, floor_id),
            )
            rows = cur.fetchall()
        except Exception:
            try:
                cur.connection.rollback()
            except Exception:
                pass
            return
        if rows or time.perf_counter() >= deadline:
            break
        time.sleep(0.05)
    if not rows:
        return

    elapsed_ms = [float(row[1]) for row in rows if row[1] is not None]
    execute_ms = [float(row[2]) for row in rows if row[2] is not None]
    _LAST_RUN_DETAIL["sidecar_engine"] = engine
    _LAST_RUN_DETAIL["sidecar_event_count"] = len(rows)
    median_elapsed = _median_or_none(elapsed_ms)
    median_execute = _median_or_none(execute_ms)
    if median_elapsed is not None:
        _LAST_RUN_DETAIL["sidecar_elapsed_ms"] = median_elapsed
    if median_execute is not None:
        _LAST_RUN_DETAIL["sidecar_execute_ms"] = median_execute

    gqe_samples: dict[str, list[float]] = {}
    last_gqe: dict[str, object] | None = None
    for row in rows:
        try:
            cache = json.loads(row[7] or "{}")
        except Exception:
            continue
        gqe = cache.get("gqe")
        if not isinstance(gqe, dict):
            continue
        last_gqe = gqe
        for key, value in gqe.items():
            if isinstance(value, (int, float)):
                gqe_samples.setdefault(key, []).append(float(value))
    if not gqe_samples:
        return
    gqe_summary: dict[str, object] = {}
    for key, values in sorted(gqe_samples.items()):
        if key.startswith("median_"):
            gqe_summary[key] = float(statistics.median(values))
        elif last_gqe is not None and key in last_gqe:
            gqe_summary[key] = last_gqe[key]
    _LAST_RUN_DETAIL["gqe"] = gqe_summary


def _route_explain(cur, sql: str):
    try:
        cur.execute("SELECT rvbbit.route_explain(%s)::text", (sql,))
        value = cur.fetchone()
        if value and value[0]:
            return json.loads(value[0])
    except Exception as e:
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return {"error": str(e).splitlines()[0][:240]}
    return None


def run_pg(
    dsn: str,
    sql: str,
    repeat: int = 3,
    timeout_s: int = 300,
    capture_route: bool = False,
    expect_candidate: str | None = None,
    expect_layout: str | None = None,
) -> float:
    times: list[float] = []
    route_doc = None
    sidecar_engine = SIDECAR_ENGINE_BY_CANDIDATE.get(expect_candidate or "")
    sidecar_floor_id: int | None = None
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {timeout_s * 1000}".encode())  # type: ignore[arg-type]
            _apply_route_gucs(cur)
            if capture_route or expect_candidate:
                route_doc = _route_explain(cur, sql)
                if route_doc is not None:
                    _LAST_RUN_DETAIL["route"] = route_doc
                if isinstance(route_doc, dict) and route_doc.get("error"):
                    cur.execute(f"SET statement_timeout = {timeout_s * 1000}".encode())  # type: ignore[arg-type]
                    _apply_route_gucs(cur)
                if expect_candidate:
                    if not isinstance(route_doc, dict):
                        raise RuntimeError("route_explain returned no route document")
                    chosen = route_doc.get("chosen_candidate")
                    if chosen != expect_candidate:
                        reason = str(route_doc.get("reason") or "no reason")
                        raise RuntimeError(
                            f"route {chosen or 'none'} != {expect_candidate}: {reason}"
                        )
            if expect_layout is not None:
                # Integrity guard: prove the scan actually used `expect_layout`
                # (e.g. vortex_scan), not a silent fallback to parquet. EXPLAIN runs
                # on this already-forced connection, before timing. Soft-skips plans
                # with no rvbbit custom scan (metadata count(*) short-circuits).
                try:
                    cur.execute(("EXPLAIN (COSTS off) " + sql).encode())  # type: ignore[arg-type]
                    layout_lines = [r[0] for r in cur.fetchall() if "Rvbbit Layout:" in r[0]]
                except Exception:
                    cur.connection.rollback()
                    cur.execute(f"SET statement_timeout = {timeout_s * 1000}".encode())  # type: ignore[arg-type]
                    _apply_route_gucs(cur)
                    layout_lines = []
                if layout_lines and all(expect_layout not in line for line in layout_lines):
                    raise RuntimeError(
                        f"expected layout {expect_layout!r} not used: {layout_lines[0].strip()!r} "
                        "(vortex variant missing? load via run_offline.sh so .vortex is built)"
                    )
            if sidecar_engine:
                sidecar_floor_id = _sidecar_event_floor(cur, sidecar_engine)
            for _ in range(repeat):
                t0 = time.perf_counter()
                cur.execute(sql.encode())  # type: ignore[arg-type]
                cur.fetchall()
                times.append(time.perf_counter() - t0)
            _record_times(times)
            if sidecar_engine and sidecar_floor_id is not None:
                _capture_sidecar_telemetry(cur, sidecar_engine, sidecar_floor_id)
    if route_doc is not None:
        _LAST_RUN_DETAIL["route"] = route_doc
    return _median_ms(times)


def run_clickhouse(sql: str, repeat: int = 3) -> float:
    client = clickhouse_connect.get_client(host=CH_HOST, port=CH_PORT)
    times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        client.query(sql).result_rows
        times.append(time.perf_counter() - t0)
    _record_times(times)
    return _median_ms(times)


def run_duckdb(sql: str, repeat: int = 3) -> float:
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        con.execute(sql).fetchall()
        times.append(time.perf_counter() - t0)
    con.close()
    _record_times(times)
    return _median_ms(times)


def runner_for(system: str) -> Callable[..., float]:
    if system == "rvbbit":
        return lambda sql, repeat=3: run_pg(PG_DSNS["rvbbit"], sql, repeat)
    if system == "rvbbit_native":
        return lambda sql, repeat=3: run_pg(PG_DSNS["rvbbit_native"], sql, repeat)
    if system == "rvbbit_native_forced":
        return lambda sql, repeat=3: run_pg(PG_DSNS["rvbbit_native_forced"], sql, repeat)
    if system in {"rvbbit_pg_heap_forced", "rvbbit_pg_heap", "pg_heap"}:
        return lambda sql, repeat=3: run_pg(PG_DSNS[system], sql, repeat)
    if system == "rvbbit_duck_hot":
        return run_rvbbit_duck_hot
    if system == "rvbbit_duck_auto":
        return lambda sql, repeat=3: run_rvbbit_duck_hot(sql, repeat, mode="auto")
    if system in FORCED_SQL_CANDIDATES:
        candidate = FORCED_SQL_CANDIDATES[system]
        return lambda sql, repeat=3: run_pg(
            PG_DSNS[system],
            sql,
            repeat,
            capture_route=True,
            expect_candidate=candidate,
        )
    if system == "duckdb":
        return run_duckdb
    if system == "clickhouse":
        return run_clickhouse
    if system in PG_DSNS:
        dsn = PG_DSNS[system]
        return lambda sql, repeat=3: run_pg(dsn, sql, repeat)
    raise ValueError(f"unknown system: {system}")
