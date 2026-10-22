# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath
import struct
from typing import Mapping

from .errors import CatalogFormatError

_HEADER = struct.Struct("<6I")
_U32 = struct.Struct("<I")
_ENTRY = struct.Struct("<III")
_NAME_OFFSET_MASK = (1 << 23) - 1
_PACKAGE_MASK = 0x0F800000
_DIRECTORY_FLAG = 0x80000000

MAX_METADATA_SIZE = 64 * 1024 * 1024
MAX_NAME_TABLE_SIZE = 32 * 1024 * 1024
MAX_RECORDS = 1_000_000
MAX_DIRECTORY_DEPTH = 128


@dataclass(frozen=True, slots=True)
class CatalogHeader:
    metadata_size: int
    declared_file_count: int
    unknown_08: int
    unknown_0c: int
    name_table_size: int
    entry_table_size: int
    root_entry_count: int


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    path: PurePosixPath
    name_offset: int
    package_index: int
    offset: int
    size: int
    raw_name_flags: int
    table_offset: int


@dataclass(frozen=True, slots=True)
class CatalogDirectory:
    path: PurePosixPath
    name_offset: int
    child_table_offset: int
    child_count: int
    raw_name_flags: int
    table_offset: int


@dataclass(frozen=True, slots=True)
class KillzoneCatalog:
    header: CatalogHeader
    entries: tuple[CatalogEntry, ...]
    directories: tuple[CatalogDirectory, ...]
    metadata_sha256: str
    original_metadata: bytes

    def entry_by_path(self, path: str | PurePosixPath) -> CatalogEntry:
        requested = PurePosixPath(path).as_posix().casefold()
        for entry in self.entries:
            if entry.path.as_posix().casefold() == requested:
                return entry
        raise CatalogFormatError(f"Catalog has no file {path!s}")


def container_name(package_index: int) -> str:
    if package_index == 0:
        return "FILES.DAT"
    if 1 <= package_index <= 99:
        return f"FILES{package_index:02d}.DAT"
    raise CatalogFormatError(f"Unsupported package index {package_index}")


def _read_c_string(
    names: bytes,
    offset: int,
    cache: dict[int, str],
) -> str:
    cached = cache.get(offset)
    if cached is not None:
        return cached
    if not 0 <= offset < len(names):
        raise CatalogFormatError(
            f"Name offset 0x{offset:X} is outside the name table"
        )
    end = names.find(b"\x00", offset)
    if end < 0:
        raise CatalogFormatError(
            f"Name at offset 0x{offset:X} is not null-terminated"
        )
    raw_name = names[offset:end]
    try:
        name = raw_name.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CatalogFormatError(
            f"Name at offset 0x{offset:X} is not ASCII"
        ) from exc
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise CatalogFormatError(
            f"Unsafe catalog name at offset 0x{offset:X}: {name!r}"
        )
    cache[offset] = name
    return name


