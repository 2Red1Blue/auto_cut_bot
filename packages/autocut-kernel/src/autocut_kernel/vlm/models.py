"""Closed immutable values for VLM Semantic Pack v3.

Provider responses contain only window-local semantic claims. Source mapping,
global identities, ownership, request provenance, and raw-response identity are
derived by the Kernel parser and represented by the persisted values here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from ..media.types import TickRange, TimeBase, canonical_sha256, require_pts, sha256_prefixed

if TYPE_CHECKING:
    from .window import WindowManifest, WindowManifestSet


class VlmContractError(ValueError):
    """Base error for a fail-closed VLM contract."""


class VlmValidationError(VlmContractError):
    """Raised when an immutable VLM value violates its closed contract."""


def _text(
    value: object,
    field_name: str,
    *,
    maximum_length: int = 16_384,
    nullable: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value or value.isspace() or len(value) > maximum_length:  # noqa: E721
        raise VlmValidationError(
            f"{field_name} must be non-empty text of at most {maximum_length} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise VlmValidationError(f"{field_name} must not contain control characters")
    return value


def _non_negative(value: object, field_name: str) -> int:
    result = require_pts(value, field_name)
    if result < 0:
        raise VlmValidationError(f"{field_name} must be non-negative")
    return result


def _confidence(value: object, field_name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():  # noqa: E721
        raise VlmValidationError(f"{field_name} must be a finite Decimal")
    if not Decimal("0") <= value <= Decimal("1"):
        raise VlmValidationError(f"{field_name} must be between zero and one")
    return value


def _enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if type(value) is not enum_type:  # noqa: E721
        raise VlmValidationError(f"{field_name} must be a {enum_type.__name__}")


def _canonical_refs(
    values: tuple[str, ...], field_name: str, *, nonempty: bool = False
) -> tuple[str, ...]:
    result = tuple(values)
    if nonempty and not result:
        raise VlmValidationError(f"{field_name} must be non-empty")
    if any(type(item) is not str for item in result):  # noqa: E721
        raise VlmValidationError(f"{field_name} must contain strings")
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        raise VlmValidationError(f"{field_name} must be sorted and unique")
    return result


_LOCAL_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _local_id(value: object, field_name: str) -> str:
    if type(value) is not str or _LOCAL_ID.fullmatch(value) is None:  # noqa: E721
        raise VlmValidationError(f"{field_name} must be a canonical provider-local ID")
    return value


def _source_ranges_overlap(left: VlmSemanticSupport, right: VlmSemanticSupport) -> bool:
    left_range = left.source_interval.coarse_range
    right_range = right.source_interval.coarse_range
    return max(left_range.start_pts, right_range.start_pts) < min(
        left_range.end_pts, right_range.end_pts
    )


class VlmTemporalMode(str, Enum):
    PRESENT = "present"
    FLASHBACK = "flashback"
    FLASHFORWARD = "flashforward"
    DREAM = "dream"
    UNKNOWN = "unknown"


class VlmEntityKind(str, Enum):
    PERSON = "person"
    OBJECT = "object"
    LOCATION = "location"
    SCREEN_TEXT_SOURCE = "screen_text_source"


class VlmFactKind(str, Enum):
    VISIBLE_PRESENCE = "visible_presence"
    VISIBLE_STATE = "visible_state"
    VISIBLE_ACTION = "visible_action"
    VISIBLE_CHANGE = "visible_change"
    VISIBLE_RELATION = "visible_relation"
    SCENE_CONTEXT = "scene_context"
    CHARACTER_APPEARANCE = "character_appearance"
    SCREEN_TEXT = "screen_text"
    TEMPORAL_MODE = "temporal_mode"


class VlmEventKind(str, Enum):
    ACTION = "action"
    INTERACTION = "interaction"
    STATE_CHANGE = "state_change"
    REACTION = "reaction"
    REVEAL = "reveal"
    TRANSITION = "transition"


class VlmCandidateKind(str, Enum):
    HIGHLIGHT = "highlight"
    HOOK = "hook"


class VlmEditingMode(str, Enum):
    DIALOGUE = "dialogue"
    ACTION = "action"


class VlmNarrativeFunction(str, Enum):
    HOOK = "hook"
    SETUP = "setup"
    ESCALATION = "escalation"
    CONFRONTATION = "confrontation"
    REVEAL = "reveal"
    REVERSAL = "reversal"
    PAYOFF = "payoff"
    AFTERMATH = "aftermath"


class VlmCandidateTag(str, Enum):
    """Closed descriptive vocabulary; ordering is the canonical order."""

    DIALOGUE = "dialogue"
    ACTION = "action"
    EMOTION = "emotion"
    SUSPENSE = "suspense"
    CONFLICT = "conflict"
    REVEAL = "reveal"
    REVERSAL = "reversal"
    VISUAL_SPECTACLE = "visual_spectacle"
    CHARACTER_MOMENT = "character_moment"
    RELATIONSHIP_MOMENT = "relationship_moment"


class VlmMeasurementKind(str, Enum):
    HOOK_STRENGTH = "hook_strength"
    REVEAL_STRENGTH = "reveal_strength"
    EMOTIONAL_PAYOFF_STRENGTH = "emotional_payoff_strength"
    DIALOGUE_SALIENCE = "dialogue_salience"
    ACTION_SALIENCE = "action_salience"
    VISUAL_SALIENCE = "visual_salience"


def derive_vlm_global_id(member_kind: str, local_id: str, request_identity_sha256: str) -> str:
    """Derive one request-scoped global identity from a provider-local identity."""

    _text(member_kind, "member_kind", maximum_length=64)
    _local_id(local_id, "local_id")
    sha256_prefixed(request_identity_sha256, "request_identity_sha256")
    return canonical_sha256(
        {
            "local_id": local_id,
            "member_kind": member_kind,
            "request_identity_sha256": request_identity_sha256,
        }
    )


@dataclass(frozen=True, slots=True)
class VlmParsePolicy:
    """Frozen structural and resource budgets for a v3 provider response."""

    max_response_bytes: int
    max_entities: int
    max_facts: int
    max_events: int
    max_candidate_hypotheses: int
    max_temporal_segments: int
    max_measurements: int
    max_text_characters: int
    max_total_text_characters: int

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            value = require_pts(getattr(self, field_name), f"parse_policy.{field_name}")
            if value <= 0:
                raise VlmValidationError(f"parse_policy.{field_name} must be positive")
        if self.max_text_characters > self.max_total_text_characters:
            raise VlmValidationError("per-field text budget cannot exceed total text budget")

    def to_mapping(self) -> dict[str, object]:
        return {field_name: getattr(self, field_name) for field_name in self.__dataclass_fields__}

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class VlmRequestIdentity:
    """Canonical identity of one provider invocation request."""

    window_manifest_sha256: str
    source_id: str
    source_clock_id: str
    source_sha256: str
    frame_samples_sha256: str
    frame_pts_index_set_sha256: str
    window_manifest_set_sha256: str
    proxy_blob_ref_sha256: str
    preprocess_policy_sha256: str
    window_sampling_policy_sha256: str
    prompt_template_sha256: str
    prompt_version: str
    response_schema_sha256: str
    model_id: str
    provider_id: str
    request_parameters_sha256: str
    request_payload_sha256: str
    parse_policy_sha256: str

    def __post_init__(self) -> None:
        hash_fields = (
            "window_manifest_sha256",
            "source_sha256",
            "frame_samples_sha256",
            "frame_pts_index_set_sha256",
            "window_manifest_set_sha256",
            "proxy_blob_ref_sha256",
            "preprocess_policy_sha256",
            "window_sampling_policy_sha256",
            "prompt_template_sha256",
            "response_schema_sha256",
            "request_parameters_sha256",
            "request_payload_sha256",
            "parse_policy_sha256",
        )
        for field_name in hash_fields:
            sha256_prefixed(getattr(self, field_name), f"request_identity.{field_name}")
        text_fields = (
            "source_id",
            "source_clock_id",
            "prompt_version",
            "model_id",
            "provider_id",
        )
        for field_name in text_fields:
            _text(getattr(self, field_name), f"request_identity.{field_name}", maximum_length=256)

    @classmethod
    def from_manifest(
        cls,
        manifest: WindowManifest,
        manifest_set: WindowManifestSet,
        *,
        prompt_template_sha256: str,
        prompt_version: str,
        response_schema_sha256: str,
        model_id: str,
        provider_id: str,
        request_parameters_sha256: str,
        request_payload_sha256: str,
        parse_policy: VlmParsePolicy,
    ) -> VlmRequestIdentity:
        from .window import WindowManifest, WindowManifestSet

        if type(manifest) is not WindowManifest or type(manifest_set) is not WindowManifestSet:  # noqa: E721
            raise VlmValidationError("request requires exact WindowManifest values")
        manifest_set.require_member(manifest)
        if type(parse_policy) is not VlmParsePolicy:  # noqa: E721
            raise VlmValidationError("parse_policy must be a VlmParsePolicy")
        return cls(
            window_manifest_sha256=manifest.canonical_hash,
            source_id=manifest.source_id,
            source_clock_id=manifest.source_clock_id,
            source_sha256=manifest.source_sha256,
            frame_samples_sha256=manifest.frame_samples_sha256,
            frame_pts_index_set_sha256=manifest.frame_pts_index_set_sha256,
            window_manifest_set_sha256=manifest_set.canonical_hash,
            proxy_blob_ref_sha256=manifest.proxy_blob_ref.canonical_hash,
            preprocess_policy_sha256=manifest.preprocess_policy_sha256,
            window_sampling_policy_sha256=manifest.window_sampling_policy_sha256,
            prompt_template_sha256=prompt_template_sha256,
            prompt_version=prompt_version,
            response_schema_sha256=response_schema_sha256,
            model_id=model_id,
            provider_id=provider_id,
            request_parameters_sha256=request_parameters_sha256,
            request_payload_sha256=request_payload_sha256,
            parse_policy_sha256=parse_policy.canonical_hash,
        )

    def assert_manifest_binding(
        self, manifest: WindowManifest, manifest_set: WindowManifestSet
    ) -> None:
        from .window import WindowManifest, WindowManifestSet

        if type(manifest) is not WindowManifest or type(manifest_set) is not WindowManifestSet:  # noqa: E721
            raise VlmValidationError("request requires exact WindowManifest values")
        manifest_set.require_member(manifest)
        expected = {
            "window_manifest_sha256": manifest.canonical_hash,
            "source_id": manifest.source_id,
            "source_clock_id": manifest.source_clock_id,
            "source_sha256": manifest.source_sha256,
            "frame_samples_sha256": manifest.frame_samples_sha256,
            "frame_pts_index_set_sha256": manifest.frame_pts_index_set_sha256,
            "window_manifest_set_sha256": manifest_set.canonical_hash,
            "proxy_blob_ref_sha256": manifest.proxy_blob_ref.canonical_hash,
            "preprocess_policy_sha256": manifest.preprocess_policy_sha256,
            "window_sampling_policy_sha256": manifest.window_sampling_policy_sha256,
        }
        mismatches = tuple(
            field_name
            for field_name, value in expected.items()
            if getattr(self, field_name) != value
        )
        if mismatches:
            raise VlmValidationError(
                f"request identity manifest binding mismatch: {', '.join(mismatches)}"
            )

    def to_mapping(self) -> dict[str, object]:
        return {field_name: getattr(self, field_name) for field_name in self.__dataclass_fields__}

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class MappedSourceInterval:
    """A conservative coarse Source interval, never a physical endpoint proof."""

    coarse_range: TickRange
    mapping_error_bound_source_pts: int
    source_time_base: TimeBase
    provider_uncertainty_proxy_pts: int
    proxy_time_base: TimeBase

    def __post_init__(self) -> None:
        if type(self.coarse_range) is not TickRange:  # noqa: E721
            raise VlmValidationError("mapped interval must contain a TickRange")
        _non_negative(
            self.mapping_error_bound_source_pts,
            "mapped_interval.mapping_error_bound_source_pts",
        )
        _non_negative(
            self.provider_uncertainty_proxy_pts,
            "mapped_interval.provider_uncertainty_proxy_pts",
        )
        if (
            type(self.source_time_base) is not TimeBase
            or type(self.proxy_time_base) is not TimeBase
        ):  # noqa: E721
            raise VlmValidationError("mapped interval time bases must be TimeBase values")

    def to_mapping(self) -> dict[str, object]:
        source_base = {
            "denominator": self.source_time_base.denominator,
            "numerator": self.source_time_base.numerator,
        }
        return {
            "coarse_range": {
                "end_pts": self.coarse_range.end_pts,
                "start_pts": self.coarse_range.start_pts,
                "time_base": source_base,
            },
            "mapping_error_bound": {
                "clock": "source",
                "tick": self.mapping_error_bound_source_pts,
                "time_base": source_base,
            },
            "provider_uncertainty": {
                "clock": "proxy",
                "tick": self.provider_uncertainty_proxy_pts,
                "time_base": {
                    "denominator": self.proxy_time_base.denominator,
                    "numerator": self.proxy_time_base.numerator,
                },
            },
            "semantic_precision": "coarse_only",
        }


@dataclass(frozen=True, slots=True)
class VlmProxyInterval:
    proxy_range: TickRange
    uncertainty_pts: int

    def __post_init__(self) -> None:
        if type(self.proxy_range) is not TickRange:  # noqa: E721
            raise VlmValidationError("proxy interval must contain a TickRange")
        _non_negative(self.uncertainty_pts, "proxy_interval.uncertainty_pts")

    def to_mapping(self) -> dict[str, object]:
        return {
            "end_pts": self.proxy_range.end_pts,
            "start_pts": self.proxy_range.start_pts,
            "uncertainty_pts": self.uncertainty_pts,
        }


@dataclass(frozen=True, slots=True)
class VlmSemanticSupport:
    proxy_interval: VlmProxyInterval
    supporting_frame_ids: tuple[str, ...]
    confidence: Decimal
    source_interval: MappedSourceInterval
    core_owner_window_manifest_sha256: str

    def __post_init__(self) -> None:
        if type(self.proxy_interval) is not VlmProxyInterval:  # noqa: E721
            raise VlmValidationError("support.proxy_interval must be a VlmProxyInterval")
        frames = _canonical_refs(
            tuple(self.supporting_frame_ids), "support.supporting_frame_ids", nonempty=True
        )
        for frame_id in frames:
            sha256_prefixed(frame_id, "support.supporting_frame_ids[]")
        _confidence(self.confidence, "support.confidence")
        if type(self.source_interval) is not MappedSourceInterval:  # noqa: E721
            raise VlmValidationError("support.source_interval must be a MappedSourceInterval")
        sha256_prefixed(
            self.core_owner_window_manifest_sha256,
            "support.core_owner_window_manifest_sha256",
        )
        object.__setattr__(self, "supporting_frame_ids", frames)

    def to_mapping(self) -> dict[str, object]:
        return {
            "confidence": format(self.confidence, "f"),
            "core_owner_window_manifest_sha256": self.core_owner_window_manifest_sha256,
            "proxy_interval": self.proxy_interval.to_mapping(),
            "source_interval": self.source_interval.to_mapping(),
            "supporting_frame_ids": list(self.supporting_frame_ids),
        }


@dataclass(frozen=True, slots=True)
class VlmWindowSummary:
    summary: str
    dominant_temporal_mode: VlmTemporalMode
    fact_refs: tuple[str, ...]
    event_refs: tuple[str, ...]
    confidence: Decimal

    def __post_init__(self) -> None:
        _text(self.summary, "window_summary.summary")
        _enum(
            self.dominant_temporal_mode,
            VlmTemporalMode,
            "window_summary.dominant_temporal_mode",
        )
        object.__setattr__(
            self, "fact_refs", _canonical_refs(self.fact_refs, "window_summary.fact_refs")
        )
        object.__setattr__(
            self, "event_refs", _canonical_refs(self.event_refs, "window_summary.event_refs")
        )
        _confidence(self.confidence, "window_summary.confidence")

    def to_mapping(self) -> dict[str, object]:
        return {
            "confidence": format(self.confidence, "f"),
            "dominant_temporal_mode": self.dominant_temporal_mode.value,
            "event_refs": list(self.event_refs),
            "fact_refs": list(self.fact_refs),
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class VlmTemporalSegment:
    mode: VlmTemporalMode
    summary: str
    support: VlmSemanticSupport

    def __post_init__(self) -> None:
        _enum(self.mode, VlmTemporalMode, "temporal_segment.mode")
        _text(self.summary, "temporal_segment.summary")
        if type(self.support) is not VlmSemanticSupport:  # noqa: E721
            raise VlmValidationError("temporal_segment.support must be VlmSemanticSupport")

    def to_mapping(self) -> dict[str, object]:
        support = self.support.to_mapping()
        return {
            "confidence": support["confidence"],
            "core_owner_window_manifest_sha256": support["core_owner_window_manifest_sha256"],
            "mode": self.mode.value,
            "proxy_interval": support["proxy_interval"],
            "source_interval": support["source_interval"],
            "summary": self.summary,
            "supporting_frame_ids": support["supporting_frame_ids"],
        }


@dataclass(frozen=True, slots=True)
class VlmContinuity:
    starts_mid_event: bool
    ends_mid_event: bool
    continues_from_previous: bool
    continues_into_next: bool
    entry_state_fact_refs: tuple[str, ...]
    exit_state_fact_refs: tuple[str, ...]
    temporal_segments: tuple[VlmTemporalSegment, ...]

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
        if any(type(item) is not VlmTemporalSegment for item in segments):  # noqa: E721
            raise VlmValidationError(
                "continuity.temporal_segments must contain VlmTemporalSegment values"
            )
        segment_keys = tuple(
            (
                item.support.proxy_interval.proxy_range.start_pts,
                item.support.proxy_interval.proxy_range.end_pts,
            )
            for item in segments
        )
        if segment_keys != tuple(sorted(segment_keys)):
            raise VlmValidationError(
                "continuity.temporal_segments must be in canonical proxy interval order"
            )
        for left, right in zip(segments, segments[1:], strict=False):
            if (
                left.support.proxy_interval.proxy_range.end_pts
                > right.support.proxy_interval.proxy_range.start_pts
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
class VlmEntity:
    entity_id: str
    local_entity_id: str
    entity_kind: VlmEntityKind
    display_label: str
    visual_description: str
    support: VlmSemanticSupport

    def __post_init__(self) -> None:
        sha256_prefixed(self.entity_id, "entity.entity_id")
        _local_id(self.local_entity_id, "entity.local_entity_id")
        _enum(self.entity_kind, VlmEntityKind, "entity.entity_kind")
        _text(self.display_label, "entity.display_label")
        _text(self.visual_description, "entity.visual_description")
        if type(self.support) is not VlmSemanticSupport:  # noqa: E721
            raise VlmValidationError("entity.support must be VlmSemanticSupport")

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
class VlmFact:
    fact_id: str
    local_fact_id: str
    fact_kind: VlmFactKind
    subject_ref: str
    object_ref: str | None
    summary: str
    support: VlmSemanticSupport

    def __post_init__(self) -> None:
        sha256_prefixed(self.fact_id, "fact.fact_id")
        _local_id(self.local_fact_id, "fact.local_fact_id")
        _enum(self.fact_kind, VlmFactKind, "fact.fact_kind")
        sha256_prefixed(self.subject_ref, "fact.subject_ref")
        if self.object_ref is not None:
            sha256_prefixed(self.object_ref, "fact.object_ref")
        _text(self.summary, "fact.summary")
        if type(self.support) is not VlmSemanticSupport:  # noqa: E721
            raise VlmValidationError("fact.support must be VlmSemanticSupport")

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
class VlmEvent:
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
    support: VlmSemanticSupport

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
        if type(self.support) is not VlmSemanticSupport:  # noqa: E721
            raise VlmValidationError("event.support must be VlmSemanticSupport")

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
class VlmSemanticMeasurement:
    measurement_kind: VlmMeasurementKind
    value: Decimal
    confidence: Decimal
    fact_refs: tuple[str, ...]
    event_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _enum(
            self.measurement_kind,
            VlmMeasurementKind,
            "measurement.measurement_kind",
        )
        _confidence(self.value, "measurement.value")
        _confidence(self.confidence, "measurement.confidence")
        object.__setattr__(
            self, "fact_refs", _canonical_refs(self.fact_refs, "measurement.fact_refs")
        )
        object.__setattr__(
            self, "event_refs", _canonical_refs(self.event_refs, "measurement.event_refs")
        )
        if not self.fact_refs and not self.event_refs:
            raise VlmValidationError(
                "measurement fact_refs and event_refs must be non-empty collectively"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "confidence": format(self.confidence, "f"),
            "event_refs": list(self.event_refs),
            "fact_refs": list(self.fact_refs),
            "measurement_kind": self.measurement_kind.value,
            "value": format(self.value, "f"),
        }


@dataclass(frozen=True, slots=True)
class VlmCandidateHypothesis:
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
    support: VlmSemanticSupport

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
        if type(self.support) is not VlmSemanticSupport:  # noqa: E721
            raise VlmValidationError("candidate.support must be VlmSemanticSupport")
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
class VlmSemanticPack:
    request_identity_sha256: str
    window_manifest_sha256: str
    raw_response_sha256: str
    window_summary: VlmWindowSummary
    continuity: VlmContinuity
    entities: tuple[VlmEntity, ...]
    facts: tuple[VlmFact, ...]
    events: tuple[VlmEvent, ...]
    candidate_hypotheses: tuple[VlmCandidateHypothesis, ...]

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
        if type(self.continuity) is not VlmContinuity:  # noqa: E721
            raise VlmValidationError("semantic_pack.continuity must be VlmContinuity")
        collections = (
            ("entities", VlmEntity),
            ("facts", VlmFact),
            ("events", VlmEvent),
            ("candidate_hypotheses", VlmCandidateHypothesis),
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
                if not _source_ranges_overlap(event.support, facts_by_id[fact_ref].support):
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
                if not _source_ranges_overlap(candidate.support, events_by_id[event_id].support):
                    raise VlmValidationError(
                        "candidate support must overlap anchor, supporting, and payoff events"
                    )

    @staticmethod
    def _validate_causal_graph(events_by_id: dict[str, VlmEvent]) -> None:
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
            "schema_version": 3,
            "window_summary": self.window_summary.to_mapping(),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())
