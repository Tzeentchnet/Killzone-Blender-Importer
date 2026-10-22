# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import struct
from typing import Iterable

from .errors import MeshFormatError

MAGIC = b"KZMI"
FORMAT_MAJOR = 1
FORMAT_MINOR = 0

_HEADER = struct.Struct("<4sHHI")
_CHUNK_HEADER = struct.Struct("<4sI")
_COUNT = struct.Struct("<I")
_VERTEX = struct.Struct("<3f3f2f4B")
_TRIANGLE = struct.Struct("<4I")
_VERTEX_TAG = b"VERT"
_TRIANGLE_TAG = b"TRIS"
_KNOWN_TAGS = {_VERTEX_TAG, _TRIANGLE_TAG}

MAX_FILE_SIZE = 512 * 1024 * 1024
MAX_VERTICES = 10_000_000
MAX_TRIANGLES = 10_000_000
MAX_CHUNKS = 1_024


@dataclass(frozen=True, slots=True)
class Vertex:
    position: tuple[float, float, float]
    normal: tuple[float, float, float]
    uv: tuple[float, float]
    color: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class Triangle:
    vertices: tuple[int, int, int]
    material_index: int = 0


@dataclass(frozen=True, slots=True)
class OpaqueChunk:
    tag: bytes
    payload: bytes


@dataclass(frozen=True, slots=True)
class MeshData:
    vertices: tuple[Vertex, ...]
    triangles: tuple[Triangle, ...]
    opaque_chunks: tuple[OpaqueChunk, ...] = ()


def _require_available(data: bytes, offset: int, size: int, label: str) -> None:
    if size < 0 or offset < 0 or offset + size > len(data):
        raise MeshFormatError(f"Truncated {label} at offset 0x{offset:X}")


def _validate_float_tuple(values: Iterable[float], label: str) -> None:
    if not all(math.isfinite(value) for value in values):
        raise MeshFormatError(f"{label} contains a non-finite value")


def validate_mesh(mesh: MeshData) -> None:
    if len(mesh.vertices) > MAX_VERTICES:
        raise MeshFormatError(
            f"Vertex count {len(mesh.vertices)} exceeds {MAX_VERTICES}"
        )
    if len(mesh.triangles) > MAX_TRIANGLES:
        raise MeshFormatError(
            f"Triangle count {len(mesh.triangles)} exceeds {MAX_TRIANGLES}"
        )
    if len(mesh.opaque_chunks) + 2 > MAX_CHUNKS:
        raise MeshFormatError(f"Chunk count exceeds {MAX_CHUNKS}")

    for index, vertex in enumerate(mesh.vertices):
        if len(vertex.position) != 3:
            raise MeshFormatError(f"Vertex {index} position must have 3 values")
        if len(vertex.normal) != 3:
            raise MeshFormatError(f"Vertex {index} normal must have 3 values")
        if len(vertex.uv) != 2:
            raise MeshFormatError(f"Vertex {index} UV must have 2 values")
        if len(vertex.color) != 4:
            raise MeshFormatError(f"Vertex {index} color must have 4 values")
        _validate_float_tuple(vertex.position, f"Vertex {index} position")
        _validate_float_tuple(vertex.normal, f"Vertex {index} normal")
        _validate_float_tuple(vertex.uv, f"Vertex {index} UV")
        if any(
            not isinstance(component, int) or not 0 <= component <= 255
            for component in vertex.color
        ):
            raise MeshFormatError(
                f"Vertex {index} color components must be integers from 0 to 255"
            )

    vertex_count = len(mesh.vertices)
    for index, triangle in enumerate(mesh.triangles):
        if len(triangle.vertices) != 3:
            raise MeshFormatError(f"Triangle {index} must have 3 indices")
        if any(
            not isinstance(vertex_index, int)
            or not 0 <= vertex_index < vertex_count
            for vertex_index in triangle.vertices
        ):
            raise MeshFormatError(f"Triangle {index} references an invalid vertex")
        if (
            not isinstance(triangle.material_index, int)
            or not 0 <= triangle.material_index <= 0xFFFFFFFF
        ):
            raise MeshFormatError(f"Triangle {index} has an invalid material index")

    for index, chunk in enumerate(mesh.opaque_chunks):
        if len(chunk.tag) != 4:
            raise MeshFormatError(f"Opaque chunk {index} tag must be exactly 4 bytes")
        if chunk.tag in _KNOWN_TAGS:
            raise MeshFormatError(
                f"Opaque chunk {index} uses reserved tag {chunk.tag!r}"
            )
        if len(chunk.payload) > 0xFFFFFFFF:
            raise MeshFormatError(f"Opaque chunk {index} is too large")


def _parse_vertices(payload: bytes) -> tuple[Vertex, ...]:
    _require_available(payload, 0, _COUNT.size, "VERT count")
    (count,) = _COUNT.unpack_from(payload)
    if count > MAX_VERTICES:
        raise MeshFormatError(f"Vertex count {count} exceeds {MAX_VERTICES}")
    expected_size = _COUNT.size + count * _VERTEX.size
    if len(payload) != expected_size:
        raise MeshFormatError(
            f"VERT chunk size is {len(payload)}, expected {expected_size}"
        )

    vertices: list[Vertex] = []
    offset = _COUNT.size
    for _ in range(count):
        values = _VERTEX.unpack_from(payload, offset)
        vertices.append(
            Vertex(
                position=(values[0], values[1], values[2]),
                normal=(values[3], values[4], values[5]),
                uv=(values[6], values[7]),
                color=(values[8], values[9], values[10], values[11]),
            )
        )
        offset += _VERTEX.size
    return tuple(vertices)


