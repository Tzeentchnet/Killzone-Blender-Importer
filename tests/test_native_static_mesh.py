# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
import struct
import unittest

from io_scene_killzone.core.core_format import core_header_prefix_size
from io_scene_killzone.core.errors import CoreFormatError
from io_scene_killzone.core.native_static_mesh import (
    parse_native_static_mesh,
)
from tests.support import create_fixture_native_static_mesh_core_stream


class NativeStaticMeshTests(unittest.TestCase):
    def test_scans_bounded_opaque_packets(self) -> None:
        core, corestream = create_fixture_native_static_mesh_core_stream()

        mesh = parse_native_static_mesh(core, corestream)

        self.assertEqual(mesh.stream_chunk_sizes, (0x100, 0x80))
        self.assertEqual(
            tuple(record.packet_count for record in mesh.records),
            (2, 1),
        )
        self.assertEqual(mesh.root_flags, (True, False))
        self.assertEqual(mesh.opaque_root_values, bytes(range(0x44)))
        self.assertEqual(mesh.opaque_block_40, bytes(range(0x40)))
        self.assertEqual(mesh.opaque_block_80, bytes(range(0x80, 0x90)))
        self.assertEqual(mesh.table_end_offset, 0x75)
        self.assertEqual(mesh.static_stream_end_offset, 0x1C0)
        self.assertEqual(mesh.corestream_trailing_size, 0x20)
        self.assertEqual(
            tuple(
                (
                    packet.mesh_record_index,
                    packet.packet_index,
                    packet.stream_chunk_index,
                    packet.relative_offset,
                    packet.absolute_offset,
                    packet.bounded_size,
                    packet.first_word,
                )
                for packet in mesh.packets
            ),
            (
                (0, 0, 0, 0x50, 0x90, 0x40, 0x60000001),
                (0, 1, 0, 0x90, 0xD0, 0x70, 0x60000002),
                (1, 0, 1, 0x20, 0x160, 0x60, 0x60000003),
            ),
        )
        self.assertEqual(mesh.packets[0].opaque_metadata, bytes(range(0x10, 0x1C)))
        self.assertEqual(
            mesh.source_core_sha256,
            hashlib.sha256(core).hexdigest(),
        )
        self.assertEqual(
            mesh.source_corestream_sha256,
            hashlib.sha256(corestream).hexdigest(),
        )

    def test_rejects_nonzero_corestream_reserved_header(self) -> None:
        core, corestream = create_fixture_native_static_mesh_core_stream()
        damaged = bytearray(corestream)
        damaged[0x20] = 1

        with self.assertRaisesRegex(CoreFormatError, "reserved header"):
            parse_native_static_mesh(core, bytes(damaged))

    def test_rejects_packet_outside_declared_stream_chunk(self) -> None:
        core, corestream = create_fixture_native_static_mesh_core_stream()
        damaged = bytearray(corestream)
        struct.pack_into("<I", damaged, 0x40, 0x100)

        with self.assertRaisesRegex(CoreFormatError, "exceeds its stream chunk"):
            parse_native_static_mesh(core, bytes(damaged))

    def test_rejects_packet_table_overlap(self) -> None:
        core, corestream = create_fixture_native_static_mesh_core_stream()
        damaged = bytearray(corestream)
        struct.pack_into("<I", damaged, 0x40, 0x20)

        with self.assertRaisesRegex(CoreFormatError, "overlaps its packet table"):
            parse_native_static_mesh(core, bytes(damaged))

    def test_rejects_unproven_root_schema(self) -> None:
        core, corestream = create_fixture_native_static_mesh_core_stream()
        damaged = bytearray(core)
        body_offset = core_header_prefix_size(damaged[:24], len(damaged))
        struct.pack_into("<I", damaged, body_offset + 0x24, 0xDEADBEEF)

        with self.assertRaisesRegex(
            CoreFormatError, "property descriptor"
        ):
            parse_native_static_mesh(bytes(damaged), corestream)


if __name__ == "__main__":
    unittest.main()
