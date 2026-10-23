# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import struct
import unittest

from io_scene_killzone.core.core_format import CoreHeader, CoreString
from io_scene_killzone.core.errors import CoreFormatError
from io_scene_killzone.core.native_level import (
    NativeCollisionMeshInstance,
    NativeCoreObject,
    NativeObjectKey,
    NativeSimpleSkinnedInstance,
    NativeStaticInstance,
    NativeStaticResourceNode,
    build_static_model_render_plan,
    build_static_render_plan,
    parse_native_collision_mesh_instances,
    parse_native_level_instances,
)
from io_scene_killzone.core.native_static_geometry import (
    NativeRegularStaticResource,
    NativeStaticMeshRecord,
    decode_static_packet,
)


_TAIL_SHAPES = (
    (0x69, 0x6F, 0x65),
    (0x69, 0x6F, 0x6D),
    (0x69, 0x6F, 0x6D, 0x65),
    (0x69, 0x6F, 0x6D, 0x66),
    (0x69, 0x6F, 0x6D, 0x6D),
    (0x69, 0x6F, 0x6E),
    (0x69, 0x6F, 0x6E, 0x65),
    (0x69, 0x6F, 0x6E, 0x6D),
    (0x69, 0x6F, 0x6E, 0x6D, 0x62),
    (0x69, 0x6F, 0x6E, 0x6D, 0x65),
    (0x69, 0x6F, 0x6E, 0x6D, 0x65, 0x62),
    (0x69, 0x6F, 0x6E, 0x6D, 0x65, 0x66),
    (0x69, 0x6F, 0x6E, 0x6D, 0x65, 0x6A),
    (0x69, 0x6F, 0x6E, 0x6D, 0x66),
    (0x69, 0x6F, 0x6E, 0x6D, 0x6A),
    (0x69, 0x6F, 0x6E, 0x6D, 0x6D),
    (0x69, 0x6F, 0x6E, 0x6D, 0x6D, 0x66),
    (0x69, 0x6F, 0x6E, 0x6D, 0x6D, 0x6A),
)

_WIDTHS = {
    0x69: 6,
    0x6F: 2,
    0x6E: 4,
    0x65: 4,
    0x6D: 8,
    0x62: 1,
    0x66: 2,
    0x6A: 3,
}


def _command(opcode: int, count: int, address: int) -> bytes:
    return struct.pack(
        "<I", (opcode << 24) | (count << 16) | 0xC000 | address
    )


def _attribute_payload(opcode: int, count: int) -> bytes:
    if opcode == 0x69:
        return struct.pack(
            "<12h",
            -1,
            -1,
            0,
            1,
            -1,
            0,
            1,
            1,
            0,
            -1,
            1,
            0,
        )
    if opcode == 0x6F:
        normal = 15 | (15 << 5) | (30 << 10)
        return struct.pack(
            "<4H",
            normal | 0x8000,
            normal | 0x8000,
            normal,
            normal,
        )
    if opcode == 0x6E:
        return bytes((128, 128, 128, 127)) * count
    if opcode == 0x65:
        return struct.pack(
            "<8h",
            0,
            0,
            4096,
            0,
            4096,
            4096,
            0,
            4096,
        )
    if opcode == 0x6D:
        values: list[int] = []
        for index in range(count):
            values.extend((index * 1024, 0, 0, index * 1024))
        return struct.pack(f"<{len(values)}h", *values)
    if opcode in (0x62, 0x66, 0x6A):
        width = _WIDTHS[opcode]
        return bytes(
            (index + component) & 0xFF
            for index in range(count)
            for component in range(width)
        )
    raise AssertionError(f"Unhandled fixture opcode 0x{opcode:02X}")


