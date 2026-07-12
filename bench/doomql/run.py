#!/usr/bin/env python3
"""Benchmark or play the DoomQL analytical raycaster."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import select
import statistics
import sys
import termios
import time
import tty
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import psycopg

try:
    from .workload import Camera, frame_hash, frame_sql, render_frame, scripted_cameras
except ImportError:
    from workload import Camera, frame_hash, frame_sql, render_frame, scripted_cameras


HERE = Path(__file__).resolve().parent
DEFAULT_DSN = os.environ.get(
    "RVBBIT_DSN",
    "postgresql://postgres:rvbbit@localhost:55433/bench",
)
CANDIDATES = {
    "auto": None,
    "rvbbit_native": "rvbbit_native",
    "duck_vector": "duck_vector",
    "duck_vortex": "duck_vortex",
    "datafusion_vector": "datafusion_vector",
    "datafusion_vortex": "datafusion_vortex",
    "gpu_gqe": "gpu_gqe",
}
DEFAULT_SYSTEMS = (
    "auto,rvbbit_native,duck_vector,duck_vortex,"
    "datafusion_vector,datafusion_vortex,gpu_gqe,duckdb"
)


@dataclass
class SystemResult:
    system: str
    status: str
    route: str | None
    route_source: str | None
    first_ms: float | None
    median_ms: float | None
    p95_ms: float | None
    fps: float | None
    output_rows: int
    frame_hashes: list[str]
    error: str | None = None


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * pct) - 1))
    return ordered[index]


def pg_session(conn: psycopg.Connection[Any], candidate: str | None, timeout_s: int) -> None:
    conn.execute("SELECT set_config('statement_timeout', %s, false)", (f"{timeout_s}s",))
    conn.execute("SELECT set_config('rvbbit.route_gpu_gqe', 'on', false)")
    conn.execute("SELECT set_config('rvbbit.route_gpu_gqe_prior', 'on', false)")
    conn.execute("SELECT set_config('rvbbit.duck_backend_fail_open', 'off', false)")
    conn.execute("SELECT set_config('rvbbit.route_force_candidate', %s, false)", (candidate or "",))


def route_explain(conn: psycopg.Connection[Any], sql: str) -> dict[str, Any]:
    value = conn.execute("SELECT rvbbit.route_explain(%s)", (sql,)).fetchone()[0]
    return value if isinstance(value, dict) else json.loads(value)


def candidate_available(doc: dict[str, Any], candidate: str | None) -> tuple[bool, str | None]:
    if candidate is None:
        return True, None
    for item in doc.get("candidates", []):
        if item.get("name") == candidate:
            return bool(item.get("available")), str(item.get("reason") or "")
    return False, "candidate missing from route_explain"


def execute_pg_frame(
    conn: psycopg.Connection[Any],
    camera: Camera,
    table: str,
    width: int,
    height: int,
) -> tuple[list[tuple[Any, ...]], float, dict[str, Any]]:
    sql = frame_sql(camera, width=width, height=height, table_expr=table)
    doc = route_explain(conn, sql)
    started = time.perf_counter()
    rows = conn.execute(sql).fetchall()
    return rows, (time.perf_counter() - started) * 1000.0, doc


def run_pg_system(
    dsn: str,
    system: str,
    cameras: list[Camera],
    table: str,
    width: int,
    height: int,
    warmups: int,
    timeout_s: int,
) -> SystemResult:
    candidate = CANDIDATES[system]
    latencies: list[float] = []
    hashes: list[str] = []
    output_rows = 0
    first_ms = None
    route = None
    route_source = None
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            pg_session(conn, candidate, timeout_s)
            first_sql = frame_sql(cameras[0], width=width, height=height, table_expr=table)
            first_doc = route_explain(conn, first_sql)
            available, reason = candidate_available(first_doc, candidate)
            if not available:
                return SystemResult(system, "skip", None, None, None, None, None, None, 0, [], reason)

            for i in range(warmups + len(cameras)):
                camera = cameras[0] if i < warmups else cameras[i - warmups]
                rows, elapsed_ms, doc = execute_pg_frame(conn, camera, table, width, height)
                if first_ms is None:
                    first_ms = elapsed_ms
                if i < warmups:
                    continue
                latencies.append(elapsed_ms)
                output_rows += len(rows)
                frame = render_frame(rows, camera, width=width, height=height)
                hashes.append(frame_hash(frame))
                route = str(doc.get("chosen_candidate") or doc.get("route") or "")
                route_source = str(doc.get("route_source") or "")
    except Exception as exc:
        return SystemResult(system, "fail", route, route_source, first_ms, None, None, None, output_rows, hashes, str(exc))

    median_ms = statistics.median(latencies)
    p95_ms = percentile(latencies, 0.95)
    return SystemResult(
        system,
        "ok",
        route,
        route_source,
        first_ms,
        median_ms,
        p95_ms,
        1000.0 / median_ms if median_ms else None,
        output_rows,
        hashes,
    )


def run_duckdb_system(
    parquet: Path,
    cameras: list[Camera],
    width: int,
    height: int,
    warmups: int,
) -> SystemResult:
    if not parquet.exists():
        return SystemResult("duckdb", "skip", None, None, None, None, None, None, 0, [], f"missing {parquet}")
    escaped = str(parquet).replace("'", "''")
    table_expr = f"read_parquet('{escaped}')"
    latencies: list[float] = []
    hashes: list[str] = []
    output_rows = 0
    first_ms = None
    try:
        with duckdb.connect(":memory:") as conn:
            for i in range(warmups + len(cameras)):
                camera = cameras[0] if i < warmups else cameras[i - warmups]
                sql = frame_sql(
                    camera,
                    width=width,
                    height=height,
                    table_expr=table_expr,
                    dialect="duckdb",
                )
                started = time.perf_counter()
                rows = conn.execute(sql).fetchall()
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                if first_ms is None:
                    first_ms = elapsed_ms
                if i < warmups:
                    continue
                latencies.append(elapsed_ms)
                output_rows += len(rows)
                hashes.append(frame_hash(render_frame(rows, camera, width=width, height=height)))
    except Exception as exc:
        return SystemResult("duckdb", "fail", "duckdb", "standalone", first_ms, None, None, None, output_rows, hashes, str(exc))
    median_ms = statistics.median(latencies)
    p95_ms = percentile(latencies, 0.95)
    return SystemResult(
        "duckdb",
        "ok",
        "duckdb",
        "standalone",
        first_ms,
        median_ms,
        p95_ms,
        1000.0 / median_ms if median_ms else None,
        output_rows,
        hashes,
    )


def fmt_ms(value: float | None) -> str:
    if value is None:
        return "-"
    if value < 1:
        return f"{value * 1000:.0f}us"
    if value < 1000:
        return f"{value:.1f}ms"
    return f"{value / 1000:.2f}s"


def enforce_parity(results: list[SystemResult]) -> str | None:
    reference = next(
        (result for result in results if result.system == "duckdb" and result.status == "ok"),
        None,
    )
    if reference is None:
        reference = next((result for result in results if result.status == "ok"), None)
    if reference is None:
        return None
    for result in results:
        if result.status == "ok" and result.frame_hashes != reference.frame_hashes:
            result.status = "mismatch"
            result.error = f"frame hashes differ from parity reference {reference.system}"
    return reference.system


def print_results(results: list[SystemResult], parity_reference: str | None) -> None:
    print("\nDoomQL analytical frame benchmark")
    print(f"{'system':<22} {'status':<7} {'route':<19} {'cold':>10} {'median':>10} {'p95':>10} {'fps':>8}  parity")
    reference = next((result for result in results if result.system == parity_reference), None)
    baseline = reference.frame_hashes if reference is not None else None
    for result in results:
        parity = "-"
        if result.status in {"ok", "mismatch"} and baseline is not None:
            parity = "ok" if result.frame_hashes == baseline else "MISMATCH"
        fps = f"{result.fps:.2f}" if result.fps is not None else "-"
        print(
            f"{result.system:<22} {result.status:<7} {(result.route or '-'):<19} "
            f"{fmt_ms(result.first_ms):>10} {fmt_ms(result.median_ms):>10} "
            f"{fmt_ms(result.p95_ms):>10} {fps:>8}  {parity}"
        )
        if result.error:
            print(f"  {result.error.splitlines()[0][:180]}")


def collect_environment(dsn: str, table: str, parquet: Path) -> dict[str, Any]:
    environment: dict[str, Any] = {
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "source_parquet_bytes": parquet.stat().st_size if parquet.exists() else None,
    }
    if parquet.exists():
        with duckdb.connect(":memory:") as conn:
            environment["source_rows"] = conn.execute(
                "SELECT count(*) FROM read_parquet(?)", [str(parquet)]
            ).fetchone()[0]
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            environment["postgres_version"] = conn.execute("SELECT version()").fetchone()[0]
            environment["rvbbit_version"] = conn.execute(
                "SELECT rvbbit.rvbbit_version()"
            ).fetchone()[0]
            environment["runtime"] = conn.execute(
                "SELECT rvbbit.accelerator_runtime_status()"
            ).fetchone()[0]
            storage = conn.execute(
                """
                SELECT coalesce(sum(n_rows), 0)::bigint,
                       coalesce(sum(n_bytes), 0)::bigint,
                       count(*)::integer,
                       pg_total_relation_size(%s::regclass)::bigint
                FROM rvbbit.row_groups_visible
                WHERE table_oid = %s::regclass
                """,
                (table, table),
            ).fetchone()
            environment["rvbbit_storage"] = dict(
                zip(("rows", "bytes", "row_groups", "heap_bytes"), storage)
            )
    except Exception as exc:
        environment["postgres_probe_error"] = str(exc)
    return environment


def render_once(
    dsn: str,
    system: str,
    camera: Camera,
    table: str,
    width: int,
    height: int,
    timeout_s: int,
) -> tuple[str, float, str]:
    candidate = CANDIDATES[system]
    with psycopg.connect(dsn, autocommit=True) as conn:
        pg_session(conn, candidate, timeout_s)
        rows, elapsed_ms, doc = execute_pg_frame(conn, camera, table, width, height)
    frame = render_frame(rows, camera, width=width, height=height)
    route = str(doc.get("chosen_candidate") or doc.get("route") or system)
    return frame, elapsed_ms, route


def interactive(args: argparse.Namespace) -> int:
    if args.system not in CANDIDATES:
        raise SystemExit("--interactive requires a PostgreSQL-backed --system")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("--interactive requires a terminal")
    camera = Camera(draw_distance=args.draw_distance)
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            pg_session(conn, CANDIDATES[args.system], args.timeout)
            while True:
                rows, elapsed_ms, doc = execute_pg_frame(
                    conn, camera, args.table, args.width, args.height
                )
                frame = render_frame(rows, camera, width=args.width, height=args.height)
                route = str(doc.get("chosen_candidate") or doc.get("route") or args.system)
                header = (
                    f"DOOMQL  {route}  {elapsed_ms:.1f}ms  {1000.0 / elapsed_ms:.2f}fps  "
                    f"camera=({camera.x},{camera.y},{camera.z}) heading={camera.heading}  "
                    f"hash={frame_hash(frame)}"
                )
                print("\x1b[2J\x1b[H" + header[: args.width] + "\n" + frame, end="", flush=True)
                ready, _, _ = select.select([sys.stdin], [], [], 0.25)
                if not ready:
                    continue
                key = sys.stdin.read(1).lower()
                if key in {"q", "\x03"}:
                    break
                if key == "w":
                    camera = camera.moved(2)
                elif key == "s":
                    camera = camera.moved(-2)
                elif key == "a":
                    camera = camera.turned(-1)
                elif key == "d":
                    camera = camera.turned(1)
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--table", default="doomql_world")
    parser.add_argument("--parquet", type=Path, required=True)
    parser.add_argument("--systems", default=DEFAULT_SYSTEMS)
    parser.add_argument("--system", default="auto", choices=sorted(CANDIDATES))
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--height", type=int, default=40)
    parser.add_argument("--draw-distance", type=int, default=128)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--output", type=Path, default=HERE / "results" / "last_run.json")
    args = parser.parse_args()
    if args.interactive:
        return interactive(args)

    systems = [item.strip() for item in args.systems.split(",") if item.strip()]
    unknown = sorted(set(systems) - set(CANDIDATES) - {"duckdb"})
    if unknown:
        parser.error(f"unknown systems: {', '.join(unknown)}")
    cameras = scripted_cameras(args.frames, args.draw_distance)
    results: list[SystemResult] = []
    for system in systems:
        print(f"Running {system}...", flush=True)
        if system == "duckdb":
            result = run_duckdb_system(args.parquet, cameras, args.width, args.height, args.warmups)
        else:
            result = run_pg_system(
                args.dsn,
                system,
                cameras,
                args.table,
                args.width,
                args.height,
                args.warmups,
                args.timeout,
            )
        results.append(result)
    parity_reference = enforce_parity(results)
    print_results(results, parity_reference)

    if args.render:
        successful = next((result for result in results if result.status == "ok" and result.system != "duckdb"), None)
        if successful:
            frame, elapsed_ms, route = render_once(
                args.dsn,
                successful.system,
                cameras[0],
                args.table,
                args.width,
                args.height,
                args.timeout,
            )
            print(f"\nDOOMQL | {route} | {elapsed_ms:.1f}ms | hash {frame_hash(frame)}")
            print(frame)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dsn": args.dsn.rsplit("@", 1)[-1],
        "table": args.table,
        "parquet": str(args.parquet),
        "frames": args.frames,
        "width": args.width,
        "height": args.height,
        "draw_distance": args.draw_distance,
        "warmups": args.warmups,
        "parity_reference": parity_reference,
        "environment": collect_environment(args.dsn, args.table, args.parquet),
        "results": [asdict(result) for result in results],
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {args.output}")
    return 0 if all(result.status in {"ok", "skip"} for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
