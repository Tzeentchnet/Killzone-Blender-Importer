# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import struct

from .errors import CoreFormatError

_U32 = struct.Struct("<I")
_SKINNED_RECORD = struct.Struct("<4I")
_TRANSFORM_RECORD = struct.Struct("<3fI3fI")

_DMA_PREFIX_WORD_1 = 0x80000000
_VIF_STCYCL_44 = 0x01000404
_VIF_HEADER_UNPACK = 0x6C02C1D9
_VIF_STMASK = 0x20000000
_VIF_MASK = 0x3F3F3F3F
_VIF_STCYCL_16 = 0x01000106

_OP_GROUP_SIZE = 0x72
_OP_V3_8 = 0x6A
_OP_CONTROL_S16 = 0x71
_OP_COLOR = 0x6E
_OP_V2_16 = 0x65
_OP_V4_16 = 0x6D

_MAX_STREAM_CHUNKS = 256
_MAX_MESH_RECORDS = 1_000_000
_MAX_PACKETS = 10_000_000
_MAX_PACKET_TARGETS = 256
_MAX_PACKET_GROUPS = 256


@dataclass(frozen=True, slots=True)
class NativeSkinnedTransform:
    center: tuple[float, float, float]
    bone_index: int
    extent: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class NativeSkinnedVertex:
    raw_position: tuple[int, int, int]
    raw_normal: tuple[int, int, int]
    local_position: tuple[float, float, float]
    local_normal: tuple[float, float, float]
    bone_index: int
    transform_index: int
    transform_group: int
    source_pair_index: int
    source_control: int
    adc: bool
    color: tuple[int, int, int, int] | None
    raw_coordinates: tuple[tuple[int, int], ...]
    coordinates: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class NativeSkinnedPacket:
    mesh_record_index: int
    packet_index: int
    stream_chunk_index: int
    relative_offset: int
    absolute_offset: int
    bounded_size: int
    transfer_size: int
    packet_key: int
    metadata_a: int
    metadata_b: int | None
    header_a: tuple[int, int, int, int]
    header_b: tuple[int, int, int, int]
    group_sizes: tuple[int, ...]
    palette_indices: tuple[int, ...]
    tail_opcodes: tuple[int, ...]
    zero_padding_size: int
    vertices: tuple[NativeSkinnedVertex, ...]
    triangles: tuple[tuple[int, int, int], ...]
    span_sha256: str


@dataclass(frozen=True, slots=True)
class NativeSkinnedMeshRecord:
    directory_offset: int
    flags: int
    packet_count: int
    triangle_count: int
    material_id: int


@dataclass(frozen=True, slots=True)
class NativeRegularSkinnedResource:
    object_id: int
    skeleton_id: int
    records: tuple[NativeSkinnedMeshRecord, ...]
    transforms: tuple[NativeSkinnedTransform, ...]
    packets: tuple[NativeSkinnedPacket, ...]
    stream_chunk_sizes: tuple[int, ...]
    packed_resource_flags: int
    feature_flags: int
    auxiliary_offset: int
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
    object_relative_offset: int
    packet_key: int
    metadata_a: int
    metadata_b: int | None


