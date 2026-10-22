# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Any, Iterable, Mapping

import bpy

from .core.bundle import (
    Bundle,
    MaterialSpec,
    atomic_copy_verified,
    atomic_write_bytes,
    make_manifest,
    write_manifest,
)
from .core.errors import BundleValidationError, ExportValidationError
from .core.mesh_format import (
    MeshData,
    Triangle,
    Vertex,
    mesh_sha256,
    serialize_mesh,
)

_ADDON_VERSION = "0.10.2"
_VERTEX_KEY = struct.Struct("<3f3f2f4B")
_DEFAULT_MATERIAL = MaterialSpec(name="Default", properties={})


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExportValidationError(
            "Material properties must contain finite JSON-compatible values"
        ) from exc


def _material_specs_from_object(obj: bpy.types.Object) -> tuple[MaterialSpec, ...]:
    materials: list[MaterialSpec] = []
    for index, slot in enumerate(obj.material_slots):
        material = slot.material
        if material is None:
            materials.append(
                MaterialSpec(name=f"Material_{index}", properties={})
            )
            continue

        raw_properties = material.get("kz_properties_json", "{}")
        if not isinstance(raw_properties, str):
            raise ExportValidationError(
                f"Material {material.name!r} has a non-text kz_properties_json"
            )
        try:
            properties = json.loads(raw_properties)
        except json.JSONDecodeError as exc:
            raise ExportValidationError(
                f"Material {material.name!r} has invalid kz_properties_json: "
                f"{exc.msg}"
            ) from exc
        if not isinstance(properties, dict):
            raise ExportValidationError(
                f"Material {material.name!r} kz_properties_json must be an object"
            )
        _canonical_json(properties)
        materials.append(
            MaterialSpec(name=material.name, properties=properties)
        )

    return tuple(materials) if materials else (_DEFAULT_MATERIAL,)


def _color_for_loop(
    color_attribute: bpy.types.Attribute | None,
    loop_index: int,
    vertex_index: int,
) -> tuple[int, int, int, int]:
    if color_attribute is None:
        return (255, 255, 255, 255)
    if color_attribute.domain == "POINT":
        color_value = color_attribute.data[vertex_index]
    elif color_attribute.domain == "CORNER":
        color_value = color_attribute.data[loop_index]
    else:
        raise ExportValidationError(
            f"Color attribute {color_attribute.name!r} must use POINT or CORNER domain"
        )

    values = (
        color_value.color_srgb
        if hasattr(color_value, "color_srgb")
        else color_value.color
    )
    if len(values) != 4 or not all(math.isfinite(component) for component in values):
        raise ExportValidationError(
            f"Color attribute {color_attribute.name!r} contains an invalid color"
        )
    return tuple(
        max(0, min(255, int(round(component * 255.0))))
        for component in values
    )


def mesh_data_from_object(obj: bpy.types.Object) -> MeshData:
    if obj.type != "MESH":
        raise ExportValidationError(f"Object {obj.name!r} is not a mesh")
    if obj.mode == "EDIT":
        obj.update_from_editmode()

    mesh = obj.data
    mesh.calc_loop_triangles()
    uv_layer = mesh.uv_layers.active
    color_attribute = mesh.color_attributes.get("Color")
    if color_attribute is None:
        color_attribute = mesh.color_attributes.active_color

    material_count = max(1, len(obj.material_slots))
    vertices: list[Vertex] = []
    triangles: list[Triangle] = []
    vertex_lookup: dict[bytes, int] = {}
    used_source_vertices: set[int] = set()
    corner_normals = mesh.corner_normals

    for triangle_index, loop_triangle in enumerate(mesh.loop_triangles):
        material_index = int(loop_triangle.material_index)
        if not 0 <= material_index < material_count:
            raise ExportValidationError(
                f"Triangle {triangle_index} on {obj.name!r} uses material "
                f"{material_index}, but only {material_count} slots exist"
            )

        output_indices: list[int] = []
        for loop_index in loop_triangle.loops:
            loop = mesh.loops[loop_index]
            source_vertex_index = loop.vertex_index
            used_source_vertices.add(source_vertex_index)
            position = tuple(float(value) for value in mesh.vertices[source_vertex_index].co)
            if loop_index < len(corner_normals):
                normal = tuple(
                    float(value) for value in corner_normals[loop_index].vector
                )
            else:
                normal = tuple(
                    float(value) for value in mesh.vertices[source_vertex_index].normal
                )
            uv = (
                tuple(float(value) for value in uv_layer.data[loop_index].uv)
                if uv_layer is not None
                else (0.0, 0.0)
            )
            color = _color_for_loop(
                color_attribute, loop_index, source_vertex_index
            )

            values = (*position, *normal, *uv)
            if not all(math.isfinite(value) for value in values):
                raise ExportValidationError(
                    f"Object {obj.name!r} contains non-finite mesh data"
                )
            packed = _VERTEX_KEY.pack(*values, *color)
            output_index = vertex_lookup.get(packed)
            if output_index is None:
                canonical = _VERTEX_KEY.unpack(packed)
                output_index = len(vertices)
                vertex_lookup[packed] = output_index
                vertices.append(
                    Vertex(
                        position=(canonical[0], canonical[1], canonical[2]),
                        normal=(canonical[3], canonical[4], canonical[5]),
                        uv=(canonical[6], canonical[7]),
                        color=(
                            canonical[8],
                            canonical[9],
                            canonical[10],
                            canonical[11],
                        ),
                    )
                )
            output_indices.append(output_index)

        triangles.append(
            Triangle(
                vertices=(
                    output_indices[0],
                    output_indices[1],
                    output_indices[2],
                ),
                material_index=material_index,
            )
        )

    unused_vertices = len(mesh.vertices) - len(used_source_vertices)
    if unused_vertices:
        raise ExportValidationError(
            f"Object {obj.name!r} has {unused_vertices} loose vertices; "
            "the interchange format stores triangle surfaces only"
        )
    return MeshData(vertices=tuple(vertices), triangles=tuple(triangles))


