# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import struct

from .archive_source import KillzoneArchiveSource
from .core_format import CoreHeader
from .errors import CoreFormatError
from .native_animation import (
    NativePackedAnimation,
    Quaternion,
    Vector3,
    parse_packed_animation,
    sample_track_rotation,
    sample_track_scale,
    sample_track_translation,
)
from .native_level import (
    CORE_STREAM_MAGIC,
    NativeCoreObject,
    NativeCoreSection,
    NativeObjectKey,
    parse_native_core_sections,
)
from .native_skeleton import NativeSkeleton


NativeMatrix4 = tuple[
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
]

_U32 = struct.Struct("<I")
_CLIP_HEADER = struct.Struct("<6I4fI3f2I")
_CLIP_HEADER_SIZE = 0x40
_THRESHOLD_BITS = (0x3A83126F, 0x3C23D70A, 0x3C23D70A)


@dataclass(frozen=True, slots=True)
class NativeAnimationBoneMap:
    key: NativeObjectKey
    object_id: int
    identity_words: tuple[int, int, int, int] | None
    empty_string_index: int
    track_names_string_index: int
    track_names: tuple[str, ...]
    raw_blob: bytes


@dataclass(frozen=True, slots=True)
class NativeAnimationClip:
    key: NativeObjectKey
    object_id: int
    identity_words: tuple[int, int, int, int] | None
    generic_flags: int
    type_name_string_index: int
    type_name: str
    parent_token: int
    linear_storage: int
    playback_mode: int
    event_token: int
    translation_threshold: float
    rotation_axis_threshold: float
    rotation_angle_threshold: float
    duration: float
    compensate_root: bool
    total_distance: Vector3
    bone_map_token: int
    bone_map: NativeAnimationBoneMap
    storage: str
    packed: NativePackedAnimation
    raw_blob: bytes

    @property
    def max_key_time(self) -> float:
        return max(track.time_values[-1] for track in self.packed.tracks)


@dataclass(frozen=True, slots=True)
class NativeAnimationLibrary:
    core_path: str
    header: CoreHeader
    sections: tuple[NativeCoreSection, ...]
    bone_maps: tuple[NativeAnimationBoneMap, ...]
    clips: tuple[NativeAnimationClip, ...]
    source_core_sha256: str
    source_corestream_sha256: str


@dataclass(frozen=True, slots=True)
class NativeAnimationClipMetadata:
    key: NativeObjectKey
    object_id: int
    duration: float
    playback_mode: int


@dataclass(frozen=True, slots=True)
class NativeAnimationTrackBinding:
    track_index: int
    track_name: str
    bone_index: int | None
    bone_category: int | None


@dataclass(frozen=True, slots=True)
class NativeAnimationBinding:
    clip_key: NativeObjectKey
    clip_object_id: int
    skeleton_object_id: int
    tracks: tuple[NativeAnimationTrackBinding, ...]

    @property
    def matched_track_count(self) -> int:
        return sum(track.bone_index is not None for track in self.tracks)

    @property
    def unmatched_track_names(self) -> tuple[str, ...]:
        return tuple(
            track.track_name
            for track in self.tracks
            if track.bone_index is None
        )


def infer_native_animation_name(
    clip: NativeAnimationClip,
    skeleton: NativeSkeleton,
    binding: NativeAnimationBinding,
    library: NativeAnimationLibrary,
) -> str | None:
    if (
        len(library.clips) != 1
        or library.clips[0].key != clip.key
        or binding.clip_key != clip.key
        or binding.clip_object_id != clip.object_id
        or binding.skeleton_object_id != skeleton.object_id
    ):
        return None
    name_index = skeleton.name_string_index
    if not 0 <= name_index < len(library.header.strings):
        return None
    skeleton_name = library.header.strings[name_index].utf8
    prefix = "skelet_"
    if (
        skeleton_name is None
        or not skeleton_name.casefold().startswith(prefix)
    ):
        return None
    candidate = skeleton_name[len(prefix) :]
    if not candidate:
        return None
    root_indices = tuple(
        bone.index
        for bone in skeleton.bones
        if bone.parent_index is None
    )
    if len(root_indices) != 1:
        return None
    root_tracks = tuple(
        track.track_name
        for track in binding.tracks
        if track.bone_index == root_indices[0]
    )
    suffix = "_joint"
    if (
        len(root_tracks) != 1
        or not root_tracks[0].casefold().endswith(suffix)
        or root_tracks[0][: -len(suffix)].casefold()
        != candidate.casefold()
    ):
        return None
    return candidate


