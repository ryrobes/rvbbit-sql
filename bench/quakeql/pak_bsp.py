"""Read Quake PAK/BSP v29 assets and sample textured, lightmapped faces."""

from __future__ import annotations

import hashlib
import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence


BSP_VERSION = 29
HEADER_LUMPS = 15
COORD_SCALE = 8

LUMP_ENTITIES = 0
LUMP_PLANES = 1
LUMP_TEXTURES = 2
LUMP_VERTEXES = 3
LUMP_VISIBILITY = 4
LUMP_NODES = 5
LUMP_TEXINFO = 6
LUMP_FACES = 7
LUMP_LIGHTING = 8
LUMP_CLIPNODES = 9
LUMP_LEAFS = 10
LUMP_MARKSURFACES = 11
LUMP_EDGES = 12
LUMP_SURFEDGES = 13
LUMP_MODELS = 14

CONTENTS_EMPTY = -1
CONTENTS_SOLID = -2
CONTENTS_WATER = -3
CONTENTS_SLIME = -4
CONTENTS_LAVA = -5
CONTENTS_SKY = -6

SURFACE_WALL = 0
SURFACE_FLOOR = 1
SURFACE_CEILING = 2
SURFACE_SKY = 3
SURFACE_LIQUID = 4

VISIBLE_BRUSH_CLASSES = frozenset(
    {
        "func_bossgate",
        "func_button",
        "func_door",
        "func_door_secret",
        "func_episodegate",
        "func_illusionary",
        "func_plat",
        "func_train",
        "func_wall",
    }
)

SHAREWARE_PAK_SIZE = 18_689_235
SHAREWARE_PAK_MD5 = "5906e5998fc3d896ddaf5e6a62e03abb"
SHAREWARE_PAK_SHA1 = "36b42dc7b6313fd9cabc0be8b9e9864840929735"


@dataclass(frozen=True, slots=True)
class PakEntry:
    name: str
    offset: int
    size: int


class PakArchive:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.data = self.path.read_bytes()
        if len(self.data) < 12 or self.data[:4] != b"PACK":
            raise ValueError(f"not a Quake PAK archive: {self.path}")
        directory_offset, directory_size = struct.unpack_from("<2i", self.data, 4)
        if directory_offset < 12 or directory_size < 0 or directory_size % 64:
            raise ValueError("invalid PAK directory")
        if directory_offset + directory_size > len(self.data):
            raise ValueError("PAK directory extends beyond the file")
        entries: dict[str, PakEntry] = {}
        for offset in range(directory_offset, directory_offset + directory_size, 64):
            raw_name, file_offset, file_size = struct.unpack_from("<56s2i", self.data, offset)
            name = raw_name.split(b"\0", 1)[0].decode("ascii", "replace").replace("\\", "/")
            if file_offset < 0 or file_size < 0 or file_offset + file_size > len(self.data):
                raise ValueError(f"invalid PAK entry bounds: {name}")
            entries[name.lower()] = PakEntry(name, file_offset, file_size)
        self.entries = entries

    def read(self, name: str) -> bytes:
        entry = self.entries.get(name.replace("\\", "/").lower())
        if entry is None:
            raise KeyError(f"PAK entry not found: {name}")
        return self.data[entry.offset : entry.offset + entry.size]

    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries.values())

    def hashes(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "bytes": len(self.data),
            "md5": hashlib.md5(self.data).hexdigest(),  # noqa: S324 - asset identity
            "sha1": hashlib.sha1(self.data).hexdigest(),  # noqa: S324 - asset identity
            "shareware_identity": (
                len(self.data) == SHAREWARE_PAK_SIZE
                and hashlib.md5(self.data).hexdigest() == SHAREWARE_PAK_MD5  # noqa: S324
                and hashlib.sha1(self.data).hexdigest() == SHAREWARE_PAK_SHA1  # noqa: S324
            ),
        }


@dataclass(frozen=True, slots=True)
class Plane:
    normal: tuple[float, float, float]
    distance: float
    kind: int


@dataclass(frozen=True, slots=True)
class Texture:
    name: str
    width: int
    height: int
    mip_pixels: tuple[bytes, bytes, bytes, bytes]

    @property
    def pixels(self) -> bytes:
        return self.mip_pixels[0]