def _asset_state_digest(
    mesh: MeshData, materials: Iterable[MaterialSpec]
) -> str:
    material_payload = [
        {"name": material.name, "properties": dict(material.properties)}
        for material in materials
    ]
    digest = hashlib.sha256()
    digest.update(serialize_mesh(mesh))
    digest.update(_canonical_json(material_payload).encode("utf-8"))
    return digest.hexdigest()


def _create_material(spec: MaterialSpec) -> bpy.types.Material:
    material = bpy.data.materials.new(spec.name)
    material["kz_properties_json"] = _canonical_json(dict(spec.properties))

    base_color = spec.properties.get("base_color")
    if (
        isinstance(base_color, list)
        and len(base_color) == 4
        and all(
            isinstance(component, (int, float))
            and not isinstance(component, bool)
            and math.isfinite(component)
            for component in base_color
        )
    ):
        rgba = tuple(max(0.0, min(1.0, float(value))) for value in base_color)
        material.diffuse_color = rgba
    return material


def _create_mesh_object(
    asset: Any,
    mesh_data: MeshData,
    collection: bpy.types.Collection,
    manifest_path: Path,
) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(asset.name)
    mesh.from_pydata(
        [vertex.position for vertex in mesh_data.vertices],
        [],
        [triangle.vertices for triangle in mesh_data.triangles],
    )
    mesh.update(calc_edges=True)

    materials = asset.materials or (_DEFAULT_MATERIAL,)
    for material_spec in materials:
        mesh.materials.append(_create_material(material_spec))
    for polygon, triangle in zip(mesh.polygons, mesh_data.triangles, strict=True):
        polygon.material_index = triangle.material_index
        polygon.use_smooth = True

    if mesh_data.vertices:
        mesh.normals_split_custom_set_from_vertices(
            [vertex.normal for vertex in mesh_data.vertices]
        )

    uv_layer = mesh.uv_layers.new(name="UVMap")
    for loop in mesh.loops:
        uv_layer.data[loop.index].uv = mesh_data.vertices[loop.vertex_index].uv

    color_attribute = mesh.color_attributes.new(
        name="Color", type="BYTE_COLOR", domain="POINT"
    )
    for index, vertex in enumerate(mesh_data.vertices):
        color_attribute.data[index].color_srgb = tuple(
            component / 255.0 for component in vertex.color
        )

    obj = bpy.data.objects.new(asset.name, mesh)
    collection.objects.link(obj)
    obj["kz_bundle_version"] = 1
    obj["kz_asset_id"] = asset.id
    obj["kz_manifest_path"] = str(manifest_path)
    obj["kz_mesh_relative_path"] = asset.mesh_relative_path
    obj["kz_mesh_sha256"] = asset.mesh_sha256
    obj["kz_native_export_supported"] = False
    if asset.source is not None:
        obj["kz_source_relative_path"] = asset.source.relative_path
        obj["kz_source_sha256"] = asset.source.sha256
        obj["kz_source_format"] = asset.source.format

    snapshot_mesh = mesh_data_from_object(obj)
    obj["kz_snapshot_sha256"] = _asset_state_digest(
        snapshot_mesh, _material_specs_from_object(obj)
    )
    return obj


