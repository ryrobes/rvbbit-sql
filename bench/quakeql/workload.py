"""QuakeQL dataset, camera projection SQL, and terminal rendering primitives."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

try:
    from .pak_bsp import (
        COORD_SCALE,
        CollisionWorld,
        LightmapTexel,
        MaterialFrame,
        QuakeBsp,
        RasterizedMap,
        SurfaceSample,
    )
except ImportError:
    from pak_bsp import (
        COORD_SCALE,
        CollisionWorld,
        LightmapTexel,
        MaterialFrame,
        QuakeBsp,
        RasterizedMap,
        SurfaceSample,
    )


CAMERA_VECTOR_SCALE = 1_000_000
VIEW_HEIGHT = 22.0
DEFAULT_FOV_DEGREES = 90
DEFAULT_SPLAT_CAP = 6
RENDER_TYPES = {"ascii", "ansi-half"}
TABLE_COLUMNS = (
    "sample_id",
    "scan_id",
    "map_name",
    "surface_id",
    "model_id",
    "world_x",
    "world_y",
    "world_z",
    "normal_x",
    "normal_y",
    "normal_z",
    "surface_kind",
    "material_id",
    "texture_name",
    "texture_u",
    "texture_v",
    "red",
    "green",
    "blue",
    "light",
    "fullbright",
    "plane_distance",
    "texture_width",
    "texture_height",
    "texture_s_x",
    "texture_s_y",
    "texture_s_z",
    "texture_s_offset",
    "texture_t_x",
    "texture_t_y",
    "texture_t_z",
    "texture_t_offset",
)
TEXTURE_COLUMNS = (
    "map_name",
    "material_id",
    "texture_name",
    "mip_level",
    "mip_width",
    "mip_height",
    "texel_u",
    "texel_v",
    "palette_index",
    "red",
    "green",
    "blue",
    "fullbright",
)
LIGHTMAP_COLUMNS = (
    "map_name",
    "surface_id",
    "style_slot",
    "style_id",
    "light_min_s",
    "light_min_t",
    "light_width",
    "light_height",
    "light_u",
    "light_v",
    "light_value",
)
MATERIAL_COLUMNS = (
    "map_name",
    "source_material_id",
    "source_texture_name",
    "frame_index",
    "frame_count",
    "target_material_id",
    "target_texture_name",
)
COLORMAP_COLUMNS = (
    "light_level",
    "palette_index",
    "mapped_palette_index",
    "red",
    "green",
    "blue",
)
MAP_COLUMNS = (
    "map_name",
    "spawn_x",
    "spawn_y",
    "spawn_z",
    "spawn_yaw",
    "min_x",
    "min_y",
    "min_z",
    "max_x",
    "max_y",
    "max_z",
)
PLANE_COLUMNS = (
    "map_name",
    "plane_id",
    "normal_x",
    "normal_y",
    "normal_z",
    "distance",
    "plane_kind",
)
CLIPNODE_COLUMNS = (
    "map_name",
    "clipnode_id",
    "plane_id",
    "child_front",
    "child_back",
)
MODEL_COLUMNS = (
    "map_name",
    "model_id",
    "min_x",
    "min_y",
    "min_z",
    "max_x",
    "max_y",
    "max_z",
    "origin_x",
    "origin_y",
    "origin_z",
    "headnode_0",
    "headnode_1",
    "headnode_2",
    "headnode_3",
    "visleafs",
    "first_face",
    "face_count",
)
BRUSH_COLUMNS = (
    "map_name",
    "entity_id",
    "model_id",
    "classname",
    "origin_x",
    "origin_y",
    "origin_z",
    "solid",
    "mover",
    "targetname",
    "target",
    "closed_x",
    "closed_y",
    "closed_z",
    "open_x",
    "open_y",
    "open_z",
    "speed",
)


@dataclass(frozen=True, slots=True)
class Camera:
    x: float
    y: float
    z: float
    yaw: float = 0.0
    pitch: float = 0.0
    draw_distance: int = 768
    map_name: str = "E1M1"

    @property
    def eye_z(self) -> float:
        return self.z + VIEW_HEIGHT

    def turned(self, yaw: float = 0.0, pitch: float = 0.0) -> "Camera":
        return replace(
            self,
            yaw=(self.yaw + yaw) % 360.0,
            pitch=max(-80.0, min(80.0, self.pitch + pitch)),
        )

    def moved(
        self,
        amount: float,
        bsp: CollisionWorld | QuakeBsp | None = None,
        strafe: bool = False,
        brush_origins: dict[int, Sequence[float]] | None = None,
    ) -> "Camera":
        angle = math.radians(self.yaw + (90.0 if strafe else 0.0))
        target_x = self.x + math.cos(angle) * amount
        target_y = self.y + math.sin(angle) * amount
        if bsp is None:
            return replace(self, x=target_x, y=target_y)
        resolved = move_with_collision(
            bsp,
            (self.x, self.y, self.z),
            (target_x, target_y, self.z),
            brush_origins=brush_origins,
        )
        if resolved is None:
            return self
        return replace(self, x=resolved[0], y=resolved[1], z=resolved[2])


@dataclass(frozen=True, slots=True)
class FramePoint:
    pixel_x: int
    pixel_y: int
    depth_scaled: int
    world_x: int
    world_y: int
    world_z: int
    red: int
    green: int
    blue: int
    light: int
    surface_kind: int
    surface_id: int
    material_id: int
    texture_name: str
    observations: int
    pixel_rank: int
    model_id: int


def spawn_camera(world: RasterizedMap, draw_distance: int = 768) -> Camera:
    x, y, z = world.spawn_origin
    return Camera(x, y, z, world.spawn_yaw, 0.0, draw_distance, world.map_name)


def projection_focal_length(width: int, fov_degrees: int = DEFAULT_FOV_DEGREES) -> int:
    return max(1, round((width / 2) / math.tan(math.radians(fov_degrees / 2))))


def _camera_vectors(camera: Camera) -> tuple[int, int, int, int]:
    yaw = math.radians(camera.yaw % 360.0)
    pitch = math.radians(camera.pitch)
    return (
        round(math.cos(yaw) * CAMERA_VECTOR_SCALE),
        round(math.sin(yaw) * CAMERA_VECTOR_SCALE),
        round(math.cos(pitch) * CAMERA_VECTOR_SCALE),
        round(math.sin(pitch) * CAMERA_VECTOR_SCALE),
    )


def _sql_double(value: float) -> str:
    """Emit a literal that DuckDB cannot infer as a fixed-width DECIMAL."""
    return f"{value:.17e}"


QUAKE_LIGHT_STYLES = {
    0: "m",
    1: "mmnmmommommnonmmonqnmmo",
    2: "abcdefghijklmnopqrstuvwxyzyxwvutsrqponmlkjihgfedcba",
    3: "mmmmmaaaaammmmmaaaaaabcdefgabcdefg",
    4: "mamamamamama",
    5: "jklmnopqrstuvwxyzyxwvutsrqponmlkj",
    6: "nmonqnmomnmomomno",
    7: "mmmaaaabcdefgmmmmaaaammmaamm",
    8: "mmmaaammmaaammmabcdefaaaammmmabcdefmmmaaaa",
    9: "aaaaaaaazzzzzzzz",
    10: "mmamammmmammamamaaamammma",
    11: "abcdefghijklmnopqrrqponmlkjihgfedcba",
}


def _light_style_case(animation_time: float, column: str = "style_id") -> str:
    frame = max(0, math.floor(animation_time * 10.0))
    clauses = []
    for style_id, pattern in QUAKE_LIGHT_STYLES.items():
        scale = (ord(pattern[frame % len(pattern)]) - ord("a")) * 22
        clauses.append(f"WHEN {style_id} THEN {scale}")
    return f"CASE {column} {' '.join(clauses)} ELSE 264 END"


def frame_sql(
    camera: Camera,
    *,
    width: int = 120,
    pixel_height: int = 80,
    table_expr: str = "quakeql_e1m1",
    layers: int = 2,
) -> str:
    """Return one SQL query that transforms, projects, shades, and Z-ranks a frame."""
    if width <= 0 or pixel_height <= 0:
        raise ValueError("frame dimensions must be positive")
    if layers <= 0:
        raise ValueError("layers must be positive")
    if not camera.map_name.replace("_", "").isalnum():
        raise ValueError("map name must be alphanumeric")
    yaw_cos, yaw_sin, pitch_cos, pitch_sin = _camera_vectors(camera)
    camera_x = round(camera.x * COORD_SCALE)
    camera_y = round(camera.y * COORD_SCALE)
    camera_z = round(camera.eye_z * COORD_SCALE)
    draw_fixed = camera.draw_distance * COORD_SCALE
    near_fixed = max(COORD_SCALE, 4 * COORD_SCALE)
    vector_squared = CAMERA_VECTOR_SCALE * CAMERA_VECTOR_SCALE
    focal = projection_focal_length(width)
    half_width = width / 2.0
    half_height = pixel_height / 2.0
    map_name = camera.map_name.upper()
    delta_x = f"(CAST(world_x AS BIGINT) - {camera_x})"
    delta_y = f"(CAST(world_y AS BIGINT) - {camera_y})"
    delta_z = f"(CAST(world_z AS BIGINT) - {camera_z})"
    horizontal = f"({delta_x} * {yaw_cos} + {delta_y} * {yaw_sin})"
    right = f"({delta_x} * {yaw_sin} - {delta_y} * {yaw_cos})"
    depth = f"({horizontal} * {pitch_cos} + {delta_z} * {CAMERA_VECTOR_SCALE} * {pitch_sin})"
    vertical = f"({delta_z} * {CAMERA_VECTOR_SCALE} * {pitch_cos} - {horizontal} * {pitch_sin})"
    coarse = (
        f"world_x BETWEEN {camera_x - draw_fixed} AND {camera_x + draw_fixed} "
        f"AND world_y BETWEEN {camera_y - draw_fixed} AND {camera_y + draw_fixed} "
        f"AND world_z BETWEEN {camera_z - draw_fixed} AND {camera_z + draw_fixed}"
    )
    return f"""
