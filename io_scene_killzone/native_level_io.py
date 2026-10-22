# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from array import array
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import struct

import bpy
from mathutils import Matrix, Vector

from .core.native_level import (
    NativeLevel,
    NativeObjectKey,
    NativeSimpleSkinnedInstance,
    NativeStaticInstance,
    NativeStaticOccurrence,
    NativeStaticResourceNode,
    load_native_level,
)
from .core.native_level_material import (
    NativeAnimatedTexture,
    NativeLevelTexture,
    NativeMaterial,
)


@dataclass(frozen=True, slots=True)
class NativeLevelImportResult:
    level: NativeLevel
    root_collection: bpy.types.Collection
    direct_collection: bpy.types.Collection
    props_collection: bpy.types.Collection
    instance_collection: bpy.types.Collection
    animated_instance_collection: bpy.types.Collection
    objects: tuple[bpy.types.Object, ...]
    static_instance_objects: tuple[bpy.types.Object, ...]
    animated_instance_objects: tuple[bpy.types.Object, ...]
    unique_mesh_count: int
    winding_corrections: int
    native_material_count: int
    decoded_image_count: int
    material_preview_counts: dict[str, int]

    @property
    def prop_objects(self) -> tuple[bpy.types.Object, ...]:
        return self.static_instance_objects + self.animated_instance_objects


def _object_matrix(values: tuple[float, ...]) -> Matrix:
    return Matrix(
        (
            values[0:4],
            values[4:8],
            values[8:12],
            values[12:16],
        )
    ).transposed()


def _set_instance_source_properties(
    obj: bpy.types.Object,
    instance: NativeStaticInstance | NativeSimpleSkinnedInstance,
    *,
    serialized_type: str,
) -> None:
    obj["killzone_serialized_type"] = serialized_type
    obj["killzone_occurrence_kind"] = f"explicit-{serialized_type}"
    obj["killzone_explicit_instance"] = True
    obj["killzone_source_name"] = instance.name
    obj["killzone_name_string_index"] = instance.name_index
    obj["killzone_name_is_identifier"] = instance.name_is_identifier
    obj["killzone_object_id"] = f"0x{instance.object_id:08X}"
    obj["killzone_instance_section_offset"] = instance.key.section_offset
    obj["killzone_instance_object_index"] = instance.key.object_index
    obj["killzone_parent_token"] = f"0x{instance.parent_token:08X}"
    obj["killzone_resource_token"] = f"0x{instance.resource_token:08X}"
    obj["killzone_footer"] = f"0x{instance.footer:08X}"
    obj["killzone_source_matrix_f32"] = list(instance.matrix)
    obj["killzone_source_matrix_f32_hex"] = struct.pack(
        "<16f", *instance.matrix
    ).hex()


def _set_reference_key_properties(
    obj: bpy.types.Object,
    prefix: str,
    key: NativeObjectKey,
) -> None:
    obj[f"{prefix}_section_offset"] = key.section_offset
    obj[f"{prefix}_object_index"] = key.object_index


def _diagnostic_material() -> bpy.types.Material:
    name = "Killzone Native Level Diagnostic"
    material = bpy.data.materials.get(name)
    if material is not None:
        return material
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    object_info = nodes.new("ShaderNodeObjectInfo")
    principled.inputs["Roughness"].default_value = 0.72
    principled.inputs["Metallic"].default_value = 0.08
    links.new(object_info.outputs["Color"], principled.inputs["Base Color"])
    links.new(principled.outputs["BSDF"], output.inputs["Surface"])
    material["killzone_diagnostic_material"] = True
    return material


def _create_level_image(
    texture: NativeLevelTexture,
    rgba: bytes,
    *,
    palette_bank: str,
    level_path: str,
    source_path: str | Path | None,
) -> bpy.types.Image:
    level_name = PurePosixPath(level_path).stem
    suffix = "" if palette_bank == "normal" else f" {palette_bank.title()}"
    image = bpy.data.images.new(
        name=(
            f"{level_name} Texture 0x{texture.object_id:08X}{suffix}"
        ),
        width=texture.width,
        height=texture.height,
        alpha=True,
        float_buffer=False,
    )
    image.alpha_mode = "STRAIGHT"
    image.colorspace_settings.name = (
        "Non-Color"
        if texture.pixel_format_name.startswith("ALPHA_")
        else "sRGB"
    )

    pixels = array("f")
    row_size = texture.width * 4
    for y in range(texture.height - 1, -1, -1):
        row = rgba[y * row_size : (y + 1) * row_size]
        pixels.extend(component / 255.0 for component in row)
    image.pixels.foreach_set(pixels)
    image.use_fake_user = True
    image["killzone_source_level"] = level_path
    if source_path is not None:
        image["killzone_archive_source"] = str(
            Path(source_path).resolve()
        )
    image["killzone_core_local_token"] = (
        f"0x{texture.object_id:08X}"
    )
    image["killzone_physical_definition_count"] = len(
        texture.definition_keys
    )
    image["killzone_pixel_format"] = texture.pixel_format_name
    image["killzone_sampling_psm"] = texture.sampling_psm
    image["killzone_mip_count"] = texture.mip_count
    image["killzone_decode_status"] = texture.decode_status
    image["killzone_palette_bank"] = palette_bank
    image["killzone_payload_sha256"] = texture.payload_sha256
    image["killzone_decoded_rgba_sha256"] = hashlib.sha256(
        rgba
    ).hexdigest()
    image["killzone_host_row_flip"] = True
    image["killzone_half_texel_bias"] = 0.0
    image["killzone_native_export_supported"] = False
    image.update()
    image.pack()
    image["killzone_pixels_packed"] = True
    return image


_OPERATION_NAMES = (
    "WRITE",
    "MUL2X",
    "ALPHABLEND",
    "ALPHAMUL",
    "ALPHAMUL2X",
    "ALPHAADD",
    "ALPHASUB",
    "WRITEMASK",
    "MASKEDWRITE",
    "MASKEDADD",
    "MASKEDSUB",
    "LIGHTMAPCOLOR",
    "LIGHTMAPCOLOR2X",
    "LIGHTMAPINTENSITY",
    "LIGHTMAPINTENSITY2X",
    "GLOW",
    "LAYERBUMPEMBOSS",
    "LAYERBUMPEMBOSSINV",
    "LAYERBUMPDOT3",
    "PROJECTIVESHADOW",
    "STENCILSHADOW",
)


