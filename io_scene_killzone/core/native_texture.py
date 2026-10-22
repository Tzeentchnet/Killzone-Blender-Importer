# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import struct

from .archive_source import KillzoneArchiveSource
from .core_format import CoreHeader, core_header_prefix_size, parse_core_header
from .errors import CoreFormatError

_U32 = struct.Struct("<I")
_TEXTURE_INFO = struct.Struct("<6I")

_TEXTURE_SCHEMA = (
    b"Texture",
    b"General.No streaming",
    b"General.Linear storage",
    b"General.Parent",
    b"General.Name",
    b"General.Name is identifier",
    b"General.PS2Mip1Distance",
    b"General.PS2MipMapL",
)

_BODY_MARKER_OFFSET = 0x00
_FINAL_PROPERTY_VALUE_OFFSET_OFFSET = 0x80
_TEXTURE_INFO_OFFSET = 0xD0
_CHUNK_SIZES_OFFSET = 0xE8
_SUPPORTED_WIDTH = 512
_SUPPORTED_HEIGHT = 512
_SUPPORTED_CHUNK_SIZES = (0x8000,) * 8 + (0x500,)
_PALETTE_SIZE = 256 * 4
_SUPPORTED_AUXILIARY_SIZE = 0x100
_SUPPORTED_FOOTER_SIZE = 0x78


@dataclass(frozen=True, slots=True)
class NativeTexture:
    name: str
    width: int
    height: int
    rgba: bytes
    linear_indices: bytes
    raw_palette: bytes
    auxiliary_payload: bytes
    opaque_footer: bytes
    header: CoreHeader
    source_sha256: str
    original_core: bytes


@dataclass(frozen=True, slots=True)
class NativeTextureAsset:
    path: str
    name: str
    size: int


def _read_u32(data: bytes, offset: int, label: str) -> int:
    if offset < 0 or offset + _U32.size > len(data):
        raise CoreFormatError(f"Core Texture {label} is truncated")
    return _U32.unpack_from(data, offset)[0]


def _psmt8_offset(x: int, y: int, width: int) -> int:
    block_location = (y & ~0x0F) * width + (x & ~0x0F) * 2
    swap_selector = (((y + 2) >> 2) & 1) * 4
    position_y = (((y & ~3) >> 1) + (y & 1)) & 7
    column_location = (
        position_y * width * 2 + ((x + swap_selector) & 7) * 4
    )
    byte_number = ((y >> 1) & 1) + ((x >> 2) & 2)
    return block_location + column_location + byte_number


def unswizzle_psmt8(data: bytes, width: int, height: int) -> bytes:
    expected_size = width * height
    if width <= 0 or height <= 0:
        raise CoreFormatError("PSMT8 dimensions must be positive")
    if width >= 16 and (width % 16 or height % 8):
        raise CoreFormatError(
            "PSMT8 width must be a positive multiple of 16 and height "
            "a positive multiple of 8"
        )
    if len(data) != expected_size:
        raise CoreFormatError(
            f"PSMT8 payload size is {len(data)}, expected {expected_size}"
        )
    if width < 16:
        return data

    result = bytearray(expected_size)
    for y in range(height):
        row_offset = y * width
        for x in range(width):
            source_offset = _psmt8_offset(x, y, width)
            if source_offset >= len(data):
                raise CoreFormatError(
                    f"PSMT8 source offset 0x{source_offset:X} is out of bounds"
                )
            result[row_offset + x] = data[source_offset]
    return bytes(result)


def unswizzle_psmt4(data: bytes, width: int, height: int) -> bytes:
    texel_count = width * height
    expected_size = (texel_count + 1) // 2
    if width <= 0 or height <= 0:
        raise CoreFormatError("PSMT4 dimensions must be positive")
    if width >= 16 and (width % 16 or height % 8):
        raise CoreFormatError(
            "PSMT4 width must be a positive multiple of 16 and height "
            "a positive multiple of 8"
        )
    if len(data) != expected_size:
        raise CoreFormatError(
            f"PSMT4 payload size is {len(data)}, expected {expected_size}"
        )

    result = bytearray(texel_count)
    for y in range(height):
        for x in range(width):
            if width < 16:
                nibble_index = y * width + x
            else:
                block = (y & ~0x0F) * width + (x & ~0x1F) * 2
                swap = (((y + 2) >> 2) & 1) * 4
                py = (((y & ~3) >> 1) + (y & 1)) & 7
                u = ((x + swap) & 7) + ((x >> 1) & 8)
                k = ((y >> 1) & 1) + ((x >> 2) & 2)
                nibble_index = block + py * (2 * width) + 4 * u + k
            byte_offset = nibble_index >> 1
            if byte_offset >= len(data):
                raise CoreFormatError(
                    f"PSMT4 source offset 0x{byte_offset:X} is out of bounds"
                )
            value = data[byte_offset]
            result[y * width + x] = (
                (value >> 4) & 0x0F
                if nibble_index & 1
                else value & 0x0F
            )
    return bytes(result)


def _csm1_palette_index(index: int) -> int:
    return (
        (index & 0xE7)
        | ((index & 0x08) << 1)
        | ((index & 0x10) >> 1)
    )


def decode_csm1_rgba(
    linear_indices: bytes,
    raw_palette: bytes,
) -> bytes:
    if len(raw_palette) != _PALETTE_SIZE:
        raise CoreFormatError(
            f"Core Texture palette size is {len(raw_palette)}, "
            f"expected {_PALETTE_SIZE}"
        )

    rgba = bytearray(len(linear_indices) * 4)
    for pixel_offset, index in enumerate(linear_indices):
        palette_offset = _csm1_palette_index(index) * 4
        output_offset = pixel_offset * 4
        rgba[output_offset : output_offset + 4] = raw_palette[
            palette_offset : palette_offset + 4
        ]
    return bytes(rgba)


