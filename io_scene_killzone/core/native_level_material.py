# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
import math
import struct
from typing import TYPE_CHECKING, Iterable, Mapping

from .errors import CoreFormatError
from .native_texture import (
    decode_csm1_rgba,
    unswizzle_psmt4,
    unswizzle_psmt8,
    validate_swizzled_plane_shape,
)

if TYPE_CHECKING:
    from .native_level import NativeCoreObject, NativeObjectKey


_U32 = struct.Struct("<I")
_TEXTURE_INFO = struct.Struct("<6I")
_TEXTURE_INFO_OFFSET = 0x20
_CHUNK_SIZES_OFFSET = 0x38
_MATERIAL_LAYERS_OFFSET = 0x18
_MATERIAL_LAYER_SIZE = 0x40
_TEXTURE_SPAN_RECORD = struct.Struct("<BBHH")
_ANIMATED_FRAMES_OFFSET = 0x20
_ANIMATED_FRAME_SIZE = 8
_MAX_CHUNKS = 256
_MAX_MIPS = 4
_MAX_MATERIAL_LAYERS = 1_000
_MAX_ANIMATION_FRAMES = 10_000

_PIXEL_FORMATS = {
    0: ("INDEX_4", 4, "PSMT4"),
    1: ("INDEX_8", 8, "PSMT8"),
    2: ("ALPHA_4", 4, "PSMT4"),
    3: ("ALPHA_8", 8, "PSMT8"),
    4: ("GLOW_8", 8, "PSMT8"),
    5: ("RGBA_8888", 32, "PSMCT32"),
    13: ("RGB_888", 24, "PSMCT24"),
    22: ("INVALID_PIXELFORMAT", 0, "invalid"),
}
_INDEXED_COLOR_FORMATS = {0, 1}
NATIVE_GS_COLOR_ONE = 128
NATIVE_LAYERBUMPDOT3_V4_8_ALPHA = 127
NATIVE_LAYERBUMPDOT3_FRAGMENT_ALPHA = (
    NATIVE_LAYERBUMPDOT3_V4_8_ALPHA // 2
)
NATIVE_MASKEDWRITE_DECODED_ALPHA_CUTOFF = 204
NATIVE_TEXTURE_PIXELS_DEFERRED = "pixels-deferred-until-selected"


@dataclass(frozen=True, slots=True)
class NativeLevelTextureMip:
    level: int
    width: int
    height: int
    payload_offset: int
    payload_size: int


@dataclass(frozen=True, slots=True)
class NativeTextureSpanRecord:
    chunk_index: int
    byte_offset: int
    qwc: int


@dataclass(frozen=True, slots=True)
class NativeLevelTexture:
    key: NativeObjectKey
    definition_keys: tuple[NativeObjectKey, ...]
    object_id: int
    no_streaming: int
    linear_storage: int
    width: int
    height: int
    pixel_format: int
    pixel_format_name: str
    bits_per_pixel: int
    sampling_psm: str
    mip_count: int
    chunk_sizes: tuple[int, ...]
    palette_chunk_index: int
    span_records: tuple[NativeTextureSpanRecord, ...]
    mips: tuple[NativeLevelTextureMip, ...]
    pixel_storage_size: int
    payload: bytes
    alignment_padding: bytes
    palette_payload: bytes
    opaque_footer: bytes
    inline_payload: bool
    decode_status: str
    base_indices: bytes | None
    base_rgba: bytes | None
    base_rgba_complement: bytes | None
    serialized_blob: bytes
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class NativeAnimatedTextureFrame:
    texture_token: int
    texture_key: NativeObjectKey
    duration: float


@dataclass(frozen=True, slots=True)
class NativeAnimatedTexture:
    key: NativeObjectKey
    object_id: int
    frames: tuple[NativeAnimatedTextureFrame, ...]
    opaque_header: bytes
    opaque_tail: bytes


@dataclass(frozen=True, slots=True)
class NativeMaterialLayer:
    index: int
    kind: str
    resource_token: int
    resource_key: NativeObjectKey | None
    flags: int
    operation: int
    lighting_mode: int
    z_write: int
    framebuffer_selector: int
    depth_comparison: int
    alpha_test: int
    alpha_reference: int
    wrap_s: int
    wrap_t: int
    filter_policy: int
    texture_enabled: int
    tfx_policy: int
    alpha_source_mode: int
    alpha_scalar: int
    fade_mode: int
    fade_values: tuple[float, float, float, float]
    coordinate_selector: int
    matrix_4x4: tuple[float, ...] | None
    transform_6f: tuple[float, ...] | None
    uses_default_transform: bool
    directional_vector: tuple[float, float, float]
    vu_features: tuple[bool, bool]
    palette_policy: str
    raw_record: bytes
    raw_optional_payload: bytes


@dataclass(frozen=True, slots=True)
class NativeMaterial:
    key: NativeObjectKey
    object_id: int
    layers: tuple[NativeMaterialLayer, ...]
    opaque_header: bytes
    opaque_trailer: bytes


@dataclass(frozen=True, slots=True)
class NativeLevelMaterialPlan:
    direct_material_tokens: tuple[int, ...]
    instance_material_tokens: tuple[int, ...]
    direct_texture_tokens: tuple[int, ...]
    instance_texture_tokens: tuple[int, ...]


