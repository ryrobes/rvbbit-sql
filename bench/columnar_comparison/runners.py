"""Per-system query runners. Each runner takes (sql, repeat) and returns
the median wall time in ms across `repeat` runs."""
from __future__ import annotations

import os
import statistics
import time
from typing import Callable

import psycopg
import clickhouse_connect
import duckdb


# Connection details — match the docker-compose definitions.
PG_DSNS = {
    "pg_baseline": "postgresql://postgres:bench@bench-pg-baseline:5432/bench",
    "citus":       "postgresql://postgres:bench@bench-citus:5432/bench",
    "hydra":       "postgresql://postgres:bench@bench-hydra:5432/bench",
    "alloydb":     "postgresql://postgres:bench@bench-alloydb:5432/postgres",
    "rvbbit":      "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench",
}
CH_HOST = "bench-clickhouse"
CH_PORT = 8123
DUCKDB_PATH = "/data/duckdb.db"


def _median_ms(times: list[float]) -> float:
    return statistics.median(times) * 1000.0


def run_pg(dsn: str, sql: str, repeat: int = 3) -> float:
    times: list[float] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for _ in range(repeat):
                t0 = time.perf_counter()
                cur.execute(sql)
                cur.fetchall()
                times.append(time.perf_counter() - t0)
    return _median_ms(times)


def run_clickhouse(sql: str, repeat: int = 3) -> float:
    client = clickhouse_connect.get_client(host=CH_HOST, port=CH_PORT)
    times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        client.query(sql).result_rows
        times.append(time.perf_counter() - t0)
    return _median_ms(times)


def run_duckdb(sql: str, repeat: int = 3) -> float:
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        con.execute(sql).fetchall()
        times.append(time.perf_counter() - t0)
    con.close()
    return _median_ms(times)


def runner_for(system: str) -> Callable[[str, int], float]:
    if system == "duckdb":
        return run_duckdb
    if system == "clickhouse":
        return run_clickhouse
    if system in PG_DSNS:
        dsn = PG_DSNS[system]
        return lambda sql, repeat=3: run_pg(dsn, sql, repeat)
    raise ValueError(f"unknown system: {system}")
