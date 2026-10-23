# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import json
import math
import struct

import bpy
from mathutils import Matrix, Quaternion, Vector

from .core.errors import CoreFormatError
from .core.native_animation_resource import (
    NativeAnimationBinding,
    NativeAnimationClip,
    NativeAnimationLibrary,
    bind_native_animation,
    infer_native_animation_name,
    native_animation_bake_times,
    sample_native_animation_pose,
)
from .core.native_skeleton import NativeSkeleton
from .native_skinned_io import native_inverse_bind_matrix


ANIMATION_WRITER_BULK = "BULK"
ANIMATION_WRITER_LEGACY = "LEGACY"


class NativeAnimationBulkWriteError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NativeAnimationActionResult:
    clip: NativeAnimationClip
    binding: NativeAnimationBinding
    action: bpy.types.Action
    sample_times: tuple[float, ...]
    frame_start: float
    frame_end: float
    bake_rate: float
    maximum_decomposition_residual: float
    root_trajectory_policy: str
    writer: str


@dataclass(frozen=True, slots=True)
class NativeAnimationBakeFailure:
    object_id: int
    section_offset: int
    object_index: int
    message: str


@dataclass(frozen=True, slots=True)
class NativeAnimationBatchResult:
    actions: tuple[NativeAnimationActionResult, ...]
    incompatible_clip_count: int
    failures: tuple[NativeAnimationBakeFailure, ...]
    fallback_clip_count: int


@dataclass(frozen=True, slots=True)
class _BoneChannels:
    location: tuple[float, float, float]
    rotation: tuple[float, float, float, float]
    scale: tuple[float, float, float]


def _source_bone_names(
    armature_object: bpy.types.Object,
    skeleton: NativeSkeleton,
) -> tuple[str, ...]:
    if armature_object.type != "ARMATURE":
        raise ValueError("Animation target must be an Armature object")
    names: list[str | None] = [None] * skeleton.bone_count
    for bone in armature_object.data.bones:
        source_index = bone.get("killzone_source_index")
        if (
            isinstance(source_index, bool)
            or not isinstance(source_index, int)
            or not 0 <= source_index < skeleton.bone_count
        ):
            continue
        if names[source_index] is not None:
            raise ValueError(
                f"Armature has duplicate source bone index {source_index}"
            )
        names[source_index] = bone.name
    missing = tuple(
        index for index, name in enumerate(names) if name is None
    )
    if missing:
        raise ValueError(
            "Armature is missing source bone indices "
            + ", ".join(str(index) for index in missing)
        )
    return tuple(name for name in names if name is not None)


def _native_matrix(values: tuple[float, ...]) -> Matrix:
    return Matrix(
        (
            values[0:4],
            values[4:8],
            values[8:12],
            values[12:16],
        )
    )


def _matrix_values(matrix: Matrix) -> tuple[float, ...]:
    return tuple(value for row in matrix for value in row)


def _matrix_residual(left: Matrix, right: Matrix) -> float:
    return max(
        abs(left[row][column] - right[row][column])
        for row in range(4)
        for column in range(4)
    )


def _rest_fingerprint(rest_matrices: tuple[Matrix, ...]) -> str:
    digest = hashlib.sha256()
    for matrix in rest_matrices:
        digest.update(struct.pack("<16d", *_matrix_values(matrix)))
    return digest.hexdigest()


