# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import struct
import tempfile
from typing import BinaryIO, Iterator

from .errors import IsoFormatError

SECTOR_SIZE = 2_048
_VOLUME_DESCRIPTOR_START = 16
_PRIMARY_VOLUME_DESCRIPTOR = 1
_VOLUME_DESCRIPTOR_TERMINATOR = 255
_DIRECTORY_FLAG = 0x02
_MULTI_EXTENT_FLAG = 0x80
_MAX_VOLUME_DESCRIPTORS = 128
_MAX_DIRECTORY_SIZE = 64 * 1024 * 1024
_MAX_DIRECTORY_DEPTH = 64
_MAX_RECORDS = 1_000_000


@dataclass(frozen=True, slots=True)
class IsoRecord:
    path: PurePosixPath
    extent_lba: int
    size: int
    flags: int

    @property
    def is_directory(self) -> bool:
        return bool(self.flags & _DIRECTORY_FLAG)


@dataclass(frozen=True, slots=True)
class _RawRecord:
    identifier: bytes
    extent_lba: int
    size: int
    flags: int

    @property
    def is_directory(self) -> bool:
        return bool(self.flags & _DIRECTORY_FLAG)


class Iso9660Image:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self._stream: BinaryIO | None = None
        self._file_size = 0
        self._volume_blocks = 0
        self._root: _RawRecord | None = None
        self._records: tuple[IsoRecord, ...] | None = None
        self.volume_id = ""

    def __enter__(self) -> Iso9660Image:
        try:
            resolved = self.path.resolve(strict=True)
        except OSError as exc:
            raise IsoFormatError(f"ISO image does not exist: {self.path}") from exc
        if not resolved.is_file():
            raise IsoFormatError(f"ISO image is not a file: {resolved}")

        try:
            stream = resolved.open("rb")
            file_size = resolved.stat().st_size
        except OSError as exc:
            raise IsoFormatError(f"Unable to open ISO image: {resolved}") from exc

        self.path = resolved
        self._stream = stream
        self._file_size = file_size
        try:
            self._read_primary_volume_descriptor()
        except Exception:
            stream.close()
            self._stream = None
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def _require_open(self) -> BinaryIO:
        if self._stream is None:
            raise IsoFormatError("ISO image is not open")
        return self._stream

    def _read_exact(self, offset: int, size: int, label: str) -> bytes:
        if offset < 0 or size < 0 or offset + size > self._file_size:
            raise IsoFormatError(
                f"{label} range 0x{offset:X}+0x{size:X} is outside the ISO"
            )
        stream = self._require_open()
        try:
            stream.seek(offset)
            data = stream.read(size)
        except OSError as exc:
            raise IsoFormatError(f"Unable to read {label}") from exc
        if len(data) != size:
            raise IsoFormatError(f"Truncated {label} at offset 0x{offset:X}")
        return data

    def _read_sector(self, lba: int, label: str) -> bytes:
        if lba < 0:
            raise IsoFormatError(f"{label} has a negative LBA")
        return self._read_exact(lba * SECTOR_SIZE, SECTOR_SIZE, label)

    @staticmethod
    def _both_endian_u16(data: bytes, offset: int, label: str) -> int:
        little = struct.unpack_from("<H", data, offset)[0]
        big = struct.unpack_from(">H", data, offset + 2)[0]
        if little != big:
            raise IsoFormatError(
                f"{label} endian copies disagree: {little} != {big}"
            )
        return little

    @staticmethod
    def _both_endian_u32(data: bytes, offset: int, label: str) -> int:
        little = struct.unpack_from("<I", data, offset)[0]
        big = struct.unpack_from(">I", data, offset + 4)[0]
        if little != big:
            raise IsoFormatError(
                f"{label} endian copies disagree: {little} != {big}"
            )
        return little

    def _read_primary_volume_descriptor(self) -> None:
        primary: bytes | None = None
        saw_terminator = False
        for index in range(_MAX_VOLUME_DESCRIPTORS):
            sector = self._read_sector(
                _VOLUME_DESCRIPTOR_START + index,
                f"volume descriptor {index}",
            )
            descriptor_type = sector[0]
            if sector[1:6] != b"CD001" or sector[6] != 1:
                raise IsoFormatError(
                    f"Invalid ISO 9660 volume descriptor {index}"
                )
            if descriptor_type == _PRIMARY_VOLUME_DESCRIPTOR and primary is None:
                primary = sector
            if descriptor_type == _VOLUME_DESCRIPTOR_TERMINATOR:
                saw_terminator = True
                break

        if primary is None:
            raise IsoFormatError("ISO has no primary volume descriptor")
        if not saw_terminator:
            raise IsoFormatError("ISO volume descriptor sequence has no terminator")

        logical_block_size = self._both_endian_u16(
            primary, 128, "logical block size"
        )
        if logical_block_size != SECTOR_SIZE:
            raise IsoFormatError(
                f"Unsupported ISO logical block size {logical_block_size}; "
                f"expected {SECTOR_SIZE}"
            )

        volume_blocks = self._both_endian_u32(
            primary, 80, "volume space size"
        )
        if volume_blocks <= _VOLUME_DESCRIPTOR_START:
            raise IsoFormatError(f"Invalid ISO volume size {volume_blocks} blocks")
        if volume_blocks * SECTOR_SIZE > self._file_size:
            raise IsoFormatError(
                "ISO volume space size extends beyond the image file"
            )

        volume_id_bytes = primary[40:72].rstrip(b" ")
        try:
            self.volume_id = volume_id_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise IsoFormatError("ISO volume identifier is not ASCII") from exc

        root = self._parse_raw_record(primary, 156, "root directory record")
        if not root.is_directory:
            raise IsoFormatError("Primary volume root record is not a directory")
        self._volume_blocks = volume_blocks
        self._validate_extent(root, "root directory")
        self._root = root

    def _parse_raw_record(
        self, data: bytes, offset: int, label: str
    ) -> _RawRecord:
        if offset < 0 or offset >= len(data):
            raise IsoFormatError(f"{label} offset is outside its containing data")
        record_length = data[offset]
        if record_length < 34 or offset + record_length > len(data):
            raise IsoFormatError(f"{label} has invalid length {record_length}")

        record = data[offset : offset + record_length]
        extent_lba = self._both_endian_u32(record, 2, f"{label} extent")
        size = self._both_endian_u32(record, 10, f"{label} size")
        flags = record[25]
        if record[26] != 0 or record[27] != 0:
            raise IsoFormatError(f"{label} uses unsupported interleaving")
        volume_sequence = self._both_endian_u16(
            record, 28, f"{label} volume sequence"
        )
        if volume_sequence != 1:
            raise IsoFormatError(
                f"{label} uses unsupported volume sequence {volume_sequence}"
            )

        identifier_length = record[32]
        identifier_end = 33 + identifier_length
        if identifier_length == 0 or identifier_end > len(record):
            raise IsoFormatError(f"{label} has an invalid identifier length")
        identifier = record[33:identifier_end]
        return _RawRecord(
            identifier=identifier,
            extent_lba=extent_lba,
            size=size,
            flags=flags,
        )

    def _validate_extent(self, record: _RawRecord, label: str) -> None:
        if record.extent_lba >= self._volume_blocks:
            raise IsoFormatError(
                f"{label} starts beyond the declared ISO volume"
            )
        extent_end = record.extent_lba * SECTOR_SIZE + record.size
        volume_end = self._volume_blocks * SECTOR_SIZE
        if extent_end > volume_end or extent_end > self._file_size:
            raise IsoFormatError(f"{label} extent exceeds the ISO volume")

    @staticmethod
    def _decode_identifier(identifier: bytes, label: str) -> str:
        try:
            name = identifier.decode("ascii")
        except UnicodeDecodeError as exc:
            raise IsoFormatError(f"{label} identifier is not ASCII") from exc
        if ";" in name:
            base, separator, version = name.rpartition(";")
            if separator and version.isdigit():
                name = base
        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or "\x00" in name
        ):
            raise IsoFormatError(f"{label} has unsafe identifier {name!r}")
        return name

    def _read_directory(self, directory: _RawRecord, path: PurePosixPath) -> list[_RawRecord]:
        if directory.size > _MAX_DIRECTORY_SIZE:
            raise IsoFormatError(
                f"Directory {path.as_posix() or '/'} exceeds "
                f"{_MAX_DIRECTORY_SIZE} bytes"
            )
        self._validate_extent(directory, f"directory {path.as_posix() or '/'}")
        data = self._read_exact(
            directory.extent_lba * SECTOR_SIZE,
            directory.size,
            f"directory {path.as_posix() or '/'}",
        )

        records: list[_RawRecord] = []
        offset = 0
        while offset < len(data):
            record_length = data[offset]
            if record_length == 0:
                offset = min(
                    ((offset // SECTOR_SIZE) + 1) * SECTOR_SIZE,
                    len(data),
                )
                continue
            if (offset % SECTOR_SIZE) + record_length > SECTOR_SIZE:
                raise IsoFormatError(
                    f"Directory record at 0x{offset:X} crosses a sector boundary"
                )
            raw = self._parse_raw_record(
                data,
                offset,
                f"directory record at {path.as_posix() or '/'}+0x{offset:X}",
            )
            self._validate_extent(
                raw,
                f"directory entry at {path.as_posix() or '/'}+0x{offset:X}",
            )
            records.append(raw)
            offset += record_length
        return records

    def records(self) -> tuple[IsoRecord, ...]:
        self._require_open()
        if self._records is not None:
            return self._records
        if self._root is None:
            raise IsoFormatError("ISO root directory is unavailable")

        output: list[IsoRecord] = []
        stack: list[tuple[_RawRecord, PurePosixPath, int]] = [
            (self._root, PurePosixPath(), 0)
        ]
        visited_directories: set[tuple[int, int]] = set()
        seen_paths: set[str] = set()

        while stack:
            directory, parent_path, depth = stack.pop()
            if depth > _MAX_DIRECTORY_DEPTH:
                raise IsoFormatError(
                    f"ISO directory depth exceeds {_MAX_DIRECTORY_DEPTH}"
                )
            directory_key = (directory.extent_lba, directory.size)
            if directory_key in visited_directories:
                continue
            visited_directories.add(directory_key)

            for raw in self._read_directory(directory, parent_path):
                if raw.identifier in {b"\x00", b"\x01"}:
                    continue
                name = self._decode_identifier(
                    raw.identifier,
                    f"entry under {parent_path.as_posix() or '/'}",
                )
                path = parent_path / name
                normalized = path.as_posix().casefold()
                if normalized in seen_paths:
                    raise IsoFormatError(
                        f"ISO contains a case-colliding path: {path.as_posix()}"
                    )
                seen_paths.add(normalized)
                if raw.flags & _MULTI_EXTENT_FLAG:
                    raise IsoFormatError(
                        f"Multi-extent file is unsupported: {path.as_posix()}"
                    )

                record = IsoRecord(
                    path=path,
                    extent_lba=raw.extent_lba,
                    size=raw.size,
                    flags=raw.flags,
                )
                output.append(record)
                if len(output) > _MAX_RECORDS:
                    raise IsoFormatError(
                        f"ISO record count exceeds {_MAX_RECORDS}"
                    )
                if raw.is_directory:
                    stack.append((raw, path, depth + 1))

        self._records = tuple(sorted(output, key=lambda item: item.path.as_posix()))
        return self._records

    def iter_file(
        self, record: IsoRecord, chunk_size: int = 1024 * 1024
    ) -> Iterator[bytes]:
        self._require_open()
        if record.is_directory:
            raise IsoFormatError(f"Cannot read directory as a file: {record.path}")
        if chunk_size <= 0:
            raise IsoFormatError("Chunk size must be positive")

        raw = _RawRecord(
            identifier=record.path.name.encode("ascii"),
            extent_lba=record.extent_lba,
            size=record.size,
            flags=record.flags,
        )
        self._validate_extent(raw, f"file {record.path.as_posix()}")
        stream = self._require_open()
        try:
            stream.seek(record.extent_lba * SECTOR_SIZE)
            remaining = record.size
            while remaining:
                chunk = stream.read(min(chunk_size, remaining))
                if not chunk:
                    raise IsoFormatError(
                        f"Truncated file data for {record.path.as_posix()}"
                    )
                remaining -= len(chunk)
                yield chunk
        except OSError as exc:
            raise IsoFormatError(
                f"Unable to read {record.path.as_posix()}"
            ) from exc

    def read_prefix(self, record: IsoRecord, size: int = 64) -> bytes:
        if size < 0:
            raise IsoFormatError("Prefix size must not be negative")
        return self.read_file_range(record, 0, min(size, record.size))

    def read_file_range(
        self,
        record: IsoRecord,
        offset: int,
        size: int,
    ) -> bytes:
        if record.is_directory:
            raise IsoFormatError(f"Cannot read directory as a file: {record.path}")
        if offset < 0 or size < 0:
            raise IsoFormatError("File range offset and size must not be negative")
        if offset > record.size or size > record.size - offset:
            raise IsoFormatError(
                f"Range 0x{offset:X}+0x{size:X} exceeds "
                f"{record.path.as_posix()} size 0x{record.size:X}"
            )
        return self._read_exact(
            record.extent_lba * SECTOR_SIZE + offset,
            size,
            f"range of {record.path.as_posix()}",
        )

    def hash_file(self, record: IsoRecord) -> str:
        digest = hashlib.sha256()
        for chunk in self.iter_file(record):
            digest.update(chunk)
        return digest.hexdigest()

    def extract_file(self, record: IsoRecord, destination: Path) -> str:
        if record.is_directory:
            raise IsoFormatError(f"Cannot extract directory: {record.path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        digest = hashlib.sha256()
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                for chunk in self.iter_file(record):
                    stream.write(chunk)
                    digest.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, destination)
        except IsoFormatError:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise IsoFormatError(
                f"Unable to extract {record.path.as_posix()} to {destination}"
            ) from exc
        return digest.hexdigest()