def supports_bounded_native_layerbumpdot3_preview(
    layers: Iterable[NativeMaterialLayer],
    textures_by_token: Mapping[int, NativeLevelTexture],
) -> bool:
    material_layers = tuple(layers)
    if len(material_layers) != 2:
        return False
    base, detail = material_layers
    if base.lighting_mode not in (0, 1):
        return False

    def supports_common_state(
        layer: NativeMaterialLayer,
        *,
        index: int,
        framebuffer_selector: int,
    ) -> bool:
        return (
            layer.index == index
            and layer.kind == "texture"
            and layer.resource_token != 0
            and layer.flags == 0
            and layer.lighting_mode == base.lighting_mode
            and layer.z_write == 1
            and layer.framebuffer_selector == framebuffer_selector
            and layer.depth_comparison == 3
            and layer.alpha_test == 7
            and layer.alpha_reference == 0
            and layer.filter_policy == 4
            and layer.texture_enabled == 1
            and layer.alpha_source_mode == 0
            and layer.alpha_scalar == 255
            and layer.fade_mode == 0
            and layer.fade_values == (0.0, 0.5, 0.5, 1.0)
            and layer.matrix_4x4 is None
            and layer.transform_6f is None
            and not layer.uses_default_transform
            and layer.directional_vector == (1.0, 0.0, 0.0)
            and layer.vu_features == (False, False)
            and layer.palette_policy == "normal"
            and layer.raw_optional_payload == b""
        )

    if not (
        supports_common_state(
            base,
            index=0,
            framebuffer_selector=1,
        )
        and base.operation == 5
        and base.wrap_s == 0
        and base.wrap_t == 0
        and base.tfx_policy == 1
        and base.coordinate_selector == 0
        and supports_common_state(
            detail,
            index=1,
            framebuffer_selector=0,
        )
        and detail.operation == 18
        and detail.wrap_s == 1
        and detail.wrap_t == 1
        and detail.tfx_policy == 3
        and detail.coordinate_selector == 1
    ):
        return False

    base_texture = textures_by_token.get(base.resource_token)
    detail_texture = textures_by_token.get(detail.resource_token)
    if base_texture is None or detail_texture is None:
        return False
    expected_base_status = {
        0: "decoded-index4-two-bank",
        1: "decoded-index8-csm1",
    }.get(base_texture.pixel_format)
    return (
        expected_base_status is not None
        and base_texture.pixel_format_name in ("INDEX_4", "INDEX_8")
        and base_texture.decode_status == expected_base_status
        and base_texture.base_rgba is not None
        and detail_texture.pixel_format == 3
        and detail_texture.pixel_format_name == "ALPHA_8"
        and detail_texture.decode_status == "decoded-alpha8-two-bank"
        and detail_texture.base_rgba is not None
    )


def supports_diagnostic_native_maskedwrite_alphaadd_preview(
    layers: Iterable[NativeMaterialLayer],
    textures_by_token: Mapping[int, NativeLevelTexture],
) -> bool:
    material_layers = tuple(layers)
    if len(material_layers) != 2:
        return False
    masked_write, alpha_add = material_layers

    def supports_texture_layer(
        layer: NativeMaterialLayer,
        *,
        index: int,
    ) -> bool:
        return (
            layer.index == index
            and layer.kind == "texture"
            and layer.resource_token != 0
            and layer.texture_enabled == 1
            and layer.coordinate_selector == 0
            and layer.matrix_4x4 is None
            and layer.transform_6f is None
            and not layer.uses_default_transform
        )

    if not (
        supports_texture_layer(masked_write, index=0)
        and masked_write.operation == 8
        and masked_write.z_write == 1
        and masked_write.alpha_test == 4
        and masked_write.alpha_reference
        == NATIVE_MASKEDWRITE_DECODED_ALPHA_CUTOFF
        and supports_texture_layer(alpha_add, index=1)
        and alpha_add.operation == 5
        and alpha_add.z_write == 0
        and alpha_add.resource_token == masked_write.resource_token
    ):
        return False

    texture = textures_by_token.get(masked_write.resource_token)
    return (
        texture is not None
        and texture.pixel_format_name
        in ("INDEX_4", "INDEX_8", "RGBA_8888")
        and texture.base_rgba is not None
    )


def native_layerbumpdot3_source_alpha_byte(texture_alpha: int) -> int:
    if type(texture_alpha) is not int or not 0 <= texture_alpha <= 0xFF:
        raise ValueError("Texture alpha must be a byte")
    return min(
        texture_alpha + NATIVE_LAYERBUMPDOT3_FRAGMENT_ALPHA,
        0xFF,
    )


def native_layerbumpdot3_multiplier(texture_alpha: int) -> float:
    return (
        native_layerbumpdot3_source_alpha_byte(texture_alpha)
        / NATIVE_GS_COLOR_ONE
    )


def native_layerbumpdot3_blend_byte(
    destination: int,
    texture_alpha: int,
) -> int:
    if type(destination) is not int or not 0 <= destination <= 0xFF:
        raise ValueError("Destination color must be a byte")
    source_alpha = native_layerbumpdot3_source_alpha_byte(texture_alpha)
    return min(
        (destination * source_alpha) >> 7,
        0xFF,
    )


def native_alpha_reference_byte(serialized_reference: int) -> int:
    if (
        type(serialized_reference) is not int
        or not 0 <= serialized_reference <= 0xFF
    ):
        raise ValueError("Serialized alpha reference must be a byte")
    return serialized_reference >> 1


def native_alpha_test_passes(
    source_alpha: int,
    alpha_test: int,
    serialized_reference: int,
) -> bool:
    if type(source_alpha) is not int or not 0 <= source_alpha <= 0xFF:
        raise ValueError("Source alpha must be a byte")
    if type(alpha_test) is not int or not 0 <= alpha_test <= 7:
        raise ValueError("Alpha test must be in the range 0..7")
    reference = native_alpha_reference_byte(serialized_reference)
    if alpha_test == 0:
        return False
    if alpha_test == 1:
        return source_alpha < reference
    if alpha_test == 2:
        return source_alpha == reference
    if alpha_test == 3:
        return source_alpha <= reference
    if alpha_test == 4:
        return source_alpha > reference
    if alpha_test == 5:
        return source_alpha != reference
    if alpha_test == 6:
        return source_alpha >= reference
    return True


