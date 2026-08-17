"""Compile operator-reviewed junction constraints into immutable edit plans.

This stage is deliberately local and deterministic.  It does not discover
semantic cutaways, call a model, or mutate the effective Story Plan.  An
operator (or a separate reviewed detector) supplies safe/forbidden visual
ranges; this compiler proves that an audio-tail visual repair can be executed
without shortening either adjacent Clip's audio.  The execution strategy is
closed and pair-level: a reviewed visual bridge or a direct right-A/V overlap.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from autocut_core.io import atomic_write_json, load_json, sha256_file, stable_id
from autocut_core.libs._common import rounded


CONSTRAINTS_METHOD = "operator-junction-edit-constraints-v1"
CONSTRAINTS_V2_METHOD = "operator-junction-edit-constraints-v2"
LEGACY_PLAN_METHOD = "deterministic-junction-edit-plan-v1"
PLAN_METHOD = "deterministic-junction-edit-plan-v2-pair-timeline"
LEGACY_INDEX_METHOD = "deterministic-junction-edit-index-v1"
INDEX_METHOD = "deterministic-junction-edit-index-v2-pair-timeline"
LEGACY_EDIT_TYPE = "audio_tail_over_bridge"
EDIT_EFFECT = "audio_tail_visual_repair"
from autocut_core.libs.editorial_knowledge import load_knowledge_section

REVIEWED_BRIDGE = "reviewed_bridge"
RIGHT_AV_OVERLAP = "right_av_overlap"

_junction_edits = load_knowledge_section("junction_edits") or {}
_supported = set(_junction_edits.get("supported_strategies") or [])
SUPPORTED_STRATEGIES = _supported if _supported else {REVIEWED_BRIDGE, RIGHT_AV_OVERLAP}
DEFAULT_OUTPUT_FPS = 25
DEFAULT_MAX_AUDIO_TAIL_SECONDS = 2.0
RECOMMENDED_MAX_OVERLAP_SECONDS = 0.8
HARD_MAX_OVERLAP_SECONDS = 1.2
MAX_SIMULTANEOUS_SPEECH_SECONDS = 0.1
DEFAULT_LEFT_AUDIO_FADE_OUT_SECONDS = 0.25
DEFAULT_RIGHT_AUDIO_FADE_IN_SECONDS = 0.05
TIME_TOLERANCE_SECONDS = 0.002


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _range_overlap(
    left_start: float,
    left_end: float,
    right_start: float,
    right_end: float,
) -> float:
    """Return overlap for half-open ranges, ignoring millisecond noise."""

    return max(0.0, min(left_end, right_end) - max(left_start, right_start))


def junction_strategy(edit: dict[str, Any]) -> str:
    """Return the canonical strategy, including for immutable v1 artifacts."""

    strategy = edit.get("strategy")
    if isinstance(strategy, str):
        return strategy
    if edit.get("type") == LEGACY_EDIT_TYPE:
        return REVIEWED_BRIDGE
    return ""


def _speech_windows(
    intervals: list[dict[str, Any]], *, start: float, end: float
) -> list[tuple[float, float]]:
    windows: list[tuple[float, float]] = []
    for interval in intervals:
        if not isinstance(interval, dict):
            continue
        interval_start = interval.get("start")
        interval_end = interval.get("end")
        if not _finite_number(interval_start) or not _finite_number(interval_end):
            continue
        clipped_start = max(start, float(interval_start))
        clipped_end = min(end, float(interval_end))
        if clipped_end > clipped_start + TIME_TOLERANCE_SECONDS:
            windows.append((clipped_start - start, clipped_end - start))
    return windows


def _simultaneous_speech_seconds(
    *,
    left_intervals: list[dict[str, Any]],
    right_intervals: list[dict[str, Any]],
    left_start: float,
    left_end: float,
    right_start: float,
) -> float:
    overlap_duration = left_end - left_start
    left_windows = _speech_windows(
        left_intervals, start=left_start, end=left_end
    )
    right_windows = _speech_windows(
        right_intervals,
        start=right_start,
        end=right_start + overlap_duration,
    )
    intersections: list[tuple[float, float]] = []
    for left_window in left_windows:
        for right_window in right_windows:
            start = max(left_window[0], right_window[0])
            end = min(left_window[1], right_window[1])
            if end > start + TIME_TOLERANCE_SECONDS:
                intersections.append((start, end))
    if not intersections:
        return 0.0
    intersections.sort()
    merged: list[list[float]] = []
    for start, end in intersections:
        if not merged or start > merged[-1][1] + TIME_TOLERANCE_SECONDS:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def ordered_clips(plan: dict[str, Any]) -> list[dict[str, Any]]:
    clips: list[dict[str, Any]] = []
    for block in sorted(
        plan.get("blocks", []), key=lambda item: item.get("play_order", 0)
    ):
        for order, clip in enumerate(block.get("clips", []), start=1):
            clips.append(
                {
                    **clip,
                    "_block_id": block.get("id"),
                    "_block_role": block.get("role"),
                    "_clip_order_in_block": order,
                }
            )
    return clips


def validate_constraints_document(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["junction edit constraints must be an object"]
    expected_top = {"schema_version", "method", "edits"}
    unknown_top = sorted(set(value) - expected_top)
    if unknown_top:
        errors.append(f"constraints have unknown properties {unknown_top}")
    schema_version = value.get("schema_version")
    expected_method = {
        "1.0": CONSTRAINTS_METHOD,
        "2.0": CONSTRAINTS_V2_METHOD,
    }.get(schema_version)
    if expected_method is None:
        errors.append("constraints schema_version must be '1.0' or '2.0'")
    elif value.get("method") != expected_method:
        errors.append(f"constraints method must be {expected_method!r}")
    edits = value.get("edits")
    if not isinstance(edits, list) or not edits:
        errors.append("constraints edits must be a non-empty array")
        return errors
    seen_ids: set[str] = set()
    legacy_edit_fields = {
        "id",
        "story_id",
        "type",
        "from_clip_id",
        "to_clip_id",
        "left_video_end_seconds",
        "bridge_candidate",
        "forbidden_visual_ranges",
        "reason",
        "max_audio_tail_seconds",
    }
    pair_edit_fields = {
        "id",
        "story_id",
        "effect",
        "strategy",
        "from_clip_id",
        "to_clip_id",
        "left_video_end_seconds",
        "bridge_candidate",
        "forbidden_visual_ranges",
        "right_entry_visual_review",
        "reason",
        "max_audio_tail_seconds",
        "max_simultaneous_speech_seconds",
        "left_audio_fade_out_seconds",
        "right_audio_fade_in_seconds",
    }
    expected_bridge = {
        "source_id",
        "safe_start_seconds",
        "safe_end_seconds",
    }
    expected_forbidden = {
        "source_id",
        "start_seconds",
        "end_seconds",
        "reason",
    }
    for index, edit in enumerate(edits):
        where = f"edits[{index}]"
        if not isinstance(edit, dict):
            errors.append(f"{where} must be an object")
            continue
        expected_edit = (
            legacy_edit_fields if schema_version == "1.0" else pair_edit_fields
        )
        unknown = sorted(set(edit) - expected_edit)
        if unknown:
            errors.append(f"{where} has unknown properties {unknown}")
        identity_field = "type" if schema_version == "1.0" else "effect"
        for field in (
            "id",
            "story_id",
            identity_field,
            "from_clip_id",
            "to_clip_id",
            "reason",
        ):
            if not isinstance(edit.get(field), str) or not edit[field].strip():
                errors.append(f"{where}.{field} must be a non-empty string")
        edit_id = edit.get("id")
        if isinstance(edit_id, str):
            if edit_id in seen_ids:
                errors.append(f"duplicate junction edit constraint ID {edit_id!r}")
            seen_ids.add(edit_id)
        if schema_version == "1.0":
            if edit.get("type") != LEGACY_EDIT_TYPE:
                errors.append(f"{where}.type must be {LEGACY_EDIT_TYPE!r}")
            strategy = REVIEWED_BRIDGE
        else:
            if edit.get("effect") != EDIT_EFFECT:
                errors.append(f"{where}.effect must be {EDIT_EFFECT!r}")
            strategy = edit.get("strategy")
            if strategy not in SUPPORTED_STRATEGIES:
                errors.append(
                    f"{where}.strategy must be one of "
                    f"{sorted(SUPPORTED_STRATEGIES)}"
                )
        if not _finite_number(edit.get("left_video_end_seconds")):
            errors.append(
                f"{where}.left_video_end_seconds must be a finite number"
            )
        max_tail = edit.get(
            "max_audio_tail_seconds", DEFAULT_MAX_AUDIO_TAIL_SECONDS
        )
        if not _finite_number(max_tail) or float(max_tail) <= 0:
            errors.append(
                f"{where}.max_audio_tail_seconds must be a positive finite number"
            )
        bridge = edit.get("bridge_candidate")
        if strategy == REVIEWED_BRIDGE and not isinstance(bridge, dict):
            errors.append(
                f"{where}.bridge_candidate must be an object for reviewed_bridge"
            )
        elif strategy == RIGHT_AV_OVERLAP and bridge is not None:
            errors.append(
                f"{where}.bridge_candidate is forbidden for right_av_overlap"
            )
        elif isinstance(bridge, dict):
            unknown_bridge = sorted(set(bridge) - expected_bridge)
            if unknown_bridge:
                errors.append(
                    f"{where}.bridge_candidate has unknown properties "
                    f"{unknown_bridge}"
                )
            if not isinstance(bridge.get("source_id"), str) or not bridge[
                "source_id"
            ].strip():
                errors.append(
                    f"{where}.bridge_candidate.source_id must be non-empty"
                )
            for field in ("safe_start_seconds", "safe_end_seconds"):
                if not _finite_number(bridge.get(field)):
                    errors.append(
                        f"{where}.bridge_candidate.{field} must be finite"
                    )
            if all(
                _finite_number(bridge.get(field))
                for field in ("safe_start_seconds", "safe_end_seconds")
            ) and float(bridge["safe_end_seconds"]) <= float(
                bridge["safe_start_seconds"]
            ):
                errors.append(
                    f"{where}.bridge_candidate must have positive duration"
                )
        if strategy == RIGHT_AV_OVERLAP:
            if edit.get("right_entry_visual_review") != "safe":
                errors.append(
                    f"{where}.right_entry_visual_review must be 'safe' for "
                    "right_av_overlap"
                )
            for field, default in (
                (
                    "max_simultaneous_speech_seconds",
                    MAX_SIMULTANEOUS_SPEECH_SECONDS,
                ),
                (
                    "left_audio_fade_out_seconds",
                    DEFAULT_LEFT_AUDIO_FADE_OUT_SECONDS,
                ),
                (
                    "right_audio_fade_in_seconds",
                    DEFAULT_RIGHT_AUDIO_FADE_IN_SECONDS,
                ),
            ):
                number = edit.get(field, default)
                if not _finite_number(number) or float(number) < 0:
                    errors.append(f"{where}.{field} must be a finite non-negative number")
                elif (
                    field == "max_simultaneous_speech_seconds"
                    and float(number)
                    > MAX_SIMULTANEOUS_SPEECH_SECONDS
                    + TIME_TOLERANCE_SECONDS
                ):
                    errors.append(
                        f"{where}.{field} cannot exceed the "
                        f"{MAX_SIMULTANEOUS_SPEECH_SECONDS:.3f}s hard limit"
                    )
        forbidden = edit.get("forbidden_visual_ranges")
        if not isinstance(forbidden, list) or not forbidden:
            errors.append(
                f"{where}.forbidden_visual_ranges must be a non-empty array"
            )
            continue
        for range_index, item in enumerate(forbidden):
            range_where = f"{where}.forbidden_visual_ranges[{range_index}]"
            if not isinstance(item, dict):
                errors.append(f"{range_where} must be an object")
                continue
            unknown_range = sorted(set(item) - expected_forbidden)
            if unknown_range:
                errors.append(
                    f"{range_where} has unknown properties {unknown_range}"
                )
            for field in ("source_id", "reason"):
                if not isinstance(item.get(field), str) or not item[
                    field
                ].strip():
                    errors.append(f"{range_where}.{field} must be non-empty")
            for field in ("start_seconds", "end_seconds"):
                if not _finite_number(item.get(field)):
                    errors.append(f"{range_where}.{field} must be finite")
            if all(
                _finite_number(item.get(field))
                for field in ("start_seconds", "end_seconds")
            ) and float(item["end_seconds"]) <= float(item["start_seconds"]):
                errors.append(f"{range_where} must have positive duration")
    return errors


def compile_story_plan(
    *,
    plan: dict[str, Any],
    constraints: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    input_fingerprints: dict[str, str],
    output_fps: int = DEFAULT_OUTPUT_FPS,
    speech_intervals_by_source: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Compile reviewed constraints for one Story into an executable plan."""

    if output_fps < 1:
        raise ValueError("junction edit output_fps must be positive")
    story_id = plan.get("story_id")
    if not isinstance(story_id, str) or not story_id:
        raise ValueError("effective Story Plan has no story_id")
    clips = ordered_clips(plan)
    if not clips:
        raise ValueError(f"{story_id}: effective Story Plan has no Clips")
    clips_by_id = {clip["id"]: clip for clip in clips}
    positions = {clip["id"]: index for index, clip in enumerate(clips)}
    compiled: list[dict[str, Any]] = []
    edited_clip_ids: set[str] = set()
    frame_duration = 1.0 / float(output_fps)
    for constraint in constraints:
        constraint_id = constraint["id"]
        if constraint.get("story_id") != story_id:
            raise ValueError(
                f"{constraint_id}: constraint Story differs from {story_id}"
            )
        from_clip_id = constraint["from_clip_id"]
        to_clip_id = constraint["to_clip_id"]
        left = clips_by_id.get(from_clip_id)
        right = clips_by_id.get(to_clip_id)
        if left is None or right is None:
            raise ValueError(
                f"{story_id}/{constraint_id}: junction references unknown Clips"
            )
        if positions[to_clip_id] != positions[from_clip_id] + 1:
            raise ValueError(
                f"{story_id}/{constraint_id}: junction Clips are not adjacent"
            )
        strategy = (
            REVIEWED_BRIDGE
            if constraint.get("type") == LEGACY_EDIT_TYPE
            else constraint.get("strategy")
        )
        if strategy not in SUPPORTED_STRATEGIES:
            raise ValueError(
                f"{story_id}/{constraint_id}: unsupported Junction strategy"
            )
        if strategy == RIGHT_AV_OVERLAP:
            if left.get("_block_role") == "teaser" or right.get(
                "_block_role"
            ) == "teaser":
                raise ValueError(
                    f"{story_id}/{constraint_id}: right_av_overlap is limited "
                    "to adjacent body Clips"
                )
            if left.get("episode") != right.get("episode"):
                raise ValueError(
                    f"{story_id}/{constraint_id}: right_av_overlap is limited "
                    "to one episode"
                )
        elif (
            left.get("_block_role") == "teaser"
            and right.get("_block_role") != "teaser"
        ):
            raise ValueError(
                f"{story_id}/{constraint_id}: teaser-to-body separator junction "
                "cannot carry an audio-tail bridge"
            )
        if from_clip_id in edited_clip_ids or to_clip_id in edited_clip_ids:
            raise ValueError(
                f"{story_id}/{constraint_id}: a Clip cannot participate in "
                "multiple Junction Edits"
            )
        edited_clip_ids.update((from_clip_id, to_clip_id))
        left_start = float(left["source_start"])
        left_audio_end = float(left["source_end"])
        left_video_end = float(constraint["left_video_end_seconds"])
        if not (
            left_start + TIME_TOLERANCE_SECONDS
            < left_video_end
            < left_audio_end - TIME_TOLERANCE_SECONDS
        ):
            raise ValueError(
                f"{story_id}/{constraint_id}: left_video_end_seconds must be "
                "strictly inside the left Clip"
            )
        audio_tail_duration = left_audio_end - left_video_end
        max_tail = float(
            constraint.get(
                "max_audio_tail_seconds", DEFAULT_MAX_AUDIO_TAIL_SECONDS
            )
        )
        if audio_tail_duration > max_tail + TIME_TOLERANCE_SECONDS:
            raise ValueError(
                f"{story_id}/{constraint_id}: audio tail exceeds "
                f"{max_tail:.3f}s policy"
            )
        forbidden_ranges: list[dict[str, Any]] = []
        for item in constraint["forbidden_visual_ranges"]:
            forbidden = {
                "source_id": item["source_id"],
                "start_seconds": rounded(item["start_seconds"]),
                "end_seconds": rounded(item["end_seconds"]),
                "reason": item["reason"],
            }
            forbidden_ranges.append(forbidden)
            if forbidden["source_id"] == left["source_id"] and _range_overlap(
                left_start,
                left_video_end,
                float(forbidden["start_seconds"]),
                float(forbidden["end_seconds"]),
            ) > TIME_TOLERANCE_SECONDS:
                raise ValueError(
                    f"{story_id}/{constraint_id}: retained left video overlaps "
                    "a forbidden visual range"
                )
        edit: dict[str, Any] = {
            "constraint_id": constraint_id,
            "type": (
                LEGACY_EDIT_TYPE
                if strategy == REVIEWED_BRIDGE
                else EDIT_EFFECT
            ),
            "effect": EDIT_EFFECT,
            "strategy": strategy,
            "from_clip_id": from_clip_id,
            "to_clip_id": to_clip_id,
            "from_source_id": left["source_id"],
            "to_source_id": right["source_id"],
            "left_video_end_seconds": rounded(left_video_end),
            "left_audio_end_seconds": rounded(left_audio_end),
            "audio_tail_duration_seconds": rounded(audio_tail_duration),
            "right_video_start_seconds": rounded(right["source_start"]),
            "right_audio_start_seconds": rounded(right["source_start"]),
            "preserve_left_audio": True,
            "preserve_right_audio": True,
            "forbidden_visual_ranges": forbidden_ranges,
            "reason": constraint["reason"],
        }
        identity: dict[str, Any] = {
            "constraint_id": constraint_id,
            "story_id": story_id,
            "strategy": strategy,
            "from_clip_id": from_clip_id,
            "to_clip_id": to_clip_id,
            "left_video_end_seconds": rounded(left_video_end),
            "left_audio_end_seconds": rounded(left_audio_end),
            "output_fps": output_fps,
        }
        if strategy == REVIEWED_BRIDGE:
            frame_count = int(
                math.ceil(audio_tail_duration * float(output_fps) - 1e-9)
            )
            bridge_duration = frame_count * frame_duration
            padding_duration = max(0.0, bridge_duration - audio_tail_duration)
            if padding_duration > frame_duration + TIME_TOLERANCE_SECONDS:
                raise ValueError(
                    f"{story_id}/{constraint_id}: frame alignment needs more "
                    "than one frame of silence"
                )
            bridge_candidate = constraint["bridge_candidate"]
            bridge_source_id = bridge_candidate["source_id"]
            bridge_source = sources.get(bridge_source_id)
            if bridge_source is None:
                raise ValueError(
                    f"{story_id}/{constraint_id}: bridge Source is unavailable"
                )
            safe_start = float(bridge_candidate["safe_start_seconds"])
            safe_end = float(bridge_candidate["safe_end_seconds"])
            selected_end = safe_start + bridge_duration
            source_duration = float(bridge_source["duration_seconds"])
            if safe_start < 0 or safe_end > source_duration + TIME_TOLERANCE_SECONDS:
                raise ValueError(
                    f"{story_id}/{constraint_id}: bridge safe range exceeds Source"
                )
            if selected_end > safe_end + TIME_TOLERANCE_SECONDS:
                raise ValueError(
                    f"{story_id}/{constraint_id}: bridge safe range is too short "
                    "for the preserved audio tail"
                )
            for forbidden in forbidden_ranges:
                if forbidden["source_id"] == bridge_source_id and _range_overlap(
                    safe_start,
                    selected_end,
                    float(forbidden["start_seconds"]),
                    float(forbidden["end_seconds"]),
                ) > TIME_TOLERANCE_SECONDS:
                    raise ValueError(
                        f"{story_id}/{constraint_id}: selected bridge overlaps "
                        "a forbidden visual range"
                    )
            edit["bridge"] = {
                "source_id": bridge_source_id,
                "source_start": rounded(safe_start),
                "source_end": rounded(selected_end),
                "duration_seconds": rounded(bridge_duration),
                "frame_count": frame_count,
                "audio_policy": "mute",
            }
            edit["audio_padding"] = {
                "type": "silence",
                "duration_seconds": rounded(padding_duration),
            }
            edit["duration_delta_seconds"] = rounded(
                bridge_duration - audio_tail_duration
            )
            identity.update(
                {
                    "bridge_source_id": bridge_source_id,
                    "bridge_source_start": rounded(safe_start),
                    "bridge_source_end": rounded(selected_end),
                }
            )
        else:
            if audio_tail_duration > HARD_MAX_OVERLAP_SECONDS + TIME_TOLERANCE_SECONDS:
                raise ValueError(
                    f"{story_id}/{constraint_id}: right_av_overlap exceeds the "
                    f"{HARD_MAX_OVERLAP_SECONDS:.3f}s hard cap"
                )
            right_duration = float(right["source_end"]) - float(
                right["source_start"]
            )
            if right_duration + TIME_TOLERANCE_SECONDS < audio_tail_duration:
                raise ValueError(
                    f"{story_id}/{constraint_id}: right Clip has insufficient "
                    "A/V handle for the overlap"
                )
            intervals = speech_intervals_by_source or {}
            if left["source_id"] not in intervals or right["source_id"] not in intervals:
                raise ValueError(
                    f"{story_id}/{constraint_id}: right_av_overlap requires "
                    "dual-track VAD evidence for both Sources"
                )
            if "audio_boundary_report_sha256" not in input_fingerprints:
                raise ValueError(
                    f"{story_id}/{constraint_id}: right_av_overlap must bind "
                    "the audio boundary report"
                )
            simultaneous = _simultaneous_speech_seconds(
                left_intervals=intervals[left["source_id"]],
                right_intervals=intervals[right["source_id"]],
                left_start=left_video_end,
                left_end=left_audio_end,
                right_start=float(right["source_start"]),
            )
            requested_simultaneous = float(
                constraint.get(
                    "max_simultaneous_speech_seconds",
                    MAX_SIMULTANEOUS_SPEECH_SECONDS,
                )
            )
            allowed_simultaneous = min(
                requested_simultaneous, MAX_SIMULTANEOUS_SPEECH_SECONDS
            )
            if simultaneous > allowed_simultaneous + TIME_TOLERANCE_SECONDS:
                raise ValueError(
                    f"{story_id}/{constraint_id}: simultaneous active dialogue "
                    f"is {simultaneous:.3f}s, above the "
                    f"{allowed_simultaneous:.3f}s safety limit"
                )
            left_fade = min(
                audio_tail_duration,
                float(
                    constraint.get(
                        "left_audio_fade_out_seconds",
                        DEFAULT_LEFT_AUDIO_FADE_OUT_SECONDS,
                    )
                ),
            )
            right_fade = min(
                audio_tail_duration,
                float(
                    constraint.get(
                        "right_audio_fade_in_seconds",
                        DEFAULT_RIGHT_AUDIO_FADE_IN_SECONDS,
                    )
                ),
            )
            edit["overlap"] = {
                "duration_seconds": rounded(audio_tail_duration),
                "left_audio_fade_out_seconds": rounded(left_fade),
                "right_audio_fade_in_seconds": rounded(right_fade),
                "simultaneous_speech_seconds": rounded(simultaneous),
                "max_simultaneous_speech_seconds": rounded(allowed_simultaneous),
                "right_entry_visual_review": "safe",
                "right_av_sync_offset_seconds": 0.0,
            }
            edit["duration_delta_seconds"] = rounded(-audio_tail_duration)
            identity["overlap"] = edit["overlap"]
        edit["id"] = stable_id("junction-edit", identity)
        compiled.append(edit)
    result = {
        "schema_version": "2.0",
        "method": PLAN_METHOD,
        "story_id": story_id,
        "title": plan.get("title", ""),
        "production_slot": plan.get("production_slot"),
        "status": "ready",
        "input_fingerprints": dict(input_fingerprints),
        "output_fps": output_fps,
        "edit_count": len(compiled),
        "edits": compiled,
    }
    errors = validate_junction_edit_plan(
        result, effective_plan=plan, sources=sources
    )
    if errors:
        raise ValueError(
            f"{story_id}: compiled Junction Edit Plan is invalid: "
            + "; ".join(errors[:30])
        )
    return result