def _configure_texture_node(
    node: bpy.types.ShaderNodeTexImage,
    layer: object,
) -> None:
    node.interpolation = (
        "Closest" if layer.filter_policy == 0 else "Linear"
    )
    if layer.wrap_s == 0 and layer.wrap_t == 0:
        node.extension = "REPEAT"
    elif layer.wrap_s != 0 and layer.wrap_t != 0:
        node.extension = "EXTEND"
    node["killzone_wrap_s"] = (
        "REPEAT" if layer.wrap_s == 0 else "CLAMP"
    )
    node["killzone_wrap_t"] = (
        "REPEAT" if layer.wrap_t == 0 else "CLAMP"
    )
    node["killzone_filter_policy"] = layer.filter_policy


def _connect_layer_coordinates(
    *,
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    image_node: bpy.types.ShaderNodeTexImage,
    layer: object,
    node_y: float,
) -> str:
    if layer.matrix_4x4 is not None or layer.transform_6f is not None:
        return "preserved-unconnected-transform-convention-unresolved"
    selector = layer.coordinate_selector
    if selector in (0, 1, 2, 3, 4, 5, 6, 7):
        uv_index = selector if selector <= 3 else 0
        uv_node = nodes.new("ShaderNodeUVMap")
        uv_node.name = f"Native Layer {layer.index} UV{uv_index}"
        uv_node.label = (
            f"Selector {selector}: Native UV{uv_index}, V-down source"
        )
        uv_node.uv_map = f"Native UV{uv_index}"
        uv_node.location = (-820.0, node_y)
        links.new(uv_node.outputs["UV"], image_node.inputs["Vector"])
        return f"connected-native-uv{uv_index}"
    if selector == 8:
        return "preserved-unconnected-generated-environment"
    if selector in (9, 10):
        return "preserved-unconnected-generated-reflection"
    if selector == 11:
        return "preserved-unconnected-matrix-convention-unresolved"
    return f"preserved-unconnected-selector-{selector}"


def _native_material(
    native: NativeMaterial,
    *,
    level_path: str,
    textures_by_token: dict[int, NativeLevelTexture],
    animations_by_token: dict[int, NativeAnimatedTexture],
    images_by_token: dict[tuple[int, str], bpy.types.Image],
) -> bpy.types.Material:
    level_name = PurePosixPath(level_path).stem
    material = bpy.data.materials.new(
        f"{level_name} Material 0x{native.object_id:08X}"
    )
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (420.0, 0.0)
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (120.0, 0.0)
    principled.inputs["Roughness"].default_value = 0.72
    principled.inputs["Metallic"].default_value = 0.08
    object_info = nodes.new("ShaderNodeObjectInfo")
    object_info.location = (-180.0, 0.0)
    links.new(object_info.outputs["Color"], principled.inputs["Base Color"])
    links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    layer_documents: list[dict[str, object]] = []
    for layer in native.layers:
        document: dict[str, object] = {
            "index": layer.index,
            "kind": layer.kind,
            "resource_token": f"0x{layer.resource_token:08X}",
            "flags": layer.flags,
            "operation": layer.operation,
            "operation_name": _OPERATION_NAMES[layer.operation],
            "lighting_mode": layer.lighting_mode,
            "z_write": layer.z_write,
            "framebuffer_selector": layer.framebuffer_selector,
            "depth_comparison": layer.depth_comparison,
            "alpha_test": layer.alpha_test,
            "alpha_reference": layer.alpha_reference,
            "wrap_s": layer.wrap_s,
            "wrap_t": layer.wrap_t,
            "filter_policy": layer.filter_policy,
            "texture_enabled": layer.texture_enabled,
            "tfx_policy": layer.tfx_policy,
            "alpha_source_mode": layer.alpha_source_mode,
            "coordinate_selector": layer.coordinate_selector,
            "palette_policy": layer.palette_policy,
            "alpha_scalar": layer.alpha_scalar,
            "fade_mode": layer.fade_mode,
            "fade_values": layer.fade_values,
            "matrix_4x4": layer.matrix_4x4,
            "transform_6f": layer.transform_6f,
            "uses_default_transform": layer.uses_default_transform,
            "directional_vector": layer.directional_vector,
            "vu_features": layer.vu_features,
            "raw_record_hex": layer.raw_record.hex(),
            "raw_optional_payload_hex": (
                layer.raw_optional_payload.hex()
            ),
        }
        node_y = -300.0 * layer.index
        if layer.kind == "texture":
            texture = textures_by_token[layer.resource_token]
            image_bank = (
                layer.palette_policy
                if texture.base_rgba_complement is not None
                else "normal"
            )
            node = nodes.new("ShaderNodeTexImage")
            node.name = f"Native Texture Layer {layer.index}"
            node.label = (
                f"Layer {layer.index} {_OPERATION_NAMES[layer.operation]}: "
                f"0x{layer.resource_token:08X} "
                f"{texture.pixel_format_name} [{layer.palette_policy}]"
            )
            node.location = (-520.0, node_y)
            node.image = images_by_token.get(
                (layer.resource_token, image_bank)
            )
            _configure_texture_node(node, layer)
            route_status = _connect_layer_coordinates(
                nodes=nodes,
                links=links,
                image_node=node,
                layer=layer,
                node_y=node_y,
            )
            node["killzone_layer_kind"] = layer.kind
            node["killzone_layer_index"] = layer.index
            node["killzone_resource_token"] = (
                f"0x{layer.resource_token:08X}"
            )
            node["killzone_decode_status"] = texture.decode_status
            node["killzone_palette_policy"] = layer.palette_policy
            node["killzone_coordinate_route_status"] = route_status
            node["killzone_operation"] = layer.operation
            node["killzone_operation_name"] = _OPERATION_NAMES[
                layer.operation
            ]
            document.update(
                {
                    "pixel_format": texture.pixel_format_name,
                    "sampling_psm": texture.sampling_psm,
                    "decode_status": texture.decode_status,
                    "effective_image_bank": image_bank,
                    "coordinate_route_status": route_status,
                }
            )
            if (
                layer.palette_policy == "runtime-gated"
                and texture.base_rgba_complement is not None
            ):
                for bank, x in (
                    ("normal", -240.0),
                    ("complement", 40.0),
                ):
                    variant = nodes.new("ShaderNodeTexImage")
                    variant.name = (
                        f"Native Layer {layer.index} {bank.title()} Bank"
                    )
                    variant.label = (
                        f"Runtime-gated {bank} palette candidate"
                    )
                    variant.location = (x, node_y - 120.0)
                    variant.image = images_by_token.get(
                        (layer.resource_token, bank)
                    )
                    variant["killzone_palette_variant"] = bank
        elif layer.kind == "animated_texture":
            animation = animations_by_token[layer.resource_token]
            first_frame = animation.frames[0]
            first_texture = textures_by_token[first_frame.texture_token]
            image_bank = (
                layer.palette_policy
                if first_texture.base_rgba_complement is not None
                else "normal"
            )
            node = nodes.new("ShaderNodeTexImage")
            node.name = f"Native Animated Layer {layer.index}"
            node.label = (
                f"Layer {layer.index}: AnimatedTexture "
                f"0x{layer.resource_token:08X}, "
                f"{len(animation.frames)} frames"
            )
            node.location = (-520.0, node_y)
            node.image = images_by_token.get(
                (first_frame.texture_token, image_bank)
            )
            _configure_texture_node(node, layer)
            route_status = _connect_layer_coordinates(
                nodes=nodes,
                links=links,
                image_node=node,
                layer=layer,
                node_y=node_y,
            )
            node["killzone_layer_kind"] = layer.kind
            node["killzone_layer_index"] = layer.index
            node["killzone_resource_token"] = (
                f"0x{layer.resource_token:08X}"
            )
            node["killzone_palette_policy"] = layer.palette_policy
            node["killzone_coordinate_route_status"] = route_status
            node["killzone_operation"] = layer.operation
            node["killzone_operation_name"] = _OPERATION_NAMES[
                layer.operation
            ]
            frames = [
                {
                    "texture_token": f"0x{frame.texture_token:08X}",
                    "duration": frame.duration,
                }
                for frame in animation.frames
            ]
            node["killzone_animation_frames_json"] = json.dumps(
                frames,
                separators=(",", ":"),
            )
            document["frames"] = frames
            document["coordinate_route_status"] = route_status
        elif layer.kind == "null_texture":
            node = nodes.new("ShaderNodeTexImage")
            node.name = f"Native Null Provider Layer {layer.index}"
            node.label = f"Layer {layer.index}: explicit null provider"
            node.location = (-520.0, node_y)
            node["killzone_layer_kind"] = layer.kind
            node["killzone_layer_index"] = layer.index
            node["killzone_operation"] = layer.operation
            node["killzone_operation_name"] = _OPERATION_NAMES[
                layer.operation
            ]
        layer_documents.append(document)

    material.diffuse_color = (0.8, 0.35, 0.08, 1.0)
    material["killzone_source_level"] = level_path
    material["killzone_core_local_token"] = (
        f"0x{native.object_id:08X}"
    )
    material["killzone_layers_json"] = json.dumps(
        layer_documents,
        separators=(",", ":"),
    )
    material["killzone_layer_images_connected_to_shader"] = False
    material["killzone_shader_status"] = (
        "Diagnostic object color; native layer state and proven direct UV "
        "routes are inspectable, but VU source color and runtime mutation "
        "are not reconstructed"
    )
    material["killzone_native_export_supported"] = False
    return material


