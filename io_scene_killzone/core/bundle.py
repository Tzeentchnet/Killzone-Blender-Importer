# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Any, Mapping, Sequence

from .errors import BundleValidationError
from .mesh_format import MAX_FILE_SIZE, MeshData, parse_mesh

BUNDLE_FORMAT = "killzone-asset-bundle"
BUNDLE_VERSION = 1
MAX_MANIFEST_SIZE = 4 * 1024 * 1024
_ASSET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COORDINATE_SYSTEM = {
    "handedness": "right",
    "up_axis": "Z",
    "forward_axis": "-Y",
    "unit_scale_meters": 1.0,
}


@dataclass(frozen=True, slots=True)
class MaterialSpec:
    name: str
    properties: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SourceBlob:
    relative_path: str
    path: Path
    sha256: str
    format: str


@dataclass(frozen=True, slots=True)
class MeshAsset:
    id: str
    name: str
    mesh_relative_path: str
    mesh_path: Path
    mesh_sha256: str
    materials: tuple[MaterialSpec, ...]
    source: SourceBlob | None


@dataclass(frozen=True, slots=True)
class Bundle:
    manifest_path: Path
    root: Path
    meshes: tuple[MeshAsset, ...]
    raw_manifest: Mapping[str, Any]

    @classmethod
    def load(cls, manifest_path: str | os.PathLike[str]) -> Bundle:
        path = Path(manifest_path).expanduser()
        try:
            path = path.resolve(strict=True)
        except OSError as exc:
            raise BundleValidationError(
                f"Bundle manifest does not exist: {path}"
            ) from exc
        if not path.is_file():
            raise BundleValidationError(f"Bundle manifest is not a file: {path}")

        try:
            manifest_size = path.stat().st_size
        except OSError as exc:
            raise BundleValidationError(
                f"Unable to inspect bundle manifest: {path}"
            ) from exc
        if manifest_size > MAX_MANIFEST_SIZE:
            raise BundleValidationError(
                f"Manifest size {manifest_size} exceeds {MAX_MANIFEST_SIZE} bytes"
            )

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise BundleValidationError(
                f"Unable to read UTF-8 bundle manifest: {path}"
            ) from exc
        try:
            document = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise BundleValidationError(
                f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc

        root = path.parent.resolve(strict=True)
        manifest = _require_mapping(document, "manifest")
        if manifest.get("format") != BUNDLE_FORMAT:
            raise BundleValidationError(
                f"Manifest format must be {BUNDLE_FORMAT!r}"
            )
        version = manifest.get("version")
        if type(version) is not int or version != BUNDLE_VERSION:
            raise BundleValidationError(
                f"Manifest version must be integer {BUNDLE_VERSION}"
            )
        if manifest.get("coordinate_system") != _COORDINATE_SYSTEM:
            raise BundleValidationError(
                "Bundle coordinate_system must be right-handed, Z-up, "
                "-Y-forward, with units in meters"
            )

        assets = _require_mapping(manifest.get("assets"), "assets")
        mesh_entries = _require_sequence(assets.get("meshes"), "assets.meshes")
        meshes: list[MeshAsset] = []
        seen_ids: set[str] = set()

        for index, entry_value in enumerate(mesh_entries):
            label = f"assets.meshes[{index}]"
            entry = _require_mapping(entry_value, label)
            asset_id = _require_string(entry.get("id"), f"{label}.id")
            if not _ASSET_ID.fullmatch(asset_id):
                raise BundleValidationError(
                    f"{label}.id must match {_ASSET_ID.pattern}"
                )
            if asset_id in seen_ids:
                raise BundleValidationError(f"Duplicate mesh id {asset_id!r}")
            seen_ids.add(asset_id)

            name = _require_string(entry.get("name"), f"{label}.name")
            mesh_info = _require_mapping(entry.get("mesh"), f"{label}.mesh")
            mesh_relative_path, mesh_path = _resolve_bundle_file(
                root,
                mesh_info.get("path"),
                f"{label}.mesh.path",
            )
            mesh_digest = _require_sha256(
                mesh_info.get("sha256"), f"{label}.mesh.sha256"
            )

            material_values = _require_sequence(
                entry.get("materials", []), f"{label}.materials"
            )
            materials: list[MaterialSpec] = []
            for material_index, material_value in enumerate(material_values):
                material_label = f"{label}.materials[{material_index}]"
                material = _require_mapping(material_value, material_label)
                material_name = _require_string(
                    material.get("name"), f"{material_label}.name"
                )
                properties = _require_mapping(
                    material.get("properties", {}),
                    f"{material_label}.properties",
                )
                materials.append(
                    MaterialSpec(name=material_name, properties=dict(properties))
                )

            source_value = entry.get("source")
            source: SourceBlob | None = None
            if source_value is not None:
                source_info = _require_mapping(source_value, f"{label}.source")
                source_relative_path, source_path = _resolve_bundle_file(
                    root,
                    source_info.get("path"),
                    f"{label}.source.path",
                )
                source = SourceBlob(
                    relative_path=source_relative_path,
                    path=source_path,
                    sha256=_require_sha256(
                        source_info.get("sha256"), f"{label}.source.sha256"
                    ),
                    format=_require_string(
                        source_info.get("format"), f"{label}.source.format"
                    ),
                )

            meshes.append(
                MeshAsset(
                    id=asset_id,
                    name=name,
                    mesh_relative_path=mesh_relative_path,
                    mesh_path=mesh_path,
                    mesh_sha256=mesh_digest,
                    materials=tuple(materials),
                    source=source,
                )
            )

        return cls(
            manifest_path=path,
            root=root,
            meshes=tuple(meshes),
            raw_manifest=manifest,
        )

    def mesh_by_id(self, asset_id: str) -> MeshAsset:
        for asset in self.meshes:
            if asset.id == asset_id:
                return asset
        raise BundleValidationError(f"Bundle has no mesh with id {asset_id!r}")

    def read_mesh_bytes(self, asset: MeshAsset) -> bytes:
        try:
            size = asset.mesh_path.stat().st_size
        except OSError as exc:
            raise BundleValidationError(
                f"Unable to inspect mesh file: {asset.mesh_path}"
            ) from exc
        if size > MAX_FILE_SIZE:
            raise BundleValidationError(
                f"Mesh {asset.id!r} exceeds {MAX_FILE_SIZE} bytes"
            )
        try:
            data = asset.mesh_path.read_bytes()
        except OSError as exc:
            raise BundleValidationError(
                f"Unable to read mesh file: {asset.mesh_path}"
            ) from exc
        actual_digest = hashlib.sha256(data).hexdigest()
        if actual_digest != asset.mesh_sha256:
            raise BundleValidationError(
                f"Mesh {asset.id!r} SHA-256 mismatch: "
                f"expected {asset.mesh_sha256}, got {actual_digest}"
            )
        return data

    def read_mesh(self, asset: MeshAsset) -> MeshData:
        mesh = parse_mesh(self.read_mesh_bytes(asset))
        material_count = max(1, len(asset.materials))
        for triangle_index, triangle in enumerate(mesh.triangles):
            if triangle.material_index >= material_count:
                raise BundleValidationError(
                    f"Mesh {asset.id!r} triangle {triangle_index} uses material "
                    f"{triangle.material_index}, but only {material_count} slots exist"
                )
        return mesh

    def verify_source(self, asset: MeshAsset) -> None:
        if asset.source is None:
            return
        actual_digest = hash_file(asset.source.path)
        if actual_digest != asset.source.sha256:
            raise BundleValidationError(
                f"Source blob for {asset.id!r} SHA-256 mismatch: "
                f"expected {asset.source.sha256}, got {actual_digest}"
            )

    def validate(self) -> None:
        for asset in self.meshes:
            self.read_mesh(asset)
            self.verify_source(asset)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BundleValidationError(f"Duplicate JSON key {key!r}")
        result[key] = value
    return result


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise BundleValidationError(f"{label} must be an object")
    return value


def _require_sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise BundleValidationError(f"{label} must be an array")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BundleValidationError(f"{label} must be a non-empty string")
    return value


def _require_sha256(value: Any, label: str) -> str:
    digest = _require_string(value, label)
    if not _SHA256.fullmatch(digest):
        raise BundleValidationError(
            f"{label} must be a lowercase hexadecimal SHA-256"
        )
    return digest


def _resolve_bundle_file(
    root: Path, value: Any, label: str
) -> tuple[str, Path]:
    relative_path = _require_string(value, label)
    if "\\" in relative_path:
        raise BundleValidationError(
            f"{label} must use forward slashes as bundle separators"
        )
    pure_path = PurePosixPath(relative_path)
    if pure_path.is_absolute() or any(
        part in {"", ".", ".."} for part in pure_path.parts
    ):
        raise BundleValidationError(
            f"{label} must be a normalized relative bundle path"
        )

    candidate = root.joinpath(*pure_path.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise BundleValidationError(
            f"{label} resolves outside the bundle or does not exist: {relative_path}"
        ) from exc
    if not resolved.is_file():
        raise BundleValidationError(f"{label} is not a file: {relative_path}")
    return pure_path.as_posix(), resolved


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(chunk_size):
                digest.update(chunk)
    except OSError as exc:
        raise BundleValidationError(f"Unable to hash file: {path}") from exc
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise BundleValidationError(f"Unable to write file atomically: {path}") from exc


def atomic_copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    try:
        if source.resolve(strict=True) == destination.resolve(strict=True):
            actual_digest = hash_file(source)
            if actual_digest != expected_sha256:
                raise BundleValidationError(
                    f"Source SHA-256 mismatch: expected {expected_sha256}, "
                    f"got {actual_digest}"
                )
            return
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise BundleValidationError(
            f"Unable to resolve source or destination: {source} -> {destination}"
        ) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    digest = hashlib.sha256()
    try:
        with source.open("rb") as input_stream:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as output_stream:
                temporary_path = Path(output_stream.name)
                while chunk := input_stream.read(1024 * 1024):
                    digest.update(chunk)
                    output_stream.write(chunk)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        actual_digest = digest.hexdigest()
        if actual_digest != expected_sha256:
            temporary_path.unlink(missing_ok=True)
            raise BundleValidationError(
                f"Source SHA-256 mismatch: expected {expected_sha256}, "
                f"got {actual_digest}"
            )
        os.replace(temporary_path, destination)
    except BundleValidationError:
        raise
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise BundleValidationError(
            f"Unable to copy source blob: {source} -> {destination}"
        ) from exc


def write_manifest(path: Path, document: Mapping[str, Any]) -> None:
    try:
        encoded = (
            json.dumps(
                document,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BundleValidationError("Manifest contains invalid JSON data") from exc
    if len(encoded) > MAX_MANIFEST_SIZE:
        raise BundleValidationError(
            f"Manifest size {len(encoded)} exceeds {MAX_MANIFEST_SIZE} bytes"
        )
    atomic_write_bytes(path, encoded)


def make_manifest(
    mesh_entries: Sequence[Mapping[str, Any]], generator: str
) -> dict[str, Any]:
    return {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "generator": generator,
        "coordinate_system": dict(_COORDINATE_SYSTEM),
        "assets": {"meshes": list(mesh_entries)},
    }
