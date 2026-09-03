"""Project admitted editorial intent into one candidate-local exact A/V query.

This module is deliberately pure.  It does not choose a Stage 3 material,
read Store state, run the exact selector, or grant physical Admission.  Its
only job is to turn the already selected semantic objects into the closed
``ExactAvSpanRequest`` consumed by the candidate-local compiler.

The VLM anchor remains semantic and coarse.  It is never copied into a Recipe
as a physical endpoint: the downstream compiler must still select decoded
video PTS and audio sample boundaries from committed media evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from ..contracts.compiler.canonical import canonical_json_hash
from ..media.timed_evidence import CandidateTimedEvidenceSet
from ..media.types import TickRange, TimeBase, canonical_sha256, require_pts
from ..semantic_chain.candidate_catalog import Candidate
from ..semantic_chain.editorial_blueprint import (
    EDITORIAL_BLUEPRINT_STRATEGY_VERSION,
    BlueprintEvidenceRequirement,
    EditorialBlueprintBeat,
)
from ..semantic_chain.editorial_material_search import MaterialSearchChoice
from ..semantic_chain.editorial_models import EvidenceAlternative, SpanIntent
from ..semantic_chain.member_refs import SemanticObjectRef
from ..vlm.models import VlmCandidateHypothesis, VlmEditingMode, VlmSemanticPack
from .dialogue_guard import DialogueRequirement
from .exact_span import ExactAvSpanRequest, VideoClockRange

EDITORIAL_EXACT_SPAN_STRATEGY = "editorial-exact-span-v1"


class EditorialExactSpanError(ValueError):
    """The semantic selection cannot produce a closed physical query."""


class EditorialExactSpanIndeterminateError(EditorialExactSpanError):
    """Evidence is valid but insufficient for the requested span intent."""


@dataclass(frozen=True, slots=True)
class EditorialExactSpanPolicy:
    """Explicit semantic-width policy; safety/search limits live elsewhere."""

    strategy_version: str
    context_maximum_extension_tick: int
    context_maximum_extension_time_base: TimeBase

    def __post_init__(self) -> None:
        if self.strategy_version != EDITORIAL_EXACT_SPAN_STRATEGY:
            raise EditorialExactSpanError("unsupported editorial exact-span strategy")
        if require_pts(
            self.context_maximum_extension_tick,
            "context_maximum_extension_tick",
        ) <= 0:
            raise EditorialExactSpanError("context maximum extension must be positive")
        if type(self.context_maximum_extension_time_base) is not TimeBase:  # noqa: E721
            raise EditorialExactSpanError("context maximum extension requires an exact TimeBase")

    def to_mapping(self) -> dict[str, object]:
        base = self.context_maximum_extension_time_base
        return {
            "strategy_version": self.strategy_version,
            "context_maximum_extension": {
                "tick": self.context_maximum_extension_tick,
                "time_base": {
                    "numerator": base.numerator,
                    "denominator": base.denominator,
                },
            },
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class EditorialExactSpanQuery:
    """Auditable projection result; still not a selected or admitted span."""

    story_id: str
    beat_id: str
    evidence_requirement_id: str
    alternative_id: str
    candidate_id: str
    anchor_event_id: str
    anchor_event_sha256: str
    span_intent: SpanIntent
    dominant_editing_mode: VlmEditingMode
    policy_sha256: str
    blueprint_beat_sha256: str
    evidence_requirement_sha256: str
    alternative_sha256: str
    catalog_candidate_sha256: str
    semantic_pack_sha256: str
    timed_evidence_sha256: str
    dialogue_protection_kind: Literal["known_speech", "complete_dialogue"]
    request: ExactAvSpanRequest

    def __post_init__(self) -> None:
        values = (
            self.story_id,
            self.beat_id,
            self.evidence_requirement_id,
            self.alternative_id,
            self.candidate_id,
            self.anchor_event_id,
            self.anchor_event_sha256,
            self.policy_sha256,
            self.blueprint_beat_sha256,
            self.evidence_requirement_sha256,
            self.alternative_sha256,
            self.catalog_candidate_sha256,
            self.semantic_pack_sha256,
            self.timed_evidence_sha256,
        )
        if any(type(value) is not str or not value for value in values):  # noqa: E721
            raise EditorialExactSpanError("editorial query identities must be non-empty strings")
        if self.span_intent not in ("tight", "scene", "context"):
            raise EditorialExactSpanError("editorial query span intent is unsupported")
        if type(self.dominant_editing_mode) is not VlmEditingMode:  # noqa: E721
            raise EditorialExactSpanError("editorial query editing mode must retain the VLM enum")
        if type(self.request) is not ExactAvSpanRequest:  # noqa: E721
            raise EditorialExactSpanError("editorial query requires an exact A/V request")
        expected = (
            "complete_dialogue"
            if self.request.dialogue_requirement is DialogueRequirement.COMPLETE
            else "known_speech"
        )
        if self.dialogue_protection_kind != expected:
            raise EditorialExactSpanError("dialogue protection kind contradicts its exact request")

    def to_mapping(self) -> dict[str, object]:
        return {
            "strategy_version": EDITORIAL_EXACT_SPAN_STRATEGY,
            "story_id": self.story_id,
            "beat_id": self.beat_id,
            "evidence_requirement_id": self.evidence_requirement_id,
            "alternative_id": self.alternative_id,
            "candidate_id": self.candidate_id,
            "anchor_event_id": self.anchor_event_id,
            "anchor_event_sha256": self.anchor_event_sha256,
            "span_intent": self.span_intent,
            "dominant_editing_mode": self.dominant_editing_mode.value,
            "policy_sha256": self.policy_sha256,
            "blueprint_beat_sha256": self.blueprint_beat_sha256,
            "evidence_requirement_sha256": self.evidence_requirement_sha256,
            "alternative_sha256": self.alternative_sha256,
            "catalog_candidate_sha256": self.catalog_candidate_sha256,
            "semantic_pack_sha256": self.semantic_pack_sha256,
            "timed_evidence_sha256": self.timed_evidence_sha256,
            "dialogue_protection_kind": self.dialogue_protection_kind,
            "request": self.request.to_mapping(),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def _ceil_fraction(value: Fraction) -> int:
    return (value.numerator + value.denominator - 1) // value.denominator


def minimum_video_ticks(seconds: int, time_base: TimeBase) -> int:
    """Convert whole seconds to the smallest covering number of native ticks."""
    if type(seconds) is not int or seconds <= 0:  # noqa: E721
        raise EditorialExactSpanError("minimum usable seconds must be a positive integer")
    if type(time_base) is not TimeBase:  # noqa: E721
        raise EditorialExactSpanError("minimum duration requires an exact video TimeBase")
    return _ceil_fraction(Fraction(seconds * time_base.denominator, time_base.numerator))


def _maximum_extension_ticks(policy: EditorialExactSpanPolicy, time_base: TimeBase) -> int:
    source = policy.context_maximum_extension_time_base
    duration = Fraction(policy.context_maximum_extension_tick * source.numerator, source.denominator)
    # This is a maximum, so floor rather than ceil.  Rounding upward would
    # silently exceed the frozen wall-clock policy.
    ticks = (duration / Fraction(time_base.numerator, time_base.denominator)).numerator // (
        duration / Fraction(time_base.numerator, time_base.denominator)
    ).denominator
    if ticks <= 0:
        raise EditorialExactSpanIndeterminateError(
            "context extension is smaller than one target video tick"
        )
    return ticks


def _scene_anchor(evidence: CandidateTimedEvidenceSet, anchor: TickRange) -> TickRange:
    scenes = evidence.scene_boundaries
    edges = tuple(sorted({
        scenes.coverage.in_tick,
        scenes.coverage.out_tick,
        *(point.tick for point in scenes.points),
    }))
    matches = tuple(
        TickRange(start, end)
        for start, end in zip(edges, edges[1:], strict=False)
        if start <= anchor.start_pts and anchor.end_pts <= end
    )
    if len(matches) != 1:
        raise EditorialExactSpanIndeterminateError(
            "anchor event is not contained by one proven scene segment"
        )
    scene = matches[0]
    if not evidence.candidate_window.current_range.contains(scene):
        raise EditorialExactSpanIndeterminateError(
            "candidate-local evidence does not cover the complete anchor scene"
        )
    return scene


def _context_anchor(
    evidence: CandidateTimedEvidenceSet,
    anchor: TickRange,
    policy: EditorialExactSpanPolicy,
) -> TickRange:
    window = evidence.candidate_window.current_range
    extension = _maximum_extension_ticks(policy, evidence.candidate_window.source_time_base)
    expanded = TickRange(
        max(window.start_pts, anchor.start_pts - extension),
        min(window.end_pts, anchor.end_pts + extension),
    )
    if expanded == anchor:
        raise EditorialExactSpanIndeterminateError(
            "candidate-local evidence contains no additional context"
        )
    return expanded


def _source_is_allowed(candidate: Candidate, requirement: BlueprintEvidenceRequirement) -> bool:
    constraints = requirement.source_constraints
    return candidate.source_ref not in constraints.forbidden_source_refs and (
        not constraints.allowed_source_refs or candidate.source_ref in constraints.allowed_source_refs
    )


def _dialogue_requirement(requirement: BlueprintEvidenceRequirement) -> DialogueRequirement:
    return (
        DialogueRequirement.COMPLETE
        if any(
            item.requirement_kind == "dialogue_integrity" and item.mode == "complete"
            for item in requirement.physical_requirements
        )
        else DialogueRequirement.NOT_REQUIRED
    )


def _dominant_mode(modes: tuple[VlmEditingMode, ...]) -> VlmEditingMode:
    # Dialogue dominance is semantic selection policy.  ASR/VAD remain only
    # physical boundary evidence and never invent or change these VLM modes.
    return VlmEditingMode.DIALOGUE if VlmEditingMode.DIALOGUE in modes else VlmEditingMode.ACTION


def _confidence_text(value: object) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def derive_editorial_exact_span_query(
    *,
    admitted_choice: MaterialSearchChoice,
    beat: EditorialBlueprintBeat,
    requirement: BlueprintEvidenceRequirement,
    alternative: EvidenceAlternative,
    selected_candidate_ref: SemanticObjectRef,
    candidate: Candidate,
    semantic_pack: VlmSemanticPack,
    raw_candidate: VlmCandidateHypothesis,
    timed_evidence: CandidateTimedEvidenceSet,
    span_intent: SpanIntent,
    policy: EditorialExactSpanPolicy,
) -> EditorialExactSpanQuery:
    """Derive one closed query from exact semantic and candidate-local inputs.

    The direct anchor event, rather than the candidate's wider context/payoff
    support, owns the semantic anchor.  ``scene`` and ``context`` modify only
    the intended semantic width.  Every final endpoint is still selected by
    ``compile_candidate_av_span`` from physical indexes.
    """
    if (
        type(beat) is not EditorialBlueprintBeat  # noqa: E721
        or type(admitted_choice) is not MaterialSearchChoice  # noqa: E721
        or type(requirement) is not BlueprintEvidenceRequirement  # noqa: E721
        or type(alternative) is not EvidenceAlternative  # noqa: E721
        or type(selected_candidate_ref) is not SemanticObjectRef  # noqa: E721
        or type(candidate) is not Candidate  # noqa: E721
        or type(semantic_pack) is not VlmSemanticPack  # noqa: E721
        or type(raw_candidate) is not VlmCandidateHypothesis  # noqa: E721
        or type(timed_evidence) is not CandidateTimedEvidenceSet  # noqa: E721
        or type(policy) is not EditorialExactSpanPolicy  # noqa: E721
    ):
        raise EditorialExactSpanError("query derivation requires exact typed inputs")
    if requirement not in beat.evidence_requirements or alternative not in requirement.alternatives:
        raise EditorialExactSpanError("query selection is outside its Blueprint requirement")
    expected_beat_id = canonical_json_hash({
        "schema_version": "stage3-editorial-beat-id-v1",
        "story_id": admitted_choice.story_id,
        "strategy_version": EDITORIAL_BLUEPRINT_STRATEGY_VERSION,
        "ordinal": beat.ordinal,
    })
    if (
        beat.beat_id != expected_beat_id
        or (
            admitted_choice.requirement_id,
            admitted_choice.alternative_key,
        )
        != (requirement.evidence_requirement_id, alternative.alternative_id)
        or selected_candidate_ref.canonical_hash not in admitted_choice.candidate_keys
        or selected_candidate_ref not in alternative.candidate_refs
        or selected_candidate_ref.member_ref.artifact_type != "candidate_catalog"
        or selected_candidate_ref.object_type != "candidate"
        or selected_candidate_ref.object_id != candidate.candidate_id
    ):
        raise EditorialExactSpanError("query candidate is outside the selected alternative")
    if span_intent not in beat.span_policy.allowed:
        raise EditorialExactSpanError("query span intent is not allowed by the Blueprint")
    if not _source_is_allowed(candidate, requirement):
        raise EditorialExactSpanError("query candidate Source is forbidden by the Blueprint")

    raw_hash = canonical_sha256(raw_candidate.to_mapping())
    window = timed_evidence.candidate_window
    if (
        raw_candidate not in semantic_pack.candidate_hypotheses
        or raw_candidate.candidate_id != candidate.candidate_id
        or raw_candidate.local_candidate_id != candidate.local_candidate_id
        or tuple(mode.value for mode in raw_candidate.editing_modes) != candidate.editing_modes
        or raw_candidate.support.proxy_interval != candidate.support.proxy_interval
        or raw_candidate.support.source_interval != candidate.support.source_interval
        or raw_candidate.support.supporting_frame_ids != candidate.support.supporting_frame_ids
        or _confidence_text(raw_candidate.support.confidence) != candidate.support.confidence
        or raw_hash != window.vlm_candidate_sha256
        or semantic_pack.request_identity_sha256 != window.vlm_request_identity_sha256
        or semantic_pack.window_manifest_sha256 != window.window_manifest_sha256
        or candidate.source_window_ref.object_id != window.window_manifest_sha256
        or candidate.source_ref.object_id != window.source_id
        or raw_candidate.support.core_owner_window_manifest_sha256 != window.window_manifest_sha256
    ):
        raise EditorialExactSpanError("candidate, VLM pack and timed evidence owners differ")

    events = tuple(event for event in semantic_pack.events if event.event_id == raw_candidate.anchor_event_ref)
    if (
        len(events) != 1
        or candidate.anchor_event.vlm_event_ref.object_id != raw_candidate.anchor_event_ref
    ):
        raise EditorialExactSpanError("candidate has no unique direct anchor event")
    event = events[0]
    anchor = event.support.source_interval.coarse_range
    if (
        event.support.core_owner_window_manifest_sha256 != window.window_manifest_sha256
        or event.support.source_interval.source_time_base != window.source_time_base
        or not window.current_range.contains(anchor)
    ):
        raise EditorialExactSpanError("direct anchor event is outside candidate-local Source evidence")

    intent = span_intent
    if intent == "scene":
        anchor = _scene_anchor(timed_evidence, anchor)
    elif intent == "context":
        anchor = _context_anchor(timed_evidence, anchor, policy)

    context = timed_evidence.frame_pts_index.context
    if (
        context.source_id != window.source_id
        or context.source_sha256 != window.source_sha256
        or context.clock_id != window.source_clock_id
        or context.time_base != window.source_time_base
    ):
        raise EditorialExactSpanError("candidate-local frame clock differs from its Source window")
    def bound(value: TickRange) -> VideoClockRange:
        return VideoClockRange(
            context.source_id,
            context.source_sha256,
            context.clock_id,
            context.time_base,
            value,
        )
    request = ExactAvSpanRequest(
        bound(window.current_range),
        bound(anchor),
        minimum_video_ticks(requirement.minimum_usable_seconds, context.time_base),
        _dialogue_requirement(requirement),
    )
    return EditorialExactSpanQuery(
        admitted_choice.story_id,
        beat.beat_id,
        requirement.evidence_requirement_id,
        alternative.alternative_id,
        candidate.candidate_id,
        event.event_id,
        canonical_sha256(event.to_mapping()),
        intent,
        _dominant_mode(raw_candidate.editing_modes),
        policy.canonical_hash,
        canonical_sha256(beat.to_mapping()),
        canonical_sha256(requirement.to_mapping()),
        canonical_sha256(alternative.to_mapping()),
        canonical_sha256(candidate.to_mapping()),
        semantic_pack.canonical_hash,
        timed_evidence.canonical_hash,
        (
            "complete_dialogue"
            if request.dialogue_requirement is DialogueRequirement.COMPLETE
            else "known_speech"
        ),
        request,
    )


__all__ = [
    "EDITORIAL_EXACT_SPAN_STRATEGY",
    "EditorialExactSpanError",
    "EditorialExactSpanIndeterminateError",
    "EditorialExactSpanPolicy",
    "EditorialExactSpanQuery",
    "derive_editorial_exact_span_query",
    "minimum_video_ticks",
]
