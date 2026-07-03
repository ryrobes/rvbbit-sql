"""Pretty-print a ClickBench results JSON as a colored console grid.

Usage:
  python /bench/clickbench/format_report.py [path]
  default path: /bench/clickbench/results/last_run.json
"""
from __future__ import annotations

import json
import math
import os
import sys
from typing import Any

from tabulate import tabulate


# ANSI escape codes. Detection: respect NO_COLOR; force off if stdout
# isn't a tty unless FORCE_COLOR is set.
def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


COLOR_ON = _color_enabled()


def c(code: str, s: str) -> str:
    if not COLOR_ON:
        return s
    return f"\x1b[{code}m{s}\x1b[0m"


BOLD_GREEN = "1;32"
DIM_RED = "2;31"
DIM_YELLOW = "2;33"
DIM = "2"
BOLD = "1"
CYAN = "36"


def fmt_ms(ms: float | None) -> str:
    if ms is None:
        return "-"
    if ms < 1.0:
        return f"{ms*1000:.0f}µs"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms/1000:.2f}s"


def fmt_ratio(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x:.2f}x"


def fmt_seconds(ms: float | None) -> str:
    if ms is None:
        return "-"
    return f"{ms / 1000:.1f}s"


def fmt_bytes(n: float | int | None) -> str:
    if n is None:
        return "-"
    value = float(n)
    for suffix in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024.0 or suffix == "GiB":
            if suffix == "B":
                return f"{value:.0f}{suffix}"
            return f"{value:.1f}{suffix}"
        value /= 1024.0
    return f"{value:.1f}GiB"


def fmt_signed_seconds(ms: float | None) -> str:
    if ms is None:
        return "-"
    sign = "+" if ms >= 0 else "-"
    return f"{sign}{abs(ms) / 1000:.1f}s"


def is_skip_status(status: str) -> bool:
    return status.lower().startswith("skip")


def load(path: str) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def _gmean(xs: list[float]) -> float | None:
    positive = [x for x in xs if x > 0]
    if not positive:
        return None
    return math.exp(sum(math.log(x) for x in positive) / len(positive))