def _bake_channels(
    clip: NativeAnimationClip,
    skeleton: NativeSkeleton,
    binding: NativeAnimationBinding,
    armature_object: bpy.types.Object,
    bone_names: tuple[str, ...],
    sample_times: tuple[float, ...],
    *,
    reconstruct_root_trajectory: bool,
    residual_tolerance: float,
) -> tuple[
    tuple[tuple[_BoneChannels, ...], ...],
    float,
    str,
]:
    edit_rests = tuple(
        armature_object.data.bones[name].matrix_local.copy()
        for name in bone_names
    )
    edit_rest_inverses = tuple(
        matrix.inverted() for matrix in edit_rests
    )
    inverse_binds = tuple(
        native_inverse_bind_matrix(bone.temporary_transform)
        for bone in skeleton.bones
    )
    source_to_blender = Matrix.Identity(4)
    blender_to_source = source_to_blender.inverted()
    previous_rotations: list[Quaternion | None] = [
        None for _ in skeleton.bones
    ]
    baked: list[tuple[_BoneChannels, ...]] = []
    maximum_residual = 0.0

    for time in sample_times:
        pose = sample_native_animation_pose(
            clip,
            skeleton,
            binding,
            time,
            reconstruct_root_trajectory=reconstruct_root_trajectory,
        )
        targets = tuple(
            source_to_blender
            @ _native_matrix(bone_pose.global_matrix)
            @ inverse_binds[bone_pose.bone_index]
            @ blender_to_source
            @ edit_rests[bone_pose.bone_index]
            for bone_pose in pose.bones
        )
        frame_channels: list[_BoneChannels] = []
        for bone in skeleton.bones:
            if bone.parent_index is None:
                basis = edit_rest_inverses[bone.index] @ targets[bone.index]
            else:
                basis = (
                    edit_rest_inverses[bone.index]
                    @ edit_rests[bone.parent_index]
                    @ targets[bone.parent_index].inverted()
                    @ targets[bone.index]
                )
            location, rotation, scale = basis.decompose()
            previous = previous_rotations[bone.index]
            if previous is not None and rotation.dot(previous) < 0.0:
                rotation.negate()
            previous_rotations[bone.index] = rotation.copy()
            recomposed = Matrix.LocRotScale(location, rotation, scale)
            residual = _matrix_residual(basis, recomposed)
            maximum_residual = max(maximum_residual, residual)
            if not math.isfinite(residual) or residual > residual_tolerance:
                raise CoreFormatError(
                    f"Animation clip 0x{clip.object_id:08X} bone "
                    f"{bone.index} ({bone.name}) at {time:.9g}s has "
                    f"decomposition residual {residual:.9g}, exceeding "
                    f"{residual_tolerance:.9g}"
                )
            frame_channels.append(
                _BoneChannels(
                    location=tuple(location),
                    rotation=tuple(rotation),
                    scale=tuple(scale),
                )
            )
        baked.append(tuple(frame_channels))
    return (
        tuple(baked),
        maximum_residual,
        _rest_fingerprint(edit_rests),
    )


def _set_linear_interpolation(
    action: bpy.types.Action,
    slot: bpy.types.ActionSlot,
) -> None:
    for layer in action.layers:
        for strip in layer.strips:
            channelbag = strip.channelbag(slot)
            if channelbag is None:
                continue
            for curve in channelbag.fcurves:
                curve.extrapolation = "CONSTANT"
                for point in curve.keyframe_points:
                    point.interpolation = "LINEAR"


def _legacy_action_name(
    clip: NativeAnimationClip,
    library: NativeAnimationLibrary | None,
) -> str:
    prefix = (
        library.core_path.rsplit("/", 1)[-1].removesuffix(".core")
        if library is not None and library.core_path
        else "Killzone"
    )
    return (
        f"{prefix} Animation 0x{clip.object_id:08X} "
        f"[{clip.key.section_offset:06X}:{clip.key.object_index}]"
    )


def _resolve_action_name(
    clip: NativeAnimationClip,
    skeleton: NativeSkeleton,
    binding: NativeAnimationBinding,
    library: NativeAnimationLibrary | None,
    action_name: str | None,
) -> tuple[str, str, str]:
    legacy_name = _legacy_action_name(clip, library)
    if action_name:
        return action_name, "explicit-override", legacy_name
    semantic_name = (
        infer_native_animation_name(
            clip,
            skeleton,
            binding,
            library,
        )
        if library is not None
        else None
    )
    if semantic_name is not None:
        return (
            semantic_name,
            "serialized-single-clip-skeleton-root",
            legacy_name,
        )
    return legacy_name, "token-directory-fallback", legacy_name


