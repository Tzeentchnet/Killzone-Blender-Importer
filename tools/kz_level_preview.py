#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path, PurePosixPath
import sys
from typing import Sequence

import bpy
from mathutils import Vector

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from io_scene_killzone.core.bundle import atomic_write_bytes
from io_scene_killzone.core.native_decal import (
    NativeDecalLibrary,
    load_native_decal_library,
)
from io_scene_killzone.core.native_level import load_native_level
from io_scene_killzone.native_level_io import (
    NativeLevelImportResult,
    enable_bounded_native_material_previews,
    import_native_level_data,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create diagnostic renders for a native Killzone level"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("level_path")
    parser.add_argument("--blend", type=Path, required=True)
    parser.add_argument("--png", type=Path, required=True)
    parser.add_argument("--overview-png", type=Path, required=True)
    parser.add_argument("--material-png", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--texture-log", type=Path)
    return parser


def _look_at(obj: bpy.types.Object, target: Vector) -> None:
    obj.rotation_euler = (target - obj.location).to_track_quat(
        "-Z", "Y"
    ).to_euler()


def _object_points(
    objects: Sequence[bpy.types.Object],
) -> list[Vector]:
    return [
        obj.matrix_world @ Vector(corner)
        for obj in objects
        for corner in obj.bound_box
    ]


def _point_span(points: Sequence[Vector]) -> float:
    minimum = Vector(
        tuple(min(point[axis] for point in points) for axis in range(3))
    )
    maximum = Vector(
        tuple(max(point[axis] for point in points) for axis in range(3))
    )
    return max(maximum[axis] - minimum[axis] for axis in range(3))


def _frame_camera(
    camera: bpy.types.Object,
    points: Sequence[Vector],
    *,
    direction: tuple[float, float, float] = (0.82, -1.18, 0.62),
) -> tuple[Vector, float]:
    if not points:
        raise RuntimeError("Cannot frame an empty level import")
    minimum = Vector(
        tuple(min(point[axis] for point in points) for axis in range(3))
    )
    maximum = Vector(
        tuple(max(point[axis] for point in points) for axis in range(3))
    )
    center = (minimum + maximum) * 0.5
    radius = max((point - center).length for point in points)
    view_direction = Vector(direction).normalized()
    lens = camera.data.lens
    vertical_fov = 2.0 * math.atan(camera.data.sensor_height / (2.0 * lens))
    distance = radius / math.sin(vertical_fov * 0.42)
    camera.location = center + view_direction * distance
    camera.data.clip_start = max(0.05, radius * 0.0005)
    camera.data.clip_end = max(10_000.0, distance + radius * 4.0)
    _look_at(camera, center)
    return center, radius


def _emission_material(
    name: str,
    color: tuple[float, float, float, float],
    strength: float,
) -> bpy.types.Material:
    material = bpy.data.materials.new(name)
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = color
    emission.inputs["Strength"].default_value = strength
    links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return material


def _add_camera_text(
    camera: bpy.types.Object,
    body: str,
    y: float,
    size: float,
    material: bpy.types.Material,
) -> bpy.types.Object:
    curve = bpy.data.curves.new(f"Label {body}", type="FONT")
    curve.body = body
    curve.align_x = "CENTER"
    curve.align_y = "CENTER"
    curve.size = size
    curve.extrude = 0.0
    curve.materials.append(material)
    obj = bpy.data.objects.new(body, curve)
    bpy.context.scene.collection.objects.link(obj)
    obj.parent = camera
    obj.location = (0.0, y, -5.0)
    obj.rotation_euler = (0.0, 0.0, 0.0)
    return obj


def _configure_scene(level_name: str) -> bpy.types.Object:
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = 1600
    scene.render.resolution_y = 1000
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = False
    scene.view_settings.look = "AgX - Medium High Contrast"

    world = bpy.data.worlds.new("Killzone Diagnostic World")
    scene.world = world
    background = world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0.012, 0.018, 0.028, 1.0)
    background.inputs["Strength"].default_value = 0.22

    camera_data = bpy.data.cameras.new(f"{level_name} Diagnostic Camera")
    camera_data.lens = 52.0
    camera = bpy.data.objects.new(
        f"{level_name} Diagnostic Camera", camera_data
    )
    scene.collection.objects.link(camera)
    scene.camera = camera

    sun_data = bpy.data.lights.new("Diagnostic Sun", type="SUN")
    sun_data.energy = 2.4
    sun_data.angle = math.radians(18.0)
    sun_data.color = (1.0, 0.78, 0.58)
    sun = bpy.data.objects.new("Diagnostic Sun", sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (
        math.radians(28.0),
        math.radians(-18.0),
        math.radians(-32.0),
    )

    fill_data = bpy.data.lights.new("Diagnostic Fill", type="AREA")
    fill_data.energy = 1500.0
    fill_data.shape = "DISK"
    fill_data.size = 80.0
    fill_data.color = (0.28, 0.55, 1.0)
    fill = bpy.data.objects.new("Diagnostic Fill", fill_data)
    scene.collection.objects.link(fill)
    fill.location = (-120.0, -180.0, 160.0)
    _look_at(fill, Vector((0.0, 0.0, 0.0)))
    return camera


def _set_textured_preview_viewports() -> int:
    updated = 0
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            space.shading.type = "MATERIAL"
            space.shading.color_type = "TEXTURE"
            space.shading.use_scene_lights_render = True
            space.shading.use_scene_world_render = True
            space.region_3d.view_perspective = "CAMERA"
            space.region_3d.view_camera_zoom = 0.0
            updated += 1
    return updated


def _report(
    level_path: str,
    result: object,
    preview_hidden: Sequence[bpy.types.Object],
    decal_library: NativeDecalLibrary,
) -> dict[str, object]:
    level = result.level
    plan = level.render_plan
    level_decal_types = sorted(
        {
            value.utf8
            for value in level.header.strings
            if value.utf8 in {"DecalResource", "DecalInstance"}
        }
    )
    texture_formats = Counter(
        texture.pixel_format_name for texture in level.textures
    )
    material_layer_kinds = Counter(
        layer.kind
        for material in level.materials
        for layer in material.layers
    )
    material_operations = Counter(
        layer.operation
        for material in level.materials
        for layer in material.layers
    )
    coordinate_selectors = Counter(
        layer.coordinate_selector
        for material in level.materials
        for layer in material.layers
    )
    return {
        "report_version": 6,
        "level_path": level_path,
        "source_core_sha256": level.source_core_sha256,
        "source_corestream_sha256": level.source_corestream_sha256,
        "sections": [
            {
                "type": section.type_name,
                "offset": section.offset,
                "objects": len(section.objects),
                "directory_kind": section.directory_kind,
            }
            for section in level.sections
        ],
        "resource_nodes": len(level.resources),
        "regular_resources": sum(
            node.kind == "regular" for node in level.resources
        ),
        "multi_resources": sum(
            node.kind == "multi" for node in level.resources
        ),
        "lod_resources": sum(
            node.kind == "lod" for node in level.resources
        ),
        "static_instances": len(level.instances),
        "collision_mesh_instances": len(level.collision_instances),
        "physical_textures": len(level.textures),
        "logical_textures": len(level.logical_textures),
        "texture_payload_bytes": sum(
            len(texture.payload) for texture in level.textures
        ),
        "texture_span_records": sum(
            len(texture.span_records) for texture in level.textures
        ),
        "texture_formats": dict(sorted(texture_formats.items())),
        "animated_textures": len(level.animated_textures),
        "animated_texture_frames": sum(
            len(animation.frames)
            for animation in level.animated_textures
        ),
        "selected_materials": len(level.materials),
        "material_layers": sum(
            len(material.layers) for material in level.materials
        ),
        "material_layer_kinds": dict(
            sorted(material_layer_kinds.items())
        ),
        "material_operations": {
            str(key): value
            for key, value in sorted(material_operations.items())
        },
        "coordinate_selectors": {
            str(key): value
            for key, value in sorted(coordinate_selectors.items())
        },
        "optional_six_float_transforms": sum(
            layer.transform_6f is not None
            for material in level.materials
            for layer in material.layers
        ),
        "selected_direct_textures": len(
            level.material_plan.direct_texture_tokens
        ),
        "selected_instance_textures": len(
            level.material_plan.instance_texture_tokens
        ),
        "physical_packets": level.physical_packet_count,
        "physical_triangles": level.physical_triangle_count,
        "packet_shapes": [
            {
                "opcodes": [f"0x{opcode:02X}" for opcode in shape],
                "packets": count,
            }
            for shape, count in level.packet_shapes
        ],
        "resource_roots": len(plan.roots),
        "direct_world_roots": len(plan.direct_roots),
        "direct_leaf_occurrences": len(plan.direct_occurrences),
        "instance_leaf_occurrences": len(plan.instance_occurrences),
        "static_instance_leaf_occurrences": sum(
            occurrence.path == "instance"
            for occurrence in plan.instance_occurrences
        ),
        "collision_instance_leaf_occurrences": sum(
            occurrence.path == "collision-instance"
            for occurrence in plan.instance_occurrences
        ),
        "direct_packets": plan.direct_packet_count,
        "direct_triangles": plan.direct_triangle_count,
        "instance_packets": plan.instance_packet_count,
        "instance_triangles": plan.instance_triangle_count,
        "render_triangles": (
            plan.direct_triangle_count + plan.instance_triangle_count
        ),
        "serialized_static_props": len(level.instances),
        "serialized_collision_mesh_props": len(
            level.collision_instances
        ),
        "serialized_animated_props": len(level.skinned_instances),
        "blender_objects": len(result.objects),
        "blender_mesh_objects": len(result.objects),
        "blender_static_prop_owners": len(
            result.static_instance_objects
        ),
        "blender_collision_mesh_prop_owners": len(
            result.collision_instance_objects
        ),
        "blender_animated_prop_owners": len(
            result.animated_instance_objects
        ),
        "blender_unique_meshes": result.unique_mesh_count,
        "blender_native_materials": result.native_material_count,
        "blender_decoded_images": result.decoded_image_count,
        "winding_corrections": result.winding_corrections,
        "runtime_decals": {
            "resource_library_path": decal_library.core_path,
            "resource_library_core_sha256": (
                decal_library.source_core_sha256
            ),
            "resource_library_corestream_sha256": (
                decal_library.source_corestream_sha256
            ),
            "resource_count": len(decal_library.resources),
            "level_type_names": level_decal_types,
            "geometry_status": (
                "not-reconstructed-runtime-mark-state-is-not-serialized"
            ),
            "material_policy": (
                "ordinary-native-material-layers-no-decal-preset"
            ),
            "resources": [
                {
                    "object_id": f"0x{resource.object_id:08X}",
                    "material_token": (
                        f"0x{resource.material_token:08X}"
                    ),
                    "sort_mode": resource.sort_mode.name,
                    "sort_order": resource.sort_order,
                    "max_decals": resource.max_decals,
                    "decal_threshold": resource.decal_threshold,
                    "random_rotation_property": (
                        resource.random_rotation
                    ),
                    "min_size": resource.min_size,
                    "max_size": resource.max_size,
                    "fade_in_time": resource.fade_in_time,
                    "fade_time": resource.fade_time,
                    "clip_decal": resource.clip_decal,
                }
                for resource in decal_library.resources
            ],
        },
        "preview_hidden_enclosing_objects": [
            {
                "name": obj.name,
                "object_id": obj.get("killzone_leaf_object_id"),
                "dimensions": [float(value) for value in obj.dimensions],
            }
            for obj in preview_hidden
        ],
    }


def _texture_investigation(
    result: NativeLevelImportResult,
) -> dict[str, object]:
    level = result.level
    textures_by_token = {
        texture.object_id: texture
        for texture in level.logical_textures
    }
    selected_tokens = set(
        level.material_plan.direct_texture_tokens
    ) | set(level.material_plan.instance_texture_tokens)
    direct_materials = set(level.material_plan.direct_material_tokens)
    instance_materials = set(
        level.material_plan.instance_material_tokens
    )

    blender_materials = {
        str(material.get("killzone_core_local_token")): material
        for material in bpy.data.materials
        if material.get("killzone_core_local_token")
    }
    mesh_users: dict[str, set[bpy.types.Mesh]] = defaultdict(set)
    for obj in result.objects:
        if obj.type != "MESH":
            continue
        for material in obj.data.materials:
            if material is None:
                continue
            token = material.get("killzone_core_local_token")
            if token:
                mesh_users[str(token)].add(obj.data)

    images_by_token: dict[str, list[bpy.types.Image]] = defaultdict(list)
    for image in bpy.data.images:
        token = image.get("killzone_core_local_token")
        if token:
            images_by_token[str(token)].append(image)

    entries: dict[int, dict[str, object]] = {}
    for token in sorted(selected_tokens):
        texture = textures_by_token[token]
        token_text = f"0x{token:08X}"
        image_names = sorted(
            image.name for image in images_by_token.get(token_text, ())
        )
        if texture.base_rgba is not None and image_names:
            resolution_status = "resolved-packed-image"
        elif texture.pixel_format_name == "GLOW_8":
            resolution_status = "unresolved-dynamic-glow-palette"
        elif texture.pixel_format_name == "INVALID_PIXELFORMAT":
            resolution_status = "invalid-empty-placeholder"
        elif texture.base_rgba is not None:
            resolution_status = "decoded-but-blender-image-missing"
        else:
            resolution_status = "unresolved-no-rgba-image"
        entries[token] = {
            "texture_token": token_text,
            "pixel_format": texture.pixel_format_name,
            "dimensions": [texture.width, texture.height],
            "mip_count": texture.mip_count,
            "decode_status": texture.decode_status,
            "resolution_status": resolution_status,
            "blender_images": image_names,
            "inline_payload": texture.inline_payload,
            "usages": [],
        }

    provider_issues: list[dict[str, object]] = []
    for native_material in level.materials:
        material_token = f"0x{native_material.object_id:08X}"
        blender_material = blender_materials.get(material_token)
        if blender_material is None:
            provider_issues.append(
                {
                    "material_token": material_token,
                    "reason": "blender-material-missing",
                }
            )
            continue
        layer_documents = json.loads(
            blender_material["killzone_layers_json"]
        )
        material_paths = []
        if native_material.object_id in direct_materials:
            material_paths.append("direct")
        if native_material.object_id in instance_materials:
            material_paths.append("instance")
        preview_mode = blender_material.get("killzone_preview_mode")
        preview_layer_index = int(
            blender_material.get("killzone_preview_layer_index", 0)
        )
        users = mesh_users.get(material_token, set())

        for layer, document in zip(
            native_material.layers, layer_documents
        ):
            if layer.kind == "null_texture":
                provider_issues.append(
                    {
                        "material_token": material_token,
                        "layer_index": layer.index,
                        "paths": material_paths,
                        "reason": "explicit-null-texture-provider",
                    }
                )
                continue

            providers: list[tuple[int, int | None]] = []
            if layer.kind == "texture":
                providers.append((layer.resource_token, None))
            elif layer.kind == "animated_texture":
                frames = document.get("frames", [])
                providers.extend(
                    (
                        int(str(frame["texture_token"]), 16),
                        frame_index,
                    )
                    for frame_index, frame in enumerate(frames)
                )

            route_status = str(
                document.get("coordinate_route_status", "missing")
            )
            if (
                preview_mode
                == "two-layer-alphaadd-layerbumpdot3"
                and layer.index in (0, 1)
            ):
                render_status = (
                    "resolved-bounded-alphaadd-layerbumpdot3-composition"
                )
            elif (
                preview_mode
                == "two-layer-maskedwrite-alphaadd-cutout"
                and layer.index in (0, 1)
            ):
                render_status = (
                    "diagnostic-maskedwrite-alphaadd-cutout-preview"
                )
            elif layer.index == preview_layer_index and preview_mode:
                if str(preview_mode).startswith("single-layer-index"):
                    render_status = "bounded-alphaadd-preview"
                elif preview_mode == "layer0-write":
                    render_status = "diagnostic-write-preview"
                elif str(preview_mode).startswith(
                    "diagnostic-color-layer"
                ):
                    render_status = "diagnostic-color-preview"
                else:
                    render_status = "unknown-preview-mode"
            else:
                render_status = "inspection-only-final-shader-unconnected"

            for texture_token, frame_index in providers:
                entry = entries.get(texture_token)
                if entry is None:
                    provider_issues.append(
                        {
                            "material_token": material_token,
                            "layer_index": layer.index,
                            "texture_token": (
                                f"0x{texture_token:08X}"
                            ),
                            "reason": "selected-provider-texture-missing",
                        }
                    )
                    continue
                texture = textures_by_token[texture_token]
                reasons: list[str] = []
                if texture.base_rgba is None:
                    reasons.append(entry["resolution_status"])
                if render_status == "inspection-only-final-shader-unconnected":
                    reasons.append(
                        "native-material-composition-unreconstructed"
                    )
                elif render_status == (
                    "resolved-bounded-alphaadd-"
                    "layerbumpdot3-composition"
                ):
                    reasons.extend(
                        (
                            "framebuffer-history-before-layer0-"
                            "not-representable",
                            "gs-byte-quantization-host-approximated",
                        )
                    )
                    if layer.lighting_mode == 1:
                        reasons.append(
                            "complete-native-vu-lighting-before-layer0-"
                            "host-approximated"
                        )
                elif render_status == (
                    "diagnostic-maskedwrite-alphaadd-cutout-preview"
                ):
                    reasons.extend(
                        (
                            "strict-decoded-alpha-greater-than-204-"
                            "diagnostic",
                            "layer0-maskedwrite-additive-rgb-and-depth-"
                            "effects-omitted",
                            "layer1-alphaadd-low-alpha-fringe-omitted",
                            "per-pass-tfx-vu-framebuffer-state-"
                            "unreconstructed",
                        )
                    )
                elif render_status == "diagnostic-write-preview":
                    reasons.append(
                        "write-preview-is-diagnostic-not-native-final"
                    )
                elif render_status == "bounded-alphaadd-preview":
                    reasons.append(
                        "native-lighting-and-range-fade-unreconstructed"
                    )
                elif render_status == "diagnostic-color-preview":
                    reasons.append(
                        "color-layer-preview-is-diagnostic-not-native-final"
                    )
                if not route_status.startswith("connected-native-uv"):
                    reasons.append(
                        f"coordinate-route-{route_status}"
                    )
                else:
                    uv_index = int(route_status.rsplit("uv", 1)[1])
                    uv_name = f"Native UV{uv_index}"
                    missing_uv_users = sum(
                        mesh.uv_layers.get(uv_name) is None
                        for mesh in users
                    )
                    if missing_uv_users:
                        reasons.append(
                            f"missing-{uv_name.replace(' ', '-').lower()}"
                            f"-on-{missing_uv_users}-user-meshes"
                        )
                if layer.palette_policy == "runtime-gated":
                    reasons.append(
                        "runtime-palette-bank-selection-unresolved"
                    )
                if layer.kind == "animated_texture":
                    reasons.append(
                        "animated-texture-preview-shows-first-frame-only"
                    )
                if texture.pixel_format_name == "RGB_888":
                    reasons.append(
                        "rgb888-host-alpha-is-not-file-alpha"
                    )

                usage = {
                    "material_token": material_token,
                    "layer_index": layer.index,
                    "provider_kind": layer.kind,
                    "animated_frame_index": frame_index,
                    "paths": material_paths,
                    "operation": layer.operation,
                    "operation_name": document.get("operation_name"),
                    "coordinate_selector": layer.coordinate_selector,
                    "coordinate_route_status": route_status,
                    "palette_policy": layer.palette_policy,
                    "render_status": render_status,
                    "preview_mode": preview_mode,
                    "user_mesh_count": len(users),
                    "reasons": sorted(set(reasons)),
                }
                entry["usages"].append(usage)

    resolution_counts: Counter[str] = Counter()
    render_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    pixel_format_counts: Counter[str] = Counter()
    decode_status_counts: Counter[str] = Counter()
    investigation_tokens: list[str] = []
    texture_records: list[dict[str, object]] = []
    for token in sorted(entries):
        entry = entries[token]
        usages = entry["usages"]
        resolution_counts[str(entry["resolution_status"])] += 1
        pixel_format_counts[str(entry["pixel_format"])] += 1
        decode_status_counts[str(entry["decode_status"])] += 1
        render_statuses = sorted(
            {
                str(usage["render_status"])
                for usage in usages
            }
        )
        reasons = sorted(
            {
                reason
                for usage in usages
                for reason in usage["reasons"]
            }
        )
        for status in render_statuses:
            render_counts[status] += 1
        for reason in reasons:
            reason_counts[reason] += 1
        if reasons or not usages:
            investigation_tokens.append(str(entry["texture_token"]))
        entry["render_statuses"] = render_statuses
        entry["reasons"] = reasons
        entry["material_tokens"] = sorted(
            {
                str(usage["material_token"])
                for usage in usages
            }
        )
        texture_records.append(entry)

    return {
        "report_version": 1,
        "source_core_sha256": level.source_core_sha256,
        "scope": (
            "default-LOD selected direct and instance material textures"
        ),
        "summary": {
            "selected_texture_count": len(entries),
            "resolution_status_counts": dict(
                sorted(resolution_counts.items())
            ),
            "pixel_format_counts": dict(
                sorted(pixel_format_counts.items())
            ),
            "decode_status_counts": dict(
                sorted(decode_status_counts.items())
            ),
            "render_status_texture_counts": dict(
                sorted(render_counts.items())
            ),
            "reason_texture_counts": dict(
                sorted(reason_counts.items())
            ),
            "investigation_texture_count": len(
                investigation_tokens
            ),
            "provider_issue_count": len(provider_issues),
        },
        "investigation_texture_tokens": investigation_tokens,
        "provider_issues": provider_issues,
        "textures": texture_records,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    source = args.source.expanduser().resolve(strict=True)
    blend_path = args.blend.expanduser().resolve()
    png_path = args.png.expanduser().resolve()
    overview_path = args.overview_png.expanduser().resolve()
    material_path = (
        args.material_png.expanduser().resolve()
        if args.material_png is not None
        else None
    )
    report_path = args.report.expanduser().resolve()
    texture_log_path = (
        args.texture_log.expanduser().resolve()
        if args.texture_log is not None
        else None
    )
    output_paths = [blend_path, png_path, overview_path, report_path]
    if material_path is not None:
        output_paths.append(material_path)
    if texture_log_path is not None:
        output_paths.append(texture_log_path)
    for path in output_paths:
        path.parent.mkdir(parents=True, exist_ok=True)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    level = load_native_level(source, args.level_path)
    decal_library = load_native_decal_library(source)
    result = import_native_level_data(
        bpy.context,
        level,
        level_path=args.level_path,
        source_path=source,
    )
    level_name = PurePosixPath(args.level_path).stem
    level_title = level_name.removeprefix("level.").replace(".", " ").upper()
    camera = _configure_scene(level_title)

    direct_objects = tuple(
        obj for obj in result.objects if obj.get("killzone_path") == "direct"
    )
    main_points = _object_points(direct_objects)
    direct_span = _point_span(main_points)
    preview_hidden = tuple(
        obj
        for obj in result.objects
        if obj.get("killzone_path") == "instance"
        and max(obj.dimensions) > direct_span * 2.0
    )
    for obj in preview_hidden:
        obj.hide_render = True
        obj["killzone_preview_hidden"] = True
        obj["killzone_preview_hidden_reason"] = (
            "Enclosing background mesh obscures diagnostic geometry"
        )
    _frame_camera(camera, main_points)

    label_material = _emission_material(
        "Diagnostic Labels", (0.9, 0.95, 1.0, 1.0), 2.5
    )
    _add_camera_text(
        camera,
        f"{level_title} - COMPLETE STATIC RESOURCE GRAPH",
        0.92,
        0.07,
        label_material,
    )
    subtitle = _add_camera_text(
        camera,
        (
            f"DIRECT WORLD {level.render_plan.direct_triangle_count:,} "
            f"(ORANGE)  |  INSTANCED "
            f"{level.render_plan.instance_triangle_count:,} (BLUE)"
        ),
        0.66,
        0.045,
        label_material,
    )
    warning_material = _emission_material(
        "Diagnostic Material Warning", (0.12, 0.45, 1.0, 1.0), 3.0
    )
    _add_camera_text(
        camera,
        (
            "NATIVE MATERIALS ARE DIAGNOSTIC - RUNTIME DECALS NEED "
            "LIVE MARK STATE"
        ),
        0.43,
        0.032,
        warning_material,
    )

    main_location = camera.location.copy()
    main_rotation = camera.rotation_euler.copy()
    main_clip_start = camera.data.clip_start
    main_clip_end = camera.data.clip_end

    scene = bpy.context.scene
    scene.render.filepath = str(png_path)
    bpy.ops.render.render(write_still=True)

    visible_objects = tuple(
        obj for obj in result.objects if not obj.hide_render
    )
    all_points = _object_points(visible_objects)
    _frame_camera(camera, all_points, direction=(0.35, -0.55, 2.4))
    scene.render.filepath = str(overview_path)
    bpy.ops.render.render(write_still=True)

    camera.location = main_location
    camera.rotation_euler = main_rotation
    camera.data.clip_start = main_clip_start
    camera.data.clip_end = main_clip_end
    scene.render.filepath = str(png_path)

    report = _report(
        args.level_path, result, preview_hidden, decal_library
    )
    report["blend"] = str(blend_path)
    report["png"] = str(png_path)
    report["overview_png"] = str(overview_path)
    if material_path is not None:
        subtitle.data.body = (
            "BOUNDED ALPHAADD + LAYERBUMPDOT3; MASKEDWRITE FOLIAGE; "
            "WRITE / V4-8 / COLOR-LAYER DIAGNOSTICS"
        )
        preview_counts = enable_bounded_native_material_previews(
            (
                material
                for material in bpy.data.materials
                if material.get("killzone_core_local_token")
            ),
            result.objects,
            include_write=True,
            include_color_diagnostics=True,
        )
        scene.render.filepath = str(material_path)
        bpy.ops.render.render(write_still=True)
        report["material_png"] = str(material_path)
        report["material_preview_connected_materials"] = (
            sum(preview_counts.values())
        )
        report["material_preview_connection_modes"] = preview_counts
        report["material_preview_persisted_in_blend"] = True
    if texture_log_path is not None:
        texture_investigation = _texture_investigation(result)
        texture_investigation["level_path"] = args.level_path
        encoded_texture_log = (
            json.dumps(
                texture_investigation,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        atomic_write_bytes(texture_log_path, encoded_texture_log)
        report["texture_investigation_log"] = str(texture_log_path)
        report["texture_investigation_summary"] = (
            texture_investigation["summary"]
        )
    report["textured_preview_viewports"] = (
        _set_textured_preview_viewports()
    )
    encoded_report = (
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(report_path, encoded_report)

    notes = bpy.data.texts.new("NATIVE_LEVEL_IMPORT_DIAGNOSTICS.txt")
    notes.write(encoded_report.decode("utf-8"))
    if texture_log_path is not None:
        texture_notes = bpy.data.texts.new(
            "NATIVE_TEXTURE_INVESTIGATION.json"
        )
        texture_notes.write(encoded_texture_log.decode("utf-8"))
    root = result.root_collection
    root["killzone_report_path"] = str(report_path)
    root["killzone_preview_path"] = str(png_path)
    root["killzone_overview_path"] = str(overview_path)
    if texture_log_path is not None:
        root["killzone_texture_investigation_path"] = str(
            texture_log_path
        )
    root["killzone_runtime_decal_resource_count"] = len(
        decal_library.resources
    )
    root["killzone_runtime_decal_geometry_status"] = (
        "not-reconstructed-runtime-mark-state-is-not-serialized"
    )
    root["killzone_runtime_decal_material_policy"] = (
        "ordinary-native-material-layers-no-decal-preset"
    )
    if material_path is not None:
        root["killzone_material_preview_path"] = str(material_path)
        root["killzone_material_preview_persisted"] = True
    scene["killzone_viewport_shading"] = "MATERIAL_CAMERA"

    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
    print("KZ_LEVEL_PREVIEW=" + json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    arguments = sys.argv
    arguments = arguments[arguments.index("--") + 1 :] if "--" in arguments else []
    raise SystemExit(main(arguments))
