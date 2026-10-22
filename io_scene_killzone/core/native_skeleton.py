# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import math
import struct

from .errors import CoreFormatError

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")

_PACKED_HEADER_SIZE = 0x40
_TEMPORARY_TRANSFORM_SIZE = 0x30
_BOUNDS_SIZE = 0x18
_AUXILIARY_SIZE = 0x50
_BONE_RECORD_SIZE = 0x20
_MAX_BONES = 255
_MAX_HULLS = 1_000_000


@dataclass(frozen=True, slots=True)
class NativeSkeletonBone:
    index: int
    name: str
    name_offset: int
    parent_index: int | None
    parent_name: str
    parent_name_offset: int
    hull_name: str
    hull_name_offset: int
    hull_slot: int | None
    category: int
    category_flags: int
    unknown_vector: tuple[float, float, float]
    temporary_transform: tuple[float, ...]
    candidate_bounds: tuple[float, ...]
    serialized_transform_word: int
    word_a: int
    category_ordinal: int

    @property
    def static_translation(self) -> tuple[float, float, float]:
        return self.unknown_vector


@dataclass(frozen=True, slots=True)
class NativeSkeletonAttachment:
    index: int
    name: str
    name_offset: int
    owner_bone_index: int
    affine_matrix: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class NativeSkeleton:
    object_id: int
    name_string_index: int
    bone_set_string_index: int
    hull_tokens: tuple[int, ...]
    generic_fields: bytes
    custom_words: tuple[int, int, int, int]
    custom_alignment: bytes
    stored_size: int
    runtime_size: int
    archive_marker: int
    archive_transient: int
    category_summary: tuple[int, ...]
    padded_name_pool_size: int
    used_name_pool_size: int
    bones: tuple[NativeSkeletonBone, ...]
    attachments: tuple[NativeSkeletonAttachment, ...]
    words_b: tuple[int, ...]
    category_order_a: tuple[int, ...]
    category_order_b: tuple[int, ...]
    name_pool: bytes
    blob_tail: bytes

    @property
    def bone_count(self) -> int:
        return len(self.bones)