def _replace_input_link(
    links: bpy.types.NodeLinks,
    input_socket: bpy.types.NodeSocket,
    output_socket: bpy.types.NodeSocket,
) -> None:
    for link in tuple(input_socket.links):
        links.remove(link)
    links.new(output_socket, input_socket)


def _ensure_shader_node(
    nodes: bpy.types.Nodes,
    node_type: str,
    name: str,
) -> bpy.types.Node:
    node = nodes.get(name)
    if node is not None:
        if node.bl_idname != node_type:
            raise RuntimeError(
                f"Node {name!r} has type {node.bl_idname}, "
                f"expected {node_type}"
            )
        return node
    node = nodes.new(node_type)
    node.name = name
    return node


def supports_bounded_native_alphaadd_preview(
    layer: Mapping[str, object],
) -> bool:
    if not (
        layer.get("kind") == "texture"
        and layer.get("operation") == 5
        and layer.get("coordinate_selector") == 0
        and layer.get("palette_policy") == "normal"
        and layer.get("alpha_scalar") == 255
        and layer.get("matrix_4x4") is None
        and layer.get("transform_6f") is None
        and layer.get("effective_image_bank") == "normal"
        and str(
            layer.get("coordinate_route_status", "")
        ).startswith("connected-native-uv")
    ):
        return False

    pixel_format = layer.get("pixel_format")
    if pixel_format == "INDEX_4":
        return layer.get("fade_mode") == 2
    if pixel_format != "INDEX_8":
        return False
    return (
        layer.get("flags") == 0
        and layer.get("lighting_mode") in (0, 1)
        and layer.get("z_write") == 0
        and layer.get("framebuffer_selector") == 1
        and layer.get("depth_comparison") == 3
        and layer.get("alpha_test") == 7
        and layer.get("alpha_reference") == 0
        and layer.get("texture_enabled") == 1
        and layer.get("tfx_policy") == 1
        and layer.get("alpha_source_mode") == 0
        and layer.get("fade_mode") in (0, 2)
        and layer.get("vu_features") == [False, False]
    )


