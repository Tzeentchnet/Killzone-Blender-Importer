# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math
import struct
from typing import Sequence, TypeVar

from .errors import (
    AnimationDecodeUnavailableError,
    CoreFormatError,
)


_U32 = struct.Struct("<I")
_F32 = struct.Struct("<f")
_PACKED_U16_QUATERNION_SCALE = _F32.unpack(
    _U32.pack(0x38000101)
)[0]
_PACKED_101010_QUATERNION_SCALE = _F32.unpack(
    _U32.pack(0x3B004020)
)[0]
_ADAPTIVE_QUATERNION_ENDPOINT_SCALE = _F32.unpack(
    _U32.pack(0x38000100)
)[0]
_ALIGNED_U16_FRACTION_SCALE = _F32.unpack(
    _U32.pack(0x37800080)
)[0]
_ANGLE_U16_SCALE = _F32.unpack(_U32.pack(0x38C910A4))[0]
_QUATERNION_LINEAR_THRESHOLD = _F32.unpack(
    _U32.pack(0x3F7FFF58)
)[0]
_ACOS_COEFFICIENTS = (
    _F32.unpack(_U32.pack(0x3D981627))[0],
    _F32.unpack(_U32.pack(0xBC996E30))[0],
    _F32.unpack(_U32.pack(0xBE593484))[0],
    _F32.unpack(_U32.pack(0x3FC90DA4))[0],
)
_SIN_COEFFICIENTS = (
    -0.1666666716337204,
    0.008333331905305386,
    -0.00019840900495182723,
    0.0000027526000394573202,
    -0.000000023900000201138027,
)
_CSC_COEFFICIENTS = (
    0.1666666716337204,
    0.01944444514811039,
    0.0020502645056694746,
    0.00020998677064199,
    0.00002133604539267253,
)
_FIXED_AXIS_LOOKUP = (
    -1.0,
    _F32.unpack(_U32.pack(0xBF3504F7))[0],
    -0.5,
    0.0,
    0.5,
    _F32.unpack(_U32.pack(0x3F3504F7))[0],
    1.0,
    1.0,
)

POSE_CATEGORY_STRIDES = (0x10, 0x20, 0x30)

Vector3 = tuple[float, float, float]
Vector4 = tuple[float, float, float, float]
Quaternion = tuple[float, float, float, float]

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class NativeAdaptiveTranslationAxis:
    lower: float
    upper: float
    width: int


@dataclass(frozen=True, slots=True)
class NativeAnimationTimeWindow:
    lower_index: int
    upper_index: int
    fraction: float


@dataclass(frozen=True, slots=True)
class NativePackedTrack:
    index: int
    offset: int
    end_offset: int
    sample_count: int
    time_codec: int
    translation_codec: int
    rotation_codec: int
    scale_codec: int
    time_payload: bytes
    rotation_payload: bytes
    translation_payload: bytes
    scale_payload: bytes
    time_values: tuple[float, ...]
    duration: float
    rotation_samples: tuple[Quaternion, ...] | None
    rotation_words_101010: tuple[int, ...]
    rotation_angles: tuple[float, ...]
    rotation_axis: Vector3 | None
    rotation_decode_status: str
    translation_samples: tuple[Vector4, ...] | None
    adaptive_translation_axes: (
        tuple[
            NativeAdaptiveTranslationAxis,
            NativeAdaptiveTranslationAxis,
            NativeAdaptiveTranslationAxis,
        ]
        | None
    )
    translation_decode_status: str
    scale_samples: tuple[Vector3, ...]
    scale_decode_status: str
    raw_record: bytes


@dataclass(frozen=True, slots=True)
class NativePackedAnimation:
    track_count: int
    packed_byte_size: int
    alignment: int
    tracks: tuple[NativePackedTrack, ...]
    zero_padding: bytes
    raw_block: bytes


@dataclass(frozen=True, slots=True)
class NativePoseTrackMapping:
    output_slot: int
    track_index: int


def _require_range(
    data: bytes,
    offset: int,
    size: int,
    label: str,
    *,
    limit: int | None = None,
) -> None:
    end = len(data) if limit is None else limit
    if (
        offset < 0
        or size < 0
        or end < 0
        or end > len(data)
        or offset > end
        or size > end - offset
    ):
        raise CoreFormatError(f"{label} is truncated")


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def _float_from_bits(bits: int) -> float:
    return _F32.unpack(_U32.pack(bits & 0xFFFFFFFF))[0]


def _f32(value: float) -> float:
    return _F32.unpack(_F32.pack(value))[0]


def _f32_add(left: float, right: float) -> float:
    return _f32(_f32(left) + _f32(right))


def _f32_sub(left: float, right: float) -> float:
    return _f32(_f32(left) - _f32(right))


def _f32_mul(left: float, right: float) -> float:
    return _f32(_f32(left) * _f32(right))


def _f32_sqrt(value: float) -> float:
    return _f32(math.sqrt(max(_f32(value), 0.0)))


def _normalize_vector3(vector: Vector3) -> Vector3:
    length_squared = _f32_add(
        _f32_add(
            _f32_mul(vector[0], vector[0]),
            _f32_mul(vector[1], vector[1]),
        ),
        _f32_mul(vector[2], vector[2]),
    )
    if length_squared == 0.0:
        raise CoreFormatError(
            "Fixed-axis quaternion has a zero-length axis"
        )
    length = _f32_sqrt(length_squared)
    return tuple(_f32(value / length) for value in vector)


def _axis_angle_quaternion(axis: Vector3, angle: float) -> Quaternion:
    half_angle = _f32_mul(angle, 0.5)
    sine = _f32(math.sin(half_angle))
    cosine = _f32(math.cos(half_angle))
    return (
        _f32_mul(axis[0], sine),
        _f32_mul(axis[1], sine),
        _f32_mul(axis[2], sine),
        cosine,
    )