WITH camera_space AS (
    SELECT
        surface_id,
        model_id,
        material_id,
        texture_name,
        world_x,
        world_y,
        world_z,
        surface_kind,
        red,
        green,
        blue,
        light,
        fullbright,
        {depth} AS depth_scaled,
        {right} * {CAMERA_VECTOR_SCALE} AS right_scaled,
        {vertical} AS vertical_scaled
    FROM {table_expr}
    WHERE map_name = '{map_name}'
      AND {coarse}
), visible_space AS (
    SELECT *
    FROM camera_space
    WHERE depth_scaled BETWEEN {near_fixed * vector_squared} AND {draw_fixed * vector_squared}
), projected AS (
    SELECT
        CAST(FLOOR({half_width} + CAST(right_scaled AS DOUBLE PRECISION) * {focal}
                   / NULLIF(CAST(depth_scaled AS DOUBLE PRECISION), 0.0)) AS INTEGER) AS pixel_x,
        CAST(FLOOR({half_height} - CAST(vertical_scaled AS DOUBLE PRECISION) * {focal}
                   / NULLIF(CAST(depth_scaled AS DOUBLE PRECISION), 0.0)) AS INTEGER) AS pixel_y,
        depth_scaled,
        surface_id,
        model_id,
        material_id,
        texture_name,
        world_x,
        world_y,
        world_z,
        surface_kind,
        red,
        green,
        blue,
        light,
        fullbright
    FROM visible_space
), collapsed AS (
    SELECT
        pixel_x,
        pixel_y,
        depth_scaled,
        surface_id,
        model_id,
        material_id,
        texture_name,
        world_x,
        world_y,
        world_z,
        surface_kind,
        red,
        green,
        blue,
        light,
        fullbright,
        COUNT(*) AS observations
    FROM projected
    WHERE pixel_x BETWEEN 0 AND {width - 1}
      AND pixel_y BETWEEN 0 AND {pixel_height - 1}
    GROUP BY
        pixel_x, pixel_y, depth_scaled, surface_id, model_id, material_id, texture_name,
        world_x, world_y, world_z, surface_kind, red, green, blue, light, fullbright
), ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY pixel_x, pixel_y
            ORDER BY depth_scaled, surface_id, world_x, world_y, world_z
        ) AS pixel_rank
    FROM collapsed
)
SELECT
    pixel_x,
    pixel_y,
    depth_scaled,
    world_x,
    world_y,
    world_z,
    CAST(FLOOR(CAST(red AS BIGINT) * CAST((CASE WHEN fullbright = 1 THEN 255 ELSE GREATEST(72, LEAST(255, light * 2)) END) AS BIGINT)
               * GREATEST(88.0, 255.0 - CAST(depth_scaled AS DOUBLE PRECISION)
                   * 150.0 / {max(1, draw_fixed * vector_squared)}) / 65025.0) AS INTEGER) AS shaded_red,
    CAST(FLOOR(CAST(green AS BIGINT) * CAST((CASE WHEN fullbright = 1 THEN 255 ELSE GREATEST(72, LEAST(255, light * 2)) END) AS BIGINT)
               * GREATEST(88.0, 255.0 - CAST(depth_scaled AS DOUBLE PRECISION)
                   * 150.0 / {max(1, draw_fixed * vector_squared)}) / 65025.0) AS INTEGER) AS shaded_green,
    CAST(FLOOR(CAST(blue AS BIGINT) * CAST((CASE WHEN fullbright = 1 THEN 255 ELSE GREATEST(72, LEAST(255, light * 2)) END) AS BIGINT)
               * GREATEST(88.0, 255.0 - CAST(depth_scaled AS DOUBLE PRECISION)
                   * 150.0 / {max(1, draw_fixed * vector_squared)}) / 65025.0) AS INTEGER) AS shaded_blue,
    light,
    surface_kind,
    surface_id,
    material_id,
    texture_name,
    observations,
    pixel_rank,
    model_id
