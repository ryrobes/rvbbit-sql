"""Persist benchmark JSON into the rvbbit benchmark database.

This is intentionally benchmark-owned state, not pg_rvbbit extension-owned
state. It lives under bench_history so normal benchmark table reloads and
extension upgrades do not wipe it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import socket
import sys
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


DEFAULT_DSN = "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"


DDL = """
CREATE SCHEMA IF NOT EXISTS bench_history;

CREATE TABLE IF NOT EXISTS bench_history.runs (
    run_id text PRIMARY KEY,
    test_name text NOT NULL,
    suite text NOT NULL,
    scale text,
    row_count bigint,
    started_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    systems text[] NOT NULL,
    repeats integer,
    query_count integer NOT NULL,
    settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw jsonb NOT NULL,
    results_path text,
    report_path text,
    git_commit text,
    git_dirty boolean,
    host text
);

CREATE TABLE IF NOT EXISTS bench_history.query_results (
    run_id text NOT NULL REFERENCES bench_history.runs(run_id) ON DELETE CASCADE,
    test_name text NOT NULL,
    suite text NOT NULL,
    scale text,
    row_count bigint,
    started_at timestamptz NOT NULL,
    qid text NOT NULL,
    description text,
    system text NOT NULL,
    median_ms double precision,
    status text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, qid, system)
);

CREATE INDEX IF NOT EXISTS query_results_suite_scale_idx
    ON bench_history.query_results (suite, scale, row_count, system);

CREATE INDEX IF NOT EXISTS query_results_started_at_idx
    ON bench_history.query_results (started_at);

CREATE OR REPLACE VIEW bench_history.run_system_summary AS
WITH best AS (
    SELECT run_id, qid, min(median_ms) AS best_ms
    FROM bench_history.query_results
    WHERE median_ms IS NOT NULL
    GROUP BY run_id, qid
)
SELECT
    r.run_id,
    r.test_name,
    r.suite,
    r.scale,
    r.row_count,
    r.started_at,
    q.system,
    count(q.median_ms) AS query_count,
    count(*) FILTER (WHERE q.median_ms IS NULL) AS failures,
    exp(avg(ln(q.median_ms)) FILTER (WHERE q.median_ms > 0)) AS geomean_ms,
    sum(q.median_ms) FILTER (WHERE q.median_ms IS NOT NULL) AS suite_time_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY q.median_ms)
        FILTER (WHERE q.median_ms IS NOT NULL) AS p95_ms,
    max(q.median_ms) AS max_ms,
    count(*) FILTER (WHERE q.median_ms = best.best_ms) AS wins,
    count(*) FILTER (WHERE q.median_ms <= best.best_ms * 1.05) AS within_5pct,
    count(*) FILTER (WHERE q.median_ms <= best.best_ms * 1.10) AS within_10pct
FROM bench_history.runs r
JOIN bench_history.query_results q ON q.run_id = r.run_id
LEFT JOIN best ON best.run_id = q.run_id AND best.qid = q.qid
GROUP BY r.run_id, r.test_name, r.suite, r.scale, r.row_count, r.started_at, q.system;

CREATE OR REPLACE VIEW bench_history.tatp_system_summary AS
SELECT
    run_id,
    test_name,
    suite,
    scale,
    row_count,
    started_at,
    system,
    median_ms,
    (detail->>'p95_ms')::double precision AS p95_ms,
    (detail->>'p99_ms')::double precision AS p99_ms,
    (detail->>'tps')::double precision AS tps,
    (detail->>'txns')::bigint AS txns,
    (detail->>'ok')::bigint AS ok,
    (detail->>'errors')::bigint AS errors,
    status,
    detail
