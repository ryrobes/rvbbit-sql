#!/usr/bin/env python3
"""Benchmark or explore the QuakeQL BSP SQL renderer."""

from __future__ import annotations

import argparse
import json
import math
import os
import select
import statistics
import sys
import termios
import time
import tty
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import duckdb
import psycopg

try:
    from .load import DEFAULT_DSN, simple_identifier
    from .pak_bsp import BrushEntity, ClipNode, CollisionWorld, Model, Plane
    from .workload import (
        BRUSH_COLUMNS,
        CLIPNODE_COLUMNS,
        MAP_COLUMNS,
        MODEL_COLUMNS,
        PLANE_COLUMNS,
    )
    from .workload import (
        RENDER_TYPES,
        Camera,
        frame_hash,
        frame_sql,
        render_frame,
        scripted_cameras,
        sql_texture_frame_sql,
    )
except ImportError:
    from load import DEFAULT_DSN, simple_identifier
    from pak_bsp import BrushEntity, ClipNode, CollisionWorld, Model, Plane
    from workload import (
        BRUSH_COLUMNS,
        CLIPNODE_COLUMNS,
        MAP_COLUMNS,
        MODEL_COLUMNS,
        PLANE_COLUMNS,
    )
    from workload import (
        RENDER_TYPES,
        Camera,
        frame_hash,
        frame_sql,
        render_frame,
        scripted_cameras,
        sql_texture_frame_sql,
    )


HERE = Path(__file__).resolve().parent
DEFAULT_POSTGRES_DSN = os.environ.get(
    "QUAKEQL_POSTGRES_DSN",
    "postgresql://postgres:rvbbit@localhost:55432/bench",
)
DEFAULT_PARQUET = HERE / "data" / "quakeql_e1m1_5000000.parquet"
DEFAULT_TEXTURE_PARQUET = HERE / "data" / "quakeql_e1m1_5000000_texels.parquet"
DEFAULT_LIGHTMAP_PARQUET = HERE / "data" / "quakeql_e1m1_5000000_lightmaps.parquet"
DEFAULT_MATERIAL_PARQUET = HERE / "data" / "quakeql_e1m1_5000000_materials.parquet"
DEFAULT_COLORMAP_PARQUET = HERE / "data" / "quakeql_colormap.parquet"
DEFAULT_EPISODE_PARQUET = HERE / "data" / "quakeql_episode1_natural.parquet"
DEFAULT_EPISODE_TEXTURE_PARQUET = HERE / "data" / "quakeql_episode1_natural_texels.parquet"
DEFAULT_EPISODE_LIGHTMAP_PARQUET = HERE / "data" / "quakeql_episode1_natural_lightmaps.parquet"
DEFAULT_EPISODE_MATERIAL_PARQUET = HERE / "data" / "quakeql_episode1_natural_materials.parquet"


def runtime_parquet_defaults(geometry: Path) -> dict[str, Path]:
    return {
        name: geometry.with_name(f"{geometry.stem}_{name}.parquet")
        for name in ("maps", "planes", "clipnodes", "models", "brushes")
    }


CANDIDATES = {
    "auto": None,
    "rvbbit_native": "rvbbit_native",
    "duck_vector": "duck_vector",
    "duck_vortex": "duck_vortex",
    "datafusion_vector": "datafusion_vector",
    "datafusion_vortex": "datafusion_vortex",
}
SYSTEM_LABELS = {
    "auto": "RVBBIT Auto",
    "rvbbit_native": "RVBBIT Native",
    "duck_vector": "RVBBIT Duck Vector",
    "duck_vortex": "RVBBIT Duck Vortex",
    "datafusion_vector": "RVBBIT DataFusion Vector",
    "datafusion_vortex": "RVBBIT DataFusion Vortex",
    "duckdb": "DuckDB",
    "postgres": "PostgreSQL",
}
DEFAULT_SYSTEMS = ",".join((*CANDIDATES, "duckdb"))


@dataclass
class SystemResult:
    system: str
    status: str
    route: str | None
    cold_ms: float | None
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


def pixel_height(height: int, render_type: str) -> int:
    return height * 2 if render_type == "ansi-half" else height


def pg_session(conn: psycopg.Connection[Any], candidate: str | None, timeout_s: int) -> None:
    conn.execute("SELECT set_config('statement_timeout', %s, false)", (f"{timeout_s}s",))
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


def route_name(doc: dict[str, Any], fallback: str) -> str:
    return str(doc.get("chosen_candidate") or doc.get("route") or fallback)


def render_rows(rows: list[tuple[Any, ...]], camera: Camera, args: argparse.Namespace) -> str:
    return render_frame(
        rows,
        camera,
        width=args.width,
        height=args.height,
        render_type=args.render_type,
        sample_step=args.sample_step,
        brush_sample_step=args.brush_sample_step,
        splat_cap=0 if args.renderer == "sql-texture" else args.splat_cap,
    )