def parse_native_texture(data: bytes) -> NativeTexture:
    if len(data) < 24:
        raise CoreFormatError("Core Texture fixed header is truncated")
    required = core_header_prefix_size(data[:24], len(data))
    header = parse_core_header(data[:required], len(data))
    if header.variant != 1 or header.first_type != "Texture":
        raise CoreFormatError(
            "Only self-contained Core variant-1 Texture resources are supported"
        )
    if len(header.strings) != len(_TEXTURE_SCHEMA) + 1:
        raise CoreFormatError(
            "Unsupported Core Texture string-table shape"
        )
    actual_schema = tuple(
        value.raw for value in header.strings[: len(_TEXTURE_SCHEMA)]
    )
    if actual_schema != _TEXTURE_SCHEMA:
        raise CoreFormatError("Unsupported Core Texture property schema")
    name = header.strings[-1].utf8
    if not name:
        raise CoreFormatError("Core Texture name is not valid non-empty UTF-8")

    body = data[header.body_offset :]
    if _read_u32(body, _BODY_MARKER_OFFSET, "body marker") != 0xAC:
        raise CoreFormatError("Unsupported Core Texture body marker")
    final_property_value_offset = _read_u32(
        body,
        _FINAL_PROPERTY_VALUE_OFFSET_OFFSET,
        "final property value offset",
    )
    if final_property_value_offset != 0x18:
        raise CoreFormatError(
            "Unsupported Core Texture final property value offset "
            f"0x{final_property_value_offset:X}"
        )
    if _TEXTURE_INFO_OFFSET + _TEXTURE_INFO.size > len(body):
        raise CoreFormatError("Core Texture metadata is truncated")

    width, height, level_count, unknown_dc, buffer_count, chunk_count = (
        _TEXTURE_INFO.unpack_from(body, _TEXTURE_INFO_OFFSET)
    )
    if (width, height) != (_SUPPORTED_WIDTH, _SUPPORTED_HEIGHT):
        raise CoreFormatError(
            f"Unsupported Core Texture dimensions {width}x{height}; "
            f"expected {_SUPPORTED_WIDTH}x{_SUPPORTED_HEIGHT}"
        )
    if (level_count, unknown_dc, buffer_count) != (1, 0, 1):
        raise CoreFormatError(
            "Unsupported Core Texture level or buffer metadata"
        )
    if chunk_count != len(_SUPPORTED_CHUNK_SIZES):
        raise CoreFormatError(
            f"Unsupported Core Texture chunk count {chunk_count}"
        )

    chunk_table_size = chunk_count * _U32.size
    payload_offset = _CHUNK_SIZES_OFFSET + chunk_table_size
    if payload_offset > len(body):
        raise CoreFormatError("Core Texture chunk table is truncated")
    chunk_sizes = struct.unpack_from(f"<{chunk_count}I", body, _CHUNK_SIZES_OFFSET)
    if chunk_sizes != _SUPPORTED_CHUNK_SIZES:
        raise CoreFormatError(
            "Unsupported Core Texture chunk-size layout"
        )

    image_size = width * height
    if sum(chunk_sizes[:-1]) != image_size:
        raise CoreFormatError(
            "Core Texture image chunks do not match its dimensions"
        )
    payload_size = sum(chunk_sizes)
    payload_end = payload_offset + payload_size
    expected_end = payload_end + _SUPPORTED_FOOTER_SIZE
    if len(body) != expected_end:
        raise CoreFormatError(
            f"Core Texture body size is 0x{len(body):X}, "
            f"expected 0x{expected_end:X}"
        )

    swizzled_indices = body[payload_offset : payload_offset + image_size]
    palette_offset = payload_offset + image_size
    raw_palette = body[palette_offset : palette_offset + _PALETTE_SIZE]
    auxiliary_offset = palette_offset + _PALETTE_SIZE
    auxiliary_end = auxiliary_offset + _SUPPORTED_AUXILIARY_SIZE
    auxiliary_payload = body[auxiliary_offset:auxiliary_end]
    if auxiliary_end != payload_end:
        raise CoreFormatError(
            "Core Texture palette chunk has an unsupported layout"
        )

    linear_indices = unswizzle_psmt8(swizzled_indices, width, height)
    rgba = decode_csm1_rgba(linear_indices, raw_palette)
    return NativeTexture(
        name=name,
        width=width,
        height=height,
        rgba=rgba,
        linear_indices=linear_indices,
        raw_palette=raw_palette,
        auxiliary_payload=auxiliary_payload,
        opaque_footer=body[payload_end:],
        header=header,
        source_sha256=hashlib.sha256(data).hexdigest(),
        original_core=data,
    )


def scan_native_textures(
    source_path: str | Path,
) -> tuple[NativeTextureAsset, ...]:
    assets: list[NativeTextureAsset] = []
    with KillzoneArchiveSource(source_path) as source:
        for entry in source.load_catalog().entries:
            if entry.path.suffix.casefold() != ".core":
                continue
            fixed = source.read_entry_prefix(entry, 24)
            required = core_header_prefix_size(fixed, entry.size)
            header = parse_core_header(
                source.read_entry_range(entry, 0, required),
                entry.size,
            )
            if header.variant != 1 or header.first_type != "Texture":
                continue

            texture = parse_native_texture(
                source.read_entry_range(entry, 0, entry.size)
            )
            assets.append(
                NativeTextureAsset(
                    path=entry.path.as_posix(),
                    name=texture.name,
                    size=entry.size,
                )
            )
    return tuple(sorted(assets, key=lambda asset: asset.path.casefold()))
