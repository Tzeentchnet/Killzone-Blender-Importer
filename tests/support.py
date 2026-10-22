# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
from pathlib import Path
import struct

from io_scene_killzone.core.bundle import make_manifest, write_manifest
from io_scene_killzone.core.mesh_format import (
    MeshData,
    OpaqueChunk,
    Triangle,
    Vertex,
    serialize_mesh,
)


def create_fixture_bundle(root: Path) -> tuple[Path, bytes, bytes]:
    vertices = (
        Vertex(
            position=(-1.0, -1.0, 0.0),
            normal=(0.0, 0.0, 1.0),
            uv=(0.0, 0.0),
            color=(255, 0, 0, 255),
        ),
        Vertex(
            position=(1.0, -1.0, 0.0),
            normal=(0.0, 0.0, 1.0),
            uv=(1.0, 0.0),
            color=(0, 255, 0, 255),
        ),
        Vertex(
            position=(1.0, 1.0, 0.0),
            normal=(0.0, 0.0, 1.0),
            uv=(1.0, 1.0),
            color=(0, 0, 255, 255),
        ),
        Vertex(
            position=(-1.0, 1.0, 0.0),
            normal=(0.0, 0.0, 1.0),
            uv=(0.0, 1.0),
            color=(255, 255, 255, 255),
        ),
    )
    mesh = MeshData(
        vertices=vertices,
        triangles=(
            Triangle(vertices=(0, 1, 2), material_index=0),
            Triangle(vertices=(0, 2, 3), material_index=0),
        ),
        opaque_chunks=(
            OpaqueChunk(tag=b"TEST", payload=b"preserve-this"),
        ),
    )
    mesh_bytes = serialize_mesh(mesh)
    source_bytes = b"SYNTHETIC-MSS-FIXTURE\x00\x01\x02"

    mesh_path = root / "meshes" / "sample.kzm"
    source_path = root / "original" / "sample.mss"
    mesh_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    mesh_path.write_bytes(mesh_bytes)
    source_path.write_bytes(source_bytes)

    manifest = make_manifest(
        [
            {
                "id": "sample",
                "name": "Synthetic Sample",
                "mesh": {
                    "path": "meshes/sample.kzm",
                    "sha256": hashlib.sha256(mesh_bytes).hexdigest(),
                },
                "materials": [
                    {
                        "name": "Synthetic Material",
                        "properties": {
                            "base_color": [0.2, 0.4, 0.8, 1.0],
                            "ps2_state": {"alpha_test": False},
                        },
                    }
                ],
                "source": {
                    "path": "original/sample.mss",
                    "sha256": hashlib.sha256(source_bytes).hexdigest(),
                    "format": "synthetic-mss",
                },
            }
        ],
        generator="test-fixture",
    )
    manifest_path = root / "manifest.json"
    write_manifest(manifest_path, manifest)
    return manifest_path, mesh_bytes, source_bytes


def _both_endian_u16(value: int) -> bytes:
    return struct.pack("<H", value) + struct.pack(">H", value)


def _both_endian_u32(value: int) -> bytes:
    return struct.pack("<I", value) + struct.pack(">I", value)


def _iso_record(
    identifier: bytes,
    extent_lba: int,
    size: int,
    *,
    directory: bool,
) -> bytes:
    padding = 1 if len(identifier) % 2 == 0 else 0
    record = bytearray(33 + len(identifier) + padding)
    record[0] = len(record)
    record[1] = 0
    record[2:10] = _both_endian_u32(extent_lba)
    record[10:18] = _both_endian_u32(size)
    record[18:25] = bytes((126, 10, 17, 23, 0, 0, 0))
    record[25] = 0x02 if directory else 0
    record[26] = 0
    record[27] = 0
    record[28:32] = _both_endian_u16(1)
    record[32] = len(identifier)
    record[33 : 33 + len(identifier)] = identifier
    return bytes(record)