FROM ranked
WHERE pixel_rank <= {int(layers)}
ORDER BY pixel_rank DESC, depth_scaled DESC, pixel_y, pixel_x
    """.strip()


def sql_texture_frame_sql(
    camera: Camera,
    *,
    width: int = 120,
    pixel_height: int = 80,
    table_expr: str = "quakeql_e1m1",
    texture_expr: str = "quakeql_e1m1_texels",
    lightmap_expr: str | None = None,
    material_expr: str | None = None,
    colormap_expr: str | None = None,
    layers: int = 2,
    splat_cap: int = DEFAULT_SPLAT_CAP,
    sample_step: float = 16.0,
    brush_sample_step: float = 4.0,
    mip_bias: int = 0,
    animation_time: float = 0.0,
    brush_offsets: Sequence[tuple[int, float, float, float]] = (),
) -> str:
    """Return a frame query with ray/plane UV reconstruction and SQL texel sampling."""
    if width <= 0 or pixel_height <= 0:
        raise ValueError("frame dimensions must be positive")
    if layers <= 0 or sample_step <= 0 or brush_sample_step <= 0:
        raise ValueError("layers and sample steps must be positive")
    if splat_cap < 0:
        raise ValueError("splat_cap cannot be negative")
    if animation_time < 0:
        raise ValueError("animation_time cannot be negative")
    if not camera.map_name.replace("_", "").isalnum():
        raise ValueError("map name must be alphanumeric")

    yaw = math.radians(camera.yaw % 360.0)
    pitch = math.radians(camera.pitch)
    forward = (
        math.cos(yaw) * math.cos(pitch),
        math.sin(yaw) * math.cos(pitch),
        math.sin(pitch),
    )
    right = (math.sin(yaw), -math.cos(yaw), 0.0)
    up = (
        -math.cos(yaw) * math.sin(pitch),
        -math.sin(yaw) * math.sin(pitch),
        math.cos(pitch),
    )
    focal = projection_focal_length(width)
    half_width = width / 2.0
    half_height = pixel_height / 2.0
    camera_x, camera_y, camera_z = camera.x, camera.y, camera.eye_z
    camera_fixed = tuple(round(value * COORD_SCALE) for value in (camera_x, camera_y, camera_z))
    draw_fixed = camera.draw_distance * COORD_SCALE
    map_name = camera.map_name.upper()
    vector_squared = CAMERA_VECTOR_SCALE * CAMERA_VECTOR_SCALE
    depth_output_scale = vector_squared * COORD_SCALE
    offsets = ",\n        ".join(
        f"({offset_x}, {offset_y})"
        for offset_y in range(-splat_cap, splat_cap + 1)
        for offset_x in range(-splat_cap, splat_cap + 1)
    )
    moving_brushes = {int(model_id): (x, y, z) for model_id, x, y, z in brush_offsets}
    moving_brushes.pop(0, None)
    brush_rows = [(0, 0.0, 0.0, 0.0), *sorted(moving_brushes.items())]
    brush_values = []
    for item in brush_rows:
        if item[0] == 0:
            model_id, offset_x, offset_y, offset_z = item
        else:
            model_id, (offset_x, offset_y, offset_z) = item
        brush_values.append(
            f"({model_id}, {_sql_double(offset_x)}, {_sql_double(offset_y)}, "
            f"{_sql_double(offset_z)})"
        )
    brush_values_sql = ",\n        ".join(brush_values)
    geometry_columns = (
        "surface_id, model_id, material_id, texture_name, world_x, world_y, world_z, "
        "normal_x, normal_y, normal_z, surface_kind, light, plane_distance, "
        "texture_width, texture_height, texture_s_x, texture_s_y, texture_s_z, "
        "texture_s_offset, texture_t_x, texture_t_y, texture_t_z, texture_t_offset"
    )
    coarse = (
        f"world_x BETWEEN {camera_fixed[0] - draw_fixed} AND {camera_fixed[0] + draw_fixed} "
        f"AND world_y BETWEEN {camera_fixed[1] - draw_fixed} AND "
        f"{camera_fixed[1] + draw_fixed} "
        f"AND world_z BETWEEN {camera_fixed[2] - draw_fixed} AND "
        f"{camera_fixed[2] + draw_fixed}"
    )
    forward_sql = ", ".join(_sql_double(value) for value in forward)
    right_sql = ", ".join(_sql_double(value) for value in right)
    up_sql = ", ".join(_sql_double(value) for value in up)
    forward_x, forward_y, forward_z = forward_sql.split(", ")
    right_x, right_y, right_z = right_sql.split(", ")
    up_x, up_y, up_z = up_sql.split(", ")
    camera_x_sql, camera_y_sql, camera_z_sql = (
        _sql_double(value) for value in (camera_x, camera_y, camera_z)
    )
    half_width_sql = _sql_double(half_width)
    half_height_sql = _sql_double(half_height)
    sample_step_sql = _sql_double(sample_step)
    brush_sample_step_sql = _sql_double(brush_sample_step)
    animation_time_sql = _sql_double(animation_time)
    animation_frame = max(0, math.floor(animation_time * 5.0))
    base_mip = (
        f"CASE WHEN texels_per_pixel <= 1.0 THEN 0 "
        f"WHEN texels_per_pixel <= 2.0 THEN 1 "
        f"WHEN texels_per_pixel <= 4.0 THEN 2 ELSE 3 END + {int(mip_bias)}"
    )

    if material_expr:
        material_select = """
        animation.target_material_id AS sampled_material_id,
        animation.target_texture_name AS sampled_texture_name
        """.strip()
        material_join = f"""
    JOIN {material_expr} animation
      ON animation.map_name = '{map_name}'
     AND animation.source_material_id = geometry.material_id
     AND animation.source_texture_name = geometry.texture_name
     AND animation.frame_index = MOD({animation_frame}, animation.frame_count)
        """.strip()
    else:
        material_select = """
        geometry.material_id AS sampled_material_id,
        geometry.texture_name AS sampled_texture_name
        """.strip()
        material_join = ""

    sql_lightmaps = bool(lightmap_expr and colormap_expr)
    if sql_lightmaps:
        light_style = _light_style_case(animation_time, "samples.style_id")
        lighting_ctes = f"""
, light_corners(corner_x, corner_y) AS (
    VALUES (0, 0), (1, 0), (0, 1), (1, 1)
), lightmap_surfaces AS (
    SELECT
        surface_id,
        style_slot,
        style_id,
        light_min_s,
        light_min_t,
        light_width,
        light_height
    FROM {lightmap_expr}
    WHERE map_name = '{map_name}'
    GROUP BY surface_id, style_slot, style_id, light_min_s, light_min_t,
        light_width, light_height
), light_coordinates AS (
    SELECT
        fragments.fragment_id,
        metadata.surface_id,
        metadata.style_slot,
        metadata.style_id,
        corners.corner_x,
        corners.corner_y,
        metadata.light_min_s,
        metadata.light_min_t,
        metadata.light_width,
        metadata.light_height,
        GREATEST(0.0, LEAST(
            CAST(metadata.light_width - 1 AS DOUBLE PRECISION),
            (fragments.texture_s - metadata.light_min_s) / 16.0
        )) AS light_x,
        GREATEST(0.0, LEAST(
            CAST(metadata.light_height - 1 AS DOUBLE PRECISION),
            (fragments.texture_t - metadata.light_min_t) / 16.0
        )) AS light_y
    FROM visible_fragments fragments
    LEFT JOIN lightmap_surfaces metadata
      ON metadata.surface_id = fragments.surface_id
    CROSS JOIN light_corners corners
), light_positions AS (
    SELECT
        coordinates.*,
        CAST(GREATEST(0, LEAST(
            coordinates.light_width - 1,
            CAST(FLOOR(coordinates.light_x) AS INTEGER) + coordinates.corner_x
        )) AS INTEGER) AS light_u,
        CAST(GREATEST(0, LEAST(
            coordinates.light_height - 1,
            CAST(FLOOR(coordinates.light_y) AS INTEGER) + coordinates.corner_y
        )) AS INTEGER) AS light_v,
        (coordinates.light_x - FLOOR(coordinates.light_x)) AS light_fx,
        (coordinates.light_y - FLOOR(coordinates.light_y)) AS light_fy
    FROM light_coordinates coordinates
), light_samples AS (
    SELECT
        positions.*,
        samples.light_value
    FROM light_positions positions
    LEFT JOIN {lightmap_expr} samples
      ON samples.map_name = '{map_name}'
     AND samples.surface_id = positions.surface_id
     AND samples.style_slot = positions.style_slot
     AND samples.light_u = positions.light_u
     AND samples.light_v = positions.light_v
), fragment_lights AS (
    SELECT
        fragments.fragment_id,
        CASE WHEN COUNT(samples.light_value) = 0 THEN MAX(fragments.light)
        ELSE GREATEST(0.0, LEAST(255.0,
            SUM(
                samples.light_value
                * CASE samples.corner_x WHEN 0 THEN 1.0 - samples.light_fx
                    ELSE samples.light_fx END
                * CASE samples.corner_y WHEN 0 THEN 1.0 - samples.light_fy
                    ELSE samples.light_fy END
                * ({light_style})
            ) / 256.0
        )) END AS computed_light
    FROM visible_fragments fragments
    LEFT JOIN light_samples samples ON samples.fragment_id = fragments.fragment_id
    GROUP BY fragments.fragment_id
), lit_fragments AS (
    SELECT
        fragments.*,
        CAST(ROUND(lights.computed_light) AS INTEGER) AS computed_light,
        CAST(GREATEST(0, LEAST(63,
            FLOOR((255.0 - lights.computed_light) * 63.0 / 255.0)
        )) AS INTEGER) AS light_level
    FROM visible_fragments fragments
    JOIN fragment_lights lights ON lights.fragment_id = fragments.fragment_id
), colored_fragments AS (
    SELECT
        fragments.*,
        colormap.red AS render_red,
        colormap.green AS render_green,
        colormap.blue AS render_blue
    FROM lit_fragments fragments
    JOIN {colormap_expr} colormap
      ON colormap.light_level = fragments.light_level
     AND colormap.palette_index = fragments.palette_index
)
        """
    else:
        lighting_ctes = f"""