@dataclass(frozen=True, slots=True)
class NativeAnimationBonePose:
    bone_index: int
    rotation: Quaternion
    translation: Vector3
    scale: Vector3
    local_matrix: NativeMatrix4
    global_matrix: NativeMatrix4


@dataclass(frozen=True, slots=True)
class NativeAnimationPose:
    requested_time: float
    sample_time: float
    root_trajectory_reconstructed: bool
    bones: tuple[NativeAnimationBonePose, ...]


def _parse_bone_map(
    obj: NativeCoreObject,
    header: CoreHeader,
) -> NativeAnimationBoneMap:
    if len(obj.blob) != 0x10:
        raise CoreFormatError(
            "SkeletonAnimationBoneMap blob size is "
            f"0x{len(obj.blob):X}, expected 0x10"
        )
    empty_index, zero_a, zero_b, names_index = struct.unpack(
        "<4I", obj.blob
    )
    if zero_a != 0 or zero_b != 0:
        raise CoreFormatError(
            "SkeletonAnimationBoneMap reserved words are non-zero"
        )
    if empty_index >= len(header.strings):
        raise CoreFormatError(
            "SkeletonAnimationBoneMap empty-string index is invalid"
        )
    if header.strings[empty_index].utf8 != "":
        raise CoreFormatError(
            "SkeletonAnimationBoneMap empty-string field is not empty"
        )
    if names_index >= len(header.strings):
        raise CoreFormatError(
            "SkeletonAnimationBoneMap track-name index is invalid"
        )
    names_text = header.strings[names_index].utf8
    if names_text is None:
        raise CoreFormatError(
            "SkeletonAnimationBoneMap track names are not UTF-8"
        )
    if names_text.endswith("\n"):
        names_text = names_text[:-1]
    track_names = (
        () if not names_text else tuple(names_text.split("\n"))
    )
    if any(not name for name in track_names):
        raise CoreFormatError(
            "SkeletonAnimationBoneMap contains an empty track name"
        )
    return NativeAnimationBoneMap(
        key=obj.key,
        object_id=obj.object_id,
        identity_words=obj.identity_words,
        empty_string_index=empty_index,
        track_names_string_index=names_index,
        track_names=track_names,
        raw_blob=obj.blob,
    )


def _resolve_bone_map(
    token: int,
    bone_maps: tuple[NativeAnimationBoneMap, ...],
) -> NativeAnimationBoneMap:
    matches = tuple(
        bone_map
        for bone_map in bone_maps
        if bone_map.object_id == token
    )
    if len(matches) != 1:
        raise CoreFormatError(
            f"Animation BoneMap token 0x{token:08X} resolves to "
            f"{len(matches)} objects"
        )
    return matches[0]


