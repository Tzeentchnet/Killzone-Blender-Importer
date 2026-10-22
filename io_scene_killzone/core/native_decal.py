# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import hashlib
import math
from pathlib import Path
import struct
from typing import Iterable

from .archive_source import KillzoneArchiveSource
from .errors import CoreFormatError
from .native_level import parse_native_core_sections


DECAL_LIBRARY_CORE_PATH = (
    "localcache.ps2/lumps/custom.materialeffects.core"
)
_CORE_STREAM_MAGIC = b"<CoreStreamData>"
_DECAL_RESOURCE_SIZE = 0x40
_I32 = struct.Struct("<i")
_U32 = struct.Struct("<I")
_F32 = struct.Struct("<f")


class NativeDecalSortMode(IntEnum):
    OFF = 0
    FRONT_TO_BACK = 1
    BACK_TO_FRONT = 2


@dataclass(frozen=True, slots=True)
class NativeDecalResource:
    object_id: int
    identity_words: tuple[int, int, int, int] | None
    sort_mode: NativeDecalSortMode
    sort_order: int
    max_decals: int
    decal_threshold: int
    random_rotation: bool
    min_size: float
    max_size: float
    fade_in_time: float
    fade_time: float
    clip_decal: bool
    material_token: int
    raw_blob: bytes


@dataclass(frozen=True, slots=True)
class NativeDecalLibrary:
    core_path: str
    resources: tuple[NativeDecalResource, ...]
    source_core_sha256: str
    source_corestream_sha256: str


@dataclass(frozen=True, slots=True)
class NativeDecalVertex2D:
    position: tuple[float, float]
    uv: tuple[float, float]


@dataclass(frozen=True, slots=True)
class NativeDecalDrawSpan:
    start: int
    count: int


def _read_bool32(blob: bytes, offset: int, label: str) -> bool:
    value = _U32.unpack_from(blob, offset)[0]
    if value not in (0, 1):
        raise CoreFormatError(
            f"Serialized DecalResource {label} is not a boolean"
        )
    return bool(value)


def _read_finite_f32(blob: bytes, offset: int, label: str) -> float:
    value = _F32.unpack_from(blob, offset)[0]
    if not math.isfinite(value):
        raise CoreFormatError(
            f"Serialized DecalResource {label} is not finite"
        )
    return value


def parse_serialized_decal_resource(
    blob: bytes,
    *,
    object_id: int,
    identity_words: tuple[int, int, int, int] | None = None,
) -> NativeDecalResource:
    if len(blob) != _DECAL_RESOURCE_SIZE:
        raise CoreFormatError(
            "Serialized DecalResource must be exactly 0x40 bytes"
        )

    raw_sort_mode = _I32.unpack_from(blob, 0x08)[0]
    try:
        sort_mode = NativeDecalSortMode(raw_sort_mode)
    except ValueError as exc:
        raise CoreFormatError(
            f"Serialized DecalResource has invalid sort mode "
            f"{raw_sort_mode}"
        ) from exc

    sort_order = _I32.unpack_from(blob, 0x0C)[0]
    if not 0 <= sort_order <= 15:
        raise CoreFormatError(
            "Serialized DecalResource sort order is outside 0..15"
        )
    max_decals = _I32.unpack_from(blob, 0x18)[0]
    decal_threshold = _I32.unpack_from(blob, 0x1C)[0]
    if max_decals < 0 or decal_threshold < 0:
        raise CoreFormatError(
            "Serialized DecalResource capacities must not be negative"
        )

    return NativeDecalResource(
        object_id=object_id,
        identity_words=identity_words,
        sort_mode=sort_mode,
        sort_order=sort_order,
        max_decals=max_decals,
        decal_threshold=decal_threshold,
        random_rotation=_read_bool32(
            blob, 0x20, "Random Rotation"
        ),
        min_size=_read_finite_f32(blob, 0x24, "Min Size"),
        max_size=_read_finite_f32(blob, 0x28, "Max Size"),
        fade_in_time=_read_finite_f32(
            blob, 0x2C, "Fade In Time"
        ),
        fade_time=_read_finite_f32(blob, 0x30, "Fade Time"),
        clip_decal=_read_bool32(blob, 0x34, "Clip Decal"),
        material_token=_U32.unpack_from(blob, 0x38)[0],
        raw_blob=bytes(blob),
    )


