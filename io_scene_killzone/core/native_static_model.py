# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
from pathlib import Path

from .archive_source import KillzoneArchiveSource
from .core_format import CoreHeader
from .errors import CoreFormatError
from .native_level import (
    CORE_STREAM_MAGIC,
    NativeCoreSection,
    NativeObjectKey,
    NativeStaticRenderPlan,
    NativeStaticResourceNode,
    build_static_model_render_plan,
    expand_native_static_resource,
    native_static_resource_roots,
    parse_native_core_sections,
    parse_native_static_resources,
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


@dataclass(frozen=True, slots=True)
class NativeStaticModel:
    header: CoreHeader
    sections: tuple[NativeCoreSection, ...]
    resources: tuple[NativeStaticResourceNode, ...]
    root_key: NativeObjectKey
    root_token: int
    render_plan: NativeStaticRenderPlan
    textures: tuple[NativeLevelTexture, ...]
    logical_textures: tuple[NativeLevelTexture, ...]
    animated_textures: tuple[NativeAnimatedTexture, ...]
    materials: tuple[NativeMaterial, ...]
    material_plan: NativeLevelMaterialPlan
    packet_shapes: tuple[tuple[tuple[int, ...], int], ...]
    packet_count: int
    triangle_count: int
    source_core_sha256: str
    source_corestream_sha256: str


def _validate_corestream(corestream_data: bytes) -> None:
    if len(corestream_data) < 0x40:
        raise CoreFormatError("Native static model CoreStream header is truncated")
    if corestream_data[: len(CORE_STREAM_MAGIC)] != CORE_STREAM_MAGIC:
        raise CoreFormatError("Native static model CoreStream magic is invalid")
    if any(corestream_data[len(CORE_STREAM_MAGIC) : 0x40]):
        raise CoreFormatError(
            "Native static model CoreStream reserved header bytes are non-zero"
        )


def _select_root(
    resources: tuple[NativeStaticResourceNode, ...],
    root_token: int | None,
) -> NativeStaticResourceNode:
    nodes_by_key = {node.key: node for node in resources}
    roots = tuple(nodes_by_key[key] for key in native_static_resource_roots(resources))
    if not roots:
        raise CoreFormatError("Native static model has no supported graph roots")
    if root_token is None:
        if len(roots) != 1:
            raise CoreFormatError(
                "Native static model has multiple roots; select a root token"
            )
        return roots[0]
    matches = tuple(node for node in roots if node.object_id == root_token)
    if len(matches) != 1:
        raise CoreFormatError(
            f"Static root token 0x{root_token:08X} resolves to "
            f"{len(matches)} graph roots"
        )
    return matches[0]


def parse_native_static_model(
    core_data: bytes,
    corestream_data: bytes,
    *,
    root_token: int | None = None,
) -> NativeStaticModel:
    _validate_corestream(corestream_data)
    header, sections = parse_native_core_sections(
        core_data,
        len(corestream_data),
    )
    objects = tuple(obj for section in sections for obj in section.objects)
    graph = parse_native_static_resources(objects)
    if not graph:
        raise CoreFormatError("Native static model has no supported resources")
    root = _select_root(graph, root_token)
    selected_leaf_keys = set(expand_native_static_resource(root.key, graph))
    resources = parse_native_static_resources(
        objects,
        corestream_data,
        decode_keys=selected_leaf_keys,
    )
    render_plan = build_static_model_render_plan(resources, root.key)
    nodes_by_key = {node.key: node for node in resources}
    material_tokens = tuple(
        sorted(
            {
                record.material_id
                for occurrence in render_plan.direct_occurrences
                for record in nodes_by_key[occurrence.leaf_key].regular.records
            }
        )
    )
    textures, logical_textures = parse_level_textures(objects, corestream_data)
    animated_textures = parse_animated_textures(objects, logical_textures)
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
    selected_resources = tuple(
        nodes_by_key[key].regular for key in selected_leaf_keys
    )
    if any(resource is None for resource in selected_resources):
        raise AssertionError("Selected static model leaf was not decoded")
    decoded_resources = tuple(
        resource for resource in selected_resources if resource is not None
    )
    shape_counts = Counter(
        packet.tail_opcodes
        for resource in decoded_resources
        for packet in resource.packets
    )
    return NativeStaticModel(
        header=header,
        sections=sections,
        resources=resources,
        root_key=root.key,
        root_token=root.object_id,
        render_plan=render_plan,
        textures=textures,
        logical_textures=logical_textures,
        animated_textures=animated_textures,
        materials=materials,
        material_plan=material_plan,
        packet_shapes=tuple(sorted(shape_counts.items())),
        packet_count=render_plan.direct_packet_count,
        triangle_count=render_plan.direct_triangle_count,
        source_core_sha256=hashlib.sha256(core_data).hexdigest(),
        source_corestream_sha256=hashlib.sha256(corestream_data).hexdigest(),
    )


def load_native_static_model(
    source_path: str | Path,
    model_path: str,
    *,
    root_token: int | None = None,
) -> NativeStaticModel:
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
                f"Native static model Core is not in the catalog: {model_path}"
            )
        stream_path = entry.path.with_suffix(".corestream").as_posix()
        stream_entry = entries_by_path.get(stream_path.casefold())
        if stream_entry is None:
            raise CoreFormatError(
                f"Native static model CoreStream is missing: {stream_path}"
            )
        core_data = source.read_entry_range(entry, 0, entry.size)
        corestream_data = source.read_entry_range(
            stream_entry,
            0,
            stream_entry.size,
        )
    try:
        return parse_native_static_model(
            core_data,
            corestream_data,
            root_token=root_token,
        )
    except CoreFormatError as exc:
        raise CoreFormatError(f"{model_path}: {exc}") from exc
