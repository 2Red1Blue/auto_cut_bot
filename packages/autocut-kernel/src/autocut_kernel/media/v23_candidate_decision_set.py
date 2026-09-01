"""Commit-ready aggregate of every V23 candidate-window compile decision.

The value is deliberately normalized and hash-bound rather than duplicating
the upstream SemanticPack, WindowManifest, or FramePtsIndex.  A later committed
reader must reread those exact artifacts and call the verifier below.  Decoding
this value restores immutable facts; it does not establish Store authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..vlm.semantic_pack_v4 import VlmSemanticPackV4
from ..vlm.window import WindowManifest
from .root_evidence import CanonicalEvidence, FramePtsIndexSet
from .timed_evidence import TimedEvidenceValidationError
from .types import MediaValidationError, TickRange, TimeBase, sha256_prefixed
from .v23_candidate_evidence_window import (
    V23CandidateWindowCompileDecision,
    V23CandidateWindowCompileOutcome,
    V23CandidateWindowCompilePolicy,
    compile_v23_candidate_evidence_window,
    validate_v23_candidate_window_compile_context,
)

V23_CANDIDATE_DECISION_SET_SCHEMA = "v23-candidate-decision-set-v1"


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise TimedEvidenceValidationError(f"{field_name} must be non-empty text")
    return value


def _sha(value: object, field_name: str) -> str:
    try:
        return sha256_prefixed(value, field_name)
    except MediaValidationError as error:
        raise TimedEvidenceValidationError(str(error)) from error


@dataclass(frozen=True, slots=True)
class V23CandidateDecisionSet(CanonicalEvidence):
    """One canonical decision for every candidate in one immutable V4 pack."""

    schema_version: str
    compile_policy: V23CandidateWindowCompilePolicy
    semantic_pack_sha256: str
    vlm_request_identity_sha256: str
    window_manifest_sha256: str
    frame_pts_index_set_sha256: str
    source_id: str
    source_sha256: str
    source_clock_id: str
    source_time_base: TimeBase
    stream_index: int
    source_range: TickRange
    candidate_ids: tuple[str, ...]
    decisions: tuple[V23CandidateWindowCompileDecision, ...]

    def __post_init__(self) -> None:
        if self.schema_version != V23_CANDIDATE_DECISION_SET_SCHEMA:
            raise TimedEvidenceValidationError(
                "candidate decision set schema_version is unsupported"
            )
        if type(self.compile_policy) is not V23CandidateWindowCompilePolicy:  # noqa: E721
            raise TimedEvidenceValidationError(
                "candidate decision set compile_policy must be exact"
            )
        for field_name in (
            "semantic_pack_sha256",
            "vlm_request_identity_sha256",
            "window_manifest_sha256",
            "frame_pts_index_set_sha256",
            "source_sha256",
        ):
            _sha(getattr(self, field_name), f"decision_set.{field_name}")
        _text(self.source_id, "decision_set.source_id")
        _text(self.source_clock_id, "decision_set.source_clock_id")
        if type(self.source_time_base) is not TimeBase:  # noqa: E721
            raise TimedEvidenceValidationError(
                "candidate decision set source_time_base must be exact"
            )
        if type(self.stream_index) is not int or self.stream_index < 0:  # noqa: E721
            raise TimedEvidenceValidationError(
                "candidate decision set stream_index must be a non-negative integer"
            )
        if type(self.source_range) is not TickRange:  # noqa: E721
            raise TimedEvidenceValidationError("candidate decision set source_range must be exact")

        candidate_ids = tuple(self.candidate_ids)
        if any(type(item) is not str for item in candidate_ids):  # noqa: E721
            raise TimedEvidenceValidationError(
                "candidate decision set candidate_ids must contain hashes"
            )
        for position, candidate_id in enumerate(candidate_ids):
            _sha(candidate_id, f"decision_set.candidate_ids[{position}]")
        if candidate_ids != tuple(sorted(candidate_ids)) or len(candidate_ids) != len(
            set(candidate_ids)
        ):
            raise TimedEvidenceValidationError(
                "candidate decision set candidate_ids must be sorted and unique"
            )
        object.__setattr__(self, "candidate_ids", candidate_ids)

        decisions = tuple(self.decisions)
        if any(type(item) is not V23CandidateWindowCompileDecision for item in decisions):  # noqa: E721
            raise TimedEvidenceValidationError(
                "candidate decision set decisions must contain exact values"
            )
        object.__setattr__(self, "decisions", decisions)
        if tuple(item.candidate_id for item in decisions) != candidate_ids:
            raise TimedEvidenceValidationError(
                "candidate decision set must contain exactly one ordered decision per candidate"
            )
        expected_bindings = (
            self.compile_policy.canonical_hash,
            self.semantic_pack_sha256,
            self.vlm_request_identity_sha256,
            self.window_manifest_sha256,
            self.frame_pts_index_set_sha256,
            self.source_id,
            self.source_sha256,
            self.source_clock_id,
            self.source_time_base,
            self.source_range,
        )
        if any(
            (
                item.policy_sha256,
                item.semantic_pack_sha256,
                item.vlm_request_identity_sha256,
                item.window_manifest_sha256,
                item.frame_pts_index_set_sha256,
                item.source_id,
                item.source_sha256,
                item.source_clock_id,
                item.source_time_base,
                item.source_range,
            )
            != expected_bindings
            for item in decisions
        ):
            raise TimedEvidenceValidationError(
                "candidate decisions do not have the exact decision-set bindings"
            )

    @property
    def eligible_count(self) -> int:
        return sum(
            item.outcome is V23CandidateWindowCompileOutcome.ELIGIBLE for item in self.decisions
        )

    @property
    def noneligible_count(self) -> int:
        return len(self.decisions) - self.eligible_count

    @property
    def decision_set_id(self) -> str:
        return self.canonical_hash


def compile_v23_candidate_decision_set(
    semantic_pack: VlmSemanticPackV4,
    window_manifest: WindowManifest,
    frame_pts_index: FramePtsIndexSet,
    policy: V23CandidateWindowCompilePolicy,
) -> V23CandidateDecisionSet:
    """Compile all candidates, retaining every nonlocal routing decision."""

    source_range = validate_v23_candidate_window_compile_context(
        semantic_pack, window_manifest, frame_pts_index, policy
    )
    decisions = tuple(
        sorted(
            (
                compile_v23_candidate_evidence_window(
                    candidate,
                    semantic_pack,
                    window_manifest,
                    frame_pts_index,
                    policy,
                )
                for candidate in semantic_pack.candidate_hypotheses
            ),
            key=lambda item: item.candidate_id,
        )
    )
    context = frame_pts_index.context
    return V23CandidateDecisionSet(
        schema_version=V23_CANDIDATE_DECISION_SET_SCHEMA,
        compile_policy=policy,
        semantic_pack_sha256=semantic_pack.canonical_hash,
        vlm_request_identity_sha256=semantic_pack.request_identity_sha256,
        window_manifest_sha256=window_manifest.canonical_hash,
        frame_pts_index_set_sha256=frame_pts_index.canonical_hash,
        source_id=context.source_id,
        source_sha256=context.source_sha256,
        source_clock_id=context.clock_id,
        source_time_base=context.time_base,
        stream_index=window_manifest.stream_index,
        source_range=source_range,
        candidate_ids=tuple(item.candidate_id for item in decisions),
        decisions=decisions,
    )


def verify_v23_candidate_decision_set(
    decision_set: V23CandidateDecisionSet,
    semantic_pack: VlmSemanticPackV4,
    window_manifest: WindowManifest,
    frame_pts_index: FramePtsIndexSet,
    policy: V23CandidateWindowCompilePolicy,
) -> V23CandidateDecisionSet:
    """Reread-time verification against the exact committed dependencies."""

    if type(decision_set) is not V23CandidateDecisionSet:  # noqa: E721
        raise TimedEvidenceValidationError("decision_set must be a V23CandidateDecisionSet")
    if type(semantic_pack) is not VlmSemanticPackV4:  # noqa: E721
        raise TimedEvidenceValidationError("semantic pack must be exact")
    if decision_set.semantic_pack_sha256 != semantic_pack.canonical_hash:
        raise TimedEvidenceValidationError(
            "candidate decision set semantic pack differs from the committed input"
        )
    expected = compile_v23_candidate_decision_set(
        semantic_pack, window_manifest, frame_pts_index, policy
    )
    if decision_set != expected:
        raise TimedEvidenceValidationError(
            "candidate decision set does not match independent recomputation"
        )
    return decision_set


__all__ = [
    "V23_CANDIDATE_DECISION_SET_SCHEMA",
    "V23CandidateDecisionSet",
    "compile_v23_candidate_decision_set",
    "verify_v23_candidate_decision_set",
]
