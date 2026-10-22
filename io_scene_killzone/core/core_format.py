# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct

from .errors import CoreFormatError

CORE_MAGIC = b"Core<1.79><PSX2>"
_FIXED_HEADER = struct.Struct("<16sII")
_U32 = struct.Struct("<I")
_SUPPORTED_VARIANTS = {1, 5}
MAX_STRING_TABLE_SIZE = 64 * 1024 * 1024
MAX_STRINGS = 1_000_000
MAX_STRING_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CoreString:
    raw: bytes
    utf8: str | None


@dataclass(frozen=True, slots=True)
class CoreHeader:
    variant: int
    string_table_size: int
    strings: tuple[CoreString, ...]
    string_table_padding: int
    body_offset: int
    total_size: int
    header_sha256: str
    original_header: bytes

    @property
    def first_type(self) -> str:
        if not self.strings:
            return ""
        try:
            return self.strings[0].raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise CoreFormatError(
                "Core first serialized type is not ASCII"
            ) from exc

    @property
    def root_type(self) -> str:
        return self.first_type


def core_header_prefix_size(fixed_header: bytes, total_size: int) -> int:
    if len(fixed_header) < _FIXED_HEADER.size:
        raise CoreFormatError("Core fixed header is truncated")
    magic, variant, string_table_size = _FIXED_HEADER.unpack_from(fixed_header)
    if magic != CORE_MAGIC:
        raise CoreFormatError(f"Invalid Core magic {magic!r}")
    if variant not in _SUPPORTED_VARIANTS:
        raise CoreFormatError(
            f"Unsupported Core variant {variant}; expected 1 or 5"
        )
    if string_table_size > MAX_STRING_TABLE_SIZE:
        raise CoreFormatError(
            f"Core string table size {string_table_size} exceeds "
            f"{MAX_STRING_TABLE_SIZE}"
        )
    required = _FIXED_HEADER.size + string_table_size
    if required > total_size:
        raise CoreFormatError(
            f"Core string table ends at 0x{required:X}, file size is "
            f"0x{total_size:X}"
        )
    return required


def parse_core_header(data: bytes, total_size: int) -> CoreHeader:
    required = core_header_prefix_size(data, total_size)
    if len(data) != required:
        raise CoreFormatError(
            f"Core header buffer size is {len(data)}, expected {required}"
        )
    _, variant, string_table_size = _FIXED_HEADER.unpack_from(data)
    payload = data[_FIXED_HEADER.size:required]
    if len(payload) < _U32.size:
        raise CoreFormatError("Core string table has no string count")

    string_count = _U32.unpack_from(payload)[0]
    if string_count > MAX_STRINGS:
        raise CoreFormatError(
            f"Core string count {string_count} exceeds {MAX_STRINGS}"
        )
    offset = _U32.size
    strings: list[CoreString] = []
    for index in range(string_count):
        if offset + _U32.size > len(payload):
            raise CoreFormatError(
                f"Core string {index} length is truncated"
            )
        length = _U32.unpack_from(payload, offset)[0]
        offset += _U32.size
        if length > MAX_STRING_SIZE:
            raise CoreFormatError(
                f"Core string {index} length {length} exceeds {MAX_STRING_SIZE}"
            )
        if offset + length > len(payload):
            raise CoreFormatError(f"Core string {index} data is truncated")
        raw_string = payload[offset : offset + length]
        if b"\x00" in raw_string:
            raise CoreFormatError(f"Core string {index} contains a null byte")
        try:
            utf8: str | None = raw_string.decode("utf-8")
        except UnicodeDecodeError:
            utf8 = None
        strings.append(CoreString(raw=raw_string, utf8=utf8))
        offset += length

        alignment = (-length) & 3
        if offset + alignment > len(payload):
            raise CoreFormatError(
                f"Core string {index} alignment padding is truncated"
            )
        if any(payload[offset : offset + alignment]):
            raise CoreFormatError(
                f"Core string {index} has non-zero alignment padding"
            )
        offset += alignment

    trailing_padding = len(payload) - offset
    if trailing_padding not in {0, 4, 8, 12}:
        raise CoreFormatError(
            f"Core string table has {trailing_padding} unexpected trailing bytes"
        )
    if any(payload[offset:]):
        raise CoreFormatError("Core string table has non-zero trailing padding")

    return CoreHeader(
        variant=variant,
        string_table_size=string_table_size,
        strings=tuple(strings),
        string_table_padding=trailing_padding,
        body_offset=required,
        total_size=total_size,
        header_sha256=hashlib.sha256(data).hexdigest(),
        original_header=data,
    )