, colored_fragments AS (
    SELECT
        fragments.*,
        fragments.light AS computed_light,
        CAST(FLOOR(CAST(fragments.texture_red AS BIGINT)
            * CAST(CASE WHEN fragments.fullbright = 1 THEN 255
                ELSE GREATEST(72, LEAST(255, fragments.light * 2)) END AS BIGINT)
            * GREATEST(88.0, 255.0 - fragments.hit_depth * 150.0
                / {camera.draw_distance}) / 65025.0) AS INTEGER) AS render_red,
        CAST(FLOOR(CAST(fragments.texture_green AS BIGINT)
            * CAST(CASE WHEN fragments.fullbright = 1 THEN 255
                ELSE GREATEST(72, LEAST(255, fragments.light * 2)) END AS BIGINT)
            * GREATEST(88.0, 255.0 - fragments.hit_depth * 150.0
                / {camera.draw_distance}) / 65025.0) AS INTEGER) AS render_green,
        CAST(FLOOR(CAST(fragments.texture_blue AS BIGINT)
            * CAST(CASE WHEN fragments.fullbright = 1 THEN 255
                ELSE GREATEST(72, LEAST(255, fragments.light * 2)) END AS BIGINT)
            * GREATEST(88.0, 255.0 - fragments.hit_depth * 150.0
                / {camera.draw_distance}) / 65025.0) AS INTEGER) AS render_blue
    FROM visible_fragments fragments
)
        """

    return f"""
WITH splat_offsets(offset_x, offset_y) AS (
    VALUES
        {offsets}
), brush_offsets(model_id, offset_x, offset_y, offset_z) AS (
    VALUES
        {brush_values_sql}
), transformed_geometry AS (
    SELECT
        source.surface_id,
        source.model_id,
        source.material_id,
        source.texture_name,
        CAST(source.world_x + ROUND(COALESCE(movement.offset_x, 0.0) * {COORD_SCALE})
            AS INTEGER) AS world_x,
        CAST(source.world_y + ROUND(COALESCE(movement.offset_y, 0.0) * {COORD_SCALE})
            AS INTEGER) AS world_y,
        CAST(source.world_z + ROUND(COALESCE(movement.offset_z, 0.0) * {COORD_SCALE})
            AS INTEGER) AS world_z,
        source.normal_x,
        source.normal_y,
        source.normal_z,
        source.surface_kind,
        source.light,
        source.plane_distance
            + CAST(source.normal_x AS DOUBLE PRECISION) / 1024.0
                * COALESCE(movement.offset_x, 0.0)
            + CAST(source.normal_y AS DOUBLE PRECISION) / 1024.0
                * COALESCE(movement.offset_y, 0.0)
            + CAST(source.normal_z AS DOUBLE PRECISION) / 1024.0
                * COALESCE(movement.offset_z, 0.0) AS plane_distance,
        source.texture_width,
        source.texture_height,
        source.texture_s_x,
        source.texture_s_y,
        source.texture_s_z,
        source.texture_s_offset
            - source.texture_s_x * COALESCE(movement.offset_x, 0.0)
            - source.texture_s_y * COALESCE(movement.offset_y, 0.0)
            - source.texture_s_z * COALESCE(movement.offset_z, 0.0) AS texture_s_offset,
        source.texture_t_x,
        source.texture_t_y,
        source.texture_t_z,
        source.texture_t_offset
            - source.texture_t_x * COALESCE(movement.offset_x, 0.0)
            - source.texture_t_y * COALESCE(movement.offset_y, 0.0)
            - source.texture_t_z * COALESCE(movement.offset_z, 0.0) AS texture_t_offset
    FROM {table_expr} source
    LEFT JOIN brush_offsets movement ON movement.model_id = source.model_id
    WHERE source.map_name = '{map_name}'
), source_anchors AS (
    SELECT
        {geometry_columns},
        COUNT(*) AS observations
    FROM transformed_geometry
    WHERE {coarse}
    GROUP BY {geometry_columns}
), camera_space AS (
    SELECT
        *,
        (CAST(world_x AS DOUBLE PRECISION) / {COORD_SCALE} - {camera_x_sql}) AS delta_x,
        (CAST(world_y AS DOUBLE PRECISION) / {COORD_SCALE} - {camera_y_sql}) AS delta_y,
        (CAST(world_z AS DOUBLE PRECISION) / {COORD_SCALE} - {camera_z_sql}) AS delta_z
    FROM source_anchors
), projected_space AS (
    SELECT
        *,
        delta_x * {forward_x} + delta_y * {forward_y} + delta_z * {forward_z}
            AS anchor_depth,
        delta_x * {right_x} + delta_y * {right_y} + delta_z * {right_z}
            AS anchor_right,
        delta_x * {up_x} + delta_y * {up_y} + delta_z * {up_z}
            AS anchor_up
    FROM camera_space
), visible_anchors AS (
    SELECT
        *,
        CAST(FLOOR({half_width_sql} + anchor_right * {focal}
            / NULLIF(anchor_depth, 0.0)) AS INTEGER) AS anchor_pixel_x,
        CAST(FLOOR({half_height_sql} - anchor_up * {focal}
            / NULLIF(anchor_depth, 0.0)) AS INTEGER) AS anchor_pixel_y,
        LEAST(
            {splat_cap},
            GREATEST(
                0,
                CAST(CEIL({focal} * CASE WHEN model_id = 0 THEN {sample_step_sql}
                    ELSE {brush_sample_step_sql} END
                    / NULLIF(anchor_depth, 0.0) / 2.0) AS INTEGER)
            )
        ) AS splat_radius
    FROM projected_space
    WHERE anchor_depth BETWEEN 4.0 AND {camera.draw_distance}
), fragment_candidates AS (
    SELECT
        visible_anchors.*,
        anchor_pixel_x + offset_x AS pixel_x,
        anchor_pixel_y + offset_y AS pixel_y,
        offset_x * offset_x + offset_y * offset_y AS offset_distance
    FROM visible_anchors
    CROSS JOIN splat_offsets
    WHERE anchor_pixel_x + offset_x BETWEEN 0 AND {width - 1}
      AND anchor_pixel_y + offset_y BETWEEN 0 AND {pixel_height - 1}
      AND offset_x * offset_x + offset_y * offset_y
          <= (splat_radius + 0.5) * (splat_radius + 0.5)
), fragment_rays AS (
    SELECT
        *,
        {forward_x} * {focal} + {right_x} * (pixel_x + 0.5 - {half_width_sql})
            + {up_x} * ({half_height_sql} - pixel_y - 0.5) AS ray_x,
        {forward_y} * {focal} + {right_y} * (pixel_x + 0.5 - {half_width_sql})
            + {up_y} * ({half_height_sql} - pixel_y - 0.5) AS ray_y,
        {forward_z} * {focal} + {right_z} * (pixel_x + 0.5 - {half_width_sql})
            + {up_z} * ({half_height_sql} - pixel_y - 0.5) AS ray_z,
        CAST(normal_x AS DOUBLE PRECISION) / 1024.0 AS plane_x,
        CAST(normal_y AS DOUBLE PRECISION) / 1024.0 AS plane_y,
        CAST(normal_z AS DOUBLE PRECISION) / 1024.0 AS plane_z
    FROM fragment_candidates
), ray_intersections AS (
    SELECT
        *,
        (plane_distance - plane_x * {camera_x_sql} - plane_y * {camera_y_sql}
            - plane_z * {camera_z_sql})
        / NULLIF(plane_x * ray_x + plane_y * ray_y + plane_z * ray_z, 0.0) AS ray_t
    FROM fragment_rays
), hit_positions AS (
    SELECT
        *,
        {camera_x_sql} + ray_t * ray_x AS hit_x,
        {camera_y_sql} + ray_t * ray_y AS hit_y,
        {camera_z_sql} + ray_t * ray_z AS hit_z,
        ray_t * {focal} AS hit_depth
    FROM ray_intersections
    WHERE ray_t > 0.0
), texture_coordinates AS (
    SELECT
        *,
        hit_x * texture_s_x + hit_y * texture_s_y + hit_z * texture_s_z
            + texture_s_offset AS texture_s,
        hit_x * texture_t_x + hit_y * texture_t_y + hit_z * texture_t_z
            + texture_t_offset AS texture_t,
        hit_depth / {focal} * GREATEST(
            SQRT(texture_s_x * texture_s_x + texture_s_y * texture_s_y
                + texture_s_z * texture_s_z),
            SQRT(texture_t_x * texture_t_x + texture_t_y * texture_t_y
                + texture_t_z * texture_t_z)
        ) AS texels_per_pixel
    FROM hit_positions
    WHERE hit_depth BETWEEN 4.0 AND {camera.draw_distance}
), warped_coordinates AS (
    SELECT
        *,
        CASE WHEN texture_name LIKE '*%' THEN texture_s
                + SIN(texture_t * 0.125 + {animation_time_sql} * 2.0) * 8.0
            WHEN LOWER(texture_name) LIKE 'sky%' THEN texture_s + {animation_time_sql} * 8.0
            ELSE texture_s END AS sampled_texture_s,
        CASE WHEN texture_name LIKE '*%' THEN texture_t
                + SIN(texture_s * 0.125 + {animation_time_sql} * 2.0 + 1.5707963267948966)
                    * 8.0
            WHEN LOWER(texture_name) LIKE 'sky%' THEN texture_t + {animation_time_sql} * 4.0
            ELSE texture_t END AS sampled_texture_t
    FROM texture_coordinates
), mip_choice AS (
    SELECT
        *,
        GREATEST(0, LEAST(3, {base_mip})) AS mip_level
    FROM warped_coordinates
), mip_geometry AS (
    SELECT
        *,
        CASE mip_level WHEN 0 THEN 1 WHEN 1 THEN 2 WHEN 2 THEN 4 ELSE 8 END AS mip_scale,
        GREATEST(1, CAST(FLOOR(texture_width
            / CASE mip_level WHEN 0 THEN 1 WHEN 1 THEN 2 WHEN 2 THEN 4 ELSE 8 END)
            AS INTEGER)) AS mip_width,
        GREATEST(1, CAST(FLOOR(texture_height
            / CASE mip_level WHEN 0 THEN 1 WHEN 1 THEN 2 WHEN 2 THEN 4 ELSE 8 END)
            AS INTEGER)) AS mip_height
    FROM mip_choice
), wrapped_texels AS (
    SELECT
        *,
        CAST(FLOOR(sampled_texture_s / mip_scale)
            - FLOOR(FLOOR(sampled_texture_s / mip_scale) / mip_width) * mip_width AS INTEGER)
            AS texel_u,
        CAST(FLOOR(sampled_texture_t / mip_scale)
            - FLOOR(FLOOR(sampled_texture_t / mip_scale) / mip_height) * mip_height AS INTEGER)
            AS texel_v
    FROM mip_geometry
), ranked_geometry AS (
    SELECT
        geometry.*,
        ROW_NUMBER() OVER (
            PARTITION BY geometry.pixel_x, geometry.pixel_y,
                geometry.surface_id, geometry.model_id
            ORDER BY geometry.offset_distance, geometry.anchor_depth,
                geometry.world_x, geometry.world_y, geometry.world_z
        ) AS surface_sample_rank
    FROM wrapped_texels geometry
), surface_geometry AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            ORDER BY pixel_y, pixel_x, hit_depth, surface_id, model_id
        ) AS fragment_id
    FROM ranked_geometry
    WHERE surface_sample_rank = 1
), material_geometry AS (
    SELECT
        geometry.*,
        {material_select}
    FROM surface_geometry geometry
    {material_join}
), sampled_fragments AS (
    SELECT
        geometry.*,
        texels.palette_index,
        texels.red AS texture_red,
        texels.green AS texture_green,
        texels.blue AS texture_blue,
        texels.fullbright
    FROM material_geometry geometry
    JOIN {texture_expr} texels
      ON texels.map_name = '{map_name}'
     AND texels.material_id = geometry.sampled_material_id
     AND texels.texture_name = geometry.sampled_texture_name
     AND texels.mip_level = geometry.mip_level
     AND texels.texel_u = geometry.texel_u
     AND texels.texel_v = geometry.texel_v
), ranked_pixels AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY pixel_x, pixel_y
            ORDER BY hit_depth, surface_id, model_id
        ) AS pixel_rank
    FROM sampled_fragments
), visible_fragments AS (
    SELECT *
    FROM ranked_pixels
    WHERE pixel_rank <= {int(layers)}
)
{lighting_ctes}
SELECT
    pixel_x,
    pixel_y,
    CAST(ROUND(hit_depth * {depth_output_scale}) AS BIGINT) AS depth_scaled,
    CAST(ROUND(hit_x * {COORD_SCALE}) AS INTEGER) AS world_x,
    CAST(ROUND(hit_y * {COORD_SCALE}) AS INTEGER) AS world_y,
    CAST(ROUND(hit_z * {COORD_SCALE}) AS INTEGER) AS world_z,
    render_red AS shaded_red,
    render_green AS shaded_green,
    render_blue AS shaded_blue,
    computed_light AS light,
    surface_kind,
    surface_id,
    sampled_material_id AS material_id,
    sampled_texture_name AS texture_name,
    observations,
    pixel_rank,
    model_id
