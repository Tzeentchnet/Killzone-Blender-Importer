# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import struct
import time

import bpy
from mathutils import Matrix

from . import ADDON_VERSION
from .core.native_level import (
    NativeCollisionMeshInstance,
    NativeLevel,
    NativeObjectKey,
    NativeSimpleSkinnedInstance,
    NativeStaticInstance,
    NativeStaticOccurrence,
    NativeStaticResourceNode,
    load_native_level,
)
from .core.native_level_material import (
    NATIVE_GS_COLOR_ONE,
    NATIVE_LAYERBUMPDOT3_FRAGMENT_ALPHA,
    NATIVE_LAYERBUMPDOT3_V4_8_ALPHA,
    NATIVE_MASKEDWRITE_DECODED_ALPHA_CUTOFF,
    NativeAnimatedTexture,
    NativeLevelTexture,
    NativeMaterial,
    native_alpha_reference_byte,
    supports_bounded_native_layerbumpdot3_preview,
    supports_diagnostic_native_maskedwrite_alphaadd_preview,
)
from .native_blender_data import (
    build_native_mesh,
    create_packed_rgba_image,
    set_attribute_values,
)


# The GS blends display-encoded framebuffer bytes. Blender works in linear
# light, so display-space products are mapped through this gamma.
NATIVE_DISPLAY_GAMMA = 2.2
# Fragment alpha the VU supplies on the no-color-stream route
# (COMBINE_LIGHT_UNLIT_ALPHA_DIV_2); see docs/material_shader_research.md.
NATIVE_UNLIT_FRAGMENT_ALPHA = 64


@dataclass(frozen=True, slots=True)
class NativeMaterialPreviewOptions:
    """Opt-in artist approximations layered on the bounded previews."""

    artist_lightmap_layers: bool = False
    artist_fallback: bool = False


@dataclass(frozen=True, slots=True)
class NativeLevelImportResult:
    level: NativeLevel
    root_collection: bpy.types.Collection
    direct_collection: bpy.types.Collection
    props_collection: bpy.types.Collection
    instance_collection: bpy.types.Collection
    collision_instance_collection: bpy.types.Collection
    animated_instance_collection: bpy.types.Collection
    objects: tuple[bpy.types.Object, ...]
    static_instance_objects: tuple[bpy.types.Object, ...]
    collision_instance_objects: tuple[bpy.types.Object, ...]
    animated_instance_objects: tuple[bpy.types.Object, ...]
    unique_mesh_count: int
    winding_corrections: int
    native_material_count: int
    decoded_image_count: int
    material_preview_counts: dict[str, int]

    @property
    def prop_objects(self) -> tuple[bpy.types.Object, ...]:
        return (
            self.static_instance_objects
            + self.collision_instance_objects
            + self.animated_instance_objects
        )


def _object_matrix(values: tuple[float, ...]) -> Matrix:
    return Matrix(
        (
            values[0:4],
            values[4:8],
            values[8:12],
            values[12:16],
        )
    ).transposed()


def native_uv_to_blender(
    coordinate: tuple[float, float],
) -> tuple[float, float]:
    return coordinate[0], 1.0 - coordinate[1]


def set_native_import_provenance(
    collection: bpy.types.Collection,
    *,
    product: str,
    operator: str,
    level_placement_context_loaded: bool,
) -> None:
    collection["killzone_addon_version"] = ".".join(
        str(component) for component in ADDON_VERSION
    )
    collection["killzone_import_product"] = product
    collection["killzone_import_operator"] = operator
    collection["killzone_level_placement_context_loaded"] = (
        level_placement_context_loaded
    )


