# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from array import array
import hashlib
from pathlib import Path, PurePosixPath

import bpy

from .core.archive_source import KillzoneArchiveSource
from .core.native_texture import NativeTexture, parse_native_texture


def create_native_texture_image(
    texture: NativeTexture,
    *,
    source_path: str | Path,
    asset_path: str,
) -> bpy.types.Image:
    image_name = PurePosixPath(asset_path).stem or texture.name
    image = bpy.data.images.new(
        name=image_name,
        width=texture.width,
        height=texture.height,
        alpha=True,
        float_buffer=False,
    )
    image.alpha_mode = "STRAIGHT"
    image.colorspace_settings.name = "sRGB"

    pixels = array("f")
    row_size = texture.width * 4
    for y in range(texture.height - 1, -1, -1):
        row = texture.rgba[y * row_size : (y + 1) * row_size]
        pixels.extend(component / 255.0 for component in row)
    image.pixels.foreach_set(pixels)
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
    image.update()
    image.pack()
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
