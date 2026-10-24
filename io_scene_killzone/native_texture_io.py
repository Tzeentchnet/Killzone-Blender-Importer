# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

import bpy

from .core.archive_source import KillzoneArchiveSource
from .core.native_texture import NativeTexture, parse_native_texture
from .native_blender_data import create_packed_rgba_image


def create_native_texture_image(
    texture: NativeTexture,
    *,
    source_path: str | Path,
    asset_path: str,
) -> bpy.types.Image:
    image_name = PurePosixPath(asset_path).stem or texture.name
    image = create_packed_rgba_image(
        image_name,
        texture.rgba,
        texture.width,
        texture.height,
        colorspace="sRGB",
    )
    image.use_fake_user = True
    image["kz_archive_source"] = str(Path(source_path).resolve())
    image["kz_archive_path"] = PurePosixPath(asset_path).as_posix()
    image["kz_core_variant"] = texture.header.variant
    image["kz_native_encoding"] = "core-v1-psmt8-csm1"
    image["kz_native_export_supported"] = False
    image["kz_original_core_sha256"] = texture.source_sha256
    image["kz_decoded_rgba_sha256"] = hashlib.sha256(
        texture.rgba
    ).hexdigest()
    image["kz_pixels_packed"] = True
    return image


def import_native_texture(
    source_path: str | Path,
    asset_path: str,
) -> bpy.types.Image:
    with KillzoneArchiveSource(source_path) as source:
        entry = source.load_catalog().entry_by_path(asset_path)
        texture = parse_native_texture(
            source.read_entry_range(entry, 0, entry.size)
        )
        resolved_source = source.source_path
    return create_native_texture_image(
        texture,
        source_path=resolved_source,
        asset_path=asset_path,
    )
