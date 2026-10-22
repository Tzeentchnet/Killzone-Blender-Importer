# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from .archive_source import KillzoneArchiveSource
from .core_format import core_header_prefix_size, parse_core_header
from .errors import CoreFormatError
from .native_level import (
    expand_native_static_resource,
    native_static_resource_roots,
    parse_native_core_sections,
    parse_native_static_resources,
)
from .native_animation_resource import discover_native_animation_clips
from .native_skinned_model import discover_native_skinned_model_roots
from .native_texture import parse_native_texture


NATIVE_ASSET_CACHE_VERSION = 2

_CAMPAIGN_NAMES = {
    1: "Helghast Assault",
    2: "Vekta Evacuates",
    3: "New Allies",
    4: "Strange Company",
    5: "Escape",
    6: "Misty Waters",
    7: "Hunting the Traitor",
    8: "Forging a Path",
    9: "Hidden Pasts",
    10: "Onwards and Upwards",
    11: "Hope",
}

_MULTIPLAYER_NAMES = {
    1: "Delta Creek",
    2: "Canyon Crossing",
    3: "Vektan Slums",
    4: "Core Shaft",
    5: "Park Terminal",
    6: "Orbital Station",
    7: "Southern Hills",
    8: "Beach Head",
}

_AREA_NAMES = {
    "hqgrounds": "HQ Grounds",
    "lightindustry": "Light Industry",
    "park2": "Park 2",
    "street1": "Street 1",
    "street2": "Street 2",
    "docks1": "Docks 1",
    "docks2": "Docks 2",
    "fields1": "Fields 1",
    "fields2": "Fields 2",
    "1": "Area 1",
    "2": "Area 2",
    "3": "Area 3",
    "forwardlogisticsbase": "Forward Logistics Base",
    "junglevalley": "Jungle Valley",
    "alpinevalley": "Alpine Valley",
    "mountainpass": "Mountain Pass",
    "weatherstation": "Weather Station",
    "controlroom": "Control Room",
    "sddockingbay": "SD Docking Bay",
    "sdplatform_choyan": "SD Platform - Choyan",
    "sdplatform_hakha": "SD Platform - Hakha",
    "sdplatform_luger": "SD Platform - Luger",
    "sdplatform_templar": "SD Platform - Templar",
}

_CAMPAIGN_PATTERN = re.compile(
    r"^level\.(?P<chapter>\d+)\.(?P<area>.+)\.core$",
    re.IGNORECASE,
)
_MULTIPLAYER_PATTERN = re.compile(
    r"^level\.mp_level(?P<index>\d+)\.core$",
    re.IGNORECASE,
)
_NATURAL_PART = re.compile(r"(\d+)")

_STATIC_TYPES = {
    "RegularStaticMeshResource",
    "MultiStaticMeshResource",
    "LodStaticMeshResource",
}
_SKINNED_TYPES = {
    "RegularSkinnedMeshResource",
    "MultiSkinnedMeshResource",
    "LodSkinnedMeshResource",
}
_ANIMATION_TYPES = {
    "SkeletonAnimationBoneMap",
    "SkeletonAnimationResource",
}
_CANDIDATE_TYPES = _STATIC_TYPES | _SKINNED_TYPES | _ANIMATION_TYPES


@dataclass(frozen=True, slots=True)
class NativeCachedMap:
    path: str
    corestream_path: str
    display_name: str
    core_size: int
    corestream_size: int


@dataclass(frozen=True, slots=True)
class NativeCachedTexture:
    path: str
    display_name: str
    texture_name: str
    size: int


@dataclass(frozen=True, slots=True)
class NativeCachedModel:
    path: str
    corestream_path: str
    display_name: str
    kind: str
    root_token: int
    root_section_offset: int
    root_object_index: int
    own_animation_count: int


@dataclass(frozen=True, slots=True)
class NativeCachedAnimationClip:
    object_id: int
    section_offset: int
    object_index: int
    duration: float
    playback_mode: int


@dataclass(frozen=True, slots=True)
class NativeCachedAnimationBank:
    path: str
    display_name: str
    clip_count: int
    clips: tuple[NativeCachedAnimationClip, ...]


