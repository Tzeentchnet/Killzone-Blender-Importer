# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import math
import struct
import unittest

from io_scene_killzone.core.errors import CoreFormatError
from io_scene_killzone.core.native_decal import (
    NativeDecalDrawSpan,
    NativeDecalSortMode,
    clipped_decal_footprint,
    decal_buffer_capacity,
    decal_candidate_is_accepted,
    decal_fade_prefix_count,
    decal_generated_vertex_span,
    decal_initial_opacity,
    decal_provisional_budget,
    decal_queue_bucket_order,
    decal_queue_key,
    decal_ring_draw_spans,
    decal_size_and_angle,
    decal_vertex_rgba_with_opacity,
    pack_decal_sort_descriptor,
    parse_serialized_decal_resource,
    step_decal_fade_in,
    step_decal_fade_out,
    unclipped_decal_triangle,
)


def _resource_blob() -> bytes:
    blob = bytearray(0x40)
    struct.pack_into("<i", blob, 0x08, 2)
    struct.pack_into("<i", blob, 0x0C, 11)
    struct.pack_into("<I", blob, 0x10, 0x110)
    struct.pack_into("<i", blob, 0x18, 600)
    struct.pack_into("<i", blob, 0x1C, 400)
    struct.pack_into("<I", blob, 0x20, 1)
    struct.pack_into("<f", blob, 0x24, 0.3)
    struct.pack_into("<f", blob, 0x28, 0.6)
    struct.pack_into("<f", blob, 0x2C, 0.0)
    struct.pack_into("<f", blob, 0x30, 5.0)
    struct.pack_into("<I", blob, 0x34, 1)
    struct.pack_into("<I", blob, 0x38, 0x00541078)
    return bytes(blob)


