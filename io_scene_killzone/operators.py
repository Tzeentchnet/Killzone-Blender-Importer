# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
import traceback

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)

from .core.errors import CoreFormatError, KZAssetError
from .core.native_animation_resource import load_native_animation_library
from .core.native_asset_catalog import (
    NativeAssetCatalog,
    NativeCachedAnimationBank,
    NativeCachedMap,
    NativeCachedModel,
    scan_native_asset_catalog,
)
from .core.native_level import NativeObjectKey
from .native_animation_io import (
    ANIMATION_WRITER_BULK,
    ANIMATION_WRITER_LEGACY,
    NativeAnimationBakeFailure,
    NativeAnimationBulkWriteError,
    bake_compatible_native_animation_library,
)
from .native_level_io import (
    NativeMaterialPreviewOptions,
    import_native_level,
)
from .native_skinned_io import import_native_skinned_model
from .native_static_io import import_native_static_model


_ADDON_ID = __package__
_NO_ITEMS = "__NONE__"
_NO_BANK = "NONE"
_cached_json = ""
_cached_catalog: NativeAssetCatalog | None = None
_enum_items: dict[str, list[tuple[str, str, str]]] = {}
_ANIMATION_WRITER_ITEMS = (
    (
        ANIMATION_WRITER_BULK,
        "Optimized Bulk FCurves",
        "Create layered FCurves with vectorized keyframes and exact "
        "constant-channel reduction",
    ),
    (
        ANIMATION_WRITER_LEGACY,
        "Legacy Keyframe Insert",
        "Use the original known-working per-bone keyframe_insert writer",
    ),
)
_THIRD_PERSON_CHARACTER_PREFIXES = (
    "entity.helghast.3rd.",
    "entity.isa.3rd.",
    "entity.player.3rd.",
)
_THIRD_PERSON_NON_CHARACTER_TERMS = (
    "apc",
    "boat",
    "craft",
    "cupola",
    "dropship",
    "emplaced",
    "gun",
    "hovertank",
    "jetbike",
    "mrl",
    "sentrybot",
    "turret",
)


def _invalidate_runtime_cache() -> None:
    global _cached_json, _cached_catalog
    _cached_json = ""
    _cached_catalog = None
    _enum_items.clear()


def _addon_preferences(
    context: bpy.types.Context,
) -> KZ_AddonPreferences:
    addon = context.preferences.addons.get(_ADDON_ID)
    if addon is None:
        raise CoreFormatError(
            "Killzone Asset Tools preferences are unavailable"
        )
    return addon.preferences


def _catalog_from_preferences(
    preferences: KZ_AddonPreferences,
) -> NativeAssetCatalog | None:
    global _cached_json, _cached_catalog
    raw = preferences.asset_cache_json
    if not raw:
        return None
    if raw != _cached_json:
        _cached_catalog = NativeAssetCatalog.from_json(raw)
        _cached_json = raw
        _enum_items.clear()
    return _cached_catalog


def _normalized_source(value: str) -> str:
    if not value:
        return ""
    expanded = bpy.path.abspath(value)
    return os.path.normcase(str(Path(expanded).expanduser().resolve()))


def _current_catalog(
    context: bpy.types.Context,
) -> NativeAssetCatalog | None:
    preferences = _addon_preferences(context)
    catalog = _catalog_from_preferences(preferences)
    if catalog is None:
        return None
    if _normalized_source(preferences.archive_source) != os.path.normcase(
        catalog.source_path
    ):
        return None
    return catalog


def _safe_current_catalog(
    context: bpy.types.Context | None,
) -> NativeAssetCatalog | None:
    if context is None:
        return None
    try:
        return _current_catalog(context)
    except KZAssetError:
        return None


def _cached_items(
    key: str,
    values: tuple[object, ...],
    label: Callable[[object], str],
    description: Callable[[object], str],
) -> list[tuple[str, str, str]]:
    cached = _enum_items.get(key)
    if cached is not None:
        return cached
    if not values:
        cached = [(_NO_ITEMS, "No cached assets", "Scan the ISO first")]
    else:
        cached = [
            (str(index), label(value), description(value))
            for index, value in enumerate(values)
        ]
    _enum_items[key] = cached
    return cached


def _map_items(
    _self: object,
    context: bpy.types.Context,
) -> list[tuple[str, str, str]]:
    catalog = _safe_current_catalog(context)
    values = () if catalog is None else catalog.maps
    return _cached_items(
        "maps",
        values,
        lambda value: value.display_name,
        lambda value: value.path,
    )


def _model_items(
    _self: object,
    context: bpy.types.Context,
) -> list[tuple[str, str, str]]:
    catalog = _safe_current_catalog(context)
    values = () if catalog is None else catalog.models
    return _cached_items(
        "models",
        values,
        lambda value: value.display_name,
        lambda value: (
            f"{value.kind.title()} model from {value.path}; "
            f"root 0x{value.root_token:08X}"
        ),
    )


