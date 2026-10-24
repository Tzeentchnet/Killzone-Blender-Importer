# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import struct

from .archive_source import KillzoneArchiveSource
from .core_format import CoreHeader, core_header_prefix_size, parse_core_header
from .errors import CoreFormatError
from .native_static_geometry import (
    NativeRegularStaticResource,
    parse_regular_static_resource,
)
from .native_level_material import (
    NativeAnimatedTexture,
    NativeLevelMaterialPlan,
    NativeLevelTexture,
    NativeMaterial,
    parse_selected_material_set,
)

CORE_STREAM_MAGIC = b"<CoreStreamData>"

_U32 = struct.Struct("<I")
_SECTION_HEADER = struct.Struct("<5I")
_EXPANDED_DIRECTORY_ENTRY = struct.Struct("<8I")
_BONE_MAP_DIRECTORY_ENTRY = struct.Struct("<6I")
_COMPACT_DIRECTORY_ENTRY = struct.Struct("<2I")

_STATIC_RESOURCE_TYPES = {
    "RegularStaticMeshResource",
    "MultiStaticMeshResource",
    "LodStaticMeshResource",
}
_SKINNED_RESOURCE_TYPES = {
    "LinkedStaticMeshResource",
    "RegularSkinnedMeshResource",
    "MultiSkinnedMeshResource",
    "LodSkinnedMeshResource",
    "SkinnedBlendShapeResource",
}
_COLLISION_RESOURCE_TYPES = {
    "CollisionResourceBox",
    "CollisionResourcePolySoup",
    "CollisionMultiResource",
}
_EXPANDED_TYPES = (
    _STATIC_RESOURCE_TYPES
    | _SKINNED_RESOURCE_TYPES
    | _COLLISION_RESOURCE_TYPES
    | {
        "DecalResource",
        "Texture",
        "AnimatedTexture",
        "Material",
        "Skeleton",
        "SkeletonAnimationResource",
    }
)
_BONE_MAP_TYPES = {"SkeletonAnimationBoneMap"}
_COMPACT_TYPES = {
    "CollisionMeshInstance",
    "ProximitySensorInstance",
    "SimpleAnimatingSkinnedMeshInstance",
    "StaticMeshInstance",
}
_WANTED_TYPES = _EXPANDED_TYPES | _BONE_MAP_TYPES | _COMPACT_TYPES

