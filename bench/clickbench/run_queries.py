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
    record_run_error,
    record_rvbbit_route_observation,
    FORCED_SQL_CANDIDATES,
    run_rvbbit_duck_hot,
    run_clickhouse,
    run_duckdb,
    run_pg,
    run_sirius,
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
REPORT_GQE_TELEMETRY = os.environ.get("BENCH_REPORT_GQE_TELEMETRY", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def fmt_ms(ms: float) -> str:
    if ms < 1.0:
        return f"{ms*1000:.0f}µs"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms/1000:.2f}s"


def is_skip_status(status: str) -> bool:
    return status.lower().startswith("skip")


def status_label(ms: float | None, status: str) -> str:
    if ms is not None:
        return fmt_ms(ms)
    if is_skip_status(status):
        return "SKIP"
    return f"FAIL ({status})"


def run_one(system: str, sql: str, qid: str) -> tuple[float | None, str]:
    try:
        if system == "rvbbit_gpu_gqe_forced":
            skip_reason = os.environ.get("RVBBIT_GPU_GQE_SKIP_REASON", "").strip()
            if skip_reason:
                return None, f"skip: {skip_reason}"
        if system == "duckdb":
            return run_duckdb(sql, REPEATS), "ok"
        if system == "sirius":
            # GPU duckdb (sirius extension) over the SAME rvbbit parquet row
            # groups, via the shim in the rvbbit-sirius container.
            return run_sirius(sql, REPEATS), "ok"
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
        if system == "rvbbit_native_vortex":
            # Native engine reading the vortex columnar layout. Route-assert it
            # landed on native; deliberately DO NOT record a route observation —
            # native+vortex is not a distinct router Candidate yet (Phase 4), so
            # logging it as 'rvbbit_native' would pollute native's learned cost
            # curve with current no-pushdown (all-columns) timings.
            ms = run_pg(
                PG_DSNS[system],
                sql,
                REPEATS,
                TIMEOUT_S,
                capture_route=True,
                expect_candidate="rvbbit_native",
                expect_layout="vortex_scan",
            )
            return ms, "ok"
        if system in FORCED_SQL_CANDIDATES:
            candidate = FORCED_SQL_CANDIDATES[system]
            ms = run_pg(
                PG_DSNS[system],
                sql,
                REPEATS,
                TIMEOUT_S,
                capture_route=True,
                expect_candidate=candidate,
            )
            record_rvbbit_route_observation(
                sql,
                candidate,
                ms,
                "ok",
                f"benchmark:clickbench:{system}:sql_forced",
            )
            return ms, "ok"
        if system in PG_DSNS:
            return run_pg(PG_DSNS[system], sql, REPEATS, TIMEOUT_S), "ok"
        return None, "unknown system"
    except Exception as e:
        error = str(e).splitlines()[0][:240]
        record_run_error(error)
        return None, error[:80]


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
            label = status_label(ms, status)
            suffix = ""
            if REPORT_COLD_WARM and detail and "first_ms" in detail:
                suffix = f" cold {fmt_ms(detail['first_ms'])}"
                if "warm_median_ms" in detail:
                    suffix += f" warm {fmt_ms(detail['warm_median_ms'])}"
            if REPORT_GQE_TELEMETRY and isinstance(detail.get("gqe"), dict):
                gqe = detail["gqe"]
                parts = []
                mode = gqe.get("client_mode")
                if isinstance(mode, str) and mode:
                    parts.append(f"mode {mode}")
                if isinstance(gqe.get("median_flight_ms"), (int, float)) and float(gqe["median_flight_ms"]) > 0:
                    parts.append(f"flight {fmt_ms(float(gqe['median_flight_ms']))}")
                if isinstance(gqe.get("median_cli_ms"), (int, float)):
                    cli_ms = float(gqe["median_cli_ms"])
                    if cli_ms > 0 or not parts:
                        parts.append(f"cli {fmt_ms(cli_ms)}")
                if isinstance(gqe.get("median_result_read_ms"), (int, float)):
                    parts.append(f"read {fmt_ms(float(gqe['median_result_read_ms']))}")
                if isinstance(gqe.get("median_materialize_ms"), (int, float)):
                    parts.append(f"mat {fmt_ms(float(gqe['median_materialize_ms']))}")
                event_count = detail.get("sidecar_event_count")
                if isinstance(event_count, int):
                    parts.append(f"events {event_count}")
                if parts:
                    suffix += " gqe[" + " ".join(parts) + "]"
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
            elif is_skip_status(status):
                row.append("SKIP")
            else:
                row.append("FAIL")
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