FROM colored_fragments
ORDER BY pixel_rank DESC, hit_depth DESC, pixel_y, pixel_x
""".strip()


def coerce_points(rows: Iterable[Sequence[object]]) -> list[FramePoint]:
    return [
        FramePoint(
            int(row[0]),
            int(row[1]),
            int(row[2]),
            int(row[3]),
            int(row[4]),
            int(row[5]),
            int(row[6]),
            int(row[7]),
            int(row[8]),
            int(row[9]),
            int(row[10]),
            int(row[11]),
            int(row[12]),
            str(row[13]),
            int(row[14]),
            int(row[15]),
            int(row[16]),
        )
        for row in rows
    ]


def render_frame(
    rows: Iterable[Sequence[object]] | Iterable[FramePoint],
    camera: Camera,
    *,
    width: int = 120,
    height: int = 40,
    render_type: str = "ansi-half",
    sample_step: float = 16.0,
    brush_sample_step: float = 4.0,
    splat_cap: int = DEFAULT_SPLAT_CAP,
) -> str:
    if render_type not in RENDER_TYPES:
        raise ValueError(f"unsupported render type: {render_type}")
    pixel_height = height * 2 if render_type == "ansi-half" else height
    colors = render_pixels(
        rows,
        camera,
        width=width,
        pixel_height=pixel_height,
        sample_step=sample_step,
        brush_sample_step=brush_sample_step,
        splat_cap=splat_cap,
    )
    if render_type == "ansi-half":
        return _encode_ansi_half(colors)
    glyphs = " .,:;irsXA253hMHGS#9B&@"
    lines: list[str] = []
    for row in colors:
        line = []
        for red, green, blue in row:
            luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
            amplified = min(255.0, luminance * 1.8)
            line.append(glyphs[min(len(glyphs) - 1, round(amplified / 255 * (len(glyphs) - 1)))])
        lines.append("".join(line))
    return "\n".join(lines)


def render_pixels(
    rows: Iterable[Sequence[object]] | Iterable[FramePoint],
    camera: Camera,
    *,
    width: int,
    pixel_height: int,
    sample_step: float = 16.0,
    brush_sample_step: float = 4.0,
    splat_cap: int = DEFAULT_SPLAT_CAP,
) -> list[list[tuple[int, int, int]]]:
    if splat_cap < 0:
        raise ValueError("splat_cap cannot be negative")
    points = list(rows)
    if points and not isinstance(points[0], FramePoint):
        points = coerce_points(points)  # type: ignore[arg-type]
    colors = [[(5, 7, 10) for _ in range(width)] for _ in range(pixel_height)]
    depth_buffer = [[math.inf for _ in range(width)] for _ in range(pixel_height)]
    focal = projection_focal_length(width)
    scale = CAMERA_VECTOR_SCALE * CAMERA_VECTOR_SCALE * COORD_SCALE
    for point in points:  # type: ignore[assignment]
        depth = point.depth_scaled / scale
        if depth <= 0:
            continue
        point_step = brush_sample_step if point.model_id else sample_step
        radius = min(
            splat_cap,
            max(0, math.ceil(focal * point_step / max(1.0, depth) / 2.0)),
        )
        for pixel_y in range(
            max(0, point.pixel_y - radius), min(pixel_height, point.pixel_y + radius + 1)
        ):
            for pixel_x in range(
                max(0, point.pixel_x - radius), min(width, point.pixel_x + radius + 1)
            ):
                if (
                    radius
                    and (pixel_x - point.pixel_x) ** 2 + (pixel_y - point.pixel_y) ** 2
                    > (radius + 0.5) ** 2
                ):
                    continue
                if point.depth_scaled >= depth_buffer[pixel_y][pixel_x]:
                    continue
                depth_buffer[pixel_y][pixel_x] = point.depth_scaled
                colors[pixel_y][pixel_x] = (
                    max(0, min(255, point.red)),
                    max(0, min(255, point.green)),
                    max(0, min(255, point.blue)),
                )
    _fill_pinholes(colors, depth_buffer, passes=1)
    return colors


def _fill_pinholes(
    colors: list[list[tuple[int, int, int]]],
    depths: list[list[float]],
    passes: int,
) -> None:
    if not colors:
        return
    height, width = len(colors), len(colors[0])
    for _ in range(passes):
        updates: list[tuple[int, int, tuple[int, int, int], float]] = []
        for y in range(1, height - 1):
            for x in range(1, width - 1):
                if math.isfinite(depths[y][x]):
                    continue
                neighbors = [
                    (depths[y - 1][x], colors[y - 1][x]),
                    (depths[y + 1][x], colors[y + 1][x]),
                    (depths[y][x - 1], colors[y][x - 1]),
                    (depths[y][x + 1], colors[y][x + 1]),
                ]
                finite = [(depth, color) for depth, color in neighbors if math.isfinite(depth)]
                if len(finite) < 3:
                    continue
                depth, color = min(finite, key=lambda item: item[0])
                updates.append((x, y, color, depth))
        for x, y, color, depth in updates:
            colors[y][x] = color
            depths[y][x] = depth


def _encode_ansi_half(colors: list[list[tuple[int, int, int]]]) -> str:
    lines: list[str] = []
    for y in range(0, len(colors), 2):
        top = colors[y]
        bottom = colors[min(y + 1, len(colors) - 1)]
        chunks: list[str] = []
        previous: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None
        for foreground, background in zip(top, bottom):
            pair = (foreground, background)
            if pair != previous:
                chunks.append(
                    f"\x1b[38;2;{foreground[0]};{foreground[1]};{foreground[2]}m"
                    f"\x1b[48;2;{background[0]};{background[1]};{background[2]}m"
                )
                previous = pair
            chunks.append("▀")
        chunks.append("\x1b[0m")
        lines.append("".join(chunks))
    return "\n".join(lines)


def frame_hash(frame: str) -> str:
    return hashlib.sha256(frame.encode("utf-8")).hexdigest()[:16]


def move_with_collision(
    bsp: CollisionWorld | QuakeBsp,
    start: tuple[float, float, float],
    target: tuple[float, float, float],
    step_height: float = 18.0,
    brush_origins: dict[int, Sequence[float]] | None = None,
) -> tuple[float, float, float] | None:
    travel = math.dist(start[:2], target[:2])
    segments = max(1, math.ceil(travel / 4.0))
    current = start
    for segment in range(1, segments + 1):
        ratio = segment / segments
        desired = (
            start[0] + (target[0] - start[0]) * ratio,
            start[1] + (target[1] - start[1]) * ratio,
            current[2],
        )
        resolved = _resolve_step(bsp, desired, step_height, brush_origins)
        if resolved is None:
            return None
        current = resolved
    return current


def _resolve_step(
    bsp: CollisionWorld | QuakeBsp,
    target: tuple[float, float, float],
    step_height: float,
    brush_origins: dict[int, Sequence[float]] | None = None,
) -> tuple[float, float, float] | None:
    candidate: tuple[float, float, float] | None = None
    for rise in range(0, round(step_height) + 1, 2):
        point = (target[0], target[1], target[2] + rise)
        if not bsp.position_is_solid(point, hull=1, brush_origins=brush_origins):
            candidate = point
            break
    if candidate is None:
        return None
    settled = candidate
    for drop in range(2, 27, 2):
        point = (candidate[0], candidate[1], candidate[2] - drop)
        if bsp.position_is_solid(point, hull=1, brush_origins=brush_origins):
            break
        settled = point
    return settled


def scripted_cameras(
    start: Camera, frames: int, bsp: CollisionWorld | QuakeBsp | None = None
) -> list[Camera]:
    if frames <= 0:
        raise ValueError("frames must be positive")
    cameras = [start]
    camera = start
    pattern = (
        ("move", 24.0),
        ("move", 24.0),
        ("yaw", -15.0),
        ("move", 20.0),
        ("pitch", 8.0),
        ("yaw", 30.0),
        ("strafe", -20.0),
        ("pitch", -12.0),
        ("move", -16.0),
        ("yaw", 45.0),
        ("strafe", 20.0),
        ("pitch", 4.0),
    )
    while len(cameras) < frames:
        action, amount = pattern[(len(cameras) - 1) % len(pattern)]
        if action == "move":
            camera = camera.moved(amount, bsp)
        elif action == "strafe":
            camera = camera.moved(amount, bsp, strafe=True)
        elif action == "yaw":
            camera = camera.turned(yaw=amount)
        else:
            camera = camera.turned(pitch=amount)
        cameras.append(camera)
    return cameras


def samples_to_arrow(samples: Sequence[SurfaceSample], map_name: str):
    import pyarrow as pa

    columns = {
        "base_id": pa.array((sample.base_id for sample in samples), type=pa.int64()),
        "map_name": pa.array((map_name for _ in samples), type=pa.string()).dictionary_encode(),
        "surface_id": pa.array((sample.surface_id for sample in samples), type=pa.int32()),
        "model_id": pa.array((sample.model_id for sample in samples), type=pa.int16()),
        "world_x": pa.array((sample.world_x for sample in samples), type=pa.int32()),
        "world_y": pa.array((sample.world_y for sample in samples), type=pa.int32()),
        "world_z": pa.array((sample.world_z for sample in samples), type=pa.int32()),
        "normal_x": pa.array((sample.normal_x for sample in samples), type=pa.int16()),
        "normal_y": pa.array((sample.normal_y for sample in samples), type=pa.int16()),
        "normal_z": pa.array((sample.normal_z for sample in samples), type=pa.int16()),
        "surface_kind": pa.array((sample.surface_kind for sample in samples), type=pa.int16()),
        "material_id": pa.array((sample.material_id for sample in samples), type=pa.int16()),
        "texture_name": pa.array(
            (sample.texture_name for sample in samples), type=pa.string()
        ).dictionary_encode(),
        "texture_u": pa.array((sample.texture_u for sample in samples), type=pa.int16()),
        "texture_v": pa.array((sample.texture_v for sample in samples), type=pa.int16()),
        "red": pa.array((sample.red for sample in samples), type=pa.int16()),
        "green": pa.array((sample.green for sample in samples), type=pa.int16()),
        "blue": pa.array((sample.blue for sample in samples), type=pa.int16()),
        "light": pa.array((sample.light for sample in samples), type=pa.int16()),
        "fullbright": pa.array((sample.fullbright for sample in samples), type=pa.int16()),
        "plane_distance": pa.array(
            (sample.plane_distance for sample in samples), type=pa.float64()
        ),
        "texture_width": pa.array((sample.texture_width for sample in samples), type=pa.int16()),
        "texture_height": pa.array((sample.texture_height for sample in samples), type=pa.int16()),
        "texture_s_x": pa.array((sample.texture_s_x for sample in samples), type=pa.float64()),
        "texture_s_y": pa.array((sample.texture_s_y for sample in samples), type=pa.float64()),
        "texture_s_z": pa.array((sample.texture_s_z for sample in samples), type=pa.float64()),
        "texture_s_offset": pa.array(
            (sample.texture_s_offset for sample in samples), type=pa.float64()
        ),
        "texture_t_x": pa.array((sample.texture_t_x for sample in samples), type=pa.float64()),
        "texture_t_y": pa.array((sample.texture_t_y for sample in samples), type=pa.float64()),
        "texture_t_z": pa.array((sample.texture_t_z for sample in samples), type=pa.float64()),
        "texture_t_offset": pa.array(
            (sample.texture_t_offset for sample in samples), type=pa.float64()
        ),
    }
    return pa.table(columns)


def texture_texels_to_arrow(bsp: QuakeBsp):
    import pyarrow as pa

    rows: list[tuple[object, ...]] = []
    for material_id, texture in enumerate(bsp.textures):
        if texture is None:
            continue
        for mip_level, pixels in enumerate(texture.mip_pixels):
            mip_width = max(1, texture.width >> mip_level)
            mip_height = max(1, texture.height >> mip_level)
            for index, palette_index in enumerate(pixels):
                red, green, blue = bsp.palette[palette_index]
                rows.append(
                    (
                        bsp.map_name,
                        material_id,
                        texture.name,
                        mip_level,
                        mip_width,
                        mip_height,
                        index % mip_width,
                        index // mip_width,
                        palette_index,
                        red,
                        green,
                        blue,
                        int(palette_index >= 224),
                    )
                )
    return pa.table(
        {
            name: pa.array((row[index] for row in rows), type=data_type)
            for index, (name, data_type) in enumerate(
                (
                    ("map_name", pa.string()),
                    ("material_id", pa.int16()),
                    ("texture_name", pa.string()),
                    ("mip_level", pa.int16()),
                    ("mip_width", pa.int16()),
                    ("mip_height", pa.int16()),
                    ("texel_u", pa.int16()),
                    ("texel_v", pa.int16()),
                    ("palette_index", pa.int16()),
                    ("red", pa.int16()),
                    ("green", pa.int16()),
                    ("blue", pa.int16()),
                    ("fullbright", pa.int16()),
                )
            )
        }
    )


def create_texture_parquet(path: Path, bsp: QuakeBsp, row_group_size: int = 250_000) -> int:
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    texels = texture_texels_to_arrow(bsp)
    pq.write_table(texels, path, compression="zstd", row_group_size=row_group_size)
    return texels.num_rows


def create_lightmap_parquet(
    path: Path,
    map_name: str,
    texels: Sequence[LightmapTexel],
    row_group_size: int = 250_000,
) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "map_name": pa.array((map_name for _ in texels), type=pa.string()),
            "surface_id": pa.array((texel.surface_id for texel in texels), type=pa.int32()),
            "style_slot": pa.array((texel.style_slot for texel in texels), type=pa.int16()),
            "style_id": pa.array((texel.style_id for texel in texels), type=pa.int16()),
            "light_min_s": pa.array((texel.light_min_s for texel in texels), type=pa.int32()),
            "light_min_t": pa.array((texel.light_min_t for texel in texels), type=pa.int32()),
            "light_width": pa.array((texel.light_width for texel in texels), type=pa.int16()),
            "light_height": pa.array((texel.light_height for texel in texels), type=pa.int16()),
            "light_u": pa.array((texel.light_u for texel in texels), type=pa.int16()),
            "light_v": pa.array((texel.light_v for texel in texels), type=pa.int16()),
            "light_value": pa.array((texel.light_value for texel in texels), type=pa.int16()),
        }
    )
    pq.write_table(table, path, compression="zstd", row_group_size=row_group_size)
    return table.num_rows


def create_material_parquet(
    path: Path,
    map_name: str,
    frames: Sequence[MaterialFrame],
    row_group_size: int = 10_000,
) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "map_name": pa.array((map_name for _ in frames), type=pa.string()),
            "source_material_id": pa.array(
                (frame.source_material_id for frame in frames), type=pa.int16()
            ),
            "source_texture_name": pa.array(
                (frame.source_texture_name for frame in frames), type=pa.string()
            ),
            "frame_index": pa.array((frame.frame_index for frame in frames), type=pa.int16()),
            "frame_count": pa.array((frame.frame_count for frame in frames), type=pa.int16()),
            "target_material_id": pa.array(
                (frame.target_material_id for frame in frames), type=pa.int16()
            ),
            "target_texture_name": pa.array(
                (frame.target_texture_name for frame in frames), type=pa.string()
            ),
        }
    )
    pq.write_table(table, path, compression="zstd", row_group_size=row_group_size)
    return table.num_rows


def create_colormap_parquet(path: Path, palette: bytes, colormap: bytes) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    if len(palette) < 768 or len(colormap) < 64 * 256:
        raise ValueError("Quake palette/colormap data is truncated")
    rows = []
    for light_level in range(64):
        for palette_index in range(256):
            mapped = colormap[light_level * 256 + palette_index]
            offset = mapped * 3
            rows.append(
                (
                    light_level,
                    palette_index,
                    mapped,
                    palette[offset],
                    palette[offset + 1],
                    palette[offset + 2],
                )
            )
    table = pa.table(
        {
            "light_level": pa.array((row[0] for row in rows), type=pa.int16()),
            "palette_index": pa.array((row[1] for row in rows), type=pa.int16()),
            "mapped_palette_index": pa.array((row[2] for row in rows), type=pa.int16()),
            "red": pa.array((row[3] for row in rows), type=pa.int16()),
            "green": pa.array((row[4] for row in rows), type=pa.int16()),
            "blue": pa.array((row[5] for row in rows), type=pa.int16()),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd", row_group_size=16_384)
    return table.num_rows


def runtime_tables_to_arrow(bsp: QuakeBsp) -> dict[str, object]:
    import pyarrow as pa

    spawn, yaw = bsp.spawn()
    world = bsp.models[0]
    maps = pa.table(
        {
            "map_name": pa.array([bsp.map_name], type=pa.string()),
            "spawn_x": pa.array([spawn[0]], type=pa.float64()),
            "spawn_y": pa.array([spawn[1]], type=pa.float64()),
            "spawn_z": pa.array([spawn[2]], type=pa.float64()),
            "spawn_yaw": pa.array([yaw], type=pa.float64()),
            "min_x": pa.array([world.mins[0]], type=pa.float64()),
            "min_y": pa.array([world.mins[1]], type=pa.float64()),
            "min_z": pa.array([world.mins[2]], type=pa.float64()),
            "max_x": pa.array([world.maxs[0]], type=pa.float64()),
            "max_y": pa.array([world.maxs[1]], type=pa.float64()),
            "max_z": pa.array([world.maxs[2]], type=pa.float64()),
        }
    )
    planes = pa.table(
        {
            "map_name": pa.array((bsp.map_name for _ in bsp.planes), type=pa.string()),
            "plane_id": pa.array(range(len(bsp.planes)), type=pa.int32()),
            "normal_x": pa.array((plane.normal[0] for plane in bsp.planes), type=pa.float64()),
            "normal_y": pa.array((plane.normal[1] for plane in bsp.planes), type=pa.float64()),
            "normal_z": pa.array((plane.normal[2] for plane in bsp.planes), type=pa.float64()),
            "distance": pa.array((plane.distance for plane in bsp.planes), type=pa.float64()),
            "plane_kind": pa.array((plane.kind for plane in bsp.planes), type=pa.int16()),
        }
    )
    clipnodes = pa.table(
        {
            "map_name": pa.array((bsp.map_name for _ in bsp.clipnodes), type=pa.string()),
            "clipnode_id": pa.array(range(len(bsp.clipnodes)), type=pa.int32()),
            "plane_id": pa.array((node.plane_id for node in bsp.clipnodes), type=pa.int32()),
            "child_front": pa.array((node.children[0] for node in bsp.clipnodes), type=pa.int32()),
            "child_back": pa.array((node.children[1] for node in bsp.clipnodes), type=pa.int32()),
        }
    )
    models = pa.table(
        {
            "map_name": pa.array((bsp.map_name for _ in bsp.models), type=pa.string()),
            "model_id": pa.array(range(len(bsp.models)), type=pa.int32()),
            "min_x": pa.array((model.mins[0] for model in bsp.models), type=pa.float64()),
            "min_y": pa.array((model.mins[1] for model in bsp.models), type=pa.float64()),
            "min_z": pa.array((model.mins[2] for model in bsp.models), type=pa.float64()),
            "max_x": pa.array((model.maxs[0] for model in bsp.models), type=pa.float64()),
            "max_y": pa.array((model.maxs[1] for model in bsp.models), type=pa.float64()),
            "max_z": pa.array((model.maxs[2] for model in bsp.models), type=pa.float64()),
            "origin_x": pa.array((model.origin[0] for model in bsp.models), type=pa.float64()),
            "origin_y": pa.array((model.origin[1] for model in bsp.models), type=pa.float64()),
            "origin_z": pa.array((model.origin[2] for model in bsp.models), type=pa.float64()),
            "headnode_0": pa.array((model.headnodes[0] for model in bsp.models), type=pa.int32()),
            "headnode_1": pa.array((model.headnodes[1] for model in bsp.models), type=pa.int32()),
            "headnode_2": pa.array((model.headnodes[2] for model in bsp.models), type=pa.int32()),
            "headnode_3": pa.array((model.headnodes[3] for model in bsp.models), type=pa.int32()),
            "visleafs": pa.array((model.visleafs for model in bsp.models), type=pa.int32()),
            "first_face": pa.array((model.first_face for model in bsp.models), type=pa.int32()),
            "face_count": pa.array((model.face_count for model in bsp.models), type=pa.int32()),
        }
    )
    brushes = bsp.brush_entities()
    brush_table = pa.table(
        {
            "map_name": pa.array((bsp.map_name for _ in brushes), type=pa.string()),
            "entity_id": pa.array((brush.entity_id for brush in brushes), type=pa.int32()),
            "model_id": pa.array((brush.model_id for brush in brushes), type=pa.int32()),
            "classname": pa.array((brush.classname for brush in brushes), type=pa.string()),
            "origin_x": pa.array((brush.origin[0] for brush in brushes), type=pa.float64()),
            "origin_y": pa.array((brush.origin[1] for brush in brushes), type=pa.float64()),
            "origin_z": pa.array((brush.origin[2] for brush in brushes), type=pa.float64()),
            "solid": pa.array((brush.solid for brush in brushes), type=pa.bool_()),
            "mover": pa.array((brush.mover for brush in brushes), type=pa.bool_()),
            "targetname": pa.array((brush.targetname for brush in brushes), type=pa.string()),
            "target": pa.array((brush.target for brush in brushes), type=pa.string()),
            "closed_x": pa.array((brush.closed_origin[0] for brush in brushes), type=pa.float64()),
            "closed_y": pa.array((brush.closed_origin[1] for brush in brushes), type=pa.float64()),
            "closed_z": pa.array((brush.closed_origin[2] for brush in brushes), type=pa.float64()),
            "open_x": pa.array((brush.open_origin[0] for brush in brushes), type=pa.float64()),
            "open_y": pa.array((brush.open_origin[1] for brush in brushes), type=pa.float64()),
            "open_z": pa.array((brush.open_origin[2] for brush in brushes), type=pa.float64()),
            "speed": pa.array((brush.speed for brush in brushes), type=pa.float64()),
        }
    )
    return {
        "maps": maps,
        "planes": planes,
        "clipnodes": clipnodes,
        "models": models,
        "brushes": brush_table,
    }


def create_runtime_parquets(bsp: QuakeBsp, paths: dict[str, Path]) -> dict[str, int]:
    import pyarrow.parquet as pq

    tables = runtime_tables_to_arrow(bsp)
    if set(paths) != set(tables):
        raise ValueError("runtime parquet paths must cover every runtime relation")
    counts: dict[str, int] = {}
    for name, table in tables.items():
        path = paths[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, path, compression="zstd", row_group_size=250_000)
        counts[name] = table.num_rows
    return counts


def create_parquet(
    path: Path,
    world: RasterizedMap,
    rows: int,
    row_group_size: int = 1_000_000,
) -> None:
    import duckdb

    if rows < len(world.samples):
        raise ValueError(f"rows must be at least the {len(world.samples):,} base surface samples")
    path.parent.mkdir(parents=True, exist_ok=True)
    base = samples_to_arrow(world.samples, world.map_name)
    escaped = str(path).replace("'", "''")
    with duckdb.connect(":memory:") as conn:
        conn.register("quake_base", base)
        conn.execute(
            f"""
            COPY (
                SELECT
                    CAST(ids.range AS BIGINT) AS sample_id,
                    CAST(ids.range // {len(world.samples)} AS INTEGER) AS scan_id,
                    b.map_name,
                    b.surface_id,
                    b.model_id,
                    b.world_x,
                    b.world_y,
                    b.world_z,
                    b.normal_x,
                    b.normal_y,
                    b.normal_z,
                    b.surface_kind,
                    b.material_id,
                    b.texture_name,
                    b.texture_u,
                    b.texture_v,
                    b.red,
                    b.green,
                    b.blue,
                    b.light,
                    b.fullbright,
                    b.plane_distance,
                    b.texture_width,
                    b.texture_height,
                    b.texture_s_x,
                    b.texture_s_y,
                    b.texture_s_z,
                    b.texture_s_offset,
                    b.texture_t_x,
                    b.texture_t_y,
                    b.texture_t_z,
                    b.texture_t_offset
                FROM range({int(rows)}) ids
                JOIN quake_base b ON b.base_id = ids.range % {len(world.samples)}
                ORDER BY ids.range
            ) TO '{escaped}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {int(row_group_size)})
            """
        )


def combine_geometry_parquets(
    path: Path,
    sources: Sequence[Path],
    base_rows: int,
    rows: int,
    row_group_size: int = 1_000_000,
) -> None:
    import duckdb

    if not sources:
        raise ValueError("at least one geometry parquet is required")
    if rows < base_rows:
        raise ValueError(f"rows must be at least the {base_rows:,} natural geometry samples")
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped_path = str(path).replace("'", "''")
    source_list = ", ".join("'" + str(source).replace("'", "''") + "'" for source in sources)
    data_columns = ", ".join(TABLE_COLUMNS[2:])
    with duckdb.connect(":memory:") as conn:
        if rows == base_rows:
            query = f"""
                SELECT
                    CAST(ROW_NUMBER() OVER () - 1 AS BIGINT) AS sample_id,
                    CAST(0 AS INTEGER) AS scan_id,
                    {data_columns}
                FROM read_parquet([{source_list}])
            """
        else:
            query = f"""
                WITH natural_rows AS (
                    SELECT
                        CAST(ROW_NUMBER() OVER () - 1 AS BIGINT) AS base_id,
                        {data_columns}
                    FROM read_parquet([{source_list}])
                )
                SELECT
                    CAST(ids.range AS BIGINT) AS sample_id,
                    CAST(ids.range // {base_rows} AS INTEGER) AS scan_id,
                    {data_columns}
                FROM range({rows}) ids
                JOIN natural_rows base ON base.base_id = ids.range % {base_rows}
                ORDER BY ids.range
            """
        conn.execute(
            f"""
            COPY ({query}) TO '{escaped_path}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {int(row_group_size)})
            """
        )


def combine_texture_parquets(
    path: Path,
    sources: Sequence[Path],
    row_group_size: int = 250_000,
) -> int:
    return combine_relation_parquets(path, sources, TEXTURE_COLUMNS, row_group_size)


def combine_relation_parquets(
    path: Path,
    sources: Sequence[Path],
    columns: Sequence[str],
    row_group_size: int = 250_000,
) -> int:
    import duckdb

    if not sources:
        raise ValueError("at least one source parquet is required")
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped_path = str(path).replace("'", "''")
    source_list = ", ".join("'" + str(source).replace("'", "''") + "'" for source in sources)
    with duckdb.connect(":memory:") as conn:
        row_count = conn.execute(f"SELECT COUNT(*) FROM read_parquet([{source_list}])").fetchone()[
            0
        ]
        conn.execute(
            f"""
            COPY (
                SELECT {", ".join(columns)}
                FROM read_parquet([{source_list}])
            ) TO '{escaped_path}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {int(row_group_size)})
            """
        )
    return int(row_count)