_IDENTITY_MATRIX = (
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

_MAX_SECTION_OBJECTS = 1_000_000
_MAX_SECTION_PROPERTIES = 10_000


@dataclass(frozen=True, slots=True, order=True)
class NativeObjectKey:
    section_offset: int
    object_index: int


@dataclass(frozen=True, slots=True)
class NativeCoreObject:
    key: NativeObjectKey
    type_name: str
    object_id: int
    blob_offset: int
    blob: bytes
    identity_words: tuple[int, int, int, int] | None
    stream_offset: int
    stream_size: int


@dataclass(frozen=True, slots=True)
class NativeCoreSection:
    type_name: str
    offset: int
    end_offset: int
    data_offset: int
    inheritance_level: int
    property_count: int
    directory_kind: str
    objects: tuple[NativeCoreObject, ...]


@dataclass(frozen=True, slots=True)
class NativeStaticResourceNode:
    key: NativeObjectKey
    object_id: int
    kind: str
    child_keys: tuple[NativeObjectKey, ...]
    child_tokens: tuple[int, ...]
    lod_thresholds: tuple[float, ...]
    regular: NativeRegularStaticResource | None


@dataclass(frozen=True, slots=True)
class NativeStaticInstance:
    key: NativeObjectKey
    object_id: int
    name: str
    name_index: int
    name_is_identifier: bool
    parent_token: int
    resource_token: int
    resource_key: NativeObjectKey | None
    matrix: tuple[float, ...]
    footer: int
    raw_blob: bytes


@dataclass(frozen=True, slots=True)
class NativeCollisionMeshInstance:
    key: NativeObjectKey
    object_id: int
    name: str
    name_index: int
    name_is_identifier: bool
    collision_group: int
    parent_token: int
    collision_enabled: bool
    collision_resource_token: int
    collision_resource_key: NativeObjectKey
    collision_resource_type: str
    resource_token: int
    resource_key: NativeObjectKey
    matrix: tuple[float, ...]
    footer: int
    raw_blob: bytes


@dataclass(frozen=True, slots=True)
class _SerializedCollisionMeshInstance:
    key: NativeObjectKey
    object_id: int
    name: str
    name_index: int
    name_is_identifier: bool
    collision_group: int
    parent_token: int
    collision_enabled: bool
    collision_resource_token: int
    resource_token: int
    matrix: tuple[float, ...]
    footer: int
    raw_blob: bytes


@dataclass(frozen=True, slots=True)
class NativeSimpleSkinnedInstance:
    key: NativeObjectKey
    object_id: int
    name: str
    name_index: int
    name_is_identifier: bool
    parent_token: int
    parent_key: NativeObjectKey | None
    resource_token: int
    resource_key: NativeObjectKey
    resource_type: str
    default_animation_token: int
    default_animation_key: NativeObjectKey
    blend_shape_animation_token: int
    matrix: tuple[float, ...]
    footer: int
    raw_blob: bytes


@dataclass(frozen=True, slots=True)
class _SerializedSimpleSkinnedInstance:
    key: NativeObjectKey
    object_id: int
    name: str
    name_index: int
    name_is_identifier: bool
    parent_token: int
    resource_token: int
    default_animation_token: int
    blend_shape_animation_token: int
    matrix: tuple[float, ...]
    footer: int
    raw_blob: bytes


@dataclass(frozen=True, slots=True)
class NativeStaticOccurrence:
    leaf_key: NativeObjectKey
    source_key: NativeObjectKey
    instance_key: NativeObjectKey | None
    matrix: tuple[float, ...]
    path: str


@dataclass(frozen=True, slots=True)
class NativeStaticRenderPlan:
    roots: tuple[NativeObjectKey, ...]
    direct_roots: tuple[NativeObjectKey, ...]
    direct_occurrences: tuple[NativeStaticOccurrence, ...]
    instance_occurrences: tuple[NativeStaticOccurrence, ...]
    direct_packet_count: int
    direct_triangle_count: int
    instance_packet_count: int
    instance_triangle_count: int


@dataclass(frozen=True, slots=True)
class NativeLevel:
    header: CoreHeader
    sections: tuple[NativeCoreSection, ...]
    resources: tuple[NativeStaticResourceNode, ...]
    instances: tuple[NativeStaticInstance, ...]
    collision_instances: tuple[NativeCollisionMeshInstance, ...]
    skinned_instances: tuple[NativeSimpleSkinnedInstance, ...]
    render_plan: NativeStaticRenderPlan
    textures: tuple[NativeLevelTexture, ...]
    logical_textures: tuple[NativeLevelTexture, ...]
    animated_textures: tuple[NativeAnimatedTexture, ...]
    materials: tuple[NativeMaterial, ...]
    material_plan: NativeLevelMaterialPlan
    packet_shapes: tuple[tuple[tuple[int, ...], int], ...]
    physical_packet_count: int
    physical_triangle_count: int
    source_core_sha256: str
    source_corestream_sha256: str


@dataclass(frozen=True, slots=True)
class NativeLevelAsset:
    path: str
    corestream_path: str
    core_size: int
    corestream_size: int


def _require_range(data: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise CoreFormatError(f"{label} is truncated")


def _validate_property_descriptors(
    body: bytes,
    offset: int,
    property_count: int,
    string_count: int,
) -> None:
    _require_range(
        body,
        offset,
        property_count * 16,
        "Core typed-section property descriptors",
    )
    for index in range(property_count):
        string_index = _U32.unpack_from(body, offset + index * 16)[0]
        if string_index >= string_count:
            raise CoreFormatError(
                "Core typed-section property descriptor has an invalid "
                "string index"
            )


def _parse_expanded_section_candidate(
    body: bytes,
    *,
    offset: int,
    type_name: str,
    type_index: int,
    string_count: int,
    corestream_size: int,
) -> NativeCoreSection | None:
    _require_range(body, offset, _SECTION_HEADER.size, "Core section header")
    data_offset, actual_type, object_count, inheritance, property_count = (
        _SECTION_HEADER.unpack_from(body, offset)
    )
    if actual_type != type_index:
        return None
    if (
        object_count > _MAX_SECTION_OBJECTS
        or property_count > _MAX_SECTION_PROPERTIES
    ):
        return None
    expected_data_offset = (
        0x1C + 0x10 * property_count + 0x20 * object_count
    )
    if data_offset != expected_data_offset:
        return None

    descriptor_offset = offset + _SECTION_HEADER.size
    try:
        _validate_property_descriptors(
            body, descriptor_offset, property_count, string_count
        )
    except CoreFormatError:
        return None
    prefix_offset = descriptor_offset + property_count * 16
    _require_range(body, prefix_offset, 8, "Core expanded section prefix")
    if struct.unpack_from("<2I", body, prefix_offset) != (1, 1):
        return None

    directory_offset = prefix_offset + 8
    directory_size = object_count * _EXPANDED_DIRECTORY_ENTRY.size
    _require_range(
        body,
        directory_offset,
        directory_size,
        "Core expanded section directory",
    )
    prefix_word_offset = offset + data_offset
    _require_range(body, prefix_word_offset, 4, "Core object prefix")
    if _U32.unpack_from(body, prefix_word_offset)[0] != 0:
        return None

    blob_cursor = prefix_word_offset + 4
    objects: list[NativeCoreObject] = []
    for object_index in range(object_count):
        values = _EXPANDED_DIRECTORY_ENTRY.unpack_from(
            body,
            directory_offset
            + object_index * _EXPANDED_DIRECTORY_ENTRY.size,
        )
        blob_size = values[0]
        object_id = values[1]
        stream_offset = values[6]
        stream_size = values[7]
        if (
            stream_offset > corestream_size
            or stream_size > corestream_size - stream_offset
        ):
            return None
        if blob_size > len(body) - blob_cursor:
            return None
        objects.append(
            NativeCoreObject(
                key=NativeObjectKey(offset, object_index),
                type_name=type_name,
                object_id=object_id,
                blob_offset=blob_cursor,
                blob=body[blob_cursor : blob_cursor + blob_size],
                identity_words=values[2:6],
                stream_offset=stream_offset,
                stream_size=stream_size,
            )
        )
        blob_cursor += blob_size

    return NativeCoreSection(
        type_name=type_name,
        offset=offset,
        end_offset=blob_cursor,
        data_offset=data_offset,
        inheritance_level=inheritance,
        property_count=property_count,
        directory_kind="expanded",
        objects=tuple(objects),
    )


def _parse_compact_section_candidate(
    body: bytes,
    *,
    offset: int,
    type_name: str,
    type_index: int,
    string_count: int,
) -> NativeCoreSection | None:
    _require_range(body, offset, _SECTION_HEADER.size, "Core section header")
    data_offset, actual_type, object_count, inheritance, property_count = (
        _SECTION_HEADER.unpack_from(body, offset)
    )
    if actual_type != type_index:
        return None
    if (
        object_count > _MAX_SECTION_OBJECTS
        or property_count > _MAX_SECTION_PROPERTIES
    ):
        return None
    base = 0x1C + 0x10 * property_count + 8 * object_count
    if data_offset not in (base, base + 8):
        return None

    descriptor_offset = offset + _SECTION_HEADER.size
    try:
        _validate_property_descriptors(
            body, descriptor_offset, property_count, string_count
        )
    except CoreFormatError:
        return None
    prefix_offset = descriptor_offset + property_count * 16
    _require_range(body, prefix_offset, 8, "Core compact section prefix")
    if struct.unpack_from("<2I", body, prefix_offset) != (0, 0):
        return None

    directory_offset = prefix_offset + 8
    directory_size = object_count * _COMPACT_DIRECTORY_ENTRY.size
    _require_range(
        body,
        directory_offset,
        directory_size,
        "Core compact section directory",
    )
    alignment_offset = directory_offset + directory_size
    alignment_size = offset + data_offset - alignment_offset
    if alignment_size not in (0, 8):
        return None
    if any(body[alignment_offset : alignment_offset + alignment_size]):
        return None

    prefix_word_offset = offset + data_offset
    _require_range(body, prefix_word_offset, 4, "Core object prefix")
    if _U32.unpack_from(body, prefix_word_offset)[0] != 0:
        return None

    blob_cursor = prefix_word_offset + 4
    objects: list[NativeCoreObject] = []
    for object_index in range(object_count):
        blob_size, object_id = _COMPACT_DIRECTORY_ENTRY.unpack_from(
            body,
            directory_offset
            + object_index * _COMPACT_DIRECTORY_ENTRY.size,
        )
        if blob_size > len(body) - blob_cursor:
            return None
        objects.append(
            NativeCoreObject(
                key=NativeObjectKey(offset, object_index),
                type_name=type_name,
                object_id=object_id,
                blob_offset=blob_cursor,
                blob=body[blob_cursor : blob_cursor + blob_size],
                identity_words=None,
                stream_offset=0,
                stream_size=0,
            )
        )
        blob_cursor += blob_size

    return NativeCoreSection(
        type_name=type_name,
        offset=offset,
        end_offset=blob_cursor,
        data_offset=data_offset,
        inheritance_level=inheritance,
        property_count=property_count,
        directory_kind="compact",
        objects=tuple(objects),
    )


def _parse_bone_map_section_candidate(
    body: bytes,
    *,
    offset: int,
    type_name: str,
    type_index: int,
    string_count: int,
) -> NativeCoreSection | None:
    _require_range(body, offset, _SECTION_HEADER.size, "Core section header")
    data_offset, actual_type, object_count, inheritance, property_count = (
        _SECTION_HEADER.unpack_from(body, offset)
    )
    if actual_type != type_index:
        return None
    if (
        object_count > _MAX_SECTION_OBJECTS
        or property_count > _MAX_SECTION_PROPERTIES
    ):
        return None
    directory_end = (
        0x1C + 0x10 * property_count + 0x18 * object_count
    )
    expected_data_offset = (
        (directory_end + 4 + 0x0F) & ~0x0F
    ) - 4
    if data_offset != expected_data_offset:
        return None

    descriptor_offset = offset + _SECTION_HEADER.size
    try:
        _validate_property_descriptors(
            body, descriptor_offset, property_count, string_count
        )
    except CoreFormatError:
        return None
    prefix_offset = descriptor_offset + property_count * 16
    _require_range(body, prefix_offset, 8, "Core BoneMap section prefix")
    if struct.unpack_from("<2I", body, prefix_offset) != (1, 0):
        return None

    directory_offset = prefix_offset + 8
    directory_size = object_count * _BONE_MAP_DIRECTORY_ENTRY.size
    _require_range(
        body,
        directory_offset,
        directory_size,
        "Core BoneMap section directory",
    )
    alignment_offset = directory_offset + directory_size
    prefix_word_offset = offset + data_offset
    if any(body[alignment_offset:prefix_word_offset]):
        return None
    _require_range(body, prefix_word_offset, 4, "Core object prefix")
    if _U32.unpack_from(body, prefix_word_offset)[0] != 0:
        return None

    blob_cursor = prefix_word_offset + 4
    objects: list[NativeCoreObject] = []
    for object_index in range(object_count):
        values = _BONE_MAP_DIRECTORY_ENTRY.unpack_from(
            body,
            directory_offset
            + object_index * _BONE_MAP_DIRECTORY_ENTRY.size,
        )
        blob_size = values[0]
        if blob_size > len(body) - blob_cursor:
            return None
        objects.append(
            NativeCoreObject(
                key=NativeObjectKey(offset, object_index),
                type_name=type_name,
                object_id=values[1],
                blob_offset=blob_cursor,
                blob=body[blob_cursor : blob_cursor + blob_size],
                identity_words=values[2:6],
                stream_offset=0,
                stream_size=0,
            )
        )
        blob_cursor += blob_size

    return NativeCoreSection(
        type_name=type_name,
        offset=offset,
        end_offset=blob_cursor,
        data_offset=data_offset,
        inheritance_level=inheritance,
        property_count=property_count,
        directory_kind="bone-map",
        objects=tuple(objects),
    )


def parse_native_core_sections(
    core_data: bytes,
    corestream_size: int,
) -> tuple[CoreHeader, tuple[NativeCoreSection, ...]]:
    if len(core_data) < 24:
        raise CoreFormatError("Native Core header is truncated")
    required = core_header_prefix_size(core_data[:24], len(core_data))
    header = parse_core_header(core_data[:required], len(core_data))
    if header.variant != 5:
        raise CoreFormatError("Native Core must use variant 5")
    body = core_data[header.body_offset :]

    type_indices: dict[str, tuple[int, ...]] = {}
    for type_name in _WANTED_TYPES:
        indices = tuple(
            index
            for index, value in enumerate(header.strings)
            if value.utf8 == type_name
        )
        if indices:
            type_indices[type_name] = indices

    sections_by_offset: dict[int, NativeCoreSection] = {}
    for offset in range(0, len(body) - _SECTION_HEADER.size + 1, 4):
        actual_type = _U32.unpack_from(body, offset + 4)[0]
        for type_name, indices in type_indices.items():
            if actual_type not in indices:
                continue
            if type_name in _EXPANDED_TYPES:
                section = _parse_expanded_section_candidate(
                    body,
                    offset=offset,
                    type_name=type_name,
                    type_index=actual_type,
                    string_count=len(header.strings),
                    corestream_size=corestream_size,
                )
            elif type_name in _BONE_MAP_TYPES:
                section = _parse_bone_map_section_candidate(
                    body,
                    offset=offset,
                    type_name=type_name,
                    type_index=actual_type,
                    string_count=len(header.strings),
                )
            else:
                section = _parse_compact_section_candidate(
                    body,
                    offset=offset,
                    type_name=type_name,
                    type_index=actual_type,
                    string_count=len(header.strings),
                )
            if section is None:
                continue
            existing = sections_by_offset.get(offset)
            if existing is not None and existing != section:
                raise CoreFormatError(
                    "Core section offset has conflicting valid layouts"
                )
            sections_by_offset[offset] = section

    sections = tuple(
        sections_by_offset[offset] for offset in sorted(sections_by_offset)
    )
    for previous, current in zip(sections, sections[1:]):
        if current.offset < previous.end_offset:
            raise CoreFormatError("Validated native Core sections overlap")
    return header, sections


def parse_native_level_sections(
    core_data: bytes,
    corestream_size: int,
) -> tuple[CoreHeader, tuple[NativeCoreSection, ...]]:
    header, sections = parse_native_core_sections(
        core_data, corestream_size
    )
    missing = sorted(
        type_name
        for type_name in (
            "RegularStaticMeshResource",
            "StaticMeshInstance",
        )
        if not any(section.type_name == type_name for section in sections)
    )
    if missing:
        raise CoreFormatError(
            "Native level is missing required section types: "
            + ", ".join(missing)
        )
    return header, sections


def _resolve_resource_token(
    token: int,
    nodes: tuple[NativeStaticResourceNode, ...],
) -> NativeStaticResourceNode:
    matches = tuple(node for node in nodes if node.object_id == token)
    if len(matches) != 1:
        raise CoreFormatError(
            f"Static resource token 0x{token:08X} resolves to "
            f"{len(matches)} graph nodes"
        )
    return matches[0]


def parse_native_static_resources(
    objects: tuple[NativeCoreObject, ...],
    corestream_data: bytes | None = None,
    *,
    decode_keys: set[NativeObjectKey] | None = None,
) -> tuple[NativeStaticResourceNode, ...]:
    resource_objects = tuple(
        obj for obj in objects if obj.type_name in _STATIC_RESOURCE_TYPES
    )
    preliminary: list[NativeStaticResourceNode] = []
    for obj in resource_objects:
        if obj.type_name == "RegularStaticMeshResource":
            regular = (
                parse_regular_static_resource(
                    obj.blob,
                    corestream_data,
                    object_id=obj.object_id,
                    stream_offset=obj.stream_offset,
                    stream_size=obj.stream_size,
                )
                if corestream_data is not None
                and (decode_keys is None or obj.key in decode_keys)
                else None
            )
            preliminary.append(
                NativeStaticResourceNode(
                    key=obj.key,
                    object_id=obj.object_id,
                    kind="regular",
                    child_keys=(),
                    child_tokens=(),
                    lod_thresholds=(),
                    regular=regular,
                )
            )
        elif obj.type_name == "MultiStaticMeshResource":
            preliminary.append(
                NativeStaticResourceNode(
                    key=obj.key,
                    object_id=obj.object_id,
                    kind="multi",
                    child_keys=(),
                    child_tokens=_parse_multi_tokens(obj.blob),
                    lod_thresholds=(),
                    regular=None,
                )
            )
        elif obj.type_name == "LodStaticMeshResource":
            child_tokens, thresholds = _parse_lod(obj.blob)
            preliminary.append(
                NativeStaticResourceNode(
                    key=obj.key,
                    object_id=obj.object_id,
                    kind="lod",
                    child_keys=(),
                    child_tokens=child_tokens,
                    lod_thresholds=thresholds,
                    regular=None,
                )
            )

    preliminary_tuple = tuple(preliminary)
    return tuple(
        NativeStaticResourceNode(
            key=node.key,
            object_id=node.object_id,
            kind=node.kind,
            child_keys=tuple(
                _resolve_resource_token(token, preliminary_tuple).key
                for token in node.child_tokens
            ),
            child_tokens=node.child_tokens,
            lod_thresholds=node.lod_thresholds,
            regular=node.regular,
        )
        for node in preliminary_tuple
    )


def _parse_multi_tokens(blob: bytes) -> tuple[int, ...]:
    _require_range(blob, 0, 0x28, "Multi-static resource")
    count = _U32.unpack_from(blob, 0)[0]
    if count == 0:
        raise CoreFormatError("Multi-static resource has no parts")
    required = 0x28 + count * 4
    _require_range(blob, 0, required, "Multi-static part array")
    if any(blob[required:]):
        raise CoreFormatError("Multi-static resource has a non-zero tail")
    return struct.unpack_from(f"<{count}I", blob, 0x28)


def _parse_lod(
    blob: bytes,
) -> tuple[tuple[int, ...], tuple[float, ...]]:
    _require_range(blob, 0, 0x2C, "LOD-static resource")
    mesh_count, threshold_count = struct.unpack_from("<2I", blob, 0)
    if mesh_count == 0 or mesh_count != threshold_count:
        raise CoreFormatError("LOD-static resource has invalid array counts")
    mesh_end = 0x2C + mesh_count * 4
    required = mesh_end + threshold_count * 4
    _require_range(blob, 0, required, "LOD-static arrays")
    if any(blob[required:]):
        raise CoreFormatError("LOD-static resource has a non-zero tail")
    child_tokens = struct.unpack_from(f"<{mesh_count}I", blob, 0x2C)
    thresholds = struct.unpack_from(f"<{threshold_count}f", blob, mesh_end)
    if thresholds[0] != 0.0:
        raise CoreFormatError("LOD-static baseline threshold is not zero")
    if any(
        not math.isfinite(value) or value <= 0.0
        for value in thresholds[1:]
    ):
        raise CoreFormatError("LOD-static threshold is invalid")
    if any(
        thresholds[index] > thresholds[index + 1]
        for index in range(len(thresholds) - 1)
    ):
        raise CoreFormatError("LOD-static thresholds are not monotonic")
    return child_tokens, thresholds


def _parse_affine_matrix(
    blob: bytes,
    *,
    offset: int,
    type_name: str,
) -> tuple[float, ...]:
    matrix = struct.unpack_from("<16f", blob, offset)
    if not all(math.isfinite(value) for value in matrix):
        raise CoreFormatError(f"{type_name} matrix is not finite")
    if (
        matrix[3] != 0.0
        or matrix[7] != 0.0
        or matrix[11] != 0.0
        or matrix[15] != 1.0
    ):
        raise CoreFormatError(f"{type_name} matrix is not row-vector affine")
    return matrix


def _parse_static_instance(
    obj: NativeCoreObject,
    header: CoreHeader,
) -> NativeStaticInstance:
    blob = obj.blob
    if len(blob) != 0x60:
        raise CoreFormatError(
            f"StaticMeshInstance blob size is 0x{len(blob):X}, expected 0x60"
        )
    name_flag, name_index, parent = struct.unpack_from("<3I", blob, 0)
    if name_flag not in (0, 1):
        raise CoreFormatError("StaticMeshInstance name flag is not boolean")
    if name_index >= len(header.strings):
        raise CoreFormatError("StaticMeshInstance name index is invalid")
    if parent != 0:
        raise CoreFormatError("StaticMeshInstance parent token is not null")
    matrix = _parse_affine_matrix(
        blob,
        offset=0x0C,
        type_name="StaticMeshInstance",
    )
    resource_token = _U32.unpack_from(blob, 0x4C)[0]
    footer = _U32.unpack_from(blob, 0x50)[0]
    if any(blob[0x54:0x60]):
        raise CoreFormatError("StaticMeshInstance trailer is non-zero")
    name = header.strings[name_index].utf8
    if name is None:
        raise CoreFormatError("StaticMeshInstance name is not UTF-8")
    return NativeStaticInstance(
        key=obj.key,
        object_id=obj.object_id,
        name=name,
        name_index=name_index,
        name_is_identifier=bool(name_flag),
        parent_token=parent,
        resource_token=resource_token,
        resource_key=None,
        matrix=matrix,
        footer=footer,
        raw_blob=blob,
    )


def _parse_collision_mesh_instance(
    obj: NativeCoreObject,
    header: CoreHeader,
) -> _SerializedCollisionMeshInstance:
    blob = obj.blob
    if len(blob) != 0x70:
        raise CoreFormatError(
            f"CollisionMeshInstance blob size is 0x{len(blob):X}, "
            "expected 0x70"
        )
    (
        name_index,
        collision_group,
        parent_token,
        collision_resource_token,
    ) = struct.unpack_from("<4I", blob, 0)
    collision_enabled = blob[0x10]
    name_flag = blob[0x11]
    if name_index >= len(header.strings):
        raise CoreFormatError("CollisionMeshInstance name index is invalid")
    if collision_enabled not in (0, 1):
        raise CoreFormatError(
            "CollisionMeshInstance enabled flag is not boolean"
        )
    if name_flag not in (0, 1):
        raise CoreFormatError(
            "CollisionMeshInstance name flag is not boolean"
        )
    if any(blob[0x12:0x14]):
        raise CoreFormatError(
            "CollisionMeshInstance flag padding is non-zero"
        )
    if parent_token != 0:
        raise CoreFormatError(
            "CollisionMeshInstance parent token is not null"
        )
    if collision_resource_token == 0:
        raise CoreFormatError(
            "CollisionMeshInstance collision resource token is null"
        )
    matrix = _parse_affine_matrix(
        blob,
        offset=0x14,
        type_name="CollisionMeshInstance",
    )
    resource_token, zero_a, zero_b, footer = struct.unpack_from(
        "<4I", blob, 0x54
    )
    if resource_token == 0:
        raise CoreFormatError(
            "CollisionMeshInstance mesh resource token is null"
        )
    if zero_a != 0 or zero_b != 0 or any(blob[0x64:0x70]):
        raise CoreFormatError(
            "CollisionMeshInstance trailer is non-zero"
        )
    name = header.strings[name_index].utf8
    if name is None:
        raise CoreFormatError(
            "CollisionMeshInstance name is not UTF-8"
        )
    return _SerializedCollisionMeshInstance(
        key=obj.key,
        object_id=obj.object_id,
        name=name,
        name_index=name_index,
        name_is_identifier=bool(name_flag),
        collision_group=collision_group,
        parent_token=parent_token,
        collision_enabled=bool(collision_enabled),
        collision_resource_token=collision_resource_token,
        resource_token=resource_token,
        matrix=matrix,
        footer=footer,
        raw_blob=blob,
    )


def _parse_simple_skinned_instance(
    obj: NativeCoreObject,
    header: CoreHeader,
) -> _SerializedSimpleSkinnedInstance:
    blob = obj.blob
    if len(blob) != 0x70:
        raise CoreFormatError(
            "SimpleAnimatingSkinnedMeshInstance blob size is "
            f"0x{len(blob):X}, expected 0x70"
        )
    name_index, name_flag, parent_token = struct.unpack_from("<3I", blob, 0)
    if name_index >= len(header.strings):
        raise CoreFormatError(
            "SimpleAnimatingSkinnedMeshInstance name index is invalid"
        )
    if name_flag not in (0, 1):
        raise CoreFormatError(
            "SimpleAnimatingSkinnedMeshInstance name flag is not boolean"
        )
    matrix = _parse_affine_matrix(
        blob,
        offset=0x0C,
        type_name="SimpleAnimatingSkinnedMeshInstance",
    )
    (
        resource_token,
        default_animation_token,
        blend_shape_animation_token,
        zero_a,
        zero_b,
        footer,
    ) = struct.unpack_from("<6I", blob, 0x4C)
    if blend_shape_animation_token != 0:
        raise CoreFormatError(
            "SimpleAnimatingSkinnedMeshInstance blend-shape animation "
            "token is not null"
        )
    if zero_a != 0 or zero_b != 0 or any(blob[0x64:0x70]):
        raise CoreFormatError(
            "SimpleAnimatingSkinnedMeshInstance trailer is non-zero"
        )
    name = header.strings[name_index].utf8
    if name is None:
        raise CoreFormatError(
            "SimpleAnimatingSkinnedMeshInstance name is not UTF-8"
        )
    return _SerializedSimpleSkinnedInstance(
        key=obj.key,
        object_id=obj.object_id,
        name=name,
        name_index=name_index,
        name_is_identifier=bool(name_flag),
        parent_token=parent_token,
        resource_token=resource_token,
        default_animation_token=default_animation_token,
        blend_shape_animation_token=blend_shape_animation_token,
        matrix=matrix,
        footer=footer,
        raw_blob=blob,
    )


def _resolve_core_object_token(
    token: int,
    objects: tuple[NativeCoreObject, ...],
    expected_types: set[str],
    label: str,
) -> NativeCoreObject:
    matches = tuple(
        obj
        for obj in objects
        if obj.object_id == token and obj.type_name in expected_types
    )
    if len(matches) != 1:
        expected = ", ".join(sorted(expected_types))
        raise CoreFormatError(
            f"{label} token 0x{token:08X} resolves to {len(matches)} "
            f"objects of expected type {expected}"
        )
    return matches[0]


def parse_native_collision_mesh_instances(
    header: CoreHeader,
    objects: tuple[NativeCoreObject, ...],
    resources: tuple[NativeStaticResourceNode, ...],
) -> tuple[NativeCollisionMeshInstance, ...]:
    parsed = tuple(
        _parse_collision_mesh_instance(obj, header)
        for obj in objects
        if obj.type_name == "CollisionMeshInstance"
    )
    instances: list[NativeCollisionMeshInstance] = []
    for instance in parsed:
        collision_resource = _resolve_core_object_token(
            instance.collision_resource_token,
            objects,
            _COLLISION_RESOURCE_TYPES,
            "CollisionMeshInstance collision resource",
        )
        mesh_resource = _resolve_resource_token(
            instance.resource_token,
            resources,
        )
        instances.append(
            NativeCollisionMeshInstance(
                key=instance.key,
                object_id=instance.object_id,
                name=instance.name,
                name_index=instance.name_index,
                name_is_identifier=instance.name_is_identifier,
                collision_group=instance.collision_group,
                parent_token=instance.parent_token,
                collision_enabled=instance.collision_enabled,
                collision_resource_token=(
                    instance.collision_resource_token
                ),
                collision_resource_key=collision_resource.key,
                collision_resource_type=collision_resource.type_name,
                resource_token=instance.resource_token,
                resource_key=mesh_resource.key,
                matrix=instance.matrix,
                footer=instance.footer,
                raw_blob=instance.raw_blob,
            )
        )
    return tuple(instances)


def parse_native_level_instances(
    header: CoreHeader,
    objects: tuple[NativeCoreObject, ...],
    resources: tuple[NativeStaticResourceNode, ...],
) -> tuple[
    tuple[NativeStaticInstance, ...],
    tuple[NativeSimpleSkinnedInstance, ...],
]:
    parsed_static = tuple(
        _parse_static_instance(obj, header)
        for obj in objects
        if obj.type_name == "StaticMeshInstance"
    )
    static_instances = tuple(
        NativeStaticInstance(
            key=instance.key,
            object_id=instance.object_id,
            name=instance.name,
            name_index=instance.name_index,
            name_is_identifier=instance.name_is_identifier,
            parent_token=instance.parent_token,
            resource_token=instance.resource_token,
            resource_key=(
                None
                if instance.resource_token == 0
                else _resolve_resource_token(
                    instance.resource_token, resources
                ).key
            ),
            matrix=instance.matrix,
            footer=instance.footer,
            raw_blob=instance.raw_blob,
        )
        for instance in parsed_static
    )

    parsed_skinned = tuple(
        _parse_simple_skinned_instance(obj, header)
        for obj in objects
        if obj.type_name == "SimpleAnimatingSkinnedMeshInstance"
    )
    skinned_instances: list[NativeSimpleSkinnedInstance] = []
    for instance in parsed_skinned:
        resource = _resolve_core_object_token(
            instance.resource_token,
            objects,
            _SKINNED_RESOURCE_TYPES,
            "SimpleAnimatingSkinnedMeshInstance resource",
        )
        animation = _resolve_core_object_token(
            instance.default_animation_token,
            objects,
            {"SkeletonAnimationResource"},
            "SimpleAnimatingSkinnedMeshInstance default animation",
        )
        parent = (
            None
            if instance.parent_token == 0
            else _resolve_core_object_token(
                instance.parent_token,
                objects,
                {"ProximitySensorInstance"},
                "SimpleAnimatingSkinnedMeshInstance parent",
            )
        )
        skinned_instances.append(
            NativeSimpleSkinnedInstance(
                key=instance.key,
                object_id=instance.object_id,
                name=instance.name,
                name_index=instance.name_index,
                name_is_identifier=instance.name_is_identifier,
                parent_token=instance.parent_token,
                parent_key=None if parent is None else parent.key,
                resource_token=instance.resource_token,
                resource_key=resource.key,
                resource_type=resource.type_name,
                default_animation_token=instance.default_animation_token,
                default_animation_key=animation.key,
                blend_shape_animation_token=(
                    instance.blend_shape_animation_token
                ),
                matrix=instance.matrix,
                footer=instance.footer,
                raw_blob=instance.raw_blob,
            )
        )
    return static_instances, tuple(skinned_instances)


def _expand_resource(
    key: NativeObjectKey,
    nodes_by_key: dict[NativeObjectKey, NativeStaticResourceNode],
    memo: dict[NativeObjectKey, tuple[NativeObjectKey, ...]],
    stack: set[NativeObjectKey],
) -> tuple[NativeObjectKey, ...]:
    cached = memo.get(key)
    if cached is not None:
        return cached
    if key in stack:
        raise CoreFormatError("Static resource graph contains a cycle")
    node = nodes_by_key.get(key)
    if node is None:
        raise CoreFormatError("Static resource graph references a missing node")
    stack.add(key)
    try:
        if node.kind == "regular":
            leaves = (key,)
        elif node.kind == "multi":
            leaves = tuple(
                leaf
                for child in node.child_keys
                for leaf in _expand_resource(
                    child, nodes_by_key, memo, stack
                )
            )
        elif node.kind == "lod":
            if not node.child_keys:
                raise CoreFormatError("LOD-static resource has no children")
            leaves = _expand_resource(
                node.child_keys[0], nodes_by_key, memo, stack
            )
        else:
            raise CoreFormatError(
                f"Unsupported static resource node kind {node.kind!r}"
            )
    finally:
        stack.remove(key)
    memo[key] = leaves
    return leaves


def native_static_resource_roots(
    resources: tuple[NativeStaticResourceNode, ...],
) -> tuple[NativeObjectKey, ...]:
    incoming = {node.key: 0 for node in resources}
    if len(incoming) != len(resources):
        raise CoreFormatError("Static resource graph has duplicate keys")
    for node in resources:
        for child_key in node.child_keys:
            if child_key not in incoming:
                raise CoreFormatError(
                    "Static resource wrapper references a missing child"
                )
            incoming[child_key] += 1
    return tuple(sorted(key for key, count in incoming.items() if count == 0))


def expand_native_static_resource(
    root_key: NativeObjectKey,
    resources: tuple[NativeStaticResourceNode, ...],
) -> tuple[NativeObjectKey, ...]:
    nodes_by_key = {node.key: node for node in resources}
    if len(nodes_by_key) != len(resources):
        raise CoreFormatError("Static resource graph has duplicate keys")
    if root_key not in native_static_resource_roots(resources):
        raise CoreFormatError("Selected static resource is not a graph root")
    return _expand_resource(root_key, nodes_by_key, {}, set())


def build_static_render_plan(
    resources: tuple[NativeStaticResourceNode, ...],
    instances: tuple[NativeStaticInstance, ...],
    collision_instances: tuple[NativeCollisionMeshInstance, ...] = (),
) -> NativeStaticRenderPlan:
    nodes_by_key = {node.key: node for node in resources}
    if len(nodes_by_key) != len(resources):
        raise CoreFormatError("Static resource graph has duplicate keys")

    roots = native_static_resource_roots(resources)

    memo: dict[NativeObjectKey, tuple[NativeObjectKey, ...]] = {}
    instance_occurrences: list[NativeStaticOccurrence] = []
    instance_target_keys: set[NativeObjectKey] = set()
    for instance in instances:
        if instance.resource_key is None:
            continue
        instance_target_keys.add(instance.resource_key)
        for leaf_key in _expand_resource(
            instance.resource_key, nodes_by_key, memo, set()
        ):
            instance_occurrences.append(
                NativeStaticOccurrence(
                    leaf_key=leaf_key,
                    source_key=instance.resource_key,
                    instance_key=instance.key,
                    matrix=instance.matrix,
                    path="instance",
                )
            )
    for instance in collision_instances:
        instance_target_keys.add(instance.resource_key)
        for leaf_key in _expand_resource(
            instance.resource_key, nodes_by_key, memo, set()
        ):
            instance_occurrences.append(
                NativeStaticOccurrence(
                    leaf_key=leaf_key,
                    source_key=instance.resource_key,
                    instance_key=instance.key,
                    matrix=instance.matrix,
                    path="collision-instance",
                )
            )

    direct_roots = tuple(
        key for key in roots if key not in instance_target_keys
    )
    direct_occurrences: list[NativeStaticOccurrence] = []
    for root_key in direct_roots:
        for leaf_key in _expand_resource(
            root_key, nodes_by_key, memo, set()
        ):
            direct_occurrences.append(
                NativeStaticOccurrence(
                    leaf_key=leaf_key,
                    source_key=root_key,
                    instance_key=None,
                    matrix=_IDENTITY_MATRIX,
                    path="direct",
                )
            )

    def totals(
        occurrences: tuple[NativeStaticOccurrence, ...],
    ) -> tuple[int, int]:
        packet_count = 0
        triangle_count = 0
        for occurrence in occurrences:
            node = nodes_by_key[occurrence.leaf_key]
            if node.regular is None:
                raise CoreFormatError(
                    "Static render plan leaf is not a regular resource"
                )
            packet_count += len(node.regular.packets)
            triangle_count += node.regular.triangle_count
        return packet_count, triangle_count

    direct_tuple = tuple(direct_occurrences)
    instance_tuple = tuple(instance_occurrences)
    direct_packets, direct_triangles = totals(direct_tuple)
    instance_packets, instance_triangles = totals(instance_tuple)
    return NativeStaticRenderPlan(
        roots=roots,
        direct_roots=direct_roots,
        direct_occurrences=direct_tuple,
        instance_occurrences=instance_tuple,
        direct_packet_count=direct_packets,
        direct_triangle_count=direct_triangles,
        instance_packet_count=instance_packets,
        instance_triangle_count=instance_triangles,
    )


def build_static_model_render_plan(
    resources: tuple[NativeStaticResourceNode, ...],
    root_key: NativeObjectKey,
) -> NativeStaticRenderPlan:
    roots = native_static_resource_roots(resources)
    if root_key not in roots:
        raise CoreFormatError("Selected static model resource is not a root")
    nodes_by_key = {node.key: node for node in resources}
    occurrences = tuple(
        NativeStaticOccurrence(
            leaf_key=leaf_key,
            source_key=root_key,
            instance_key=None,
            matrix=_IDENTITY_MATRIX,
            path="direct",
        )
        for leaf_key in _expand_resource(
            root_key,
            nodes_by_key,
            {},
            set(),
        )
    )
    packet_count = 0
    triangle_count = 0
    for occurrence in occurrences:
        regular = nodes_by_key[occurrence.leaf_key].regular
        if regular is None:
            raise CoreFormatError(
                "Static model render-plan leaf was not decoded"
            )
        packet_count += len(regular.packets)
        triangle_count += regular.triangle_count
    return NativeStaticRenderPlan(
        roots=roots,
        direct_roots=(root_key,),
        direct_occurrences=occurrences,
        instance_occurrences=(),
        direct_packet_count=packet_count,
        direct_triangle_count=triangle_count,
        instance_packet_count=0,
        instance_triangle_count=0,
    )


def _occurrence_material_tokens(
    occurrences: tuple[NativeStaticOccurrence, ...],
    resources: tuple[NativeStaticResourceNode, ...],
) -> tuple[int, ...]:
    nodes_by_key = {node.key: node for node in resources}
    tokens: set[int] = set()
    for occurrence in occurrences:
        node = nodes_by_key[occurrence.leaf_key]
        if node.regular is None:
            raise CoreFormatError(
                "Static material selection reached a non-regular leaf"
            )
        tokens.update(
            record.material_id for record in node.regular.records
        )
    return tuple(sorted(tokens))


def parse_native_level(
    core_data: bytes,
    corestream_data: bytes,
    *,
    include_textures: bool = True,
) -> NativeLevel:
    if len(corestream_data) < 0x40:
        raise CoreFormatError("Native level CoreStream header is truncated")
    if corestream_data[: len(CORE_STREAM_MAGIC)] != CORE_STREAM_MAGIC:
        raise CoreFormatError("Native level CoreStream magic is invalid")
    if any(corestream_data[len(CORE_STREAM_MAGIC) : 0x40]):
        raise CoreFormatError(
            "Native level CoreStream reserved header bytes are non-zero"
        )

    header, sections = parse_native_level_sections(
        core_data, len(corestream_data)
    )
    objects = tuple(obj for section in sections for obj in section.objects)
    resources = parse_native_static_resources(
        objects,
        corestream_data,
    )

    instances, skinned_instances = parse_native_level_instances(
        header,
        objects,
        resources,
    )
    collision_instances = parse_native_collision_mesh_instances(
        header,
        objects,
        resources,
    )

    render_plan = build_static_render_plan(
        resources,
        instances,
        collision_instances,
    )
    direct_material_tokens = _occurrence_material_tokens(
        render_plan.direct_occurrences, resources
    )
    instance_material_tokens = _occurrence_material_tokens(
        render_plan.instance_occurrences, resources
    )
    if include_textures:
        material_set = parse_selected_material_set(
            objects,
            corestream_data,
            direct_material_tokens,
            instance_material_tokens,
        )
        textures = material_set.textures
        logical_textures = material_set.logical_textures
        animated_textures = material_set.animated_textures
        materials = material_set.materials
        material_plan = material_set.material_plan
    else:
        textures = ()
        logical_textures = ()
        animated_textures = ()
        materials = ()
        material_plan = NativeLevelMaterialPlan(
            direct_material_tokens=direct_material_tokens,
            instance_material_tokens=instance_material_tokens,
            direct_texture_tokens=(),
            instance_texture_tokens=(),
        )
    regular_resources = tuple(
        node.regular for node in resources if node.regular is not None
    )
    shape_counts = Counter(
        packet.tail_opcodes
        for resource in regular_resources
        for packet in resource.packets
    )
    return NativeLevel(
        header=header,
        sections=sections,
        resources=resources,
        instances=instances,
        collision_instances=collision_instances,
        skinned_instances=skinned_instances,
        render_plan=render_plan,
        textures=textures,
        logical_textures=logical_textures,
        animated_textures=animated_textures,
        materials=materials,
        material_plan=material_plan,
        packet_shapes=tuple(sorted(shape_counts.items())),
        physical_packet_count=sum(
            len(resource.packets) for resource in regular_resources
        ),
        physical_triangle_count=sum(
            resource.triangle_count for resource in regular_resources
        ),
        source_core_sha256=hashlib.sha256(core_data).hexdigest(),
        source_corestream_sha256=hashlib.sha256(
            corestream_data
        ).hexdigest(),
    )


def scan_native_levels(
    source_path: str | Path,
) -> tuple[NativeLevelAsset, ...]:
    assets: list[NativeLevelAsset] = []
    with KillzoneArchiveSource(source_path) as source:
        catalog = source.load_catalog()
        entries_by_path = {
            entry.path.as_posix().casefold(): entry
            for entry in catalog.entries
        }
        for entry in sorted(
            catalog.entries,
            key=lambda candidate: candidate.path.as_posix(),
        ):
            name = entry.path.name.casefold()
            if (
                entry.path.suffix.casefold() != ".core"
                or not name.startswith("level.")
                or name.endswith(".missionscript.core")
            ):
                continue
            stream_path = entry.path.with_suffix(
                ".corestream"
            ).as_posix()
            stream_entry = entries_by_path.get(stream_path.casefold())
            if stream_entry is None:
                raise CoreFormatError(
                    f"{entry.path.as_posix()}: paired CoreStream is missing"
                )
            assets.append(
                NativeLevelAsset(
                    path=entry.path.as_posix(),
                    corestream_path=stream_path,
                    core_size=entry.size,
                    corestream_size=stream_entry.size,
                )
            )
    return tuple(assets)


def load_native_level(
    source_path: str | Path,
    level_path: str,
    *,
    include_textures: bool = True,
) -> NativeLevel:
    normalized_path = level_path.casefold()
    with KillzoneArchiveSource(source_path) as source:
        catalog = source.load_catalog()
        entries_by_path = {
            entry.path.as_posix().casefold(): entry
            for entry in catalog.entries
        }
        entry = entries_by_path.get(normalized_path)
        if entry is None:
            raise CoreFormatError(
                f"Native level Core is not in the catalog: {level_path}"
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
        return parse_native_level(
            core_data,
            corestream_data,
            include_textures=include_textures,
        )
    except CoreFormatError as exc:
        raise CoreFormatError(f"{level_path}: {exc}") from exc
