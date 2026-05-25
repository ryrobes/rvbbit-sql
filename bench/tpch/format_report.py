"""Pretty-print a TPC-H results JSON as a colored console grid."""
from __future__ import annotations

import json
import math
import os
import sys
from typing import Any

from tabulate import tabulate


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
    path = sys.argv[1] if len(sys.argv) > 1 else "/bench/tpch/results/last_run.json"
    if not os.path.exists(path):
        print(f"ERROR: results file not found: {path}")
        return 1
    data = load(path)
    systems: list[str] = data["systems"]
    queries: list[dict] = data["queries"]
    suite = data.get("suite", "TPC-H")
    scale = data.get("scale", "?")

    times_by_sys: dict[str, list[float]] = {s: [] for s in systems}
    wins_by_sys: dict[str, int] = {s: 0 for s in systems}
    fails_by_sys: dict[str, int] = {s: 0 for s in systems}

    headers = ["Query", "Description"] + systems
    rows = []
    for q in queries:
        qid = q["qid"]
        desc = q["description"][:38]
        row = [c(BOLD, qid), c(DIM, desc)]
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
    print(c(BOLD, f"=== {suite} results — sf={scale}, {data.get('repeats', '?')} runs, median ==="))
    print(c(DIM, f"   systems: {', '.join(systems)}"))
    print(c(DIM, f"   queries: {len(queries)}  (best per-row in green, FAIL in red)"))
    print()
    print(
        tabulate(
            rows,
            headers=headers,
            tablefmt="rounded_grid",
            stralign="right",
            maxcolwidths=[None, 40] + [None] * len(systems),
        )
    )

    def _gmean(xs: list[float]) -> float | None:
        if not xs:
            return None
        return math.exp(sum(math.log(x) for x in xs) / len(xs))

    summary_rows = [
        [c(BOLD, "geomean (ms)")] + [fmt_ms(_gmean(times_by_sys[s])) for s in systems],
        [
            c(BOLD, "sum of medians (s)"),
            *[
                f"{sum(times_by_sys[s]) / 1000:.1f}s" if times_by_sys[s] else "-"
                for s in systems
            ],
        ],
    ]
    max_wins = max(wins_by_sys.values()) if wins_by_sys else 0
    summary_rows.append(
        [c(BOLD, "wins (best of row)")]
        + [
            c(BOLD_GREEN, str(wins_by_sys[s]))
            if wins_by_sys[s] == max_wins and max_wins > 0
            else str(wins_by_sys[s])
            for s in systems
        ]
    )
    summary_rows.append(
        [c(BOLD, "failures")]
        + [c(DIM_RED, str(fails_by_sys[s])) if fails_by_sys[s] else "0" for s in systems]
    )

    print()
    print(c(BOLD, "=== summary ==="))
    print()
    print(tabulate(summary_rows, headers=["Metric"] + systems, tablefmt="rounded_grid", stralign="right"))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
