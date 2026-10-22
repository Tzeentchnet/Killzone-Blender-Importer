# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import replace
import math
import struct
import unittest

from io_scene_killzone.core.errors import CoreFormatError
from io_scene_killzone.core.native_animation import (
    interpolate_quaternion_vu0,
    parse_packed_animation,
    parse_pose_track_mapping,
    sample_track_rotation,
    sample_track_scale,
    sample_track_translation,
    scatter_category_records,
    select_animation_time,
)
from io_scene_killzone.core.native_animation_resource import (
    bind_native_animation,
    discover_native_animation_clips,
    native_animation_bake_times,
    parse_native_animation_library,
    sample_native_animation_pose,
)
from io_scene_killzone.core.native_level import CORE_STREAM_MAGIC
from io_scene_killzone.core.native_skeleton import parse_native_skeleton
from tests.support import create_fixture_core
from tests.test_native_skinned import _make_skeleton_blob


def _count_bytes(time_codec: int, sample_count: int) -> bytes:
    if time_codec == 0:
        if sample_count != 1:
            raise ValueError("Time codec zero has one implicit sample")
        return b""
    if sample_count < 0x80:
        return bytes((sample_count,))
    return bytes(
        (
            0x80 | (sample_count & 0x7F),
            sample_count >> 7,
        )
    )


def _record(
    *,
    time_codec: int,
    translation_codec: int = 0,
    rotation_codec: int = 0,
    scale_codec: int = 0,
    sample_count: int = 1,
    time_payload: bytes = b"",
    rotation_payload: bytes = b"",
    translation_payload: bytes = b"",
    scale_payload: bytes = b"",
) -> bytes:
    return (
        bytes(
            (
                time_codec | (translation_codec << 4),
                rotation_codec | (scale_codec << 6),
            )
        )
        + _count_bytes(time_codec, sample_count)
        + time_payload
        + rotation_payload
        + translation_payload
        + scale_payload
    )


def _block(
    records: list[bytes],
    *,
    alignment: int = 16,
) -> bytes:
    packed = b"".join(records)
    data = struct.pack("<2I", len(records), len(packed)) + packed
    return data + bytes((-len(data)) % alignment)


def _float_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def _truncated_vector(
    x: float,
    y: float,
    z: float,
) -> bytes:
    x_bits = _float_bits(x)
    y_bits = _float_bits(y)
    z_bits = _float_bits(z)
    if x_bits & 0x7FF or y_bits & 0x7FF or z_bits & 0x3FF:
        raise ValueError("Fixture value cannot be represented exactly")
    packed = (
        ((x_bits >> 11) & 0x1FFFFF) << 43
        | ((y_bits >> 11) & 0x1FFFFF) << 22
        | ((z_bits >> 10) & 0x3FFFFF)
    )
    return packed.to_bytes(8, "little")


