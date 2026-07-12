from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

import duckdb
import pytest

from .run import e1m1_scripted_cameras
from .wad_world import (
    SURFACE_CEILING,
    SURFACE_FLOOR,
    SURFACE_MASKED,
    SURFACE_SKY,
    SURFACE_WALL,
    RasterizedWorld,
    rasterize_map,
    read_wad_map,
)
from .workload import Camera, frame_hash, frame_sql, render_frame


WAD_PATH = Path(
    os.environ.get("DOOMQL_WAD", "~/repos2026/diffoom/assets/DOOM1.WAD")
).expanduser()


@pytest.fixture(scope="module")
def e1m1_world() -> RasterizedWorld:
    if not WAD_PATH.exists():
        pytest.skip(f"missing optional Doom shareware WAD: {WAD_PATH}")
    return rasterize_map(read_wad_map(WAD_PATH, "E1M1"), 16)


def test_e1m1_wad_geometry_and_player_start(e1m1_world: RasterizedWorld):
    doom_map = e1m1_world.doom_map
    assert len(doom_map.vertices) == 467
    assert len(doom_map.lines) == 475
    assert len(doom_map.sectors) == 85
    assert len(doom_map.subsectors) == 237
    assert e1m1_world.player_camera(96) == (114, 78, 41, 90, 96)
    assert e1m1_world.doom_map.point_sector(1056, -3616) == 38


def test_e1m1_query_keeps_the_gqe_portable_integer_shape():
    sql = frame_sql(Camera(114, 78, 41, 75, 96), world="e1m1").lower()
    assert "floor(" not in sql
    assert "%" not in sql
    assert "//" not in sql
    assert "group by" in sql
    assert "z_bottom" in sql
    assert "surface_kind" in sql
    assert "depth_scaled between 512" in sql
    assert frame_sql(Camera(), world="e1m1", dialect="clickhouse") == frame_sql(
        Camera(), world="e1m1", dialect="postgres"
    )


def test_e1m1_raster_contains_planes_walls_windows_and_steps(
    e1m1_world: RasterizedWorld,
):
    kinds = Counter(surface.surface_kind for surface in e1m1_world.surfaces)
    assert kinds[SURFACE_FLOOR] > 18_000
    assert kinds[SURFACE_CEILING] > 10_000
    assert kinds[SURFACE_SKY] > 0
    assert kinds[SURFACE_WALL] > 4_000
    assert kinds[SURFACE_MASKED] > 0
    assert e1m1_world.try_move(114, 78, 114, 100) == (114, 100, 25)


def test_e1m1_player_radius_blocks_wall_crossing_without_closing_portals(
    e1m1_world: RasterizedWorld,
):
    assert e1m1_world.position_is_clear(114, 78)
    assert e1m1_world.try_move(114, 78, 114, 76) == (114, 76, 41)
    assert e1m1_world.try_move(114, 76, 114, 74) is None
    assert not e1m1_world.position_is_clear(114, 74)
    assert e1m1_world.try_move(114, 78, 114, 100) == (114, 100, 25)


def test_e1m1_sql_projects_height_spans_and_renders_a_z_buffer(
    e1m1_world: RasterizedWorld,
):
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
                (
                    surface.surface_id,
                    surface.world_x,
                    surface.world_y,
                    surface.z_bottom,
                    surface.z_top,
                    surface.surface_kind,
                    surface.material,
                    surface.light,
                    surface.sector_id,
                )
                for surface in e1m1_world.surfaces
            ],
        )
        camera = Camera(*e1m1_world.player_camera(96))
        sql = frame_sql(
            camera,
            width=80,
            height=28,
            table_expr="surfaces",
            dialect="duckdb",
            world="e1m1",
        )
        rows = conn.execute(sql).fetchall()
    kinds = {int(row[4]) for row in rows}
    assert {SURFACE_WALL, SURFACE_FLOOR, SURFACE_CEILING} <= kinds
    frame = render_frame(
        rows,
        camera,
        width=80,
        height=28,
        world="e1m1",
        grid_scale=16,
    )
    lines = frame.splitlines()
    assert len(rows) > 5_000
    assert len(lines) == 28
    assert all(len(line) == 80 for line in lines)
    assert len(frame_hash(frame)) == 12
    ansi_frame = render_frame(
        rows,
        camera,
        width=80,
        height=28,
        world="e1m1",
        grid_scale=16,
        render_type="ansi-half",
    )
    plain_ansi = re.sub(r"\x1b\[[0-9;]*m", "", ansi_frame)
    assert "\x1b[38;2;" in ansi_frame
    assert "\x1b[48;2;" in ansi_frame
    assert "▀" in plain_ansi
    assert len(plain_ansi.splitlines()) == 28
    assert all(len(line) == 80 for line in plain_ansi.splitlines())
    assert len(frame_hash(ansi_frame)) == 12


def test_e1m1_scripted_walk_changes_position_height_and_heading(
    e1m1_world: RasterizedWorld,
):
    cameras = e1m1_scripted_cameras(e1m1_world, 12, 96)
    assert len({(camera.x, camera.y) for camera in cameras}) == 12
    assert len({camera.z for camera in cameras}) > 1
    assert len({camera.heading for camera in cameras}) == 12