def _require_range(data: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise CoreFormatError(f"{label} is truncated")


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def _vif_count(word: int) -> int:
    value = (word >> 16) & 0xFF
    return 256 if value == 0 else value


def _vif_opcode(word: int) -> int:
    return (word >> 24) & 0x7F


def _vif_address(word: int) -> int:
    return word & 0x3FF


def _read_aligned_payload(
    packet_data: bytes,
    cursor: int,
    payload_size: int,
    transfer_size: int,
    label: str,
) -> tuple[bytes, int]:
    aligned_size = _align(payload_size, 4)
    if cursor + aligned_size > transfer_size:
        raise CoreFormatError(f"{label} exceeds the DMA transfer")
    payload = packet_data[cursor : cursor + payload_size]
    if any(packet_data[cursor + payload_size : cursor + aligned_size]):
        raise CoreFormatError(f"{label} alignment padding is non-zero")
    return payload, cursor + aligned_size


def _peek_packet_group_count(
    packet_data: bytes,
    *,
    bounded_size: int,
) -> int:
    if bounded_size < 0 or bounded_size > len(packet_data):
        raise CoreFormatError("Skinned packet bound exceeds supplied data")
    _require_range(packet_data, 0, 0x30, "Skinned packet fixed header")
    first_word, second_word, stcycl, header_unpack = struct.unpack_from(
        "<4I", packet_data, 0
    )
    if first_word & 0xFFFF0000 != 0x60000000:
        raise CoreFormatError("Skinned packet does not begin with a DMA RET tag")
    if second_word != _DMA_PREFIX_WORD_1:
        raise CoreFormatError("Skinned packet has an unexpected DMA tag word")
    if stcycl != _VIF_STCYCL_44 or header_unpack != _VIF_HEADER_UNPACK:
        raise CoreFormatError("Skinned packet has an unexpected VIF prefix")
    transfer_size = 0x10 + 0x10 * (first_word & 0xFFFF)
    if transfer_size > bounded_size:
        raise CoreFormatError(
            "Skinned packet DMA transfer exceeds its directory-derived bound"
        )
    header_a = struct.unpack_from("<4I", packet_data, 0x10)
    group_count = header_a[3]
    if not 1 <= group_count <= _MAX_PACKET_GROUPS:
        raise CoreFormatError(
            f"Unsupported skinned packet group count {group_count}"
        )
    return group_count


def decode_skinned_packet(
    packet_data: bytes,
    *,
    transforms: tuple[NativeSkinnedTransform, ...],
    palette_indices: tuple[int, ...],
    mesh_record_index: int = 0,
    packet_index: int = 0,
    stream_chunk_index: int = 0,
    relative_offset: int = 0,
    absolute_offset: int = 0,
    bounded_size: int | None = None,
    packet_key: int = 0,
    metadata_a: int = 0,
    metadata_b: int | None = None,
) -> NativeSkinnedPacket:
    if bounded_size is None:
        bounded_size = len(packet_data)
    group_count = _peek_packet_group_count(
        packet_data,
        bounded_size=bounded_size,
    )
    if len(palette_indices) != group_count:
        raise CoreFormatError(
            "Skinned packet palette length does not match its group count"
        )
    if any(index >= len(transforms) for index in palette_indices):
        raise CoreFormatError(
            "Skinned packet palette references an invalid transform"
        )

    first_word = _U32.unpack_from(packet_data, 0)[0]
    transfer_size = 0x10 + 0x10 * (first_word & 0xFFFF)
    header_a = struct.unpack_from("<4I", packet_data, 0x10)
    header_b = struct.unpack_from("<4I", packet_data, 0x20)
    target_count = header_a[0]
    if not 1 <= target_count <= _MAX_PACKET_TARGETS:
        raise CoreFormatError(
            f"Unsupported skinned packet target count {target_count}"
        )
    aligned_target_count = _align(target_count, 4)
    if header_a[1] != 3 * target_count:
        raise CoreFormatError("Skinned packet header has invalid 3*N value")
    if header_a[2] != 3 * aligned_target_count:
        raise CoreFormatError(
            "Skinned packet header has invalid aligned 3*Q value"
        )

    cursor = 0x30
    _require_range(packet_data, cursor, 16, "Skinned packet group prefix")
    stmask, mask, stcycl_16, group_command = struct.unpack_from(
        "<4I", packet_data, cursor
    )
    if (
        stmask != _VIF_STMASK
        or mask != _VIF_MASK
        or stcycl_16 != _VIF_STCYCL_16
    ):
        raise CoreFormatError("Skinned packet group VIF prefix is invalid")
    if (
        _vif_opcode(group_command) != _OP_GROUP_SIZE
        or _vif_count(group_command) != group_count
        or _vif_address(group_command) != 0xBA
        or group_command & 0xC000 != 0xC000
    ):
        raise CoreFormatError("Skinned packet group-size command is invalid")
    cursor += 16
    group_payload, cursor = _read_aligned_payload(
        packet_data,
        cursor,
        group_count,
        transfer_size,
        "Skinned packet group-size payload",
    )
    group_sizes = tuple(group_payload)
    if any(size == 0 for size in group_sizes):
        raise CoreFormatError("Skinned packet contains an empty transform group")
    pair_count = sum(group_sizes)

    _require_range(packet_data, cursor, 8, "Skinned packet V3 prefix")
    stcycl_44, vector_command = struct.unpack_from(
        "<2I", packet_data, cursor
    )
    expected_vector_count = 2 * pair_count
    expected_v3_start = 0x0B5 + 6 * group_count
    if stcycl_44 != _VIF_STCYCL_44:
        raise CoreFormatError("Skinned packet V3 STCYCL command is invalid")
    if (
        _vif_opcode(vector_command) != _OP_V3_8
        or _vif_count(vector_command) != expected_vector_count
        or _vif_address(vector_command) != expected_v3_start
        or vector_command & 0xC000 != 0x8000
    ):
        raise CoreFormatError("Skinned packet signed V3-8 command is invalid")
    cursor += 8
    vector_payload, cursor = _read_aligned_payload(
        packet_data,
        cursor,
        expected_vector_count * 3,
        transfer_size,
        "Skinned packet signed V3-8 payload",
    )
    vector_values = struct.unpack(
        f"<{expected_vector_count * 3}b", vector_payload
    )
    vectors = tuple(
        (
            vector_values[index],
            vector_values[index + 1],
            vector_values[index + 2],
        )
        for index in range(0, len(vector_values), 3)
    )

    _require_range(packet_data, cursor, 4, "Skinned packet control command")
    control_command = _U32.unpack_from(packet_data, cursor)[0]
    if (
        _vif_opcode(control_command) != _OP_CONTROL_S16
        or _vif_count(control_command) != expected_vector_count
        or _vif_address(control_command) != expected_v3_start
        or control_command & 0xC000 != 0xC000
    ):
        raise CoreFormatError("Skinned packet S16 control command is invalid")
    cursor += 4
    control_payload, cursor = _read_aligned_payload(
        packet_data,
        cursor,
        expected_vector_count * 2,
        transfer_size,
        "Skinned packet S16 control payload",
    )
    controls = struct.unpack(
        f"<{expected_vector_count}H", control_payload
    )

    expected_normal_scratch = 0x0B5 + aligned_target_count
    expected_tail_base = max(
        0x0B5 + 2 * aligned_target_count,
        expected_v3_start + 2 * pair_count,
    )
    if header_b[:3] != (
        expected_v3_start,
        expected_normal_scratch,
        expected_tail_base,
    ):
        raise CoreFormatError("Skinned packet VU addresses are inconsistent")

    coordinate_values: list[list[tuple[int, int]]] = [
        [] for _ in range(target_count)
    ]
    colors: tuple[tuple[int, int, int, int], ...] | None = None
    tail_opcodes: list[int] = []
    coordinate_address = header_b[3]
    zero_padding_size = 0
    saw_padding = False
    while cursor < transfer_size:
        _require_range(packet_data, cursor, 4, "Skinned packet tail command")
        word = _U32.unpack_from(packet_data, cursor)[0]
        if word == 0:
            saw_padding = True
            zero_padding_size += 4
            cursor += 4
            continue
        if saw_padding:
            raise CoreFormatError(
                "Skinned packet has a tail command after VIF padding"
            )
        opcode = _vif_opcode(word)
        count = _vif_count(word)
        address = _vif_address(word)
        if count != target_count:
            raise CoreFormatError(
                "Skinned packet tail count does not match target count"
            )
        cursor += 4
        if opcode == _OP_COLOR:
            if (
                colors is not None
                or address != expected_tail_base
                or word & 0xC000 != 0xC000
            ):
                raise CoreFormatError(
                    "Skinned packet color command is inconsistent"
                )
            width = 4
        elif opcode in (_OP_V2_16, _OP_V4_16):
            if (
                colors is not None
                or address != coordinate_address
                or word & 0xC000 != 0x8000
            ):
                raise CoreFormatError(
                    "Skinned packet coordinate command is inconsistent"
                )
            width = 4 if opcode == _OP_V2_16 else 8
            coordinate_address += target_count
        else:
            raise CoreFormatError(
                f"Unsupported skinned packet tail opcode 0x{opcode:02X}"
            )
        payload, cursor = _read_aligned_payload(
            packet_data,
            cursor,
            target_count * width,
            transfer_size,
            "Skinned packet tail payload",
        )
        if opcode == _OP_COLOR:
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
            values = struct.unpack(f"<{target_count * 2}h", payload)
            for target_index in range(target_count):
                base = target_index * 2
                coordinate_values[target_index].append(
                    (values[base], values[base + 1])
                )
        else:
            values = struct.unpack(f"<{target_count * 4}h", payload)
            for target_index in range(target_count):
                base = target_index * 4
                coordinate_values[target_index].extend(
                    (
                        (values[base], values[base + 1]),
                        (values[base + 2], values[base + 3]),
                    )
                )
        tail_opcodes.append(opcode)

    if cursor != transfer_size:
        raise CoreFormatError("Skinned packet tail does not end at DMA boundary")
    expected_uv_base = expected_tail_base + (
        target_count if colors is not None else 0
    )
    if header_b[3] != expected_uv_base:
        raise CoreFormatError("Skinned packet UV base is inconsistent")
    if any(len(values) > 4 for values in coordinate_values):
        raise CoreFormatError(
            "Skinned packet contains more than four coordinate pairs"
        )

    owners: list[
        tuple[
            tuple[int, int, int],
            tuple[int, int, int],
            tuple[float, float, float],
            tuple[float, float, float],
            int,
            int,
            int,
            int,
            int,
            bool,
        ]
        | None
    ] = [None] * target_count
    pair_index = 0
    for group_index, group_size in enumerate(group_sizes):
        transform_index = palette_indices[group_index]
        transform = transforms[transform_index]
        for _ in range(group_size):
            raw_position = vectors[pair_index * 2]
            raw_normal = vectors[pair_index * 2 + 1]
            rounded_normal_length = round(
                math.sqrt(sum(value * value for value in raw_normal))
            )
            if not 126 <= rounded_normal_length <= 129:
                raise CoreFormatError(
                    "Skinned packet contains an invalid signed normal"
                )
            local_position = tuple(
                transform.center[axis]
                + transform.extent[axis]
                * ((raw_position[axis] + 0.5) / 255.0)
                for axis in range(3)
            )
            local_normal = tuple(value / 127.0 for value in raw_normal)
            pair_controls = controls[pair_index * 2 : pair_index * 2 + 2]
            targets: list[tuple[int, int, bool]] = []
            for control in pair_controls:
                raw_control = control & 0xFFFF
                qword_offset = raw_control & 0x7FFF
                if qword_offset % 3:
                    raise CoreFormatError(
                        "Skinned packet control is not target aligned"
                    )
                target = qword_offset // 3
                if target >= target_count:
                    raise CoreFormatError(
                        "Skinned packet control target is out of range"
                    )
                targets.append(
                    (
                        target,
                        raw_control,
                        bool(raw_control & 0x8000),
                    )
                )
            if (
                targets[0][0] == targets[1][0]
                and targets[0][2] != targets[1][2]
            ):
                raise CoreFormatError(
                    "Skinned packet same-target ADC controls disagree"
                )
            for target, control, adc in dict.fromkeys(targets):
                if owners[target] is not None:
                    raise CoreFormatError(
                        "Skinned packet target has multiple logical owners"
                    )
                owners[target] = (
                    raw_position,
                    raw_normal,
                    local_position,
                    local_normal,
                    transform.bone_index,
                    transform_index,
                    group_index,
                    pair_index,
                    control,
                    adc,
                )
            pair_index += 1
    if pair_index != pair_count or any(owner is None for owner in owners):
        raise CoreFormatError(
            "Skinned packet logical pairs do not cover every target"
        )

    vertices: list[NativeSkinnedVertex] = []
    for target_index, owner in enumerate(owners):
        if owner is None:
            raise AssertionError("Validated skinned target has no owner")
        vertices.append(
            NativeSkinnedVertex(
                raw_position=owner[0],
                raw_normal=owner[1],
                local_position=owner[2],
                local_normal=owner[3],
                bone_index=owner[4],
                transform_index=owner[5],
                transform_group=owner[6],
                source_pair_index=owner[7],
                source_control=owner[8],
                adc=owner[9],
                color=(
                    colors[target_index] if colors is not None else None
                ),
                raw_coordinates=tuple(coordinate_values[target_index]),
                coordinates=tuple(
                    (value[0] / 4096.0, value[1] / 4096.0)
                    for value in coordinate_values[target_index]
                ),
            )
        )

    triangles: list[tuple[int, int, int]] = []
    for target_index in range(2, target_count):
        if vertices[target_index].adc:
            continue
        if target_index & 1:
            triangles.append(
                (target_index - 1, target_index - 2, target_index)
            )
        else:
            triangles.append(
                (target_index - 2, target_index - 1, target_index)
            )
    if sum(not vertex.adc for vertex in vertices) != len(triangles):
        raise CoreFormatError(
            "Skinned packet ADC targets do not match strip triangles"
        )

    return NativeSkinnedPacket(
        mesh_record_index=mesh_record_index,
        packet_index=packet_index,
        stream_chunk_index=stream_chunk_index,
        relative_offset=relative_offset,
        absolute_offset=absolute_offset,
        bounded_size=bounded_size,
        transfer_size=transfer_size,
        packet_key=packet_key,
        metadata_a=metadata_a,
        metadata_b=metadata_b,
        header_a=header_a,
        header_b=header_b,
        group_sizes=group_sizes,
        palette_indices=palette_indices,
        tail_opcodes=tuple(tail_opcodes),
        zero_padding_size=zero_padding_size,
        vertices=tuple(vertices),
        triangles=tuple(triangles),
        span_sha256=hashlib.sha256(packet_data[:bounded_size]).hexdigest(),
    )


def _parse_records(
    blob: bytes,
    offset: int,
) -> tuple[tuple[NativeSkinnedMeshRecord, ...], int]:
    _require_range(blob, offset, 4, "Skinned resource mesh record count")
    record_count = _U32.unpack_from(blob, offset)[0]
    if not 1 <= record_count <= _MAX_MESH_RECORDS:
        raise CoreFormatError(
            f"Unsupported skinned resource record count {record_count}"
        )
    offset += 4
    _require_range(
        blob,
        offset,
        record_count * _SKINNED_RECORD.size,
        "Skinned resource mesh records",
    )
    records: list[NativeSkinnedMeshRecord] = []
    total_packet_count = 0
    for index in range(record_count):
        directory_and_flags, packet_count, triangle_count, material_id = (
            _SKINNED_RECORD.unpack_from(
                blob, offset + index * _SKINNED_RECORD.size
            )
        )
        total_packet_count += packet_count
        if total_packet_count > _MAX_PACKETS:
            raise CoreFormatError("Skinned resource packet count is excessive")
        flags = directory_and_flags & 0xFFFF
        if flags not in (0, 1):
            raise CoreFormatError(
                f"Skinned resource record {index} has unsupported flags"
            )
        records.append(
            NativeSkinnedMeshRecord(
                directory_offset=directory_and_flags >> 16,
                flags=flags,
                packet_count=packet_count,
                triangle_count=triangle_count,
                material_id=material_id,
            )
        )
    return (
        tuple(records),
        offset + record_count * _SKINNED_RECORD.size,
    )


def parse_regular_skinned_resource(
    blob: bytes,
    corestream_data: bytes,
    *,
    object_id: int,
    stream_offset: int,
    stream_size: int,
    skeleton_bone_count: int | None = None,
) -> NativeRegularSkinnedResource:
    _require_range(blob, 0, 0x44, "Regular-skinned resource")
    skeleton_id = _U32.unpack_from(blob, 0x24)[0]
    cursor = 0x40
    chunk_count = _U32.unpack_from(blob, cursor)[0]
    if not 1 <= chunk_count <= _MAX_STREAM_CHUNKS:
        raise CoreFormatError(
            f"Unsupported skinned resource chunk count {chunk_count}"
        )
    cursor += 4
    _require_range(
        blob,
        cursor,
        chunk_count * 4,
        "Skinned resource chunk sizes",
    )
    chunk_sizes = struct.unpack_from(f"<{chunk_count}I", blob, cursor)
    if any(size == 0 for size in chunk_sizes):
        raise CoreFormatError("Skinned resource contains an empty stream chunk")
    if sum(chunk_sizes) != stream_size:
        raise CoreFormatError(
            "Skinned resource chunk sizes do not equal owned stream size"
        )
    cursor = _align(cursor + chunk_count * 4, 4)
    records, cursor = _parse_records(blob, cursor)
    _require_range(blob, cursor, 8, "Skinned resource auxiliary descriptor")
    packed_resource_flags, auxiliary_offset = struct.unpack_from(
        "<2I", blob, cursor
    )
    cursor += 8
    blob_tail = blob[cursor:]
    if any(blob_tail):
        raise CoreFormatError("Skinned resource has a non-zero object tail")
    transform_count = packed_resource_flags >> 24
    feature_flags = packed_resource_flags & 0xFFFFFF
    if transform_count == 0:
        raise CoreFormatError("Skinned resource has no transform records")
    if auxiliary_offset == 0:
        raise CoreFormatError("Skinned resource has a null auxiliary offset")

    if (
        stream_offset < 0
        or stream_size < 0
        or stream_offset > len(corestream_data)
        or stream_size > len(corestream_data) - stream_offset
    ):
        raise CoreFormatError("Skinned resource owned stream range is invalid")
    chunk_ranges: list[tuple[int, int, int]] = []
    chunk_absolute = stream_offset
    chunk_relative = 0
    for size in chunk_sizes:
        chunk_ranges.append(
            (chunk_absolute, chunk_absolute + size, chunk_relative)
        )
        chunk_absolute += size
        chunk_relative += size

    object_stream = corestream_data[
        stream_offset : stream_offset + stream_size
    ]
    _require_range(
        object_stream,
        auxiliary_offset,
        transform_count * _TRANSFORM_RECORD.size,
        "Skinned resource transform table",
    )

    directory_offsets = tuple(
        record.directory_offset
        for record in records
        if record.packet_count > 0
    )
    if len(set(directory_offsets)) != len(directory_offsets):
        raise CoreFormatError(
            "Skinned resource contains duplicate record directories"
        )
    if any(offset >= auxiliary_offset for offset in directory_offsets):
        raise CoreFormatError(
            "Skinned resource auxiliary area precedes a record directory"
        )
    sorted_boundaries = sorted((*directory_offsets, auxiliary_offset))

    references: list[_PacketReference] = []
    for record_index, record in enumerate(records):
        if record.packet_count == 0:
            continue
        directory = record.directory_offset
        next_boundary = min(
            boundary
            for boundary in sorted_boundaries
            if boundary > directory
        )
        keys_size = record.packet_count * 4
        offsets_size = record.packet_count * 4
        indices_size = record.packet_count
        metadata_b_count = max(record.packet_count - 1, 0)
        directory_end = _align(
            directory + keys_size + offsets_size + indices_size,
            4,
        )
        directory_end = _align(
            directory_end + record.packet_count,
            4,
        )
        directory_end = _align(
            directory_end + metadata_b_count,
            4,
        )
        if directory_end > next_boundary:
            raise CoreFormatError(
                f"Skinned record {record_index} directory overlaps "
                "the next owned area"
            )
        _require_range(
            object_stream,
            directory,
            directory_end - directory,
            f"Skinned record {record_index} directory",
        )
        directory_cursor = directory
        packet_keys = struct.unpack_from(
            f"<{record.packet_count}I",
            object_stream,
            directory_cursor,
        )
        directory_cursor += keys_size
        packet_offsets = struct.unpack_from(
            f"<{record.packet_count}I",
            object_stream,
            directory_cursor,
        )
        directory_cursor += offsets_size
        packet_chunk_indices = tuple(
            object_stream[
                directory_cursor : directory_cursor + indices_size
            ]
        )
        directory_cursor = _align(directory_cursor + indices_size, 4)
        metadata_a = tuple(
            object_stream[
                directory_cursor : directory_cursor + record.packet_count
            ]
        )
        directory_cursor = _align(
            directory_cursor + record.packet_count,
            4,
        )
        metadata_b = tuple(
            object_stream[
                directory_cursor : directory_cursor + metadata_b_count
            ]
        )
        directory_cursor = _align(
            directory_cursor + metadata_b_count,
            4,
        )
        if directory_cursor != directory_end:
            raise AssertionError("Skinned directory extent is inconsistent")
        if any(object_stream[directory_cursor:next_boundary]):
            raise CoreFormatError(
                f"Skinned record {record_index} directory padding is non-zero"
            )

        for packet_index, packet_offset in enumerate(packet_offsets):
            if packet_offset & 0xF:
                raise CoreFormatError(
                    "Skinned resource packet offset is not 16-byte aligned"
                )
            chunk_index = packet_chunk_indices[packet_index]
            if chunk_index >= len(chunk_ranges):
                raise CoreFormatError(
                    "Skinned resource packet references an invalid chunk"
                )
            chunk_start, chunk_end, chunk_relative_start = chunk_ranges[
                chunk_index
            ]
            chunk_size = chunk_end - chunk_start
            if packet_offset > chunk_size - 0x10:
                raise CoreFormatError(
                    "Skinned resource packet does not contain a complete "
                    "DMA tag in its chunk"
                )
            absolute_offset = chunk_start + packet_offset
            first_word = _U32.unpack_from(
                corestream_data,
                absolute_offset,
            )[0]
            dma_extent = 0x10 * ((first_word & 0xFFFF) + 1)
            if dma_extent > chunk_size - packet_offset:
                raise CoreFormatError(
                    "Skinned resource packet DMA transfer exceeds its chunk"
                )
            references.append(
                _PacketReference(
                    mesh_record_index=record_index,
                    packet_index=packet_index,
                    stream_chunk_index=chunk_index,
                    relative_offset=packet_offset,
                    absolute_offset=absolute_offset,
                    object_relative_offset=(
                        chunk_relative_start + packet_offset
                    ),
                    packet_key=packet_keys[packet_index],
                    metadata_a=metadata_a[packet_index],
                    metadata_b=(
                        metadata_b[packet_index]
                        if packet_index < metadata_b_count
                        else None
                    ),
                )
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
                "Skinned resource contains duplicate packet offsets"
            )
        chunk_start, chunk_end, _ = chunk_ranges[chunk_index]
        chunk_size = chunk_end - chunk_start
        for index, start in enumerate(ordered):
            packet_ends[(chunk_index, start)] = (
                ordered[index + 1]
                if index + 1 < len(ordered)
                else chunk_size
            )

    transforms: list[NativeSkinnedTransform] = []
    for index in range(transform_count):
        values = _TRANSFORM_RECORD.unpack_from(
            object_stream,
            auxiliary_offset + index * _TRANSFORM_RECORD.size,
        )
        center = values[:3]
        bone_index = values[3]
        extent = values[4:7]
        trailing_zero = values[7]
        if not all(math.isfinite(value) for value in center + extent):
            raise CoreFormatError(
                f"Skinned transform {index} contains non-finite values"
            )
        if any(value <= 0.0 for value in extent):
            raise CoreFormatError(
                f"Skinned transform {index} has a non-positive extent"
            )
        if trailing_zero != 0:
            raise CoreFormatError(
                f"Skinned transform {index} has a non-zero reserved word"
            )
        if (
            skeleton_bone_count is not None
            and bone_index >= skeleton_bone_count
        ):
            raise CoreFormatError(
                f"Skinned transform {index} has an invalid bone selector"
            )
        transforms.append(
            NativeSkinnedTransform(
                center=center,
                bone_index=bone_index,
                extent=extent,
            )
        )

    group_counts: list[int] = []
    for reference in references:
        relative_end = packet_ends[
            (reference.stream_chunk_index, reference.relative_offset)
        ]
        bounded_size = relative_end - reference.relative_offset
        span = corestream_data[
            reference.absolute_offset : reference.absolute_offset + bounded_size
        ]
        group_counts.append(
            _peek_packet_group_count(span, bounded_size=bounded_size)
        )

    palette_start = (
        auxiliary_offset + transform_count * _TRANSFORM_RECORD.size
    )
    palette_size = sum(group_counts)
    _require_range(
        object_stream,
        palette_start,
        palette_size,
        "Skinned resource palette",
    )
    palette = tuple(
        object_stream[palette_start : palette_start + palette_size]
    )
    if any(index >= transform_count for index in palette):
        raise CoreFormatError(
            "Skinned resource palette has an invalid transform index"
        )
    padded_palette_end = _align(palette_start + palette_size, 0x10)
    _require_range(
        object_stream,
        palette_start + palette_size,
        padded_palette_end - palette_start - palette_size,
        "Skinned resource palette padding",
    )
    if any(object_stream[palette_start + palette_size : padded_palette_end]):
        raise CoreFormatError("Skinned resource palette padding is non-zero")
    if references:
        first_packet_offset = min(
            reference.object_relative_offset for reference in references
        )
        if padded_palette_end != first_packet_offset:
            raise CoreFormatError(
                "Skinned resource palette does not end at the first packet"
            )
        if set(palette) != set(range(transform_count)):
            raise CoreFormatError(
                "Skinned resource contains an unreferenced transform record"
            )
    elif any(object_stream[padded_palette_end:]):
        raise CoreFormatError(
            "Skinned resource without packets has trailing stream data"
        )

    packets: list[NativeSkinnedPacket] = []
    packets_by_record: list[list[NativeSkinnedPacket]] = [
        [] for _ in records
    ]
    palette_cursor = 0
    transform_tuple = tuple(transforms)
    for reference, group_count in zip(references, group_counts):
        relative_end = packet_ends[
            (reference.stream_chunk_index, reference.relative_offset)
        ]
        bounded_size = relative_end - reference.relative_offset
        span = corestream_data[
            reference.absolute_offset : reference.absolute_offset + bounded_size
        ]
        packet_palette = palette[
            palette_cursor : palette_cursor + group_count
        ]
        palette_cursor += group_count
        packet = decode_skinned_packet(
            span,
            transforms=transform_tuple,
            palette_indices=packet_palette,
            mesh_record_index=reference.mesh_record_index,
            packet_index=reference.packet_index,
            stream_chunk_index=reference.stream_chunk_index,
            relative_offset=reference.relative_offset,
            absolute_offset=reference.absolute_offset,
            bounded_size=bounded_size,
            packet_key=reference.packet_key,
            metadata_a=reference.metadata_a,
            metadata_b=reference.metadata_b,
        )
        packets.append(packet)
        packets_by_record[reference.mesh_record_index].append(packet)
    if palette_cursor != len(palette):
        raise AssertionError("Validated skinned palette was not consumed")

    for record_index, record in enumerate(records):
        triangle_count = sum(
            len(packet.triangles)
            for packet in packets_by_record[record_index]
        )
        if triangle_count != record.triangle_count:
            raise CoreFormatError(
                f"Skinned record {record_index} declares "
                f"{record.triangle_count} triangles but decodes "
                f"{triangle_count}"
            )

    return NativeRegularSkinnedResource(
        object_id=object_id,
        skeleton_id=skeleton_id,
        records=records,
        transforms=transform_tuple,
        packets=tuple(packets),
        stream_chunk_sizes=tuple(chunk_sizes),
        packed_resource_flags=packed_resource_flags,
        feature_flags=feature_flags,
        auxiliary_offset=auxiliary_offset,
        blob_tail=blob_tail,
    )
