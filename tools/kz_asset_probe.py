#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from io_scene_killzone.core.bundle import atomic_write_bytes, hash_file
from io_scene_killzone.core.errors import KZAssetError
from io_scene_killzone.core.iso9660 import Iso9660Image, IsoRecord

ARCHIVE_NAMES = (
    "FILES.DAT",
    "FILES01.DAT",
    "FILES02.DAT",
    "FILES03.DAT",
    "FILES04.DAT",
)
_SAMPLE_SIZE = 64 * 1024
_PREFIX_SIZE = 64


class AssetProbeError(KZAssetError):
    """Raised when native container discovery cannot complete safely."""


def _sample_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    length = len(data)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in counts.values()
    )


def _prefix_ascii(prefix: bytes) -> str:
    return "".join(
        chr(value) if 32 <= value < 127 else "."
        for value in prefix
    )


def _archive_report(
    *,
    name: str,
    source_path: str,
    size: int,
    prefix: bytes,
    sample: bytes,
    sha256: str | None,
    extent_lba: int | None,
    extracted_path: Path | None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "name": name,
        "source_path": source_path,
        "size": size,
        "prefix_hex": prefix.hex(),
        "prefix_ascii": _prefix_ascii(prefix),
        "sample_size": len(sample),
        "sample_entropy_bits_per_byte": round(_sample_entropy(sample), 6),
        "sha256": sha256,
    }
    if extent_lba is not None:
        report["extent_lba"] = extent_lba
    if extracted_path is not None:
        report["extracted_path"] = str(extracted_path)
    return report


def _find_iso_archives(
    records: Iterable[IsoRecord],
) -> Mapping[str, IsoRecord]:
    found: dict[str, IsoRecord] = {}
    targets = {name.casefold(): name for name in ARCHIVE_NAMES}
    for record in records:
        if record.is_directory:
            continue
        target_name = targets.get(record.path.name.casefold())
        if target_name is None:
            continue
        if target_name in found:
            raise AssetProbeError(
                f"ISO contains more than one {target_name}: "
                f"{found[target_name].path.as_posix()} and "
                f"{record.path.as_posix()}"
            )
        found[target_name] = record
    return found


def _find_directory_archives(root: Path) -> Mapping[str, Path]:
    targets = {name.casefold(): name for name in ARCHIVE_NAMES}
    found: dict[str, Path] = {}
    try:
        for candidate in root.rglob("*"):
            target_name = targets.get(candidate.name.casefold())
            if target_name is None or not candidate.is_file():
                continue
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as exc:
                raise AssetProbeError(
                    f"Archive path escapes the source directory: {candidate}"
                ) from exc
            if target_name in found:
                raise AssetProbeError(
                    f"Directory contains more than one {target_name}: "
                    f"{found[target_name]} and {resolved}"
                )
            found[target_name] = resolved
    except OSError as exc:
        raise AssetProbeError(f"Unable to enumerate directory: {root}") from exc
    return found


