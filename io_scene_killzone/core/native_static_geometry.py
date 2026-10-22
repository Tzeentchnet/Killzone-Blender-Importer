# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import struct

from .errors import CoreFormatError

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_STATIC_RECORD = struct.Struct("<IIHBB6h")

_DMA_PREFIX_WORD_1 = 0x80000000
_VIF_STCYCL_44 = 0x01000404
_VIF_HEADER_UNPACK = 0x6C02C1D9
_PENDING_FINAL_UNPACK = 0x6501C1EE

_OP_POSITION = 0x69
_OP_NORMAL_ADC = 0x6F
_OP_COLOR = 0x6E
_OP_V2_16 = 0x65
_OP_V4_16 = 0x6D
_OP_V1_8 = 0x62
_OP_V2_8 = 0x66
_OP_V3_8 = 0x6A

_COORDINATE_WIDTHS = {
    _OP_V2_16: 4,
    _OP_V4_16: 8,
}
_AUXILIARY_WIDTHS = {
    _OP_V1_8: 1,
    _OP_V2_8: 2,
    _OP_V3_8: 3,
}

_MAX_STREAM_CHUNKS = 256
_MAX_MESH_RECORDS = 1_000_000
_MAX_PACKETS = 10_000_000
_MAX_PACKET_VERTICES = 256


@dataclass(frozen=True, slots=True)
class NativeStaticVertex:
    raw_position: tuple[int, int, int]
    position: tuple[float, float, float]
    normal: tuple[float, float, float]
    adc: bool
    color: tuple[int, int, int, int] | None
    raw_coordinates: tuple[tuple[int, int], ...]
    coordinates: tuple[tuple[float, float], ...]
    auxiliary: bytes | None


@dataclass(frozen=True, slots=True)
class NativeStaticPacket:
    mesh_record_index: int
    packet_index: int
    stream_chunk_index: int
    relative_offset: int
    absolute_offset: int
    bounded_size: int
    transfer_size: int
    raw_bounds: tuple[int, int, int, int, int, int]
    tail_opcodes: tuple[int, ...]
    zero_padding_size: int
    vertices: tuple[NativeStaticVertex, ...]
    triangles: tuple[tuple[int, int, int], ...]
    span_sha256: str


@dataclass(frozen=True, slots=True)
class NativeStaticMeshRecord:
    material_id: int
    triangle_count: int
    packet_count: int
    flag_a: int
    attribute_flags: int
    raw_bounds: tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class NativeRegularStaticResource:
    object_id: int
    records: tuple[NativeStaticMeshRecord, ...]
    packets: tuple[NativeStaticPacket, ...]
    stream_chunk_sizes: tuple[int, ...]
    root_flags: tuple[bool, bool]
    position_matrix: tuple[float, ...]
    position_scale: tuple[float, float, float]
    position_translation: tuple[float, float, float]
    blob_tail: bytes

    @property
    def triangle_count(self) -> int:
        return sum(record.triangle_count for record in self.records)


@dataclass(frozen=True, slots=True)
class _PacketReference:
    mesh_record_index: int
    packet_index: int
    stream_chunk_index: int
    relative_offset: int
    absolute_offset: int
    raw_bounds: tuple[int, int, int, int, int, int]