def _parse_triangles(payload: bytes) -> tuple[Triangle, ...]:
    _require_available(payload, 0, _COUNT.size, "TRIS count")
    (count,) = _COUNT.unpack_from(payload)
    if count > MAX_TRIANGLES:
        raise MeshFormatError(f"Triangle count {count} exceeds {MAX_TRIANGLES}")
    expected_size = _COUNT.size + count * _TRIANGLE.size
    if len(payload) != expected_size:
        raise MeshFormatError(
            f"TRIS chunk size is {len(payload)}, expected {expected_size}"
        )

    triangles: list[Triangle] = []
    offset = _COUNT.size
    for _ in range(count):
        a, b, c, material_index = _TRIANGLE.unpack_from(payload, offset)
        triangles.append(
            Triangle(vertices=(a, b, c), material_index=material_index)
        )
        offset += _TRIANGLE.size
    return tuple(triangles)


def parse_mesh(data: bytes) -> MeshData:
    if len(data) > MAX_FILE_SIZE:
        raise MeshFormatError(
            f"Mesh size {len(data)} exceeds {MAX_FILE_SIZE} bytes"
        )
    _require_available(data, 0, _HEADER.size, "mesh header")
    magic, major, minor, chunk_count = _HEADER.unpack_from(data)
    if magic != MAGIC:
        raise MeshFormatError(f"Invalid mesh magic {magic!r}")
    if (major, minor) != (FORMAT_MAJOR, FORMAT_MINOR):
        raise MeshFormatError(
            f"Unsupported mesh version {major}.{minor}; "
            f"expected {FORMAT_MAJOR}.{FORMAT_MINOR}"
        )
    if chunk_count > MAX_CHUNKS:
        raise MeshFormatError(f"Chunk count {chunk_count} exceeds {MAX_CHUNKS}")

    vertices: tuple[Vertex, ...] | None = None
    triangles: tuple[Triangle, ...] | None = None
    opaque_chunks: list[OpaqueChunk] = []
    offset = _HEADER.size

    for chunk_index in range(chunk_count):
        _require_available(data, offset, _CHUNK_HEADER.size, "chunk header")
        tag, payload_size = _CHUNK_HEADER.unpack_from(data, offset)
        offset += _CHUNK_HEADER.size
        _require_available(data, offset, payload_size, f"{tag!r} payload")
        payload = data[offset : offset + payload_size]
        offset += payload_size

        padding_size = (-payload_size) & 3
        _require_available(data, offset, padding_size, f"{tag!r} padding")
        padding = data[offset : offset + padding_size]
        if any(padding):
            raise MeshFormatError(
                f"Chunk {chunk_index} ({tag!r}) has non-zero padding"
            )
        offset += padding_size

        if tag == _VERTEX_TAG:
            if vertices is not None:
                raise MeshFormatError("Mesh contains more than one VERT chunk")
            vertices = _parse_vertices(payload)
        elif tag == _TRIANGLE_TAG:
            if triangles is not None:
                raise MeshFormatError("Mesh contains more than one TRIS chunk")
            triangles = _parse_triangles(payload)
        else:
            opaque_chunks.append(OpaqueChunk(tag=tag, payload=payload))

    if offset != len(data):
        raise MeshFormatError(
            f"Mesh has {len(data) - offset} unreferenced trailing bytes"
        )
    if vertices is None:
        raise MeshFormatError("Mesh is missing its VERT chunk")
    if triangles is None:
        raise MeshFormatError("Mesh is missing its TRIS chunk")

    mesh = MeshData(
        vertices=vertices,
        triangles=triangles,
        opaque_chunks=tuple(opaque_chunks),
    )
    validate_mesh(mesh)
    return mesh


def _pack_chunk(tag: bytes, payload: bytes) -> bytes:
    padding_size = (-len(payload)) & 3
    return _CHUNK_HEADER.pack(tag, len(payload)) + payload + bytes(padding_size)


def serialize_mesh(mesh: MeshData) -> bytes:
    validate_mesh(mesh)

    vertex_payload = bytearray(_COUNT.pack(len(mesh.vertices)))
    for vertex in mesh.vertices:
        vertex_payload.extend(
            _VERTEX.pack(
                *vertex.position,
                *vertex.normal,
                *vertex.uv,
                *vertex.color,
            )
        )

    triangle_payload = bytearray(_COUNT.pack(len(mesh.triangles)))
    for triangle in mesh.triangles:
        triangle_payload.extend(
            _TRIANGLE.pack(*triangle.vertices, triangle.material_index)
        )

    output = bytearray(
        _HEADER.pack(
            MAGIC,
            FORMAT_MAJOR,
            FORMAT_MINOR,
            len(mesh.opaque_chunks) + 2,
        )
    )
    output.extend(_pack_chunk(_VERTEX_TAG, bytes(vertex_payload)))
    output.extend(_pack_chunk(_TRIANGLE_TAG, bytes(triangle_payload)))
    for chunk in mesh.opaque_chunks:
        output.extend(_pack_chunk(chunk.tag, chunk.payload))

    if len(output) > MAX_FILE_SIZE:
        raise MeshFormatError(
            f"Serialized mesh size {len(output)} exceeds {MAX_FILE_SIZE} bytes"
        )
    return bytes(output)


def mesh_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
