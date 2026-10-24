# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import struct
import unittest

from io_scene_killzone.core.errors import CoreFormatError
from io_scene_killzone.core.native_skeleton import parse_native_skeleton
from io_scene_killzone.core.native_skinned_geometry import (
    NativeSkinnedTransform,
    decode_skinned_packet,
    parse_regular_skinned_resource,
)


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def _command(
    opcode: int,
    count: int,
    address: int,
    flags: int,
) -> bytes:
    encoded_count = 0 if count == 256 else count
    return struct.pack(
        "<I",
        (opcode << 24) | (encoded_count << 16) | flags | address,
    )


def _make_skinned_packet() -> bytes:
    target_count = 3
    group_count = 1
    pair_count = 3
    aligned_target_count = 4
    v3_start = 0x0B5 + 6 * group_count
    tail_base = max(
        0x0B5 + 2 * aligned_target_count,
        v3_start + 2 * pair_count,
    )
    header_a = (
        target_count,
        3 * target_count,
        3 * aligned_target_count,
        group_count,
    )
    header_b = (
        v3_start,
        0x0B5 + aligned_target_count,
        tail_base,
        tail_base,
    )
    vectors = (
        -127,
        -127,
        -127,
        0,
        0,
        127,
        0,
        -127,
        -127,
        0,
        0,
        127,
        -127,
        0,
        -127,
        0,
        0,
        127,
    )
    controls = (
        0x8000,
        0x8000,
        0x8003,
        0x8003,
        0x0006,
        0x0006,
    )
    coordinates = (0, 0, 4096, 0, 0, 4096)

    body = bytearray(struct.pack("<8I", *(header_a + header_b)))
    body.extend(struct.pack("<3I", 0x20000000, 0x3F3F3F3F, 0x01000106))
    body.extend(_command(0x72, group_count, 0xBA, 0xC000))
    body.extend(bytes((pair_count, 0, 0, 0)))
    body.extend(struct.pack("<I", 0x01000404))
    body.extend(_command(0x6A, 2 * pair_count, v3_start, 0x8000))
    body.extend(struct.pack(f"<{len(vectors)}b", *vectors))
    body.extend(bytes((-len(vectors)) & 3))
    body.extend(_command(0x71, 2 * pair_count, v3_start, 0xC000))
    body.extend(struct.pack(f"<{len(controls)}H", *controls))
    body.extend(_command(0x65, target_count, tail_base, 0x8000))
    body.extend(struct.pack(f"<{len(coordinates)}h", *coordinates))
    body.extend(bytes((-len(body)) & 15))
    qwc = len(body) // 16
    return (
        struct.pack(
            "<4I",
            0x60000000 | qwc,
            0x80000000,
            0x01000404,
            0x6C02C1D9,
        )
        + body
    )


def _make_regular_resource(
    *,
    include_empty_record: bool = False,
) -> tuple[bytes, bytes]:
    packet = _make_skinned_packet()
    packet_offset = 0x40
    stream = bytearray(packet_offset + len(packet))
    struct.pack_into("<I", stream, 0, 0x12345678)
    struct.pack_into("<I", stream, 4, packet_offset)
    stream[8] = 0
    stream[12] = 7
    struct.pack_into(
        "<3fI3fI",
        stream,
        0x10,
        0.0,
        0.0,
        0.0,
        1,
        2.0,
        2.0,
        2.0,
        0,
    )
    stream[0x30] = 0
    stream[packet_offset:] = packet

    records = [(1, 1, 1, 0x5555)]
    if include_empty_record:
        records.append((0xFFF00000, 0, 0, 0x6666))
    blob = bytearray(0x4C + len(records) * 0x10 + 8)
    struct.pack_into("<I", blob, 0x24, 0x4444)
    struct.pack_into("<I", blob, 0x40, 1)
    struct.pack_into("<I", blob, 0x44, len(stream))
    struct.pack_into("<I", blob, 0x48, len(records))
    cursor = 0x4C
    for record in records:
        struct.pack_into("<4I", blob, cursor, *record)
        cursor += 0x10
    struct.pack_into("<2I", blob, cursor, 0x01000001, 0x10)
    return bytes(blob), bytes(stream)