def create_fixture_iso(path: Path) -> dict[str, bytes]:
    sector_size = 2_048
    volume_blocks = 40
    root_lba = 20
    root_size = sector_size
    archive_names = (
        "FILES.DAT",
        "FILES01.DAT",
        "FILES02.DAT",
        "FILES03.DAT",
        "FILES04.DAT",
    )
    contents = {
        name: f"SYNTHETIC-{name}-".encode("ascii") + bytes(range(index + 1))
        for index, name in enumerate(archive_names)
    }

    image = bytearray(volume_blocks * sector_size)
    primary = memoryview(image)[16 * sector_size : 17 * sector_size]
    primary[0] = 1
    primary[1:6] = b"CD001"
    primary[6] = 1
    primary[8:40] = b"PYTHON".ljust(32, b" ")
    primary[40:72] = b"KZ_TEST_DISC".ljust(32, b" ")
    primary[80:88] = _both_endian_u32(volume_blocks)
    primary[120:124] = _both_endian_u16(1)
    primary[124:128] = _both_endian_u16(1)
    primary[128:132] = _both_endian_u16(sector_size)
    primary[156:190] = _iso_record(
        b"\x00", root_lba, root_size, directory=True
    )

    terminator = memoryview(image)[17 * sector_size : 18 * sector_size]
    terminator[0] = 255
    terminator[1:6] = b"CD001"
    terminator[6] = 1

    root_records = [
        _iso_record(b"\x00", root_lba, root_size, directory=True),
        _iso_record(b"\x01", root_lba, root_size, directory=True),
    ]
    for index, name in enumerate(archive_names):
        content = contents[name]
        extent_lba = 21 + index
        root_records.append(
            _iso_record(
                f"{name};1".encode("ascii"),
                extent_lba,
                len(content),
                directory=False,
            )
        )
        start = extent_lba * sector_size
        image[start : start + len(content)] = content

    root_data = b"".join(root_records)
    image[root_lba * sector_size : root_lba * sector_size + len(root_data)] = (
        root_data
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image)
    return contents


def create_fixture_catalog() -> tuple[bytes, dict[int, int]]:
    names = b"root.txt\x00models\x00mesh.mss\x00"
    root_name_offset = 0
    models_name_offset = len(b"root.txt\x00")
    mesh_name_offset = models_name_offset + len(b"models\x00")

    table = bytearray(struct.pack("<I", 2))
    table.extend(struct.pack("<III", root_name_offset, 100, 10))
    child_table_offset = 4 + 2 * 12
    table.extend(
        struct.pack(
            "<III",
            0x80000000 | models_name_offset,
            child_table_offset,
            1,
        )
    )
    table.extend(
        struct.pack(
            "<III",
            (3 << 23) | mesh_name_offset,
            200,
            30,
        )
    )

    metadata_size = 4 + len(names) + 4 + len(table)
    metadata = (
        struct.pack(
            "<6I",
            metadata_size,
            2,
            0x11223344,
            0x55667788,
            0,
            0,
        )
        + struct.pack("<I", len(names))
        + names
        + struct.pack("<I", len(table))
        + bytes(table)
    )
    return metadata, {0: 1_000, 1: 1, 2: 1, 3: 2_000, 4: 1}


def create_fixture_core(
    strings: tuple[str, ...] = ("RegularStaticMeshResource", "General.Name"),
    *,
    variant: int = 5,
    trailing_padding: int = 8,
    body: bytes = b"OPAQUE-CORE-BODY",
) -> bytes:
    table = bytearray(struct.pack("<I", len(strings)))
    for value in strings:
        encoded = value.encode("utf-8")
        table.extend(struct.pack("<I", len(encoded)))
        table.extend(encoded)
        table.extend(bytes((-len(encoded)) & 3))
    table.extend(bytes(trailing_padding))
    return (
        b"Core<1.79><PSX2>"
        + struct.pack("<II", variant, len(table))
        + bytes(table)
        + body
    )