def _parse_clip(
    obj: NativeCoreObject,
    header: CoreHeader,
    bone_maps: tuple[NativeAnimationBoneMap, ...],
    corestream_data: bytes,
) -> NativeAnimationClip:
    if len(obj.blob) < _CLIP_HEADER_SIZE:
        raise CoreFormatError(
            "SkeletonAnimationResource property block is truncated"
        )
    (
        generic_flags,
        type_name_index,
        parent_token,
        linear_storage,
        playback_mode,
        event_token,
        translation_threshold,
        rotation_axis_threshold,
        rotation_angle_threshold,
        duration,
        compensate_root,
        total_x,
        total_y,
        total_z,
        bone_map_token,
        zero,
    ) = _CLIP_HEADER.unpack_from(obj.blob)
    if generic_flags not in (0, 0x100):
        raise CoreFormatError(
            "SkeletonAnimationResource has unsupported generic flags "
            f"0x{generic_flags:08X}"
        )
    if type_name_index >= len(header.strings):
        raise CoreFormatError(
            "SkeletonAnimationResource type-name index is invalid"
        )
    type_name = header.strings[type_name_index].utf8
    if type_name != "SkeletonAnimationResource":
        raise CoreFormatError(
            "SkeletonAnimationResource type-name field does not name "
            "its resource type"
        )
    if parent_token != 0 or linear_storage != 0 or zero != 0:
        raise CoreFormatError(
            "SkeletonAnimationResource reserved fields are non-zero"
        )
    if playback_mode not in (1, 2):
        raise CoreFormatError(
            "SkeletonAnimationResource playback mode is outside the "
            "retail range 1..2"
        )
    if struct.unpack_from("<3I", obj.blob, 0x18) != _THRESHOLD_BITS:
        raise CoreFormatError(
            "SkeletonAnimationResource thresholds do not match the "
            "retail constants"
        )
    if not math.isfinite(duration) or duration <= 0.0:
        raise CoreFormatError(
            "SkeletonAnimationResource duration is not positive and finite"
        )
    if compensate_root not in (0, 1):
        raise CoreFormatError(
            "SkeletonAnimationResource Compensate Root is not boolean"
        )
    total_distance = (total_x, total_y, total_z)
    if not all(math.isfinite(value) for value in total_distance):
        raise CoreFormatError(
            "SkeletonAnimationResource Total Distance is not finite"
        )
    if bone_map_token == 0:
        raise CoreFormatError(
            "SkeletonAnimationResource has a null BoneMap token"
        )
    bone_map = _resolve_bone_map(bone_map_token, bone_maps)

    if generic_flags == 0:
        if len(obj.blob) != _CLIP_HEADER_SIZE:
            raise CoreFormatError(
                "Streamed SkeletonAnimationResource has an inline tail"
            )
        if obj.stream_size == 0:
            raise CoreFormatError(
                "Streamed SkeletonAnimationResource has no CoreStream data"
            )
        if obj.stream_offset < 0x40 or obj.stream_offset % 0x40:
            raise CoreFormatError(
                "Streamed SkeletonAnimationResource range is not "
                "64-byte aligned"
            )
        packed_data = corestream_data[
            obj.stream_offset : obj.stream_offset + obj.stream_size
        ]
        storage = "streamed"
        alignment = 64
    else:
        if obj.stream_size != 0:
            raise CoreFormatError(
                "Inline SkeletonAnimationResource has a non-empty "
                "CoreStream range"
            )
        if obj.stream_offset % 0x40:
            raise CoreFormatError(
                "Inline SkeletonAnimationResource CoreStream anchor is "
                "not 64-byte aligned"
            )
        packed_data = obj.blob[_CLIP_HEADER_SIZE:]
        storage = "inline"
        alignment = 16
    try:
        packed = parse_packed_animation(
            packed_data,
            alignment=alignment,
        )
    except CoreFormatError as exc:
        raise CoreFormatError(
            f"SkeletonAnimationResource 0x{obj.object_id:08X} "
            f"[{obj.key.section_offset:06X}:{obj.key.object_index}]: {exc}"
        ) from exc
    if packed.track_count != len(bone_map.track_names):
        raise CoreFormatError(
            f"SkeletonAnimationResource 0x{obj.object_id:08X} has "
            f"{packed.track_count} tracks but its BoneMap has "
            f"{len(bone_map.track_names)} names"
        )
    return NativeAnimationClip(
        key=obj.key,
        object_id=obj.object_id,
        identity_words=obj.identity_words,
        generic_flags=generic_flags,
        type_name_string_index=type_name_index,
        type_name=type_name,
        parent_token=parent_token,
        linear_storage=linear_storage,
        playback_mode=playback_mode,
        event_token=event_token,
        translation_threshold=translation_threshold,
        rotation_axis_threshold=rotation_axis_threshold,
        rotation_angle_threshold=rotation_angle_threshold,
        duration=duration,
        compensate_root=bool(compensate_root),
        total_distance=total_distance,
        bone_map_token=bone_map_token,
        bone_map=bone_map,
        storage=storage,
        packed=packed,
        raw_blob=obj.blob,
    )