def _variable_time_payload(deltas: list[int]) -> bytes:
    bits: list[int] = []
    for delta in deltas:
        if delta == 1:
            bits.append(0)
        elif 2 <= delta <= 9:
            bits.append(1)
            extra = delta - 2
            bits.extend((extra >> bit) & 1 for bit in range(3))
        else:
            raise ValueError("Variable time delta must be 1..9")
    bitstream = bytearray((len(bits) + 7) // 8)
    for index, value in enumerate(bits):
        bitstream[index >> 3] |= value << (index & 7)
    return bytes((1 + len(bitstream),)) + bytes(bitstream)


def _pack_lsb_values(
    samples: list[tuple[int, ...]],
    widths: tuple[int, ...],
) -> bytes:
    bits: list[int] = []
    for sample in samples:
        if len(sample) != len(widths):
            raise ValueError("Sample and width counts differ")
        for value, width in zip(sample, widths):
            if value < 0 or value >= 1 << width:
                raise ValueError("Sample value exceeds its bit width")
            bits.extend((value >> bit) & 1 for bit in range(width))
    output = bytearray((len(bits) + 7) // 8)
    for index, value in enumerate(bits):
        output[index >> 3] |= value << (index & 7)
    return bytes(output)


def _adaptive_rotation_payload(
    *,
    codec: int,
    samples: list[tuple[int, int, int, int]],
) -> bytes:
    if codec not in (7, 8):
        raise ValueError("Adaptive fixture codec must be 7 or 8")
    h0 = 0x2E | (1 if codec == 7 else 0)
    h1 = 0x22
    metadata = b"".join(
        struct.pack(">2h", 0, 16384) for _ in range(3)
    )
    fields = (
        [(z, y, x, sign) for x, y, z, sign in samples]
        if codec == 7
        else [(z, y, x) for x, y, z, _ in samples]
    )
    widths = (2, 2, 2, 1) if codec == 7 else (2, 2, 2)
    return bytes((h0, h1)) + metadata + _pack_lsb_values(
        fields,
        widths,
    )


def _fixed_axis_rotation_payload(
    *,
    compact_axis: bool,
    codes: list[int],
    width: int = 2,
    extent_raw: int = 32768,
    lower_raw: int = 0,
) -> bytes:
    descriptor = width | (0x10 if compact_axis else 0)
    axis = bytes((0x1B,)) if compact_axis else b"\x00\x00"
    metadata = struct.pack("<2H", extent_raw, lower_raw)
    bitstream = _pack_lsb_values(
        [(code,) for code in codes],
        (width,),
    )
    return bytes((descriptor,)) + axis + metadata + bitstream


def _bone_map_section(
    *,
    type_index: int,
    object_id: int,
    blob: bytes,
) -> bytes:
    directory_end = 0x1C + 0x18
    data_offset = ((directory_end + 4 + 0x0F) & ~0x0F) - 4
    return (
        struct.pack("<5I", data_offset, type_index, 1, 0, 0)
        + struct.pack("<2I", 1, 0)
        + struct.pack(
            "<6I",
            len(blob),
            object_id,
            0x10,
            0x20,
            0x30,
            0x40,
        )
        + bytes(data_offset - directory_end)
        + bytes(4)
        + blob
    )


def _expanded_section(
    *,
    type_index: int,
    object_id: int,
    blob: bytes,
    stream_offset: int = 0,
    stream_size: int = 0,
) -> bytes:
    data_offset = 0x1C + 0x20
    return (
        struct.pack("<5I", data_offset, type_index, 1, 0, 0)
        + struct.pack("<2I", 1, 1)
        + struct.pack(
            "<8I",
            len(blob),
            object_id,
            0x11,
            0x22,
            0x33,
            0x44,
            stream_offset,
            stream_size,
        )
        + bytes(4)
        + blob
    )


def _animation_library_fixture(
    *,
    streamed: bool = False,
    inline_stream_anchor: int = 0,
    inline_stream_size: int = 0,
) -> tuple[bytes, bytes]:
    if streamed and (inline_stream_anchor or inline_stream_size):
        raise ValueError("Streamed fixtures cannot use an inline range")
    strings = (
        "SkeletonAnimationBoneMap",
        "SkeletonAnimationResource",
        "",
        "root\ncHiLd\n",
    )
    bone_map_token = 0xB000
    bone_map_blob = struct.pack("<4I", 2, 0, 0, 3)
    time_payload = struct.pack("<f", 1.0) + struct.pack(
        ">2H", 0, 0xFFFF
    )
    packed = _block(
        [
            _record(time_codec=0),
            _record(
                time_codec=1,
                translation_codec=2,
                sample_count=2,
                time_payload=time_payload,
                translation_payload=(
                    _truncated_vector(0.0, 0.0, 1.0)
                    + _truncated_vector(1.0, 0.0, 1.0)
                ),
            ),
        ],
        alignment=64 if streamed else 16,
    )
    clip_header = struct.pack(
        "<6I4fI3f2I",
        0 if streamed else 0x100,
        1,
        0,
        0,
        2,
        0,
        0.001,
        0.01,
        0.01,
        1.0,
        1,
        2.0,
        0.0,
        0.0,
        bone_map_token,
        0,
    )
    clip_blob = clip_header if streamed else clip_header + packed
    stream_offset = 0x40 if streamed else inline_stream_anchor
    stream_size = len(packed) if streamed else inline_stream_size
    body = (
        _bone_map_section(
            type_index=0,
            object_id=bone_map_token,
            blob=bone_map_blob,
        )
        + _expanded_section(
            type_index=1,
            object_id=0xA000,
            blob=clip_blob,
            stream_offset=stream_offset,
            stream_size=stream_size,
        )
    )
    return (
        create_fixture_core(
            strings,
            trailing_padding=0,
            body=body,
        ),
        (
            CORE_STREAM_MAGIC
            + bytes(0x40 - len(CORE_STREAM_MAGIC))
            + packed
            if streamed
            else (
                CORE_STREAM_MAGIC
                + bytes(
                    max(
                        0x40,
                        inline_stream_anchor + inline_stream_size,
                    )
                    - len(CORE_STREAM_MAGIC)
                )
                if inline_stream_anchor or inline_stream_size
                else b""
            )
        ),
    )


class NativePackedAnimationTests(unittest.TestCase):
    def test_parses_payload_order_and_samples_fixed_channels(self) -> None:
        time_payload = struct.pack("<f", 2.0) + struct.pack(
            ">2H", 0, 0xFFFF
        )
        quaternion_a = struct.pack("<4H", 0, 0x4000, 0x8000, 0xFFFF)
        quaternion_b = struct.pack("<4H", 0xFFFF, 0x8000, 0x4000, 0)
        translation_payload = _truncated_vector(
            1.0, 2.0, 4.0
        ) + _truncated_vector(-1.0, -2.0, 0.5)
        scale_payload = _truncated_vector(
            1.0, 1.0, 1.0
        ) + _truncated_vector(2.0, 2.0, 2.0)
        animation = parse_packed_animation(
            _block(
                [
                    _record(
                        time_codec=1,
                        translation_codec=2,
                        rotation_codec=2,
                        scale_codec=2,
                        sample_count=2,
                        time_payload=time_payload,
                        rotation_payload=quaternion_a
                        + quaternion_b,
                        translation_payload=translation_payload,
                        scale_payload=scale_payload,
                    )
                ]
            ),
            alignment=16,
        )
        track = animation.tracks[0]

        self.assertEqual(track.offset, 8)
        self.assertEqual(track.sample_count, 2)
        self.assertEqual(track.time_payload, time_payload)
        self.assertEqual(
            track.rotation_payload,
            quaternion_a + quaternion_b,
        )
        self.assertEqual(
            track.translation_payload,
            translation_payload,
        )
        self.assertEqual(track.scale_payload, scale_payload)
        self.assertEqual(track.time_values, (0.0, 2.0))
        self.assertEqual(
            sample_track_translation(track, 1.0),
            (0.0, 0.0, 2.25, 0.0),
        )
        self.assertEqual(
            sample_track_scale(track, 1.0),
            (1.5, 1.5, 1.5),
        )
        self.assertEqual(
            sample_track_rotation(track, 0.0),
            track.rotation_samples[0],
        )
        midpoint = sample_track_rotation(track, 1.0)
        self.assertTrue(all(math.isfinite(value) for value in midpoint))
        self.assertEqual(
            track.rotation_decode_status,
            "decoded-packed-u16-keyed-vu0-approximate-slerp",
        )

    def test_accepts_extended_sample_count(self) -> None:
        animation = parse_packed_animation(
            _block(
                [
                    _record(
                        time_codec=3,
                        sample_count=130,
                    )
                ]
            ),
            alignment=16,
        )
        track = animation.tracks[0]

        self.assertEqual(track.sample_count, 130)
        self.assertEqual(len(track.time_values), 130)
        self.assertAlmostEqual(track.time_values[-1], 129 / 6)

    def test_decodes_all_nine_time_modes(self) -> None:
        normalized = struct.pack("<f", 2.0) + struct.pack(
            ">3H", 0, 0x8000, 0xFFFF
        )
        explicit = bytes((0, 6, 12))
        variable = _variable_time_payload([1, 3, 9])
        records = [
            _record(time_codec=0),
            _record(
                time_codec=1,
                sample_count=3,
                time_payload=normalized,
            ),
            _record(
                time_codec=2,
                sample_count=3,
                time_payload=explicit,
            ),
            *[
                _record(time_codec=codec, sample_count=3)
                for codec in (3, 4, 5)
            ],
            *[
                _record(
                    time_codec=codec,
                    sample_count=4,
                    time_payload=variable,
                )
                for codec in (6, 7, 8)
            ],
        ]
        tracks = parse_packed_animation(
            _block(records),
            alignment=16,
        ).tracks

        self.assertEqual(tracks[0].time_values, (0.0,))
        self.assertAlmostEqual(tracks[1].time_values[1], 65536 / 65535)
        self.assertEqual(tracks[2].time_values, (0.0, 0.25, 0.5))
        self.assertEqual(tracks[3].time_values, (0.0, 1 / 6, 2 / 6))
        self.assertEqual(tracks[4].time_values, (0.0, 1 / 12, 2 / 12))
        self.assertEqual(tracks[5].time_values, (0.0, 1 / 24, 2 / 24))
        self.assertEqual(
            tracks[6].time_values,
            (0.0, 1 / 12, 4 / 12, 13 / 12),
        )
        self.assertEqual(
            tracks[7].time_values,
            (0.0, 1 / 24, 4 / 24, 13 / 24),
        )
        self.assertEqual(
            tracks[8].time_values,
            (0.0, 1 / 6, 4 / 6, 13 / 6),
        )
        self.assertEqual(
            select_animation_time(tracks[2], 0.375),
            type(select_animation_time(tracks[2], 0.375))(1, 2, 0.5),
        )

    def test_decodes_fixed_translation_formats(self) -> None:
        word_101010 = (
            768
            | (256 << 10)
            | (512 << 20)
        )
        sparse_value = -512
        sparse_word = ((sparse_value << 2) & 0xFFFF) | 2
        records = [
            _record(
                time_codec=0,
                translation_codec=1,
                translation_payload=_truncated_vector(
                    1.0, -2.0, 0.5
                ),
            ),
            _record(
                time_codec=0,
                translation_codec=4,
                translation_payload=struct.pack(
                    "<I", word_101010
                ),
            ),
            _record(
                time_codec=0,
                translation_codec=6,
                translation_payload=struct.pack(">H", sparse_word),
            ),
        ]
        tracks = parse_packed_animation(
            _block(records),
            alignment=16,
        ).tracks

        self.assertEqual(
            tracks[0].translation_samples,
            ((1.0, -2.0, 0.5, 0.0),),
        )
        self.assertEqual(
            tracks[1].translation_samples,
            ((0.25, -0.25, 0.0, 0.0),),
        )
        self.assertEqual(
            tracks[2].translation_samples,
            ((0.0, 0.0, -0.5, 0.0),),
        )

    def test_decodes_both_adaptive_translation_metadata_forms(self) -> None:
        descriptor = bytes((0x72,))
        x_metadata = struct.pack("<I", 2 | (2048 << 19))
        y_metadata = struct.pack("<I", 4096 << 16) + bytes((1,))
        z_metadata = (2048).to_bytes(2, "big")
        per_axis_payload = (
            descriptor
            + x_metadata
            + y_metadata
            + z_metadata
            + bytes((0x38,))
        )
        full_payload = (
            bytes((1,))
            + _truncated_vector(0.0, 0.0, 0.0)
            + _truncated_vector(1.0, 2.0, 4.0)
            + bytes((1, 2, 1))
            + bytes((0x0F,))
        )
        tracks = parse_packed_animation(
            _block(
                [
                    _record(
                        time_codec=3,
                        translation_codec=5,
                        sample_count=2,
                        translation_payload=per_axis_payload,
                    ),
                    _record(
                        time_codec=0,
                        translation_codec=5,
                        translation_payload=full_payload,
                    ),
                ]
            ),
            alignment=16,
        ).tracks

        self.assertEqual(
            tracks[0].translation_samples,
            (
                (-1.0, -8.0, -7.5, 0.0),
                (-0.625, -7.5, -7.5, 0.0),
            ),
        )
        self.assertEqual(
            tracks[1].translation_samples,
            ((0.5, 1.5, 2.0, 0.0),),
        )
        self.assertEqual(
            tuple(axis.width for axis in tracks[1].adaptive_translation_axes),
            (1, 2, 1),
        )

    def test_walks_all_rotation_payload_shapes(self) -> None:
        payloads = {
            0: b"",
            1: bytes(8),
            2: bytes(8),
            3: b"",
            4: struct.pack("<I", 0x3FFFFFFF),
            5: struct.pack("<I", 0xC1234567),
            6: b"",
            7: bytes(2),
            8: bytes(2),
            9: bytes((1,)) + bytes(7),
        }
        tracks = parse_packed_animation(
            _block(
                [
                    _record(
                        time_codec=0,
                        rotation_codec=codec,
                        rotation_payload=payload,
                    )
                    for codec, payload in payloads.items()
                ],
                alignment=64,
            ),
            alignment=64,
        ).tracks

        self.assertEqual(
            tuple(len(track.rotation_payload) for track in tracks),
            (0, 8, 8, 0, 4, 4, 0, 2, 2, 8),
        )
        self.assertEqual(
            tracks[4].rotation_words_101010,
            (0x3FFFFFFF,),
        )
        self.assertEqual(
            tracks[7].rotation_decode_status,
            "decoded-adaptive-reconstructed-component",
        )
        self.assertTrue(
            all(
                math.isfinite(value)
                for value in sample_track_rotation(tracks[4], 0.0)
            )
        )

    def test_decodes_101010_quaternions(self) -> None:
        identity_word = 511 | (511 << 10) | (511 << 20)
        normalized_word = 0x3FFFFFFF
        tracks = parse_packed_animation(
            _block(
                [
                    _record(
                        time_codec=3,
                        rotation_codec=4,
                        sample_count=2,
                        rotation_payload=struct.pack(
                            "<2I",
                            identity_word,
                            normalized_word,
                        ),
                    ),
                    _record(
                        time_codec=0,
                        rotation_codec=5,
                        rotation_payload=struct.pack(
                            "<I", identity_word
                        ),
                    ),
                ]
            ),
            alignment=16,
        ).tracks

        self.assertEqual(
            tracks[0].rotation_samples[0],
            (0.0, 0.0, 0.0, 1.0),
        )
        self.assertEqual(
            tracks[1].rotation_samples,
            ((0.0, 0.0, 0.0, 1.0),),
        )
        normalized = tracks[0].rotation_samples[1]
        self.assertAlmostEqual(normalized[0], 1 / math.sqrt(3), places=6)
        self.assertAlmostEqual(normalized[1], 1 / math.sqrt(3), places=6)
        self.assertAlmostEqual(normalized[2], 1 / math.sqrt(3), places=6)
        self.assertEqual(normalized[3], 0.0)
        midpoint = sample_track_rotation(tracks[0], 1 / 12)
        self.assertAlmostEqual(
            math.sqrt(sum(value * value for value in midpoint)),
            1.0,
            delta=0.001,
        )

    def test_decodes_adaptive_quaternion_variants(self) -> None:
        tracks = parse_packed_animation(
            _block(
                [
                    _record(
                        time_codec=3,
                        rotation_codec=7,
                        sample_count=2,
                        rotation_payload=_adaptive_rotation_payload(
                            codec=7,
                            samples=[
                                (0, 0, 0, 0),
                                (2, 0, 0, 1),
                            ],
                        ),
                    ),
                    _record(
                        time_codec=3,
                        rotation_codec=8,
                        sample_count=2,
                        rotation_payload=_adaptive_rotation_payload(
                            codec=8,
                            samples=[
                                (0, 0, 0, 0),
                                (2, 0, 0, 0),
                            ],
                        ),
                    ),
                ]
            ),
            alignment=16,
        ).tracks

        reconstructed = tracks[0].rotation_samples
        self.assertEqual(
            reconstructed[0],
            (0.0, 0.0, 0.0, 1.0),
        )
        self.assertGreater(reconstructed[1][0], 0.24)
        self.assertLess(reconstructed[1][3], -0.96)
        self.assertAlmostEqual(
            sum(value * value for value in reconstructed[1]),
            1.0,
            places=6,
        )

        euler = tracks[1].rotation_samples
        self.assertEqual(euler[0], (0.0, 0.0, 0.0, 1.0))
        self.assertAlmostEqual(euler[1][1], 0.0, places=6)
        self.assertAlmostEqual(euler[1][2], 0.0, places=6)
        self.assertAlmostEqual(euler[1][0], math.sin(math.pi / 8), places=4)
        self.assertAlmostEqual(euler[1][3], math.cos(math.pi / 8), places=4)
        self.assertTrue(
            all(
                math.isfinite(value)
                for value in sample_track_rotation(
                    tracks[1],
                    1 / 12,
                )
            )
        )

    def test_decodes_fixed_axis_variants_and_angle_wraps(self) -> None:
        lower_raw = 1820
        extent_raw = 61894
        tracks = parse_packed_animation(
            _block(
                [
                    _record(
                        time_codec=3,
                        rotation_codec=9,
                        sample_count=2,
                        rotation_payload=_fixed_axis_rotation_payload(
                            compact_axis=False,
                            codes=[0, 3],
                            lower_raw=lower_raw,
                            extent_raw=extent_raw,
                        ),
                    ),
                    _record(
                        time_codec=3,
                        rotation_codec=9,
                        sample_count=2,
                        rotation_payload=_fixed_axis_rotation_payload(
                            compact_axis=True,
                            codes=[3, 0],
                            lower_raw=lower_raw,
                            extent_raw=extent_raw,
                        ),
                    ),
                ]
            ),
            alignment=16,
        ).tracks

        for track in tracks:
            self.assertAlmostEqual(track.rotation_axis[0], 0.0)
            self.assertAlmostEqual(track.rotation_axis[1], 0.0)
            self.assertAlmostEqual(track.rotation_axis[2], 1.0)
            self.assertEqual(
                track.rotation_decode_status,
                "decoded-fixed-axis-adaptive-angle",
            )

        wrapped_positive = sample_track_rotation(tracks[0], 1 / 12)
        positive_angle = 2 * math.atan2(
            wrapped_positive[2],
            wrapped_positive[3],
        )
        self.assertAlmostEqual(
            math.degrees(positive_angle),
            137.5,
            delta=0.1,
        )

        fraction = 0.99
        wrapped_negative = sample_track_rotation(
            tracks[1],
            fraction / 6,
        )
        negative_angle = 2 * math.atan2(
            wrapped_negative[2],
            wrapped_negative[3],
        )
        lower, upper = tracks[1].rotation_angles
        expected = lower + (upper - lower + 2 * math.pi) * fraction
        if expected > math.pi:
            expected = 2 * math.pi - expected
        self.assertAlmostEqual(negative_angle, expected, places=5)

    def test_reproduces_vu0_quaternion_interpolation_paths(self) -> None:
        identity = (0.0, 0.0, 0.0, 1.0)
        self.assertEqual(
            interpolate_quaternion_vu0(
                identity,
                identity,
                0.25,
            ),
            identity,
        )
        self.assertEqual(
            interpolate_quaternion_vu0(
                identity,
                (0.0, 0.0, 0.0, -1.0),
                0.5,
            ),
            identity,
        )

        polynomial = interpolate_quaternion_vu0(
            identity,
            (0.0, 0.0, 1.0, 0.0),
            0.5,
        )
        self.assertAlmostEqual(
            polynomial[2],
            math.sqrt(0.5),
            delta=0.001,
        )
        self.assertAlmostEqual(
            polynomial[3],
            math.sqrt(0.5),
            delta=0.001,
        )

        unnormalized = interpolate_quaternion_vu0(
            identity,
            (0.0, 0.0, 0.0, 1.0001),
            0.5,
        )
        self.assertGreater(unnormalized[3], 1.0)
        self.assertAlmostEqual(unnormalized[3], 1.00005, places=6)

    def test_rejects_nonzero_padding_and_truncated_walk(self) -> None:
        valid = bytearray(
            _block([_record(time_codec=0)])
        )
        valid[-1] = 1
        with self.assertRaisesRegex(
            CoreFormatError, "padding is non-zero"
        ):
            parse_packed_animation(bytes(valid), alignment=16)

        damaged = bytearray(
            _block([_record(time_codec=0)])
        )
        struct.pack_into("<I", damaged, 4, 3)
        with self.assertRaisesRegex(
            CoreFormatError, "does not end"
        ):
            parse_packed_animation(bytes(damaged), alignment=16)

    def test_enforces_retail_codec_invariants(self) -> None:
        invalid_records = (
            (
                _record(time_codec=0, translation_codec=7),
                "translation codec 7",
            ),
            (
                _record(
                    time_codec=0,
                    rotation_codec=8,
                    rotation_payload=bytes((1, 0)),
                ),
                "descriptor bit zero",
            ),
            (
                _record(
                    time_codec=0,
                    rotation_codec=9,
                    rotation_payload=bytes(7),
                ),
                "range 1..12",
            ),
            (
                _record(
                    time_codec=0,
                    translation_codec=5,
                    translation_payload=(
                        bytes((1,))
                        + _truncated_vector(0.0, 0.0, 0.0)
                        + _truncated_vector(1.0, 1.0, 1.0)
                        + bytes((17, 0, 0))
                        + bytes(3)
                    ),
                ),
                "maximum 16",
            ),
            (
                _record(
                    time_codec=0,
                    translation_codec=6,
                    translation_payload=bytes((0, 3)),
                ),
                "W lane",
            ),
        )
        for record, message in invalid_records:
            with self.subTest(message=message):
                with self.assertRaisesRegex(CoreFormatError, message):
                    parse_packed_animation(
                        _block([record]),
                        alignment=16,
                    )

        with self.assertRaisesRegex(CoreFormatError, "range 1..120"):
            parse_packed_animation(_block([]), alignment=16)

    def test_loads_bone_map_clip_and_samples_direct_local_pose(self) -> None:
        core_data, corestream_data = _animation_library_fixture()
        library = parse_native_animation_library(
            core_data,
            corestream_data,
            core_path="fixtures/animation.core",
        )

        self.assertEqual(
            tuple(section.directory_kind for section in library.sections),
            ("bone-map", "expanded"),
        )
        self.assertEqual(len(library.bone_maps), 1)
        self.assertEqual(len(library.clips), 1)
        metadata = discover_native_animation_clips(library.sections)
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0].object_id, 0xA000)
        self.assertEqual(metadata[0].duration, 1.0)
        self.assertEqual(metadata[0].playback_mode, 2)
        clip = library.clips[0]
        self.assertEqual(clip.object_id, 0xA000)
        self.assertEqual(clip.storage, "inline")
        self.assertEqual(clip.playback_mode, 2)
        self.assertEqual(clip.duration, 1.0)
        self.assertTrue(clip.compensate_root)
        self.assertEqual(clip.total_distance, (2.0, 0.0, 0.0))
        self.assertEqual(clip.bone_map.track_names, ("root", "cHiLd"))
        self.assertEqual(clip.packed.track_count, 2)

        skeleton = parse_native_skeleton(
            _make_skeleton_blob(),
            object_id=0x4444,
            core_string_count=2,
        )
        binding = bind_native_animation(clip, skeleton)
        self.assertEqual(binding.matched_track_count, 2)
        self.assertEqual(binding.unmatched_track_names, ())

        pose = sample_native_animation_pose(
            clip,
            skeleton,
            binding,
            0.5,
        )
        child = pose.bones[1]
        self.assertEqual(child.translation, (0.5, 0.0, 1.0))
        self.assertAlmostEqual(child.global_matrix[3], 0.5)
        self.assertAlmostEqual(child.global_matrix[11], 1.0)
        self.assertEqual(
            native_animation_bake_times(clip, 2.0),
            (0.0, 0.5, 1.0),
        )

        animated_root = replace(
            skeleton,
            bones=(
                replace(skeleton.bones[0], category=1),
                skeleton.bones[1],
            ),
        )
        root_binding = bind_native_animation(clip, animated_root)
        trajectory = sample_native_animation_pose(
            clip,
            animated_root,
            root_binding,
            0.5,
            reconstruct_root_trajectory=True,
        )
        self.assertEqual(
            trajectory.bones[0].translation,
            (1.0, 0.0, 0.0),
        )
        held = sample_native_animation_pose(
            clip,
            animated_root,
            root_binding,
            2.0,
            reconstruct_root_trajectory=True,
        )
        self.assertEqual(held.sample_time, 1.0)
        self.assertEqual(held.bones[0].translation, (2.0, 0.0, 0.0))

        repeating = replace(clip, playback_mode=1)
        wrapped = sample_native_animation_pose(
            repeating,
            animated_root,
            root_binding,
            1.0,
            reconstruct_root_trajectory=True,
        )
        self.assertEqual(wrapped.sample_time, 0.0)
        self.assertEqual(wrapped.bones[0].translation, (0.0, 0.0, 0.0))

    def test_loads_streamed_animation_block(self) -> None:
        core_data, corestream_data = _animation_library_fixture(
            streamed=True
        )
        library = parse_native_animation_library(
            core_data,
            corestream_data,
            core_path="fixtures/streamed-animation.core",
        )

        self.assertEqual(len(library.clips), 1)
        self.assertEqual(library.clips[0].storage, "streamed")
        self.assertEqual(library.clips[0].packed.alignment, 64)
        self.assertEqual(library.clips[0].packed.track_count, 2)

    def test_loads_inline_animation_with_empty_stream_anchor(self) -> None:
        core_data, corestream_data = _animation_library_fixture(
            inline_stream_anchor=0x80,
        )
        library = parse_native_animation_library(
            core_data,
            corestream_data,
            core_path="fixtures/anchored-inline-animation.core",
        )

        clip = library.clips[0]
        self.assertEqual(clip.storage, "inline")
        self.assertEqual(clip.packed.alignment, 16)
        self.assertEqual(
            library.sections[1].objects[0].stream_offset,
            0x80,
        )
        self.assertEqual(
            library.sections[1].objects[0].stream_size,
            0,
        )

    def test_rejects_inline_animation_with_nonempty_stream_range(
        self,
    ) -> None:
        core_data, corestream_data = _animation_library_fixture(
            inline_stream_anchor=0x40,
            inline_stream_size=0x40,
        )

        with self.assertRaisesRegex(
            CoreFormatError,
            "non-empty CoreStream range",
        ):
            parse_native_animation_library(
                core_data,
                corestream_data,
                core_path="fixtures/invalid-inline-animation.core",
            )

    def test_parses_pose_mapping_and_scatters_source_order(self) -> None:
        entries, end = parse_pose_track_mapping(
            bytes((2, 1, 0, 0, 0xFF, 0x7E, 0xAA)),
            track_count=2,
        )

        self.assertEqual(end, 6)
        self.assertEqual(
            tuple(
                (entry.output_slot, entry.track_index)
                for entry in entries
            ),
            ((2, 1), (0, 0)),
        )
        self.assertEqual(
            scatter_category_records(
                ("category-0", "category-1", "category-2"),
                (2, 0, 1),
            ),
            ("category-1", "category-2", "category-0"),
        )
        with self.assertRaisesRegex(CoreFormatError, "duplicated"):
            parse_pose_track_mapping(
                bytes((0, 0, 0, 1, 0xFF, 0)),
                track_count=2,
            )
        with self.assertRaisesRegex(CoreFormatError, "outside"):
            parse_pose_track_mapping(
                bytes((0, 2, 0xFF, 0)),
                track_count=2,
            )


if __name__ == "__main__":
    unittest.main()