def _animation_bank_items(
    _self: object,
    context: bpy.types.Context,
) -> list[tuple[str, str, str]]:
    catalog = _safe_current_catalog(context)
    values = () if catalog is None else catalog.animation_banks
    key = "animation-banks"
    cached = _enum_items.get(key)
    if cached is None:
        cached = [
            (
                _NO_BANK,
                "None (model Core only)",
                "Bake compatible clips stored with the selected model",
            )
        ]
        cached.extend(
            (
                f"bank:{index}",
                f"{Path(value.path).name} - {value.display_name}",
                value.path,
            )
            for index, value in enumerate(values)
        )
        _enum_items[key] = cached
    return cached


def _animation_bank_selection_label(
    scene: bpy.types.Scene,
    catalog: NativeAssetCatalog,
) -> str:
    identifier = scene.kz_additional_animation_bank
    if identifier == _NO_BANK:
        return "Selected: None (model Core only)"
    if not identifier.startswith("bank:"):
        return "Selected bank is invalid; rescan the ISO"
    try:
        index = int(identifier.removeprefix("bank:"))
    except ValueError:
        return "Selected bank is invalid; rescan the ISO"
    if not 0 <= index < len(catalog.animation_banks):
        return "Selected bank is stale; rescan the ISO"
    bank = catalog.animation_banks[index]
    return (
        f"Selected: {Path(bank.path).name} "
        f"({bank.clip_count:,} clips)"
    )


def _should_suggest_allweapons(model: NativeCachedModel) -> bool:
    file_name = Path(model.path).name.casefold()
    return (
        model.kind == "skinned"
        and model.own_animation_count == 0
        and file_name.startswith(_THIRD_PERSON_CHARACTER_PREFIXES)
        and not any(
            term in file_name
            for term in _THIRD_PERSON_NON_CHARACTER_TERMS
        )
    )


def _selected_index(identifier: str, count: int, label: str) -> int:
    if identifier == _NO_ITEMS:
        raise CoreFormatError(f"No cached {label} is selected")
    try:
        index = int(identifier)
    except ValueError as exc:
        raise CoreFormatError(
            f"Cached {label} selection is invalid; rescan the ISO"
        ) from exc
    if not 0 <= index < count:
        raise CoreFormatError(
            f"Cached {label} selection is stale; rescan the ISO"
        )
    return index


def _selected_map(
    context: bpy.types.Context,
) -> NativeCachedMap:
    catalog = _current_catalog(context)
    if catalog is None:
        raise CoreFormatError("Scan the configured ISO before importing")
    index = _selected_index(
        context.scene.kz_map_asset,
        len(catalog.maps),
        "map",
    )
    return catalog.maps[index]


def _selected_model(
    context: bpy.types.Context,
) -> NativeCachedModel:
    catalog = _current_catalog(context)
    if catalog is None:
        raise CoreFormatError("Scan the configured ISO before importing")
    index = _selected_index(
        context.scene.kz_model_asset,
        len(catalog.models),
        "model",
    )
    return catalog.models[index]


def _animation_banks_for_scene(
    scene: bpy.types.Scene,
    catalog: NativeAssetCatalog,
) -> tuple[NativeCachedAnimationBank, ...]:
    model_index = _selected_index(
        scene.kz_model_asset,
        len(catalog.models),
        "model",
    )
    model = catalog.models[model_index]
    if model.kind != "skinned":
        return ()
    banks_by_path = {
        bank.path.casefold(): bank for bank in catalog.animation_banks
    }
    selected: list[NativeCachedAnimationBank] = []
    if model.own_animation_count:
        own_bank = banks_by_path.get(model.path.casefold())
        if own_bank is None:
            raise CoreFormatError(
                f"Cached model animation bank is missing: {model.path}"
            )
        selected.append(own_bank)
    identifier = scene.kz_additional_animation_bank
    if identifier != _NO_BANK:
        if not identifier.startswith("bank:"):
            raise CoreFormatError(
                "Cached animation-bank selection is invalid; rescan the ISO"
            )
        try:
            bank_index = int(identifier.removeprefix("bank:"))
        except ValueError as exc:
            raise CoreFormatError(
                "Cached animation-bank selection is invalid; rescan the ISO"
            ) from exc
        if not 0 <= bank_index < len(catalog.animation_banks):
            raise CoreFormatError(
                "Cached animation-bank selection is stale; rescan the ISO"
            )
        additional = catalog.animation_banks[bank_index]
        if all(
            bank.path.casefold() != additional.path.casefold()
            for bank in selected
        ):
            selected.append(additional)
    return tuple(selected)


def _animation_clip_selection_key(
    scene: bpy.types.Scene,
    catalog: NativeAssetCatalog,
    banks: tuple[NativeCachedAnimationBank, ...],
) -> str:
    model_index = _selected_index(
        scene.kz_model_asset,
        len(catalog.models),
        "model",
    )
    model = catalog.models[model_index]
    return "|".join(
        (
            catalog.source_path.casefold(),
            model.path.casefold(),
            f"{model.root_token:08X}",
            *(bank.path.casefold() for bank in banks),
        )
    )