def _set_instance_source_properties(
    obj: bpy.types.Object,
    instance: (
        NativeStaticInstance
        | NativeCollisionMeshInstance
        | NativeSimpleSkinnedInstance
    ),
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
    obj["killzone_instance_directory_key"] = (
        f"{instance.key.section_offset:08X}:{instance.key.object_index}"
    )
    obj["killzone_parent_token"] = f"0x{instance.parent_token:08X}"
    obj["killzone_resource_token"] = f"0x{instance.resource_token:08X}"
    obj["killzone_footer"] = f"0x{instance.footer:08X}"
    obj["killzone_source_matrix_f32"] = list(instance.matrix)
    obj["killzone_source_matrix_f32_hex"] = struct.pack(
        "<16f", *instance.matrix
    ).hex()
    obj["killzone_transform_conversion"] = (
        "source-row-vector-matrix-transposed-once"
    )


def _set_reference_key_properties(
    obj: bpy.types.Object,
    prefix: str,
    key: NativeObjectKey,
) -> None:
    obj[f"{prefix}_section_offset"] = key.section_offset
    obj[f"{prefix}_object_index"] = key.object_index


def _set_collision_instance_properties(
    obj: bpy.types.Object,
    instance: NativeCollisionMeshInstance,
) -> None:
    obj["killzone_collision_group"] = (
        f"0x{instance.collision_group:08X}"
    )
    obj["killzone_collision_enabled"] = instance.collision_enabled
    obj["killzone_collision_resource_token"] = (
        f"0x{instance.collision_resource_token:08X}"
    )
    obj["killzone_collision_resource_type"] = (
        instance.collision_resource_type
    )
    _set_reference_key_properties(
        obj,
        "killzone_collision_resource",
        instance.collision_resource_key,
    )


def _diagnostic_material() -> bpy.types.Material:
    name = "Killzone Native Level Diagnostic"
    material = bpy.data.materials.get(name)
    if material is not None:
        return material
    material = bpy.data.materials.new(name)
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


def _native_image_intent(texture: NativeLevelTexture) -> str:
    if texture.pixel_format_name.startswith("ALPHA_"):
        return "scalar-mask"
    if texture.pixel_format_name == "GLOW_8":
        return "unresolved-dynamic-palette"
    return "visible-color"


def _level_image_name(
    texture: NativeLevelTexture,
    *,
    palette_bank: str,
    level_path: str,
) -> str:
    level_name = PurePosixPath(level_path).stem
    suffix = "" if palette_bank == "normal" else f" {palette_bank.title()}"
    return f"{level_name} Texture 0x{texture.object_id:08X}{suffix}"


def _existing_level_images() -> dict[tuple[str, str, str, str], bpy.types.Image]:
    """Index images from earlier imports for exact-content reuse."""

    existing: dict[tuple[str, str, str, str], bpy.types.Image] = {}
    for image in bpy.data.images:
        key = (
            image.get("killzone_source_level"),
            image.get("killzone_core_local_token"),
            image.get("killzone_palette_bank"),
            image.get("killzone_decoded_rgba_sha256"),
        )
        if all(isinstance(value, str) for value in key) and (
            image.packed_file is not None
        ):
            existing.setdefault(key, image)
    return existing


def _create_level_image(
    texture: NativeLevelTexture,
    rgba: bytes,
    *,
    palette_bank: str,
    level_path: str,
    source_path: str | Path | None,
    existing_images: (
        dict[tuple[str, str, str, str], bpy.types.Image] | None
    ) = None,
) -> bpy.types.Image:
    decoded_sha256 = hashlib.sha256(rgba).hexdigest()
    token = f"0x{texture.object_id:08X}"
    if existing_images is not None:
        reused = existing_images.get(
            (level_path, token, palette_bank, decoded_sha256)
        )
        if reused is not None:
            reused["killzone_reused_by_reimport"] = True
            return reused

    image_intent = _native_image_intent(texture)
    scalar_image = image_intent != "visible-color"
    image = create_packed_rgba_image(
        _level_image_name(
            texture,
            palette_bank=palette_bank,
            level_path=level_path,
        ),
        rgba,
        texture.width,
        texture.height,
        colorspace="Non-Color" if scalar_image else "sRGB",
    )
    image["killzone_source_level"] = level_path
    if source_path is not None:
        image["killzone_archive_source"] = str(
            Path(source_path).resolve()
        )
    image["killzone_core_local_token"] = token
    image["killzone_physical_definition_count"] = len(
        texture.definition_keys
    )
    image["killzone_pixel_format"] = texture.pixel_format_name
    image["killzone_sampling_psm"] = texture.sampling_psm
    image["killzone_mip_count"] = texture.mip_count
    image["killzone_decode_status"] = texture.decode_status
    image["killzone_palette_bank"] = palette_bank
    image["killzone_image_intent"] = image_intent
    image["killzone_payload_sha256"] = texture.payload_sha256
    image["killzone_decoded_rgba_sha256"] = decoded_sha256
    image["killzone_host_row_flip"] = True
    image["killzone_half_texel_bias"] = 0.0
    image["killzone_native_export_supported"] = False
    image["killzone_pixels_packed"] = True
    image["killzone_packed_encoding"] = "lossless-png-rgba8"
    if existing_images is not None:
        existing_images[(level_path, token, palette_bank, decoded_sha256)] = (
            image
        )
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
    # Mixed repeat/clamp layers sample with REPEAT and clamp only the
    # clamped axis in shader space; see _connect_mixed_wrap_coordinates.
    node.extension = (
        "EXTEND"
        if layer.wrap_s != 0 and layer.wrap_t != 0
        else "REPEAT"
    )
    node["killzone_wrap_s"] = (
        "REPEAT" if layer.wrap_s == 0 else "CLAMP"
    )
    node["killzone_wrap_t"] = (
        "REPEAT" if layer.wrap_t == 0 else "CLAMP"
    )
    node["killzone_filter_policy"] = layer.filter_policy


def _connect_mixed_wrap_coordinates(
    *,
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    coordinate_socket: bpy.types.NodeSocket,
    layer: object,
    node_y: float,
    image: bpy.types.Image | None = None,
) -> bpy.types.NodeSocket:
    """Emulate per-axis GS REPEAT/CLAMP on a REPEAT-extension image.

    The repeating axis is passed through unchanged so texture derivatives
    stay continuous (a FRACT here produces seam lines at every tile edge).
    The clamped axis is limited to the outermost texel centres, which
    prevents bilinear filtering from blending in the wrapped edge.
    """

    separate = nodes.new("ShaderNodeSeparateXYZ")
    separate.name = f"Native Layer {layer.index} Split UV"
    separate.label = "Native per-axis wrap"
    separate.location = (-660.0, node_y)
    links.new(coordinate_socket, separate.inputs["Vector"])

    combine = nodes.new("ShaderNodeCombineXYZ")
    combine.name = f"Native Layer {layer.index} Wrapped UV"
    combine.label = (
        f"U {'repeat' if layer.wrap_s == 0 else 'clamp'}, "
        f"V {'repeat' if layer.wrap_t == 0 else 'clamp'}"
    )
    combine.location = (-420.0, node_y)

    image_size = (
        tuple(image.size)
        if image is not None and image.size[0] and image.size[1]
        else (0, 0)
    )
    for axis, wrap_value, y_offset, texel_count in (
        ("X", layer.wrap_s, 80.0, image_size[0]),
        ("Y", layer.wrap_t, -80.0, image_size[1]),
    ):
        source = separate.outputs[axis]
        if wrap_value == 0:
            links.new(source, combine.inputs[axis])
            continue
        half_texel = 0.5 / texel_count if texel_count else 0.0
        lower = nodes.new("ShaderNodeMath")
        lower.name = f"Native Layer {layer.index} {axis} Clamp Lower"
        lower.operation = "MAXIMUM"
        lower.inputs[1].default_value = half_texel
        lower.location = (-600.0, node_y + y_offset)
        links.new(source, lower.inputs[0])

        upper = nodes.new("ShaderNodeMath")
        upper.name = f"Native Layer {layer.index} {axis} Clamp Upper"
        upper.label = f"{axis} clamp to edge texel centres"
        upper.operation = "MINIMUM"
        upper.inputs[1].default_value = 1.0 - half_texel
        upper.location = (-480.0, node_y + y_offset)
        links.new(lower.outputs["Value"], upper.inputs[0])
        links.new(upper.outputs["Value"], combine.inputs[axis])

    links.new(separate.outputs["Z"], combine.inputs["Z"])
    return combine.outputs["Vector"]


def _connect_axis_aligned_transform_6f(
    *,
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    coordinate_socket: bpy.types.NodeSocket,
    layer: object,
    node_y: float,
) -> bpy.types.NodeSocket | None:
    transform = layer.transform_6f
    if transform is None:
        return coordinate_socket
    if len(transform) != 6:
        raise ValueError("Native six-float UV transform must have six values")
    scale_u, cross_v_to_u, cross_u_to_v, scale_v, offset_u, offset_v = (
        transform
    )
    tolerance = 1.0e-5
    if (
        abs(cross_v_to_u) > tolerance
        or abs(cross_u_to_v) > tolerance
    ):
        return None

    mapping = nodes.new("ShaderNodeMapping")
    mapping.name = f"Native Layer {layer.index} Axis UV Transform"
    mapping.label = (
        "Bounded source UV scale/offset (V-down converted to V-up)"
    )
    mapping.vector_type = "POINT"
    mapping.location = (-650.0, node_y)
    mapping.inputs["Location"].default_value = (
        offset_u,
        1.0 - scale_v - offset_v,
        0.0,
    )
    mapping.inputs["Scale"].default_value = (
        scale_u,
        scale_v,
        1.0,
    )
    links.new(coordinate_socket, mapping.inputs["Vector"])
    return mapping.outputs["Vector"]


def _connect_layer_coordinates(
    *,
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    image_node: bpy.types.ShaderNodeTexImage,
    layer: object,
    node_y: float,
) -> str:
    if layer.matrix_4x4 is not None:
        return "preserved-unconnected-transform-convention-unresolved"
    selector = layer.coordinate_selector
    if selector in (0, 1, 2, 3, 4, 5, 6, 7):
        uv_index = selector if selector <= 3 else 0
        uv_node = nodes.new("ShaderNodeUVMap")
        uv_node.name = f"Native Layer {layer.index} UV{uv_index}"
        uv_node.label = (
            f"Selector {selector}: Native UV{uv_index}, V-up host"
        )
        uv_node.uv_map = f"Native UV{uv_index}"
        uv_node.location = (-820.0, node_y)
        coordinate_socket = uv_node.outputs["UV"]
        coordinate_socket = _connect_axis_aligned_transform_6f(
            nodes=nodes,
            links=links,
            coordinate_socket=coordinate_socket,
            layer=layer,
            node_y=node_y,
        )
        if coordinate_socket is None:
            nodes.remove(uv_node)
            return "preserved-unconnected-transform-convention-unresolved"
        transform_suffix = (
            "-bounded-axis-transform6f"
            if layer.transform_6f is not None
            else ""
        )
        if (layer.wrap_s == 0) != (layer.wrap_t == 0):
            coordinate_socket = _connect_mixed_wrap_coordinates(
                nodes=nodes,
                links=links,
                coordinate_socket=coordinate_socket,
                layer=layer,
                node_y=node_y,
                image=image_node.image,
            )
            route_status = (
                f"connected-native-uv{uv_index}{transform_suffix}"
                "-mixed-axis-wrap"
            )
        else:
            route_status = (
                f"connected-native-uv{uv_index}{transform_suffix}"
            )
        links.new(coordinate_socket, image_node.inputs["Vector"])
        return route_status
    if layer.transform_6f is not None:
        return "preserved-unconnected-transform-convention-unresolved"
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
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (420.0, 0.0)
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (120.0, 0.0)
    principled.inputs["Roughness"].default_value = 0.72
    principled.inputs["Metallic"].default_value = 0.0
    specular = principled.inputs.get("Specular IOR Level")
    if specular is not None:
        specular.default_value = 0.0
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
            image_intent = _native_image_intent(texture)
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
            node["killzone_image_intent"] = image_intent
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
                    "image_intent": image_intent,
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
            image_intent = _native_image_intent(first_texture)
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
            node["killzone_image_intent"] = image_intent
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
            document["image_intent"] = image_intent
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

    material["killzone_bounded_layerbumpdot3_candidate"] = (
        supports_bounded_native_layerbumpdot3_preview(
            native.layers,
            textures_by_token,
        )
    )
    material["killzone_maskedwrite_alphaadd_cutout_candidate"] = (
        supports_diagnostic_native_maskedwrite_alphaadd_preview(
            native.layers,
            textures_by_token,
        )
    )
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
    unimplemented_state = {
        "native-vu-lighting",
        "runtime-layer-mask-and-mutation",
        "per-pass-depth-and-framebuffer-state",
    }
    if len(native.layers) > 1:
        unimplemented_state.add("general-multipass-composition")
    if any(
        layer.coordinate_selector >= 8
        or layer.matrix_4x4 is not None
        or layer.transform_6f is not None
        for layer in native.layers
    ):
        unimplemented_state.add("generated-or-transformed-coordinates")
    if any(layer.kind == "animated_texture" for layer in native.layers):
        unimplemented_state.add("animated-texture-timing-and-looping")
    material["killzone_shader_profile"] = "unresolved"
    material["killzone_shader_unimplemented_state_json"] = json.dumps(
        sorted(unimplemented_state),
        separators=(",", ":"),
    )
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


def _quantized_alpha_byte(
    *,
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    alpha_socket: bpy.types.NodeSocket,
    prefix: str,
) -> bpy.types.NodeSocket:
    lower = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        f"{prefix} Clamp Lower",
    )
    lower.operation = "MAXIMUM"
    lower.inputs[1].default_value = 0.0
    _replace_input_link(links, lower.inputs[0], alpha_socket)

    upper = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        f"{prefix} Clamp Upper",
    )
    upper.operation = "MINIMUM"
    upper.inputs[1].default_value = 1.0
    _replace_input_link(
        links,
        upper.inputs[0],
        lower.outputs["Value"],
    )

    scale = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        f"{prefix} Byte Scale",
    )
    scale.operation = "MULTIPLY"
    scale.inputs[1].default_value = 255.0
    _replace_input_link(
        links,
        scale.inputs[0],
        upper.outputs["Value"],
    )

    quantize = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        f"{prefix} Byte Quantize",
    )
    quantize.operation = "ROUND"
    _replace_input_link(
        links,
        quantize.inputs[0],
        scale.outputs["Value"],
    )
    return quantize.outputs["Value"]