def create_fixture_native_texture_core(
) -> tuple[bytes, bytes, bytes, bytes, bytes]:
    width = 512
    height = 512
    linear_indices = bytes(
        ((x // 32) + (y // 32) * 16) & 0xFF
        for y in range(height)
        for x in range(width)
    )
    swizzled_indices = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            block_location = (y & ~0x0F) * width + (x & ~0x0F) * 2
            swap_selector = (((y + 2) >> 2) & 1) * 4
            position_y = (((y & ~3) >> 1) + (y & 1)) & 7
            column_location = (
                position_y * width * 2
                + ((x + swap_selector) & 7) * 4
            )
            byte_number = ((y >> 1) & 1) + ((x >> 2) & 2)
            target = block_location + column_location + byte_number
            swizzled_indices[target] = linear_indices[y * width + x]

    logical_palette = bytearray()
    for index in range(256):
        logical_palette.extend(
            (index, 255 - index, (index * 3) & 0xFF, index)
        )
    raw_palette = bytearray(256 * 4)
    for index in range(256):
        target = (
            (index & 0xE7)
            | ((index & 0x08) << 1)
            | ((index & 0x10) >> 1)
        )
        raw_palette[target * 4 : target * 4 + 4] = logical_palette[
            index * 4 : index * 4 + 4
        ]

    chunk_sizes = (0x8000,) * 8 + (0x500,)
    payload_offset = 0xE8 + len(chunk_sizes) * 4
    auxiliary_payload = bytes(range(256))
    opaque_footer = b"OPAQUE-FOOTER" + bytes(120 - len(b"OPAQUE-FOOTER"))
    body = bytearray(payload_offset + sum(chunk_sizes) + len(opaque_footer))
    struct.pack_into("<I", body, 0x00, 0xAC)
    struct.pack_into("<I", body, 0x80, 0x18)
    struct.pack_into("<6I", body, 0xD0, width, height, 1, 0, 1, 9)
    struct.pack_into("<9I", body, 0xE8, *chunk_sizes)
    cursor = payload_offset
    body[cursor : cursor + len(swizzled_indices)] = swizzled_indices
    cursor += len(swizzled_indices)
    body[cursor : cursor + len(raw_palette)] = raw_palette
    cursor += len(raw_palette)
    body[cursor : cursor + len(auxiliary_payload)] = auxiliary_payload
    cursor += len(auxiliary_payload)
    body[cursor:] = opaque_footer

    strings = (
        "Texture",
        "General.No streaming",
        "General.Linear storage",
        "General.Parent",
        "General.Name",
        "General.Name is identifier",
        "General.PS2Mip1Distance",
        "General.PS2MipMapL",
        "fixture_texture",
    )
    core = create_fixture_core(
        strings,
        variant=1,
        trailing_padding=0,
        body=bytes(body),
    )
    return (
        core,
        linear_indices,
        bytes(logical_palette),
        auxiliary_payload,
        opaque_footer,
    )


def create_fixture_native_static_mesh_core_stream() -> tuple[bytes, bytes]:
    strings = (
        "RegularStaticMeshResource",
        "Sorting.Sort Order",
        "Lighting.Receives Dynamic Shadows",
        "Lighting.Cast Static Shadows",
        "General.Linear storage",
        "Lighting.Prelit",
        "Sorting.Sort Mode",
        "General.Name is identifier",
        "General.Parent",
        "Lighting.Cast Dynamic Shadows",
        "General.Name",
        "General.No streaming",
        "Lighting.Lightmap Density Factor",
        "Visibility.Maximum visible distance",
        "Mesh Description.Bounding Box",
    )
    properties = (
        (1, 0x5352544F, 0x10000004, 0xFFFFFFFF),
        (2, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
        (3, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
        (4, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
        (5, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
        (6, 0x5352544D, 0x10000004, 0xFFFFFFFF),
        (7, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
        (8, 0x43CF5245, 0x40000004, 0xFFFFFFFF),
        (9, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
        (10, 0x53545220, 0xFFFFFFFF, 0xFFFFFFFF),
        (11, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
        (12, 0x464C4F41, 0x40000004, 0x1C),
        (13, 0x464C4F41, 0x40000004, 0x20),
        (14, 0x42424F58, 0x40000018, 0x28),
    )

    body = bytearray(0x220)
    struct.pack_into("<5I", body, 0, 0x11C, 0, 1, 0, len(properties))
    for index, descriptor in enumerate(properties):
        struct.pack_into("<4I", body, 0x14 + index * 16, *descriptor)
    body[0x11C:0x160] = bytes(range(0x44))

    cursor = 0x160
    struct.pack_into("<3I", body, cursor, 2, 0x100, 0x80)
    cursor += 12
    struct.pack_into("<I", body, cursor, 2)
    cursor += 4
    struct.pack_into(
        "<IIHBB6H",
        body,
        cursor,
        0x12345678,
        0x20,
        2,
        1,
        0,
        1,
        2,
        3,
        4,
        5,
        6,
    )
    cursor += 24
    struct.pack_into(
        "<IIHBB6H",
        body,
        cursor,
        0x87654321,
        0x40,
        1,
        1,
        16,
        7,
        8,
        9,
        10,
        11,
        12,
    )
    cursor += 24
    body[cursor : cursor + 2] = b"\x01\x00"
    cursor = (cursor + 2 + 3) & ~3
    body[cursor : cursor + 0x40] = bytes(range(0x40))
    cursor += 0x40
    body[cursor : cursor + 0x10] = bytes(range(0x80, 0x90))

    core = create_fixture_core(
        strings,
        variant=5,
        trailing_padding=8,
        body=bytes(body),
    )

    stream = bytearray(0x1E0)
    stream[:16] = b"<CoreStreamData>"
    struct.pack_into("<2I", stream, 0x40, 0x50, 0x90)
    stream[0x48:0x60] = bytes(range(0x10, 0x28))
    stream[0x60:0x62] = b"\x00\x00"
    struct.pack_into("<I", stream, 0x64, 0x20)
    stream[0x68:0x74] = bytes(range(0x30, 0x3C))
    stream[0x74] = 1

    struct.pack_into("<I", stream, 0x90, 0x60000001)
    stream[0x94:0xD0] = b"A" * (0xD0 - 0x94)
    struct.pack_into("<I", stream, 0xD0, 0x60000002)
    stream[0xD4:0x140] = b"B" * (0x140 - 0xD4)
    struct.pack_into("<I", stream, 0x160, 0x60000003)
    stream[0x164:0x1C0] = b"C" * (0x1C0 - 0x164)
    stream[0x1C0:] = b"T" * (len(stream) - 0x1C0)
    return core, bytes(stream)