def _make_skeleton_blob() -> bytes:
    names = b"\0Root\0Child\0hull\0attach\0"
    used_name_size = len(names)
    padded_name_size = _align(used_name_size, 4)
    root_offset = names.index(b"Root")
    child_offset = names.index(b"Child")
    hull_offset = names.index(b"hull")
    attachment_name_offset = names.index(b"attach")
    bone_count = 2
    attachment_count = 1

    relative = 0x40
    transform_offset = relative
    relative += bone_count * 0x30
    relative = _align(relative, 0x40)
    # Bone bounds follow the transforms; the fixture leaves them zeroed.
    relative += bone_count * 0x18
    relative = _align(relative, 0x10)
    attachment_offset = relative
    relative += attachment_count * 0x50
    relative = _align(relative, 0x10)
    bone_offset = relative
    relative += bone_count * 0x20
    relative = _align(relative, 4)
    words_a_offset = relative
    relative += bone_count * 4
    relative = _align(relative, 4)
    words_b_offset = relative
    relative += bone_count * 4
    bytes_a_offset = relative
    relative += bone_count
    bytes_b_offset = relative
    relative += bone_count
    parent_offset = relative
    relative += bone_count
    bytes_d_offset = relative
    relative += bone_count
    name_pool_offset = relative
    relative += padded_name_size
    stored_size = relative

    runtime_size = 0x40
    runtime_size = _align(runtime_size, 0x40) + bone_count * 0x18
    runtime_size = _align(runtime_size, 0x10) + attachment_count * 0x50
    runtime_size = _align(runtime_size, 0x10) + bone_count * 0x20
    runtime_size = _align(runtime_size, 4) + bone_count * 4
    runtime_size = _align(runtime_size, 4) + bone_count * 4
    runtime_size += bone_count * 4 + padded_name_size

    block_offset = 0x30
    blob = bytearray(_align(block_offset + stored_size, 0x10))
    struct.pack_into("<I", blob, 0, 1)
    struct.pack_into("<I", blob, 4, 0)
    struct.pack_into("<I", blob, 0x14, 0x7777)
    struct.pack_into("<I", blob, 0x18, 1)
    struct.pack_into(
        "<2H4B",
        blob,
        block_offset,
        stored_size,
        runtime_size,
        1,
        0,
        attachment_count,
        bone_count,
    )
    blob[block_offset + 8 : block_offset + 14] = bytes(
        (0, 1, 1, 1, 2, 0)
    )
    struct.pack_into(
        "<2H",
        blob,
        block_offset + 0x10,
        padded_name_size,
        used_name_size,
    )
    identity_transform = (
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
    )
    child_transform = identity_transform[:9] + (0.0, 0.0, -1.0)
    struct.pack_into(
        "<12f", blob, block_offset + transform_offset, *identity_transform
    )
    struct.pack_into(
        "<12f",
        blob,
        block_offset + transform_offset + 0x30,
        *child_transform,
    )
    struct.pack_into(
        "<16f",
        blob,
        block_offset + attachment_offset,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )
    struct.pack_into(
        "<4I",
        blob,
        block_offset + attachment_offset + 0x40,
        attachment_name_offset,
        1,
        0xDEADBEEF,
        0xDEADC0DE,
    )
    struct.pack_into(
        "<3f5I",
        blob,
        block_offset + bone_offset,
        0.0,
        0.0,
        0.0,
        root_offset,
        0,
        hull_offset,
        0,
        0,
    )
    struct.pack_into(
        "<3f5I",
        blob,
        block_offset + bone_offset + 0x20,
        0.0,
        0.0,
        0.0,
        child_offset,
        root_offset,
        0,
        0x3FF,
        1,
    )
    struct.pack_into("<2I", blob, block_offset + words_a_offset, 10, 20)
    struct.pack_into("<2I", blob, block_offset + words_b_offset, 10, 20)
    blob[block_offset + bytes_a_offset : block_offset + bytes_a_offset + 2] = (
        bytes((0, 0))
    )
    blob[block_offset + bytes_b_offset : block_offset + bytes_b_offset + 2] = (
        bytes((0, 1))
    )
    blob[block_offset + parent_offset : block_offset + parent_offset + 2] = (
        bytes((0xFF, 0))
    )
    blob[block_offset + bytes_d_offset : block_offset + bytes_d_offset + 2] = (
        bytes((0, 1))
    )
    blob[
        block_offset + name_pool_offset :
        block_offset + name_pool_offset + used_name_size
    ] = names
    return bytes(blob)