def _decode_truncated_vector3(payload: bytes) -> Vector3:
    if len(payload) != 8:
        raise CoreFormatError(
            "Mantissa-truncated animation vector is truncated"
        )
    packed = int.from_bytes(payload, "little")
    values = (
        _float_from_bits(((packed >> 43) & 0x1FFFFF) << 11),
        _float_from_bits(((packed >> 22) & 0x1FFFFF) << 11),
        _float_from_bits((packed & 0x3FFFFF) << 10),
    )
    if not all(math.isfinite(value) for value in values):
        raise CoreFormatError(
            "Mantissa-truncated animation vector is not finite"
        )
    return values


def _decode_101010_vector(word: int) -> Vector3:
    return (
        ((word & 0x3FF) - 512) / 1024.0,
        (((word >> 10) & 0x3FF) - 512) / 1024.0,
        (((word >> 20) & 0x3FF) - 512) / 1024.0,
    )


def _read_lsb_bits(
    data: bytes,
    bit_offset: int,
    width: int,
    label: str,
) -> tuple[int, int]:
    if width < 0:
        raise CoreFormatError(f"{label} has a negative bit width")
    if width == 0:
        return 0, bit_offset
    if bit_offset < 0 or width > len(data) * 8 - bit_offset:
        raise CoreFormatError(f"{label} bitstream is truncated")
    value = 0
    for bit_index in range(width):
        source_bit = bit_offset + bit_index
        value |= (
            (data[source_bit >> 3] >> (source_bit & 7)) & 1
        ) << bit_index
    return value, bit_offset + width


def _decode_time_payload(
    codec: int,
    sample_count: int,
    payload: bytes,
) -> tuple[tuple[float, ...], float]:
    if codec == 0:
        return (0.0,), 0.0
    if codec == 1:
        duration = _F32.unpack_from(payload)[0]
        if not math.isfinite(duration) or duration < 0.0:
            raise CoreFormatError(
                "Normalized animation time duration is invalid"
            )
        values = tuple(
            duration
            * int.from_bytes(
                payload[4 + index * 2 : 6 + index * 2],
                "big",
            )
            / 65535.0
            for index in range(sample_count)
        )
    elif codec == 2:
        values = tuple(
            value / 24.0 for value in payload[:sample_count]
        )
        duration = 255.0 / 24.0
    elif codec in (3, 4, 5):
        rate = (6.0, 12.0, 24.0)[codec - 3]
        values = tuple(
            index / rate for index in range(sample_count)
        )
        duration = values[-1]
    elif codec in (6, 7, 8):
        if not payload or payload[0] != len(payload):
            raise CoreFormatError(
                "Variable animation time payload length is invalid"
            )
        rate = (12.0, 24.0, 6.0)[codec - 6]
        ticks = 0
        tick_values = [0]
        bit_offset = 0
        bitstream = payload[1:]
        for _ in range(sample_count - 1):
            extended, bit_offset = _read_lsb_bits(
                bitstream,
                bit_offset,
                1,
                "Variable animation time",
            )
            if extended:
                extra, bit_offset = _read_lsb_bits(
                    bitstream,
                    bit_offset,
                    3,
                    "Variable animation time",
                )
                delta = 2 + extra
            else:
                delta = 1
            ticks += delta
            tick_values.append(ticks)
        values = tuple(value / rate for value in tick_values)
        duration = values[-1]
    else:
        raise CoreFormatError(
            f"Animation track has unknown time codec {codec}"
        )
    if any(
        current < previous
        for previous, current in zip(values, values[1:])
    ):
        raise CoreFormatError(
            "Animation key times are not nondecreasing"
        )
    return values, duration


def _time_payload_size(
    data: bytes,
    offset: int,
    limit: int,
    codec: int,
    sample_count: int,
) -> int:
    if codec == 0 or codec in (3, 4, 5):
        return 0
    if codec == 1:
        return 4 + 2 * sample_count
    if codec == 2:
        return sample_count
    if codec in (6, 7, 8):
        _require_range(
            data,
            offset,
            1,
            "Variable animation time payload",
            limit=limit,
        )
        size = data[offset]
        if size == 0:
            raise CoreFormatError(
                "Variable animation time payload has zero size"
            )
        return size
    raise CoreFormatError(
        f"Animation track has unknown time codec {codec}"
    )


def _rotation_payload_size(
    data: bytes,
    offset: int,
    limit: int,
    codec: int,
    sample_count: int,
) -> int:
    if codec in (0, 3, 6):
        return 0
    if codec == 1:
        return 8
    if codec == 2:
        return 8 * sample_count
    if codec == 4:
        return 4 * sample_count
    if codec == 5:
        return 4
    if codec in (7, 8):
        _require_range(
            data,
            offset,
            2,
            "Adaptive quaternion header",
            limit=limit,
        )
        h0, h1 = data[offset : offset + 2]
        if codec == 8 and h0 & 1:
            raise CoreFormatError(
                "Adaptive three-angle quaternion descriptor bit zero "
                "is set"
            )
        sample_bits = (
            (h0 >> 4)
            + (h1 & 0x0F)
            + (h1 >> 4)
            + (h0 & 1)
        )
        size = 2 + (sample_bits * sample_count + 7) // 8
        if h0 & 0x02:
            size += 2
            if h1 & 0xF0:
                size += 2
        if h0 & 0x04:
            size += 2
            if h1 & 0x0F:
                size += 2
        if h0 & 0x08:
            size += 2
            if h0 & 0xF0:
                size += 2
        return size
    if codec == 9:
        _require_range(
            data,
            offset,
            1,
            "Alternate adaptive quaternion header",
            limit=limit,
        )
        h0 = data[offset]
        width = h0 & 0x0F
        if not 1 <= width <= 12:
            raise CoreFormatError(
                "Fixed-axis quaternion width is outside the retail "
                "range 1..12"
            )
        return (
            (width * sample_count + 7) // 8
            + (6 if h0 & 0x10 else 7)
        )
    raise CoreFormatError(
        f"Animation track has unknown rotation codec {codec}"
    )


