# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import struct

from .archive_source import KillzoneArchiveSource
from .core_format import CoreHeader, core_header_prefix_size, parse_core_header
from .errors import CoreFormatError

CORE_STREAM_MAGIC = b"<CoreStreamData>"

_U32 = struct.Struct("<I")
_ROOT_RECORD = struct.Struct("<5I")
_PROPERTY_DESCRIPTOR = struct.Struct("<4I")
_MESH_RECORD = struct.Struct("<IIHBB6H")

_CORE_STREAM_HEADER_SIZE = 0x40
_ROOT_DATA_OFFSET = 0x11C
_ROOT_VALUE_BLOCK_SIZE = 0x44
_ROOT_CUSTOM_DATA_OFFSET = _ROOT_DATA_OFFSET + _ROOT_VALUE_BLOCK_SIZE
_OPAQUE_BLOCK_40_SIZE = 0x40
_OPAQUE_BLOCK_80_SIZE = 0x10
_MAX_STREAM_CHUNKS = 256
_MAX_MESH_RECORDS = 1_000_000
_MAX_PACKETS = 10_000_000

_ROOT_SCHEMA = (
    b"RegularStaticMeshResource",
    b"Sorting.Sort Order",
    b"Lighting.Receives Dynamic Shadows",
    b"Lighting.Cast Static Shadows",
    b"General.Linear storage",
    b"Lighting.Prelit",
    b"Sorting.Sort Mode",
    b"General.Name is identifier",
    b"General.Parent",
    b"Lighting.Cast Dynamic Shadows",
    b"General.Name",
    b"General.No streaming",
    b"Lighting.Lightmap Density Factor",
    b"Visibility.Maximum visible distance",
    b"Mesh Description.Bounding Box",
)