@dataclass(frozen=True, slots=True)
class TexInfo:
    s: tuple[float, float, float, float]
    t: tuple[float, float, float, float]
    texture_id: int
    flags: int


@dataclass(frozen=True, slots=True)
class Face:
    plane_id: int
    side: int
    first_edge: int
    edge_count: int
    texinfo_id: int
    styles: tuple[int, int, int, int]
    light_offset: int


@dataclass(frozen=True, slots=True)
class Node:
    plane_id: int
    children: tuple[int, int]


@dataclass(frozen=True, slots=True)
class ClipNode:
    plane_id: int
    children: tuple[int, int]


@dataclass(frozen=True, slots=True)
class Leaf:
    contents: int


@dataclass(frozen=True, slots=True)
class Model:
    mins: tuple[float, float, float]
    maxs: tuple[float, float, float]
    origin: tuple[float, float, float]
    headnodes: tuple[int, int, int, int]
    visleafs: int
    first_face: int
    face_count: int


@dataclass(frozen=True, slots=True)
class BrushEntity:
    entity_id: int
    model_id: int
    classname: str
    origin: tuple[float, float, float]
    solid: bool
    mover: bool
    targetname: str | None
    target: str | None
    closed_origin: tuple[float, float, float]
    open_origin: tuple[float, float, float]
    speed: float


@dataclass(frozen=True, slots=True)
class CollisionWorld:
    map_name: str
    spawn_origin: tuple[float, float, float]
    spawn_yaw: float
    bounds: tuple[tuple[float, float, float], tuple[float, float, float]]
    planes: tuple[Plane, ...]
    clipnodes: tuple[ClipNode, ...]
    models: tuple[Model, ...]
    brushes: tuple[BrushEntity, ...]

    def spawn(self) -> tuple[tuple[float, float, float], float]:
        return self.spawn_origin, self.spawn_yaw

    def brush_entities(self, renderable_only: bool = True) -> tuple[BrushEntity, ...]:
        if not renderable_only:
            return self.brushes
        return tuple(brush for brush in self.brushes if brush.classname in VISIBLE_BRUSH_CLASSES)

    def hull_contents(
        self,
        point: Sequence[float],
        hull: int = 1,
        model_id: int = 0,
        origin: Sequence[float] = (0.0, 0.0, 0.0),
    ) -> int:
        if not 1 <= hull < 4:
            raise ValueError("SQL collision worlds support hulls 1 through 3")
        if not 0 <= model_id < len(self.models):
            raise ValueError("invalid BSP model")
        local_point = tuple(point[index] - origin[index] for index in range(3))
        current = self.models[model_id].headnodes[hull]
        while current >= 0:
            if current >= len(self.clipnodes):
                return CONTENTS_SOLID
            node = self.clipnodes[current]
            plane = self.planes[node.plane_id]
            distance = dot(local_point, plane.normal) - plane.distance
            current = node.children[0] if distance >= 0 else node.children[1]
        return current

    def position_is_solid(
        self,
        point: Sequence[float],
        hull: int = 1,
        include_brush_models: bool = True,
        brush_origins: dict[int, Sequence[float]] | None = None,
    ) -> bool:
        if self.hull_contents(point, hull=hull) == CONTENTS_SOLID:
            return True
        if not include_brush_models:
            return False
        return any(
            brush.solid
            and self.hull_contents(
                point,
                hull=hull,
                model_id=brush.model_id,
                origin=(brush_origins or {}).get(brush.model_id, brush.origin),
            )
            == CONTENTS_SOLID
            for brush in self.brushes
        )


@dataclass(frozen=True, slots=True)
class SurfaceSample:
    base_id: int
    surface_id: int
    model_id: int
    world_x: int
    world_y: int
    world_z: int
    normal_x: int
    normal_y: int
    normal_z: int
    surface_kind: int
    material_id: int
    texture_name: str
    texture_u: int
    texture_v: int
    red: int
    green: int
    blue: int
    light: int
    fullbright: int
    plane_distance: float
    texture_width: int
    texture_height: int
    texture_s_x: float
    texture_s_y: float
    texture_s_z: float
    texture_s_offset: float
    texture_t_x: float
    texture_t_y: float
    texture_t_z: float
    texture_t_offset: float


@dataclass(frozen=True, slots=True)
class LightmapTexel:
    surface_id: int
    style_slot: int
    style_id: int
    light_min_s: int
    light_min_t: int
    light_width: int
    light_height: int
    light_u: int
    light_v: int
    light_value: int


