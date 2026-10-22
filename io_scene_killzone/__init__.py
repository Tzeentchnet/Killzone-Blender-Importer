# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Killzone Asset Tools",
    "author": "Killzone Blender contributors",
    "version": (0, 13, 0),
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