class NativeDecalTests(unittest.TestCase):
    def test_serialized_resource_layout(self) -> None:
        blob = _resource_blob()
        identity = (1, 2, 3, 4)
        resource = parse_serialized_decal_resource(
            blob,
            object_id=0x0054108E,
            identity_words=identity,
        )

        self.assertEqual(resource.object_id, 0x0054108E)
        self.assertEqual(resource.identity_words, identity)
        self.assertEqual(
            resource.sort_mode, NativeDecalSortMode.BACK_TO_FRONT
        )
        self.assertEqual(resource.sort_order, 11)
        self.assertEqual(resource.max_decals, 600)
        self.assertEqual(resource.decal_threshold, 400)
        self.assertTrue(resource.random_rotation)
        self.assertAlmostEqual(resource.min_size, 0.3)
        self.assertAlmostEqual(resource.max_size, 0.6)
        self.assertEqual(resource.fade_in_time, 0.0)
        self.assertEqual(resource.fade_time, 5.0)
        self.assertTrue(resource.clip_decal)
        self.assertEqual(resource.material_token, 0x00541078)
        self.assertEqual(resource.raw_blob, blob)

    def test_serialized_resource_rejects_invalid_values(self) -> None:
        with self.assertRaisesRegex(CoreFormatError, "exactly 0x40"):
            parse_serialized_decal_resource(
                bytes(0x3F), object_id=1
            )

        blob = bytearray(_resource_blob())
        struct.pack_into("<I", blob, 0x20, 2)
        with self.assertRaisesRegex(CoreFormatError, "not a boolean"):
            parse_serialized_decal_resource(bytes(blob), object_id=1)

        blob = bytearray(_resource_blob())
        struct.pack_into("<i", blob, 0x0C, 16)
        with self.assertRaisesRegex(CoreFormatError, "outside 0..15"):
            parse_serialized_decal_resource(bytes(blob), object_id=1)

    def test_shared_random_scalar_controls_size_and_angle(self) -> None:
        size, angle = decal_size_and_angle(2.0, 6.0, 0.25)
        self.assertEqual(size, 3.0)
        self.assertAlmostEqual(angle, math.pi / 2.0)
        with self.assertRaisesRegex(ValueError, "within 0..1"):
            decal_size_and_angle(2.0, 6.0, 1.01)

    def test_initial_state_and_capacity_boundaries(self) -> None:
        self.assertEqual(decal_initial_opacity(0.0), 1.0)
        self.assertEqual(decal_initial_opacity(-1.0), 1.0)
        self.assertEqual(decal_initial_opacity(0.1), 0.0)
        self.assertEqual(decal_provisional_budget(0.5), 16)
        self.assertEqual(
            decal_provisional_budget(math.nextafter(0.5, 1.0)),
            128,
        )
        self.assertEqual(decal_generated_vertex_span(clip_decal=False), 3)
        self.assertEqual(
            decal_generated_vertex_span(
                clip_decal=True, clipped_vertex_count=6
            ),
            12,
        )
        self.assertEqual(decal_buffer_capacity(11, 200), 15)
        self.assertEqual(decal_buffer_capacity(100, 3), 9)

    def test_exact_projector_triangle_and_winding(self) -> None:
        triangle = unclipped_decal_triangle(2.0)
        self.assertEqual(
            tuple(vertex.position for vertex in triangle),
            ((-2.0, -2.0), (6.0, -2.0), (-2.0, 6.0)),
        )
        self.assertEqual(
            tuple(vertex.uv for vertex in triangle),
            ((0.0, 0.0), (2.0, 0.0), (0.0, 2.0)),
        )
        swapped = unclipped_decal_triangle(
            2.0, swap_winding=True
        )
        self.assertEqual(swapped, (triangle[0], triangle[2], triangle[1]))

    def test_clipped_footprint_size_boundary(self) -> None:
        small = clipped_decal_footprint(
            math.nextafter(0.2, 0.0)
        )
        boundary = clipped_decal_footprint(0.2)
        self.assertEqual(len(small), 3)
        self.assertEqual(len(boundary), 4)
        self.assertEqual(
            tuple(vertex.uv for vertex in boundary),
            ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        )

    def test_receiver_candidate_thresholds(self) -> None:
        common = {
            "candidate_tag": 7,
            "mark_tag": 7,
            "direction_dot_normal": -0.1,
            "size": 0.5,
        }
        self.assertTrue(
            decal_candidate_is_accepted(
                **common, plane_distance=-0.5
            )
        )
        self.assertFalse(
            decal_candidate_is_accepted(
                **common,
                plane_distance=math.nextafter(-0.5, -1.0),
            )
        )
        self.assertTrue(
            decal_candidate_is_accepted(
                **{**common, "size": 0.499},
                plane_distance=-100.0,
            )
        )
        self.assertFalse(
            decal_candidate_is_accepted(
                **{**common, "candidate_tag": 8},
                plane_distance=0.0,
            )
        )

    def test_soft_pressure_selects_each_group_oldest_prefix(self) -> None:
        self.assertEqual(
            decal_fade_prefix_count(
                (3, 9, 6),
                total_instance_vertices=30,
                decal_threshold=5,
            ),
            3,
        )
        self.assertEqual(
            decal_fade_prefix_count(
                (12, 3),
                total_instance_vertices=30,
                decal_threshold=5,
            ),
            2,
        )
        self.assertEqual(
            decal_fade_prefix_count(
                (3, 9),
                total_instance_vertices=15,
                decal_threshold=5,
            ),
            0,
        )

    def test_fade_and_vertex_alpha(self) -> None:
        self.assertAlmostEqual(
            step_decal_fade_out(
                0.8, delta_time=1.0, fade_time=5.0
            ),
            0.6,
        )
        self.assertEqual(
            step_decal_fade_out(
                0.8, delta_time=1.0, fade_time=0.0
            ),
            0.0,
        )
        self.assertAlmostEqual(
            step_decal_fade_in(
                0.0, delta_time=0.5, fade_in_time=2.0
            ),
            0.25,
        )
        self.assertEqual(
            decal_vertex_rgba_with_opacity(0xA1123456, 0.5),
            0x7F123456,
        )
        self.assertEqual(
            decal_vertex_rgba_with_opacity(0x00123456, 2.0),
            0xFF123456,
        )

    def test_ring_draw_order(self) -> None:
        self.assertEqual(
            decal_ring_draw_spans(first=2, cursor=8, limit=12),
            (NativeDecalDrawSpan(2, 6),),
        )
        self.assertEqual(
            decal_ring_draw_spans(first=9, cursor=3, limit=12),
            (
                NativeDecalDrawSpan(9, 3),
                NativeDecalDrawSpan(0, 3),
            ),
        )
        self.assertEqual(
            decal_ring_draw_spans(first=3, cursor=3, limit=12),
            (
                NativeDecalDrawSpan(3, 9),
                NativeDecalDrawSpan(0, 3),
            ),
        )

    def test_queue_bucket_and_depth_key_ordering(self) -> None:
        order = decal_queue_bucket_order()
        self.assertEqual(len(order), 48)
        self.assertEqual(
            order[:3],
            (
                (0, NativeDecalSortMode.OFF),
                (0, NativeDecalSortMode.FRONT_TO_BACK),
                (0, NativeDecalSortMode.BACK_TO_FRONT),
            ),
        )
        self.assertEqual(
            order[-1], (15, NativeDecalSortMode.BACK_TO_FRONT)
        )
        self.assertEqual(
            decal_queue_key(
                bounds_min=(-1.0, 1.0, -1.0),
                bounds_max=(1.0, 3.0, 1.0),
                camera_position=(0.0, 0.0, 0.0),
                camera_forward=(0.0, 1.0, 0.0),
            ),
            4.0,
        )
        self.assertEqual(
            decal_queue_key(
                bounds_min=(-1.0, -3.0, -1.0),
                bounds_max=(1.0, -1.0, 1.0),
                camera_position=(0.0, 0.0, 0.0),
                camera_forward=(0.0, 1.0, 0.0),
            ),
            -4.0,
        )

    def test_sort_descriptor_bit_packing(self) -> None:
        self.assertEqual(
            pack_decal_sort_descriptor(
                sort_mode=NativeDecalSortMode.BACK_TO_FRONT,
                sort_order=11,
                enabled=True,
                material_flags=0x0400,
            ),
            0x046E,
        )
        with self.assertRaisesRegex(ValueError, "bits 7..31"):
            pack_decal_sort_descriptor(
                sort_mode=NativeDecalSortMode.OFF,
                sort_order=0,
                enabled=False,
                material_flags=1,
            )


if __name__ == "__main__":
    unittest.main()