def import_bundle(
    context: bpy.types.Context, manifest_path: str | Path
) -> tuple[bpy.types.Object, ...]:
    bundle = Bundle.load(manifest_path)
    loaded_assets = []
    for asset in bundle.meshes:
        loaded_assets.append((asset, bundle.read_mesh(asset)))
        bundle.verify_source(asset)

    collection = bpy.data.collections.new(f"KZ_{bundle.root.name}")
    context.scene.collection.children.link(collection)
    collection["kz_manifest_path"] = str(bundle.manifest_path)
    collection["kz_bundle_version"] = 1

    created_objects: list[bpy.types.Object] = []
    created_meshes: list[bpy.types.Mesh] = []
    created_materials: list[bpy.types.Material] = []
    materials_before = set(bpy.data.materials)
    try:
        for asset, mesh_data in loaded_assets:
            obj = _create_mesh_object(
                asset, mesh_data, collection, bundle.manifest_path
            )
            created_objects.append(obj)
            created_meshes.append(obj.data)
            created_materials.extend(
                material
                for material in obj.data.materials
                if material not in materials_before
            )
    except Exception:
        for obj in created_objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in created_meshes:
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        for material in created_materials:
            if material.users == 0:
                bpy.data.materials.remove(material)
        bpy.data.collections.remove(collection)
        raise

    for selected in context.selected_objects:
        selected.select_set(False)
    for obj in created_objects:
        obj.select_set(True)
    if created_objects:
        context.view_layer.objects.active = created_objects[0]
    context.scene.kz_last_manifest = str(bundle.manifest_path)
    return tuple(created_objects)


def _require_kz_objects(
    objects: Iterable[bpy.types.Object],
) -> tuple[bpy.types.Object, ...]:
    result = tuple(
        obj
        for obj in objects
        if obj.type == "MESH" and isinstance(obj.get("kz_asset_id"), str)
    )
    if not result:
        raise ExportValidationError(
            "Select at least one mesh imported from a Killzone bundle"
        )
    seen_ids: set[str] = set()
    for obj in result:
        asset_id = obj["kz_asset_id"]
        if asset_id in seen_ids:
            raise ExportValidationError(
                f"Selection contains duplicate asset id {asset_id!r}"
            )
        seen_ids.add(asset_id)
    return result


def export_bundle(
    context: bpy.types.Context,
    manifest_path: str | Path,
    objects: Iterable[bpy.types.Object] | None = None,
) -> Mapping[str, Any]:
    output_manifest = Path(manifest_path).expanduser().resolve()
    output_root = output_manifest.parent
    selected_objects = _require_kz_objects(
        context.selected_objects if objects is None else objects
    )

    entries: list[Mapping[str, Any]] = []
    for obj in sorted(selected_objects, key=lambda item: item["kz_asset_id"]):
        asset_id = obj["kz_asset_id"]
        source_manifest = obj.get("kz_manifest_path")
        if not isinstance(source_manifest, str) or not source_manifest:
            raise ExportValidationError(
                f"Object {obj.name!r} is missing kz_manifest_path"
            )

        source_bundle = Bundle.load(source_manifest)
        source_asset = source_bundle.mesh_by_id(asset_id)
        original_mesh_bytes = source_bundle.read_mesh_bytes(source_asset)
        original_mesh = source_bundle.read_mesh(source_asset)
        source_bundle.verify_source(source_asset)

        current_mesh = mesh_data_from_object(obj)
        materials = _material_specs_from_object(obj)
        current_digest = _asset_state_digest(current_mesh, materials)
        unchanged = current_digest == obj.get("kz_snapshot_sha256")
        if unchanged:
            output_mesh_bytes = original_mesh_bytes
            edit_state = "unchanged"
        else:
            output_mesh_bytes = serialize_mesh(
                MeshData(
                    vertices=current_mesh.vertices,
                    triangles=current_mesh.triangles,
                    opaque_chunks=original_mesh.opaque_chunks,
                )
            )
            edit_state = "modified"

        mesh_relative_path = f"meshes/{asset_id}.kzm"
        atomic_write_bytes(
            output_root.joinpath(*mesh_relative_path.split("/")),
            output_mesh_bytes,
        )

        entry: dict[str, Any] = {
            "id": asset_id,
            "name": obj.name,
            "edit_state": edit_state,
            "mesh": {
                "path": mesh_relative_path,
                "sha256": mesh_sha256(output_mesh_bytes),
            },
            "materials": [
                {
                    "name": material.name,
                    "properties": dict(material.properties),
                }
                for material in materials
            ],
        }

        if source_asset.source is not None:
            suffix = source_asset.source.path.suffix or ".bin"
            source_relative_path = f"original/{asset_id}{suffix}"
            source_destination = output_root.joinpath(
                *source_relative_path.split("/")
            )
            atomic_copy_verified(
                source_asset.source.path,
                source_destination,
                source_asset.source.sha256,
            )
            entry["source"] = {
                "path": source_relative_path,
                "sha256": source_asset.source.sha256,
                "format": source_asset.source.format,
                "mode": "passthrough-only",
                "status": (
                    "unchanged"
                    if unchanged
                    else "preserved-original-not-reencoded"
                ),
            }

        entries.append(entry)

    document = make_manifest(
        entries,
        generator=f"io_scene_killzone/{_ADDON_VERSION} Blender "
        f"{bpy.app.version_string}",
    )
    write_manifest(output_manifest, document)
    context.scene.kz_last_manifest = str(output_manifest)
    return document