def _populate_animation_clips(
    scene: bpy.types.Scene,
    catalog: NativeAssetCatalog,
) -> None:
    scene.kz_animation_clips.clear()
    if not catalog.models or scene.kz_model_asset == _NO_ITEMS:
        scene.kz_animation_clip_selection_key = ""
        return
    banks = _animation_banks_for_scene(scene, catalog)
    scene.kz_animation_clip_selection_key = (
        _animation_clip_selection_key(scene, catalog, banks)
    )
    model_index = _selected_index(
        scene.kz_model_asset,
        len(catalog.models),
        "model",
    )
    model_path = catalog.models[model_index].path.casefold()
    for bank in banks:
        source_label = (
            "Model Core"
            if bank.path.casefold() == model_path
            else Path(bank.path).name.removesuffix(".core")
        )
        for clip in bank.clips:
            item = scene.kz_animation_clips.add()
            item.selected = True
            item.source_path = bank.path
            item.source_label = source_label
            item.object_id = f"0x{clip.object_id:08X}"
            item.section_offset = clip.section_offset
            item.object_index = clip.object_index
            item.duration = clip.duration
            item.playback_mode = clip.playback_mode
            item.display_name = (
                f"{source_label}: 0x{clip.object_id:08X} "
                f"[{clip.section_offset:06X}:{clip.object_index}] "
                f"({clip.duration:.3g}s)"
            )
    scene.kz_animation_clip_index = 0


def _refresh_animation_clips(
    _owner: object,
    context: bpy.types.Context,
) -> None:
    catalog = _safe_current_catalog(context)
    if catalog is None:
        context.scene.kz_animation_clips.clear()
        context.scene.kz_animation_clip_selection_key = ""
        return
    try:
        _populate_animation_clips(context.scene, catalog)
    except KZAssetError:
        context.scene.kz_animation_clips.clear()
        context.scene.kz_animation_clip_selection_key = ""


def _reset_scene_selections(catalog: NativeAssetCatalog) -> None:
    selections = (
        ("kz_map_asset", bool(catalog.maps)),
        ("kz_model_asset", bool(catalog.models)),
    )
    for scene in bpy.data.scenes:
        for property_name, has_items in selections:
            setattr(scene, property_name, "0" if has_items else _NO_ITEMS)
        scene.kz_additional_animation_bank = _NO_BANK
        _populate_animation_clips(scene, catalog)


class KZ_PG_animation_clip(bpy.types.PropertyGroup):
    selected: BoolProperty(name="Import", default=True)
    source_path: StringProperty(name="Source Path")
    source_label: StringProperty(name="Source")
    object_id: StringProperty(name="Object Token")
    section_offset: IntProperty(name="Section Offset", min=0)
    object_index: IntProperty(name="Object Index", min=0)
    duration: FloatProperty(name="Duration", min=0.0)
    playback_mode: IntProperty(name="Playback Mode", min=0)
    display_name: StringProperty(name="Clip")


class KZ_UL_animation_clips(bpy.types.UIList):
    def draw_item(
        self,
        context: bpy.types.Context,
        layout: bpy.types.UILayout,
        data: object,
        item: KZ_PG_animation_clip,
        icon: int,
        active_data: object,
        active_property: str,
        index: int,
        flt_flag: int = 0,
    ) -> None:
        del context, data, icon, active_data, active_property, index, flt_flag
        row = layout.row(align=True)
        row.prop(item, "selected", text="")
        row.label(
            text=item.display_name,
            icon="ACTION",
        )


