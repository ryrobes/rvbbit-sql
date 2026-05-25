"""Run the canonical query set on every system, print a table.

Each query is run `BENCH_REPEATS` times (default 3). We report the
median; first run is included in the median so cold-cache effects
are part of the picture. For pure warm-cache numbers, set
BENCH_REPEATS=5 and report the min instead.

Usage from inside the bench container:
  docker compose -f docker/docker-compose.yml \\
                 -f docker/docker-compose.competitors.yml \\
                 exec bench python /bench/columnar_comparison/run_queries.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

sys.path.insert(0, "/bench/columnar_comparison")
from queries import QUERIES, SEMANTIC_QUERIES, STATS_PUSHDOWN_QUERIES  # noqa: E402
from runners import runner_for  # noqa: E402


SYSTEMS = os.environ.get(
    "BENCH_SYSTEMS",
    "duckdb,clickhouse,pg_baseline,citus,hydra,alloydb,rvbbit",
).split(",")
SYSTEMS = [s.strip() for s in SYSTEMS if s.strip()]
REPEATS = int(os.environ.get("BENCH_REPEATS", "3"))


def fmt_ms(ms: float) -> str:
    if ms < 1.0:
        return f"{ms*1000:.0f}µs"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms/1000:.2f}s"


def run_one(system: str, sql: str) -> tuple[float | None, str]:
    """Returns (median_ms, status). status='ok' on success, else error msg."""
    try:
        ms = runner_for(system)(sql, REPEATS)
        return ms, "ok"
    except Exception as e:
        return None, str(e).splitlines()[0][:60]


def main() -> None:
    print(f"\n=== cross-DB query bench ({REPEATS} runs each, median reported) ===")
    print(f"systems: {SYSTEMS}\n")

    # Results matrix: results[query_name][system] = (ms, status).
    results: dict = {}

    for qname, qdesc, sql in QUERIES:
        print(f"-> {qname}: {qdesc}")
        results[qname] = {}
        for sys_ in SYSTEMS:
            t0 = time.perf_counter()
            ms, status = run_one(sys_, sql)
            results[qname][sys_] = (ms, status)
            took = time.perf_counter() - t0
            label = fmt_ms(ms) if ms is not None else f"FAIL ({status})"
            print(f"     {sys_:<12} {label:>10}    (wall {took:.1f}s)")
        print()

    # ---- Stats-pushdown aggregates (rvbbit-only helper functions) ---
    print("\n=== stats-pushdown aggregates (rvbbit-only, sub-ms from row-group meta) ===\n")
    for qname, qdesc, sql in STATS_PUSHDOWN_QUERIES:
        print(f"-> {qname}: {qdesc}")
        results[qname] = {}
        for sys_ in SYSTEMS:
            if sys_ != "rvbbit":
                results[qname][sys_] = (None, "N/A — rvbbit-specific helper")
                print(f"     {sys_:<12}       N/A")
                continue
            ms, status = run_one(sys_, sql)
            results[qname][sys_] = (ms, status)
            label = fmt_ms(ms) if ms is not None else f"FAIL ({status})"
            print(f"     {sys_:<12} {label:>10}")
        print()

    # ---- Semantic queries (rvbbit-only) -----------------------------
    # Flush rvbbit's L1 cache so the cold number reflects actual specialist
    # call cost, not a hit on warm rows from a previous run.
    print("\n=== semantic queries (rvbbit-only; others N/A) ===")
    try:
        import psycopg
        with psycopg.connect("postgresql://postgres:rvbbit@pg-rvbbit:5432/bench",
                             autocommit=True) as c:
            c.execute("SELECT rvbbit.flush_cache()")
            c.execute("DELETE FROM rvbbit.receipts "
                      "WHERE operator = 'sentiment_bigfoot'")
            print("(rvbbit L1 + L2 cache flushed for sentiment_bigfoot)")
    except Exception as e:
        print(f"(cache flush skipped: {e})")
    print()
    for qname, qdesc, sql in SEMANTIC_QUERIES:
        print(f"-> {qname}: {qdesc}")
        results[qname] = {}
        for sys_ in SYSTEMS:
            if sys_ != "rvbbit":
                results[qname][sys_] = (None, "N/A — no LLM/specialist support")
                print(f"     {sys_:<12}       N/A")
                continue
            ms, status = run_one(sys_, sql)
            results[qname][sys_] = (ms, status)
            label = fmt_ms(ms) if ms is not None else f"FAIL ({status})"
            print(f"     {sys_:<12} {label:>10}")
        print()

    # ---- Markdown table ---------------------------------------------
    print("\n=== markdown summary ===\n")
    header = "| Query | " + " | ".join(SYSTEMS) + " |"
    sep = "|" + "|".join(["---"] * (1 + len(SYSTEMS))) + "|"
    print(header)
    print(sep)
    for qname, _, _ in QUERIES + STATS_PUSHDOWN_QUERIES + SEMANTIC_QUERIES:
        row = [qname]
        for sys_ in SYSTEMS:
            ms, status = results[qname].get(sys_, (None, "missing"))
            if ms is not None:
                row.append(fmt_ms(ms))
            elif "N/A" in status:
                row.append("N/A")
            else:
                row.append(f"FAIL")
        print("| " + " | ".join(row) + " |")

    # Persist for postprocessing.
    out_path = "/bench/columnar_comparison/results/last_run.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({
            "systems": SYSTEMS,
            "repeats": REPEATS,
            "queries": [
                {"name": q[0], "description": q[1],
                 "results": {s: list(results[q[0]].get(s, (None, "missing")))
                             for s in SYSTEMS}}
                for q in QUERIES + STATS_PUSHDOWN_QUERIES + SEMANTIC_QUERIES
            ],
        }, f, indent=2, default=str)
    print(f"\nresults JSON: {out_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