@dataclass(frozen=True, slots=True)
class NativeAssetScanIssue:
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class NativeAssetCatalog:
    source_path: str
    source_type: str
    container_sizes: tuple[int, ...]
    maps: tuple[NativeCachedMap, ...]
    textures: tuple[NativeCachedTexture, ...]
    models: tuple[NativeCachedModel, ...]
    animation_banks: tuple[NativeCachedAnimationBank, ...]
    issues: tuple[NativeAssetScanIssue, ...]

    def to_json(self) -> str:
        document = {
            "cache_version": NATIVE_ASSET_CACHE_VERSION,
            "source_path": self.source_path,
            "source_type": self.source_type,
            "container_sizes": list(self.container_sizes),
            "maps": [asdict(value) for value in self.maps],
            "textures": [asdict(value) for value in self.textures],
            "models": [asdict(value) for value in self.models],
            "animation_banks": [
                asdict(value) for value in self.animation_banks
            ],
            "issues": [asdict(value) for value in self.issues],
        }
        return json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, value: str) -> NativeAssetCatalog:
        try:
            document = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CoreFormatError("Native asset cache is not valid JSON") from exc
        if not isinstance(document, Mapping):
            raise CoreFormatError("Native asset cache root must be an object")
        if document.get("cache_version") != NATIVE_ASSET_CACHE_VERSION:
            raise CoreFormatError(
                "Native asset cache version is unsupported; rescan the ISO"
            )

        def require_string(name: str) -> str:
            result = document.get(name)
            if not isinstance(result, str):
                raise CoreFormatError(
                    f"Native asset cache field {name!r} must be text"
                )
            return result

        def require_int_tuple(name: str) -> tuple[int, ...]:
            result = document.get(name)
            if (
                not isinstance(result, list)
                or any(
                    isinstance(item, bool) or not isinstance(item, int)
                    for item in result
                )
            ):
                raise CoreFormatError(
                    f"Native asset cache field {name!r} must contain integers"
                )
            return tuple(result)

        def records(name: str, record_type: type[Any]) -> tuple[Any, ...]:
            result = document.get(name)
            if not isinstance(result, list):
                raise CoreFormatError(
                    f"Native asset cache field {name!r} must be an array"
                )
            parsed: list[Any] = []
            for index, item in enumerate(result):
                if not isinstance(item, Mapping):
                    raise CoreFormatError(
                        f"Native asset cache {name}[{index}] must be an object"
                    )
                try:
                    parsed.append(record_type(**dict(item)))
                except (TypeError, ValueError) as exc:
                    raise CoreFormatError(
                        f"Native asset cache {name}[{index}] is invalid"
                    ) from exc
            return tuple(parsed)

        raw_banks = document.get("animation_banks")
        if not isinstance(raw_banks, list):
            raise CoreFormatError(
                "Native asset cache field 'animation_banks' must be an array"
            )
        animation_banks: list[NativeCachedAnimationBank] = []
        for index, item in enumerate(raw_banks):
            if not isinstance(item, Mapping):
                raise CoreFormatError(
                    f"Native asset cache animation_banks[{index}] "
                    "must be an object"
                )
            raw_clips = item.get("clips")
            if not isinstance(raw_clips, list):
                raise CoreFormatError(
                    f"Native asset cache animation_banks[{index}].clips "
                    "must be an array"
                )
            try:
                clips = tuple(
                    NativeCachedAnimationClip(**dict(clip))
                    for clip in raw_clips
                    if isinstance(clip, Mapping)
                )
                if len(clips) != len(raw_clips):
                    raise TypeError("Animation clip entry must be an object")
                bank_values = dict(item)
                bank_values["clips"] = clips
                bank = NativeCachedAnimationBank(**bank_values)
                if bank.clip_count != len(bank.clips):
                    raise ValueError(
                        "Animation bank clip count does not match metadata"
                    )
                animation_banks.append(bank)
            except (TypeError, ValueError) as exc:
                raise CoreFormatError(
                    f"Native asset cache animation_banks[{index}] is invalid"
                ) from exc

        return cls(
            source_path=require_string("source_path"),
            source_type=require_string("source_type"),
            container_sizes=require_int_tuple("container_sizes"),
            maps=records("maps", NativeCachedMap),
            textures=records("textures", NativeCachedTexture),
            models=records("models", NativeCachedModel),
            animation_banks=tuple(animation_banks),
            issues=records("issues", NativeAssetScanIssue),
        )


def _natural_key(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in _NATURAL_PART.split(value)
        if part
    )


def _friendly_words(value: str) -> str:
    replacements = {
        "hq": "HQ",
        "ots": "OTS",
        "sd": "SD",
        "isa": "ISA",
        "apc": "APC",
    }
    words = re.split(r"[._-]+", value)
    expanded: list[str] = []
    for word in words:
        numbered = re.fullmatch(r"([A-Za-z]+)(\d+)", word)
        if numbered is None:
            expanded.append(word)
        else:
            expanded.extend(numbered.groups())
    return " ".join(
        replacements.get(word.casefold(), word.capitalize())
        for word in expanded
        if word
    )


