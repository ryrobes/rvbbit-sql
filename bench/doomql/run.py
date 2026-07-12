#!/usr/bin/env python3
"""Benchmark or play the DoomQL analytical raycaster."""

from __future__ import annotations

import argparse
import hashlib
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

import clickhouse_connect
import duckdb
import psycopg

try:
    from .load import DEFAULT_WAD, WORLD_COLUMNS, simple_identifier
    from .wad_world import (
        DEFAULT_GRID_SCALE,
        EPISODE_MAPS,
        MaterialColorRamp,
        MaterialTexture,
        RasterizedWorld,
        rasterize_episode,
        rasterize_map,
        read_wad_map,
    )
    from .workload import (
        CAMERA_VECTOR_SCALE,
        RENDER_TYPES,
        Camera,
        camera_vector,
        frame_hash,
        frame_sql,
        render_frame,
        scripted_cameras,
    )
except ImportError:
    from load import DEFAULT_WAD, WORLD_COLUMNS, simple_identifier
    from wad_world import (
        DEFAULT_GRID_SCALE,
        EPISODE_MAPS,
        MaterialColorRamp,
        MaterialTexture,
        RasterizedWorld,
        rasterize_episode,
        rasterize_map,
        read_wad_map,
    )
    from workload import (
        CAMERA_VECTOR_SCALE,
        RENDER_TYPES,
        Camera,
        camera_vector,
        frame_hash,
        frame_sql,
        render_frame,
        scripted_cameras,
    )


HERE = Path(__file__).resolve().parent
DEFAULT_DSN = os.environ.get(
    "RVBBIT_DSN",
    "postgresql://postgres:rvbbit@localhost:55433/bench",
)
DEFAULT_POSTGRES_DSN = os.environ.get(
    "DOOMQL_POSTGRES_DSN",
    "postgresql://postgres:bench@localhost:5440/bench",
)
DEFAULT_CITUS_DSN = os.environ.get(
    "DOOMQL_CITUS_DSN",
    "postgresql://postgres:bench@localhost:5441/bench",
)
DEFAULT_HYDRA_DSN = os.environ.get(
    "DOOMQL_HYDRA_DSN",
    "postgresql://postgres:bench@localhost:5442/bench",
)
DEFAULT_ALLOYDB_DSN = os.environ.get(
    "DOOMQL_ALLOYDB_DSN",
    "postgresql://postgres:bench@localhost:5443/postgres",
)
DEFAULT_CLICKHOUSE_HOST = os.environ.get("DOOMQL_CLICKHOUSE_HOST", "localhost")
DEFAULT_CLICKHOUSE_PORT = int(os.environ.get("DOOMQL_CLICKHOUSE_PORT", "8123"))
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
PG_COMPETITOR_ROUTES = {
    "postgres": "postgres_heap",
    "citus": "citus_columnar",
    "hydra": "hydra_columnar",
    "alloydb": "alloydb_omni",
}
VANILLA_SYSTEMS = {*PG_COMPETITOR_ROUTES, "clickhouse"}
SYSTEM_LABELS = {
    "auto": "RVBBIT Auto",
    "rvbbit_native": "RVBBIT Native",
    "duck_vector": "RVBBIT Duck Vector",
    "duck_vortex": "RVBBIT Duck Vortex",
    "datafusion_vector": "RVBBIT DataFusion Vector",
    "datafusion_vortex": "RVBBIT DataFusion Vortex",
    "gpu_gqe": "RVBBIT NVIDIA GQE",
    "duckdb": "DuckDB",
    "postgres": "PostgreSQL",
    "citus": "Citus Columnar",
    "hydra": "Hydra Columnar",
    "alloydb": "AlloyDB Omni",
    "clickhouse": "ClickHouse",
}
SESSION_FORMAT = "doomql-session-v1"
REPLAY_SETTING_FIELDS = (
    "world",
    "wad",
    "map_name",
    "grid_scale",
    "table",
    "parquet",
    "width",
    "height",
    "draw_distance",
    "turn_degrees",
    "render_type",
)
SESSION_PATH_FIELDS = {"wad", "parquet"}
SESSION_INTEGER_FIELDS = {
    "grid_scale",
    "width",
    "height",
    "draw_distance",
    "turn_degrees",
}


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


def camera_payload(camera: Camera) -> dict[str, object]:
    return {
        "x": camera.x,
        "y": camera.y,
        "z": camera.z,
        "heading": camera.heading,
        "draw_distance": camera.draw_distance,
        "map_name": camera.map_name,
        "open_doors": list(camera.open_doors),
    }