def _percentile(xs: list[float], pct: float) -> float | None:
    if not xs:
        return None
    ordered = sorted(xs)
    idx = min(len(ordered) - 1, max(0, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return ordered[idx]


def _float_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _env_enabled(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _gqe_breakdown_limit() -> int | None:
    raw = os.environ.get("BENCH_GQE_BREAKDOWN_ROWS", "12").strip().lower()
    if raw in {"all", "full"}:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return 12


def _print_gqe_breakdown(data: dict[str, Any], queries: list[dict]) -> None:
    if not _env_enabled("BENCH_GQE_BREAKDOWN", True):
        return
    details = data.get("details", {})
    if not isinstance(details, dict):
        return

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    repeats = data.get("repeats", "?")
    for q in queries:
        qid = q["qid"]
        per_system = details.get(qid, {})
        if not isinstance(per_system, dict):
            continue
        detail = per_system.get("rvbbit_gpu_gqe_forced", {})
        if not isinstance(detail, dict):
            continue
        result = q.get("results", {}).get("rvbbit_gpu_gqe_forced")
        status = result[1] if isinstance(result, list) and len(result) > 1 else None
        error = detail.get("error")
        if isinstance(error, str) and error.strip():
            route = detail.get("route") if isinstance(detail.get("route"), dict) else {}
            failures.append({
                "qid": qid,
                "description": q.get("description", "")[:34],
                "route": route.get("chosen_candidate") or route.get("route") or "-",
                "status": "SKIP" if isinstance(status, str) and status.lower().startswith("skip") else "FAIL",
                "error": error.strip(),
            })
        gqe = detail.get("gqe")
        if not isinstance(gqe, dict):
            continue

        median_ms = _float_or_none(detail.get("median_ms"))
        sidecar_ms = _float_or_none(detail.get("sidecar_execute_ms"))
        cli_ms = _float_or_none(gqe.get("median_cli_ms"))
        flight_ms = _float_or_none(gqe.get("median_flight_ms"))
        exec_ms = flight_ms if flight_ms is not None and flight_ms > 0 else cli_ms
        read_ms = _float_or_none(gqe.get("median_result_read_ms"))
        materialize_ms = _float_or_none(gqe.get("median_materialize_ms"))
        rows.append({
            "qid": qid,
            "description": q.get("description", "")[:34],
            "median_ms": median_ms,
            "sidecar_ms": sidecar_ms,
            "mode": gqe.get("client_mode") or ("flight" if flight_ms else "cli"),
            "exec_ms": exec_ms,
            "cli_ms": cli_ms,
            "flight_ms": flight_ms,
            "read_ms": read_ms,
            "materialize_ms": materialize_ms,
            "events": detail.get("sidecar_event_count"),
            "result_rows": gqe.get("result_rows"),
            "result_bytes": gqe.get("result_bytes"),
        })

    if not rows and not failures:
        return

    cli_values = [r["cli_ms"] for r in rows if r["cli_ms"] is not None]
    flight_values = [r["flight_ms"] for r in rows if r["flight_ms"] is not None and r["flight_ms"] > 0]
    exec_values = [r["exec_ms"] for r in rows if r["exec_ms"] is not None]
    sidecar_values = [r["sidecar_ms"] for r in rows if r["sidecar_ms"] is not None]
    exec_shares = [
        r["exec_ms"] / r["sidecar_ms"]
        for r in rows
        if r["exec_ms"] is not None and r["sidecar_ms"] is not None and r["sidecar_ms"] > 0
    ]
    read_mat_values = [
        (r["read_ms"] or 0.0) + (r["materialize_ms"] or 0.0)
        for r in rows
        if r["read_ms"] is not None or r["materialize_ms"] is not None
    ]

    limit = _gqe_breakdown_limit()
    top_rows = sorted(rows, key=lambda r: r["exec_ms"] if r["exec_ms"] is not None else -1, reverse=True)
    if limit is not None:
        top_rows = top_rows[:limit]

    print(c(BOLD, "=== GQE diagnostic breakdown ==="))
    if rows:
        print(c(DIM, "   diagnostic sidecar samples; event counts may be lower than benchmark repeats"))
        print(c(DIM, f"   captured queries: {len(rows)}; showing slowest by GQE execution time"))
        print()
        diag_rows = [
            [c(BOLD, "captured queries"), str(len(rows))],
            [c(BOLD, "geomean GQE exec"), fmt_ms(_gmean(exec_values)) if exec_values else "-"],
            [c(BOLD, "geomean GQE Flight"), fmt_ms(_gmean(flight_values)) if flight_values else "-"],
            [c(BOLD, "geomean GQE CLI"), fmt_ms(_gmean(cli_values)) if cli_values else "-"],
            [c(BOLD, "geomean sidecar execute"), fmt_ms(_gmean(sidecar_values)) if sidecar_values else "-"],
            [
                c(BOLD, "GQE exec share of sidecar"),
                f"{(_gmean(exec_shares) or 0) * 100:.0f}%" if exec_shares else "-",
            ],
            [c(BOLD, "geomean read+materialize"), fmt_ms(_gmean(read_mat_values)) if read_mat_values else "-"],
        ]
        print(tabulate(diag_rows, headers=["Metric", "Value"], tablefmt="rounded_grid", stralign="right"))
        print()

        headers = ["Query", "Description", "query median", "sidecar sample", "mode", "gqe exec", "flight", "cli", "read", "mat", "events", "output"]
        body = []
        for row in top_rows:
            events = row["events"]
            if isinstance(events, int):
                event_text = f"{events}/{repeats}"
            else:
                event_text = "-"
            result_rows = row["result_rows"] if isinstance(row["result_rows"], int) else None
            output = f"{result_rows if result_rows is not None else '?'} rows / {fmt_bytes(_float_or_none(row['result_bytes']))}"
            body.append([
                c(BOLD, str(row["qid"])),
                c(DIM, str(row["description"])),
                fmt_ms(row["median_ms"]),
                fmt_ms(row["sidecar_ms"]),
                str(row["mode"]),
                fmt_ms(row["exec_ms"]),
                fmt_ms(row["flight_ms"]),
                fmt_ms(row["cli_ms"]),
                fmt_ms(row["read_ms"]),
                fmt_ms(row["materialize_ms"]),
                event_text,
                output,
            ])
        print(tabulate(body, headers=headers, tablefmt="rounded_grid", stralign="right",
                       maxcolwidths=[None, 36, None, None, None, None, None, None, None, None, None, None]))
    if failures:
        if rows:
            print()
        fail_body = []
        for failure in failures:
            fail_body.append([
                c(BOLD, str(failure["qid"])),
                c(DIM, str(failure["description"])),
                str(failure["route"]),
                str(failure["status"]),
                str(failure["error"]),
            ])
        print(c(BOLD, "GQE failures"))
        print(tabulate(
            fail_body,
            headers=["Query", "Description", "route", "status", "error"],
            tablefmt="rounded_grid",
            stralign="right",
            maxcolwidths=[None, 36, None, 28, 90],
        ))
    print()


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "/bench/clickbench/results/last_run.json"
    if not os.path.exists(path):
        print(f"ERROR: results file not found: {path}")
        return 1
    data = load(path)
    systems: list[str] = data["systems"]
    queries: list[dict] = data["queries"]

    # ---- Per-system stats -------------------------------------------
    times_by_sys: dict[str, list[float]] = {s: [] for s in systems}
    slowdown_by_sys: dict[str, list[float]] = {s: [] for s in systems}
    wins_by_sys: dict[str, int] = {s: 0 for s in systems}
    near_5_by_sys: dict[str, int] = {s: 0 for s in systems}
    near_10_by_sys: dict[str, int] = {s: 0 for s in systems}
    fails_by_sys: dict[str, int] = {s: 0 for s in systems}
    skips_by_sys: dict[str, int] = {s: 0 for s in systems}

    # ---- Build the grid ---------------------------------------------
    headers = ["Query", "Description"] + systems
    rows = []
    for q in queries:
        qid = q["qid"]
        desc = q["description"][:38]
        row = [c(BOLD, qid), c(DIM, desc)]

        # Find the winner (lowest ms among non-FAIL) for this row
        valid: list[tuple[str, float]] = []
        for s in systems:
            ms, status = q["results"].get(s, (None, "missing"))
            if ms is not None:
                valid.append((s, float(ms)))
                times_by_sys[s].append(float(ms))
            elif is_skip_status(str(status)):
                skips_by_sys[s] += 1
            else:
                fails_by_sys[s] += 1
        winner: str | None = None
        best_ms: float | None = None
        if valid:
            winner, best_ms = min(valid, key=lambda t: t[1])
            wins_by_sys[winner] += 1

        for s in systems:
            ms, status = q["results"].get(s, (None, "missing"))
            if ms is None:
                if is_skip_status(str(status)):
                    cell = c(DIM_YELLOW, "SKIP")
                else:
                    cell = c(DIM_RED, "FAIL")
            else:
                ms_f = float(ms)
                if best_ms and best_ms > 0:
                    slowdown_by_sys[s].append(ms_f / best_ms)
                    if ms_f <= best_ms * 1.05:
                        near_5_by_sys[s] += 1
                    if ms_f <= best_ms * 1.10:
                        near_10_by_sys[s] += 1
                cell = fmt_ms(ms_f)
                if s == winner:
                    cell = c(BOLD_GREEN, cell)
            row.append(cell)
        rows.append(row)

    print()
    print(c(BOLD, f"=== ClickBench results — {data.get('repeats', '?')} runs, median ==="))
    print(c(DIM, f"   systems: {', '.join(systems)}"))
    print(c(DIM, f"   queries: {len(queries)}  (best per-row in green, SKIP in yellow, FAIL in red)"))
    print()
    print(tabulate(rows, headers=headers, tablefmt="rounded_grid", stralign="right",
                   maxcolwidths=[None, 40] + [None] * len(systems)))

    # ---- Summary footer ---------------------------------------------
    summary_rows = []
    summary_headers = ["Metric"] + systems

    def _row(label: str, vals: list[str]) -> list[str]:
        return [c(BOLD, label)] + vals

    # Geomean (ms)
    summary_rows.append(_row(
        "geomean (ms)",
        [fmt_ms(_gmean(times_by_sys[s])) for s in systems],
    ))
    # Total wall (sum of medians, seconds)
    summary_rows.append(_row(
        "suite time (sum medians)",
        [fmt_seconds(sum(times_by_sys[s])) if times_by_sys[s] else "-" for s in systems],
    ))
    summary_rows.append(_row(
        "geo slowdown vs best",
        [fmt_ratio(_gmean(slowdown_by_sys[s])) for s in systems],
    ))
    summary_rows.append(_row(
        "p95 query median",
        [fmt_ms(_percentile(times_by_sys[s], 95)) for s in systems],
    ))
    summary_rows.append(_row(
        "max query median",
        [fmt_ms(max(times_by_sys[s]) if times_by_sys[s] else None) for s in systems],
    ))
    summary_rows.append(_row(
        "within 5% of best",
        [str(near_5_by_sys[s]) for s in systems],
    ))
    summary_rows.append(_row(
        "within 10% of best",
        [str(near_10_by_sys[s]) for s in systems],
    ))
    # Wins (highlight the most)
    max_wins = max(wins_by_sys.values()) if wins_by_sys else 0
    summary_rows.append(_row(
        "wins (best of row)",
        [
            c(BOLD_GREEN, str(wins_by_sys[s])) if wins_by_sys[s] == max_wins and max_wins > 0
            else str(wins_by_sys[s])
            for s in systems
        ],
    ))
    # Failures
    summary_rows.append(_row(
        "skipped",
        [
            c(DIM_YELLOW, str(skips_by_sys[s])) if skips_by_sys[s] > 0 else "0"
            for s in systems
        ],
    ))
    summary_rows.append(_row(
        "failures",
        [
            c(DIM_RED, str(fails_by_sys[s])) if fails_by_sys[s] > 0 else "0"
            for s in systems
        ],
    ))

    print()
    print(c(BOLD, "=== summary ==="))
    print()
    print(tabulate(summary_rows, headers=summary_headers, tablefmt="rounded_grid",
                   stralign="right"))
    print()
    _print_gqe_breakdown(data, queries)
    if "rvbbit" in systems and "alloydb" in systems:
        paired: list[tuple[float, float]] = []
        for q in queries:
            rvbbit_ms, _ = q["results"].get("rvbbit", (None, "missing"))
            alloydb_ms, _ = q["results"].get("alloydb", (None, "missing"))
            if rvbbit_ms is not None and alloydb_ms is not None:
                paired.append((float(rvbbit_ms), float(alloydb_ms)))
        if paired:
            rvbbit_faster = sum(1 for r, a in paired if r < a)
            alloydb_faster = sum(1 for r, a in paired if a < r)
            net_saved_ms = sum(a - r for r, a in paired)
            speedups = [a / r for r, a in paired if r > 0]
            h2h_rows = [
                [c(BOLD, "comparable queries"), str(len(paired))],
                [c(BOLD, "rvbbit faster"), f"{rvbbit_faster}/{len(paired)}"],
                [c(BOLD, "alloydb faster"), f"{alloydb_faster}/{len(paired)}"],
                [c(BOLD, "net time saved by rvbbit"), fmt_signed_seconds(net_saved_ms)],
                [c(BOLD, "rvbbit geomean speedup"), fmt_ratio(_gmean(speedups))],
            ]
            print(c(BOLD, "=== rvbbit vs alloydb ==="))
            print()
            print(tabulate(h2h_rows, headers=["Metric", "Value"], tablefmt="rounded_grid", stralign="right"))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
