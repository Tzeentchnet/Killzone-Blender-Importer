# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import struct
import sys
import tempfile
import traceback
from types import SimpleNamespace

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

import bpy
from mathutils import Matrix

import io_scene_killzone
import io_scene_killzone.native_animation_io as native_animation_io
import io_scene_killzone.operators as native_operators
from io_scene_killzone.core.bundle import Bundle
from io_scene_killzone.core.core_format import CoreHeader
from io_scene_killzone.core.native_animation_resource import (
    parse_native_animation_library,
)
from io_scene_killzone.core.native_level import (
    NativeCollisionMeshInstance,
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
    _configure_texture_node,
    _connect_layer_coordinates,
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

_NATIVE_SOURCE_UV0 = (
    (-0.5, -1.0),
    (1.5, 0.25),
    (2.5, 1.5),
)
_NATIVE_SOURCE_UV0_RAW = (
    (-2048, -4096),
    (6144, 1024),
    (10240, 6144),
)
_NATIVE_BLENDER_UV0 = (
    (-0.5, 2.0),
    (1.5, 0.75),
    (2.5, -0.5),
)
_NATIVE_SOURCE_UV1 = (3.25, -0.25)
_NATIVE_SOURCE_UV1_RAW = (13312, -1024)
_NATIVE_BLENDER_UV1_WITH_FALLBACK = (
    (3.25, 1.25),
    (0.0, 0.0),
    (0.0, 0.0),
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
            raw_coordinates=(
                (
                    _NATIVE_SOURCE_UV0_RAW[index],
                    _NATIVE_SOURCE_UV1_RAW,
                )
                if index == 0
                else (_NATIVE_SOURCE_UV0_RAW[index],)
            ),
            coordinates=(
                (_NATIVE_SOURCE_UV0[index], _NATIVE_SOURCE_UV1)
                if index == 0
                else (_NATIVE_SOURCE_UV0[index],)
            ),
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
    collision_leaf = _synthetic_static_resource(
        NativeObjectKey(0x350, 0),
        0x3500,
        6.0,
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
    collision_instance = NativeCollisionMeshInstance(
        key=NativeObjectKey(0x550, 0),
        object_id=0x5500,
        name="Collision Prop",
        name_index=4,
        name_is_identifier=True,
        collision_group=0x1C000002,
        parent_token=0,
        collision_enabled=True,
        collision_resource_token=0xA000,
        collision_resource_key=NativeObjectKey(0xA00, 0),
        collision_resource_type="CollisionResourcePolySoup",
        resource_token=collision_leaf.object_id,
        resource_key=collision_leaf.key,
        matrix=single_matrix,
        footer=0x00020001,
        raw_blob=b"",
    )
    collision_instances = (
        collision_instance,
        replace(
            collision_instance,
            key=NativeObjectKey(0x550, 1),
            object_id=0x5501,
            name="Collision Prop Duplicate",
            matrix=placed_matrix,
        ),
    )
    resources = (
        leaf_a,
        leaf_b,
        direct_leaf,
        collision_leaf,
        wrapper,
    )
    render_plan = build_static_render_plan(
        resources,
        static_instances,
        collision_instances,
    )
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
        collision_instances=collision_instances,
        skinned_instances=(skinned_instance,),
        render_plan=render_plan,
        textures=(),
        logical_textures=(),
        animated_textures=(),
        materials=(),
        material_plan=NativeLevelMaterialPlan((), (), (), ()),
        packet_shapes=(),
        physical_packet_count=4,
        physical_triangle_count=4,
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
    packet = regular.packets[0]
    regular = replace(
        regular,
        packets=(
            replace(
                packet,
                vertices=tuple(
                    replace(
                        vertex,
                        raw_coordinates=(
                            _NATIVE_SOURCE_UV0_RAW[index],
                        ),
                        coordinates=(_NATIVE_SOURCE_UV0[index],),
                    )
                    for index, vertex in enumerate(packet.vertices)
                ),
            ),
        ),
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


def _assert_mesh_uvs(
    mesh: bpy.types.Mesh,
    layer_name: str,
    expected_by_vertex: tuple[tuple[float, float], ...],
) -> None:
    layer = mesh.uv_layers.get(layer_name)
    assert layer is not None
    for loop in mesh.loops:
        actual = layer.data[loop.index].uv
        expected = expected_by_vertex[loop.vertex_index]
        assert all(
            abs(actual[axis] - expected[axis]) < 1.0e-6
            for axis in range(2)
        )


def run() -> None:
    io_scene_killzone.register()
    try:
        bank = SimpleNamespace(
            path="localcache.ps2/lumps/allweapons.default.core",
            clip_count=1509,
        )
        bank_label = native_operators._animation_bank_selection_label(
            SimpleNamespace(
                kz_additional_animation_bank="bank:0",
            ),
            SimpleNamespace(animation_banks=(bank,)),
        )
        assert (
            bank_label
            == "Selected: allweapons.default.core (1,509 clips)"
        )
        assert hasattr(bpy.ops.kz, "select_animation_bank")
        assert (
            native_operators.KZ_OT_select_animation_bank.bl_property
            == "bank"
        )
        assert native_operators._should_suggest_allweapons(
            SimpleNamespace(
                path=(
                    "localcache.ps2/lumps/"
                    "entity.helghast.3rd.basecommander.core"
                ),
                kind="skinned",
                own_animation_count=0,
            )
        )
        assert not native_operators._should_suggest_allweapons(
            SimpleNamespace(
                path=(
                    "localcache.ps2/lumps/"
                    "entity.helghast.3rd.heavyhovertank.core"
                ),
                kind="skinned",
                own_animation_count=0,
            )
        )
        assert not native_operators._should_suggest_allweapons(
            SimpleNamespace(
                path=(
                    "localcache.ps2/lumps/"
                    "entity.player.3rd.luger.core"
                ),
                kind="skinned",
                own_animation_count=174,
            )
        )
        preference_properties = (
            native_operators.KZ_AddonPreferences.bl_rna.properties
        )
        assert (
            preference_properties["animation_writer"].default
            == ANIMATION_WRITER_BULK
        )
        assert (
            preference_properties[
                "animation_fallback_to_legacy"
            ].default
            is True
        )
        assert abs(
            preference_properties[
                "animation_residual_tolerance"
            ].default
            - 0.05
        ) < 1.0e-8
        assert not hasattr(bpy.types.Scene, "kz_animation_writer")
        assert not hasattr(
            bpy.types.Scene,
            "kz_animation_fallback_to_legacy",
        )
        assert not hasattr(
            bpy.types.Scene,
            "kz_animation_residual_tolerance",
        )
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
            assert (
                level_result.root_collection[
                    "killzone_bounded_layerbumpdot3_preview_count"
                ]
                == 0
            )
            assert level_result.props_collection.name == "Props"
            assert (
                level_result.root_collection["killzone_import_product"]
                == "native-map-placement"
            )
            assert (
                level_result.root_collection[
                    "killzone_level_placement_context_loaded"
                ]
                is True
            )
            assert (
                level_result.root_collection[
                    "killzone_serialized_static_instance_count"
                ]
                == len(prop_level.instances)
            )
            assert (
                level_result.root_collection[
                    "killzone_selected_static_instance_leaf_occurrence_count"
                ]
                == sum(
                    occurrence.path == "instance"
                    for occurrence in prop_level.render_plan.instance_occurrences
                )
            )
            assert (
                level_result.root_collection[
                    "killzone_selected_collision_mesh_leaf_occurrence_count"
                ]
                == len(prop_level.collision_instances)
            )
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
                    "Collision Mesh Instances"
                )
                == level_result.collision_instance_collection
            )
            assert (
                level_result.props_collection.children.get(
                    "Animated Skinned Instances (metadata only)"
                )
                == level_result.animated_instance_collection
            )
            assert len(level_result.static_instance_objects) == 3
            assert len(level_result.collision_instance_objects) == 2
            assert len(level_result.animated_instance_objects) == 1
            assert len(level_result.prop_objects) == 6
            assert len(level_result.objects) == 6
            assert all(obj.type == "MESH" for obj in level_result.objects)
            assert level_result.unique_mesh_count == 4

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

            collision_owner, duplicate_collision_owner = (
                level_result.collision_instance_objects
            )
            assert collision_owner.parent is None
            assert collision_owner["killzone_path"] == "collision-instance"
            assert collision_owner["killzone_serialized_type"] == (
                "CollisionMeshInstance"
            )
            assert collision_owner["killzone_collision_group"] == (
                "0x1C000002"
            )
            assert collision_owner["killzone_collision_enabled"] is True
            assert collision_owner["killzone_collision_resource_type"] == (
                "CollisionResourcePolySoup"
            )
            assert collision_owner["killzone_resource_object_index"] == 0
            assert collision_owner.data is duplicate_collision_owner.data
            assert tuple(collision_owner.location) == (-4.0, 5.0, 6.0)

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
            _assert_mesh_uvs(
                repeated_owner.data,
                "Native UV0",
                _NATIVE_BLENDER_UV0,
            )
            _assert_mesh_uvs(
                repeated_owner.data,
                "Native UV1",
                _NATIVE_BLENDER_UV1_WITH_FALLBACK,
            )

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
            assert (
                direct_object["killzone_leaf_object_id"] == "0x00003000"
            )
            assert not any(
                obj.get("killzone_path") == "direct"
                and obj.get("killzone_leaf_object_id") == "0x00003500"
                for obj in level_result.objects
            )

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
            assert (
                level_result.root_collection[
                    "killzone_collision_mesh_prop_count"
                ]
                == 2
            )
            assert (
                level_result.root_collection["killzone_prop_count"] == 6
            )
            assert (
                level_result.root_collection[
                    "killzone_static_instance_owner_count"
                ]
                == len(prop_level.instances)
            )
            assert (
                level_result.root_collection[
                    "killzone_collision_mesh_instance_owner_count"
                ]
                == len(prop_level.collision_instances)
            )

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
            assert (
                static_result.root_collection["killzone_import_product"]
                == "standalone-static-definition-preview"
            )
            assert (
                static_result.root_collection[
                    "killzone_level_placement_context_loaded"
                ]
                is False
            )
            assert static_result.root_collection[
                "killzone_triangle_count"
            ] == 2
            for static_object in static_result.mesh_objects:
                _assert_mesh_uvs(
                    static_object.data,
                    "Native UV0",
                    _NATIVE_BLENDER_UV0,
                )
                _assert_mesh_uvs(
                    static_object.data,
                    "Native UV1",
                    _NATIVE_BLENDER_UV1_WITH_FALLBACK,
                )

            skinned_model = _synthetic_skinned_model()
            skinned_regular = skinned_model.resources[0].regular
            assert skinned_regular is not None
            assert tuple(
                vertex.coordinates[0]
                for vertex in skinned_regular.packets[0].vertices
            ) == _NATIVE_SOURCE_UV0
            skinned_result = import_native_skinned_model_data(
                bpy.context,
                skinned_model,
                model_path="synthetic/entity.rigged.core",
            )
            assert len(skinned_result.armature_object.data.bones) == 2
            assert (
                skinned_result.root_collection["killzone_import_product"]
                == "standalone-skinned-model"
            )
            assert skinned_result.bone_names == ("Root", "Child")
            assert len(skinned_result.mesh_objects) == 1
            skinned_object = skinned_result.mesh_objects[0]
            assert len(skinned_object.data.vertices) == 3
            assert len(skinned_object.data.polygons) == 1
            assert len(skinned_object.data.uv_layers) == 1
            _assert_mesh_uvs(
                skinned_object.data,
                "Native UV0",
                _NATIVE_BLENDER_UV0,
            )
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
            assert action["killzone_action_name"] == action.name
            assert (
                action["killzone_action_name_requested"]
                == action["killzone_legacy_action_name"]
            )
            assert (
                action["killzone_action_name_source"]
                == "token-directory-fallback"
            )
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
                bulk_result.action["killzone_action_name_requested"]
                == action["killzone_legacy_action_name"]
            )
            assert (
                bulk_result.action["killzone_legacy_action_name"]
                == action["killzone_legacy_action_name"]
            )
            assert (
                bulk_result.action["killzone_action_name_source"]
                == "token-directory-fallback"
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
            semantic_skeleton = replace(
                skinned_model.skeleton,
                bones=(
                    replace(
                        skinned_model.skeleton.bones[0],
                        name="Root_joint",
                    ),
                    skinned_model.skeleton.bones[1],
                ),
            )
            semantic_bone_map = replace(
                animation_library.clips[0].bone_map,
                track_names=("Root_joint", "cHiLd"),
            )
            semantic_clip = replace(
                animation_library.clips[0],
                bone_map=semantic_bone_map,
            )
            semantic_strings = list(animation_library.header.strings)
            semantic_strings[semantic_skeleton.name_string_index] = replace(
                semantic_strings[semantic_skeleton.name_string_index],
                raw=b"skelet_Root",
                utf8="skelet_Root",
            )
            semantic_library = replace(
                animation_library,
                header=replace(
                    animation_library.header,
                    strings=tuple(semantic_strings),
                ),
                bone_maps=(semantic_bone_map,),
                clips=(semantic_clip,),
            )
            semantic_result = bake_native_animation_action_bulk(
                bpy.context.scene,
                skinned_result.armature_object,
                semantic_skeleton,
                semantic_clip,
                library=semantic_library,
                bake_rate=2.0,
            )
            assert semantic_result.action.name == "Root"
            assert (
                semantic_result.action["killzone_action_name_requested"]
                == "Root"
            )
            assert (
                semantic_result.action["killzone_action_name_source"]
                == "serialized-single-clip-skeleton-root"
            )
            assert (
                semantic_result.action["killzone_legacy_action_name"]
                == action["killzone_legacy_action_name"]
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
            assert (
                repeat_result.action["killzone_action_name_requested"]
                == "Synthetic Repeating Animation"
            )
            assert (
                repeat_result.action["killzone_action_name_source"]
                == "explicit-override"
            )
            assert (
                repeat_result.action["killzone_legacy_action_name"]
                == action["killzone_legacy_action_name"]
            )
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
                preview_material["killzone_shader_profile"]
                == "bounded-native-preview"
            )
            assert (
                output.inputs["Surface"].links[0].from_node.name
                == "Killzone Preview Surface Mix"
            )
            assert (
                preview_nodes["Killzone Preview Emission"]
                .inputs["Strength"]
                .default_value
                == 1.0
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
            assert not preview_nodes[
                "Native Layer 0 Alpha Scale"
            ].use_clamp

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

            composition_material = bpy.data.materials.new(
                "Synthetic ALPHAADD LAYERBUMPDOT3"
            )
            composition_material.use_nodes = True
            composition_nodes = composition_material.node_tree.nodes
            composition_links = composition_material.node_tree.links
            composition_nodes.clear()
            composition_output = composition_nodes.new(
                "ShaderNodeOutputMaterial"
            )
            composition_principled = composition_nodes.new(
                "ShaderNodeBsdfPrincipled"
            )
            composition_links.new(
                composition_principled.outputs["BSDF"],
                composition_output.inputs["Surface"],
            )
            composition_base_image = bpy.data.images.new(
                "Synthetic LayerBumpDot3 Base",
                width=1,
                height=1,
                alpha=True,
            )
            composition_detail_image = bpy.data.images.new(
                "Synthetic LayerBumpDot3 Detail",
                width=1,
                height=1,
                alpha=True,
            )
            composition_base = composition_nodes.new(
                "ShaderNodeTexImage"
            )
            composition_base.name = "Native Texture Layer 0"
            composition_base.image = composition_base_image
            composition_base.extension = "REPEAT"
            composition_base["killzone_layer_index"] = 0
            composition_base["killzone_operation"] = 5
            composition_base["killzone_coordinate_route_status"] = (
                "connected-native-uv0"
            )
            composition_detail = composition_nodes.new(
                "ShaderNodeTexImage"
            )
            composition_detail.name = "Native Texture Layer 1"
            composition_detail.image = composition_detail_image
            composition_detail.extension = "EXTEND"
            composition_detail["killzone_layer_index"] = 1
            composition_detail["killzone_operation"] = 18
            composition_detail["killzone_coordinate_route_status"] = (
                "connected-native-uv1"
            )
            composition_uv0 = composition_nodes.new("ShaderNodeUVMap")
            composition_uv0.name = "Native Layer 0 UV0"
            composition_uv0.uv_map = "Native UV0"
            composition_links.new(
                composition_uv0.outputs["UV"],
                composition_base.inputs["Vector"],
            )
            composition_uv1 = composition_nodes.new("ShaderNodeUVMap")
            composition_uv1.name = "Native Layer 1 UV1"
            composition_uv1.uv_map = "Native UV1"
            composition_links.new(
                composition_uv1.outputs["UV"],
                composition_detail.inputs["Vector"],
            )
            composition_layers = [
                {
                    "index": 0,
                    "kind": "texture",
                    "operation": 5,
                    "operation_name": "ALPHAADD",
                    "lighting_mode": 1,
                    "pixel_format": "INDEX_8",
                    "coordinate_route_status": "connected-native-uv0",
                },
                {
                    "index": 1,
                    "kind": "texture",
                    "operation": 18,
                    "operation_name": "LAYERBUMPDOT3",
                    "lighting_mode": 1,
                    "pixel_format": "ALPHA_8",
                    "coordinate_route_status": "connected-native-uv1",
                },
            ]
            composition_material["killzone_core_local_token"] = (
                "0x00004000"
            )
            composition_material[
                "killzone_bounded_layerbumpdot3_candidate"
            ] = True
            composition_material["killzone_layers_json"] = json.dumps(
                composition_layers
            )
            composition_mesh = bpy.data.meshes.new(
                "Synthetic LayerBumpDot3 Mesh"
            )
            composition_mesh.from_pydata(
                ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
                (),
                ((0, 1, 2),),
            )
            composition_mesh.uv_layers.new(name="Native UV0")
            composition_mesh.uv_layers.new(name="Native UV1")
            composition_mesh.materials.append(composition_material)
            composition_object = bpy.data.objects.new(
                "Synthetic LayerBumpDot3 Object",
                composition_mesh,
            )
            bpy.context.scene.collection.objects.link(composition_object)

            composition_v4_8 = composition_mesh.color_attributes.new(
                name="Native V4-8",
                type="BYTE_COLOR",
                domain="POINT",
            )
            preview_counts = enable_bounded_native_material_previews(
                (composition_material,),
                (composition_object,),
                include_write=False,
            )
            assert preview_counts == {"write": 0, "alphaadd": 0}
            composition_mesh[
                "killzone_material_v4_8_presence_json"
            ] = json.dumps({"0x00004000": True})
            preview_counts = enable_bounded_native_material_previews(
                (composition_material,),
                (composition_object,),
                include_write=False,
            )
            assert preview_counts == {"write": 0, "alphaadd": 0}
            composition_mesh[
                "killzone_material_v4_8_presence_json"
            ] = json.dumps({"0x00004000": False})
            composition_mesh[
                "killzone_material_v4_8_alpha_range_json"
            ] = json.dumps({"0x00004000": [127, 127]})
            preview_counts = enable_bounded_native_material_previews(
                (composition_material,),
                (composition_object,),
                include_write=False,
            )
            assert preview_counts == {"write": 0, "alphaadd": 0}
            composition_mesh[
                "killzone_material_v4_8_presence_json"
            ] = json.dumps({"0x00004000": True})

            preview_counts = enable_bounded_native_material_previews(
                (composition_material,),
                (composition_object,),
                include_write=False,
            )
            assert preview_counts == {
                "write": 0,
                "alphaadd": 0,
                "layerbumpdot3": 1,
            }
            assert (
                composition_material["killzone_preview_mode"]
                == "two-layer-alphaadd-layerbumpdot3"
            )
            assert (
                composition_material["killzone_shader_profile"]
                == "bounded-native-preview"
            )
            assert composition_detail.extension == "EXTEND"
            assert (
                composition_detail.inputs["Vector"]
                .links[0]
                .from_node.name
                == "Native Layer 1 UV1"
            )
            assert (
                composition_detail["killzone_preview_role"]
                == "framebuffer-alpha-multiplier-not-normal-map"
            )
            assert (
                composition_nodes["Native Layer 1 Alpha Byte"].operation
                == "MULTIPLY"
            )
            assert (
                composition_nodes[
                    "Native Layer 1 Fragment Alpha"
                ].inputs[1].default_value
                == 63.0
            )
            assert (
                composition_nodes[
                    "Native Layer 1 HIGHLIGHT Clamp"
                ].operation
                == "MINIMUM"
            )
            assert (
                composition_nodes[
                    "Native Layer 1 HIGHLIGHT Clamp"
                ].inputs[1].default_value
                == 255.0
            )
            assert (
                composition_nodes[
                    "Native Layer 1 GS Multiplier"
                ].inputs[1].default_value
                == 128.0
            )
            assert (
                composition_principled.inputs["Base Color"]
                .links[0]
                .from_node.name
                == "Native LayerBumpDot3 Framebuffer Scale"
            )
            assert (
                composition_nodes[
                    "Native LayerBumpDot3 Framebuffer Scale"
                ].inputs[0]
                .links[0]
                .from_node.name
                == "Native Layer 0 RGB Scale"
            )
            assert (
                composition_material[
                    "killzone_preview_v4_8_alpha_byte"
                ]
                == 127
            )
            assert (
                composition_output.inputs["Surface"]
                .links[0]
                .from_node.name
                == "Killzone Preview Emission"
            )
            assert not any(
                link.to_socket.name == "Normal"
                for link in composition_material.node_tree.links
                if link.from_node == composition_detail
            )

            masked_material = bpy.data.materials.new(
                "Synthetic MASKEDWRITE ALPHAADD Foliage"
            )
            masked_material.use_nodes = True
            masked_nodes = masked_material.node_tree.nodes
            masked_links = masked_material.node_tree.links
            masked_nodes.clear()
            masked_output = masked_nodes.new("ShaderNodeOutputMaterial")
            masked_principled = masked_nodes.new(
                "ShaderNodeBsdfPrincipled"
            )
            masked_links.new(
                masked_principled.outputs["BSDF"],
                masked_output.inputs["Surface"],
            )
            masked_image = bpy.data.images.new(
                "Synthetic MASKEDWRITE Foliage",
                width=1,
                height=1,
                alpha=True,
            )
            masked_uv = masked_nodes.new("ShaderNodeUVMap")
            masked_uv.name = "Native MASKEDWRITE UV0"
            masked_uv.uv_map = "Native UV0"
            for layer_index, operation in ((0, 8), (1, 5)):
                layer_node = masked_nodes.new("ShaderNodeTexImage")
                layer_node.name = f"Native Texture Layer {layer_index}"
                layer_node.image = masked_image
                layer_node["killzone_layer_index"] = layer_index
                layer_node["killzone_operation"] = operation
                layer_node["killzone_coordinate_route_status"] = (
                    "connected-native-uv0"
                )
                masked_links.new(
                    masked_uv.outputs["UV"],
                    layer_node.inputs["Vector"],
                )
            masked_layers = [
                {
                    "index": 0,
                    "kind": "texture",
                    "resource_token": "0x060D0000",
                    "operation": 8,
                    "operation_name": "MASKEDWRITE",
                    "z_write": 1,
                    "alpha_test": 4,
                    "alpha_reference": 204,
                    "alpha_scalar": 255,
                    "pixel_format": "INDEX_8",
                    "effective_image_bank": "normal",
                    "coordinate_route_status": "connected-native-uv0",
                },
                {
                    "index": 1,
                    "kind": "texture",
                    "resource_token": "0x060D0000",
                    "operation": 5,
                    "operation_name": "ALPHAADD",
                    "z_write": 0,
                    "alpha_test": 7,
                    "alpha_reference": 0,
                    "alpha_scalar": 255,
                    "pixel_format": "INDEX_8",
                    "effective_image_bank": "normal",
                    "coordinate_route_status": "connected-native-uv0",
                },
            ]
            masked_material["killzone_core_local_token"] = "0x060C4C60"
            masked_material[
                "killzone_maskedwrite_alphaadd_cutout_candidate"
            ] = True
            masked_material["killzone_layers_json"] = json.dumps(
                masked_layers
            )
            masked_mesh = bpy.data.meshes.new(
                "Synthetic MASKEDWRITE Foliage Mesh"
            )
            masked_mesh.from_pydata(
                ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
                (),
                ((0, 1, 2),),
            )
            masked_mesh.uv_layers.new(name="Native UV0")
            masked_mesh.materials.append(masked_material)
            masked_object = bpy.data.objects.new(
                "Synthetic MASKEDWRITE Foliage Object",
                masked_mesh,
            )
            bpy.context.scene.collection.objects.link(masked_object)

            preview_counts = enable_bounded_native_material_previews(
                (masked_material,),
                (masked_object,),
                include_write=False,
            )
            assert preview_counts == {
                "write": 0,
                "alphaadd": 0,
                "maskedwrite": 1,
            }
            assert (
                masked_material["killzone_preview_mode"]
                == "two-layer-maskedwrite-alphaadd-cutout"
            )
            assert (
                masked_material["killzone_shader_profile"]
                == "artist-approximation"
            )
            assert masked_material.surface_render_method == "DITHERED"
            masked_compare = masked_nodes[
                "Native MASKEDWRITE Decoded Alpha Cutout Compare"
            ]
            assert masked_compare.operation == "GREATER_THAN"
            assert masked_compare.inputs[1].default_value == 204.0
            assert (
                masked_compare["killzone_runtime_alpha_reference"]
                == 102
            )
            assert (
                masked_principled.inputs["Alpha"]
                .links[0]
                .from_node
                == masked_compare
            )
            assert (
                masked_output.inputs["Surface"].links[0].from_node.name
                == "Killzone Preview Surface Mix"
            )
            assert (
                masked_material[
                    "killzone_preview_decoded_alpha_cutoff_byte"
                ]
                == 204
            )
            assert (
                masked_material["killzone_preview_second_pass_composed"]
                is False
            )
            assert (
                masked_nodes["Native Texture Layer 1"][
                    "killzone_preview_role"
                ]
                == "preserved-layer1-low-alpha-fringe-not-composed"
            )
            assert (
                "layer1-alphaadd-low-alpha-fringe"
                in json.loads(
                    masked_material[
                        "killzone_shader_unimplemented_state_json"
                    ]
                )
            )

            composition_layers[1][
                "coordinate_route_status"
            ] = "connected-native-uv0"
            composition_material["killzone_layers_json"] = json.dumps(
                composition_layers
            )
            preview_counts = enable_bounded_native_material_previews(
                (composition_material,),
                (composition_object,),
                include_write=False,
            )
            assert preview_counts == {"write": 0, "alphaadd": 0}

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
            alpha_test_layer = dict(color_layer)
            alpha_test_layer.update(
                {
                    "alpha_test": 4,
                    "alpha_reference": 204,
                }
            )
            preview_material["killzone_layers_json"] = json.dumps(
                [alpha_test_layer]
            )
            preview_counts = enable_bounded_native_material_previews(
                (preview_material,),
                (preview_object,),
                include_write=False,
                include_color_diagnostics=True,
            )
            assert preview_counts["color"] == 1
            comparison = preview_nodes[
                "Native Layer 0 Alpha Test Compare"
            ]
            assert comparison.operation == "GREATER_THAN"
            assert (
                comparison["killzone_runtime_alpha_reference"] == 102
            )
            assert (
                preview_material["killzone_shader_profile"]
                == "artist-approximation"
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
                    "transform_6f": [
                        0.9878843,
                        -0.1551932,
                        0.1551936,
                        0.9878841,
                        -0.0836546,
                        -0.0715389,
                    ],
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

            axis_transform_material = bpy.data.materials.new(
                "Synthetic Axis UV Transform"
            )
            axis_transform_material.use_nodes = True
            axis_nodes = axis_transform_material.node_tree.nodes
            axis_links = axis_transform_material.node_tree.links
            axis_nodes.clear()
            axis_image = axis_nodes.new("ShaderNodeTexImage")
            axis_layer = SimpleNamespace(
                index=0,
                filter_policy=1,
                wrap_s=0,
                wrap_t=0,
                matrix_4x4=None,
                transform_6f=(
                    0.24999995529651642,
                    3.740848910638306e-07,
                    1.2421777739746176e-07,
                    0.9999999403953552,
                    0.2499999701976776,
                    8.313790544889343e-08,
                ),
                coordinate_selector=0,
            )
            _configure_texture_node(axis_image, axis_layer)
            route_status = _connect_layer_coordinates(
                nodes=axis_nodes,
                links=axis_links,
                image_node=axis_image,
                layer=axis_layer,
                node_y=0.0,
            )
            assert route_status == (
                "connected-native-uv0-bounded-axis-transform6f"
            )
            axis_mapping = axis_nodes[
                "Native Layer 0 Axis UV Transform"
            ]
            expected_scale = (
                axis_layer.transform_6f[0],
                axis_layer.transform_6f[3],
                1.0,
            )
            assert all(
                abs(actual - expected) < 1.0e-6
                for actual, expected in zip(
                    axis_mapping.inputs["Scale"].default_value,
                    expected_scale,
                )
            )
            expected_location = (
                axis_layer.transform_6f[4],
                (
                    1.0
                    - axis_layer.transform_6f[3]
                    - axis_layer.transform_6f[5]
                ),
                0.0,
            )
            assert all(
                abs(actual - expected) < 1.0e-6
                for actual, expected in zip(
                    axis_mapping.inputs["Location"].default_value,
                    expected_location,
                )
            )
            assert (
                axis_image.inputs["Vector"].links[0].from_node
                == axis_mapping
            )

            wrap_material = bpy.data.materials.new(
                "Synthetic Mixed Wrap"
            )
            wrap_material.use_nodes = True
            wrap_nodes = wrap_material.node_tree.nodes
            wrap_links = wrap_material.node_tree.links
            wrap_nodes.clear()
            wrap_image = wrap_nodes.new("ShaderNodeTexImage")
            wrap_layer = SimpleNamespace(
                index=0,
                filter_policy=0,
                wrap_s=0,
                wrap_t=1,
                matrix_4x4=None,
                transform_6f=None,
                coordinate_selector=0,
            )
            _configure_texture_node(wrap_image, wrap_layer)
            route_status = _connect_layer_coordinates(
                nodes=wrap_nodes,
                links=wrap_links,
                image_node=wrap_image,
                layer=wrap_layer,
                node_y=0.0,
            )
            assert route_status == (
                "connected-native-uv0-mixed-axis-wrap"
            )
            assert wrap_image.extension == "EXTEND"
            assert (
                wrap_nodes["Native Layer 0 X Repeat"].operation
                == "FRACT"
            )
            assert (
                wrap_nodes["Native Layer 0 Y Clamp Lower"].operation
                == "MAXIMUM"
            )
            assert (
                wrap_nodes["Native Layer 0 Y Clamp Upper"].operation
                == "MINIMUM"
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
