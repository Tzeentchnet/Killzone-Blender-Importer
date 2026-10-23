# SPDX-License-Identifier: GPL-3.0-or-later

ADDON_VERSION = (0, 13, 1)

bl_info = {
    "name": "Killzone Asset Tools",
    "author": "Killzone Blender contributors",
    "version": ADDON_VERSION,
    "blender": (5, 2, 0),
    "location": "3D View > Sidebar > KZ Tools",
    "description": "Import Killzone maps, models, textures, and animations",
    "warning": "Edited native Killzone export is not yet supported",
    "category": "Import-Export",
}


def register() -> None:
    from .operators import register as register_operators

    register_operators()


def unregister() -> None:
    from .operators import unregister as unregister_operators

    unregister_operators()
