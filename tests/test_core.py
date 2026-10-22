# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from io_scene_killzone.core.bundle import (
    Bundle,
    BundleValidationError,
    atomic_copy_verified,
)
from io_scene_killzone.core.mesh_format import (
    MeshFormatError,
    parse_mesh,
    serialize_mesh,
)

from tests.support import create_fixture_bundle


class MeshFormatTests(unittest.TestCase):
    def test_parse_serialize_preserves_canonical_mesh_and_opaque_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path, original_bytes, _ = create_fixture_bundle(
                Path(directory)
            )
            bundle = Bundle.load(manifest_path)
            mesh = bundle.read_mesh(bundle.meshes[0])

        self.assertEqual(serialize_mesh(mesh), original_bytes)
        self.assertEqual(mesh.opaque_chunks[0].tag, b"TEST")
        self.assertEqual(mesh.opaque_chunks[0].payload, b"preserve-this")

    def test_truncated_mesh_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, original_bytes, _ = create_fixture_bundle(Path(directory))

        with self.assertRaises(MeshFormatError):
            parse_mesh(original_bytes[:-1])


class BundleTests(unittest.TestCase):
    def test_deep_validation_accepts_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path, _, _ = create_fixture_bundle(Path(directory))
            bundle = Bundle.load(manifest_path)
            bundle.validate()

        self.assertEqual(bundle.meshes[0].id, "sample")

    def test_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path, _, _ = create_fixture_bundle(root)
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["assets"]["meshes"][0]["mesh"]["path"] = "../outside.kzm"
            manifest_path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaises(BundleValidationError):
                Bundle.load(manifest_path)

    def test_mesh_digest_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path, _, _ = create_fixture_bundle(root)
            bundle = Bundle.load(manifest_path)
            bundle.meshes[0].mesh_path.write_bytes(b"corrupt")

            with self.assertRaises(BundleValidationError):
                bundle.read_mesh(bundle.meshes[0])

    def test_verified_copy_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            destination = root / "nested" / "destination.bin"
            data = bytes(range(256)) * 8
            source.write_bytes(data)

            atomic_copy_verified(
                source,
                destination,
                hashlib.sha256(data).hexdigest(),
            )

            self.assertEqual(destination.read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
