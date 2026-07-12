from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import duckdb

from .run import (
    SystemResult,
    enforce_parity,
    load_session,
    percentile,
    system_label,
    write_session,
)
from .sweep import parse_scales, scale_table
from .workload import (
    Camera,
    _fragment_texture_offset,
    camera_vector,
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


def test_camera_turns_and_moves_on_sub_cardinal_headings():
    camera = Camera().turned(15)
    assert camera.heading == 15
    assert camera_vector(camera.heading)[0] > camera_vector(camera.heading)[1] > 0
    assert Camera().turned(-15).heading == 345
    assert Camera(18, 112, heading=45).moved(2) == Camera(19, 113, heading=45)


def test_projected_texture_fragment_interpolates_one_grid_cell():
    offsets = [
        _fragment_texture_offset(screen_x, 2, 16)
        for screen_x in range(-2, 3)
    ]
    assert offsets == [-8, -4, 0, 4, 8]
    assert _fragment_texture_offset(0, 0, 16) == 0


def test_gqe_query_avoids_known_unsupported_projection_functions():
    sql = frame_sql(Camera()).lower()
    assert "floor(" not in sql
    assert "%" not in sql
    assert "//" not in sql
    assert "group by lateral_scaled, material" in sql


def test_clickhouse_uses_the_portable_postgres_query_shape():
    assert frame_sql(Camera(), dialect="clickhouse") == frame_sql(
        Camera(), dialect="postgres"
    )


def test_episode_query_filters_maps_doors_and_aggregates_face_light():
    with duckdb.connect(":memory:") as conn:
        conn.execute(
            """
            CREATE TABLE surfaces (
                sample_id BIGINT,
                scan_id INTEGER,
                map_name VARCHAR,
                surface_id INTEGER,
                world_x SMALLINT,
                world_y SMALLINT,
                z_bottom SMALLINT,
                z_top SMALLINT,
                surface_kind SMALLINT,
                material SMALLINT,
                light SMALLINT,
                sector_id SMALLINT,
                linedef_id SMALLINT,
                texture_u INTEGER,
                texture_v INTEGER,
                face_light SMALLINT,
                door_id SMALLINT
            )
            """
        )
        conn.executemany(
            "INSERT INTO surfaces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (0, 0, "E1M1", 1, 10, 0, 0, 64, 1, 1, 160, 1, 10, 3, 4, 16, -1),
                (1, 0, "E1M1", 2, 12, 0, 0, 64, 1, 2, 128, 2, 11, 5, 6, -16, 7),
                (2, 0, "E1M2", 3, 14, 0, 0, 64, 1, 3, 255, 3, 12, 7, 8, 0, -1),
            ],
        )
        closed_rows = conn.execute(
            frame_sql(
                Camera(0, 0, 41, 0, 32, map_name="E1M1"),
                table_expr="surfaces",
                dialect="duckdb",
                world="episode1",
            )
        ).fetchall()
        open_rows = conn.execute(
            frame_sql(
                Camera(0, 0, 41, 0, 32, map_name="E1M1", open_doors=(7,)),
                table_expr="surfaces",
                dialect="duckdb",
                world="episode1",
            )
        ).fetchall()

    assert len(closed_rows) == 2
    assert len(open_rows) == 1
    assert float(open_rows[0][7]) == 176
    assert tuple(int(value) for value in open_rows[0][10:15]) == (10, 3, 4, 16, -1)


def test_camera_space_preserves_left_and_right_orientation():
    with duckdb.connect(":memory:") as conn:
        conn.execute(
            """
            CREATE TABLE surfaces (
                surface_id INTEGER,
                world_x SMALLINT,
                world_y SMALLINT,
                z_bottom SMALLINT,
                z_top SMALLINT,
                surface_kind SMALLINT,
                material SMALLINT,
                light SMALLINT,
                sector_id SMALLINT
            )
            """
        )
        conn.executemany(
            "INSERT INTO surfaces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 10, -1, 0, 64, 1, 1, 192, 1),
                (2, 10, 1, 0, 64, 1, 2, 192, 1),
            ],
        )
        rows = conn.execute(
            frame_sql(
                Camera(0, 0, 41, 0, 32),
                table_expr="surfaces",
                dialect="duckdb",
                world="e1m1",
            )
        ).fetchall()

    lateral_by_material = {int(row[5]): int(row[0]) for row in rows}
    assert lateral_by_material[1] > 0  # South is screen-right when facing east.
    assert lateral_by_material[2] < 0  # North is screen-left when facing east.


