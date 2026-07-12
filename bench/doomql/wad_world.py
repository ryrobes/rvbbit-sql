"""Dependency-free Doom WAD parsing and surface rasterization for DoomQL."""

from __future__ import annotations

import json
import math
import struct
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ML_BLOCKING = 1
SURFACE_WALL = 1
SURFACE_FLOOR = 2
SURFACE_CEILING = 3
SURFACE_MASKED = 4
SURFACE_SKY = 5
DEFAULT_GRID_SCALE = 16
PLAYER_HEIGHT = 56
PLAYER_EYE_HEIGHT = 41
PLAYER_RADIUS = 16
MAX_STEP_HEIGHT = 24
RGBColor = tuple[int, int, int]
MaterialColorRamp = tuple[RGBColor, ...]
SURFACE_COLUMNS = (
    "sample_id",
    "scan_id",
    "surface_id",
    "world_x",
    "world_y",
    "z_bottom",
    "z_top",
    "surface_kind",
    "material",
    "light",
    "sector_id",
)


@dataclass(frozen=True)
class Vertex:
    x: int
    y: int


@dataclass(frozen=True)
class Sector:
    floor_height: int
    ceiling_height: int
    floor_texture: str
    ceiling_texture: str
    light: int
    special: int
    tag: int


@dataclass(frozen=True)
class SideDef:
    x_offset: int
    y_offset: int
    upper_texture: str
    lower_texture: str
    middle_texture: str
    sector: int


@dataclass(frozen=True)
class LineDef:
    v1: int
    v2: int
    flags: int
    special: int
    tag: int
    right_side: int
    left_side: int | None


@dataclass(frozen=True)
class Seg:
    v1: int
    v2: int
    linedef: int
    side: int


@dataclass(frozen=True)
class SubSector:
    count: int
    first_seg: int


@dataclass(frozen=True)
class Node:
    x: int
    y: int
    dx: int
    dy: int
    children: tuple[int, int]


@dataclass(frozen=True)
class Thing:
    x: int
    y: int
    angle: int
    kind: int
    flags: int


@dataclass(frozen=True)
class SurfaceSample:
    surface_id: int
    world_x: int
    world_y: int
    z_bottom: int
    z_top: int
    surface_kind: int
    material: int
    light: int
    sector_id: int


@dataclass
class DoomMap:
    name: str
    vertices: list[Vertex]
    sectors: list[Sector]
    sides: list[SideDef]
    lines: list[LineDef]
    segs: list[Seg]
    subsectors: list[SubSector]
    nodes: list[Node]
    things: list[Thing]
    material_color_ramps: dict[str, MaterialColorRamp]

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        return (
            min(vertex.x for vertex in self.vertices),
            min(vertex.y for vertex in self.vertices),
            max(vertex.x for vertex in self.vertices),
            max(vertex.y for vertex in self.vertices),
        )

    @property
    def player_start(self) -> Thing:
        try:
            return next(thing for thing in self.things if thing.kind == 1)
        except StopIteration as exc:
            raise ValueError(f"{self.name} has no player-one start") from exc

    def side_sector(self, side_index: int | None) -> int | None:
        if side_index is None:
            return None
        return self.sides[side_index].sector

    def subsector_sector(self, subsector: SubSector) -> int:
        for seg in self.segs[subsector.first_seg : subsector.first_seg + subsector.count]:
            line = self.lines[seg.linedef]
            side_index = line.right_side if seg.side == 0 else line.left_side
            if side_index is not None:
                return self.sides[side_index].sector
        raise ValueError(f"subsector at seg {subsector.first_seg} has no sidedef")

    def point_subsector(self, x: int, y: int) -> int:
        if not self.nodes:
            return 0
        node_index = len(self.nodes) - 1
        while node_index & 0x8000 == 0:
            if not 0 <= node_index < len(self.nodes):
                raise ValueError(f"invalid BSP node index {node_index}")
            node = self.nodes[node_index]
            if node.dx == 0:
                side = int(node.dy > 0) if x <= node.x else int(node.dy < 0)
            elif node.dy == 0:
                side = int(node.dx < 0) if y <= node.y else int(node.dx > 0)
            else:
                left = node.dy * (x - node.x)
                right = (y - node.y) * node.dx
                side = 0 if right < left else 1
            node_index = node.children[side]
        subsector_index = node_index & 0x7FFF
        if not 0 <= subsector_index < len(self.subsectors):
            raise ValueError(f"invalid BSP subsector index {subsector_index}")
        return subsector_index

    def point_sector(self, x: int, y: int) -> int:
        return self.subsector_sector(self.subsectors[self.point_subsector(x, y)])