def _set_native_action_metadata(
    action: bpy.types.Action,
    clip: NativeAnimationClip,
    skeleton: NativeSkeleton,
    binding: NativeAnimationBinding,
    *,
    library: NativeAnimationLibrary | None,
    rest_fingerprint: str,
    bake_rate: float,
    scene_rate: float,
    action_start: float,
    frame_end: float,
    root_policy: str,
    maximum_residual: float,
    writer: str,
    requested_action_name: str,
    action_name_source: str,
    legacy_action_name: str,
    fcurve_count: int | None = None,
    scalar_keyframe_count: int | None = None,
) -> None:
    action.use_fake_user = True
    action.use_cyclic = clip.playback_mode == 1
    action.use_frame_range = True
    action.frame_start = action_start
    action.frame_end = frame_end
    action["killzone_decoder_status"] = "retail-variant5-strict"
    action["killzone_object_id"] = f"0x{clip.object_id:08X}"
    action["killzone_section_offset"] = clip.key.section_offset
    action["killzone_object_index"] = clip.key.object_index
    action["killzone_storage"] = clip.storage
    action["killzone_duration_seconds"] = clip.duration
    action["killzone_max_key_time_seconds"] = clip.max_key_time
    action["killzone_playback_mode"] = clip.playback_mode
    action["killzone_event_token"] = f"0x{clip.event_token:08X}"
    action["killzone_compensate_root_serialized"] = clip.compensate_root
    action["killzone_total_distance"] = list(clip.total_distance)
    action["killzone_bone_map_token"] = f"0x{clip.bone_map_token:08X}"
    action["killzone_bone_map_section_offset"] = (
        clip.bone_map.key.section_offset
    )
    action["killzone_bone_map_object_index"] = (
        clip.bone_map.key.object_index
    )
    action["killzone_skeleton_token"] = f"0x{skeleton.object_id:08X}"
    action["killzone_basis"] = "identity"
    action["killzone_rest_fingerprint_sha256"] = rest_fingerprint
    action["killzone_bake_rate"] = bake_rate
    action["killzone_scene_rate"] = scene_rate
    action["killzone_action_start"] = action_start
    action["killzone_root_trajectory_policy"] = root_policy
    action["killzone_unmatched_tracks_json"] = json.dumps(
        binding.unmatched_track_names
    )
    action["killzone_decomposition_residual_max"] = maximum_residual
    action["killzone_action_writer"] = writer
    action["killzone_action_name"] = action.name
    action["killzone_action_name_requested"] = requested_action_name
    action["killzone_action_name_source"] = action_name_source
    action["killzone_legacy_action_name"] = legacy_action_name
    action["killzone_export_supported"] = False
    if fcurve_count is not None:
        action["killzone_fcurve_count"] = fcurve_count
    if scalar_keyframe_count is not None:
        action["killzone_scalar_keyframe_count"] = scalar_keyframe_count
    if library is not None:
        action["killzone_source_path"] = library.core_path
        action["killzone_core_sha256"] = library.source_core_sha256
        action["killzone_corestream_sha256"] = (
            library.source_corestream_sha256
        )