def validate_junction_edit_plan(
    value: Any,
    *,
    effective_plan: dict[str, Any] | None = None,
    sources: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["Junction Edit Plan must be an object"]
    required = {
        "schema_version",
        "method",
        "story_id",
        "title",
        "production_slot",
        "status",
        "input_fingerprints",
        "output_fps",
        "edit_count",
        "edits",
    }
    unknown = sorted(set(value) - required)
    missing = sorted(required - set(value))
    if unknown:
        errors.append(f"Junction Edit Plan has unknown properties {unknown}")
    if missing:
        errors.append(f"Junction Edit Plan misses properties {missing}")
        return errors
    schema_version = value.get("schema_version")
    expected_method = {
        "1.0": LEGACY_PLAN_METHOD,
        "2.0": PLAN_METHOD,
    }.get(schema_version)
    if expected_method is None:
        errors.append("Junction Edit Plan schema_version must be '1.0' or '2.0'")
    elif value.get("method") != expected_method:
        errors.append(f"Junction Edit Plan method must be {expected_method!r}")
    if value.get("status") != "ready":
        errors.append("Junction Edit Plan status must be 'ready'")
    fps = value.get("output_fps")
    if not isinstance(fps, int) or isinstance(fps, bool) or fps < 1:
        errors.append("Junction Edit Plan output_fps must be a positive integer")
        return errors
    edits = value.get("edits")
    if not isinstance(edits, list) or not edits:
        errors.append("Junction Edit Plan edits must be non-empty")
        return errors
    if value.get("edit_count") != len(edits):
        errors.append("Junction Edit Plan edit_count is inconsistent")
    fingerprint_fields = {
        "effective_story_plan_index_sha256",
        "effective_story_plan_sha256",
        "constraints_sha256",
        "source_manifest_sha256",
        "local_source_manifest_sha256",
    }
    fingerprints = value.get("input_fingerprints")
    allowed_fingerprint_fields = fingerprint_fields | {
        "audio_boundary_report_sha256"
    }
    if (
        not isinstance(fingerprints, dict)
        or not fingerprint_fields.issubset(fingerprints)
        or not set(fingerprints).issubset(allowed_fingerprint_fields)
    ):
        errors.append("Junction Edit Plan input fingerprints are incomplete")
    elif any(
        not isinstance(item, str) or len(item) != 64
        for item in fingerprints.values()
    ):
        errors.append("Junction Edit Plan input fingerprints must be SHA-256")
    plan_clips = ordered_clips(effective_plan) if effective_plan else []
    clip_positions = {
        clip["id"]: index for index, clip in enumerate(plan_clips)
    }
    clips_by_id = {clip["id"]: clip for clip in plan_clips}
    seen_ids: set[str] = set()
    seen_clips: set[str] = set()
    for index, edit in enumerate(edits):
        where = f"edits[{index}]"
        if not isinstance(edit, dict):
            errors.append(f"{where} must be an object")
            continue
        edit_id = edit.get("id")
        if not isinstance(edit_id, str) or not edit_id:
            errors.append(f"{where}.id must be non-empty")
        elif edit_id in seen_ids:
            errors.append(f"duplicate Junction Edit ID {edit_id!r}")
        else:
            seen_ids.add(edit_id)
        strategy = junction_strategy(edit)
        if strategy not in SUPPORTED_STRATEGIES:
            errors.append(f"{where}.strategy is unsupported")
        if schema_version == "1.0":
            if edit.get("type") != LEGACY_EDIT_TYPE:
                errors.append(f"{where}.type is unsupported")
            if "strategy" in edit or "effect" in edit:
                errors.append(f"{where} edit has pair-timeline fields")
        else:
            if edit.get("effect") != EDIT_EFFECT:
                errors.append(f"{where}.effect is unsupported")
            expected_type = (
                LEGACY_EDIT_TYPE
                if strategy == REVIEWED_BRIDGE
                else EDIT_EFFECT
            )
            if edit.get("type") != expected_type:
                errors.append(f"{where}.type does not match its strategy")
        from_clip_id = edit.get("from_clip_id")
        to_clip_id = edit.get("to_clip_id")
        left_clip: dict[str, Any] | None = None
        for clip_id in (from_clip_id, to_clip_id):
            if isinstance(clip_id, str) and clip_id in seen_clips:
                errors.append(
                    f"{where} reuses Clip {clip_id!r} across Junction Edits"
                )
            elif isinstance(clip_id, str):
                seen_clips.add(clip_id)
        if effective_plan is not None:
            if from_clip_id not in clips_by_id or to_clip_id not in clips_by_id:
                errors.append(f"{where} references unknown effective Plan Clips")
                continue
            if clip_positions[to_clip_id] != clip_positions[from_clip_id] + 1:
                errors.append(f"{where} does not reference adjacent Clips")
            left = clips_by_id[from_clip_id]
            right = clips_by_id[to_clip_id]
            left_clip = left
            if strategy == RIGHT_AV_OVERLAP and (
                left.get("_block_role") == "teaser"
                or right.get("_block_role") == "teaser"
            ):
                errors.append(f"{where} right_av_overlap is not a body junction")
            elif strategy == RIGHT_AV_OVERLAP and left.get(
                "episode"
            ) != right.get("episode"):
                errors.append(f"{where} right_av_overlap crosses episodes")
            elif (
                left.get("_block_role") == "teaser"
                and right.get("_block_role") != "teaser"
            ):
                errors.append(
                    f"{where} cannot replace the teaser-to-body separator"
                )
            if edit.get("from_source_id") != left.get("source_id"):
                errors.append(f"{where}.from_source_id differs from Plan")
            if edit.get("to_source_id") != right.get("source_id"):
                errors.append(f"{where}.to_source_id differs from Plan")
            if abs(
                float(edit.get("left_audio_end_seconds", -1))
                - float(left["source_end"])
            ) > TIME_TOLERANCE_SECONDS:
                errors.append(f"{where} does not preserve the left audio end")
            for field in (
                "right_video_start_seconds",
                "right_audio_start_seconds",
            ):
                if abs(
                    float(edit.get(field, -1)) - float(right["source_start"])
                ) > TIME_TOLERANCE_SECONDS:
                    errors.append(f"{where}.{field} differs from the right Clip")
            left_start = float(left["source_start"])
            left_audio_end = float(left["source_end"])
            left_video_end = float(edit.get("left_video_end_seconds", -1))
            if not (
                left_start + TIME_TOLERANCE_SECONDS
                < left_video_end
                < left_audio_end - TIME_TOLERANCE_SECONDS
            ):
                errors.append(f"{where}.left_video_end_seconds is outside the Clip")
            if abs(
                float(edit.get("audio_tail_duration_seconds", -1))
                - (left_audio_end - left_video_end)
            ) > TIME_TOLERANCE_SECONDS:
                errors.append(f"{where}.audio_tail_duration_seconds is inconsistent")
        if edit.get("preserve_left_audio") is not True:
            errors.append(f"{where} must preserve left audio")
        if edit.get("preserve_right_audio") is not True:
            errors.append(f"{where} must preserve right audio")
        audio_tail = float(edit.get("audio_tail_duration_seconds", -1))
        bridge = edit.get("bridge")
        if strategy == REVIEWED_BRIDGE:
            if not isinstance(bridge, dict):
                errors.append(f"{where}.bridge must be an object")
                continue
            if bridge.get("audio_policy") != "mute":
                errors.append(f"{where}.bridge.audio_policy must be mute")
            if bridge.get("frame_count", 0) < 1:
                errors.append(f"{where}.bridge.frame_count must be positive")
            bridge_duration = float(bridge.get("duration_seconds", -1))
            expected_duration = float(bridge.get("frame_count", 0)) / fps
            if abs(bridge_duration - expected_duration) > TIME_TOLERANCE_SECONDS:
                errors.append(f"{where}.bridge duration is not frame aligned")
            if abs(
                float(bridge.get("source_end", -1))
                - float(bridge.get("source_start", 0))
                - bridge_duration
            ) > TIME_TOLERANCE_SECONDS:
                errors.append(f"{where}.bridge source duration is inconsistent")
            if sources is not None:
                source = sources.get(bridge.get("source_id"))
                if source is None:
                    errors.append(f"{where}.bridge Source is unavailable")
                elif float(bridge.get("source_end", 0)) > float(
                    source["duration_seconds"]
                ) + TIME_TOLERANCE_SECONDS:
                    errors.append(f"{where}.bridge exceeds its Source")
            padding = edit.get("audio_padding")
            if not isinstance(padding, dict) or padding.get("type") != "silence":
                errors.append(f"{where}.audio_padding must be silence")
            else:
                padding_duration = float(padding.get("duration_seconds", -1))
                if padding_duration < -TIME_TOLERANCE_SECONDS:
                    errors.append(f"{where}.audio_padding duration is negative")
                if abs(
                    audio_tail + padding_duration - bridge_duration
                ) > TIME_TOLERANCE_SECONDS:
                    errors.append(
                        f"{where} audio tail plus padding differs from bridge duration"
                    )
            expected_delta = bridge_duration - audio_tail
        else:
            if bridge is not None or "audio_padding" in edit:
                errors.append(f"{where} right_av_overlap cannot carry bridge fields")
            overlap = edit.get("overlap")
            if not isinstance(overlap, dict):
                errors.append(f"{where}.overlap must be an object")
                continue
            duration = float(overlap.get("duration_seconds", -1))
            if abs(duration - audio_tail) > TIME_TOLERANCE_SECONDS:
                errors.append(f"{where}.overlap duration differs from audio tail")
            if duration > HARD_MAX_OVERLAP_SECONDS + TIME_TOLERANCE_SECONDS:
                errors.append(f"{where}.overlap exceeds the hard cap")
            if overlap.get("right_entry_visual_review") != "safe":
                errors.append(f"{where}.overlap has no safe visual review")
            if abs(float(overlap.get("right_av_sync_offset_seconds", -1))) > TIME_TOLERANCE_SECONDS:
                errors.append(f"{where}.overlap moves right audio against video")
            simultaneous = float(overlap.get("simultaneous_speech_seconds", -1))
            allowed = float(overlap.get("max_simultaneous_speech_seconds", -1))
            if allowed > MAX_SIMULTANEOUS_SPEECH_SECONDS + TIME_TOLERANCE_SECONDS:
                errors.append(f"{where}.overlap weakens simultaneous-speech policy")
            if simultaneous < -TIME_TOLERANCE_SECONDS or simultaneous > allowed + TIME_TOLERANCE_SECONDS:
                errors.append(f"{where}.overlap has unsafe simultaneous speech")
            for field in (
                "left_audio_fade_out_seconds",
                "right_audio_fade_in_seconds",
            ):
                fade = float(overlap.get(field, -1))
                if fade < -TIME_TOLERANCE_SECONDS or fade > duration + TIME_TOLERANCE_SECONDS:
                    errors.append(f"{where}.overlap {field} is invalid")
            if not isinstance(fingerprints, dict) or "audio_boundary_report_sha256" not in fingerprints:
                errors.append(f"{where} is not bound to an audio boundary report")
            expected_delta = -audio_tail
        if abs(
            float(edit.get("duration_delta_seconds", expected_delta))
            - expected_delta
        ) > TIME_TOLERANCE_SECONDS:
            errors.append(f"{where}.duration_delta_seconds is inconsistent")
        for forbidden in edit.get("forbidden_visual_ranges", []):
            if (
                strategy == REVIEWED_BRIDGE
                and isinstance(bridge, dict)
                and forbidden.get("source_id") == bridge.get("source_id")
                and _range_overlap(
                    float(bridge["source_start"]),
                    float(bridge["source_end"]),
                    float(forbidden["start_seconds"]),
                    float(forbidden["end_seconds"]),
                )
                > TIME_TOLERANCE_SECONDS
            ):
                errors.append(f"{where}.bridge overlaps forbidden video")
            if (
                left_clip is not None
                and forbidden.get("source_id") == left_clip.get("source_id")
                and _range_overlap(
                    float(left_clip["source_start"]),
                    float(edit["left_video_end_seconds"]),
                    float(forbidden["start_seconds"]),
                    float(forbidden["end_seconds"]),
                )
                > TIME_TOLERANCE_SECONDS
            ):
                errors.append(
                    f"{where}.retained left video overlaps forbidden video"
                )
    return errors


def _manifest_source_map(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item
        for item in value.get("sources", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _load_speech_intervals(
    job_root: Path, audio_boundary_report_path: Path
) -> dict[str, list[dict[str, Any]]]:
    report = load_json(audio_boundary_report_path)
    result: dict[str, list[dict[str, Any]]] = {}
    for analysis in report.get("source_analyses", []):
        if not isinstance(analysis, dict) or not isinstance(
            analysis.get("source_id"), str
        ):
            continue
        vad_path = Path(str(analysis.get("vad_path", ""))).expanduser()
        if not vad_path.is_file():
            fingerprint = analysis.get("source_fingerprint")
            if isinstance(fingerprint, str) and len(fingerprint) == 64:
                vad_path = (
                    job_root
                    / ".audio-boundary-cache"
                    / fingerprint[:2]
                    / fingerprint
                    / "vad.json"
                )
        if not vad_path.is_file():
            continue
        vad = load_json(vad_path)
        intervals = vad.get("speech_intervals")
        if isinstance(intervals, list):
            result[analysis["source_id"]] = intervals
    return result


def compile_artifacts(
    job_root: Path,
    *,
    effective_plan_index_path: Path,
    constraints_path: Path,
    source_manifest_path: Path,
    local_source_manifest_path: Path,
    audio_boundary_report_path: Path | None = None,
    output_fps: int = DEFAULT_OUTPUT_FPS,
) -> dict[str, Any]:
    """Compile and persist content-bound per-Story Junction Edit Plans."""

    job_root = job_root.expanduser().resolve()
    effective_plan_index_path = effective_plan_index_path.expanduser().resolve()
    constraints_path = constraints_path.expanduser().resolve()
    source_manifest_path = source_manifest_path.expanduser().resolve()
    local_source_manifest_path = local_source_manifest_path.expanduser().resolve()
    audio_boundary_report_path = (
        audio_boundary_report_path.expanduser().resolve()
        if audio_boundary_report_path is not None
        else None
    )
    for path in (
        effective_plan_index_path,
        constraints_path,
        source_manifest_path,
        local_source_manifest_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(f"missing Junction Edit input: {path}")
    constraints_doc = load_json(constraints_path)
    constraint_errors = validate_constraints_document(constraints_doc)
    if constraint_errors:
        raise ValueError("; ".join(constraint_errors[:30]))
    needs_audio_evidence = any(
        isinstance(edit, dict)
        and edit.get("strategy") == RIGHT_AV_OVERLAP
        for edit in constraints_doc["edits"]
    )
    if needs_audio_evidence and (
        audio_boundary_report_path is None
        or not audio_boundary_report_path.is_file()
    ):
        raise FileNotFoundError(
            "right_av_overlap requires the current audio boundary report"
        )
    effective_index = load_json(effective_plan_index_path)
    public_sources = _manifest_source_map(load_json(source_manifest_path))
    local_sources = _manifest_source_map(load_json(local_source_manifest_path))
    sources: dict[str, dict[str, Any]] = {}
    for source_id, public in public_sources.items():
        local = local_sources.get(source_id)
        if local is None:
            continue
        if public.get("episode") != local.get("episode"):
            raise ValueError(f"{source_id}: local Source episode mismatch")
        sources[source_id] = {
            **public,
            **local,
            "duration_seconds": float(local["duration_seconds"]),
        }
    plan_entries = {
        item["story_id"]: item
        for item in effective_index.get("plans", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    constraints_by_story: dict[str, list[dict[str, Any]]] = {}
    for edit in constraints_doc["edits"]:
        constraints_by_story.setdefault(edit["story_id"], []).append(edit)
    unknown_stories = sorted(set(constraints_by_story) - set(plan_entries))
    if unknown_stories:
        raise ValueError(
            f"Junction Edit constraints reference unknown Stories: {unknown_stories}"
        )
    output_dir = job_root / "junction-edits"
    output_dir.mkdir(parents=True, exist_ok=True)
    index_entries: list[dict[str, Any]] = []
    constraints_sha256 = sha256_file(constraints_path)
    index_sha256 = sha256_file(effective_plan_index_path)
    source_sha256 = sha256_file(source_manifest_path)
    local_source_sha256 = sha256_file(local_source_manifest_path)
    audio_report_sha256 = (
        sha256_file(audio_boundary_report_path)
        if needs_audio_evidence and audio_boundary_report_path is not None
        else None
    )
    speech_intervals_by_source = (
        _load_speech_intervals(job_root, audio_boundary_report_path)
        if needs_audio_evidence and audio_boundary_report_path is not None
        else None
    )
    for story_id in sorted(
        constraints_by_story,
        key=lambda item: int(plan_entries[item].get("production_slot", 0)),
    ):
        plan_entry = plan_entries[story_id]
        plan_path = Path(plan_entry["path"]).expanduser().resolve()
        if not plan_path.is_file():
            raise FileNotFoundError(f"{story_id}: effective Story Plan is missing")
        actual_plan_sha256 = sha256_file(plan_path)
        if plan_entry.get("plan_sha256") != actual_plan_sha256:
            raise ValueError(f"{story_id}: effective Story Plan SHA-256 is stale")
        fingerprints = {
            "effective_story_plan_index_sha256": index_sha256,
            "effective_story_plan_sha256": actual_plan_sha256,
            "constraints_sha256": constraints_sha256,
            "source_manifest_sha256": source_sha256,
            "local_source_manifest_sha256": local_source_sha256,
        }
        if audio_report_sha256 is not None:
            fingerprints["audio_boundary_report_sha256"] = audio_report_sha256
        compiled = compile_story_plan(
            plan=load_json(plan_path),
            constraints=constraints_by_story[story_id],
            sources=sources,
            input_fingerprints=fingerprints,
            output_fps=output_fps,
            speech_intervals_by_source=speech_intervals_by_source,
        )
        output_path = output_dir / f"{story_id}.json"
        atomic_write_json(output_path, compiled)
        index_entries.append(
            {
                "story_id": story_id,
                "title": compiled["title"],
                "production_slot": compiled["production_slot"],
                "path": str(output_path),
                "sha256": sha256_file(output_path),
                "edit_count": compiled["edit_count"],
            }
        )
    index = {
        "schema_version": "2.0",
        "method": INDEX_METHOD,
        "status": "ready",
        "effective_story_plan_index_path": str(effective_plan_index_path),
        "effective_story_plan_index_sha256": index_sha256,
        "constraints_path": str(constraints_path),
        "constraints_sha256": constraints_sha256,
        "source_manifest_sha256": source_sha256,
        "local_source_manifest_path": str(local_source_manifest_path),
        "local_source_manifest_sha256": local_source_sha256,
        "plan_count": len(index_entries),
        "plans": index_entries,
    }
    if needs_audio_evidence and audio_boundary_report_path is not None:
        index["audio_boundary_report_path"] = str(audio_boundary_report_path)
        index["audio_boundary_report_sha256"] = audio_report_sha256
    atomic_write_json(output_dir / "index.json", index)
    return index


def load_index_plans(
    index_path: Path,
) -> tuple[dict[str, Any], dict[str, tuple[dict[str, Any], Path, str]]]:
    """Load an immutable Junction Edit Index and verify every plan hash."""

    index_path = index_path.expanduser().resolve()
    if not index_path.is_file():
        raise FileNotFoundError(f"missing Junction Edit Index: {index_path}")
    index = load_json(index_path)
    index_identity = (index.get("schema_version"), index.get("method"))
    if index_identity not in {
        ("1.0", LEGACY_INDEX_METHOD),
        ("2.0", INDEX_METHOD),
    }:
        raise ValueError("invalid Junction Edit Index identity")
    entries = index.get("plans")
    if not isinstance(entries, list) or index.get("plan_count") != len(entries):
        raise ValueError("Junction Edit Index plan_count is inconsistent")
    plans: dict[str, tuple[dict[str, Any], Path, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(
            entry.get("story_id"), str
        ):
            raise ValueError("Junction Edit Index contains an invalid entry")
        story_id = entry["story_id"]
        if story_id in plans:
            raise ValueError(f"duplicate Junction Edit Story {story_id}")
        path = Path(entry["path"]).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"{story_id}: Junction Edit Plan is missing")
        actual_sha256 = sha256_file(path)
        if entry.get("sha256") != actual_sha256:
            raise ValueError(f"{story_id}: Junction Edit Plan SHA-256 is stale")
        value = load_json(path)
        errors = validate_junction_edit_plan(value)
        if errors:
            raise ValueError(
                f"{story_id}: invalid Junction Edit Plan: "
                + "; ".join(errors[:30])
            )
        if value.get("story_id") != story_id:
            raise ValueError(f"{story_id}: Junction Edit Plan identity mismatch")
        if entry.get("edit_count") != value.get("edit_count"):
            raise ValueError(f"{story_id}: Junction Edit edit_count is stale")
        plans[story_id] = (value, path, actual_sha256)
    return index, plans