def frame_query(
    camera: Camera,
    args: argparse.Namespace,
    table_expr: str,
    texture_expr: str,
    lightmap_expr: str | None = None,
    material_expr: str | None = None,
    colormap_expr: str | None = None,
    animation_time: float = 0.0,
    brush_offsets: Sequence[tuple[int, float, float, float]] = (),
) -> str:
    common = {
        "width": args.width,
        "pixel_height": pixel_height(args.height, args.render_type),
        "table_expr": table_expr,
        "layers": args.layers,
    }
    if args.renderer == "samples":
        return frame_sql(camera, **common)
    return sql_texture_frame_sql(
        camera,
        **common,
        texture_expr=texture_expr,
        lightmap_expr=lightmap_expr,
        material_expr=material_expr,
        colormap_expr=colormap_expr,
        splat_cap=args.splat_cap,
        sample_step=args.sample_step,
        brush_sample_step=args.brush_sample_step,
        mip_bias=args.mip_bias,
        animation_time=animation_time,
        brush_offsets=brush_offsets,
    )


def frame_time(index: int, warmups: int, animation_step: float) -> float:
    return max(0, index - warmups) * animation_step


def postgres_frame_query(
    camera: Camera,
    args: argparse.Namespace,
    animation_time: float = 0.0,
    brush_offsets: Sequence[tuple[int, float, float, float]] = (),
) -> str:
    return frame_query(
        camera,
        args,
        args.table,
        args.texture_table,
        args.lightmap_table,
        args.material_table,
        args.colormap_table,
        animation_time,
        brush_offsets,
    )


def duck_relation(path: Path) -> str:
    escaped = str(path).replace("'", "''")
    return f"read_parquet('{escaped}')"


def complete_result(
    system: str,
    route: str,
    cold_ms: float | None,
    latencies: list[float],
    output_rows: int,
    hashes: list[str],
) -> SystemResult:
    median_ms = statistics.median(latencies)
    p95_ms = percentile(latencies, 0.95)
    return SystemResult(
        system,
        "ok",
        route,
        cold_ms,
        median_ms,
        p95_ms,
        1000.0 / median_ms if median_ms else None,
        output_rows,
        hashes,
    )


def run_rvbbit(system: str, cameras: list[Camera], args: argparse.Namespace) -> SystemResult:
    candidate = CANDIDATES[system]
    latencies: list[float] = []
    hashes: list[str] = []
    output_rows = 0
    cold_ms = None
    route = system
    try:
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            pg_session(conn, candidate, args.timeout)
            first_sql = postgres_frame_query(cameras[0], args)
            doc = route_explain(conn, first_sql)
            available, reason = candidate_available(doc, candidate)
            if not available:
                return SystemResult(system, "skip", None, None, None, None, None, 0, [], reason)
            route = route_name(doc, system)
            for index in range(args.warmups + len(cameras)):
                camera = cameras[0] if index < args.warmups else cameras[index - args.warmups]
                sql = postgres_frame_query(
                    camera,
                    args,
                    frame_time(index, args.warmups, args.animation_step),
                )
                started = time.perf_counter()
                rows = conn.execute(sql).fetchall()
                elapsed = (time.perf_counter() - started) * 1000.0
                if cold_ms is None:
                    cold_ms = elapsed
                if index < args.warmups:
                    continue
                latencies.append(elapsed)
                output_rows += len(rows)
                hashes.append(frame_hash(render_rows(rows, camera, args)))
    except Exception as exc:
        return SystemResult(
            system, "fail", route, cold_ms, None, None, None, output_rows, hashes, str(exc)
        )
    return complete_result(system, route, cold_ms, latencies, output_rows, hashes)


def run_duckdb(cameras: list[Camera], args: argparse.Namespace) -> SystemResult:
    if not args.parquet.exists():
        return SystemResult(
            "duckdb", "skip", "duckdb", None, None, None, None, 0, [], f"missing {args.parquet}"
        )
    table_expr = duck_relation(args.parquet)
    texture_expr = duck_relation(args.texture_parquet)
    lightmap_expr = duck_relation(args.lightmap_parquet)
    material_expr = duck_relation(args.material_parquet)
    colormap_expr = duck_relation(args.colormap_parquet)
    required = (
        args.texture_parquet,
        args.lightmap_parquet,
        args.material_parquet,
        args.colormap_parquet,
    )
    missing = next((path for path in required if not path.exists()), None)
    if args.renderer == "sql-texture" and missing is not None:
        return SystemResult(
            "duckdb",
            "skip",
            "duckdb",
            None,
            None,
            None,
            None,
            0,
            [],
            f"missing {missing}",
        )
    latencies: list[float] = []
    hashes: list[str] = []
    output_rows = 0
    cold_ms = None
    try:
        with duckdb.connect(":memory:") as conn:
            for index in range(args.warmups + len(cameras)):
                camera = cameras[0] if index < args.warmups else cameras[index - args.warmups]
                sql = frame_query(
                    camera,
                    args,
                    table_expr,
                    texture_expr,
                    lightmap_expr,
                    material_expr,
                    colormap_expr,
                    frame_time(index, args.warmups, args.animation_step),
                )
                started = time.perf_counter()
                rows = conn.execute(sql).fetchall()
                elapsed = (time.perf_counter() - started) * 1000.0
                if cold_ms is None:
                    cold_ms = elapsed
                if index < args.warmups:
                    continue
                latencies.append(elapsed)
                output_rows += len(rows)
                hashes.append(frame_hash(render_rows(rows, camera, args)))
    except Exception as exc:
        return SystemResult(
            "duckdb", "fail", "duckdb", cold_ms, None, None, None, output_rows, hashes, str(exc)
        )
    return complete_result("duckdb", "duckdb", cold_ms, latencies, output_rows, hashes)