def bake_native_animation_action(
    scene: bpy.types.Scene,
    armature_object: bpy.types.Object,
    skeleton: NativeSkeleton,
    clip: NativeAnimationClip,
    *,
    library: NativeAnimationLibrary | None = None,
    binding: NativeAnimationBinding | None = None,
    bake_rate: float | None = None,
    action_start: float | None = None,
    reconstruct_root_trajectory: bool = False,
    residual_tolerance: float = 1.0e-5,
    action_name: str | None = None,
) -> NativeAnimationActionResult:
    fps_base = scene.render.fps_base
    if not math.isfinite(fps_base) or fps_base <= 0.0:
        raise ValueError("Scene FPS base must be positive and finite")
    effective_scene_rate = scene.render.fps / fps_base
    if (
        not math.isfinite(effective_scene_rate)
        or effective_scene_rate <= 0.0
    ):
        raise ValueError("Scene frame rate must be positive and finite")
    if bake_rate is None:
        bake_rate = effective_scene_rate
    if not math.isfinite(bake_rate) or bake_rate <= 0.0:
        raise ValueError("Animation bake rate must be positive and finite")
    if action_start is None:
        action_start = float(scene.frame_start)
    if not math.isfinite(action_start):
        raise ValueError("Animation action start must be finite")
    if (
        not math.isfinite(residual_tolerance)
        or residual_tolerance <= 0.0
    ):
        raise ValueError(
            "Animation decomposition tolerance must be positive and finite"
        )

    bone_names = _source_bone_names(armature_object, skeleton)
    resolved_binding = binding or bind_native_animation(clip, skeleton)
    sample_times = native_animation_bake_times(clip, bake_rate)
    baked, maximum_residual, rest_fingerprint = _bake_channels(
        clip,
        skeleton,
        resolved_binding,
        armature_object,
        bone_names,
        sample_times,
        reconstruct_root_trajectory=reconstruct_root_trajectory,
        residual_tolerance=residual_tolerance,
    )
    frames = tuple(
        action_start + time * effective_scene_rate
        for time in sample_times
    )
    frame_end = action_start + clip.duration * effective_scene_rate
    root_policy = (
        "reconstructed-total-distance"
        if reconstruct_root_trajectory
        else "sampled-tracks-only"
    )

    (
        resolved_action_name,
        action_name_source,
        legacy_action_name,
    ) = _resolve_action_name(
        clip,
        skeleton,
        resolved_binding,
        library,
        action_name,
    )
    action = bpy.data.actions.new(resolved_action_name)
    animation_data = armature_object.animation_data_create()
    previous_action = animation_data.action
    previous_pose = tuple(
        (
            armature_object.pose.bones[name].rotation_mode,
            armature_object.pose.bones[name].matrix_basis.copy(),
        )
        for name in bone_names
    )
    try:
        animation_data.action = action
        for pose_bone, bone_name in zip(
            (
                armature_object.pose.bones[name]
                for name in bone_names
            ),
            bone_names,
        ):
            pose_bone.rotation_mode = "QUATERNION"
            if pose_bone.name != bone_name:
                raise AssertionError("Pose-bone source order changed")
        for frame, frame_channels in zip(frames, baked):
            for bone_name, channels in zip(
                bone_names,
                frame_channels,
            ):
                pose_bone = armature_object.pose.bones[bone_name]
                pose_bone.location = Vector(channels.location)
                pose_bone.rotation_quaternion = Quaternion(
                    channels.rotation
                )
                pose_bone.scale = Vector(channels.scale)
                for data_path in (
                    "location",
                    "rotation_quaternion",
                    "scale",
                ):
                    if not pose_bone.keyframe_insert(
                        data_path=data_path,
                        frame=frame,
                        group=bone_name,
                    ):
                        raise RuntimeError(
                            f"Unable to insert {data_path} keyframe for "
                            f"{bone_name}"
                        )
        slot = animation_data.action_slot
        if slot is None:
            raise RuntimeError(
                "Blender did not create an OBJECT Action slot"
            )
        _set_linear_interpolation(action, slot)
        _set_native_action_metadata(
            action,
            clip,
            skeleton,
            resolved_binding,
            library=library,
            rest_fingerprint=rest_fingerprint,
            bake_rate=bake_rate,
            scene_rate=effective_scene_rate,
            action_start=action_start,
            frame_end=frame_end,
            root_policy=root_policy,
            maximum_residual=maximum_residual,
            writer="legacy-keyframe-insert",
            requested_action_name=resolved_action_name,
            action_name_source=action_name_source,
            legacy_action_name=legacy_action_name,
        )
    except Exception:
        animation_data.action = previous_action
        for bone_name, state in zip(bone_names, previous_pose):
            pose_bone = armature_object.pose.bones[bone_name]
            pose_bone.rotation_mode = state[0]
            pose_bone.matrix_basis = state[1]
        bpy.data.actions.remove(action)
        raise

    return NativeAnimationActionResult(
        clip=clip,
        binding=resolved_binding,
        action=action,
        sample_times=sample_times,
        frame_start=action_start,
        frame_end=frame_end,
        bake_rate=bake_rate,
        maximum_decomposition_residual=maximum_residual,
        root_trajectory_policy=root_policy,
        writer=ANIMATION_WRITER_LEGACY,
    )


