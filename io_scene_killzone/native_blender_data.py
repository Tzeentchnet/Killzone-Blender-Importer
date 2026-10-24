# SPDX-License-Identifier: GPL-3.0-or-later

"""Bulk Blender datablock builders shared by the native importers.

Mesh and image data are written with ``foreach_set`` and NumPy arrays
instead of per-element RNA assignment, which dominated import time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
import struct
import zlib

import bpy
import numpy as np

# zlib level 6 keeps packed .blend files close to Blender's own PNG size
# while staying far cheaper than Image.pack() re-encoding float pixels.
_PNG_COMPRESSION_LEVEL = 6
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_rgba_png(rgba: bytes, width: int, height: int) -> bytes:
    """Encode top-row-first 8-bit RGBA as a non-interlaced PNG."""

    stride = width * 4
    if width <= 0 or height <= 0 or len(rgba) != stride * height:
        raise ValueError("RGBA payload does not match its dimensions")
    scanlines = b"".join(
        b"\x00" + rgba[row * stride : (row + 1) * stride]
        for row in range(height)
    )
    return (
        _PNG_SIGNATURE
        + _png_chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0),
        )
        + _png_chunk(
            b"IDAT",
            zlib.compress(scanlines, _PNG_COMPRESSION_LEVEL),
        )
        + _png_chunk(b"IEND", b"")
    )


def create_packed_rgba_image(
    name: str,
    rgba: bytes,
    width: int,
    height: int,
    *,
    colorspace: str,
) -> bpy.types.Image:
    """Create an Image whose pixels live in a packed lossless PNG.

    Packing pre-encoded PNG bytes avoids both the float pixel upload and
    Blender re-encoding the generated buffer in ``Image.pack()``.
    """

    png = encode_rgba_png(rgba, width, height)
    image = bpy.data.images.new(
        name=name,
        width=width,
        height=height,
        alpha=True,
        float_buffer=False,
    )
    image.source = "FILE"
    image.filepath_raw = (
        f"//killzone_textures/{_UNSAFE_FILENAME.sub('_', image.name)}.png"
    )
    image.file_format = "PNG"
    image.pack(data=png, data_len=len(png))
    image.alpha_mode = "STRAIGHT"
    image.colorspace_settings.name = colorspace
    return image


def flip_inconsistent_windings(
    positions: np.ndarray,
    normals: np.ndarray,
    triangles: np.ndarray,
) -> int:
    """Flip triangles whose face normal opposes their vertex normals.

    ``triangles`` is modified in place; the number of flips is returned.
    """

    if not len(triangles):
        return 0
    a = positions[triangles[:, 0]]
    b = positions[triangles[:, 1]]
    c = positions[triangles[:, 2]]
    cross = np.cross(b - a, c - a)
    average = (
        normals[triangles[:, 0]]
        + normals[triangles[:, 1]]
        + normals[triangles[:, 2]]
    )
    flip = (
        (np.einsum("ij,ij->i", cross, cross) > 0.0)
        & (np.einsum("ij,ij->i", average, average) > 0.0)
        & (np.einsum("ij,ij->i", cross, average) < 0.0)
    )
    triangles[flip] = triangles[flip][:, [0, 2, 1]]
    return int(np.count_nonzero(flip))


def set_attribute_values(
    mesh: bpy.types.Mesh,
    name: str,
    attribute_type: str,
    domain: str,
    values: Sequence[object] | np.ndarray,
) -> bpy.types.Attribute:
    attribute = mesh.attributes.new(
        name=name,
        type=attribute_type,
        domain=domain,
    )
    if attribute_type == "BOOLEAN":
        array = np.asarray(values, dtype=bool)
    elif attribute_type == "INT":
        array = np.asarray(values, dtype=np.int32)
    else:
        array = np.asarray(values, dtype=np.float32)
    attribute.data.foreach_set("value", array.ravel())
    return attribute


def build_native_mesh(
    name: str,
    *,
    positions: Sequence[Sequence[float]],
    normals: Sequence[Sequence[float]],
    triangles: Sequence[Sequence[int]],
    material_ids: Sequence[int],
    materials_by_token: Mapping[int, bpy.types.Material],
    fallback_material: bpy.types.Material,
    coordinates: Sequence[Sequence[tuple[float, float]]],
    colors: Sequence[tuple[int, int, int, int] | None],
) -> tuple[bpy.types.Mesh, int, int]:
    """Build a triangle mesh with native UV, color, and material data.

    Returns the mesh, the number of winding corrections, and the number of
    UV layers created.
    """

    position_array = np.asarray(positions, dtype=np.float32).reshape(-1, 3)
    normal_array = np.asarray(normals, dtype=np.float32).reshape(-1, 3)
    triangle_array = np.asarray(triangles, dtype=np.int32).reshape(-1, 3)
    winding_corrections = flip_inconsistent_windings(
        position_array,
        normal_array,
        triangle_array,
    )

    vertex_count = len(position_array)
    triangle_count = len(triangle_array)
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(vertex_count)
    mesh.vertices.foreach_set("co", position_array.ravel())
    mesh.loops.add(triangle_count * 3)
    mesh.loops.foreach_set("vertex_index", triangle_array.ravel())
    mesh.polygons.add(triangle_count)
    mesh.polygons.foreach_set(
        "loop_start",
        np.arange(0, triangle_count * 3, 3, dtype=np.int32),
    )
    mesh.update(calc_edges=True)

    material_slots: dict[int, int] = {}
    for material_id in material_ids:
        if material_id in material_slots:
            continue
        material_slots[material_id] = len(mesh.materials)
        mesh.materials.append(
            materials_by_token.get(material_id, fallback_material)
        )
    if triangle_count:
        mesh.polygons.foreach_set(
            "material_index",
            np.fromiter(
                (material_slots[value] for value in material_ids),
                dtype=np.int32,
                count=triangle_count,
            ),
        )

    mesh.normals_split_custom_set_from_vertices(normal_array)

    uv_count = max((len(value) for value in coordinates), default=0)
    if uv_count:
        # Missing lanes keep Blender (0, 0), matching the previous writer.
        uv_arrays = np.zeros((uv_count, vertex_count, 2), dtype=np.float32)
        for vertex_index, vertex_coordinates in enumerate(coordinates):
            for uv_index, (u, v) in enumerate(vertex_coordinates):
                uv_arrays[uv_index, vertex_index] = (u, 1.0 - v)
        loop_vertices = triangle_array.ravel()
        for uv_index in range(uv_count):
            uv_layer = mesh.uv_layers.new(name=f"Native UV{uv_index}")
            uv_layer.uv.foreach_set(
                "vector",
                uv_arrays[uv_index][loop_vertices].ravel(),
            )

    if any(color is not None for color in colors):
        color_attribute = mesh.color_attributes.new(
            name="Native V4-8",
            type="BYTE_COLOR",
            domain="POINT",
        )
        color_array = np.asarray(
            [
                color if color is not None else (128, 128, 128, 128)
                for color in colors
            ],
            dtype=np.float32,
        )
        color_attribute.data.foreach_set(
            "color",
            (color_array * (1.0 / 255.0)).ravel(),
        )

    set_attribute_values(
        mesh,
        "killzone_material_id",
        "INT",
        "FACE",
        material_ids,
    )
    return mesh, winding_corrections, uv_count