@dataclass(frozen=True, slots=True)
class MaterialFrame:
    source_material_id: int
    source_texture_name: str
    frame_index: int
    frame_count: int
    target_material_id: int
    target_texture_name: str


@dataclass(frozen=True, slots=True)
class RasterizedMap:
    map_name: str
    samples: tuple[SurfaceSample, ...]
    spawn_origin: tuple[float, float, float]
    spawn_yaw: float
    bounds: tuple[tuple[float, float, float], tuple[float, float, float]]
    face_count: int
    sampled_faces: int
    model_count: int
    texture_names: tuple[str, ...]
    surface_counts: dict[str, int]
    brush_models: tuple[BrushEntity, ...]
    brush_sample_count: int
    brush_sample_step: float


class QuakeBsp:
    def __init__(self, data: bytes, palette: bytes, map_name: str = "E1M1"):
        if len(data) < 4 + HEADER_LUMPS * 8:
            raise ValueError("BSP header is truncated")
        version = struct.unpack_from("<i", data, 0)[0]
        if version != BSP_VERSION:
            raise ValueError(f"unsupported BSP version {version}; expected {BSP_VERSION}")
        if len(palette) < 256 * 3:
            raise ValueError("Quake palette must contain 768 bytes")
        self.data = data
        self.palette = tuple(
            (palette[index], palette[index + 1], palette[index + 2])
            for index in range(0, 256 * 3, 3)
        )
        self.map_name = map_name.upper()
        self.lumps = tuple(
            struct.unpack_from("<2i", data, 4 + index * 8) for index in range(HEADER_LUMPS)
        )
        for index, (offset, size) in enumerate(self.lumps):
            if offset < 0 or size < 0 or offset + size > len(data):
                raise ValueError(f"BSP lump {index} extends beyond the file")

        self.entities_text = (
            self._lump(LUMP_ENTITIES).split(b"\0", 1)[0].decode("latin1", "replace")
        )
        self.entities = parse_entities(self.entities_text)
        self.planes = self._parse_records(
            LUMP_PLANES, "<4fi", lambda row: Plane(tuple(row[:3]), row[3], row[4])
        )
        self.textures = self._parse_textures()
        self.vertices = self._parse_records(LUMP_VERTEXES, "<3f", lambda row: tuple(row))
        self.nodes = self._parse_records(
            LUMP_NODES, "<i2h3h3h2H", lambda row: Node(row[0], (row[1], row[2]))
        )
        self.texinfo = self._parse_records(
            LUMP_TEXINFO,
            "<8f2i",
            lambda row: TexInfo(tuple(row[:4]), tuple(row[4:8]), row[8], row[9]),
        )
        self.faces = self._parse_records(
            LUMP_FACES,
            "<2hihh4Bi",
            lambda row: Face(row[0], row[1], row[2], row[3], row[4], tuple(row[5:9]), row[9]),
        )
        self.light_data = self._lump(LUMP_LIGHTING)
        self.clipnodes = self._parse_records(
            LUMP_CLIPNODES, "<i2h", lambda row: ClipNode(row[0], (row[1], row[2]))
        )
        self.leaves = self._parse_records(LUMP_LEAFS, "<ii3h3h2H4B", lambda row: Leaf(row[0]))
        self.edges = self._parse_records(LUMP_EDGES, "<2H", lambda row: (row[0], row[1]))
        surfedge_lump = self._lump(LUMP_SURFEDGES)
        if len(surfedge_lump) % 4:
            raise ValueError("invalid surfedge lump size")
        self.surfedges = tuple(row[0] for row in struct.iter_unpack("<i", surfedge_lump))
        self.models = self._parse_records(
            LUMP_MODELS,
            "<9f7i",
            lambda row: Model(
                tuple(row[:3]),
                tuple(row[3:6]),
                tuple(row[6:9]),
                tuple(row[9:13]),
                row[13],
                row[14],
                row[15],
            ),
        )
        if not self.models:
            raise ValueError("BSP has no world model")

    @classmethod
    def from_pak(cls, pak: PakArchive, map_name: str = "E1M1") -> "QuakeBsp":
        return cls(pak.read(f"maps/{map_name.lower()}.bsp"), pak.read("gfx/palette.lmp"), map_name)

    def _lump(self, index: int) -> bytes:
        offset, size = self.lumps[index]
        return self.data[offset : offset + size]

    def _parse_records(self, lump_index: int, fmt: str, factory):
        raw = self._lump(lump_index)
        size = struct.calcsize(fmt)
        if len(raw) % size:
            raise ValueError(f"invalid BSP lump {lump_index} record size")
        return tuple(factory(row) for row in struct.iter_unpack(fmt, raw))

    def _parse_textures(self) -> tuple[Texture | None, ...]:
        raw = self._lump(LUMP_TEXTURES)
        if len(raw) < 4:
            return ()
        count = struct.unpack_from("<i", raw, 0)[0]
        if count < 0 or 4 + count * 4 > len(raw):
            raise ValueError("invalid BSP texture directory")
        offsets = struct.unpack_from(f"<{count}i", raw, 4)
        textures: list[Texture | None] = []
        for texture_offset in offsets:
            if texture_offset < 0:
                textures.append(None)
                continue
            if texture_offset + 40 > len(raw):
                raise ValueError("BSP mip texture header is truncated")
            name_raw, width, height, *mip_offsets = struct.unpack_from(
                "<16s6I", raw, texture_offset
            )
            if width <= 0 or height <= 0:
                raise ValueError("BSP mip texture dimensions are invalid")
            name = name_raw.split(b"\0", 1)[0].decode("ascii", "replace")
            mip_pixels: list[bytes] = []
            for mip_level, mip_offset in enumerate(mip_offsets):
                mip_width = max(1, width >> mip_level)
                mip_height = max(1, height >> mip_level)
                start = texture_offset + mip_offset
                end = start + mip_width * mip_height
                if mip_offset <= 0 or end > len(raw):
                    raise ValueError("BSP mip texture pixels are truncated")
                mip_pixels.append(raw[start:end])
            textures.append(Texture(name, width, height, tuple(mip_pixels)))
        return tuple(textures)

    def face_vertices(self, face: Face) -> tuple[tuple[float, float, float], ...]:
        vertices: list[tuple[float, float, float]] = []
        end = face.first_edge + face.edge_count
        if face.first_edge < 0 or end > len(self.surfedges):
            raise ValueError("face surfedge range is invalid")
        for surfedge in self.surfedges[face.first_edge : end]:
            edge_index = abs(surfedge)
            if edge_index >= len(self.edges):
                raise ValueError("face references an invalid edge")
            edge = self.edges[edge_index]
            vertex_id = edge[0] if surfedge >= 0 else edge[1]
            if vertex_id >= len(self.vertices):
                raise ValueError("edge references an invalid vertex")
            vertices.append(self.vertices[vertex_id])
        return tuple(vertices)

    def face_model_ids(self) -> tuple[int, ...]:
        result = [-1] * len(self.faces)
        for model_id, model in enumerate(self.models):
            for face_id in range(
                model.first_face, min(len(self.faces), model.first_face + model.face_count)
            ):
                if result[face_id] == -1:
                    result[face_id] = model_id
        return tuple(result)

    def brush_entities(self, renderable_only: bool = True) -> tuple[BrushEntity, ...]:
        brushes: list[BrushEntity] = []
        for entity_id, entity in enumerate(self.entities):
            model_name = entity.get("model", "")
            if not model_name.startswith("*") or not model_name[1:].isdigit():
                continue
            model_id = int(model_name[1:])
            if not 0 < model_id < len(self.models):
                continue
            classname = entity.get("classname", "")
            if renderable_only and classname not in VISIBLE_BRUSH_CLASSES:
                continue
            origin, closed_origin, open_origin, speed = _brush_motion(entity, self.models[model_id])
            brushes.append(
                BrushEntity(
                    entity_id,
                    model_id,
                    classname,
                    origin,
                    classname != "func_illusionary",
                    closed_origin != open_origin,
                    entity.get("targetname"),
                    entity.get("target"),
                    closed_origin,
                    open_origin,
                    speed,
                )
            )
        return tuple(brushes)

    def spawn(self) -> tuple[tuple[float, float, float], float]:
        preferred = next(
            (entity for entity in self.entities if entity.get("classname") == "info_player_start"),
            None,
        )
        if preferred is None:
            preferred = next(
                (
                    entity
                    for entity in self.entities
                    if entity.get("classname", "").startswith("info_player")
                ),
                None,
            )
        if preferred is None:
            world = self.models[0]
            return tuple((low + high) / 2 for low, high in zip(world.mins, world.maxs)), 0.0
        origin = parse_vector(preferred.get("origin", "0 0 0"))
        yaw = float(preferred.get("angle", "0") or 0)
        return origin, yaw

    def point_contents(self, point: Sequence[float], node_index: int | None = None) -> int:
        current = self.models[0].headnodes[0] if node_index is None else node_index
        while current >= 0:
            if current >= len(self.nodes):
                return CONTENTS_SOLID
            node = self.nodes[current]
            plane = self.planes[node.plane_id]
            distance = dot(point, plane.normal) - plane.distance
            current = node.children[0] if distance >= 0 else node.children[1]
        leaf_index = -current - 1
        if not 0 <= leaf_index < len(self.leaves):
            return CONTENTS_SOLID
        return self.leaves[leaf_index].contents

    def hull_contents(
        self,
        point: Sequence[float],
        hull: int = 1,
        model_id: int = 0,
        origin: Sequence[float] = (0.0, 0.0, 0.0),
    ) -> int:
        if not 0 <= hull < 4:
            raise ValueError("hull must be between 0 and 3")
        if not 0 <= model_id < len(self.models):
            raise ValueError("invalid BSP model")
        local_point = tuple(point[index] - origin[index] for index in range(3))
        if hull == 0:
            if model_id != 0:
                raise ValueError("submodel hull 0 is not available from the BSP clipnode lump")
            return self.point_contents(local_point)
        current = self.models[model_id].headnodes[hull]
        while current >= 0:
            if current >= len(self.clipnodes):
                return CONTENTS_SOLID
            node = self.clipnodes[current]
            plane = self.planes[node.plane_id]
            distance = dot(local_point, plane.normal) - plane.distance
            current = node.children[0] if distance >= 0 else node.children[1]
        return current

    def position_is_solid(
        self,
        point: Sequence[float],
        hull: int = 1,
        include_brush_models: bool = True,
        brush_origins: dict[int, Sequence[float]] | None = None,
    ) -> bool:
        if self.hull_contents(point, hull=hull) == CONTENTS_SOLID:
            return True
        if not include_brush_models:
            return False
        return any(
            brush.solid
            and self.hull_contents(
                point,
                hull=hull,
                model_id=brush.model_id,
                origin=(brush_origins or {}).get(brush.model_id, brush.origin),
            )
            == CONTENTS_SOLID
            for brush in self.brush_entities()
        )