def _native_alpha_test_mask(
    *,
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    alpha_socket: bpy.types.NodeSocket,
    layer: Mapping[str, object],
) -> bpy.types.NodeSocket | None:
    alpha_test = int(layer.get("alpha_test", 7))
    if alpha_test == 7:
        return None
    reference = native_alpha_reference_byte(
        int(layer.get("alpha_reference", 0))
    )
    layer_index = int(layer.get("index", 0))
    prefix = f"Native Layer {layer_index} Alpha Test"

    if alpha_test == 0:
        never = _ensure_shader_node(
            nodes,
            "ShaderNodeValue",
            f"{prefix} Never",
        )
        never.label = "Native alpha test NEVER"
        never.outputs["Value"].default_value = 0.0
        return never.outputs["Value"]

    quantized_alpha = _quantized_alpha_byte(
        nodes=nodes,
        links=links,
        alpha_socket=alpha_socket,
        prefix=prefix,
    )

    comparison = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        f"{prefix} Compare",
    )
    _replace_input_link(
        links,
        comparison.inputs[0],
        quantized_alpha,
    )
    comparison["killzone_alpha_test"] = alpha_test
    comparison["killzone_runtime_alpha_reference"] = reference

    if alpha_test == 1:
        comparison.operation = "LESS_THAN"
        comparison.inputs[1].default_value = float(reference)
    elif alpha_test == 2:
        comparison.operation = "COMPARE"
        comparison.inputs[1].default_value = float(reference)
        comparison.inputs[2].default_value = 0.1
    elif alpha_test == 3:
        comparison.operation = "LESS_THAN"
        comparison.inputs[1].default_value = reference + 0.5
    elif alpha_test == 4:
        comparison.operation = "GREATER_THAN"
        comparison.inputs[1].default_value = float(reference)
    elif alpha_test == 5:
        comparison.operation = "COMPARE"
        comparison.inputs[1].default_value = float(reference)
        comparison.inputs[2].default_value = 0.1
        invert = _ensure_shader_node(
            nodes,
            "ShaderNodeMath",
            f"{prefix} Invert",
        )
        invert.operation = "SUBTRACT"
        invert.inputs[0].default_value = 1.0
        _replace_input_link(
            links,
            invert.inputs[1],
            comparison.outputs["Value"],
        )
        return invert.outputs["Value"]
    elif alpha_test == 6:
        comparison.operation = "GREATER_THAN"
        comparison.inputs[1].default_value = reference - 0.5
    else:
        raise ValueError(f"Unsupported native alpha test {alpha_test}")
    return comparison.outputs["Value"]


def _decoded_alpha_greater_mask(
    *,
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    alpha_socket: bpy.types.NodeSocket,
    decoded_reference: int,
) -> bpy.types.NodeSocket:
    if not 0 <= decoded_reference <= 0xFF:
        raise ValueError("Decoded alpha reference must be a byte")
    prefix = "Native MASKEDWRITE Decoded Alpha Cutout"
    quantized_alpha = _quantized_alpha_byte(
        nodes=nodes,
        links=links,
        alpha_socket=alpha_socket,
        prefix=prefix,
    )
    comparison = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        f"{prefix} Compare",
    )
    comparison.operation = "GREATER_THAN"
    comparison.inputs[1].default_value = float(decoded_reference)
    comparison["killzone_decoded_alpha_comparison"] = "GREATER"
    comparison["killzone_decoded_alpha_reference"] = decoded_reference
    comparison["killzone_runtime_alpha_reference"] = (
        native_alpha_reference_byte(decoded_reference)
    )
    _replace_input_link(
        links,
        comparison.inputs[0],
        quantized_alpha,
    )
    return comparison.outputs["Value"]


def _multiply_alpha_sockets(
    *,
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    left: bpy.types.NodeSocket,
    right: bpy.types.NodeSocket,
    name: str,
) -> bpy.types.NodeSocket:
    multiply = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        name,
    )
    multiply.operation = "MULTIPLY"
    multiply.use_clamp = True
    _replace_input_link(links, multiply.inputs[0], left)
    _replace_input_link(links, multiply.inputs[1], right)
    return multiply.outputs["Value"]


def _connect_emission_surface(
    *,
    material: bpy.types.Material,
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    color_socket: bpy.types.NodeSocket,
    alpha_socket: bpy.types.NodeSocket | None,
    render_method: str,
) -> None:
    output = next(
        (node for node in nodes if node.type == "OUTPUT_MATERIAL"),
        None,
    )
    if output is None:
        raise RuntimeError("Native material has no Material Output node")

    emission = _ensure_shader_node(
        nodes,
        "ShaderNodeEmission",
        "Killzone Preview Emission",
    )
    emission.label = "Unlit native color preview"
    emission.inputs["Strength"].default_value = 1.0
    emission.location = (120.0, -180.0)
    _replace_input_link(
        links,
        emission.inputs["Color"],
        color_socket,
    )

    if alpha_socket is None:
        _replace_input_link(
            links,
            output.inputs["Surface"],
            emission.outputs["Emission"],
        )
        return

    transparent = _ensure_shader_node(
        nodes,
        "ShaderNodeBsdfTransparent",
        "Killzone Preview Transparent",
    )
    transparent.location = (120.0, -300.0)
    mix = _ensure_shader_node(
        nodes,
        "ShaderNodeMixShader",
        "Killzone Preview Surface Mix",
    )
    mix.label = "Native source alpha / alpha test"
    mix.location = (340.0, -180.0)
    _replace_input_link(links, mix.inputs[0], alpha_socket)
    _replace_input_link(
        links,
        mix.inputs[1],
        transparent.outputs["BSDF"],
    )
    _replace_input_link(
        links,
        mix.inputs[2],
        emission.outputs["Emission"],
    )
    _replace_input_link(
        links,
        output.inputs["Surface"],
        mix.outputs["Shader"],
    )
    material.surface_render_method = render_method


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


def _connect_bounded_native_layerbumpdot3_preview(
    *,
    material: bpy.types.Material,
    layer_documents: list[Mapping[str, object]],
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    principled: bpy.types.Node,
) -> bool:
    if not material.get("killzone_bounded_layerbumpdot3_candidate"):
        return False
    if len(layer_documents) != 2:
        return False
    base_layer, detail_layer = layer_documents
    if not (
        base_layer.get("index") == 0
        and base_layer.get("operation") == 5
        and base_layer.get("pixel_format") in ("INDEX_4", "INDEX_8")
        and base_layer.get("coordinate_route_status")
        == "connected-native-uv0"
        and detail_layer.get("index") == 1
        and detail_layer.get("operation") == 18
        and detail_layer.get("pixel_format") == "ALPHA_8"
        and detail_layer.get("coordinate_route_status")
        == "connected-native-uv1"
    ):
        return False

    image_nodes: dict[int, bpy.types.Node] = {}
    for node in nodes:
        if (
            node.type == "TEX_IMAGE"
            and node.image is not None
            and node.get("killzone_layer_index") in (0, 1)
        ):
            image_nodes[int(node["killzone_layer_index"])] = node
    if image_nodes.keys() != {0, 1}:
        return False
    base_image = image_nodes[0]
    detail_image = image_nodes[1]
    if base_image.extension != "REPEAT" or detail_image.extension != "EXTEND":
        return False
    lighting_mode = int(base_layer.get("lighting_mode", -1))
    if lighting_mode not in (0, 1):
        return False

    base_color_socket = base_image.outputs["Color"]
    if lighting_mode == 1:
        base_color_socket, _ = _connect_v4_8_modulation(
            nodes=nodes,
            links=links,
            principled=principled,
            image_node=base_image,
            diagnostic=False,
        )

    alpha_byte = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        "Native Layer 1 Alpha Byte",
    )
    alpha_byte.operation = "MULTIPLY"
    alpha_byte.use_clamp = False
    alpha_byte.label = "At byte = texture alpha * 255"
    alpha_byte.location = (-250.0, -380.0)
    alpha_byte.inputs[1].default_value = 255.0
    _replace_input_link(
        links,
        alpha_byte.inputs[0],
        detail_image.outputs["Alpha"],
    )

    fragment_alpha = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        "Native Layer 1 Fragment Alpha",
    )
    fragment_alpha.operation = "ADD"
    fragment_alpha.use_clamp = False
    fragment_alpha.label = (
        "GS HIGHLIGHT: At + trunc(V4-8 alpha 127 / 2)"
    )
    fragment_alpha.location = (-30.0, -380.0)
    fragment_alpha.inputs[1].default_value = float(
        NATIVE_LAYERBUMPDOT3_FRAGMENT_ALPHA
    )
    _replace_input_link(
        links,
        fragment_alpha.inputs[0],
        alpha_byte.outputs["Value"],
    )

    highlight_clamp = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        "Native Layer 1 HIGHLIGHT Clamp",
    )
    highlight_clamp.operation = "MINIMUM"
    highlight_clamp.use_clamp = False
    highlight_clamp.label = "As = min(At + 63, 255)"
    highlight_clamp.location = (190.0, -380.0)
    highlight_clamp.inputs[1].default_value = 255.0
    _replace_input_link(
        links,
        highlight_clamp.inputs[0],
        fragment_alpha.outputs["Value"],
    )

    multiplier = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        "Native Layer 1 GS Multiplier",
    )
    multiplier.operation = "DIVIDE"
    multiplier.use_clamp = False
    multiplier.label = "Blend 0x89 factor: As / 128"
    multiplier.location = (410.0, -380.0)
    multiplier.inputs[1].default_value = float(NATIVE_GS_COLOR_ONE)
    _replace_input_link(
        links,
        multiplier.inputs[0],
        highlight_clamp.outputs["Value"],
    )

    multiplier_gamma = _ensure_shader_node(
        nodes,
        "ShaderNodeMath",
        "Native Layer 1 GS Multiplier Display Gamma",
    )
    multiplier_gamma.operation = "POWER"
    multiplier_gamma.use_clamp = False
    multiplier_gamma.label = "Display-space factor to linear"
    multiplier_gamma.location = (520.0, -380.0)
    multiplier_gamma.inputs[1].default_value = NATIVE_DISPLAY_GAMMA
    _replace_input_link(
        links,
        multiplier_gamma.inputs[0],
        multiplier.outputs["Value"],
    )

    framebuffer_scale = _ensure_shader_node(
        nodes,
        "ShaderNodeVectorMath",
        "Native LayerBumpDot3 Framebuffer Scale",
    )
    framebuffer_scale.operation = "SCALE"
    framebuffer_scale.label = "Layer 0 RGB * min(At + 63, 255) / 128"
    framebuffer_scale.location = (640.0, -80.0)
    _replace_input_link(
        links,
        framebuffer_scale.inputs[0],
        base_color_socket,
    )
    _replace_input_link(
        links,
        framebuffer_scale.inputs[3],
        multiplier_gamma.outputs["Value"],
    )
    composed_color = _connect_gs_color_clamp(
        nodes=nodes,
        links=links,
        color_socket=framebuffer_scale.outputs["Vector"],
        name="Native LayerBumpDot3 Framebuffer Clamp",
        location=(820.0, -80.0),
    )

    _replace_input_link(
        links,
        principled.inputs["Base Color"],
        composed_color,
    )
    _replace_input_link(
        links,
        principled.inputs["Emission Color"],
        composed_color,
    )
    for link in tuple(principled.inputs["Alpha"].links):
        links.remove(link)
    principled.inputs["Alpha"].default_value = 1.0
    principled.inputs["Emission Strength"].default_value = 0.35
    _connect_emission_surface(
        material=material,
        nodes=nodes,
        links=links,
        color_socket=composed_color,
        alpha_socket=None,
        render_method="DITHERED",
    )

    detail_image["killzone_preview_role"] = (
        "framebuffer-alpha-multiplier-not-normal-map"
    )
    material["killzone_layer_images_connected_to_shader"] = True
    material["killzone_preview_mode"] = (
        "two-layer-alphaadd-layerbumpdot3"
    )
    material["killzone_preview_layer_index"] = 0
    material["killzone_preview_layer_indices_json"] = "[0,1]"
    material["killzone_preview_detail_layer_index"] = 1
    material["killzone_preview_alpha_connected"] = False
    material["killzone_preview_ps2_color_one"] = NATIVE_GS_COLOR_ONE
    material["killzone_preview_fragment_alpha_byte"] = (
        NATIVE_LAYERBUMPDOT3_FRAGMENT_ALPHA
    )
    material["killzone_preview_v4_8_alpha_byte"] = (
        NATIVE_LAYERBUMPDOT3_V4_8_ALPHA
    )
    material["killzone_preview_fragment_alpha_source"] = (
        "uniform-native-v4-8-alpha-div-2"
    )
    material["killzone_preview_base_v4_8_rgb_modulation"] = (
        lighting_mode == 1
    )
    material["killzone_preview_detail_tfx"] = "HIGHLIGHT"
    material["killzone_preview_gs_alpha_policy"] = "0x89"
    material["killzone_preview_native_equation"] = (
        "min(layer0.rgb * min(layer1.alpha_byte + 63, 255) / 128, 255)"
    )
    material["killzone_preview_modulation_space"] = "display-gamma"
    material["killzone_preview_display_gamma"] = NATIVE_DISPLAY_GAMMA
    material["killzone_preview_emission_strength"] = 1.0
    material["killzone_shader_profile"] = "bounded-native-preview"
    unimplemented_state = [
        "framebuffer-history-before-layer0",
        "gs-byte-quantization",
    ]
    if lighting_mode == 1:
        unimplemented_state.append(
            "complete-native-vu-lighting-before-layer0"
        )
    material["killzone_shader_unimplemented_state_json"] = json.dumps(
        unimplemented_state,
        separators=(",", ":"),
    )
    material["killzone_shader_status"] = (
        "Resolved bounded direct-UV0/UV1 ALPHAADD + LAYERBUMPDOT3 "
        "composition: uniform V4-8 A=127 produces fragment alpha 63; "
        "layer 1 uses GS HIGHLIGHT min(At + 63, 255) "
        "and blend 0x89 destination scaling by As/128; framebuffer history "
        "before layer 0 and byte quantization remain host limitations; "
        "modulation is evaluated in display-gamma space and saturated"
    )
    return True


