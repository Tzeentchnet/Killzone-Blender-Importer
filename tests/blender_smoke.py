# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import struct
import sys
import tempfile
import traceback

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

import bpy
from mathutils import Matrix

import io_scene_killzone
import io_scene_killzone.native_animation_io as native_animation_io
from io_scene_killzone.core.bundle import Bundle
from io_scene_killzone.core.core_format import CoreHeader
from io_scene_killzone.core.native_animation_resource import (
    parse_native_animation_library,
)
from io_scene_killzone.core.native_level import (
    NativeLevel,
    NativeObjectKey,
    NativeSimpleSkinnedInstance,
    NativeStaticInstance,
    NativeStaticResourceNode,
    build_static_model_render_plan,
    build_static_render_plan,
)
from io_scene_killzone.core.native_level_material import (
    NativeLevelMaterialPlan,
)
from io_scene_killzone.core.native_static_geometry import (
    NativeRegularStaticResource,
    NativeStaticMeshRecord,
    NativeStaticPacket,
    NativeStaticVertex,
)
from io_scene_killzone.core.native_skeleton import parse_native_skeleton
from io_scene_killzone.core.native_skinned_geometry import (
    parse_regular_skinned_resource,
)
from io_scene_killzone.core.native_skinned_model import (
    NativeSkinnedModel,
    NativeSkinnedResourceNode,
)
from io_scene_killzone.core.native_static_model import NativeStaticModel
from io_scene_killzone.core.native_texture import parse_native_texture
from io_scene_killzone.native_level_io import (
    enable_bounded_native_material_previews,
    import_native_level_data,
    select_native_color_diagnostic_layer,
    supports_bounded_native_alphaadd_preview,
)
from io_scene_killzone.native_animation_io import (
    ANIMATION_WRITER_BULK,
    ANIMATION_WRITER_LEGACY,
    NativeAnimationBulkWriteError,
    bake_compatible_native_animation_library,
    bake_native_animation_action,
    bake_native_animation_action_bulk,
)
from io_scene_killzone.native_skinned_io import (
    import_native_skinned_model_data,
)
from io_scene_killzone.native_static_io import (
    import_native_static_model_data,
)
from io_scene_killzone.native_texture_io import create_native_texture_image
from io_scene_killzone.scene_io import export_bundle, import_bundle
from tests.support import (
    create_fixture_bundle,
    create_fixture_native_texture_core,
)
from tests.test_native_skinned import (
    _make_regular_resource,
    _make_skeleton_blob,
)
from tests.test_native_animation import _animation_library_fixture


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


def _synthetic_static_resource(
    key: NativeObjectKey,
    object_id: int,
    x_offset: float,
) -> NativeStaticResourceNode:
    positions = (
        (x_offset, 0.0, 0.0),
        (x_offset + 1.0, 0.0, 0.0),
        (x_offset, 1.0, 0.0),
    )
    vertices = tuple(
        NativeStaticVertex(
            raw_position=(index & 1, index >> 1, 0),
            position=position,
            normal=(0.0, 0.0, 1.0),
            adc=False,
            color=None,
            raw_coordinates=((0, 0),),
            coordinates=((position[0], position[1]),),
            auxiliary=None,
        )
        for index, position in enumerate(positions)
    )
    record = NativeStaticMeshRecord(
        material_id=0,
        triangle_count=1,
        packet_count=1,
        flag_a=1,
        attribute_flags=0,
        raw_bounds=(0, 0, 0, 1, 1, 0),
    )
    packet = NativeStaticPacket(
        mesh_record_index=0,
        packet_index=0,
        stream_chunk_index=0,
        relative_offset=0,
        absolute_offset=0,
        bounded_size=0,
        transfer_size=0,
        raw_bounds=record.raw_bounds,
        tail_opcodes=(),
        zero_padding_size=0,
        vertices=vertices,
        triangles=((0, 1, 2),),
        span_sha256="0" * 64,
    )
    resource = NativeRegularStaticResource(
        object_id=object_id,
        records=(record,),
        packets=(packet,),
        stream_chunk_sizes=(1,),
        root_flags=(False, False),
        position_matrix=_IDENTITY_MATRIX,
        position_scale=(1.0, 1.0, 1.0),
        position_translation=(0.0, 0.0, 0.0),
        blob_tail=b"",
    )
    return NativeStaticResourceNode(
        key=key,
        object_id=object_id,
        kind="regular",
        child_keys=(),
        child_tokens=(),
        lod_thresholds=(),
        regular=resource,
    )