FROM bench_history.query_results
WHERE suite IN ('TATP', 'TATP-style') AND qid = 'txn_mix';
"""


def _parse_started_at(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc)
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = dt.datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        return dt.datetime.now(dt.timezone.utc)


def _parse_int(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _setting_value(value: str) -> Any:
    low = value.strip().lower()
    if low in {"true", "false"}:
        return low == "true"
    if low in {"none", "null"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _settings(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key = key.strip()
        if key:
            out[key] = _setting_value(value)
    return out


def _gmean(values: list[float]) -> float | None:
    positives = [v for v in values if v > 0]
    if not positives:
        return None
    return math.exp(sum(math.log(v) for v in positives) / len(positives))


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return ordered[idx]


def _summary(raw: dict[str, Any]) -> dict[str, Any]:
    if "runs" in raw and "queries" not in raw:
        by_system: dict[str, dict[str, Any]] = {}
        for run in raw.get("runs") or []:
            system = str(run.get("system") or "")
            if not system:
                continue
            by_system[system] = {
                "txns": run.get("txns"),
                "ok": run.get("ok"),
                "errors": run.get("errors"),
                "tps": run.get("tps"),
                "median_ms": run.get("median_ms"),
                "p95_ms": run.get("p95_ms"),
                "p99_ms": run.get("p99_ms"),
                "status": "error" if run.get("error") else "ok",
                "error": run.get("error"),
            }
        return {"systems": by_system}

    systems = list(raw.get("systems") or [])
    queries = list(raw.get("queries") or [])
    times_by_system: dict[str, list[float]] = {s: [] for s in systems}
    slowdown_by_system: dict[str, list[float]] = {s: [] for s in systems}
    wins_by_system: dict[str, int] = {s: 0 for s in systems}
    near_5_by_system: dict[str, int] = {s: 0 for s in systems}
    near_10_by_system: dict[str, int] = {s: 0 for s in systems}
    failures_by_system: dict[str, int] = {s: 0 for s in systems}

    for query in queries:
        valid: list[tuple[str, float]] = []
        results = query.get("results") or {}
        for system in systems:
            result = results.get(system) or [None, "missing"]
            ms = result[0] if result else None
            if ms is None:
                failures_by_system[system] += 1
                continue
            ms_f = float(ms)
            valid.append((system, ms_f))
            times_by_system[system].append(ms_f)
        if not valid:
            continue
        winner, best_ms = min(valid, key=lambda item: item[1])
        wins_by_system[winner] += 1
        if best_ms <= 0:
            continue
        for system, ms_f in valid:
            slowdown_by_system[system].append(ms_f / best_ms)
            if ms_f <= best_ms * 1.05:
                near_5_by_system[system] += 1
            if ms_f <= best_ms * 1.10:
                near_10_by_system[system] += 1

    by_system: dict[str, dict[str, Any]] = {}
    for system in systems:
        values = times_by_system[system]
        by_system[system] = {
            "query_count": len(values),
            "failures": failures_by_system[system],
            "geomean_ms": _gmean(values),
            "suite_time_ms": sum(values) if values else None,
            "geo_slowdown_vs_best": _gmean(slowdown_by_system[system]),
            "p95_ms": _percentile(values, 95),
            "max_ms": max(values) if values else None,
            "within_5pct": near_5_by_system[system],
            "within_10pct": near_10_by_system[system],
            "wins": wins_by_system[system],
        }

    paired_summary: dict[str, Any] = {}
    if "rvbbit" in systems and "alloydb" in systems:
        paired: list[tuple[float, float]] = []
        for query in queries:
            results = query.get("results") or {}
            rvbbit_ms = (results.get("rvbbit") or [None])[0]
            alloydb_ms = (results.get("alloydb") or [None])[0]
            if rvbbit_ms is not None and alloydb_ms is not None:
                paired.append((float(rvbbit_ms), float(alloydb_ms)))
        if paired:
            paired_summary["rvbbit_vs_alloydb"] = {
                "comparable_queries": len(paired),
                "rvbbit_faster": sum(1 for r, a in paired if r < a),
                "alloydb_faster": sum(1 for r, a in paired if a < r),
                "net_time_saved_by_rvbbit_ms": sum(a - r for r, a in paired),
                "rvbbit_geomean_speedup": _gmean([a / r for r, a in paired if r > 0]),
            }

    return {"systems": by_system, **paired_summary}


def _enriched_raw(
    raw: dict[str, Any],
    *,
    run_id: str,
    test_name: str,
    suite: str,
    scale: str | None,
    row_count: int | None,
    started_at: dt.datetime,
    settings: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    out = dict(raw)
    out["run_id"] = run_id
    out["test_name"] = test_name
    out["suite"] = suite
    if scale is not None:
        out["scale"] = scale
    if row_count is not None:
        out["row_count"] = row_count
    out["started_at"] = started_at.isoformat()
    out["settings"] = settings
    out["summary"] = summary
    return out


def _query_rows(
    raw: dict[str, Any],
    *,
    run_id: str,
    test_name: str,
    suite: str,
    scale: str | None,
    row_count: int | None,
    started_at: dt.datetime,
) -> list[tuple[Any, ...]]:
    if "runs" in raw and "queries" not in raw:
        rows: list[tuple[Any, ...]] = []
        for run in raw.get("runs") or []:
            system = str(run.get("system") or "")
            if not system:
                continue
            status = "error" if run.get("error") else "ok"
            median_ms = run.get("median_ms")
            rows.append(
                (
                    run_id,
                    test_name,
                    suite,
                    scale,
                    row_count,
                    started_at,
                    "txn_mix",
                    "TATP transaction mix",
                    system,
                    float(median_ms) if median_ms is not None else None,
                    status,
                    Jsonb(run),
                )
            )
        return rows

    systems = list(raw.get("systems") or [])
    details = raw.get("details") or {}
    rows: list[tuple[Any, ...]] = []
    for query in raw.get("queries") or []:
        qid = str(query.get("qid") or "")
        description = query.get("description")
        results = query.get("results") or {}
        query_details = details.get(qid) or {}
        for system in systems:
            result = results.get(system) or [None, "missing"]
            median_ms = result[0] if result else None
            status = str(result[1] if len(result) > 1 else "missing")
            rows.append(
                (
                    run_id,
                    test_name,
                    suite,
                    scale,
                    row_count,
                    started_at,
                    qid,
                    description,
                    system,
                    float(median_ms) if median_ms is not None else None,
                    status,
                    Jsonb(query_details.get(system) or {}),
                )
            )
    return rows


def _write(
    dsn: str,
    raw: dict[str, Any],
    args: argparse.Namespace,
    settings: dict[str, Any],
) -> None:
    started_at = _parse_started_at(args.started_at)
    suite = args.suite or raw.get("suite") or "unknown"
    test_name = args.test_name or suite
    scale = args.scale if args.scale is not None else raw.get("scale")
    row_count = _parse_int(args.row_count if args.row_count is not None else raw.get("row_count"))
    run_id = args.run_id or raw.get("run_id") or f"{suite.lower()}_{started_at:%Y%m%dT%H%M%SZ}"
    systems = list(raw.get("systems") or [])
    repeats = _parse_int(raw.get("repeats"))
    query_count = len(raw.get("queries") or [])
    summary = _summary(raw)
    enriched = _enriched_raw(
        raw,
        run_id=run_id,
        test_name=test_name,
        suite=suite,
        scale=scale,
        row_count=row_count,
        started_at=started_at,
        settings=settings,
        summary=summary,
    )
    rows = _query_rows(
        enriched,
        run_id=run_id,
        test_name=test_name,
        suite=suite,
        scale=scale,
        row_count=row_count,
        started_at=started_at,
    )

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("DELETE FROM bench_history.query_results WHERE run_id = %s", (run_id,))
            cur.execute(
                """
                INSERT INTO bench_history.runs (
                    run_id, test_name, suite, scale, row_count, started_at,
                    systems, repeats, query_count, settings, summary, raw,
                    results_path, report_path, git_commit, git_dirty, host
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (run_id) DO UPDATE SET
                    test_name = EXCLUDED.test_name,
                    suite = EXCLUDED.suite,
                    scale = EXCLUDED.scale,
                    row_count = EXCLUDED.row_count,
                    started_at = EXCLUDED.started_at,
                    recorded_at = now(),
                    systems = EXCLUDED.systems,
                    repeats = EXCLUDED.repeats,
                    query_count = EXCLUDED.query_count,
                    settings = EXCLUDED.settings,
                    summary = EXCLUDED.summary,
                    raw = EXCLUDED.raw,
                    results_path = EXCLUDED.results_path,
                    report_path = EXCLUDED.report_path,
                    git_commit = EXCLUDED.git_commit,
                    git_dirty = EXCLUDED.git_dirty,
                    host = EXCLUDED.host
                """,
                (
                    run_id,
                    test_name,
                    suite,
                    scale,
                    row_count,
                    started_at,
                    systems,
                    repeats,
                    query_count,
                    Jsonb(settings),
                    Jsonb(summary),
                    Jsonb(enriched),
                    args.results_path,
                    args.report_path,
                    args.git_commit,
                    args.git_dirty,
                    socket.gethostname(),
                ),
            )
            if rows:
                cur.executemany(
                    """
                    INSERT INTO bench_history.query_results (
                        run_id, test_name, suite, scale, row_count, started_at,
                        qid, description, system, median_ms, status, detail
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
        conn.commit()
    print(f"recorded benchmark run {run_id} into bench_history ({len(rows)} result rows)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("BENCH_HISTORY_DSN", DEFAULT_DSN))
    parser.add_argument("--results", required=True, help="Container path to last_run.json")
    parser.add_argument("--results-path", help="Path to store in bench_history.runs")
    parser.add_argument("--report-path")
    parser.add_argument("--run-id")
    parser.add_argument("--test-name", "--name", dest="test_name")
    parser.add_argument("--suite")
    parser.add_argument("--scale")
    parser.add_argument("--row-count")
    parser.add_argument("--started-at")
    parser.add_argument("--git-commit")
    parser.add_argument("--git-dirty", action=argparse.BooleanOptionalAction)
    parser.add_argument("--setting", action="append", default=[])
    args = parser.parse_args()

    with open(args.results) as f:
        raw = json.load(f)
    settings = _settings(args.setting)
    if args.results_path is None:
        args.results_path = args.results
    try:
        _write(args.dsn, raw, args, settings)
    except Exception as exc:
        print(f"WARNING: could not record benchmark history: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