def write_session(
    path: Path,
    args: argparse.Namespace,
    commands: list[dict[str, Any]],
    cameras: list[Camera],
    queries_run: int,
) -> None:
    settings = {
        "world": args.world,
        "wad": str(args.wad.expanduser().resolve()),
        "map_name": args.map_name,
        "maps": getattr(args, "maps", ",".join(EPISODE_MAPS)),
        "grid_scale": args.grid_scale,
        "table": args.table,
        "parquet": str(args.parquet.expanduser().resolve()),
        "width": args.width,
        "height": args.height,
        "draw_distance": args.draw_distance,
        "turn_degrees": args.turn_degrees,
        "render_type": args.render_type,
        "movement_step": 2,
        "capture_system": args.system,
    }
    document = {
        "format": SESSION_FORMAT,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settings": settings,
        "commands": commands,
        "frames": [camera_payload(camera) for camera in cameras],
        "summary": {
            "commands": len(commands),
            "frames": len(cameras),
            "unique_cameras": len(set(cameras)),
            "blocked_movements": sum(
                bool(command.get("blocked")) for command in commands
            ),
            "interactive_queries": queries_run,
        },
    }
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def load_session(path: Path) -> tuple[dict[str, Any], list[Camera], str]:
    path = path.expanduser()
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid session JSON: {exc}") from exc
    if not isinstance(document, dict) or document.get("format") != SESSION_FORMAT:
        raise ValueError(f"session format must be {SESSION_FORMAT}")
    settings = document.get("settings")
    frames = document.get("frames")
    if not isinstance(settings, dict):
        raise ValueError("session settings must be an object")
    missing = [field for field in REPLAY_SETTING_FIELDS if field not in settings]
    if missing:
        raise ValueError(f"session settings missing: {', '.join(missing)}")
    if not isinstance(frames, list) or not frames:
        raise ValueError("session must contain at least one frame")
    cameras: list[Camera] = []
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise ValueError(f"session frame {index} must be an object")
        try:
            camera = Camera(
                x=int(frame["x"]),
                y=int(frame["y"]),
                z=int(frame["z"]),
                heading=int(frame["heading"]) % 360,
                draw_distance=int(frame["draw_distance"]),
                map_name=(
                    str(frame["map_name"]).upper()
                    if frame.get("map_name") is not None
                    else None
                ),
                open_doors=tuple(
                    sorted({int(door_id) for door_id in frame.get("open_doors", [])})
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"session frame {index} is invalid") from exc
        if camera.draw_distance <= 0:
            raise ValueError(f"session frame {index} has invalid draw distance")
        cameras.append(camera)
    digest = hashlib.sha256(raw).hexdigest()
    return settings, cameras, digest


def apply_session_settings(
    args: argparse.Namespace,
    settings: dict[str, Any],
) -> None:
    for field in REPLAY_SETTING_FIELDS:
        value = settings[field]
        if field in SESSION_PATH_FIELDS:
            value = Path(str(value))
        elif field in SESSION_INTEGER_FIELDS:
            value = int(value)
        elif field == "table":
            value = simple_identifier(str(value))
        else:
            value = str(value)
        setattr(args, field, value)
    if "maps" in settings:
        args.maps = str(settings["maps"])


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
    world: str = "synthetic",
) -> tuple[list[tuple[Any, ...]], float, dict[str, Any]]:
    sql = frame_sql(camera, width=width, height=height, table_expr=table, world=world)
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
    world: str = "synthetic",
    grid_scale: int = DEFAULT_GRID_SCALE,
    render_type: str = "ascii",
    material_color_ramps: dict[int, MaterialColorRamp] | None = None,
    material_textures: dict[int, MaterialTexture] | None = None,
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
            first_sql = frame_sql(
                cameras[0],
                width=width,
                height=height,
                table_expr=table,
                world=world,
            )
            first_doc = route_explain(conn, first_sql)
            available, reason = candidate_available(first_doc, candidate)
            if not available:
                return SystemResult(system, "skip", None, None, None, None, None, None, 0, [], reason)

            for i in range(warmups + len(cameras)):
                camera = cameras[0] if i < warmups else cameras[i - warmups]
                rows, elapsed_ms, doc = execute_pg_frame(
                    conn,
                    camera,
                    table,
                    width,
                    height,
                    world,
                )
                if first_ms is None:
                    first_ms = elapsed_ms
                if i < warmups:
                    continue
                latencies.append(elapsed_ms)
                output_rows += len(rows)
                frame = render_frame(
                    rows,
                    camera,
                    width=width,
                    height=height,
                    world=world,
                    grid_scale=grid_scale,
                    render_type=render_type,
                    material_color_ramps=material_color_ramps,
                    material_textures=material_textures,
                )
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
    world: str = "synthetic",
    grid_scale: int = DEFAULT_GRID_SCALE,
    render_type: str = "ascii",
    material_color_ramps: dict[int, MaterialColorRamp] | None = None,
    material_textures: dict[int, MaterialTexture] | None = None,
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
                    world=world,
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
                hashes.append(
                    frame_hash(
                        render_frame(
                            rows,
                            camera,
                            width=width,
                            height=height,
                            world=world,
                            grid_scale=grid_scale,
                            render_type=render_type,
                            material_color_ramps=material_color_ramps,
                            material_textures=material_textures,
                        )
                    )
                )
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


