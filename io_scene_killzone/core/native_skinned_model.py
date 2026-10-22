# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import struct

from .archive_source import KillzoneArchiveSource
from .core_format import CoreHeader
from .errors import CoreFormatError
from .native_level import (
    CORE_STREAM_MAGIC,
    NativeCoreObject,
    NativeCoreSection,
    NativeObjectKey,
    parse_native_core_sections,
)
from .native_level_material import (
    NativeAnimatedTexture,
    NativeLevelMaterialPlan,
    NativeLevelTexture,
    NativeMaterial,
    build_level_material_plan,
    parse_animated_textures,
    parse_level_textures,
    parse_selected_materials,
)
from .native_skeleton import NativeSkeleton, parse_native_skeleton
from .native_skinned_geometry import (
    NativeRegularSkinnedResource,
    parse_regular_skinned_resource,
)

_U32 = struct.Struct("<I")
_SKINNED_RESOURCE_TYPES = {
    "LinkedStaticMeshResource",
    "RegularSkinnedMeshResource",
    "MultiSkinnedMeshResource",
    "LodSkinnedMeshResource",
    "SkinnedBlendShapeResource",
}


@dataclass(frozen=True, slots=True)
class NativeSkinnedResourceNode:
    key: NativeObjectKey
    object_id: int
    kind: str
    skeleton_id: int
    child_keys: tuple[NativeObjectKey, ...]
    child_tokens: tuple[int, ...]
    lod_thresholds: tuple[float, ...]
    regular: NativeRegularSkinnedResource | None


@dataclass(frozen=True, slots=True)
class NativeSkinnedModelRoot:
    key: NativeObjectKey
    object_id: int
    kind: str
    skeleton_token: int
    selected_leaf_keys: tuple[NativeObjectKey, ...]


@dataclass(frozen=True, slots=True)
class NativeSkinnedModel:
    header: CoreHeader
    sections: tuple[NativeCoreSection, ...]
    resources: tuple[NativeSkinnedResourceNode, ...]
    root_key: NativeObjectKey
    root_token: int
    selected_leaf_keys: tuple[NativeObjectKey, ...]
    skeleton: NativeSkeleton
    textures: tuple[NativeLevelTexture, ...]
    logical_textures: tuple[NativeLevelTexture, ...]
    animated_textures: tuple[NativeAnimatedTexture, ...]
    materials: tuple[NativeMaterial, ...]
    material_plan: NativeLevelMaterialPlan
    packet_shapes: tuple[tuple[tuple[int, ...], int], ...]
    packet_count: int
    triangle_count: int
    vertex_count: int
    source_core_sha256: str
    source_corestream_sha256: str