def _require_range(data: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise CoreFormatError(f"{label} is truncated")


def _align4(value: int) -> int:
    return (value + 3) & ~3


def _vif_count(word: int) -> int:
    value = (word >> 16) & 0xFF
    return 256 if value == 0 else value


def _vif_address(word: int) -> int:
    return word & 0x3FF


def _normalized_packed_normal(word: int) -> tuple[float, float, float]:
    x = ((word >> 0) & 0x1F) - 15
    y = ((word >> 5) & 0x1F) - 15
    z = ((word >> 10) & 0x1F) - 15
    length = math.sqrt(float(x * x + y * y + z * z))
    if length == 0.0:
        raise CoreFormatError("Static packet contains a zero packed normal")
    return (x / length, y / length, z / length)


def _raw_bounds(
    positions: tuple[tuple[int, int, int], ...],
) -> tuple[int, int, int, int, int, int]:
    if not positions:
        raise CoreFormatError("Static packet contains no positions")
    return (
        min(value[0] for value in positions),
        min(value[1] for value in positions),
        min(value[2] for value in positions),
        max(value[0] for value in positions),
        max(value[1] for value in positions),
        max(value[2] for value in positions),
    )


def decode_static_packet(
    packet_data: bytes,
    *,
    mesh_record_index: int = 0,
    packet_index: int = 0,
    stream_chunk_index: int = 0,
    relative_offset: int = 0,
    absolute_offset: int = 0,
    bounded_size: int | None = None,
    raw_bounds: tuple[int, int, int, int, int, int] | None = None,
    position_scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
    position_translation: tuple[float, float, float] = (0.0, 0.0, 0.0),
    color_expected: bool | None = None,
) -> NativeStaticPacket:
    if bounded_size is None:
        bounded_size = len(packet_data)
    if bounded_size < 0 or bounded_size > len(packet_data):
        raise CoreFormatError("Static packet bound exceeds supplied data")
    _require_range(packet_data, 0, 0x30, "Static packet fixed header")

    first_word, second_word, stcycl, header_unpack = struct.unpack_from(
        "<4I", packet_data, 0
    )
    if first_word & 0xFFFF0000 != 0x60000000:
        raise CoreFormatError("Static packet does not begin with a DMA RET tag")
    if second_word != _DMA_PREFIX_WORD_1:
        raise CoreFormatError("Static packet has an unexpected DMA tag word")
    if stcycl != _VIF_STCYCL_44 or header_unpack != _VIF_HEADER_UNPACK:
        raise CoreFormatError("Static packet has an unexpected VIF prefix")

    qwc = first_word & 0xFFFF
    transfer_size = 0x10 + 0x10 * qwc
    if transfer_size > bounded_size:
        raise CoreFormatError(
            "Static packet DMA transfer exceeds its directory-derived bound"
        )
    _require_range(
        packet_data,
        0,
        transfer_size,
        "Static packet DMA transfer",
    )

    header_a = struct.unpack_from("<4I", packet_data, 0x10)
    header_b = struct.unpack_from("<4I", packet_data, 0x20)
    vertex_count = header_a[0]
    if not 1 <= vertex_count <= _MAX_PACKET_VERTICES:
        raise CoreFormatError(
            f"Unsupported static packet vertex count {vertex_count}"
        )
    aligned_vertex_count = _align4(vertex_count)
    if header_a[1] != 3 * vertex_count:
        raise CoreFormatError("Static packet header has invalid 3*N value")
    if header_a[2] != 3 * aligned_vertex_count:
        raise CoreFormatError("Static packet header has invalid aligned value")
    if header_b[0] != 0xB7:
        raise CoreFormatError("Static packet position address is not 0xB7")
    if header_b[1] != header_b[0] + vertex_count:
        raise CoreFormatError("Static packet normal address is inconsistent")
    if header_b[2] != header_b[1] + vertex_count:
        raise CoreFormatError("Static packet tail address is inconsistent")

    raw_positions: tuple[tuple[int, int, int], ...] | None = None
    normal_words: tuple[int, ...] | None = None
    colors: tuple[tuple[int, int, int, int], ...] | None = None
    coordinate_values: list[list[tuple[int, int]]] = [
        [] for _ in range(vertex_count)
    ]
    auxiliary: tuple[bytes, ...] | None = None

    opcodes: list[int] = []
    cursor = 0x30
    zero_padding_size = 0
    saw_padding = False
    saw_pending = False
    saw_auxiliary = False
    coordinate_address = header_b[3]

    while cursor < transfer_size:
        _require_range(packet_data, cursor, 4, "Static packet VIF command")
        word = _U32.unpack_from(packet_data, cursor)[0]
        if word == _PENDING_FINAL_UNPACK:
            if cursor + 4 != transfer_size:
                raise CoreFormatError(
                    "Static packet final pending unpack is not the last word"
                )
            cursor += 4
            saw_pending = True
            break
        if word == 0:
            saw_padding = True
            zero_padding_size += 4
            cursor += 4
            continue
        if saw_padding:
            raise CoreFormatError(
                "Static packet has an attribute command after VIF padding"
            )

        opcode = (word >> 24) & 0x7F
        count = _vif_count(word)
        address = _vif_address(word)
        if count != vertex_count:
            raise CoreFormatError(
                "Static packet attribute count does not match vertex count"
            )
        cursor += 4

        if not opcodes:
            if opcode != _OP_POSITION or address != header_b[0]:
                raise CoreFormatError(
                    "Static packet first attribute is not position V3-16"
                )
            width = 6
        elif len(opcodes) == 1:
            if opcode != _OP_NORMAL_ADC or address != header_b[1]:
                raise CoreFormatError(
                    "Static packet second attribute is not normal/ADC V4-5"
                )
            width = 2
        elif opcode == _OP_COLOR:
            if (
                colors is not None
                or coordinate_values[0]
                or saw_auxiliary
                or address != header_b[2]
            ):
                raise CoreFormatError(
                    "Static packet color command violates tail ordering"
                )
            width = 4
        elif opcode in _COORDINATE_WIDTHS:
            if saw_auxiliary or address != coordinate_address:
                raise CoreFormatError(
                    "Static packet coordinate command has invalid destination"
                )
            width = _COORDINATE_WIDTHS[opcode]
            coordinate_address += vertex_count
        elif opcode in _AUXILIARY_WIDTHS:
            if saw_auxiliary or address != header_a[3]:
                raise CoreFormatError(
                    "Static packet auxiliary command has invalid destination"
                )
            width = _AUXILIARY_WIDTHS[opcode]
            saw_auxiliary = True
        else:
            raise CoreFormatError(
                f"Unsupported static packet VIF opcode 0x{opcode:02X}"
            )

        payload_size = count * width
        aligned_payload_size = _align4(payload_size)
        _require_range(
            packet_data,
            cursor,
            aligned_payload_size,
            "Static packet attribute payload",
        )
        payload = packet_data[cursor : cursor + payload_size]
        if any(packet_data[cursor + payload_size : cursor + aligned_payload_size]):
            raise CoreFormatError(
                "Static packet attribute alignment padding is non-zero"
            )

        if opcode == _OP_POSITION:
            values = struct.unpack_from(f"<{vertex_count * 3}h", payload)
            raw_positions = tuple(
                (values[index], values[index + 1], values[index + 2])
                for index in range(0, len(values), 3)
            )
        elif opcode == _OP_NORMAL_ADC:
            normal_words = struct.unpack_from(f"<{vertex_count}H", payload)
        elif opcode == _OP_COLOR:
            colors = tuple(
                (
                    payload[index],
                    payload[index + 1],
                    payload[index + 2],
                    payload[index + 3],
                )
                for index in range(0, len(payload), 4)
            )
        elif opcode == _OP_V2_16:
            values = struct.unpack_from(f"<{vertex_count * 2}h", payload)
            for vertex_index in range(vertex_count):
                base = vertex_index * 2
                coordinate_values[vertex_index].append(
                    (values[base], values[base + 1])
                )
        elif opcode == _OP_V4_16:
            values = struct.unpack_from(f"<{vertex_count * 4}h", payload)
            for vertex_index in range(vertex_count):
                base = vertex_index * 4
                coordinate_values[vertex_index].extend(
                    (
                        (values[base], values[base + 1]),
                        (values[base + 2], values[base + 3]),
                    )
                )
        elif opcode in _AUXILIARY_WIDTHS:
            auxiliary = tuple(
                payload[index * width : (index + 1) * width]
                for index in range(vertex_count)
            )

        opcodes.append(opcode)
        cursor += aligned_payload_size

    if cursor != transfer_size or not saw_pending:
        raise CoreFormatError("Static packet is missing its final pending unpack")
    if raw_positions is None or normal_words is None:
        raise CoreFormatError("Static packet is missing position or normal data")
    if coordinate_address != header_a[3]:
        raise CoreFormatError(
            "Static packet coordinate arrays do not end at header_a[3]"
        )

    has_color = colors is not None
    expected_uv_address = header_b[2] + (vertex_count if has_color else 0)
    if header_b[3] != expected_uv_address:
        raise CoreFormatError("Static packet UV address is inconsistent")
    if color_expected is not None and has_color != color_expected:
        raise CoreFormatError(
            "Static packet color presence disagrees with mesh record flags"
        )

    actual_raw_bounds = _raw_bounds(raw_positions)
    if raw_bounds is not None and actual_raw_bounds != raw_bounds:
        raise CoreFormatError(
            "Static packet position extrema do not match directory bounds"
        )
    if raw_bounds is None:
        raw_bounds = actual_raw_bounds

    vertices: list[NativeStaticVertex] = []
    for index, raw_position in enumerate(raw_positions):
        position = tuple(
            raw_position[axis] * position_scale[axis]
            + position_translation[axis]
            for axis in range(3)
        )
        raw_coordinate_tuple = tuple(coordinate_values[index])
        vertices.append(
            NativeStaticVertex(
                raw_position=raw_position,
                position=position,
                normal=_normalized_packed_normal(normal_words[index]),
                adc=bool(normal_words[index] & 0x8000),
                color=colors[index] if colors is not None else None,
                raw_coordinates=raw_coordinate_tuple,
                coordinates=tuple(
                    (value[0] / 4096.0, value[1] / 4096.0)
                    for value in raw_coordinate_tuple
                ),
                auxiliary=(
                    auxiliary[index] if auxiliary is not None else None
                ),
            )
        )

    triangles: list[tuple[int, int, int]] = []
    for index in range(2, vertex_count):
        if vertices[index].adc:
            continue
        if index & 1:
            triangles.append((index - 1, index - 2, index))
        else:
            triangles.append((index - 2, index - 1, index))

    return NativeStaticPacket(
        mesh_record_index=mesh_record_index,
        packet_index=packet_index,
        stream_chunk_index=stream_chunk_index,
        relative_offset=relative_offset,
        absolute_offset=absolute_offset,
        bounded_size=bounded_size,
        transfer_size=transfer_size,
        raw_bounds=raw_bounds,
        tail_opcodes=tuple(opcodes),
        zero_padding_size=zero_padding_size,
        vertices=tuple(vertices),
        triangles=tuple(triangles),
        span_sha256=hashlib.sha256(packet_data[:bounded_size]).hexdigest(),
    )


def _parse_records(
    blob: bytes,
    offset: int,
) -> tuple[
    tuple[NativeStaticMeshRecord, ...],
    int,
]:
    _require_range(blob, offset, 4, "Static resource mesh record count")
    record_count = _U32.unpack_from(blob, offset)[0]
    if not 1 <= record_count <= _MAX_MESH_RECORDS:
        raise CoreFormatError(
            f"Unsupported static resource record count {record_count}"
        )
    offset += 4
    _require_range(
        blob,
        offset,
        record_count * _STATIC_RECORD.size,
        "Static resource mesh records",
    )
    records: list[NativeStaticMeshRecord] = []
    packet_count = 0
    for index in range(record_count):
        values = _STATIC_RECORD.unpack_from(
            blob, offset + index * _STATIC_RECORD.size
        )
        record = NativeStaticMeshRecord(
            material_id=values[0],
            triangle_count=values[1],
            packet_count=values[2],
            flag_a=values[3],
            attribute_flags=values[4],
            raw_bounds=values[5:],
        )
        packet_count += record.packet_count
        if packet_count > _MAX_PACKETS:
            raise CoreFormatError("Static resource packet count is excessive")
        records.append(record)
    return tuple(records), offset + record_count * _STATIC_RECORD.size


def parse_regular_static_resource(
    blob: bytes,
    corestream_data: bytes,
    *,
    object_id: int,
    stream_offset: int,
    stream_size: int,
) -> NativeRegularStaticResource:
    custom_offset = 0x40
    _require_range(blob, custom_offset, 4, "Static resource custom block")
    cursor = custom_offset
    chunk_count = _U32.unpack_from(blob, cursor)[0]
    if not 1 <= chunk_count <= _MAX_STREAM_CHUNKS:
        raise CoreFormatError(
            f"Unsupported static resource chunk count {chunk_count}"
        )
    cursor += 4
    _require_range(
        blob,
        cursor,
        chunk_count * 4,
        "Static resource chunk sizes",
    )
    chunk_sizes = struct.unpack_from(f"<{chunk_count}I", blob, cursor)
    if any(size == 0 for size in chunk_sizes):
        raise CoreFormatError("Static resource contains an empty stream chunk")
    if sum(chunk_sizes) != stream_size:
        raise CoreFormatError(
            "Static resource chunk sizes do not equal owned stream size"
        )
    cursor = _align4(cursor + chunk_count * 4)

    records, cursor = _parse_records(blob, cursor)
    _require_range(blob, cursor, 2, "Static resource root flags")
    flag_values = blob[cursor : cursor + 2]
    if any(value not in (0, 1) for value in flag_values):
        raise CoreFormatError("Static resource root flags are not boolean")
    root_flags = (bool(flag_values[0]), bool(flag_values[1]))
    cursor = _align4(cursor + 2)

    _require_range(blob, cursor, 0x50, "Static resource decode transform")
    position_matrix = struct.unpack_from("<16f", blob, cursor)
    position_scale4 = struct.unpack_from("<4f", blob, cursor + 0x40)
    if not all(math.isfinite(value) for value in position_matrix + position_scale4):
        raise CoreFormatError("Static resource transform is not finite")
    position_scale = position_scale4[:3]
    position_translation = position_matrix[12:15]
    cursor += 0x50

    if (
        stream_offset < 0
        or stream_size < 0
        or stream_offset > len(corestream_data)
        or stream_size > len(corestream_data) - stream_offset
    ):
        raise CoreFormatError("Static resource owned stream range is invalid")

    chunk_ranges: list[tuple[int, int]] = []
    chunk_cursor = stream_offset
    for size in chunk_sizes:
        chunk_ranges.append((chunk_cursor, chunk_cursor + size))
        chunk_cursor += size

    table_cursor = chunk_ranges[0][0]
    first_chunk_end = chunk_ranges[0][1]
    references: list[_PacketReference] = []
    for record_index, record in enumerate(records):
        table_cursor = _align4(table_cursor)
        offsets_end = table_cursor + record.packet_count * 4
        bounds_end = offsets_end + record.packet_count * 12
        indices_end = bounds_end + record.packet_count
        if indices_end > first_chunk_end:
            raise CoreFormatError(
                f"Static packet table for record {record_index} exceeds "
                "the first owned stream chunk"
            )
        offsets = struct.unpack_from(
            f"<{record.packet_count}I", corestream_data, table_cursor
        )
        for packet_index, relative_offset in enumerate(offsets):
            if relative_offset & 0xF:
                raise CoreFormatError(
                    "Static resource packet offset is not 16-byte aligned"
                )
            chunk_index = corestream_data[bounds_end + packet_index]
            if chunk_index >= len(chunk_ranges):
                raise CoreFormatError(
                    "Static resource packet references an invalid stream chunk"
                )
            chunk_start, chunk_end = chunk_ranges[chunk_index]
            if relative_offset > chunk_end - chunk_start - 4:
                raise CoreFormatError(
                    "Static resource packet offset exceeds its stream chunk"
                )
            references.append(
                _PacketReference(
                    mesh_record_index=record_index,
                    packet_index=packet_index,
                    stream_chunk_index=chunk_index,
                    relative_offset=relative_offset,
                    absolute_offset=chunk_start + relative_offset,
                    raw_bounds=struct.unpack_from(
                        "<6h",
                        corestream_data,
                        offsets_end + packet_index * 12,
                    ),
                )
            )
        table_cursor = indices_end

    table_relative_end = table_cursor - chunk_ranges[0][0]
    if any(
        reference.stream_chunk_index == 0
        and reference.relative_offset < table_relative_end
        for reference in references
    ):
        raise CoreFormatError(
            "Static resource packet overlaps its packet directory"
        )

    starts_by_chunk: dict[int, list[int]] = {}
    for reference in references:
        starts_by_chunk.setdefault(reference.stream_chunk_index, []).append(
            reference.relative_offset
        )
    packet_ends: dict[tuple[int, int], int] = {}
    for chunk_index, starts in starts_by_chunk.items():
        ordered = sorted(starts)
        if len(ordered) != len(set(ordered)):
            raise CoreFormatError(
                "Static resource contains duplicate packet offsets"
            )
        chunk_start, chunk_end = chunk_ranges[chunk_index]
        chunk_size = chunk_end - chunk_start
        for index, start in enumerate(ordered):
            packet_ends[(chunk_index, start)] = (
                ordered[index + 1]
                if index + 1 < len(ordered)
                else chunk_size
            )

    packets: list[NativeStaticPacket] = []
    packets_by_record: list[list[NativeStaticPacket]] = [
        [] for _ in records
    ]
    for reference in references:
        relative_end = packet_ends[
            (reference.stream_chunk_index, reference.relative_offset)
        ]
        bounded_size = relative_end - reference.relative_offset
        span = corestream_data[
            reference.absolute_offset : reference.absolute_offset + bounded_size
        ]
        record = records[reference.mesh_record_index]
        packet = decode_static_packet(
            span,
            mesh_record_index=reference.mesh_record_index,
            packet_index=reference.packet_index,
            stream_chunk_index=reference.stream_chunk_index,
            relative_offset=reference.relative_offset,
            absolute_offset=reference.absolute_offset,
            bounded_size=bounded_size,
            raw_bounds=reference.raw_bounds,
            position_scale=position_scale,
            position_translation=position_translation,
            color_expected=bool(record.attribute_flags & 0x10),
        )
        packets.append(packet)
        packets_by_record[reference.mesh_record_index].append(packet)

    for record_index, record in enumerate(records):
        record_packets = packets_by_record[record_index]
        triangle_count = sum(
            len(packet.triangles) for packet in record_packets
        )
        if triangle_count != record.triangle_count:
            raise CoreFormatError(
                f"Static record {record_index} declares "
                f"{record.triangle_count} triangles but decodes "
                f"{triangle_count}"
            )
        positions = tuple(
            vertex.raw_position
            for packet in record_packets
            for vertex in packet.vertices
        )
        if _raw_bounds(positions) != record.raw_bounds:
            raise CoreFormatError(
                f"Static record {record_index} bounds do not match positions"
            )

    return NativeRegularStaticResource(
        object_id=object_id,
        records=records,
        packets=tuple(packets),
        stream_chunk_sizes=tuple(chunk_sizes),
        root_flags=root_flags,
        position_matrix=position_matrix,
        position_scale=position_scale,
        position_translation=position_translation,
        blob_tail=blob[cursor:],
    )
