#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import struct
import sys
from typing import Any, Mapping, Sequence

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from io_scene_killzone.core.bundle import atomic_write_bytes
from io_scene_killzone.core.errors import CatalogFormatError, KZAssetError
from io_scene_killzone.core.iso9660 import Iso9660Image, IsoRecord
from io_scene_killzone.core.killzone_dat import (
    MAX_METADATA_SIZE,
    KillzoneCatalog,
    container_name,
    parse_catalog,
)
from tools.kz_asset_probe import ARCHIVE_NAMES

_U32 = struct.Struct("<I")
_HEADER_SIZE = 24


class CatalogSourceError(KZAssetError):
    """Raised when the catalog containers cannot be located or read."""


def _iso_containers(records: Sequence[IsoRecord]) -> Mapping[str, IsoRecord]:
    targets = {name.casefold(): name for name in ARCHIVE_NAMES}
    found: dict[str, IsoRecord] = {}
    for record in records:
        if record.is_directory:
            continue
        canonical = targets.get(record.path.name.casefold())
        if canonical is None:
            continue
        if canonical in found:
            raise CatalogSourceError(
                f"ISO contains duplicate {canonical} containers"
            )
        found[canonical] = record
    missing = [name for name in ARCHIVE_NAMES if name not in found]
    if missing:
        raise CatalogSourceError(
            "Missing expected containers: " + ", ".join(missing)
        )
    return found


def _directory_containers(root: Path) -> Mapping[str, Path]:
    targets = {name.casefold(): name for name in ARCHIVE_NAMES}
    found: dict[str, Path] = {}
    try:
        for candidate in root.rglob("*"):
            canonical = targets.get(candidate.name.casefold())
            if canonical is None or not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise CatalogSourceError(
                    f"Container path escapes source directory: {candidate}"
                ) from exc
            if canonical in found:
                raise CatalogSourceError(
                    f"Directory contains duplicate {canonical} containers"
                )
            found[canonical] = resolved
    except OSError as exc:
        raise CatalogSourceError(
            f"Unable to enumerate source directory: {root}"
        ) from exc

    missing = [name for name in ARCHIVE_NAMES if name not in found]
    if missing:
        raise CatalogSourceError(
            "Missing expected containers: " + ", ".join(missing)
        )
    return found


def _required_metadata_size(header: bytes, container_size: int) -> int:
    if len(header) != _HEADER_SIZE:
        raise CatalogSourceError("FILES.DAT header is truncated")
    metadata_size = _U32.unpack_from(header)[0]
    if metadata_size > MAX_METADATA_SIZE:
        raise CatalogFormatError(
            f"Metadata size {metadata_size} exceeds {MAX_METADATA_SIZE}"
        )
    required = _HEADER_SIZE + metadata_size
    if required > container_size:
        raise CatalogSourceError(
            f"FILES.DAT metadata requires {required} bytes, container has "
            f"{container_size}"
        )
    return required