def _decode_packed_u16_quaternion(payload: bytes) -> Quaternion:
    if len(payload) != 8:
        raise CoreFormatError("Packed-u16 quaternion is truncated")
    packed = struct.unpack("<4H", payload)
    return tuple(
        -1.0 + value * _PACKED_U16_QUATERNION_SCALE
        for value in packed
    )


def _decode_101010_quaternion(word: int) -> Quaternion:
    components = tuple(
        _f32_sub(
            _f32_mul(
                float((word >> shift) & 0x3FF),
                _PACKED_101010_QUATERNION_SCALE,
            ),
            1.0,
        )
        for shift in (0, 10, 20)
    )
    magnitude_squared = _f32_add(
        _f32_add(
            _f32_mul(components[0], components[0]),
            _f32_mul(components[1], components[1]),
        ),
        _f32_mul(components[2], components[2]),
    )
    if magnitude_squared < 1.0:
        return (
            components[0],
            components[1],
            components[2],
            _f32_sqrt(_f32_sub(1.0, magnitude_squared)),
        )
    length = _f32_sqrt(magnitude_squared)
    return (
        _f32(components[0] / length),
        _f32(components[1] / length),
        _f32(components[2] / length),
        0.0,
    )


def _decode_adaptive_quaternion_metadata(
    payload: bytes,
) -> tuple[
    int,
    tuple[int, int, int],
    tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
    ],
]:
    h0, h1 = payload[:2]
    widths = (h1 >> 4, h1 & 0x0F, h0 >> 4)
    present = (h0 & 0x02, h0 & 0x04, h0 & 0x08)
    cursor = 2
    bounds: list[tuple[float, float]] = []
    for is_present, width in zip(present, widths):
        if not is_present:
            bounds.append((0.0, 0.0))
            continue
        lower = _f32_mul(
            float(
                int.from_bytes(
                    payload[cursor : cursor + 2],
                    "big",
                    signed=True,
                )
            ),
            _ADAPTIVE_QUATERNION_ENDPOINT_SCALE,
        )
        cursor += 2
        if width:
            upper = _f32_mul(
                float(
                    int.from_bytes(
                        payload[cursor : cursor + 2],
                        "big",
                        signed=True,
                    )
                ),
                _ADAPTIVE_QUATERNION_ENDPOINT_SCALE,
            )
            cursor += 2
        else:
            upper = lower
        bounds.append((lower, upper))
    return cursor, widths, (bounds[0], bounds[1], bounds[2])


def _dequantize_adaptive_component(
    lower: float,
    upper: float,
    width: int,
    code: int,
) -> float:
    if width == 0:
        return lower
    aligned = code << (16 - width)
    fraction = _f32_mul(
        float(aligned),
        _ALIGNED_U16_FRACTION_SCALE,
    )
    return _f32_add(
        lower,
        _f32_mul(_f32_sub(upper, lower), fraction),
    )


def _decode_adaptive_quaternions(
    codec: int,
    sample_count: int,
    payload: bytes,
) -> tuple[Quaternion, ...]:
    h0 = payload[0]
    bitstream_offset, widths, bounds = (
        _decode_adaptive_quaternion_metadata(payload)
    )
    bitstream = payload[bitstream_offset:]
    bit_offset = 0
    samples: list[Quaternion] = []
    for _ in range(sample_count):
        z_code, bit_offset = _read_lsb_bits(
            bitstream,
            bit_offset,
            widths[2],
            "Adaptive quaternion",
        )
        y_code, bit_offset = _read_lsb_bits(
            bitstream,
            bit_offset,
            widths[1],
            "Adaptive quaternion",
        )
        x_code, bit_offset = _read_lsb_bits(
            bitstream,
            bit_offset,
            widths[0],
            "Adaptive quaternion",
        )
        values = (
            _dequantize_adaptive_component(
                *bounds[0], widths[0], x_code
            ),
            _dequantize_adaptive_component(
                *bounds[1], widths[1], y_code
            ),
            _dequantize_adaptive_component(
                *bounds[2], widths[2], z_code
            ),
        )
        if codec == 7:
            sign_bit = 0
            if h0 & 1:
                sign_bit, bit_offset = _read_lsb_bits(
                    bitstream,
                    bit_offset,
                    1,
                    "Adaptive quaternion sign",
                )
            magnitude_squared = _f32_add(
                _f32_add(
                    _f32_mul(values[0], values[0]),
                    _f32_mul(values[1], values[1]),
                ),
                _f32_mul(values[2], values[2]),
            )
            w = _f32_sqrt(
                max(_f32_sub(1.0, magnitude_squared), 0.0)
            )
            if sign_bit:
                w = _f32(-w)
            samples.append((*values, w))
            continue

        angles = tuple(
            _f32_mul(value, _f32(math.pi))
            for value in values
        )
        half_x = _f32_mul(angles[0], 0.5)
        half_y = _f32_mul(angles[1], 0.5)
        half_z = _f32_mul(angles[2], 0.5)
        sx, cx = _f32(math.sin(half_x)), _f32(math.cos(half_x))
        sy, cy = _f32(math.sin(half_y)), _f32(math.cos(half_y))
        sz, cz = _f32(math.sin(half_z)), _f32(math.cos(half_z))
        samples.append(
            (
                _f32_sub(
                    _f32_mul(_f32_mul(sx, cy), cz),
                    _f32_mul(_f32_mul(cx, sy), sz),
                ),
                _f32_add(
                    _f32_mul(_f32_mul(cx, sy), cz),
                    _f32_mul(_f32_mul(sx, cy), sz),
                ),
                _f32_sub(
                    _f32_mul(_f32_mul(cx, cy), sz),
                    _f32_mul(_f32_mul(sx, sy), cz),
                ),
                _f32_add(
                    _f32_mul(_f32_mul(cx, cy), cz),
                    _f32_mul(_f32_mul(sx, sy), sz),
                ),
            )
        )
    return tuple(samples)