_ROOT_PROPERTIES = (
    (1, 0x5352544F, 0x10000004, 0xFFFFFFFF),
    (2, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
    (3, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
    (4, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
    (5, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
    (6, 0x5352544D, 0x10000004, 0xFFFFFFFF),
    (7, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
    (8, 0x43CF5245, 0x40000004, 0xFFFFFFFF),
    (9, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
    (10, 0x53545220, 0xFFFFFFFF, 0xFFFFFFFF),
    (11, 0x424F4F4C, 0x10000001, 0xFFFFFFFF),
    (12, 0x464C4F41, 0x40000004, 0x1C),
    (13, 0x464C4F41, 0x40000004, 0x20),
    (14, 0x42424F58, 0x40000018, 0x28),
)


@dataclass(frozen=True, slots=True)
class NativeStaticMeshRecord:
    opaque_reference: int
    opaque_word: int
    packet_count: int
    flag_a: int
    flag_b: int
    opaque_u16: tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class NativeStaticMeshPacket:
    mesh_record_index: int
    packet_index: int
    stream_chunk_index: int
    relative_offset: int
    absolute_offset: int
    bounded_size: int
    opaque_metadata: bytes
    first_word: int
    span_sha256: str


@dataclass(frozen=True, slots=True)
class NativeStaticMesh:
    header: CoreHeader
    stream_chunk_sizes: tuple[int, ...]
    records: tuple[NativeStaticMeshRecord, ...]
    packets: tuple[NativeStaticMeshPacket, ...]
    root_flags: tuple[bool, bool]
    opaque_root_values: bytes
    custom_alignment_padding: bytes
    opaque_block_40: bytes
    opaque_block_80: bytes
    table_end_offset: int
    static_stream_end_offset: int
    corestream_trailing_size: int
    source_core_sha256: str
    source_corestream_sha256: str


@dataclass(frozen=True, slots=True)
class NativeStaticMeshAsset:
    path: str
    corestream_path: str
    core_size: int
    corestream_size: int
    layout: NativeStaticMesh


@dataclass(frozen=True, slots=True)
class _PacketReference:
    mesh_record_index: int
    packet_index: int
    stream_chunk_index: int
    relative_offset: int
    absolute_offset: int
    opaque_metadata: bytes


def _require_range(
    data: bytes,
    offset: int,
    size: int,
    label: str,
) -> None:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise CoreFormatError(f"{label} is truncated")


def _read_u32(data: bytes, offset: int, label: str) -> int:
    _require_range(data, offset, _U32.size, label)
    return _U32.unpack_from(data, offset)[0]


def _checked_table_end(
    data: bytes,
    offset: int,
    count: int,
    item_size: int,
    label: str,
) -> int:
    if count < 0 or count > (len(data) - offset) // item_size:
        raise CoreFormatError(f"{label} exceeds its containing data")
    return offset + count * item_size


def _validate_root_schema(header: CoreHeader, body: bytes) -> int:
    if len(header.strings) < len(_ROOT_SCHEMA):
        raise CoreFormatError(
            "Core static mesh string table is missing root schema symbols"
        )
    actual_schema = tuple(
        value.raw for value in header.strings[: len(_ROOT_SCHEMA)]
    )
    if actual_schema != _ROOT_SCHEMA:
        raise CoreFormatError("Unsupported Core static mesh root schema")

    _require_range(body, 0, _ROOT_RECORD.size, "Core static mesh root record")
    root_record = _ROOT_RECORD.unpack_from(body)
    expected_record = (
        _ROOT_DATA_OFFSET,
        0,
        1,
        0,
        len(_ROOT_PROPERTIES),
    )
    if root_record != expected_record:
        raise CoreFormatError("Unsupported Core static mesh root record")

    descriptor_offset = _ROOT_RECORD.size
    descriptor_end = _checked_table_end(
        body,
        descriptor_offset,
        len(_ROOT_PROPERTIES),
        _PROPERTY_DESCRIPTOR.size,
        "Core static mesh property descriptors",
    )
    for index, expected in enumerate(_ROOT_PROPERTIES):
        actual = _PROPERTY_DESCRIPTOR.unpack_from(
            body,
            descriptor_offset + index * _PROPERTY_DESCRIPTOR.size,
        )
        if actual != expected:
            raise CoreFormatError(
                f"Unsupported Core static mesh property descriptor {index}"
            )
    if descriptor_end > _ROOT_DATA_OFFSET:
        raise CoreFormatError(
            "Core static mesh descriptors overlap the root value block"
        )
    _require_range(
        body,
        _ROOT_DATA_OFFSET,
        _ROOT_VALUE_BLOCK_SIZE,
        "Core static mesh root value block",
    )
    return _ROOT_CUSTOM_DATA_OFFSET


def _parse_root_custom_data(
    body: bytes,
    offset: int,
) -> tuple[
    tuple[int, ...],
    tuple[NativeStaticMeshRecord, ...],
    tuple[bool, bool],
    bytes,
    bytes,
    bytes,
    int,
]:
    stream_chunk_count = _read_u32(
        body, offset, "Core static mesh stream chunk count"
    )
    if not 1 <= stream_chunk_count <= _MAX_STREAM_CHUNKS:
        raise CoreFormatError(
            "Unsupported Core static mesh stream chunk count "
            f"{stream_chunk_count}"
        )
    offset += _U32.size
    chunk_table_end = _checked_table_end(
        body,
        offset,
        stream_chunk_count,
        _U32.size,
        "Core static mesh stream chunk table",
    )
    stream_chunk_sizes = struct.unpack_from(
        f"<{stream_chunk_count}I", body, offset
    )
    if any(size == 0 for size in stream_chunk_sizes):
        raise CoreFormatError("Core static mesh has an empty stream chunk")
    offset = (chunk_table_end + 3) & ~3

    mesh_record_count = _read_u32(
        body, offset, "Core static mesh record count"
    )
    if not 1 <= mesh_record_count <= _MAX_MESH_RECORDS:
        raise CoreFormatError(
            f"Unsupported Core static mesh record count {mesh_record_count}"
        )
    offset += _U32.size
    record_table_end = _checked_table_end(
        body,
        offset,
        mesh_record_count,
        _MESH_RECORD.size,
        "Core static mesh record table",
    )

    records: list[NativeStaticMeshRecord] = []
    packet_count = 0
    for index in range(mesh_record_count):
        values = _MESH_RECORD.unpack_from(
            body, offset + index * _MESH_RECORD.size
        )
        record = NativeStaticMeshRecord(
            opaque_reference=values[0],
            opaque_word=values[1],
            packet_count=values[2],
            flag_a=values[3],
            flag_b=values[4],
            opaque_u16=values[5:],
        )
        packet_count += record.packet_count
        if packet_count > _MAX_PACKETS:
            raise CoreFormatError(
                f"Core static mesh packet count exceeds {_MAX_PACKETS}"
            )
        records.append(record)
    offset = record_table_end

    _require_range(body, offset, 2, "Core static mesh root flags")
    flag_values = body[offset : offset + 2]
    if any(value not in (0, 1) for value in flag_values):
        raise CoreFormatError("Core static mesh root flags are not boolean")
    root_flags = (bool(flag_values[0]), bool(flag_values[1]))
    offset += 2

    aligned_offset = (offset + 3) & ~3
    _require_range(
        body,
        offset,
        aligned_offset - offset,
        "Core static mesh custom alignment",
    )
    alignment_padding = body[offset:aligned_offset]
    offset = aligned_offset

    _require_range(
        body,
        offset,
        _OPAQUE_BLOCK_40_SIZE,
        "Core static mesh opaque block at runtime offset 0x40",
    )
    opaque_block_40 = body[offset : offset + _OPAQUE_BLOCK_40_SIZE]
    offset += _OPAQUE_BLOCK_40_SIZE
    _require_range(
        body,
        offset,
        _OPAQUE_BLOCK_80_SIZE,
        "Core static mesh opaque block at runtime offset 0x80",
    )
    opaque_block_80 = body[offset : offset + _OPAQUE_BLOCK_80_SIZE]
    offset += _OPAQUE_BLOCK_80_SIZE
    return (
        tuple(stream_chunk_sizes),
        tuple(records),
        root_flags,
        alignment_padding,
        opaque_block_40,
        opaque_block_80,
        offset,
    )


def _stream_chunk_ranges(
    data: bytes,
    chunk_sizes: tuple[int, ...],
) -> tuple[tuple[int, int], ...]:
    _require_range(
        data,
        0,
        _CORE_STREAM_HEADER_SIZE,
        "CoreStream fixed header",
    )
    if data[: len(CORE_STREAM_MAGIC)] != CORE_STREAM_MAGIC:
        raise CoreFormatError(
            f"Invalid CoreStream magic {data[:len(CORE_STREAM_MAGIC)]!r}"
        )
    if any(data[len(CORE_STREAM_MAGIC) : _CORE_STREAM_HEADER_SIZE]):
        raise CoreFormatError("CoreStream reserved header bytes are non-zero")

    ranges: list[tuple[int, int]] = []
    offset = _CORE_STREAM_HEADER_SIZE
    for index, size in enumerate(chunk_sizes):
        if size > len(data) - offset:
            raise CoreFormatError(
                f"Core static mesh stream chunk {index} exceeds CoreStream data"
            )
        end = offset + size
        ranges.append((offset, end))
        offset = end
    return tuple(ranges)


def _parse_packet_table(
    data: bytes,
    records: tuple[NativeStaticMeshRecord, ...],
    chunk_ranges: tuple[tuple[int, int], ...],
) -> tuple[tuple[NativeStaticMeshPacket, ...], int]:
    table_offset = chunk_ranges[0][0]
    first_chunk_end = chunk_ranges[0][1]
    references: list[_PacketReference] = []

    for record_index, record in enumerate(records):
        table_offset = (table_offset + 3) & ~3
        offsets_end = table_offset + record.packet_count * _U32.size
        metadata_end = offsets_end + record.packet_count * 12
        indices_end = metadata_end + record.packet_count
        if indices_end > first_chunk_end:
            raise CoreFormatError(
                f"Core static mesh packet table for record {record_index} "
                "exceeds the first stream chunk"
            )

        offsets = struct.unpack_from(
            f"<{record.packet_count}I", data, table_offset
        )
        for packet_index, relative_offset in enumerate(offsets):
            if relative_offset & 0xF:
                raise CoreFormatError(
                    "Core static mesh packet offset is not 16-byte aligned"
                )
            stream_chunk_index = data[metadata_end + packet_index]
            if stream_chunk_index >= len(chunk_ranges):
                raise CoreFormatError(
                    "Core static mesh packet references stream chunk "
                    f"{stream_chunk_index}, but only {len(chunk_ranges)} exist"
                )
            chunk_start, chunk_end = chunk_ranges[stream_chunk_index]
            chunk_size = chunk_end - chunk_start
            if relative_offset > chunk_size - _U32.size:
                raise CoreFormatError(
                    "Core static mesh packet offset exceeds its stream chunk"
                )
            absolute_offset = chunk_start + relative_offset
            references.append(
                _PacketReference(
                    mesh_record_index=record_index,
                    packet_index=packet_index,
                    stream_chunk_index=stream_chunk_index,
                    relative_offset=relative_offset,
                    absolute_offset=absolute_offset,
                    opaque_metadata=data[
                        offsets_end + packet_index * 12 :
                        offsets_end + (packet_index + 1) * 12
                    ],
                )
            )
        table_offset = indices_end

    first_chunk_start = chunk_ranges[0][0]
    table_relative_end = table_offset - first_chunk_start
    if any(
        reference.stream_chunk_index == 0
        and reference.relative_offset < table_relative_end
        for reference in references
    ):
        raise CoreFormatError(
            "Core static mesh packet overlaps its packet table"
        )

    starts_by_chunk: dict[int, list[int]] = {}
    for reference in references:
        starts_by_chunk.setdefault(
            reference.stream_chunk_index, []
        ).append(reference.relative_offset)
    packet_ends: dict[tuple[int, int], int] = {}
    for chunk_index, starts in starts_by_chunk.items():
        ordered = sorted(starts)
        if len(ordered) != len(set(ordered)):
            raise CoreFormatError(
                "Core static mesh contains duplicate packet offsets"
            )
        chunk_start, chunk_end = chunk_ranges[chunk_index]
        chunk_size = chunk_end - chunk_start
        for index, start in enumerate(ordered):
            packet_ends[(chunk_index, start)] = (
                ordered[index + 1]
                if index + 1 < len(ordered)
                else chunk_size
            )

    packets: list[NativeStaticMeshPacket] = []
    for reference in references:
        relative_end = packet_ends[
            (reference.stream_chunk_index, reference.relative_offset)
        ]
        bounded_size = relative_end - reference.relative_offset
        span = data[
            reference.absolute_offset :
            reference.absolute_offset + bounded_size
        ]
        packets.append(
            NativeStaticMeshPacket(
                mesh_record_index=reference.mesh_record_index,
                packet_index=reference.packet_index,
                stream_chunk_index=reference.stream_chunk_index,
                relative_offset=reference.relative_offset,
                absolute_offset=reference.absolute_offset,
                bounded_size=bounded_size,
                opaque_metadata=reference.opaque_metadata,
                first_word=_U32.unpack_from(data, reference.absolute_offset)[0],
                span_sha256=hashlib.sha256(span).hexdigest(),
            )
        )
    return tuple(packets), table_offset


def parse_native_static_mesh(
    core_data: bytes,
    corestream_data: bytes,
) -> NativeStaticMesh:
    if len(core_data) < 24:
        raise CoreFormatError("Core static mesh fixed header is truncated")
    required = core_header_prefix_size(core_data[:24], len(core_data))
    header = parse_core_header(core_data[:required], len(core_data))
    if (
        header.variant != 5
        or header.first_type != "RegularStaticMeshResource"
    ):
        raise CoreFormatError(
            "Only paired Core variant-5 RegularStaticMeshResource roots "
            "are supported"
        )

    body = core_data[header.body_offset :]
    custom_offset = _validate_root_schema(header, body)
    (
        stream_chunk_sizes,
        records,
        root_flags,
        alignment_padding,
        opaque_block_40,
        opaque_block_80,
        _,
    ) = _parse_root_custom_data(body, custom_offset)
    chunk_ranges = _stream_chunk_ranges(corestream_data, stream_chunk_sizes)
    packets, table_end_offset = _parse_packet_table(
        corestream_data,
        records,
        chunk_ranges,
    )
    static_stream_end_offset = chunk_ranges[-1][1]

    return NativeStaticMesh(
        header=header,
        stream_chunk_sizes=stream_chunk_sizes,
        records=records,
        packets=packets,
        root_flags=root_flags,
        opaque_root_values=body[
            _ROOT_DATA_OFFSET:_ROOT_CUSTOM_DATA_OFFSET
        ],
        custom_alignment_padding=alignment_padding,
        opaque_block_40=opaque_block_40,
        opaque_block_80=opaque_block_80,
        table_end_offset=table_end_offset,
        static_stream_end_offset=static_stream_end_offset,
        corestream_trailing_size=(
            len(corestream_data) - static_stream_end_offset
        ),
        source_core_sha256=hashlib.sha256(core_data).hexdigest(),
        source_corestream_sha256=hashlib.sha256(
            corestream_data
        ).hexdigest(),
    )


def scan_native_static_meshes(
    source_path: str | Path,
) -> tuple[NativeStaticMeshAsset, ...]:
    assets: list[NativeStaticMeshAsset] = []
    with KillzoneArchiveSource(source_path) as source:
        catalog = source.load_catalog()
        entries_by_path = {
            entry.path.as_posix().casefold(): entry
            for entry in catalog.entries
        }
        for entry in sorted(
            catalog.entries,
            key=lambda candidate: candidate.path.as_posix(),
        ):
            if entry.path.suffix.lower() != ".core":
                continue
            fixed = source.read_entry_prefix(entry, 24)
            required = core_header_prefix_size(fixed, entry.size)
            header = parse_core_header(
                source.read_entry_range(entry, 0, required),
                entry.size,
            )
            if header.first_type != "RegularStaticMeshResource":
                continue

            stream_path = entry.path.with_suffix(
                ".corestream"
            ).as_posix()
            stream_entry = entries_by_path.get(stream_path.casefold())
            if stream_entry is None:
                raise CoreFormatError(
                    f"{entry.path.as_posix()}: paired CoreStream is missing"
                )
            core_data = source.read_entry_range(entry, 0, entry.size)
            corestream_data = source.read_entry_range(
                stream_entry, 0, stream_entry.size
            )
            try:
                layout = parse_native_static_mesh(
                    core_data, corestream_data
                )
            except CoreFormatError as exc:
                raise CoreFormatError(
                    f"{entry.path.as_posix()}: {exc}"
                ) from exc
            assets.append(
                NativeStaticMeshAsset(
                    path=entry.path.as_posix(),
                    corestream_path=stream_path,
                    core_size=entry.size,
                    corestream_size=stream_entry.size,
                    layout=layout,
                )
            )
    return tuple(assets)
