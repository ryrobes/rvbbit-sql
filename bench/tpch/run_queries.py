"""Run the 22 TPC-H queries on each selected system."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue
import sys
import time
import traceback

sys.path.insert(0, "/bench/tpch")
from queries import base_queries, sql_for_system  # noqa: E402
from runners import (  # noqa: E402
    PG_DSNS,
    rvbbit_duck_hot_status,
    clear_run_detail,
    last_run_detail,
    record_rvbbit_route_observation,
    run_rvbbit_datafusion_hive_forced,
    run_rvbbit_datafusion_forced,
    run_rvbbit_duck_hive_forced,
    run_rvbbit_duck_vortex_forced,
    run_rvbbit_duck_hot,
    run_clickhouse,
    run_duckdb,
    run_pg,
)


SYSTEMS = os.environ.get(
    "BENCH_SYSTEMS",
    "rvbbit,duckdb,clickhouse,pg_baseline,citus,hydra,alloydb",
).split(",")
SYSTEMS = [s.strip() for s in SYSTEMS if s.strip()]
REPEATS = int(os.environ.get("BENCH_REPEATS", "3"))
TIMEOUT_S = int(os.environ.get("BENCH_TIMEOUT", "300"))
WALL_TIMEOUT_S = float(os.environ.get("BENCH_WALL_TIMEOUT", str(TIMEOUT_S)))
WALL_TIMEOUT_GRACE_S = float(os.environ.get("BENCH_WALL_TIMEOUT_GRACE", "5"))
WALL_TIMEOUT_SYSTEMS = {"duckdb", "clickhouse", "rvbbit_duck_vortex_forced"}
SELECTED = os.environ.get("BENCH_QUERIES")
SELECTED_SET = set(SELECTED.split(",")) if SELECTED else None
REPORT_COLD_WARM = os.environ.get("BENCH_REPORT_COLD_WARM", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def fmt_ms(ms: float) -> str:
    if ms < 1.0:
        return f"{ms*1000:.0f}µs"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms/1000:.2f}s"


def run_one(system: str, sql: str, qid: str) -> tuple[float | None, str]:
    try:
        if system == "duckdb":
            return run_duckdb(sql, REPEATS), "ok"
        if system == "clickhouse":
            return run_clickhouse(sql, REPEATS), "ok"
        if system == "rvbbit":
            return run_pg(PG_DSNS["rvbbit"], sql, REPEATS, TIMEOUT_S, capture_route=True), "ok"
        if system in {"rvbbit_native", "rvbbit_native_forced"}:
            ms = run_pg(PG_DSNS[system], sql, REPEATS, TIMEOUT_S)
            record_rvbbit_route_observation(
                sql,
                "rvbbit_native",
                ms,
                "ok",
                f"benchmark:tpch:{system}",
            )
            return ms, "ok"
        if system in {"rvbbit_pg_heap_forced", "rvbbit_pg_heap", "pg_heap"}:
            ms = run_pg(PG_DSNS[system], sql, REPEATS, TIMEOUT_S)
            record_rvbbit_route_observation(
                sql,
                "pg_rowstore",
                ms,
                "ok",
                f"benchmark:tpch:{system}",
            )
            return ms, "ok"
        if system == "rvbbit_duck_hot":
            ms = run_rvbbit_duck_hot(sql, REPEATS, TIMEOUT_S, label=qid, suite="tpch")
            return ms, rvbbit_duck_hot_status()
        if system == "rvbbit_duck_auto":
            ms = run_rvbbit_duck_hot(sql, REPEATS, TIMEOUT_S, mode="auto", label=qid, suite="tpch")
            return ms, rvbbit_duck_hot_status()
        if system == "rvbbit_duck_forced":
            ms = run_rvbbit_duck_hot(sql, REPEATS, TIMEOUT_S, mode="force-duck", label=qid, suite="tpch")
            return ms, rvbbit_duck_hot_status()
        if system == "rvbbit_duck_hive_forced":
            ms = run_rvbbit_duck_hive_forced(sql, REPEATS, TIMEOUT_S, label=qid, suite="tpch")
            return ms, rvbbit_duck_hot_status()
        if system == "rvbbit_duck_vortex_forced":
            ms = run_rvbbit_duck_vortex_forced(sql, REPEATS, TIMEOUT_S, label=qid, suite="tpch")
            return ms, rvbbit_duck_hot_status()
        if system == "rvbbit_datafusion_forced":
            ms = run_rvbbit_datafusion_forced(sql, REPEATS, TIMEOUT_S, label=qid, suite="tpch")
            return ms, rvbbit_duck_hot_status()
        if system == "rvbbit_datafusion_hive_forced":
            ms = run_rvbbit_datafusion_hive_forced(sql, REPEATS, TIMEOUT_S, label=qid, suite="tpch")
            return ms, rvbbit_duck_hot_status()
        if system == "rvbbit_datafusion_mem_forced":
            ms = run_pg(PG_DSNS[system], sql, REPEATS, TIMEOUT_S)
            record_rvbbit_route_observation(
                sql,
                "datafusion_mem",
                ms,
                "ok",
                f"benchmark:tpch:{system}",
            )
            return ms, "ok"
        if system == "rvbbit_datafusion_vortex_forced":
            ms = run_pg(
                PG_DSNS[system],
                sql,
                REPEATS,
                TIMEOUT_S,
                capture_route=True,
                expect_candidate="datafusion_vortex",
            )
            record_rvbbit_route_observation(
                sql,
                "datafusion_vortex",
                ms,
                "ok",
                f"benchmark:tpch:{system}",
            )
            return ms, "ok"
        if system in PG_DSNS:
            return run_pg(PG_DSNS[system], sql, REPEATS, TIMEOUT_S), "ok"
        return None, "unknown system"
    except Exception as e:
        return None, str(e).splitlines()[0][:120]


def _run_one_worker(result_queue, system: str, sql: str, qid: str) -> None:
    try:
        clear_run_detail()
        ms, status = run_one(system, sql, qid)
        result_queue.put((ms, status, last_run_detail()))
    except BaseException as e:
        result_queue.put((None, str(e).splitlines()[0][:120], {}))


def run_one_guarded(system: str, sql: str, qid: str) -> tuple[float | None, str, dict]:
    if system not in WALL_TIMEOUT_SYSTEMS:
        clear_run_detail()
        ms, status = run_one(system, sql, qid)
        return ms, status, last_run_detail()

    ctx = mp.get_context("fork")
    result_queue = ctx.Queue()
    proc = ctx.Process(target=_run_one_worker, args=(result_queue, system, sql, qid))
    proc.start()
    proc.join(WALL_TIMEOUT_S + WALL_TIMEOUT_GRACE_S)
    if proc.is_alive():
        proc.terminate()
        proc.join(2)
        if proc.is_alive():
            proc.kill()
            proc.join(2)
        return (
            None,
            f"wall timeout after {WALL_TIMEOUT_S:.0f}s",
            {"wall_timeout_s": WALL_TIMEOUT_S},
        )
    try:
        return result_queue.get_nowait()
    except queue.Empty:
        status = f"runner exited without result (exit={proc.exitcode})"
        return None, status[:120], {}


def _write_json(path: str, systems: list, queries: list, results: dict, details: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(
            {
                "suite": "TPC-H",
                "scale": os.environ.get("TPCH_SCALE", "0.1"),
                "systems": systems,
                "repeats": REPEATS,
                "details": {
                    q[0]: {s: details.get(q[0], {}).get(s, {}) for s in systems}
                    for q in queries
                },
                "queries": [
                    {
                        "qid": q[0],
                        "description": q[1],
                        "results": {
                            s: list(results.get(q[0], {}).get(s, (None, "missing")))
                            for s in systems
                        },
                    }
                    for q in queries
                ],
            },
            f,
            indent=2,
            default=str,
        )


def main() -> None:
    print(f"\n=== TPC-H query bench ({REPEATS} runs, median, {TIMEOUT_S}s timeout) ===", flush=True)
    print(f"systems: {SYSTEMS}\n", flush=True)
    queries = [q for q in base_queries() if SELECTED_SET is None or q[0] in SELECTED_SET]
    results: dict = {}
    details: dict = {}
    out_path = "/bench/tpch/results/last_run.json"

    for qid, qdesc, sql in queries:
        print(f"-> {qid}: {qdesc}", flush=True)
        results[qid] = {}
        details[qid] = {}
        for sys_ in SYSTEMS:
            t0 = time.perf_counter()
            ms, status, detail = run_one_guarded(sys_, sql_for_system(sql, sys_, qid), qid)
            took = time.perf_counter() - t0
            results[qid][sys_] = (ms, status)
            details[qid][sys_] = detail
            label = fmt_ms(ms) if ms is not None else f"FAIL ({status})"
            suffix = ""
            if REPORT_COLD_WARM and detail and "first_ms" in detail:
                suffix = f" cold {fmt_ms(detail['first_ms'])}"
                if "warm_median_ms" in detail:
                    suffix += f" warm {fmt_ms(detail['warm_median_ms'])}"
            print(f"     {sys_:<14} {label:>14}    (wall {took:.1f}s){suffix}", flush=True)
        _write_json(out_path, SYSTEMS, queries, results, details)
        print(flush=True)

    print("\n=== markdown summary ===\n", flush=True)
    header = "| Query | " + " | ".join(SYSTEMS) + " |"
    sep = "|" + "|".join(["---"] * (1 + len(SYSTEMS))) + "|"
    print(header, flush=True)
    print(sep, flush=True)
    for qid, _, _ in queries:
        row = [qid]
        for sys_ in SYSTEMS:
            ms, status = results[qid].get(sys_, (None, "missing"))
            row.append(fmt_ms(ms) if ms is not None else "FAIL")
        print("| " + " | ".join(row) + " |", flush=True)

    _write_json(out_path, SYSTEMS, queries, results, details)
    print(f"\nresults JSON: {out_path}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
