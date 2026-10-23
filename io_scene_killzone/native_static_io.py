# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import bpy

from .core.native_level import NativeObjectKey
from .core.native_static_model import (
    NativeStaticModel,
    load_native_static_model,
)
from .native_level_io import (
    _build_resource_mesh,
    _diagnostic_material,
    _object_matrix,
    create_native_materials,
    enable_bounded_native_material_previews,
    set_native_import_provenance,
)


@dataclass(frozen=True, slots=True)
class NativeStaticImportResult:
    model: NativeStaticModel
    root_collection: bpy.types.Collection
    mesh_collection: bpy.types.Collection
    mesh_objects: tuple[bpy.types.Object, ...]
    unique_mesh_count: int
    winding_corrections: int
    native_material_count: int
    decoded_image_count: int
    preview_counts: dict[str, int]


def import_native_static_model_data(
    context: bpy.types.Context,
    model: NativeStaticModel,
    *,
    model_path: str,
    source_path: str | Path | None = None,
) -> NativeStaticImportResult:
    display_name = Path(model_path).name.removesuffix(".core")
    root_collection = bpy.data.collections.new(f"Killzone {display_name}")
    mesh_collection = bpy.data.collections.new("Static Meshes (LOD0)")
    context.scene.collection.children.link(root_collection)
    root_collection.children.link(mesh_collection)
    set_native_import_provenance(
        root_collection,
        product="standalone-static-definition-preview",
        operator="kz.import_model",
        level_placement_context_loaded=False,
    )
    root_collection["killzone_source_path"] = model_path
    root_collection["killzone_core_sha256"] = model.source_core_sha256
    root_collection["killzone_corestream_sha256"] = (
        model.source_corestream_sha256
    )
    root_collection["killzone_model_kind"] = "static"
    root_collection["killzone_root_token"] = f"0x{model.root_token:08X}"
    root_collection["killzone_mesh_part_count"] = len(
        model.render_plan.direct_occurrences
    )
    root_collection["killzone_packet_count"] = model.packet_count
    root_collection["killzone_triangle_count"] = model.triangle_count
    root_collection["killzone_packet_shapes"] = str(model.packet_shapes)
    root_collection["killzone_lod_policy"] = "LOD child zero"
    root_collection["killzone_definition_preview"] = True
    root_collection["killzone_definition_occurrence_count"] = len(
        model.render_plan.direct_occurrences
    )

    selected_texture_tokens = (
        set(model.material_plan.direct_texture_tokens)
        | set(model.material_plan.instance_texture_tokens)
    )
    materials_by_token, images_by_token = create_native_materials(
        model.logical_textures,
        model.animated_textures,
        model.materials,
        selected_texture_tokens=selected_texture_tokens,
        asset_path=model_path,
        source_path=source_path,
    )
    fallback_material = _diagnostic_material()
    resources_by_key = {node.key: node for node in model.resources}
    mesh_cache: dict[NativeObjectKey, bpy.types.Mesh] = {}
    mesh_objects: list[bpy.types.Object] = []
    winding_corrections = 0
    for part_index, occurrence in enumerate(
        model.render_plan.direct_occurrences
    ):
        node = resources_by_key[occurrence.leaf_key]
        mesh = mesh_cache.get(node.key)
        if mesh is None:
            mesh, corrections = _build_resource_mesh(
                node,
                materials_by_token,
                fallback_material,
            )
            mesh_cache[node.key] = mesh
            winding_corrections += corrections
        obj = bpy.data.objects.new(
            f"Part {part_index:02d} 0x{node.object_id:08X}",
            mesh,
        )
        obj.matrix_world = _object_matrix(occurrence.matrix)
        obj["killzone_part_index"] = part_index
        obj["killzone_resource_token"] = f"0x{node.object_id:08X}"
        obj["killzone_model_kind"] = "static"
        obj["killzone_occurrence_kind"] = (
            "standalone-definition-preview"
        )
        obj["killzone_level_placement_context_loaded"] = False
        mesh_collection.objects.link(obj)
        mesh_objects.append(obj)

    preview_counts = enable_bounded_native_material_previews(
        materials_by_token.values(),
        mesh_objects,
        include_write=True,
        include_color_diagnostics=True,
    )
    root_collection["killzone_native_material_count"] = len(
        materials_by_token
    )
    root_collection["killzone_decoded_image_count"] = len(images_by_token)
    root_collection["killzone_material_preview_counts"] = str(
        preview_counts
    )
    root_collection["killzone_winding_corrections"] = winding_corrections
    root_collection["killzone_unique_leaf_mesh_count"] = len(mesh_cache)
    return NativeStaticImportResult(
        model=model,
        root_collection=root_collection,
        mesh_collection=mesh_collection,
        mesh_objects=tuple(mesh_objects),
        unique_mesh_count=len(mesh_cache),
        winding_corrections=winding_corrections,
        native_material_count=len(materials_by_token),
        decoded_image_count=len(images_by_token),
        preview_counts=preview_counts,
    )


def import_native_static_model(
    context: bpy.types.Context,
    source_path: str | Path,
    model_path: str,
    *,
    root_token: int | None = None,
) -> NativeStaticImportResult:
    model = load_native_static_model(
        source_path,
        model_path,
        root_token=root_token,
    )
    return import_native_static_model_data(
        context,
        model,
        model_path=model_path,
        source_path=source_path,
    )