def _make_packet(shape: tuple[int, ...]) -> bytes:
    count = 4
    has_color = 0x6E in shape
    coordinate_commands = sum(
        opcode in (0x65, 0x6D) for opcode in shape
    )
    header_b = (
        0xB7,
        0xB7 + count,
        0xB7 + 2 * count,
        0xB7 + (3 if has_color else 2) * count,
    )
    header_a = (
        count,
        3 * count,
        3 * count,
        header_b[3] + coordinate_commands * count,
    )

    body = bytearray(struct.pack("<8I", *(header_a + header_b)))
    coordinate_address = header_b[3]
    for index, opcode in enumerate(shape):
        if index == 0:
            address = header_b[0]
        elif index == 1:
            address = header_b[1]
        elif opcode == 0x6E:
            address = header_b[2]
        elif opcode in (0x65, 0x6D):
            address = coordinate_address
            coordinate_address += count
        else:
            address = header_a[3]
        payload = _attribute_payload(opcode, count)
        body.extend(_command(opcode, count, address))
        body.extend(payload)
        body.extend(bytes((-len(payload)) & 3))

    while (len(body) + 4) % 16:
        body.extend(bytes(4))
    body.extend(struct.pack("<I", 0x6501C1EE))
    qwc = len(body) // 16
    return (
        struct.pack(
            "<4I",
            0x60000000 | qwc,
            0x80000000,
            0x01000404,
            0x6C02C1D9,
        )
        + bytes(body)
    )


def _regular(
    key: NativeObjectKey,
    object_id: int,
    triangle_count: int,
) -> NativeStaticResourceNode:
    record = NativeStaticMeshRecord(
        material_id=1,
        triangle_count=triangle_count,
        packet_count=0,
        flag_a=1,
        attribute_flags=0,
        raw_bounds=(0, 0, 0, 0, 0, 0),
    )
    resource = NativeRegularStaticResource(
        object_id=object_id,
        records=(record,),
        packets=(),
        stream_chunk_sizes=(),
        root_flags=(False, False),
        position_matrix=(
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
        ),
        position_scale=(1.0, 1.0, 1.0),
        position_translation=(0.0, 0.0, 0.0),
        blob_tail=b"",
    )
    return NativeStaticResourceNode(
        key=key,
        object_id=object_id,
        kind="regular",
        child_keys=(),
        child_tokens=(),
        lod_thresholds=(),
        regular=resource,
    )


def _wrapper(
    key: NativeObjectKey,
    object_id: int,
    kind: str,
    children: tuple[NativeObjectKey, ...],
) -> NativeStaticResourceNode:
    return NativeStaticResourceNode(
        key=key,
        object_id=object_id,
        kind=kind,
        child_keys=children,
        child_tokens=tuple(range(len(children))),
        lod_thresholds=(
            tuple(float(index) for index in range(len(children)))
            if kind == "lod"
            else ()
        ),
        regular=None,
    )


def _instance(
    key: NativeObjectKey,
    object_id: int,
    target: NativeObjectKey,
) -> NativeStaticInstance:
    return NativeStaticInstance(
        key=key,
        object_id=object_id,
        name=f"instance_{object_id}",
        name_index=0,
        name_is_identifier=True,
        parent_token=0,
        resource_token=object_id,
        resource_key=target,
        matrix=(
            1.0,
            0.0,
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
            0.0,
            1.0,
        ),
        footer=1,
        raw_blob=b"",
    )


def _collision_instance(
    key: NativeObjectKey,
    object_id: int,
    target: NativeObjectKey,
) -> NativeCollisionMeshInstance:
    return NativeCollisionMeshInstance(
        key=key,
        object_id=object_id,
        name=f"collision_instance_{object_id}",
        name_index=0,
        name_is_identifier=False,
        collision_group=0x1C000002,
        parent_token=0,
        collision_enabled=True,
        collision_resource_token=0x9000,
        collision_resource_key=NativeObjectKey(0x900, 0),
        collision_resource_type="CollisionResourcePolySoup",
        resource_token=object_id,
        resource_key=target,
        matrix=(
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            10.0,
            20.0,
            30.0,
            1.0,
        ),
        footer=0x00020001,
        raw_blob=b"",
    )


def _header(*values: str) -> CoreHeader:
    strings = tuple(
        CoreString(raw=value.encode("utf-8"), utf8=value)
        for value in values
    )
    return CoreHeader(
        variant=5,
        string_table_size=0,
        strings=strings,
        string_table_padding=0,
        body_offset=0,
        total_size=0,
        header_sha256="",
        original_header=b"",
    )


