# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from io_scene_killzone.core.archive_source import KillzoneArchiveSource
from io_scene_killzone.core.core_format import (
    core_header_prefix_size,
    parse_core_header,
)
from io_scene_killzone.core.errors import CatalogFormatError, CoreFormatError
from tests.support import create_fixture_catalog, create_fixture_core


class CoreFormatTests(unittest.TestCase):
    def test_parses_utf8_string_table_and_opaque_body_boundary(self) -> None:
        data = create_fixture_core(
            ("Texture", "Localized.Caf\u00e9"),
            variant=5,
            trailing_padding=4,
        )
        fixed = data[:24]
        required = core_header_prefix_size(fixed, len(data))

        header = parse_core_header(data[:required], len(data))

        self.assertEqual(header.variant, 5)
        self.assertEqual(header.first_type, "Texture")
        self.assertEqual(header.root_type, "Texture")
        self.assertEqual(header.strings[1].utf8, "Localized.Caf\u00e9")
        self.assertEqual(
            header.strings[1].raw,
            "Localized.Caf\u00e9".encode("utf-8"),
        )
        self.assertEqual(header.string_table_padding, 4)
        self.assertEqual(data[header.body_offset:], b"OPAQUE-CORE-BODY")

    def test_rejects_nonzero_string_padding(self) -> None:
        data = bytearray(
            create_fixture_core(("AB",), trailing_padding=0, body=b"")
        )
        data[24 + 4 + 4 + 2] = 1

        with self.assertRaises(CoreFormatError):
            parse_core_header(bytes(data), len(data))


class ArchiveSourceTests(unittest.TestCase):
    def test_reads_catalog_entries_from_container_directory(self) -> None:
        metadata, archive_sizes = create_fixture_catalog()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for package_index, size in archive_sizes.items():
                name = (
                    "FILES.DAT"
                    if package_index == 0
                    else f"FILES{package_index:02d}.DAT"
                )
                payload = bytearray(size)
                if package_index == 0:
                    payload[: len(metadata)] = metadata
                    payload[100:110] = b"ROOT-DATA!"
                if package_index == 3:
                    payload[200:230] = b"M" * 30
                (root / name).write_bytes(payload)

            with KillzoneArchiveSource(root) as source:
                catalog = source.load_catalog()
                root_entry = catalog.entry_by_path("root.txt")
                child_entry = catalog.entry_by_path("models/mesh.mss")

                self.assertEqual(
                    source.read_entry_range(root_entry, 0, 10),
                    b"ROOT-DATA!",
                )
                self.assertEqual(
                    source.read_entry_prefix(child_entry, 4),
                    b"MMMM",
                )
                with self.assertRaises(CatalogFormatError):
                    source.read_entry_range(child_entry, 29, 2)


if __name__ == "__main__":
    unittest.main()