def select_native_color_diagnostic_layer(
    layers: Iterable[Mapping[str, object]],
) -> Mapping[str, object] | None:
    layer_documents = tuple(layers)

    def eligible(layer: Mapping[str, object]) -> bool:
        if not (
            layer.get("kind") == "texture"
            and layer.get("operation") in (0, 5, 8, 11)
            and layer.get("pixel_format")
            in ("INDEX_4", "INDEX_8", "RGBA_8888", "RGB_888")
            and layer.get("palette_policy") == "normal"
            and layer.get("effective_image_bank") == "normal"
            and layer.get("texture_enabled") == 1
            and layer.get("alpha_scalar") == 255
        ):
            return False
        selector = layer.get("coordinate_selector")
        if not isinstance(selector, int) or not 0 <= selector <= 7:
            return False
        route_status = str(layer.get("coordinate_route_status", ""))
        if route_status.startswith("connected-native-uv"):
            return True
        return (
            route_status
            == "preserved-unconnected-transform-convention-unresolved"
            and layer.get("matrix_4x4") is None
            and layer.get("transform_6f") is not None
        )

    if (
        len(layer_documents) == 1
        and layer_documents[0].get("operation") in (5, 8)
        and eligible(layer_documents[0])
    ):
        return layer_documents[0]

    first = layer_documents[0] if layer_documents else None
    if (
        first is not None
        and first.get("operation") == 0
        and first.get("transform_6f") is not None
        and eligible(first)
    ):
        return first

    if len(layer_documents) >= 2:
        second = layer_documents[1]
        if (
            (first.get("operation"), second.get("operation"))
            in ((5, 11), (11, 5))
            and first.get("resource_token")
            == second.get("resource_token")
            and first.get("coordinate_selector")
            == second.get("coordinate_selector")
            and eligible(first)
        ):
            return first
    return None


def _connect_v4_8_modulation(
    *,
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    principled: bpy.types.Node,
    image_node: bpy.types.Node,
    diagnostic: bool,
) -> None:
    vertex_color = _ensure_shader_node(
        nodes,
        "ShaderNodeVertexColor",
        "Native Layer 0 V4-8",
    )
    vertex_color.label = (
        "Diagnostic V4-8 source color (128 = 1.0)"
        if diagnostic
        else "PS2 source color (128 = 1.0)"
    )
    vertex_color.layer_name = "Native V4-8"
    vertex_color.location = (-280.0, -180.0)

    rgb_multiply = _ensure_shader_node(
        nodes,
        "ShaderNodeVectorMath",
        "Native Layer 0 RGB Modulate",
    )
    rgb_multiply.operation = "MULTIPLY"
    rgb_multiply.location = (-40.0, 20.0)
    _replace_input_link(
        links,
        rgb_multiply.inputs[0],
        image_node.outputs["Color"],
    )
    _replace_input_link(
        links,
        rgb_multiply.inputs[1],
        vertex_color.outputs["Color"],
    )

    rgb_scale = _ensure_shader_node(
        nodes,
        "ShaderNodeVectorMath",
        "Native Layer 0 RGB Scale",
    )
    rgb_scale.operation = "SCALE"
    rgb_scale.location = (180.0, 20.0)
    rgb_scale.inputs[3].default_value = 255.0 / 128.0
    _replace_input_link(
        links,
        rgb_scale.inputs[0],
        rgb_multiply.outputs["Vector"],
    )

    alpha_multiply = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        "Native Layer 0 Alpha Modulate",
    )
    alpha_multiply.operation = "MULTIPLY"
    alpha_multiply.location = (-40.0, -180.0)
    _replace_input_link(
        links,
        alpha_multiply.inputs[0],
        image_node.outputs["Alpha"],
    )
    _replace_input_link(
        links,
        alpha_multiply.inputs[1],
        vertex_color.outputs["Alpha"],
    )

    alpha_scale = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        "Native Layer 0 Alpha Scale",
    )
    alpha_scale.operation = "MULTIPLY"
    alpha_scale.use_clamp = True
    alpha_scale.location = (180.0, -180.0)
    alpha_scale.inputs[1].default_value = 255.0 / 128.0
    _replace_input_link(
        links,
        alpha_scale.inputs[0],
        alpha_multiply.outputs["Value"],
    )

    _replace_input_link(
        links,
        principled.inputs["Base Color"],
        rgb_scale.outputs["Vector"],
    )
    _replace_input_link(
        links,
        principled.inputs["Emission Color"],
        rgb_scale.outputs["Vector"],
    )
    _replace_input_link(
        links,
        principled.inputs["Alpha"],
        alpha_scale.outputs["Value"],
    )


