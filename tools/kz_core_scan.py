#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import statistics
import sys
from typing import Any, Mapping, Sequence

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from io_scene_killzone.core.archive_source import KillzoneArchiveSource
from io_scene_killzone.core.bundle import atomic_write_bytes
from io_scene_killzone.core.core_format import (
    core_header_prefix_size,
    parse_core_header,
)
from io_scene_killzone.core.errors import CoreFormatError, KZAssetError

_CORE_STREAM_MAGIC = b"<CoreStreamData>"
_KEYWORDS = (
    "Animation",
    "Collision",
    "Material",
    "Mesh",
    "Skeleton",
    "Texture",
)


def scan_core_source(
    source_path: str | os.PathLike[str],
    *,
    full: bool = False,
) -> dict[str, Any]:
    with KillzoneArchiveSource(source_path) as source:
        catalog = source.load_catalog()
        entries_by_path = {
            entry.path.as_posix().casefold(): entry
            for entry in catalog.entries
        }
        core_entries = sorted(
            (
                entry
                for entry in catalog.entries
                if entry.path.suffix.lower() == ".core"
            ),
            key=lambda entry: entry.path.as_posix(),
        )

        variants = Counter()
        first_types = Counter()
        pair_matrix = Counter()
        keyword_files = Counter()
        string_counts: list[int] = []
        table_sizes: list[int] = []
        details: list[dict[str, Any]] = []

        for entry in core_entries:
            try:
                fixed = source.read_entry_prefix(entry, 24)
                required = core_header_prefix_size(fixed, entry.size)
                header = parse_core_header(
                    source.read_entry_range(entry, 0, required),
                    entry.size,
                )
            except CoreFormatError as exc:
                raise CoreFormatError(
                    f"{entry.path.as_posix()}: {exc}"
                ) from exc
            variants[header.variant] += 1
            first_types[header.first_type or "<empty>"] += 1
            string_counts.append(len(header.strings))
            table_sizes.append(header.string_table_size)
            for keyword in _KEYWORDS:
                encoded_keyword = keyword.encode("ascii")
                if any(
                    encoded_keyword in value.raw for value in header.strings
                ):
                    keyword_files[keyword] += 1

            stream_path = entry.path.with_suffix(".corestream").as_posix()
            stream_entry = entries_by_path.get(stream_path.casefold())
            has_stream = stream_entry is not None
            pair_matrix[(header.variant, has_stream)] += 1
            if stream_entry is not None:
                magic = source.read_entry_prefix(
                    stream_entry, len(_CORE_STREAM_MAGIC)
                )
                if magic != _CORE_STREAM_MAGIC:
                    raise CoreFormatError(
                        f"Paired stream {stream_path} has invalid magic {magic!r}"
                    )

            if full:
                details.append(
                    {
                        "path": entry.path.as_posix(),
                        "size": entry.size,
                        "variant": header.variant,
                        "first_type": header.first_type,
                        "string_count": len(header.strings),
                        "string_table_size": header.string_table_size,
                        "string_table_padding": header.string_table_padding,
                        "body_offset": header.body_offset,
                        "header_sha256": header.header_sha256,
                        "corestream_path": stream_path if has_stream else None,
                        "corestream_size": (
                            stream_entry.size if stream_entry is not None else None
                        ),
                    }
                )

        def distribution(values: list[int]) -> Mapping[str, int]:
            if not values:
                return {"minimum": 0, "median": 0, "maximum": 0}
            return {
                "minimum": min(values),
                "median": int(statistics.median(values)),
                "maximum": max(values),
            }

        report: dict[str, Any] = {
            "report_version": 2,
            "source_type": source.source_type,
            "source_path": str(source.source_path),
            "core_count": len(core_entries),
            "variant_counts": [
                {"variant": variant, "count": count}
                for variant, count in sorted(variants.items())
            ],
            "pair_matrix": [
                {
                    "variant": variant,
                    "has_corestream": has_stream,
                    "count": count,
                }
                for (variant, has_stream), count in sorted(pair_matrix.items())
            ],
            "string_count": distribution(string_counts),
            "string_table_size": distribution(table_sizes),
            "first_types": [
                {"type": first_type, "count": count}
                for first_type, count in sorted(
                    first_types.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
            "schema_keyword_files": [
                {"keyword": keyword, "count": keyword_files[keyword]}
                for keyword in _KEYWORDS
            ],
        }
        if full:
            report["resources"] = details
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
        raise CoreFormatError("Core report contains invalid JSON data") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Killzone Core headers and symbol tables without "
            "extracting resource bodies."
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
        help="Include one record per Core resource",
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
        report = scan_core_source(args.source, full=args.full)
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
