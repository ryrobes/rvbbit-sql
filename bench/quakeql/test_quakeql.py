from __future__ import annotations

import os
import re
import struct
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from .pak_bsp import (
    PakArchive,
    QuakeBsp,
    RasterizedMap,
    SurfaceSample,
    extract_lightmap_texels,
    extract_material_frames,
    find_default_pak,
    parse_entities,
    rasterize_map,
)
from .run import (
    SystemResult,
    activation_group,
    enforce_parity,
    load_collision_world,
    percentile,
)
from .workload import (
    Camera,
    combine_geometry_parquets,
    create_parquet,
    create_runtime_parquets,
    frame_hash,
    frame_sql,
    render_frame,
    scripted_cameras,
    sql_texture_frame_sql,
)


PAK_PATH = Path(os.environ.get("QUAKEQL_PAK", find_default_pak())).expanduser()


def write_pak(path: Path, entries: dict[str, bytes]) -> None:
    payload = bytearray(b"PACK" + b"\0" * 8)
    records: list[tuple[str, int, int]] = []
    for name, value in entries.items():
        offset = len(payload)
        payload.extend(value)
        records.append((name, offset, len(value)))
    directory_offset = len(payload)
    for name, offset, size in records:
        encoded = name.encode("ascii")
        payload.extend(struct.pack("<56s2i", encoded, offset, size))
    struct.pack_into("<2i", payload, 4, directory_offset, len(records) * 64)
    path.write_bytes(payload)


def test_pak_archive_reads_case_insensitive_entries_and_rejects_missing(tmp_path: Path):
    path = tmp_path / "pak0.pak"
    write_pak(path, {"maps/e1m1.bsp": b"bsp-data", "gfx/palette.lmp": b"palette"})
    pak = PakArchive(path)

    assert pak.read("MAPS/E1M1.BSP") == b"bsp-data"
    assert pak.names() == ("maps/e1m1.bsp", "gfx/palette.lmp")
    assert pak.hashes()["shareware_identity"] is False
    with pytest.raises(KeyError, match="not found"):
        pak.read("missing")


def test_entity_parser_preserves_quake_key_value_blocks():
    entities = parse_entities(
        """
        { "classname" "worldspawn" "message" "The Slipgate Complex" }
        { "classname" "info_player_start" "origin" "480 -352 88" "angle" "90" }
        """
    )

    assert entities[0]["message"] == "The Slipgate Complex"
    assert entities[1] == {
        "classname": "info_player_start",
        "origin": "480 -352 88",
        "angle": "90",
    }


def test_combined_geometry_preserves_natural_rows_and_only_inflates_explicitly(
    tmp_path: Path,
):
    def world(map_name: str, world_x: int) -> RasterizedMap:
        sample = SurfaceSample(
            base_id=0,
            surface_id=1,
            model_id=0,
            world_x=world_x,
            world_y=0,
            world_z=0,
            normal_x=1024,
            normal_y=0,
            normal_z=0,
            surface_kind=0,
            material_id=1,
            texture_name="test",
            texture_u=0,
            texture_v=0,
            red=1,
            green=2,
            blue=3,
            light=255,
            fullbright=0,
            plane_distance=world_x / 8,
            texture_width=8,
            texture_height=8,
            texture_s_x=0,
            texture_s_y=1,
            texture_s_z=0,
            texture_s_offset=0,
            texture_t_x=0,
            texture_t_y=0,
            texture_t_z=1,
            texture_t_offset=0,
        )
        return RasterizedMap(
            map_name=map_name,
            samples=(sample,),
            spawn_origin=(0, 0, 0),
            spawn_yaw=0,
            bounds=((0, 0, 0), (1, 1, 1)),
            face_count=1,
            sampled_faces=1,
            model_count=1,
            texture_names=("test",),
            surface_counts={"wall": 1},
            brush_models=(),
            brush_sample_count=0,
            brush_sample_step=4,
        )

    parts = (tmp_path / "a.parquet", tmp_path / "b.parquet")
    create_parquet(parts[0], world("A", 8), 1, 10)
    create_parquet(parts[1], world("B", 16), 1, 10)
    combined = tmp_path / "combined.parquet"
    combine_geometry_parquets(combined, parts, base_rows=2, rows=2, row_group_size=10)
    with duckdb.connect(":memory:") as conn:
        natural = conn.execute(
            """
            SELECT count(*), count(DISTINCT sample_id), min(scan_id), max(scan_id),
                   string_agg(map_name, ',' ORDER BY sample_id)
            FROM read_parquet(?)
            """,
            [str(combined)],
        ).fetchone()
    assert natural == (2, 2, 0, 0, "A,B")

    combine_geometry_parquets(combined, parts, base_rows=2, rows=3, row_group_size=10)
    with duckdb.connect(":memory:") as conn:
        inflated = conn.execute(
            "SELECT count(*), count(DISTINCT sample_id), min(scan_id), max(scan_id) "
            "FROM read_parquet(?)",
            [str(combined)],
        ).fetchone()
    assert inflated == (3, 3, 0, 1)