def _connect_native_color_diagnostic(
    *,
    material: bpy.types.Material,
    layer_documents: list[Mapping[str, object]],
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    principled: bpy.types.Node,
    use_v4_8: bool,
) -> bool:
    layer = select_native_color_diagnostic_layer(layer_documents)
    if layer is None:
        return False
    layer_index = int(layer["index"])
    image_node = next(
        (
            node
            for node in nodes
            if node.type == "TEX_IMAGE"
            and node.get("killzone_layer_index") == layer_index
            and node.image is not None
        ),
        None,
    )
    if image_node is None:
        return False

    route_status = str(layer["coordinate_route_status"])
    transform_ignored = route_status.startswith(
        "preserved-unconnected-transform"
    )
    if transform_ignored:
        selector = int(layer["coordinate_selector"])
        uv_index = selector if selector <= 3 else 0
        uv_node = _ensure_shader_node(
            nodes,
            "ShaderNodeUVMap",
            f"Native Layer {layer_index} Diagnostic UV{uv_index}",
        )
        uv_node.label = (
            f"Diagnostic Native UV{uv_index}; six-float transform ignored"
        )
        uv_node.uv_map = f"Native UV{uv_index}"
        uv_node.location = (-820.0, -300.0 * layer_index)
        _replace_input_link(
            links,
            image_node.inputs["Vector"],
            uv_node.outputs["UV"],
        )
        image_node["killzone_preview_coordinate_route"] = (
            f"diagnostic-native-uv{uv_index}-transform-ignored"
        )

    operation = int(layer["operation"])
    use_v4_8 = use_v4_8 and operation == 5
    if use_v4_8:
        _connect_v4_8_modulation(
            nodes=nodes,
            links=links,
            principled=principled,
            image_node=image_node,
            diagnostic=True,
        )
    else:
        _replace_input_link(
            links,
            principled.inputs["Base Color"],
            image_node.outputs["Color"],
        )
        _replace_input_link(
            links,
            principled.inputs["Emission Color"],
            image_node.outputs["Color"],
        )
    resource_token = layer["resource_token"]
    uses_alpha = (
        operation in (5, 8)
        and len(layer_documents) == 1
    ) or (
        operation in (11, 12)
        and any(
            candidate.get("operation") == 5
            and candidate.get("resource_token") == resource_token
            for candidate in layer_documents[layer_index + 1 :]
        )
    )
    if uses_alpha:
        if not use_v4_8:
            _replace_input_link(
                links,
                principled.inputs["Alpha"],
                image_node.outputs["Alpha"],
            )
        material.surface_render_method = (
            "DITHERED" if operation == 8 else "BLENDED"
        )
    else:
        for link in tuple(principled.inputs["Alpha"].links):
            links.remove(link)
        principled.inputs["Alpha"].default_value = 1.0
        material.surface_render_method = "DITHERED"

    principled.inputs["Emission Strength"].default_value = 0.35
    operation_name = str(layer["operation_name"])
    material["killzone_layer_images_connected_to_shader"] = True
    material["killzone_preview_mode"] = (
        (
            f"diagnostic-v4-8-color-layer{layer_index}"
            if use_v4_8
            else f"diagnostic-color-layer{layer_index}"
        )
    )
    material["killzone_preview_layer_index"] = layer_index
    material["killzone_preview_operation"] = operation_name
    material["killzone_preview_pixel_format"] = str(
        layer["pixel_format"]
    )
    material["killzone_preview_alpha_connected"] = uses_alpha
    material["killzone_preview_transform_ignored"] = transform_ignored
    material["killzone_preview_v4_8_modulation"] = use_v4_8
    material["killzone_preview_emission_strength"] = 0.35
    material["killzone_shader_status"] = (
        (
            f"Diagnostic decoded color from layer {layer_index} "
            f"{operation_name}, modulated by serialized Native V4-8; "
            "native depth/blend state and lighting remain unreconstructed"
        )
        if use_v4_8
        else (
            f"Diagnostic decoded color from layer {layer_index} "
            f"{operation_name}; native lighting and multi-layer composition "
            "remain unreconstructed"
        )
    )
    return True


def enable_bounded_native_material_previews(
    materials: Iterable[bpy.types.Material],
    objects: Iterable[bpy.types.Object],
    *,
    include_write: bool,
    include_color_diagnostics: bool = False,
) -> dict[str, int]:
    native_materials = tuple(materials)
    user_meshes: dict[bpy.types.Material, set[bpy.types.Mesh]] = {
        material: set() for material in native_materials
    }
    for obj in objects:
        if obj.type != "MESH":
            continue
        mesh = obj.data
        for material in mesh.materials:
            if material in user_meshes:
                user_meshes[material].add(mesh)

    counts = {"write": 0, "alphaadd": 0}
    if include_color_diagnostics:
        counts["color"] = 0
    for material in native_materials:
        if not material.get("killzone_core_local_token"):
            continue
        node_tree = material.node_tree
        if node_tree is None:
            continue
        nodes = node_tree.nodes
        links = node_tree.links
        principled = next(
            (
                node
                for node in nodes
                if node.type == "BSDF_PRINCIPLED"
            ),
            None,
        )
        if principled is None:
            continue

        if include_write:
            write_node = next(
                (
                    node
                    for node in nodes
                    if node.type == "TEX_IMAGE"
                    and node.get("killzone_layer_index") == 0
                    and node.get("killzone_operation") == 0
                    and node.image is not None
                    and str(
                        node.get(
                            "killzone_coordinate_route_status",
                            "",
                        )
                    ).startswith("connected-native-uv")
                ),
                None,
            )
            if write_node is not None:
                _replace_input_link(
                    links,
                    principled.inputs["Base Color"],
                    write_node.outputs["Color"],
                )
                _replace_input_link(
                    links,
                    principled.inputs["Emission Color"],
                    write_node.outputs["Color"],
                )
                principled.inputs[
                    "Emission Strength"
                ].default_value = 0.35
                material[
                    "killzone_preview_base_write_connected"
                ] = True
                material["killzone_preview_emission_strength"] = 0.35
                material[
                    "killzone_layer_images_connected_to_shader"
                ] = True
                material["killzone_preview_mode"] = "layer0-write"
                material["killzone_preview_layer_index"] = 0
                counts["write"] += 1
                continue

        raw_layers = material.get("killzone_layers_json")
        if not isinstance(raw_layers, str):
            continue
        layer_documents = json.loads(raw_layers)
        if include_color_diagnostics:
            meshes = user_meshes[material]
            has_v4_8 = bool(meshes) and all(
                mesh.color_attributes.get("Native V4-8") is not None
                for mesh in meshes
            )
            exact_alphaadd = (
                len(layer_documents) == 1
                and supports_bounded_native_alphaadd_preview(
                    layer_documents[0]
                )
                and has_v4_8
            )
            if (
                not exact_alphaadd
                and _connect_native_color_diagnostic(
                    material=material,
                    layer_documents=layer_documents,
                    nodes=nodes,
                    links=links,
                    principled=principled,
                    use_v4_8=has_v4_8,
                )
            ):
                counts["color"] += 1
                continue
        if len(layer_documents) != 1:
            continue
        layer = layer_documents[0]
        if not supports_bounded_native_alphaadd_preview(layer):
            continue
        meshes = user_meshes[material]
        if not meshes or any(
            mesh.color_attributes.get("Native V4-8") is None
            for mesh in meshes
        ):
            continue
        image_node = next(
            (
                node
                for node in nodes
                if node.type == "TEX_IMAGE"
                and node.get("killzone_layer_index") == 0
                and node.get("killzone_operation") == 5
                and node.image is not None
            ),
            None,
        )
        if image_node is None:
            continue

        _connect_v4_8_modulation(
            nodes=nodes,
            links=links,
            principled=principled,
            image_node=image_node,
            diagnostic=False,
        )
        principled.inputs["Emission Strength"].default_value = 0.35
        material.surface_render_method = "BLENDED"
        material["killzone_layer_images_connected_to_shader"] = True
        pixel_format = str(layer["pixel_format"])
        material["killzone_preview_mode"] = (
            f"single-layer-{pixel_format.lower().replace('_', '')}"
            "-alphaadd-v4-8"
        )
        material["killzone_preview_layer_index"] = 0
        material["killzone_preview_pixel_format"] = pixel_format
        material["killzone_preview_source_over"] = True
        material["killzone_preview_ps2_color_one"] = 128
        material["killzone_preview_emission_strength"] = 0.35
        material["killzone_shader_status"] = (
            f"Bounded direct-UV {pixel_format} ALPHAADD preview with "
            "Native V4-8 color/alpha modulation; full runtime lighting "
            "remains unreconstructed"
        )
        counts["alphaadd"] += 1

    return counts