@dataclass
class RasterizedWorld:
    doom_map: DoomMap
    grid_scale: int
    origin_x: int
    origin_y: int
    width: int
    height: int
    cell_sectors: dict[tuple[int, int], int]
    blocked_cells: set[tuple[int, int]]
    surfaces: list[SurfaceSample]
    material_names: dict[int, str]
    material_color_ramps: dict[int, MaterialColorRamp]

    def to_grid(self, x: int, y: int) -> tuple[int, int]:
        return (
            round((x - self.origin_x) / self.grid_scale),
            round((y - self.origin_y) / self.grid_scale),
        )

    def to_map(self, x: int, y: int) -> tuple[int, int]:
        return (
            self.origin_x + x * self.grid_scale,
            self.origin_y + y * self.grid_scale,
        )

    def sector_at(self, x: int, y: int) -> Sector | None:
        sector_id = self.cell_sectors.get((x, y))
        return self.doom_map.sectors[sector_id] if sector_id is not None else None

    def player_camera(self, draw_distance: int) -> tuple[int, int, int, int, int]:
        start = self.doom_map.player_start
        x, y = self.to_grid(start.x, start.y)
        sector = self.sector_at(x, y)
        if sector is None:
            raise ValueError("player start did not rasterize into a sector")
        return x, y, sector.floor_height + PLAYER_EYE_HEIGHT, start.angle % 360, draw_distance

    def line_blocks_player(self, line: LineDef) -> bool:
        if line.left_side is None or line.flags & ML_BLOCKING:
            return True
        front_id = self.doom_map.side_sector(line.right_side)
        back_id = self.doom_map.side_sector(line.left_side)
        if front_id is None or back_id is None:
            return True
        front = self.doom_map.sectors[front_id]
        back = self.doom_map.sectors[back_id]
        opening_bottom = max(front.floor_height, back.floor_height)
        opening_top = min(front.ceiling_height, back.ceiling_height)
        return opening_top - opening_bottom < PLAYER_HEIGHT

    def position_is_clear(
        self,
        x: int,
        y: int,
        radius: int = PLAYER_RADIUS,
    ) -> bool:
        map_x, map_y = self.to_map(x, y)
        radius_squared = radius * radius
        for line in self.doom_map.lines:
            if not self.line_blocks_player(line):
                continue
            start = self.doom_map.vertices[line.v1]
            end = self.doom_map.vertices[line.v2]
            if _point_segment_distance_squared(map_x, map_y, start, end) < radius_squared:
                return False
        return True

    def try_move(
        self,
        x: int,
        y: int,
        target_x: int,
        target_y: int,
    ) -> tuple[int, int, int] | None:
        current_sector = self.sector_at(x, y)
        if current_sector is None:
            return None
        last_x, last_y = x, y
        steps = max(abs(target_x - x), abs(target_y - y), 1)
        for step in range(1, steps + 1):
            next_x = round(x + (target_x - x) * step / steps)
            next_y = round(y + (target_y - y) * step / steps)
            if not self.position_is_clear(next_x, next_y):
                return None
            next_sector = self.sector_at(next_x, next_y)
            if next_sector is None:
                return None
            if next_sector.floor_height - current_sector.floor_height > MAX_STEP_HEIGHT:
                return None
            if next_sector.ceiling_height - next_sector.floor_height < PLAYER_HEIGHT:
                return None
            last_x, last_y = next_x, next_y
            current_sector = next_sector
        return last_x, last_y, current_sector.floor_height + PLAYER_EYE_HEIGHT


def _decode_name(value: bytes) -> str:
    return value.rstrip(b"\0").decode("ascii")


def _records(blob: bytes, fmt: str) -> Iterable[tuple[object, ...]]:
    size = struct.calcsize(fmt)
    if len(blob) % size:
        raise ValueError(f"lump size {len(blob)} is not divisible by record size {size}")
    for offset in range(0, len(blob), size):
        yield struct.unpack_from(fmt, blob, offset)