def _decode_fixed_axis(
    payload: bytes,
) -> tuple[Vector3, int]:
    descriptor = payload[0]
    if descriptor & 0x10:
        code = payload[1]
        x = _FIXED_AXIS_LOOKUP[code & 7]
        y = _FIXED_AXIS_LOOKUP[(code >> 3) & 7]
        z = _f32_sqrt(
            max(
                _f32_sub(
                    1.0,
                    _f32_add(
                        _f32_mul(x, x),
                        _f32_mul(y, y),
                    ),
                ),
                0.0,
            )
        )
        if code & 0x40:
            z = _f32(-z)
        return (x, y, z), 2

    code = int.from_bytes(payload[1:3], "big")
    x = (code >> 7) & 0x3F
    y = code & 0x7F
    if x + y >= 127:
        x = 127 - x
        y = 127 - y
    z = 126 - (x + y)
    if code & 0x8000:
        x = -x
    if code & 0x4000:
        y = -y
    if code & 0x2000:
        z = -z
    return _normalize_vector3(
        (float(x), float(y), float(z))
    ), 3


def _decode_fixed_axis_rotation(
    sample_count: int,
    payload: bytes,
) -> tuple[Vector3, tuple[float, ...], tuple[Quaternion, ...]]:
    descriptor = payload[0]
    width = descriptor & 0x0F
    axis, metadata_offset = _decode_fixed_axis(payload)
    extent_raw, lower_raw = struct.unpack_from(
        "<2H",
        payload,
        metadata_offset,
    )
    extent = _f32_mul(float(extent_raw), _ANGLE_U16_SCALE)
    lower = _f32_sub(
        _f32_mul(float(lower_raw), _ANGLE_U16_SCALE),
        _f32(math.pi),
    )
    bitstream = payload[metadata_offset + 4 :]
    angles: list[float] = []
    quaternions: list[Quaternion] = []
    bit_offset = 0
    for _ in range(sample_count):
        code, bit_offset = _read_lsb_bits(
            bitstream,
            bit_offset,
            width,
            "Fixed-axis quaternion angle",
        )
        aligned = code << (16 - width) if width else 0
        fraction = _f32_mul(
            float(aligned),
            _ALIGNED_U16_FRACTION_SCALE,
        )
        angle = _f32_add(
            lower,
            _f32_mul(extent, fraction),
        )
        angles.append(angle)
        quaternions.append(_axis_angle_quaternion(axis, angle))
    return axis, tuple(angles), tuple(quaternions)


def _decode_rotation(
    codec: int,
    sample_count: int,
    payload: bytes,
) -> tuple[
    tuple[Quaternion, ...] | None,
    tuple[int, ...],
    tuple[float, ...],
    Vector3 | None,
    str,
]:
    if codec in (0, 3, 6):
        return (
            ((0.0, 0.0, 0.0, 1.0),),
            (),
            (),
            None,
            "identity",
        )
    if codec == 1:
        return (
            (_decode_packed_u16_quaternion(payload),),
            (),
            (),
            None,
            "decoded-packed-u16-constant",
        )
    if codec == 2:
        return (
            tuple(
                _decode_packed_u16_quaternion(
                    payload[index * 8 : index * 8 + 8]
                )
                for index in range(sample_count)
            ),
            (),
            (),
            None,
            "decoded-packed-u16-keyed-vu0-approximate-slerp",
        )
    if codec in (4, 5):
        words = struct.unpack(
            f"<{len(payload) // 4}I",
            payload,
        )
        return (
            tuple(
                _decode_101010_quaternion(word) for word in words
            ),
            words,
            (),
            None,
            (
                "decoded-101010-keyed-vu0-approximate-slerp"
                if codec == 4
                else "decoded-101010-constant"
            ),
        )
    if codec in (7, 8):
        return (
            _decode_adaptive_quaternions(
                codec,
                sample_count,
                payload,
            ),
            (),
            (),
            None,
            (
                "decoded-adaptive-reconstructed-component"
                if codec == 7
                else "decoded-adaptive-three-angle"
            ),
        )
    axis, angles, samples = _decode_fixed_axis_rotation(
        sample_count,
        payload,
    )
    return (
        samples,
        (),
        angles,
        axis,
        "decoded-fixed-axis-adaptive-angle",
    )