def create_native_materials(
    logical_textures: Iterable[NativeLevelTexture],
    animated_textures: Iterable[NativeAnimatedTexture],
    materials: Iterable[NativeMaterial],
    *,
    selected_texture_tokens: Iterable[int],
    asset_path: str,
    source_path: str | Path | None,
) -> tuple[
    dict[int, bpy.types.Material],
    dict[tuple[int, str], bpy.types.Image],
]:
    textures_by_token = {
        texture.object_id: texture
        for texture in logical_textures
    }
    images_by_token: dict[tuple[int, str], bpy.types.Image] = {}
    for token in sorted(set(selected_texture_tokens)):
        texture = textures_by_token[token]
        if texture.base_rgba is not None:
            images_by_token[(token, "normal")] = _create_level_image(
                texture,
                texture.base_rgba,
                palette_bank="normal",
                level_path=asset_path,
                source_path=source_path,
            )
        if texture.base_rgba_complement is not None:
            images_by_token[(token, "complement")] = _create_level_image(
                texture,
                texture.base_rgba_complement,
                palette_bank="complement",
                level_path=asset_path,
                source_path=source_path,
            )
    animations_by_token = {
        animation.object_id: animation
        for animation in animated_textures
    }
    materials_by_token = {
        material.object_id: _native_material(
            material,
            level_path=asset_path,
            textures_by_token=textures_by_token,
            animations_by_token=animations_by_token,
            images_by_token=images_by_token,
        )
        for material in materials
    }
    return materials_by_token, images_by_token


def _create_level_materials(
    level: NativeLevel,
    *,
    level_path: str,
    source_path: str | Path | None,
    import_textures: bool,
) -> tuple[
    dict[int, bpy.types.Material],
    dict[tuple[int, str], bpy.types.Image],
]:
    return create_native_materials(
        level.logical_textures,
        level.animated_textures,
        level.materials,
        selected_texture_tokens=(
            (
                set(level.material_plan.direct_texture_tokens)
                | set(level.material_plan.instance_texture_tokens)
            )
            if import_textures
            else ()
        ),
        asset_path=level_path,
        source_path=source_path,
    )


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


