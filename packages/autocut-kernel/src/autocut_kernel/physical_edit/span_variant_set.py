"""Closed bounded variants over one complete candidate-local A/V relation.

The retained variants are a canonical top-K projection, never the complete
relation.  Relation completeness is represented by its full count and digest;
Store commitment and parent Recipe verification remain command-layer duties.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final, Mapping, cast

from ..media.types import (
    MediaValidationError,
    TickRange,
    TimeBase,
    canonical_sha256,
    require_pts,
    sha256_prefixed,
)
from .candidate_dialogue_guard import CandidateDialogueGuard
from .candidate_exact_span import CandidateExactSpanResult
from .candidate_timed_speech_authority import CandidateTimedSpeechAuthorityKind
from .dialogue_guard import DialogueGuardKind, DialogueRequirement, ProtectedAudioRange
from .editorial_exact_span import EditorialExactSpanQuery
from .exact_span import BoundaryProof

SPAN_VARIANT_SET_SCHEMA_VERSION: Final = "span-variant-set-v1"
SPAN_VARIANT_SET_POLICY_SCHEMA_VERSION: Final = "span-variant-set-policy-v1"
_MAX_PORTABLE_COUNT: Final = 2**53 - 1
_MAX_VARIANTS: Final = 16
_DECIMAL_COUNT: Final = re.compile(r"(?:0|[1-9][0-9]*)\Z")


class SpanVariantSetError(ValueError):
    """A bounded span variant value is malformed or internally inconsistent."""


def _object(value: object, fields: tuple[str, ...], label: str) -> Mapping[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise SpanVariantSetError(f"{label} must be a closed object")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw) or set(raw) != set(fields):  # noqa: E721
        raise SpanVariantSetError(f"{label} has missing or unknown fields")
    return cast(Mapping[str, object], raw)


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise SpanVariantSetError(f"{label} must be an array")
    return cast(list[object], value)


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise SpanVariantSetError(f"{label} must be non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise SpanVariantSetError(f"{label} must be valid UTF-8") from error
    return value


def _integer(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int = _MAX_PORTABLE_COUNT,
) -> int:
    try:
        result = require_pts(value, label)
    except MediaValidationError as error:
        raise SpanVariantSetError(str(error)) from error
    lower = -maximum if minimum is None else minimum
    if not lower <= result <= maximum:
        raise SpanVariantSetError(f"{label} is outside its exact integer bounds")
    return result


def _hash(value: object, label: str) -> str:
    try:
        result = sha256_prefixed(value, label)
    except MediaValidationError as error:
        raise SpanVariantSetError(str(error)) from error
    if result == "sha256:" + "0" * 64:
        raise SpanVariantSetError(f"{label} must not be the all-zero digest")
    return result


def _count(value: object, label: str) -> int:
    text = _text(value, label)
    if _DECIMAL_COUNT.fullmatch(text) is None:
        raise SpanVariantSetError(f"{label} must be a canonical decimal count")
    result = int(text)
    if result > _MAX_PORTABLE_COUNT:
        raise SpanVariantSetError(f"{label} exceeds the portable exact-integer limit")
    return result


def _time_base(value: object, label: str) -> TimeBase:
    raw = _object(value, ("numerator", "denominator"), label)
    try:
        return TimeBase(
            _integer(raw["numerator"], f"{label}.numerator", minimum=1),
            _integer(raw["denominator"], f"{label}.denominator", minimum=1),
        )
    except MediaValidationError as error:
        raise SpanVariantSetError(str(error)) from error


def _tick_range(value: object, label: str) -> TickRange:
    raw = _object(value, ("start_pts", "end_pts"), label)
    try:
        return TickRange(
            _integer(raw["start_pts"], f"{label}.start_pts"),
            _integer(raw["end_pts"], f"{label}.end_pts"),
        )
    except MediaValidationError as error:
        raise SpanVariantSetError(str(error)) from error


def _protected_range(value: object, ordinal: int) -> ProtectedAudioRange:
    label = f"dialogue_guard.protected_ranges[{ordinal}]"
    raw = _object(
        value,
        ("source_id", "source_sha256", "clock_id", "time_base", "in_tick", "out_tick"),
        label,
    )
    try:
        return ProtectedAudioRange(
            _text(raw["source_id"], f"{label}.source_id"),
            _hash(raw["source_sha256"], f"{label}.source_sha256"),
            _text(raw["clock_id"], f"{label}.clock_id"),
            _time_base(raw["time_base"], f"{label}.time_base"),
            _integer(raw["in_tick"], f"{label}.in_tick"),
            _integer(raw["out_tick"], f"{label}.out_tick"),
        )
    except ValueError as error:
        raise SpanVariantSetError(str(error)) from error


def _dialogue_guard(value: object) -> CandidateDialogueGuard:
    fields = (
        "schema_version",
        "root_evidence_sha256",
        "candidate_evidence_sha256",
        "candidate_window_sha256",
        "window_plan_sha256",
        "timed_speech_authority_sha256",
        "original_authority_kind",
        "original_authority_sha256",
        "guard_policy_sha256",
        "source_id",
        "source_sha256",
        "source_audio_clock_id",
        "source_audio_time_base",
        "source_audio_range",
        "requirement",
        "kind",
        "reason",
        "protected_ranges",
    )
    raw = _object(value, fields, "dialogue_guard")
    if raw["schema_version"] != "candidate-dialogue-guard-v2":
        raise SpanVariantSetError("dialogue guard schema is unsupported")
    coverage = raw["source_audio_range"]
    try:
        return CandidateDialogueGuard(
            root_evidence_sha256=_hash(raw["root_evidence_sha256"], "root_evidence_sha256"),
            candidate_evidence_sha256=_hash(
                raw["candidate_evidence_sha256"], "candidate_evidence_sha256"
            ),
            candidate_window_sha256=_hash(
                raw["candidate_window_sha256"], "candidate_window_sha256"
            ),
            window_plan_sha256=_hash(raw["window_plan_sha256"], "window_plan_sha256"),
            timed_speech_authority_sha256=_hash(
                raw["timed_speech_authority_sha256"], "timed_speech_authority_sha256"
            ),
            original_authority_kind=CandidateTimedSpeechAuthorityKind(
                _text(raw["original_authority_kind"], "original_authority_kind")
            ),
            original_authority_sha256=_hash(
                raw["original_authority_sha256"], "original_authority_sha256"
            ),
            guard_policy_sha256=_hash(raw["guard_policy_sha256"], "guard_policy_sha256"),
            source_id=_text(raw["source_id"], "source_id"),
            source_sha256=_hash(raw["source_sha256"], "source_sha256"),
            source_audio_clock_id=_text(raw["source_audio_clock_id"], "source_audio_clock_id"),
            source_audio_time_base=_time_base(
                raw["source_audio_time_base"], "source_audio_time_base"
            ),
            source_audio_range=(
                None if coverage is None else _tick_range(coverage, "source_audio_range")
            ),
            requirement=DialogueRequirement(_text(raw["requirement"], "requirement")),
            kind=DialogueGuardKind(_text(raw["kind"], "kind")),
            reason=_text(raw["reason"], "reason"),
            protected_ranges=tuple(
                _protected_range(item, index)
                for index, item in enumerate(_array(raw["protected_ranges"], "protected_ranges"))
            ),
        )
    except ValueError as error:
        raise SpanVariantSetError(str(error)) from error


def _boundary_proof(value: object) -> BoundaryProof:
    fields = (
        "source_id",
        "source_sha256",
        "video_clock_id",
        "video_time_base",
        "video_in_tick",
        "video_out_tick",
        "audio_clock_id",
        "audio_time_base",
        "audio_in_tick",
        "audio_out_tick",
        "frame_pts_index_set_sha256",
        "audio_sample_boundary_set_sha256",
        "visual_validity_set_sha256",
        "subtitle_cue_set_sha256",
        "clock_map_certificate_sha256",
    )
    raw = _object(value, fields, "boundary_proof")
    return BoundaryProof(
        _text(raw["source_id"], "boundary_proof.source_id"),
        _hash(raw["source_sha256"], "boundary_proof.source_sha256"),
        _text(raw["video_clock_id"], "boundary_proof.video_clock_id"),
        _time_base(raw["video_time_base"], "boundary_proof.video_time_base"),
        _integer(raw["video_in_tick"], "boundary_proof.video_in_tick"),
        _integer(raw["video_out_tick"], "boundary_proof.video_out_tick"),
        _text(raw["audio_clock_id"], "boundary_proof.audio_clock_id"),
        _time_base(raw["audio_time_base"], "boundary_proof.audio_time_base"),
        _integer(raw["audio_in_tick"], "boundary_proof.audio_in_tick"),
        _integer(raw["audio_out_tick"], "boundary_proof.audio_out_tick"),
        _hash(raw["frame_pts_index_set_sha256"], "frame_pts_index_set_sha256"),
        _hash(raw["audio_sample_boundary_set_sha256"], "audio_sample_boundary_set_sha256"),
        _hash(raw["visual_validity_set_sha256"], "visual_validity_set_sha256"),
        _hash(raw["subtitle_cue_set_sha256"], "subtitle_cue_set_sha256"),
        _hash(raw["clock_map_certificate_sha256"], "clock_map_certificate_sha256"),
    )


def _validate_exact_result(result: CandidateExactSpanResult) -> None:
    if type(result) is not CandidateExactSpanResult:  # noqa: E721
        raise SpanVariantSetError("variant requires an exact candidate A/V result")
    if type(result.video_range) is not TickRange or type(result.audio_range) is not TickRange:  # noqa: E721
        raise SpanVariantSetError("variant result must retain exact TickRange values")
    if type(result.boundary_proof) is not BoundaryProof:  # noqa: E721
        raise SpanVariantSetError("variant result must retain a BoundaryProof")
    if type(result.dialogue_guard) is not CandidateDialogueGuard:  # noqa: E721
        raise SpanVariantSetError("variant result must retain a CandidateDialogueGuard")
    if result.dialogue_guard.source_audio_range is None:
        raise SpanVariantSetError("candidate A/V variants require proven local audio coverage")
    _integer(result.common_segment_ordinal, "common_segment_ordinal")
    key = result.canonical_decision_key
    if type(key) is not tuple or len(key) != 10:  # noqa: E721
        raise SpanVariantSetError("canonical decision key must contain ten exact integers")
    for index, item in enumerate(key):
        _integer(item, f"canonical_decision_key[{index}]")
    endpoints = (
        result.video_range.start_pts,
        result.video_range.end_pts,
        result.audio_range.start_pts,
        result.audio_range.end_pts,
    )
    if key[-4:] != endpoints:
        raise SpanVariantSetError("canonical decision key endpoints differ from the result")
    logical_count = _count(result.logical_cartesian_count_decimal, "logical_cartesian_count_decimal")
    visits = _integer(result.visited_av_pair_count, "visited_av_pair_count", minimum=1)
    feasible = _integer(result.feasible_count, "feasible_count", minimum=1)
    if feasible > visits or feasible > logical_count:
        raise SpanVariantSetError("variant result counts are inconsistent")
    for name in (
        "request_sha256",
        "policy_sha256",
        "candidate_domain_sha256",
        "feasible_relation_sha256",
    ):
        _hash(getattr(result, name), f"exact_span_result.{name}")
    proof = result.boundary_proof
    _text(proof.source_id, "boundary_proof.source_id")
    _text(proof.video_clock_id, "boundary_proof.video_clock_id")
    _text(proof.audio_clock_id, "boundary_proof.audio_clock_id")
    if type(proof.video_time_base) is not TimeBase or type(proof.audio_time_base) is not TimeBase:  # noqa: E721
        raise SpanVariantSetError("BoundaryProof must retain exact A/V TimeBase values")
    for name in (
        "source_sha256",
        "frame_pts_index_set_sha256",
        "audio_sample_boundary_set_sha256",
        "visual_validity_set_sha256",
        "subtitle_cue_set_sha256",
        "clock_map_certificate_sha256",
    ):
        _hash(getattr(proof, name), f"boundary_proof.{name}")
    if (
        proof.video_in_tick,
        proof.video_out_tick,
        proof.audio_in_tick,
        proof.audio_out_tick,
    ) != endpoints:
        raise SpanVariantSetError("BoundaryProof endpoints differ from the result")
    guard = result.dialogue_guard
    if (
        proof.source_id,
        proof.source_sha256,
        proof.audio_clock_id,
        proof.audio_time_base,
    ) != (
        guard.source_id,
        guard.source_sha256,
        guard.source_audio_clock_id,
        guard.source_audio_time_base,
    ):
        raise SpanVariantSetError("BoundaryProof and DialogueGuard source clocks differ")


def _exact_result(value: object) -> CandidateExactSpanResult:
    fields = (
        "strategy",
        "video_range",
        "audio_range",
        "boundary_proof",
        "dialogue_guard",
        "common_segment_ordinal",
        "canonical_decision_key",
        "logical_cartesian_count_decimal",
        "visited_av_pair_count",
        "feasible_count",
        "request_sha256",
        "policy_sha256",
        "candidate_domain_sha256",
        "feasible_relation_sha256",
    )
    raw = _object(value, fields, "exact_span_result")
    if raw["strategy"] != "candidate-local-exact-v1":
        raise SpanVariantSetError("exact span result strategy is unsupported")
    result = CandidateExactSpanResult(
        video_range=_tick_range(raw["video_range"], "video_range"),
        audio_range=_tick_range(raw["audio_range"], "audio_range"),
        boundary_proof=_boundary_proof(raw["boundary_proof"]),
        dialogue_guard=_dialogue_guard(raw["dialogue_guard"]),
        common_segment_ordinal=_integer(raw["common_segment_ordinal"], "common_segment_ordinal"),
        canonical_decision_key=tuple(
            _integer(item, f"canonical_decision_key[{index}]")
            for index, item in enumerate(_array(raw["canonical_decision_key"], "decision_key"))
        ),
        logical_cartesian_count_decimal=_text(
            raw["logical_cartesian_count_decimal"], "logical_cartesian_count_decimal"
        ),
        visited_av_pair_count=_integer(
            raw["visited_av_pair_count"], "visited_av_pair_count", minimum=1
        ),
        feasible_count=_integer(raw["feasible_count"], "feasible_count", minimum=1),
        request_sha256=_hash(raw["request_sha256"], "request_sha256"),
        policy_sha256=_hash(raw["policy_sha256"], "policy_sha256"),
        candidate_domain_sha256=_hash(
            raw["candidate_domain_sha256"], "candidate_domain_sha256"
        ),
        feasible_relation_sha256=_hash(
            raw["feasible_relation_sha256"], "feasible_relation_sha256"
        ),
    )
    _validate_exact_result(result)
    return result


def _variant_id(query_sha256: str, result: CandidateExactSpanResult) -> str:
    return canonical_sha256(
        {
            "schema_version": "span-variant-identity-v1",
            "exact_span_query_sha256": query_sha256,
            "exact_span_result_sha256": result.canonical_hash,
        }
    )


@dataclass(frozen=True, slots=True)
class SpanVariantSetPolicy:
    max_variants: int

    def __post_init__(self) -> None:
        _integer(self.max_variants, "max_variants", minimum=1, maximum=_MAX_VARIANTS)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": SPAN_VARIANT_SET_POLICY_SCHEMA_VERSION,
            "max_variants": self.max_variants,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class SpanVariant:
    ordinal: int
    variant_id: str
    exact_span_query_sha256: str
    exact_span_result: CandidateExactSpanResult

    def __post_init__(self) -> None:
        _integer(self.ordinal, "variant.ordinal", maximum=_MAX_VARIANTS - 1)
        query_hash = _hash(self.exact_span_query_sha256, "variant.exact_span_query_sha256")
        _validate_exact_result(self.exact_span_result)
        if self.variant_id != _variant_id(query_hash, self.exact_span_result):
            raise SpanVariantSetError("variant_id is stale or forged")

    @classmethod
    def from_result(
        cls,
        *,
        ordinal: int,
        exact_span_query_sha256: str,
        result: CandidateExactSpanResult,
    ) -> SpanVariant:
        query_hash = _hash(exact_span_query_sha256, "exact_span_query_sha256")
        return cls(ordinal, _variant_id(query_hash, result), query_hash, result)

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "variant_id": self.variant_id,
            "exact_span_query_sha256": self.exact_span_query_sha256,
            "exact_span_result": self.exact_span_result.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class SpanVariantEntry:
    ordinal: int
    story_id: str
    beat_id: str
    requirement_id: str
    alternative_id: str
    candidate_id: str
    exact_span_query_sha256: str
    feasible_count: int
    omitted_count: int
    request_sha256: str
    policy_sha256: str
    candidate_domain_sha256: str
    feasible_relation_sha256: str
    variants: tuple[SpanVariant, ...]

    def __post_init__(self) -> None:
        _integer(self.ordinal, "entry.ordinal")
        for name in ("story_id", "beat_id", "requirement_id", "alternative_id", "candidate_id"):
            _text(getattr(self, name), f"entry.{name}")
        query_hash = _hash(self.exact_span_query_sha256, "entry.exact_span_query_sha256")
        feasible = _integer(self.feasible_count, "entry.feasible_count", minimum=1)
        omitted = _integer(self.omitted_count, "entry.omitted_count")
        for name in (
            "request_sha256",
            "policy_sha256",
            "candidate_domain_sha256",
            "feasible_relation_sha256",
        ):
            _hash(getattr(self, name), f"entry.{name}")
        if (
            type(self.variants) is not tuple  # noqa: E721
            or not self.variants
            or len(self.variants) > _MAX_VARIANTS
            or any(type(item) is not SpanVariant for item in self.variants)  # noqa: E721
        ):
            raise SpanVariantSetError("entry variants must be a non-empty bounded tuple")
        if feasible - len(self.variants) != omitted:
            raise SpanVariantSetError("omitted_count does not close the complete feasible count")
        if tuple(item.ordinal for item in self.variants) != tuple(range(len(self.variants))):
            raise SpanVariantSetError("variant ordinals must be contiguous from zero")
        keys = tuple(item.exact_span_result.canonical_decision_key for item in self.variants)
        if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
            raise SpanVariantSetError("retained variants must be strictly canonical ordered")
        first = self.variants[0].exact_span_result
        shared = (
            first.logical_cartesian_count_decimal,
            first.visited_av_pair_count,
            first.feasible_count,
            first.request_sha256,
            first.policy_sha256,
            first.candidate_domain_sha256,
            first.feasible_relation_sha256,
            first.dialogue_guard.canonical_hash,
        )
        for item in self.variants:
            result = item.exact_span_result
            if item.exact_span_query_sha256 != query_hash:
                raise SpanVariantSetError("variant query binding differs from its entry")
            if (
                result.logical_cartesian_count_decimal,
                result.visited_av_pair_count,
                result.feasible_count,
                result.request_sha256,
                result.policy_sha256,
                result.candidate_domain_sha256,
                result.feasible_relation_sha256,
                result.dialogue_guard.canonical_hash,
            ) != shared:
                raise SpanVariantSetError("retained variants do not describe one complete relation")
        if (
            feasible,
            self.request_sha256,
            self.policy_sha256,
            self.candidate_domain_sha256,
            self.feasible_relation_sha256,
        ) != (
            first.feasible_count,
            first.request_sha256,
            first.policy_sha256,
            first.candidate_domain_sha256,
            first.feasible_relation_sha256,
        ):
            raise SpanVariantSetError("entry relation evidence differs from its retained variants")

    @classmethod
    def from_results(
        cls,
        *,
        ordinal: int,
        story_id: str,
        beat_id: str,
        requirement_id: str,
        alternative_id: str,
        candidate_id: str,
        query: EditorialExactSpanQuery,
        results: tuple[CandidateExactSpanResult, ...],
    ) -> SpanVariantEntry:
        if type(query) is not EditorialExactSpanQuery:  # noqa: E721
            raise SpanVariantSetError("entry requires an exact editorial query")
        if (
            story_id,
            beat_id,
            requirement_id,
            alternative_id,
            candidate_id,
        ) != (
            query.story_id,
            query.beat_id,
            query.evidence_requirement_id,
            query.alternative_id,
            query.candidate_id,
        ):
            raise SpanVariantSetError("entry identities differ from its exact editorial query")
        if type(results) is not tuple or not results:  # noqa: E721
            raise SpanVariantSetError("entry requires a non-empty exact result tuple")
        query_hash = query.canonical_hash
        variants = tuple(
            SpanVariant.from_result(
                ordinal=index,
                exact_span_query_sha256=query_hash,
                result=result,
            )
            for index, result in enumerate(results)
        )
        first = results[0]
        if first.request_sha256 != query.request.canonical_hash:
            raise SpanVariantSetError("retained relation does not bind the editorial query request")
        return cls(
            ordinal,
            story_id,
            beat_id,
            requirement_id,
            alternative_id,
            candidate_id,
            query_hash,
            first.feasible_count,
            first.feasible_count - len(results),
            first.request_sha256,
            first.policy_sha256,
            first.candidate_domain_sha256,
            first.feasible_relation_sha256,
            variants,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "story_id": self.story_id,
            "beat_id": self.beat_id,
            "requirement_id": self.requirement_id,
            "alternative_id": self.alternative_id,
            "candidate_id": self.candidate_id,
            "exact_span_query_sha256": self.exact_span_query_sha256,
            "feasible_count": self.feasible_count,
            "omitted_count": self.omitted_count,
            "request_sha256": self.request_sha256,
            "policy_sha256": self.policy_sha256,
            "candidate_domain_sha256": self.candidate_domain_sha256,
            "feasible_relation_sha256": self.feasible_relation_sha256,
            "variants": [item.to_mapping() for item in self.variants],
        }


@dataclass(frozen=True, slots=True)
class SpanVariantSet:
    policy: SpanVariantSetPolicy
    parent_request_sha256: str
    parent_artifact_set_sha256: str
    entries: tuple[SpanVariantEntry, ...]

    def __post_init__(self) -> None:
        if type(self.policy) is not SpanVariantSetPolicy:  # noqa: E721
            raise SpanVariantSetError("variant set requires an exact policy")
        _hash(self.parent_request_sha256, "parent_request_sha256")
        _hash(self.parent_artifact_set_sha256, "parent_artifact_set_sha256")
        if (
            type(self.entries) is not tuple  # noqa: E721
            or not self.entries
            or any(type(item) is not SpanVariantEntry for item in self.entries)  # noqa: E721
        ):
            raise SpanVariantSetError("variant set entries must be a non-empty exact tuple")
        if tuple(item.ordinal for item in self.entries) != tuple(range(len(self.entries))):
            raise SpanVariantSetError("entry ordinals must be contiguous from zero")
        identities = tuple(
            (item.story_id, item.beat_id, item.requirement_id, item.alternative_id, item.candidate_id)
            for item in self.entries
        )
        if len(set(identities)) != len(identities):
            raise SpanVariantSetError("variant set contains duplicate entry identities")
        if any(len(item.variants) > self.policy.max_variants for item in self.entries):
            raise SpanVariantSetError("entry retained more variants than policy permits")
        if any(
            len(item.variants) != min(item.feasible_count, self.policy.max_variants)
            for item in self.entries
        ):
            raise SpanVariantSetError("entry does not retain the complete canonical policy prefix")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": SPAN_VARIANT_SET_SCHEMA_VERSION,
            "policy": self.policy.to_mapping(),
            "parent_request_sha256": self.parent_request_sha256,
            "parent_artifact_set_sha256": self.parent_artifact_set_sha256,
            "entries": [item.to_mapping() for item in self.entries],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def _decode_policy(value: object) -> SpanVariantSetPolicy:
    raw = _object(value, ("schema_version", "max_variants"), "variant set policy")
    if raw["schema_version"] != SPAN_VARIANT_SET_POLICY_SCHEMA_VERSION:
        raise SpanVariantSetError("variant set policy schema is unsupported")
    return SpanVariantSetPolicy(_integer(raw["max_variants"], "max_variants", minimum=1))


def _decode_variant(value: object) -> SpanVariant:
    raw = _object(
        value,
        ("ordinal", "variant_id", "exact_span_query_sha256", "exact_span_result"),
        "span variant",
    )
    return SpanVariant(
        _integer(raw["ordinal"], "variant.ordinal"),
        _hash(raw["variant_id"], "variant.variant_id"),
        _hash(raw["exact_span_query_sha256"], "variant.exact_span_query_sha256"),
        _exact_result(raw["exact_span_result"]),
    )


def _decode_entry(value: object) -> SpanVariantEntry:
    fields = (
        "ordinal",
        "story_id",
        "beat_id",
        "requirement_id",
        "alternative_id",
        "candidate_id",
        "exact_span_query_sha256",
        "feasible_count",
        "omitted_count",
        "request_sha256",
        "policy_sha256",
        "candidate_domain_sha256",
        "feasible_relation_sha256",
        "variants",
    )
    raw = _object(value, fields, "span variant entry")
    return SpanVariantEntry(
        _integer(raw["ordinal"], "entry.ordinal"),
        _text(raw["story_id"], "entry.story_id"),
        _text(raw["beat_id"], "entry.beat_id"),
        _text(raw["requirement_id"], "entry.requirement_id"),
        _text(raw["alternative_id"], "entry.alternative_id"),
        _text(raw["candidate_id"], "entry.candidate_id"),
        _hash(raw["exact_span_query_sha256"], "entry.exact_span_query_sha256"),
        _integer(raw["feasible_count"], "entry.feasible_count", minimum=1),
        _integer(raw["omitted_count"], "entry.omitted_count"),
        _hash(raw["request_sha256"], "entry.request_sha256"),
        _hash(raw["policy_sha256"], "entry.policy_sha256"),
        _hash(raw["candidate_domain_sha256"], "entry.candidate_domain_sha256"),
        _hash(raw["feasible_relation_sha256"], "entry.feasible_relation_sha256"),
        tuple(_decode_variant(item) for item in _array(raw["variants"], "variants")),
    )


def decode_span_variant_set(value: object) -> SpanVariantSet:
    raw = _object(
        value,
        (
            "schema_version",
            "policy",
            "parent_request_sha256",
            "parent_artifact_set_sha256",
            "entries",
        ),
        "span variant set",
    )
    if raw["schema_version"] != SPAN_VARIANT_SET_SCHEMA_VERSION:
        raise SpanVariantSetError("variant set schema is unsupported")
    return SpanVariantSet(
        _decode_policy(raw["policy"]),
        _hash(raw["parent_request_sha256"], "parent_request_sha256"),
        _hash(raw["parent_artifact_set_sha256"], "parent_artifact_set_sha256"),
        tuple(_decode_entry(item) for item in _array(raw["entries"], "entries")),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SpanVariantSetError("duplicate JSON key")
        result[key] = value
    return result


def _reject_number(value: str) -> object:
    raise SpanVariantSetError(f"unsupported JSON number {value}")


def decode_span_variant_set_json(raw: bytes, *, max_bytes: int) -> SpanVariantSet:
    if type(raw) is not bytes:  # noqa: E721
        raise SpanVariantSetError("variant set payload must be exact bytes")
    limit = _integer(max_bytes, "max_bytes", minimum=1)
    if not raw or len(raw) > limit:
        raise SpanVariantSetError("variant set payload exceeds its explicit byte bound")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except SpanVariantSetError:
        raise
    except (ValueError, UnicodeError, RecursionError) as error:
        raise SpanVariantSetError("variant set must be bounded strict UTF-8 JSON") from error
    return decode_span_variant_set(value)


def encode_span_variant_set_json(value: SpanVariantSet) -> bytes:
    if type(value) is not SpanVariantSet:  # noqa: E721
        raise SpanVariantSetError("only an exact SpanVariantSet can be encoded")
    try:
        return json.dumps(
            value.to_mapping(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise SpanVariantSetError("variant set is not canonical finite JSON") from error


__all__ = (
    "SPAN_VARIANT_SET_POLICY_SCHEMA_VERSION",
    "SPAN_VARIANT_SET_SCHEMA_VERSION",
    "SpanVariant",
    "SpanVariantEntry",
    "SpanVariantSet",
    "SpanVariantSetError",
    "SpanVariantSetPolicy",
    "decode_span_variant_set",
    "decode_span_variant_set_json",
    "encode_span_variant_set_json",
)