def parse_catalog(
    metadata: bytes,
    archive_sizes: Mapping[int, int],
) -> KillzoneCatalog:
    if len(metadata) < _HEADER.size + _U32.size:
        raise CatalogFormatError("FILES.DAT metadata header is truncated")
    (
        metadata_size,
        declared_file_count,
        unknown_08,
        unknown_0c,
        reserved_10,
        reserved_14,
    ) = _HEADER.unpack_from(metadata)
    if metadata_size > MAX_METADATA_SIZE:
        raise CatalogFormatError(
            f"Metadata size {metadata_size} exceeds {MAX_METADATA_SIZE}"
        )
    if len(metadata) != _HEADER.size + metadata_size:
        raise CatalogFormatError(
            f"Metadata buffer size is {len(metadata)}, expected "
            f"{_HEADER.size + metadata_size}"
        )
    if reserved_10 != 0 or reserved_14 != 0:
        raise CatalogFormatError(
            "Reserved FILES.DAT header fields at 0x10 and 0x14 must be zero"
        )
    if declared_file_count > MAX_RECORDS:
        raise CatalogFormatError(
            f"Declared file count {declared_file_count} exceeds {MAX_RECORDS}"
        )

    name_table_size = _U32.unpack_from(metadata, _HEADER.size)[0]
    if name_table_size > MAX_NAME_TABLE_SIZE:
        raise CatalogFormatError(
            f"Name table size {name_table_size} exceeds {MAX_NAME_TABLE_SIZE}"
        )
    names_start = _HEADER.size + _U32.size
    names_end = names_start + name_table_size
    if names_end + _U32.size > len(metadata):
        raise CatalogFormatError("FILES.DAT name table is truncated")
    names = metadata[names_start:names_end]

    entry_table_size = _U32.unpack_from(metadata, names_end)[0]
    table_start = names_end + _U32.size
    table_end = table_start + entry_table_size
    if table_end != len(metadata):
        raise CatalogFormatError(
            f"Entry table ends at 0x{table_end:X}, metadata ends at "
            f"0x{len(metadata):X}"
        )
    if entry_table_size < _U32.size:
        raise CatalogFormatError("Entry table is missing its root count")
    if (entry_table_size - _U32.size) % _ENTRY.size != 0:
        raise CatalogFormatError(
            "Entry table payload is not a multiple of 12 bytes"
        )

    total_records = (entry_table_size - _U32.size) // _ENTRY.size
    if total_records > MAX_RECORDS:
        raise CatalogFormatError(
            f"Catalog record count {total_records} exceeds {MAX_RECORDS}"
        )
    table = metadata[table_start:table_end]
    root_entry_count = _U32.unpack_from(table)[0]

    for package_index, size in archive_sizes.items():
        if type(package_index) is not int or not 0 <= package_index <= 31:
            raise CatalogFormatError(
                f"Invalid archive size key {package_index!r}"
            )
        if type(size) is not int or size < 0:
            raise CatalogFormatError(
                f"Invalid size for package {package_index}: {size!r}"
            )

    entries: list[CatalogEntry] = []
    directories: list[CatalogDirectory] = []
    claimed_record_offsets: set[int] = set()
    active_ranges: set[tuple[int, int]] = set()
    seen_paths: set[str] = set()
    name_cache: dict[int, str] = {}

    def walk(
        offset: int,
        count: int,
        parent: PurePosixPath,
        depth: int,
    ) -> None:
        if depth > MAX_DIRECTORY_DEPTH:
            raise CatalogFormatError(
                f"Directory depth exceeds {MAX_DIRECTORY_DEPTH}"
            )
        if count == 0:
            return
        if offset < _U32.size or (offset - _U32.size) % _ENTRY.size != 0:
            raise CatalogFormatError(
                f"Directory table offset 0x{offset:X} is not record-aligned"
            )
        if count > MAX_RECORDS or offset + count * _ENTRY.size > len(table):
            raise CatalogFormatError(
                f"Directory range 0x{offset:X}+{count} records is outside "
                "the entry table"
            )
        range_key = (offset, count)
        if range_key in active_ranges:
            raise CatalogFormatError(
                f"Directory table cycle at 0x{offset:X}"
            )
        active_ranges.add(range_key)
        try:
            for index in range(count):
                record_offset = offset + index * _ENTRY.size
                if record_offset in claimed_record_offsets:
                    raise CatalogFormatError(
                        f"Entry record at 0x{record_offset:X} is referenced "
                        "more than once"
                    )
                claimed_record_offsets.add(record_offset)

                name_flags, value_a, value_b = _ENTRY.unpack_from(
                    table, record_offset
                )
                name_offset = name_flags & _NAME_OFFSET_MASK
                name = _read_c_string(names, name_offset, name_cache)
                path = parent / name
                normalized_path = path.as_posix().casefold()
                if normalized_path in seen_paths:
                    raise CatalogFormatError(
                        f"Case-colliding catalog path: {path.as_posix()}"
                    )
                seen_paths.add(normalized_path)

                if name_flags & _DIRECTORY_FLAG:
                    directory = CatalogDirectory(
                        path=path,
                        name_offset=name_offset,
                        child_table_offset=value_a,
                        child_count=value_b,
                        raw_name_flags=name_flags,
                        table_offset=record_offset,
                    )
                    directories.append(directory)
                    walk(value_a, value_b, path, depth + 1)
                    continue

                unknown_file_flags = name_flags & ~(
                    _NAME_OFFSET_MASK | _PACKAGE_MASK
                )
                if unknown_file_flags:
                    raise CatalogFormatError(
                        f"File {path.as_posix()} has unknown flags "
                        f"0x{unknown_file_flags:08X}"
                    )
                package_index = (name_flags >> 23) & 0x1F
                archive_size = archive_sizes.get(package_index)
                if archive_size is None:
                    raise CatalogFormatError(
                        f"File {path.as_posix()} references missing package "
                        f"{package_index}"
                    )
                if value_a > archive_size or value_b > archive_size - value_a:
                    raise CatalogFormatError(
                        f"File {path.as_posix()} range "
                        f"0x{value_a:X}+0x{value_b:X} exceeds "
                        f"{container_name(package_index)} size "
                        f"0x{archive_size:X}"
                    )
                entries.append(
                    CatalogEntry(
                        path=path,
                        name_offset=name_offset,
                        package_index=package_index,
                        offset=value_a,
                        size=value_b,
                        raw_name_flags=name_flags,
                        table_offset=record_offset,
                    )
                )
        finally:
            active_ranges.remove(range_key)

    walk(_U32.size, root_entry_count, PurePosixPath(), 0)

    if len(entries) != declared_file_count:
        raise CatalogFormatError(
            f"Catalog contains {len(entries)} files, header declares "
            f"{declared_file_count}"
        )
    expected_offsets = set(
        range(_U32.size, entry_table_size, _ENTRY.size)
    )
    if claimed_record_offsets != expected_offsets:
        missing = sorted(expected_offsets - claimed_record_offsets)
        extra = sorted(claimed_record_offsets - expected_offsets)
        detail = []
        if missing:
            detail.append(f"first orphan record 0x{missing[0]:X}")
        if extra:
            detail.append(f"first invalid record 0x{extra[0]:X}")
        raise CatalogFormatError(
            "Entry table is not covered exactly"
            + (f": {', '.join(detail)}" if detail else "")
        )

    header = CatalogHeader(
        metadata_size=metadata_size,
        declared_file_count=declared_file_count,
        unknown_08=unknown_08,
        unknown_0c=unknown_0c,
        name_table_size=name_table_size,
        entry_table_size=entry_table_size,
        root_entry_count=root_entry_count,
    )
    return KillzoneCatalog(
        header=header,
        entries=tuple(entries),
        directories=tuple(directories),
        metadata_sha256=hashlib.sha256(metadata).hexdigest(),
        original_metadata=metadata,
    )
