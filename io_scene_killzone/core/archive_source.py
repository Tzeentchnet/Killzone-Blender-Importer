# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import struct

from .errors import CatalogFormatError
from .iso9660 import Iso9660Image, IsoRecord
from .killzone_dat import (
    MAX_METADATA_SIZE,
    CatalogEntry,
    KillzoneCatalog,
    container_name,
    parse_catalog,
)

_HEADER_SIZE = 24
_U32 = struct.Struct("<I")


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    package_index: int
    name: str
    size: int


@dataclass(frozen=True, slots=True)
class _ContainerLocation:
    info: ContainerInfo
    iso_record: IsoRecord | None
    path: Path | None


class KillzoneArchiveSource:
    def __init__(self, source: str | os.PathLike[str]) -> None:
        self.source_path = Path(source).expanduser()
        self.source_type = ""
        self._iso: Iso9660Image | None = None
        self._containers: dict[int, _ContainerLocation] = {}
        self._catalog: KillzoneCatalog | None = None
        self._open = False

    def __enter__(self) -> KillzoneArchiveSource:
        try:
            resolved = self.source_path.resolve(strict=True)
        except OSError as exc:
            raise CatalogFormatError(
                f"Archive source does not exist: {self.source_path}"
            ) from exc

        self.source_path = resolved
        try:
            if resolved.is_file():
                self._open_iso(resolved)
            elif resolved.is_dir():
                self._open_directory(resolved)
            else:
                raise CatalogFormatError(
                    f"Archive source must be an ISO or directory: {resolved}"
                )
        except Exception:
            if self._iso is not None:
                self._iso.__exit__(None, None, None)
                self._iso = None
            self._containers.clear()
            raise
        self._open = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._iso is not None:
            self._iso.__exit__(exc_type, exc, traceback)
            self._iso = None
        self._containers.clear()
        self._catalog = None
        self._open = False

    def _open_iso(self, path: Path) -> None:
        image = Iso9660Image(path)
        image.__enter__()
        self._iso = image
        self.source_type = "iso9660"

        targets = {
            container_name(index).casefold(): index for index in range(5)
        }
        for record in image.records():
            if record.is_directory:
                continue
            package_index = targets.get(record.path.name.casefold())
            if package_index is None:
                continue
            if package_index in self._containers:
                raise CatalogFormatError(
                    f"ISO contains duplicate {container_name(package_index)}"
                )
            self._containers[package_index] = _ContainerLocation(
                info=ContainerInfo(
                    package_index=package_index,
                    name=container_name(package_index),
                    size=record.size,
                ),
                iso_record=record,
                path=None,
            )
        self._require_all_containers()

    def _open_directory(self, root: Path) -> None:
        self.source_type = "directory"
        targets = {
            container_name(index).casefold(): index for index in range(5)
        }
        try:
            for candidate in root.rglob("*"):
                package_index = targets.get(candidate.name.casefold())
                if package_index is None or not candidate.is_file():
                    continue
                resolved = candidate.resolve(strict=True)
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise CatalogFormatError(
                        f"Container path escapes source directory: {candidate}"
                    ) from exc
                if package_index in self._containers:
                    raise CatalogFormatError(
                        f"Directory contains duplicate "
                        f"{container_name(package_index)}"
                    )
                self._containers[package_index] = _ContainerLocation(
                    info=ContainerInfo(
                        package_index=package_index,
                        name=container_name(package_index),
                        size=resolved.stat().st_size,
                    ),
                    iso_record=None,
                    path=resolved,
                )
        except OSError as exc:
            raise CatalogFormatError(
                f"Unable to enumerate archive directory: {root}"
            ) from exc
        self._require_all_containers()

    def _require_all_containers(self) -> None:
        missing = [
            container_name(index)
            for index in range(5)
            if index not in self._containers
        ]
        if missing:
            raise CatalogFormatError(
                "Missing expected containers: " + ", ".join(missing)
            )

    def _require_open(self) -> None:
        if not self._open:
            raise CatalogFormatError("Archive source is not open")

    @property
    def containers(self) -> tuple[ContainerInfo, ...]:
        self._require_open()
        return tuple(
            self._containers[index].info for index in sorted(self._containers)
        )

    def read_container_range(
        self,
        package_index: int,
        offset: int,
        size: int,
    ) -> bytes:
        self._require_open()
        location = self._containers.get(package_index)
        if location is None:
            raise CatalogFormatError(f"Missing package {package_index}")
        if offset < 0 or size < 0:
            raise CatalogFormatError(
                "Container range offset and size must not be negative"
            )
        if offset > location.info.size or size > location.info.size - offset:
            raise CatalogFormatError(
                f"Range 0x{offset:X}+0x{size:X} exceeds "
                f"{location.info.name} size 0x{location.info.size:X}"
            )

        if location.iso_record is not None:
            if self._iso is None:
                raise CatalogFormatError("ISO archive source is unavailable")
            return self._iso.read_file_range(
                location.iso_record, offset, size
            )
        if location.path is None:
            raise CatalogFormatError("Directory archive source is unavailable")
        try:
            with location.path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(size)
        except OSError as exc:
            raise CatalogFormatError(
                f"Unable to read {location.info.name}"
            ) from exc
        if len(data) != size:
            raise CatalogFormatError(
                f"Truncated read from {location.info.name} at 0x{offset:X}"
            )
        return data

    def load_catalog(self) -> KillzoneCatalog:
        self._require_open()
        if self._catalog is not None:
            return self._catalog
        header = self.read_container_range(0, 0, _HEADER_SIZE)
        metadata_size = _U32.unpack_from(header)[0]
        if metadata_size > MAX_METADATA_SIZE:
            raise CatalogFormatError(
                f"Metadata size {metadata_size} exceeds {MAX_METADATA_SIZE}"
            )
        required = _HEADER_SIZE + metadata_size
        metadata = self.read_container_range(0, 0, required)
        archive_sizes = {
            info.package_index: info.size for info in self.containers
        }
        self._catalog = parse_catalog(metadata, archive_sizes)
        return self._catalog

    def read_entry_range(
        self,
        entry: CatalogEntry,
        offset: int,
        size: int,
    ) -> bytes:
        if offset < 0 or size < 0:
            raise CatalogFormatError(
                "Entry range offset and size must not be negative"
            )
        if offset > entry.size or size > entry.size - offset:
            raise CatalogFormatError(
                f"Range 0x{offset:X}+0x{size:X} exceeds "
                f"{entry.path.as_posix()} size 0x{entry.size:X}"
            )
        return self.read_container_range(
            entry.package_index,
            entry.offset + offset,
            size,
        )

    def read_entry_prefix(self, entry: CatalogEntry, size: int) -> bytes:
        if size < 0:
            raise CatalogFormatError("Prefix size must not be negative")
        return self.read_entry_range(entry, 0, min(size, entry.size))
