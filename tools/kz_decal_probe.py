#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from io_scene_killzone.core.bundle import atomic_write_bytes
from io_scene_killzone.core.errors import KZAssetError
from io_scene_killzone.core.native_decal import (
    load_native_decal_library,
)


def probe_native_decals(source_path: Path) -> dict[str, Any]:
    library = load_native_decal_library(source_path)
    return {
        "report_version": 1,
        "resource_library_path": library.core_path,
        "source_core_sha256": library.source_core_sha256,
        "source_corestream_sha256": library.source_corestream_sha256,
        "resource_count": len(library.resources),
        "runtime_geometry_status": (
            "not-serialized-requires-live-marks-and-receivers"
        ),
        "material_policy": (
            "ordinary-native-material-layers-no-decal-preset"
        ),
        "resources": [
            {
                "object_id": f"0x{resource.object_id:08X}",
                "identity_words": (
                    list(resource.identity_words)
                    if resource.identity_words is not None
                    else None
                ),
                "sort_mode": resource.sort_mode.name,
                "sort_order": resource.sort_order,
                "max_decals": resource.max_decals,
                "decal_threshold": resource.decal_threshold,
                "random_rotation_property": resource.random_rotation,
                "min_size": resource.min_size,
                "max_size": resource.max_size,
                "fade_in_time": resource.fade_in_time,
                "fade_time": resource.fade_time,
                "clip_decal": resource.clip_decal,
                "material_token": (
                    f"0x{resource.material_token:08X}"
                ),
                "serialized_blob_hex": resource.raw_blob.hex(),
            }
            for resource in library.resources
        ],
    }


def _encode_report(report: Mapping[str, Any]) -> bytes:
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and report the shared Killzone DecalResource library."
        )
    )
    parser.add_argument(
        "source",
        type=Path,
        help=(
            "Authorized ISO 9660 image or directory containing all five "
            "DAT files"
        ),
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
        encoded = _encode_report(probe_native_decals(args.source))
        if args.output is None:
            sys.stdout.buffer.write(encoded)
        else:
            atomic_write_bytes(args.output.expanduser().resolve(), encoded)
    except (KZAssetError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