def _parse_adaptive_translation_layout(
    data: bytes,
    offset: int,
    limit: int,
    sample_count: int,
) -> tuple[
    int,
    tuple[
        NativeAdaptiveTranslationAxis,
        NativeAdaptiveTranslationAxis,
        NativeAdaptiveTranslationAxis,
    ],
    int,
]:
    _require_range(
        data,
        offset,
        1,
        "Adaptive translation descriptor",
        limit=limit,
    )
    descriptor = data[offset]
    cursor = offset + 1
    axes: list[NativeAdaptiveTranslationAxis] = []
    if descriptor & 1:
        _require_range(
            data,
            cursor,
            19,
            "Adaptive translation full metadata",
            limit=limit,
        )
        lower = _decode_truncated_vector3(data[cursor : cursor + 8])
        upper = _decode_truncated_vector3(
            data[cursor + 8 : cursor + 16]
        )
        widths = data[cursor + 16 : cursor + 19]
        cursor += 19
        axes.extend(
            NativeAdaptiveTranslationAxis(
                lower[index],
                upper[index],
                widths[index],
            )
            for index in range(3)
        )
    else:
        for axis_index, shift in enumerate((1, 3, 5)):
            mode = (descriptor >> shift) & 3
            label = (
                f"Adaptive translation axis {axis_index} metadata"
            )
            if mode == 0:
                axes.append(
                    NativeAdaptiveTranslationAxis(0.0, 0.0, 0)
                )
            elif mode == 1:
                _require_range(
                    data,
                    cursor,
                    4,
                    label,
                    limit=limit,
                )
                raw = _U32.unpack_from(data, cursor)[0]
                cursor += 4
                axes.append(
                    NativeAdaptiveTranslationAxis(
                        ((raw >> 6) & 0x1FFF) / 4096.0 - 1.0,
                        (raw >> 19) / 4096.0 - 1.0,
                        raw & 0x3F,
                    )
                )
            elif mode == 2:
                _require_range(
                    data,
                    cursor,
                    5,
                    label,
                    limit=limit,
                )
                raw = _U32.unpack_from(data, cursor)[0]
                width = data[cursor + 4]
                cursor += 5
                axes.append(
                    NativeAdaptiveTranslationAxis(
                        (raw & 0xFFFF) / 4096.0 - 8.0,
                        (raw >> 16) / 4096.0 - 8.0,
                        width,
                    )
                )
            else:
                _require_range(
                    data,
                    cursor,
                    2,
                    label,
                    limit=limit,
                )
                raw = int.from_bytes(
                    data[cursor : cursor + 2],
                    "big",
                )
                cursor += 2
                value = raw / 4096.0 - 8.0
                axes.append(
                    NativeAdaptiveTranslationAxis(value, value, 0)
                )
    axis_tuple = (axes[0], axes[1], axes[2])
    if any(axis.width > 16 for axis in axis_tuple):
        raise CoreFormatError(
            "Adaptive translation axis width exceeds the retail maximum 16"
        )
    sample_bits = sum(axis.width for axis in axis_tuple)
    if any(
        ((index * sample_bits) & 7) + sample_bits > 54
        for index in range(sample_count)
    ):
        raise CoreFormatError(
            "Adaptive translation sample exceeds the retail 54-bit window"
        )
    bitstream_size = (sample_bits * sample_count + 7) // 8
    _require_range(
        data,
        cursor,
        bitstream_size,
        "Adaptive translation bitstream",
        limit=limit,
    )
    return cursor + bitstream_size - offset, axis_tuple, cursor - offset


def _translation_payload_layout(
    data: bytes,
    offset: int,
    limit: int,
    codec: int,
    sample_count: int,
) -> tuple[
    int,
    tuple[
        NativeAdaptiveTranslationAxis,
        NativeAdaptiveTranslationAxis,
        NativeAdaptiveTranslationAxis,
    ]
    | None,
    int,
]:
    if codec == 0:
        return 0, None, 0
    if codec == 1:
        return 8, None, 0
    if codec == 2:
        return 8 * sample_count, None, 0
    if codec == 3:
        return 4 * sample_count, None, 0
    if codec == 4:
        return 4, None, 0
    if codec == 5:
        return _parse_adaptive_translation_layout(
            data,
            offset,
            limit,
            sample_count,
        )
    if codec == 6:
        return 2, None, 0
    raise CoreFormatError(
        f"Animation track has unknown translation codec {codec}"
    )


def _decode_adaptive_translation(
    payload: bytes,
    sample_count: int,
    axes: tuple[
        NativeAdaptiveTranslationAxis,
        NativeAdaptiveTranslationAxis,
        NativeAdaptiveTranslationAxis,
    ],
    bitstream_offset: int,
) -> tuple[Vector4, ...]:
    bitstream = payload[bitstream_offset:]
    bit_offset = 0
    samples: list[Vector4] = []
    x_axis, y_axis, z_axis = axes
    for _ in range(sample_count):
        z_code, bit_offset = _read_lsb_bits(
            bitstream,
            bit_offset,
            z_axis.width,
            "Adaptive translation",
        )
        y_code, bit_offset = _read_lsb_bits(
            bitstream,
            bit_offset,
            y_axis.width,
            "Adaptive translation",
        )
        x_code, bit_offset = _read_lsb_bits(
            bitstream,
            bit_offset,
            x_axis.width,
            "Adaptive translation",
        )

        def dequantize(
            axis: NativeAdaptiveTranslationAxis,
            code: int,
        ) -> float:
            return (
                axis.lower
                + (axis.upper - axis.lower)
                * code
                / float(1 << axis.width)
            )

        samples.append(
            (
                dequantize(x_axis, x_code),
                dequantize(y_axis, y_code),
                dequantize(z_axis, z_code),
                0.0,
            )
        )
    return tuple(samples)


def _decode_translation(
    codec: int,
    sample_count: int,
    payload: bytes,
    axes: tuple[
        NativeAdaptiveTranslationAxis,
        NativeAdaptiveTranslationAxis,
        NativeAdaptiveTranslationAxis,
    ]
    | None,
    bitstream_offset: int,
) -> tuple[tuple[Vector4, ...] | None, str]:
    if codec == 0:
        return ((0.0, 0.0, 0.0, 0.0),), "default-zero"
    if codec in (1, 2):
        count = 1 if codec == 1 else sample_count
        samples = tuple(
            (*_decode_truncated_vector3(
                payload[index * 8 : index * 8 + 8]
            ), 0.0)
            for index in range(count)
        )
        return (
            samples,
            "mantissa-truncated-constant"
            if codec == 1
            else "mantissa-truncated-keyed",
        )
    if codec in (3, 4):
        count = sample_count if codec == 3 else 1
        words = struct.unpack(f"<{count}I", payload)
        return (
            tuple((*_decode_101010_vector(word), 0.0) for word in words),
            "fixed-101010-keyed"
            if codec == 3
            else "fixed-101010-constant",
        )
    if codec == 5:
        if axes is None:
            raise CoreFormatError(
                "Adaptive translation metadata is missing"
            )
        return (
            _decode_adaptive_translation(
                payload,
                sample_count,
                axes,
                bitstream_offset,
            ),
            "adaptive-keyed",
        )
    if codec == 6:
        packed_unsigned = int.from_bytes(payload, "big")
        packed_signed = int.from_bytes(
            payload,
            "big",
            signed=True,
        )
        lane = packed_unsigned & 3
        if lane == 3:
            raise CoreFormatError(
                "Sparse translation selects the unvalidated W lane"
            )
        value = (packed_signed >> 2) / 1024.0
        vector = [0.0, 0.0, 0.0, 0.0]
        vector[lane] = value
        return (tuple(vector),), "sparse-signed-14-constant"
    return None, "runtime-unhandled"


