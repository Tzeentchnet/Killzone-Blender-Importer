# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import replace
import struct
import unittest

from io_scene_killzone.core.errors import CoreFormatError
from io_scene_killzone.core.native_level import (
    NativeCoreObject,
    NativeObjectKey,
)
from io_scene_killzone.core.native_level_material import (
    build_level_material_plan,
    native_alpha_reference_byte,
    native_alpha_test_passes,
    native_layerbumpdot3_blend_byte,
    native_layerbumpdot3_multiplier,
    native_layerbumpdot3_source_alpha_byte,
    parse_animated_textures,
    parse_level_textures,
    parse_selected_materials,
    supports_bounded_native_layerbumpdot3_preview,
    supports_diagnostic_native_maskedwrite_alphaadd_preview,
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


def _bounded_layerbumpdot3_layer(
    resource_token: int,
    *,
    operation: int,
    lighting_mode: int,
    framebuffer_selector: int,
    wrap: int,
    tfx_policy: int,
    selector: int,
) -> bytes:
    data = bytearray(0x40)
    struct.pack_into("<I", data, 0, resource_token)
    struct.pack_into("<I", data, 8, 0x74)
    data[0x0C] = operation
    data[0x0D] = lighting_mode
    data[0x0E] = 1
    data[0x0F] = framebuffer_selector
    data[0x10] = 3
    data[0x11] = 7
    data[0x13] = wrap
    data[0x14] = wrap
    data[0x15] = 4
    data[0x16] = 1
    data[0x18] = tfx_policy
    data[0x1A] = 255
    struct.pack_into("<4f", data, 0x1C, 0.0, 0.5, 0.5, 1.0)
    data[0x31] = selector
    struct.pack_into("<3f", data, 0x32, 1.0, 0.0, 0.0)
    return bytes(data)


def _bounded_layerbumpdot3_fixture(
    lighting_mode: int,
) -> tuple[tuple[object, ...], dict[int, object]]:
    texture_object, payload = _index8_texture(
        key_index=0,
        object_id=0x1000,
        stream_offset=0,
    )
    _, parsed_textures = parse_level_textures(
        (texture_object,),
        payload,
    )
    base_texture = parsed_textures[0]
    detail_texture = replace(
        base_texture,
        object_id=0x2000,
        pixel_format=3,
        pixel_format_name="ALPHA_8",
        decode_status="decoded-alpha8-two-bank",
    )
    material_blob = (
        struct.pack("<I", 2)
        + bytes(0x14)
        + _bounded_layerbumpdot3_layer(
            0x1000,
            operation=5,
            lighting_mode=lighting_mode,
            framebuffer_selector=1,
            wrap=0,
            tfx_policy=1,
            selector=0,
        )
        + _bounded_layerbumpdot3_layer(
            0x2000,
            operation=18,
            lighting_mode=lighting_mode,
            framebuffer_selector=0,
            wrap=1,
            tfx_policy=3,
            selector=1,
        )
        + struct.pack("<2I", 0x12345678, 0)
    )
    material_object = _object(
        key_index=1,
        type_name="Material",
        object_id=0x3000,
        blob=material_blob,
    )
    material = parse_selected_materials(
        (material_object,),
        (0x3000,),
        (base_texture, detail_texture),
        (),
    )[0]
    return (
        material.layers,
        {
            base_texture.object_id: base_texture,
            detail_texture.object_id: detail_texture,
        },
    )


def _maskedwrite_alphaadd_fixture(
) -> tuple[tuple[object, ...], dict[int, object]]:
    layers, textures = _bounded_layerbumpdot3_fixture(0)
    base, detail = layers
    masked_write = replace(
        base,
        operation=8,
        z_write=1,
        alpha_test=4,
        alpha_reference=204,
        alpha_scalar=255,
        coordinate_selector=0,
    )
    alpha_add = replace(
        detail,
        resource_token=base.resource_token,
        resource_key=base.resource_key,
        operation=5,
        z_write=0,
        coordinate_selector=0,
    )
    return (
        (masked_write, alpha_add),
        {base.resource_token: textures[base.resource_token]},
    )


class NativeLevelMaterialTests(unittest.TestCase):
    def test_applies_native_alpha_reference_and_comparisons(self) -> None:
        self.assertEqual(native_alpha_reference_byte(204), 102)
        expected = {
            0: (False, False, False),
            1: (True, False, False),
            2: (False, True, False),
            3: (True, True, False),
            4: (False, False, True),
            5: (True, False, True),
            6: (False, True, True),
            7: (True, True, True),
        }
        for alpha_test, outcomes in expected.items():
            with self.subTest(alpha_test=alpha_test):
                self.assertEqual(
                    tuple(
                        native_alpha_test_passes(
                            source_alpha,
                            alpha_test,
                            204,
                        )
                        for source_alpha in (101, 102, 103)
                    ),
                    outcomes,
                )

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

    def test_admits_bounded_alphaadd_layerbumpdot3_family(self) -> None:
        for lighting_mode in (0, 1):
            with self.subTest(lighting_mode=lighting_mode):
                layers, textures = _bounded_layerbumpdot3_fixture(
                    lighting_mode
                )
                self.assertTrue(
                    supports_bounded_native_layerbumpdot3_preview(
                        layers,
                        textures,
                    )
                )

        layers, textures = _bounded_layerbumpdot3_fixture(1)
        base = textures[0x1000]
        textures[0x1000] = replace(
            base,
            pixel_format=0,
            pixel_format_name="INDEX_4",
            decode_status="decoded-index4-two-bank",
        )
        self.assertTrue(
            supports_bounded_native_layerbumpdot3_preview(
                layers,
                textures,
            )
        )

    def test_rejects_layerbumpdot3_near_misses(self) -> None:
        layers, textures = _bounded_layerbumpdot3_fixture(0)
        base, detail = layers
        near_misses = (
            (replace(base, operation=0), detail),
            (base, replace(detail, operation=17)),
            (base, replace(detail, coordinate_selector=0)),
            (base, replace(detail, wrap_t=0)),
            (base, replace(detail, lighting_mode=1)),
            (base, replace(detail, z_write=0)),
            (base, replace(detail, vu_features=(True, False))),
            (base, detail, detail),
        )
        for candidate in near_misses:
            with self.subTest(candidate=candidate):
                self.assertFalse(
                    supports_bounded_native_layerbumpdot3_preview(
                        candidate,
                        textures,
                    )
                )

        wrong_base = replace(
            textures[0x1000],
            pixel_format=5,
            pixel_format_name="RGBA_8888",
            decode_status="decoded-rgba8888",
        )
        self.assertFalse(
            supports_bounded_native_layerbumpdot3_preview(
                layers,
                {**textures, 0x1000: wrong_base},
            )
        )
        wrong_detail = replace(
            textures[0x2000],
            pixel_format=1,
            pixel_format_name="INDEX_8",
            decode_status="decoded-index8-csm1",
        )
        self.assertFalse(
            supports_bounded_native_layerbumpdot3_preview(
                layers,
                {**textures, 0x2000: wrong_detail},
            )
        )

    def test_applies_layerbumpdot3_highlight_and_blend_bytes(self) -> None:
        self.assertEqual(native_layerbumpdot3_source_alpha_byte(0), 63)
        self.assertEqual(native_layerbumpdot3_source_alpha_byte(191), 254)
        self.assertEqual(native_layerbumpdot3_source_alpha_byte(192), 255)
        self.assertEqual(native_layerbumpdot3_source_alpha_byte(255), 255)
        self.assertEqual(
            native_layerbumpdot3_multiplier(0),
            63.0 / 128.0,
        )
        self.assertEqual(
            native_layerbumpdot3_multiplier(192),
            255.0 / 128.0,
        )
        self.assertEqual(native_layerbumpdot3_blend_byte(128, 0), 63)
        self.assertEqual(native_layerbumpdot3_blend_byte(255, 65), 255)
        self.assertEqual(native_layerbumpdot3_blend_byte(255, 192), 255)
        with self.assertRaises(ValueError):
            native_layerbumpdot3_source_alpha_byte(256)
        with self.assertRaises(ValueError):
            native_layerbumpdot3_blend_byte(-1, 0)

    def test_admits_diagnostic_maskedwrite_alphaadd_family(self) -> None:
        layers, textures = _maskedwrite_alphaadd_fixture()

        self.assertTrue(
            supports_diagnostic_native_maskedwrite_alphaadd_preview(
                layers,
                textures,
            )
        )

    def test_rejects_maskedwrite_alphaadd_near_misses(self) -> None:
        layers, textures = _maskedwrite_alphaadd_fixture()
        masked_write, alpha_add = layers
        near_misses = (
            (replace(masked_write, operation=0), alpha_add),
            (replace(masked_write, z_write=0), alpha_add),
            (replace(masked_write, alpha_test=6), alpha_add),
            (replace(masked_write, alpha_reference=203), alpha_add),
            (replace(masked_write, coordinate_selector=1), alpha_add),
            (replace(masked_write, texture_enabled=0), alpha_add),
            (
                replace(masked_write, transform_6f=(1.0,) * 6),
                alpha_add,
            ),
            (masked_write, replace(alpha_add, operation=0)),
            (masked_write, replace(alpha_add, z_write=1)),
            (masked_write, replace(alpha_add, coordinate_selector=1)),
            (
                masked_write,
                replace(
                    alpha_add,
                    resource_token=alpha_add.resource_token + 1,
                ),
            ),
            (masked_write, alpha_add, alpha_add),
        )
        for candidate in near_misses:
            with self.subTest(candidate=candidate):
                self.assertFalse(
                    supports_diagnostic_native_maskedwrite_alphaadd_preview(
                        candidate,
                        textures,
                    )
                )

        texture = textures[masked_write.resource_token]
        self.assertFalse(
            supports_diagnostic_native_maskedwrite_alphaadd_preview(
                layers,
                {
                    masked_write.resource_token: replace(
                        texture,
                        base_rgba=None,
                    )
                },
            )
        )
        self.assertFalse(
            supports_diagnostic_native_maskedwrite_alphaadd_preview(
                layers,
                {
                    masked_write.resource_token: replace(
                        texture,
                        pixel_format=13,
                        pixel_format_name="RGB_888",
                    )
                },
            )
        )


if __name__ == "__main__":
    unittest.main()