def native_map_sort_key(path: str) -> tuple[Any, ...]:
    name = PurePosixPath(path).name
    campaign = _CAMPAIGN_PATTERN.match(name)
    if campaign is not None:
        return (
            0,
            int(campaign.group("chapter")),
            _natural_key(campaign.group("area")),
        )
    multiplayer = _MULTIPLAYER_PATTERN.match(name)
    if multiplayer is not None:
        return (1, int(multiplayer.group("index")))
    return (2, _natural_key(name))


def native_map_display_name(path: str) -> str:
    name = PurePosixPath(path).name
    campaign = _CAMPAIGN_PATTERN.match(name)
    if campaign is not None:
        chapter = int(campaign.group("chapter"))
        chapter_name = _CAMPAIGN_NAMES.get(chapter, f"Chapter {chapter}")
        area_key = campaign.group("area").casefold()
        area = _AREA_NAMES.get(area_key, _friendly_words(area_key))
        return f"{chapter}. {chapter_name} - {area}"
    multiplayer = _MULTIPLAYER_PATTERN.match(name)
    if multiplayer is not None:
        index = int(multiplayer.group("index"))
        retail_name = _MULTIPLAYER_NAMES.get(index, f"Map {index}")
        return f"Multiplayer {index}. {retail_name}"
    return _friendly_words(name.removesuffix(".core"))


def _model_base_name(path: str) -> str:
    stem = PurePosixPath(path).name.removesuffix(".core")
    for prefix in ("entity.", "custom.", "weapon.", "pickup."):
        if stem.casefold().startswith(prefix):
            stem = stem[len(prefix) :]
            break
    return _friendly_words(stem)


def _model_display_name(
    path: str,
    kind: str,
    root_token: int,
    *,
    include_token: bool,
) -> str:
    kind_label = "Skinned" if kind == "skinned" else "Static"
    suffix = (
        f", 0x{root_token:08X}" if include_token else ""
    )
    return f"{_model_base_name(path)} ({kind_label}{suffix})"


def _animation_bank_display_name(path: str, clip_count: int) -> str:
    stem = PurePosixPath(path).name.removesuffix(".core")
    label = _friendly_words(stem)
    noun = "clip" if clip_count == 1 else "clips"
    return f"{label} ({clip_count} {noun})"


