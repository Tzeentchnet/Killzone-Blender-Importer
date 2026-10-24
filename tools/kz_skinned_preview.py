#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Vector

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from io_scene_killzone.core.bundle import atomic_write_bytes
from io_scene_killzone.core.native_animation_resource import (
    find_native_animation_clip,
    load_native_animation_library,
)
from io_scene_killzone.core.native_level import NativeObjectKey
from io_scene_killzone.native_animation_io import (
    NativeAnimationActionResult,
    bake_native_animation_action,
)
from io_scene_killzone.native_skinned_io import (
    NativeSkinnedImportResult,
    import_native_skinned_model,
)


def _parse_object_key(value: str) -> NativeObjectKey:
    try:
        section, index = value.split(":", 1)
        return NativeObjectKey(int(section, 0), int(index, 0))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "Animation keys must use SECTION_OFFSET:OBJECT_INDEX"
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import and validate a native Killzone skinned model"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("model_path")
    parser.add_argument("--root-token", type=lambda value: int(value, 0))
    parser.add_argument("--animation-path")
    parser.add_argument(
        "--animation-token",
        action="append",
        type=lambda value: int(value, 0),
        default=[],
    )
    parser.add_argument(
        "--animation-key",
        action="append",
        type=_parse_object_key,
        default=[],
    )
    parser.add_argument("--animation-bake-rate", type=float)
    parser.add_argument(
        "--animation-residual-tolerance",
        type=float,
        default=1.0e-5,
    )
    parser.add_argument(
        "--reconstruct-root-trajectory",
        action="store_true",
    )
    parser.add_argument("--blend", type=Path, required=True)
    parser.add_argument("--png", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def _clear_scene() -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in tuple(bpy.data.collections):
        if collection.users == 0:
            bpy.data.collections.remove(collection)


def _look_at(obj: bpy.types.Object, target: Vector) -> None:
    obj.rotation_euler = (target - obj.location).to_track_quat(
        "-Z", "Y"
    ).to_euler()


def _render_preview(
    result: NativeSkinnedImportResult,
    output_path: Path,
) -> None:
    points = [
        obj.matrix_world @ vertex.co
        for obj in result.mesh_objects
        for vertex in obj.data.vertices
    ]
    minimum = Vector(
        tuple(min(point[axis] for point in points) for axis in range(3))
    )
    maximum = Vector(
        tuple(max(point[axis] for point in points) for axis in range(3))
    )
    center = (minimum + maximum) * 0.5
    radius = max((point - center).length for point in points)

    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = 900
    scene.render.resolution_y = 1200
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.view_settings.look = "AgX - Medium High Contrast"
    world = bpy.data.worlds.new("Skinned Preview World")
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (
        0.015,
        0.02,
        0.03,
        1.0,
    )
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.3
    scene.world = world

    camera_data = bpy.data.cameras.new("Skinned Preview Camera")
    camera_data.lens = 58.0
    camera = bpy.data.objects.new("Skinned Preview Camera", camera_data)
    scene.collection.objects.link(camera)
    direction = Vector((1.9, 4.8, 1.4)).normalized()
    camera.location = center + direction * (radius * 3.1)
    camera.data.clip_start = 0.01
    camera.data.clip_end = 100.0
    _look_at(camera, center + Vector((0.0, 0.0, 0.05)))
    scene.camera = camera

    key_data = bpy.data.lights.new("Skinned Preview Key", type="AREA")
    key_data.energy = 800.0
    key_data.shape = "DISK"
    key_data.size = 4.0
    key = bpy.data.objects.new("Skinned Preview Key", key_data)
    scene.collection.objects.link(key)
    key.location = center + Vector((2.8, -3.2, 3.7))
    _look_at(key, center)

    fill_data = bpy.data.lights.new("Skinned Preview Fill", type="AREA")
    fill_data.energy = 450.0
    fill_data.size = 3.0
    fill = bpy.data.objects.new("Skinned Preview Fill", fill_data)
    scene.collection.objects.link(fill)
    fill.location = center + Vector((-2.5, -1.0, 2.0))
    _look_at(fill, center)

    rim_data = bpy.data.lights.new("Skinned Preview Rim", type="AREA")
    rim_data.energy = 650.0
    rim_data.size = 2.5
    rim = bpy.data.objects.new("Skinned Preview Rim", rim_data)
    scene.collection.objects.link(rim)
    rim.location = center + Vector((0.0, 2.8, 2.8))
    _look_at(rim, center)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    scene.render.filepath = str(output_path.resolve())
    bpy.ops.render.render(write_still=True)


def _weight_report(
    result: NativeSkinnedImportResult,
) -> dict[str, object]:
    total_vertices = 0
    unweighted = 0
    multiweighted = 0
    invalid_groups = 0
    invalid_weights = 0
    bone_names = set(result.bone_names)
    per_bone: dict[str, int] = {}
    for obj in result.mesh_objects:
        group_names = {
            group.index: group.name for group in obj.vertex_groups
        }
        for vertex in obj.data.vertices:
            total_vertices += 1
            if not vertex.groups:
                unweighted += 1
                continue
            if len(vertex.groups) != 1:
                multiweighted += 1
            for assignment in vertex.groups:
                name = group_names.get(assignment.group)
                if name not in bone_names:
                    invalid_groups += 1
                    continue
                per_bone[name] = per_bone.get(name, 0) + 1
                if abs(assignment.weight - 1.0) > 1.0e-7:
                    invalid_weights += 1
    if unweighted or multiweighted or invalid_groups or invalid_weights:
        raise RuntimeError(
            "Imported weights failed rigid single-influence validation"
        )
    return {
        "vertex_count": total_vertices,
        "unweighted_vertex_count": unweighted,
        "multiweighted_vertex_count": multiweighted,
        "invalid_group_count": invalid_groups,
        "invalid_weight_count": invalid_weights,
        "weighted_bone_count": len(per_bone),
        "vertices_by_bone": dict(sorted(per_bone.items())),
    }


def _evaluated_coordinates(
    obj: bpy.types.Object,
) -> tuple[Vector, ...]:
    dependency_graph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(dependency_graph)
    mesh = evaluated.to_mesh()
    try:
        return tuple(vertex.co.copy() for vertex in mesh.vertices)
    finally:
        evaluated.to_mesh_clear()


def _controlled_deformation(
    result: NativeSkinnedImportResult,
) -> dict[str, object]:
    skeleton = result.model.skeleton
    descendants: dict[int, set[int]] = {
        bone.index: {bone.index} for bone in skeleton.bones
    }
    for bone in reversed(skeleton.bones):
        if bone.parent_index is not None:
            descendants[bone.parent_index].update(descendants[bone.index])

    assignments: dict[int, list[tuple[bpy.types.Object, int]]] = {}
    for obj in result.mesh_objects:
        source_indices = {
            group.index: result.bone_names.index(group.name)
            for group in obj.vertex_groups
        }
        for vertex in obj.data.vertices:
            assignment = vertex.groups[0]
            bone_index = source_indices[assignment.group]
            assignments.setdefault(bone_index, []).append(
                (obj, vertex.index)
            )

    preferred_names = (
        "upperarm1_left",
        "lowerarm1_left",
        "Upper_Back",
        "Upper_Body",
    )
    candidates = [
        bone.index
        for name in preferred_names
        for bone in skeleton.bones
        if bone.name == name and bone.index in assignments
    ]
    candidates.extend(
        bone.index
        for bone in skeleton.bones
        if bone.parent_index is not None
        and bone.index in assignments
        and bone.index not in candidates
    )
    if not candidates:
        raise RuntimeError("No weighted non-root bone is available to test")

    baseline = {
        obj: _evaluated_coordinates(obj)
        for obj in result.mesh_objects
    }
    dependency_graph = bpy.context.evaluated_depsgraph_get()
    for bone_index in candidates:
        pose_bone = result.armature_object.pose.bones[
            result.bone_names[bone_index]
        ]
        pose_bone.rotation_mode = "XYZ"
        pose_bone.rotation_euler.x = math.radians(20.0)
        dependency_graph.update()
        deformed = {
            obj: _evaluated_coordinates(obj)
            for obj in result.mesh_objects
        }
        directly_weighted = assignments[bone_index]
        moved_distance = max(
            (
                deformed[obj][vertex_index]
                - baseline[obj][vertex_index]
            ).length
            for obj, vertex_index in directly_weighted
        )
        unrelated_distances = [
            (
                deformed[obj][vertex_index]
                - baseline[obj][vertex_index]
            ).length
            for assigned_bone, vertices in assignments.items()
            if assigned_bone not in descendants[bone_index]
            for obj, vertex_index in vertices
        ]
        unrelated_distance = max(unrelated_distances, default=0.0)
        pose_bone.rotation_euler.x = 0.0
        dependency_graph.update()
        if moved_distance > 1.0e-5 and unrelated_distance < 1.0e-6:
            return {
                "bone_index": bone_index,
                "bone_name": skeleton.bones[bone_index].name,
                "direct_vertex_count": len(directly_weighted),
                "maximum_direct_displacement": moved_distance,
                "maximum_unrelated_displacement": unrelated_distance,
                "rotation_degrees": 20.0,
                "passed": True,
            }
    raise RuntimeError(
        "No weighted bone produced an isolated controlled deformation"
    )


def _report(
    result: NativeSkinnedImportResult,
    weight_report: dict[str, object],
    deformation_report: dict[str, object],
    animation_results: tuple[NativeAnimationActionResult, ...],
    blend_path: Path,
    preview_path: Path | None,
) -> dict[str, object]:
    packed_images = tuple(
        image
        for image in bpy.data.images
        if image.get("killzone_core_local_token")
    )
    if any(image.packed_file is None for image in packed_images):
        raise RuntimeError("A decoded native model image is not packed")
    mesh_triangle_count = sum(
        len(obj.data.polygons) for obj in result.mesh_objects
    )
    if mesh_triangle_count != result.model.triangle_count:
        raise RuntimeError("Saved mesh triangle count disagrees with source")
    return {
        "blend_path": str(blend_path.resolve()),
        "preview_path": (
            str(preview_path.resolve()) if preview_path is not None else None
        ),
        "root_token": f"0x{result.model.root_token:08X}",
        "skeleton_token": f"0x{result.model.skeleton.object_id:08X}",
        "bone_count": len(result.bone_names),
        "mesh_part_count": len(result.mesh_objects),
        "packet_count": result.model.packet_count,
        "vertex_count": result.model.vertex_count,
        "triangle_count": mesh_triangle_count,
        "native_material_count": result.native_material_count,
        "packed_image_count": len(packed_images),
        "preview_counts": result.preview_counts,
        "winding_corrections": result.winding_corrections,
        "weight_validation": weight_report,
        "controlled_deformation": deformation_report,
        "animations": [
            {
                "name": result.action.name,
                "object_id": (
                    f"0x{result.clip.object_id:08X}"
                ),
                "object_key": (
                    f"0x{result.clip.key.section_offset:X}:"
                    f"{result.clip.key.object_index}"
                ),
                "duration_seconds": result.clip.duration,
                "playback_mode": result.clip.playback_mode,
                "sample_count": len(result.sample_times),
                "bake_rate": result.bake_rate,
                "frame_start": result.frame_start,
                "frame_end": result.frame_end,
                "matched_track_count": (
                    result.binding.matched_track_count
                ),
                "unmatched_track_names": list(
                    result.binding.unmatched_track_names
                ),
                "root_trajectory_policy": (
                    result.root_trajectory_policy
                ),
                "maximum_decomposition_residual": (
                    result.maximum_decomposition_residual
                ),
            }
            for result in animation_results
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if (
        args.animation_token or args.animation_key
    ) and args.animation_path is None:
        parser.error(
            "--animation-token/--animation-key require --animation-path"
        )
    _clear_scene()
    result = import_native_skinned_model(
        bpy.context,
        args.source,
        args.model_path,
        root_token=args.root_token,
    )
    weight_report = _weight_report(result)
    deformation_report = _controlled_deformation(result)
    animation_results: list[NativeAnimationActionResult] = []
    if args.animation_path is not None:
        library = load_native_animation_library(
            args.source,
            args.animation_path,
        )
        selected = [
            find_native_animation_clip(library, object_id=token)
            for token in args.animation_token
        ]
        selected.extend(
            find_native_animation_clip(library, key=key)
            for key in args.animation_key
        )
        if not selected:
            if len(library.clips) != 1:
                parser.error(
                    "--animation-path contains multiple clips; select "
                    "with --animation-token or --animation-key"
                )
            selected.append(library.clips[0])
        seen_keys: set[NativeObjectKey] = set()
        for clip in selected:
            if clip.key in seen_keys:
                continue
            seen_keys.add(clip.key)
            animation_results.append(
                bake_native_animation_action(
                    bpy.context.scene,
                    result.armature_object,
                    result.model.skeleton,
                    clip,
                    library=library,
                    bake_rate=args.animation_bake_rate,
                    reconstruct_root_trajectory=(
                        args.reconstruct_root_trajectory
                    ),
                    residual_tolerance=(
                        args.animation_residual_tolerance
                    ),
                )
            )
        result.armature_object.animation_data.action = (
            animation_results[0].action
        )
        bpy.context.scene.frame_start = int(
            round(animation_results[0].frame_start)
        )
        bpy.context.scene.frame_end = int(
            round(animation_results[0].frame_end)
        )
        bpy.context.scene.frame_set(bpy.context.scene.frame_start)
    if args.png is not None:
        _render_preview(result, args.png)
    args.blend.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=str(args.blend.resolve()))
    report = _report(
        result,
        weight_report,
        deformation_report,
        tuple(animation_results),
        args.blend,
        args.png,
    )
    atomic_write_bytes(
        args.report,
        (
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    arguments = (
        sys.argv[sys.argv.index("--") + 1 :]
        if "--" in sys.argv
        else sys.argv[1:]
    )
    raise SystemExit(main(arguments))
