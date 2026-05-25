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


def load(path: str) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


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
    wins_by_sys: dict[str, int] = {s: 0 for s in systems}
    fails_by_sys: dict[str, int] = {s: 0 for s in systems}

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
            else:
                fails_by_sys[s] += 1
        winner: str | None = None
        if valid:
            winner = min(valid, key=lambda t: t[1])[0]
            wins_by_sys[winner] += 1

        for s in systems:
            ms, status = q["results"].get(s, (None, "missing"))
            if ms is None:
                cell = c(DIM_RED, "FAIL")
            else:
                cell = fmt_ms(float(ms))
                if s == winner:
                    cell = c(BOLD_GREEN, cell)
            row.append(cell)
        rows.append(row)

    print()
    print(c(BOLD, f"=== ClickBench results — {data.get('repeats', '?')} runs, median ==="))
    print(c(DIM, f"   systems: {', '.join(systems)}"))
    print(c(DIM, f"   queries: {len(queries)}  (best per-row in green, FAIL in red)"))
    print()
    print(tabulate(rows, headers=headers, tablefmt="rounded_grid", stralign="right",
                   maxcolwidths=[None, 40] + [None] * len(systems)))

    # ---- Summary footer ---------------------------------------------
    summary_rows = []
    summary_headers = ["Metric"] + systems

    def _gmean(xs: list[float]) -> float | None:
        if not xs:
            return None
        return math.exp(sum(math.log(x) for x in xs) / len(xs))

    def _row(label: str, vals: list[str]) -> list[str]:
        return [c(BOLD, label)] + vals

    # Geomean (ms)
    summary_rows.append(_row(
        "geomean (ms)",
        [fmt_ms(_gmean(times_by_sys[s])) for s in systems],
    ))
    # Total wall (sum of medians, seconds)
    summary_rows.append(_row(
        "sum of medians (s)",
        [f"{sum(times_by_sys[s])/1000:.1f}s" if times_by_sys[s] else "-"
         for s in systems],
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
