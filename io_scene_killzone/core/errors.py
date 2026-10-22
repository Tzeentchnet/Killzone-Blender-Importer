# SPDX-License-Identifier: GPL-3.0-or-later


class KZAssetError(Exception):
    """Base class for expected Killzone asset tool failures."""


class BundleValidationError(KZAssetError):
    """Raised when a bundle manifest or referenced file is invalid."""


class MeshFormatError(KZAssetError):
    """Raised when an interchange mesh is malformed or unsupported."""


class IsoFormatError(KZAssetError):
    """Raised when an ISO 9660 image is malformed or unsupported."""


class CatalogFormatError(KZAssetError):
    """Raised when a Killzone FILES.DAT catalog is malformed or unsupported."""


class CoreFormatError(KZAssetError):
    """Raised when a Killzone Core resource is malformed or unsupported."""


class AnimationDecodeUnavailableError(KZAssetError):
    """Raised when a proven animation payload still lacks an exact decoder."""


class ExportValidationError(KZAssetError):
    """Raised when Blender data cannot be represented safely."""
