# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from io_scene_killzone.core.archive_source import KillzoneArchiveSource
from io_scene_killzone.core.errors import CatalogFormatError
from io_scene_killzone.core.killzone_dat import container_name
from io_scene_killzone.core.native_asset_catalog import (
    scan_native_asset_catalog,
)
from tests.support import create_fixture_catalog


def _write_fixture_directory(root: Path) -> dict[int, bytes]:
    metadata, archive_sizes = create_fixture_catalog()
    contents: dict[int, bytes] = {}
    for package_index, size in archive_sizes.items():
        data = bytearray(
            (package_index * 31 + offset) & 0xFF for offset in range(size)
        )
        if package_index == 0:
            data[: len(metadata)] = metadata
        contents[package_index] = bytes(data)
        (root / container_name(package_index)).write_bytes(data)
    return contents


class DirectoryArchiveSourceTests(unittest.TestCase):
    def test_reuses_one_handle_per_container_and_closes_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contents = _write_fixture_directory(root)
            with KillzoneArchiveSource(root) as source:
                entry = source.load_catalog().entry_by_path("models/mesh.mss")
                first = source.read_entry_range(entry, 0, entry.size)
                second = source.read_entry_range(entry, 4, 8)
                self.assertEqual(
                    first,
                    contents[3][entry.offset : entry.offset + entry.size],
                )
                self.assertEqual(second, first[4:12])
                streams = dict(source._streams)
                self.assertEqual(set(streams), {0, 3})
                source.read_entry_range(entry, 0, 1)
                self.assertIs(source._streams[3], streams[3])
            self.assertTrue(all(stream.closed for stream in streams.values()))
            self.assertEqual(source._streams, {})
            with self.assertRaises(CatalogFormatError):
                source.read_entry_range(entry, 0, 1)

    def test_scan_reports_monotonic_progress_ending_at_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_fixture_directory(root)
            fractions: list[float] = []
            catalog = scan_native_asset_catalog(
                root,
                progress=fractions.append,
            )
            self.assertEqual(catalog.source_type, "directory")
            self.assertEqual(fractions[0], 0.0)
            self.assertEqual(fractions[-1], 1.0)
            self.assertEqual(fractions, sorted(fractions))


if __name__ == "__main__":
    unittest.main()
