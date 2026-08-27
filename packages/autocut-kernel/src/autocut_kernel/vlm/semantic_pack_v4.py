"""Independent Semantic Pack v4 values; video observations are not frame evidence.

The semantic graph invariants remain explicit here rather than manufacturing
v3 support values. Only support-free enums, summaries and measurements are shared.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..media.types import canonical_sha256, sha256_prefixed
from .models import (
    VlmCandidateKind,
    VlmCandidateTag,
    VlmEditingMode,
    VlmEntityKind,
    VlmEventKind,
    VlmFactKind,
    VlmNarrativeFunction,
    VlmSemanticMeasurement,
    VlmTemporalMode,
    VlmValidationError,
    VlmWindowSummary,
    # Deliberate reuse of pure validators from the immutable v3 source bundle.
    _canonical_refs,  # pyright: ignore[reportPrivateUsage]
    _enum,  # pyright: ignore[reportPrivateUsage]
    _local_id,  # pyright: ignore[reportPrivateUsage]
    _text,  # pyright: ignore[reportPrivateUsage]
    derive_vlm_global_id,
)
from .semantic_support_v4 import (
    FrameAnchoredObservationSupportV4,
    SemanticSupportV4,
    VideoObservationSupportV4,
)


def _require_support(value: object) -> None:
    if type(value) not in (VideoObservationSupportV4, FrameAnchoredObservationSupportV4):
        raise VlmValidationError("support must be an exact v4 video or frame observation")


def _observation_ranges_overlap(left: SemanticSupportV4, right: SemanticSupportV4) -> bool:
    # Every support in a pack is bound to the same exact window. Its original
    # milliseconds therefore share one clock; outward-rounded coarse media
    # ranges are for locating evidence, not for establishing semantic overlap.
    left_range = left.interval_ms
    right_range = right.interval_ms
    return max(left_range.start_ms, right_range.start_ms) < min(left_range.end_ms, right_range.end_ms)


@dataclass(frozen=True, slots=True)
class VlmTemporalSegmentV4:
    mode: VlmTemporalMode
    summary: str
    support: SemanticSupportV4

    def __post_init__(self) -> None:
        _enum(self.mode, VlmTemporalMode, "temporal_segment.mode")
        _text(self.summary, "temporal_segment.summary")
        _require_support(self.support)

    def to_mapping(self) -> dict[str, object]:
        return {"mode": self.mode.value, "summary": self.summary, "support": self.support.to_mapping()}


@dataclass(frozen=True, slots=True)
class VlmContinuityV4:
    starts_mid_event: bool
    ends_mid_event: bool
    continues_from_previous: bool
    continues_into_next: bool
    entry_state_fact_refs: tuple[str, ...]
    exit_state_fact_refs: tuple[str, ...]
    temporal_segments: tuple[VlmTemporalSegmentV4, ...]

    def __post_init__(self) -> None:
        bool_fields = (
            "starts_mid_event",
            "ends_mid_event",
            "continues_from_previous",
            "continues_into_next",
        )
        for field_name in bool_fields:
            if type(getattr(self, field_name)) is not bool:  # noqa: E721
                raise VlmValidationError(f"continuity.{field_name} must be boolean")
        object.__setattr__(
            self,
            "entry_state_fact_refs",
            _canonical_refs(self.entry_state_fact_refs, "continuity.entry_state_fact_refs"),
        )
        object.__setattr__(
            self,
            "exit_state_fact_refs",
            _canonical_refs(self.exit_state_fact_refs, "continuity.exit_state_fact_refs"),
        )
        segments = tuple(self.temporal_segments)
        if any(type(item) is not VlmTemporalSegmentV4 for item in segments):  # noqa: E721
            raise VlmValidationError(
                "continuity.temporal_segments must contain VlmTemporalSegmentV4 values"
            )
        segment_keys = tuple(
            (
                item.support.interval_ms.start_ms,
                item.support.interval_ms.end_ms,
            )
            for item in segments
        )
        if segment_keys != tuple(sorted(segment_keys)):
            raise VlmValidationError(
                "continuity.temporal_segments must be in canonical proxy interval order"
            )
        for left, right in zip(segments, segments[1:], strict=False):
            if (
                left.support.interval_ms.end_ms
                > right.support.interval_ms.start_ms
            ):
                raise VlmValidationError(
                    "continuity.temporal_segments must not overlap in proxy time"
                )
        if self.starts_mid_event != self.continues_from_previous:
            raise VlmValidationError(
                "continuity starts_mid_event must equal continues_from_previous"
            )
        if self.ends_mid_event != self.continues_into_next:
            raise VlmValidationError("continuity ends_mid_event must equal continues_into_next")
        if bool(self.entry_state_fact_refs) != self.continues_from_previous:
            raise VlmValidationError(
                "continuity entry_state_fact_refs must be non-empty exactly when "
                "continues_from_previous is true"
            )
        if bool(self.exit_state_fact_refs) != self.continues_into_next:
            raise VlmValidationError(
                "continuity exit_state_fact_refs must be non-empty exactly when "
                "continues_into_next is true"
            )
        object.__setattr__(self, "temporal_segments", segments)

    def to_mapping(self) -> dict[str, object]:
        return {
            "continues_from_previous": self.continues_from_previous,
            "continues_into_next": self.continues_into_next,
            "ends_mid_event": self.ends_mid_event,
            "entry_state_fact_refs": list(self.entry_state_fact_refs),
            "exit_state_fact_refs": list(self.exit_state_fact_refs),
            "starts_mid_event": self.starts_mid_event,
            "temporal_segments": [item.to_mapping() for item in self.temporal_segments],
        }


@dataclass(frozen=True, slots=True)
class VlmEntityV4:
    entity_id: str
    local_entity_id: str
    entity_kind: VlmEntityKind
    display_label: str
    visual_description: str
    support: SemanticSupportV4

    def __post_init__(self) -> None:
        sha256_prefixed(self.entity_id, "entity.entity_id")
        _local_id(self.local_entity_id, "entity.local_entity_id")
        _enum(self.entity_kind, VlmEntityKind, "entity.entity_kind")
        _text(self.display_label, "entity.display_label")
        _text(self.visual_description, "entity.visual_description")
        _require_support(self.support)

    def to_mapping(self) -> dict[str, object]:
        return {
            "display_label": self.display_label,
            "entity_id": self.entity_id,
            "entity_kind": self.entity_kind.value,
            "local_entity_id": self.local_entity_id,
            "support": self.support.to_mapping(),
            "visual_description": self.visual_description,
        }


@dataclass(frozen=True, slots=True)
class VlmFactV4:
    fact_id: str
    local_fact_id: str
    fact_kind: VlmFactKind
    subject_ref: str
    object_ref: str | None
    summary: str
    support: SemanticSupportV4

    def __post_init__(self) -> None:
        sha256_prefixed(self.fact_id, "fact.fact_id")
        _local_id(self.local_fact_id, "fact.local_fact_id")
        _enum(self.fact_kind, VlmFactKind, "fact.fact_kind")
        sha256_prefixed(self.subject_ref, "fact.subject_ref")
        if self.object_ref is not None:
            sha256_prefixed(self.object_ref, "fact.object_ref")
        _text(self.summary, "fact.summary")
        _require_support(self.support)

    def to_mapping(self) -> dict[str, object]:
        return {
            "fact_id": self.fact_id,
            "fact_kind": self.fact_kind.value,
            "local_fact_id": self.local_fact_id,
            "object_ref": self.object_ref,
            "subject_ref": self.subject_ref,
            "summary": self.summary,
            "support": self.support.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class VlmEventV4:
    event_id: str
    local_event_id: str
    event_kind: VlmEventKind
    summary: str
    participant_refs: tuple[str, ...]
    fact_refs: tuple[str, ...]
    cause_event_refs: tuple[str, ...]
    effect_event_refs: tuple[str, ...]
    open_question: str | None
    temporal_mode: VlmTemporalMode
    support: SemanticSupportV4

    def __post_init__(self) -> None:
        sha256_prefixed(self.event_id, "event.event_id")
        _local_id(self.local_event_id, "event.local_event_id")
        _enum(self.event_kind, VlmEventKind, "event.event_kind")
        _text(self.summary, "event.summary")
        ref_fields = (
            "participant_refs",
            "fact_refs",
            "cause_event_refs",
            "effect_event_refs",
        )
        for field_name in ref_fields:
            object.__setattr__(
                self,
                field_name,
                _canonical_refs(getattr(self, field_name), f"event.{field_name}"),
            )
        if not self.fact_refs:
            raise VlmValidationError("event.fact_refs must be non-empty")
        _text(self.open_question, "event.open_question", nullable=True)
        _enum(self.temporal_mode, VlmTemporalMode, "event.temporal_mode")
        _require_support(self.support)

    def to_mapping(self) -> dict[str, object]:
        return {
            "cause_event_refs": list(self.cause_event_refs),
            "effect_event_refs": list(self.effect_event_refs),
            "event_id": self.event_id,
            "event_kind": self.event_kind.value,
            "fact_refs": list(self.fact_refs),
            "local_event_id": self.local_event_id,
            "open_question": self.open_question,
            "participant_refs": list(self.participant_refs),
            "summary": self.summary,
            "support": self.support.to_mapping(),
            "temporal_mode": self.temporal_mode.value,
        }


@dataclass(frozen=True, slots=True)
class VlmCandidateHypothesisV4:
    candidate_id: str
    local_candidate_id: str
    candidate_kind: VlmCandidateKind
    anchor_event_ref: str
    supporting_event_refs: tuple[str, ...]
    context_event_refs: tuple[str, ...]
    payoff_event_refs: tuple[str, ...]
    open_question: str | None
    reason: str
    anchor_summary: str
    payoff_or_open_question: str
    dialogue_excerpt: str | None
    editing_modes: tuple[VlmEditingMode, ...]
    narrative_functions: tuple[VlmNarrativeFunction, ...]
    tags: tuple[VlmCandidateTag, ...]
    measurements: tuple[VlmSemanticMeasurement, ...]
    support: SemanticSupportV4

    def __post_init__(self) -> None:
        sha256_prefixed(self.candidate_id, "candidate.candidate_id")
        _local_id(self.local_candidate_id, "candidate.local_candidate_id")
        _enum(self.candidate_kind, VlmCandidateKind, "candidate.candidate_kind")
        sha256_prefixed(self.anchor_event_ref, "candidate.anchor_event_ref")
        ref_fields = (
            "supporting_event_refs",
            "context_event_refs",
            "payoff_event_refs",
        )
        for field_name in ref_fields:
            object.__setattr__(
                self,
                field_name,
                _canonical_refs(getattr(self, field_name), f"candidate.{field_name}"),
            )
        _text(self.open_question, "candidate.open_question", nullable=True)
        _text(self.reason, "candidate.reason")
        _text(self.anchor_summary, "candidate.anchor_summary")
        _text(self.payoff_or_open_question, "candidate.payoff_or_open_question")
        _text(self.dialogue_excerpt, "candidate.dialogue_excerpt", nullable=True)
        enum_fields = (
            ("editing_modes", VlmEditingMode),
            ("narrative_functions", VlmNarrativeFunction),
            ("tags", VlmCandidateTag),
        )
        for field_name, enum_type in enum_fields:
            values = tuple(getattr(self, field_name))
            if not values or len(values) != len(set(values)):
                raise VlmValidationError(f"candidate.{field_name} must be non-empty and unique")
            if any(type(item) is not enum_type for item in values):  # noqa: E721
                raise VlmValidationError(
                    f"candidate.{field_name} must contain {enum_type.__name__} values"
                )
            expected = tuple(item for item in enum_type if item in values)
            if values != expected:
                raise VlmValidationError(f"candidate.{field_name} is not in canonical order")
            object.__setattr__(self, field_name, values)
        measurements = tuple(self.measurements)
        if any(type(item) is not VlmSemanticMeasurement for item in measurements):  # noqa: E721
            raise VlmValidationError(
                "candidate.measurements must contain VlmSemanticMeasurement values"
            )
        object.__setattr__(self, "measurements", measurements)
        if not measurements:
            raise VlmValidationError("candidate.measurements must be non-empty")
        _require_support(self.support)
        if self.candidate_kind is VlmCandidateKind.HOOK:
            if self.open_question is None or self.payoff_event_refs:
                raise VlmValidationError(
                    "hook candidate requires open_question and empty payoff_event_refs"
                )
        elif not self.payoff_event_refs:
            raise VlmValidationError("highlight candidate requires non-empty payoff_event_refs")

    def to_mapping(self) -> dict[str, object]:
        return {
            "anchor_event_ref": self.anchor_event_ref,
            "anchor_summary": self.anchor_summary,
            "candidate_id": self.candidate_id,
            "candidate_kind": self.candidate_kind.value,
            "context_event_refs": list(self.context_event_refs),
            "dialogue_excerpt": self.dialogue_excerpt,
            "editing_modes": [item.value for item in self.editing_modes],
            "local_candidate_id": self.local_candidate_id,
            "measurements": [item.to_mapping() for item in self.measurements],
            "narrative_functions": [item.value for item in self.narrative_functions],
            "open_question": self.open_question,
            "payoff_event_refs": list(self.payoff_event_refs),
            "payoff_or_open_question": self.payoff_or_open_question,
            "reason": self.reason,
            "support": self.support.to_mapping(),
            "supporting_event_refs": list(self.supporting_event_refs),
            "tags": [item.value for item in self.tags],
        }


@dataclass(frozen=True, slots=True)
class VlmSemanticPackV4:
    request_identity_sha256: str
    window_manifest_sha256: str
    raw_response_sha256: str
    window_summary: VlmWindowSummary
    continuity: VlmContinuityV4
    entities: tuple[VlmEntityV4, ...]
    facts: tuple[VlmFactV4, ...]
    events: tuple[VlmEventV4, ...]
    candidate_hypotheses: tuple[VlmCandidateHypothesisV4, ...]

    def __post_init__(self) -> None:
        hash_fields = (
            "request_identity_sha256",
            "window_manifest_sha256",
            "raw_response_sha256",
        )
        for field_name in hash_fields:
            sha256_prefixed(getattr(self, field_name), f"semantic_pack.{field_name}")
        if type(self.window_summary) is not VlmWindowSummary:  # noqa: E721
            raise VlmValidationError("semantic_pack.window_summary must be VlmWindowSummary")
        if type(self.continuity) is not VlmContinuityV4:  # noqa: E721
            raise VlmValidationError("semantic_pack.continuity must be VlmContinuityV4")
        collections = (
            ("entities", VlmEntityV4),
            ("facts", VlmFactV4),
            ("events", VlmEventV4),
            ("candidate_hypotheses", VlmCandidateHypothesisV4),
        )
        local_fields = {
            "entities": "local_entity_id",
            "facts": "local_fact_id",
            "events": "local_event_id",
            "candidate_hypotheses": "local_candidate_id",
        }
        for field_name, item_type in collections:
            values = tuple(getattr(self, field_name))
            if any(type(item) is not item_type for item in values):  # noqa: E721
                raise VlmValidationError(
                    f"semantic_pack.{field_name} must contain {item_type.__name__} values"
                )
            local_ids = tuple(getattr(item, local_fields[field_name]) for item in values)
            if local_ids != tuple(sorted(local_ids)) or len(local_ids) != len(set(local_ids)):
                raise VlmValidationError(
                    f"semantic_pack.{field_name} must be sorted by unique local IDs"
                )
            object.__setattr__(self, field_name, values)
        if not self.facts:
            raise VlmValidationError("semantic_pack.facts must contain at least one fact")
        supports = tuple(item.support for items in (
            self.entities, self.facts, self.events, self.candidate_hypotheses,
            self.continuity.temporal_segments,
        ) for item in items)
        if any(support.manifest.canonical_hash != self.window_manifest_sha256 for support in supports):
            raise VlmValidationError("semantic_pack support belongs to a different window manifest")
        if len({support.manifest_set.canonical_hash for support in supports}) != 1:
            raise VlmValidationError("semantic_pack supports must share one exact manifest set")
        self._validate_global_identities_and_references()

    def _validate_global_identities_and_references(self) -> None:
        entity_ids = {item.entity_id for item in self.entities}
        fact_ids = {item.fact_id for item in self.facts}
        event_ids = {item.event_id for item in self.events}
        facts_by_id = {item.fact_id: item for item in self.facts}
        events_by_id = {item.event_id: item for item in self.events}
        expected_ids = (
            (self.entities, "entity", "local_entity_id", "entity_id"),
            (self.facts, "fact", "local_fact_id", "fact_id"),
            (self.events, "event", "local_event_id", "event_id"),
            (
                self.candidate_hypotheses,
                "candidate",
                "local_candidate_id",
                "candidate_id",
            ),
        )
        for values, kind, local_field, global_field in expected_ids:
            for item in values:
                expected = derive_vlm_global_id(
                    kind, getattr(item, local_field), self.request_identity_sha256
                )
                if getattr(item, global_field) != expected:
                    raise VlmValidationError(f"{kind} global ID is not Kernel-derived")
        for fact in self.facts:
            if fact.subject_ref not in entity_ids or (
                fact.object_ref is not None and fact.object_ref not in entity_ids
            ):
                raise VlmValidationError("fact entity reference is not closed")
        for event in self.events:
            if not set(event.participant_refs) <= entity_ids:
                raise VlmValidationError("event participant reference is not closed")
            if not set(event.fact_refs) <= fact_ids:
                raise VlmValidationError("event fact reference is not closed")
            if (
                not set(event.cause_event_refs) <= event_ids
                or not set(event.effect_event_refs) <= event_ids
            ):
                raise VlmValidationError("event causal reference is not closed")
            if (
                event.event_id in event.cause_event_refs
                or event.event_id in event.effect_event_refs
            ):
                raise VlmValidationError("event causal graph must not contain self-loops")
            for fact_ref in event.fact_refs:
                if not _observation_ranges_overlap(event.support, facts_by_id[fact_ref].support):
                    raise VlmValidationError(
                        "event support must overlap every directly referenced fact support"
                    )
        self._validate_causal_graph(events_by_id)
        if (
            not set(self.window_summary.fact_refs) <= fact_ids
            or not set(self.window_summary.event_refs) <= event_ids
        ):
            raise VlmValidationError("window_summary reference is not closed")
        if (
            not set(self.continuity.entry_state_fact_refs) <= fact_ids
            or not set(self.continuity.exit_state_fact_refs) <= fact_ids
        ):
            raise VlmValidationError("continuity fact reference is not closed")
        for candidate in self.candidate_hypotheses:
            candidate_events = {
                candidate.anchor_event_ref,
                *candidate.supporting_event_refs,
                *candidate.context_event_refs,
                *candidate.payoff_event_refs,
            }
            if not candidate_events <= event_ids:
                raise VlmValidationError("candidate event reference is not closed")
            closure_fact_ids = {
                fact_ref
                for event_id in candidate_events
                for fact_ref in events_by_id[event_id].fact_refs
            }
            for measurement in candidate.measurements:
                if (
                    not set(measurement.fact_refs) <= closure_fact_ids
                    or not set(measurement.event_refs) <= candidate_events
                ):
                    raise VlmValidationError(
                        "measurement references must belong to the candidate semantic closure"
                    )
            support_event_ids = {
                candidate.anchor_event_ref,
                *candidate.supporting_event_refs,
                *candidate.payoff_event_refs,
            }
            for event_id in support_event_ids:
                if not _observation_ranges_overlap(candidate.support, events_by_id[event_id].support):
                    raise VlmValidationError(
                        "candidate support must overlap anchor, supporting, and payoff events"
                    )

    @staticmethod
    def _validate_causal_graph(events_by_id: dict[str, VlmEventV4]) -> None:
        for event in events_by_id.values():
            for cause_ref in event.cause_event_refs:
                if event.event_id not in events_by_id[cause_ref].effect_event_refs:
                    raise VlmValidationError(
                        "event cause/effect references must be mutually inverse"
                    )
            for effect_ref in event.effect_event_refs:
                if event.event_id not in events_by_id[effect_ref].cause_event_refs:
                    raise VlmValidationError(
                        "event cause/effect references must be mutually inverse"
                    )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(event_id: str) -> None:
            if event_id in visiting:
                raise VlmValidationError("event causal graph must be acyclic")
            if event_id in visited:
                return
            visiting.add(event_id)
            for effect_ref in events_by_id[event_id].effect_event_refs:
                visit(effect_ref)
            visiting.remove(event_id)
            visited.add(event_id)

        for event_id in events_by_id:
            visit(event_id)

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_hypotheses": [item.to_mapping() for item in self.candidate_hypotheses],
            "continuity": self.continuity.to_mapping(),
            "entities": [item.to_mapping() for item in self.entities],
            "events": [item.to_mapping() for item in self.events],
            "facts": [item.to_mapping() for item in self.facts],
            "provenance": {
                "raw_response_sha256": self.raw_response_sha256,
                "request_identity_sha256": self.request_identity_sha256,
                "window_manifest_sha256": self.window_manifest_sha256,
            },
            "schema_version": 4,
            "window_summary": self.window_summary.to_mapping(),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())
