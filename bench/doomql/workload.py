"""Dataset, query, and terminal rendering primitives for DoomQL."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:
    from .wad_world import (
        DEFAULT_GRID_SCALE,
        SURFACE_CEILING,
        SURFACE_FLOOR,
        SURFACE_MASKED,
        SURFACE_SKY,
        SURFACE_WALL,
    )
except ImportError:
    from wad_world import (
        DEFAULT_GRID_SCALE,
        SURFACE_CEILING,
        SURFACE_FLOOR,
        SURFACE_MASKED,
        SURFACE_SKY,
        SURFACE_WALL,
    )


WORLD_SIZE = 256
WORLD_HEIGHT = 16
VOXELS_PER_SCAN = WORLD_SIZE * WORLD_SIZE * WORLD_HEIGHT
CAMERA_VECTOR_SCALE = 1024
DEFAULT_FOV_DEGREES = 75
RENDER_TYPES = {"ascii", "ansi-half"}
TABLE_COLUMNS = (
    "sample_id",
    "scan_id",
    "world_x",
    "world_y",
    "world_z",
    "solid",
    "material",
    "light",
)


@dataclass(frozen=True)
class Camera:
    x: int = 18
    y: int = 112
    z: int = 8
    heading: int = 0
    draw_distance: int = 128

    def moved(self, amount: int) -> "Camera":
        direction_x, direction_y = camera_vector(self.heading)
        target_x = round(self.x + direction_x * amount / CAMERA_VECTOR_SCALE)
        target_y = round(self.y + direction_y * amount / CAMERA_VECTOR_SCALE)
        target_x = min(WORLD_SIZE - 2, max(1, target_x))
        target_y = min(WORLD_SIZE - 2, max(1, target_y))
        if wall_material(target_x, target_y) != 0:
            return self
        return Camera(target_x, target_y, self.z, self.heading, self.draw_distance)

    def turned(self, amount: int) -> "Camera":
        return Camera(self.x, self.y, self.z, (self.heading + amount) % 360, self.draw_distance)


@dataclass(frozen=True)
class RayHit:
    lateral_scaled: int
    material: int
    nearest_depth: int
    avg_light: float
    samples: int


@dataclass(frozen=True)
class SurfaceHit:
    lateral_scaled: int
    depth_scaled: int
    z_bottom: int
    z_top: int
    surface_kind: int
    material: int
    sector_id: int
    avg_light: float
    samples: int
    surface_id: int


def wall_material(x: int, y: int) -> int:
    """Match the deterministic wall classifier used by the generated dataset."""
    if x in {0, WORLD_SIZE - 1} or y in {0, WORLD_SIZE - 1}:
        return 1
    if x % 32 == 0 and (y + (x // 32) * 7) % 48 not in range(20, 28):
        return 2
    if y % 40 == 0 and (x + (y // 40) * 11) % 56 not in range(24, 32):
        return 3
    if any((x - px) ** 2 + (y - py) ** 2 <= 9 for px, py in ((80, 80), (144, 104), (208, 176))):
        return 4
    return 0


def camera_vector(heading: int) -> tuple[int, int]:
    """Return a deterministic fixed-point forward vector for an angle in degrees."""
    radians = math.radians(heading % 360)
    return (
        round(math.cos(radians) * CAMERA_VECTOR_SCALE),
        round(math.sin(radians) * CAMERA_VECTOR_SCALE),
    )


def projection_focal_length(width: int, fov_degrees: int = DEFAULT_FOV_DEGREES) -> int:
    return max(1, round((width / 2) / math.tan(math.radians(fov_degrees / 2))))


def world_select_sql(rows: int) -> str:
    """Return DuckDB SQL that deterministically generates `rows` observations."""
    if rows <= 0:
        raise ValueError("rows must be positive")
    return f"""
