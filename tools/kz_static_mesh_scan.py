#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from io_scene_killzone.core.bundle import atomic_write_bytes
from io_scene_killzone.core.errors import CoreFormatError, KZAssetError
from io_scene_killzone.core.native_static_mesh import (
    NativeStaticMeshAsset,
    scan_native_static_meshes,
)


def _asset_report(asset: NativeStaticMeshAsset) -> dict[str, Any]:
    layout = asset.layout
    return {
        "path": asset.path,
        "corestream_path": asset.corestream_path,
        "core_size": asset.core_size,
        "corestream_size": asset.corestream_size,
        "core_sha256": layout.source_core_sha256,
        "corestream_sha256": layout.source_corestream_sha256,
        "stream_chunk_sizes": list(layout.stream_chunk_sizes),
        "static_stream_end_offset": layout.static_stream_end_offset,
        "corestream_trailing_size": layout.corestream_trailing_size,
        "table_end_offset": layout.table_end_offset,
        "root_flags": list(layout.root_flags),
        "mesh_records": [
            {
                "index": index,
                "opaque_reference": record.opaque_reference,
                "opaque_word": record.opaque_word,
                "packet_count": record.packet_count,
                "flag_a": record.flag_a,
                "flag_b": record.flag_b,
                "opaque_u16": list(record.opaque_u16),
            }
            for index, record in enumerate(layout.records)
        ],
        "packets": [
            {
                "mesh_record_index": packet.mesh_record_index,
                "packet_index": packet.packet_index,
                "stream_chunk_index": packet.stream_chunk_index,
                "relative_offset": packet.relative_offset,
                "absolute_offset": packet.absolute_offset,
                "bounded_size": packet.bounded_size,
                "opaque_metadata": packet.opaque_metadata.hex(),
                "first_word": packet.first_word,
                "span_sha256": packet.span_sha256,
            }
            for packet in layout.packets
        ],
    }


def scan_static_mesh_source(
    source_path: str | os.PathLike[str],
) -> dict[str, Any]:
    resolved = Path(source_path).expanduser().resolve(strict=True)
    assets = scan_native_static_meshes(resolved)
    return {
        "report_version": 1,
        "source_path": str(resolved),
        "supported_leading_resource_count": len(assets),
        "mesh_record_count": sum(
            len(asset.layout.records) for asset in assets
        ),
        "packet_count": sum(
            len(asset.layout.packets) for asset in assets
        ),
        "resources": [_asset_report(asset) for asset in assets],
    }


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
        raise CoreFormatError(
            "Static mesh report contains invalid JSON data"
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the bounded packet directory in supported leading "
            "RegularStaticMeshResource Core/CoreStream pairs. Geometry "
            "packet bodies remain opaque."
        )
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Authorized ISO 9660 image or directory containing all five DAT files",
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
        report = scan_static_mesh_source(args.source)
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
