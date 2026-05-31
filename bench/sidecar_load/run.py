"""Concurrent load harness for Rvbbit sidecar execution paths.

This intentionally goes through normal PostgreSQL SQL execution. For a forced
Duck+Vortex run, each client opens a real PG connection, sets
rvbbit.route_force_candidate=duck_vortex, executes regular SELECTs, and lets
the extension rewrite to rvbbit.duck_vortex_query_json.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clickbench"))
from queries import QUERIES as CLICKBENCH_QUERIES  # noqa: E402


DEFAULT_DSN = os.environ.get(
    "RVBBIT_DSN",
    "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench",
)
DEFAULT_QUERIES = "Q5,Q12,Q16,Q25,Q29,Q30,Q32,Q40,Q41,Q42"


@dataclass
class WorkerResult:
    latencies: list[tuple[str, float]] = field(default_factory=list)
    warmup_ok: int = 0
    errors: int = 0
    error_samples: dict[str, int] = field(default_factory=dict)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[min(len(ordered) - 1, max(0, idx))]


def fmt_ms(value: float | None) -> str:
    if value is None:
        return "-"
    if value < 1:
        return f"{value * 1000:.0f}us"
    if value < 1000:
        return f"{value:.1f}ms"
    return f"{value / 1000:.2f}s"


def parse_csv_ints(value: str) -> list[int]:
    out = []
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        parsed = int(raw)
        if parsed <= 0:
            raise argparse.ArgumentTypeError("client counts must be positive")
        out.append(parsed)
    if not out:
        raise argparse.ArgumentTypeError("at least one client count is required")
    return out


def duck_threads_default() -> int:
    raw = os.environ.get("SIDECAR_LOAD_DUCK_THREADS") or os.environ.get("RVBBIT_DUCK_THREADS")
    if not raw:
        return 0
    return int(raw)


def selected_queries(query_ids: str) -> list[tuple[str, str, str]]:
    wanted = {qid.strip().upper() for qid in query_ids.split(",") if qid.strip()}
    by_id = {qid.upper(): (qid, desc, sql) for qid, desc, sql in CLICKBENCH_QUERIES}
    missing = sorted(wanted - set(by_id))
    if missing:
        raise SystemExit(f"unknown ClickBench query id(s): {', '.join(missing)}")
    return [by_id[qid] for qid in by_id if qid in wanted]


def set_guc(cur: psycopg.Cursor[Any], name: str, value: str) -> None:
    cur.execute("SELECT set_config(%s, %s, false)", (name, value))


def apply_session_settings(cur: psycopg.Cursor[Any], args: argparse.Namespace, app: str) -> None:
    set_guc(cur, "application_name", app)
    set_guc(cur, "statement_timeout", f"{args.statement_timeout_s}s")
    if args.duck_threads > 0:
        set_guc(cur, "rvbbit.duck_threads", str(args.duck_threads))
    if args.candidate != "auto":
        set_guc(cur, "rvbbit.route_force_candidate", args.candidate)
    if args.persistent != "default":
        set_guc(cur, "rvbbit.duck_backend_persistent", args.persistent)
    if args.arrow_ipc != "default":
        set_guc(cur, "rvbbit.duck_arrow_ipc", args.arrow_ipc)
    if args.fail_open != "default":
        set_guc(cur, "rvbbit.duck_backend_fail_open", args.fail_open)


def route_explain(
    dsn: str,
    args: argparse.Namespace,
    queries: list[tuple[str, str, str]],
) -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            apply_session_settings(cur, args, "rvbbit-sidecar-load-preflight")
            for qid, _desc, sql in queries:
                cur.execute("SELECT rvbbit.route_explain(%s)::text", (sql,))
                raw = cur.fetchone()[0]
                doc = json.loads(raw)
                docs[qid] = doc
    return docs


def check_vortex_catalog(dsn: str) -> dict[str, Any]:
    sql = """
    SELECT
        to_regclass('rvbbit.layout_variant_status') IS NOT NULL AS has_status,
        to_regclass('rvbbit.row_group_variants') IS NOT NULL AS has_variants,
        coalesce((
            SELECT count(*)
            FROM rvbbit.layout_variant_status
            WHERE layout = 'vortex_scan' AND status = 'ready'
        ), 0) AS ready_tables,
        coalesce((
            SELECT count(*)
            FROM rvbbit.row_group_variants
            WHERE layout = 'vortex_scan'
        ), 0) AS files,
        coalesce((
            SELECT sum(n_rows)
            FROM rvbbit.row_group_variants
            WHERE layout = 'vortex_scan'
        ), 0) AS rows
    """
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
                return {
                    "has_status": bool(row[0]),
                    "has_variants": bool(row[1]),
                    "ready_tables": int(row[2] or 0),
                    "files": int(row[3] or 0),
                    "rows": int(row[4] or 0),
                }
    except Exception as exc:
        return {"error": str(exc)}


def pg_activity_sample(dsn: str, stop: threading.Event, interval_s: float) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                while not stop.wait(interval_s):
                    cur.execute(
                        """
                        SELECT
                            count(*) FILTER (WHERE application_name LIKE 'rvbbit-sidecar-load-%'),
                            count(*) FILTER (
                                WHERE application_name LIKE 'rvbbit-sidecar-load-%'
                                  AND state = 'active'
                            ),
                            count(*) FILTER (
                                WHERE application_name LIKE 'rvbbit-sidecar-load-%'
                                  AND wait_event IS NOT NULL
                            ),
                            count(*),
                            count(*) FILTER (WHERE application_name = 'rvbbit-duck-sidecar')
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                        """
                    )
                    total, active, waiting, db_total, sidecar_total = cur.fetchone()
                    samples.append(
                        {
                            "ts": now_utc(),
                            "connections": int(total or 0),
                            "active": int(active or 0),
                            "waiting": int(waiting or 0),
                            "db_connections": int(db_total or 0),
                            "sidecar_connections": int(sidecar_total or 0),
                        }
                    )
    except Exception as exc:
        samples.append({"ts": now_utc(), "error": str(exc)})
    return samples


def run_worker(
    worker_id: int,
    dsn: str,
    args: argparse.Namespace,
    queries: list[tuple[str, str, str]],
    barrier: threading.Barrier,
    start_at: float,
) -> WorkerResult:
    result = WorkerResult()
    measure_start = start_at + args.warmup_s
    end_at = measure_start + args.duration_s
    app = f"rvbbit-sidecar-load-{worker_id}"
    idx = worker_id
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                apply_session_settings(cur, args, app)
                barrier.wait()
                delay = start_at - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                while time.perf_counter() < end_at:
                    qid, _desc, sql = queries[idx % len(queries)]
                    idx += 1
                    before = time.perf_counter()
                    try:
                        cur.execute(sql.encode())  # type: ignore[arg-type]
                        cur.fetchall()
                        elapsed_ms = (time.perf_counter() - before) * 1000.0
                        if before >= measure_start:
                            result.latencies.append((qid, elapsed_ms))
                        else:
                            result.warmup_ok += 1
                    except Exception:
                        result.errors += 1
                        err = str(sys.exc_info()[1]).splitlines()[0][:200]
                        result.error_samples[err] = result.error_samples.get(err, 0) + 1
                        try:
                            conn.rollback()
                        except Exception:
                            pass
    except Exception:
        result.errors += 1
        err = str(sys.exc_info()[1]).splitlines()[0][:200]
        result.error_samples[err] = result.error_samples.get(err, 0) + 1
    return result


def summarize_sweep(
    clients: int,
    elapsed_s: float,
    results: list[WorkerResult],
    pg_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    all_latencies = [lat for result in results for _qid, lat in result.latencies]
    by_query: dict[str, list[float]] = {}
    for result in results:
        for qid, lat in result.latencies:
            by_query.setdefault(qid, []).append(lat)
    errors: dict[str, int] = {}
    for result in results:
        for err, count in result.error_samples.items():
            errors[err] = errors.get(err, 0) + count

    return {
        "clients": clients,
        "elapsed_s": elapsed_s,
        "ok": len(all_latencies),
        "warmup_ok": sum(result.warmup_ok for result in results),
        "errors": sum(result.errors for result in results),
        "qps": len(all_latencies) / elapsed_s if elapsed_s > 0 else None,
        "median_ms": statistics.median(all_latencies) if all_latencies else None,
        "p95_ms": percentile(all_latencies, 95),
        "p99_ms": percentile(all_latencies, 99),
        "max_ms": max(all_latencies) if all_latencies else None,
        "queries": {
            qid: {
                "ok": len(values),
                "median_ms": statistics.median(values),
                "p95_ms": percentile(values, 95),
                "max_ms": max(values),
            }
            for qid, values in sorted(by_query.items())
        },
        "error_samples": dict(sorted(errors.items(), key=lambda item: (-item[1], item[0]))[:10]),
        "pg_activity": {
            "samples": len(pg_samples),
            "max_connections": max((s.get("connections", 0) for s in pg_samples), default=0),
            "max_active": max((s.get("active", 0) for s in pg_samples), default=0),
            "max_waiting": max((s.get("waiting", 0) for s in pg_samples), default=0),
            "max_db_connections": max((s.get("db_connections", 0) for s in pg_samples), default=0),
            "max_sidecar_connections": max(
                (s.get("sidecar_connections", 0) for s in pg_samples),
                default=0,
            ),
        },
    }


def run_sweep(
    clients: int,
    dsn: str,
    args: argparse.Namespace,
    queries: list[tuple[str, str, str]],
) -> dict[str, Any]:
    barrier = threading.Barrier(clients + 1)
    start_at = time.perf_counter() + 1.0
    stop_sampler = threading.Event()
    pg_samples: list[dict[str, Any]] = []
    sampler = threading.Thread(
        target=lambda: pg_samples.extend(
            pg_activity_sample(dsn, stop_sampler, args.sample_interval_s)
        ),
        daemon=True,
    )
    sampler.start()

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as pool:
        futures = [
            pool.submit(run_worker, idx, dsn, args, queries, barrier, start_at)
            for idx in range(clients)
        ]
        barrier.wait()
        results = [future.result() for future in futures]
    elapsed = time.perf_counter() - t0
    stop_sampler.set()
    sampler.join(timeout=2)
    return summarize_sweep(clients, elapsed, results, pg_samples)


def print_table(sweeps: list[dict[str, Any]]) -> None:
    headers = [
        "clients",
        "ok",
        "errors",
        "qps",
        "median",
        "p95",
        "p99",
        "max",
        "pg_conn",
        "pg_active",
        "pg_wait",
        "duck_pg",
    ]
    widths = [len(h) for h in headers]
    rows = []
    for sweep in sweeps:
        row = [
            str(sweep["clients"]),
            str(sweep["ok"]),
            str(sweep["errors"]),
            f"{sweep['qps']:.2f}" if sweep["qps"] is not None else "-",
            fmt_ms(sweep["median_ms"]),
            fmt_ms(sweep["p95_ms"]),
            fmt_ms(sweep["p99_ms"]),
            fmt_ms(sweep["max_ms"]),
            str(sweep["pg_activity"].get("max_db_connections", "-")),
            str(sweep["pg_activity"]["max_active"]),
            str(sweep["pg_activity"].get("max_waiting", "-")),
            str(sweep["pg_activity"].get("max_sidecar_connections", "-")),
        ]
        rows.append(row)
        widths = [max(w, len(cell)) for w, cell in zip(widths, row)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(cell.rjust(w) for cell, w in zip(row, widths)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument(
        "--clients",
        type=parse_csv_ints,
        default=parse_csv_ints(os.environ.get("SIDECAR_LOAD_CLIENTS", "1,2,4,8,16")),
        help="Comma-separated concurrent client counts.",
    )
    parser.add_argument("--duration-s", type=float, default=float(os.environ.get("SIDECAR_LOAD_DURATION_S", "30")))
    parser.add_argument("--warmup-s", type=float, default=float(os.environ.get("SIDECAR_LOAD_WARMUP_S", "5")))
    parser.add_argument("--sample-interval-s", type=float, default=float(os.environ.get("SIDECAR_LOAD_SAMPLE_INTERVAL_S", "1")))
    parser.add_argument("--statement-timeout-s", type=int, default=int(os.environ.get("SIDECAR_LOAD_STATEMENT_TIMEOUT_S", "300")))
    parser.add_argument("--queries", default=os.environ.get("SIDECAR_LOAD_QUERIES", DEFAULT_QUERIES))
    parser.add_argument(
        "--candidate",
        default=os.environ.get("SIDECAR_LOAD_CANDIDATE", "duck_vortex"),
        help="Route candidate to force, or auto.",
    )
    parser.add_argument("--allow-fallback", action="store_true")
    parser.add_argument("--persistent", choices=["default", "on", "off"], default=os.environ.get("SIDECAR_LOAD_PERSISTENT", "default"))
    parser.add_argument("--arrow-ipc", choices=["default", "on", "off"], default=os.environ.get("SIDECAR_LOAD_ARROW_IPC", "default"))
    parser.add_argument("--fail-open", choices=["default", "on", "off"], default=os.environ.get("SIDECAR_LOAD_FAIL_OPEN", "off"))
    parser.add_argument(
        "--duck-threads",
        type=int,
        default=duck_threads_default(),
        help="Set rvbbit.duck_threads for each client session. 0 keeps the extension default.",
    )
    parser.add_argument("--json-out", default=os.environ.get("SIDECAR_LOAD_JSON_OUT", ""))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    queries = selected_queries(args.queries)
    catalog = check_vortex_catalog(args.dsn)
    try:
        routes = route_explain(args.dsn, args, queries)
    except Exception as exc:
        print("route preflight failed", file=sys.stderr)
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "catalog": catalog,
                    "hint": "Load the selected benchmark dataset first; for the default query set this requires public.hits.",
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    if args.candidate != "auto" and not args.allow_fallback:
        mismatches = {
            qid: {
                "chosen": doc.get("chosen_candidate"),
                "reason": doc.get("reason"),
                "route_source": doc.get("route_source"),
            }
            for qid, doc in routes.items()
            if doc.get("chosen_candidate") != args.candidate
        }
        if mismatches:
            print("forced route is not available for every selected query", file=sys.stderr)
            print(json.dumps({"catalog": catalog, "mismatches": mismatches}, indent=2), file=sys.stderr)
            return 2

    run_doc: dict[str, Any] = {
        "run_id": f"sidecar_load_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "started_at": now_utc(),
        "config": {
            "clients": args.clients,
            "duration_s": args.duration_s,
            "warmup_s": args.warmup_s,
            "candidate": args.candidate,
            "queries": [qid for qid, _desc, _sql in queries],
            "persistent": args.persistent,
            "arrow_ipc": args.arrow_ipc,
            "fail_open": args.fail_open,
            "duck_threads": args.duck_threads,
        },
        "catalog": catalog,
        "routes": {
            qid: {
                "route": doc.get("route"),
                "chosen_candidate": doc.get("chosen_candidate"),
                "route_source": doc.get("route_source"),
                "reason": doc.get("reason"),
            }
            for qid, doc in routes.items()
        },
        "sweeps": [],
    }

    print(
        f"sidecar load run: candidate={args.candidate} clients={args.clients} "
        f"duration={args.duration_s}s warmup={args.warmup_s}s duck_threads={args.duck_threads or 'default'}"
    )
    print(f"queries: {', '.join(qid for qid, _desc, _sql in queries)}")
    print(f"vortex catalog: {catalog}")

    for clients in args.clients:
        print(f"\n== clients={clients}")
        sweep = run_sweep(clients, args.dsn, args, queries)
        run_doc["sweeps"].append(sweep)
        print_table([sweep])
        if sweep["error_samples"]:
            print("errors:")
            for err, count in sweep["error_samples"].items():
                print(f"  {count}x {err}")

    run_doc["finished_at"] = now_utc()
    print("\n== summary")
    print_table(run_doc["sweeps"])

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(run_doc, indent=2, sort_keys=True) + "\n")
        print(f"\njson saved to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
