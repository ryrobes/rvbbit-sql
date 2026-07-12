"""Dataset, query, and terminal rendering primitives for DoomQL."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


WORLD_SIZE = 256
WORLD_HEIGHT = 16
VOXELS_PER_SCAN = WORLD_SIZE * WORLD_SIZE * WORLD_HEIGHT
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
        dx, dy = ((1, 0), (0, 1), (-1, 0), (0, -1))[self.heading % 4]
        target_x = min(WORLD_SIZE - 2, max(1, self.x + dx * amount))
        target_y = min(WORLD_SIZE - 2, max(1, self.y + dy * amount))
        if wall_material(target_x, target_y) != 0:
            return self
        return Camera(target_x, target_y, self.z, self.heading, self.draw_distance)

    def turned(self, amount: int) -> "Camera":
        return Camera(self.x, self.y, self.z, (self.heading + amount) % 4, self.draw_distance)


@dataclass(frozen=True)
class RayHit:
    screen_x: int
    material: int
    nearest_depth: int
    avg_light: float
    samples: int


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
    heading = camera.heading % 4
    if heading == 0:
        return (
            f"world_x - {camera.x}",
            f"world_y - {camera.y}",
            f"world_x BETWEEN {camera.x + 1} AND {min(WORLD_SIZE - 1, camera.x + camera.draw_distance)}",
        )
    if heading == 1:
        return (
            f"world_y - {camera.y}",
            f"{camera.x} - world_x",
            f"world_y BETWEEN {camera.y + 1} AND {min(WORLD_SIZE - 1, camera.y + camera.draw_distance)}",
        )
    if heading == 2:
        return (
            f"{camera.x} - world_x",
            f"{camera.y} - world_y",
            f"world_x BETWEEN {max(0, camera.x - camera.draw_distance)} AND {camera.x - 1}",
        )
    return (
        f"{camera.y} - world_y",
        f"world_x - {camera.x}",
        f"world_y BETWEEN {max(0, camera.y - camera.draw_distance)} AND {camera.y - 1}",
    )


def frame_sql(
    camera: Camera,
    *,
    width: int = 120,
    height: int = 40,
    table_expr: str = "doomql_world",
    dialect: str = "postgres",
) -> str:
    """Build one analytical frame query for PostgreSQL/GQE or DuckDB."""
    if width <= 0 or height <= 0:
        raise ValueError("frame dimensions must be positive")
    if dialect not in {"postgres", "duckdb", "clickhouse"}:
        raise ValueError(f"unsupported dialect: {dialect}")
    depth, lateral_offset, coarse_filter = _camera_expressions(camera)
    half_width = width // 2
    return f"""
WITH camera_space AS (
    SELECT
        {depth} AS depth,
        {lateral_offset} AS lateral_offset,
        material,
        light
    FROM {table_expr}
    WHERE solid = 1
      AND {coarse_filter}
)
SELECT
    CAST(lateral_offset + {half_width} AS INTEGER) AS screen_x,
    material,
    min(depth) AS nearest_depth,
    avg(light) AS avg_light,
    count(*) AS samples
FROM camera_space
WHERE lateral_offset BETWEEN (0 - {half_width}) AND {int(width) - half_width - 1}
GROUP BY lateral_offset, material
ORDER BY screen_x, material
""".strip()


def coerce_hits(rows: Iterable[Sequence[object]]) -> list[RayHit]:
    return [
        RayHit(
            screen_x=int(row[0]),
            material=int(row[1]),
            nearest_depth=int(row[2]),
            avg_light=float(row[3]),
            samples=int(row[4]),
        )
        for row in rows
    ]


def render_frame(
    rows: Iterable[Sequence[object]] | Iterable[RayHit],
    camera: Camera,
    *,
    width: int = 120,
    height: int = 40,
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
    for hit in hits:  # type: ignore[assignment]
        if not 0 <= hit.screen_x < width:
            continue
        previous = nearest.get(hit.screen_x)
        if previous is None or (hit.nearest_depth, hit.material) < (
            previous.nearest_depth,
            previous.material,
        ):
            nearest[hit.screen_x] = hit

    palettes = {
        1: "@%#*+=-:.",
        2: "#*+=-:.",
        3: "Xx+=-:.",
        4: "O0o+=-:.",
    }
    for x, hit in nearest.items():
        palette = palettes.get(hit.material, "#*+=-:.")
        distance_ratio = min(1.0, hit.nearest_depth / max(1, camera.draw_distance))
        darkness = max(0.0, min(1.0, (192.0 - hit.avg_light) / 256.0))
        shade = min(len(palette) - 1, int((distance_ratio * 0.75 + darkness * 0.25) * len(palette)))
        wall_height = min(height, max(1, int(height * 8 / max(1, hit.nearest_depth))))
        top = max(0, horizon - wall_height // 2)
        bottom = min(height, top + wall_height)
        for y in range(top, bottom):
            canvas[y][x] = palette[shade]
    return "\n".join("".join(row) for row in canvas)


def frame_hash(frame: str) -> str:
    return hashlib.sha256(frame.encode("ascii")).hexdigest()[:12]


def scripted_cameras(frames: int, draw_distance: int = 128) -> list[Camera]:
    if frames <= 0:
        raise ValueError("frames must be positive")
    keyframes = (
        Camera(18, 112, 8, 0, draw_distance),
        Camera(24, 112, 8, 0, draw_distance),
        Camera(30, 112, 8, 0, draw_distance),
        Camera(36, 112, 8, 0, draw_distance),
        Camera(44, 112, 8, 0, draw_distance),
        Camera(52, 112, 8, 1, draw_distance),
        Camera(52, 120, 8, 1, draw_distance),
        Camera(52, 128, 8, 2, draw_distance),
        Camera(44, 128, 8, 2, draw_distance),
        Camera(36, 128, 8, 3, draw_distance),
        Camera(36, 116, 8, 3, draw_distance),
        Camera(36, 112, 8, 0, draw_distance),
    )
    return [keyframes[i % len(keyframes)] for i in range(frames)]