def _decode_patch(blob: bytes) -> tuple[int, int, list[int | None]]:
    if len(blob) < 8:
        raise ValueError("patch lump is too short")
    width, height = struct.unpack_from("<hh", blob, 0)
    if width <= 0 or height <= 0 or len(blob) < 8 + width * 4:
        raise ValueError("patch lump has invalid dimensions")
    column_offsets = struct.unpack_from(f"<{width}I", blob, 8)
    pixels: list[int | None] = [None] * (width * height)
    for x, column_offset in enumerate(column_offsets):
        cursor = column_offset
        while cursor < len(blob):
            top = blob[cursor]
            cursor += 1
            if top == 255:
                break
            if cursor + 2 > len(blob):
                raise ValueError("patch column has a truncated post")
            length = blob[cursor]
            cursor += 2
            if cursor + length + 1 > len(blob):
                raise ValueError("patch column pixels are truncated")
            for offset, palette_index in enumerate(blob[cursor : cursor + length]):
                y = top + offset
                if y < height:
                    pixels[y * width + x] = palette_index
            cursor += length + 1
    return width, height, pixels


def _decode_wall_textures(lumps: dict[str, bytes]) -> dict[str, list[int]]:
    pnames_blob = lumps.get("PNAMES")
    if pnames_blob is None or len(pnames_blob) < 4:
        return {}
    patch_count = struct.unpack_from("<i", pnames_blob, 0)[0]
    if patch_count < 0 or len(pnames_blob) < 4 + patch_count * 8:
        raise ValueError("PNAMES lump is truncated")
    patch_names = [
        _decode_name(pnames_blob[4 + index * 8 : 12 + index * 8])
        for index in range(patch_count)
    ]
    decoded_patches: dict[str, tuple[int, int, list[int | None]]] = {}
    textures: dict[str, list[int]] = {}
    for lump_name in ("TEXTURE1", "TEXTURE2"):
        texture_blob = lumps.get(lump_name)
        if texture_blob is None:
            continue
        if len(texture_blob) < 4:
            raise ValueError(f"{lump_name} lump is truncated")
        texture_count = struct.unpack_from("<i", texture_blob, 0)[0]
        if texture_count < 0 or len(texture_blob) < 4 + texture_count * 4:
            raise ValueError(f"{lump_name} directory is truncated")
        offsets = struct.unpack_from(f"<{texture_count}I", texture_blob, 4)
        for texture_offset in offsets:
            if texture_offset + 22 > len(texture_blob):
                raise ValueError(f"{lump_name} texture header is truncated")
            raw_name, _masked, width, height, _column_dir, texture_patch_count = (
                struct.unpack_from("<8sihhih", texture_blob, texture_offset)
            )
            if width <= 0 or height <= 0 or texture_patch_count < 0:
                raise ValueError(f"{lump_name} texture has invalid dimensions")
            records_offset = texture_offset + 22
            if records_offset + texture_patch_count * 10 > len(texture_blob):
                raise ValueError(f"{lump_name} texture patches are truncated")
            canvas: list[int | None] = [None] * (width * height)
            for patch_offset in range(texture_patch_count):
                origin_x, origin_y, patch_index, _step_dir, _color_map = (
                    struct.unpack_from(
                        "<hhhhh",
                        texture_blob,
                        records_offset + patch_offset * 10,
                    )
                )
                if not 0 <= patch_index < len(patch_names):
                    continue
                patch_name = patch_names[patch_index].upper()
                patch_blob = lumps.get(patch_name)
                if patch_blob is None:
                    continue
                patch = decoded_patches.get(patch_name)
                if patch is None:
                    patch = _decode_patch(patch_blob)
                    decoded_patches[patch_name] = patch
                patch_width, patch_height, patch_pixels = patch
                for patch_y in range(patch_height):
                    target_y = origin_y + patch_y
                    if not 0 <= target_y < height:
                        continue
                    for patch_x in range(patch_width):
                        target_x = origin_x + patch_x
                        palette_index = patch_pixels[patch_y * patch_width + patch_x]
                        if 0 <= target_x < width and palette_index is not None:
                            canvas[target_y * width + target_x] = palette_index
            textures[_decode_name(raw_name)] = [
                palette_index
                for palette_index in canvas
                if palette_index is not None
            ]
    return textures