def _build_resource_mesh(
    node: NativeStaticResourceNode,
    materials_by_token: dict[int, bpy.types.Material],
    fallback_material: bpy.types.Material,
) -> tuple[bpy.types.Mesh, int]:
    resource = node.regular
    if resource is None:
        raise ValueError("Only regular static leaves can create Blender meshes")

    positions: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    colors: list[tuple[int, int, int, int] | None] = []
    coordinates: list[tuple[tuple[float, float], ...]] = []
    adc_values: list[bool] = []
    triangles: list[tuple[int, int, int]] = []
    material_ids: list[int] = []
    winding_corrections = 0

    for packet in resource.packets:
        base = len(positions)
        for vertex in packet.vertices:
            positions.append(vertex.position)
            normals.append(vertex.normal)
            colors.append(vertex.color)
            coordinates.append(vertex.coordinates)
            adc_values.append(vertex.adc)
        material_id = resource.records[packet.mesh_record_index].material_id
        for packet_triangle in packet.triangles:
            triangle = tuple(base + index for index in packet_triangle)
            corrected, changed = _corrected_triangle(
                triangle, positions, normals
            )
            triangles.append(corrected)
            material_ids.append(material_id)
            winding_corrections += int(changed)

    mesh = bpy.data.meshes.new(
        f"Static 0x{node.object_id:08X} "
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

    adc_attribute = mesh.attributes.new(
        name="killzone_adc",
        type="BOOLEAN",
        domain="POINT",
    )
    for index, value in enumerate(adc_values):
        adc_attribute.data[index].value = value

    material_attribute = mesh.attributes.new(
        name="killzone_material_id",
        type="INT",
        domain="FACE",
    )
    for index, value in enumerate(material_ids):
        material_attribute.data[index].value = value

    mesh["killzone_object_id"] = f"0x{node.object_id:08X}"
    mesh["killzone_section_offset"] = node.key.section_offset
    mesh["killzone_object_index"] = node.key.object_index
    mesh["killzone_packet_count"] = len(resource.packets)
    mesh["killzone_triangle_count"] = resource.triangle_count
    mesh["killzone_uv_layer_count"] = uv_count
    mesh["killzone_winding_corrections"] = winding_corrections
    mesh["killzone_packet_shapes"] = str(
        sorted({packet.tail_opcodes for packet in resource.packets})
    )
    return mesh, winding_corrections


def _object_color(
    occurrence: NativeStaticOccurrence,
    object_id: int,
) -> tuple[float, float, float, float]:
    variation = ((object_id >> 4) & 0xFF) / 255.0
    if occurrence.path == "direct":
        return (
            0.72 + 0.2 * variation,
            0.24 + 0.18 * variation,
            0.06,
            1.0,
        )
    return (
        0.04,
        0.32 + 0.22 * variation,
        0.72 + 0.2 * variation,
        1.0,
    )


def import_native_level_data(
    context: bpy.types.Context,
    level: NativeLevel,
    *,
    level_path: str,
    source_path: str | Path | None = None,
    import_textures: bool = True,
) -> NativeLevelImportResult:
    scene_collection = context.scene.collection
    display_name = Path(level_path).name.removesuffix(".core")
    root_collection = bpy.data.collections.new(
        f"Killzone {display_name}"
    )
    direct_collection = bpy.data.collections.new("Direct World (identity)")
    props_collection = bpy.data.collections.new("Props")
    instance_collection = bpy.data.collections.new("Static Instances")
    animated_instance_collection = bpy.data.collections.new(
        "Animated Skinned Instances (metadata only)"
    )
    scene_collection.children.link(root_collection)
    root_collection.children.link(direct_collection)
    root_collection.children.link(props_collection)
    props_collection.children.link(instance_collection)
    props_collection.children.link(animated_instance_collection)

    root_collection["killzone_source_path"] = level_path
    root_collection["killzone_core_sha256"] = level.source_core_sha256
    root_collection["killzone_corestream_sha256"] = (
        level.source_corestream_sha256
    )
    root_collection["killzone_physical_packet_count"] = (
        level.physical_packet_count
    )
    root_collection["killzone_physical_triangle_count"] = (
        level.physical_triangle_count
    )
    root_collection["killzone_resource_root_count"] = len(
        level.render_plan.roots
    )
    root_collection["killzone_direct_root_count"] = len(
        level.render_plan.direct_roots
    )
    root_collection["killzone_direct_triangle_count"] = (
        level.render_plan.direct_triangle_count
    )
    root_collection["killzone_instance_triangle_count"] = (
        level.render_plan.instance_triangle_count
    )
    root_collection["killzone_static_prop_count"] = len(level.instances)
    root_collection["killzone_animated_prop_count"] = len(
        level.skinned_instances
    )
    root_collection["killzone_prop_count"] = (
        len(level.instances) + len(level.skinned_instances)
    )
    root_collection["killzone_null_static_prop_count"] = sum(
        instance.resource_key is None for instance in level.instances
    )
    root_collection["killzone_packet_shapes"] = str(level.packet_shapes)
    root_collection["killzone_physical_texture_count"] = len(
        level.textures
    )
    root_collection["killzone_logical_texture_count"] = len(
        level.logical_textures
    )
    root_collection["killzone_animated_texture_count"] = len(
        level.animated_textures
    )
    root_collection["killzone_animated_texture_frame_count"] = sum(
        len(animation.frames) for animation in level.animated_textures
    )
    root_collection["killzone_selected_material_count"] = len(
        level.materials
    )
    root_collection["killzone_textures_imported"] = import_textures

    resources_by_key = {node.key: node for node in level.resources}
    fallback_material = _diagnostic_material()
    materials_by_token, images_by_token = _create_level_materials(
        level,
        level_path=level_path,
        source_path=source_path,
        import_textures=import_textures,
    )
    mesh_cache: dict[NativeObjectKey, bpy.types.Mesh] = {}
    winding_corrections = 0
    objects: list[bpy.types.Object] = []
    static_instance_objects: list[bpy.types.Object] = []
    animated_instance_objects: list[bpy.types.Object] = []

    def mesh_for(
        node: NativeStaticResourceNode,
    ) -> bpy.types.Mesh:
        nonlocal winding_corrections
        mesh = mesh_cache.get(node.key)
        if mesh is None:
            mesh, corrections = _build_resource_mesh(
                node,
                materials_by_token,
                fallback_material,
            )
            mesh_cache[node.key] = mesh
            winding_corrections += corrections
        return mesh

    for occurrence_index, occurrence in enumerate(
        level.render_plan.direct_occurrences
    ):
        node = resources_by_key[occurrence.leaf_key]
        obj = bpy.data.objects.new(
            f"Direct {occurrence_index:04d} "
            f"0x{node.object_id:08X}",
            mesh_for(node),
        )
        obj.matrix_world = _object_matrix(occurrence.matrix)
        obj.color = _object_color(occurrence, node.object_id)
        obj["killzone_path"] = "direct"
        obj["killzone_occurrence_kind"] = "implicit-direct-world"
        obj["killzone_explicit_instance"] = False
        obj["killzone_leaf_object_id"] = f"0x{node.object_id:08X}"
        obj["killzone_leaf_section_offset"] = node.key.section_offset
        obj["killzone_leaf_object_index"] = node.key.object_index
        obj["killzone_source_section_offset"] = (
            occurrence.source_key.section_offset
        )
        obj["killzone_source_object_index"] = (
            occurrence.source_key.object_index
        )
        direct_collection.objects.link(obj)
        objects.append(obj)

    occurrences_by_instance: dict[
        NativeObjectKey, list[tuple[int, NativeStaticOccurrence]]
    ] = {}
    direct_count = len(level.render_plan.direct_occurrences)
    for index, occurrence in enumerate(
        level.render_plan.instance_occurrences,
        start=direct_count,
    ):
        if occurrence.instance_key is None:
            raise ValueError("Static instance occurrence has no instance key")
        occurrences_by_instance.setdefault(
            occurrence.instance_key, []
        ).append((index, occurrence))

    for instance in level.instances:
        instance_occurrences = occurrences_by_instance.pop(
            instance.key, []
        )
        resource = (
            None
            if instance.resource_key is None
            else resources_by_key[instance.resource_key]
        )
        if len(instance_occurrences) == 1:
            if resource is None:
                raise ValueError(
                    "Static instance occurrence has no resolved resource"
                )
            occurrence_index, occurrence = instance_occurrences[0]
            node = resources_by_key[occurrence.leaf_key]
            owner = bpy.data.objects.new(
                instance.name
                or f"Static Prop 0x{instance.object_id:08X}",
                mesh_for(node),
            )
            instance_collection.objects.link(owner)
            owner.matrix_world = _object_matrix(instance.matrix)
            owner.color = _object_color(occurrence, node.object_id)
            owner["killzone_path"] = "instance"
            owner["killzone_render_role"] = (
                "authored-single-leaf-static-occurrence"
            )
            _set_instance_source_properties(
                owner,
                instance,
                serialized_type="StaticMeshInstance",
            )
            owner["killzone_resource_status"] = "resolved"
            owner["killzone_resource_kind"] = resource.kind
            _set_reference_key_properties(
                owner,
                "killzone_resource",
                resource.key,
            )
            owner["killzone_selected_leaf_count"] = 1
            owner["killzone_leaf_object_id"] = (
                f"0x{node.object_id:08X}"
            )
            owner["killzone_leaf_section_offset"] = (
                node.key.section_offset
            )
            owner["killzone_leaf_object_index"] = node.key.object_index
            owner["killzone_source_section_offset"] = (
                occurrence.source_key.section_offset
            )
            owner["killzone_source_object_index"] = (
                occurrence.source_key.object_index
            )
            owner["killzone_render_occurrence_index"] = occurrence_index
            static_instance_objects.append(owner)
            objects.append(owner)
            continue

        owner = bpy.data.objects.new(
            instance.name
            or f"Static Prop 0x{instance.object_id:08X}",
            None,
        )
        owner.empty_display_type = "PLAIN_AXES"
        instance_collection.objects.link(owner)
        owner.matrix_world = _object_matrix(instance.matrix)
        owner["killzone_path"] = "instance-owner"
        _set_instance_source_properties(
            owner,
            instance,
            serialized_type="StaticMeshInstance",
        )
        if instance.resource_key is None:
            owner["killzone_resource_status"] = "null"
            owner["killzone_selected_leaf_count"] = 0
        else:
            owner["killzone_resource_status"] = "resolved"
            owner["killzone_resource_kind"] = resource.kind
            _set_reference_key_properties(
                owner,
                "killzone_resource",
                resource.key,
            )
        owner["killzone_selected_leaf_count"] = len(instance_occurrences)
        static_instance_objects.append(owner)

        for occurrence_index, occurrence in instance_occurrences:
            node = resources_by_key[occurrence.leaf_key]
            obj = bpy.data.objects.new(
                f"Instance {occurrence_index:04d} "
                f"0x{node.object_id:08X}",
                mesh_for(node),
            )
            obj.parent = owner
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_world = owner.matrix_world.copy()
            obj.color = _object_color(occurrence, node.object_id)
            obj["killzone_path"] = "instance"
            obj["killzone_occurrence_kind"] = "static-instance-leaf"
            obj["killzone_explicit_instance"] = True
            obj["killzone_leaf_object_id"] = f"0x{node.object_id:08X}"
            obj["killzone_leaf_section_offset"] = node.key.section_offset
            obj["killzone_leaf_object_index"] = node.key.object_index
            obj["killzone_source_section_offset"] = (
                occurrence.source_key.section_offset
            )
            obj["killzone_source_object_index"] = (
                occurrence.source_key.object_index
            )
            obj["killzone_instance_section_offset"] = (
                instance.key.section_offset
            )
            obj["killzone_instance_object_index"] = (
                instance.key.object_index
            )
            instance_collection.objects.link(obj)
            objects.append(obj)

    if occurrences_by_instance:
        raise ValueError(
            "Static render plan references unknown instance keys"
        )

    for instance in level.skinned_instances:
        owner = bpy.data.objects.new(
            instance.name
            or f"Animated Prop 0x{instance.object_id:08X}",
            None,
        )
        owner.empty_display_type = "ARROWS"
        owner.matrix_world = _object_matrix(instance.matrix)
        owner["killzone_path"] = "animated-instance-owner"
        _set_instance_source_properties(
            owner,
            instance,
            serialized_type="SimpleAnimatingSkinnedMeshInstance",
        )
        owner["killzone_geometry_status"] = (
            "metadata-only-skinned-geometry-not-decoded"
        )
        owner["killzone_resource_type"] = instance.resource_type
        _set_reference_key_properties(
            owner,
            "killzone_resource",
            instance.resource_key,
        )
        owner["killzone_default_animation_token"] = (
            f"0x{instance.default_animation_token:08X}"
        )
        _set_reference_key_properties(
            owner,
            "killzone_default_animation",
            instance.default_animation_key,
        )
        owner["killzone_blend_shape_animation_token"] = (
            f"0x{instance.blend_shape_animation_token:08X}"
        )
        if instance.parent_key is None:
            owner["killzone_parenting_status"] = "no-parent"
        else:
            owner["killzone_parenting_status"] = (
                "reference-preserved-composition-order-unresolved"
            )
            _set_reference_key_properties(
                owner,
                "killzone_parent",
                instance.parent_key,
            )
        animated_instance_collection.objects.link(owner)
        animated_instance_objects.append(owner)

    preview_counts = enable_bounded_native_material_previews(
        materials_by_token.values(),
        objects,
        include_write=import_textures,
        include_color_diagnostics=import_textures,
    )
    root_collection["killzone_unique_leaf_mesh_count"] = len(mesh_cache)
    root_collection["killzone_winding_corrections"] = winding_corrections
    root_collection["killzone_decoded_image_count"] = len(images_by_token)
    root_collection["killzone_bounded_alphaadd_preview_count"] = (
        preview_counts["alphaadd"]
    )
    root_collection["killzone_write_preview_count"] = preview_counts["write"]
    root_collection["killzone_color_diagnostic_preview_count"] = (
        preview_counts.get("color", 0)
    )
    root_collection["killzone_visible_material_preview_count"] = sum(
        preview_counts.values()
    )
    return NativeLevelImportResult(
        level=level,
        root_collection=root_collection,
        direct_collection=direct_collection,
        props_collection=props_collection,
        instance_collection=instance_collection,
        animated_instance_collection=animated_instance_collection,
        objects=tuple(objects),
        static_instance_objects=tuple(static_instance_objects),
        animated_instance_objects=tuple(animated_instance_objects),
        unique_mesh_count=len(mesh_cache),
        winding_corrections=winding_corrections,
        native_material_count=len(materials_by_token),
        decoded_image_count=len(images_by_token),
        material_preview_counts=preview_counts,
    )


def import_native_level(
    context: bpy.types.Context,
    source_path: str,
    level_path: str,
    *,
    import_textures: bool = True,
) -> NativeLevelImportResult:
    level = load_native_level(
        source_path,
        level_path,
        include_textures=import_textures,
    )
    return import_native_level_data(
        context,
        level,
        level_path=level_path,
        source_path=source_path,
        import_textures=import_textures,
    )
