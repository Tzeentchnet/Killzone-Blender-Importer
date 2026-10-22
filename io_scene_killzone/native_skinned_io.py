# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

from .core.native_level import NativeObjectKey
from .core.native_skinned_model import (
    NativeSkinnedModel,
    NativeSkinnedResourceNode,
    load_native_skinned_model,
)
from .native_level_io import (
    create_native_materials,
    enable_bounded_native_material_previews,
)


@dataclass(frozen=True, slots=True)
class NativeSkinnedImportResult:
    model: NativeSkinnedModel
    root_collection: bpy.types.Collection
    mesh_collection: bpy.types.Collection
    armature_object: bpy.types.Object
    mesh_objects: tuple[bpy.types.Object, ...]
    bone_names: tuple[str, ...]
    winding_corrections: int
    native_material_count: int
    decoded_image_count: int
    preview_counts: dict[str, int]


def native_inverse_bind_matrix(values: tuple[float, ...]) -> Matrix:
    source_row_matrix = Matrix(
        (
            (values[0], values[1], values[2], 0.0),
            (values[3], values[4], values[5], 0.0),
            (values[6], values[7], values[8], 0.0),
            (values[9], values[10], values[11], 1.0),
        )
    )
    return source_row_matrix.transposed()


def native_rest_matrix(values: tuple[float, ...]) -> Matrix:
    return native_inverse_bind_matrix(values).inverted()


def _bone_display_matrix(rest: Matrix) -> Matrix:
    basis = rest.to_3x3().normalized().to_4x4()
    basis.translation = rest.translation
    return basis


def _matrix_values(matrix: Matrix) -> list[float]:
    return [value for row in matrix for value in row]


def _diagnostic_material() -> bpy.types.Material:
    name = "Killzone Native Skinned Diagnostic"
    material = bpy.data.materials.get(name)
    if material is not None:
        return material
    material = bpy.data.materials.new(name)
    material.diffuse_color = (0.32, 0.05, 0.05, 1.0)
    material.use_nodes = True
    principled = next(
        (
            node
            for node in material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        ),
        None,
    )
    if principled is not None:
        principled.inputs["Base Color"].default_value = (
            0.32,
            0.05,
            0.05,
            1.0,
        )
        principled.inputs["Roughness"].default_value = 0.72
    material["killzone_diagnostic_material"] = True
    return material


def _corrected_triangle(
    triangle: tuple[int, int, int],
    positions: list[tuple[float, float, float]],
    normals: list[tuple[float, float, float]],
) -> tuple[tuple[int, int, int], bool]:
    first, second, third = triangle
    a = Vector(positions[first])
    b = Vector(positions[second])
    c = Vector(positions[third])
    cross = (b - a).cross(c - a)
    average = (
        Vector(normals[first])
        + Vector(normals[second])
        + Vector(normals[third])
    )
    if cross.length_squared > 0.0 and average.length_squared > 0.0:
        if cross.dot(average) < 0.0:
            return (first, third, second), True
    return triangle, False