class KZ_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = _ADDON_ID

    animation_writer: EnumProperty(
        name="Action Writer",
        description="Blender Action writing strategy",
        items=_ANIMATION_WRITER_ITEMS,
        default=ANIMATION_WRITER_BULK,
    )
    animation_fallback_to_legacy: BoolProperty(
        name="Retry with Legacy Writer on Failure",
        description=(
            "Retry a clip with the known-working keyframe_insert writer if "
            "the optimized Blender Action writer fails"
        ),
        default=True,
    )
    animation_residual_tolerance: FloatProperty(
        name="Animation Tolerance",
        description=(
            "Maximum matrix decomposition residual allowed when converting "
            "native animation to Blender channels"
        ),
        default=0.05,
        min=1.0e-5,
        soft_max=0.1,
        precision=5,
    )
    archive_source: StringProperty(
        name="Killzone ISO / DAT Directory",
        description="Authorized Killzone ISO or extracted DAT directory",
        subtype="FILE_PATH",
        default="",
    )
    asset_cache_json: StringProperty(
        name="Native Asset Cache",
        options={"HIDDEN"},
        default="",
    )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.prop(self, "archive_source")
        layout.operator(
            KZ_OT_scan_assets.bl_idname,
            text="Scan / Rescan Importable Assets",
            icon="FILE_REFRESH",
        )
        animation_box = layout.box()
        animation_box.label(text="Animation Import", icon="ACTION")
        animation_box.prop(self, "animation_writer")
        if self.animation_writer == ANIMATION_WRITER_BULK:
            animation_box.prop(self, "animation_fallback_to_legacy")
        animation_box.prop(self, "animation_residual_tolerance")
        if not self.asset_cache_json:
            layout.label(
                text="Choose an ISO or DAT directory, then scan once.",
                icon="INFO",
            )
            return
        try:
            catalog = _catalog_from_preferences(self)
        except KZAssetError as exc:
            box = layout.box()
            box.alert = True
            box.label(text=str(exc), icon="ERROR")
            return
        if catalog is None:
            return
        if _normalized_source(self.archive_source) != os.path.normcase(
            catalog.source_path
        ):
            box = layout.box()
            box.alert = True
            box.label(
                text="The source changed after scanning. Rescan it.",
                icon="ERROR",
            )
            return
        box = layout.box()
        box.label(
            text=(
                f"Cached: {len(catalog.maps)} maps, "
                f"{len(catalog.models)} models, "
                f"{len(catalog.textures)} textures"
            ),
            icon="CHECKMARK",
        )
        box.label(
            text=f"Animation banks: {len(catalog.animation_banks)}"
        )
        if catalog.issues:
            warning = box.row()
            warning.alert = True
            warning.label(
                text=(
                    f"{len(catalog.issues)} unsupported candidate(s) "
                    "were skipped"
                ),
                icon="ERROR",
            )