def _validate_corestream(corestream_data: bytes) -> None:
    if len(corestream_data) < 0x40:
        raise CoreFormatError(
            "Native animation CoreStream header is truncated"
        )
    if corestream_data[: len(CORE_STREAM_MAGIC)] != CORE_STREAM_MAGIC:
        raise CoreFormatError(
            "Native animation CoreStream magic is invalid"
        )
    if any(corestream_data[len(CORE_STREAM_MAGIC) : 0x40]):
        raise CoreFormatError(
            "Native animation CoreStream reserved header bytes are non-zero"
        )


def discover_native_animation_clips(
    sections: tuple[NativeCoreSection, ...],
) -> tuple[NativeAnimationClipMetadata, ...]:
    metadata: list[NativeAnimationClipMetadata] = []
    for section in sections:
        if section.type_name != "SkeletonAnimationResource":
            continue
        for obj in section.objects:
            if len(obj.blob) < _CLIP_HEADER_SIZE:
                raise CoreFormatError(
                    "SkeletonAnimationResource property block is truncated"
                )
            values = _CLIP_HEADER.unpack_from(obj.blob)
            playback_mode = values[4]
            duration = values[9]
            if playback_mode not in (1, 2):
                raise CoreFormatError(
                    "SkeletonAnimationResource playback mode is outside "
                    "the retail range 1..2"
                )
            if not math.isfinite(duration) or duration <= 0.0:
                raise CoreFormatError(
                    "SkeletonAnimationResource duration is not positive "
                    "and finite"
                )
            metadata.append(
                NativeAnimationClipMetadata(
                    key=obj.key,
                    object_id=obj.object_id,
                    duration=duration,
                    playback_mode=playback_mode,
                )
            )
    return tuple(metadata)


def parse_native_animation_library(
    core_data: bytes,
    corestream_data: bytes = b"",
    *,
    core_path: str = "",
) -> NativeAnimationLibrary:
    header, sections = parse_native_core_sections(
        core_data, len(corestream_data)
    )
    bone_map_objects = tuple(
        obj
        for section in sections
        if section.type_name == "SkeletonAnimationBoneMap"
        for obj in section.objects
    )
    clip_objects = tuple(
        obj
        for section in sections
        if section.type_name == "SkeletonAnimationResource"
        for obj in section.objects
    )
    if not bone_map_objects:
        raise CoreFormatError(
            "Native animation library has no SkeletonAnimationBoneMap "
            "objects"
        )
    if not clip_objects:
        raise CoreFormatError(
            "Native animation library has no SkeletonAnimationResource "
            "objects"
        )
    if corestream_data:
        _validate_corestream(corestream_data)
    if any(obj.stream_size for obj in clip_objects) and not corestream_data:
        raise CoreFormatError(
            "Native animation library has streamed clips but no CoreStream"
        )

    bone_maps = tuple(
        _parse_bone_map(obj, header) for obj in bone_map_objects
    )
    clips = tuple(
        _parse_clip(
            obj,
            header,
            bone_maps,
            corestream_data,
        )
        for obj in clip_objects
    )
    return NativeAnimationLibrary(
        core_path=core_path,
        header=header,
        sections=sections,
        bone_maps=bone_maps,
        clips=clips,
        source_core_sha256=hashlib.sha256(core_data).hexdigest(),
        source_corestream_sha256=hashlib.sha256(
            corestream_data
        ).hexdigest(),
    )