def _synthetic_prop_level() -> NativeLevel:
    leaf_a = _synthetic_static_resource(
        NativeObjectKey(0x100, 0),
        0x1000,
        0.0,
    )
    leaf_b = _synthetic_static_resource(
        NativeObjectKey(0x200, 0),
        0x2000,
        2.0,
    )
    direct_leaf = _synthetic_static_resource(
        NativeObjectKey(0x300, 0),
        0x3000,
        4.0,
    )
    wrapper = NativeStaticResourceNode(
        key=NativeObjectKey(0x400, 0),
        object_id=0x4000,
        kind="multi",
        child_keys=(leaf_a.key, leaf_b.key),
        child_tokens=(leaf_a.object_id, leaf_b.object_id),
        lod_thresholds=(),
        regular=None,
    )
    placed_matrix = (
        2.0,
        0.0,
        0.0,
        0.0,
        0.0,
        3.0,
        0.0,
        0.0,
        0.0,
        0.0,
        4.0,
        0.0,
        10.0,
        20.0,
        30.0,
        1.0,
    )
    single_matrix = (
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
        -4.0,
        5.0,
        6.0,
        1.0,
    )
    static_instances = (
        NativeStaticInstance(
            key=NativeObjectKey(0x500, 0),
            object_id=0x5000,
            name="Duplicate Prop",
            name_index=2,
            name_is_identifier=True,
            parent_token=0,
            resource_token=wrapper.object_id,
            resource_key=wrapper.key,
            matrix=placed_matrix,
            footer=0x00030001,
            raw_blob=b"",
        ),
        NativeStaticInstance(
            key=NativeObjectKey(0x500, 1),
            object_id=0x5001,
            name="Duplicate Prop",
            name_index=2,
            name_is_identifier=True,
            parent_token=0,
            resource_token=leaf_a.object_id,
            resource_key=leaf_a.key,
            matrix=single_matrix,
            footer=1,
            raw_blob=b"",
        ),
        NativeStaticInstance(
            key=NativeObjectKey(0x500, 2),
            object_id=0x5002,
            name="Null Prop",
            name_index=3,
            name_is_identifier=False,
            parent_token=0,
            resource_token=0,
            resource_key=None,
            matrix=_IDENTITY_MATRIX,
            footer=1,
            raw_blob=b"",
        ),
    )
    resources = (leaf_a, leaf_b, direct_leaf, wrapper)
    render_plan = build_static_render_plan(resources, static_instances)
    skinned_instance = NativeSimpleSkinnedInstance(
        key=NativeObjectKey(0x600, 0),
        object_id=0x6000,
        name="Animated Prop",
        name_index=4,
        name_is_identifier=False,
        parent_token=0x7000,
        parent_key=NativeObjectKey(0x700, 0),
        resource_token=0x8000,
        resource_key=NativeObjectKey(0x800, 0),
        resource_type="RegularSkinnedMeshResource",
        default_animation_token=0x9000,
        default_animation_key=NativeObjectKey(0x900, 0),
        blend_shape_animation_token=0,
        matrix=placed_matrix,
        footer=0x00010001,
        raw_blob=b"",
    )
    header = CoreHeader(
        variant=5,
        string_table_size=0,
        strings=(),
        string_table_padding=0,
        body_offset=0,
        total_size=0,
        header_sha256="",
        original_header=b"",
    )
    return NativeLevel(
        header=header,
        sections=(),
        resources=resources,
        instances=static_instances,
        skinned_instances=(skinned_instance,),
        render_plan=render_plan,
        textures=(),
        logical_textures=(),
        animated_textures=(),
        materials=(),
        material_plan=NativeLevelMaterialPlan((), (), (), ()),
        packet_shapes=(),
        physical_packet_count=3,
        physical_triangle_count=3,
        source_core_sha256="1" * 64,
        source_corestream_sha256="2" * 64,
    )


def _synthetic_static_model() -> NativeStaticModel:
    level = _synthetic_prop_level()
    root = next(node for node in level.resources if node.kind == "multi")
    render_plan = build_static_model_render_plan(
        level.resources,
        root.key,
    )
    return NativeStaticModel(
        header=level.header,
        sections=(),
        resources=level.resources,
        root_key=root.key,
        root_token=root.object_id,
        render_plan=render_plan,
        textures=(),
        logical_textures=(),
        animated_textures=(),
        materials=(),
        material_plan=NativeLevelMaterialPlan((0,), (), (), ()),
        packet_shapes=(((0x65,), 2),),
        packet_count=render_plan.direct_packet_count,
        triangle_count=render_plan.direct_triangle_count,
        source_core_sha256="5" * 64,
        source_corestream_sha256="6" * 64,
    )


