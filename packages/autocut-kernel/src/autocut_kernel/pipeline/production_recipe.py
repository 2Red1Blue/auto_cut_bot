"""Closed immutable Stage 4 production Recipe values.

These records preserve the exact editorial query, candidate-local A/V result,
and both source clocks.  They do not read Store state, perform rendering, or
grant Admission: constructing or decoding a Recipe only proves that its value
is internally closed and hash-consistent.

``candidate-dialogue-guard-v2`` is the first guard grammar accepted by this
durable production Recipe model.  No production Recipe using the v1 guard was
persisted, so this boundary has no v1 migration obligation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal, Mapping, cast
from uuid import UUID

from ..media.types import (
    MediaValidationError,
    TickRange,
    TimeBase,
    canonical_sha256,
    require_pts,
    sha256_prefixed,
)
from ..physical_edit.candidate_dialogue_guard import CandidateDialogueGuard
from ..physical_edit.candidate_exact_span import CandidateExactSpanResult
from ..physical_edit.candidate_timed_speech_authority import CandidateTimedSpeechAuthorityKind
from ..physical_edit.dialogue_guard import (
    DialogueGuardKind,
    DialogueRequirement,
    ProtectedAudioRange,
)
from ..physical_edit.editorial_exact_span import EditorialExactSpanQuery
from ..physical_edit.exact_span import BoundaryProof, ExactAvSpanRequest, VideoClockRange
from ..store.models import BlobRef, CommittedArtifactMemberReference, StoreValidationError
from ..vlm.models import VlmEditingMode

PRODUCTION_RECIPE_SCHEMA_VERSION: Final = "stage4-production-recipe-v1"
PRODUCTION_RECIPE_PRODUCER_ID: Final = "stage4-exact-av-compiler-v1"
_DECIMAL_COUNT: Final = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_SPAN_INTENTS: Final = frozenset(("tight", "scene", "context"))


class ProductionRecipeError(ValueError):
    """A production Recipe value is malformed or internally inconsistent."""


def _object(value: object, fields: tuple[str, ...], label: str) -> Mapping[str, object]:
    if type(value) is not dict:  # noqa: E721 - the JSON boundary is deliberately exact.
        raise ProductionRecipeError(f"{label} must be a closed object")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw) or set(raw) != set(fields):  # noqa: E721
        raise ProductionRecipeError(f"{label} has missing or unknown fields")
    return cast(Mapping[str, object], raw)


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise ProductionRecipeError(f"{label} must be an array")
    return cast(list[object], value)


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise ProductionRecipeError(f"{label} must be non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ProductionRecipeError(f"{label} must be valid UTF-8") from error
    return value


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    try:
        result = require_pts(value, label)
    except MediaValidationError as error:
        raise ProductionRecipeError(str(error)) from error
    if minimum is not None and result < minimum:
        raise ProductionRecipeError(f"{label} must be >= {minimum}")
    return result


def _sha256(value: object, label: str) -> str:
    try:
        result = sha256_prefixed(value, label)
    except MediaValidationError as error:
        raise ProductionRecipeError(str(error)) from error
    if result == "sha256:" + "0" * 64:
        raise ProductionRecipeError(f"{label} must not be the all-zero digest")
    return result


def _count(value: object, label: str) -> str:
    result = _text(value, label)
    if _DECIMAL_COUNT.fullmatch(result) is None:
        raise ProductionRecipeError(f"{label} must be a canonical decimal count")
    return result


def _time_base(value: object, label: str) -> TimeBase:
    raw = _object(value, ("numerator", "denominator"), label)
    try:
        return TimeBase(
            _integer(raw["numerator"], f"{label}.numerator", minimum=1),
            _integer(raw["denominator"], f"{label}.denominator", minimum=1),
        )
    except MediaValidationError as error:
        raise ProductionRecipeError(str(error)) from error


def _tick_range(value: object, label: str) -> TickRange:
    raw = _object(value, ("start_pts", "end_pts"), label)
    try:
        return TickRange(
            _integer(raw["start_pts"], f"{label}.start_pts"),
            _integer(raw["end_pts"], f"{label}.end_pts"),
        )
    except MediaValidationError as error:
        raise ProductionRecipeError(str(error)) from error


def _video_clock_range(value: object, label: str) -> VideoClockRange:
    raw = _object(
        value,
        ("source_id", "source_sha256", "clock_id", "time_base", "tick_range"),
        label,
    )
    try:
        return VideoClockRange(
            _text(raw["source_id"], f"{label}.source_id"),
            _sha256(raw["source_sha256"], f"{label}.source_sha256"),
            _text(raw["clock_id"], f"{label}.clock_id"),
            _time_base(raw["time_base"], f"{label}.time_base"),
            _tick_range(raw["tick_range"], f"{label}.tick_range"),
        )
    except ValueError as error:
        raise ProductionRecipeError(str(error)) from error


def _exact_request(value: object) -> ExactAvSpanRequest:
    raw = _object(
        value,
        (
            "desired_video_range",
            "anchor_video_range",
            "minimum_video_duration_tick",
            "dialogue_requirement",
        ),
        "exact_span_query.request",
    )
    try:
        requirement = DialogueRequirement(
            _text(raw["dialogue_requirement"], "exact_span_query.request.dialogue_requirement")
        )
        return ExactAvSpanRequest(
            _video_clock_range(raw["desired_video_range"], "desired_video_range"),
            _video_clock_range(raw["anchor_video_range"], "anchor_video_range"),
            _integer(
                raw["minimum_video_duration_tick"],
                "minimum_video_duration_tick",
                minimum=1,
            ),
            requirement,
        )
    except ValueError as error:
        raise ProductionRecipeError(str(error)) from error


def _editorial_query(value: object) -> EditorialExactSpanQuery:
    fields = (
        "strategy_version",
        "story_id",
        "beat_id",
        "evidence_requirement_id",
        "alternative_id",
        "candidate_id",
        "anchor_event_id",
        "anchor_event_sha256",
        "span_intent",
        "dominant_editing_mode",
        "policy_sha256",
        "blueprint_beat_sha256",
        "evidence_requirement_sha256",
        "alternative_sha256",
        "catalog_candidate_sha256",
        "semantic_pack_sha256",
        "timed_evidence_sha256",
        "dialogue_protection_kind",
        "request",
    )
    raw = _object(value, fields, "exact_span_query")
    if raw["strategy_version"] != "editorial-exact-span-v1":
        raise ProductionRecipeError("exact_span_query strategy is unsupported")
    span_intent = _text(raw["span_intent"], "exact_span_query.span_intent")
    if span_intent not in _SPAN_INTENTS:
        raise ProductionRecipeError("exact_span_query span intent is unsupported")
    try:
        return EditorialExactSpanQuery(
            story_id=_text(raw["story_id"], "exact_span_query.story_id"),
            beat_id=_text(raw["beat_id"], "exact_span_query.beat_id"),
            evidence_requirement_id=_text(
                raw["evidence_requirement_id"], "exact_span_query.evidence_requirement_id"
            ),
            alternative_id=_text(raw["alternative_id"], "exact_span_query.alternative_id"),
            candidate_id=_text(raw["candidate_id"], "exact_span_query.candidate_id"),
            anchor_event_id=_text(raw["anchor_event_id"], "exact_span_query.anchor_event_id"),
            anchor_event_sha256=_sha256(
                raw["anchor_event_sha256"], "exact_span_query.anchor_event_sha256"
            ),
            span_intent=span_intent,
            dominant_editing_mode=VlmEditingMode(
                _text(raw["dominant_editing_mode"], "exact_span_query.dominant_editing_mode")
            ),
            policy_sha256=_sha256(raw["policy_sha256"], "exact_span_query.policy_sha256"),
            blueprint_beat_sha256=_sha256(
                raw["blueprint_beat_sha256"], "exact_span_query.blueprint_beat_sha256"
            ),
            evidence_requirement_sha256=_sha256(
                raw["evidence_requirement_sha256"],
                "exact_span_query.evidence_requirement_sha256",
            ),
            alternative_sha256=_sha256(
                raw["alternative_sha256"], "exact_span_query.alternative_sha256"
            ),
            catalog_candidate_sha256=_sha256(
                raw["catalog_candidate_sha256"], "exact_span_query.catalog_candidate_sha256"
            ),
            semantic_pack_sha256=_sha256(
                raw["semantic_pack_sha256"], "exact_span_query.semantic_pack_sha256"
            ),
            timed_evidence_sha256=_sha256(
                raw["timed_evidence_sha256"], "exact_span_query.timed_evidence_sha256"
            ),
            dialogue_protection_kind=cast(
                Literal["known_speech", "complete_dialogue"],
                _text(raw["dialogue_protection_kind"], "exact_span_query.dialogue_protection_kind"),
            ),
            request=_exact_request(raw["request"]),
        )
    except ValueError as error:
        raise ProductionRecipeError(str(error)) from error


def _protected_range(value: object, index: int) -> ProtectedAudioRange:
    label = f"dialogue_guard.protected_ranges[{index}]"
    raw = _object(
        value,
        ("source_id", "source_sha256", "clock_id", "time_base", "in_tick", "out_tick"),
        label,
    )
    try:
        return ProtectedAudioRange(
            _text(raw["source_id"], f"{label}.source_id"),
            _sha256(raw["source_sha256"], f"{label}.source_sha256"),
            _text(raw["clock_id"], f"{label}.clock_id"),
            _time_base(raw["time_base"], f"{label}.time_base"),
            _integer(raw["in_tick"], f"{label}.in_tick"),
            _integer(raw["out_tick"], f"{label}.out_tick"),
        )
    except ValueError as error:
        raise ProductionRecipeError(str(error)) from error


def _candidate_guard(value: object) -> CandidateDialogueGuard:
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
        raise ProductionRecipeError("dialogue guard schema is unsupported")
    if raw["source_audio_range"] is None:
        raise ProductionRecipeError("production A/V dialogue guard cannot contain null")
    try:
        requirement = DialogueRequirement(_text(raw["requirement"], "dialogue_guard.requirement"))
        kind = DialogueGuardKind(_text(raw["kind"], "dialogue_guard.kind"))
        original_kind = CandidateTimedSpeechAuthorityKind(
            _text(raw["original_authority_kind"], "dialogue_guard.original_authority_kind")
        )
        if kind is DialogueGuardKind.NOT_APPLICABLE:
            raise ProductionRecipeError("production A/V dialogue guard requires audio")
        protected = _array(raw["protected_ranges"], "dialogue_guard.protected_ranges")
        return CandidateDialogueGuard(
            root_evidence_sha256=_sha256(
                raw["root_evidence_sha256"], "dialogue_guard.root_evidence_sha256"
            ),
            candidate_evidence_sha256=_sha256(
                raw["candidate_evidence_sha256"], "dialogue_guard.candidate_evidence_sha256"
            ),
            candidate_window_sha256=_sha256(
                raw["candidate_window_sha256"], "dialogue_guard.candidate_window_sha256"
            ),
            window_plan_sha256=_sha256(
                raw["window_plan_sha256"], "dialogue_guard.window_plan_sha256"
            ),
            timed_speech_authority_sha256=_sha256(
                raw["timed_speech_authority_sha256"],
                "dialogue_guard.timed_speech_authority_sha256",
            ),
            original_authority_kind=original_kind,
            original_authority_sha256=_sha256(
                raw["original_authority_sha256"],
                "dialogue_guard.original_authority_sha256",
            ),
            guard_policy_sha256=_sha256(
                raw["guard_policy_sha256"], "dialogue_guard.guard_policy_sha256"
            ),
            source_id=_text(raw["source_id"], "dialogue_guard.source_id"),
            source_sha256=_sha256(raw["source_sha256"], "dialogue_guard.source_sha256"),
            source_audio_clock_id=_text(
                raw["source_audio_clock_id"], "dialogue_guard.source_audio_clock_id"
            ),
            source_audio_time_base=_time_base(
                raw["source_audio_time_base"], "dialogue_guard.source_audio_time_base"
            ),
            source_audio_range=_tick_range(
                raw["source_audio_range"], "dialogue_guard.source_audio_range"
            ),
            requirement=requirement,
            kind=kind,
            reason=_text(raw["reason"], "dialogue_guard.reason"),
            protected_ranges=tuple(
                _protected_range(item, index) for index, item in enumerate(protected)
            ),
        )
    except ValueError as error:
        raise ProductionRecipeError(str(error)) from error


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
        _sha256(raw["source_sha256"], "boundary_proof.source_sha256"),
        _text(raw["video_clock_id"], "boundary_proof.video_clock_id"),
        _time_base(raw["video_time_base"], "boundary_proof.video_time_base"),
        _integer(raw["video_in_tick"], "boundary_proof.video_in_tick"),
        _integer(raw["video_out_tick"], "boundary_proof.video_out_tick"),
        _text(raw["audio_clock_id"], "boundary_proof.audio_clock_id"),
        _time_base(raw["audio_time_base"], "boundary_proof.audio_time_base"),
        _integer(raw["audio_in_tick"], "boundary_proof.audio_in_tick"),
        _integer(raw["audio_out_tick"], "boundary_proof.audio_out_tick"),
        _sha256(raw["frame_pts_index_set_sha256"], "boundary_proof.frame_pts_index_set_sha256"),
        _sha256(
            raw["audio_sample_boundary_set_sha256"],
            "boundary_proof.audio_sample_boundary_set_sha256",
        ),
        _sha256(raw["visual_validity_set_sha256"], "boundary_proof.visual_validity_set_sha256"),
        _sha256(raw["subtitle_cue_set_sha256"], "boundary_proof.subtitle_cue_set_sha256"),
        _sha256(
            raw["clock_map_certificate_sha256"],
            "boundary_proof.clock_map_certificate_sha256",
        ),
    )


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
        raise ProductionRecipeError("exact_span_result strategy is unsupported")
    decision_key = _array(raw["canonical_decision_key"], "canonical_decision_key")
    result = CandidateExactSpanResult(
        video_range=_tick_range(raw["video_range"], "exact_span_result.video_range"),
        audio_range=_tick_range(raw["audio_range"], "exact_span_result.audio_range"),
        boundary_proof=_boundary_proof(raw["boundary_proof"]),
        dialogue_guard=_candidate_guard(raw["dialogue_guard"]),
        common_segment_ordinal=_integer(
            raw["common_segment_ordinal"], "common_segment_ordinal", minimum=0
        ),
        canonical_decision_key=tuple(
            _integer(item, f"canonical_decision_key[{index}]")
            for index, item in enumerate(decision_key)
        ),
        logical_cartesian_count_decimal=_count(
            raw["logical_cartesian_count_decimal"], "logical_cartesian_count_decimal"
        ),
        visited_av_pair_count=_integer(
            raw["visited_av_pair_count"], "visited_av_pair_count", minimum=1
        ),
        feasible_count=_integer(raw["feasible_count"], "feasible_count", minimum=1),
        request_sha256=_sha256(raw["request_sha256"], "exact_span_result.request_sha256"),
        policy_sha256=_sha256(raw["policy_sha256"], "exact_span_result.policy_sha256"),
        candidate_domain_sha256=_sha256(
            raw["candidate_domain_sha256"], "exact_span_result.candidate_domain_sha256"
        ),
        feasible_relation_sha256=_sha256(
            raw["feasible_relation_sha256"], "exact_span_result.feasible_relation_sha256"
        ),
    )
    _validate_result_shape(result)
    return result


def _validate_boundary_proof(proof: BoundaryProof) -> None:
    if type(proof) is not BoundaryProof:  # noqa: E721
        raise ProductionRecipeError("exact span requires the shared BoundaryProof value")
    _text(proof.source_id, "boundary_proof.source_id")
    _sha256(proof.source_sha256, "boundary_proof.source_sha256")
    _text(proof.video_clock_id, "boundary_proof.video_clock_id")
    _text(proof.audio_clock_id, "boundary_proof.audio_clock_id")
    if type(proof.video_time_base) is not TimeBase or type(proof.audio_time_base) is not TimeBase:  # noqa: E721
        raise ProductionRecipeError("boundary proof must retain exact A/V TimeBase values")
    try:
        TickRange(proof.video_in_tick, proof.video_out_tick)
        TickRange(proof.audio_in_tick, proof.audio_out_tick)
    except MediaValidationError as error:
        raise ProductionRecipeError(str(error)) from error
    for name in (
        "frame_pts_index_set_sha256",
        "audio_sample_boundary_set_sha256",
        "visual_validity_set_sha256",
        "subtitle_cue_set_sha256",
        "clock_map_certificate_sha256",
    ):
        _sha256(getattr(proof, name), f"boundary_proof.{name}")


def _validate_result_shape(result: CandidateExactSpanResult) -> None:
    if type(result) is not CandidateExactSpanResult:  # noqa: E721
        raise ProductionRecipeError("Beat requires a candidate-local exact A/V result")
    if type(result.video_range) is not TickRange or type(result.audio_range) is not TickRange:  # noqa: E721
        raise ProductionRecipeError("exact result must retain shared TickRange values")
    _validate_boundary_proof(result.boundary_proof)
    if type(result.dialogue_guard) is not CandidateDialogueGuard:  # noqa: E721
        raise ProductionRecipeError("exact result must retain a candidate dialogue guard")
    if result.dialogue_guard.source_audio_range is None:
        raise ProductionRecipeError("production A/V Recipe requires an audio-bearing guard")
    _integer(result.common_segment_ordinal, "common_segment_ordinal", minimum=0)
    if type(result.canonical_decision_key) is not tuple or len(result.canonical_decision_key) != 10:  # noqa: E721
        raise ProductionRecipeError("canonical decision key must retain all ten exact positions")
    for index, value in enumerate(result.canonical_decision_key):
        _integer(value, f"canonical_decision_key[{index}]")
    expected_endpoints = (
        result.video_range.start_pts,
        result.video_range.end_pts,
        result.audio_range.start_pts,
        result.audio_range.end_pts,
    )
    if result.canonical_decision_key[-4:] != expected_endpoints:
        raise ProductionRecipeError("canonical decision key endpoints differ from the exact result")
    logical_count = int(_count(result.logical_cartesian_count_decimal, "logical_cartesian_count_decimal"))
    visits = _integer(result.visited_av_pair_count, "visited_av_pair_count", minimum=1)
    feasible = _integer(result.feasible_count, "feasible_count", minimum=1)
    if feasible > visits or feasible > logical_count:
        raise ProductionRecipeError("exact result counts are inconsistent")
    for name in (
        "request_sha256",
        "policy_sha256",
        "candidate_domain_sha256",
        "feasible_relation_sha256",
    ):
        _sha256(getattr(result, name), f"exact_span_result.{name}")


@dataclass(frozen=True, slots=True)
class ProductionSpan:
    """One executable requirement/candidate selection within a Blueprint Beat."""

    ordinal: int
    requirement_id: str
    alternative_id: str
    candidate_id: str
    catalog_candidate_sha256: str
    source_blob: BlobRef
    source_manifest_ref: CommittedArtifactMemberReference
    exact_span_query: EditorialExactSpanQuery
    exact_span_query_sha256: str
    exact_span_result: CandidateExactSpanResult
    exact_span_result_sha256: str
    exact_span_proof_sha256: str
    av_pairing_proof_sha256: str

    def __post_init__(self) -> None:
        _integer(self.ordinal, "span.ordinal", minimum=0)
        _text(self.requirement_id, "span.requirement_id")
        _text(self.alternative_id, "span.alternative_id")
        _text(self.candidate_id, "span.candidate_id")
        for name in (
            "catalog_candidate_sha256",
            "exact_span_query_sha256",
            "exact_span_result_sha256",
            "exact_span_proof_sha256",
            "av_pairing_proof_sha256",
        ):
            _sha256(getattr(self, name), f"span.{name}")
        if type(self.source_blob) is not BlobRef:  # noqa: E721
            raise ProductionRecipeError("production span requires an exact immutable source BlobRef")
        if self.source_blob.object_id.int == 0 or self.source_blob.byte_length <= 0:
            raise ProductionRecipeError("production source BlobRef must not be empty")
        _sha256(self.source_blob.content_hash, "span.source_blob.content_hash")
        if "fixture" in self.source_blob.media_type.casefold():
            raise ProductionRecipeError("fixture source media is forbidden in production Recipes")
        if type(self.source_manifest_ref) is not CommittedArtifactMemberReference:  # noqa: E721
            raise ProductionRecipeError("production span requires a full committed SourceManifest ref")
        source_ref = self.source_manifest_ref
        if (
            source_ref.receipt_id.int == 0
            or source_ref.artifact_set_id.int == 0
            or source_ref.artifact_type != "whole_series_source_manifest"
            or source_ref.logical_id != "whole_series_source_manifest"
            or source_ref.scope.namespace != "pipeline"
            or source_ref.scope.kind != "job"
            or not source_ref.scope.key
        ):
            raise ProductionRecipeError("source manifest reference has a non-production owner")
        _sha256(source_ref.content_hash, "span.source_manifest_ref.content_hash")
        if type(self.exact_span_query) is not EditorialExactSpanQuery:  # noqa: E721
            raise ProductionRecipeError("span requires an EditorialExactSpanQuery")
        _validate_result_shape(self.exact_span_result)
        query = self.exact_span_query
        result = self.exact_span_result
        proof = result.boundary_proof
        guard = result.dialogue_guard
        video = query.request.desired_video_range
        if (
            self.requirement_id != query.evidence_requirement_id
            or self.alternative_id != query.alternative_id
            or self.candidate_id != query.candidate_id
            or self.catalog_candidate_sha256 != query.catalog_candidate_sha256
        ):
            raise ProductionRecipeError("span identities differ from the exact editorial query")
        if self.exact_span_query_sha256 != query.canonical_hash:
            raise ProductionRecipeError("exact span query content hash is stale or forged")
        if self.exact_span_result_sha256 != result.canonical_hash:
            raise ProductionRecipeError("exact span result content hash is stale or forged")
        if self.exact_span_proof_sha256 != proof.canonical_hash:
            raise ProductionRecipeError("exact span proof content hash is stale or forged")
        if self.av_pairing_proof_sha256 != proof.clock_map_certificate_sha256:
            raise ProductionRecipeError("A/V pairing proof reference differs from BoundaryProof")
        if self.source_blob.content_hash != proof.source_sha256:
            raise ProductionRecipeError("render source BlobRef differs from the selected Source bytes")
        if result.request_sha256 != query.request.canonical_hash:
            raise ProductionRecipeError("exact result does not bind the embedded query request")
        if (
            guard.candidate_evidence_sha256 != query.timed_evidence_sha256
            or guard.requirement is not query.request.dialogue_requirement
        ):
            raise ProductionRecipeError("exact result speech proof differs from the editorial query")
        if (
            proof.source_id,
            proof.source_sha256,
            proof.video_clock_id,
            proof.video_time_base,
        ) != (video.source_id, video.source_sha256, video.clock_id, video.time_base):
            raise ProductionRecipeError("query/result video Source clock mismatch")
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
            raise ProductionRecipeError("result/proof audio Source clock mismatch")
        if (
            proof.video_in_tick,
            proof.video_out_tick,
            proof.audio_in_tick,
            proof.audio_out_tick,
        ) != (
            result.video_range.start_pts,
            result.video_range.end_pts,
            result.audio_range.start_pts,
            result.audio_range.end_pts,
        ):
            raise ProductionRecipeError("BoundaryProof endpoints differ from the exact A/V result")
        if not video.tick_range.contains(result.video_range):
            raise ProductionRecipeError("selected video endpoints escape the exact query domain")
        audio_coverage = guard.source_audio_range
        if audio_coverage is None or not audio_coverage.contains(result.audio_range):
            raise ProductionRecipeError("selected audio endpoints escape proven audio coverage")

    @classmethod
    def from_exact_span(
        cls,
        *,
        ordinal: int,
        source_blob: BlobRef,
        source_manifest_ref: CommittedArtifactMemberReference,
        query: EditorialExactSpanQuery,
        result: CandidateExactSpanResult,
    ) -> ProductionSpan:
        """Close one exact span without treating it as physical Admission."""
        if type(query) is not EditorialExactSpanQuery:  # noqa: E721
            raise ProductionRecipeError("query must be an EditorialExactSpanQuery")
        if type(result) is not CandidateExactSpanResult:  # noqa: E721
            raise ProductionRecipeError("result must be a CandidateExactSpanResult")
        proof = result.boundary_proof
        return cls(
            ordinal,
            query.evidence_requirement_id,
            query.alternative_id,
            query.candidate_id,
            query.catalog_candidate_sha256,
            source_blob,
            source_manifest_ref,
            query,
            query.canonical_hash,
            result,
            result.canonical_hash,
            proof.canonical_hash,
            proof.clock_map_certificate_sha256,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "requirement_id": self.requirement_id,
            "alternative_id": self.alternative_id,
            "candidate_id": self.candidate_id,
            "catalog_candidate_sha256": self.catalog_candidate_sha256,
            "source_blob": {
                "object_id": str(self.source_blob.object_id),
                "content_hash": self.source_blob.content_hash,
                "byte_length": self.source_blob.byte_length,
                "media_type": self.source_blob.media_type,
            },
            "source_manifest_ref": self.source_manifest_ref.to_mapping(),
            "exact_span_query": self.exact_span_query.to_mapping(),
            "exact_span_query_sha256": self.exact_span_query_sha256,
            "exact_span_result": self.exact_span_result.to_mapping(),
            "exact_span_result_sha256": self.exact_span_result_sha256,
            "exact_span_proof_sha256": self.exact_span_proof_sha256,
            "av_pairing_proof_sha256": self.av_pairing_proof_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> ProductionSpan:
        raw = _object(
            value,
            (
                "ordinal",
                "requirement_id",
                "alternative_id",
                "candidate_id",
                "catalog_candidate_sha256",
                "source_blob",
                "source_manifest_ref",
                "exact_span_query",
                "exact_span_query_sha256",
                "exact_span_result",
                "exact_span_result_sha256",
                "exact_span_proof_sha256",
                "av_pairing_proof_sha256",
            ),
            "production span",
        )
        return cls(
            _integer(raw["ordinal"], "span.ordinal", minimum=0),
            _text(raw["requirement_id"], "span.requirement_id"),
            _text(raw["alternative_id"], "span.alternative_id"),
            _text(raw["candidate_id"], "span.candidate_id"),
            _sha256(raw["catalog_candidate_sha256"], "span.catalog_candidate_sha256"),
            _blob_ref(raw["source_blob"]),
            _source_manifest_ref(raw["source_manifest_ref"]),
            _editorial_query(raw["exact_span_query"]),
            _sha256(raw["exact_span_query_sha256"], "span.exact_span_query_sha256"),
            _exact_result(raw["exact_span_result"]),
            _sha256(raw["exact_span_result_sha256"], "span.exact_span_result_sha256"),
            _sha256(raw["exact_span_proof_sha256"], "span.exact_span_proof_sha256"),
            _sha256(raw["av_pairing_proof_sha256"], "span.av_pairing_proof_sha256"),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def _blob_ref(value: object) -> BlobRef:
    raw = _object(
        value,
        ("object_id", "content_hash", "byte_length", "media_type"),
        "source_blob",
    )
    try:
        encoded_object_id = _text(raw["object_id"], "source_blob.object_id")
        object_id = UUID(encoded_object_id)
        if str(object_id) != encoded_object_id:
            raise ProductionRecipeError("source_blob.object_id must use canonical UUID text")
        return BlobRef(
            object_id,
            _sha256(raw["content_hash"], "source_blob.content_hash"),
            _integer(raw["byte_length"], "source_blob.byte_length", minimum=1),
            _text(raw["media_type"], "source_blob.media_type"),
        )
    except (StoreValidationError, ValueError) as error:
        raise ProductionRecipeError(str(error)) from error


def _source_manifest_ref(value: object) -> CommittedArtifactMemberReference:
    try:
        result = CommittedArtifactMemberReference.from_mapping(value)
    except (StoreValidationError, TypeError, ValueError) as error:
        raise ProductionRecipeError(str(error)) from error
    if result.to_mapping() != value:
        raise ProductionRecipeError("source manifest reference must preserve canonical wire values")
    return result


@dataclass(frozen=True, slots=True)
class ProductionBeat:
    """One unique Blueprint Beat containing every selected requirement span."""

    ordinal: int
    beat_id: str
    blueprint_beat_sha256: str
    spans: tuple[ProductionSpan, ...]

    def __post_init__(self) -> None:
        _integer(self.ordinal, "beat.ordinal", minimum=0)
        _text(self.beat_id, "beat.beat_id")
        _sha256(self.blueprint_beat_sha256, "beat.blueprint_beat_sha256")
        if type(self.spans) is not tuple or not self.spans:  # noqa: E721
            raise ProductionRecipeError("production Beat must contain at least one span")
        if any(type(item) is not ProductionSpan for item in self.spans):  # noqa: E721
            raise ProductionRecipeError("production Beat contains a non-span value")
        if tuple(item.ordinal for item in self.spans) != tuple(range(len(self.spans))):
            raise ProductionRecipeError("span ordinals must be complete, unique, and ordered")
        if any(
            item.exact_span_query.beat_id != self.beat_id
            or item.exact_span_query.blueprint_beat_sha256 != self.blueprint_beat_sha256
            for item in self.spans
        ):
            raise ProductionRecipeError("production span belongs to another Blueprint Beat")
        keys = tuple(
            (item.requirement_id, item.alternative_id, item.candidate_id) for item in self.spans
        )
        if len(set(keys)) != len(keys):
            raise ProductionRecipeError("production Beat repeats a requirement/candidate span")

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "beat_id": self.beat_id,
            "blueprint_beat_sha256": self.blueprint_beat_sha256,
            "spans": [item.to_mapping() for item in self.spans],
        }

    @classmethod
    def from_mapping(cls, value: object) -> ProductionBeat:
        raw = _object(
            value,
            ("ordinal", "beat_id", "blueprint_beat_sha256", "spans"),
            "production beat",
        )
        return cls(
            _integer(raw["ordinal"], "beat.ordinal", minimum=0),
            _text(raw["beat_id"], "beat.beat_id"),
            _sha256(raw["blueprint_beat_sha256"], "beat.blueprint_beat_sha256"),
            tuple(
                ProductionSpan.from_mapping(item)
                for item in _array(raw["spans"], "beat.spans")
            ),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ProductionStory:
    """One ordered, non-empty Story in a production Recipe."""

    ordinal: int
    story_id: str
    blueprint_story_sha256: str
    beats: tuple[ProductionBeat, ...]

    def __post_init__(self) -> None:
        _integer(self.ordinal, "story.ordinal", minimum=0)
        _text(self.story_id, "story.story_id")
        _sha256(self.blueprint_story_sha256, "story.blueprint_story_sha256")
        if type(self.beats) is not tuple or not self.beats:  # noqa: E721
            raise ProductionRecipeError("production Story must contain at least one Beat")
        if any(type(item) is not ProductionBeat for item in self.beats):  # noqa: E721
            raise ProductionRecipeError("production Story contains a non-Beat value")
        if tuple(item.ordinal for item in self.beats) != tuple(range(len(self.beats))):
            raise ProductionRecipeError("Beat ordinals must be complete, unique, and ordered")
        if len({item.beat_id for item in self.beats}) != len(self.beats):
            raise ProductionRecipeError("production Story repeats a Beat identity")
        if any(
            span.exact_span_query.story_id != self.story_id
            for beat in self.beats
            for span in beat.spans
        ):
            raise ProductionRecipeError("production Beat belongs to another Story")

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "story_id": self.story_id,
            "blueprint_story_sha256": self.blueprint_story_sha256,
            "beats": [item.to_mapping() for item in self.beats],
        }

    @classmethod
    def from_mapping(cls, value: object) -> ProductionStory:
        raw = _object(
            value,
            ("ordinal", "story_id", "blueprint_story_sha256", "beats"),
            "production story",
        )
        return cls(
            _integer(raw["ordinal"], "story.ordinal", minimum=0),
            _text(raw["story_id"], "story.story_id"),
            _sha256(raw["blueprint_story_sha256"], "story.blueprint_story_sha256"),
            tuple(ProductionBeat.from_mapping(item) for item in _array(raw["beats"], "story.beats")),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ProductionRecipe:
    """One Story's production A/V Recipe value, never an Admission decision."""

    producer_id: str
    profile_id: str
    profile_sha256: str
    story: ProductionStory

    def __post_init__(self) -> None:
        if self.producer_id != PRODUCTION_RECIPE_PRODUCER_ID:
            raise ProductionRecipeError("production Recipe has an unsupported producer")
        profile = _text(self.profile_id, "recipe.profile_id")
        if "fixture" in profile.casefold():
            raise ProductionRecipeError("fixture profiles are forbidden in production Recipes")
        _sha256(self.profile_sha256, "recipe.profile_sha256")
        if type(self.story) is not ProductionStory:  # noqa: E721
            raise ProductionRecipeError("production Recipe requires exactly one Story")
        if self.story.ordinal != 0:
            raise ProductionRecipeError("the production Recipe Story ordinal must be zero")
        beats = self.story.beats
        spans = tuple(span for beat in beats for span in beat.spans)
        scopes = {span.source_manifest_ref.scope for span in spans}
        if len(scopes) != 1:
            raise ProductionRecipeError("production Recipe source manifests must share one Job scope")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": PRODUCTION_RECIPE_SCHEMA_VERSION,
            "producer_id": self.producer_id,
            "profile_id": self.profile_id,
            "profile_sha256": self.profile_sha256,
            "story": self.story.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> ProductionRecipe:
        raw = _object(
            value,
            ("schema_version", "producer_id", "profile_id", "profile_sha256", "story"),
            "production recipe",
        )
        if raw["schema_version"] != PRODUCTION_RECIPE_SCHEMA_VERSION:
            raise ProductionRecipeError("production Recipe schema version is unsupported")
        return cls(
            _text(raw["producer_id"], "recipe.producer_id"),
            _text(raw["profile_id"], "recipe.profile_id"),
            _sha256(raw["profile_sha256"], "recipe.profile_sha256"),
            ProductionStory.from_mapping(raw["story"]),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


__all__ = (
    "PRODUCTION_RECIPE_PRODUCER_ID",
    "PRODUCTION_RECIPE_SCHEMA_VERSION",
    "ProductionBeat",
    "ProductionRecipe",
    "ProductionRecipeError",
    "ProductionSpan",
    "ProductionStory",
)