def load_native_animation_library(
    source_path: str | Path,
    animation_path: str,
) -> NativeAnimationLibrary:
    normalized_path = animation_path.casefold()
    with KillzoneArchiveSource(source_path) as source:
        catalog = source.load_catalog()
        entries_by_path = {
            entry.path.as_posix().casefold(): entry
            for entry in catalog.entries
        }
        entry = entries_by_path.get(normalized_path)
        if entry is None:
            raise CoreFormatError(
                "Native animation Core is not in the catalog: "
                f"{animation_path}"
            )
        stream_path = entry.path.with_suffix(".corestream").as_posix()
        stream_entry = entries_by_path.get(stream_path.casefold())
        core_data = source.read_entry_range(entry, 0, entry.size)
        corestream_data = (
            source.read_entry_range(stream_entry, 0, stream_entry.size)
            if stream_entry is not None
            else b""
        )
    try:
        return parse_native_animation_library(
            core_data,
            corestream_data,
            core_path=entry.path.as_posix(),
        )
    except CoreFormatError as exc:
        raise CoreFormatError(f"{animation_path}: {exc}") from exc


def find_native_animation_clip(
    library: NativeAnimationLibrary,
    *,
    object_id: int | None = None,
    key: NativeObjectKey | None = None,
) -> NativeAnimationClip:
    if (object_id is None) == (key is None):
        raise ValueError(
            "Select an animation clip by exactly one of object_id or key"
        )
    matches = tuple(
        clip
        for clip in library.clips
        if (
            clip.object_id == object_id
            if object_id is not None
            else clip.key == key
        )
    )
    selector = (
        f"token 0x{object_id:08X}"
        if object_id is not None
        else f"key {key.section_offset:06X}:{key.object_index}"
    )
    if len(matches) != 1:
        raise CoreFormatError(
            f"Animation clip {selector} resolves to {len(matches)} objects"
        )
    return matches[0]


def bind_native_animation(
    clip: NativeAnimationClip,
    skeleton: NativeSkeleton,
) -> NativeAnimationBinding:
    if clip.packed.track_count != len(clip.bone_map.track_names):
        raise CoreFormatError(
            "Animation track count disagrees with its BoneMap"
        )
    first_bone_by_name: dict[str, int] = {}
    for bone in skeleton.bones:
        first_bone_by_name.setdefault(bone.name.casefold(), bone.index)

    tracks: list[NativeAnimationTrackBinding] = []
    for track_index, track_name in enumerate(clip.bone_map.track_names):
        bone_index = first_bone_by_name.get(track_name.casefold())
        bone_category = (
            skeleton.bones[bone_index].category
            if bone_index is not None
            else None
        )
        tracks.append(
            NativeAnimationTrackBinding(
                track_index=track_index,
                track_name=track_name,
                bone_index=bone_index,
                bone_category=bone_category,
            )
        )
    return NativeAnimationBinding(
        clip_key=clip.key,
        clip_object_id=clip.object_id,
        skeleton_object_id=skeleton.object_id,
        tracks=tuple(tracks),
    )


def _matrix_multiply(
    left: NativeMatrix4,
    right: NativeMatrix4,
) -> NativeMatrix4:
    values = tuple(
        sum(
            left[row * 4 + inner] * right[inner * 4 + column]
            for inner in range(4)
        )
        for row in range(4)
        for column in range(4)
    )
    return values


def _local_matrix(
    rotation: Quaternion,
    translation: Vector3,
    scale: Vector3,
) -> NativeMatrix4:
    x, y, z, w = rotation
    sx, sy, sz = scale
    q00 = 1.0 - 2.0 * (y * y + z * z)
    q01 = 2.0 * (x * y - z * w)
    q02 = 2.0 * (x * z + y * w)
    q10 = 2.0 * (x * y + z * w)
    q11 = 1.0 - 2.0 * (x * x + z * z)
    q12 = 2.0 * (y * z - x * w)
    q20 = 2.0 * (x * z - y * w)
    q21 = 2.0 * (y * z + x * w)
    q22 = 1.0 - 2.0 * (x * x + y * y)
    return (
        q00 * sx,
        q01 * sy,
        q02 * sz,
        translation[0],
        q10 * sx,
        q11 * sy,
        q12 * sz,
        translation[1],
        q20 * sx,
        q21 * sy,
        q22 * sz,
        translation[2],
        0.0,
        0.0,
        0.0,
        1.0,
    )


