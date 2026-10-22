# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest

from io_scene_killzone.core.errors import CatalogFormatError
from io_scene_killzone.core.killzone_dat import parse_catalog
from tests.support import create_fixture_catalog
from tools.kz_catalog import catalog_source


class KillzoneCatalogTests(unittest.TestCase):
    def test_parses_recursive_catalog_and_package_mapping(self) -> None:
        metadata, archive_sizes = create_fixture_catalog()

        catalog = parse_catalog(metadata, archive_sizes)

        self.assertEqual(catalog.header.declared_file_count, 2)
        self.assertEqual(catalog.header.unknown_08, 0x11223344)
        self.assertEqual(catalog.header.unknown_0c, 0x55667788)
        self.assertEqual(len(catalog.entries), 2)
        self.assertEqual(len(catalog.directories), 1)
        child = catalog.entry_by_path("models/mesh.mss")
        self.assertEqual(child.package_index, 3)
        self.assertEqual(child.offset, 200)
        self.assertEqual(child.size, 30)
        self.assertEqual(catalog.original_metadata, metadata)

    def test_rejects_file_range_outside_package(self) -> None:
        metadata, archive_sizes = create_fixture_catalog()
        corrupted = bytearray(metadata)
        name_size = struct.unpack_from("<I", corrupted, 24)[0]
        table_start = 28 + name_size + 4
        child_record = table_start + 28
        struct.pack_into("<I", corrupted, child_record + 8, 10_000)

        with self.assertRaises(CatalogFormatError):
            parse_catalog(bytes(corrupted), archive_sizes)

    def test_rejects_orphan_entry_records(self) -> None:
        metadata, archive_sizes = create_fixture_catalog()
        corrupted = bytearray(metadata)
        name_size = struct.unpack_from("<I", corrupted, 24)[0]
        table_start = 28 + name_size + 4
        struct.pack_into("<I", corrupted, table_start, 1)

        with self.assertRaises(CatalogFormatError):
            parse_catalog(bytes(corrupted), archive_sizes)

    def test_directory_source_report_is_deterministic(self) -> None:
        metadata, archive_sizes = create_fixture_catalog()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for package_index, size in archive_sizes.items():
                name = (
                    "FILES.DAT"
                    if package_index == 0
                    else f"FILES{package_index:02d}.DAT"
                )
                payload = (
                    metadata + bytes(size - len(metadata))
                    if package_index == 0
                    else bytes(size)
                )
                (root / name).write_bytes(payload)

            report = catalog_source(root, full=True)

        self.assertEqual(report["catalog"]["file_count"], 2)
        self.assertEqual(report["catalog"]["directory_count"], 1)
        self.assertEqual(
            [entry["path"] for entry in report["entries"]],
            ["models/mesh.mss", "root.txt"],
        )


if __name__ == "__main__":
    unittest.main()