def _create_armature(
    context: bpy.types.Context,
    model: NativeSkinnedModel,
    collection: bpy.types.Collection,
    display_name: str,
) -> tuple[bpy.types.Object, tuple[str, ...], tuple[Matrix, ...]]:
    skeleton = model.skeleton
    rest_matrices = tuple(
        native_rest_matrix(bone.temporary_transform)
        for bone in skeleton.bones
    )
    armature = bpy.data.armatures.new(f"{display_name} Skeleton")
    armature_object = bpy.data.objects.new(
        f"{display_name} Armature",
        armature,
    )
    collection.objects.link(armature_object)
    armature_object.show_in_front = True
    armature_object["killzone_object_id"] = (
        f"0x{skeleton.object_id:08X}"
    )
    armature_object["killzone_bone_count"] = skeleton.bone_count
    armature_object["killzone_attachment_count"] = len(
        skeleton.attachments
    )
    armature_object["killzone_bind_interpretation"] = (
        "serialized row-vector global inverse-bind; transposed for Blender"
    )
    armature_object["killzone_skinning_model"] = (
        "one source-order bone influence per target at weight 1.0"
    )

    previous_active = context.view_layer.objects.active
    previously_selected = tuple(context.selected_objects)
    for obj in previously_selected:
        obj.select_set(False)
    armature_object.select_set(True)
    context.view_layer.objects.active = armature_object
    bpy.ops.object.mode_set(mode="EDIT")

    children: dict[int, list[int]] = defaultdict(list)
    for bone in skeleton.bones:
        if bone.parent_index is not None:
            children[bone.parent_index].append(bone.index)

    edit_bones: list[bpy.types.EditBone] = []
    bone_names: list[str] = []
    for bone in skeleton.bones:
        edit_bone = armature.edit_bones.new(bone.name)
        edit_bone.matrix = _bone_display_matrix(rest_matrices[bone.index])
        child_distances = tuple(
            (
                rest_matrices[child_index].translation
                - rest_matrices[bone.index].translation
            ).length
            for child_index in children.get(bone.index, ())
        )
        if child_distances:
            display_length = min(child_distances)
        elif bone.parent_index is not None:
            display_length = (
                rest_matrices[bone.index].translation
                - rest_matrices[bone.parent_index].translation
            ).length * 0.35
        else:
            display_length = 0.1
        edit_bone.length = min(max(display_length, 0.02), 0.25)
        edit_bones.append(edit_bone)
        bone_names.append(edit_bone.name)
    for bone, edit_bone in zip(skeleton.bones, edit_bones):
        if bone.parent_index is not None:
            edit_bone.parent = edit_bones[bone.parent_index]
            edit_bone.use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")

    for bone, bone_name, rest in zip(
        skeleton.bones,
        bone_names,
        rest_matrices,
    ):
        blender_bone = armature.bones[bone_name]
        blender_bone["killzone_source_index"] = bone.index
        blender_bone["killzone_source_name"] = bone.name
        blender_bone["killzone_category"] = bone.category
        blender_bone["killzone_category_flags"] = (
            f"0x{bone.category_flags:08X}"
        )
        blender_bone["killzone_hull_slot"] = (
            -1 if bone.hull_slot is None else bone.hull_slot
        )
        blender_bone["killzone_hull_name"] = bone.hull_name
        blender_bone["killzone_parent_index"] = (
            -1 if bone.parent_index is None else bone.parent_index
        )
        blender_bone["killzone_temporary_transform_f32"] = list(
            bone.temporary_transform
        )
        blender_bone["killzone_inverse_bind_global_matrix"] = (
            _matrix_values(
                native_inverse_bind_matrix(bone.temporary_transform)
            )
        )
        blender_bone["killzone_rest_global_matrix"] = _matrix_values(rest)
        blender_bone["killzone_candidate_rest_matrix"] = _matrix_values(
            rest
        )

    armature_object.select_set(False)
    for obj in previously_selected:
        if obj.name in bpy.data.objects:
            obj.select_set(True)
    context.view_layer.objects.active = (
        previous_active
        if previous_active is not None
        and previous_active.name in bpy.data.objects
        else None
    )
    return armature_object, tuple(bone_names), rest_matrices


