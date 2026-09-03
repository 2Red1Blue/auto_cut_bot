"""Closed Stage 4 compilation evidence and independent physical admission.

The report records what the compiler attempted and selected.  It is not an
authority decision.  Admission is built only by comparing the report and the
prospective Recipe subjects with independently recomputed, hash-bound replay
censuses.  No Store access, renderer action, latest lookup, or default policy
exists in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Literal, Mapping, cast
from uuid import UUID

from ..media.types import MediaValidationError, canonical_sha256, require_pts, sha256_prefixed
from ..physical_edit.candidate_exact_span import CandidateExactSpanResult
from ..physical_edit.candidate_timed_speech_authority import CandidateTimedSpeechAuthorityKind
from ..physical_edit.editorial_exact_span import (
    EDITORIAL_EXACT_SPAN_STRATEGY,
    EditorialExactSpanQuery,
)
from ..store.models import (
    ArtifactScope,
    BlobRef,
    CommittedArtifactMemberReference,
    StoreValidationError,
)
from .production_recipe import (
    ProductionRecipeError,
    ProductionSpan,
)

PHYSICAL_EDIT_COMPILATION_REPORT_SCHEMA_VERSION: Final = "physical-edit-compilation-report-v1"
PHYSICAL_EDIT_COMPILATION_STRATEGY_VERSION: Final = "stage4-production-compilation-v1"
PHYSICAL_EDIT_EXACT_SPAN_STRATEGY_VERSION: Final = "candidate-local-exact-v1"
PHYSICAL_EDIT_ADMISSION_SCHEMA_VERSION: Final = "physical-edit-admission-v1"
PHYSICAL_EDIT_ADMISSION_STRATEGY_VERSION: Final = "stage4-independent-admission-v1"
PHYSICAL_EDIT_REPLAY_EVALUATOR_STRATEGY_VERSION: Final = "stage4-independent-physical-replay-v1"

PHYSICAL_EDIT_RULE_IDS: Final = (
    "PE-IN-001",
    "PE-CHOICE-001",
    "PE-QUERY-001",
    "PE-REL-001",
    "PE-DLG-001",
    "PE-AV-001",
    "PE-TIME-001",
    "PE-OUT-001",
)

PhysicalEditRuleId = Literal[
    "PE-IN-001",
    "PE-CHOICE-001",
    "PE-QUERY-001",
    "PE-REL-001",
    "PE-DLG-001",
    "PE-AV-001",
    "PE-TIME-001",
    "PE-OUT-001",
]
PhysicalEditBackend = Literal["installed_cpu_profile", "runtime_cuda_capability"]
PhysicalEditCheckStatus = Literal["pass", "fail", "indeterminate"]
PhysicalEditValidationStatus = Literal["valid", "invalid", "indeterminate"]
PhysicalEditNextAction = Literal["render", "stop", "quarantine"]

_SPAN_INTENTS: Final = frozenset(("tight", "scene", "context"))
_ATTEMPT_CODES: Final = {
    "selected": frozenset(("STAGE4_SPAN_SELECTED",)),
    "no_legal_span": frozenset(("STAGE4_NO_LEGAL_SPAN",)),
    "indeterminate": frozenset(
        (
            "STAGE4_PHYSICAL_EVIDENCE_INDETERMINATE",
            "STAGE4_DIALOGUE_EVIDENCE_INDETERMINATE",
            "STAGE4_COMPILATION_BLOCKED",
            "STAGE4_OUTPUT_TIMING_INDETERMINATE",
        )
    ),
}
_STABLE_CODE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_FIXTURE_RECEIPT = UUID("11111111-1111-4111-8111-111111111111")
_FIXTURE_SET = UUID("22222222-2222-4222-8222-222222222222")
_FIXTURE_BLOB = UUID("33333333-3333-4333-8333-333333333333")

_STAGE3_STORY_MEMBER_TYPES: Final = (
    "editorial_blueprint",
    "evidence_closure_set",
    "context_manifest",
)
_CPU_CHILD_MEMBER_TYPES: Final = (
    "root_media_evidence_bundle",
    "candidate_timed_evidence_index",
    "timed_speech_profile_admission",
    "presentation_timeline_probe",
    "committed_video_to_audio_clock_map_certificate",
)
_CUDA_CHILD_MEMBER_TYPES: Final = (
    "root_media_evidence_bundle",
    "candidate_timed_evidence_index",
    "runtime_timed_speech_capability_admission",
    "presentation_timeline_probe",
    "committed_video_to_audio_clock_map_certificate",
)


class PhysicalEditAdmissionError(ValueError):
    """A compilation or admission value is malformed or self-contradictory."""


def _rule_code(rule_id: str, suffix: str) -> str:
    return rule_id.replace("-", "_") + suffix


def _object(value: object, fields: tuple[str, ...], label: str) -> Mapping[str, object]:
    if type(value) is not dict:  # noqa: E721 - exact JSON object boundary.
        raise PhysicalEditAdmissionError(f"{label} must be a closed object")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw) or set(raw) != set(fields):  # noqa: E721
        raise PhysicalEditAdmissionError(f"{label} has missing or unknown fields")
    return cast(Mapping[str, object], raw)


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise PhysicalEditAdmissionError(f"{label} must be an array")
    return cast(list[object], value)


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise PhysicalEditAdmissionError(f"{label} must be non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise PhysicalEditAdmissionError(f"{label} must be valid UTF-8") from error
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    try:
        result = require_pts(value, label)
    except MediaValidationError as error:
        raise PhysicalEditAdmissionError(str(error)) from error
    if result < minimum:
        raise PhysicalEditAdmissionError(f"{label} must be >= {minimum}")
    return result


def _sha256(value: object, label: str) -> str:
    try:
        result = sha256_prefixed(value, label)
    except MediaValidationError as error:
        raise PhysicalEditAdmissionError(str(error)) from error
    if result == "sha256:" + "0" * 64:
        raise PhysicalEditAdmissionError(f"{label} must not be the all-zero digest")
    return result


def _optional_sha256(value: object, label: str) -> str | None:
    return None if value is None else _sha256(value, label)


def _scope(value: object, label: str) -> ArtifactScope:
    raw = _object(value, ("namespace", "kind", "key"), label)
    try:
        return ArtifactScope(
            _text(raw["namespace"], f"{label}.namespace"),
            _text(raw["kind"], f"{label}.kind"),
            _text(raw["key"], f"{label}.key"),
        )
    except StoreValidationError as error:
        raise PhysicalEditAdmissionError(str(error)) from error


def _reference(value: object, label: str) -> CommittedArtifactMemberReference:
    try:
        result = CommittedArtifactMemberReference.from_mapping(value)
    except (StoreValidationError, TypeError, ValueError) as error:
        raise PhysicalEditAdmissionError(f"{label}: {error}") from error
    if result.to_mapping() != value:
        raise PhysicalEditAdmissionError(f"{label} must preserve canonical wire values")
    return result


def _validate_reference_group(
    refs: tuple[CommittedArtifactMemberReference, ...],
    label: str,
    *,
    exact_count: int | None = None,
) -> None:
    if (
        type(refs) is not tuple  # noqa: E721
        or not refs
        or any(type(item) is not CommittedArtifactMemberReference for item in refs)  # noqa: E721
    ):
        raise PhysicalEditAdmissionError(f"{label} must be a non-empty exact reference tuple")
    if exact_count is not None and len(refs) != exact_count:
        raise PhysicalEditAdmissionError(f"{label} must contain exactly {exact_count} members")
    if any(item.receipt_id.int == 0 or item.artifact_set_id.int == 0 for item in refs):
        raise PhysicalEditAdmissionError(f"{label} cannot contain nil committed owners")
    if tuple(item.member_ordinal for item in refs) != tuple(range(len(refs))):
        raise PhysicalEditAdmissionError(f"{label} member ordinals must be complete and ordered")
    if len({(item.receipt_id, item.artifact_set_id, item.scope) for item in refs}) != 1:
        raise PhysicalEditAdmissionError(f"{label} members must share one committed owner")
    if len({item.revision for item in refs}) != 1:
        raise PhysicalEditAdmissionError(f"{label} members must share one revision")


def _require_pipeline_job_scope(scope: ArtifactScope, label: str) -> None:
    if (
        type(scope) is not ArtifactScope  # noqa: E721
        or scope.namespace != "pipeline"
        or scope.kind != "job"
        or not scope.key
    ):
        raise PhysicalEditAdmissionError(f"{label} must use canonical pipeline/job scope")


def _stage3_story_ids(
    refs: tuple[CommittedArtifactMemberReference, ...],
) -> tuple[str, ...]:
    if len(refs) < 4 or (len(refs) - 1) % 3:
        raise PhysicalEditAdmissionError("Stage 3 references must have exact 3N+1 layout")
    stories: list[str] = []
    for offset in range(0, len(refs) - 1, 3):
        group = refs[offset : offset + 3]
        if tuple(item.artifact_type for item in group) != _STAGE3_STORY_MEMBER_TYPES:
            raise PhysicalEditAdmissionError("Stage 3 Story member type layout is invalid")
        prefix = "editorial_blueprint@"
        logical_id = group[0].logical_id
        if not logical_id.startswith(prefix) or not logical_id[len(prefix) :]:
            raise PhysicalEditAdmissionError("Stage 3 Blueprint logical identity is invalid")
        story_id = logical_id[len(prefix) :]
        if tuple(item.logical_id for item in group) != (
            f"editorial_blueprint@{story_id}",
            f"evidence_closure_set@{story_id}",
            f"context_manifest@{story_id}",
        ):
            raise PhysicalEditAdmissionError("Stage 3 Story logical-id layout is invalid")
        stories.append(story_id)
    admission = refs[-1]
    if (
        admission.artifact_type != "semantic_feasibility_admission"
        or admission.logical_id != "semantic_feasibility_admission"
    ):
        raise PhysicalEditAdmissionError("Stage 3 terminal Admission member is invalid")
    if len(set(stories)) != len(stories):
        raise PhysicalEditAdmissionError("Stage 3 Story layout repeats a Story")
    return tuple(stories)


def _validate_child_layout(
    refs: tuple[CommittedArtifactMemberReference, ...],
    *,
    backend: PhysicalEditBackend,
    episode_ordinal: int,
) -> None:
    types = (
        _CPU_CHILD_MEMBER_TYPES if backend == "installed_cpu_profile" else _CUDA_CHILD_MEMBER_TYPES
    )
    if tuple(item.artifact_type for item in refs) != types:
        raise PhysicalEditAdmissionError("timed-media child member type layout is invalid")
    if backend == "installed_cpu_profile":
        suffix = f"episode_{episode_ordinal:04d}"
        expected_logical_ids = (
            f"root_media_evidence_{suffix}",
            f"candidate_timed_evidence_{suffix}",
            f"timed_speech_profile_admission_{suffix}",
            f"presentation_timeline_probe_{suffix}",
            f"video_to_audio_clock_map_{suffix}",
        )
    else:
        expected_logical_ids = (
            "root_media_evidence",
            "candidate_timed_evidence",
            "runtime_timed_speech_capability_admission",
            "presentation_timeline_probe",
            "video_to_audio_clock_map",
        )
    if tuple(item.logical_id for item in refs) != expected_logical_ids:
        raise PhysicalEditAdmissionError("timed-media child logical-id layout is invalid")


def _validate_selected_pair(
    query: EditorialExactSpanQuery,
    result: CandidateExactSpanResult,
) -> None:
    """Reuse the production Recipe's public exact-span closure without persisting it."""
    if type(query) is not EditorialExactSpanQuery or type(result) is not CandidateExactSpanResult:  # noqa: E721
        raise PhysicalEditAdmissionError(
            "selected query/result must retain their exact shared types"
        )
    proof = result.boundary_proof
    try:
        ProductionSpan.from_exact_span(
            ordinal=0,
            source_blob=BlobRef(_FIXTURE_BLOB, proof.source_sha256, 1, "video/mp4"),
            source_manifest_ref=CommittedArtifactMemberReference(
                _FIXTURE_RECEIPT,
                _FIXTURE_SET,
                0,
                ArtifactScope("pipeline", "job", "physical-edit-value-validation"),
                "whole_series_source_manifest",
                "whole_series_source_manifest",
                1,
                proof.frame_pts_index_set_sha256,
            ),
            query=query,
            result=result,
        )
    except (ProductionRecipeError, StoreValidationError, ValueError) as error:
        raise PhysicalEditAdmissionError(str(error)) from error