def _core_object(
    key: NativeObjectKey,
    type_name: str,
    object_id: int,
    blob: bytes = b"",
) -> NativeCoreObject:
    return NativeCoreObject(
        key=key,
        type_name=type_name,
        object_id=object_id,
        blob_offset=0,
        blob=blob,
        identity_words=None,
        stream_offset=0,
        stream_size=0,
    )


class NativeStaticPacketTests(unittest.TestCase):
    def test_accepts_all_eighteen_level_packet_shapes(self) -> None:
        self.assertEqual(len(set(_TAIL_SHAPES)), 18)
        for shape in _TAIL_SHAPES:
            with self.subTest(shape=shape):
                packet_data = _make_packet(shape)
                packet = decode_static_packet(
                    packet_data,
                    bounded_size=len(packet_data),
                    raw_bounds=(-1, -1, 0, 1, 1, 0),
                    color_expected=0x6E in shape,
                )
                expected_coordinates = sum(
                    1 if opcode == 0x65 else 2 if opcode == 0x6D else 0
                    for opcode in shape
                )
                self.assertEqual(packet.tail_opcodes, shape)
                self.assertEqual(len(packet.vertices), 4)
                self.assertEqual(len(packet.triangles), 2)
                self.assertEqual(
                    len(packet.vertices[0].coordinates),
                    expected_coordinates,
                )
                expected_auxiliary_width = next(
                    (
                        _WIDTHS[opcode]
                        for opcode in shape
                        if opcode in (0x62, 0x66, 0x6A)
                    ),
                    0,
                )
                self.assertEqual(
                    len(packet.vertices[0].auxiliary or b""),
                    expected_auxiliary_width,
                )

    def test_requires_external_pending_unpack_command(self) -> None:
        damaged = bytearray(_make_packet((0x69, 0x6F, 0x6D)))
        struct.pack_into("<I", damaged, len(damaged) - 4, 0)

        with self.assertRaisesRegex(CoreFormatError, "pending unpack"):
            decode_static_packet(bytes(damaged))


class NativeStaticGraphTests(unittest.TestCase):
    def test_imports_unreferenced_roots_at_identity_and_instance_children(self) -> None:
        regular_a = _regular(NativeObjectKey(0x100, 0), 1, 10)
        regular_lod0 = _regular(NativeObjectKey(0x200, 0), 2, 20)
        regular_lod1 = _regular(NativeObjectKey(0x300, 0), 3, 30)
        regular_direct = _regular(NativeObjectKey(0x400, 0), 4, 40)
        lod = _wrapper(
            NativeObjectKey(0x500, 0),
            5,
            "lod",
            (regular_lod0.key, regular_lod1.key),
        )
        multi = _wrapper(
            NativeObjectKey(0x600, 0),
            6,
            "multi",
            (regular_a.key, lod.key),
        )
        resources = (
            regular_a,
            regular_lod0,
            regular_lod1,
            regular_direct,
            lod,
            multi,
        )
        instances = (
            _instance(NativeObjectKey(0x700, 0), 7, regular_lod0.key),
            _instance(NativeObjectKey(0x700, 1), 8, regular_direct.key),
        )

        plan = build_static_render_plan(resources, instances)

        self.assertEqual(set(plan.roots), {multi.key, regular_direct.key})
        self.assertEqual(plan.direct_roots, (multi.key,))
        self.assertEqual(
            tuple(value.leaf_key for value in plan.direct_occurrences),
            (regular_a.key, regular_lod0.key),
        )
        self.assertEqual(
            tuple(value.leaf_key for value in plan.instance_occurrences),
            (regular_lod0.key, regular_direct.key),
        )
        self.assertEqual(plan.direct_triangle_count, 30)
        self.assertEqual(plan.instance_triangle_count, 60)

    def test_selects_one_standalone_static_model_root(self) -> None:
        regular_a = _regular(NativeObjectKey(0x100, 0), 1, 10)
        regular_b = _regular(NativeObjectKey(0x200, 0), 2, 20)
        wrapper = _wrapper(
            NativeObjectKey(0x300, 0),
            3,
            "multi",
            (regular_a.key, regular_b.key),
        )
        unrelated = _regular(NativeObjectKey(0x400, 0), 4, 40)

        plan = build_static_model_render_plan(
            (regular_a, regular_b, wrapper, unrelated),
            wrapper.key,
        )

        self.assertEqual(set(plan.roots), {wrapper.key, unrelated.key})
        self.assertEqual(plan.direct_roots, (wrapper.key,))
        self.assertEqual(
            tuple(value.leaf_key for value in plan.direct_occurrences),
            (regular_a.key, regular_b.key),
        )
        self.assertEqual(plan.direct_triangle_count, 30)
        self.assertEqual(plan.instance_occurrences, ())

    def test_collision_mesh_reference_is_a_placed_occurrence(self) -> None:
        resource = _regular(NativeObjectKey(0x100, 0), 1, 10)
        collision_instance = _collision_instance(
            NativeObjectKey(0x200, 0),
            2,
            resource.key,
        )

        plan = build_static_render_plan(
            (resource,),
            (),
            (collision_instance,),
        )

        self.assertEqual(plan.direct_roots, ())
        self.assertEqual(plan.direct_occurrences, ())
        self.assertEqual(len(plan.instance_occurrences), 1)
        occurrence = plan.instance_occurrences[0]
        self.assertEqual(occurrence.path, "collision-instance")
        self.assertEqual(occurrence.instance_key, collision_instance.key)
        self.assertEqual(occurrence.matrix, collision_instance.matrix)

    def test_rejects_wrapper_cycles(self) -> None:
        key_a = NativeObjectKey(0x100, 0)
        key_b = NativeObjectKey(0x200, 0)
        resources = (
            _wrapper(key_a, 1, "multi", (key_b,)),
            _wrapper(key_b, 2, "multi", (key_a,)),
        )
        instances = (_instance(NativeObjectKey(0x300, 0), 3, key_a),)

        with self.assertRaisesRegex(CoreFormatError, "cycle"):
            build_static_render_plan(resources, instances)


