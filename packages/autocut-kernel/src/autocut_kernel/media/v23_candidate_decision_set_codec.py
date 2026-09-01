"""Strict value decoder for the hash-bound V23 candidate decision set.

Decoding restores immutable values only.  Store identity and upstream Artifact
ownership are checked later by rereading committed dependencies and invoking
``verify_v23_candidate_decision_set``.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import TypeVar, cast

from .root_evidence_codec import decode_media_evidence_json, decode_time_base
from .timed_evidence_codec import decode_candidate_evidence_window
from .types import MediaValidationError, TickRange, require_pts, sha256_prefixed
from .v23_candidate_decision_set import V23CandidateDecisionSet
from .v23_candidate_evidence_window import (
    V23CandidateWindowCompileDecision,
    V23CandidateWindowCompileOutcome,
    V23CandidateWindowCompilePolicy,
    V23CandidateWindowCompileReason,
    V23DirectEventSupport,
)

_T = TypeVar("_T")
_E = TypeVar("_E", bound=Enum)


def _object(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise MediaValidationError("candidate decision value must be a JSON object")
    item = cast(dict[object, object], value)
    if any(type(key) is not str for key in item) or set(item) != set(fields):  # noqa: E721
        raise MediaValidationError("candidate decision object has missing or unknown fields")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if type(value) is not str or not value:  # noqa: E721
        raise MediaValidationError("candidate decision text must be an exact nonempty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise MediaValidationError("candidate decision text must be valid UTF-8") from error
    return value


def _hash(value: object) -> str:
    return sha256_prefixed(_text(value), "candidate decision hash")


def _integer(value: object) -> int:
    return require_pts(value, "candidate decision integer")


def _enum(value: object, enum_type: type[_E]) -> _E:
    try:
        return enum_type(_text(value))
    except ValueError as error:
        raise MediaValidationError("candidate decision enum is unsupported") from error


def _array(value: object, decoder: Callable[[object], _T]) -> tuple[_T, ...]:
    if type(value) is not list:  # noqa: E721
        raise MediaValidationError("candidate decision collection must be a JSON array")
    return tuple(decoder(item) for item in cast(list[object], value))


def _range(value: object) -> TickRange:
    item = _object(value, ("start_pts", "end_pts"))
    return TickRange(_integer(item["start_pts"]), _integer(item["end_pts"]))


def decode_v23_candidate_window_compile_policy(
    value: object,
) -> V23CandidateWindowCompilePolicy:
    item = _object(
        value,
        (
            "strategy_version",
            "time_base",
            "initial_left_expansion_pts",
            "initial_right_expansion_pts",
            "max_direct_event_gap_pts",
            "max_seed_duration_pts",
            "max_source_coverage_ppm",
        ),
    )
    return V23CandidateWindowCompilePolicy(
        strategy_version=_text(item["strategy_version"]),
        time_base=decode_time_base(item["time_base"]),
        initial_left_expansion_pts=_integer(item["initial_left_expansion_pts"]),
        initial_right_expansion_pts=_integer(item["initial_right_expansion_pts"]),
        max_direct_event_gap_pts=_integer(item["max_direct_event_gap_pts"]),
        max_seed_duration_pts=_integer(item["max_seed_duration_pts"]),
        max_source_coverage_ppm=_integer(item["max_source_coverage_ppm"]),
    )


def decode_v23_direct_event_support(value: object) -> V23DirectEventSupport:
    item = _object(
        value,
        (
            "event_ref",
            "coarse_range",
            "mapping_error_bound_source_pts",
            "uncertainty_expanded_range",
        ),
    )
    return V23DirectEventSupport(
        event_ref=_hash(item["event_ref"]),
        coarse_range=_range(item["coarse_range"]),
        mapping_error_bound_source_pts=_integer(item["mapping_error_bound_source_pts"]),
        uncertainty_expanded_range=_range(item["uncertainty_expanded_range"]),
    )


def decode_v23_candidate_window_compile_decision(
    value: object,
) -> V23CandidateWindowCompileDecision:
    item = _object(
        value,
        (
            "policy_sha256",
            "semantic_pack_sha256",
            "direct_support_dependency_sha256",
            "vlm_candidate_sha256",
            "candidate_id",
            "vlm_request_identity_sha256",
            "source_id",
            "source_sha256",
            "source_clock_id",
            "source_time_base",
            "window_manifest_sha256",
            "frame_pts_index_set_sha256",
            "source_range",
            "direct_event_supports",
            "direct_event_hull",
            "merged_uncertainty_regions",
            "outcome",
            "reason",
            "window",
        ),
    )
    window = item["window"]
    if window is not None and type(window) is not dict:  # noqa: E721
        raise MediaValidationError("candidate decision window must be an object or null")
    return V23CandidateWindowCompileDecision(
        policy_sha256=_hash(item["policy_sha256"]),
        semantic_pack_sha256=_hash(item["semantic_pack_sha256"]),
        direct_support_dependency_sha256=_hash(item["direct_support_dependency_sha256"]),
        vlm_candidate_sha256=_hash(item["vlm_candidate_sha256"]),
        candidate_id=_hash(item["candidate_id"]),
        vlm_request_identity_sha256=_hash(item["vlm_request_identity_sha256"]),
        source_id=_text(item["source_id"]),
        source_sha256=_hash(item["source_sha256"]),
        source_clock_id=_text(item["source_clock_id"]),
        source_time_base=decode_time_base(item["source_time_base"]),
        window_manifest_sha256=_hash(item["window_manifest_sha256"]),
        frame_pts_index_set_sha256=_hash(item["frame_pts_index_set_sha256"]),
        source_range=_range(item["source_range"]),
        direct_event_supports=_array(
            item["direct_event_supports"], decode_v23_direct_event_support
        ),
        direct_event_hull=_range(item["direct_event_hull"]),
        merged_uncertainty_regions=_array(item["merged_uncertainty_regions"], _range),
        outcome=_enum(item["outcome"], V23CandidateWindowCompileOutcome),
        reason=_enum(item["reason"], V23CandidateWindowCompileReason),
        window=(
            None
            if window is None
            else decode_candidate_evidence_window(cast(dict[str, object], window))
        ),
    )


def decode_v23_candidate_decision_set(value: object) -> V23CandidateDecisionSet:
    item = _object(
        value,
        (
            "schema_version",
            "compile_policy",
            "semantic_pack_sha256",
            "vlm_request_identity_sha256",
            "window_manifest_sha256",
            "frame_pts_index_set_sha256",
            "source_id",
            "source_sha256",
            "source_clock_id",
            "source_time_base",
            "stream_index",
            "source_range",
            "candidate_ids",
            "decisions",
        ),
    )
    return V23CandidateDecisionSet(
        schema_version=_text(item["schema_version"]),
        compile_policy=decode_v23_candidate_window_compile_policy(item["compile_policy"]),
        semantic_pack_sha256=_hash(item["semantic_pack_sha256"]),
        vlm_request_identity_sha256=_hash(item["vlm_request_identity_sha256"]),
        window_manifest_sha256=_hash(item["window_manifest_sha256"]),
        frame_pts_index_set_sha256=_hash(item["frame_pts_index_set_sha256"]),
        source_id=_text(item["source_id"]),
        source_sha256=_hash(item["source_sha256"]),
        source_clock_id=_text(item["source_clock_id"]),
        source_time_base=decode_time_base(item["source_time_base"]),
        stream_index=_integer(item["stream_index"]),
        source_range=_range(item["source_range"]),
        candidate_ids=_array(item["candidate_ids"], _hash),
        decisions=_array(item["decisions"], decode_v23_candidate_window_compile_decision),
    )


def decode_v23_candidate_decision_set_json(
    raw: bytes, *, max_bytes: int
) -> V23CandidateDecisionSet:
    """Decode bounded strict UTF-8 JSON; commitment is verified by a later reader."""

    return decode_v23_candidate_decision_set(decode_media_evidence_json(raw, max_bytes=max_bytes))


__all__ = [
    "decode_v23_candidate_decision_set",
    "decode_v23_candidate_decision_set_json",
    "decode_v23_candidate_window_compile_decision",
    "decode_v23_candidate_window_compile_policy",
    "decode_v23_direct_event_support",
]