def _build_skinned_mesh(
    node: NativeSkinnedResourceNode,
    rest_matrices: tuple[Matrix, ...],
    materials_by_token: dict[int, bpy.types.Material],
    fallback_material: bpy.types.Material,
) -> tuple[bpy.types.Mesh, tuple[int, ...], int]:
    resource = node.regular
    if resource is None:
        raise ValueError("Only selected regular skinned leaves can create meshes")

    positions: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    bone_indices: list[int] = []
    controls: list[int] = []
    adc_values: list[bool] = []
    colors: list[tuple[int, int, int, int] | None] = []
    coordinates: list[tuple[tuple[float, float], ...]] = []
    triangles: list[tuple[int, int, int]] = []
    material_ids: list[int] = []
    winding_corrections = 0

    for packet in resource.packets:
        base = len(positions)
        for vertex in packet.vertices:
            rest = rest_matrices[vertex.bone_index]
            position = rest @ Vector(vertex.local_position)
            normal = rest.to_3x3() @ Vector(vertex.local_normal)
            if normal.length_squared == 0.0:
                raise ValueError("Decoded skinned vertex has a zero rest normal")
            normal.normalize()
            positions.append(tuple(position))
            normals.append(tuple(normal))
            bone_indices.append(vertex.bone_index)
            controls.append(vertex.source_control)
            adc_values.append(vertex.adc)
            colors.append(vertex.color)
            coordinates.append(vertex.coordinates)
        material_id = resource.records[
            packet.mesh_record_index
        ].material_id
        for packet_triangle in packet.triangles:
            triangle = tuple(base + index for index in packet_triangle)
            corrected, changed = _corrected_triangle(
                triangle,
                positions,
                normals,
            )
            triangles.append(corrected)
            material_ids.append(material_id)
            winding_corrections += int(changed)

    if len(triangles) != resource.triangle_count:
        raise ValueError(
            "Blender skinned mesh triangle count disagrees with source"
        )
    mesh = bpy.data.meshes.new(
        f"Skinned 0x{node.object_id:08X} "
        f"[{node.key.section_offset:06X}:{node.key.object_index}]"
    )
    mesh.from_pydata(positions, (), triangles)
    mesh.update()

    material_slots: dict[int, int] = {}
    for material_id in material_ids:
        if material_id in material_slots:
            continue
        material_slots[material_id] = len(mesh.materials)
        mesh.materials.append(
            materials_by_token.get(material_id, fallback_material)
        )
    for polygon, material_id in zip(mesh.polygons, material_ids):
        polygon.material_index = material_slots[material_id]

    if hasattr(mesh, "normals_split_custom_set_from_vertices"):
        mesh.normals_split_custom_set_from_vertices(normals)

    uv_count = max((len(value) for value in coordinates), default=0)
    for uv_index in range(uv_count):
        uv_layer = mesh.uv_layers.new(name=f"Native UV{uv_index}")
        for loop in mesh.loops:
            vertex_coordinates = coordinates[loop.vertex_index]
            uv_layer.data[loop.index].uv = (
                vertex_coordinates[uv_index]
                if uv_index < len(vertex_coordinates)
                else (0.0, 0.0)
            )

    if any(color is not None for color in colors):
        color_attribute = mesh.color_attributes.new(
            name="Native V4-8",
            type="BYTE_COLOR",
            domain="POINT",
        )
        for index, color in enumerate(colors):
            value = color if color is not None else (128, 128, 128, 128)
            color_attribute.data[index].color = tuple(
                component / 255.0 for component in value
            )

    bone_attribute = mesh.attributes.new(
        name="killzone_bone_index",
        type="INT",
        domain="POINT",
    )
    control_attribute = mesh.attributes.new(
        name="killzone_source_control",
        type="INT",
        domain="POINT",
    )
    adc_attribute = mesh.attributes.new(
        name="killzone_adc",
        type="BOOLEAN",
        domain="POINT",
    )
    for index, (bone_index, control, adc) in enumerate(
        zip(bone_indices, controls, adc_values)
    ):
        bone_attribute.data[index].value = bone_index
        control_attribute.data[index].value = control
        adc_attribute.data[index].value = adc

    material_attribute = mesh.attributes.new(
        name="killzone_material_id",
        type="INT",
        domain="FACE",
    )
    for index, material_id in enumerate(material_ids):
        material_attribute.data[index].value = material_id

    mesh["killzone_object_id"] = f"0x{node.object_id:08X}"
    mesh["killzone_section_offset"] = node.key.section_offset
    mesh["killzone_object_index"] = node.key.object_index
    mesh["killzone_packet_count"] = len(resource.packets)
    mesh["killzone_triangle_count"] = resource.triangle_count
    mesh["killzone_vertex_count"] = len(positions)
    mesh["killzone_uv_layer_count"] = uv_count
    mesh["killzone_winding_corrections"] = winding_corrections
    mesh["killzone_weight_model"] = "single rigid influence, weight 1.0"
    mesh["killzone_unweighted_vertex_count"] = 0
    mesh["killzone_out_of_range_bone_count"] = 0
    mesh["killzone_packet_shapes"] = str(
        sorted({packet.tail_opcodes for packet in resource.packets})
    )
    return mesh, tuple(bone_indices), winding_corrections


