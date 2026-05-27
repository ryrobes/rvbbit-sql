"""Per-system query runners for TPC-H."""
from __future__ import annotations

import os
import statistics
import sys
import time

import clickhouse_connect
import duckdb
import psycopg

sys.path.insert(0, "/bench")
sys.path.insert(0, "/bench/tpch")
from rvbbit_duck_hot import (  # noqa: E402
    rvbbit_duck_hot_status,
    rvbbit_duck_hot_detail,
    clear_rvbbit_duck_hot_detail,
    record_rvbbit_route_observation,
    run_rvbbit_datafusion_hive_forced,
    run_rvbbit_datafusion_forced,
    run_rvbbit_duck_hive_forced,
    run_rvbbit_duck_hot,
)
from schema import duckdb_path_for_scale  # noqa: E402


PG_DSNS = {
    "pg_baseline": "postgresql://postgres:bench@bench-pg-baseline:5432/bench",
    "citus": "postgresql://postgres:bench@bench-citus:5432/bench",
    "hydra": "postgresql://postgres:bench@bench-hydra:5432/bench",
    "alloydb": "postgresql://postgres:bench@bench-alloydb:5432/postgres",
    "rvbbit": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench",
    "rvbbit_native": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.duck_backend%3Doff",
    "rvbbit_native_forced": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.route_force_candidate%3Drvbbit_native",
    "rvbbit_datafusion_mem_forced": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.route_force_candidate%3Ddatafusion_mem",
    "rvbbit_pg_heap_forced": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.duck_backend%3Doff%20-c%20rvbbit.force_heap_scan%3Don",
    "rvbbit_pg_heap": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.duck_backend%3Doff%20-c%20rvbbit.force_heap_scan%3Don",
    "pg_heap": "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench?options=-c%20rvbbit.duck_backend%3Doff%20-c%20rvbbit.force_heap_scan%3Don",
}
CH_HOST = "bench-clickhouse"
CH_PORT = 8123


def _median_ms(times: list[float]) -> float:
    return statistics.median(times) * 1000.0


_LAST_RUN_DETAIL: dict[str, float] = {}
ROUTE_GUCS = {
    "RVBBIT_ROUTE_DUCK_VECTOR": "rvbbit.route_duck_vector",
    "RVBBIT_ROUTE_DUCK_HIVE": "rvbbit.route_duck_hive",
    "RVBBIT_ROUTE_DATAFUSION_MEM": "rvbbit.route_datafusion_mem",
    "RVBBIT_ROUTE_DATAFUSION_VECTOR": "rvbbit.route_datafusion_vector",
    "RVBBIT_ROUTE_DATAFUSION_HIVE": "rvbbit.route_datafusion_hive",
    "RVBBIT_ROUTE_HIVE": "rvbbit.route_hive",
    "RVBBIT_ROUTE_PG_ROWSTORE": "rvbbit.route_pg_rowstore",
    "RVBBIT_ROUTE_RVBBIT_NATIVE": "rvbbit.route_rvbbit_native",
    "RVBBIT_ROUTE_FORCE_CANDIDATE": "rvbbit.route_force_candidate",
    "RVBBIT_ROUTE_HIVE_MIN_CONFIDENCE": "rvbbit.route_hive_min_confidence",
    "RVBBIT_HOT_STORE_BUDGET_MB": "rvbbit.hot_store_budget_mb",
    "RVBBIT_HOT_STORE_ROUTE_MAX_ROWS": "rvbbit.hot_store_route_max_rows",
    # In-process DataFusion (default on as of Phase 1). Set "off" to
    # force the legacy rvbbit-duck sidecar path for A/B benchmarking.
    "RVBBIT_DF_INPROCESS": "rvbbit.df_inprocess",
}


def clear_run_detail() -> None:
    _LAST_RUN_DETAIL.clear()
    clear_rvbbit_duck_hot_detail()


def last_run_detail() -> dict[str, float]:
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


def run_pg(dsn: str, sql: str, repeat: int = 3, timeout_s: int = 300) -> float:
    times: list[float] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {timeout_s * 1000}".encode())  # type: ignore[arg-type]
            _apply_route_gucs(cur)
            for _ in range(repeat):
                t0 = time.perf_counter()
                cur.execute(sql.encode())  # type: ignore[arg-type]
                cur.fetchall()
                times.append(time.perf_counter() - t0)
    _record_times(times)
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
    scale = os.environ.get("TPCH_SCALE", "0.1")
    con = duckdb.connect(duckdb_path_for_scale(scale), read_only=True)
    times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        con.execute(sql).fetchall()
        times.append(time.perf_counter() - t0)
    con.close()
    _record_times(times)
    return _median_ms(times)