def _decode_selected_pair(
    query_value: object,
    result_value: object,
    *,
    requirement_id: str,
    alternative_id: str,
    candidate_id: str,
) -> tuple[EditorialExactSpanQuery, CandidateExactSpanResult]:
    """Decode nested shared values through the production Recipe's closed codec."""
    query_raw = _object(
        query_value,
        (
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
        ),
        "selected_query",
    )
    result_raw = _object(
        result_value,
        (
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
        ),
        "selected_result",
    )
    proof = _object(
        result_raw["boundary_proof"],
        (
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
        ),
        "selected_result.boundary_proof",
    )
    span_wire: dict[str, object] = {
        "ordinal": 0,
        "requirement_id": requirement_id,
        "alternative_id": alternative_id,
        "candidate_id": candidate_id,
        "catalog_candidate_sha256": query_raw["catalog_candidate_sha256"],
        "source_blob": {
            "object_id": str(_FIXTURE_BLOB),
            "content_hash": proof["source_sha256"],
            "byte_length": 1,
            "media_type": "video/mp4",
        },
        "source_manifest_ref": CommittedArtifactMemberReference(
            _FIXTURE_RECEIPT,
            _FIXTURE_SET,
            0,
            ArtifactScope("pipeline", "job", "physical-edit-value-validation"),
            "whole_series_source_manifest",
            "whole_series_source_manifest",
            1,
            _sha256(proof["frame_pts_index_set_sha256"], "frame index hash"),
        ).to_mapping(),
        "exact_span_query": query_value,
        "exact_span_query_sha256": canonical_sha256(query_value),
        "exact_span_result": result_value,
        "exact_span_result_sha256": canonical_sha256(result_value),
        "exact_span_proof_sha256": canonical_sha256(proof),
        "av_pairing_proof_sha256": proof["clock_map_certificate_sha256"],
    }
    try:
        span = ProductionSpan.from_mapping(span_wire)
    except (ProductionRecipeError, StoreValidationError, ValueError) as error:
        raise PhysicalEditAdmissionError(str(error)) from error
    return span.exact_span_query, span.exact_span_result