def run_postgres(cameras: list[Camera], args: argparse.Namespace) -> SystemResult:
    latencies: list[float] = []
    hashes: list[str] = []
    output_rows = 0
    cold_ms = None
    try:
        with psycopg.connect(args.postgres_dsn, autocommit=True) as conn:
            conn.execute("SELECT set_config('statement_timeout', %s, false)", (f"{args.timeout}s",))
            for index in range(args.warmups + len(cameras)):
                camera = cameras[0] if index < args.warmups else cameras[index - args.warmups]
                sql = postgres_frame_query(
                    camera,
                    args,
                    frame_time(index, args.warmups, args.animation_step),
                )
                started = time.perf_counter()
                rows = conn.execute(sql).fetchall()
                elapsed = (time.perf_counter() - started) * 1000.0
                if cold_ms is None:
                    cold_ms = elapsed
                if index < args.warmups:
                    continue
                latencies.append(elapsed)
                output_rows += len(rows)
                hashes.append(frame_hash(render_rows(rows, camera, args)))
    except Exception as exc:
        return SystemResult(
            "postgres",
            "fail",
            "postgres_heap",
            cold_ms,
            None,
            None,
            None,
            output_rows,
            hashes,
            str(exc),
        )
    return complete_result("postgres", "postgres_heap", cold_ms, latencies, output_rows, hashes)


def print_results(results: list[SystemResult], renderer: str = "samples") -> None:
    reference = parity_reference(results)
    print(f"QuakeQL BSP surface projection benchmark [{renderer}]")
    print(
        f"{'system':<28} {'status':<7} {'route':<24} {'cold':>9} {'median':>10} {'p95':>10} {'fps':>8}  parity"
    )
    for result in results:
        cold = f"{result.cold_ms:.1f}ms" if result.cold_ms is not None else "-"
        median = f"{result.median_ms:.1f}ms" if result.median_ms is not None else "-"
        p95 = f"{result.p95_ms:.1f}ms" if result.p95_ms is not None else "-"
        fps = f"{result.fps:.2f}" if result.fps is not None else "-"
        parity = (
            "ok"
            if reference is not None and result.frame_hashes == reference
            else ("-" if not result.frame_hashes else "diff")
        )
        print(
            f"{SYSTEM_LABELS[result.system]:<28} {result.status:<7} {(result.route or '-'):<24} {cold:>9} {median:>10} {p95:>10} {fps:>8}  {parity}"
        )
        if result.error:
            print(f"  {result.error}")


def parity_reference(results: list[SystemResult]) -> list[str] | None:
    duckdb_hashes = next(
        (
            result.frame_hashes
            for result in results
            if result.system == "duckdb" and result.status == "ok"
        ),
        None,
    )
    if duckdb_hashes is not None:
        return duckdb_hashes
    return next(
        (result.frame_hashes for result in results if result.status == "ok"),
        None,
    )


def enforce_parity(results: list[SystemResult]) -> str | None:
    reference = parity_reference(results)
    if reference is None:
        return None
    reference_result = next(
        (result for result in results if result.system == "duckdb" and result.status == "ok"),
        next(result for result in results if result.status == "ok"),
    )
    reference_system = reference_result.system
    for result in results:
        if result.status == "ok" and result.frame_hashes != reference:
            result.status = "mismatch"
            result.error = f"rendered frames differ from {reference_system}"
    return reference_system


RUNTIME_COLUMNS = {
    "maps": MAP_COLUMNS,
    "planes": PLANE_COLUMNS,
    "clipnodes": CLIPNODE_COLUMNS,
    "models": MODEL_COLUMNS,
    "brushes": BRUSH_COLUMNS,
}
RUNTIME_ORDER = {
    "maps": "map_name",
    "planes": "plane_id",
    "clipnodes": "clipnode_id",
    "models": "model_id",
    "brushes": "entity_id",
}