def create_surface_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE surfaces (
            sample_id BIGINT, scan_id INTEGER, map_name VARCHAR,
            surface_id INTEGER, model_id SMALLINT,
            world_x INTEGER, world_y INTEGER, world_z INTEGER,
            normal_x SMALLINT, normal_y SMALLINT, normal_z SMALLINT,
            surface_kind SMALLINT, material_id SMALLINT, texture_name VARCHAR,
            texture_u SMALLINT, texture_v SMALLINT,
            red SMALLINT, green SMALLINT, blue SMALLINT,
            light SMALLINT, fullbright SMALLINT
        )
        """
    )


def test_sql_projects_left_right_pitch_and_depth_ranks():
    with duckdb.connect(":memory:") as conn:
        create_surface_table(conn)
        conn.executemany(
            "INSERT INTO surfaces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    0,
                    0,
                    "E1M1",
                    10,
                    0,
                    640,
                    -80,
                    176,
                    0,
                    0,
                    0,
                    0,
                    1,
                    "left",
                    0,
                    0,
                    220,
                    40,
                    30,
                    255,
                    0,
                ),
                (
                    1,
                    0,
                    "E1M1",
                    11,
                    0,
                    640,
                    80,
                    176,
                    0,
                    0,
                    0,
                    0,
                    2,
                    "right",
                    0,
                    0,
                    30,
                    220,
                    40,
                    255,
                    0,
                ),
                (
                    2,
                    0,
                    "E1M1",
                    12,
                    0,
                    960,
                    120,
                    176,
                    0,
                    0,
                    0,
                    0,
                    3,
                    "behind",
                    0,
                    0,
                    30,
                    40,
                    220,
                    255,
                    0,
                ),
            ],
        )
        camera = Camera(0, 0, 0, yaw=0, draw_distance=256)
        rows = conn.execute(
            frame_sql(camera, width=80, pixel_height=50, table_expr="surfaces")
        ).fetchall()
        pitched = conn.execute(
            frame_sql(camera.turned(pitch=15), width=80, pixel_height=50, table_expr="surfaces")
        ).fetchall()

    by_surface = {int(row[11]): row for row in rows}
    pitched_by_surface = {int(row[11]): row for row in pitched}
    assert by_surface[11][0] < 40 < by_surface[10][0]
    assert camera.moved(10, strafe=True).y > camera.y  # Quake +Y is left when facing +X.
    assert by_surface[11][2] < by_surface[12][2]
    assert pitched_by_surface[10][1] > by_surface[10][1]
    sql = frame_sql(camera, table_expr="surfaces").lower()
    assert "row_number() over" in sql
    assert "partition by pixel_x, pixel_y" in sql
    assert "group by" in sql
    assert "light * 2" in sql


def test_sql_texture_renderer_intersects_plane_joins_mip_texel_and_shades():
    with duckdb.connect(":memory:") as conn:
        conn.execute(
            """
            CREATE TABLE textured_surfaces (
                sample_id BIGINT, scan_id INTEGER, map_name VARCHAR,
                surface_id INTEGER, model_id SMALLINT,
                world_x INTEGER, world_y INTEGER, world_z INTEGER,
                normal_x SMALLINT, normal_y SMALLINT, normal_z SMALLINT,
                surface_kind SMALLINT, material_id SMALLINT, texture_name VARCHAR,
                texture_u SMALLINT, texture_v SMALLINT,
                red SMALLINT, green SMALLINT, blue SMALLINT,
                light SMALLINT, fullbright SMALLINT,
                plane_distance DOUBLE, texture_width SMALLINT, texture_height SMALLINT,
                texture_s_x DOUBLE, texture_s_y DOUBLE, texture_s_z DOUBLE,
                texture_s_offset DOUBLE, texture_t_x DOUBLE, texture_t_y DOUBLE,
                texture_t_z DOUBLE, texture_t_offset DOUBLE
            )
            """
        )
        conn.execute(
            """
            INSERT INTO textured_surfaces VALUES (
                0, 0, 'E1M1', 1, 0, 640, 0, 176, 1024, 0, 0,
                0, 1, 'test', 0, 0, 1, 2, 3, 255, 0,
                80.0, 8, 8, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE texels (
                map_name VARCHAR, material_id SMALLINT, texture_name VARCHAR,
                mip_level SMALLINT, mip_width SMALLINT, mip_height SMALLINT,
                texel_u SMALLINT, texel_v SMALLINT, palette_index SMALLINT,
                red SMALLINT, green SMALLINT, blue SMALLINT, fullbright SMALLINT
            )
            """
        )
        texels = []
        for mip_level, size in enumerate((8, 4, 2, 1)):
            for texel_v in range(size):
                for texel_u in range(size):
                    texels.append(
                        (
                            "E1M1",
                            1,
                            "test",
                            mip_level,
                            size,
                            size,
                            texel_u,
                            texel_v,
                            42,
                            200,
                            100,
                            50,
                            0,
                        )
                    )
        conn.executemany(
            "INSERT INTO texels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", texels
        )
        camera = Camera(0, 0, 0, yaw=0, draw_distance=256)
        sql = sql_texture_frame_sql(
            camera,
            width=80,
            pixel_height=50,
            table_expr="textured_surfaces",
            texture_expr="texels",
            splat_cap=0,
        )
        rows = conn.execute(sql).fetchall()
        turned_sql = sql_texture_frame_sql(
            camera.turned(yaw=10, pitch=7),
            width=80,
            pixel_height=50,
            table_expr="textured_surfaces",
            texture_expr="texels",
            splat_cap=0,
        )
        turned_rows = conn.execute(turned_sql).fetchall()
        conn.execute(
            """
            CREATE TABLE materials (
                map_name VARCHAR, source_material_id SMALLINT, source_texture_name VARCHAR,
                frame_index SMALLINT, frame_count SMALLINT,
                target_material_id SMALLINT, target_texture_name VARCHAR
            )
            """
        )
        conn.executemany(
            "INSERT INTO materials VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("E1M1", 1, "test", 0, 2, 1, "test"),
                ("E1M1", 1, "test", 1, 2, 2, "test2"),
            ],
        )
        animated_texels = []
        for mip_level, size in enumerate((8, 4, 2, 1)):
            for texel_v in range(size):
                for texel_u in range(size):
                    animated_texels.append(
                        (
                            "E1M1",
                            2,
                            "test2",
                            mip_level,
                            size,
                            size,
                            texel_u,
                            texel_v,
                            99,
                            1,
                            1,
                            1,
                            0,
                        )
                    )
        conn.executemany(
            "INSERT INTO texels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            animated_texels,
        )
        conn.execute(
            """
            CREATE TABLE lightmaps (
                map_name VARCHAR, surface_id INTEGER, style_slot SMALLINT,
                style_id SMALLINT, light_min_s INTEGER, light_min_t INTEGER,
                light_width SMALLINT, light_height SMALLINT,
                light_u SMALLINT, light_v SMALLINT, light_value SMALLINT
            )
            """
        )
        conn.executemany(
            "INSERT INTO lightmaps VALUES ('E1M1', 1, 0, 0, -16, 16, 2, 2, ?, ?, 128)",
            [(0, 0), (1, 0), (0, 1), (1, 1)],
        )
        conn.execute(
            """
            CREATE TABLE colormap (
                light_level SMALLINT, palette_index SMALLINT,
                mapped_palette_index SMALLINT, red SMALLINT, green SMALLINT, blue SMALLINT
            )
            """
        )
        conn.execute("INSERT INTO colormap VALUES (30, 99, 99, 9, 8, 7)")
        supported_sql = sql_texture_frame_sql(
            camera,
            width=80,
            pixel_height=50,
            table_expr="textured_surfaces",
            texture_expr="texels",
            lightmap_expr="lightmaps",
            material_expr="materials",
            colormap_expr="colormap",
            splat_cap=0,
            animation_time=0.2,
        )
        supported_rows = conn.execute(supported_sql).fetchall()

    assert len(rows) == 1
    assert rows[0][0:2] == (40, 25)
    assert rows[0][3:6] == (640, -8, 168)
    assert rows[0][6:9] == (163, 81, 40)
    assert rows[0][11:14] == (1, 1, "test")
    assert "ray_t" in sql
    assert "texels.texel_u = geometry.texel_u" in sql
    assert "row_number() over" in sql.lower()
    assert turned_rows
    assert re.search(r"-\d\.\d{17}e[+-]\d{2}", turned_sql)
    assert supported_rows[0][6:10] == (9, 8, 7, 132)
    assert supported_rows[0][12:14] == (2, "test2")
    assert "lightmap_surfaces" in supported_sql
    assert "sampled_texture_s" in supported_sql


def test_renderers_have_stable_terminal_dimensions():
    camera = Camera(0, 0, 0)
    rows = [(4, 4, 1_000_000, 0, 0, 0, 200, 100, 50, 180, 0, 1, 1, "stone", 1, 1, 0)]
    ascii_frame = render_frame(rows, camera, width=12, height=6, render_type="ascii")
    ansi_frame = render_frame(rows, camera, width=12, height=6, render_type="ansi-half")
    point_frame = render_frame(rows, camera, width=12, height=6, render_type="ascii", splat_cap=0)
    plain_ansi = re.sub(r"\x1b\[[0-9;]*m", "", ansi_frame)

    assert len(ascii_frame.splitlines()) == 6
    assert all(len(line) == 12 for line in ascii_frame.splitlines())
    assert len(plain_ansi.splitlines()) == 6
    assert all(len(line) == 12 for line in plain_ansi.splitlines())
    assert "\x1b[38;2;" in ansi_frame
    assert len(frame_hash(ansi_frame)) == 16
    assert frame_hash(point_frame) != frame_hash(ascii_frame)

    with pytest.raises(ValueError, match="cannot be negative"):
        render_frame(rows, camera, splat_cap=-1)


def test_benchmark_parity_uses_duckdb_and_marks_mismatch():
    rvbbit = SystemResult("auto", "ok", "duck_vortex", 1, 1, 1, 1000, 1, ["a"])
    duckdb_result = SystemResult("duckdb", "ok", "duckdb", 1, 1, 1, 1000, 1, ["b"])

    assert enforce_parity([rvbbit, duckdb_result]) == "duckdb"
    assert rvbbit.status == "mismatch"
    assert percentile([1, 2, 3, 4], 0.95) == 4

    matching = SystemResult("auto", "ok", "duck_vortex", 1, 1, 1, 1000, 1, ["b"])
    assert enforce_parity([matching, duckdb_result]) == "duckdb"
    assert matching.status == "ok"


@pytest.fixture(scope="module")
def e1m1_bsp() -> QuakeBsp:
    if not PAK_PATH.exists():
        pytest.skip(f"missing optional Quake PAK: {PAK_PATH}")
    return QuakeBsp.from_pak(PakArchive(PAK_PATH), "E1M1")


def test_real_e1m1_bsp_structure_spawn_and_collision(e1m1_bsp: QuakeBsp):
    assert len(e1m1_bsp.faces) == 5_516
    assert len(e1m1_bsp.models) == 58
    assert len(e1m1_bsp.textures) == 81
    assert (
        sum(
            sum(len(mip) for mip in texture.mip_pixels)
            for texture in e1m1_bsp.textures
            if texture is not None
        )
        == 540_600
    )
    assert e1m1_bsp.spawn() == ((480.0, -352.0, 88.0), 90.0)
    assert e1m1_bsp.hull_contents(e1m1_bsp.spawn()[0], hull=1) != -2
    assert not e1m1_bsp.position_is_solid(e1m1_bsp.spawn()[0])

    brushes = {brush.model_id: brush for brush in e1m1_bsp.brush_entities()}
    assert len(brushes) == 31
    assert {brush.classname for brush in brushes.values()} == {
        "func_button",
        "func_door",
        "func_door_secret",
        "func_plat",
        "func_wall",
    }
    assert brushes[7].origin == (0.0, 0.0, -152.0)
    assert brushes[22].origin == (0.0, 0.0, -400.0)
    assert brushes[8].origin[1] == pytest.approx(-240.0)
    assert brushes[1].closed_origin == (0.0, 0.0, 0.0)
    assert brushes[1].open_origin[1] == pytest.approx(-96.0)
    assert brushes[1].speed == 400.0

    door = e1m1_bsp.models[1]
    door_center = tuple(
        (door.mins[index] + door.maxs[index]) / 2 + brushes[1].origin[index] for index in range(3)
    )
    assert e1m1_bsp.hull_contents(door_center, hull=1, model_id=1, origin=brushes[1].origin) == -2
    assert e1m1_bsp.position_is_solid(door_center)
    moving_brushes = {model_id: brush for model_id, brush in brushes.items() if brush.mover}
    linked = activation_group(e1m1_bsp, moving_brushes, 1)
    assert linked == {1, 2}
    open_origins = {model_id: moving_brushes[model_id].open_origin for model_id in linked}
    assert not e1m1_bsp.position_is_solid(door_center, brush_origins=open_origins)

    start = Camera(*e1m1_bsp.spawn()[0], yaw=90, draw_distance=768)
    cameras = scripted_cameras(start, 6, e1m1_bsp)
    assert len({(camera.x, camera.y, camera.z, camera.yaw, camera.pitch) for camera in cameras}) > 3


def test_real_e1m1_raster_has_textures_lightmaps_and_3d_planes(e1m1_bsp: QuakeBsp):
    world = rasterize_map(e1m1_bsp, sample_step=16)
    kinds = {sample.surface_kind for sample in world.samples}

    assert world.sampled_faces == 5_342
    assert len(world.samples) == 1_098_853
    assert len(world.brush_models) == 31
    assert world.brush_sample_count == 335_546
    assert world.brush_sample_step == 4.0
    assert sum(sample.model_id == 0 for sample in world.samples) == 763_307
    sampled_models = {sample.model_id for sample in world.samples}
    assert sampled_models == {0, *(brush.model_id for brush in world.brush_models)}
    assert 11 not in sampled_models  # Invisible trigger_once volume.
    assert max(sample.world_z for sample in world.samples if sample.model_id == 7) < 0
    assert kinds == {0, 1, 2, 3, 4}
    assert len({sample.texture_name for sample in world.samples}) > 40
    assert len({sample.light for sample in world.samples}) > 16
    assert len({sample.world_z for sample in world.samples}) > 100


def test_real_e1m1_extracts_sql_lightmaps_and_material_animation(e1m1_bsp: QuakeBsp):
    lightmaps = extract_lightmap_texels(e1m1_bsp)
    materials = extract_material_frames(e1m1_bsp)
    planet = [frame for frame in materials if frame.source_texture_name == "+0planet"]

    assert len(lightmaps) == 163_096
    assert {texel.style_id for texel in lightmaps} >= {0, 10, 32, 33, 34, 35, 36}
    assert [frame.target_texture_name for frame in planet] == [
        "+0planet",
        "+1planet",
        "+2planet",
        "+3planet",
    ]


def test_sql_runtime_round_trip_preserves_collision_world(e1m1_bsp: QuakeBsp, tmp_path: Path):
    paths = {
        name: tmp_path / f"runtime_{name}.parquet"
        for name in ("maps", "planes", "clipnodes", "models", "brushes")
    }
    counts = create_runtime_parquets(e1m1_bsp, paths)
    world = load_collision_world(
        SimpleNamespace(
            runtime_source="parquet",
            runtime_parquets=paths,
            map_name="E1M1",
        )
    )

    assert counts == {
        "maps": 1,
        "planes": 1_810,
        "clipnodes": 5_408,
        "models": 58,
        "brushes": 31,
    }
    assert world.spawn() == e1m1_bsp.spawn()
    assert world.bounds == (e1m1_bsp.models[0].mins, e1m1_bsp.models[0].maxs)
    assert world.position_is_solid(world.spawn()[0]) == e1m1_bsp.position_is_solid(world.spawn()[0])

    brushes = {brush.model_id: brush for brush in world.brush_entities() if brush.mover}
    linked = activation_group(world, brushes, 1)
    door = world.models[1]
    door_center = tuple(
        (door.mins[index] + door.maxs[index]) / 2 + brushes[1].closed_origin[index]
        for index in range(3)
    )
    open_origins = {model_id: brushes[model_id].open_origin for model_id in linked}
    assert world.position_is_solid(door_center)
    assert not world.position_is_solid(door_center, brush_origins=open_origins)
