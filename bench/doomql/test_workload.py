from __future__ import annotations

import duckdb

from .run import SystemResult, enforce_parity, percentile
from .sweep import parse_scales
from .workload import (
    Camera,
    frame_hash,
    frame_sql,
    render_frame,
    scripted_cameras,
    wall_material,
    world_select_sql,
)


def test_starting_camera_and_scripted_corridor_are_open():
    assert wall_material(18, 112) == 0
    assert wall_material(24, 112) == 0
    assert wall_material(30, 112) == 0
    assert all(wall_material(camera.x, camera.y) == 0 for camera in scripted_cameras(12))


def test_gqe_query_avoids_known_unsupported_projection_functions():
    sql = frame_sql(Camera()).lower()
    assert "floor(" not in sql
    assert "%" not in sql
    assert "//" not in sql
    assert "group by lateral_offset, material" in sql


def test_clickhouse_uses_the_portable_postgres_query_shape():
    assert frame_sql(Camera(), dialect="clickhouse") == frame_sql(
        Camera(), dialect="postgres"
    )


def test_duckdb_query_executes_and_renders_stable_dimensions():
    with duckdb.connect(":memory:") as conn:
        conn.execute(f"CREATE VIEW doomql_world AS {world_select_sql(300_000)}")
        rows = conn.execute(frame_sql(Camera(), dialect="duckdb")).fetchall()
    frame = render_frame(rows, Camera())
    lines = frame.splitlines()
    assert rows
    assert len(lines) == 40
    assert all(len(line) == 120 for line in lines)
    assert len(frame_hash(frame)) == 12


def test_parity_and_nearest_rank_percentile_are_enforced():
    reference = SystemResult("duckdb", "ok", "duckdb", "standalone", 1, 1, 1, 1000, 1, ["aaa"])
    mismatch = SystemResult("gpu_gqe", "ok", "gpu_gqe", "forced", 2, 2, 2, 500, 1, ["bbb"])
    assert enforce_parity([mismatch, reference]) == "duckdb"
    assert mismatch.status == "mismatch"
    assert percentile([1, 2, 3, 4], 0.95) == 4
    assert parse_scales("1_000_000, 5000000") == [1_000_000, 5_000_000]