def collision_world_from_rows(
    map_rows: list[tuple[Any, ...]],
    plane_rows: list[tuple[Any, ...]],
    clipnode_rows: list[tuple[Any, ...]],
    model_rows: list[tuple[Any, ...]],
    brush_rows: list[tuple[Any, ...]],
) -> CollisionWorld:
    if len(map_rows) != 1:
        raise ValueError(f"runtime map query returned {len(map_rows)} rows, expected one")
    if [int(row[1]) for row in plane_rows] != list(range(len(plane_rows))):
        raise ValueError("runtime plane IDs are not contiguous")
    if [int(row[1]) for row in clipnode_rows] != list(range(len(clipnode_rows))):
        raise ValueError("runtime clipnode IDs are not contiguous")
    if [int(row[1]) for row in model_rows] != list(range(len(model_rows))):
        raise ValueError("runtime model IDs are not contiguous")
    item = map_rows[0]
    planes = tuple(
        Plane((float(row[2]), float(row[3]), float(row[4])), float(row[5]), int(row[6]))
        for row in plane_rows
    )
    clipnodes = tuple(ClipNode(int(row[2]), (int(row[3]), int(row[4]))) for row in clipnode_rows)
    models = tuple(
        Model(
            (float(row[2]), float(row[3]), float(row[4])),
            (float(row[5]), float(row[6]), float(row[7])),
            (float(row[8]), float(row[9]), float(row[10])),
            (int(row[11]), int(row[12]), int(row[13]), int(row[14])),
            int(row[15]),
            int(row[16]),
            int(row[17]),
        )
        for row in model_rows
    )
    brushes = tuple(
        BrushEntity(
            int(row[1]),
            int(row[2]),
            str(row[3]),
            (float(row[4]), float(row[5]), float(row[6])),
            bool(row[7]),
            bool(row[8]),
            str(row[9]) if row[9] is not None else None,
            str(row[10]) if row[10] is not None else None,
            (float(row[11]), float(row[12]), float(row[13])),
            (float(row[14]), float(row[15]), float(row[16])),
            float(row[17]),
        )
        for row in brush_rows
    )
    return CollisionWorld(
        str(item[0]),
        (float(item[1]), float(item[2]), float(item[3])),
        float(item[4]),
        (
            (float(item[5]), float(item[6]), float(item[7])),
            (float(item[8]), float(item[9]), float(item[10])),
        ),
        planes,
        clipnodes,
        models,
        brushes,
    )


def load_collision_world(args: argparse.Namespace) -> CollisionWorld:
    rows: dict[str, list[tuple[Any, ...]]] = {}
    if args.runtime_source == "parquet":
        missing = next(
            (path for path in args.runtime_parquets.values() if not path.exists()),
            None,
        )
        if missing is not None:
            raise RuntimeError(f"missing SQL runtime relation: {missing}")
        with duckdb.connect(":memory:") as conn:
            for name, columns in RUNTIME_COLUMNS.items():
                rows[name] = conn.execute(
                    f"SELECT {', '.join(columns)} "
                    f"FROM {duck_relation(args.runtime_parquets[name])} "
                    f"WHERE map_name = ? ORDER BY {RUNTIME_ORDER[name]}",
                    [args.map_name],
                ).fetchall()
    else:
        dsn = args.dsn if args.runtime_source == "rvbbit" else args.postgres_dsn
        with psycopg.connect(dsn, autocommit=True) as conn:
            for name, columns in RUNTIME_COLUMNS.items():
                rows[name] = conn.execute(
                    f"SELECT {', '.join(columns)} FROM {args.runtime_tables[name]} "
                    f"WHERE map_name = %s ORDER BY {RUNTIME_ORDER[name]}",
                    (args.map_name,),
                ).fetchall()
    return collision_world_from_rows(
        rows["maps"],
        rows["planes"],
        rows["clipnodes"],
        rows["models"],
        rows["brushes"],
    )


def resolve_runtime_source(args: argparse.Namespace) -> str:
    if args.runtime_source != "auto":
        return args.runtime_source
    if args.interactive or args.render:
        if args.system == "duckdb":
            return "parquet"
        if args.system == "postgres":
            return "postgres"
        return "rvbbit"
    systems = {item.strip() for item in args.systems.split(",") if item.strip()}
    if systems & set(CANDIDATES):
        return "rvbbit"
    if "postgres" in systems:
        return "postgres"
    return "parquet"


FrameExecutor = Callable[
    [Camera, float, Sequence[tuple[int, float, float, float]]],
    tuple[list[tuple[Any, ...]], float, str],
]