def _read_file_sample(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            return stream.read(_SAMPLE_SIZE)
    except OSError as exc:
        raise AssetProbeError(f"Unable to sample archive: {path}") from exc


def _copy_file_with_hash(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    digest = hashlib.sha256()
    try:
        with source.open("rb") as input_stream:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as output_stream:
                temporary_path = Path(output_stream.name)
                while chunk := input_stream.read(1024 * 1024):
                    output_stream.write(chunk)
                    digest.update(chunk)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        os.replace(temporary_path, destination)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise AssetProbeError(
            f"Unable to extract {source} to {destination}"
        ) from exc
    return digest.hexdigest()


def _probe_iso(
    source: Path,
    *,
    hash_archives: bool,
    extract_to: Path | None,
) -> dict[str, Any]:
    with Iso9660Image(source) as image:
        found = _find_iso_archives(image.records())
        archives = []
        for name in ARCHIVE_NAMES:
            record = found.get(name)
            if record is None:
                continue
            sample = image.read_prefix(record, _SAMPLE_SIZE)
            extracted_path: Path | None = None
            digest: str | None = None
            if extract_to is not None:
                extracted_path = (extract_to / name).resolve()
                digest = image.extract_file(record, extracted_path)
            elif hash_archives:
                digest = image.hash_file(record)
            archives.append(
                _archive_report(
                    name=name,
                    source_path=record.path.as_posix(),
                    size=record.size,
                    prefix=sample[:_PREFIX_SIZE],
                    sample=sample,
                    sha256=digest,
                    extent_lba=record.extent_lba,
                    extracted_path=extracted_path,
                )
            )

        return {
            "report_version": 1,
            "source_type": "iso9660",
            "source_path": str(image.path),
            "source_size": image.path.stat().st_size,
            "volume_id": image.volume_id,
            "archives": archives,
            "missing": [name for name in ARCHIVE_NAMES if name not in found],
        }


def _probe_directory(
    source: Path,
    *,
    hash_archives: bool,
    extract_to: Path | None,
) -> dict[str, Any]:
    try:
        root = source.resolve(strict=True)
    except OSError as exc:
        raise AssetProbeError(f"Source directory does not exist: {source}") from exc
    found = _find_directory_archives(root)
    archives = []

    for name in ARCHIVE_NAMES:
        path = found.get(name)
        if path is None:
            continue
        sample = _read_file_sample(path)
        extracted_path: Path | None = None
        digest: str | None = None
        if extract_to is not None:
            extracted_path = (extract_to / name).resolve()
            if extracted_path == path:
                digest = hash_file(path)
            else:
                digest = _copy_file_with_hash(path, extracted_path)
        elif hash_archives:
            digest = hash_file(path)
        try:
            size = path.stat().st_size
            source_path = path.relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            raise AssetProbeError(f"Unable to inspect archive: {path}") from exc
        archives.append(
            _archive_report(
                name=name,
                source_path=source_path,
                size=size,
                prefix=sample[:_PREFIX_SIZE],
                sample=sample,
                sha256=digest,
                extent_lba=None,
                extracted_path=extracted_path,
            )
        )

    return {
        "report_version": 1,
        "source_type": "directory",
        "source_path": str(root),
        "archives": archives,
        "missing": [name for name in ARCHIVE_NAMES if name not in found],
    }


def probe_source(
    source: str | os.PathLike[str],
    *,
    hash_archives: bool = False,
    extract_to: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    source_path = Path(source).expanduser()
    try:
        resolved = source_path.resolve(strict=True)
    except OSError as exc:
        raise AssetProbeError(f"Source does not exist: {source_path}") from exc

    extraction_root: Path | None = None
    if extract_to is not None:
        extraction_root = Path(extract_to).expanduser().resolve()
        try:
            extraction_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AssetProbeError(
                f"Unable to create extraction directory: {extraction_root}"
            ) from exc

    if resolved.is_dir():
        report = _probe_directory(
            resolved,
            hash_archives=hash_archives,
            extract_to=extraction_root,
        )
    elif resolved.is_file():
        report = _probe_iso(
            resolved,
            hash_archives=hash_archives,
            extract_to=extraction_root,
        )
    else:
        raise AssetProbeError(
            f"Source must be an ISO file or extracted directory: {resolved}"
        )

    if not report["archives"]:
        raise AssetProbeError(
            "No Killzone FILES.DAT or FILES01-04.DAT containers were found"
        )
    return report


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
        raise AssetProbeError("Probe report contains invalid JSON data") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory Killzone FILES.DAT and FILES01-04.DAT containers "
            "without interpreting their unverified internal format."
        )
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Authorized ISO 9660 image or extracted disc directory",
    )
    parser.add_argument(
        "--hash",
        action="store_true",
        dest="hash_archives",
        help="Compute SHA-256 for each discovered container",
    )
    parser.add_argument(
        "--extract-to",
        type=Path,
        help="Copy discovered containers to this private directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the JSON report to this file instead of stdout",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Return success when only some of the five containers are present",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = probe_source(
            args.source,
            hash_archives=args.hash_archives,
            extract_to=args.extract_to,
        )
        encoded = _encode_report(report)
        if args.output is not None:
            atomic_write_bytes(args.output.expanduser().resolve(), encoded)
        else:
            sys.stdout.buffer.write(encoded)
    except (KZAssetError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if report["missing"] and not args.allow_missing:
        print(
            "error: missing expected containers: "
            + ", ".join(report["missing"]),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
