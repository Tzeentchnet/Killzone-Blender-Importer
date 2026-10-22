# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from io_scene_killzone.core.errors import IsoFormatError
from io_scene_killzone.core.iso9660 import Iso9660Image
from tests.support import create_fixture_iso
from tools.kz_asset_probe import ARCHIVE_NAMES, probe_source


class Iso9660Tests(unittest.TestCase):
    def test_lists_and_reads_expected_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            iso_path = root / "fixture.iso"
            contents = create_fixture_iso(iso_path)

            with Iso9660Image(iso_path) as image:
                files = {
                    record.path.name: record
                    for record in image.records()
                    if not record.is_directory
                }
                self.assertEqual(image.volume_id, "KZ_TEST_DISC")
                self.assertEqual(set(files), set(ARCHIVE_NAMES))
                for name, expected in contents.items():
                    actual = b"".join(image.iter_file(files[name]))
                    self.assertEqual(actual, expected)

    def test_rejects_disagreeing_endian_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            iso_path = Path(directory) / "corrupt.iso"
            create_fixture_iso(iso_path)
            image = bytearray(iso_path.read_bytes())
            volume_size_big_endian = 16 * 2_048 + 84
            image[volume_size_big_endian : volume_size_big_endian + 4] = (
                b"\x00\x00\x00\x29"
            )
            iso_path.write_bytes(image)

            with self.assertRaises(IsoFormatError):
                with Iso9660Image(iso_path):
                    pass


class AssetProbeTests(unittest.TestCase):
    def test_iso_probe_hashes_and_extracts_exact_containers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            iso_path = root / "fixture.iso"
            extraction_root = root / "extracted"
            contents = create_fixture_iso(iso_path)

            report = probe_source(
                iso_path,
                hash_archives=True,
                extract_to=extraction_root,
            )

            self.assertEqual(report["source_type"], "iso9660")
            self.assertEqual(report["missing"], [])
            self.assertEqual(len(report["archives"]), 5)
            for archive in report["archives"]:
                expected = contents[archive["name"]]
                self.assertEqual(
                    archive["sha256"], hashlib.sha256(expected).hexdigest()
                )
                self.assertEqual(
                    (extraction_root / archive["name"]).read_bytes(),
                    expected,
                )

    def test_directory_probe_reports_missing_containers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = b"one-container"
            (root / "FILES.DAT").write_bytes(data)

            report = probe_source(root, hash_archives=True)

            self.assertEqual(report["source_type"], "directory")
            self.assertEqual(len(report["archives"]), 1)
            self.assertEqual(
                report["archives"][0]["sha256"],
                hashlib.sha256(data).hexdigest(),
            )
            self.assertEqual(
                report["missing"],
                ["FILES01.DAT", "FILES02.DAT", "FILES03.DAT", "FILES04.DAT"],
            )


if __name__ == "__main__":
    unittest.main()