def _require_range(data: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise CoreFormatError(f"{label} is truncated")


def _align_0x100(value: int) -> int:
    return (value + 0xFF) & ~0xFF


def _texture_payload(
    obj: NativeCoreObject,
    corestream_data: bytes,
    chunk_sizes: tuple[int, ...],
) -> tuple[bytes, int, bool]:
    payload_size = sum(chunk_sizes)
    if obj.stream_size:
        if obj.stream_size != payload_size:
            raise CoreFormatError(
                f"Texture 0x{obj.object_id:08X} chunk sum is "
                f"{payload_size}, expected owned stream size {obj.stream_size}"
            )
        _require_range(
            corestream_data,
            obj.stream_offset,
            obj.stream_size,
            f"Texture 0x{obj.object_id:08X} CoreStream payload",
        )
        return (
            corestream_data[
                obj.stream_offset : obj.stream_offset + obj.stream_size
            ],
            _CHUNK_SIZES_OFFSET + len(chunk_sizes) * 4,
            False,
        )

    if payload_size == 0:
        return b"", _CHUNK_SIZES_OFFSET, False
    payload_offset = _CHUNK_SIZES_OFFSET + len(chunk_sizes) * 4
    _require_range(
        obj.blob,
        payload_offset,
        payload_size,
        f"Texture 0x{obj.object_id:08X} inline payload",
    )
    return (
        obj.blob[payload_offset : payload_offset + payload_size],
        payload_offset + payload_size,
        True,
    )


def _validate_index4_palette(
    object_id: int,
    palette: bytes,
) -> tuple[bytes, bytes]:
    if len(palette) != 0x100:
        raise CoreFormatError(
            f"Texture 0x{object_id:08X} INDEX_4 palette size is "
            f"0x{len(palette):X}, expected 0x100"
        )
    if palette[:0x10] != palette[0x50:0x60]:
        raise CoreFormatError(
            f"Texture 0x{object_id:08X} INDEX_4 bank headers differ"
        )
    if any(palette[0xA0:]):
        raise CoreFormatError(
            f"Texture 0x{object_id:08X} INDEX_4 palette tail is non-zero"
        )
    first_bank = palette[0x10:0x50]
    second_bank = palette[0x60:0xA0]
    for index in range(16):
        first = first_bank[index * 4 : index * 4 + 4]
        second = second_bank[index * 4 : index * 4 + 4]
        if first[:3] != second[:3] or first[3] + second[3] != 255:
            raise CoreFormatError(
                f"Texture 0x{object_id:08X} INDEX_4 palette entry "
                f"{index} is not a complementary-alpha pair"
            )
    return first_bank, second_bank


def _validate_index8_palette(
    object_id: int,
    palette: bytes,
) -> bytes:
    if len(palette) != 0x500:
        raise CoreFormatError(
            f"Texture 0x{object_id:08X} INDEX_8 palette size is "
            f"0x{len(palette):X}, expected 0x500"
        )
    if any(palette[0x410:]):
        raise CoreFormatError(
            f"Texture 0x{object_id:08X} INDEX_8 palette tail is non-zero"
        )
    return palette[0x10:0x410]


def _decode_base_rgba(
    *,
    object_id: int,
    pixel_format: int,
    width: int,
    height: int,
    base_plane: bytes,
    palette_payload: bytes,
    decode_pixels: bool = True,
    include_complement: bool = True,
) -> tuple[
    str,
    bytes | None,
    bytes | None,
    bytes | None,
]:
    if pixel_format == 1:
        raw_palette = _validate_index8_palette(object_id, palette_payload)
        if not decode_pixels:
            return NATIVE_TEXTURE_PIXELS_DEFERRED, None, None, None
        linear_indices = unswizzle_psmt8(base_plane, width, height)
        return (
            "decoded-index8-csm1",
            linear_indices,
            decode_csm1_rgba(linear_indices, raw_palette),
            None,
        )
    if pixel_format == 5:
        if not decode_pixels:
            return NATIVE_TEXTURE_PIXELS_DEFERRED, None, None, None
        return "decoded-rgba8888", None, base_plane, None
    if pixel_format == 13:
        if not decode_pixels:
            return NATIVE_TEXTURE_PIXELS_DEFERRED, None, None, None
        pixel_count = width * height
        rgba = bytearray(pixel_count * 4)
        rgba[0::4] = base_plane[0::3]
        rgba[1::4] = base_plane[1::3]
        rgba[2::4] = base_plane[2::3]
        rgba[3::4] = b"\xff" * pixel_count
        return (
            "decoded-rgb888-host-alpha",
            None,
            bytes(rgba),
            None,
        )
    if pixel_format == 0:
        normal_palette, complement_palette = _validate_index4_palette(
            object_id, palette_payload
        )
        if not decode_pixels:
            return NATIVE_TEXTURE_PIXELS_DEFERRED, None, None, None
        linear_indices = unswizzle_psmt4(base_plane, width, height)
        return (
            "decoded-index4-two-bank",
            linear_indices,
            _decode_linear_palette(linear_indices, normal_palette),
            (
                _decode_linear_palette(linear_indices, complement_palette)
                if include_complement
                else None
            ),
        )
    if pixel_format == 2:
        if not decode_pixels:
            return NATIVE_TEXTURE_PIXELS_DEFERRED, None, None, None
        linear_indices = unswizzle_psmt4(base_plane, width, height)
        return (
            "decoded-alpha4-two-bank",
            linear_indices,
            _decode_alpha_mask(linear_indices, scale=17, complement=False),
            (
                _decode_alpha_mask(linear_indices, scale=17, complement=True)
                if include_complement
                else None
            ),
        )
    if pixel_format == 3:
        if not decode_pixels:
            return NATIVE_TEXTURE_PIXELS_DEFERRED, None, None, None
        linear_indices = unswizzle_psmt8(base_plane, width, height)
        return (
            "decoded-alpha8-two-bank",
            linear_indices,
            _decode_alpha_mask(linear_indices, scale=1, complement=False),
            (
                _decode_alpha_mask(linear_indices, scale=1, complement=True)
                if include_complement
                else None
            ),
        )
    if pixel_format == 4:
        if not decode_pixels:
            return NATIVE_TEXTURE_PIXELS_DEFERRED, None, None, None
        linear_indices = unswizzle_psmt8(base_plane, width, height)
        return (
            "decoded-glow8-indices-dynamic-palette",
            linear_indices,
            None,
            None,
        )
    if pixel_format == 22:
        return "missing-invalid-pixelformat", None, None, None
    raise CoreFormatError(
        f"Texture 0x{object_id:08X} uses unsupported pixel format "
        f"{pixel_format}"
    )


def _decode_linear_palette(
    linear_indices: bytes,
    palette: bytes,
) -> bytes:
    entry_count = len(palette) // 4
    if linear_indices and max(linear_indices) >= entry_count:
        raise CoreFormatError("Indexed texture references past its palette")
    entries = [
        palette[index * 4 : index * 4 + 4] for index in range(entry_count)
    ]
    return b"".join(map(entries.__getitem__, linear_indices))


def _decode_alpha_mask(
    linear_indices: bytes,
    *,
    scale: int,
    complement: bool,
) -> bytes:
    pixel_count = len(linear_indices)
    alpha_table = bytes(
        (
            (255 - value * scale if complement else value * scale)
            if value * scale <= 255
            else 0
        )
        for value in range(256)
    )
    if pixel_count and max(linear_indices) * scale > 255:
        raise CoreFormatError("Alpha texture index exceeds the byte range")
    rgba = bytearray(b"\xff" * (pixel_count * 4))
    rgba[3::4] = linear_indices.translate(alpha_table)
    return bytes(rgba)


def _parse_span_records(
    *,
    object_id: int,
    blob: bytes,
    footer_offset: int,
    chunk_sizes: tuple[int, ...],
    payload: bytes,
    raw_size: int,
) -> tuple[tuple[NativeTextureSpanRecord, ...], bytes]:
    _require_range(
        blob,
        footer_offset,
        8,
        f"Texture 0x{object_id:08X} span footer",
    )
    zero, record_count = struct.unpack_from("<2I", blob, footer_offset)
    if zero != 0 or record_count % 2:
        raise CoreFormatError(
            f"Texture 0x{object_id:08X} span footer is invalid"
        )
    records_offset = footer_offset + 8
    records_size = record_count * _TEXTURE_SPAN_RECORD.size
    _require_range(
        blob,
        records_offset,
        records_size,
        f"Texture 0x{object_id:08X} span records",
    )
    records: list[NativeTextureSpanRecord] = []
    for record_index in range(record_count):
        chunk_index, reserved, byte_offset, qwc = (
            _TEXTURE_SPAN_RECORD.unpack_from(
                blob,
                records_offset
                + record_index * _TEXTURE_SPAN_RECORD.size,
            )
        )
        if reserved != 0 or chunk_index >= len(chunk_sizes) or qwc == 0:
            raise CoreFormatError(
                f"Texture 0x{object_id:08X} span record "
                f"{record_index} is invalid"
            )
        span_size = qwc * 16
        if (
            byte_offset > chunk_sizes[chunk_index]
            or span_size > chunk_sizes[chunk_index] - byte_offset
        ):
            raise CoreFormatError(
                f"Texture 0x{object_id:08X} span record "
                f"{record_index} exceeds its chunk"
            )
        records.append(
            NativeTextureSpanRecord(
                chunk_index=chunk_index,
                byte_offset=byte_offset,
                qwc=qwc,
            )
        )
    half = record_count // 2
    if records[:half] != records[half:]:
        raise CoreFormatError(
            f"Texture 0x{object_id:08X} span record halves differ"
        )

    chunk_offsets: list[int] = []
    cursor = 0
    for chunk_size in chunk_sizes:
        chunk_offsets.append(cursor)
        cursor += chunk_size
    reconstructed = bytearray()
    for record in records[:half]:
        start = chunk_offsets[record.chunk_index] + record.byte_offset
        reconstructed.extend(
            payload[start : start + record.qwc * 16]
        )
    if bytes(reconstructed) != payload[:raw_size]:
        raise CoreFormatError(
            f"Texture 0x{object_id:08X} span records do not reconstruct "
            "its raw mip prefix"
        )
    trailing = blob[records_offset + records_size :]
    if any(trailing):
        raise CoreFormatError(
            f"Texture 0x{object_id:08X} span footer tail is non-zero"
        )
    return tuple(records), trailing


def _parse_texture(
    obj: NativeCoreObject,
    corestream_data: bytes,
    *,
    decode_pixels: bool = True,
) -> NativeLevelTexture:
    _require_range(
        obj.blob,
        0,
        _CHUNK_SIZES_OFFSET,
        f"Texture 0x{obj.object_id:08X} header",
    )
    no_streaming, linear_storage = struct.unpack_from("<2I", obj.blob, 0)
    width, height, pixel_format, reserved, mip_count, chunk_count = (
        _TEXTURE_INFO.unpack_from(obj.blob, _TEXTURE_INFO_OFFSET)
    )
    if linear_storage != 0:
        raise CoreFormatError(
            f"Texture 0x{obj.object_id:08X} linear-storage flag is non-zero"
        )
    if reserved != 0:
        raise CoreFormatError(
            f"Texture 0x{obj.object_id:08X} reserved format word is non-zero"
        )
    format_info = _PIXEL_FORMATS.get(pixel_format)
    if format_info is None:
        raise CoreFormatError(
            f"Texture 0x{obj.object_id:08X} has unknown pixel format "
            f"{pixel_format}"
        )
    if chunk_count > _MAX_CHUNKS:
        raise CoreFormatError(
            f"Texture 0x{obj.object_id:08X} has too many chunks"
        )
    chunk_table_size = chunk_count * 4
    _require_range(
        obj.blob,
        _CHUNK_SIZES_OFFSET,
        chunk_table_size,
        f"Texture 0x{obj.object_id:08X} chunk table",
    )
    chunk_sizes = (
        struct.unpack_from(
            f"<{chunk_count}I",
            obj.blob,
            _CHUNK_SIZES_OFFSET,
        )
        if chunk_count
        else ()
    )
    payload, palette_word_offset, inline_payload = _texture_payload(
        obj, corestream_data, chunk_sizes
    )
    if inline_payload:
        if no_streaming != 1:
            raise CoreFormatError(
                f"Texture 0x{obj.object_id:08X} inline payload is not "
                "marked no-streaming"
            )
    elif no_streaming != 0:
        raise CoreFormatError(
            f"Texture 0x{obj.object_id:08X} streamed payload is marked "
            "no-streaming"
        )
    _require_range(
        obj.blob,
        palette_word_offset,
        4,
        f"Texture 0x{obj.object_id:08X} palette chunk index",
    )
    palette_chunk_index = _U32.unpack_from(
        obj.blob, palette_word_offset
    )[0]
    span_footer_offset = palette_word_offset + 4

    pixel_format_name, bits_per_pixel, sampling_psm = format_info
    if pixel_format == 22:
        if (
            (width, height, mip_count) != (0xFFFFFFFF,) * 3
            or chunk_count != 0
            or payload
            or palette_chunk_index != 0
        ):
            raise CoreFormatError(
                f"Texture 0x{obj.object_id:08X} invalid-format placeholder "
                "has unexpected storage"
            )
        span_records, opaque_footer = _parse_span_records(
            object_id=obj.object_id,
            blob=obj.blob,
            footer_offset=span_footer_offset,
            chunk_sizes=chunk_sizes,
            payload=payload,
            raw_size=0,
        )
        (
            decode_status,
            base_indices,
            base_rgba,
            base_rgba_complement,
        ) = _decode_base_rgba(
            object_id=obj.object_id,
            pixel_format=pixel_format,
            width=width,
            height=height,
            base_plane=b"",
            palette_payload=b"",
        )
        return NativeLevelTexture(
            key=obj.key,
            definition_keys=(obj.key,),
            object_id=obj.object_id,
            no_streaming=no_streaming,
            linear_storage=linear_storage,
            width=width,
            height=height,
            pixel_format=pixel_format,
            pixel_format_name=pixel_format_name,
            bits_per_pixel=bits_per_pixel,
            sampling_psm=sampling_psm,
            mip_count=mip_count,
            chunk_sizes=chunk_sizes,
            palette_chunk_index=palette_chunk_index,
            span_records=span_records,
            mips=(),
            pixel_storage_size=0,
            payload=payload,
            alignment_padding=b"",
            palette_payload=b"",
            opaque_footer=opaque_footer,
            inline_payload=inline_payload,
            decode_status=decode_status,
            base_indices=base_indices,
            base_rgba=base_rgba,
            base_rgba_complement=base_rgba_complement,
            serialized_blob=obj.blob,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
        )

    if width == 0 or height == 0:
        raise CoreFormatError(
            f"Texture 0x{obj.object_id:08X} has zero dimensions"
        )
    if mip_count == 0 or mip_count > _MAX_MIPS:
        raise CoreFormatError(
            f"Texture 0x{obj.object_id:08X} has invalid mip count "
            f"{mip_count}"
        )

    mip_entries: list[NativeLevelTextureMip] = []
    raw_size = 0
    for level in range(mip_count):
        mip_width = max(1, width >> level)
        mip_height = max(1, height >> level)
        mip_size = (
            mip_width * mip_height * bits_per_pixel + 7
        ) // 8
        mip_entries.append(
            NativeLevelTextureMip(
                level=level,
                width=mip_width,
                height=mip_height,
                payload_offset=raw_size,
                payload_size=mip_size,
            )
        )
        raw_size += mip_size
    pixel_storage_size = _align_0x100(raw_size)
    if pixel_storage_size > len(payload):
        raise CoreFormatError(
            f"Texture 0x{obj.object_id:08X} pixel storage is truncated"
        )
    alignment_padding = payload[raw_size:pixel_storage_size]
    if any(alignment_padding):
        raise CoreFormatError(
            f"Texture 0x{obj.object_id:08X} pixel alignment is non-zero"
        )

    if pixel_format in _INDEXED_COLOR_FORMATS:
        if chunk_count == 0 or palette_chunk_index != chunk_count - 1:
            raise CoreFormatError(
                f"Texture 0x{obj.object_id:08X} palette chunk index is "
                "invalid"
            )
        if sum(chunk_sizes[:palette_chunk_index]) != pixel_storage_size:
            raise CoreFormatError(
                f"Texture 0x{obj.object_id:08X} pixel chunks do not match "
                "aligned mip storage"
            )
        palette_payload = payload[pixel_storage_size:]
        if len(palette_payload) != chunk_sizes[palette_chunk_index]:
            raise CoreFormatError(
                f"Texture 0x{obj.object_id:08X} palette chunk is not final"
            )
    else:
        if palette_chunk_index != 0 or len(payload) != pixel_storage_size:
            raise CoreFormatError(
                f"Texture 0x{obj.object_id:08X} direct or auxiliary format "
                "has unexpected palette storage"
            )
        palette_payload = b""

    if pixel_format in (0, 1, 2, 3, 4):
        # Every mip plane must have a legal PSMT4/PSMT8 block shape. Checking
        # the shape is enough; unswizzling unused mips only to discard them
        # dominated texture parse time.
        for mip in mip_entries:
            validate_swizzled_plane_shape(
                sampling_psm,
                mip.width,
                mip.height,
            )

    span_records, opaque_footer = _parse_span_records(
        object_id=obj.object_id,
        blob=obj.blob,
        footer_offset=span_footer_offset,
        chunk_sizes=chunk_sizes,
        payload=payload,
        raw_size=raw_size,
    )
    base = mip_entries[0]
    base_plane = payload[
        base.payload_offset : base.payload_offset + base.payload_size
    ]
    (
        decode_status,
        base_indices,
        base_rgba,
        base_rgba_complement,
    ) = _decode_base_rgba(
        object_id=obj.object_id,
        pixel_format=pixel_format,
        width=width,
        height=height,
        base_plane=base_plane,
        palette_payload=palette_payload,
        decode_pixels=decode_pixels,
    )
    return NativeLevelTexture(
        key=obj.key,
        definition_keys=(obj.key,),
        object_id=obj.object_id,
        no_streaming=no_streaming,
        linear_storage=linear_storage,
        width=width,
        height=height,
        pixel_format=pixel_format,
        pixel_format_name=pixel_format_name,
        bits_per_pixel=bits_per_pixel,
        sampling_psm=sampling_psm,
        mip_count=mip_count,
        chunk_sizes=chunk_sizes,
        palette_chunk_index=palette_chunk_index,
        span_records=span_records,
        mips=tuple(mip_entries),
        pixel_storage_size=pixel_storage_size,
        payload=payload,
        alignment_padding=alignment_padding,
        palette_payload=palette_payload,
        opaque_footer=opaque_footer,
        inline_payload=inline_payload,
        decode_status=decode_status,
        base_indices=base_indices,
        base_rgba=base_rgba,
        base_rgba_complement=base_rgba_complement,
        serialized_blob=obj.blob,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
    )


def decode_level_texture_pixels(
    texture: NativeLevelTexture,
    *,
    include_complement: bool = True,
) -> NativeLevelTexture:
    """Return ``texture`` with its base-mip pixels decoded.

    Parsing can defer the pixel work because a level Core carries far more
    Texture objects than its selected Materials reference.
    """

    if texture.pixel_format == 22:
        return texture
    if texture.decode_status != NATIVE_TEXTURE_PIXELS_DEFERRED and (
        texture.base_rgba_complement is not None
        or not include_complement
        or texture.pixel_format not in (0, 2, 3)
    ):
        return texture
    (
        decode_status,
        base_indices,
        base_rgba,
        base_rgba_complement,
    ) = _decode_base_rgba(
        object_id=texture.object_id,
        pixel_format=texture.pixel_format,
        width=texture.width,
        height=texture.height,
        base_plane=texture_base_payload(texture),
        palette_payload=texture.palette_payload,
        include_complement=include_complement,
    )
    return replace(
        texture,
        decode_status=decode_status,
        base_indices=base_indices,
        base_rgba=base_rgba,
        base_rgba_complement=base_rgba_complement,
    )


def decode_selected_level_textures(
    logical_textures: Iterable[NativeLevelTexture],
    selected_tokens: Iterable[int],
    complement_tokens: Iterable[int] = (),
) -> tuple[NativeLevelTexture, ...]:
    """Decode only the selected logical textures.

    The complementary-alpha bank is decoded only for ``complement_tokens``;
    ordinary layers always sample the normal bank.
    """

    selected = set(selected_tokens)
    complement = set(complement_tokens)
    return tuple(
        decode_level_texture_pixels(
            texture,
            include_complement=texture.object_id in complement,
        )
        if texture.object_id in selected
        else texture
        for texture in logical_textures
    )


def parse_level_textures(
    objects: Iterable[NativeCoreObject],
    corestream_data: bytes,
    *,
    decode_pixels: bool = True,
) -> tuple[
    tuple[NativeLevelTexture, ...],
    tuple[NativeLevelTexture, ...],
]:
    physical = tuple(
        _parse_texture(obj, corestream_data, decode_pixels=False)
        for obj in objects
        if obj.type_name == "Texture"
    )
    by_token: dict[int, list[NativeLevelTexture]] = defaultdict(list)
    for texture in physical:
        by_token[texture.object_id].append(texture)

    logical: list[NativeLevelTexture] = []
    for object_id in sorted(by_token):
        definitions = by_token[object_id]
        first = definitions[0]
        for candidate in definitions[1:]:
            if (
                candidate.serialized_blob != first.serialized_blob
                or candidate.payload != first.payload
            ):
                raise CoreFormatError(
                    f"Texture token 0x{object_id:08X} has non-equivalent "
                    "duplicate definitions"
                )
        logical.append(
            replace(
                first,
                definition_keys=tuple(
                    definition.key for definition in definitions
                ),
            )
        )
    if not decode_pixels:
        return physical, tuple(logical)

    # Decode each logical texture once and share the pixels with its
    # byte-equivalent physical duplicates.
    decoded = tuple(decode_level_texture_pixels(value) for value in logical)
    decoded_by_token = {value.object_id: value for value in decoded}
    physical = tuple(
        replace(
            texture,
            decode_status=decoded_by_token[texture.object_id].decode_status,
            base_indices=decoded_by_token[texture.object_id].base_indices,
            base_rgba=decoded_by_token[texture.object_id].base_rgba,
            base_rgba_complement=(
                decoded_by_token[texture.object_id].base_rgba_complement
            ),
        )
        for texture in physical
    )
    return physical, decoded


def _unique_by_token(
    values: Iterable[NativeLevelTexture | NativeAnimatedTexture],
    label: str,
) -> dict[int, NativeLevelTexture | NativeAnimatedTexture]:
    result: dict[
        int, NativeLevelTexture | NativeAnimatedTexture
    ] = {}
    for value in values:
        if value.object_id in result:
            raise CoreFormatError(
                f"{label} token 0x{value.object_id:08X} is duplicated"
            )
        result[value.object_id] = value
    return result


def parse_animated_textures(
    objects: Iterable[NativeCoreObject],
    logical_textures: tuple[NativeLevelTexture, ...],
) -> tuple[NativeAnimatedTexture, ...]:
    textures_by_token = _unique_by_token(logical_textures, "Texture")
    animations: list[NativeAnimatedTexture] = []
    for obj in objects:
        if obj.type_name != "AnimatedTexture":
            continue
        _require_range(
            obj.blob,
            0,
            _ANIMATED_FRAMES_OFFSET,
            f"AnimatedTexture 0x{obj.object_id:08X} header",
        )
        frame_count = _U32.unpack_from(obj.blob, 0)[0]
        if frame_count == 0 or frame_count > _MAX_ANIMATION_FRAMES:
            raise CoreFormatError(
                f"AnimatedTexture 0x{obj.object_id:08X} has invalid frame "
                f"count {frame_count}"
            )
        frames_size = frame_count * _ANIMATED_FRAME_SIZE
        _require_range(
            obj.blob,
            _ANIMATED_FRAMES_OFFSET,
            frames_size,
            f"AnimatedTexture 0x{obj.object_id:08X} frames",
        )
        frames: list[NativeAnimatedTextureFrame] = []
        for frame_index in range(frame_count):
            texture_token, duration = struct.unpack_from(
                "<If",
                obj.blob,
                _ANIMATED_FRAMES_OFFSET
                + frame_index * _ANIMATED_FRAME_SIZE,
            )
            texture = textures_by_token.get(texture_token)
            if texture is None:
                raise CoreFormatError(
                    f"AnimatedTexture 0x{obj.object_id:08X} frame "
                    f"{frame_index} references missing Texture "
                    f"0x{texture_token:08X}"
                )
            if not math.isfinite(duration) or duration <= 0.0:
                raise CoreFormatError(
                    f"AnimatedTexture 0x{obj.object_id:08X} frame "
                    f"{frame_index} has invalid duration"
                )
            frames.append(
                NativeAnimatedTextureFrame(
                    texture_token=texture_token,
                    texture_key=texture.key,
                    duration=duration,
                )
            )
        frame_end = (
            _ANIMATED_FRAMES_OFFSET
            + frame_count * _ANIMATED_FRAME_SIZE
        )
        animations.append(
            NativeAnimatedTexture(
                key=obj.key,
                object_id=obj.object_id,
                frames=tuple(frames),
                opaque_header=obj.blob[4:_ANIMATED_FRAMES_OFFSET],
                opaque_tail=obj.blob[frame_end:],
            )
        )
    _unique_by_token(animations, "AnimatedTexture")
    return tuple(animations)


def parse_selected_materials(
    objects: Iterable[NativeCoreObject],
    selected_tokens: Iterable[int],
    logical_textures: tuple[NativeLevelTexture, ...],
    animated_textures: tuple[NativeAnimatedTexture, ...],
) -> tuple[NativeMaterial, ...]:
    material_objects: dict[int, NativeCoreObject] = {}
    for obj in objects:
        if obj.type_name != "Material":
            continue
        if obj.object_id in material_objects:
            raise CoreFormatError(
                f"Material token 0x{obj.object_id:08X} is duplicated"
            )
        material_objects[obj.object_id] = obj

    textures_by_token = _unique_by_token(logical_textures, "Texture")
    animations_by_token = _unique_by_token(
        animated_textures, "AnimatedTexture"
    )
    materials: list[NativeMaterial] = []
    for token in sorted(set(selected_tokens)):
        obj = material_objects.get(token)
        if obj is None:
            raise CoreFormatError(
                f"Selected Material token 0x{token:08X} does not resolve"
            )
        _require_range(
            obj.blob,
            0,
            _MATERIAL_LAYERS_OFFSET,
            f"Material 0x{token:08X} header",
        )
        layer_count = _U32.unpack_from(obj.blob, 0)[0]
        if layer_count > _MAX_MATERIAL_LAYERS:
            raise CoreFormatError(
                f"Material 0x{token:08X} has too many layers"
            )
        layers: list[NativeMaterialLayer] = []
        cursor = _MATERIAL_LAYERS_OFFSET
        for layer_index in range(layer_count):
            _require_range(
                obj.blob,
                cursor,
                _MATERIAL_LAYER_SIZE,
                f"Material 0x{token:08X} layer {layer_index}",
            )
            raw_record = obj.blob[cursor : cursor + _MATERIAL_LAYER_SIZE]
            resource_token = _U32.unpack_from(raw_record, 0)[0]
            flags = raw_record[4]
            if any(raw_record[5:8]) or _U32.unpack_from(
                raw_record, 8
            )[0] != 0x74:
                raise CoreFormatError(
                    f"Material 0x{token:08X} layer {layer_index} "
                    "framing is invalid"
                )
            if flags & ~0x60:
                raise CoreFormatError(
                    f"Material 0x{token:08X} layer {layer_index} "
                    f"has unsupported flags 0x{flags:02X}"
                )

            resource_key = None
            if resource_token == 0:
                kind = "null_texture"
            else:
                texture = textures_by_token.get(resource_token)
                animation = animations_by_token.get(resource_token)
                if texture is not None and animation is not None:
                    raise CoreFormatError(
                        f"Material 0x{token:08X} layer {layer_index} "
                        "resolves to both Texture and AnimatedTexture"
                    )
                if texture is not None:
                    kind = "texture"
                    resource_key = texture.key
                elif animation is not None:
                    kind = "animated_texture"
                    resource_key = animation.key
                else:
                    raise CoreFormatError(
                        f"Material 0x{token:08X} layer {layer_index} "
                        f"references missing provider "
                        f"0x{resource_token:08X}"
                    )

            operation = raw_record[0x0C]
            coordinate_selector = raw_record[0x31]
            alpha_test = raw_record[0x11]
            if (
                operation > 20
                or coordinate_selector > 21
                or alpha_test > 7
            ):
                raise CoreFormatError(
                    f"Material 0x{token:08X} layer {layer_index} "
                    "has an out-of-range operation, coordinate selector, "
                    "or alpha test"
                )
            fade_values = struct.unpack_from("<4f", raw_record, 0x1C)
            directional_vector = struct.unpack_from(
                "<3f", raw_record, 0x32
            )
            if not all(
                math.isfinite(value)
                for value in fade_values + directional_vector
            ):
                raise CoreFormatError(
                    f"Material 0x{token:08X} layer {layer_index} "
                    "contains a non-finite fixed value"
                )
            if raw_record[0x3E] not in (0, 1) or raw_record[
                0x3F
            ] not in (0, 1):
                raise CoreFormatError(
                    f"Material 0x{token:08X} layer {layer_index} "
                    "has a non-boolean VU feature"
                )

            cursor += _MATERIAL_LAYER_SIZE
            optional_start = cursor
            matrix_4x4 = None
            if coordinate_selector == 11:
                _require_range(
                    obj.blob,
                    cursor,
                    0x40,
                    f"Material 0x{token:08X} layer {layer_index} matrix",
                )
                matrix_4x4 = struct.unpack_from("<16f", obj.blob, cursor)
                if not all(math.isfinite(value) for value in matrix_4x4):
                    raise CoreFormatError(
                        f"Material 0x{token:08X} layer {layer_index} "
                        "matrix is not finite"
                    )
                cursor += 0x40

            transform_6f = None
            if flags & 0x20:
                _require_range(
                    obj.blob,
                    cursor,
                    0x18,
                    f"Material 0x{token:08X} layer {layer_index} transform",
                )
                transform_6f = struct.unpack_from("<6f", obj.blob, cursor)
                if not all(math.isfinite(value) for value in transform_6f):
                    raise CoreFormatError(
                        f"Material 0x{token:08X} layer {layer_index} "
                        "transform is not finite"
                    )
                cursor += 0x18

            alpha_scalar = raw_record[0x1A]
            fade_mode = raw_record[0x1B]
            if operation in (6, 17):
                palette_policy = (
                    "complement"
                    if alpha_scalar != 255 or fade_mode != 0
                    else "runtime-gated"
                )
            else:
                palette_policy = "normal"

            layers.append(
                NativeMaterialLayer(
                    index=layer_index,
                    kind=kind,
                    resource_token=resource_token,
                    resource_key=resource_key,
                    flags=flags,
                    operation=operation,
                    lighting_mode=raw_record[0x0D],
                    z_write=raw_record[0x0E],
                    framebuffer_selector=raw_record[0x0F],
                    depth_comparison=raw_record[0x10],
                    alpha_test=alpha_test,
                    alpha_reference=raw_record[0x12],
                    wrap_s=raw_record[0x13],
                    wrap_t=raw_record[0x14],
                    filter_policy=raw_record[0x15],
                    texture_enabled=raw_record[0x16],
                    tfx_policy=raw_record[0x18],
                    alpha_source_mode=raw_record[0x19],
                    alpha_scalar=alpha_scalar,
                    fade_mode=fade_mode,
                    fade_values=fade_values,
                    coordinate_selector=coordinate_selector,
                    matrix_4x4=matrix_4x4,
                    transform_6f=transform_6f,
                    uses_default_transform=bool(flags & 0x40),
                    directional_vector=directional_vector,
                    vu_features=(
                        bool(raw_record[0x3E]),
                        bool(raw_record[0x3F]),
                    ),
                    palette_policy=palette_policy,
                    raw_record=raw_record,
                    raw_optional_payload=obj.blob[optional_start:cursor],
                )
            )
        opaque_trailer = obj.blob[cursor:]
        if len(opaque_trailer) not in (8, 16) or any(opaque_trailer[4:]):
            raise CoreFormatError(
                f"Material 0x{token:08X} trailer is outside the "
                "validated framing"
            )
        materials.append(
            NativeMaterial(
                key=obj.key,
                object_id=obj.object_id,
                layers=tuple(layers),
                opaque_header=obj.blob[4:_MATERIAL_LAYERS_OFFSET],
                opaque_trailer=opaque_trailer,
            )
        )
    return tuple(materials)


def _material_texture_tokens(
    material_tokens: Iterable[int],
    materials: tuple[NativeMaterial, ...],
    animations: tuple[NativeAnimatedTexture, ...],
) -> tuple[int, ...]:
    materials_by_token = {value.object_id: value for value in materials}
    animations_by_token = {
        value.object_id: value for value in animations
    }
    texture_tokens: set[int] = set()
    for material_token in material_tokens:
        material = materials_by_token.get(material_token)
        if material is None:
            raise CoreFormatError(
                f"Material plan references missing Material "
                f"0x{material_token:08X}"
            )
        for layer in material.layers:
            if layer.kind == "texture":
                texture_tokens.add(layer.resource_token)
            elif layer.kind == "animated_texture":
                animation = animations_by_token[layer.resource_token]
                texture_tokens.update(
                    frame.texture_token for frame in animation.frames
                )
    return tuple(sorted(texture_tokens))


def material_complement_texture_tokens(
    material_tokens: Iterable[int],
    materials: tuple[NativeMaterial, ...],
    animations: tuple[NativeAnimatedTexture, ...],
) -> tuple[int, ...]:
    """Texture tokens whose complementary-alpha bank a layer may sample."""

    wanted = set(material_tokens)
    animations_by_token = {
        value.object_id: value for value in animations
    }
    tokens: set[int] = set()
    for material in materials:
        if material.object_id not in wanted:
            continue
        for layer in material.layers:
            if layer.palette_policy == "normal":
                continue
            if layer.kind == "texture":
                tokens.add(layer.resource_token)
            elif layer.kind == "animated_texture":
                tokens.update(
                    frame.texture_token
                    for frame in animations_by_token[
                        layer.resource_token
                    ].frames
                )
    return tuple(sorted(tokens))


def build_level_material_plan(
    direct_material_tokens: Iterable[int],
    instance_material_tokens: Iterable[int],
    materials: tuple[NativeMaterial, ...],
    animations: tuple[NativeAnimatedTexture, ...],
) -> NativeLevelMaterialPlan:
    direct = tuple(sorted(set(direct_material_tokens)))
    instance = tuple(sorted(set(instance_material_tokens)))
    return NativeLevelMaterialPlan(
        direct_material_tokens=direct,
        instance_material_tokens=instance,
        direct_texture_tokens=_material_texture_tokens(
            direct, materials, animations
        ),
        instance_texture_tokens=_material_texture_tokens(
            instance, materials, animations
        ),
    )


@dataclass(frozen=True, slots=True)
class NativeSelectedMaterialSet:
    textures: tuple[NativeLevelTexture, ...]
    logical_textures: tuple[NativeLevelTexture, ...]
    animated_textures: tuple[NativeAnimatedTexture, ...]
    materials: tuple[NativeMaterial, ...]
    material_plan: NativeLevelMaterialPlan


def parse_selected_material_set(
    objects: Iterable[NativeCoreObject],
    corestream_data: bytes,
    direct_material_tokens: Iterable[int],
    instance_material_tokens: Iterable[int] = (),
) -> NativeSelectedMaterialSet:
    """Parse selected Materials and decode only the textures they sample."""

    objects = tuple(objects)
    direct = tuple(direct_material_tokens)
    instance = tuple(instance_material_tokens)
    textures, logical_textures = parse_level_textures(
        objects,
        corestream_data,
        decode_pixels=False,
    )
    animated_textures = parse_animated_textures(objects, logical_textures)
    materials = parse_selected_materials(
        objects,
        direct + instance,
        logical_textures,
        animated_textures,
    )
    material_plan = build_level_material_plan(
        direct,
        instance,
        materials,
        animated_textures,
    )
    logical_textures = decode_selected_level_textures(
        logical_textures,
        set(material_plan.direct_texture_tokens)
        | set(material_plan.instance_texture_tokens),
        material_complement_texture_tokens(
            direct + instance,
            materials,
            animated_textures,
        ),
    )
    return NativeSelectedMaterialSet(
        textures=textures,
        logical_textures=logical_textures,
        animated_textures=animated_textures,
        materials=materials,
        material_plan=material_plan,
    )


def texture_base_payload(texture: NativeLevelTexture) -> bytes:
    if not texture.mips:
        return b""
    base = texture.mips[0]
    return texture.payload[
        base.payload_offset : base.payload_offset + base.payload_size
    ]