def _average_color_ramp(
    palette_indices: Iterable[int],
    palette: tuple[RGBColor, ...],
    color_maps: tuple[bytes, ...],
) -> MaterialColorRamp:
    histogram = Counter(palette_indices)
    total = sum(histogram.values())
    if not total:
        return ()
    ramp: list[RGBColor] = []
    for color_map in color_maps:
        channels = [0, 0, 0]
        for palette_index, count in histogram.items():
            color = palette[color_map[palette_index]]
            for channel, value in enumerate(color):
                channels[channel] += value * count
        ramp.append(tuple(round(value / total) for value in channels))
    return tuple(ramp)


def _read_material_color_ramps(
    data: bytes,
    directory: list[tuple[str, int, int]],
    material_names: set[str],
) -> dict[str, MaterialColorRamp]:
    lumps = {
        name: data[offset : offset + size]
        for name, offset, size in directory
        if size > 0
    }
    playpal = lumps.get("PLAYPAL", b"")
    colormap = lumps.get("COLORMAP", b"")
    if len(playpal) < 256 * 3 or len(colormap) < 32 * 256:
        return {}
    palette = tuple(
        tuple(playpal[index : index + 3])
        for index in range(0, 256 * 3, 3)
    )
    color_maps = tuple(
        colormap[level * 256 : (level + 1) * 256]
        for level in range(32)
    )
    wall_textures = _decode_wall_textures(lumps)
    ramps: dict[str, MaterialColorRamp] = {}
    for material_name in material_names:
        surface_kind, texture_name = material_name.split(":", 1)
        if surface_kind == "wall":
            palette_indices = wall_textures.get(texture_name, [])
        else:
            flat = lumps.get(texture_name, b"")
            palette_indices = flat if len(flat) == 64 * 64 else []
        ramp = _average_color_ramp(palette_indices, palette, color_maps)
        if ramp:
            ramps[material_name] = ramp
    return ramps