WITH ids AS (
    SELECT CAST(range AS BIGINT) AS sample_id
    FROM range({int(rows)})
), coords AS (
    SELECT
        sample_id,
        CAST(sample_id // {VOXELS_PER_SCAN} AS INTEGER) AS scan_id,
        CAST(sample_id % {WORLD_SIZE} AS SMALLINT) AS world_x,
        CAST((sample_id // {WORLD_SIZE}) % {WORLD_SIZE} AS SMALLINT) AS world_y,
        CAST((sample_id // {WORLD_SIZE * WORLD_SIZE}) % {WORLD_HEIGHT} AS SMALLINT) AS world_z
    FROM ids
), classified AS (
    SELECT
        *,
        CASE
            WHEN world_x IN (0, {WORLD_SIZE - 1}) OR world_y IN (0, {WORLD_SIZE - 1}) THEN 1
            WHEN world_x % 32 = 0
                 AND (world_y + (world_x // 32) * 7) % 48 NOT BETWEEN 20 AND 27 THEN 2
            WHEN world_y % 40 = 0
                 AND (world_x + (world_y // 40) * 11) % 56 NOT BETWEEN 24 AND 31 THEN 3
            WHEN ((CAST(world_x AS INTEGER) - 80) * (CAST(world_x AS INTEGER) - 80)
                  + (CAST(world_y AS INTEGER) - 80) * (CAST(world_y AS INTEGER) - 80) <= 9)
              OR ((CAST(world_x AS INTEGER) - 144) * (CAST(world_x AS INTEGER) - 144)
                  + (CAST(world_y AS INTEGER) - 104) * (CAST(world_y AS INTEGER) - 104) <= 9)
              OR ((CAST(world_x AS INTEGER) - 208) * (CAST(world_x AS INTEGER) - 208)
                  + (CAST(world_y AS INTEGER) - 176) * (CAST(world_y AS INTEGER) - 176) <= 9)
            THEN 4
            ELSE 0
        END AS material_value
    FROM coords
)
SELECT
    sample_id,
    scan_id,
    world_x,
    world_y,
    world_z,
    CAST(CASE WHEN material_value = 0 THEN 0 ELSE 1 END AS SMALLINT) AS solid,
    CAST(material_value AS SMALLINT) AS material,
    CAST(48 + ((world_x * 3 + world_y * 5 + world_z * 11 + scan_id * 7) % 160) AS SMALLINT) AS light
FROM classified
""".strip()


def create_parquet(path: Path, rows: int, row_group_size: int = 1_000_000) -> None:
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    escaped = str(path).replace("'", "''")
    with duckdb.connect(":memory:") as conn:
        conn.execute(
            f"COPY ({world_select_sql(rows)}) TO '{escaped}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {int(row_group_size)})"
        )


def _camera_expressions(camera: Camera) -> tuple[str, str, str]:
    direction_x, direction_y = camera_vector(camera.heading)
    delta_x = f"(CAST(world_x AS INTEGER) - {camera.x})"
    delta_y = f"(CAST(world_y AS INTEGER) - {camera.y})"
    depth_scaled = f"({delta_x} * {direction_x} + {delta_y} * {direction_y})"
    lateral_scaled = f"({delta_x} * {direction_y} - {delta_y} * {direction_x})"
    coarse_filter = (
        f"world_x BETWEEN {max(0, camera.x - camera.draw_distance)} "
        f"AND {min(WORLD_SIZE - 1, camera.x + camera.draw_distance)} "
        f"AND world_y BETWEEN {max(0, camera.y - camera.draw_distance)} "
        f"AND {min(WORLD_SIZE - 1, camera.y + camera.draw_distance)}"
    )
    return depth_scaled, lateral_scaled, coarse_filter


def frame_sql(
    camera: Camera,
    *,
    width: int = 120,
    height: int = 40,
    table_expr: str = "doomql_world",
    dialect: str = "postgres",
    world: str = "synthetic",
) -> str:
    """Build one analytical frame query for PostgreSQL/GQE or DuckDB."""
    if width <= 0 or height <= 0:
        raise ValueError("frame dimensions must be positive")
    if dialect not in {"postgres", "duckdb", "clickhouse"}:
        raise ValueError(f"unsupported dialect: {dialect}")
    if world not in {"synthetic", "e1m1"}:
        raise ValueError(f"unsupported world: {world}")
    depth_scaled, lateral_scaled, coarse_filter = _camera_expressions(camera)
    half_width = width // 2
    focal_length = projection_focal_length(width)
    if world == "e1m1":
        surface_filter = (
            f"world_x BETWEEN {camera.x - camera.draw_distance} "
            f"AND {camera.x + camera.draw_distance} "
            f"AND world_y BETWEEN {camera.y - camera.draw_distance} "
            f"AND {camera.y + camera.draw_distance}"
        )
        return f"""
WITH camera_space AS (
    SELECT
        surface_id,
        {depth_scaled} AS depth_scaled,
        {lateral_scaled} AS lateral_scaled,
        z_bottom,
        z_top,
        surface_kind,
        material,
        light,
        sector_id
    FROM {table_expr}
    WHERE {surface_filter}
), visible_space AS (
    SELECT *
    FROM camera_space
    WHERE depth_scaled BETWEEN {CAMERA_VECTOR_SCALE // 2}
                           AND {camera.draw_distance * CAMERA_VECTOR_SCALE}
      AND lateral_scaled * {focal_length}
          BETWEEN (0 - {half_width}) * depth_scaled
              AND {int(width) - half_width} * depth_scaled
)
SELECT
    lateral_scaled,
    depth_scaled,
    z_bottom,
    z_top,
    surface_kind,
    material,
    sector_id,
    avg(light) AS avg_light,
    count(*) AS samples,
    surface_id
FROM visible_space
GROUP BY
    lateral_scaled,
    depth_scaled,
    z_bottom,
    z_top,
    surface_kind,
    material,
    sector_id,
    surface_id
ORDER BY depth_scaled, lateral_scaled, surface_kind, surface_id
""".strip()
    return f"""
WITH camera_space AS (
    SELECT
        {depth_scaled} AS depth_scaled,
        {lateral_scaled} AS lateral_scaled,
        material,
        light
    FROM {table_expr}
    WHERE solid = 1
      AND {coarse_filter}
), visible_space AS (
    SELECT depth_scaled, lateral_scaled, material, light
    FROM camera_space
    WHERE depth_scaled BETWEEN 1 AND {camera.draw_distance * CAMERA_VECTOR_SCALE}
      AND lateral_scaled * {focal_length}
          BETWEEN (0 - {half_width}) * depth_scaled
              AND {int(width) - half_width} * depth_scaled
)
SELECT
    lateral_scaled,
    material,
    min(depth_scaled) AS nearest_depth,
    avg(light) AS avg_light,
    count(*) AS samples
FROM visible_space
GROUP BY lateral_scaled, material
ORDER BY lateral_scaled, material
""".strip()


def coerce_hits(rows: Iterable[Sequence[object]]) -> list[RayHit]:
    return [
        RayHit(
            lateral_scaled=int(row[0]),
            material=int(row[1]),
            nearest_depth=int(row[2]),
            avg_light=float(row[3]),
            samples=int(row[4]),
        )
        for row in rows
    ]


def coerce_surface_hits(rows: Iterable[Sequence[object]]) -> list[SurfaceHit]:
    return [
        SurfaceHit(
            lateral_scaled=int(row[0]),
            depth_scaled=int(row[1]),
            z_bottom=int(row[2]),
            z_top=int(row[3]),
            surface_kind=int(row[4]),
            material=int(row[5]),
            sector_id=int(row[6]),
            avg_light=float(row[7]),
            samples=int(row[8]),
            surface_id=int(row[9]),
        )
        for row in rows
    ]


def render_frame(
    rows: Iterable[Sequence[object]] | Iterable[RayHit] | Iterable[SurfaceHit],
    camera: Camera,
    *,
    width: int = 120,
    height: int = 40,
    world: str = "synthetic",
    grid_scale: int = DEFAULT_GRID_SCALE,
    render_type: str = "ascii",
    material_color_ramps: Mapping[int, Sequence[tuple[int, int, int]]] | None = None,
) -> str:
    if render_type not in RENDER_TYPES:
        raise ValueError(f"unsupported render type: {render_type}")
    if world == "e1m1":
        return render_surface_frame(
            rows,
            camera,
            width=width,
            height=height,
            grid_scale=grid_scale,
            render_type=render_type,
            material_color_ramps=material_color_ramps,
        )
    if world != "synthetic":
        raise ValueError(f"unsupported world: {world}")
    if render_type == "ansi-half":
        pixels = _render_synthetic_ascii(rows, camera, width=width, height=height * 2)
        return _ansi_half_from_ascii(pixels)
    return _render_synthetic_ascii(rows, camera, width=width, height=height)


def _render_synthetic_ascii(
    rows: Iterable[Sequence[object]] | Iterable[RayHit] | Iterable[SurfaceHit],
    camera: Camera,
    *,
    width: int,
    height: int,
) -> str:
    hits = list(rows)
    if hits and not isinstance(hits[0], RayHit):
        hits = coerce_hits(hits)  # type: ignore[arg-type]

    horizon = height // 2
    canvas = [
        [" " if y < horizon else ("." if (x + y) % 3 else " ") for x in range(width)]
        for y in range(height)
    ]
    nearest: dict[int, RayHit] = {}
    focal_length = projection_focal_length(width)
    for hit in hits:  # type: ignore[assignment]
        projection = hit.lateral_scaled * focal_length
        if projection >= 0:
            screen_offset = projection // hit.nearest_depth
        else:
            screen_offset = -((-projection) // hit.nearest_depth)
        screen_x = screen_offset + width // 2
        if not 0 <= screen_x < width:
            continue
        projected_half_width = max(
            0,
            math.ceil(focal_length * CAMERA_VECTOR_SCALE / hit.nearest_depth / 2),
        )
        for projected_x in range(
            max(0, screen_x - projected_half_width),
            min(width, screen_x + projected_half_width + 1),
        ):
            previous = nearest.get(projected_x)
            if previous is None or (hit.nearest_depth, hit.material) < (
                previous.nearest_depth,
                previous.material,
            ):
                nearest[projected_x] = hit

    palettes = {
        1: "@%#*+=-:.",
        2: "#*+=-:.",
        3: "Xx+=-:.",
        4: "O0o+=-:.",
    }
    for x, hit in nearest.items():
        palette = palettes.get(hit.material, "#*+=-:.")
        depth = hit.nearest_depth / CAMERA_VECTOR_SCALE
        distance_ratio = min(1.0, depth / max(1, camera.draw_distance))
        darkness = max(0.0, min(1.0, (192.0 - hit.avg_light) / 256.0))
        shade = min(len(palette) - 1, int((distance_ratio * 0.75 + darkness * 0.25) * len(palette)))
        wall_height = min(height, max(1, int(height * 8 / max(1, depth))))
        top = max(0, horizon - wall_height // 2)
        bottom = min(height, top + wall_height)
        for y in range(top, bottom):
            canvas[y][x] = palette[shade]
    return "\n".join("".join(row) for row in canvas)


def _ansi_escape(foreground: tuple[int, int, int], background: tuple[int, int, int]) -> str:
    return (
        f"\x1b[38;2;{foreground[0]};{foreground[1]};{foreground[2]}m"
        f"\x1b[48;2;{background[0]};{background[1]};{background[2]}m"
    )


def _encode_ansi_half(colors: list[list[tuple[int, int, int]]]) -> str:
    if not colors:
        return ""
    lines: list[str] = []
    for y in range(0, len(colors), 2):
        top = colors[y]
        bottom = colors[min(y + 1, len(colors) - 1)]
        chunks: list[str] = []
        previous: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None
        for foreground, background in zip(top, bottom):
            pair = (foreground, background)
            if pair != previous:
                chunks.append(_ansi_escape(foreground, background))
                previous = pair
            chunks.append("▀")
        chunks.append("\x1b[0m")
        lines.append("".join(chunks))
    return "\n".join(lines)


def _ansi_half_from_ascii(frame: str) -> str:
    glyph_colors = {
        " ": (8, 11, 16),
        ".": (29, 27, 23),
        "@": (190, 184, 164),
        "%": (164, 156, 137),
        "#": (139, 126, 104),
        "*": (117, 101, 78),
        "+": (94, 83, 66),
        "=": (75, 69, 58),
        "-": (58, 57, 52),
        ":": (42, 44, 42),
        "X": (155, 70, 58),
        "x": (119, 60, 51),
        "M": (156, 104, 62),
        "W": (134, 88, 55),
        "H": (111, 73, 49),
        "K": (88, 60, 44),
        "O": (129, 132, 69),
        "0": (104, 110, 61),
        "o": (81, 90, 55),
        "[": (93, 119, 137),
        "]": (84, 108, 126),
        "|": (70, 93, 111),
        "!": (59, 78, 94),
    }
    colors = [
        [glyph_colors.get(character, (92, 85, 72)) for character in line]
        for line in frame.splitlines()
    ]
    return _encode_ansi_half(colors)


def _trunc_div(numerator: int, denominator: int) -> int:
    if numerator >= 0:
        return numerator // denominator
    return -((-numerator) // denominator)


def _doom_light_level(avg_light: float, distance_ratio: float) -> int:
    sector_level = round((255.0 - max(0.0, min(255.0, avg_light))) / 8.0)
    distance_level = round(max(0.0, min(1.0, distance_ratio)) * 8.0)
    return min(31, sector_level + distance_level)


def render_surface_frame(
    rows: Iterable[Sequence[object]] | Iterable[RayHit] | Iterable[SurfaceHit],
    camera: Camera,
    *,
    width: int = 120,
    height: int = 40,
    grid_scale: int = DEFAULT_GRID_SCALE,
    render_type: str = "ascii",
    material_color_ramps: Mapping[int, Sequence[tuple[int, int, int]]] | None = None,
) -> str:
    hits = list(rows)
    if hits and not isinstance(hits[0], SurfaceHit):
        hits = coerce_surface_hits(hits)  # type: ignore[arg-type]

    pixel_height = height * 2 if render_type == "ansi-half" else height
    vertical_scale = 2 if render_type == "ansi-half" else 1
    horizon = pixel_height // 2
    canvas = [
        [" " if y < horizon else ("." if (x + y) % 3 else " ") for x in range(width)]
        for y in range(pixel_height)
    ]
    colors = [
        [
            (8, 12, 19)
            if y < horizon
            else ((30, 26, 21) if (x + y) % 3 else (24, 22, 19))
            for x in range(width)
        ]
        for y in range(pixel_height)
    ]
    depth_buffer = [[math.inf for _ in range(width)] for _ in range(pixel_height)]
    priority_buffer = [[99 for _ in range(width)] for _ in range(pixel_height)]
    focal_length = projection_focal_length(width)
    wall_palettes = (
        "@%#*+=-:.",
        "Xx#*+=-:.",
        "MWHK+=-:.",
        "O0o*+=-:.",
        "[]|!+=-:.",
    )
    plane_palettes = {
        SURFACE_FLOOR: "#*+=-:., ",
        SURFACE_CEILING: "@%*+=-:. ",
        SURFACE_MASKED: "#H|!+=-:. ",
    }

    for hit in hits:  # type: ignore[assignment]
        if hit.depth_scaled <= 0 or hit.surface_kind == SURFACE_SKY:
            continue
        depth = hit.depth_scaled / CAMERA_VECTOR_SCALE
        screen_x = (
            _trunc_div(hit.lateral_scaled * focal_length, hit.depth_scaled)
            + width // 2
        )
        projected_half_width = max(
            0,
            math.ceil(focal_length * CAMERA_VECTOR_SCALE / hit.depth_scaled / 2),
        )
        x_start = max(0, screen_x - projected_half_width)
        x_stop = min(width, screen_x + projected_half_width + 1)
        if x_start >= x_stop:
            continue

        def project_y(z_value: int) -> int:
            offset = _trunc_div(
                (z_value - camera.z) * focal_length * CAMERA_VECTOR_SCALE,
                hit.depth_scaled * grid_scale,
            )
            return horizon - offset * vertical_scale

        if hit.surface_kind in {SURFACE_WALL, SURFACE_MASKED}:
            y_top = project_y(hit.z_top)
            y_bottom = project_y(hit.z_bottom)
            y_start = max(0, min(y_top, y_bottom))
            y_stop = min(pixel_height, max(y_top, y_bottom) + 1)
        else:
            screen_y = project_y(hit.z_bottom)
            projected_half_height = max(
                0,
                math.ceil(
                    focal_length
                    * CAMERA_VECTOR_SCALE
                    * vertical_scale
                    / hit.depth_scaled
                    / 2
                ),
            )
            y_start = max(0, screen_y - projected_half_height)
            y_stop = min(pixel_height, screen_y + projected_half_height + 1)
        if y_start >= y_stop:
            continue

        distance_ratio = min(1.0, depth / max(1, camera.draw_distance))
        light_level = _doom_light_level(hit.avg_light, distance_ratio)
        if hit.surface_kind == SURFACE_WALL:
            palette = wall_palettes[hit.material % len(wall_palettes)]
            priority = 0
        else:
            palette = plane_palettes.get(hit.surface_kind, "#*+=-:. ")
            priority = 1 if hit.surface_kind == SURFACE_MASKED else 2
        shade = min(
            len(palette) - 1,
            round(light_level / 31 * (len(palette) - 1)),
        )
        character = palette[shade]
        base_colors = {
            SURFACE_FLOOR: (92, 78, 55),
            SURFACE_CEILING: (112, 116, 109),
            SURFACE_MASKED: (112, 128, 105),
        }
        wall_colors = (
            (137, 91, 55),
            (105, 111, 103),
            (111, 121, 72),
            (139, 72, 58),
            (79, 104, 124),
            (151, 125, 75),
        )
        base_color = (
            wall_colors[hit.material % len(wall_colors)]
            if hit.surface_kind == SURFACE_WALL
            else base_colors.get(hit.surface_kind, (104, 99, 85))
        )
        material_ramp = (
            material_color_ramps.get(hit.material)
            if material_color_ramps is not None
            else None
        )
        if material_ramp:
            ramp_index = min(len(material_ramp) - 1, light_level)
            color = tuple(material_ramp[ramp_index])
        else:
            color_factor = max(0.08, 1.0 - light_level / 34.0)
            color = tuple(
                max(0, min(255, round(component * color_factor)))
                for component in base_color
            )
        for screen_y in range(y_start, y_stop):
            for projected_x in range(x_start, x_stop):
                if hit.surface_kind == SURFACE_MASKED and (
                    projected_x + screen_y + hit.material
                ) % 2:
                    continue
                current = (depth_buffer[screen_y][projected_x], priority_buffer[screen_y][projected_x])
                candidate = (hit.depth_scaled, priority)
                if candidate < current:
                    depth_buffer[screen_y][projected_x] = hit.depth_scaled
                    priority_buffer[screen_y][projected_x] = priority
                    canvas[screen_y][projected_x] = character
                    colors[screen_y][projected_x] = color  # type: ignore[assignment]
    if render_type == "ansi-half":
        return _encode_ansi_half(colors)
    return "\n".join("".join(row) for row in canvas)


def frame_hash(frame: str) -> str:
    return hashlib.sha256(frame.encode("utf-8")).hexdigest()[:12]


def scripted_cameras(frames: int, draw_distance: int = 128) -> list[Camera]:
    if frames <= 0:
        raise ValueError("frames must be positive")
    keyframes = (
        Camera(18, 112, 8, 0, draw_distance),
        Camera(24, 112, 8, 0, draw_distance),
        Camera(30, 112, 8, 15, draw_distance),
        Camera(36, 112, 8, 30, draw_distance),
        Camera(44, 112, 8, 45, draw_distance),
        Camera(52, 112, 8, 75, draw_distance),
        Camera(52, 120, 8, 90, draw_distance),
        Camera(52, 128, 8, 135, draw_distance),
        Camera(44, 128, 8, 180, draw_distance),
        Camera(36, 128, 8, 225, draw_distance),
        Camera(36, 116, 8, 270, draw_distance),
        Camera(36, 112, 8, 345, draw_distance),
    )
    return [keyframes[i % len(keyframes)] for i in range(frames)]