@dataclass(frozen=True, slots=True)
class PhysicalEditCompilationAttempt:
    """One ordered span-intent attempt and its closed outcome."""

    span_intent: Literal["tight", "scene", "context"]
    outcome: Literal["selected", "no_legal_span", "indeterminate"]
    code: str
    exact_span_query_sha256: str | None = None
    exact_span_result_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.span_intent not in _SPAN_INTENTS:
            raise PhysicalEditAdmissionError("compilation attempt span intent is unsupported")
        if self.outcome not in _ATTEMPT_CODES:
            raise PhysicalEditAdmissionError("compilation attempt outcome is unsupported")
        if type(self.code) is not str or self.code not in _ATTEMPT_CODES[self.outcome]:  # noqa: E721
            raise PhysicalEditAdmissionError("compilation attempt code contradicts its outcome")
        query_hash = self.exact_span_query_sha256
        result_hash = self.exact_span_result_sha256
        if query_hash is not None:
            _sha256(query_hash, "attempt.exact_span_query_sha256")
        if result_hash is not None:
            _sha256(result_hash, "attempt.exact_span_result_sha256")
        if self.outcome == "selected" and (query_hash is None or result_hash is None):
            raise PhysicalEditAdmissionError(
                "selected attempt requires exact query and result hashes"
            )
        if self.outcome == "no_legal_span" and (query_hash is None or result_hash is not None):
            raise PhysicalEditAdmissionError(
                "no-legal-span attempt requires only an exact query hash"
            )
        if self.outcome == "indeterminate" and result_hash is not None:
            raise PhysicalEditAdmissionError("indeterminate attempt cannot claim an exact result")

    def to_mapping(self) -> dict[str, object]:
        return {
            "span_intent": self.span_intent,
            "outcome": self.outcome,
            "code": self.code,
            "exact_span_query_sha256": self.exact_span_query_sha256,
            "exact_span_result_sha256": self.exact_span_result_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> PhysicalEditCompilationAttempt:
        raw = _object(
            value,
            (
                "span_intent",
                "outcome",
                "code",
                "exact_span_query_sha256",
                "exact_span_result_sha256",
            ),
            "compilation attempt",
        )
        return cls(
            cast(
                Literal["tight", "scene", "context"],
                _text(raw["span_intent"], "attempt.span_intent"),
            ),
            cast(
                Literal["selected", "no_legal_span", "indeterminate"],
                _text(raw["outcome"], "attempt.outcome"),
            ),
            _text(raw["code"], "attempt.code"),
            _optional_sha256(raw["exact_span_query_sha256"], "attempt.exact_span_query_sha256"),
            _optional_sha256(raw["exact_span_result_sha256"], "attempt.exact_span_result_sha256"),
        )


@dataclass(frozen=True, slots=True)
class PhysicalEditCompilationEntry:
    """One admitted Stage 3 choice and its uniquely selected exact A/V span."""

    ordinal: int
    story_id: str
    beat_id: str
    requirement_id: str
    alternative_id: str
    candidate_id: str
    episode_ordinal: int
    candidate_ordinal: int
    attempts: tuple[PhysicalEditCompilationAttempt, ...]
    selected_query: EditorialExactSpanQuery
    selected_result: CandidateExactSpanResult

    def __post_init__(self) -> None:
        _integer(self.ordinal, "entry.ordinal")
        _integer(self.episode_ordinal, "entry.episode_ordinal")
        _integer(self.candidate_ordinal, "entry.candidate_ordinal")
        for name in ("story_id", "beat_id", "requirement_id", "alternative_id", "candidate_id"):
            _text(getattr(self, name), f"entry.{name}")
        if (
            type(self.attempts) is not tuple  # noqa: E721
            or not self.attempts
            or any(type(item) is not PhysicalEditCompilationAttempt for item in self.attempts)  # noqa: E721
        ):
            raise PhysicalEditAdmissionError("compilation entry requires non-empty exact attempts")
        if len({item.span_intent for item in self.attempts}) != len(self.attempts):
            raise PhysicalEditAdmissionError("compilation entry repeats a span intent")
        selected = tuple(item for item in self.attempts if item.outcome == "selected")
        if len(selected) != 1 or self.attempts[-1] is not selected[0]:
            raise PhysicalEditAdmissionError("entry must end with exactly one selected attempt")
        _validate_selected_pair(self.selected_query, self.selected_result)
        query = self.selected_query
        result = self.selected_result
        attempt = selected[0]
        if (
            query.story_id,
            query.beat_id,
            query.evidence_requirement_id,
            query.alternative_id,
            query.candidate_id,
        ) != (
            self.story_id,
            self.beat_id,
            self.requirement_id,
            self.alternative_id,
            self.candidate_id,
        ):
            raise PhysicalEditAdmissionError(
                "compilation entry identities differ from selected query"
            )
        if attempt.span_intent != query.span_intent:
            raise PhysicalEditAdmissionError("selected attempt intent differs from selected query")
        if (
            attempt.exact_span_query_sha256 != query.canonical_hash
            or attempt.exact_span_result_sha256 != result.canonical_hash
        ):
            raise PhysicalEditAdmissionError("selected attempt hashes differ from embedded values")

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "story_id": self.story_id,
            "beat_id": self.beat_id,
            "requirement_id": self.requirement_id,
            "alternative_id": self.alternative_id,
            "candidate_id": self.candidate_id,
            "episode_ordinal": self.episode_ordinal,
            "candidate_ordinal": self.candidate_ordinal,
            "attempts": [item.to_mapping() for item in self.attempts],
            "selected_query": self.selected_query.to_mapping(),
            "selected_result": self.selected_result.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> PhysicalEditCompilationEntry:
        raw = _object(
            value,
            (
                "ordinal",
                "story_id",
                "beat_id",
                "requirement_id",
                "alternative_id",
                "candidate_id",
                "episode_ordinal",
                "candidate_ordinal",
                "attempts",
                "selected_query",
                "selected_result",
            ),
            "compilation entry",
        )
        requirement_id = _text(raw["requirement_id"], "entry.requirement_id")
        alternative_id = _text(raw["alternative_id"], "entry.alternative_id")
        candidate_id = _text(raw["candidate_id"], "entry.candidate_id")
        query, result = _decode_selected_pair(
            raw["selected_query"],
            raw["selected_result"],
            requirement_id=requirement_id,
            alternative_id=alternative_id,
            candidate_id=candidate_id,
        )
        return cls(
            _integer(raw["ordinal"], "entry.ordinal"),
            _text(raw["story_id"], "entry.story_id"),
            _text(raw["beat_id"], "entry.beat_id"),
            requirement_id,
            alternative_id,
            candidate_id,
            _integer(raw["episode_ordinal"], "entry.episode_ordinal"),
            _integer(raw["candidate_ordinal"], "entry.candidate_ordinal"),
            tuple(
                PhysicalEditCompilationAttempt.from_mapping(item)
                for item in _array(raw["attempts"], "entry.attempts")
            ),
            query,
            result,
        )


@dataclass(frozen=True, slots=True)
class PhysicalEditChoiceIdentity:
    """One independently reread Stage 3 choice in its frozen compilation order."""

    ordinal: int
    story_id: str
    beat_id: str
    requirement_id: str
    alternative_id: str
    candidate_id: str
    episode_ordinal: int
    candidate_ordinal: int

    def __post_init__(self) -> None:
        _integer(self.ordinal, "choice_identity.ordinal")
        for name in (
            "story_id",
            "beat_id",
            "requirement_id",
            "alternative_id",
            "candidate_id",
        ):
            _text(getattr(self, name), f"choice_identity.{name}")
        _integer(self.episode_ordinal, "choice_identity.episode_ordinal")
        _integer(self.candidate_ordinal, "choice_identity.candidate_ordinal")

    @classmethod
    def from_entry(cls, entry: PhysicalEditCompilationEntry) -> PhysicalEditChoiceIdentity:
        if type(entry) is not PhysicalEditCompilationEntry:  # noqa: E721
            raise PhysicalEditAdmissionError("choice identity requires an exact compilation entry")
        return cls(
            entry.ordinal,
            entry.story_id,
            entry.beat_id,
            entry.requirement_id,
            entry.alternative_id,
            entry.candidate_id,
            entry.episode_ordinal,
            entry.candidate_ordinal,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "story_id": self.story_id,
            "beat_id": self.beat_id,
            "requirement_id": self.requirement_id,
            "alternative_id": self.alternative_id,
            "candidate_id": self.candidate_id,
            "episode_ordinal": self.episode_ordinal,
            "candidate_ordinal": self.candidate_ordinal,
        }


@dataclass(frozen=True, slots=True)
class PhysicalEditCompilationReport:
    """Exact successful compilation evidence; never an Admission authority."""

    input_binding_sha256: str
    stage3_member_refs: tuple[CommittedArtifactMemberReference, ...]
    media_batch_member_ref: CommittedArtifactMemberReference
    timed_media_child_member_refs: tuple[tuple[CommittedArtifactMemberReference, ...], ...]
    backend_discriminator: PhysicalEditBackend
    authority_sha256: str
    editorial_exact_policy_sha256: str
    candidate_exact_policy_sha256: str
    entries: tuple[PhysicalEditCompilationEntry, ...]
    compilation_strategy_version: str = PHYSICAL_EDIT_COMPILATION_STRATEGY_VERSION
    editorial_strategy_version: str = EDITORIAL_EXACT_SPAN_STRATEGY
    exact_span_strategy_version: str = PHYSICAL_EDIT_EXACT_SPAN_STRATEGY_VERSION

    def __post_init__(self) -> None:
        for name in (
            "input_binding_sha256",
            "authority_sha256",
            "editorial_exact_policy_sha256",
            "candidate_exact_policy_sha256",
        ):
            _sha256(getattr(self, name), f"report.{name}")
        if self.compilation_strategy_version != PHYSICAL_EDIT_COMPILATION_STRATEGY_VERSION:
            raise PhysicalEditAdmissionError("compilation report strategy is unsupported")
        if self.editorial_strategy_version != EDITORIAL_EXACT_SPAN_STRATEGY:
            raise PhysicalEditAdmissionError("editorial query strategy is unsupported")
        if self.exact_span_strategy_version != PHYSICAL_EDIT_EXACT_SPAN_STRATEGY_VERSION:
            raise PhysicalEditAdmissionError("candidate exact-span strategy is unsupported")
        if self.backend_discriminator not in ("installed_cpu_profile", "runtime_cuda_capability"):
            raise PhysicalEditAdmissionError("compilation backend discriminator is unsupported")
        _validate_reference_group(self.stage3_member_refs, "stage3_member_refs")
        stage3_story_ids = _stage3_story_ids(self.stage3_member_refs)
        _require_pipeline_job_scope(self.stage3_member_refs[0].scope, "Stage 3 references")
        if type(self.media_batch_member_ref) is not CommittedArtifactMemberReference:  # noqa: E721
            raise PhysicalEditAdmissionError("media batch member ref must be exact")
        expected_batch_type = (
            "timed_media_evidence_batch"
            if self.backend_discriminator == "installed_cpu_profile"
            else "runtime_timed_media_evidence_batch"
        )
        batch = self.media_batch_member_ref
        if batch.receipt_id.int == 0 or batch.artifact_set_id.int == 0:
            raise PhysicalEditAdmissionError("media batch member cannot have a nil committed owner")
        if (
            batch.member_ordinal != 0
            or batch.artifact_type != expected_batch_type
            or batch.logical_id != expected_batch_type
        ):
            raise PhysicalEditAdmissionError(
                "media batch member differs from backend discriminator"
            )
        _require_pipeline_job_scope(batch.scope, "media batch member")
        children = self.timed_media_child_member_refs
        if (
            type(children) is not tuple
            or not children
            or any(type(row) is not tuple for row in children)
        ):  # noqa: E721
            raise PhysicalEditAdmissionError("report requires every ordered timed-media child")
        for index, refs in enumerate(children):
            _validate_reference_group(
                refs, f"timed_media_child_member_refs[{index}]", exact_count=5
            )
            _validate_child_layout(refs, backend=self.backend_discriminator, episode_ordinal=index)
            _require_pipeline_job_scope(refs[0].scope, "timed-media child")
        all_refs = (*self.stage3_member_refs, batch, *(ref for row in children for ref in row))
        if len({ref.scope for ref in all_refs}) != 1:
            raise PhysicalEditAdmissionError(
                "report predecessor references must share one Job scope"
            )
        identities = tuple(ref.to_mapping() for ref in all_refs)
        if len({canonical_sha256(item) for item in identities}) != len(identities):
            raise PhysicalEditAdmissionError("report repeats a predecessor member reference")
        if (
            type(self.entries) is not tuple  # noqa: E721
            or not self.entries
            or any(type(item) is not PhysicalEditCompilationEntry for item in self.entries)  # noqa: E721
        ):
            raise PhysicalEditAdmissionError(
                "compilation report requires ordered non-empty entries"
            )
        if tuple(item.ordinal for item in self.entries) != tuple(range(len(self.entries))):
            raise PhysicalEditAdmissionError(
                "compilation entry ordinals must be complete and ordered"
            )
        choice_keys = tuple(
            (
                item.story_id,
                item.beat_id,
                item.requirement_id,
                item.alternative_id,
                item.candidate_id,
            )
            for item in self.entries
        )
        if len(set(choice_keys)) != len(choice_keys):
            raise PhysicalEditAdmissionError("compilation report repeats an admitted choice")
        if _story_census(self.entries) != stage3_story_ids:
            raise PhysicalEditAdmissionError(
                "compilation entry Story order differs from exact Stage 3 layout"
            )
        expected_kind = (
            CandidateTimedSpeechAuthorityKind.INSTALLED_CPU_PROFILE
            if self.backend_discriminator == "installed_cpu_profile"
            else CandidateTimedSpeechAuthorityKind.RUNTIME_CUDA_CAPABILITY
        )
        for entry in self.entries:
            if entry.episode_ordinal >= len(children):
                raise PhysicalEditAdmissionError("entry episode ordinal escapes media child census")
            query = entry.selected_query
            result = entry.selected_result
            guard = result.dialogue_guard
            if query.policy_sha256 != self.editorial_exact_policy_sha256:
                raise PhysicalEditAdmissionError(
                    "selected query differs from report editorial policy"
                )
            if result.policy_sha256 != self.candidate_exact_policy_sha256:
                raise PhysicalEditAdmissionError("selected result differs from report exact policy")
            if (
                guard.original_authority_kind is not expected_kind
                or guard.original_authority_sha256 != self.authority_sha256
            ):
                raise PhysicalEditAdmissionError(
                    "selected result differs from report backend authority"
                )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": PHYSICAL_EDIT_COMPILATION_REPORT_SCHEMA_VERSION,
            "compilation_strategy_version": self.compilation_strategy_version,
            "editorial_strategy_version": self.editorial_strategy_version,
            "exact_span_strategy_version": self.exact_span_strategy_version,
            "input_binding_sha256": self.input_binding_sha256,
            "stage3_member_refs": [item.to_mapping() for item in self.stage3_member_refs],
            "media_batch_member_ref": self.media_batch_member_ref.to_mapping(),
            "timed_media_child_member_refs": [
                [item.to_mapping() for item in row] for row in self.timed_media_child_member_refs
            ],
            "backend_discriminator": self.backend_discriminator,
            "authority_sha256": self.authority_sha256,
            "editorial_exact_policy_sha256": self.editorial_exact_policy_sha256,
            "candidate_exact_policy_sha256": self.candidate_exact_policy_sha256,
            "entries": [item.to_mapping() for item in self.entries],
        }

    @classmethod
    def from_mapping(cls, value: object) -> PhysicalEditCompilationReport:
        raw = _object(
            value,
            (
                "schema_version",
                "compilation_strategy_version",
                "editorial_strategy_version",
                "exact_span_strategy_version",
                "input_binding_sha256",
                "stage3_member_refs",
                "media_batch_member_ref",
                "timed_media_child_member_refs",
                "backend_discriminator",
                "authority_sha256",
                "editorial_exact_policy_sha256",
                "candidate_exact_policy_sha256",
                "entries",
            ),
            "compilation report",
        )
        if raw["schema_version"] != PHYSICAL_EDIT_COMPILATION_REPORT_SCHEMA_VERSION:
            raise PhysicalEditAdmissionError("compilation report schema is unsupported")
        child_rows = _array(raw["timed_media_child_member_refs"], "report child refs")
        return cls(
            _sha256(raw["input_binding_sha256"], "report.input_binding_sha256"),
            tuple(
                _reference(item, f"stage3_member_refs[{index}]")
                for index, item in enumerate(_array(raw["stage3_member_refs"], "stage3 refs"))
            ),
            _reference(raw["media_batch_member_ref"], "media_batch_member_ref"),
            tuple(
                tuple(
                    _reference(item, f"child_refs[{row_index}][{index}]")
                    for index, item in enumerate(_array(row, f"child_refs[{row_index}]"))
                )
                for row_index, row in enumerate(child_rows)
            ),
            cast(PhysicalEditBackend, _text(raw["backend_discriminator"], "backend_discriminator")),
            _sha256(raw["authority_sha256"], "report.authority_sha256"),
            _sha256(raw["editorial_exact_policy_sha256"], "report.editorial policy"),
            _sha256(raw["candidate_exact_policy_sha256"], "report exact policy"),
            tuple(
                PhysicalEditCompilationEntry.from_mapping(item)
                for item in _array(raw["entries"], "report.entries")
            ),
            _text(raw["compilation_strategy_version"], "compilation strategy"),
            _text(raw["editorial_strategy_version"], "editorial strategy"),
            _text(raw["exact_span_strategy_version"], "exact span strategy"),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class PhysicalEditRecipeSubject:
    """Pre-commit Recipe identity without circular Receipt or ArtifactSet IDs."""

    ordinal: int
    story_id: str
    artifact_type: Literal["recipe"]
    logical_id: str
    revision: int
    scope: ArtifactScope
    content_hash: str

    def __post_init__(self) -> None:
        _integer(self.ordinal, "recipe_subject.ordinal")
        _text(self.story_id, "recipe_subject.story_id")
        _text(self.logical_id, "recipe_subject.logical_id")
        _integer(self.revision, "recipe_subject.revision", minimum=1)
        if type(self.scope) is not ArtifactScope:  # noqa: E721
            raise PhysicalEditAdmissionError("recipe subject scope must be exact")
        if self.artifact_type != "recipe":
            raise PhysicalEditAdmissionError("recipe subject artifact type must be recipe")
        _sha256(self.content_hash, "recipe_subject.content_hash")

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "story_id": self.story_id,
            "artifact_type": self.artifact_type,
            "logical_id": self.logical_id,
            "revision": self.revision,
            "scope": {
                "namespace": self.scope.namespace,
                "kind": self.scope.kind,
                "key": self.scope.key,
            },
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_mapping(cls, value: object) -> PhysicalEditRecipeSubject:
        raw = _object(
            value,
            (
                "ordinal",
                "story_id",
                "artifact_type",
                "logical_id",
                "revision",
                "scope",
                "content_hash",
            ),
            "recipe subject",
        )
        return cls(
            _integer(raw["ordinal"], "recipe_subject.ordinal"),
            _text(raw["story_id"], "recipe_subject.story_id"),
            cast(Literal["recipe"], _text(raw["artifact_type"], "recipe_subject.artifact_type")),
            _text(raw["logical_id"], "recipe_subject.logical_id"),
            _integer(raw["revision"], "recipe_subject.revision", minimum=1),
            _scope(raw["scope"], "recipe_subject.scope"),
            _sha256(raw["content_hash"], "recipe_subject.content_hash"),
        )


@dataclass(frozen=True, slots=True)
class PhysicalEditReplayFact:
    """One independently recomputed rule census, or a closed unavailable reason."""

    rule_id: PhysicalEditRuleId
    evidence_count: int | None
    evidence_sha256: str | None
    indeterminate_code: str | None = None

    def __post_init__(self) -> None:
        if self.rule_id not in PHYSICAL_EDIT_RULE_IDS:
            raise PhysicalEditAdmissionError("replay fact rule id is unsupported")
        if self.evidence_count is None or self.evidence_sha256 is None:
            if self.evidence_count is not None or self.evidence_sha256 is not None:
                raise PhysicalEditAdmissionError("replay fact count/hash availability must agree")
            if self.indeterminate_code != _rule_code(self.rule_id, "_REPLAY_INDETERMINATE"):
                raise PhysicalEditAdmissionError(
                    "unavailable replay fact needs its exact stable rule code"
                )
            return
        _integer(self.evidence_count, "replay_fact.evidence_count")
        _sha256(self.evidence_sha256, "replay_fact.evidence_sha256")
        if self.indeterminate_code is not None:
            raise PhysicalEditAdmissionError("complete replay fact cannot claim indeterminate")

    def to_mapping(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "evidence_count": self.evidence_count,
            "evidence_sha256": self.evidence_sha256,
            "indeterminate_code": self.indeterminate_code,
        }

    @classmethod
    def from_mapping(cls, value: object) -> PhysicalEditReplayFact:
        raw = _object(
            value,
            ("rule_id", "evidence_count", "evidence_sha256", "indeterminate_code"),
            "replay fact",
        )
        count = (
            None
            if raw["evidence_count"] is None
            else _integer(raw["evidence_count"], "replay count")
        )
        code = (
            None
            if raw["indeterminate_code"] is None
            else _text(raw["indeterminate_code"], "indeterminate code")
        )
        return cls(
            cast(PhysicalEditRuleId, _text(raw["rule_id"], "replay rule id")),
            count,
            _optional_sha256(raw["evidence_sha256"], "replay evidence hash"),
            code,
        )


@dataclass(frozen=True, slots=True)
class PhysicalEditReplayEvidence:
    """Untrusted value carrying an independent evaluator's exact eight censuses.

    Construction or decoding does not prove that the evaluator actually ran.
    Only ``verify_physical_edit_admission`` may combine this value with trusted,
    independently reread predecessor and Stage 3 choice inputs.
    """

    facts: tuple[PhysicalEditReplayFact, ...]
    evaluator_strategy_version: str

    def __post_init__(self) -> None:
        if (
            type(self.facts) is not tuple  # noqa: E721
            or any(type(item) is not PhysicalEditReplayFact for item in self.facts)  # noqa: E721
            or tuple(item.rule_id for item in self.facts) != PHYSICAL_EDIT_RULE_IDS
        ):
            raise PhysicalEditAdmissionError(
                "replay evidence must contain the exact ordered rule census"
            )
        if self.evaluator_strategy_version != PHYSICAL_EDIT_REPLAY_EVALUATOR_STRATEGY_VERSION:
            raise PhysicalEditAdmissionError("replay evaluator strategy is unsupported")

    def to_mapping(self) -> dict[str, object]:
        return {
            "facts": [item.to_mapping() for item in self.facts],
            "evaluator_strategy_version": self.evaluator_strategy_version,
        }

    @classmethod
    def from_mapping(cls, value: object) -> PhysicalEditReplayEvidence:
        raw = _object(value, ("facts", "evaluator_strategy_version"), "replay evidence")
        return cls(
            tuple(
                PhysicalEditReplayFact.from_mapping(item)
                for item in _array(raw["facts"], "replay facts")
            ),
            _text(raw["evaluator_strategy_version"], "replay evaluator strategy"),
        )


@dataclass(frozen=True, slots=True)
class PhysicalEditCheck:
    """One hash-bound, derived physical rule result."""

    rule_id: PhysicalEditRuleId
    status: PhysicalEditCheckStatus
    expected_count: int
    expected_sha256: str
    observed_count: int | None
    observed_sha256: str | None
    violations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.rule_id not in PHYSICAL_EDIT_RULE_IDS:
            raise PhysicalEditAdmissionError("physical edit check rule is unsupported")
        if self.status not in ("pass", "fail", "indeterminate"):
            raise PhysicalEditAdmissionError("physical edit check status is unsupported")
        _integer(self.expected_count, "check.expected_count")
        _sha256(self.expected_sha256, "check.expected_sha256")
        if (self.observed_count is None) != (self.observed_sha256 is None):
            raise PhysicalEditAdmissionError("check observed count/hash availability must agree")
        if self.observed_count is not None:
            _integer(self.observed_count, "check.observed_count")
            _sha256(self.observed_sha256, "check.observed_sha256")
        if (
            type(self.violations) is not tuple  # noqa: E721
            or any(
                type(item) is not str or _STABLE_CODE.fullmatch(item) is None
                for item in self.violations
            )  # noqa: E721
            or tuple(sorted(set(self.violations))) != self.violations
        ):
            raise PhysicalEditAdmissionError(
                "physical edit violations must be stable ordered codes"
            )
        equal = (
            self.observed_count == self.expected_count
            and self.observed_sha256 == self.expected_sha256
        )
        if self.status == "pass" and (not equal or self.violations):
            raise PhysicalEditAdmissionError("pass check must exactly match its replay census")
        if self.status == "fail" and (
            self.observed_count is None
            or equal
            or self.violations != (_rule_code(self.rule_id, "_REPLAY_MISMATCH"),)
        ):
            raise PhysicalEditAdmissionError(
                "fail check requires a mismatched complete replay census"
            )
        if self.status == "indeterminate" and (
            self.observed_count is not None
            or self.violations != (_rule_code(self.rule_id, "_REPLAY_INDETERMINATE"),)
        ):
            raise PhysicalEditAdmissionError(
                "indeterminate check requires unavailable replay evidence"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "status": self.status,
            "expected_count": self.expected_count,
            "expected_sha256": self.expected_sha256,
            "observed_count": self.observed_count,
            "observed_sha256": self.observed_sha256,
            "violations": list(self.violations),
        }

    @classmethod
    def from_mapping(cls, value: object) -> PhysicalEditCheck:
        raw = _object(
            value,
            (
                "rule_id",
                "status",
                "expected_count",
                "expected_sha256",
                "observed_count",
                "observed_sha256",
                "violations",
            ),
            "physical edit check",
        )
        observed_count = (
            None
            if raw["observed_count"] is None
            else _integer(raw["observed_count"], "check.observed_count")
        )
        return cls(
            cast(PhysicalEditRuleId, _text(raw["rule_id"], "check.rule_id")),
            cast(PhysicalEditCheckStatus, _text(raw["status"], "check.status")),
            _integer(raw["expected_count"], "check.expected_count"),
            _sha256(raw["expected_sha256"], "check.expected_sha256"),
            observed_count,
            _optional_sha256(raw["observed_sha256"], "check.observed_sha256"),
            tuple(
                _text(item, "check.violation")
                for item in _array(raw["violations"], "check.violations")
            ),
        )


def _story_census(entries: tuple[PhysicalEditCompilationEntry, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for entry in entries:
        if entry.story_id not in result:
            result.append(entry.story_id)
    return tuple(result)


def _expected_rule_evidence(
    report: PhysicalEditCompilationReport,
    subjects: tuple[PhysicalEditRecipeSubject, ...],
) -> tuple[tuple[int, str], ...]:
    entries = report.entries
    input_rows = {
        "input_binding_sha256": report.input_binding_sha256,
        "stage3_member_refs": [item.to_mapping() for item in report.stage3_member_refs],
        "media_batch_member_ref": report.media_batch_member_ref.to_mapping(),
        "timed_media_child_member_refs": [
            [item.to_mapping() for item in row] for row in report.timed_media_child_member_refs
        ],
        "backend_discriminator": report.backend_discriminator,
        "authority_sha256": report.authority_sha256,
        "editorial_exact_policy_sha256": report.editorial_exact_policy_sha256,
        "candidate_exact_policy_sha256": report.candidate_exact_policy_sha256,
    }
    choices = [
        {
            "ordinal": item.ordinal,
            "story_id": item.story_id,
            "beat_id": item.beat_id,
            "requirement_id": item.requirement_id,
            "alternative_id": item.alternative_id,
            "candidate_id": item.candidate_id,
            "episode_ordinal": item.episode_ordinal,
            "candidate_ordinal": item.candidate_ordinal,
        }
        for item in entries
    ]
    queries = [
        {
            "ordinal": item.ordinal,
            "query": item.selected_query.to_mapping(),
            "query_sha256": item.selected_query.canonical_hash,
        }
        for item in entries
    ]
    relations = [
        {
            "ordinal": item.ordinal,
            "result": item.selected_result.to_mapping(),
            "result_sha256": item.selected_result.canonical_hash,
        }
        for item in entries
    ]
    dialogue = [
        {
            "ordinal": item.ordinal,
            "guard": item.selected_result.dialogue_guard.to_mapping(),
            "guard_sha256": item.selected_result.dialogue_guard.canonical_hash,
        }
        for item in entries
    ]
    av = [
        {
            "ordinal": item.ordinal,
            "boundary_proof": item.selected_result.boundary_proof.to_mapping(),
            "boundary_proof_sha256": item.selected_result.boundary_proof.canonical_hash,
        }
        for item in entries
    ]
    timing = [
        {
            "ordinal": item.ordinal,
            "story_id": item.story_id,
            "beat_id": item.beat_id,
            "video_range": {
                "start_pts": item.selected_result.video_range.start_pts,
                "end_pts": item.selected_result.video_range.end_pts,
            },
            "audio_range": {
                "start_pts": item.selected_result.audio_range.start_pts,
                "end_pts": item.selected_result.audio_range.end_pts,
            },
        }
        for item in entries
    ]
    outputs = [item.to_mapping() for item in subjects]
    return (
        (
            len(report.stage3_member_refs)
            + 1
            + sum(map(len, report.timed_media_child_member_refs)),
            canonical_sha256(input_rows),
        ),
        (len(choices), canonical_sha256(choices)),
        (len(queries), canonical_sha256(queries)),
        (len(relations), canonical_sha256(relations)),
        (len(dialogue), canonical_sha256(dialogue)),
        (len(av), canonical_sha256(av)),
        (len(timing), canonical_sha256(timing)),
        (len(outputs), canonical_sha256(outputs)),
    )


@dataclass(frozen=True, slots=True)
class PhysicalEditAdmission:
    """Non-authoritative hash-bound pre-commit Admission candidate.

    ``validation_status=valid`` and ``next_action=render`` are only proposed
    values.  Construction and decoding grant no rendering authority.  A caller
    must obtain ``VerifiedPhysicalEditAdmission`` from the trusted verifier.
    """

    compilation_report_sha256: str
    recipe_subjects: tuple[PhysicalEditRecipeSubject, ...]
    input_binding_sha256: str
    authority_sha256: str
    editorial_exact_policy_sha256: str
    candidate_exact_policy_sha256: str
    checks: tuple[PhysicalEditCheck, ...]
    validation_status: PhysicalEditValidationStatus
    next_action: PhysicalEditNextAction
    strategy_version: str = PHYSICAL_EDIT_ADMISSION_STRATEGY_VERSION
    authority_status: Literal["unverified"] = "unverified"

    def __post_init__(self) -> None:
        for name in (
            "compilation_report_sha256",
            "input_binding_sha256",
            "authority_sha256",
            "editorial_exact_policy_sha256",
            "candidate_exact_policy_sha256",
        ):
            _sha256(getattr(self, name), f"admission.{name}")
        if self.strategy_version != PHYSICAL_EDIT_ADMISSION_STRATEGY_VERSION:
            raise PhysicalEditAdmissionError("physical Admission strategy is unsupported")
        if self.authority_status != "unverified":
            raise PhysicalEditAdmissionError("Admission values cannot self-declare authority")
        subjects = self.recipe_subjects
        if (
            type(subjects) is not tuple  # noqa: E721
            or not subjects
            or any(type(item) is not PhysicalEditRecipeSubject for item in subjects)  # noqa: E721
            or tuple(item.ordinal for item in subjects) != tuple(range(len(subjects)))
            or len({item.story_id for item in subjects}) != len(subjects)
            or len({item.logical_id for item in subjects}) != len(subjects)
            or len({item.scope for item in subjects}) != 1
        ):
            raise PhysicalEditAdmissionError(
                "Admission recipe subjects must be a complete ordered census"
            )
        if (
            type(self.checks) is not tuple  # noqa: E721
            or any(type(item) is not PhysicalEditCheck for item in self.checks)  # noqa: E721
            or tuple(item.rule_id for item in self.checks) != PHYSICAL_EDIT_RULE_IDS
        ):
            raise PhysicalEditAdmissionError("Admission requires the exact ordered physical checks")
        if any(item.status == "fail" for item in self.checks):
            expected = ("invalid", "stop")
        elif any(item.status == "indeterminate" for item in self.checks):
            expected = ("indeterminate", "quarantine")
        else:
            expected = ("valid", "render")
        if (self.validation_status, self.next_action) != expected:
            raise PhysicalEditAdmissionError("Admission status/action contradict derived checks")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": PHYSICAL_EDIT_ADMISSION_SCHEMA_VERSION,
            "strategy_version": self.strategy_version,
            "compilation_report_sha256": self.compilation_report_sha256,
            "recipe_subjects": [item.to_mapping() for item in self.recipe_subjects],
            "input_binding_sha256": self.input_binding_sha256,
            "authority_sha256": self.authority_sha256,
            "editorial_exact_policy_sha256": self.editorial_exact_policy_sha256,
            "candidate_exact_policy_sha256": self.candidate_exact_policy_sha256,
            "checks": [item.to_mapping() for item in self.checks],
            "validation_status": self.validation_status,
            "next_action": self.next_action,
            "authority_status": self.authority_status,
        }

    @classmethod
    def from_mapping(cls, value: object) -> PhysicalEditAdmission:
        raw = _object(
            value,
            (
                "schema_version",
                "strategy_version",
                "compilation_report_sha256",
                "recipe_subjects",
                "input_binding_sha256",
                "authority_sha256",
                "editorial_exact_policy_sha256",
                "candidate_exact_policy_sha256",
                "checks",
                "validation_status",
                "next_action",
                "authority_status",
            ),
            "physical Admission",
        )
        if raw["schema_version"] != PHYSICAL_EDIT_ADMISSION_SCHEMA_VERSION:
            raise PhysicalEditAdmissionError("physical Admission schema is unsupported")
        return cls(
            _sha256(raw["compilation_report_sha256"], "admission report hash"),
            tuple(
                PhysicalEditRecipeSubject.from_mapping(item)
                for item in _array(raw["recipe_subjects"], "admission recipe subjects")
            ),
            _sha256(raw["input_binding_sha256"], "admission input binding"),
            _sha256(raw["authority_sha256"], "admission authority"),
            _sha256(raw["editorial_exact_policy_sha256"], "admission editorial policy"),
            _sha256(raw["candidate_exact_policy_sha256"], "admission exact policy"),
            tuple(
                PhysicalEditCheck.from_mapping(item)
                for item in _array(raw["checks"], "admission checks")
            ),
            cast(
                PhysicalEditValidationStatus, _text(raw["validation_status"], "validation_status")
            ),
            cast(PhysicalEditNextAction, _text(raw["next_action"], "next_action")),
            _text(raw["strategy_version"], "admission strategy"),
            cast(
                Literal["unverified"],
                _text(raw["authority_status"], "admission authority status"),
            ),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def build_physical_edit_admission(
    report: PhysicalEditCompilationReport,
    recipe_subjects: tuple[PhysicalEditRecipeSubject, ...],
    replay_evidence: PhysicalEditReplayEvidence,
) -> PhysicalEditAdmission:
    """Derive Admission by exact comparison with independent replay censuses."""
    if type(report) is not PhysicalEditCompilationReport:  # noqa: E721
        raise PhysicalEditAdmissionError("Admission builder requires an exact compilation report")
    if (
        type(recipe_subjects) is not tuple  # noqa: E721
        or not recipe_subjects
        or any(type(item) is not PhysicalEditRecipeSubject for item in recipe_subjects)  # noqa: E721
    ):
        raise PhysicalEditAdmissionError("Admission builder requires exact Recipe subjects")
    if type(replay_evidence) is not PhysicalEditReplayEvidence:  # noqa: E721
        raise PhysicalEditAdmissionError("Admission builder requires independent replay evidence")
    stories = _story_census(report.entries)
    if tuple(item.ordinal for item in recipe_subjects) != tuple(range(len(recipe_subjects))):
        raise PhysicalEditAdmissionError("Recipe subject ordinals differ from output order")
    if tuple(item.story_id for item in recipe_subjects) != stories:
        raise PhysicalEditAdmissionError("Recipe subject census differs from compiled Story order")
    if any(item.scope != report.media_batch_member_ref.scope for item in recipe_subjects):
        raise PhysicalEditAdmissionError(
            "Recipe subject scope differs from compiled predecessor Job"
        )
    expected = _expected_rule_evidence(report, recipe_subjects)
    checks: list[PhysicalEditCheck] = []
    for rule_id, (expected_count, expected_hash), fact in zip(
        PHYSICAL_EDIT_RULE_IDS, expected, replay_evidence.facts, strict=True
    ):
        if fact.evidence_count is None:
            checks.append(
                PhysicalEditCheck(
                    rule_id,
                    "indeterminate",
                    expected_count,
                    expected_hash,
                    None,
                    None,
                    (cast(str, fact.indeterminate_code),),
                )
            )
        elif fact.evidence_count == expected_count and fact.evidence_sha256 == expected_hash:
            checks.append(
                PhysicalEditCheck(
                    rule_id,
                    "pass",
                    expected_count,
                    expected_hash,
                    fact.evidence_count,
                    fact.evidence_sha256,
                    (),
                )
            )
        else:
            checks.append(
                PhysicalEditCheck(
                    rule_id,
                    "fail",
                    expected_count,
                    expected_hash,
                    fact.evidence_count,
                    fact.evidence_sha256,
                    (_rule_code(rule_id, "_REPLAY_MISMATCH"),),
                )
            )
    statuses = {item.status for item in checks}
    if "fail" in statuses:
        validation: PhysicalEditValidationStatus = "invalid"
        action: PhysicalEditNextAction = "stop"
    elif "indeterminate" in statuses:
        validation = "indeterminate"
        action = "quarantine"
    else:
        validation = "valid"
        action = "render"
    return PhysicalEditAdmission(
        report.canonical_hash,
        recipe_subjects,
        report.input_binding_sha256,
        report.authority_sha256,
        report.editorial_exact_policy_sha256,
        report.candidate_exact_policy_sha256,
        tuple(checks),
        validation,
        action,
    )


def _verify_trusted_physical_inputs(
    *,
    candidate_admission: PhysicalEditAdmission,
    report: PhysicalEditCompilationReport,
    recipe_subjects: tuple[PhysicalEditRecipeSubject, ...],
    expected_job_scope: ArtifactScope,
    expected_input_binding_sha256: str,
    expected_authority_sha256: str,
    expected_editorial_exact_policy_sha256: str,
    expected_candidate_exact_policy_sha256: str,
    expected_stage3_member_refs: tuple[CommittedArtifactMemberReference, ...],
    expected_media_batch_member_ref: CommittedArtifactMemberReference,
    expected_timed_media_child_member_refs: tuple[
        tuple[CommittedArtifactMemberReference, ...], ...
    ],
    frozen_choice_order: tuple[PhysicalEditChoiceIdentity, ...],
    replay_evidence: PhysicalEditReplayEvidence,
) -> tuple[PhysicalEditAdmission, str]:
    if type(candidate_admission) is not PhysicalEditAdmission:  # noqa: E721
        raise PhysicalEditAdmissionError("trusted verifier requires an exact Admission candidate")
    if type(report) is not PhysicalEditCompilationReport:  # noqa: E721
        raise PhysicalEditAdmissionError("trusted verifier requires an exact compilation report")
    if type(expected_job_scope) is not ArtifactScope:  # noqa: E721
        raise PhysicalEditAdmissionError(
            "trusted verifier requires an exact independently read scope"
        )
    _require_pipeline_job_scope(expected_job_scope, "trusted verifier Job scope")
    expected_bindings = (
        _sha256(expected_input_binding_sha256, "trusted input binding"),
        _sha256(expected_authority_sha256, "trusted authority"),
        _sha256(expected_editorial_exact_policy_sha256, "trusted editorial exact policy"),
        _sha256(expected_candidate_exact_policy_sha256, "trusted candidate exact policy"),
    )
    report_bindings = (
        report.input_binding_sha256,
        report.authority_sha256,
        report.editorial_exact_policy_sha256,
        report.candidate_exact_policy_sha256,
    )
    candidate_bindings = (
        candidate_admission.input_binding_sha256,
        candidate_admission.authority_sha256,
        candidate_admission.editorial_exact_policy_sha256,
        candidate_admission.candidate_exact_policy_sha256,
    )
    if expected_bindings != report_bindings or expected_bindings != candidate_bindings:
        raise PhysicalEditAdmissionError(
            "physical edit bindings differ from independently resolved trusted values"
        )
    if (
        type(expected_stage3_member_refs) is not tuple  # noqa: E721
        or type(expected_media_batch_member_ref) is not CommittedArtifactMemberReference  # noqa: E721
        or type(expected_timed_media_child_member_refs) is not tuple  # noqa: E721
        or any(type(row) is not tuple for row in expected_timed_media_child_member_refs)  # noqa: E721
    ):
        raise PhysicalEditAdmissionError("trusted verifier predecessor closure is not exact")
    _validate_reference_group(expected_stage3_member_refs, "trusted Stage 3 references")
    expected_story_order = _stage3_story_ids(expected_stage3_member_refs)
    expected_backend: PhysicalEditBackend
    if expected_media_batch_member_ref.artifact_type == "timed_media_evidence_batch":
        expected_backend = "installed_cpu_profile"
    elif expected_media_batch_member_ref.artifact_type == "runtime_timed_media_evidence_batch":
        expected_backend = "runtime_cuda_capability"
    else:
        raise PhysicalEditAdmissionError("trusted media batch layout is unsupported")
    if (
        expected_stage3_member_refs != report.stage3_member_refs
        or expected_media_batch_member_ref != report.media_batch_member_ref
        or expected_timed_media_child_member_refs != report.timed_media_child_member_refs
        or expected_backend != report.backend_discriminator
    ):
        raise PhysicalEditAdmissionError(
            "compilation report predecessor closure differs from independently reread values"
        )
    all_expected_refs = (
        *expected_stage3_member_refs,
        expected_media_batch_member_ref,
        *(item for row in expected_timed_media_child_member_refs for item in row),
    )
    if any(item.scope != expected_job_scope for item in all_expected_refs):
        raise PhysicalEditAdmissionError("trusted predecessor closure crosses the exact Job scope")
    if (
        type(frozen_choice_order) is not tuple  # noqa: E721
        or not frozen_choice_order
        or any(type(item) is not PhysicalEditChoiceIdentity for item in frozen_choice_order)  # noqa: E721
    ):
        raise PhysicalEditAdmissionError("trusted verifier requires frozen Stage 3 choice order")
    report_choice_order = tuple(
        PhysicalEditChoiceIdentity.from_entry(item) for item in report.entries
    )
    if frozen_choice_order != report_choice_order:
        raise PhysicalEditAdmissionError(
            "compilation entries differ from independently reread frozen Stage 3 choices"
        )
    if _story_census(report.entries) != expected_story_order:
        raise PhysicalEditAdmissionError("compiled Story order differs from trusted Stage 3 order")
    if (
        type(recipe_subjects) is not tuple  # noqa: E721
        or any(type(item) is not PhysicalEditRecipeSubject for item in recipe_subjects)  # noqa: E721
        or any(item.scope != expected_job_scope for item in recipe_subjects)
        or tuple(item.story_id for item in recipe_subjects) != expected_story_order
    ):
        raise PhysicalEditAdmissionError(
            "Recipe subjects differ from trusted Stage 3 Story order or Job scope"
        )
    if type(replay_evidence) is not PhysicalEditReplayEvidence:  # noqa: E721
        raise PhysicalEditAdmissionError("trusted verifier requires exact replay evidence")
    expected_admission = build_physical_edit_admission(report, recipe_subjects, replay_evidence)
    if (
        expected_admission.validation_status != "valid"
        or expected_admission.next_action != "render"
        or any(item.status != "pass" for item in expected_admission.checks)
    ):
        raise PhysicalEditAdmissionError(
            "trusted verification cannot authorize a non-pass physical edit"
        )
    if (
        candidate_admission.to_mapping() != expected_admission.to_mapping()
        or candidate_admission.canonical_hash != expected_admission.canonical_hash
    ):
        raise PhysicalEditAdmissionError(
            "Admission candidate differs from independently recomputed Admission"
        )
    verification_binding = canonical_sha256(
        {
            "schema_version": "verified-physical-edit-admission-binding-v1",
            "candidate_admission_sha256": candidate_admission.canonical_hash,
            "compilation_report_sha256": report.canonical_hash,
            "expected_job_scope": {
                "namespace": expected_job_scope.namespace,
                "kind": expected_job_scope.kind,
                "key": expected_job_scope.key,
            },
            "expected_input_binding_sha256": expected_input_binding_sha256,
            "expected_authority_sha256": expected_authority_sha256,
            "expected_editorial_exact_policy_sha256": expected_editorial_exact_policy_sha256,
            "expected_candidate_exact_policy_sha256": expected_candidate_exact_policy_sha256,
            "expected_stage3_member_refs": [
                item.to_mapping() for item in expected_stage3_member_refs
            ],
            "expected_media_batch_member_ref": expected_media_batch_member_ref.to_mapping(),
            "expected_timed_media_child_member_refs": [
                [item.to_mapping() for item in row]
                for row in expected_timed_media_child_member_refs
            ],
            "frozen_choice_order": [item.to_mapping() for item in frozen_choice_order],
            "recipe_subjects": [item.to_mapping() for item in recipe_subjects],
            "replay_evidence": replay_evidence.to_mapping(),
        }
    )
    return expected_admission, verification_binding


@dataclass(frozen=True, slots=True)
class VerifiedPhysicalEditAdmission:
    """Non-serializable render capability closed over independently reread inputs.

    The type itself is not a security boundary.  Its constructor performs the
    same full verification as ``verify_physical_edit_admission``; consumers
    must obtain it at their trusted committed-reader boundary.
    """

    candidate_admission: PhysicalEditAdmission
    report: PhysicalEditCompilationReport
    recipe_subjects: tuple[PhysicalEditRecipeSubject, ...]
    expected_job_scope: ArtifactScope
    expected_input_binding_sha256: str
    expected_authority_sha256: str
    expected_editorial_exact_policy_sha256: str
    expected_candidate_exact_policy_sha256: str
    expected_stage3_member_refs: tuple[CommittedArtifactMemberReference, ...]
    expected_media_batch_member_ref: CommittedArtifactMemberReference
    expected_timed_media_child_member_refs: tuple[tuple[CommittedArtifactMemberReference, ...], ...]
    frozen_choice_order: tuple[PhysicalEditChoiceIdentity, ...]
    replay_evidence: PhysicalEditReplayEvidence
    verification_binding_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        verified, binding = _verify_trusted_physical_inputs(
            candidate_admission=self.candidate_admission,
            report=self.report,
            recipe_subjects=self.recipe_subjects,
            expected_job_scope=self.expected_job_scope,
            expected_input_binding_sha256=self.expected_input_binding_sha256,
            expected_authority_sha256=self.expected_authority_sha256,
            expected_editorial_exact_policy_sha256=self.expected_editorial_exact_policy_sha256,
            expected_candidate_exact_policy_sha256=self.expected_candidate_exact_policy_sha256,
            expected_stage3_member_refs=self.expected_stage3_member_refs,
            expected_media_batch_member_ref=self.expected_media_batch_member_ref,
            expected_timed_media_child_member_refs=self.expected_timed_media_child_member_refs,
            frozen_choice_order=self.frozen_choice_order,
            replay_evidence=self.replay_evidence,
        )
        if verified != self.candidate_admission:
            raise PhysicalEditAdmissionError("verified Admission value changed during verification")
        object.__setattr__(self, "verification_binding_sha256", binding)

    @property
    def admission(self) -> PhysicalEditAdmission:
        return self.candidate_admission

    @property
    def render_authorized(self) -> Literal[True]:
        return True


def verify_physical_edit_admission(
    candidate_admission: PhysicalEditAdmission,
    *,
    report: PhysicalEditCompilationReport,
    recipe_subjects: tuple[PhysicalEditRecipeSubject, ...],
    expected_job_scope: ArtifactScope,
    expected_input_binding_sha256: str,
    expected_authority_sha256: str,
    expected_editorial_exact_policy_sha256: str,
    expected_candidate_exact_policy_sha256: str,
    expected_stage3_member_refs: tuple[CommittedArtifactMemberReference, ...],
    expected_media_batch_member_ref: CommittedArtifactMemberReference,
    expected_timed_media_child_member_refs: tuple[
        tuple[CommittedArtifactMemberReference, ...], ...
    ],
    frozen_choice_order: tuple[PhysicalEditChoiceIdentity, ...],
    replay_evidence: PhysicalEditReplayEvidence,
) -> VerifiedPhysicalEditAdmission:
    """Authorize render only after exact trusted closure and canonical replay."""
    return VerifiedPhysicalEditAdmission(
        candidate_admission,
        report,
        recipe_subjects,
        expected_job_scope,
        expected_input_binding_sha256,
        expected_authority_sha256,
        expected_editorial_exact_policy_sha256,
        expected_candidate_exact_policy_sha256,
        expected_stage3_member_refs,
        expected_media_batch_member_ref,
        expected_timed_media_child_member_refs,
        frozen_choice_order,
        replay_evidence,
    )


__all__ = (
    "PHYSICAL_EDIT_ADMISSION_SCHEMA_VERSION",
    "PHYSICAL_EDIT_ADMISSION_STRATEGY_VERSION",
    "PHYSICAL_EDIT_COMPILATION_REPORT_SCHEMA_VERSION",
    "PHYSICAL_EDIT_COMPILATION_STRATEGY_VERSION",
    "PHYSICAL_EDIT_EXACT_SPAN_STRATEGY_VERSION",
    "PHYSICAL_EDIT_REPLAY_EVALUATOR_STRATEGY_VERSION",
    "PHYSICAL_EDIT_RULE_IDS",
    "PhysicalEditAdmission",
    "PhysicalEditAdmissionError",
    "PhysicalEditBackend",
    "PhysicalEditCheck",
    "PhysicalEditChoiceIdentity",
    "PhysicalEditCompilationAttempt",
    "PhysicalEditCompilationEntry",
    "PhysicalEditCompilationReport",
    "PhysicalEditRecipeSubject",
    "PhysicalEditReplayEvidence",
    "PhysicalEditReplayFact",
    "VerifiedPhysicalEditAdmission",
    "build_physical_edit_admission",
    "verify_physical_edit_admission",
)