def test_duckdb_query_executes_and_renders_stable_dimensions():
    with duckdb.connect(":memory:") as conn:
        conn.execute(f"CREATE VIEW doomql_world AS {world_select_sql(300_000)}")
        rows = conn.execute(frame_sql(Camera(), dialect="duckdb")).fetchall()
        angled_rows = conn.execute(
            frame_sql(Camera(heading=15), dialect="duckdb")
        ).fetchall()
    frame = render_frame(rows, Camera())
    angled_frame = render_frame(angled_rows, Camera(heading=15))
    lines = frame.splitlines()
    assert rows
    assert len(lines) == 40
    assert all(len(line) == 120 for line in lines)
    assert len(frame_hash(frame)) == 12
    assert frame_hash(angled_frame) != frame_hash(frame)
    ansi_frame = render_frame(
        rows,
        Camera(),
        width=120,
        height=40,
        render_type="ansi-half",
    )
    plain_ansi = re.sub(r"\x1b\[[0-9;]*m", "", ansi_frame)
    assert len(plain_ansi.splitlines()) == 40
    assert all(len(line) == 120 for line in plain_ansi.splitlines())


def test_parity_and_nearest_rank_percentile_are_enforced():
    reference = SystemResult("duckdb", "ok", "duckdb", "standalone", 1, 1, 1, 1000, 1, ["aaa"])
    mismatch = SystemResult("gpu_gqe", "ok", "gpu_gqe", "forced", 2, 2, 2, 500, 1, ["bbb"])
    assert enforce_parity([mismatch, reference]) == "duckdb"
    assert mismatch.status == "mismatch"
    assert percentile([1, 2, 3, 4], 0.95) == 4
    assert parse_scales("1_000_000, 5000000") == [1_000_000, 5_000_000]
    assert parse_scales("5m,50M") == [5_000_000, 50_000_000]
    assert scale_table("doomql_e1m1", 50_000_000) == "doomql_e1m1_50m"


def test_benchmark_display_labels_distinguish_rvbbit_engines():
    assert system_label("auto") == "RVBBIT Auto"
    assert system_label("datafusion_vortex") == "RVBBIT DataFusion Vortex"
    assert system_label("duckdb") == "DuckDB"
    assert system_label("postgres") == "PostgreSQL"
    assert system_label("citus") == "Citus Columnar"
    assert system_label("hydra") == "Hydra Columnar"
    assert system_label("alloydb") == "AlloyDB Omni"


def test_interactive_session_round_trips_resolved_camera_frames(tmp_path: Path):
    session_path = tmp_path / "tour.json"
    args = argparse.Namespace(
        world="e1m1",
        wad=Path("/tmp/DOOM1.WAD"),
        map_name="E1M1",
        grid_scale=16,
        table="doomql_e1m1",
        parquet=Path("/tmp/doomql_e1m1_5000000.parquet"),
        width=120,
        height=40,
        draw_distance=96,
        turn_degrees=15,
        render_type="ansi-half",
        system="auto",
    )
    cameras = [
        Camera(114, 78, 41, 90, 96),
        Camera(114, 80, 41, 90, 96),
        Camera(114, 80, 41, 75, 96),
    ]
    commands = [
        {
            "index": 0,
            "key": "w",
            "action": "forward",
            "before": {},
            "after": {},
            "blocked": False,
        },
        {
            "index": 1,
            "key": "d",
            "action": "turn_right",
            "before": {},
            "after": {},
            "blocked": False,
        },
    ]

    write_session(session_path, args, commands, cameras, queries_run=9)
    settings, loaded_cameras, digest = load_session(session_path)
    document = json.loads(session_path.read_text())

    assert settings["render_type"] == "ansi-half"
    assert loaded_cameras == cameras
    assert len(digest) == 64
    assert document["summary"] == {
        "commands": 2,
        "frames": 3,
        "unique_cameras": 3,
        "blocked_movements": 0,
        "interactive_queries": 9,
    }


def test_session_round_trips_episode_map_and_door_state(tmp_path: Path):
    session_path = tmp_path / "episode-tour.json"
    args = argparse.Namespace(
        world="episode1",
        wad=Path("/tmp/DOOM1.WAD"),
        map_name="E1M1",
        maps="E1M1,E1M2",
        grid_scale=16,
        table="doomql_episode1",
        parquet=Path("/tmp/doomql_episode1_5000000.parquet"),
        width=120,
        height=40,
        draw_distance=96,
        turn_degrees=15,
        render_type="ansi-half",
        system="auto",
    )
    cameras = [
        Camera(114, 78, 41, 90, 96, "E1M1", (7,)),
        Camera(42, 61, 41, 180, 96, "E1M2", ()),
    ]

    write_session(session_path, args, [], cameras, queries_run=2)
    settings, loaded_cameras, _ = load_session(session_path)

    assert settings["maps"] == "E1M1,E1M2"
    assert loaded_cameras == cameras