def interactive_executor(stack: ExitStack, args: argparse.Namespace) -> FrameExecutor:
    if args.system in CANDIDATES:
        conn = stack.enter_context(psycopg.connect(args.dsn, autocommit=True))
        candidate = CANDIDATES[args.system]
        pg_session(conn, candidate, args.timeout)
        first_doc: dict[str, Any] | None = None

        def execute(
            camera: Camera,
            animation_time: float = 0.0,
            brush_offsets: Sequence[tuple[int, float, float, float]] = (),
        ) -> tuple[list[tuple[Any, ...]], float, str]:
            nonlocal first_doc
            sql = postgres_frame_query(camera, args, animation_time, brush_offsets)
            if first_doc is None:
                first_doc = route_explain(conn, sql)
                available, reason = candidate_available(first_doc, candidate)
                if not available:
                    raise RuntimeError(reason)
            started = time.perf_counter()
            rows = conn.execute(sql).fetchall()
            return (
                rows,
                (time.perf_counter() - started) * 1000.0,
                route_name(first_doc, args.system),
            )

        return execute
    if args.system == "postgres":
        conn = stack.enter_context(psycopg.connect(args.postgres_dsn, autocommit=True))
        conn.execute("SELECT set_config('statement_timeout', %s, false)", (f"{args.timeout}s",))

        def execute(
            camera: Camera,
            animation_time: float = 0.0,
            brush_offsets: Sequence[tuple[int, float, float, float]] = (),
        ) -> tuple[list[tuple[Any, ...]], float, str]:
            sql = postgres_frame_query(camera, args, animation_time, brush_offsets)
            started = time.perf_counter()
            rows = conn.execute(sql).fetchall()
            return rows, (time.perf_counter() - started) * 1000.0, "postgres_heap"

        return execute
    if not args.parquet.exists():
        raise RuntimeError(f"missing {args.parquet}")
    conn = stack.enter_context(duckdb.connect(":memory:"))
    table_expr = duck_relation(args.parquet)
    required = (
        args.texture_parquet,
        args.lightmap_parquet,
        args.material_parquet,
        args.colormap_parquet,
    )
    missing = next((path for path in required if not path.exists()), None)
    if args.renderer == "sql-texture" and missing is not None:
        raise RuntimeError(f"missing {missing}")
    texture_expr = duck_relation(args.texture_parquet)
    lightmap_expr = duck_relation(args.lightmap_parquet)
    material_expr = duck_relation(args.material_parquet)
    colormap_expr = duck_relation(args.colormap_parquet)

    def execute(
        camera: Camera,
        animation_time: float = 0.0,
        brush_offsets: Sequence[tuple[int, float, float, float]] = (),
    ) -> tuple[list[tuple[Any, ...]], float, str]:
        sql = frame_query(
            camera,
            args,
            table_expr,
            texture_expr,
            lightmap_expr,
            material_expr,
            colormap_expr,
            animation_time,
            brush_offsets,
        )
        started = time.perf_counter()
        rows = conn.execute(sql).fetchall()
        return rows, (time.perf_counter() - started) * 1000.0, "duckdb"

    return execute


def brush_origin(brush: BrushEntity, progress: float) -> tuple[float, float, float]:
    return tuple(
        brush.closed_origin[index]
        + (brush.open_origin[index] - brush.closed_origin[index]) * progress
        for index in range(3)
    )


def initial_brush_progress(brush: BrushEntity) -> float:
    return (
        1.0
        if math.dist(brush.origin, brush.open_origin) < math.dist(brush.origin, brush.closed_origin)
        else 0.0
    )


def advance_brushes(
    brushes: dict[int, BrushEntity],
    progress: dict[int, float],
    targets: dict[int, float],
    elapsed: float,
) -> None:
    for model_id, brush in brushes.items():
        travel = math.dist(brush.closed_origin, brush.open_origin)
        if travel <= 0.0:
            continue
        step = brush.speed * elapsed / travel
        if progress[model_id] < targets[model_id]:
            progress[model_id] = min(targets[model_id], progress[model_id] + step)
        elif progress[model_id] > targets[model_id]:
            progress[model_id] = max(targets[model_id], progress[model_id] - step)


def current_brush_origins(
    brushes: dict[int, BrushEntity], progress: dict[int, float]
) -> dict[int, tuple[float, float, float]]:
    return {
        model_id: brush_origin(brush, progress[model_id]) for model_id, brush in brushes.items()
    }


def current_brush_offsets(
    brushes: dict[int, BrushEntity], origins: dict[int, tuple[float, float, float]]
) -> tuple[tuple[int, float, float, float], ...]:
    return tuple(
        (
            model_id,
            origins[model_id][0] - brush.origin[0],
            origins[model_id][1] - brush.origin[1],
            origins[model_id][2] - brush.origin[2],
        )
        for model_id, brush in sorted(brushes.items())
        if any(abs(origins[model_id][index] - brush.origin[index]) > 1e-9 for index in range(3))
    )


def brush_distance(
    camera: Camera,
    world: CollisionWorld,
    brush: BrushEntity,
    origin: Sequence[float],
) -> float:
    model = world.models[brush.model_id]
    point = (camera.x, camera.y, camera.eye_z)
    squared = 0.0
    for index in range(3):
        low = model.mins[index] + origin[index]
        high = model.maxs[index] + origin[index]
        delta = max(low - point[index], 0.0, point[index] - high)
        squared += delta * delta
    return math.sqrt(squared)