def _scale_payload_size(codec: int, sample_count: int) -> int:
    if codec in (0, 3):
        return 0
    if codec == 1:
        return 8
    if codec == 2:
        return 8 * sample_count
    raise CoreFormatError(
        f"Animation track has unknown scale codec {codec}"
    )


def _decode_scale(
    codec: int,
    sample_count: int,
    payload: bytes,
) -> tuple[tuple[Vector3, ...], str]:
    if codec in (0, 3):
        return ((1.0, 1.0, 1.0),), "default-unit"
    count = 1 if codec == 1 else sample_count
    samples = tuple(
        _decode_truncated_vector3(
            payload[index * 8 : index * 8 + 8]
        )
        for index in range(count)
    )
    return (
        samples,
        "mantissa-truncated-constant"
        if codec == 1
        else "mantissa-truncated-keyed",
    )


def _parse_track(
    data: bytes,
    offset: int,
    limit: int,
    index: int,
) -> NativePackedTrack:
    _require_range(
        data,
        offset,
        2,
        f"Animation track {index} header",
        limit=limit,
    )
    byte0, byte1 = data[offset : offset + 2]
    time_codec = byte0 & 0x0F
    translation_codec = (byte0 >> 4) & 7
    rotation_codec = byte1 & 0x0F
    scale_codec = byte1 >> 6
    if time_codec > 8:
        raise CoreFormatError(
            f"Animation track {index} has unknown time codec "
            f"{time_codec}"
        )
    if rotation_codec > 9:
        raise CoreFormatError(
            f"Animation track {index} has unknown rotation codec "
            f"{rotation_codec}"
        )
    if translation_codec > 6:
        raise CoreFormatError(
            f"Animation track {index} has unknown translation codec "
            f"{translation_codec}"
        )

    cursor = offset + 2
    if time_codec == 0:
        sample_count = 1
    else:
        _require_range(
            data,
            cursor,
            1,
            f"Animation track {index} sample count",
            limit=limit,
        )
        first = data[cursor]
        cursor += 1
        if first & 0x80:
            _require_range(
                data,
                cursor,
                1,
                f"Animation track {index} extended sample count",
                limit=limit,
            )
            sample_count = (first & 0x7F) | (data[cursor] << 7)
            cursor += 1
        else:
            sample_count = first
        if sample_count == 0:
            raise CoreFormatError(
                f"Animation track {index} has zero samples"
            )

    time_size = _time_payload_size(
        data,
        cursor,
        limit,
        time_codec,
        sample_count,
    )
    _require_range(
        data,
        cursor,
        time_size,
        f"Animation track {index} time payload",
        limit=limit,
    )
    time_payload = data[cursor : cursor + time_size]
    cursor += time_size

    rotation_size = _rotation_payload_size(
        data,
        cursor,
        limit,
        rotation_codec,
        sample_count,
    )
    _require_range(
        data,
        cursor,
        rotation_size,
        f"Animation track {index} rotation payload",
        limit=limit,
    )
    rotation_payload = data[cursor : cursor + rotation_size]
    cursor += rotation_size

    (
        translation_size,
        adaptive_axes,
        adaptive_bitstream_offset,
    ) = _translation_payload_layout(
        data,
        cursor,
        limit,
        translation_codec,
        sample_count,
    )
    _require_range(
        data,
        cursor,
        translation_size,
        f"Animation track {index} translation payload",
        limit=limit,
    )
    translation_payload = data[cursor : cursor + translation_size]
    cursor += translation_size

    scale_size = _scale_payload_size(
        scale_codec,
        sample_count,
    )
    _require_range(
        data,
        cursor,
        scale_size,
        f"Animation track {index} scale payload",
        limit=limit,
    )
    scale_payload = data[cursor : cursor + scale_size]
    cursor += scale_size

    time_values, duration = _decode_time_payload(
        time_codec,
        sample_count,
        time_payload,
    )
    (
        rotation_samples,
        rotation_words,
        rotation_angles,
        rotation_axis,
        rotation_status,
    ) = _decode_rotation(
        rotation_codec,
        sample_count,
        rotation_payload,
    )
    translation_samples, translation_status = _decode_translation(
        translation_codec,
        sample_count,
        translation_payload,
        adaptive_axes,
        adaptive_bitstream_offset,
    )
    scale_samples, scale_status = _decode_scale(
        scale_codec,
        sample_count,
        scale_payload,
    )
    decoded_values = (
        tuple(value for sample in rotation_samples for value in sample)
        if rotation_samples is not None
        else ()
    ) + tuple(rotation_angles)
    if rotation_axis is not None:
        decoded_values += tuple(rotation_axis)
    if translation_samples is not None:
        decoded_values += tuple(
            value for sample in translation_samples for value in sample
        )
    decoded_values += tuple(
        value for sample in scale_samples for value in sample
    )
    decoded_values += tuple(time_values)
    if not all(math.isfinite(value) for value in decoded_values):
        raise CoreFormatError(
            f"Animation track {index} decodes to a non-finite value"
        )
    return NativePackedTrack(
        index=index,
        offset=offset,
        end_offset=cursor,
        sample_count=sample_count,
        time_codec=time_codec,
        translation_codec=translation_codec,
        rotation_codec=rotation_codec,
        scale_codec=scale_codec,
        time_payload=time_payload,
        rotation_payload=rotation_payload,
        translation_payload=translation_payload,
        scale_payload=scale_payload,
        time_values=time_values,
        duration=duration,
        rotation_samples=rotation_samples,
        rotation_words_101010=rotation_words,
        rotation_angles=rotation_angles,
        rotation_axis=rotation_axis,
        rotation_decode_status=rotation_status,
        translation_samples=translation_samples,
        adaptive_translation_axes=adaptive_axes,
        translation_decode_status=translation_status,
        scale_samples=scale_samples,
        scale_decode_status=scale_status,
        raw_record=data[offset:cursor],
    )