class KZ_OT_scan_assets(bpy.types.Operator):
    """Scan the configured archive once and persist its importable assets"""

    bl_idname = "kz.scan_assets"
    bl_label = "Scan Importable Assets"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        preferences = _addon_preferences(context)
        if not preferences.archive_source:
            self.report(
                {"ERROR"},
                "Choose a Killzone ISO or DAT directory first",
            )
            return {"CANCELLED"}
        source_path = bpy.path.abspath(preferences.archive_source)
        context.window_manager.progress_begin(0, 1)
        try:
            catalog = scan_native_asset_catalog(source_path)
        except (KZAssetError, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            context.window_manager.progress_end()
        preferences.archive_source = catalog.source_path
        preferences.asset_cache_json = catalog.to_json()
        _invalidate_runtime_cache()
        _catalog_from_preferences(preferences)
        _reset_scene_selections(catalog)
        if catalog.issues:
            self.report(
                {"WARNING"},
                f"Skipped {len(catalog.issues)} unsupported candidate(s)",
            )
        self.report(
            {"INFO"},
            f"Cached {len(catalog.maps)} maps, {len(catalog.models)} models, "
            f"{len(catalog.textures)} textures, and "
            f"{len(catalog.animation_banks)} animation banks",
        )
        return {"FINISHED"}


class KZ_OT_open_preferences(bpy.types.Operator):
    """Open Blender preferences to configure the Killzone archive"""

    bl_idname = "kz.open_preferences"
    bl_label = "Open Add-on Preferences"

    def execute(self, context: bpy.types.Context) -> set[str]:
        context.preferences.active_section = "ADDONS"
        bpy.ops.screen.userpref_show("INVOKE_DEFAULT")
        return {"FINISHED"}


class KZ_OT_select_animation_bank(bpy.types.Operator):
    """Search the cached animation banks and select one for model import"""

    bl_idname = "kz.select_animation_bank"
    bl_label = "Select Additional Animation Bank"
    bl_property = "bank"
    bl_options = {"REGISTER"}

    bank: EnumProperty(
        name="Animation Bank",
        description="Cached shared animation bank to bake with the model",
        items=_animation_bank_items,
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        catalog = _safe_current_catalog(context)
        return catalog is not None and bool(catalog.animation_banks)

    def invoke(
        self,
        context: bpy.types.Context,
        _event: bpy.types.Event,
    ) -> set[str]:
        identifiers = {
            identifier
            for identifier, _label, _description in _animation_bank_items(
                self,
                context,
            )
        }
        current = context.scene.kz_additional_animation_bank
        self.bank = current if current in identifiers else _NO_BANK
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        identifiers = {
            identifier
            for identifier, _label, _description in _animation_bank_items(
                self,
                context,
            )
        }
        if self.bank not in identifiers:
            self.report(
                {"ERROR"},
                "Animation-bank selection is stale; rescan the ISO",
            )
            return {"CANCELLED"}
        context.scene.kz_additional_animation_bank = self.bank
        return {"FINISHED"}


class KZ_OT_set_animation_clip_selection(bpy.types.Operator):
    """Select or clear every cached animation clip"""

    bl_idname = "kz.set_animation_clip_selection"
    bl_label = "Set Animation Clip Selection"
    bl_options = {"REGISTER", "UNDO"}

    selected: BoolProperty(name="Selected", default=True)

    def execute(self, context: bpy.types.Context) -> set[str]:
        for clip in context.scene.kz_animation_clips:
            clip.selected = self.selected
        state = "Selected" if self.selected else "Cleared"
        self.report(
            {"INFO"},
            f"{state} {len(context.scene.kz_animation_clips)} clip(s)",
        )
        return {"FINISHED"}


_ROLLBACK_DATA_COLLECTIONS = (
    "objects",
    "meshes",
    "materials",
    "images",
    "collections",
    "armatures",
    "actions",
)


class _DatablockSnapshot:
    """Remember existing datablocks so a failed import can be removed."""

    def __init__(self) -> None:
        self._before = {
            name: set(getattr(bpy.data, name))
            for name in _ROLLBACK_DATA_COLLECTIONS
        }

    def remove_new(self) -> int:
        created = [
            datablock
            for name in _ROLLBACK_DATA_COLLECTIONS
            for datablock in getattr(bpy.data, name)
            if datablock not in self._before[name]
        ]
        if created:
            bpy.data.batch_remove(created)
        return len(created)


def _run_import_with_rollback(
    operator: bpy.types.Operator,
    context: bpy.types.Context,
    run: Callable[[bpy.types.Context], set[str]],
) -> set[str]:
    """Run an import; on failure remove everything it had created."""

    snapshot = _DatablockSnapshot()
    window_manager = context.window_manager
    window_manager.progress_begin(0.0, 1.0)
    try:
        status = run(context)
    except Exception as exc:
        traceback.print_exc()
        removed = snapshot.remove_new()
        operator.report(
            {"ERROR"},
            f"Import failed with {type(exc).__name__}: {exc} "
            f"({removed} partial datablock(s) removed; see the system "
            "console for details)",
        )
        return {"CANCELLED"}
    finally:
        window_manager.progress_end()
    if "CANCELLED" in status:
        snapshot.remove_new()
    return status


def _material_preview_options(
    scene: bpy.types.Scene,
) -> NativeMaterialPreviewOptions:
    return NativeMaterialPreviewOptions(
        artist_lightmap_layers=scene.kz_material_artist_lightmaps,
        artist_fallback=scene.kz_material_artist_fallback,
    )


def _apply_preview_view_transform(scene: bpy.types.Scene) -> None:
    """Show unlit native colors without AgX tone mapping."""

    if not scene.kz_use_standard_view_transform:
        return
    view_settings = scene.view_settings
    view_settings.view_transform = "Standard"
    view_settings.look = "None"
    view_settings.exposure = 0.0
    view_settings.gamma = 1.0


class KZ_OT_import_map(bpy.types.Operator):
    """Import the selected map's default-LOD static render graph"""

    bl_idname = "kz.import_map"
    bl_label = "Import Map"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        catalog = _safe_current_catalog(context)
        return catalog is not None and bool(catalog.maps)

    def execute(self, context: bpy.types.Context) -> set[str]:
        return _run_import_with_rollback(self, context, self._import)

    def _import(self, context: bpy.types.Context) -> set[str]:
        try:
            catalog = _current_catalog(context)
            if catalog is None:
                raise CoreFormatError(
                    "Scan the configured ISO before importing"
                )
            asset = _selected_map(context)
            result = import_native_level(
                context,
                catalog.source_path,
                asset.path,
                import_textures=context.scene.kz_import_map_textures,
                material_options=_material_preview_options(context.scene),
                progress=context.window_manager.progress_update,
            )
        except (KZAssetError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        context.scene.kz_last_native_level = asset.path
        if context.scene.kz_import_map_textures:
            _apply_preview_view_transform(context.scene)
        total_triangles = (
            result.level.render_plan.direct_triangle_count
            + result.level.render_plan.instance_triangle_count
        )
        texture_status = (
            f"{result.decoded_image_count:,} decoded images, "
            f"{sum(result.material_preview_counts.values()):,} visible "
            "material previews"
            if context.scene.kz_import_map_textures
            else "geometry only"
        )
        visual_prop_count = (
            len(result.static_instance_objects)
            + len(result.collision_instance_objects)
        )
        self.report(
            {"INFO"},
            f"Imported {asset.display_name}: {total_triangles:,} triangles, "
            f"{visual_prop_count:,} "
            "authored visual prop occurrence(s) "
            f"({len(result.static_instance_objects):,} static, "
            f"{len(result.collision_instance_objects):,} collision-backed), "
            f"{result.unique_mesh_count:,} unique leaf "
            f"mesh(es), {texture_status}",
        )
        return {"FINISHED"}


class KZ_OT_import_model(bpy.types.Operator):
    """Import the selected static or skinned native model"""

    bl_idname = "kz.import_model"
    bl_label = "Import Model"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        catalog = _safe_current_catalog(context)
        return catalog is not None and bool(catalog.models)

    def execute(self, context: bpy.types.Context) -> set[str]:
        return _run_import_with_rollback(self, context, self._import)

    def _import(self, context: bpy.types.Context) -> set[str]:
        try:
            catalog = _current_catalog(context)
            if catalog is None:
                raise CoreFormatError(
                    "Scan the configured ISO before importing"
                )
            preferences = _addon_preferences(context)
            asset = _selected_model(context)
            library_requests = []
            selected_clip_count = 0
            if (
                asset.kind == "skinned"
                and context.scene.kz_import_model_animations
            ):
                banks = _animation_banks_for_scene(
                    context.scene,
                    catalog,
                )
                expected_key = _animation_clip_selection_key(
                    context.scene,
                    catalog,
                    banks,
                )
                if (
                    context.scene.kz_animation_clip_selection_key
                    != expected_key
                ):
                    _populate_animation_clips(context.scene, catalog)
                selected_by_path: dict[
                    str, set[NativeObjectKey]
                ] = {}
                for clip in context.scene.kz_animation_clips:
                    if not clip.selected:
                        continue
                    selected_by_path.setdefault(
                        clip.source_path.casefold(),
                        set(),
                    ).add(
                        NativeObjectKey(
                            clip.section_offset,
                            clip.object_index,
                        )
                    )
                selected_clip_count = sum(
                    len(keys) for keys in selected_by_path.values()
                )
                for bank in banks:
                    selected_keys = selected_by_path.get(
                        bank.path.casefold(),
                        set(),
                    )
                    if not selected_keys:
                        continue
                    library = load_native_animation_library(
                        catalog.source_path,
                        bank.path,
                    )
                    selected_clips = tuple(
                        clip
                        for clip in library.clips
                        if clip.key in selected_keys
                    )
                    if len(selected_clips) != len(selected_keys):
                        raise CoreFormatError(
                            f"Cached clip selection for {bank.path} is stale; "
                            "rescan the ISO"
                        )
                    library_requests.append(
                        (library, selected_clips)
                    )

            if asset.kind == "static":
                result = import_native_static_model(
                    context,
                    catalog.source_path,
                    asset.path,
                    root_token=asset.root_token,
                    material_options=_material_preview_options(
                        context.scene
                    ),
                )
                action_count = 0
                incompatible_count = 0
                fallback_count = 0
                failures: list[NativeAnimationBakeFailure] = []
                triangle_count = result.model.triangle_count
                part_count = len(result.mesh_objects)
                root_collection = result.root_collection
            elif asset.kind == "skinned":
                result = import_native_skinned_model(
                    context,
                    catalog.source_path,
                    asset.path,
                    root_token=asset.root_token,
                    material_options=_material_preview_options(
                        context.scene
                    ),
                )
                action_count = 0
                incompatible_count = 0
                fallback_count = 0
                failures = []
                for library, selected_clips in library_requests:
                    batch = bake_compatible_native_animation_library(
                        context.scene,
                        result.armature_object,
                        result.model.skeleton,
                        library,
                        clips=selected_clips,
                        writer=preferences.animation_writer,
                        fallback_to_legacy=(
                            preferences.animation_fallback_to_legacy
                        ),
                        residual_tolerance=(
                            preferences.animation_residual_tolerance
                        ),
                    )
                    action_count += len(batch.actions)
                    incompatible_count += batch.incompatible_clip_count
                    fallback_count += batch.fallback_clip_count
                    failures.extend(batch.failures)
                triangle_count = result.model.triangle_count
                part_count = len(result.mesh_objects)
                root_collection = result.root_collection
            else:
                raise CoreFormatError(
                    f"Unsupported cached model kind {asset.kind!r}"
                )
        except (
            KZAssetError,
            NativeAnimationBulkWriteError,
            ValueError,
        ) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        _apply_preview_view_transform(context.scene)
        root_collection["killzone_animation_action_count"] = action_count
        root_collection["killzone_incompatible_animation_clip_count"] = (
            incompatible_count
        )
        root_collection["killzone_selected_animation_clip_count"] = (
            selected_clip_count
        )
        root_collection["killzone_animation_writer"] = (
            preferences.animation_writer
        )
        root_collection["killzone_animation_fallback_clip_count"] = (
            fallback_count
        )
        root_collection["killzone_animation_failures_json"] = json.dumps(
            [
                {
                    "object_id": f"0x{failure.object_id:08X}",
                    "section_offset": failure.section_offset,
                    "object_index": failure.object_index,
                    "message": failure.message,
                }
                for failure in failures
            ],
            separators=(",", ":"),
        )
        context.scene.kz_last_native_model = asset.path
        if failures:
            self.report(
                {"WARNING"},
                f"{len(failures)} compatible animation clip(s) exceeded "
                "the configured decomposition tolerance",
            )
        if incompatible_count:
            self.report(
                {"WARNING"},
                f"Skipped {incompatible_count} animation clip(s) that do not "
                "cover the complete model Skeleton",
            )
        if fallback_count:
            self.report(
                {"WARNING"},
                f"Used the legacy Action writer for {fallback_count} clip(s) "
                "after an optimized writer failure",
            )
        placement_note = (
            ", standalone definition preview (no map placement context)"
            if asset.kind == "static"
            else ""
        )
        self.report(
            {"INFO"},
            f"Imported {asset.display_name}: {part_count} part(s), "
            f"{triangle_count:,} triangles, {action_count} Action(s)"
            f"{placement_note}",
        )
        return {"FINISHED"}


class KZ_PT_tools(bpy.types.Panel):
    bl_label = "Killzone Asset Tools"
    bl_idname = "KZ_PT_asset_tools"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "KZ Tools"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        try:
            preferences = _addon_preferences(context)
            catalog = _catalog_from_preferences(preferences)
        except KZAssetError as exc:
            box = layout.box()
            box.alert = True
            box.label(text=str(exc), icon="ERROR")
            box.operator(KZ_OT_open_preferences.bl_idname)
            return

        source_matches = (
            catalog is not None
            and _normalized_source(preferences.archive_source)
            == os.path.normcase(catalog.source_path)
        )
        if not source_matches:
            box = layout.box()
            box.label(
                text="Configure the ISO and scan assets once.",
                icon="INFO",
            )
            box.operator(
                KZ_OT_open_preferences.bl_idname,
                icon="PREFERENCES",
            )
            return

        header = layout.row(align=True)
        header.label(
            text=Path(catalog.source_path).name,
            icon="CHECKMARK",
        )
        header.operator(
            KZ_OT_open_preferences.bl_idname,
            text="Settings",
            icon="PREFERENCES",
        )

        material_box = layout.box()
        material_box.label(text="Material Preview", icon="MATERIAL")
        material_box.prop(context.scene, "kz_use_standard_view_transform")
        material_box.prop(context.scene, "kz_material_artist_lightmaps")
        material_box.prop(context.scene, "kz_material_artist_fallback")
        if (
            context.scene.kz_material_artist_lightmaps
            or context.scene.kz_material_artist_fallback
        ):
            material_box.label(
                text="Artist modes are approximations, not native output",
                icon="INFO",
            )

        map_box = layout.box()
        map_box.label(text="Import Map", icon="WORLD_DATA")
        if catalog.maps:
            map_box.prop(context.scene, "kz_map_asset", text="Map")
            map_box.label(
                text="Includes authored static prop placements",
                icon="INFO",
            )
            map_box.prop(context.scene, "kz_import_map_textures")
            if context.scene.kz_import_map_textures:
                map_box.label(
                    text="View textures in Material Preview or Rendered mode",
                    icon="MATERIAL",
                )
            map_box.operator(
                KZ_OT_import_map.bl_idname,
                icon="IMPORT",
            )
        else:
            map_box.label(text="No importable maps were cached")

        model_box = layout.box()
        model_box.label(text="Import Model", icon="MESH_DATA")
        if catalog.models:
            model_box.prop(context.scene, "kz_model_asset", text="Model")
            try:
                selected_model = _selected_model(context)
            except KZAssetError:
                selected_model = None
            if selected_model is not None:
                model_box.label(
                    text=(
                        "Rigged skinned mesh"
                        if selected_model.kind == "skinned"
                        else "Static definition preview (no map placement)"
                    ),
                    icon=(
                        "ARMATURE_DATA"
                        if selected_model.kind == "skinned"
                        else "OBJECT_DATA"
                    ),
                )
                if selected_model.kind == "skinned":
                    model_box.prop(
                        context.scene,
                        "kz_import_model_animations",
                    )
                    if context.scene.kz_import_model_animations:
                        if selected_model.own_animation_count:
                            model_box.label(
                                text=(
                                    f"Model Core: "
                                    f"{selected_model.own_animation_count} "
                                    "clip(s)"
                                )
                            )
                        else:
                            model_box.label(
                                text="Model Core has no animation clips"
                            )
                            if _should_suggest_allweapons(selected_model):
                                hint = model_box.box()
                                hint.label(
                                    text="Shared character Actions may be in:",
                                    icon="INFO",
                                )
                                hint.label(
                                    text="allweapons.default.core"
                                )
                        bank_row = model_box.row(align=True)
                        bank_row.label(text="Additional Bank")
                        bank_row.operator(
                            KZ_OT_select_animation_bank.bl_idname,
                            text=(
                                f"Search {len(catalog.animation_banks)} "
                                "Banks..."
                            ),
                            icon="VIEWZOOM",
                        )
                        model_box.label(
                            text=_animation_bank_selection_label(
                                context.scene,
                                catalog,
                            ),
                            icon="ACTION",
                        )
                        clips = context.scene.kz_animation_clips
                        if clips:
                            selected_count = sum(
                                clip.selected for clip in clips
                            )
                            model_box.label(
                                text=(
                                    f"Animation Clips: {selected_count} / "
                                    f"{len(clips)} selected"
                                ),
                                icon="ACTION",
                            )
                            model_box.template_list(
                                KZ_UL_animation_clips.__name__,
                                "",
                                context.scene,
                                "kz_animation_clips",
                                context.scene,
                                "kz_animation_clip_index",
                                rows=min(8, max(3, len(clips))),
                            )
                            selection_row = model_box.row(align=True)
                            select_all = selection_row.operator(
                                KZ_OT_set_animation_clip_selection.bl_idname,
                                text="All",
                            )
                            select_all.selected = True
                            select_none = selection_row.operator(
                                KZ_OT_set_animation_clip_selection.bl_idname,
                                text="None",
                            )
                            select_none.selected = False
                            if selected_count > 50:
                                warning = model_box.row()
                                warning.alert = True
                                warning.label(
                                    text=(
                                        f"{selected_count} clips selected; "
                                        "import can still take time"
                                    ),
                                    icon="ERROR",
                                )
                        else:
                            model_box.label(
                                text="No animation clips selected",
                                icon="INFO",
                            )
            model_box.operator(
                KZ_OT_import_model.bl_idname,
                icon="IMPORT",
            )
        else:
            model_box.label(text="No importable models were cached")


_CLASSES = (
    KZ_PG_animation_clip,
    KZ_UL_animation_clips,
    KZ_AddonPreferences,
    KZ_OT_scan_assets,
    KZ_OT_open_preferences,
    KZ_OT_select_animation_bank,
    KZ_OT_set_animation_clip_selection,
    KZ_OT_import_map,
    KZ_OT_import_model,
    KZ_PT_tools,
)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.kz_last_manifest = StringProperty(
        name="Last Killzone Manifest",
        subtype="FILE_PATH",
        default="",
    )
    bpy.types.Scene.kz_map_asset = EnumProperty(
        name="Map",
        description="Cached native map to import",
        items=_map_items,
    )
    bpy.types.Scene.kz_import_map_textures = BoolProperty(
        name="Import Map Textures",
        description=(
            "Decode and pack selected native map textures; disable for "
            "geometry-only import"
        ),
        default=True,
    )
    bpy.types.Scene.kz_model_asset = EnumProperty(
        name="Model",
        description="Cached static or skinned native model to import",
        items=_model_items,
        update=_refresh_animation_clips,
    )
    bpy.types.Scene.kz_import_model_animations = BoolProperty(
        name="Import Animations",
        description=(
            "Bake compatible clips from the model Core and optional "
            "additional bank"
        ),
        default=True,
    )
    bpy.types.Scene.kz_additional_animation_bank = EnumProperty(
        name="Additional Animation Bank",
        description="Optional shared animation bank to bake with the model",
        items=_animation_bank_items,
        update=_refresh_animation_clips,
    )
    bpy.types.Scene.kz_animation_clips = CollectionProperty(
        type=KZ_PG_animation_clip,
    )
    bpy.types.Scene.kz_animation_clip_index = IntProperty(
        name="Animation Clip",
        min=0,
        default=0,
    )
    bpy.types.Scene.kz_animation_clip_selection_key = StringProperty(
        name="Animation Clip Selection Key",
        options={"HIDDEN"},
        default="",
    )
    bpy.types.Scene.kz_last_native_level = StringProperty(
        name="Last Native Map",
        default="",
    )
    bpy.types.Scene.kz_last_native_model = StringProperty(
        name="Last Native Model",
        default="",
    )
    bpy.types.Scene.kz_use_standard_view_transform = BoolProperty(
        name="Use Standard View Transform",
        description=(
            "Switch the scene to the Standard view transform after import so "
            "unlit PS2 colors are displayed without AgX tone mapping"
        ),
        default=True,
    )
    bpy.types.Scene.kz_material_artist_lightmaps = BoolProperty(
        name="Artist Lightmap Layers",
        description=(
            "Compose LAYERBUMPDOT3 scalar layers (usually baked shading on "
            "UV1) over WRITE base textures. Artist approximation: not yet "
            "verified against GS output for this family"
        ),
        default=False,
    )
    bpy.types.Scene.kz_material_artist_fallback = BoolProperty(
        name="Artist Fallback Textures",
        description=(
            "Show the first decoded color layer on materials that have no "
            "admitted preview instead of the diagnostic object color"
        ),
        default=False,
    )


def unregister() -> None:
    del bpy.types.Scene.kz_material_artist_fallback
    del bpy.types.Scene.kz_material_artist_lightmaps
    del bpy.types.Scene.kz_use_standard_view_transform
    del bpy.types.Scene.kz_last_native_model
    del bpy.types.Scene.kz_last_native_level
    del bpy.types.Scene.kz_animation_clip_selection_key
    del bpy.types.Scene.kz_animation_clip_index
    del bpy.types.Scene.kz_animation_clips
    del bpy.types.Scene.kz_additional_animation_bank
    del bpy.types.Scene.kz_import_model_animations
    del bpy.types.Scene.kz_model_asset
    del bpy.types.Scene.kz_import_map_textures
    del bpy.types.Scene.kz_map_asset
    del bpy.types.Scene.kz_last_manifest
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _invalidate_runtime_cache()