def _clip_sample_time(clip: NativeAnimationClip, time: float) -> float:
    if not math.isfinite(time):
        raise ValueError("Animation sample time must be finite")
    if clip.playback_mode == 1:
        return time % clip.duration
    if clip.playback_mode == 2:
        return min(max(time, 0.0), clip.duration)
    raise CoreFormatError(
        f"Unsupported animation playback mode {clip.playback_mode}"
    )


def sample_native_animation_pose(
    clip: NativeAnimationClip,
    skeleton: NativeSkeleton,
    binding: NativeAnimationBinding,
    time: float,
    *,
    reconstruct_root_trajectory: bool = False,
) -> NativeAnimationPose:
    if (
        binding.clip_key != clip.key
        or binding.clip_object_id != clip.object_id
        or binding.skeleton_object_id != skeleton.object_id
    ):
        raise ValueError(
            "Animation binding does not belong to the clip and Skeleton"
        )
    if len(binding.tracks) != clip.packed.track_count:
        raise ValueError(
            "Animation binding track count does not match the clip"
        )
    sample_time = _clip_sample_time(clip, time)
    rotations: list[Quaternion] = [
        (0.0, 0.0, 0.0, 1.0) for _ in skeleton.bones
    ]
    translations: list[Vector3] = [
        (0.0, 0.0, 0.0) for _ in skeleton.bones
    ]
    scales: list[Vector3] = [
        (1.0, 1.0, 1.0) for _ in skeleton.bones
    ]
    for track_binding in binding.tracks:
        bone_index = track_binding.bone_index
        if bone_index is None:
            continue
        bone = skeleton.bones[bone_index]
        track = clip.packed.tracks[track_binding.track_index]
        rotations[bone_index] = sample_track_rotation(track, sample_time)
        if bone.category >= 1:
            translation = sample_track_translation(track, sample_time)
            translations[bone_index] = translation[:3]
        if bone.category == 2:
            scales[bone_index] = sample_track_scale(track, sample_time)

    if reconstruct_root_trajectory and skeleton.bones[0].category >= 1:
        phase = sample_time / clip.duration
        translations[0] = tuple(
            translations[0][axis] + clip.total_distance[axis] * phase
            for axis in range(3)
        )

    poses: list[NativeAnimationBonePose] = []
    global_matrices: list[NativeMatrix4] = []
    for bone in skeleton.bones:
        translation = (
            bone.static_translation
            if bone.category == 0
            else translations[bone.index]
        )
        scale = (
            scales[bone.index]
            if bone.category == 2
            else (1.0, 1.0, 1.0)
        )
        local = _local_matrix(
            rotations[bone.index],
            translation,
            scale,
        )
        global_matrix = (
            local
            if bone.parent_index is None
            else _matrix_multiply(
                global_matrices[bone.parent_index],
                local,
            )
        )
        if not all(math.isfinite(value) for value in global_matrix):
            raise CoreFormatError(
                f"Animation pose for bone {bone.index} is not finite"
            )
        global_matrices.append(global_matrix)
        poses.append(
            NativeAnimationBonePose(
                bone_index=bone.index,
                rotation=rotations[bone.index],
                translation=translation,
                scale=scale,
                local_matrix=local,
                global_matrix=global_matrix,
            )
        )
    return NativeAnimationPose(
        requested_time=time,
        sample_time=sample_time,
        root_trajectory_reconstructed=reconstruct_root_trajectory,
        bones=tuple(poses),
    )


def native_animation_bake_times(
    clip: NativeAnimationClip,
    bake_rate: float,
) -> tuple[float, ...]:
    if not math.isfinite(bake_rate) or bake_rate <= 0.0:
        raise ValueError("Animation bake rate must be positive and finite")
    whole_samples = int(math.floor(clip.duration * bake_rate))
    times = [
        index / bake_rate
        for index in range(whole_samples + 1)
        if index / bake_rate < clip.duration
    ]
    times.append(clip.duration)
    return tuple(times)
