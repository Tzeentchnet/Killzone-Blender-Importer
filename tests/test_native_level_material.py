# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import struct
import unittest

from io_scene_killzone.core.errors import CoreFormatError
from io_scene_killzone.core.native_level import (
    NativeCoreObject,
    NativeObjectKey,
)
from io_scene_killzone.core.native_level_material import (
    build_level_material_plan,
    parse_animated_textures,
    parse_level_textures,
    parse_selected_materials,
)


def _object(
    *,
    key_index: int,
    type_name: str,
    object_id: int,
    blob: bytes,
    stream_offset: int = 0,
    stream_size: int = 0,
) -> NativeCoreObject:
    return NativeCoreObject(
        key=NativeObjectKey(0x100 + key_index * 0x10, key_index),
        type_name=type_name,
        object_id=object_id,
        blob_offset=0,
        blob=blob,
        identity_words=(1, 2, 3, 4),
        stream_offset=stream_offset,
        stream_size=stream_size,
    )


def _index8_texture(
    *,
    key_index: int,
    object_id: int,
    stream_offset: int,
) -> tuple[NativeCoreObject, bytes]:
    width = 16
    height = 8
    pixel_storage = bytes(0x100)
    palette_entries = bytes(
        component
        for index in range(256)
        for component in (index, index, index, 255)
    )
    palette = bytes(0x10) + palette_entries + bytes(0xF0)
    payload = pixel_storage + palette
    blob = bytearray(0x60)
    struct.pack_into("<2I", blob, 0, 0, 0)
    struct.pack_into(
        "<6I",
        blob,
        0x20,
        width,
        height,
        1,
        0,
        1,
        2,
    )
    struct.pack_into("<2I", blob, 0x38, 0x100, 0x500)
    struct.pack_into("<I", blob, 0x40, 1)
    struct.pack_into("<2I", blob, 0x44, 0, 2)
    struct.pack_into("<BBHH", blob, 0x4C, 0, 0, 0, 8)
    struct.pack_into("<BBHH", blob, 0x52, 0, 0, 0, 8)
    return (
        _object(
            key_index=key_index,
            type_name="Texture",
            object_id=object_id,
            blob=bytes(blob),
            stream_offset=stream_offset,
            stream_size=len(payload),
        ),
        payload,
    )


def _layer(
    resource_token: int,
    *,
    flags: int = 0,
    operation: int = 0,
    selector: int = 0,
) -> bytes:
    data = bytearray(0x40)
    struct.pack_into("<I", data, 0, resource_token)
    data[4] = flags
    struct.pack_into("<I", data, 8, 0x74)
    data[0x0C] = operation
    data[0x1A] = 255
    data[0x31] = selector
    return bytes(data)


class NativeLevelMaterialTests(unittest.TestCase):
    def test_parses_index8_and_collapses_equivalent_definitions(self) -> None:
        first, payload = _index8_texture(
            key_index=0,
            object_id=0x1234,
            stream_offset=0,
        )
        second, _ = _index8_texture(
            key_index=1,
            object_id=0x1234,
            stream_offset=len(payload),
        )

        physical, logical = parse_level_textures(
            (first, second),
            payload + payload,
        )

        self.assertEqual(len(physical), 2)
        self.assertEqual(len(logical), 1)
        self.assertEqual(len(logical[0].definition_keys), 2)
        self.assertEqual(logical[0].decode_status, "decoded-index8-csm1")
        self.assertEqual(
            len(logical[0].base_rgba or b""),
            logical[0].width * logical[0].height * 4,
        )

    def test_rejects_non_equivalent_duplicate_texture_tokens(self) -> None:
        first, payload = _index8_texture(
            key_index=0,
            object_id=0x1234,
            stream_offset=0,
        )
        second, _ = _index8_texture(
            key_index=1,
            object_id=0x1234,
            stream_offset=len(payload),
        )
        changed = bytearray(payload)
        changed[0] = 1

        with self.assertRaisesRegex(
            CoreFormatError,
            "non-equivalent duplicate",
        ):
            parse_level_textures(
                (first, second),
                payload + bytes(changed),
            )

    def test_resolves_variable_material_layers(self) -> None:
        texture_object, payload = _index8_texture(
            key_index=0,
            object_id=0x1000,
            stream_offset=0,
        )
        _, textures = parse_level_textures(
            (texture_object,),
            payload,
        )

        animation_blob = bytearray(0x30)
        struct.pack_into("<I", animation_blob, 0, 1)
        struct.pack_into("<If", animation_blob, 0x20, 0x1000, 0.25)
        animation_object = _object(
            key_index=1,
            type_name="AnimatedTexture",
            object_id=0x2000,
            blob=bytes(animation_blob),
        )
        animations = parse_animated_textures(
            (animation_object,),
            textures,
        )

        transformed_layer = _layer(0x1000, flags=0x20, selector=11)
        matrix = struct.pack(
            "<16f",
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
        transform = struct.pack("<6f", 1.0, 0.0, 0.0, 1.0, 0.5, 0.25)
        material_blob = (
            struct.pack("<I", 4)
            + bytes(0x14)
            + transformed_layer
            + matrix
            + transform
            + _layer(0x2000)
            + _layer(0)
            + _layer(
                0x1000,
                flags=0x40,
                operation=5,
                selector=1,
            )
            + struct.pack("<2I", 0x12345678, 0)
        )
        material_object = _object(
            key_index=2,
            type_name="Material",
            object_id=0x3000,
            blob=material_blob,
        )
        materials = parse_selected_materials(
            (material_object,),
            (0x3000,),
            textures,
            animations,
        )

        self.assertEqual(
            tuple(layer.kind for layer in materials[0].layers),
            (
                "texture",
                "animated_texture",
                "null_texture",
                "texture",
            ),
        )
        self.assertEqual(
            materials[0].layers[0].transform_6f,
            (1.0, 0.0, 0.0, 1.0, 0.5, 0.25),
        )
        self.assertEqual(materials[0].layers[0].matrix_4x4[0], 1.0)
        self.assertEqual(materials[0].layers[3].operation, 5)
        self.assertEqual(materials[0].layers[3].coordinate_selector, 1)
        self.assertTrue(materials[0].layers[3].uses_default_transform)
        plan = build_level_material_plan(
            (0x3000,),
            (),
            materials,
            animations,
        )
        self.assertEqual(plan.direct_texture_tokens, (0x1000,))
        self.assertEqual(plan.instance_texture_tokens, ())


if __name__ == "__main__":
    unittest.main()