def parse_packed_animation(
    data: bytes,
    *,
    alignment: int,
) -> NativePackedAnimation:
    if alignment not in (16, 64):
        raise ValueError(
            "Packed animation alignment must be 16 or 64"
        )
    _require_range(data, 0, 8, "Packed animation header")
    track_count, packed_byte_size = struct.unpack_from("<2I", data)
    if not 1 <= track_count <= 120:
        raise CoreFormatError(
            "Packed animation track count is outside the retail range 1..120"
        )
    expected_size = _align(8 + packed_byte_size, alignment)
    if len(data) != expected_size:
        raise CoreFormatError(
            f"Packed animation block is {len(data)} bytes, "
            f"expected {expected_size}"
        )
    packed_end = 8 + packed_byte_size
    if track_count > packed_byte_size // 2:
        raise CoreFormatError(
            "Packed animation track count cannot fit its byte block"
        )
    tracks: list[NativePackedTrack] = []
    cursor = 8
    for index in range(track_count):
        try:
            track = _parse_track(data, cursor, packed_end, index)
        except CoreFormatError as exc:
            selectors = ""
            if cursor <= packed_end - 2:
                byte0, byte1 = data[cursor : cursor + 2]
                selectors = (
                    f" (T={byte0 & 0x0F},"
                    f"R={byte1 & 0x0F},"
                    f"P={(byte0 >> 4) & 7},"
                    f"S={byte1 >> 6})"
                )
            raise CoreFormatError(
                f"Animation track {index} at packed offset "
                f"0x{cursor:X}{selectors}: {exc}"
            ) from exc
        tracks.append(track)
        cursor = track.end_offset
    if cursor != packed_end:
        raise CoreFormatError(
            "Packed animation track walk does not end at the "
            "declared byte size"
        )
    zero_padding = data[packed_end:]
    if any(zero_padding):
        raise CoreFormatError(
            "Packed animation alignment padding is non-zero"
        )
    return NativePackedAnimation(
        track_count=track_count,
        packed_byte_size=packed_byte_size,
        alignment=alignment,
        tracks=tuple(tracks),
        zero_padding=zero_padding,
        raw_block=data,
    )


def select_animation_time(
    track: NativePackedTrack,
    time: float,
) -> NativeAnimationTimeWindow:
    if not math.isfinite(time):
        raise ValueError("Animation sample time must be finite")
    values = track.time_values
    if len(values) == 1 or time <= values[0]:
        return NativeAnimationTimeWindow(0, 0, 0.0)
    last = len(values) - 1
    if time >= values[-1]:
        return NativeAnimationTimeWindow(last, last, 0.0)
    upper = bisect_right(values, time)
    lower = upper - 1
    span = values[upper] - values[lower]
    fraction = 0.0 if span == 0.0 else (
        time - values[lower]
    ) / span
    return NativeAnimationTimeWindow(lower, upper, fraction)


def _interpolate(
    lower: Sequence[float],
    upper: Sequence[float],
    fraction: float,
) -> tuple[float, ...]:
    return tuple(
        start + (end - start) * fraction
        for start, end in zip(lower, upper)
    )


def _sin_approximation(value: float) -> float:
    squared = _f32_mul(value, value)
    s1, s2, s3, s4, s5 = (
        _f32(coefficient)
        for coefficient in _SIN_COEFFICIENTS
    )
    polynomial = _f32_add(s4, _f32_mul(s5, squared))
    polynomial = _f32_add(s3, _f32_mul(squared, polynomial))
    polynomial = _f32_add(s2, _f32_mul(squared, polynomial))
    polynomial = _f32_add(s1, _f32_mul(squared, polynomial))
    polynomial = _f32_add(1.0, _f32_mul(squared, polynomial))
    return _f32_mul(value, polynomial)


def _cosecant_approximation(value: float) -> float:
    squared = _f32_mul(value, value)
    c0, c1, c2, c3, c4 = (
        _f32(coefficient)
        for coefficient in _CSC_COEFFICIENTS
    )
    polynomial = _f32_add(c3, _f32_mul(c4, squared))
    polynomial = _f32_add(c2, _f32_mul(squared, polynomial))
    polynomial = _f32_add(c1, _f32_mul(squared, polynomial))
    polynomial = _f32_add(c0, _f32_mul(squared, polynomial))
    correction = _f32_mul(
        value,
        polynomial,
    )
    return _f32_add(_f32(1.0 / value), correction)