def closed_bounds(
    world: CollisionWorld, brush: BrushEntity
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    model = world.models[brush.model_id]
    return (
        tuple(model.mins[index] + brush.closed_origin[index] for index in range(3)),
        tuple(model.maxs[index] + brush.closed_origin[index] for index in range(3)),
    )


def doors_touch(world: CollisionWorld, left: BrushEntity, right: BrushEntity) -> bool:
    if left.classname != "func_door" or right.classname != "func_door":
        return False
    left_low, left_high = closed_bounds(world, left)
    right_low, right_high = closed_bounds(world, right)
    return all(
        left_low[index] <= right_high[index] and right_low[index] <= left_high[index]
        for index in range(3)
    )


def activation_group(
    world: CollisionWorld, brushes: dict[int, BrushEntity], selected: int
) -> set[int]:
    group = {selected}
    selected_brush = brushes[selected]
    if selected_brush.target:
        group.update(
            model_id
            for model_id, brush in brushes.items()
            if brush.targetname == selected_brush.target
        )
    if selected_brush.targetname:
        group.update(
            model_id
            for model_id, brush in brushes.items()
            if brush.targetname == selected_brush.targetname
        )
    changed = True
    while changed:
        changed = False
        for model_id, brush in brushes.items():
            if model_id in group:
                continue
            if any(doors_touch(world, brush, brushes[member]) for member in group):
                group.add(model_id)
                changed = True
    return group


def toggle_nearest_brush(
    camera: Camera,
    world: CollisionWorld,
    brushes: dict[int, BrushEntity],
    origins: dict[int, tuple[float, float, float]],
    targets: dict[int, float],
    use_distance: float,
) -> int | None:
    nearby = sorted(
        (
            (brush_distance(camera, world, brush, origins[model_id]), model_id)
            for model_id, brush in brushes.items()
        )
    )
    if not nearby or nearby[0][0] > use_distance:
        return None
    model_id = nearby[0][1]
    next_target = 0.0 if targets[model_id] >= 0.5 else 1.0
    for member in activation_group(world, brushes, model_id):
        targets[member] = next_target
    return model_id


def interactive(args: argparse.Namespace, world: CollisionWorld) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("--interactive requires a terminal")
    spawn, yaw = world.spawn()
    start = Camera(*spawn, yaw=yaw, draw_distance=args.draw_distance, map_name=args.map_name)
    camera = start
    old_settings = termios.tcgetattr(sys.stdin)
    queries = 0
    brushes = {brush.model_id: brush for brush in world.brush_entities() if brush.mover}
    progress = {model_id: initial_brush_progress(brush) for model_id, brush in brushes.items()}
    initial_progress = progress.copy()
    targets = progress.copy()
    animation_started = time.monotonic()
    last_tick = animation_started
    selected_brush: int | None = None
    try:
        tty.setcbreak(sys.stdin.fileno())
        with ExitStack() as stack:
            execute = interactive_executor(stack, args)
            while True:
                now = time.monotonic()
                advance_brushes(brushes, progress, targets, now - last_tick)
                last_tick = now
                origins = current_brush_origins(brushes, progress)
                rows, elapsed, route = execute(
                    camera,
                    now - animation_started,
                    current_brush_offsets(brushes, origins),
                )
                queries += 1
                frame = render_rows(rows, camera, args)
                header = (
                    f"QUAKEQL  {route}/{args.renderer}  {elapsed:.1f}ms  "
                    f"{1000.0 / elapsed:.2f}fps  queries={queries}  "
                    f"pos=({camera.x:.0f},{camera.y:.0f},{camera.z:.0f}) "
                    f"yaw={camera.yaw:.0f} pitch={camera.pitch:.0f} "
                    f"movers={sum(abs(progress[mid] - targets[mid]) > 1e-3 for mid in brushes)} "
                    f"use={selected_brush or '-'}  hash={frame_hash(frame)}"
                )
                print("\x1b[2J\x1b[H" + header[: args.width] + "\n" + frame, end="", flush=True)
                ready, _, _ = select.select([sys.stdin], [], [], 0.25)
                if not ready:
                    continue
                key = sys.stdin.read(1).lower()
                if key == "w":
                    camera = camera.moved(args.move_step, world, brush_origins=origins)
                elif key == "s":
                    camera = camera.moved(-args.move_step, world, brush_origins=origins)
                elif key == "a":
                    camera = camera.turned(yaw=args.turn_degrees)
                elif key == "d":
                    camera = camera.turned(yaw=-args.turn_degrees)
                elif key == "z":
                    camera = camera.moved(args.move_step, world, strafe=True, brush_origins=origins)
                elif key == "c":
                    camera = camera.moved(
                        -args.move_step, world, strafe=True, brush_origins=origins
                    )
                elif key == "r":
                    camera = camera.turned(pitch=args.pitch_degrees)
                elif key == "f":
                    camera = camera.turned(pitch=-args.pitch_degrees)
                elif key == "x":
                    camera = start
                    progress = initial_progress.copy()
                    targets = progress.copy()
                    selected_brush = None
                elif key == " ":
                    selected_brush = toggle_nearest_brush(
                        camera,
                        world,
                        brushes,
                        origins,
                        targets,
                        args.use_distance,
                    )
                elif key in {"q", "\x03", "\x1b"}:
                    break
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--postgres-dsn", default=DEFAULT_POSTGRES_DSN)
    parser.add_argument("--map-name", default="E1M1")
    parser.add_argument(
        "--episode1",
        action="store_true",
        help="use the natural-cardinality START/E1M1-E1M8 dataset",
    )
    parser.add_argument("--table", type=simple_identifier)
    parser.add_argument("--texture-table", type=simple_identifier)
    parser.add_argument("--lightmap-table", type=simple_identifier)
    parser.add_argument("--material-table", type=simple_identifier)
    parser.add_argument("--colormap-table", type=simple_identifier, default="quakeql_colormap")
    parser.add_argument("--map-table", type=simple_identifier)
    parser.add_argument("--plane-table", type=simple_identifier)
    parser.add_argument("--clipnode-table", type=simple_identifier)
    parser.add_argument("--model-table", type=simple_identifier)
    parser.add_argument("--brush-table", type=simple_identifier)
    parser.add_argument("--parquet", type=Path)
    parser.add_argument("--texture-parquet", type=Path)
    parser.add_argument("--lightmap-parquet", type=Path)
    parser.add_argument("--material-parquet", type=Path)
    parser.add_argument("--colormap-parquet", type=Path, default=DEFAULT_COLORMAP_PARQUET)
    parser.add_argument("--map-parquet", type=Path)
    parser.add_argument("--plane-parquet", type=Path)
    parser.add_argument("--clipnode-parquet", type=Path)
    parser.add_argument("--model-parquet", type=Path)
    parser.add_argument("--brush-parquet", type=Path)
    parser.add_argument(
        "--runtime-source",
        choices=("auto", "parquet", "rvbbit", "postgres"),
        default="auto",
        help="SQL source used to hydrate map and collision state",
    )
    parser.add_argument("--systems", default=DEFAULT_SYSTEMS)
    parser.add_argument(
        "--system", choices=sorted((*CANDIDATES, "duckdb", "postgres")), default="auto"
    )
    parser.add_argument("--frames", type=int, default=16)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--height", type=int, default=40)
    parser.add_argument(
        "--render-distance",
        "--draw-distance",
        dest="draw_distance",
        type=int,
        default=768,
        metavar="UNITS",
        help="far clipping distance in Quake world units (default: 768)",
    )
    parser.add_argument("--layers", type=int, default=2)
    parser.add_argument("--sample-step", type=float, default=16.0)
    parser.add_argument("--brush-sample-step", type=float, default=4.0)
    parser.add_argument(
        "--renderer",
        choices=("samples", "sql-texture"),
        default="samples",
        help="sample colors directly or reconstruct perspective texture samples in SQL",
    )
    parser.add_argument(
        "--splat-cap",
        type=int,
        default=6,
        help="maximum screen-space splat radius; 0 draws one pixel",
    )
    parser.add_argument("--turn-degrees", type=float, default=10.0)
    parser.add_argument("--pitch-degrees", type=float, default=5.0)
    parser.add_argument("--move-step", type=float, default=24.0)
    parser.add_argument(
        "--use-distance",
        type=float,
        default=128.0,
        help="maximum distance for Space to activate a brush model",
    )
    parser.add_argument(
        "--animation-step",
        type=float,
        default=0.1,
        help="seconds of texture/light animation advanced per benchmark frame",
    )
    parser.add_argument(
        "--mip-bias",
        type=int,
        default=0,
        help="SQL texture mip offset; negative values retain more detail",
    )
    parser.add_argument("--render-type", choices=sorted(RENDER_TYPES), default="ansi-half")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--output", type=Path, default=HERE / "results" / "last_run.json")
    args = parser.parse_args()
    if args.episode1:
        args.table = args.table or "quakeql_episode1"
        args.texture_table = args.texture_table or "quakeql_episode1_texels"
        args.lightmap_table = args.lightmap_table or "quakeql_episode1_lightmaps"
        args.material_table = args.material_table or "quakeql_episode1_materials"
        args.parquet = args.parquet or DEFAULT_EPISODE_PARQUET
        args.texture_parquet = args.texture_parquet or DEFAULT_EPISODE_TEXTURE_PARQUET
        args.lightmap_parquet = args.lightmap_parquet or DEFAULT_EPISODE_LIGHTMAP_PARQUET
        args.material_parquet = args.material_parquet or DEFAULT_EPISODE_MATERIAL_PARQUET
    else:
        args.table = args.table or "quakeql_e1m1"
        args.texture_table = args.texture_table or "quakeql_e1m1_texels"
        args.lightmap_table = args.lightmap_table or "quakeql_e1m1_lightmaps"
        args.material_table = args.material_table or "quakeql_e1m1_materials"
        args.parquet = args.parquet or DEFAULT_PARQUET
        args.texture_parquet = args.texture_parquet or DEFAULT_TEXTURE_PARQUET
        args.lightmap_parquet = args.lightmap_parquet or DEFAULT_LIGHTMAP_PARQUET
        args.material_parquet = args.material_parquet or DEFAULT_MATERIAL_PARQUET
    args.parquet = args.parquet.expanduser().resolve()
    args.texture_parquet = args.texture_parquet.expanduser().resolve()
    args.lightmap_parquet = args.lightmap_parquet.expanduser().resolve()
    args.material_parquet = args.material_parquet.expanduser().resolve()
    args.colormap_parquet = args.colormap_parquet.expanduser().resolve()
    args.runtime_tables = {
        "maps": args.map_table or f"{args.table}_maps",
        "planes": args.plane_table or f"{args.table}_planes",
        "clipnodes": args.clipnode_table or f"{args.table}_clipnodes",
        "models": args.model_table or f"{args.table}_models",
        "brushes": args.brush_table or f"{args.table}_brushes",
    }
    runtime_defaults = runtime_parquet_defaults(args.parquet)
    args.runtime_parquets = {
        "maps": (args.map_parquet or runtime_defaults["maps"]).expanduser().resolve(),
        "planes": (args.plane_parquet or runtime_defaults["planes"]).expanduser().resolve(),
        "clipnodes": (args.clipnode_parquet or runtime_defaults["clipnodes"])
        .expanduser()
        .resolve(),
        "models": (args.model_parquet or runtime_defaults["models"]).expanduser().resolve(),
        "brushes": (args.brush_parquet or runtime_defaults["brushes"]).expanduser().resolve(),
    }
    args.runtime_source = resolve_runtime_source(args)
    args.map_name = args.map_name.upper()
    if args.frames <= 0 or args.width <= 0 or args.height <= 0:
        parser.error("--frames, --width, and --height must be positive")
    if args.warmups < 0:
        parser.error("--warmups cannot be negative")
    if args.splat_cap < 0:
        parser.error("--splat-cap cannot be negative")
    if args.animation_step < 0:
        parser.error("--animation-step cannot be negative")
    if (
        args.draw_distance <= 0
        or args.layers <= 0
        or args.sample_step <= 0
        or args.brush_sample_step <= 0
        or args.use_distance <= 0
    ):
        parser.error(
            "--render-distance, --layers, --sample-step, --brush-sample-step, and "
            "--use-distance must be positive"
        )
    try:
        world = load_collision_world(args)
    except (OSError, RuntimeError, ValueError, psycopg.Error, duckdb.Error) as exc:
        parser.error(str(exc))
    if args.interactive:
        return interactive(args, world)
    spawn, yaw = world.spawn()
    start = Camera(*spawn, yaw=yaw, draw_distance=args.draw_distance, map_name=args.map_name)
    if args.render:
        with ExitStack() as stack:
            execute = interactive_executor(stack, args)
            rows, elapsed, route = execute(start)
        frame = render_rows(rows, start, args)
        print(
            f"QUAKEQL  {route}/{args.renderer}  {elapsed:.1f}ms  "
            f"{1000.0 / elapsed:.2f}fps  hash={frame_hash(frame)}"
        )
        print(frame)
        return 0
    cameras = scripted_cameras(start, args.frames, world)
    systems = [item.strip() for item in args.systems.split(",") if item.strip()]
    unknown = sorted(set(systems) - set(CANDIDATES) - {"duckdb", "postgres"})
    if unknown:
        parser.error(f"unknown systems: {', '.join(unknown)}")
    results: list[SystemResult] = []
    for system in systems:
        if system in CANDIDATES:
            results.append(run_rvbbit(system, cameras, args))
        elif system == "duckdb":
            results.append(run_duckdb(cameras, args))
        else:
            results.append(run_postgres(cameras, args))
    reference_system = enforce_parity(results)
    print_results(results, args.renderer)
    document = {
        "format": "quakeql-benchmark-v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settings": {
            "map": args.map_name,
            "episode1": args.episode1,
            "table": args.table,
            "texture_table": args.texture_table,
            "lightmap_table": args.lightmap_table,
            "material_table": args.material_table,
            "colormap_table": args.colormap_table,
            "runtime_source": args.runtime_source,
            "runtime_tables": args.runtime_tables,
            "parquet": str(args.parquet),
            "texture_parquet": str(args.texture_parquet),
            "lightmap_parquet": str(args.lightmap_parquet),
            "material_parquet": str(args.material_parquet),
            "colormap_parquet": str(args.colormap_parquet),
            "runtime_parquets": {name: str(path) for name, path in args.runtime_parquets.items()},
            "frames": len(cameras),
            "warmups": args.warmups,
            "width": args.width,
            "height": args.height,
            "draw_distance": args.draw_distance,
            "layers": args.layers,
            "sample_step": args.sample_step,
            "brush_sample_step": args.brush_sample_step,
            "splat_cap": args.splat_cap,
            "renderer": args.renderer,
            "mip_bias": args.mip_bias,
            "animation_step": args.animation_step,
            "render_type": args.render_type,
        },
        "results": [asdict(result) for result in results],
        "parity_reference": reference_system,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {args.output}")
    return 1 if any(result.status in {"fail", "mismatch"} for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