def read_wad_map(path: Path, map_name: str = "E1M1") -> DoomMap:
    data = path.read_bytes()
    if len(data) < 12:
        raise ValueError(f"{path} is not a WAD file")
    identification, lump_count, directory_offset = struct.unpack_from("<4sii", data, 0)
    if identification not in {b"IWAD", b"PWAD"}:
        raise ValueError(f"{path} has invalid WAD identification {identification!r}")
    if lump_count < 0 or directory_offset < 0 or directory_offset + lump_count * 16 > len(data):
        raise ValueError(f"{path} has an invalid WAD directory")

    directory: list[tuple[str, int, int]] = []
    for index in range(lump_count):
        offset, size, raw_name = struct.unpack_from("<ii8s", data, directory_offset + index * 16)
        if offset < 0 or size < 0 or offset + size > len(data):
            raise ValueError(f"{path} has an invalid lump at directory index {index}")
        directory.append((_decode_name(raw_name), offset, size))

    normalized_map = map_name.upper()
    try:
        marker = next(index for index, item in enumerate(directory) if item[0] == normalized_map)
    except StopIteration as exc:
        raise ValueError(f"{path} does not contain map {normalized_map}") from exc
    required = (
        "THINGS",
        "LINEDEFS",
        "SIDEDEFS",
        "VERTEXES",
        "SEGS",
        "SSECTORS",
        "NODES",
        "SECTORS",
        "REJECT",
        "BLOCKMAP",
    )
    level_entries = directory[marker + 1 : marker + 1 + len(required)]
    if tuple(entry[0] for entry in level_entries) != required:
        raise ValueError(f"{normalized_map} does not use the classic Doom map lump order")
    lumps = {name: data[offset : offset + size] for name, offset, size in level_entries}

    vertices = [Vertex(int(x), int(y)) for x, y in _records(lumps["VERTEXES"], "<hh")]
    sectors = [
        Sector(int(floor), int(ceiling), _decode_name(floor_tex), _decode_name(ceiling_tex), int(light), int(special), int(tag))
        for floor, ceiling, floor_tex, ceiling_tex, light, special, tag in _records(
            lumps["SECTORS"], "<hh8s8shhh"
        )
    ]
    sides = [
        SideDef(int(x_offset), int(y_offset), _decode_name(upper), _decode_name(lower), _decode_name(middle), int(sector))
        for x_offset, y_offset, upper, lower, middle, sector in _records(
            lumps["SIDEDEFS"], "<hh8s8s8sH"
        )
    ]
    lines = [
        LineDef(int(v1), int(v2), int(flags), int(special), int(tag), int(right), None if left == 0xFFFF else int(left))
        for v1, v2, flags, special, tag, right, left in _records(lumps["LINEDEFS"], "<HHHHHHH")
    ]
    segs = [
        Seg(int(v1), int(v2), int(linedef), int(side))
        for v1, v2, _angle, linedef, side, _offset in _records(lumps["SEGS"], "<HHHHHH")
    ]
    subsectors = [
        SubSector(int(count), int(first_seg))
        for count, first_seg in _records(lumps["SSECTORS"], "<HH")
    ]
    nodes = []
    for values in _records(lumps["NODES"], "<hhhhhhhhhhhhHH"):
        nodes.append(
            Node(
                x=int(values[0]),
                y=int(values[1]),
                dx=int(values[2]),
                dy=int(values[3]),
                children=(int(values[-2]), int(values[-1])),
            )
        )
    things = [
        Thing(int(x), int(y), int(angle), int(kind), int(flags))
        for x, y, angle, kind, flags in _records(lumps["THINGS"], "<hhhhh")
    ]
    material_names = {
        *(f"floor:{sector.floor_texture}" for sector in sectors),
        *(f"ceiling:{sector.ceiling_texture}" for sector in sectors),
        *(
            f"wall:{texture}"
            for side in sides
            for texture in (
                side.upper_texture,
                side.lower_texture,
                side.middle_texture,
            )
            if texture != "-"
        ),
    }
    try:
        material_color_ramps = _read_material_color_ramps(
            data,
            directory,
            material_names,
        )
    except (IndexError, KeyError, struct.error, ValueError):
        material_color_ramps = {}
    return DoomMap(
        normalized_map,
        vertices,
        sectors,
        sides,
        lines,
        segs,
        subsectors,
        nodes,
        things,
        material_color_ramps,
    )


def _point_on_segment(x: int, y: int, a: Vertex, b: Vertex) -> bool:
    cross = (x - a.x) * (b.y - a.y) - (y - a.y) * (b.x - a.x)
    if cross != 0:
        return False
    return min(a.x, b.x) <= x <= max(a.x, b.x) and min(a.y, b.y) <= y <= max(a.y, b.y)


def _point_segment_distance_squared(x: int, y: int, a: Vertex, b: Vertex) -> float:
    delta_x = b.x - a.x
    delta_y = b.y - a.y
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared == 0:
        return float((x - a.x) ** 2 + (y - a.y) ** 2)
    projection = ((x - a.x) * delta_x + (y - a.y) * delta_y) / length_squared
    projection = max(0.0, min(1.0, projection))
    closest_x = a.x + projection * delta_x
    closest_y = a.y + projection * delta_y
    return (x - closest_x) ** 2 + (y - closest_y) ** 2


def _point_inside_edges(
    polygon_edges: list[tuple[Vertex, Vertex]],
    x: int,
    y: int,
) -> bool:
    inside = False
    for start, end in polygon_edges:
        if _point_on_segment(x, y, start, end):
            return True
        if (start.y > y) != (end.y > y):
            intersection_x = (end.x - start.x) * (y - start.y) / (end.y - start.y) + start.x
            if x < intersection_x:
                inside = not inside
    return inside


def _line_grid_points(
    a: Vertex,
    b: Vertex,
    origin_x: int,
    origin_y: int,
    grid_scale: int,
) -> list[tuple[int, int]]:
    steps = max(1, math.ceil(max(abs(b.x - a.x), abs(b.y - a.y)) / grid_scale))
    points: list[tuple[int, int]] = []
    for step in range(steps + 1):
        x = round((a.x + (b.x - a.x) * step / steps - origin_x) / grid_scale)
        y = round((a.y + (b.y - a.y) * step / steps - origin_y) / grid_scale)
        if not points or points[-1] != (x, y):
            points.append((x, y))
    return points