def run_postgres_system(
    system: str,
    route: str,
    dsn: str,
    cameras: list[Camera],
    table: str,
    width: int,
    height: int,
    warmups: int,
    timeout_s: int,
    world: str = "synthetic",
    grid_scale: int = DEFAULT_GRID_SCALE,
    render_type: str = "ascii",
    material_color_ramps: dict[int, MaterialColorRamp] | None = None,
    material_textures: dict[int, MaterialTexture] | None = None,
) -> SystemResult:
    latencies: list[float] = []
    hashes: list[str] = []
    output_rows = 0
    first_ms = None
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("SELECT set_config('statement_timeout', %s, false)", (f"{timeout_s}s",))
            for i in range(warmups + len(cameras)):
                camera = cameras[0] if i < warmups else cameras[i - warmups]
                sql = frame_sql(
                    camera,
                    width=width,
                    height=height,
                    table_expr=table,
                    world=world,
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
                hashes.append(
                    frame_hash(
                        render_frame(
                            rows,
                            camera,
                            width=width,
                            height=height,
                            world=world,
                            grid_scale=grid_scale,
                            render_type=render_type,
                            material_color_ramps=material_color_ramps,
                            material_textures=material_textures,
                        )
                    )
                )
    except Exception as exc:
        return SystemResult(
            system,
            "fail",
            route,
            "vanilla",
            first_ms,
            None,
            None,
            None,
            output_rows,
            hashes,
            str(exc),
        )
    median_ms = statistics.median(latencies)
    p95_ms = percentile(latencies, 0.95)
    return SystemResult(
        system,
        "ok",
        route,
        "vanilla",
        first_ms,
        median_ms,
        p95_ms,
        1000.0 / median_ms if median_ms else None,
        output_rows,
        hashes,
    )


def run_clickhouse_system(
    host: str,
    port: int,
    cameras: list[Camera],
    table: str,
    width: int,
    height: int,
    warmups: int,
    timeout_s: int,
    world: str = "synthetic",
    grid_scale: int = DEFAULT_GRID_SCALE,
    render_type: str = "ascii",
    material_color_ramps: dict[int, MaterialColorRamp] | None = None,
    material_textures: dict[int, MaterialTexture] | None = None,
) -> SystemResult:
    latencies: list[float] = []
    hashes: list[str] = []
    output_rows = 0
    first_ms = None
    try:
        client = clickhouse_connect.get_client(host=host, port=port)
        for i in range(warmups + len(cameras)):
            camera = cameras[0] if i < warmups else cameras[i - warmups]
            sql = frame_sql(
                camera,
                width=width,
                height=height,
                table_expr=table,
                dialect="clickhouse",
                world=world,
            )
            started = time.perf_counter()
            rows = client.query(
                sql,
                settings={"max_execution_time": timeout_s},
            ).result_rows
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if first_ms is None:
                first_ms = elapsed_ms
            if i < warmups:
                continue
            latencies.append(elapsed_ms)
            output_rows += len(rows)
            hashes.append(
                frame_hash(
                    render_frame(
                        rows,
                        camera,
                        width=width,
                        height=height,
                        world=world,
                        grid_scale=grid_scale,
                        render_type=render_type,
                        material_color_ramps=material_color_ramps,
                        material_textures=material_textures,
                    )
                )
            )
    except Exception as exc:
        return SystemResult(
            "clickhouse",
            "fail",
            "clickhouse_mergetree",
            "vanilla",
            first_ms,
            None,
            None,
            None,
            output_rows,
            hashes,
            str(exc),
        )
    median_ms = statistics.median(latencies)
    p95_ms = percentile(latencies, 0.95)
    return SystemResult(
        "clickhouse",
        "ok",
        "clickhouse_mergetree",
        "vanilla",
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


def system_label(system: str) -> str:
    return SYSTEM_LABELS.get(system, system)


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
    system_width = max(22, *(len(system_label(result.system)) for result in results))
    print(
        f"{'system':<{system_width}} {'status':<7} {'route':<19} "
        f"{'cold':>10} {'median':>10} {'p95':>10} {'fps':>8}  parity"
    )
    reference = next((result for result in results if result.system == parity_reference), None)
    baseline = reference.frame_hashes if reference is not None else None
    for result in results:
        parity = "-"
        if result.status in {"ok", "mismatch"} and baseline is not None:
            parity = "ok" if result.frame_hashes == baseline else "MISMATCH"
        fps = f"{result.fps:.2f}" if result.fps is not None else "-"
        print(
            f"{system_label(result.system):<{system_width}} "
            f"{result.status:<7} {(result.route or '-'):<19} "
            f"{fmt_ms(result.first_ms):>10} {fmt_ms(result.median_ms):>10} "
            f"{fmt_ms(result.p95_ms):>10} {fps:>8}  {parity}"
        )
        if result.error:
            print(f"  {result.error.splitlines()[0][:180]}")


def collect_environment(
    dsn: str,
    table: str,
    parquet: Path,
    postgres_dsn: str,
    citus_dsn: str,
    hydra_dsn: str,
    alloydb_dsn: str,
    clickhouse_host: str,
    clickhouse_port: int,
    systems: set[str],
) -> dict[str, Any]:
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
    pg_competitors = {
        "postgres": postgres_dsn,
        "citus": citus_dsn,
        "hydra": hydra_dsn,
        "alloydb": alloydb_dsn,
    }
    for system, competitor_dsn in pg_competitors.items():
        if system not in systems:
            continue
        try:
            with psycopg.connect(competitor_dsn, autocommit=True) as conn:
                row = conn.execute(
                    f"""
                    SELECT version(), count(*)::bigint,
                           pg_total_relation_size(%s::regclass)::bigint,
                           current_setting('shared_buffers'),
                           current_setting('work_mem'),
                           current_setting('max_parallel_workers_per_gather'),
                           (SELECT am.amname
                            FROM pg_class c
                            JOIN pg_am am ON am.oid = c.relam
                            WHERE c.oid = %s::regclass)
                    FROM {table}
                    """,
                    (table, table),
                ).fetchone()
                details = dict(
                    zip(
                        (
                            "version",
                            "rows",
                            "bytes",
                            "shared_buffers",
                            "work_mem",
                            "max_parallel_workers_per_gather",
                            "access_method",
                        ),
                        row,
                    )
                )
                environment[system] = details
                if system == "postgres":
                    environment["vanilla_postgres"] = details
        except Exception as exc:
            environment[f"{system}_probe_error"] = str(exc)
    if "clickhouse" in systems:
        try:
            client = clickhouse_connect.get_client(host=clickhouse_host, port=clickhouse_port)
            version = client.query("SELECT version()").result_rows[0][0]
            rows = client.query(f"SELECT count(*) FROM {table}").result_rows[0][0]
            size_bytes = client.query(
                "SELECT coalesce(sum(bytes_on_disk), 0) FROM system.parts "
                f"WHERE active AND database = currentDatabase() AND table = '{table}'"
            ).result_rows[0][0]
            table_meta = client.query(
                "SELECT engine, sorting_key FROM system.tables "
                f"WHERE database = currentDatabase() AND name = '{table}'"
            ).result_rows[0]
            environment["clickhouse"] = {
                "version": version,
                "rows": int(rows),
                "bytes": int(size_bytes),
                "engine": table_meta[0],
                "sorting_key": table_meta[1],
            }
        except Exception as exc:
            environment["clickhouse_probe_error"] = str(exc)
    return environment


def render_once(
    dsn: str,
    system: str,
    camera: Camera,
    table: str,
    width: int,
    height: int,
    timeout_s: int,
    world: str = "synthetic",
    grid_scale: int = DEFAULT_GRID_SCALE,
    render_type: str = "ascii",
    material_color_ramps: dict[int, MaterialColorRamp] | None = None,
    material_textures: dict[int, MaterialTexture] | None = None,
) -> tuple[str, float, str]:
    candidate = CANDIDATES[system]
    with psycopg.connect(dsn, autocommit=True) as conn:
        pg_session(conn, candidate, timeout_s)
        rows, elapsed_ms, doc = execute_pg_frame(
            conn,
            camera,
            table,
            width,
            height,
            world,
        )
    frame = render_frame(
        rows,
        camera,
        width=width,
        height=height,
        world=world,
        grid_scale=grid_scale,
        render_type=render_type,
        material_color_ramps=material_color_ramps,
        material_textures=material_textures,
    )
    route = str(doc.get("chosen_candidate") or doc.get("route") or system)
    return frame, elapsed_ms, route


def render_selected_once(
    args: argparse.Namespace,
    system: str,
    camera: Camera,
    material_color_ramps: dict[int, MaterialColorRamp] | None = None,
    material_textures: dict[int, MaterialTexture] | None = None,
) -> tuple[str, float, str]:
    if system in CANDIDATES:
        return render_once(
            args.dsn,
            system,
            camera,
            args.table,
            args.width,
            args.height,
            args.timeout,
            args.world,
            args.grid_scale,
            args.render_type,
            material_color_ramps,
            material_textures,
        )
    pg_dsns = {
        "postgres": args.postgres_dsn,
        "citus": args.citus_dsn,
        "hydra": args.hydra_dsn,
        "alloydb": args.alloydb_dsn,
    }
    if system in PG_COMPETITOR_ROUTES:
        with psycopg.connect(pg_dsns[system], autocommit=True) as conn:
            sql = frame_sql(
                camera,
                width=args.width,
                height=args.height,
                table_expr=args.table,
                world=args.world,
            )
            started = time.perf_counter()
            rows = conn.execute(sql).fetchall()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        frame = render_frame(
            rows,
            camera,
            width=args.width,
            height=args.height,
            world=args.world,
            grid_scale=args.grid_scale,
            render_type=args.render_type,
            material_color_ramps=material_color_ramps,
            material_textures=material_textures,
        )
        return frame, elapsed_ms, PG_COMPETITOR_ROUTES[system]
    if system == "clickhouse":
        client = clickhouse_connect.get_client(
            host=args.clickhouse_host,
            port=args.clickhouse_port,
        )
        sql = frame_sql(
            camera,
            width=args.width,
            height=args.height,
            table_expr=args.table,
            dialect="clickhouse",
            world=args.world,
        )
        started = time.perf_counter()
        rows = client.query(sql).result_rows
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        frame = render_frame(
            rows,
            camera,
            width=args.width,
            height=args.height,
            world=args.world,
            grid_scale=args.grid_scale,
            render_type=args.render_type,
            material_color_ramps=material_color_ramps,
            material_textures=material_textures,
        )
        return frame, elapsed_ms, "clickhouse_mergetree"
    escaped = str(args.parquet).replace("'", "''")
    with duckdb.connect(":memory:") as conn:
        sql = frame_sql(
            camera,
            width=args.width,
            height=args.height,
            table_expr=f"read_parquet('{escaped}')",
            dialect="duckdb",
            world=args.world,
        )
        started = time.perf_counter()
        rows = conn.execute(sql).fetchall()
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    frame = render_frame(
        rows,
        camera,
        width=args.width,
        height=args.height,
        world=args.world,
        grid_scale=args.grid_scale,
        render_type=args.render_type,
        material_color_ramps=material_color_ramps,
        material_textures=material_textures,
    )
    return frame, elapsed_ms, "duckdb"


def move_camera(
    camera: Camera,
    amount: int,
    world_map: RasterizedWorld | None,
) -> Camera:
    if world_map is None:
        return camera.moved(amount)
    direction_x, direction_y = camera_vector(camera.heading)
    target_x = round(camera.x + direction_x * amount / CAMERA_VECTOR_SCALE)
    target_y = round(camera.y + direction_y * amount / CAMERA_VECTOR_SCALE)
    moved = world_map.try_move(
        camera.x,
        camera.y,
        target_x,
        target_y,
        frozenset(camera.open_doors),
    )
    if moved is None:
        return camera
    x, y, z = moved
    return Camera(
        x,
        y,
        z,
        camera.heading,
        camera.draw_distance,
        camera.map_name,
        camera.open_doors,
    )


def strafe_camera(
    camera: Camera,
    amount: int,
    world_map: RasterizedWorld | None,
) -> Camera:
    sideways = camera.turned(90)
    moved = move_camera(sideways, amount, world_map)
    return Camera(
        moved.x,
        moved.y,
        moved.z,
        camera.heading,
        camera.draw_distance,
        camera.map_name,
        camera.open_doors,
    )


def e1m1_scripted_cameras(
    world_map: RasterizedWorld,
    frames: int,
    draw_distance: int,
) -> list[Camera]:
    if frames <= 0:
        raise ValueError("frames must be positive")
    start = Camera(*world_map.player_camera(draw_distance))
    offsets = (0, -5, 5, -15, 15, -30, 30, -45, 45, -60, 60, 90)
    keyframes = []
    walker = start
    for offset in offsets:
        keyframes.append(walker.turned(offset))
        walker = move_camera(walker, 2, world_map)
    return [keyframes[index % len(keyframes)] for index in range(frames)]


def episode_scripted_cameras(
    world_maps: dict[str, RasterizedWorld],
    frames: int,
    draw_distance: int,
) -> list[Camera]:
    if frames <= 0:
        raise ValueError("frames must be positive")
    map_names = tuple(world_maps)
    walkers = {
        map_name: Camera(
            *world_maps[map_name].player_camera(draw_distance),
            map_name=map_name,
        )
        for map_name in map_names
    }
    offsets = (0, -15, 15, -30, 30, -45, 45, 90)
    cameras = []
    for index in range(frames):
        map_name = map_names[index % len(map_names)]
        walker = walkers[map_name]
        cameras.append(walker.turned(offsets[index % len(offsets)]))
        walkers[map_name] = move_camera(walker, 2, world_maps[map_name])
    return cameras


def _active_world(
    camera: Camera,
    world_maps: dict[str, RasterizedWorld] | None,
) -> RasterizedWorld | None:
    if world_maps is None:
        return None
    if camera.map_name is not None:
        return world_maps[camera.map_name]
    return next(iter(world_maps.values()))


def interactive(
    args: argparse.Namespace,
    world_maps: dict[str, RasterizedWorld] | None = None,
) -> int:
    if args.system not in CANDIDATES:
        raise SystemExit("--interactive requires a PostgreSQL-backed --system")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("--interactive requires a terminal")
    first_map_name = next(iter(world_maps)) if world_maps is not None else None
    first_world = world_maps[first_map_name] if first_map_name is not None else None
    camera = (
        Camera(
            *first_world.player_camera(args.draw_distance),
            map_name=first_map_name if args.world == "episode1" else None,
        )
        if first_world is not None
        else Camera(draw_distance=args.draw_distance)
    )
    map_names = tuple(world_maps) if args.world == "episode1" and world_maps else ()
    map_door_states: dict[str, tuple[int, ...]] = {
        map_name: () for map_name in map_names
    }
    old_settings = termios.tcgetattr(sys.stdin)
    queries_run = 0
    session_commands: list[dict[str, Any]] = []
    session_cameras = [camera]
    try:
        tty.setcbreak(sys.stdin.fileno())
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            pg_session(conn, CANDIDATES[args.system], args.timeout)
            while True:
                world_map = _active_world(camera, world_maps)
                rows, elapsed_ms, doc = execute_pg_frame(
                    conn,
                    camera,
                    args.table,
                    args.width,
                    args.height,
                    args.world,
                )
                queries_run += 1
                frame = render_frame(
                    rows,
                    camera,
                    width=args.width,
                    height=args.height,
                    world=args.world,
                    grid_scale=args.grid_scale,
                    render_type=args.render_type,
                    material_color_ramps=(
                        world_map.material_color_ramps
                        if world_map is not None
                        else None
                    ),
                    material_textures=(
                        world_map.material_textures
                        if world_map is not None and args.world == "episode1"
                        else None
                    ),
                )
                route = str(doc.get("chosen_candidate") or doc.get("route") or args.system)
                header = (
                    f"DOOMQL  {route}  {elapsed_ms:.1f}ms  {1000.0 / elapsed_ms:.2f}fps  "
                    f"queries={queries_run}  "
                    f"map={camera.map_name or args.map_name} doors={len(camera.open_doors)}  "
                    f"camera=({camera.x},{camera.y},{camera.z}) heading={camera.heading}deg  "
                    f"hash={frame_hash(frame)}"
                )
                print("\x1b[2J\x1b[H" + header[: args.width] + "\n" + frame, end="", flush=True)
                ready, _, _ = select.select([sys.stdin], [], [], 0.25)
                if not ready:
                    continue
                key = sys.stdin.read(1).lower()
                before = camera
                action = "ignored"
                adds_frame = False
                if key == "w":
                    action = "forward"
                    camera = move_camera(camera, 2, world_map)
                    adds_frame = True
                elif key == "s":
                    action = "backward"
                    camera = move_camera(camera, -2, world_map)
                    adds_frame = True
                elif key == "a":
                    action = "turn_left"
                    camera = camera.turned(args.turn_degrees)
                    adds_frame = True
                elif key == "d":
                    action = "turn_right"
                    camera = camera.turned(-args.turn_degrees)
                    adds_frame = True
                elif key == "z":
                    action = "strafe_left"
                    camera = strafe_camera(camera, 2, world_map)
                    adds_frame = True
                elif key == "c":
                    action = "strafe_right"
                    camera = strafe_camera(camera, -2, world_map)
                    adds_frame = True
                elif key == " " and world_map is not None and args.world == "episode1":
                    action = "toggle_door"
                    door_id = world_map.nearest_door(camera.x, camera.y)
                    if door_id is not None:
                        open_doors = set(camera.open_doors)
                        if door_id in open_doors:
                            open_doors.remove(door_id)
                        else:
                            open_doors.add(door_id)
                        camera = Camera(
                            camera.x,
                            camera.y,
                            camera.z,
                            camera.heading,
                            camera.draw_distance,
                            camera.map_name,
                            tuple(sorted(open_doors)),
                        )
                        if camera.map_name is not None:
                            map_door_states[camera.map_name] = camera.open_doors
                    adds_frame = True
                elif key in {"[", "]"} and map_names:
                    action = "previous_map" if key == "[" else "next_map"
                    assert camera.map_name is not None
                    map_door_states[camera.map_name] = camera.open_doors
                    offset = -1 if key == "[" else 1
                    map_index = (map_names.index(camera.map_name) + offset) % len(map_names)
                    next_map_name = map_names[map_index]
                    next_world = world_maps[next_map_name]
                    camera = Camera(
                        *next_world.player_camera(args.draw_distance),
                        map_name=next_map_name,
                        open_doors=map_door_states[next_map_name],
                    )
                    adds_frame = True
                elif key in {"q", "\x03"}:
                    action = "quit"
                session_commands.append(
                    {
                        "index": len(session_commands),
                        "key": key,
                        "action": action,
                        "before": camera_payload(before),
                        "after": camera_payload(camera),
                        "blocked": adds_frame and camera == before,
                    }
                )
                if adds_frame:
                    session_cameras.append(camera)
                if action == "quit":
                    break
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        print()
        if args.record_session is not None:
            write_session(
                args.record_session,
                args,
                session_commands,
                session_cameras,
                queries_run,
            )
            print(f"Wrote session {args.record_session.expanduser()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--postgres-dsn", default=DEFAULT_POSTGRES_DSN)
    parser.add_argument("--citus-dsn", default=DEFAULT_CITUS_DSN)
    parser.add_argument("--hydra-dsn", default=DEFAULT_HYDRA_DSN)
    parser.add_argument("--alloydb-dsn", default=DEFAULT_ALLOYDB_DSN)
    parser.add_argument("--clickhouse-host", default=DEFAULT_CLICKHOUSE_HOST)
    parser.add_argument("--clickhouse-port", type=int, default=DEFAULT_CLICKHOUSE_PORT)
    parser.add_argument("--table", type=simple_identifier, default="doomql_world")
    parser.add_argument("--world", choices=sorted(WORLD_COLUMNS), default="synthetic")
    parser.add_argument("--wad", type=Path, default=DEFAULT_WAD)
    parser.add_argument("--map-name", default="E1M1")
    parser.add_argument("--maps", default=",".join(EPISODE_MAPS))
    parser.add_argument("--grid-scale", type=int, default=DEFAULT_GRID_SCALE)
    parser.add_argument("--parquet", type=Path)
    parser.add_argument("--systems", default=DEFAULT_SYSTEMS)
    parser.add_argument("--system", default="auto", choices=sorted(CANDIDATES))
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--height", type=int, default=40)
    parser.add_argument("--draw-distance", type=int, default=128)
    parser.add_argument("--turn-degrees", type=int, default=15)
    parser.add_argument("--render-type", choices=sorted(RENDER_TYPES), default="ascii")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--record-session", type=Path)
    parser.add_argument("--replay-session", type=Path)
    parser.add_argument("--replay-table", type=simple_identifier)
    parser.add_argument("--replay-parquet", type=Path)
    parser.add_argument("--output", type=Path, default=HERE / "results" / "last_run.json")
    args = parser.parse_args()
    if args.record_session is not None and args.replay_session is not None:
        parser.error("--record-session and --replay-session cannot be combined")
    if args.record_session is not None and not args.interactive:
        parser.error("--record-session requires --interactive")
    if args.replay_session is not None and args.interactive:
        parser.error("--replay-session is headless and cannot use --interactive")
    if args.replay_session is None and (
        args.replay_table is not None or args.replay_parquet is not None
    ):
        parser.error("--replay-table and --replay-parquet require --replay-session")
    replay_cameras: list[Camera] | None = None
    replay_digest = None
    if args.replay_session is not None:
        try:
            replay_settings, replay_cameras, replay_digest = load_session(
                args.replay_session
            )
            apply_session_settings(args, replay_settings)
            if args.replay_table is not None:
                args.table = args.replay_table
            if args.replay_parquet is not None:
                args.parquet = args.replay_parquet
        except (OSError, TypeError, ValueError) as exc:
            parser.error(str(exc))
    if args.parquet is None:
        parser.error("--parquet is required unless supplied by --replay-session")
    if args.world not in WORLD_COLUMNS:
        parser.error(f"unsupported session world: {args.world}")
    if args.render_type not in RENDER_TYPES:
        parser.error(f"unsupported session render type: {args.render_type}")
    if not 1 <= args.turn_degrees <= 90:
        parser.error("--turn-degrees must be between 1 and 90")
    if args.grid_scale <= 0:
        parser.error("--grid-scale must be positive")
    if args.width <= 0 or args.height <= 0 or args.draw_distance <= 0:
        parser.error("width, height, and draw distance must be positive")
    world_map = None
    world_maps: dict[str, RasterizedWorld] | None = None
    if args.world in {"e1m1", "episode1"}:
        wad_path = args.wad.expanduser()
        if not wad_path.exists():
            parser.error(f"missing WAD: {wad_path}")
        try:
            if args.world == "episode1":
                map_names = tuple(
                    dict.fromkeys(
                        name.strip().upper()
                        for name in args.maps.split(",")
                        if name.strip()
                    )
                )
                unknown_maps = sorted(set(map_names) - set(EPISODE_MAPS))
                if not map_names:
                    parser.error("--maps must contain at least one map")
                if unknown_maps:
                    parser.error(f"unsupported Episode 1 maps: {', '.join(unknown_maps)}")
                world_maps = rasterize_episode(wad_path, map_names, args.grid_scale)
                if replay_cameras is not None:
                    missing_maps = sorted(
                        {
                            camera.map_name
                            for camera in replay_cameras
                            if camera.map_name not in world_maps
                        },
                        key=str,
                    )
                    if missing_maps:
                        parser.error(
                            "replay references maps outside --maps: "
                            + ", ".join(str(name) for name in missing_maps)
                        )
            else:
                world_map = rasterize_map(
                    read_wad_map(wad_path, args.map_name),
                    args.grid_scale,
                )
                world_maps = {args.map_name.upper(): world_map}
        except ValueError as exc:
            parser.error(str(exc))
    if args.interactive:
        return interactive(args, world_maps)

    systems = [item.strip() for item in args.systems.split(",") if item.strip()]
    unknown = sorted(set(systems) - set(CANDIDATES) - VANILLA_SYSTEMS - {"duckdb"})
    if unknown:
        parser.error(f"unknown systems: {', '.join(unknown)}")
    if replay_cameras is not None:
        cameras = replay_cameras
    else:
        if args.world == "episode1":
            assert world_maps is not None
            cameras = episode_scripted_cameras(
                world_maps,
                args.frames,
                args.draw_distance,
            )
        elif world_map is not None:
            cameras = e1m1_scripted_cameras(
                world_map,
                args.frames,
                args.draw_distance,
            )
        else:
            cameras = scripted_cameras(args.frames, args.draw_distance)
    material_color_ramps = (
        {
            material: ramp
            for current_world in world_maps.values()
            for material, ramp in current_world.material_color_ramps.items()
        }
        if world_maps is not None
        else None
    )
    material_textures = (
        {
            material: texture
            for current_world in world_maps.values()
            for material, texture in current_world.material_textures.items()
        }
        if world_maps is not None and args.world == "episode1"
        else None
    )
    pg_competitor_dsns = {
        "postgres": args.postgres_dsn,
        "citus": args.citus_dsn,
        "hydra": args.hydra_dsn,
        "alloydb": args.alloydb_dsn,
    }
    results: list[SystemResult] = []
    for system in systems:
        print(f"Running {system}...", flush=True)
        if system == "duckdb":
            result = run_duckdb_system(
                args.parquet,
                cameras,
                args.width,
                args.height,
                args.warmups,
                args.world,
                args.grid_scale,
                args.render_type,
                material_color_ramps,
                material_textures,
            )
        elif system in PG_COMPETITOR_ROUTES:
            result = run_postgres_system(
                system,
                PG_COMPETITOR_ROUTES[system],
                pg_competitor_dsns[system],
                cameras,
                args.table,
                args.width,
                args.height,
                args.warmups,
                args.timeout,
                args.world,
                args.grid_scale,
                args.render_type,
                material_color_ramps,
                material_textures,
            )
        elif system == "clickhouse":
            result = run_clickhouse_system(
                args.clickhouse_host,
                args.clickhouse_port,
                cameras,
                args.table,
                args.width,
                args.height,
                args.warmups,
                args.timeout,
                args.world,
                args.grid_scale,
                args.render_type,
                material_color_ramps,
                material_textures,
            )
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
                args.world,
                args.grid_scale,
                args.render_type,
                material_color_ramps,
                material_textures,
            )
        results.append(result)
    parity_reference = enforce_parity(results)
    print_results(results, parity_reference)

    if args.render:
        successful = next((result for result in results if result.status == "ok"), None)
        if successful:
            frame, elapsed_ms, route = render_selected_once(
                args,
                successful.system,
                cameras[0],
                material_color_ramps,
                material_textures,
            )
            print(f"\nDOOMQL | {route} | {elapsed_ms:.1f}ms | hash {frame_hash(frame)}")
            print(frame)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dsn": args.dsn.rsplit("@", 1)[-1],
        "postgres_dsn": args.postgres_dsn.rsplit("@", 1)[-1],
        "citus_dsn": args.citus_dsn.rsplit("@", 1)[-1],
        "hydra_dsn": args.hydra_dsn.rsplit("@", 1)[-1],
        "alloydb_dsn": args.alloydb_dsn.rsplit("@", 1)[-1],
        "clickhouse": f"{args.clickhouse_host}:{args.clickhouse_port}",
        "table": args.table,
        "world": args.world,
        "wad": str(args.wad.expanduser()) if args.world in {"e1m1", "episode1"} else None,
        "map_name": args.map_name if args.world == "e1m1" else None,
        "maps": list(world_maps) if args.world == "episode1" and world_maps else None,
        "grid_scale": args.grid_scale if args.world in {"e1m1", "episode1"} else None,
        "parquet": str(args.parquet),
        "frames": len(cameras),
        "replay_session": (
            str(args.replay_session.expanduser())
            if args.replay_session is not None
            else None
        ),
        "replay_session_sha256": replay_digest,
        "width": args.width,
        "height": args.height,
        "draw_distance": args.draw_distance,
        "turn_degrees": args.turn_degrees,
        "render_type": args.render_type,
        "warmups": args.warmups,
        "parity_reference": parity_reference,
        "environment": collect_environment(
            args.dsn,
            args.table,
            args.parquet,
            args.postgres_dsn,
            args.citus_dsn,
            args.hydra_dsn,
            args.alloydb_dsn,
            args.clickhouse_host,
            args.clickhouse_port,
            set(systems),
        ),
        "results": [asdict(result) for result in results],
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {args.output}")
    return 0 if all(result.status in {"ok", "skip"} for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