def _connect_diagnostic_native_maskedwrite_alphaadd_preview(
    *,
    material: bpy.types.Material,
    layer_documents: list[Mapping[str, object]],
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    principled: bpy.types.Node,
) -> bool:
    if not material.get(
        "killzone_maskedwrite_alphaadd_cutout_candidate"
    ):
        return False
    if len(layer_documents) != 2:
        return False
    masked_write, alpha_add = layer_documents
    if not (
        masked_write.get("index") == 0
        and masked_write.get("kind") == "texture"
        and masked_write.get("operation") == 8
        and masked_write.get("z_write") == 1
        and masked_write.get("alpha_test") == 4
        and masked_write.get("alpha_reference")
        == NATIVE_MASKEDWRITE_DECODED_ALPHA_CUTOFF
        and masked_write.get("pixel_format")
        in ("INDEX_4", "INDEX_8", "RGBA_8888")
        and masked_write.get("coordinate_route_status")
        == "connected-native-uv0"
        and alpha_add.get("index") == 1
        and alpha_add.get("kind") == "texture"
        and alpha_add.get("operation") == 5
        and alpha_add.get("z_write") == 0
        and alpha_add.get("resource_token")
        == masked_write.get("resource_token")
        and alpha_add.get("effective_image_bank")
        == masked_write.get("effective_image_bank")
        and alpha_add.get("coordinate_route_status")
        == "connected-native-uv0"
    ):
        return False

    image_nodes: dict[int, bpy.types.Node] = {}
    for node in nodes:
        if (
            node.type == "TEX_IMAGE"
            and node.image is not None
            and node.get("killzone_layer_index") in (0, 1)
        ):
            image_nodes[int(node["killzone_layer_index"])] = node
    if image_nodes.keys() != {0, 1}:
        return False
    masked_write_image = image_nodes[0]
    alpha_add_image = image_nodes[1]
    if masked_write_image.image != alpha_add_image.image:
        return False

    cutout_mask = _decoded_alpha_greater_mask(
        nodes=nodes,
        links=links,
        alpha_socket=masked_write_image.outputs["Alpha"],
        decoded_reference=NATIVE_MASKEDWRITE_DECODED_ALPHA_CUTOFF,
    )
    _replace_input_link(
        links,
        principled.inputs["Base Color"],
        masked_write_image.outputs["Color"],
    )
    _replace_input_link(
        links,
        principled.inputs["Emission Color"],
        masked_write_image.outputs["Color"],
    )
    _replace_input_link(
        links,
        principled.inputs["Alpha"],
        cutout_mask,
    )
    principled.inputs["Emission Strength"].default_value = 0.35
    _connect_emission_surface(
        material=material,
        nodes=nodes,
        links=links,
        color_socket=masked_write_image.outputs["Color"],
        alpha_socket=cutout_mask,
        render_method="DITHERED",
    )

    masked_write_image["killzone_preview_role"] = (
        "decoded-color-and-layer0-cutout-alpha"
    )
    alpha_add_image["killzone_preview_role"] = (
        "preserved-layer1-low-alpha-fringe-not-composed"
    )
    material["killzone_layer_images_connected_to_shader"] = True
    material["killzone_preview_mode"] = (
        "two-layer-maskedwrite-alphaadd-cutout"
    )
    material["killzone_preview_layer_index"] = 0
    material["killzone_preview_layer_indices_json"] = "[0,1]"
    material["killzone_preview_alpha_connected"] = True
    material["killzone_preview_alpha_test"] = 4
    material["killzone_preview_alpha_reference_byte"] = (
        native_alpha_reference_byte(
            NATIVE_MASKEDWRITE_DECODED_ALPHA_CUTOFF
        )
    )
    material["killzone_preview_decoded_alpha_cutoff_byte"] = (
        NATIVE_MASKEDWRITE_DECODED_ALPHA_CUTOFF
    )
    material["killzone_preview_decoded_alpha_comparison"] = "GREATER"
    material["killzone_preview_gs_afail"] = "KEEP"
    material["killzone_preview_second_pass_composed"] = False
    material["killzone_preview_emission_strength"] = 1.0
    material["killzone_shader_profile"] = "artist-approximation"
    material["killzone_shader_unimplemented_state_json"] = json.dumps(
        [
            "layer0-maskedwrite-additive-rgb-contribution",
            "layer1-alphaadd-low-alpha-fringe",
            "per-pass-depth-and-framebuffer-state",
            "framebuffer-alpha-preservation",
            "per-pass-tfx-vu-and-alpha-source",
            "gs-intermediate-clamp",
        ],
        separators=(",", ":"),
    )
    material["killzone_shader_status"] = (
        "Diagnostic same-texture UV0 MASKEDWRITE + ALPHAADD foliage "
        "cutout using strict decoded alpha > 204; AFAIL KEEP coverage is "
        "represented, but the first-pass additive RGB/depth effects and "
        "the second-pass low-alpha fringe are not composed"
    )
    return True


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


def _connect_gs_color_clamp(
    *,
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    color_socket: bpy.types.NodeSocket,
    name: str,
    location: tuple[float, float],
) -> bpy.types.NodeSocket:
    """Saturate a color at GS byte 255, i.e. 1.0 in display space."""

    clamp = _ensure_shader_node(nodes, "ShaderNodeVectorMath", name)
    clamp.operation = "MINIMUM"
    clamp.label = "GS color saturation (byte 255)"
    clamp.location = location
    clamp.inputs[1].default_value = (1.0, 1.0, 1.0)
    _replace_input_link(links, clamp.inputs[0], color_socket)
    return clamp.outputs["Vector"]