def parse_native_decal_library(
    core_data: bytes,
    corestream_data: bytes,
    *,
    core_path: str = DECAL_LIBRARY_CORE_PATH,
) -> NativeDecalLibrary:
    if len(corestream_data) < 0x40:
        raise CoreFormatError(
            "Native decal-library CoreStream header is truncated"
        )
    if corestream_data[: len(_CORE_STREAM_MAGIC)] != _CORE_STREAM_MAGIC:
        raise CoreFormatError(
            "Native decal-library CoreStream magic is invalid"
        )
    if any(corestream_data[len(_CORE_STREAM_MAGIC) : 0x40]):
        raise CoreFormatError(
            "Native decal-library CoreStream reserved header bytes "
            "are non-zero"
        )

    _, sections = parse_native_core_sections(
        core_data, len(corestream_data)
    )
    objects = tuple(
        obj
        for section in sections
        if section.type_name == "DecalResource"
        for obj in section.objects
    )
    if not objects:
        raise CoreFormatError(
            "Native decal library has no DecalResource section"
        )

    resources = tuple(
        parse_serialized_decal_resource(
            obj.blob,
            object_id=obj.object_id,
            identity_words=obj.identity_words,
        )
        for obj in objects
    )
    object_ids = tuple(resource.object_id for resource in resources)
    if len(set(object_ids)) != len(object_ids):
        raise CoreFormatError(
            "Native decal library has duplicate DecalResource IDs"
        )
    return NativeDecalLibrary(
        core_path=core_path,
        resources=resources,
        source_core_sha256=hashlib.sha256(core_data).hexdigest(),
        source_corestream_sha256=hashlib.sha256(
            corestream_data
        ).hexdigest(),
    )


def load_native_decal_library(
    source_path: str | Path,
    *,
    core_path: str = DECAL_LIBRARY_CORE_PATH,
) -> NativeDecalLibrary:
    normalized_path = core_path.casefold()
    with KillzoneArchiveSource(source_path) as source:
        catalog = source.load_catalog()
        entries_by_path = {
            entry.path.as_posix().casefold(): entry
            for entry in catalog.entries
        }
        entry = entries_by_path.get(normalized_path)
        if entry is None:
            raise CoreFormatError(
                f"Native decal library is not in the catalog: "
                f"{core_path}"
            )
        stream_path = entry.path.with_suffix(".corestream").as_posix()
        stream_entry = entries_by_path.get(stream_path.casefold())
        if stream_entry is None:
            raise CoreFormatError(
                f"{entry.path.as_posix()}: paired CoreStream is missing"
            )
        core_data = source.read_entry_range(entry, 0, entry.size)
        corestream_data = source.read_entry_range(
            stream_entry, 0, stream_entry.size
        )
    try:
        return parse_native_decal_library(
            core_data,
            corestream_data,
            core_path=entry.path.as_posix(),
        )
    except CoreFormatError as exc:
        raise CoreFormatError(f"{core_path}: {exc}") from exc