def bake_native_animation_action_bulk(
    scene: bpy.types.Scene,
    armature_object: bpy.types.Object,
    skeleton: NativeSkeleton,
    clip: NativeAnimationClip,
    *,
    library: NativeAnimationLibrary | None = None,
    binding: NativeAnimationBinding | None = None,
    bake_rate: float | None = None,
    action_start: float | None = None,
    reconstruct_root_trajectory: bool = False,
    residual_tolerance: float = 1.0e-5,
    action_name: str | None = None,
) -> NativeAnimationActionResult:
    fps_base = scene.render.fps_base
    if not math.isfinite(fps_base) or fps_base <= 0.0:
        raise ValueError("Scene FPS base must be positive and finite")
    effective_scene_rate = scene.render.fps / fps_base
    if (
        not math.isfinite(effective_scene_rate)
        or effective_scene_rate <= 0.0
    ):
        raise ValueError("Scene frame rate must be positive and finite")
    if bake_rate is None:
        bake_rate = effective_scene_rate
    if not math.isfinite(bake_rate) or bake_rate <= 0.0:
        raise ValueError("Animation bake rate must be positive and finite")
    if action_start is None:
        action_start = float(scene.frame_start)
    if not math.isfinite(action_start):
        raise ValueError("Animation action start must be finite")
    if (
        not math.isfinite(residual_tolerance)
        or residual_tolerance <= 0.0
    ):
        raise ValueError(
            "Animation decomposition tolerance must be positive and finite"
        )

    bone_names = _source_bone_names(armature_object, skeleton)
    resolved_binding = binding or bind_native_animation(clip, skeleton)
    sample_times = native_animation_bake_times(clip, bake_rate)
    baked, maximum_residual, rest_fingerprint = _bake_channels(
        clip,
        skeleton,
        resolved_binding,
        armature_object,
        bone_names,
        sample_times,
        reconstruct_root_trajectory=reconstruct_root_trajectory,
        residual_tolerance=residual_tolerance,
    )
    frames = tuple(
        action_start + time * effective_scene_rate
        for time in sample_times
    )
    frame_end = action_start + clip.duration * effective_scene_rate
    root_policy = (
        "reconstructed-total-distance"
        if reconstruct_root_trajectory
        else "sampled-tracks-only"
    )

    (
        resolved_action_name,
        action_name_source,
        legacy_action_name,
    ) = _resolve_action_name(
        clip,
        skeleton,
        resolved_binding,
        library,
        action_name,
    )
    action = bpy.data.actions.new(resolved_action_name)
    animation_data = armature_object.animation_data_create()
    previous_action = animation_data.action
    previous_slot = animation_data.action_slot
    previous_pose = tuple(
        (
            armature_object.pose.bones[name].rotation_mode,
            armature_object.pose.bones[name].matrix_basis.copy(),
        )
        for name in bone_names
    )
    try:
        for bone_name in bone_names:
            armature_object.pose.bones[bone_name].rotation_mode = (
                "QUATERNION"
            )
        slot = action.slots.new("OBJECT", armature_object.name)
        layer = action.layers.new("Killzone Animation")
        strip = layer.strips.new(type="KEYFRAME")
        channelbag = strip.channelbag(slot, ensure=True)
        if channelbag is None:
            raise RuntimeError(
                "Blender did not create an Action channel bag"
            )

        fcurve_count = 0
        scalar_keyframe_count = 0
        channel_specs = (
            ("location", "location", 3),
            ("rotation_quaternion", "rotation", 4),
            ("scale", "scale", 3),
        )
        for bone_index, bone_name in enumerate(bone_names):
            pose_bone = armature_object.pose.bones[bone_name]
            for data_path, field_name, component_count in channel_specs:
                path = pose_bone.path_from_id(data_path)
                for component in range(component_count):
                    values = tuple(
                        getattr(frame_channels[bone_index], field_name)[
                            component
                        ]
                        for frame_channels in baked
                    )
                    if all(value == values[0] for value in values[1:]):
                        point_frames = frames[:1]
                        point_values = values[:1]
                    else:
                        point_frames = frames
                        point_values = values
                    curve = channelbag.fcurves.new(
                        path,
                        index=component,
                        group_name=bone_name,
                    )
                    curve.extrapolation = "CONSTANT"
                    curve.keyframe_points.add(len(point_frames))
                    coordinates = tuple(
                        coordinate
                        for frame, value in zip(
                            point_frames,
                            point_values,
                        )
                        for coordinate in (frame, value)
                    )
                    curve.keyframe_points.foreach_set("co", coordinates)
                    curve.keyframe_points.foreach_set(
                        "interpolation",
                        (1,) * len(point_frames),
                    )
                    curve.update()
                    fcurve_count += 1
                    scalar_keyframe_count += len(point_frames)

        animation_data.action = action
        animation_data.action_slot = slot
        _set_native_action_metadata(
            action,
            clip,
            skeleton,
            resolved_binding,
            library=library,
            rest_fingerprint=rest_fingerprint,
            bake_rate=bake_rate,
            scene_rate=effective_scene_rate,
            action_start=action_start,
            frame_end=frame_end,
            root_policy=root_policy,
            maximum_residual=maximum_residual,
            writer="bulk-fcurve-foreach-set",
            requested_action_name=resolved_action_name,
            action_name_source=action_name_source,
            legacy_action_name=legacy_action_name,
            fcurve_count=fcurve_count,
            scalar_keyframe_count=scalar_keyframe_count,
        )
        action["killzone_constant_channel_policy"] = (
            "exact-equality-single-key"
        )
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        animation_data.action = previous_action
        if previous_action is not None and previous_slot is not None:
            animation_data.action_slot = previous_slot
        for bone_name, state in zip(bone_names, previous_pose):
            pose_bone = armature_object.pose.bones[bone_name]
            pose_bone.rotation_mode = state[0]
            pose_bone.matrix_basis = state[1]
        bpy.data.actions.remove(action)
        raise NativeAnimationBulkWriteError(
            f"Unable to create bulk Action for clip "
            f"0x{clip.object_id:08X}: {exc}"
        ) from exc

    return NativeAnimationActionResult(
        clip=clip,
        binding=resolved_binding,
        action=action,
        sample_times=sample_times,
        frame_start=action_start,
        frame_end=frame_end,
        bake_rate=bake_rate,
        maximum_decomposition_residual=maximum_residual,
        root_trajectory_policy=root_policy,
        writer=ANIMATION_WRITER_BULK,
    )