def _connect_v4_8_modulation(
    *,
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    principled: bpy.types.Node,
    image_node: bpy.types.Node,
    diagnostic: bool,
) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket]:
    """Modulate layer-0 texture color/alpha by Native V4-8.

    The GS computes ``tex * vertex / 128`` on display-encoded bytes. The
    image texture is already linearised, so the vertex factor is raised to
    the display gamma before multiplying; the result is then saturated as
    the GS does. Alpha is not gamma encoded and stays linear.
    """

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
    vertex_color.location = (-520.0, -180.0)

    vertex_scale = _ensure_shader_node(
        nodes,
        "ShaderNodeVectorMath",
        "Native Layer 0 V4-8 RGB Scale",
    )
    vertex_scale.operation = "SCALE"
    vertex_scale.label = "V4-8 RGB * 255 / 128"
    vertex_scale.location = (-300.0, 60.0)
    vertex_scale.inputs[3].default_value = 255.0 / NATIVE_GS_COLOR_ONE
    _replace_input_link(
        links,
        vertex_scale.inputs[0],
        vertex_color.outputs["Color"],
    )

    vertex_gamma = _ensure_shader_node(
        nodes,
        "ShaderNodeGamma",
        "Native Layer 0 V4-8 Display Gamma",
    )
    vertex_gamma.label = "Display-space factor to linear"
    vertex_gamma.location = (-120.0, 60.0)
    vertex_gamma.inputs["Gamma"].default_value = NATIVE_DISPLAY_GAMMA
    _replace_input_link(
        links,
        vertex_gamma.inputs["Color"],
        vertex_scale.outputs["Vector"],
    )

    rgb_multiply = _ensure_shader_node(
        nodes,
        "ShaderNodeVectorMath",
        "Native Layer 0 RGB Modulate",
    )
    rgb_multiply.operation = "MULTIPLY"
    rgb_multiply.location = (60.0, 20.0)
    _replace_input_link(
        links,
        rgb_multiply.inputs[0],
        image_node.outputs["Color"],
    )
    _replace_input_link(
        links,
        rgb_multiply.inputs[1],
        vertex_gamma.outputs["Color"],
    )
    color_socket = _connect_gs_color_clamp(
        nodes=nodes,
        links=links,
        color_socket=rgb_multiply.outputs["Vector"],
        name="Native Layer 0 RGB Clamp",
        location=(240.0, 20.0),
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
    alpha_scale.use_clamp = False
    alpha_scale.location = (180.0, -180.0)
    alpha_scale.inputs[1].default_value = 255.0 / NATIVE_GS_COLOR_ONE
    _replace_input_link(
        links,
        alpha_scale.inputs[0],
        alpha_multiply.outputs["Value"],
    )

    _replace_input_link(
        links,
        principled.inputs["Base Color"],
        color_socket,
    )
    _replace_input_link(
        links,
        principled.inputs["Emission Color"],
        color_socket,
    )
    _replace_input_link(
        links,
        principled.inputs["Alpha"],
        alpha_scale.outputs["Value"],
    )
    return color_socket, alpha_scale.outputs["Value"]


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
        color_socket, source_alpha_socket = _connect_v4_8_modulation(
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
        color_socket = image_node.outputs["Color"]
        source_alpha_socket = image_node.outputs["Alpha"]
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
    alpha_test_mask = _native_alpha_test_mask(
        nodes=nodes,
        links=links,
        alpha_socket=source_alpha_socket,
        layer=layer,
    )
    opacity_socket: bpy.types.NodeSocket | None
    render_method = "DITHERED"
    if uses_alpha:
        opacity_socket = source_alpha_socket
        if alpha_test_mask is not None:
            opacity_socket = _multiply_alpha_sockets(
                nodes=nodes,
                links=links,
                left=source_alpha_socket,
                right=alpha_test_mask,
                name=(
                    f"Native Layer {layer_index} Preview Alpha Test"
                ),
            )
        # Eevee BLENDED surfaces neither write depth nor sort per fragment,
        # so alpha-tested cutouts (e.g. LIGHTMAPCOLOR foliage) use DITHERED.
        render_method = (
            "DITHERED"
            if operation == 8 or alpha_test_mask is not None
            else "BLENDED"
        )
    elif alpha_test_mask is not None:
        opacity_socket = alpha_test_mask
    else:
        opacity_socket = None

    if opacity_socket is not None:
        _replace_input_link(
            links,
            principled.inputs["Alpha"],
            opacity_socket,
        )
    else:
        for link in tuple(principled.inputs["Alpha"].links):
            links.remove(link)
        principled.inputs["Alpha"].default_value = 1.0

    _connect_emission_surface(
        material=material,
        nodes=nodes,
        links=links,
        color_socket=color_socket,
        alpha_socket=opacity_socket,
        render_method=render_method,
    )

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
    material["killzone_preview_alpha_test"] = int(
        layer.get("alpha_test", 7)
    )
    material["killzone_preview_alpha_reference_byte"] = (
        native_alpha_reference_byte(
            int(layer.get("alpha_reference", 0))
        )
    )
    material["killzone_preview_transform_ignored"] = transform_ignored
    material["killzone_preview_v4_8_modulation"] = use_v4_8
    if use_v4_8:
        material["killzone_preview_modulation_space"] = "display-gamma"
        material["killzone_preview_display_gamma"] = NATIVE_DISPLAY_GAMMA
    material["killzone_preview_emission_strength"] = 1.0
    material["killzone_shader_profile"] = "artist-approximation"
    material["killzone_shader_status"] = (
        (
            f"Unlit diagnostic decoded color from layer {layer_index} "
            f"{operation_name}, modulated by serialized Native V4-8; "
            "native depth/blend state and lighting remain unreconstructed"
        )
        if use_v4_8
        else (
            f"Unlit diagnostic decoded color from layer {layer_index} "
            f"{operation_name}; native lighting and multi-layer composition "
            "remain unreconstructed"
        )
    )
    return True


def _connect_artist_write_layerbumpdot3(
    *,
    material: bpy.types.Material,
    layer_documents: list[Mapping[str, object]],
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    base_color_socket: bpy.types.NodeSocket,
    meshes: set[bpy.types.Mesh],
) -> bpy.types.NodeSocket | None:
    """Compose a LAYERBUMPDOT3 scalar layer over a WRITE base.

    Uses the GS HIGHLIGHT/0x89 equation proved for the HQ Grounds family,
    ``Cd * min(At + Af, 255) / 128``, with fragment alpha ``Af`` taken from
    Native V4-8 alpha / 2 when every user mesh carries the color stream and
    the unlit-route constant 64 otherwise. Applying it to WRITE bases is an
    artist approximation: it restores the baked shading these layers carry
    but has not been compared against a GS capture for this family.
    """

    detail = next(
        (
            layer
            for layer in layer_documents[1:]
            if layer.get("kind") == "texture"
            and layer.get("operation") == 18
            and layer.get("pixel_format") in ("ALPHA_4", "ALPHA_8")
            and layer.get("effective_image_bank") == "normal"
            and str(layer.get("coordinate_route_status", "")).startswith(
                "connected-native-uv"
            )
        ),
        None,
    )
    if detail is None:
        return None
    detail_index = int(detail["index"])
    detail_node = next(
        (
            node
            for node in nodes
            if node.type == "TEX_IMAGE"
            and node.get("killzone_layer_index") == detail_index
            and node.image is not None
        ),
        None,
    )
    if detail_node is None:
        return None

    prefix = f"Native Artist Layer {detail_index}"
    x = 260.0
    y = -300.0 * detail_index
    alpha_byte = _ensure_shader_node(
        nodes, "ShaderNodeMath", f"{prefix} Alpha Byte"
    )
    alpha_byte.operation = "MULTIPLY"
    alpha_byte.label = "At byte = texture alpha * 255"
    alpha_byte.location = (x, y)
    alpha_byte.inputs[1].default_value = 255.0
    _replace_input_link(
        links, alpha_byte.inputs[0], detail_node.outputs["Alpha"]
    )

    uses_v4_8 = bool(meshes) and all(
        _mesh_material_has_native_v4_8(mesh, material) for mesh in meshes
    )
    fragment = _ensure_shader_node(
        nodes, "ShaderNodeMath", f"{prefix} Fragment Alpha"
    )
    fragment.operation = "ADD"
    fragment.location = (x + 440.0, y)
    _replace_input_link(links, fragment.inputs[0], alpha_byte.outputs[0])
    if uses_v4_8:
        vertex_color = _ensure_shader_node(
            nodes, "ShaderNodeVertexColor", f"{prefix} V4-8"
        )
        vertex_color.layer_name = "Native V4-8"
        vertex_color.location = (x - 200.0, y - 160.0)
        vertex_byte = _ensure_shader_node(
            nodes, "ShaderNodeMath", f"{prefix} V4-8 Alpha Byte"
        )
        vertex_byte.operation = "MULTIPLY"
        vertex_byte.inputs[1].default_value = 255.0
        vertex_byte.location = (x, y - 160.0)
        _replace_input_link(
            links, vertex_byte.inputs[0], vertex_color.outputs["Alpha"]
        )
        vertex_round = _ensure_shader_node(
            nodes, "ShaderNodeMath", f"{prefix} V4-8 Alpha Round"
        )
        vertex_round.operation = "ROUND"
        vertex_round.location = (x + 110.0, y - 160.0)
        _replace_input_link(
            links, vertex_round.inputs[0], vertex_byte.outputs[0]
        )
        vertex_half = _ensure_shader_node(
            nodes, "ShaderNodeMath", f"{prefix} V4-8 Alpha Half"
        )
        vertex_half.operation = "DIVIDE"
        vertex_half.inputs[1].default_value = 2.0
        vertex_half.location = (x + 220.0, y - 160.0)
        _replace_input_link(
            links, vertex_half.inputs[0], vertex_round.outputs[0]
        )
        vertex_floor = _ensure_shader_node(
            nodes, "ShaderNodeMath", f"{prefix} V4-8 Alpha Truncate"
        )
        vertex_floor.operation = "FLOOR"
        vertex_floor.location = (x + 330.0, y - 160.0)
        _replace_input_link(
            links, vertex_floor.inputs[0], vertex_half.outputs[0]
        )
        _replace_input_link(
            links, fragment.inputs[1], vertex_floor.outputs[0]
        )
        fragment.label = "At + trunc(V4-8 alpha / 2)"
    else:
        for link in tuple(fragment.inputs[1].links):
            links.remove(link)
        fragment.inputs[1].default_value = float(NATIVE_UNLIT_FRAGMENT_ALPHA)
        fragment.label = f"At + {NATIVE_UNLIT_FRAGMENT_ALPHA} (unlit route)"

    highlight = _ensure_shader_node(
        nodes, "ShaderNodeMath", f"{prefix} HIGHLIGHT Clamp"
    )
    highlight.operation = "MINIMUM"
    highlight.inputs[1].default_value = 255.0
    highlight.location = (x + 550.0, y)
    _replace_input_link(links, highlight.inputs[0], fragment.outputs[0])

    multiplier = _ensure_shader_node(
        nodes, "ShaderNodeMath", f"{prefix} GS Multiplier"
    )
    multiplier.operation = "DIVIDE"
    multiplier.inputs[1].default_value = float(NATIVE_GS_COLOR_ONE)
    multiplier.location = (x + 660.0, y)
    _replace_input_link(links, multiplier.inputs[0], highlight.outputs[0])

    multiplier_gamma = _ensure_shader_node(
        nodes, "ShaderNodeMath", f"{prefix} GS Multiplier Display Gamma"
    )
    multiplier_gamma.operation = "POWER"
    multiplier_gamma.inputs[1].default_value = NATIVE_DISPLAY_GAMMA
    multiplier_gamma.location = (x + 770.0, y)
    _replace_input_link(
        links, multiplier_gamma.inputs[0], multiplier.outputs[0]
    )

    scale = _ensure_shader_node(
        nodes, "ShaderNodeVectorMath", f"{prefix} Framebuffer Scale"
    )
    scale.operation = "SCALE"
    scale.label = "Layer 0 RGB * min(At + Af, 255) / 128"
    scale.location = (x + 880.0, y + 120.0)
    _replace_input_link(links, scale.inputs[0], base_color_socket)
    _replace_input_link(links, scale.inputs[3], multiplier_gamma.outputs[0])

    detail_node["killzone_preview_role"] = (
        "artist-framebuffer-alpha-multiplier-not-normal-map"
    )
    material["killzone_preview_detail_layer_index"] = detail_index
    material["killzone_preview_fragment_alpha_source"] = (
        "v4-8-alpha-div-2" if uses_v4_8 else "constant-64-unlit-route"
    )
    return _connect_gs_color_clamp(
        nodes=nodes,
        links=links,
        color_socket=scale.outputs["Vector"],
        name=f"{prefix} Framebuffer Clamp",
        location=(x + 1060.0, y + 120.0),
    )


def _mark_artist_write_layerbumpdot3(material: bpy.types.Material) -> None:
    detail_index = int(material["killzone_preview_detail_layer_index"])
    material["killzone_preview_mode"] = "layer0-write-plus-layerbumpdot3"
    material["killzone_preview_layer_indices_json"] = json.dumps(
        [0, detail_index],
        separators=(",", ":"),
    )
    material["killzone_preview_artist_lightmap"] = True
    material["killzone_preview_modulation_space"] = "display-gamma"
    material["killzone_preview_display_gamma"] = NATIVE_DISPLAY_GAMMA
    material["killzone_shader_profile"] = "artist-approximation"
    material["killzone_shader_status"] = (
        "Unlit WRITE preview with the LAYERBUMPDOT3 scalar layer composed "
        "as GS HIGHLIGHT/0x89 framebuffer scaling in display-gamma space; "
        "artist approximation not yet compared with a GS capture"
    )


def _connect_artist_fallback_preview(
    *,
    material: bpy.types.Material,
    layer_documents: list[Mapping[str, object]],
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    principled: bpy.types.Node,
) -> bool:
    """Show the first decoded visible-color layer of an unresolved material.

    This deliberately ignores the native composition and lighting; it only
    replaces the diagnostic object color with recognizable texture detail.
    """

    documents_by_index = {
        int(layer.get("index", -1)): layer for layer in layer_documents
    }
    candidates = sorted(
        (
            node
            for node in nodes
            if node.type == "TEX_IMAGE"
            and node.image is not None
            and isinstance(node.get("killzone_layer_index"), int)
            and node.get("killzone_image_intent") == "visible-color"
            and str(
                node.get("killzone_coordinate_route_status", "")
            ).startswith("connected-native-uv")
        ),
        key=lambda node: int(node["killzone_layer_index"]),
    )
    if not candidates:
        return False
    image_node = candidates[0]
    layer_index = int(image_node["killzone_layer_index"])
    layer = documents_by_index.get(layer_index, {})

    _replace_input_link(
        links, principled.inputs["Base Color"], image_node.outputs["Color"]
    )
    _replace_input_link(
        links,
        principled.inputs["Emission Color"],
        image_node.outputs["Color"],
    )
    alpha_test_mask = _native_alpha_test_mask(
        nodes=nodes,
        links=links,
        alpha_socket=image_node.outputs["Alpha"],
        layer=layer,
    )
    if alpha_test_mask is not None:
        _replace_input_link(
            links, principled.inputs["Alpha"], alpha_test_mask
        )
    _connect_emission_surface(
        material=material,
        nodes=nodes,
        links=links,
        color_socket=image_node.outputs["Color"],
        alpha_socket=alpha_test_mask,
        render_method="DITHERED",
    )
    material["killzone_layer_images_connected_to_shader"] = True
    material["killzone_preview_mode"] = f"artist-fallback-layer{layer_index}"
    material["killzone_preview_layer_index"] = layer_index
    material["killzone_preview_emission_strength"] = 1.0
    material["killzone_shader_profile"] = "artist-fallback"
    material["killzone_shader_status"] = (
        f"Artist fallback: decoded color of layer {layer_index} "
        f"{layer.get('operation_name', '')} shown unlit; native "
        "multi-layer composition, generated coordinates, and lighting are "
        "not reconstructed"
    )
    return True


def enable_bounded_native_material_previews(
    materials: Iterable[bpy.types.Material],
    objects: Iterable[bpy.types.Object],
    *,
    include_write: bool,
    include_color_diagnostics: bool = False,
    artist_lightmap_layers: bool = False,
    artist_fallback: bool = False,
) -> dict[str, int]:
    """Connect the admitted preview graphs and return per-family counts.

    ``artist_lightmap_layers`` composes a LAYERBUMPDOT3 scalar layer over
    admitted WRITE bases, and ``artist_fallback`` shows a decoded color
    layer for otherwise unresolved materials. Both are labeled artist
    approximations rather than native reconstructions.
    """

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

        raw_layers = material.get("killzone_layers_json")
        if not isinstance(raw_layers, str):
            continue
        layer_documents = json.loads(raw_layers)

        meshes = user_meshes[material]
        if (
            meshes
            and all(
                _mesh_material_has_native_v4_8(mesh, material)
                and _mesh_material_native_v4_8_alpha_range(
                    mesh,
                    material,
                )
                == (
                    NATIVE_LAYERBUMPDOT3_V4_8_ALPHA,
                    NATIVE_LAYERBUMPDOT3_V4_8_ALPHA,
                )
                for mesh in meshes
            )
            and _connect_bounded_native_layerbumpdot3_preview(
                material=material,
                layer_documents=layer_documents,
                nodes=nodes,
                links=links,
                principled=principled,
            )
        ):
            counts["layerbumpdot3"] = (
                counts.get("layerbumpdot3", 0) + 1
            )
            continue

        if _connect_diagnostic_native_maskedwrite_alphaadd_preview(
            material=material,
            layer_documents=layer_documents,
            nodes=nodes,
            links=links,
            principled=principled,
        ):
            counts["maskedwrite"] = counts.get("maskedwrite", 0) + 1
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
            if write_node is not None and layer_documents:
                layer = layer_documents[0]
                coordinate_route = str(
                    write_node.get(
                        "killzone_coordinate_route_status",
                        "",
                    )
                )
                bounded_axis_transform = (
                    "-bounded-axis-transform6f" in coordinate_route
                )
                write_color = write_node.outputs["Color"]
                lightmap_color = (
                    _connect_artist_write_layerbumpdot3(
                        material=material,
                        layer_documents=layer_documents,
                        nodes=nodes,
                        links=links,
                        base_color_socket=write_color,
                        meshes=meshes,
                    )
                    if artist_lightmap_layers
                    else None
                )
                if lightmap_color is not None:
                    write_color = lightmap_color
                _replace_input_link(
                    links,
                    principled.inputs["Base Color"],
                    write_color,
                )
                _replace_input_link(
                    links,
                    principled.inputs["Emission Color"],
                    write_color,
                )
                principled.inputs[
                    "Emission Strength"
                ].default_value = 0.35
                alpha_test_mask = _native_alpha_test_mask(
                    nodes=nodes,
                    links=links,
                    alpha_socket=write_node.outputs["Alpha"],
                    layer=layer,
                )
                if alpha_test_mask is not None:
                    _replace_input_link(
                        links,
                        principled.inputs["Alpha"],
                        alpha_test_mask,
                    )
                _connect_emission_surface(
                    material=material,
                    nodes=nodes,
                    links=links,
                    color_socket=write_color,
                    alpha_socket=alpha_test_mask,
                    render_method="DITHERED",
                )
                material[
                    "killzone_preview_base_write_connected"
                ] = True
                material["killzone_preview_emission_strength"] = 1.0
                material[
                    "killzone_layer_images_connected_to_shader"
                ] = True
                material["killzone_preview_mode"] = "layer0-write"
                material["killzone_preview_layer_index"] = 0
                material["killzone_preview_coordinate_route"] = (
                    coordinate_route
                )
                material["killzone_preview_transform_6f_applied"] = (
                    bounded_axis_transform
                )
                material["killzone_preview_alpha_test"] = int(
                    layer.get("alpha_test", 7)
                )
                material["killzone_preview_alpha_reference_byte"] = (
                    native_alpha_reference_byte(
                        int(layer.get("alpha_reference", 0))
                    )
                )
                material["killzone_shader_profile"] = (
                    "bounded-native-preview"
                )
                material["killzone_shader_status"] = (
                    "Unlit "
                    + (
                        "axis-transformed direct-UV "
                        if bounded_axis_transform
                        else "direct-UV "
                    )
                    + "WRITE preview with native alpha-test "
                    "semantics; native VU lighting and framebuffer state "
                    "remain unreconstructed"
                )
                if lightmap_color is not None:
                    _mark_artist_write_layerbumpdot3(material)
                    counts["write_layerbumpdot3"] = (
                        counts.get("write_layerbumpdot3", 0) + 1
                    )
                else:
                    counts["write"] += 1
                continue

        if include_color_diagnostics:
            has_v4_8 = bool(meshes) and all(
                _mesh_material_has_native_v4_8(mesh, material)
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
        if _connect_bounded_native_alphaadd_preview(
            material=material,
            layer_documents=layer_documents,
            nodes=nodes,
            links=links,
            principled=principled,
            meshes=meshes,
        ):
            counts["alphaadd"] += 1
            continue

        if artist_fallback and _connect_artist_fallback_preview(
            material=material,
            layer_documents=layer_documents,
            nodes=nodes,
            links=links,
            principled=principled,
        ):
            counts["fallback"] = counts.get("fallback", 0) + 1

    return counts


def _connect_bounded_native_alphaadd_preview(
    *,
    material: bpy.types.Material,
    layer_documents: list[Mapping[str, object]],
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    principled: bpy.types.Node,
    meshes: set[bpy.types.Mesh],
) -> bool:
    if len(layer_documents) != 1:
        return False
    layer = layer_documents[0]
    if not supports_bounded_native_alphaadd_preview(layer):
        return False
    if not meshes or any(
        not _mesh_material_has_native_v4_8(mesh, material)
        for mesh in meshes
    ):
        return False
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
        return False

    color_socket, source_alpha_socket = _connect_v4_8_modulation(
        nodes=nodes,
        links=links,
        principled=principled,
        image_node=image_node,
        diagnostic=False,
    )
    alpha_test_mask = _native_alpha_test_mask(
        nodes=nodes,
        links=links,
        alpha_socket=source_alpha_socket,
        layer=layer,
    )
    opacity_socket = source_alpha_socket
    if alpha_test_mask is not None:
        opacity_socket = _multiply_alpha_sockets(
            nodes=nodes,
            links=links,
            left=source_alpha_socket,
            right=alpha_test_mask,
            name="Native Layer 0 Preview Alpha Test",
        )
    _connect_emission_surface(
        material=material,
        nodes=nodes,
        links=links,
        color_socket=color_socket,
        alpha_socket=opacity_socket,
        render_method="BLENDED",
    )
    principled.inputs["Emission Strength"].default_value = 0.35
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
    material["killzone_preview_modulation_space"] = "display-gamma"
    material["killzone_preview_display_gamma"] = NATIVE_DISPLAY_GAMMA
    material["killzone_preview_emission_strength"] = 1.0
    material["killzone_preview_alpha_test"] = int(
        layer.get("alpha_test", 7)
    )
    material["killzone_preview_alpha_reference_byte"] = (
        native_alpha_reference_byte(
            int(layer.get("alpha_reference", 0))
        )
    )
    material["killzone_shader_profile"] = "bounded-native-preview"
    material["killzone_shader_status"] = (
        f"Unlit bounded direct-UV {pixel_format} ALPHAADD preview with "
        "Native V4-8 color/alpha modulation; full runtime lighting "
        "remains unreconstructed"
    )
    return True


def _mesh_material_has_native_v4_8(
    mesh: bpy.types.Mesh,
    material: bpy.types.Material,
) -> bool:
    token = material.get("killzone_core_local_token")
    encoded_presence = mesh.get(
        "killzone_material_v4_8_presence_json"
    )
    if isinstance(token, str) and isinstance(encoded_presence, str):
        presence = json.loads(encoded_presence)
        if token in presence:
            value = presence[token]
            if type(value) is not bool:
                raise ValueError(
                    "Per-material Native V4-8 presence must be Boolean"
                )
            return value
    return mesh.color_attributes.get("Native V4-8") is not None


def _mesh_material_native_v4_8_alpha_range(
    mesh: bpy.types.Mesh,
    material: bpy.types.Material,
) -> tuple[int, int] | None:
    token = material.get("killzone_core_local_token")
    encoded_ranges = mesh.get(
        "killzone_material_v4_8_alpha_range_json"
    )
    if not isinstance(token, str) or not isinstance(encoded_ranges, str):
        return None
    ranges = json.loads(encoded_ranges)
    raw_range = ranges.get(token)
    if not (
        isinstance(raw_range, list)
        and len(raw_range) == 2
        and all(type(value) is int for value in raw_range)
        and 0 <= raw_range[0] <= raw_range[1] <= 0xFF
    ):
        if raw_range is None:
            return None
        raise ValueError(
            "Per-material Native V4-8 alpha range must contain two bytes"
        )
    return raw_range[0], raw_range[1]


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
    existing_images = _existing_level_images()
    for token in sorted(set(selected_texture_tokens)):
        texture = textures_by_token[token]
        if texture.base_rgba is not None:
            images_by_token[(token, "normal")] = _create_level_image(
                texture,
                texture.base_rgba,
                palette_bank="normal",
                level_path=asset_path,
                source_path=source_path,
                existing_images=existing_images,
            )
        if texture.base_rgba_complement is not None:
            images_by_token[(token, "complement")] = _create_level_image(
                texture,
                texture.base_rgba_complement,
                palette_bank="complement",
                level_path=asset_path,
                source_path=source_path,
                existing_images=existing_images,
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
    # Material nodes keep their images alive. Only images no node samples,
    # such as later AnimatedTexture frames, need a fake user to be saved.
    for image in images_by_token.values():
        image.use_fake_user = image.users == 0
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
    material_v4_8_presence: dict[int, bool] = {}
    material_v4_8_alpha_ranges: dict[int, tuple[int, int]] = {}

    for packet in resource.packets:
        base = len(positions)
        for vertex in packet.vertices:
            positions.append(vertex.position)
            normals.append(vertex.normal)
            colors.append(vertex.color)
            coordinates.append(vertex.coordinates)
            adc_values.append(vertex.adc)
        material_id = resource.records[packet.mesh_record_index].material_id
        packet_colors = tuple(
            vertex.color
            for vertex in packet.vertices
            if vertex.color is not None
        )
        packet_has_v4_8 = len(packet_colors) == len(packet.vertices)
        if material_id in material_v4_8_presence:
            material_v4_8_presence[material_id] = (
                material_v4_8_presence[material_id]
                and packet_has_v4_8
            )
        else:
            material_v4_8_presence[material_id] = packet_has_v4_8
        if packet_colors:
            packet_alpha_min = min(color[3] for color in packet_colors)
            packet_alpha_max = max(color[3] for color in packet_colors)
            previous_range = material_v4_8_alpha_ranges.get(material_id)
            material_v4_8_alpha_ranges[material_id] = (
                (
                    packet_alpha_min,
                    packet_alpha_max,
                )
                if previous_range is None
                else (
                    min(previous_range[0], packet_alpha_min),
                    max(previous_range[1], packet_alpha_max),
                )
            )
        triangles.extend(
            (base + first, base + second, base + third)
            for first, second, third in packet.triangles
        )
        material_ids.extend((material_id,) * len(packet.triangles))

    mesh, winding_corrections, uv_count = build_native_mesh(
        f"Static 0x{node.object_id:08X} "
        f"[{node.key.section_offset:06X}:{node.key.object_index}]",
        positions=positions,
        normals=normals,
        triangles=triangles,
        material_ids=material_ids,
        materials_by_token=materials_by_token,
        fallback_material=fallback_material,
        coordinates=coordinates,
        colors=colors,
    )
    set_attribute_values(mesh, "killzone_adc", "BOOLEAN", "POINT", adc_values)
    mesh["killzone_material_v4_8_presence_json"] = json.dumps(
        {
            f"0x{material_id:08X}": present
            for material_id, present in sorted(
                material_v4_8_presence.items()
            )
        },
        separators=(",", ":"),
    )
    mesh["killzone_material_v4_8_alpha_range_json"] = json.dumps(
        {
            f"0x{material_id:08X}": list(alpha_range)
            for material_id, alpha_range in sorted(
                material_v4_8_alpha_ranges.items()
            )
        },
        separators=(",", ":"),
    )

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
    material_options: NativeMaterialPreviewOptions | None = None,
    progress: Callable[[float], None] | None = None,
    timings: dict[str, float] | None = None,
) -> NativeLevelImportResult:
    material_options = material_options or NativeMaterialPreviewOptions()
    timings = {} if timings is None else timings
    phase_start = time.perf_counter()

    def report(fraction: float) -> None:
        if progress is not None:
            progress(fraction)

    report(0.3)
    scene_collection = context.scene.collection
    display_name = Path(level_path).name.removesuffix(".core")
    root_collection = bpy.data.collections.new(
        f"Killzone {display_name}"
    )
    direct_collection = bpy.data.collections.new("Direct World (identity)")
    props_collection = bpy.data.collections.new("Props")
    instance_collection = bpy.data.collections.new("Static Instances")
    collision_instance_collection = bpy.data.collections.new(
        "Collision Mesh Instances"
    )
    animated_instance_collection = bpy.data.collections.new(
        "Animated Skinned Instances (metadata only)"
    )
    scene_collection.children.link(root_collection)
    root_collection.children.link(direct_collection)
    root_collection.children.link(props_collection)
    props_collection.children.link(instance_collection)
    props_collection.children.link(collision_instance_collection)
    props_collection.children.link(animated_instance_collection)
    set_native_import_provenance(
        root_collection,
        product="native-map-placement",
        operator="kz.import_map",
        level_placement_context_loaded=True,
    )

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
    root_collection["killzone_serialized_static_instance_count"] = len(
        level.instances
    )
    root_collection[
        "killzone_selected_static_instance_leaf_occurrence_count"
    ] = sum(
        occurrence.path == "instance"
        for occurrence in level.render_plan.instance_occurrences
    )
    root_collection["killzone_distinct_static_instance_matrix_count"] = len(
        {
            struct.pack("<16f", *instance.matrix)
            for instance in level.instances
        }
    )
    root_collection["killzone_origin_static_instance_count"] = sum(
        instance.matrix[12:15] == (0.0, 0.0, 0.0)
        for instance in level.instances
    )
    root_collection["killzone_collision_mesh_prop_count"] = len(
        level.collision_instances
    )
    root_collection["killzone_serialized_collision_mesh_instance_count"] = (
        len(level.collision_instances)
    )
    root_collection[
        "killzone_selected_collision_mesh_leaf_occurrence_count"
    ] = sum(
        occurrence.path == "collision-instance"
        for occurrence in level.render_plan.instance_occurrences
    )
    root_collection[
        "killzone_distinct_collision_mesh_instance_matrix_count"
    ] = len(
        {
            struct.pack("<16f", *instance.matrix)
            for instance in level.collision_instances
        }
    )
    root_collection["killzone_origin_collision_mesh_instance_count"] = sum(
        instance.matrix[12:15] == (0.0, 0.0, 0.0)
        for instance in level.collision_instances
    )
    root_collection["killzone_animated_prop_count"] = len(
        level.skinned_instances
    )
    root_collection["killzone_prop_count"] = (
        len(level.instances)
        + len(level.collision_instances)
        + len(level.skinned_instances)
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
    timings["materials_and_images"] = time.perf_counter() - phase_start
    phase_start = time.perf_counter()
    report(0.45)
    occurrence_total = max(
        1,
        len(level.render_plan.direct_occurrences)
        + len(level.render_plan.instance_occurrences),
    )
    mesh_cache: dict[NativeObjectKey, bpy.types.Mesh] = {}
    winding_corrections = 0
    objects: list[bpy.types.Object] = []
    static_instance_objects: list[bpy.types.Object] = []
    collision_instance_objects: list[bpy.types.Object] = []
    animated_instance_objects: list[bpy.types.Object] = []

    def mesh_for(
        node: NativeStaticResourceNode,
    ) -> bpy.types.Mesh:
        nonlocal winding_corrections
        if len(objects) % 64 == 0:
            report(0.45 + 0.5 * min(1.0, len(objects) / occurrence_total))
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
            raise ValueError(
                "Authored visual occurrence has no instance key"
            )
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

    for instance in level.collision_instances:
        instance_occurrences = occurrences_by_instance.pop(
            instance.key, []
        )
        resource = resources_by_key[instance.resource_key]
        if len(instance_occurrences) == 1:
            occurrence_index, occurrence = instance_occurrences[0]
            node = resources_by_key[occurrence.leaf_key]
            owner = bpy.data.objects.new(
                instance.name
                or f"Collision Prop 0x{instance.object_id:08X}",
                mesh_for(node),
            )
            collision_instance_collection.objects.link(owner)
            owner.matrix_world = _object_matrix(instance.matrix)
            owner.color = _object_color(occurrence, node.object_id)
            owner["killzone_path"] = "collision-instance"
            owner["killzone_render_role"] = (
                "authored-single-leaf-collision-mesh-occurrence"
            )
            _set_instance_source_properties(
                owner,
                instance,
                serialized_type="CollisionMeshInstance",
            )
            _set_collision_instance_properties(owner, instance)
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
            collision_instance_objects.append(owner)
            objects.append(owner)
            continue

        if not instance_occurrences:
            raise ValueError(
                "Collision mesh instance has no render occurrence"
            )
        owner = bpy.data.objects.new(
            instance.name
            or f"Collision Prop 0x{instance.object_id:08X}",
            None,
        )
        owner.empty_display_type = "PLAIN_AXES"
        collision_instance_collection.objects.link(owner)
        owner.matrix_world = _object_matrix(instance.matrix)
        owner["killzone_path"] = "collision-instance-owner"
        _set_instance_source_properties(
            owner,
            instance,
            serialized_type="CollisionMeshInstance",
        )
        _set_collision_instance_properties(owner, instance)
        owner["killzone_resource_status"] = "resolved"
        owner["killzone_resource_kind"] = resource.kind
        _set_reference_key_properties(
            owner,
            "killzone_resource",
            resource.key,
        )
        owner["killzone_selected_leaf_count"] = len(instance_occurrences)
        collision_instance_objects.append(owner)

        for occurrence_index, occurrence in instance_occurrences:
            node = resources_by_key[occurrence.leaf_key]
            obj = bpy.data.objects.new(
                f"Collision Instance {occurrence_index:04d} "
                f"0x{node.object_id:08X}",
                mesh_for(node),
            )
            obj.parent = owner
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_world = owner.matrix_world.copy()
            obj.color = _object_color(occurrence, node.object_id)
            obj["killzone_path"] = "collision-instance"
            obj["killzone_occurrence_kind"] = (
                "collision-mesh-instance-leaf"
            )
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
            collision_instance_collection.objects.link(obj)
            objects.append(obj)

    if occurrences_by_instance:
        raise ValueError(
            "Static render plan references unknown visual instance keys"
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

    if len(static_instance_objects) != len(level.instances):
        raise ValueError(
            "Blender static occurrence-owner count disagrees with source"
        )
    if len(collision_instance_objects) != len(
        level.collision_instances
    ):
        raise ValueError(
            "Blender collision-mesh occurrence-owner count disagrees "
            "with source"
        )

    timings["meshes_and_objects"] = time.perf_counter() - phase_start
    phase_start = time.perf_counter()
    report(0.95)
    preview_counts = enable_bounded_native_material_previews(
        materials_by_token.values(),
        objects,
        include_write=import_textures,
        include_color_diagnostics=import_textures,
        artist_lightmap_layers=(
            import_textures and material_options.artist_lightmap_layers
        ),
        artist_fallback=import_textures and material_options.artist_fallback,
    )
    root_collection["killzone_unique_leaf_mesh_count"] = len(mesh_cache)
    root_collection["killzone_static_instance_owner_count"] = len(
        static_instance_objects
    )
    root_collection["killzone_collision_mesh_instance_owner_count"] = len(
        collision_instance_objects
    )
    root_collection["killzone_winding_corrections"] = winding_corrections
    root_collection["killzone_decoded_image_count"] = len(images_by_token)
    root_collection["killzone_bounded_alphaadd_preview_count"] = (
        preview_counts["alphaadd"]
    )
    root_collection["killzone_bounded_layerbumpdot3_preview_count"] = (
        preview_counts.get("layerbumpdot3", 0)
    )
    root_collection["killzone_maskedwrite_foliage_preview_count"] = (
        preview_counts.get("maskedwrite", 0)
    )
    root_collection["killzone_write_preview_count"] = preview_counts["write"]
    root_collection["killzone_color_diagnostic_preview_count"] = (
        preview_counts.get("color", 0)
    )
    root_collection["killzone_visible_material_preview_count"] = sum(
        preview_counts.values()
    )
    root_collection["killzone_artist_lightmap_preview_count"] = (
        preview_counts.get("write_layerbumpdot3", 0)
    )
    root_collection["killzone_artist_fallback_preview_count"] = (
        preview_counts.get("fallback", 0)
    )
    timings["material_previews"] = time.perf_counter() - phase_start
    root_collection["killzone_import_timings_json"] = json.dumps(
        {name: round(value, 4) for name, value in timings.items()},
        separators=(",", ":"),
    )
    report(1.0)
    return NativeLevelImportResult(
        level=level,
        root_collection=root_collection,
        direct_collection=direct_collection,
        props_collection=props_collection,
        instance_collection=instance_collection,
        collision_instance_collection=collision_instance_collection,
        animated_instance_collection=animated_instance_collection,
        objects=tuple(objects),
        static_instance_objects=tuple(static_instance_objects),
        collision_instance_objects=tuple(collision_instance_objects),
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
    material_options: NativeMaterialPreviewOptions | None = None,
    progress: Callable[[float], None] | None = None,
) -> NativeLevelImportResult:
    if progress is not None:
        progress(0.0)
    parse_start = time.perf_counter()
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
        material_options=material_options,
        progress=progress,
        timings={"parse_and_decode": time.perf_counter() - parse_start},
    )
