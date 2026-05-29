"""Run the 43 standard ClickBench queries on every system, emit markdown.

Env:
  BENCH_SYSTEMS   Comma list (default: all)
  BENCH_REPEATS   Repeats per query (default: 3)
  BENCH_QUERIES   Comma list of Q-ids to run (default: all 43)
  BENCH_TIMEOUT   Per-query timeout seconds (default: 300)
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

sys.path.insert(0, "/bench/clickbench")
from queries import QUERIES  # noqa: E402
from runners import (  # noqa: E402
    PG_DSNS,
    rvbbit_duck_hot_status,
    clear_run_detail,
    last_run_detail,
    record_rvbbit_route_observation,
    run_rvbbit_datafusion_hive_forced,
    run_rvbbit_datafusion_forced,
    run_rvbbit_duck_hive_forced,
    run_rvbbit_duck_hot,
    run_clickhouse,
    run_duckdb,
    run_pg,
    runner_for,
)


SYSTEMS = os.environ.get(
    "BENCH_SYSTEMS",
    "rvbbit,duckdb,clickhouse,pg_baseline,citus,hydra,alloydb",
).split(",")
SYSTEMS = [s.strip() for s in SYSTEMS if s.strip()]
REPEATS = int(os.environ.get("BENCH_REPEATS", "3"))
TIMEOUT_S = int(os.environ.get("BENCH_TIMEOUT", "300"))
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
                f"benchmark:clickbench:{system}",
            )
            return ms, "ok"
        if system in {"rvbbit_pg_heap_forced", "rvbbit_pg_heap", "pg_heap"}:
            ms = run_pg(PG_DSNS[system], sql, REPEATS, TIMEOUT_S)
            record_rvbbit_route_observation(
                sql,
                "pg_rowstore",
                ms,
                "ok",
                f"benchmark:clickbench:{system}",
            )
            return ms, "ok"
        if system == "rvbbit_duck_hot":
            ms = run_rvbbit_duck_hot(sql, REPEATS, TIMEOUT_S, label=qid, suite="clickbench")
            return ms, rvbbit_duck_hot_status()
        if system == "rvbbit_duck_auto":
            ms = run_rvbbit_duck_hot(sql, REPEATS, TIMEOUT_S, mode="auto", label=qid, suite="clickbench")
            return ms, rvbbit_duck_hot_status()
        if system == "rvbbit_duck_forced":
            ms = run_rvbbit_duck_hot(sql, REPEATS, TIMEOUT_S, mode="force-duck", label=qid, suite="clickbench")
            return ms, rvbbit_duck_hot_status()
        if system == "rvbbit_duck_hive_forced":
            ms = run_rvbbit_duck_hive_forced(sql, REPEATS, TIMEOUT_S, label=qid, suite="clickbench")
            return ms, rvbbit_duck_hot_status()
        if system == "rvbbit_datafusion_forced":
            ms = run_rvbbit_datafusion_forced(sql, REPEATS, TIMEOUT_S, label=qid, suite="clickbench")
            return ms, rvbbit_duck_hot_status()
        if system == "rvbbit_datafusion_hive_forced":
            ms = run_rvbbit_datafusion_hive_forced(sql, REPEATS, TIMEOUT_S, label=qid, suite="clickbench")
            return ms, rvbbit_duck_hot_status()
        if system == "rvbbit_datafusion_mem_forced":
            ms = run_pg(PG_DSNS[system], sql, REPEATS, TIMEOUT_S)
            record_rvbbit_route_observation(
                sql,
                "datafusion_mem",
                ms,
                "ok",
                f"benchmark:clickbench:{system}",
            )
            return ms, "ok"
        if system in PG_DSNS:
            return run_pg(PG_DSNS[system], sql, REPEATS, TIMEOUT_S), "ok"
        return None, "unknown system"
    except Exception as e:
        return None, str(e).splitlines()[0][:80]


def _write_json(path: str, systems: list, queries: list, results: dict, details: dict) -> None:
    """Snapshot current results. Called after each query so a crash
    or timeout mid-run still leaves usable data on disk."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({
                "systems": systems,
                "repeats": REPEATS,
                "details": {
                    q[0]: {s: details.get(q[0], {}).get(s, {}) for s in systems}
                    for q in queries
                },
                "queries": [
                {"qid": q[0], "description": q[1],
                 "results": {s: list(results.get(q[0], {}).get(s, (None, "missing")))
                             for s in systems}}
                for q in queries
            ],
        }, f, indent=2, default=str)


def main() -> None:
    print(f"\n=== ClickBench query bench ({REPEATS} runs, median, {TIMEOUT_S}s timeout) ===")
    print(f"systems: {SYSTEMS}\n")
    queries = [q for q in QUERIES if SELECTED_SET is None or q[0] in SELECTED_SET]
    results: dict = {}
    details: dict = {}
    out_path = "/bench/clickbench/results/last_run.json"

    for qid, qdesc, sql in queries:
        print(f"-> {qid}: {qdesc}")
        results[qid] = {}
        details[qid] = {}
        for sys_ in SYSTEMS:
            t0 = time.perf_counter()
            clear_run_detail()
            ms, status = run_one(sys_, sql, qid)
            detail = last_run_detail()
            took = time.perf_counter() - t0
            results[qid][sys_] = (ms, status)
            details[qid][sys_] = detail
            label = fmt_ms(ms) if ms is not None else f"FAIL ({status})"
            suffix = ""
            if REPORT_COLD_WARM and detail and "first_ms" in detail:
                suffix = f" cold {fmt_ms(detail['first_ms'])}"
                if "warm_median_ms" in detail:
                    suffix += f" warm {fmt_ms(detail['warm_median_ms'])}"
            print(f"     {sys_:<14} {label:>10}    (wall {took:.1f}s){suffix}")
        # Incremental write — any later crash still leaves a usable JSON.
        _write_json(out_path, SYSTEMS, queries, results, details)
        print()

    print("\n=== markdown summary ===\n")
    header = "| Query | " + " | ".join(SYSTEMS) + " |"
    sep = "|" + "|".join(["---"] * (1 + len(SYSTEMS))) + "|"
    print(header)
    print(sep)
    for qid, qdesc, _ in queries:
        row = [qid]
        for sys_ in SYSTEMS:
            ms, status = results[qid].get(sys_, (None, "missing"))
            if ms is not None:
                row.append(fmt_ms(ms))
            else:
                row.append(f"FAIL")
        print("| " + " | ".join(row) + " |")

    # Final flush (also written incrementally above).
    _write_json(out_path, SYSTEMS, queries, results, details)
    print(f"\nresults JSON: {out_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