def _synthetic_skinned_model() -> NativeSkinnedModel:
    blob, stream = _make_regular_resource()
    regular = parse_regular_skinned_resource(
        blob,
        stream,
        object_id=0x3333,
        stream_offset=0,
        stream_size=len(stream),
        skeleton_bone_count=2,
    )
    skeleton = parse_native_skeleton(
        _make_skeleton_blob(),
        object_id=0x4444,
        core_string_count=2,
    )
    key = NativeObjectKey(0x700, 0)
    resource = NativeSkinnedResourceNode(
        key=key,
        object_id=regular.object_id,
        kind="regular",
        skeleton_id=skeleton.object_id,
        child_keys=(),
        child_tokens=(),
        lod_thresholds=(),
        regular=regular,
    )
    header = CoreHeader(
        variant=5,
        string_table_size=0,
        strings=(),
        string_table_padding=0,
        body_offset=0,
        total_size=0,
        header_sha256="",
        original_header=b"",
    )
    return NativeSkinnedModel(
        header=header,
        sections=(),
        resources=(resource,),
        root_key=key,
        root_token=resource.object_id,
        selected_leaf_keys=(key,),
        skeleton=skeleton,
        textures=(),
        logical_textures=(),
        animated_textures=(),
        materials=(),
        material_plan=NativeLevelMaterialPlan((), (), (), ()),
        packet_shapes=(((0x65,), 1),),
        packet_count=1,
        triangle_count=1,
        vertex_count=3,
        source_core_sha256="3" * 64,
        source_corestream_sha256="4" * 64,
    )


def _evaluated_coordinates(
    obj: bpy.types.Object,
) -> tuple[object, ...]:
    dependency_graph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(dependency_graph)
    mesh = evaluated.to_mesh()
    try:
        return tuple(vertex.co.copy() for vertex in mesh.vertices)
    finally:
        evaluated.to_mesh_clear()