def _require_finite(value: float, label: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def _require_nonnegative_int(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def decal_size_and_angle(
    min_size: float,
    max_size: float,
    random_scalar: float,
) -> tuple[float, float]:
    min_size = _require_finite(min_size, "min_size")
    max_size = _require_finite(max_size, "max_size")
    random_scalar = _require_finite(
        random_scalar, "random_scalar"
    )
    if not 0.0 <= random_scalar <= 1.0:
        raise ValueError("random_scalar must be within 0..1")
    return (
        min_size + random_scalar * (max_size - min_size),
        random_scalar * (2.0 * math.pi),
    )


def decal_initial_opacity(fade_in_time: float) -> float:
    return 0.0 if fade_in_time > 0.0 else 1.0


def decal_provisional_budget(max_size: float) -> int:
    return 128 if max_size > 0.5 else 16


def decal_generated_vertex_span(
    *,
    clip_decal: bool,
    clipped_vertex_count: int | None = None,
) -> int:
    if not clip_decal:
        return 3
    if clipped_vertex_count is None:
        raise ValueError(
            "clipped_vertex_count is required for clipped decals"
        )
    _require_nonnegative_int(
        clipped_vertex_count, "clipped_vertex_count"
    )
    return max(3 * (clipped_vertex_count - 2), 0)


def decal_buffer_capacity(total_budget: int, max_decals: int) -> int:
    total_budget = _require_nonnegative_int(
        total_budget, "total_budget"
    )
    max_decals = _require_nonnegative_int(max_decals, "max_decals")
    grown = 3 * ((total_budget + total_budget // 2) // 3)
    return min(grown, 3 * max_decals)


def _projector_triangle(size: float) -> tuple[NativeDecalVertex2D, ...]:
    return (
        NativeDecalVertex2D((-size, -size), (0.0, 0.0)),
        NativeDecalVertex2D((3.0 * size, -size), (2.0, 0.0)),
        NativeDecalVertex2D((-size, 3.0 * size), (0.0, 2.0)),
    )


def unclipped_decal_triangle(
    size: float,
    *,
    swap_winding: bool = False,
) -> tuple[NativeDecalVertex2D, ...]:
    size = _require_finite(size, "size")
    if size < 0.0:
        raise ValueError("size must not be negative")
    vertices = _projector_triangle(size)
    if swap_winding:
        return vertices[0], vertices[2], vertices[1]
    return vertices


def clipped_decal_footprint(
    size: float,
) -> tuple[NativeDecalVertex2D, ...]:
    size = _require_finite(size, "size")
    if size < 0.0:
        raise ValueError("size must not be negative")
    if size < 0.2:
        return _projector_triangle(size)
    return (
        NativeDecalVertex2D((-size, -size), (0.0, 0.0)),
        NativeDecalVertex2D((size, -size), (1.0, 0.0)),
        NativeDecalVertex2D((size, size), (1.0, 1.0)),
        NativeDecalVertex2D((-size, size), (0.0, 1.0)),
    )


def decal_candidate_is_accepted(
    *,
    candidate_tag: int,
    mark_tag: int,
    direction_dot_normal: float,
    plane_distance: float,
    size: float,
) -> bool:
    if candidate_tag != mark_tag or direction_dot_normal < -0.1:
        return False
    return not (size >= 0.5 and plane_distance < -0.5)


def decal_fade_prefix_count(
    mark_spans: Iterable[int],
    *,
    total_instance_vertices: int,
    decal_threshold: int,
) -> int:
    total_instance_vertices = _require_nonnegative_int(
        total_instance_vertices, "total_instance_vertices"
    )
    decal_threshold = _require_nonnegative_int(
        decal_threshold, "decal_threshold"
    )
    excess = max(
        total_instance_vertices - 3 * decal_threshold, 0
    )
    if excess == 0:
        return 0

    charged = 0
    count = 0
    for span in mark_spans:
        span = _require_nonnegative_int(span, "mark span")
        charged += span
        count += 1
        if charged >= excess:
            break
    return count


def step_decal_fade_out(
    opacity: float,
    *,
    delta_time: float,
    fade_time: float,
) -> float:
    opacity = _require_finite(opacity, "opacity")
    delta_time = _require_finite(delta_time, "delta_time")
    fade_time = _require_finite(fade_time, "fade_time")
    if delta_time < 0.0:
        raise ValueError("delta_time must not be negative")
    if fade_time <= 0.0:
        return 0.0
    return min(max(opacity - delta_time / fade_time, 0.0), 1.0)


def step_decal_fade_in(
    opacity: float,
    *,
    delta_time: float,
    fade_in_time: float,
) -> float:
    opacity = _require_finite(opacity, "opacity")
    delta_time = _require_finite(delta_time, "delta_time")
    fade_in_time = _require_finite(fade_in_time, "fade_in_time")
    if delta_time < 0.0:
        raise ValueError("delta_time must not be negative")
    if fade_in_time <= 0.0:
        return 1.0
    return min(max(opacity + delta_time / fade_in_time, 0.0), 1.0)


def decal_vertex_rgba_with_opacity(rgba: int, opacity: float) -> int:
    if (
        isinstance(rgba, bool)
        or not isinstance(rgba, int)
        or not 0 <= rgba <= 0xFFFFFFFF
    ):
        raise ValueError("rgba must be an unsigned 32-bit integer")
    opacity = _require_finite(opacity, "opacity")
    alpha = int(min(max(opacity, 0.0), 1.0) * 255.0)
    return (rgba & 0x00FFFFFF) | (alpha << 24)


def decal_ring_draw_spans(
    *,
    first: int,
    cursor: int,
    limit: int,
) -> tuple[NativeDecalDrawSpan, ...]:
    first = _require_nonnegative_int(first, "first")
    cursor = _require_nonnegative_int(cursor, "cursor")
    limit = _require_nonnegative_int(limit, "limit")
    if limit == 0 or first >= limit or cursor >= limit:
        raise ValueError(
            "first and cursor must be inside a non-empty ring"
        )
    if first < cursor:
        return (NativeDecalDrawSpan(first, cursor - first),)
    spans = (
        NativeDecalDrawSpan(first, limit - first),
        NativeDecalDrawSpan(0, cursor),
    )
    return tuple(span for span in spans if span.count)


def decal_queue_bucket_order(
) -> tuple[tuple[int, NativeDecalSortMode], ...]:
    return tuple(
        (sort_order, sort_mode)
        for sort_order in range(16)
        for sort_mode in NativeDecalSortMode
    )


def decal_queue_key(
    *,
    bounds_min: tuple[float, float, float],
    bounds_max: tuple[float, float, float],
    camera_position: tuple[float, float, float],
    camera_forward: tuple[float, float, float],
) -> float:
    values = bounds_min + bounds_max + camera_position + camera_forward
    if not all(math.isfinite(value) for value in values):
        raise ValueError("queue-key vectors must be finite")
    center = tuple(
        0.5 * (minimum + maximum)
        for minimum, maximum in zip(bounds_min, bounds_max)
    )
    delta = tuple(
        value - camera
        for value, camera in zip(center, camera_position)
    )
    distance_squared = sum(value * value for value in delta)
    forward_distance = sum(
        forward * value
        for forward, value in zip(camera_forward, delta)
    )
    return (
        -distance_squared
        if forward_distance < 0.0
        else distance_squared
    )


def pack_decal_sort_descriptor(
    *,
    sort_mode: NativeDecalSortMode,
    sort_order: int,
    enabled: bool,
    material_flags: int = 0,
) -> int:
    try:
        sort_mode = NativeDecalSortMode(sort_mode)
    except ValueError as exc:
        raise ValueError("sort_mode is invalid") from exc
    if (
        isinstance(sort_order, bool)
        or not isinstance(sort_order, int)
        or not 0 <= sort_order <= 15
    ):
        raise ValueError("sort_order must be within 0..15")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    if (
        isinstance(material_flags, bool)
        or not isinstance(material_flags, int)
        or not 0 <= material_flags <= 0xFFFFFFFF
        or material_flags & 0x7F
    ):
        raise ValueError(
            "material_flags must contain only descriptor bits 7..31"
        )
    return (
        int(sort_mode)
        | (sort_order << 2)
        | (int(enabled) << 6)
        | material_flags
    )