def parse_entities(text: str) -> tuple[dict[str, str], ...]:
    entities: list[dict[str, str]] = []
    for block in re.findall(r"\{([^}]*)\}", text, flags=re.DOTALL):
        pairs = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"\s*"([^"\\]*(?:\\.[^"\\]*)*)"', block)
        entities.append(
            {key.replace(r"\"", '"'): value.replace(r"\"", '"') for key, value in pairs}
        )
    return tuple(entities)


def parse_vector(value: str) -> tuple[float, float, float]:
    parts = value.split()
    if len(parts) != 3:
        raise ValueError(f"expected a three-component vector, got {value!r}")
    return float(parts[0]), float(parts[1]), float(parts[2])


def _move_direction(angle: float) -> tuple[float, float, float]:
    if angle == -1:
        return 0.0, 0.0, 1.0
    if angle == -2:
        return 0.0, 0.0, -1.0
    radians = math.radians(angle)
    return math.cos(radians), math.sin(radians), 0.0


def _brush_motion(
    entity: dict[str, str], model: Model
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    float,
]:
    base = parse_vector(entity.get("origin", "0 0 0"))
    classname = entity.get("classname", "")
    # Mod_LoadSubmodels expands both bounds by one before QuakeC reads entity size.
    size = tuple(model.maxs[index] - model.mins[index] + 2.0 for index in range(3))
    if classname in {"func_door", "func_door_secret", "func_button"}:
        direction = _move_direction(float(entity.get("angle", "0") or 0))
        default_lip = 4.0 if classname == "func_button" else 8.0
        lip = float(entity.get("lip", str(default_lip)) or default_lip)
        travel = max(0.0, abs(dot(direction, size)) - lip)
        closed = base
        opened = tuple(base[index] + direction[index] * travel for index in range(3))
        start_open = classname == "func_door" and int(entity.get("spawnflags", "0") or 0) & 1
        initial = opened if start_open else closed
        default_speed = 40.0 if classname == "func_button" else 100.0
        return initial, closed, opened, float(entity.get("speed", default_speed) or default_speed)
    if classname == "func_plat":
        height = float(entity.get("height", "0") or 0)
        travel = height if height else max(0.0, size[2] - 8.0)
        top = base
        bottom = (base[0], base[1], base[2] - travel)
        initial = top if entity.get("targetname") else bottom
        speed = float(entity.get("speed", "150") or 150)
        return initial, bottom, top, speed
    return base, base, base, 0.0