def _require_range(data: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise CoreFormatError(f"{label} is truncated")


def _resolve_resource_token(
    token: int,
    nodes: tuple[NativeSkinnedResourceNode, ...],
) -> NativeSkinnedResourceNode:
    matches = tuple(node for node in nodes if node.object_id == token)
    if len(matches) != 1:
        raise CoreFormatError(
            f"Skinned resource token 0x{token:08X} resolves to "
            f"{len(matches)} supported graph nodes"
        )
    return matches[0]


def _parse_multi(
    blob: bytes,
) -> tuple[int, tuple[int, ...]]:
    _require_range(blob, 0, 0x2C, "Multi-skinned resource")
    part_count = _U32.unpack_from(blob, 0)[0]
    if part_count == 0:
        raise CoreFormatError("Multi-skinned resource has no parts")
    skeleton_id = _U32.unpack_from(blob, 0x28)[0]
    if skeleton_id == 0:
        raise CoreFormatError("Multi-skinned resource has a null Skeleton")
    required = 0x2C + part_count * 4
    _require_range(blob, 0, required, "Multi-skinned part array")
    if any(blob[required:]):
        raise CoreFormatError("Multi-skinned resource has a non-zero tail")
    return (
        skeleton_id,
        struct.unpack_from(f"<{part_count}I", blob, 0x2C),
    )


def _parse_lod(
    blob: bytes,
) -> tuple[int, tuple[int, ...], tuple[float, ...]]:
    _require_range(blob, 0, 0x30, "LOD-skinned resource")
    mesh_count, lod_count = struct.unpack_from("<2I", blob, 0)
    if mesh_count == 0 or mesh_count != lod_count:
        raise CoreFormatError("LOD-skinned resource counts are inconsistent")
    skeleton_id = _U32.unpack_from(blob, 0x2C)[0]
    mesh_offset = 0x30
    threshold_offset = mesh_offset + mesh_count * 4
    required = threshold_offset + lod_count * 4
    _require_range(blob, 0, required, "LOD-skinned arrays")
    child_tokens = struct.unpack_from(
        f"<{mesh_count}I", blob, mesh_offset
    )
    thresholds = struct.unpack_from(
        f"<{lod_count}f", blob, threshold_offset
    )
    if not all(math.isfinite(value) for value in thresholds):
        raise CoreFormatError("LOD-skinned thresholds are not finite")
    if any(blob[required:]):
        raise CoreFormatError("LOD-skinned resource has a non-zero tail")
    return skeleton_id, child_tokens, thresholds


def _select_leaves(
    root: NativeSkinnedResourceNode,
    resources: tuple[NativeSkinnedResourceNode, ...],
) -> tuple[tuple[NativeObjectKey, ...], int]:
    nodes_by_key = {node.key: node for node in resources}
    selected: list[NativeObjectKey] = []
    selected_skeletons: set[int] = set()

    def visit(
        key: NativeObjectKey,
        inherited_skeleton: int,
        active: set[NativeObjectKey],
    ) -> None:
        if key in active:
            raise CoreFormatError("Skinned resource graph contains a cycle")
        node = nodes_by_key[key]
        skeleton_id = node.skeleton_id or inherited_skeleton
        if (
            inherited_skeleton
            and node.skeleton_id
            and node.skeleton_id != inherited_skeleton
        ):
            raise CoreFormatError(
                "Selected skinned graph contains conflicting Skeletons"
            )
        if node.kind == "regular":
            if skeleton_id == 0:
                raise CoreFormatError(
                    "Selected skinned leaf has no Skeleton context"
                )
            selected.append(key)
            selected_skeletons.add(skeleton_id)
            return
        if node.kind not in ("multi", "lod"):
            raise CoreFormatError(
                f"Selected skinned graph reaches unsupported "
                f"{node.kind}"
            )
        next_active = active | {key}
        child_keys = (
            node.child_keys[:1]
            if node.kind == "lod"
            else node.child_keys
        )
        for child_key in child_keys:
            visit(child_key, skeleton_id, next_active)

    visit(root.key, root.skeleton_id, set())
    if len(selected_skeletons) != 1:
        raise CoreFormatError(
            "Selected skinned graph does not resolve to one Skeleton"
        )
    return tuple(selected), next(iter(selected_skeletons))


def _parse_skinned_resource_graph(
    sections: tuple[NativeCoreSection, ...],
) -> tuple[
    tuple[NativeSkinnedResourceNode, ...],
    dict[NativeObjectKey, NativeCoreObject],
]:
    objects = tuple(obj for section in sections for obj in section.objects)
    resource_objects = tuple(
        obj
        for obj in objects
        if obj.type_name in _SKINNED_RESOURCE_TYPES
    )
    preliminary: list[NativeSkinnedResourceNode] = []
    objects_by_key: dict[NativeObjectKey, NativeCoreObject] = {}
    for obj in resource_objects:
        objects_by_key[obj.key] = obj
        if obj.type_name == "RegularSkinnedMeshResource":
            _require_range(obj.blob, 0x24, 4, "Regular-skinned Skeleton")
            preliminary.append(
                NativeSkinnedResourceNode(
                    key=obj.key,
                    object_id=obj.object_id,
                    kind="regular",
                    skeleton_id=_U32.unpack_from(obj.blob, 0x24)[0],
                    child_keys=(),
                    child_tokens=(),
                    lod_thresholds=(),
                    regular=None,
                )
            )
        elif obj.type_name == "MultiSkinnedMeshResource":
            skeleton_id, child_tokens = _parse_multi(obj.blob)
            preliminary.append(
                NativeSkinnedResourceNode(
                    key=obj.key,
                    object_id=obj.object_id,
                    kind="multi",
                    skeleton_id=skeleton_id,
                    child_keys=(),
                    child_tokens=child_tokens,
                    lod_thresholds=(),
                    regular=None,
                )
            )
        elif obj.type_name == "LodSkinnedMeshResource":
            skeleton_id, child_tokens, thresholds = _parse_lod(obj.blob)
            preliminary.append(
                NativeSkinnedResourceNode(
                    key=obj.key,
                    object_id=obj.object_id,
                    kind="lod",
                    skeleton_id=skeleton_id,
                    child_keys=(),
                    child_tokens=child_tokens,
                    lod_thresholds=thresholds,
                    regular=None,
                )
            )
        else:
            preliminary.append(
                NativeSkinnedResourceNode(
                    key=obj.key,
                    object_id=obj.object_id,
                    kind=obj.type_name,
                    skeleton_id=0,
                    child_keys=(),
                    child_tokens=(),
                    lod_thresholds=(),
                    regular=None,
                )
            )

    preliminary_tuple = tuple(preliminary)
    resolved = tuple(
        NativeSkinnedResourceNode(
            key=node.key,
            object_id=node.object_id,
            kind=node.kind,
            skeleton_id=node.skeleton_id,
            child_keys=tuple(
                _resolve_resource_token(token, preliminary_tuple).key
                for token in node.child_tokens
            ),
            child_tokens=node.child_tokens,
            lod_thresholds=node.lod_thresholds,
            regular=None,
        )
        for node in preliminary_tuple
    )
    return resolved, objects_by_key


def discover_native_skinned_model_roots(
    sections: tuple[NativeCoreSection, ...],
) -> tuple[NativeSkinnedModelRoot, ...]:
    resources, _ = _parse_skinned_resource_graph(sections)
    incoming = {
        child_key
        for node in resources
        for child_key in node.child_keys
    }
    roots = tuple(node for node in resources if node.key not in incoming)
    objects = tuple(obj for section in sections for obj in section.objects)
    discovered: list[NativeSkinnedModelRoot] = []
    for root in roots:
        if root.kind not in ("regular", "multi", "lod"):
            continue
        try:
            selected_leaf_keys, skeleton_token = _select_leaves(
                root,
                resources,
            )
        except CoreFormatError:
            continue
        skeleton_count = sum(
            obj.type_name == "Skeleton" and obj.object_id == skeleton_token
            for obj in objects
        )
        if skeleton_count != 1:
            continue
        discovered.append(
            NativeSkinnedModelRoot(
                key=root.key,
                object_id=root.object_id,
                kind=root.kind,
                skeleton_token=skeleton_token,
                selected_leaf_keys=selected_leaf_keys,
            )
        )
    token_counts = Counter(root.object_id for root in discovered)
    return tuple(
        root for root in discovered if token_counts[root.object_id] == 1
    )


def parse_native_skinned_model(
    core_data: bytes,
    corestream_data: bytes,
    *,
    root_token: int | None = None,
) -> NativeSkinnedModel:
    if len(corestream_data) < 0x40:
        raise CoreFormatError("Native model CoreStream header is truncated")
    if corestream_data[: len(CORE_STREAM_MAGIC)] != CORE_STREAM_MAGIC:
        raise CoreFormatError("Native model CoreStream magic is invalid")
    if any(corestream_data[len(CORE_STREAM_MAGIC) : 0x40]):
        raise CoreFormatError(
            "Native model CoreStream reserved header bytes are non-zero"
        )

    header, sections = parse_native_core_sections(
        core_data, len(corestream_data)
    )
    objects = tuple(obj for section in sections for obj in section.objects)
    resolved, objects_by_key = _parse_skinned_resource_graph(sections)
    if not resolved:
        raise CoreFormatError("Native model has no supported skinned resources")
    roots = discover_native_skinned_model_roots(sections)
    if not roots:
        raise CoreFormatError(
            "Native model has no uniquely selectable skinned roots"
        )
    if root_token is None:
        if len(roots) != 1:
            raise CoreFormatError(
                "Native model has multiple skinned roots; select a root token"
            )
        root_info = roots[0]
    else:
        matches = tuple(
            root for root in roots if root.object_id == root_token
        )
        if len(matches) != 1:
            raise CoreFormatError(
                f"Skinned root token 0x{root_token:08X} resolves to "
                f"{len(matches)} graph roots"
            )
        root_info = matches[0]
    root = next(node for node in resolved if node.key == root_info.key)
    selected_leaf_keys = root_info.selected_leaf_keys
    skeleton_token = root_info.skeleton_token
    skeleton_matches = tuple(
        obj
        for obj in objects
        if obj.type_name == "Skeleton" and obj.object_id == skeleton_token
    )
    if len(skeleton_matches) != 1:
        raise CoreFormatError(
            f"Skeleton token 0x{skeleton_token:08X} resolves to "
            f"{len(skeleton_matches)} objects"
        )
    skeleton_object = skeleton_matches[0]
    skeleton = parse_native_skeleton(
        skeleton_object.blob,
        object_id=skeleton_object.object_id,
        core_string_count=len(header.strings),
    )

    selected_key_set = set(selected_leaf_keys)
    resources: list[NativeSkinnedResourceNode] = []
    for node in resolved:
        regular = None
        if node.key in selected_key_set:
            obj = objects_by_key[node.key]
            regular = parse_regular_skinned_resource(
                obj.blob,
                corestream_data,
                object_id=obj.object_id,
                stream_offset=obj.stream_offset,
                stream_size=obj.stream_size,
                skeleton_bone_count=skeleton.bone_count,
            )
        resources.append(
            NativeSkinnedResourceNode(
                key=node.key,
                object_id=node.object_id,
                kind=node.kind,
                skeleton_id=node.skeleton_id,
                child_keys=node.child_keys,
                child_tokens=node.child_tokens,
                lod_thresholds=node.lod_thresholds,
                regular=regular,
            )
        )
    resource_tuple = tuple(resources)
    resources_by_key = {node.key: node for node in resource_tuple}
    selected_resource_list: list[NativeRegularSkinnedResource] = []
    for key in selected_leaf_keys:
        regular = resources_by_key[key].regular
        if regular is None:
            raise AssertionError("Selected skinned leaf was not decoded")
        selected_resource_list.append(regular)
    selected_resources = tuple(selected_resource_list)
    material_tokens = tuple(
        sorted(
            {
                record.material_id
                for resource in selected_resources
                for record in resource.records
            }
        )
    )
    textures, logical_textures = parse_level_textures(
        objects, corestream_data
    )
    animated_textures = parse_animated_textures(
        objects, logical_textures
    )
    materials = parse_selected_materials(
        objects,
        material_tokens,
        logical_textures,
        animated_textures,
    )
    material_plan = build_level_material_plan(
        material_tokens,
        (),
        materials,
        animated_textures,
    )
    shape_counts = Counter(
        packet.tail_opcodes
        for resource in selected_resources
        for packet in resource.packets
    )
    return NativeSkinnedModel(
        header=header,
        sections=sections,
        resources=resource_tuple,
        root_key=root.key,
        root_token=root.object_id,
        selected_leaf_keys=selected_leaf_keys,
        skeleton=skeleton,
        textures=textures,
        logical_textures=logical_textures,
        animated_textures=animated_textures,
        materials=materials,
        material_plan=material_plan,
        packet_shapes=tuple(sorted(shape_counts.items())),
        packet_count=sum(
            len(resource.packets) for resource in selected_resources
        ),
        triangle_count=sum(
            resource.triangle_count for resource in selected_resources
        ),
        vertex_count=sum(
            len(packet.vertices)
            for resource in selected_resources
            for packet in resource.packets
        ),
        source_core_sha256=hashlib.sha256(core_data).hexdigest(),
        source_corestream_sha256=hashlib.sha256(
            corestream_data
        ).hexdigest(),
    )


def load_native_skinned_model(
    source_path: str | Path,
    model_path: str,
    *,
    root_token: int | None = None,
) -> NativeSkinnedModel:
    normalized_path = model_path.casefold()
    with KillzoneArchiveSource(source_path) as source:
        catalog = source.load_catalog()
        entries_by_path = {
            entry.path.as_posix().casefold(): entry
            for entry in catalog.entries
        }
        entry = entries_by_path.get(normalized_path)
        if entry is None:
            raise CoreFormatError(
                f"Native model Core is not in the catalog: {model_path}"
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
        return parse_native_skinned_model(
            core_data,
            corestream_data,
            root_token=root_token,
        )
    except CoreFormatError as exc:
        raise CoreFormatError(f"{model_path}: {exc}") from exc