def scan_native_asset_catalog(
    source_path: str | Path,
) -> NativeAssetCatalog:
    maps: list[NativeCachedMap] = []
    textures: list[NativeCachedTexture] = []
    models: list[NativeCachedModel] = []
    animation_banks: list[NativeCachedAnimationBank] = []
    issues: list[NativeAssetScanIssue] = []

    with KillzoneArchiveSource(source_path) as source:
        catalog = source.load_catalog()
        entries_by_path = {
            entry.path.as_posix().casefold(): entry
            for entry in catalog.entries
        }
        core_entries = tuple(
            sorted(
                (
                    entry
                    for entry in catalog.entries
                    if entry.path.suffix.casefold() == ".core"
                ),
                key=lambda entry: _natural_key(entry.path.as_posix()),
            )
        )

        for entry in core_entries:
            path = entry.path.as_posix()
            name = entry.path.name.casefold()
            stream_path = entry.path.with_suffix(".corestream").as_posix()
            stream_entry = entries_by_path.get(stream_path.casefold())
            if (
                name.startswith("level.")
                and not name.endswith(".missionscript.core")
            ):
                if stream_entry is None:
                    issues.append(
                        NativeAssetScanIssue(
                            path=path,
                            message="Paired CoreStream is missing",
                        )
                    )
                else:
                    maps.append(
                        NativeCachedMap(
                            path=path,
                            corestream_path=stream_path,
                            display_name=native_map_display_name(path),
                            core_size=entry.size,
                            corestream_size=stream_entry.size,
                        )
                    )
                continue

            try:
                fixed = source.read_entry_prefix(entry, 24)
                required = core_header_prefix_size(fixed, entry.size)
                header = parse_core_header(
                    source.read_entry_range(entry, 0, required),
                    entry.size,
                )
            except CoreFormatError as exc:
                issues.append(
                    NativeAssetScanIssue(path=path, message=str(exc))
                )
                continue

            if header.variant == 1 and header.first_type == "Texture":
                try:
                    texture = parse_native_texture(
                        source.read_entry_range(entry, 0, entry.size)
                    )
                except CoreFormatError as exc:
                    issues.append(
                        NativeAssetScanIssue(path=path, message=str(exc))
                    )
                else:
                    textures.append(
                        NativeCachedTexture(
                            path=path,
                            display_name=texture.name,
                            texture_name=texture.name,
                            size=entry.size,
                        )
                    )
                continue

            string_types = {
                value.utf8
                for value in header.strings
                if value.utf8 is not None
            }
            if (
                header.variant != 5
                or stream_entry is None
                or not string_types.intersection(_CANDIDATE_TYPES)
            ):
                continue

            try:
                core_data = source.read_entry_range(entry, 0, entry.size)
                _, sections = parse_native_core_sections(
                    core_data,
                    stream_entry.size,
                )
            except CoreFormatError as exc:
                issues.append(
                    NativeAssetScanIssue(path=path, message=str(exc))
                )
                continue

            objects = tuple(
                obj for section in sections for obj in section.objects
            )
            clip_metadata = discover_native_animation_clips(sections)
            clip_count = len(clip_metadata)
            bone_map_count = sum(
                len(section.objects)
                for section in sections
                if section.type_name == "SkeletonAnimationBoneMap"
            )
            if clip_count and bone_map_count:
                animation_banks.append(
                    NativeCachedAnimationBank(
                        path=path,
                        display_name=_animation_bank_display_name(
                            path,
                            clip_count,
                        ),
                        clip_count=clip_count,
                        clips=tuple(
                            NativeCachedAnimationClip(
                                object_id=clip.object_id,
                                section_offset=clip.key.section_offset,
                                object_index=clip.key.object_index,
                                duration=clip.duration,
                                playback_mode=clip.playback_mode,
                            )
                            for clip in clip_metadata
                        ),
                    )
                )

            pending_models: list[
                tuple[str, int, int, int]
            ] = []
            try:
                static_resources = parse_native_static_resources(objects)
                static_roots = native_static_resource_roots(
                    static_resources
                )
                static_nodes = {
                    node.key: node for node in static_resources
                }
                static_token_counts = Counter(
                    static_nodes[key].object_id for key in static_roots
                )
                for key in static_roots:
                    node = static_nodes[key]
                    if static_token_counts[node.object_id] != 1:
                        continue
                    expand_native_static_resource(key, static_resources)
                    pending_models.append(
                        (
                            "static",
                            node.object_id,
                            key.section_offset,
                            key.object_index,
                        )
                    )
            except CoreFormatError as exc:
                issues.append(
                    NativeAssetScanIssue(
                        path=path,
                        message=f"Static model scan: {exc}",
                    )
                )

            try:
                skinned_roots = discover_native_skinned_model_roots(
                    sections
                )
            except CoreFormatError as exc:
                issues.append(
                    NativeAssetScanIssue(
                        path=path,
                        message=f"Skinned model scan: {exc}",
                    )
                )
                skinned_roots = ()
            for root in skinned_roots:
                pending_models.append(
                    (
                        "skinned",
                        root.object_id,
                        root.key.section_offset,
                        root.key.object_index,
                    )
                )

            root_kind_counts = Counter(
                kind for kind, _, _, _ in pending_models
            )
            for kind, token, section_offset, object_index in pending_models:
                models.append(
                    NativeCachedModel(
                        path=path,
                        corestream_path=stream_path,
                        display_name=_model_display_name(
                            path,
                            kind,
                            token,
                            include_token=root_kind_counts[kind] > 1,
                        ),
                        kind=kind,
                        root_token=token,
                        root_section_offset=section_offset,
                        root_object_index=object_index,
                        own_animation_count=clip_count,
                    )
                )

        return NativeAssetCatalog(
            source_path=str(source.source_path),
            source_type=source.source_type,
            container_sizes=tuple(
                info.size for info in source.containers
            ),
            maps=tuple(
                sorted(maps, key=lambda asset: native_map_sort_key(asset.path))
            ),
            textures=tuple(
                sorted(textures, key=lambda asset: _natural_key(asset.path))
            ),
            models=tuple(
                sorted(
                    models,
                    key=lambda asset: (
                        _natural_key(asset.display_name),
                        asset.kind,
                        asset.root_token,
                    ),
                )
            ),
            animation_banks=tuple(
                sorted(
                    animation_banks,
                    key=lambda asset: _natural_key(asset.display_name),
                )
            ),
            issues=tuple(issues),
        )