def _texture(*names: str) -> str:
    return next((name for name in names if name and name != "-"), "UNTEXTURED")


def rasterize_map(doom_map: DoomMap, grid_scale: int = DEFAULT_GRID_SCALE) -> RasterizedWorld:
    if grid_scale <= 0:
        raise ValueError("grid scale must be positive")
    min_x, min_y, max_x, max_y = doom_map.bounds
    width = math.ceil((max_x - min_x) / grid_scale) + 1
    height = math.ceil((max_y - min_y) / grid_scale) + 1
    cell_sectors: dict[tuple[int, int], int] = {}
    boundary_edges = [
        (doom_map.vertices[line.v1], doom_map.vertices[line.v2])
        for line in doom_map.lines
        if line.left_side is None
    ]

    for grid_y in range(height):
        map_y = min_y + grid_y * grid_scale
        for grid_x in range(width):
            map_x = min_x + grid_x * grid_scale
            if _point_inside_edges(boundary_edges, map_x, map_y):
                cell_sectors[(grid_x, grid_y)] = doom_map.point_sector(map_x, map_y)

    material_keys = {
        *(f"floor:{sector.floor_texture}" for sector in doom_map.sectors),
        *(f"ceiling:{sector.ceiling_texture}" for sector in doom_map.sectors),
    }
    raw_surfaces: set[tuple[int, int, int, int, int, str, int, int]] = set()
    for (grid_x, grid_y), sector_id in cell_sectors.items():
        sector = doom_map.sectors[sector_id]
        floor_material = f"floor:{sector.floor_texture}"
        ceiling_material = f"ceiling:{sector.ceiling_texture}"
        raw_surfaces.add((grid_x, grid_y, sector.floor_height, sector.floor_height, SURFACE_FLOOR, floor_material, sector.light, sector_id))
        ceiling_kind = (
            SURFACE_SKY if sector.ceiling_texture.startswith("F_SKY") else SURFACE_CEILING
        )
        raw_surfaces.add((grid_x, grid_y, sector.ceiling_height, sector.ceiling_height, ceiling_kind, ceiling_material, sector.light, sector_id))

    blocked_cells: set[tuple[int, int]] = set()
    for line_id, line in enumerate(doom_map.lines):
        front_id = doom_map.side_sector(line.right_side)
        back_id = doom_map.side_sector(line.left_side)
        if front_id is None:
            continue
        front = doom_map.sectors[front_id]
        front_side = doom_map.sides[line.right_side]
        back = doom_map.sectors[back_id] if back_id is not None else None
        back_side = doom_map.sides[line.left_side] if line.left_side is not None else None
        points = _line_grid_points(
            doom_map.vertices[line.v1],
            doom_map.vertices[line.v2],
            min_x,
            min_y,
            grid_scale,
        )
        spans: list[tuple[int, int, int, str, int, int]] = []
        if back is None:
            spans.append((front.floor_height, front.ceiling_height, SURFACE_WALL, _texture(front_side.middle_texture), front.light, front_id))
            blocked_cells.update(points)
        else:
            assert back_side is not None
            if front.floor_height != back.floor_height:
                material = (
                    _texture(front_side.lower_texture, back_side.lower_texture)
                    if front.floor_height < back.floor_height
                    else _texture(back_side.lower_texture, front_side.lower_texture)
                )
                spans.append((min(front.floor_height, back.floor_height), max(front.floor_height, back.floor_height), SURFACE_WALL, material, min(front.light, back.light), front_id))
            if front.ceiling_height != back.ceiling_height:
                material = (
                    _texture(front_side.upper_texture, back_side.upper_texture)
                    if front.ceiling_height > back.ceiling_height
                    else _texture(back_side.upper_texture, front_side.upper_texture)
                )
                spans.append((min(front.ceiling_height, back.ceiling_height), max(front.ceiling_height, back.ceiling_height), SURFACE_WALL, material, min(front.light, back.light), front_id))
            middle = _texture(front_side.middle_texture, back_side.middle_texture)
            if middle != "UNTEXTURED":
                opening_bottom = max(front.floor_height, back.floor_height)
                opening_top = min(front.ceiling_height, back.ceiling_height)
                if opening_bottom < opening_top:
                    spans.append((opening_bottom, opening_top, SURFACE_MASKED, middle, min(front.light, back.light), front_id))
            if line.flags & ML_BLOCKING:
                blocked_cells.update(points)

        for z_bottom, z_top, surface_kind, texture, light, sector_id in spans:
            if z_bottom >= z_top:
                continue
            material_key = f"wall:{texture}"
            material_keys.add(material_key)
            for grid_x, grid_y in points:
                raw_surfaces.add((grid_x, grid_y, z_bottom, z_top, surface_kind, material_key, light, sector_id))

    material_ids = {name: index + 1 for index, name in enumerate(sorted(material_keys))}
    surfaces = [
        SurfaceSample(index, x, y, z_bottom, z_top, kind, material_ids[material], light, sector_id)
        for index, (x, y, z_bottom, z_top, kind, material, light, sector_id) in enumerate(
            sorted(raw_surfaces)
        )
    ]
    return RasterizedWorld(
        doom_map=doom_map,
        grid_scale=grid_scale,
        origin_x=min_x,
        origin_y=min_y,
        width=width,
        height=height,
        cell_sectors=cell_sectors,
        blocked_cells=blocked_cells,
        surfaces=surfaces,
        material_names={identifier: name for name, identifier in material_ids.items()},
        material_color_ramps={
            identifier: doom_map.material_color_ramps[name]
            for name, identifier in material_ids.items()
            if name in doom_map.material_color_ramps
        },
    )