class NativeSkinnedPacketTests(unittest.TestCase):
    def test_decodes_rigid_weighted_packet(self) -> None:
        packet_data = _make_skinned_packet()
        transform = NativeSkinnedTransform(
            center=(0.0, 0.0, 0.0),
            bone_index=1,
            extent=(2.0, 2.0, 2.0),
        )
        packet = decode_skinned_packet(
            packet_data,
            transforms=(transform,),
            palette_indices=(0,),
        )

        self.assertEqual(len(packet.vertices), 3)
        self.assertEqual(packet.triangles, ((0, 1, 2),))
        self.assertEqual(packet.tail_opcodes, (0x65,))
        self.assertTrue(all(vertex.bone_index == 1 for vertex in packet.vertices))
        self.assertEqual(packet.vertices[0].source_control, 0x8000)
        self.assertIs(packet.vertices[0].adc, True)
        self.assertIs(packet.vertices[2].adc, False)
        self.assertAlmostEqual(
            packet.vertices[0].local_position[0],
            2.0 * (-126.5 / 255.0),
        )
        self.assertEqual(packet.vertices[2].coordinates[0], (0.0, 1.0))

    def test_rejects_invalid_palette_index(self) -> None:
        with self.assertRaisesRegex(CoreFormatError, "invalid transform"):
            decode_skinned_packet(
                _make_skinned_packet(),
                transforms=(
                    NativeSkinnedTransform(
                        center=(0.0, 0.0, 0.0),
                        bone_index=0,
                        extent=(1.0, 1.0, 1.0),
                    ),
                ),
                palette_indices=(1,),
            )

    def test_parses_directory_transform_palette_and_packet(self) -> None:
        blob, stream = _make_regular_resource()
        resource = parse_regular_skinned_resource(
            blob,
            stream,
            object_id=0x3333,
            stream_offset=0,
            stream_size=len(stream),
            skeleton_bone_count=2,
        )

        self.assertEqual(resource.skeleton_id, 0x4444)
        self.assertEqual(resource.triangle_count, 1)
        self.assertEqual(resource.records[0].material_id, 0x5555)
        self.assertEqual(resource.transforms[0].bone_index, 1)
        self.assertEqual(resource.packets[0].packet_key, 0x12345678)
        self.assertEqual(resource.packets[0].metadata_a, 7)

    def test_preserves_empty_record_without_reading_its_directory(self) -> None:
        blob, stream = _make_regular_resource(include_empty_record=True)
        resource = parse_regular_skinned_resource(
            blob,
            stream,
            object_id=0x3333,
            stream_offset=0,
            stream_size=len(stream),
            skeleton_bone_count=2,
        )

        self.assertEqual(len(resource.records), 2)
        self.assertEqual(resource.records[1].directory_offset, 0xFFF0)
        self.assertEqual(resource.records[1].packet_count, 0)
        self.assertEqual(len(resource.packets), 1)

    def test_rejects_incomplete_packet_dma_tag(self) -> None:
        blob, stream = _make_regular_resource()
        blob_data = bytearray(blob)
        stream_data = bytearray(stream)
        packet_offset = len(stream_data)
        stream_data.extend(b"\0" * 8)
        struct.pack_into("<I", blob_data, 0x44, len(stream_data))
        struct.pack_into("<I", stream_data, 4, packet_offset)

        with self.assertRaisesRegex(CoreFormatError, "complete DMA tag"):
            parse_regular_skinned_resource(
                bytes(blob_data),
                bytes(stream_data),
                object_id=0x3333,
                stream_offset=0,
                stream_size=len(stream_data),
                skeleton_bone_count=2,
            )

    def test_rejects_packet_dma_extent_outside_chunk(self) -> None:
        blob, stream = _make_regular_resource()
        stream_data = bytearray(stream)
        struct.pack_into("<I", stream_data, 0x40, 0x6000FFFF)

        with self.assertRaisesRegex(CoreFormatError, "DMA transfer exceeds"):
            parse_regular_skinned_resource(
                blob,
                bytes(stream_data),
                object_id=0x3333,
                stream_offset=0,
                stream_size=len(stream_data),
                skeleton_bone_count=2,
            )


class NativeSkeletonTests(unittest.TestCase):
    def test_parses_hierarchy_transforms_hulls_and_attachments(self) -> None:
        skeleton = parse_native_skeleton(
            _make_skeleton_blob(),
            object_id=0x1111,
            core_string_count=2,
        )

        self.assertEqual(skeleton.bone_count, 2)
        self.assertEqual(skeleton.bones[0].name, "Root")
        self.assertEqual(skeleton.bones[1].parent_index, 0)
        self.assertEqual(skeleton.bones[0].hull_slot, 0)
        self.assertEqual(skeleton.bones[1].temporary_transform[11], -1.0)
        self.assertEqual(skeleton.attachments[0].name, "attach")
        self.assertEqual(skeleton.attachments[0].owner_bone_index, 1)

    def test_rejects_non_acyclic_parent_index(self) -> None:
        blob = bytearray(_make_skeleton_blob())
        block_offset = 0x30
        bone_count = 2
        relative = 0x40 + bone_count * 0x30
        relative = _align(relative, 0x40) + bone_count * 0x18
        relative = _align(relative, 0x10) + 0x50
        relative = _align(relative, 0x10) + bone_count * 0x20
        relative = _align(relative, 4) + bone_count * 4
        relative = _align(relative, 4) + bone_count * 4
        parent_offset = relative + bone_count * 2
        blob[block_offset + parent_offset + 1] = 1

        with self.assertRaisesRegex(CoreFormatError, "acyclic"):
            parse_native_skeleton(bytes(blob), object_id=0x1111)


if __name__ == "__main__":
    unittest.main()