def run() -> None:
    io_scene_killzone.register()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root = root / "input"
            manifest_path, original_mesh_bytes, original_source_bytes = (
                create_fixture_bundle(input_root)
            )

            objects = import_bundle(bpy.context, manifest_path)
            assert len(objects) == 1
            obj = objects[0]
            assert obj.type == "MESH"
            assert len(obj.data.vertices) == 4
            assert len(obj.data.polygons) == 2
            assert len(obj.data.uv_layers) == 1
            assert obj.data.color_attributes.get("Color") is not None
            assert obj["kz_native_export_supported"] is False

            unchanged_manifest = root / "unchanged" / "manifest.json"
            unchanged_document = export_bundle(
                bpy.context, unchanged_manifest, objects=objects
            )
            assert (
                unchanged_document["assets"]["meshes"][0]["edit_state"]
                == "unchanged"
            )
            assert (
                root / "unchanged" / "meshes" / "sample.kzm"
            ).read_bytes() == original_mesh_bytes
            assert (
                root / "unchanged" / "original" / "sample.mss"
            ).read_bytes() == original_source_bytes
            Bundle.load(unchanged_manifest).validate()

            obj.data.vertices[0].co.x += 0.25
            obj.data.update()
            modified_manifest = root / "modified" / "manifest.json"
            modified_document = export_bundle(
                bpy.context, modified_manifest, objects=objects
            )
            modified_entry = modified_document["assets"]["meshes"][0]
            assert modified_entry["edit_state"] == "modified"
            assert (
                modified_entry["source"]["status"]
                == "preserved-original-not-reencoded"
            )
            assert (
                root / "modified" / "original" / "sample.mss"
            ).read_bytes() == original_source_bytes

            modified_bundle = Bundle.load(modified_manifest)
            modified_bundle.validate()
            modified_mesh = modified_bundle.read_mesh(modified_bundle.meshes[0])
            assert modified_mesh.opaque_chunks[0].payload == b"preserve-this"
            assert any(
                abs(vertex.position[0] + 0.75) < 1e-6
                for vertex in modified_mesh.vertices
            )

            texture_data, _, logical_palette, _, _ = (
                create_fixture_native_texture_core()
            )
            texture = parse_native_texture(texture_data)
            image = create_native_texture_image(
                texture,
                source_path=root / "synthetic.iso",
                asset_path="fixtures/background.core",
            )
            assert tuple(image.size) == (512, 512)
            assert image["kz_native_encoding"] == "core-v1-psmt8-csm1"
            assert image["kz_native_export_supported"] is False
            assert image["kz_original_core_sha256"] == texture.source_sha256
            assert image.use_fake_user
            assert image.packed_file is not None
            assert image["kz_pixels_packed"] is True
            source_x = 32
            source_palette_index = 1
            blender_pixel = ((511 * 512) + source_x) * 4
            expected = logical_palette[
                source_palette_index * 4 : source_palette_index * 4 + 4
            ]
            actual = image.pixels[blender_pixel : blender_pixel + 4]
            assert all(
                abs(actual[index] - expected[index] / 255.0) < 1e-6
                for index in range(4)
            )

            prop_level = _synthetic_prop_level()
            level_result = import_native_level_data(
                bpy.context,
                prop_level,
                level_path="synthetic/level.props.core",
                import_textures=False,
            )
            assert (
                level_result.root_collection["killzone_textures_imported"]
                is False
            )
            assert level_result.material_preview_counts == {
                "write": 0,
                "alphaadd": 0,
            }
            assert (
                level_result.root_collection[
                    "killzone_visible_material_preview_count"
                ]
                == 0
            )
            assert level_result.props_collection.name == "Props"
            assert (
                level_result.root_collection.children.get("Props")
                == level_result.props_collection
            )
            assert (
                level_result.props_collection.children.get(
                    "Static Instances"
                )
                == level_result.instance_collection
            )
            assert (
                level_result.props_collection.children.get(
                    "Animated Skinned Instances (metadata only)"
                )
                == level_result.animated_instance_collection
            )
            assert len(level_result.static_instance_objects) == 3
            assert len(level_result.animated_instance_objects) == 1
            assert len(level_result.prop_objects) == 4
            assert len(level_result.objects) == 4
            assert all(obj.type == "MESH" for obj in level_result.objects)
            assert level_result.unique_mesh_count == 3

            multi_owner, repeated_owner, null_owner = (
                level_result.static_instance_objects
            )
            assert multi_owner["killzone_source_name"] == "Duplicate Prop"
            assert repeated_owner["killzone_source_name"] == "Duplicate Prop"
            assert multi_owner.name != repeated_owner.name
            assert multi_owner["killzone_selected_leaf_count"] == 2
            assert repeated_owner["killzone_selected_leaf_count"] == 1
            assert multi_owner.type == "EMPTY"
            assert repeated_owner.type == "MESH"
            assert repeated_owner.parent is None
            assert len(repeated_owner.children) == 0
            assert tuple(repeated_owner.location) == (-4.0, 5.0, 6.0)
            assert (
                repeated_owner["killzone_render_role"]
                == "authored-single-leaf-static-occurrence"
            )
            assert null_owner["killzone_resource_status"] == "null"
            assert null_owner.type == "EMPTY"
            assert len(null_owner.children) == 0

            expected_matrix = Matrix(
                (
                    prop_level.instances[0].matrix[0:4],
                    prop_level.instances[0].matrix[4:8],
                    prop_level.instances[0].matrix[8:12],
                    prop_level.instances[0].matrix[12:16],
                )
            ).transposed()
            assert all(
                abs(multi_owner.matrix_world[row][column]
                    - expected_matrix[row][column])
                < 1e-6
                for row in range(4)
                for column in range(4)
            )
            assert multi_owner["killzone_source_matrix_f32_hex"] == (
                struct.pack("<16f", *prop_level.instances[0].matrix).hex()
            )
            for child in multi_owner.children:
                assert all(
                    abs(child.matrix_local[row][column]
                        - Matrix.Identity(4)[row][column])
                    < 1e-6
                    for row in range(4)
                    for column in range(4)
                )
                assert all(
                    abs(child.matrix_world[row][column]
                        - multi_owner.matrix_world[row][column])
                    < 1e-6
                    for row in range(4)
                    for column in range(4)
                )

            first_leaf = next(
                child
                for child in multi_owner.children
                if child["killzone_leaf_object_id"] == "0x00001000"
            )
            assert repeated_owner["killzone_leaf_object_id"] == "0x00001000"
            assert first_leaf.data is repeated_owner.data

            direct_object = next(
                obj
                for obj in level_result.objects
                if obj["killzone_path"] == "direct"
            )
            assert not direct_object["killzone_explicit_instance"]
            assert (
                direct_object["killzone_occurrence_kind"]
                == "implicit-direct-world"
            )
            assert direct_object.parent is None

            animated_owner = level_result.animated_instance_objects[0]
            assert animated_owner.parent is None
            assert (
                animated_owner["killzone_parenting_status"]
                == "reference-preserved-composition-order-unresolved"
            )
            assert (
                animated_owner["killzone_resource_type"]
                == "RegularSkinnedMeshResource"
            )
            assert (
                animated_owner["killzone_default_animation_token"]
                == "0x00009000"
            )
            assert (
                level_result.root_collection["killzone_static_prop_count"]
                == 3
            )
            assert (
                level_result.root_collection["killzone_animated_prop_count"]
                == 1
            )
            assert level_result.root_collection["killzone_prop_count"] == 4

            static_model = _synthetic_static_model()
            static_result = import_native_static_model_data(
                bpy.context,
                static_model,
                model_path="synthetic/custom.static-prop.core",
            )
            assert len(static_result.mesh_objects) == 2
            assert static_result.unique_mesh_count == 2
            assert static_result.model.root_token == 0x4000
            assert (
                static_result.root_collection["killzone_model_kind"]
                == "static"
            )
            assert static_result.root_collection[
                "killzone_triangle_count"
            ] == 2

            skinned_model = _synthetic_skinned_model()
            skinned_result = import_native_skinned_model_data(
                bpy.context,
                skinned_model,
                model_path="synthetic/entity.rigged.core",
            )
            assert len(skinned_result.armature_object.data.bones) == 2
            assert skinned_result.bone_names == ("Root", "Child")
            assert len(skinned_result.mesh_objects) == 1
            skinned_object = skinned_result.mesh_objects[0]
            assert len(skinned_object.data.vertices) == 3
            assert len(skinned_object.data.polygons) == 1
            assert len(skinned_object.data.uv_layers) == 1
            assert all(
                len(vertex.groups) == 1
                and abs(vertex.groups[0].weight - 1.0) < 1.0e-7
                for vertex in skinned_object.data.vertices
            )
            child_group = skinned_object.vertex_groups.get("Child")
            assert child_group is not None
            assert all(
                vertex.groups[0].group == child_group.index
                for vertex in skinned_object.data.vertices
            )
            assert any(
                modifier.type == "ARMATURE"
                and modifier.object == skinned_result.armature_object
                for modifier in skinned_object.modifiers
            )

            dependency_graph = bpy.context.evaluated_depsgraph_get()
            evaluated = skinned_object.evaluated_get(dependency_graph)
            evaluated_mesh = evaluated.to_mesh()
            before = tuple(
                vertex.co.copy() for vertex in evaluated_mesh.vertices
            )
            evaluated.to_mesh_clear()
            child_pose = skinned_result.armature_object.pose.bones["Child"]
            child_pose.rotation_mode = "XYZ"
            child_pose.rotation_euler.x = 0.35
            dependency_graph.update()
            evaluated = skinned_object.evaluated_get(dependency_graph)
            evaluated_mesh = evaluated.to_mesh()
            after = tuple(
                vertex.co.copy() for vertex in evaluated_mesh.vertices
            )
            evaluated.to_mesh_clear()
            assert max(
                (after[index] - before[index]).length
                for index in range(len(before))
            ) > 1.0e-5
            child_pose.rotation_euler.x = 0.0
            dependency_graph.update()

            animation_core, animation_stream = (
                _animation_library_fixture()
            )
            animation_library = parse_native_animation_library(
                animation_core,
                animation_stream,
                core_path="synthetic/entity.animation.core",
            )
            action_result = bake_native_animation_action(
                bpy.context.scene,
                skinned_result.armature_object,
                skinned_model.skeleton,
                animation_library.clips[0],
                library=animation_library,
                bake_rate=2.0,
            )
            action = action_result.action
            animation_data = skinned_result.armature_object.animation_data
            assert animation_data.action == action
            assert animation_data.action_slot is not None
            assert len(action.slots) == 1
            assert action.slots[0].target_id_type == "OBJECT"
            assert action.use_fake_user is True
            assert action.use_cyclic is False
            assert action["killzone_duration_seconds"] == 1.0
            assert action["killzone_playback_mode"] == 2
            assert (
                action["killzone_root_trajectory_policy"]
                == "sampled-tracks-only"
            )
            assert action_result.sample_times == (0.0, 0.5, 1.0)
            channelbag = action.layers[0].strips[0].channelbag(
                animation_data.action_slot
            )
            assert channelbag is not None
            assert len(channelbag.fcurves) == 20
            assert all(
                point.interpolation == "LINEAR"
                for curve in channelbag.fcurves
                for point in curve.keyframe_points
            )
            assert all(
                curve.extrapolation == "CONSTANT"
                for curve in channelbag.fcurves
            )
            bpy.context.scene.frame_set(int(action_result.frame_start))
            dependency_graph.update()
            animated_start = _evaluated_coordinates(skinned_object)
            animation_middle_frame = int(
                (action_result.frame_start + action_result.frame_end) / 2.0
            )
            bpy.context.scene.frame_set(animation_middle_frame)
            dependency_graph.update()
            animated_middle = _evaluated_coordinates(skinned_object)
            bpy.context.scene.frame_set(int(action_result.frame_end))
            dependency_graph.update()
            animated_end = _evaluated_coordinates(skinned_object)
            assert max(
                (
                    animated_end[index] - animated_start[index]
                ).length
                for index in range(len(animated_start))
            ) > 0.5
            bulk_result = bake_native_animation_action_bulk(
                bpy.context.scene,
                skinned_result.armature_object,
                skinned_model.skeleton,
                animation_library.clips[0],
                library=animation_library,
                bake_rate=2.0,
            )
            assert bulk_result.writer == ANIMATION_WRITER_BULK
            assert (
                bulk_result.action["killzone_action_writer"]
                == "bulk-fcurve-foreach-set"
            )
            assert (
                bulk_result.action["killzone_constant_channel_policy"]
                == "exact-equality-single-key"
            )
            bulk_animation_data = (
                skinned_result.armature_object.animation_data
            )
            assert bulk_animation_data.action == bulk_result.action
            assert bulk_animation_data.action_slot is not None
            bulk_channelbag = (
                bulk_result.action.layers[0]
                .strips[0]
                .channelbag(bulk_animation_data.action_slot)
            )
            assert bulk_channelbag is not None
            assert len(bulk_channelbag.fcurves) == 20
            assert all(
                point.interpolation == "LINEAR"
                for curve in bulk_channelbag.fcurves
                for point in curve.keyframe_points
            )
            assert any(
                len(curve.keyframe_points) == 1
                for curve in bulk_channelbag.fcurves
            )
            bpy.context.scene.frame_set(int(bulk_result.frame_start))
            dependency_graph.update()
            bulk_start = _evaluated_coordinates(skinned_object)
            bpy.context.scene.frame_set(animation_middle_frame)
            dependency_graph.update()
            bulk_middle = _evaluated_coordinates(skinned_object)
            bpy.context.scene.frame_set(int(bulk_result.frame_end))
            dependency_graph.update()
            bulk_end = _evaluated_coordinates(skinned_object)
            assert all(
                (
                    bulk_start[index] - animated_start[index]
                ).length
                < 1.0e-6
                for index in range(len(bulk_start))
            )
            assert all(
                (
                    bulk_middle[index] - animated_middle[index]
                ).length
                < 1.0e-6
                for index in range(len(bulk_middle))
            )
            assert all(
                (bulk_end[index] - animated_end[index]).length
                < 1.0e-6
                for index in range(len(bulk_end))
            )
            repeat_result = bake_native_animation_action(
                bpy.context.scene,
                skinned_result.armature_object,
                skinned_model.skeleton,
                replace(
                    animation_library.clips[0],
                    playback_mode=1,
                ),
                library=animation_library,
                bake_rate=2.0,
                action_name="Synthetic Repeating Animation",
            )
            assert repeat_result.action.use_cyclic is True
            assert repeat_result.action.use_fake_user is True
            bpy.context.scene.frame_set(int(repeat_result.frame_start))
            dependency_graph.update()
            repeat_start = _evaluated_coordinates(skinned_object)
            bpy.context.scene.frame_set(int(repeat_result.frame_end))
            dependency_graph.update()
            repeat_end = _evaluated_coordinates(skinned_object)
            assert max(
                (repeat_end[index] - repeat_start[index]).length
                for index in range(len(repeat_start))
            ) < 1.0e-6
            batch_result = bake_compatible_native_animation_library(
                bpy.context.scene,
                skinned_result.armature_object,
                skinned_model.skeleton,
                animation_library,
                clips=(animation_library.clips[0],),
                writer=ANIMATION_WRITER_BULK,
                fallback_to_legacy=True,
                bake_rate=2.0,
            )
            assert len(batch_result.actions) == 1
            assert batch_result.incompatible_clip_count == 0
            assert batch_result.failures == ()
            assert batch_result.fallback_clip_count == 0
            assert batch_result.actions[0].writer == ANIMATION_WRITER_BULK

            original_bulk_writer = (
                native_animation_io.bake_native_animation_action_bulk
            )

            def fail_bulk_writer(*args: object, **kwargs: object) -> object:
                del args, kwargs
                raise NativeAnimationBulkWriteError("synthetic failure")

            native_animation_io.bake_native_animation_action_bulk = (
                fail_bulk_writer
            )
            try:
                fallback_result = (
                    bake_compatible_native_animation_library(
                        bpy.context.scene,
                        skinned_result.armature_object,
                        skinned_model.skeleton,
                        animation_library,
                        clips=(animation_library.clips[0],),
                        writer=ANIMATION_WRITER_BULK,
                        fallback_to_legacy=True,
                        bake_rate=2.0,
                    )
                )
            finally:
                native_animation_io.bake_native_animation_action_bulk = (
                    original_bulk_writer
                )
            assert fallback_result.fallback_clip_count == 1
            assert (
                fallback_result.actions[0].writer
                == ANIMATION_WRITER_LEGACY
            )
            skinned_result.armature_object.animation_data.action = action

            preview_material = bpy.data.materials.new(
                "Synthetic INDEX_4 ALPHAADD"
            )
            preview_material.use_nodes = True
            preview_nodes = preview_material.node_tree.nodes
            preview_links = preview_material.node_tree.links
            preview_nodes.clear()
            output = preview_nodes.new("ShaderNodeOutputMaterial")
            principled = preview_nodes.new("ShaderNodeBsdfPrincipled")
            preview_links.new(
                principled.outputs["BSDF"],
                output.inputs["Surface"],
            )
            preview_image = bpy.data.images.new(
                "Synthetic INDEX_4",
                width=1,
                height=1,
                alpha=True,
            )
            image_node = preview_nodes.new("ShaderNodeTexImage")
            image_node.image = preview_image
            image_node["killzone_layer_index"] = 0
            image_node["killzone_operation"] = 5
            image_node["killzone_coordinate_route_status"] = (
                "connected-native-uv0"
            )
            preview_material["killzone_core_local_token"] = "0x00001000"
            preview_material["killzone_layers_json"] = json.dumps(
                [
                    {
                        "kind": "texture",
                        "operation": 5,
                        "coordinate_selector": 0,
                        "palette_policy": "normal",
                        "alpha_scalar": 255,
                        "fade_mode": 2,
                        "matrix_4x4": None,
                        "transform_6f": None,
                        "pixel_format": "INDEX_4",
                        "effective_image_bank": "normal",
                        "coordinate_route_status": (
                            "connected-native-uv0"
                        ),
                    }
                ]
            )

            preview_mesh = bpy.data.meshes.new("Synthetic Preview Mesh")
            preview_mesh.from_pydata(
                ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
                (),
                ((0, 1, 2),),
            )
            preview_mesh.materials.append(preview_material)
            preview_colors = preview_mesh.color_attributes.new(
                name="Native V4-8",
                type="BYTE_COLOR",
                domain="POINT",
            )
            for color in preview_colors.data:
                color.color = (
                    128.0 / 255.0,
                    128.0 / 255.0,
                    128.0 / 255.0,
                    128.0 / 255.0,
                )
            preview_object = bpy.data.objects.new(
                "Synthetic Preview Object",
                preview_mesh,
            )
            bpy.context.scene.collection.objects.link(preview_object)

            preview_counts = enable_bounded_native_material_previews(
                (preview_material,),
                (preview_object,),
                include_write=False,
            )
            assert preview_counts == {"write": 0, "alphaadd": 1}
            assert preview_material.surface_render_method == "BLENDED"
            assert (
                preview_material["killzone_preview_mode"]
                == "single-layer-index4-alphaadd-v4-8"
            )
            assert (
                principled.inputs["Base Color"]
                .links[0]
                .from_node.name
                == "Native Layer 0 RGB Scale"
            )
            assert (
                principled.inputs["Alpha"].links[0].from_node.name
                == "Native Layer 0 Alpha Scale"
            )
            assert abs(
                preview_nodes["Native Layer 0 RGB Scale"]
                .inputs[3]
                .default_value
                - 255.0 / 128.0
            ) < 1e-6

            index8_layer = json.loads(
                preview_material["killzone_layers_json"]
            )[0]
            index8_layer.update(
                {
                    "flags": 0,
                    "lighting_mode": 1,
                    "z_write": 0,
                    "framebuffer_selector": 1,
                    "depth_comparison": 3,
                    "alpha_test": 7,
                    "alpha_reference": 0,
                    "texture_enabled": 1,
                    "tfx_policy": 1,
                    "alpha_source_mode": 0,
                    "fade_mode": 0,
                    "vu_features": [False, False],
                    "pixel_format": "INDEX_8",
                }
            )
            assert supports_bounded_native_alphaadd_preview(index8_layer)
            rejected_index8_layer = dict(index8_layer)
            rejected_index8_layer["fade_mode"] = 3
            assert not supports_bounded_native_alphaadd_preview(
                rejected_index8_layer
            )
            preview_material["killzone_layers_json"] = json.dumps(
                [index8_layer]
            )
            preview_counts = enable_bounded_native_material_previews(
                (preview_material,),
                (preview_object,),
                include_write=False,
            )
            assert preview_counts == {"write": 0, "alphaadd": 1}
            assert (
                preview_material["killzone_preview_mode"]
                == "single-layer-index8-alphaadd-v4-8"
            )
            assert (
                preview_material["killzone_preview_pixel_format"]
                == "INDEX_8"
            )

            color_layer = dict(index8_layer)
            color_layer.update(
                {
                    "index": 0,
                    "operation_name": "ALPHAADD",
                    "resource_token": "0x00002000",
                    "z_write": 1,
                    "tfx_policy": 2,
                }
            )
            assert (
                select_native_color_diagnostic_layer((color_layer,))
                is color_layer
            )
            masked_color_layer = dict(color_layer)
            masked_color_layer["operation"] = 8
            masked_color_layer["operation_name"] = "MASKEDWRITE"
            assert (
                select_native_color_diagnostic_layer(
                    (masked_color_layer,)
                )
                is masked_color_layer
            )
            preview_material["killzone_layers_json"] = json.dumps(
                [color_layer]
            )
            preview_counts = enable_bounded_native_material_previews(
                (preview_material,),
                (preview_object,),
                include_write=False,
                include_color_diagnostics=True,
            )
            assert preview_counts == {
                "write": 0,
                "alphaadd": 0,
                "color": 1,
            }
            assert (
                preview_material["killzone_preview_mode"]
                == "diagnostic-v4-8-color-layer0"
            )
            assert (
                principled.inputs["Base Color"]
                .links[0]
                .from_node.name
                == "Native Layer 0 RGB Scale"
            )
            assert (
                principled.inputs["Alpha"].links[0].from_node.name
                == "Native Layer 0 Alpha Scale"
            )
            assert (
                preview_material["killzone_preview_v4_8_modulation"]
                is True
            )

            vegetation_base = dict(color_layer)
            vegetation_base.update(
                {
                    "operation": 11,
                    "operation_name": "LIGHTMAPCOLOR",
                    "resource_token": "0x00003000",
                    "vu_features": [True, True],
                }
            )
            vegetation_alpha = dict(color_layer)
            vegetation_alpha.update(
                {
                    "index": 1,
                    "resource_token": "0x00003000",
                }
            )
            preview_material["killzone_layers_json"] = json.dumps(
                [vegetation_base, vegetation_alpha]
            )
            preview_counts = enable_bounded_native_material_previews(
                (preview_material,),
                (preview_object,),
                include_write=False,
                include_color_diagnostics=True,
            )
            assert preview_counts["color"] == 1
            assert preview_material["killzone_preview_alpha_connected"]

            transformed_write = dict(color_layer)
            transformed_write.update(
                {
                    "operation": 0,
                    "operation_name": "WRITE",
                    "coordinate_route_status": (
                        "preserved-unconnected-transform-"
                        "convention-unresolved"
                    ),
                    "transform_6f": [1.0, 0.0, 0.0, 1.0, 0.25, 0.5],
                }
            )
            preview_material["killzone_layers_json"] = json.dumps(
                [transformed_write]
            )
            preview_counts = enable_bounded_native_material_previews(
                (preview_material,),
                (preview_object,),
                include_write=False,
                include_color_diagnostics=True,
            )
            assert preview_counts["color"] == 1
            assert preview_material["killzone_preview_transform_ignored"]
            assert not principled.inputs["Alpha"].links
            assert (
                image_node.inputs["Vector"].links[0].from_node.name
                == "Native Layer 0 Diagnostic UV0"
            )
    finally:
        io_scene_killzone.unregister()


if __name__ == "__main__":
    try:
        run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    print("Killzone Blender smoke test passed")