def interpolate_quaternion_vu0(
    lower: Quaternion,
    upper: Quaternion,
    fraction: float,
) -> Quaternion:
    if not 0.0 <= fraction <= 1.0 or not all(
        math.isfinite(value)
        for value in (*lower, *upper, fraction)
    ):
        raise ValueError(
            "Quaternion interpolation inputs must be finite with "
            "fraction in 0..1"
        )
    lower_f32 = tuple(_f32(value) for value in lower)
    upper_f32 = tuple(_f32(value) for value in upper)
    fraction_f32 = _f32(fraction)
    dot = _f32_add(
        _f32_add(
            _f32_mul(lower_f32[0], upper_f32[0]),
            _f32_mul(lower_f32[1], upper_f32[1]),
        ),
        _f32_add(
            _f32_mul(lower_f32[2], upper_f32[2]),
            _f32_mul(lower_f32[3], upper_f32[3]),
        ),
    )
    absolute_dot = _f32(abs(dot))
    sign = -1.0 if dot < 0.0 else 1.0
    one_minus_fraction = _f32_sub(1.0, fraction_f32)
    if absolute_dot >= _QUATERNION_LINEAR_THRESHOLD:
        signed_fraction = _f32_mul(sign, fraction_f32)
        return tuple(
            _f32_add(
                _f32_mul(one_minus_fraction, left),
                _f32_mul(signed_fraction, right),
            )
            for left, right in zip(lower_f32, upper_f32)
        )

    a0, a1, a2, a3 = _ACOS_COEFFICIENTS
    polynomial = _f32_add(
        a0,
        _f32_mul(a1, absolute_dot),
    )
    polynomial = _f32_add(
        a2,
        _f32_mul(absolute_dot, polynomial),
    )
    polynomial = _f32_add(
        a3,
        _f32_mul(absolute_dot, polynomial),
    )
    theta = _f32_mul(
        _f32_sqrt(_f32_sub(1.0, absolute_dot)),
        polynomial,
    )
    inverse_sine = _cosecant_approximation(theta)
    lower_weight = _f32_mul(
        _sin_approximation(
            _f32_mul(one_minus_fraction, theta)
        ),
        inverse_sine,
    )
    upper_weight = _f32_mul(
        sign,
        _f32_mul(
            _sin_approximation(
                _f32_mul(fraction_f32, theta)
            ),
            inverse_sine,
        ),
    )
    return tuple(
        _f32_add(
            _f32_mul(lower_weight, left),
            _f32_mul(upper_weight, right),
        )
        for left, right in zip(lower_f32, upper_f32)
    )


def _interpolate_fixed_axis_angle(
    lower: float,
    upper: float,
    fraction: float,
) -> float:
    pi = _f32(math.pi)
    two_pi = _f32(2.0 * math.pi)
    delta = _f32_sub(upper, lower)
    if delta > pi:
        angle = _f32_add(
            lower,
            _f32_mul(
                _f32_sub(delta, two_pi),
                fraction,
            ),
        )
        if angle < -pi:
            angle = _f32_add(angle, two_pi)
        return angle
    if delta < -pi:
        angle = _f32_add(
            lower,
            _f32_mul(
                _f32_add(delta, two_pi),
                fraction,
            ),
        )
        if angle > pi:
            angle = _f32_sub(two_pi, angle)
        return angle
    return _f32_add(lower, _f32_mul(delta, fraction))


def sample_track_translation(
    track: NativePackedTrack,
    time: float,
) -> Vector4:
    samples = track.translation_samples
    if samples is None:
        raise AnimationDecodeUnavailableError(
            f"Translation codec {track.translation_codec} is not "
            "handled by the observed runtime sampler"
        )
    if len(samples) == 1:
        return samples[0]
    window = select_animation_time(track, time)
    return _interpolate(
        samples[window.lower_index],
        samples[window.upper_index],
        window.fraction,
    )


def sample_track_scale(
    track: NativePackedTrack,
    time: float,
) -> Vector3:
    samples = track.scale_samples
    if len(samples) == 1:
        return samples[0]
    window = select_animation_time(track, time)
    return _interpolate(
        samples[window.lower_index],
        samples[window.upper_index],
        window.fraction,
    )


def sample_track_rotation(
    track: NativePackedTrack,
    time: float,
) -> Quaternion:
    samples = track.rotation_samples
    if samples is None:
        raise AnimationDecodeUnavailableError(
            f"Rotation codec {track.rotation_codec} requires unresolved "
            "quaternion reconstruction"
        )
    if len(samples) == 1:
        return samples[0]
    window = select_animation_time(track, time)
    if window.lower_index == window.upper_index:
        return samples[window.lower_index]
    if track.rotation_codec == 9:
        if track.rotation_axis is None:
            raise CoreFormatError(
                "Fixed-axis rotation track has no decoded axis"
            )
        angle = _interpolate_fixed_axis_angle(
            track.rotation_angles[window.lower_index],
            track.rotation_angles[window.upper_index],
            window.fraction,
        )
        return _axis_angle_quaternion(track.rotation_axis, angle)
    return interpolate_quaternion_vu0(
        samples[window.lower_index],
        samples[window.upper_index],
        window.fraction,
    )


def parse_pose_track_mapping(
    data: bytes,
    *,
    track_count: int,
    offset: int = 0,
) -> tuple[tuple[NativePoseTrackMapping, ...], int]:
    if track_count < 0 or track_count > 256:
        raise ValueError("Pose mapping track count must be 0..256")
    entries: list[NativePoseTrackMapping] = []
    output_slots: set[int] = set()
    cursor = offset
    while True:
        _require_range(data, cursor, 2, "Pose track mapping")
        output_slot, track_index = data[cursor : cursor + 2]
        cursor += 2
        if output_slot == 0xFF:
            return tuple(entries), cursor
        if track_index >= track_count:
            raise CoreFormatError(
                f"Pose mapping track index {track_index} is outside "
                f"{track_count} tracks"
            )
        if output_slot in output_slots:
            raise CoreFormatError(
                f"Pose mapping output slot {output_slot} is duplicated"
            )
        output_slots.add(output_slot)
        entries.append(
            NativePoseTrackMapping(
                output_slot=output_slot,
                track_index=track_index,
            )
        )


def scatter_category_records(
    category_records: Sequence[_T],
    category_order: Sequence[int],
) -> tuple[_T, ...]:
    if len(category_records) != len(category_order):
        raise ValueError(
            "Category records and category order must have equal length"
        )
    count = len(category_order)
    if sorted(category_order) != list(range(count)):
        raise ValueError(
            "Category order must be a permutation of source-bone indices"
        )
    if count == 0:
        return ()
    output = [category_records[0]] * count
    for category_index, source_index in enumerate(category_order):
        output[source_index] = category_records[category_index]
    return tuple(output)