def import_native_skinned_model_data(
    context: bpy.types.Context,
    model: NativeSkinnedModel,
    *,
    model_path: str,
    source_path: str | Path | None = None,
) -> NativeSkinnedImportResult:
    display_name = Path(model_path).name.removesuffix(".core")
    root_collection = bpy.data.collections.new(
        f"Killzone {display_name}"
    )
    mesh_collection = bpy.data.collections.new("Skinned Meshes (LOD0)")
    context.scene.collection.children.link(root_collection)
    root_collection.children.link(mesh_collection)
    root_collection["killzone_source_path"] = model_path
    root_collection["killzone_core_sha256"] = (
        model.source_core_sha256
    )
    root_collection["killzone_corestream_sha256"] = (
        model.source_corestream_sha256
    )
    root_collection["killzone_root_token"] = (
        f"0x{model.root_token:08X}"
    )
    root_collection["killzone_skeleton_token"] = (
        f"0x{model.skeleton.object_id:08X}"
    )
    root_collection["killzone_bone_count"] = model.skeleton.bone_count
    root_collection["killzone_mesh_part_count"] = len(
        model.selected_leaf_keys
    )
    root_collection["killzone_packet_count"] = model.packet_count
    root_collection["killzone_triangle_count"] = model.triangle_count
    root_collection["killzone_vertex_count"] = model.vertex_count
    root_collection["killzone_packet_shapes"] = str(model.packet_shapes)
    root_collection["killzone_lod_policy"] = "LOD child zero"
    root_collection["killzone_bind_interpretation"] = (
        "RestGlobal = inverse(InverseBindGlobal)"
    )

    armature_object, bone_names, rest_matrices = _create_armature(
        context,
        model,
        root_collection,
        display_name,
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
    mesh_objects: list[bpy.types.Object] = []
    winding_corrections = 0
    for part_index, key in enumerate(model.selected_leaf_keys):
        node = resources_by_key[key]
        mesh, bone_indices, corrections = _build_skinned_mesh(
            node,
            rest_matrices,
            materials_by_token,
            fallback_material,
        )
        winding_corrections += corrections
        obj = bpy.data.objects.new(
            f"Part {part_index:02d} 0x{node.object_id:08X}",
            mesh,
        )
        mesh_collection.objects.link(obj)
        obj["killzone_part_index"] = part_index
        obj["killzone_resource_token"] = f"0x{node.object_id:08X}"
        obj["killzone_skeleton_token"] = (
            f"0x{model.skeleton.object_id:08X}"
        )
        obj["killzone_vertex_count"] = len(mesh.vertices)
        obj["killzone_triangle_count"] = len(mesh.polygons)
        obj["killzone_weight_sum_min"] = 1.0
        obj["killzone_weight_sum_max"] = 1.0
        obj["killzone_unweighted_vertex_count"] = 0

        indices_by_bone: dict[int, list[int]] = defaultdict(list)
        for vertex_index, bone_index in enumerate(bone_indices):
            if not 0 <= bone_index < len(bone_names):
                raise ValueError("Skinned mesh has an out-of-range bone index")
            indices_by_bone[bone_index].append(vertex_index)
        for bone_index, vertex_indices in indices_by_bone.items():
            group = obj.vertex_groups.new(name=bone_names[bone_index])
            group.add(vertex_indices, 1.0, "REPLACE")
        modifier = obj.modifiers.new(
            name="Killzone Armature",
            type="ARMATURE",
        )
        modifier.object = armature_object
        modifier.use_vertex_groups = True
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
    return NativeSkinnedImportResult(
        model=model,
        root_collection=root_collection,
        mesh_collection=mesh_collection,
        armature_object=armature_object,
        mesh_objects=tuple(mesh_objects),
        bone_names=bone_names,
        winding_corrections=winding_corrections,
        native_material_count=len(materials_by_token),
        decoded_image_count=len(images_by_token),
        preview_counts=preview_counts,
    )


def import_native_skinned_model(
    context: bpy.types.Context,
    source_path: str | Path,
    model_path: str,
    *,
    root_token: int | None = None,
) -> NativeSkinnedImportResult:
    model = load_native_skinned_model(
        source_path,
        model_path,
        root_token=root_token,
    )
    return import_native_skinned_model_data(
        context,
        model,
        model_path=model_path,
        source_path=source_path,
    )