def _read_directory_metadata(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            header = stream.read(_HEADER_SIZE)
            required = _required_metadata_size(header, size)
            remaining = stream.read(required - _HEADER_SIZE)
    except OSError as exc:
        raise CatalogSourceError(
            f"Unable to read FILES.DAT metadata: {path}"
        ) from exc
    if len(remaining) != required - _HEADER_SIZE:
        raise CatalogSourceError("FILES.DAT metadata is truncated")
    return header + remaining


def _package_summary(
    catalog: KillzoneCatalog,
    archive_sizes: Mapping[int, int],
) -> list[dict[str, Any]]:
    counts = Counter(entry.package_index for entry in catalog.entries)
    referenced_bytes = Counter()
    max_end = Counter()
    for entry in catalog.entries:
        referenced_bytes[entry.package_index] += entry.size
        max_end[entry.package_index] = max(
            max_end[entry.package_index], entry.offset + entry.size
        )

    return [
        {
            "package_index": package_index,
            "container": container_name(package_index),
            "container_size": archive_sizes[package_index],
            "file_count": counts[package_index],
            "referenced_bytes": referenced_bytes[package_index],
            "maximum_referenced_end": max_end[package_index],
        }
        for package_index in sorted(archive_sizes)
    ]


def _catalog_report(
    catalog: KillzoneCatalog,
    archive_sizes: Mapping[int, int],
    *,
    source_type: str,
    source_path: Path,
    full: bool,
) -> dict[str, Any]:
    extensions = Counter(
        entry.path.suffix.lower() or "<none>" for entry in catalog.entries
    )
    root_items = sorted(
        [
            entry.path.as_posix()
            for entry in catalog.entries
            if len(entry.path.parts) == 1
        ]
        + [
            directory.path.as_posix()
            for directory in catalog.directories
            if len(directory.path.parts) == 1
        ]
    )
    directory_flags = Counter(
        directory.raw_name_flags & ~((1 << 23) - 1)
        for directory in catalog.directories
    )
    header = catalog.header
    report: dict[str, Any] = {
        "report_version": 1,
        "source_type": source_type,
        "source_path": str(source_path),
        "catalog": {
            "metadata_size": header.metadata_size,
            "metadata_sha256": catalog.metadata_sha256,
            "declared_file_count": header.declared_file_count,
            "file_count": len(catalog.entries),
            "directory_count": len(catalog.directories),
            "root_entry_count": header.root_entry_count,
            "name_table_size": header.name_table_size,
            "entry_table_size": header.entry_table_size,
            "unknown_08": header.unknown_08,
            "unknown_0c": header.unknown_0c,
            "root_items": root_items,
            "packages": _package_summary(catalog, archive_sizes),
            "extensions": [
                {"extension": extension, "count": count}
                for extension, count in sorted(
                    extensions.items(), key=lambda item: (-item[1], item[0])
                )
            ],
            "directory_flag_patterns": [
                {"flags": f"0x{flags:08X}", "count": count}
                for flags, count in sorted(directory_flags.items())
            ],
        },
    }

    if full:
        report["entries"] = [
            {
                "path": entry.path.as_posix(),
                "package_index": entry.package_index,
                "container": container_name(entry.package_index),
                "offset": entry.offset,
                "size": entry.size,
                "name_offset": entry.name_offset,
                "raw_name_flags": f"0x{entry.raw_name_flags:08X}",
                "table_offset": entry.table_offset,
            }
            for entry in sorted(
                catalog.entries, key=lambda item: item.path.as_posix()
            )
        ]
        report["directories"] = [
            {
                "path": directory.path.as_posix(),
                "child_table_offset": directory.child_table_offset,
                "child_count": directory.child_count,
                "name_offset": directory.name_offset,
                "raw_name_flags": f"0x{directory.raw_name_flags:08X}",
                "table_offset": directory.table_offset,
            }
            for directory in sorted(
                catalog.directories, key=lambda item: item.path.as_posix()
            )
        ]
    return report


def catalog_source(
    source: str | os.PathLike[str],
    *,
    full: bool = False,
) -> dict[str, Any]:
    source_path = Path(source).expanduser()
    try:
        resolved = source_path.resolve(strict=True)
    except OSError as exc:
        raise CatalogSourceError(f"Source does not exist: {source_path}") from exc

    if resolved.is_file():
        with Iso9660Image(resolved) as image:
            containers = _iso_containers(image.records())
            main = containers["FILES.DAT"]
            header = image.read_prefix(main, _HEADER_SIZE)
            required = _required_metadata_size(header, main.size)
            metadata = image.read_prefix(main, required)
            archive_sizes = {
                index: containers[container_name(index)].size
                for index in range(5)
            }
            catalog = parse_catalog(metadata, archive_sizes)
            return _catalog_report(
                catalog,
                archive_sizes,
                source_type="iso9660",
                source_path=image.path,
                full=full,
            )

    if resolved.is_dir():
        containers = _directory_containers(resolved)
        archive_sizes = {
            index: containers[container_name(index)].stat().st_size
            for index in range(5)
        }
        metadata = _read_directory_metadata(containers["FILES.DAT"])
        catalog = parse_catalog(metadata, archive_sizes)
        return _catalog_report(
            catalog,
            archive_sizes,
            source_type="directory",
            source_path=resolved,
            full=full,
        )

    raise CatalogSourceError(
        f"Source must be an ISO file or extracted directory: {resolved}"
    )


def _encode_report(report: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                report,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CatalogSourceError(
            "Catalog report contains invalid JSON data"
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and report the Killzone FILES.DAT catalog without "
            "extracting game data."
        )
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Authorized ISO 9660 image or directory containing all five DAT files",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include every file and directory record in the JSON report",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write JSON to this file instead of stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = catalog_source(args.source, full=args.full)
        encoded = _encode_report(report)
        if args.output is None:
            sys.stdout.buffer.write(encoded)
        else:
            atomic_write_bytes(args.output.expanduser().resolve(), encoded)
    except (KZAssetError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