def create_wad_parquet(
    path: Path,
    wad_path: Path,
    map_name: str,
    rows: int,
    row_group_size: int = 1_000_000,
    grid_scale: int = DEFAULT_GRID_SCALE,
) -> dict[str, object]:
    import duckdb

    if rows <= 0:
        raise ValueError("rows must be positive")
    doom_map = read_wad_map(wad_path, map_name)
    world = rasterize_map(doom_map, grid_scale)
    if not world.surfaces:
        raise ValueError(f"{map_name} produced no surface samples")
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped = str(path).replace("'", "''")
    with duckdb.connect(":memory:") as conn:
        conn.execute(
            """
            CREATE TABLE base_surfaces (
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
            "INSERT INTO base_surfaces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                for surface in world.surfaces
            ],
        )
        conn.execute(
            f"""
            COPY (
                SELECT
                    CAST(ids.range AS BIGINT) AS sample_id,
                    CAST(ids.range // {len(world.surfaces)} AS INTEGER) AS scan_id,
                    base.surface_id,
                    base.world_x,
                    base.world_y,
                    base.z_bottom,
                    base.z_top,
                    base.surface_kind,
                    base.material,
                    base.light,
                    base.sector_id
                FROM range({int(rows)}) AS ids
                JOIN base_surfaces AS base
                  ON base.surface_id = ids.range % {len(world.surfaces)}
            ) TO '{escaped}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {int(row_group_size)})
            """
        )

    start_x, start_y, start_z, start_heading, _ = world.player_camera(128)
    metadata: dict[str, object] = {
        "wad": str(wad_path),
        "map": doom_map.name,
        "grid_scale": grid_scale,
        "grid_width": world.width,
        "grid_height": world.height,
        "base_surfaces": len(world.surfaces),
        "floor_samples": sum(surface.surface_kind == SURFACE_FLOOR for surface in world.surfaces),
        "ceiling_samples": sum(surface.surface_kind == SURFACE_CEILING for surface in world.surfaces),
        "sky_samples": sum(surface.surface_kind == SURFACE_SKY for surface in world.surfaces),
        "wall_samples": sum(surface.surface_kind == SURFACE_WALL for surface in world.surfaces),
        "masked_samples": sum(surface.surface_kind == SURFACE_MASKED for surface in world.surfaces),
        "sectors": len(doom_map.sectors),
        "linedefs": len(doom_map.lines),
        "subsectors": len(doom_map.subsectors),
        "player_start": {
            "x": start_x,
            "y": start_y,
            "z": start_z,
            "heading": start_heading,
        },
        "materials": world.material_names,
        "rows": rows,
    }
    path.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata
