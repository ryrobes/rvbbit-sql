"""Dependency-free Doom WAD parsing and surface rasterization for DoomQL."""

from __future__ import annotations

import json
import math
import os
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
EPISODE_MAPS = tuple(f"E1M{index}" for index in range(1, 10))
DOOR_SPECIALS = {
    1, 2, 3, 4, 16, 26, 27, 28, 29, 31, 32, 33, 34, 42, 46, 50,
    61, 63, 75, 76, 86, 90, 99, 103, 105, 106, 107, 108, 109, 110,
    111, 112, 113, 114, 115, 116, 117, 118, 133, 134, 135, 136, 137,
}
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
EPISODE_SURFACE_COLUMNS = (
    "sample_id",
    "scan_id",
    "map_name",
    "surface_id",
    "world_x",
    "world_y",
    "z_bottom",
    "z_top",
    "surface_kind",
    "material",
    "light",
    "sector_id",
    "linedef_id",
    "texture_u",
    "texture_v",
    "face_light",
    "door_id",
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


@dataclass(frozen=True, slots=True)
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
    linedef_id: int = -1
    texture_u: int = 0
    texture_v: int = 0
    face_light: int = 0
    door_id: int = -1


@dataclass(frozen=True, slots=True)
class MaterialTexture:
    width: int
    height: int
    pixels: tuple[int, ...]
    palette: tuple[RGBColor, ...]
    color_maps: tuple[bytes, ...]

    def sample(self, u: int, v: int, light_level: int) -> RGBColor | None:
        if not self.pixels:
            return None
        palette_index = self.pixels[(v % self.height) * self.width + (u % self.width)]
        if palette_index < 0:
            return None
        color_map = self.color_maps[min(len(self.color_maps) - 1, max(0, light_level))]
        return self.palette[color_map[palette_index]]


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
    material_textures: dict[str, MaterialTexture]

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
    material_textures: dict[int, MaterialTexture]
    door_lines: dict[int, tuple[int, ...]]
    door_sectors: dict[int, int]

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
        sector_id = self.sector_id_at(x, y)
        return self.doom_map.sectors[sector_id] if sector_id is not None else None

    def sector_id_at(self, x: int, y: int) -> int | None:
        sector_id = self.cell_sectors.get((x, y))
        if sector_id is not None or self.cell_sectors:
            return sector_id
        map_x, map_y = self.to_map(x, y)
        return self.doom_map.point_sector(map_x, map_y)

    def player_camera(self, draw_distance: int) -> tuple[int, int, int, int, int]:
        start = self.doom_map.player_start
        x, y = self.to_grid(start.x, start.y)
        sector = self.sector_at(x, y)
        if sector is None:
            raise ValueError("player start did not rasterize into a sector")
        return x, y, sector.floor_height + PLAYER_EYE_HEIGHT, start.angle % 360, draw_distance

    def door_for_line(self, line: LineDef) -> int | None:
        front_id = self.doom_map.side_sector(line.right_side)
        back_id = self.doom_map.side_sector(line.left_side)
        for sector_id in (front_id, back_id):
            if sector_id is not None and sector_id in self.door_sectors:
                return self.door_sectors[sector_id]
        return None

    def sector_heights(
        self,
        sector_id: int,
        open_doors: frozenset[int] = frozenset(),
    ) -> tuple[int, int]:
        sector = self.doom_map.sectors[sector_id]
        door_id = self.door_sectors.get(sector_id)
        if door_id not in open_doors:
            return sector.floor_height, sector.ceiling_height
        adjacent_ceilings = []
        for line in self.doom_map.lines:
            front_id = self.doom_map.side_sector(line.right_side)
            back_id = self.doom_map.side_sector(line.left_side)
            if front_id == sector_id and back_id is not None:
                adjacent_ceilings.append(self.doom_map.sectors[back_id].ceiling_height)
            elif back_id == sector_id and front_id is not None:
                adjacent_ceilings.append(self.doom_map.sectors[front_id].ceiling_height)
        ceiling = max(adjacent_ceilings, default=sector.ceiling_height)
        return sector.floor_height, ceiling

    def line_blocks_player(
        self,
        line: LineDef,
        open_doors: frozenset[int] = frozenset(),
    ) -> bool:
        door_id = self.door_for_line(line)
        if door_id is not None and door_id in open_doors and line.left_side is not None:
            return False
        if line.left_side is None or line.flags & ML_BLOCKING:
            return True
        front_id = self.doom_map.side_sector(line.right_side)
        back_id = self.doom_map.side_sector(line.left_side)
        if front_id is None or back_id is None:
            return True
        front_floor, front_ceiling = self.sector_heights(front_id, open_doors)
        back_floor, back_ceiling = self.sector_heights(back_id, open_doors)
        opening_bottom = max(front_floor, back_floor)
        opening_top = min(front_ceiling, back_ceiling)
        return opening_top - opening_bottom < PLAYER_HEIGHT

    def position_is_clear(
        self,
        x: int,
        y: int,
        radius: int = PLAYER_RADIUS,
        open_doors: frozenset[int] = frozenset(),
    ) -> bool:
        map_x, map_y = self.to_map(x, y)
        radius_squared = radius * radius
        for line in self.doom_map.lines:
            if not self.line_blocks_player(line, open_doors):
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
        open_doors: frozenset[int] = frozenset(),
    ) -> tuple[int, int, int] | None:
        current_sector_id = self.sector_id_at(x, y)
        if current_sector_id is None:
            return None
        last_x, last_y = x, y
        steps = max(abs(target_x - x), abs(target_y - y), 1)
        for step in range(1, steps + 1):
            next_x = round(x + (target_x - x) * step / steps)
            next_y = round(y + (target_y - y) * step / steps)
            if not self.position_is_clear(next_x, next_y, open_doors=open_doors):
                return None
            next_sector_id = self.sector_id_at(next_x, next_y)
            if next_sector_id is None:
                return None
            current_floor, _ = self.sector_heights(current_sector_id, open_doors)
            next_floor, next_ceiling = self.sector_heights(next_sector_id, open_doors)
            if next_floor - current_floor > MAX_STEP_HEIGHT:
                return None
            if next_ceiling - next_floor < PLAYER_HEIGHT:
                return None
            last_x, last_y = next_x, next_y
            current_sector_id = next_sector_id
        floor_height, _ = self.sector_heights(current_sector_id, open_doors)
        return last_x, last_y, floor_height + PLAYER_EYE_HEIGHT

    def nearest_door(self, x: int, y: int, reach: int = 64) -> int | None:
        map_x, map_y = self.to_map(x, y)
        nearest: tuple[float, int] | None = None
        for door_id, line_ids in self.door_lines.items():
            for line_id in line_ids:
                line = self.doom_map.lines[line_id]
                distance = _point_segment_distance_squared(
                    map_x,
                    map_y,
                    self.doom_map.vertices[line.v1],
                    self.doom_map.vertices[line.v2],
                )
                candidate = (distance, door_id)
                if distance <= reach * reach and (nearest is None or candidate < nearest):
                    nearest = candidate
        return nearest[1] if nearest is not None else None


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


def _decode_wall_textures(
    lumps: dict[str, bytes],
) -> dict[str, tuple[int, int, tuple[int, ...]]]:
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
    textures: dict[str, tuple[int, int, tuple[int, ...]]] = {}
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
            textures[_decode_name(raw_name)] = (
                width,
                height,
                tuple(-1 if palette_index is None else palette_index for palette_index in canvas),
            )
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


def _read_material_assets(
    data: bytes,
    directory: list[tuple[str, int, int]],
    material_names: set[str],
) -> tuple[dict[str, MaterialColorRamp], dict[str, MaterialTexture]]:
    lumps = {
        name: data[offset : offset + size]
        for name, offset, size in directory
        if size > 0
    }
    playpal = lumps.get("PLAYPAL", b"")
    colormap = lumps.get("COLORMAP", b"")
    if len(playpal) < 256 * 3 or len(colormap) < 32 * 256:
        return {}, {}
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
    textures: dict[str, MaterialTexture] = {}
    for material_name in material_names:
        surface_kind, texture_name = material_name.split(":", 1)
        if surface_kind == "wall":
            texture = wall_textures.get(texture_name)
        else:
            flat = lumps.get(texture_name, b"")
            texture = (64, 64, tuple(flat)) if len(flat) == 64 * 64 else None
        if texture is None:
            continue
        texture_width, texture_height, texture_pixels = texture
        palette_indices = (index for index in texture_pixels if index >= 0)
        ramp = _average_color_ramp(palette_indices, palette, color_maps)
        if ramp:
            ramps[material_name] = ramp
            textures[material_name] = MaterialTexture(
                texture_width,
                texture_height,
                texture_pixels,
                palette,
                color_maps,
            )
    return ramps, textures


def read_wad_map(
    path: Path,
    map_name: str = "E1M1",
    load_materials: bool = True,
) -> DoomMap:
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
        material_color_ramps, material_textures = (
            _read_material_assets(data, directory, material_names)
            if load_materials
            else ({}, {})
        )
    except (IndexError, KeyError, struct.error, ValueError):
        material_color_ramps = {}
        material_textures = {}
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
        material_textures,
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


def _door_metadata(
    doom_map: DoomMap,
) -> tuple[dict[int, int], dict[int, tuple[int, ...]]]:
    door_sector_ids: set[int] = set()
    for line in doom_map.lines:
        if line.special not in DOOR_SPECIALS:
            continue
        if line.tag:
            door_sector_ids.update(
                sector_id
                for sector_id, sector in enumerate(doom_map.sectors)
                if sector.tag == line.tag
            )
        else:
            back_id = doom_map.side_sector(line.left_side)
            if back_id is not None:
                door_sector_ids.add(back_id)
    door_sectors = {sector_id: sector_id for sector_id in sorted(door_sector_ids)}
    line_ids: dict[int, list[int]] = {door_id: [] for door_id in door_sectors.values()}
    for line_id, line in enumerate(doom_map.lines):
        if line.left_side is None:
            continue
        touching = {
            sector_id
            for sector_id in (
                doom_map.side_sector(line.right_side),
                doom_map.side_sector(line.left_side),
            )
            if sector_id is not None and sector_id in door_sectors
        }
        for sector_id in touching:
            line_ids[door_sectors[sector_id]].append(line_id)
    return door_sectors, {
        door_id: tuple(ids) for door_id, ids in line_ids.items() if ids
    }


def rasterize_map(
    doom_map: DoomMap,
    grid_scale: int = DEFAULT_GRID_SCALE,
    material_ids: dict[str, int] | None = None,
    episode_details: bool = False,
    include_surfaces: bool = True,
    retain_cell_sectors: bool = True,
) -> RasterizedWorld:
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
    material_keys = {
        *(f"floor:{sector.floor_texture}" for sector in doom_map.sectors),
        *(f"ceiling:{sector.ceiling_texture}" for sector in doom_map.sectors),
    }
    door_sectors, door_lines = _door_metadata(doom_map)
    raw_surfaces: list[
        tuple[int, int, int, int, int, str, int, int, int, int, int, int, int]
    ] | set[
        tuple[int, int, int, int, int, str, int, int, int, int, int, int, int]
    ] = [] if episode_details else set()
    add_surface = raw_surfaces.append if isinstance(raw_surfaces, list) else raw_surfaces.add

    def add_sector_planes(grid_x: int, grid_y: int, sector_id: int) -> None:
        sector = doom_map.sectors[sector_id]
        floor_material = f"floor:{sector.floor_texture}"
        ceiling_material = f"ceiling:{sector.ceiling_texture}"
        texture_u = grid_x * grid_scale if episode_details else 0
        texture_v = grid_y * grid_scale if episode_details else 0
        add_surface((grid_x, grid_y, sector.floor_height, sector.floor_height, SURFACE_FLOOR, floor_material, sector.light, sector_id, -1, texture_u, texture_v, 0, -1))
        ceiling_kind = (
            SURFACE_SKY if sector.ceiling_texture.startswith("F_SKY") else SURFACE_CEILING
        )
        ceiling_door = door_sectors.get(sector_id, -1) if episode_details else -1
        add_surface((grid_x, grid_y, sector.ceiling_height, sector.ceiling_height, ceiling_kind, ceiling_material, sector.light, sector_id, -1, texture_u, texture_v, 0, ceiling_door))

    if include_surfaces:
        for grid_y in range(height):
            map_y = min_y + grid_y * grid_scale
            for grid_x in range(width):
                map_x = min_x + grid_x * grid_scale
                if not _point_inside_edges(boundary_edges, map_x, map_y):
                    continue
                sector_id = doom_map.point_sector(map_x, map_y)
                if retain_cell_sectors:
                    cell_sectors[(grid_x, grid_y)] = sector_id
                add_sector_planes(grid_x, grid_y, sector_id)

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
        points = (
            _line_grid_points(
                doom_map.vertices[line.v1],
                doom_map.vertices[line.v2],
                min_x,
                min_y,
                grid_scale,
            )
            if include_surfaces
            else ()
        )
        start_vertex = doom_map.vertices[line.v1]
        end_vertex = doom_map.vertices[line.v2]
        face_light = (
            16
            if start_vertex.x == end_vertex.x
            else (-16 if start_vertex.y == end_vertex.y else 0)
        )
        door_id = -1
        if back_id is not None:
            for sector_id in (front_id, back_id):
                if sector_id is not None and sector_id in door_sectors:
                    door_id = door_sectors[sector_id]
                    break
        spans: list[tuple[int, int, int, str, int, int]] = []
        if back is None:
            spans.append((front.floor_height, front.ceiling_height, SURFACE_WALL, _texture(front_side.middle_texture), front.light, front_id))
            if include_surfaces:
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
            if include_surfaces and line.flags & ML_BLOCKING:
                blocked_cells.update(points)

        for z_bottom, z_top, surface_kind, texture, light, sector_id in spans:
            if z_bottom >= z_top:
                continue
            material_key = f"wall:{texture}"
            material_keys.add(material_key)
            side = front_side if sector_id == front_id else (back_side or front_side)
            for point_index, (grid_x, grid_y) in enumerate(points):
                if not include_surfaces:
                    continue
                add_surface(
                    (
                        grid_x,
                        grid_y,
                        z_bottom,
                        z_top,
                        surface_kind,
                        material_key,
                        light,
                        sector_id,
                        line_id if episode_details else -1,
                        side.x_offset + point_index * grid_scale if episode_details else 0,
                        side.y_offset + z_bottom if episode_details else 0,
                        face_light if episode_details else 0,
                        door_id if episode_details else -1,
                    )
                )

    if material_ids is None:
        material_ids = {
            name: index + 1 for index, name in enumerate(sorted(material_keys))
        }
    missing_materials = material_keys - set(material_ids)
    if missing_materials:
        raise ValueError(f"missing global materials: {', '.join(sorted(missing_materials))}")
    ordered_surfaces = (
        sorted(raw_surfaces)
        if isinstance(raw_surfaces, set)
        else raw_surfaces
    )
    if isinstance(ordered_surfaces, list):
        ordered_surfaces.sort()
    surfaces = []
    while ordered_surfaces:
        (
            x,
            y,
            z_bottom,
            z_top,
            kind,
            material,
            light,
            sector_id,
            linedef_id,
            texture_u,
            texture_v,
            face_light,
            door_id,
        ) = ordered_surfaces.pop()
        surfaces.append(
            SurfaceSample(
                len(ordered_surfaces),
                x,
                y,
                z_bottom,
                z_top,
                kind,
                material_ids[material],
                light,
                sector_id,
                linedef_id,
                texture_u,
                texture_v,
                face_light,
                door_id,
            )
        )
    surfaces.reverse()
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
        material_textures={
            identifier: doom_map.material_textures[name]
            for name, identifier in material_ids.items()
            if name in doom_map.material_textures
        },
        door_lines=door_lines,
        door_sectors=door_sectors,
    )


def _episode_names(map_names: Iterable[str]) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(name.upper() for name in map_names))
    if not names:
        raise ValueError("episode must contain at least one map")
    return names


def _map_material_names(doom_map: DoomMap) -> set[str]:
    return {
        *(f"floor:{sector.floor_texture}" for sector in doom_map.sectors),
        *(f"ceiling:{sector.ceiling_texture}" for sector in doom_map.sectors),
        *(
            f"wall:{texture_name}"
            for side in doom_map.sides
            for texture_name in (
                side.upper_texture,
                side.lower_texture,
                side.middle_texture,
            )
            if texture_name != "-"
        ),
    }


def _episode_material_ids(
    wad_path: Path,
    map_names: Iterable[str],
) -> dict[str, int]:
    material_names = set()
    for map_name in _episode_names(map_names):
        material_names.update(
            _map_material_names(read_wad_map(wad_path, map_name, load_materials=False))
        )
    material_names.add("wall:UNTEXTURED")
    return {
        name: index + 1 for index, name in enumerate(sorted(material_names))
    }


def rasterize_episode(
    wad_path: Path,
    map_names: Iterable[str] = EPISODE_MAPS,
    grid_scale: int = DEFAULT_GRID_SCALE,
    retain_surfaces: bool = False,
) -> dict[str, RasterizedWorld]:
    names = _episode_names(map_names)
    material_ids = _episode_material_ids(wad_path, names)
    worlds = {}
    for map_name in names:
        doom_map = read_wad_map(wad_path, map_name)
        world = rasterize_map(
            doom_map,
            grid_scale,
            material_ids=material_ids,
            episode_details=True,
            include_surfaces=retain_surfaces,
        )
        worlds[doom_map.name] = world
    return worlds


def _write_episode_map_base(
    path: Path,
    metadata_path: Path,
    wad_path: Path,
    map_name: str,
    grid_scale: int,
    material_ids: dict[str, int],
) -> None:
    import csv
    import gzip

    doom_map = read_wad_map(wad_path, map_name, load_materials=False)
    world = rasterize_map(
        doom_map,
        grid_scale,
        material_ids=material_ids,
        episode_details=True,
        retain_cell_sectors=False,
    )
    with gzip.open(path, "wt", encoding="ascii", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(EPISODE_SURFACE_COLUMNS[2:])
        writer.writerows(
            (
                map_name,
                surface.surface_id,
                surface.world_x,
                surface.world_y,
                surface.z_bottom,
                surface.z_top,
                surface.surface_kind,
                surface.material,
                surface.light,
                surface.sector_id,
                surface.linedef_id,
                surface.texture_u,
                surface.texture_v,
                surface.face_light,
                surface.door_id,
            )
            for surface in world.surfaces
        )
    if hasattr(os, "posix_fadvise"):
        with path.open("rb") as source:
            os.posix_fadvise(
                source.fileno(),
                0,
                0,
                os.POSIX_FADV_DONTNEED,
            )
    start_x, start_y, start_z, start_heading, _ = world.player_camera(128)
    metadata_path.write_text(
        json.dumps(
            {
                "base_surfaces": len(world.surfaces),
                "doors": len(world.door_lines),
                "player_start": {
                    "x": start_x,
                    "y": start_y,
                    "z": start_z,
                    "heading": start_heading,
                },
            }
        ),
        encoding="utf-8",
    )


def _write_episode_material_ids(
    path: Path,
    wad_path: Path,
    map_names: tuple[str, ...],
) -> None:
    path.write_text(
        json.dumps(_episode_material_ids(wad_path, map_names)),
        encoding="utf-8",
    )


def create_episode_parquet(
    path: Path,
    wad_path: Path,
    map_names: Iterable[str],
    rows: int,
    row_group_size: int = 1_000_000,
    grid_scale: int = DEFAULT_GRID_SCALE,
) -> dict[str, object]:
    import duckdb

    if rows <= 0:
        raise ValueError("rows must be positive")
    names = _episode_names(map_names)
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped = str(path).replace("'", "''")
    map_metadata = {}
    import multiprocessing
    import tempfile

    available_methods = multiprocessing.get_all_start_methods()
    start_method = "forkserver" if "forkserver" in available_methods else "spawn"
    context = multiprocessing.get_context(start_method)
    with tempfile.TemporaryDirectory(prefix="doomql-episode-") as temp_dir:
        temp_root = Path(temp_dir)
        material_path = temp_root / "materials.json"
        material_worker = context.Process(
            target=_write_episode_material_ids,
            args=(material_path, wad_path, names),
        )
        material_worker.start()
        material_worker.join()
        if material_worker.exitcode != 0 or not material_path.exists():
            raise RuntimeError(
                "failed to collect Episode 1 materials "
                f"(exit {material_worker.exitcode})"
            )
        material_ids = {
            name: int(identifier)
            for name, identifier in json.loads(
                material_path.read_text(encoding="utf-8")
            ).items()
        }
        base_paths = []
        for map_name in names:
            print(f"  rasterizing {map_name}", flush=True)
            base_path = temp_root / f"{map_name.lower()}.csv.gz"
            metadata_path = temp_root / f"{map_name.lower()}.json"
            worker = context.Process(
                target=_write_episode_map_base,
                args=(
                    base_path,
                    metadata_path,
                    wad_path,
                    map_name,
                    grid_scale,
                    material_ids,
                ),
            )
            worker.start()
            worker.join()
            if worker.exitcode != 0 or not metadata_path.exists():
                raise RuntimeError(
                    f"failed to rasterize {map_name} in episode worker "
                    f"(exit {worker.exitcode})"
                )
            map_metadata[map_name] = json.loads(
                metadata_path.read_text(encoding="utf-8")
            )
            base_paths.append(base_path)
        total_surfaces = sum(
            int(metadata["base_surfaces"])
            for metadata in map_metadata.values()
        )
        if not total_surfaces:
            raise ValueError("episode produced no surface samples")
        print(
            f"  composing {total_surfaces:,} base surfaces into {rows:,} rows",
            flush=True,
        )
        parquet_sources = ", ".join(
            f"'{str(base_path).replace(chr(39), chr(39) * 2)}'"
            for base_path in base_paths
        )
        map_order = "CASE map_name " + " ".join(
            f"WHEN '{map_name}' THEN {index}"
            for index, map_name in enumerate(names)
        ) + " END"
        with duckdb.connect(":memory:") as conn:
            conn.execute(
                f"""
                COPY (
                    WITH ordered_base AS (
                        SELECT
                            row_number() OVER (
                                ORDER BY {map_order}, surface_id
                            ) - 1 AS global_surface_id,
                            *
                        FROM read_csv_auto([{parquet_sources}], header = true)
                    )
                    SELECT
                        CAST(ids.range AS BIGINT) AS sample_id,
                        CAST(ids.range // {total_surfaces} AS INTEGER) AS scan_id,
                        base.map_name,
                        CAST(base.surface_id AS INTEGER) AS surface_id,
                        CAST(base.world_x AS SMALLINT) AS world_x,
                        CAST(base.world_y AS SMALLINT) AS world_y,
                        CAST(base.z_bottom AS SMALLINT) AS z_bottom,
                        CAST(base.z_top AS SMALLINT) AS z_top,
                        CAST(base.surface_kind AS SMALLINT) AS surface_kind,
                        CAST(base.material AS SMALLINT) AS material,
                        CAST(base.light AS SMALLINT) AS light,
                        CAST(base.sector_id AS SMALLINT) AS sector_id,
                        CAST(base.linedef_id AS SMALLINT) AS linedef_id,
                        CAST(base.texture_u AS INTEGER) AS texture_u,
                        CAST(base.texture_v AS INTEGER) AS texture_v,
                        CAST(base.face_light AS SMALLINT) AS face_light,
                        CAST(base.door_id AS SMALLINT) AS door_id
                    FROM range({int(rows)}) AS ids
                    JOIN ordered_base AS base
                      ON base.global_surface_id = ids.range % {total_surfaces}
                    ORDER BY base.map_name, scan_id, base.surface_id
                ) TO '{escaped}'
                (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {int(row_group_size)})
                """
            )
    metadata: dict[str, object] = {
        "wad": str(wad_path),
        "maps": map_metadata,
        "grid_scale": grid_scale,
        "base_surfaces": total_surfaces,
        "complete_scans": rows // total_surfaces,
        "partial_scan_rows": rows % total_surfaces,
        "materials": {
            identifier: name for name, identifier in material_ids.items()
        },
        "rows": rows,
    }
    path.with_suffix(".json").write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )
    return metadata


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
