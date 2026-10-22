# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import unittest

from io_scene_killzone.core.errors import CoreFormatError
from io_scene_killzone.core.native_asset_catalog import (
    NativeAssetCatalog,
    NativeAssetScanIssue,
    NativeCachedAnimationBank,
    NativeCachedAnimationClip,
    NativeCachedMap,
    NativeCachedModel,
    NativeCachedTexture,
    native_map_display_name,
    native_map_sort_key,
)


class NativeAssetCatalogTests(unittest.TestCase):
    def test_sorts_campaign_levels_numerically_before_multiplayer(self) -> None:
        paths = (
            "localcache.ps2/lumps/level.10.mountainpass.core",
            "localcache.ps2/lumps/level.mp_level2.core",
            "localcache.ps2/lumps/level.2.courtyard.core",
            "localcache.ps2/lumps/level.1.trenches.core",
            "localcache.ps2/lumps/level.mp_level1.core",
            "localcache.ps2/lumps/level.11.controlroom.core",
        )

        self.assertEqual(
            tuple(sorted(paths, key=native_map_sort_key)),
            (
                "localcache.ps2/lumps/level.1.trenches.core",
                "localcache.ps2/lumps/level.2.courtyard.core",
                "localcache.ps2/lumps/level.10.mountainpass.core",
                "localcache.ps2/lumps/level.11.controlroom.core",
                "localcache.ps2/lumps/level.mp_level1.core",
                "localcache.ps2/lumps/level.mp_level2.core",
            ),
        )

    def test_uses_retail_chapter_and_multiplayer_names(self) -> None:
        self.assertEqual(
            native_map_display_name(
                "localcache.ps2/lumps/level.1.hqgrounds.core"
            ),
            "1. Helghast Assault - HQ Grounds",
        )
        self.assertEqual(
            native_map_display_name(
                "localcache.ps2/lumps/level.10.mountainpass.core"
            ),
            "10. Onwards and Upwards - Mountain Pass",
        )
        self.assertEqual(
            native_map_display_name(
                "localcache.ps2/lumps/level.mp_level8.core"
            ),
            "Multiplayer 8. Beach Head",
        )

    def test_round_trips_persisted_cache(self) -> None:
        catalog = NativeAssetCatalog(
            source_path=r"D:\Games\Killzone.iso",
            source_type="iso9660",
            container_sizes=(1, 2, 3, 4, 5),
            maps=(
                NativeCachedMap(
                    path="level.1.trenches.core",
                    corestream_path="level.1.trenches.corestream",
                    display_name="1. Helghast Assault - Trenches",
                    core_size=100,
                    corestream_size=200,
                ),
            ),
            textures=(
                NativeCachedTexture(
                    path="background.load.1.core",
                    display_name="Loading 1",
                    texture_name="Loading 1",
                    size=300,
                ),
            ),
            models=(
                NativeCachedModel(
                    path="entity.helghast.3rd.soldier.core",
                    corestream_path=(
                        "entity.helghast.3rd.soldier.corestream"
                    ),
                    display_name="Helghast 3rd Soldier (Skinned)",
                    kind="skinned",
                    root_token=0x0053D442,
                    root_section_offset=0x1234,
                    root_object_index=5,
                    own_animation_count=4,
                ),
            ),
            animation_banks=(
                NativeCachedAnimationBank(
                    path="allweapons.default.core",
                    display_name="Allweapons Default (1 clip)",
                    clip_count=1,
                    clips=(
                        NativeCachedAnimationClip(
                            object_id=0x0054D51A,
                            section_offset=0x1721C0,
                            object_index=437,
                            duration=3.4166667,
                            playback_mode=2,
                        ),
                    ),
                ),
            ),
            issues=(
                NativeAssetScanIssue(
                    path="unsupported.core",
                    message="Unsupported layout",
                ),
            ),
        )

        self.assertEqual(
            NativeAssetCatalog.from_json(catalog.to_json()),
            catalog,
        )

    def test_rejects_stale_cache_version(self) -> None:
        with self.assertRaisesRegex(CoreFormatError, "version"):
            NativeAssetCatalog.from_json(
                json.dumps(
                    {
                        "cache_version": 999,
                        "source_path": "",
                        "source_type": "",
                        "container_sizes": [],
                        "maps": [],
                        "textures": [],
                        "models": [],
                        "animation_banks": [],
                        "issues": [],
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