def _require_range(data: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise CoreFormatError(f"{label} is truncated")


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def _determinant3(values: tuple[float, ...]) -> float:
    return (
        values[0] * (values[4] * values[8] - values[5] * values[7])
        - values[1] * (values[3] * values[8] - values[5] * values[6])
        + values[2] * (values[3] * values[7] - values[4] * values[6])
    )


def _read_name(
    pool: bytes,
    used_size: int,
    offset: int,
    label: str,
) -> str:
    if offset >= used_size:
        raise CoreFormatError(f"{label} offset is outside the used name pool")
    end = pool.find(b"\0", offset, used_size)
    if end < 0:
        raise CoreFormatError(f"{label} is not null-terminated")
    try:
        return pool[offset:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CoreFormatError(f"{label} is not valid UTF-8") from exc


def _runtime_size(
    bone_count: int,
    attachment_count: int,
    name_pool_size: int,
) -> int:
    cursor = _align(_PACKED_HEADER_SIZE, 0x40)
    cursor += bone_count * _BOUNDS_SIZE
    cursor = _align(cursor, 0x10)
    cursor += attachment_count * _AUXILIARY_SIZE
    cursor = _align(cursor, 0x10)
    cursor += bone_count * _BONE_RECORD_SIZE
    cursor = _align(cursor, 4)
    cursor += bone_count * 4
    cursor = _align(cursor, 4)
    cursor += bone_count * 4
    cursor += bone_count * 4
    cursor += name_pool_size
    return cursor


def parse_native_skeleton(
    blob: bytes,
    *,
    object_id: int,
    core_string_count: int | None = None,
) -> NativeSkeleton:
    _require_range(blob, 0, 0x14, "Skeleton outer header")
    hull_count = _U32.unpack_from(blob, 0)[0]
    if hull_count > _MAX_HULLS:
        raise CoreFormatError(f"Unsupported Skeleton hull count {hull_count}")
    name_string_index = _U32.unpack_from(blob, 4)[0]
    if (
        core_string_count is not None
        and name_string_index >= core_string_count
    ):
        raise CoreFormatError("Skeleton name has an invalid Core string index")
    generic_fields = blob[8:0x14]

    hull_offset = 0x14
    _require_range(
        blob,
        hull_offset,
        hull_count * 4 + 20,
        "Skeleton hull and custom arrays",
    )
    hull_tokens = struct.unpack_from(
        f"<{hull_count}I", blob, hull_offset
    ) if hull_count else ()
    bone_set_offset = hull_offset + hull_count * 4
    bone_set_string_index = _U32.unpack_from(blob, bone_set_offset)[0]
    if (
        core_string_count is not None
        and bone_set_string_index >= core_string_count
    ):
        raise CoreFormatError(
            "Skeleton bone-set text has an invalid Core string index"
        )
    custom_offset = bone_set_offset + 4
    custom_words = struct.unpack_from("<4I", blob, custom_offset)
    block_offset = _align(custom_offset + 16, 0x10)
    custom_alignment = blob[custom_offset + 16 : block_offset]

    _require_range(
        blob,
        block_offset,
        _PACKED_HEADER_SIZE,
        "Skeleton packed header",
    )
    stored_size, runtime_size = struct.unpack_from("<2H", blob, block_offset)
    archive_marker = blob[block_offset + 4]
    archive_transient = blob[block_offset + 5]
    attachment_count = blob[block_offset + 6]
    bone_count = blob[block_offset + 7]
    if not 1 <= bone_count <= _MAX_BONES:
        raise CoreFormatError(
            f"Unsupported Skeleton bone count {bone_count}"
        )
    category_summary = tuple(blob[block_offset + 8 : block_offset + 14])
    if any(blob[block_offset + 14 : block_offset + 16]):
        raise CoreFormatError("Skeleton packed header reserved bytes are non-zero")
    padded_name_pool_size, used_name_pool_size = struct.unpack_from(
        "<2H", blob, block_offset + 0x10
    )
    if padded_name_pool_size != _align(used_name_pool_size, 4):
        raise CoreFormatError("Skeleton name-pool sizes are inconsistent")
    if used_name_pool_size == 0:
        raise CoreFormatError("Skeleton name pool is empty")
    if archive_marker != 1 or archive_transient != 0:
        raise CoreFormatError("Skeleton archive marker fields are invalid")
    if any(blob[block_offset + 0x14 : block_offset + 0x40]):
        raise CoreFormatError("Skeleton runtime pointer slots are non-zero")
    if stored_size < _PACKED_HEADER_SIZE:
        raise CoreFormatError("Skeleton stored block size is too small")
    _require_range(
        blob,
        block_offset,
        stored_size,
        "Skeleton packed block",
    )

    relative = _PACKED_HEADER_SIZE
    transform_offset = block_offset + relative
    relative += bone_count * _TEMPORARY_TRANSFORM_SIZE
    relative = _align(relative, 0x40)
    bounds_offset = block_offset + relative
    relative += bone_count * _BOUNDS_SIZE
    relative = _align(relative, 0x10)
    attachment_offset = block_offset + relative
    relative += attachment_count * _AUXILIARY_SIZE
    relative = _align(relative, 0x10)
    bone_record_offset = block_offset + relative
    relative += bone_count * _BONE_RECORD_SIZE
    relative = _align(relative, 4)
    words_a_offset = block_offset + relative
    relative += bone_count * 4
    relative = _align(relative, 4)
    words_b_offset = block_offset + relative
    relative += bone_count * 4
    bytes_a_offset = block_offset + relative
    relative += bone_count
    bytes_b_offset = block_offset + relative
    relative += bone_count
    parent_offset = block_offset + relative
    relative += bone_count
    bytes_d_offset = block_offset + relative
    relative += bone_count
    name_pool_offset = block_offset + relative
    relative += padded_name_pool_size
    if relative != stored_size:
        raise CoreFormatError(
            "Skeleton packed arrays do not equal the stored block size"
        )
    expected_runtime_size = _runtime_size(
        bone_count,
        attachment_count,
        padded_name_pool_size,
    )
    if runtime_size != expected_runtime_size:
        raise CoreFormatError(
            "Skeleton packed arrays do not equal the runtime block size"
        )

    block_end = block_offset + stored_size
    blob_tail = blob[block_end:]
    if len(blob) != _align(block_end, 0x10) or any(blob_tail):
        raise CoreFormatError("Skeleton has an invalid aligned object tail")

    transforms: list[tuple[float, ...]] = []
    bounds: list[tuple[float, ...]] = []
    for index in range(bone_count):
        transform = struct.unpack_from(
            "<12f",
            blob,
            transform_offset + index * _TEMPORARY_TRANSFORM_SIZE,
        )
        if not all(math.isfinite(value) for value in transform):
            raise CoreFormatError(
                f"Skeleton bone {index} transform is not finite"
            )
        if abs(_determinant3(transform[:9])) < 1.0e-12:
            raise CoreFormatError(
                f"Skeleton bone {index} transform is singular"
            )
        transforms.append(transform)

        candidate_bounds = struct.unpack_from(
            "<6f",
            blob,
            bounds_offset + index * _BOUNDS_SIZE,
        )
        if not all(math.isfinite(value) for value in candidate_bounds):
            raise CoreFormatError(
                f"Skeleton bone {index} bounds are not finite"
            )
        bounds.append(candidate_bounds)

    name_pool = blob[
        name_pool_offset : name_pool_offset + padded_name_pool_size
    ]
    parent_indices = tuple(
        blob[parent_offset + index] for index in range(bone_count)
    )
    if parent_indices[0] != 0xFF:
        raise CoreFormatError("Skeleton root parent is not 0xFF")
    if any(
        parent_indices[index] >= index
        for index in range(1, bone_count)
    ):
        raise CoreFormatError(
            "Skeleton parent table is not source-order acyclic"
        )

    words_a = struct.unpack_from(
        f"<{bone_count}I", blob, words_a_offset
    )
    words_b = struct.unpack_from(
        f"<{bone_count}I", blob, words_b_offset
    )
    bytes_a = tuple(blob[bytes_a_offset : bytes_a_offset + bone_count])
    bytes_b = tuple(blob[bytes_b_offset : bytes_b_offset + bone_count])
    bytes_d = tuple(blob[bytes_d_offset : bytes_d_offset + bone_count])

    raw_records: list[
        tuple[
            tuple[float, float, float],
            int,
            int,
            int,
            int,
            int,
        ]
    ] = []
    names: list[str] = []
    parent_names: list[str] = []
    hull_names: list[str] = []
    categories: list[int] = []
    for index in range(bone_count):
        offset = bone_record_offset + index * _BONE_RECORD_SIZE
        unknown_vector = struct.unpack_from("<3f", blob, offset)
        if not all(math.isfinite(value) for value in unknown_vector):
            raise CoreFormatError(
                f"Skeleton bone {index} vector is not finite"
            )
        name_offset, parent_name_offset, hull_name_offset = (
            struct.unpack_from("<3I", blob, offset + 0x0C)
        )
        transform_word, category_flags = struct.unpack_from(
            "<2I", blob, offset + 0x18
        )
        name = _read_name(
            name_pool,
            used_name_pool_size,
            name_offset,
            f"Skeleton bone {index} name",
        )
        if not name:
            raise CoreFormatError(f"Skeleton bone {index} has an empty name")
        parent_name = _read_name(
            name_pool,
            used_name_pool_size,
            parent_name_offset,
            f"Skeleton bone {index} parent name",
        )
        hull_name = _read_name(
            name_pool,
            used_name_pool_size,
            hull_name_offset,
            f"Skeleton bone {index} hull name",
        )
        category = category_flags & 0xF
        if category > 2:
            raise CoreFormatError(
                f"Skeleton bone {index} has unsupported category {category}"
            )
        raw_records.append(
            (
                unknown_vector,
                name_offset,
                parent_name_offset,
                hull_name_offset,
                transform_word,
                category_flags,
            )
        )
        names.append(name)
        parent_names.append(parent_name)
        hull_names.append(hull_name)
        categories.append(category)

    if parent_names[0]:
        raise CoreFormatError("Skeleton root has a non-empty parent name")
    for index in range(1, bone_count):
        if parent_names[index] != names[parent_indices[index]]:
            raise CoreFormatError(
                f"Skeleton bone {index} parent name disagrees with its index"
            )

    category_counts = tuple(categories.count(index) for index in range(3))
    expected_summary = (
        0,
        category_counts[0],
        category_counts[0],
        category_counts[1],
        category_counts[0] + category_counts[1],
        category_counts[2],
    )
    if category_summary != expected_summary:
        raise CoreFormatError("Skeleton category summary is inconsistent")

    category_order = tuple(
        sorted(range(bone_count), key=lambda index: (categories[index], index))
    )
    if bytes_b != category_order or bytes_d != category_order:
        raise CoreFormatError("Skeleton category-order byte tables disagree")
    expected_ordinals: list[int] = []
    seen_by_category = [0, 0, 0]
    for category in categories:
        expected_ordinals.append(seen_by_category[category])
        seen_by_category[category] += 1
    if bytes_a != tuple(expected_ordinals):
        raise CoreFormatError("Skeleton category ordinal table is inconsistent")
    if tuple(words_a[index] for index in bytes_b) != words_b:
        raise CoreFormatError("Skeleton category word tables are inconsistent")

    bones: list[NativeSkeletonBone] = []
    next_hull_slot = 0
    for index, raw_record in enumerate(raw_records):
        (
            unknown_vector,
            name_offset,
            parent_name_offset,
            hull_name_offset,
            transform_word,
            category_flags,
        ) = raw_record
        serialized_hull_slot = transform_word & 0x3FF
        if hull_names[index]:
            if serialized_hull_slot != next_hull_slot:
                raise CoreFormatError(
                    f"Skeleton bone {index} hull slot is out of order"
                )
            hull_slot: int | None = next_hull_slot
            next_hull_slot += 1
        else:
            if serialized_hull_slot != 0x3FF:
                raise CoreFormatError(
                    f"Skeleton bone {index} has an unnamed hull slot"
                )
            hull_slot = None
        bones.append(
            NativeSkeletonBone(
                index=index,
                name=names[index],
                name_offset=name_offset,
                parent_index=(
                    None if index == 0 else parent_indices[index]
                ),
                parent_name=parent_names[index],
                parent_name_offset=parent_name_offset,
                hull_name=hull_names[index],
                hull_name_offset=hull_name_offset,
                hull_slot=hull_slot,
                category=categories[index],
                category_flags=category_flags,
                unknown_vector=unknown_vector,
                temporary_transform=transforms[index],
                candidate_bounds=bounds[index],
                serialized_transform_word=transform_word,
                word_a=words_a[index],
                category_ordinal=bytes_a[index],
            )
        )
    if next_hull_slot != hull_count:
        raise CoreFormatError(
            "Skeleton named hull slots do not equal the outer hull count"
        )

    attachments: list[NativeSkeletonAttachment] = []
    for index in range(attachment_count):
        offset = attachment_offset + index * _AUXILIARY_SIZE
        affine_matrix = struct.unpack_from("<16f", blob, offset)
        if not all(math.isfinite(value) for value in affine_matrix):
            raise CoreFormatError(
                f"Skeleton attachment {index} matrix is not finite"
            )
        name_offset, owner_bone_index, marker_a, marker_b = (
            struct.unpack_from("<4I", blob, offset + 0x40)
        )
        if marker_a != 0xDEADBEEF or marker_b != 0xDEADC0DE:
            raise CoreFormatError(
                f"Skeleton attachment {index} sentinels are invalid"
            )
        if owner_bone_index >= bone_count:
            raise CoreFormatError(
                f"Skeleton attachment {index} has an invalid owner bone"
            )
        attachments.append(
            NativeSkeletonAttachment(
                index=index,
                name=_read_name(
                    name_pool,
                    used_name_pool_size,
                    name_offset,
                    f"Skeleton attachment {index} name",
                ),
                name_offset=name_offset,
                owner_bone_index=owner_bone_index,
                affine_matrix=affine_matrix,
            )
        )

    return NativeSkeleton(
        object_id=object_id,
        name_string_index=name_string_index,
        bone_set_string_index=bone_set_string_index,
        hull_tokens=tuple(hull_tokens),
        generic_fields=generic_fields,
        custom_words=custom_words,
        custom_alignment=custom_alignment,
        stored_size=stored_size,
        runtime_size=runtime_size,
        archive_marker=archive_marker,
        archive_transient=archive_transient,
        category_summary=category_summary,
        padded_name_pool_size=padded_name_pool_size,
        used_name_pool_size=used_name_pool_size,
        bones=tuple(bones),
        attachments=tuple(attachments),
        words_b=tuple(words_b),
        category_order_a=bytes_b,
        category_order_b=bytes_d,
        name_pool=name_pool,
        blob_tail=blob_tail,
    )