def _brush_initial_origin(entity: dict[str, str], model: Model) -> tuple[float, float, float]:
    return _brush_motion(entity, model)[0]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((a[index] - b[index]) ** 2 for index in range(3)))


def midpoint(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return tuple((a[index] + b[index]) / 2 for index in range(3))


def triangle_samples(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
    step: float,
) -> Iterator[tuple[float, float, float]]:
    stack = [(a, b, c, 0)]
    while stack:
        p0, p1, p2, depth = stack.pop()
        edges = ((distance(p0, p1), 0), (distance(p1, p2), 1), (distance(p2, p0), 2))
        longest, edge = max(edges)
        if longest <= step or depth >= 20:
            yield tuple((p0[index] + p1[index] + p2[index]) / 3 for index in range(3))
            continue
        if edge == 0:
            split = midpoint(p0, p1)
            stack.append((p0, split, p2, depth + 1))
            stack.append((split, p1, p2, depth + 1))
        elif edge == 1:
            split = midpoint(p1, p2)
            stack.append((p1, split, p0, depth + 1))
            stack.append((split, p2, p0, depth + 1))
        else:
            split = midpoint(p2, p0)
            stack.append((p2, split, p1, depth + 1))
            stack.append((split, p0, p1, depth + 1))


def _face_points(
    vertices: Sequence[tuple[float, float, float]], step: float
) -> Iterator[tuple[float, float, float]]:
    if len(vertices) < 3:
        return
    for vertex in vertices:
        yield vertex
    for index in range(1, len(vertices) - 1):
        yield from triangle_samples(vertices[0], vertices[index], vertices[index + 1], step)


def _texture_coords(point: Sequence[float], texinfo: TexInfo) -> tuple[float, float]:
    return dot(point, texinfo.s[:3]) + texinfo.s[3], dot(point, texinfo.t[:3]) + texinfo.t[3]


def _face_extents(
    vertices: Sequence[Sequence[float]], texinfo: TexInfo
) -> tuple[int, int, int, int]:
    coords = [_texture_coords(vertex, texinfo) for vertex in vertices]
    minimum_s = math.floor(min(value[0] for value in coords) / 16) * 16
    minimum_t = math.floor(min(value[1] for value in coords) / 16) * 16
    maximum_s = math.ceil(max(value[0] for value in coords) / 16) * 16
    maximum_t = math.ceil(max(value[1] for value in coords) / 16) * 16
    return minimum_s, minimum_t, maximum_s - minimum_s, maximum_t - minimum_t


def _light_at(
    bsp: QuakeBsp,
    face: Face,
    texture_s: float,
    texture_t: float,
    extents: tuple[int, int, int, int],
    special: bool,
) -> int:
    if special or face.light_offset < 0 or not bsp.light_data:
        return 224 if special else 160
    minimum_s, minimum_t, extent_s, extent_t = extents
    width = extent_s // 16 + 1
    height = extent_t // 16 + 1
    x = max(0.0, min(width - 1.0, (texture_s - minimum_s) / 16.0))
    y = max(0.0, min(height - 1.0, (texture_t - minimum_t) / 16.0))
    x0, y0 = int(math.floor(x)), int(math.floor(y))
    x1, y1 = min(width - 1, x0 + 1), min(height - 1, y0 + 1)
    base = face.light_offset
    last = base + y1 * width + x1
    if base < 0 or last >= len(bsp.light_data):
        return 160
    fx, fy = x - x0, y - y0
    values = (
        bsp.light_data[base + y0 * width + x0],
        bsp.light_data[base + y0 * width + x1],
        bsp.light_data[base + y1 * width + x0],
        bsp.light_data[base + y1 * width + x1],
    )
    top = values[0] * (1 - fx) + values[1] * fx
    bottom = values[2] * (1 - fx) + values[3] * fx
    return max(24, min(255, round(top * (1 - fy) + bottom * fy)))


def _surface_kind(texture: Texture, normal: Sequence[float]) -> int:
    name = texture.name.lower()
    if name.startswith("sky"):
        return SURFACE_SKY
    if name.startswith("*"):
        return SURFACE_LIQUID
    if normal[2] >= 0.7:
        return SURFACE_FLOOR
    if normal[2] <= -0.7:
        return SURFACE_CEILING
    return SURFACE_WALL


def rasterize_map(
    bsp: QuakeBsp,
    sample_step: float = 8.0,
    include_brush_models: bool = True,
    brush_sample_step: float | None = None,
) -> RasterizedMap:
    if sample_step <= 0:
        raise ValueError("sample_step must be positive")
    if brush_sample_step is None:
        brush_sample_step = max(2.0, sample_step / 4.0)
    if brush_sample_step <= 0:
        raise ValueError("brush_sample_step must be positive")
    model_ids = bsp.face_model_ids()
    brushes = bsp.brush_entities() if include_brush_models else ()
    brushes_by_model = {brush.model_id: brush for brush in brushes}
    samples: list[SurfaceSample] = []
    sampled_faces = 0
    counts = {"wall": 0, "floor": 0, "ceiling": 0, "sky": 0, "liquid": 0}
    kind_names = {
        SURFACE_WALL: "wall",
        SURFACE_FLOOR: "floor",
        SURFACE_CEILING: "ceiling",
        SURFACE_SKY: "sky",
        SURFACE_LIQUID: "liquid",
    }
    for face_id, face in enumerate(bsp.faces):
        model_id = model_ids[face_id]
        if model_id < 0 or (model_id != 0 and model_id not in brushes_by_model):
            continue
        if not 0 <= face.texinfo_id < len(bsp.texinfo):
            continue
        texinfo = bsp.texinfo[face.texinfo_id]
        if not 0 <= texinfo.texture_id < len(bsp.textures):
            continue
        texture = bsp.textures[texinfo.texture_id]
        if texture is None:
            continue
        vertices = bsp.face_vertices(face)
        if len(vertices) < 3:
            continue
        plane = bsp.planes[face.plane_id]
        normal = tuple(-value if face.side else value for value in plane.normal)
        kind = _surface_kind(texture, normal)
        extents = _face_extents(vertices, texinfo)
        special = bool(texinfo.flags & 1) or kind in {SURFACE_SKY, SURFACE_LIQUID}
        sampled_faces += 1
        seen: set[tuple[int, int, int]] = set()
        origin = brushes_by_model[model_id].origin if model_id else (0.0, 0.0, 0.0)
        plane_distance = (-plane.distance if face.side else plane.distance) + sum(
            normal[index] * origin[index] for index in range(3)
        )
        texture_s_offset = texinfo.s[3] - sum(
            texinfo.s[index] * origin[index] for index in range(3)
        )
        texture_t_offset = texinfo.t[3] - sum(
            texinfo.t[index] * origin[index] for index in range(3)
        )
        face_step = brush_sample_step if model_id else sample_step
        for point in _face_points(vertices, face_step):
            world_point = tuple(point[index] + origin[index] for index in range(3))
            fixed = tuple(round(value * COORD_SCALE) for value in world_point)
            if fixed in seen:
                continue
            seen.add(fixed)
            texture_s, texture_t = _texture_coords(point, texinfo)
            u = math.floor(texture_s) % texture.width
            v = math.floor(texture_t) % texture.height
            palette_index = texture.pixels[v * texture.width + u]
            red, green, blue = bsp.palette[palette_index]
            light = _light_at(bsp, face, texture_s, texture_t, extents, special)
            counts[kind_names[kind]] += 1
            samples.append(
                SurfaceSample(
                    len(samples),
                    face_id,
                    model_id,
                    fixed[0],
                    fixed[1],
                    fixed[2],
                    round(normal[0] * 1024),
                    round(normal[1] * 1024),
                    round(normal[2] * 1024),
                    kind,
                    texinfo.texture_id,
                    texture.name,
                    u,
                    v,
                    red,
                    green,
                    blue,
                    light,
                    int(palette_index >= 224),
                    plane_distance,
                    texture.width,
                    texture.height,
                    texinfo.s[0],
                    texinfo.s[1],
                    texinfo.s[2],
                    texture_s_offset,
                    texinfo.t[0],
                    texinfo.t[1],
                    texinfo.t[2],
                    texture_t_offset,
                )
            )
    spawn_origin, spawn_yaw = bsp.spawn()
    world = bsp.models[0]
    return RasterizedMap(
        bsp.map_name,
        tuple(samples),
        spawn_origin,
        spawn_yaw,
        (world.mins, world.maxs),
        len(bsp.faces),
        sampled_faces,
        len(bsp.models),
        tuple(texture.name for texture in bsp.textures if texture is not None),
        counts,
        brushes,
        sum(sample.model_id != 0 for sample in samples),
        brush_sample_step,
    )


def extract_lightmap_texels(bsp: QuakeBsp) -> tuple[LightmapTexel, ...]:
    texels: list[LightmapTexel] = []
    for surface_id, face in enumerate(bsp.faces):
        if not 0 <= face.texinfo_id < len(bsp.texinfo):
            continue
        texinfo = bsp.texinfo[face.texinfo_id]
        if not 0 <= texinfo.texture_id < len(bsp.textures):
            continue
        texture = bsp.textures[texinfo.texture_id]
        if texture is None or face.light_offset < 0:
            continue
        vertices = bsp.face_vertices(face)
        if len(vertices) < 3:
            continue
        plane = bsp.planes[face.plane_id]
        normal = tuple(-value if face.side else value for value in plane.normal)
        kind = _surface_kind(texture, normal)
        if texinfo.flags & 1 or kind in {SURFACE_SKY, SURFACE_LIQUID}:
            continue
        minimum_s, minimum_t, extent_s, extent_t = _face_extents(vertices, texinfo)
        light_width = extent_s // 16 + 1
        light_height = extent_t // 16 + 1
        style_size = light_width * light_height
        for style_slot, style_id in enumerate(face.styles):
            if style_id == 255:
                continue
            start = face.light_offset + style_slot * style_size
            end = start + style_size
            if start < 0 or end > len(bsp.light_data):
                continue
            for index, light_value in enumerate(bsp.light_data[start:end]):
                texels.append(
                    LightmapTexel(
                        surface_id,
                        style_slot,
                        style_id,
                        minimum_s,
                        minimum_t,
                        light_width,
                        light_height,
                        index % light_width,
                        index // light_width,
                        light_value,
                    )
                )
    return tuple(texels)


def extract_material_frames(bsp: QuakeBsp) -> tuple[MaterialFrame, ...]:
    groups: dict[tuple[str, bool], list[tuple[int, int, str]]] = {}
    for material_id, texture in enumerate(bsp.textures):
        if texture is None or len(texture.name) < 3 or not texture.name.startswith("+"):
            continue
        marker = texture.name[1].lower()
        if marker.isdigit():
            frame_index, alternate = int(marker), False
        elif "a" <= marker <= "j":
            frame_index, alternate = ord(marker) - ord("a"), True
        else:
            continue
        groups.setdefault((texture.name[2:].lower(), alternate), []).append(
            (frame_index, material_id, texture.name)
        )

    frames: list[MaterialFrame] = []
    for source_material_id, texture in enumerate(bsp.textures):
        if texture is None:
            continue
        targets: list[tuple[int, int, str]] | None = None
        if len(texture.name) >= 3 and texture.name.startswith("+"):
            marker = texture.name[1].lower()
            alternate = "a" <= marker <= "j"
            if marker.isdigit() or alternate:
                targets = sorted(groups.get((texture.name[2:].lower(), alternate), ()))
        if not targets:
            targets = [(0, source_material_id, texture.name)]
        for frame_index, (_source_frame, target_material_id, target_name) in enumerate(targets):
            frames.append(
                MaterialFrame(
                    source_material_id,
                    texture.name,
                    frame_index,
                    len(targets),
                    target_material_id,
                    target_name,
                )
            )
    return tuple(frames)


def find_default_pak() -> Path:
    import os

    configured = os.environ.get("QUAKEQL_PAK")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(__file__).resolve().parent / "assets" / "pak0.pak",
        Path("~/.local/share/quake/id1/pak0.pak").expanduser(),
        Path("~/Games/quake/id1/pak0.pak").expanduser(),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return candidate
    return Path(__file__).resolve().parent / "assets" / "pak0.pak"
