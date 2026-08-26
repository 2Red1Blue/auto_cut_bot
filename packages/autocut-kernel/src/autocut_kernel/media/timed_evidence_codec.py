"""Strict decoders for existing candidate-local timed-evidence producer wire.

Decoded records are immutable values only.  They neither establish committed
provenance nor admit an edit.  Root evidence field contracts stay in
``root_evidence_codec``; this module owns only the candidate wrapper wire.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import TypeVar, cast

from .root_evidence_codec import (
    decode_audio_sample_boundary_set,
    decode_frame_pts_index_set,
    decode_scene_boundary_set,
    decode_shot_boundary_set,
    decode_speech_activity_set,
    decode_subtitle_cue_set,
    decode_time_base,
    decode_transcript_set,
    decode_visual_validity_set,
)
from .timed_evidence import (
    AdaptiveEvidenceWindowPolicy,
    CalibrationBinding,
    CandidateEvidenceWindow,
    CandidateEvidenceWindowPlan,
    CandidateTimedEvidenceSet,
    CandidateWindowAssessment,
    CandidateWindowOutcome,
    SentenceCompleteness,
)
from .types import MediaValidationError, TickRange, require_pts, sha256_prefixed

_T = TypeVar("_T")
_E = TypeVar("_E", bound=Enum)


def _object(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise MediaValidationError("timed evidence value must be a JSON object")
    item = cast(dict[object, object], value)
    if any(type(key) is not str for key in item) or set(item) != set(fields):  # noqa: E721
        raise MediaValidationError("timed evidence object has missing or unknown fields")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if type(value) is not str or not value:  # noqa: E721
        raise MediaValidationError("timed evidence text must be an exact nonempty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise MediaValidationError("timed evidence text must be valid UTF-8") from error
    return value


def _hash(value: object) -> str:
    return sha256_prefixed(_text(value), "timed evidence hash")


def _integer(value: object) -> int:
    return require_pts(value, "timed evidence integer")


def _boolean(value: object) -> bool:
    if type(value) is not bool:  # noqa: E721
        raise MediaValidationError("timed evidence flag must be an exact boolean")
    return value


def _enum(value: object, enum: type[_E]) -> _E:
    try:
        return enum(_text(value))
    except ValueError as error:
        raise MediaValidationError("timed evidence enum value is unsupported") from error


def _array(value: object, decode: Callable[[object], _T]) -> tuple[_T, ...]:
    if type(value) is not list:  # noqa: E721
        raise MediaValidationError("timed evidence collection must be a JSON array")
    return tuple(decode(item) for item in cast(list[object], value))


def _range(value: object) -> TickRange:
    item = _object(value, ("start_pts", "end_pts"))
    return TickRange(_integer(item["start_pts"]), _integer(item["end_pts"]))


def decode_adaptive_evidence_window_policy(value: object) -> AdaptiveEvidenceWindowPolicy:
    item = _object(value, (
        "strategy_version", "time_base", "initial_left_expansion_pts",
        "initial_right_expansion_pts", "expansion_step_pts", "max_expansion_count",
        "boundary_touch_margin_pts",
    ))
    return AdaptiveEvidenceWindowPolicy(
        _text(item["strategy_version"]), decode_time_base(item["time_base"]),
        _integer(item["initial_left_expansion_pts"]),
        _integer(item["initial_right_expansion_pts"]), _integer(item["expansion_step_pts"]),
        _integer(item["max_expansion_count"]), _integer(item["boundary_touch_margin_pts"]),
    )


def decode_calibration_binding(value: object) -> CalibrationBinding:
    item = _object(value, (
        "policy_sha256", "detector_sha256", "calibration_record_sha256", "producer_id",
        "producer_version", "time_base", "timing_error_bound_tick", "active", "adapter_sha256",
    ))
    adapter = item["adapter_sha256"]
    if adapter is not None and type(adapter) is not str:  # noqa: E721
        raise MediaValidationError("timed evidence adapter_sha256 must be a hash or null")
    return CalibrationBinding(
        _hash(item["policy_sha256"]), _hash(item["detector_sha256"]),
        _hash(item["calibration_record_sha256"]), _text(item["producer_id"]),
        _text(item["producer_version"]), decode_time_base(item["time_base"]),
        _integer(item["timing_error_bound_tick"]), _boolean(item["active"]),
        None if adapter is None else _hash(adapter),
    )


def decode_candidate_evidence_window(value: object) -> CandidateEvidenceWindow:
    item = _object(value, (
        "source_id", "source_sha256", "source_clock_id", "source_time_base", "source_range",
        "vlm_candidate_sha256", "vlm_request_identity_sha256", "window_manifest_sha256",
        "frame_pts_index_set_sha256", "coarse_range", "current_range", "expansion_ordinal",
    ))
    return CandidateEvidenceWindow(
        _text(item["source_id"]), _hash(item["source_sha256"]), _text(item["source_clock_id"]),
        decode_time_base(item["source_time_base"]), _range(item["source_range"]),
        _hash(item["vlm_candidate_sha256"]), _hash(item["vlm_request_identity_sha256"]),
        _hash(item["window_manifest_sha256"]), _hash(item["frame_pts_index_set_sha256"]),
        _range(item["coarse_range"]), _range(item["current_range"]),
        _integer(item["expansion_ordinal"]),
    )


def _assessment(value: object) -> CandidateWindowAssessment:
    item = _object(value, (
        "candidate_window_sha256", "transcript_left_boundary_touch",
        "transcript_right_boundary_touch", "speech_left_boundary_touch",
        "speech_right_boundary_touch", "left_truncated", "right_truncated",
        "sentence_completeness",
    ))
    return CandidateWindowAssessment(
        _hash(item["candidate_window_sha256"]),
        _boolean(item["transcript_left_boundary_touch"]),
        _boolean(item["transcript_right_boundary_touch"]),
        _boolean(item["speech_left_boundary_touch"]),
        _boolean(item["speech_right_boundary_touch"]), _boolean(item["left_truncated"]),
        _boolean(item["right_truncated"]), _enum(item["sentence_completeness"], SentenceCompleteness),
    )


def decode_candidate_evidence_window_plan(value: object) -> CandidateEvidenceWindowPlan:
    item = _object(value, (
        "policy_sha256", "max_expansion_count", "vlm_candidate_sha256",
        "window_manifest_sha256", "windows", "assessments", "outcome",
    ))
    return CandidateEvidenceWindowPlan(
        _hash(item["policy_sha256"]), _integer(item["max_expansion_count"]),
        _hash(item["vlm_candidate_sha256"]), _hash(item["window_manifest_sha256"]),
        _array(item["windows"], decode_candidate_evidence_window),
        _array(item["assessments"], _assessment), _enum(item["outcome"], CandidateWindowOutcome),
    )


def decode_candidate_timed_evidence_set(value: object) -> CandidateTimedEvidenceSet:
    item = _object(value, (
        "candidate_window", "window_assessment", "transcript", "speech_activity",
        "audio_sample_boundaries", "frame_pts_index", "shot_boundaries", "scene_boundaries",
        "visual_validity", "subtitle_cues", "calibration_bindings",
    ))
    return CandidateTimedEvidenceSet(
        decode_candidate_evidence_window(item["candidate_window"]), _assessment(item["window_assessment"]),
        decode_transcript_set(item["transcript"]), decode_speech_activity_set(item["speech_activity"]),
        decode_audio_sample_boundary_set(item["audio_sample_boundaries"]),
        decode_frame_pts_index_set(item["frame_pts_index"]), decode_shot_boundary_set(item["shot_boundaries"]),
        decode_scene_boundary_set(item["scene_boundaries"]), decode_visual_validity_set(item["visual_validity"]),
        decode_subtitle_cue_set(item["subtitle_cues"]),
        _array(item["calibration_bindings"], decode_calibration_binding),
    )


__all__ = [
    "decode_adaptive_evidence_window_policy",
    "decode_calibration_binding",
    "decode_candidate_evidence_window",
    "decode_candidate_evidence_window_plan",
    "decode_candidate_timed_evidence_set",
]