def bake_compatible_native_animation_library(
    scene: bpy.types.Scene,
    armature_object: bpy.types.Object,
    skeleton: NativeSkeleton,
    library: NativeAnimationLibrary,
    *,
    clips: Iterable[NativeAnimationClip] | None = None,
    writer: str = ANIMATION_WRITER_LEGACY,
    fallback_to_legacy: bool = False,
    bake_rate: float | None = None,
    reconstruct_root_trajectory: bool = False,
    residual_tolerance: float = 0.05,
) -> NativeAnimationBatchResult:
    actions: list[NativeAnimationActionResult] = []
    failures: list[NativeAnimationBakeFailure] = []
    incompatible_clip_count = 0
    fallback_clip_count = 0
    if writer not in (ANIMATION_WRITER_BULK, ANIMATION_WRITER_LEGACY):
        raise ValueError(f"Unsupported animation writer {writer!r}")
    clips_by_key = {clip.key: clip for clip in library.clips}
    selected_clips = (
        library.clips
        if clips is None
        else tuple(
            clips_by_key.get(clip.key)
            for clip in clips
        )
    )
    if any(clip is None for clip in selected_clips):
        raise ValueError(
            "Selected animation clip does not belong to the library"
        )
    required_bones = set(range(skeleton.bone_count))
    for selected_clip in selected_clips:
        if selected_clip is None:
            raise AssertionError("Selected animation clip was not resolved")
        clip = selected_clip
        binding = bind_native_animation(clip, skeleton)
        matched_bones = {
            track.bone_index
            for track in binding.tracks
            if track.bone_index is not None
        }
        if matched_bones != required_bones:
            incompatible_clip_count += 1
            continue
        try:
            if writer == ANIMATION_WRITER_BULK:
                try:
                    result = bake_native_animation_action_bulk(
                        scene,
                        armature_object,
                        skeleton,
                        clip,
                        library=library,
                        binding=binding,
                        bake_rate=bake_rate,
                        reconstruct_root_trajectory=(
                            reconstruct_root_trajectory
                        ),
                        residual_tolerance=residual_tolerance,
                    )
                except NativeAnimationBulkWriteError:
                    if not fallback_to_legacy:
                        raise
                    result = bake_native_animation_action(
                        scene,
                        armature_object,
                        skeleton,
                        clip,
                        library=library,
                        binding=binding,
                        bake_rate=bake_rate,
                        reconstruct_root_trajectory=(
                            reconstruct_root_trajectory
                        ),
                        residual_tolerance=residual_tolerance,
                    )
                    fallback_clip_count += 1
            else:
                result = bake_native_animation_action(
                    scene,
                    armature_object,
                    skeleton,
                    clip,
                    library=library,
                    binding=binding,
                    bake_rate=bake_rate,
                    reconstruct_root_trajectory=(
                        reconstruct_root_trajectory
                    ),
                    residual_tolerance=residual_tolerance,
                )
            actions.append(result)
        except CoreFormatError as exc:
            failures.append(
                NativeAnimationBakeFailure(
                    object_id=clip.object_id,
                    section_offset=clip.key.section_offset,
                    object_index=clip.key.object_index,
                    message=str(exc),
                )
            )
    return NativeAnimationBatchResult(
        actions=tuple(actions),
        incompatible_clip_count=incompatible_clip_count,
        failures=tuple(failures),
        fallback_clip_count=fallback_clip_count,
    )
