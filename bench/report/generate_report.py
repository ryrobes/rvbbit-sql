#!/usr/bin/env python3
"""Generate a self-contained dark-mode HTML report from bench_history.

Reads bench_history.runs / run_system_summary / query_results (which survive
extension resets), embeds the data into bench/report/template.html, and writes
a single HTML file you can open directly (file://) — no server, no CDN.
Per-query bars carry the auto-router's chosen ENGINE as a color-coded badge
(NAT / DK·V / DF / GQE / ...), pulled from detail->route->route.

Usage:
  python3 bench/report/generate_report.py                 # last 30 runs -> bench/report/bench_report.html
  python3 bench/report/generate_report.py --limit 100 --out /tmp/r.html
  python3 bench/report/generate_report.py --dsn postgresql://postgres:rvbbit@localhost:55433/bench

By default data is fetched via `docker compose exec pg-rvbbit psql`; pass --dsn
to use a host psql client instead.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).resolve().parent / "template.html"
DEFAULT_OUT = Path(__file__).resolve().parent / "bench_report.html"
MARKER = "/*__BENCH_DATA__*/null"


def psql_json(sql: str, dsn: str | None, db: str) -> Any:
    """Run a SQL statement that returns a single JSON value; parse it."""
    if dsn:
        cmd = ["psql", dsn, "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql]
    else:
        cmd = [
            "docker", "compose", "-f", "docker/docker-compose.yml",
            "exec", "-T", "pg-rvbbit",
            "psql", "-U", "postgres", "-d", db, "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql,
        ]
    out = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"psql failed:\n{out.stderr.strip()}")
    text = out.stdout.strip()
    return json.loads(text) if text else None


def sql_str_array(items: list[str]) -> str:
    quoted = ",".join("'" + i.replace("'", "''") + "'" for i in items)
    return f"ARRAY[{quoted}]::text[]"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=30, help="most recent runs to embed (default 30)")
    ap.add_argument("--suite", default=None, help="only this suite (e.g. ClickBench, TPC-H)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--db", default="bench")
    ap.add_argument("--dsn", default=None, help="use host psql with this DSN instead of docker compose exec")
    args = ap.parse_args()

    suite_filter = ""
    if args.suite:
        suite_filter = "WHERE suite = '" + args.suite.replace("'", "''") + "'"

    runs = psql_json(
        f"""
        SELECT coalesce(json_agg(t ORDER BY t.started_at DESC), '[]'::json) FROM (
          SELECT run_id, test_name, suite, scale, row_count, started_at, systems,
                 repeats, query_count, git_commit, git_dirty, host
          FROM bench_history.runs
          {suite_filter}
          ORDER BY started_at DESC
          LIMIT {int(args.limit)}
        ) t
        """,
        args.dsn, args.db,
    ) or []
    if not runs:
        sys.exit("no runs found in bench_history.runs")
    run_ids = [r["run_id"] for r in runs]
    ids = sql_str_array(run_ids)

    summaries = psql_json(
        f"""
        SELECT coalesce(json_agg(t), '[]'::json) FROM (
          SELECT run_id, system, query_count, failures, geomean_ms, suite_time_ms,
                 p95_ms, max_ms, wins, within_5pct, within_10pct
          FROM bench_history.run_system_summary
          WHERE run_id = ANY({ids})
        ) t
        """,
        args.dsn, args.db,
    ) or []

    qrows = psql_json(
        f"""
        SELECT coalesce(json_agg(t), '[]'::json) FROM (
          SELECT run_id, qid, description, system, median_ms, status,
                 (detail->>'first_ms')::float8       AS first_ms,
                 (detail->>'warm_median_ms')::float8 AS warm_ms,
                 detail->'route'->>'route'           AS route,
                 detail->'route'->>'route_source'    AS route_source,
                 left(detail->'route'->>'reason', 160) AS reason
          FROM bench_history.query_results
          WHERE run_id = ANY({ids})
        ) t
        """,
        args.dsn, args.db,
    ) or []

    # assemble: run -> {meta, summaries[], queries[{qid, description, results{system: {...}}}]}
    by_run: dict[str, dict] = {r["run_id"]: r for r in runs}
    for r in runs:
        r["summaries"] = []
        r["_qmap"] = {}
    for s in summaries:
        run = by_run.get(s.pop("run_id"))
        if run is not None:
            run["summaries"].append(s)
    for row in qrows:
        run = by_run.get(row["run_id"])
        if run is None:
            continue
        q = run["_qmap"].setdefault(row["qid"], {
            "qid": row["qid"], "description": row["description"], "results": {},
        })
        entry = {"ms": row["median_ms"], "status": row["status"]}
        for k in ("first_ms", "warm_ms", "route", "route_source", "reason"):
            if row.get(k) is not None:
                entry[k] = row[k]
        q["results"][row["system"]] = entry
    for r in runs:
        r["queries"] = sorted(r.pop("_qmap").values(), key=lambda q: q["qid"])

    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ"),
        "runs": runs,
    }

    template = TEMPLATE.read_text()
    if MARKER not in template:
        sys.exit(f"template marker {MARKER!r} missing from {TEMPLATE}")
    html = template.replace(MARKER, json.dumps(data, separators=(",", ":"), default=str))
    args.out.write_text(html)
    n_q = sum(len(r["queries"]) for r in runs)
    print(f"wrote {args.out}  ({len(runs)} runs, {n_q} query rows, {args.out.stat().st_size // 1024} KB)")
    print(f"open: file://{args.out.resolve()}")


if __name__ == "__main__":
    main()