class NativeLevelInstanceTests(unittest.TestCase):
    def test_parses_static_and_animated_prop_identity_and_references(self) -> None:
        matrix = (
            2.0,
            0.0,
            0.0,
            0.0,
            0.0,
            3.0,
            0.0,
            0.0,
            0.0,
            0.0,
            4.0,
            0.0,
            10.0,
            20.0,
            30.0,
            1.0,
        )
        static_blob = struct.pack(
            "<3I16f2I12x",
            1,
            0,
            0,
            *matrix,
            0x2000,
            0x00030001,
        )
        animated_blob = struct.pack(
            "<3I16f6I12x",
            1,
            0,
            0x3000,
            *matrix,
            0x4000,
            0x5000,
            0,
            0,
            0,
            0x00010001,
        )
        static_resource = _regular(
            NativeObjectKey(0x100, 0),
            0x2000,
            1,
        )
        objects = (
            _core_object(
                NativeObjectKey(0x600, 0),
                "StaticMeshInstance",
                0x6000,
                static_blob,
            ),
            _core_object(
                NativeObjectKey(0x700, 0),
                "SimpleAnimatingSkinnedMeshInstance",
                0x7000,
                animated_blob,
            ),
            _core_object(
                NativeObjectKey(0x300, 0),
                "ProximitySensorInstance",
                0x3000,
            ),
            _core_object(
                NativeObjectKey(0x400, 0),
                "MultiSkinnedMeshResource",
                0x4000,
            ),
            _core_object(
                NativeObjectKey(0x500, 0),
                "SkeletonAnimationResource",
                0x5000,
            ),
        )

        static_instances, animated_instances = (
            parse_native_level_instances(
                _header("Shared Prop", "Shared Prop"),
                objects,
                (static_resource,),
            )
        )

        self.assertEqual(len(static_instances), 1)
        static = static_instances[0]
        self.assertEqual(static.name, "Shared Prop")
        self.assertEqual(static.name_index, 0)
        self.assertTrue(static.name_is_identifier)
        self.assertEqual(static.resource_token, 0x2000)
        self.assertEqual(static.resource_key, static_resource.key)
        self.assertEqual(static.matrix, matrix)
        self.assertEqual(static.footer, 0x00030001)
        self.assertEqual(static.raw_blob, static_blob)

        self.assertEqual(len(animated_instances), 1)
        animated = animated_instances[0]
        self.assertIsInstance(animated, NativeSimpleSkinnedInstance)
        self.assertEqual(animated.name, "Shared Prop")
        self.assertEqual(animated.name_index, 1)
        self.assertFalse(animated.name_is_identifier)
        self.assertEqual(animated.parent_token, 0x3000)
        self.assertEqual(animated.parent_key, NativeObjectKey(0x300, 0))
        self.assertEqual(animated.resource_token, 0x4000)
        self.assertEqual(animated.resource_key, NativeObjectKey(0x400, 0))
        self.assertEqual(
            animated.resource_type,
            "MultiSkinnedMeshResource",
        )
        self.assertEqual(animated.default_animation_token, 0x5000)
        self.assertEqual(
            animated.default_animation_key,
            NativeObjectKey(0x500, 0),
        )
        self.assertEqual(animated.matrix, matrix)
        self.assertEqual(animated.footer, 0x00010001)
        self.assertEqual(animated.raw_blob, animated_blob)

    def test_parses_collision_mesh_prop_and_both_references(self) -> None:
        matrix = (
            2.0,
            0.0,
            0.0,
            0.0,
            0.0,
            3.0,
            0.0,
            0.0,
            0.0,
            0.0,
            4.0,
            0.0,
            10.0,
            20.0,
            30.0,
            1.0,
        )
        blob = struct.pack(
            "<4IBB2x16f4I12x",
            0,
            0x1C000002,
            0,
            0x9000,
            1,
            1,
            *matrix,
            0x2000,
            0,
            0,
            0x00020001,
        )
        static_resource = _regular(
            NativeObjectKey(0x100, 0),
            0x2000,
            1,
        )
        collision_resource_key = NativeObjectKey(0x900, 0)
        instance_key = NativeObjectKey(0x800, 0)
        objects = (
            _core_object(
                instance_key,
                "CollisionMeshInstance",
                0x8000,
                blob,
            ),
            _core_object(
                collision_resource_key,
                "CollisionResourcePolySoup",
                0x9000,
            ),
        )

        instances = parse_native_collision_mesh_instances(
            _header("Collision Prop"),
            objects,
            (static_resource,),
        )

        self.assertEqual(len(instances), 1)
        instance = instances[0]
        self.assertEqual(instance.key, instance_key)
        self.assertEqual(instance.name, "Collision Prop")
        self.assertEqual(instance.name_index, 0)
        self.assertTrue(instance.name_is_identifier)
        self.assertEqual(instance.collision_group, 0x1C000002)
        self.assertTrue(instance.collision_enabled)
        self.assertEqual(instance.collision_resource_token, 0x9000)
        self.assertEqual(
            instance.collision_resource_key,
            collision_resource_key,
        )
        self.assertEqual(
            instance.collision_resource_type,
            "CollisionResourcePolySoup",
        )
        self.assertEqual(instance.resource_token, 0x2000)
        self.assertEqual(instance.resource_key, static_resource.key)
        self.assertEqual(instance.matrix, matrix)
        self.assertEqual(instance.footer, 0x00020001)
        self.assertEqual(instance.raw_blob, blob)

    def test_rejects_non_null_blend_shape_animation_reference(self) -> None:
        matrix = (
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
        blob = struct.pack(
            "<3I16f6I12x",
            0,
            1,
            0,
            *matrix,
            0x4000,
            0x5000,
            1,
            0,
            0,
            1,
        )
        objects = (
            _core_object(
                NativeObjectKey(0x700, 0),
                "SimpleAnimatingSkinnedMeshInstance",
                0x7000,
                blob,
            ),
        )

        with self.assertRaisesRegex(CoreFormatError, "blend-shape"):
            parse_native_level_instances(
                _header("Animated Prop"),
                objects,
                (),
            )


if __name__ == "__main__":
    unittest.main()
