# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
import struct
import unittest

from io_scene_killzone.core.core_format import core_header_prefix_size
from io_scene_killzone.core.errors import CoreFormatError
from io_scene_killzone.core.native_texture import (
    parse_native_texture,
    unswizzle_psmt4,
    unswizzle_psmt8,
)
from tests.support import create_fixture_native_texture_core


class NativeTextureTests(unittest.TestCase):
    def test_decodes_supported_core_texture_losslessly(self) -> None:
        (
            data,
            expected_indices,
            logical_palette,
            auxiliary_payload,
            opaque_footer,
        ) = create_fixture_native_texture_core()

        texture = parse_native_texture(data)

        self.assertEqual(texture.name, "fixture_texture")
        self.assertEqual((texture.width, texture.height), (512, 512))
        self.assertEqual(texture.linear_indices, expected_indices)
        self.assertEqual(texture.auxiliary_payload, auxiliary_payload)
        self.assertEqual(texture.opaque_footer, opaque_footer)
        self.assertEqual(texture.original_core, data)
        self.assertEqual(
            texture.source_sha256,
            hashlib.sha256(data).hexdigest(),
        )
        for x, y in ((0, 0), (255, 127), (511, 511)):
            pixel = y * texture.width + x
            palette_index = expected_indices[pixel]
            self.assertEqual(
                texture.rgba[pixel * 4 : pixel * 4 + 4],
                logical_palette[
                    palette_index * 4 : palette_index * 4 + 4
                ],
            )

    def test_rejects_unproven_chunk_layout(self) -> None:
        data = bytearray(create_fixture_native_texture_core()[0])
        body_offset = core_header_prefix_size(data[:24], len(data))
        struct.pack_into("<I", data, body_offset + 0xE8, 0x7FFF)

        with self.assertRaisesRegex(
            CoreFormatError, "chunk-size layout"
        ):
            parse_native_texture(bytes(data))

    def test_rejects_unproven_property_schema(self) -> None:
        data = bytearray(create_fixture_native_texture_core()[0])
        marker = b"General.No streaming"
        offset = data.index(marker)
        data[offset] = ord("X")

        with self.assertRaisesRegex(CoreFormatError, "property schema"):
            parse_native_texture(bytes(data))

    def test_rejects_invalid_psmt8_bounds(self) -> None:
        with self.assertRaisesRegex(CoreFormatError, "multiple of 16"):
            unswizzle_psmt8(bytes(17 * 16), 17, 16)
        self.assertEqual(
            len(unswizzle_psmt8(bytes(16 * 8), 16, 8)),
            16 * 8,
        )
        with self.assertRaisesRegex(CoreFormatError, "payload size"):
            unswizzle_psmt8(bytes(16 * 16 - 1), 16, 16)

    def test_decodes_native_small_psmt4_low_nibble_first(self) -> None:
        packed = bytes(
            ((index * 2 + 1) & 0x0F)
            | (((index * 2 + 2) & 0x0F) << 4)
            for index in range(32)
        )
        expected = bytes(
            value
            for index in range(32)
            for value in (
                (index * 2 + 1) & 0x0F,
                (index * 2 + 2) & 0x0F,
            )
        )
        self.assertEqual(unswizzle_psmt4(packed, 8, 8), expected)


if __name__ == "__main__":
    unittest.main()
