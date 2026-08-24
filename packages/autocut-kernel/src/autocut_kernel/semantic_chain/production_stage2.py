"""Stage 2 committed VLM authority, candidates, proposals, and portfolio evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from itertools import combinations
from typing import Final, Sequence, cast

from ..contracts.compiler.canonical import canonical_json_hash
from ..contracts.compiler.refs import ArtifactRef, DomainRef
from ..vlm.models import VlmObservation, VlmObservationSet, VlmRequestIdentity
from .production_common import (
    CanonicalModel,
    DurationRangeSeconds,
    EvaluatorOwnedModel,
    PendingBusinessSet,
    ProductionModelError,
    RuleResult,
    TimeBaseValue,
    canonical_domain_refs,
    canonical_ids,
    canonical_values,
    computed_rule_results,
    exact_decimal,
    identifier,
    integer,
    jcs_key,
    safe_token,
    text,
)


class EditingMode(str, Enum):
    DIALOGUE = "dialogue"
    ACTION = "action"


class NarrativeFunction(str, Enum):
    """The frozen shared NarrativeFunction vocabulary used by Stage 2/3."""

    HOOK_AND_ORIENT = "hook_and_orient"
    ESTABLISH_CONTEXT = "establish_context"
    ESCALATE_CONFLICT = "escalate_conflict"
    TURNING_POINT = "turning_point"
    REVEAL = "reveal"
    EMOTIONAL_PAYOFF = "emotional_payoff"
    CONSEQUENCE = "consequence"
    CODA = "coda"


_EDITING_MODE_ORDER: Final = (EditingMode.DIALOGUE, EditingMode.ACTION)
_MEASUREMENT_KINDS: Final = frozenset(
    {
        "hook_strength",
        "reveal_strength",
        "emotional_payoff_strength",
        "dialogue_salience",
        "action_salience",
        "visual_salience",
    }
)
_ANCHOR_ROLES: Final = frozenset(
    {"semantic_center", "dialogue_semantic", "visual_event", "action"}
)
_PHYSICAL_REQUIREMENTS: Final = {
    "dialogue_integrity": "complete",
    "subtitle_clearance": "protect_detected_cues",
    "visual_validity": "endpoint_and_stable_region",
}


@dataclass(frozen=True, slots=True, init=False)
class CommittedVlmObservation(EvaluatorOwnedModel):
    """Reader output binding one observation to the exact committed set/request."""

    observation_set_ref: ArtifactRef
    observation_ref: DomainRef
    source_ref: DomainRef
    window_ref: DomainRef
    request_identity: VlmRequestIdentity
    observation_set: VlmObservationSet
    observation: VlmObservation
    vlm_observation_sha256: str

    @classmethod
    def from_reader(
        cls,
        *,
        observation_set_ref: ArtifactRef,
        observation_ref: DomainRef,
        source_ref: DomainRef,
        window_ref: DomainRef,
        request_identity: VlmRequestIdentity,
        observation_set: VlmObservationSet,
        observation: VlmObservation,
    ) -> CommittedVlmObservation:
        if type(observation_set_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("observation_set_ref must be an ArtifactRef")
        if type(request_identity) is not VlmRequestIdentity:  # noqa: E721
            raise ProductionModelError("request_identity must be a VlmRequestIdentity")
        if type(observation_set) is not VlmObservationSet or type(observation) is not VlmObservation:  # noqa: E721
            raise ProductionModelError("committed observation reader returned wrong value types")
        if observation_set_ref.content_hash != observation_set.canonical_hash:
            raise ProductionModelError("observation_set_ref does not bind the exact committed set")
        if observation_set.request_identity_sha256 != request_identity.canonical_hash:
            raise ProductionModelError("observation set does not bind the exact request identity")
        matching = tuple(
            item for item in observation_set.observations if item.observation_id == observation.observation_id
        )
        if len(matching) != 1 or matching[0] != observation:
            raise ProductionModelError("observation is not an exact member of the committed set")
        if not observation.core_owned:
            raise ProductionModelError("only Kernel core-owned observations may be evaluated")
        if (
            type(observation_ref) is not DomainRef
            or observation_ref.object_type != "vlm_observation"
            or observation_ref.artifact_ref != observation_set_ref
            or observation_ref.object_id != observation.observation_id
        ):  # noqa: E721
            raise ProductionModelError("observation_ref has the wrong owner, hash, or object ID")
        if (
            type(source_ref) is not DomainRef
            or source_ref.object_type != "source"
            or source_ref.object_id != request_identity.source_id
        ):  # noqa: E721
            raise ProductionModelError("source_ref does not bind the committed request Source")
        if (
            type(window_ref) is not DomainRef
            or window_ref.object_type != "vlm_window"
            or window_ref.object_id != observation.window_manifest_sha256
        ):  # noqa: E721
            raise ProductionModelError("window_ref does not bind the committed observation window")
        instance = object.__new__(cls)
        object.__setattr__(instance, "observation_set_ref", observation_set_ref)
        object.__setattr__(instance, "observation_ref", observation_ref)
        object.__setattr__(instance, "source_ref", source_ref)
        object.__setattr__(instance, "window_ref", window_ref)
        object.__setattr__(instance, "request_identity", request_identity)
        object.__setattr__(instance, "observation_set", observation_set)
        object.__setattr__(instance, "observation", observation)
        object.__setattr__(
            instance,
            "vlm_observation_sha256",
            canonical_json_hash(observation.to_mapping()),
        )
        return instance

    def to_mapping(self) -> dict[str, object]:
        return {
            "observation_ref": self.observation_ref.to_mapping(),
            "observation_set_ref": self.observation_set_ref.to_mapping(),
            "request_identity_sha256": self.request_identity.canonical_hash,
            "source_ref": self.source_ref.to_mapping(),
            "vlm_observation_sha256": self.vlm_observation_sha256,
            "window_ref": self.window_ref.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class SemanticAnchor(CanonicalModel):
    anchor_role: str
    ref: DomainRef

    def __post_init__(self) -> None:
        if self.anchor_role not in _ANCHOR_ROLES:
            raise ProductionModelError("anchor_role is unknown")
        if type(self.ref) is not DomainRef or self.ref.object_type not in {"event", "vlm_observation"}:  # noqa: E721
            raise ProductionModelError("semantic anchor must point to an Event or VLM observation")

    def to_mapping(self) -> dict[str, object]:
        return {"anchor_role": self.anchor_role, "ref": self.ref.to_mapping()}


@dataclass(frozen=True, slots=True)
class CandidateMeasurementPolicy(CanonicalModel):
    policy_id: str
    policy_version: str
    minimum_confidence: str
    allowed_kinds: tuple[str, ...]
    allowed_producers: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier(self.policy_id, "measurement policy_id")
        safe_token(self.policy_version, "measurement policy_version")
        exact_decimal(self.minimum_confidence, "minimum_confidence")
        kinds = tuple(self.allowed_kinds)
        if not kinds or set(kinds) - _MEASUREMENT_KINDS:
            raise ProductionModelError("measurement policy contains an unknown kind")
        if tuple(sorted(kinds, key=jcs_key)) != kinds or len(kinds) != len(set(kinds)):
            raise ProductionModelError("measurement kinds must be unique and canonical")
        producers = tuple(self.allowed_producers)
        if not producers or set(producers) - {
            "audited_vlm_generation",
            "deterministic_vlm_projection",
        }:
            raise ProductionModelError("measurement policy contains an unknown producer")
        if tuple(sorted(producers, key=jcs_key)) != producers or len(producers) != len(set(producers)):
            raise ProductionModelError("measurement producers must be unique and canonical")
        object.__setattr__(self, "allowed_kinds", kinds)
        object.__setattr__(self, "allowed_producers", producers)

    def to_mapping(self) -> dict[str, object]:
        return {
            "allowed_kinds": list(self.allowed_kinds),
            "allowed_producers": list(self.allowed_producers),
            "minimum_confidence": self.minimum_confidence,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "value_domain": "exact_decimal_0_1",
        }


@dataclass(frozen=True, slots=True)
class SemanticMeasurement(CanonicalModel):
    measurement_id: str
    kind: str
    value: str
    confidence: str
    producer: str
    measurement_policy_ref: ArtifactRef
    generation_invocation_ref: ArtifactRef | None
    evidence_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        identifier(self.measurement_id, "measurement_id")
        if self.kind not in _MEASUREMENT_KINDS:
            raise ProductionModelError("semantic measurement kind is unknown")
        exact_decimal(self.value, "measurement.value")
        exact_decimal(self.confidence, "measurement.confidence")
        if self.producer not in {"audited_vlm_generation", "deterministic_vlm_projection"}:
            raise ProductionModelError("semantic measurement producer is unknown")
        if type(self.measurement_policy_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("measurement_policy_ref must be an ArtifactRef")
        if self.producer == "audited_vlm_generation":
            if type(self.generation_invocation_ref) is not ArtifactRef:  # noqa: E721
                raise ProductionModelError("audited VLM measurement requires GenerationInvocation")
        elif self.generation_invocation_ref is not None:
            raise ProductionModelError("deterministic projection cannot claim GenerationInvocation")
        evidence = canonical_domain_refs(self.evidence_refs, "measurement evidence", nonempty=True)
        if any(item.object_type not in {"event", "vlm_observation"} for item in evidence):
            raise ProductionModelError("semantic measurement evidence has a forbidden provenance")
        object.__setattr__(self, "evidence_refs", evidence)

    def to_mapping(self) -> dict[str, object]:
        return {
            "confidence": self.confidence,
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
            "generation_invocation_ref": (
                None
                if self.generation_invocation_ref is None
                else self.generation_invocation_ref.to_mapping()
            ),
            "kind": self.kind,
            "measurement_id": self.measurement_id,
            "measurement_policy_ref": self.measurement_policy_ref.to_mapping(),
            "producer": self.producer,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class CapabilityPredicate(CanonicalModel):
    predicate: str
    measurement_kind: str | None = None
    threshold: str | None = None
    anchor_role: str | None = None

    def __post_init__(self) -> None:
        if self.predicate == "measurement_at_least":
            if self.measurement_kind not in _MEASUREMENT_KINDS or self.threshold is None:
                raise ProductionModelError("measurement predicate is incomplete")
            exact_decimal(self.threshold, "capability threshold")
            if self.anchor_role is not None:
                raise ProductionModelError("measurement predicate cannot contain anchor_role")
        elif self.predicate == "anchor_role_exists":
            if self.anchor_role not in _ANCHOR_ROLES:
                raise ProductionModelError("anchor predicate role is unknown")
            if self.measurement_kind is not None or self.threshold is not None:
                raise ProductionModelError("anchor predicate cannot contain measurement fields")
        else:
            raise ProductionModelError("capability predicate is unknown")

    def to_mapping(self) -> dict[str, object]:
        if self.predicate == "measurement_at_least":
            return {
                "measurement_kind": cast(str, self.measurement_kind),
                "predicate": self.predicate,
                "threshold": cast(str, self.threshold),
            }
        return {"anchor_role": cast(str, self.anchor_role), "predicate": self.predicate}


@dataclass(frozen=True, slots=True)
class CapabilityRule(CanonicalModel):
    capability_rule_id: str
    output_kind: str
    output_value: str
    all_of: tuple[CapabilityPredicate, ...]

    def __post_init__(self) -> None:
        identifier(self.capability_rule_id, "capability_rule_id")
        if self.output_kind == "narrative_function":
            try:
                NarrativeFunction(self.output_value)
            except ValueError as error:
                raise ProductionModelError("capability rule narrative output is unknown") from error
        elif self.output_kind == "editing_mode":
            try:
                EditingMode(self.output_value)
            except ValueError as error:
                raise ProductionModelError("capability rule editing output is unknown") from error
        else:
            raise ProductionModelError("capability output_kind is unknown")
        predicates = cast(
            tuple[CapabilityPredicate, ...],
            canonical_values(self.all_of, CapabilityPredicate, "capability predicates", nonempty=True),
        )
        object.__setattr__(self, "all_of", predicates)

    def to_mapping(self) -> dict[str, object]:
        return {
            "all_of": [item.to_mapping() for item in self.all_of],
            "capability_rule_id": self.capability_rule_id,
            "output_kind": self.output_kind,
            "output_value": self.output_value,
        }


@dataclass(frozen=True, slots=True)
class CandidateCapabilityPolicy(CanonicalModel):
    policy_id: str
    policy_version: str
    rules: tuple[CapabilityRule, ...]

    def __post_init__(self) -> None:
        identifier(self.policy_id, "capability policy_id")
        safe_token(self.policy_version, "capability policy_version")
        rules = tuple(self.rules)
        if not rules or any(type(item) is not CapabilityRule for item in rules):  # noqa: E721
            raise ProductionModelError("capability policy rules are invalid")
        ids = tuple(item.capability_rule_id for item in rules)
        if ids != tuple(sorted(ids, key=jcs_key)) or len(ids) != len(set(ids)):
            raise ProductionModelError("capability rules must be unique and sorted by rule ID")
        object.__setattr__(self, "rules", rules)

    def to_mapping(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "rules": [item.to_mapping() for item in self.rules],
        }


@dataclass(frozen=True, slots=True, init=False)
class CandidateCapabilityAssessment(EvaluatorOwnedModel):
    evaluator_id: str
    evaluator_version: str
    evaluation_policy_ref: ArtifactRef
    supported_narrative_functions: tuple[NarrativeFunction, ...]
    editing_modes: tuple[EditingMode, ...]
    supporting_measurement_ids: tuple[str, ...]
    anchor_refs_hash: str
    vlm_observation_sha256: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "anchor_refs_hash": self.anchor_refs_hash,
            "editing_modes": [item.value for item in self.editing_modes],
            "evaluation_policy_ref": self.evaluation_policy_ref.to_mapping(),
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "supported_narrative_functions": [
                item.value for item in self.supported_narrative_functions
            ],
            "supporting_measurement_ids": list(self.supporting_measurement_ids),
            "vlm_observation_sha256": self.vlm_observation_sha256,
        }


@dataclass(frozen=True, slots=True, init=False)
class OwnerBoundVlmObservationRef(EvaluatorOwnedModel):
    observation_ref: DomainRef
    vlm_observation_sha256: str
    source_ref: DomainRef
    window_ref: DomainRef
    capability_assessment: CandidateCapabilityAssessment

    @property
    def editing_modes(self) -> tuple[EditingMode, ...]:
        return self.capability_assessment.editing_modes

    @property
    def supported_narrative_functions(self) -> tuple[NarrativeFunction, ...]:
        return self.capability_assessment.supported_narrative_functions

    def to_mapping(self) -> dict[str, object]:
        return {
            "capability_assessment": self.capability_assessment.to_mapping(),
            "observation_ref": self.observation_ref.to_mapping(),
            "source_ref": self.source_ref.to_mapping(),
            "vlm_observation_sha256": self.vlm_observation_sha256,
            "window_ref": self.window_ref.to_mapping(),
        }


class CandidateCapabilityEvaluator:
    """Recompute capability exclusively from committed VLM-backed inputs."""

    EVALUATOR_ID: Final = "candidate-capability-evaluator"
    EVALUATOR_VERSION: Final = "1.0.0"

    @classmethod
    def evaluate(
        cls,
        *,
        committed: CommittedVlmObservation,
        anchors: Sequence[SemanticAnchor],
        measurements: Sequence[SemanticMeasurement],
        measurement_policy_ref: ArtifactRef,
        measurement_policy: CandidateMeasurementPolicy,
        capability_policy_ref: ArtifactRef,
        capability_policy: CandidateCapabilityPolicy,
    ) -> OwnerBoundVlmObservationRef:
        if type(committed) is not CommittedVlmObservation:  # noqa: E721
            raise ProductionModelError("capability evaluator requires committed reader output")
        if measurement_policy_ref.content_hash != measurement_policy.canonical_hash:
            raise ProductionModelError("measurement policy ref does not bind the exact policy")
        if capability_policy_ref.content_hash != capability_policy.canonical_hash:
            raise ProductionModelError("capability policy ref does not bind the exact policy")
        anchor_values = cast(
            tuple[SemanticAnchor, ...],
            canonical_values(anchors, SemanticAnchor, "anchors", nonempty=True),
        )
        measurement_values = cast(
            tuple[SemanticMeasurement, ...],
            canonical_values(
                measurements, SemanticMeasurement, "semantic measurements", nonempty=True
            ),
        )
        observation_key = jcs_key(committed.observation_ref)
        if not any(jcs_key(item.ref) == observation_key for item in anchor_values):
            raise ProductionModelError("anchors do not include the exact committed observation")
        for measurement in measurement_values:
            if measurement.measurement_policy_ref != measurement_policy_ref:
                raise ProductionModelError("measurement is bound to an unrelated policy")
            if measurement.kind not in measurement_policy.allowed_kinds:
                raise ProductionModelError("measurement kind is not allowed by policy")
            if measurement.producer not in measurement_policy.allowed_producers:
                raise ProductionModelError("measurement producer is not allowed by policy")
            if Decimal(measurement.confidence) < Decimal(measurement_policy.minimum_confidence):
                raise ProductionModelError("measurement confidence is below frozen policy")
            if not any(jcs_key(ref) == observation_key for ref in measurement.evidence_refs):
                raise ProductionModelError("measurement does not bind the exact committed observation")
        anchor_roles = {item.anchor_role for item in anchor_values}
        measurement_by_kind: dict[str, list[SemanticMeasurement]] = {}
        for measurement in measurement_values:
            measurement_by_kind.setdefault(measurement.kind, []).append(measurement)
        functions: set[NarrativeFunction] = set()
        modes: set[EditingMode] = set()
        supporting: set[str] = set()
        for rule in capability_policy.rules:
            matched = True
            rule_measurements: set[str] = set()
            for predicate in rule.all_of:
                if predicate.predicate == "anchor_role_exists":
                    matched = cast(str, predicate.anchor_role) in anchor_roles
                else:
                    eligible = tuple(
                        item
                        for item in measurement_by_kind.get(
                            cast(str, predicate.measurement_kind), []
                        )
                        if Decimal(item.value) >= Decimal(cast(str, predicate.threshold))
                    )
                    matched = bool(eligible)
                    rule_measurements.update(item.measurement_id for item in eligible)
                if not matched:
                    break
            if matched:
                supporting.update(rule_measurements)
                if rule.output_kind == "narrative_function":
                    functions.add(NarrativeFunction(rule.output_value))
                else:
                    modes.add(EditingMode(rule.output_value))
        if not functions or not modes:
            raise ProductionModelError("capability assessment is indeterminate")
        ordered_functions = tuple(sorted(functions, key=lambda item: jcs_key(item.value)))
        ordered_modes = tuple(item for item in _EDITING_MODE_ORDER if item in modes)
        assessment = object.__new__(CandidateCapabilityAssessment)
        object.__setattr__(assessment, "evaluator_id", cls.EVALUATOR_ID)
        object.__setattr__(assessment, "evaluator_version", cls.EVALUATOR_VERSION)
        object.__setattr__(assessment, "evaluation_policy_ref", capability_policy_ref)
        object.__setattr__(assessment, "supported_narrative_functions", ordered_functions)
        object.__setattr__(assessment, "editing_modes", ordered_modes)
        object.__setattr__(
            assessment, "supporting_measurement_ids", tuple(sorted(supporting, key=jcs_key))
        )
        object.__setattr__(
            assessment,
            "anchor_refs_hash",
            canonical_json_hash([item.to_mapping() for item in anchor_values]),
        )
        object.__setattr__(
            assessment, "vlm_observation_sha256", committed.vlm_observation_sha256
        )
        trusted = object.__new__(OwnerBoundVlmObservationRef)
        object.__setattr__(trusted, "observation_ref", committed.observation_ref)
        object.__setattr__(trusted, "vlm_observation_sha256", committed.vlm_observation_sha256)
        object.__setattr__(trusted, "source_ref", committed.source_ref)
        object.__setattr__(trusted, "window_ref", committed.window_ref)
        object.__setattr__(trusted, "capability_assessment", assessment)
        return trusted


@dataclass(frozen=True, slots=True)
class DeclaredSpan(CanonicalModel):
    clock_id: str
    time_base: TimeBaseValue
    in_tick: int
    out_tick: int

    def __post_init__(self) -> None:
        identifier(self.clock_id, "declared_span.clock_id")
        if type(self.time_base) is not TimeBaseValue:  # noqa: E721
            raise ProductionModelError("declared_span.time_base is invalid")
        start = integer(self.in_tick, "declared_span.in_tick")
        end = integer(self.out_tick, "declared_span.out_tick", minimum=1)
        if start >= end:
            raise ProductionModelError("declared span must be a non-empty [in,out) interval")

    @property
    def duration_ticks(self) -> int:
        return self.out_tick - self.in_tick

    def to_mapping(self) -> dict[str, object]:
        return {
            "clock_id": self.clock_id,
            "in_tick": self.in_tick,
            "interval": "[in,out)",
            "out_tick": self.out_tick,
            "time_base": self.time_base.to_mapping(),
        }


@dataclass(frozen=True, slots=True, init=False)
class TickDurationProof(EvaluatorOwnedModel):
    time_base: TimeBaseValue
    total_ticks: int
    span_set_hash: str

    @classmethod
    def from_spans(cls, spans: Sequence[DeclaredSpan]) -> TickDurationProof:
        values = cast(
            tuple[DeclaredSpan, ...],
            canonical_values(spans, DeclaredSpan, "declared_spans", nonempty=True),
        )
        first = values[0]
        if any(item.time_base != first.time_base or item.clock_id != first.clock_id for item in values):
            raise ProductionModelError("duration proof requires one clock and time base")
        for previous, current in zip(values, values[1:], strict=False):
            if previous.out_tick > current.in_tick:
                raise ProductionModelError("declared spans must not overlap")
        instance = object.__new__(cls)
        object.__setattr__(instance, "time_base", first.time_base)
        object.__setattr__(instance, "total_ticks", sum(item.duration_ticks for item in values))
        object.__setattr__(
            instance,
            "span_set_hash",
            canonical_json_hash([item.to_mapping() for item in values]),
        )
        return instance

    def supports_seconds(self, seconds: int) -> bool:
        integer(seconds, "minimum_usable_seconds", minimum=1)
        return (
            self.total_ticks * self.time_base.numerator
            >= seconds * self.time_base.denominator
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "span_set_hash": self.span_set_hash,
            "time_base": self.time_base.to_mapping(),
            "total_ticks": self.total_ticks,
        }


@dataclass(frozen=True, slots=True)
class SourceAuthorizationRef(CanonicalModel):
    source_manifest_ref: ArtifactRef
    source_id: str
    purpose: str

    def __post_init__(self) -> None:
        if type(self.source_manifest_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("source_manifest_ref must be an ArtifactRef")
        identifier(self.source_id, "authorization source_id")
        if self.purpose != "render":
            raise ProductionModelError("Candidate authorization purpose must be render")

    def to_mapping(self) -> dict[str, object]:
        return {
            "purpose": self.purpose,
            "source_id": self.source_id,
            "source_manifest_ref": self.source_manifest_ref.to_mapping(),
        }


@dataclass(frozen=True, slots=True, init=False)
class Candidate(EvaluatorOwnedModel):
    candidate_id: str
    event_refs: tuple[DomainRef, ...]
    source_ref: DomainRef
    source_sha256: str
    declared_spans: tuple[DeclaredSpan, ...]
    anchor_refs: tuple[SemanticAnchor, ...]
    semantic_measurements: tuple[SemanticMeasurement, ...]
    capability_assessment: CandidateCapabilityAssessment
    vlm_observation_ref: DomainRef
    vlm_observation_sha256: str
    duration_proof: TickDurationProof
    authorization_ref: SourceAuthorizationRef

    @classmethod
    def from_evaluation(
        cls,
        *,
        candidate_id: str,
        event_refs: Sequence[DomainRef],
        committed: CommittedVlmObservation,
        authority: OwnerBoundVlmObservationRef,
        declared_spans: Sequence[DeclaredSpan],
        anchors: Sequence[SemanticAnchor],
        measurements: Sequence[SemanticMeasurement],
        authorization_ref: SourceAuthorizationRef,
    ) -> Candidate:
        identifier(candidate_id, "candidate_id")
        if type(committed) is not CommittedVlmObservation or type(authority) is not OwnerBoundVlmObservationRef:  # noqa: E721
            raise ProductionModelError("Candidate requires committed evaluator authority")
        if (
            authority.observation_ref != committed.observation_ref
            or authority.vlm_observation_sha256 != committed.vlm_observation_sha256
            or authority.source_ref != committed.source_ref
            or authority.window_ref != committed.window_ref
        ):
            raise ProductionModelError("Candidate authority is unrelated to committed observation")
        events = canonical_domain_refs(event_refs, "candidate event_refs", nonempty=True)
        if any(item.object_type != "event" for item in events):
            raise ProductionModelError("candidate event_refs must point to EventCards")
        span_values = cast(
            tuple[DeclaredSpan, ...],
            canonical_values(declared_spans, DeclaredSpan, "declared_spans", nonempty=True),
        )
        anchors_values = cast(
            tuple[SemanticAnchor, ...],
            canonical_values(anchors, SemanticAnchor, "anchor_refs", nonempty=True),
        )
        measurement_values = cast(
            tuple[SemanticMeasurement, ...],
            canonical_values(
                measurements, SemanticMeasurement, "semantic_measurements", nonempty=True
            ),
        )
        if authority.capability_assessment.anchor_refs_hash != canonical_json_hash(
            [item.to_mapping() for item in anchors_values]
        ):
            raise ProductionModelError("Candidate anchors differ from evaluator anchors")
        if not set(authority.capability_assessment.supporting_measurement_ids) <= {
            item.measurement_id for item in measurement_values
        }:
            raise ProductionModelError("Candidate omitted evaluator supporting measurements")
        interval = committed.observation.source_interval
        if any(
            item.clock_id != committed.request_identity.source_clock_id
            or item.time_base.numerator != interval.source_time_base.numerator
            or item.time_base.denominator != interval.source_time_base.denominator
            or item.out_tick <= interval.coarse_range.start_pts
            or item.in_tick >= interval.coarse_range.end_pts
            for item in span_values
        ):
            raise ProductionModelError("declared span does not intersect committed coarse observation")
        if (
            type(authorization_ref) is not SourceAuthorizationRef
            or authorization_ref.source_id != committed.request_identity.source_id
        ):  # noqa: E721
            raise ProductionModelError("authorization does not bind Candidate Source")
        instance = object.__new__(cls)
        object.__setattr__(instance, "candidate_id", candidate_id)
        object.__setattr__(instance, "event_refs", events)
        object.__setattr__(instance, "source_ref", committed.source_ref)
        object.__setattr__(instance, "source_sha256", committed.request_identity.source_sha256)
        object.__setattr__(instance, "declared_spans", span_values)
        object.__setattr__(instance, "anchor_refs", anchors_values)
        object.__setattr__(instance, "semantic_measurements", measurement_values)
        object.__setattr__(instance, "capability_assessment", authority.capability_assessment)
        object.__setattr__(instance, "vlm_observation_ref", committed.observation_ref)
        object.__setattr__(instance, "vlm_observation_sha256", committed.vlm_observation_sha256)
        object.__setattr__(instance, "duration_proof", TickDurationProof.from_spans(span_values))
        object.__setattr__(instance, "authorization_ref", authorization_ref)
        return instance

    @property
    def editing_modes(self) -> tuple[EditingMode, ...]:
        return self.capability_assessment.editing_modes

    @property
    def supported_narrative_functions(self) -> tuple[NarrativeFunction, ...]:
        return self.capability_assessment.supported_narrative_functions

    def to_mapping(self) -> dict[str, object]:
        return {
            "anchor_refs": [item.to_mapping() for item in self.anchor_refs],
            "authorization_ref": self.authorization_ref.to_mapping(),
            "candidate_id": self.candidate_id,
            "capability_assessment": self.capability_assessment.to_mapping(),
            "declared_spans": [item.to_mapping() for item in self.declared_spans],
            "duration_proof": self.duration_proof.to_mapping(),
            "event_refs": [item.to_mapping() for item in self.event_refs],
            "semantic_measurements": [item.to_mapping() for item in self.semantic_measurements],
            "source_ref": self.source_ref.to_mapping(),
            "source_sha256": self.source_sha256,
            "vlm_observation_ref": self.vlm_observation_ref.to_mapping(),
            "vlm_observation_sha256": self.vlm_observation_sha256,
        }


@dataclass(frozen=True, slots=True)
class CandidateCatalog(CanonicalModel):
    candidate_catalog_id: str
    candidates: tuple[Candidate, ...]

    def __post_init__(self) -> None:
        identifier(self.candidate_catalog_id, "candidate_catalog_id")
        candidates = cast(
            tuple[Candidate, ...],
            canonical_values(self.candidates, Candidate, "candidates", nonempty=True),
        )
        if len({item.candidate_id for item in candidates}) != len(candidates):
            raise ProductionModelError("candidates must have unique IDs")
        object.__setattr__(self, "candidates", candidates)

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_catalog_id": self.candidate_catalog_id,
            "candidates": [item.to_mapping() for item in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class PhysicalRequirement(CanonicalModel):
    requirement_kind: str
    mode: str

    def __post_init__(self) -> None:
        if _PHYSICAL_REQUIREMENTS.get(self.requirement_kind) != self.mode:
            raise ProductionModelError("physical requirement kind/mode is unknown")

    def to_mapping(self) -> dict[str, object]:
        return {"mode": self.mode, "requirement_kind": self.requirement_kind}


def physical_tuple(
    values: Sequence[PhysicalRequirement], label: str
) -> tuple[PhysicalRequirement, ...]:
    result = cast(
        tuple[PhysicalRequirement, ...],
        canonical_values(values, PhysicalRequirement, label, nonempty=True),
    )
    kinds = tuple(item.requirement_kind for item in result)
    if len(kinds) != len(set(kinds)):
        raise ProductionModelError(f"{label} must not contain duplicate kinds")
    return result


@dataclass(frozen=True, slots=True)
class MaterialRequirement(CanonicalModel):
    requirement_id: str
    obligation_ref: DomainRef
    minimum_usable_seconds: int
    physical_requirements: tuple[PhysicalRequirement, ...]
    allowed_source_refs: tuple[DomainRef, ...]
    forbidden_source_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        identifier(self.requirement_id, "requirement_id")
        if type(self.obligation_ref) is not DomainRef or self.obligation_ref.object_type != "obligation":  # noqa: E721
            raise ProductionModelError("obligation_ref must point to an obligation")
        integer(self.minimum_usable_seconds, "minimum_usable_seconds", minimum=1)
        object.__setattr__(
            self,
            "physical_requirements",
            physical_tuple(self.physical_requirements, "physical_requirements"),
        )
        allowed = canonical_domain_refs(self.allowed_source_refs, "allowed_source_refs")
        forbidden = canonical_domain_refs(self.forbidden_source_refs, "forbidden_source_refs")
        if any(item.object_type != "source" for item in (*allowed, *forbidden)):
            raise ProductionModelError("source constraints must point to Source objects")
        if {jcs_key(item) for item in allowed} & {jcs_key(item) for item in forbidden}:
            raise ProductionModelError("allowed and forbidden sources must not overlap")
        object.__setattr__(self, "allowed_source_refs", allowed)
        object.__setattr__(self, "forbidden_source_refs", forbidden)

    @property
    def physical_requirements_hash(self) -> str:
        return canonical_json_hash([item.to_mapping() for item in self.physical_requirements])

    def to_mapping(self) -> dict[str, object]:
        return {
            "allowed_source_refs": [item.to_mapping() for item in self.allowed_source_refs],
            "forbidden_source_refs": [item.to_mapping() for item in self.forbidden_source_refs],
            "minimum_usable_seconds": self.minimum_usable_seconds,
            "obligation_ref": self.obligation_ref.to_mapping(),
            "physical_requirements": [item.to_mapping() for item in self.physical_requirements],
            "physical_requirements_hash": self.physical_requirements_hash,
            "requirement_id": self.requirement_id,
        }


@dataclass(frozen=True, slots=True, init=False)
class RequirementProof(EvaluatorOwnedModel):
    requirement_id: str
    status: str
    safe_candidate_refs: tuple[DomainRef, ...]
    excluded_tainted_candidate_refs: tuple[DomainRef, ...]
    source_refs: tuple[DomainRef, ...]
    physical_requirements_hash: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "excluded_tainted_candidate_refs": [
                item.to_mapping() for item in self.excluded_tainted_candidate_refs
            ],
            "physical_requirements_hash": self.physical_requirements_hash,
            "requirement_id": self.requirement_id,
            "safe_candidate_refs": [item.to_mapping() for item in self.safe_candidate_refs],
            "source_refs": [item.to_mapping() for item in self.source_refs],
            "status": self.status,
        }


@dataclass(frozen=True, slots=True, init=False)
class MaterialSupport(EvaluatorOwnedModel):
    status: str
    requirement_proofs: tuple[RequirementProof, ...]
    rule_results: tuple[RuleResult, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "requirement_proofs": [item.to_mapping() for item in self.requirement_proofs],
            "rule_results": [item.to_mapping() for item in self.rule_results],
            "status": self.status,
        }


class MaterialSupportEvaluator:
    """Recompute per-requirement support from the exact CandidateCatalog."""

    @staticmethod
    def evaluate(
        *,
        requirements: Sequence[MaterialRequirement],
        candidate_catalog_ref: ArtifactRef,
        candidate_catalog: CandidateCatalog,
        tainted_candidate_ids: Sequence[str] = (),
    ) -> MaterialSupport:
        if candidate_catalog_ref.content_hash != candidate_catalog.canonical_hash:
            raise ProductionModelError("CandidateCatalog ref does not bind exact catalog")
        requirement_values = cast(
            tuple[MaterialRequirement, ...],
            canonical_values(
                requirements, MaterialRequirement, "material requirements", nonempty=True
            ),
        )
        tainted = set(canonical_ids(tainted_candidate_ids, "tainted_candidate_ids"))
        proofs: list[RequirementProof] = []
        for requirement in requirement_values:
            safe: list[Candidate] = []
            excluded: list[Candidate] = []
            allowed = {jcs_key(item) for item in requirement.allowed_source_refs}
            forbidden = {jcs_key(item) for item in requirement.forbidden_source_refs}
            for candidate in candidate_catalog.candidates:
                if candidate.candidate_id in tainted:
                    excluded.append(candidate)
                    continue
                source_key = jcs_key(candidate.source_ref)
                if allowed and source_key not in allowed:
                    continue
                if source_key in forbidden:
                    continue
                if candidate.authorization_ref.purpose != "render":
                    continue
                if candidate.duration_proof.supports_seconds(
                    requirement.minimum_usable_seconds
                ):
                    safe.append(candidate)
            safe_refs = tuple(
                sorted(
                    (
                        DomainRef(candidate_catalog_ref, "candidate", item.candidate_id)
                        for item in safe
                    ),
                    key=jcs_key,
                )
            )
            excluded_refs = tuple(
                sorted(
                    (
                        DomainRef(candidate_catalog_ref, "candidate", item.candidate_id)
                        for item in excluded
                    ),
                    key=jcs_key,
                )
            )
            source_refs = tuple(
                sorted({item.source_ref for item in safe}, key=jcs_key)
            )
            proof = object.__new__(RequirementProof)
            object.__setattr__(proof, "requirement_id", requirement.requirement_id)
            object.__setattr__(proof, "status", "supported" if safe_refs else "unsupported")
            object.__setattr__(proof, "safe_candidate_refs", safe_refs)
            object.__setattr__(proof, "excluded_tainted_candidate_refs", excluded_refs)
            object.__setattr__(proof, "source_refs", source_refs)
            object.__setattr__(
                proof,
                "physical_requirements_hash",
                requirement.physical_requirements_hash,
            )
            proofs.append(proof)
        ordered_proofs = tuple(sorted(proofs, key=jcs_key))
        subject_hash = canonical_json_hash(
            {
                "candidate_catalog_ref": candidate_catalog_ref.to_mapping(),
                "requirements": [item.to_mapping() for item in requirement_values],
                "tainted_candidate_ids": sorted(tainted, key=jcs_key),
            }
        )
        support = object.__new__(MaterialSupport)
        object.__setattr__(
            support,
            "status",
            "supported"
            if all(item.status == "supported" for item in ordered_proofs)
            else "unsupported",
        )
        object.__setattr__(support, "requirement_proofs", ordered_proofs)
        object.__setattr__(
            support,
            "rule_results",
            computed_rule_results({"SD-MAT-001"}, subject_hash),
        )
        return support


@dataclass(frozen=True, slots=True)
class DependencyProjection(CanonicalModel):
    narrative_root_refs: tuple[DomainRef, ...]
    narrative_taint_seed_ids: tuple[str, ...]
    candidate_taint_seed_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "narrative_root_refs",
            canonical_domain_refs(self.narrative_root_refs, "narrative_root_refs", nonempty=True),
        )
        object.__setattr__(
            self,
            "narrative_taint_seed_ids",
            canonical_ids(self.narrative_taint_seed_ids, "narrative_taint_seed_ids"),
        )
        object.__setattr__(
            self,
            "candidate_taint_seed_ids",
            canonical_ids(self.candidate_taint_seed_ids, "candidate_taint_seed_ids"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_taint_seed_ids": list(self.candidate_taint_seed_ids),
            "narrative_root_refs": [item.to_mapping() for item in self.narrative_root_refs],
            "narrative_taint_seed_ids": list(self.narrative_taint_seed_ids),
        }


@dataclass(frozen=True, slots=True)
class Proposal(CanonicalModel):
    proposal_id: str
    story_id: str
    title: str
    narrative_claim: str
    thread_refs: tuple[DomainRef, ...]
    required_obligation_refs: tuple[DomainRef, ...]
    required_fact_refs: tuple[DomainRef, ...]
    key_character_refs: tuple[DomainRef, ...]
    genre_tags: tuple[str, ...]
    editing_profile: str
    target_duration_seconds: DurationRangeSeconds
    teaser_strategy: str
    material_requirements: tuple[MaterialRequirement, ...]
    material_support: MaterialSupport
    dependency_projection: DependencyProjection
    tainted_by: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier(self.proposal_id, "proposal_id")
        identifier(self.story_id, "story_id")
        text(self.title, "title")
        text(self.narrative_claim, "narrative_claim")
        ref_fields = (
            ("thread_refs", self.thread_refs, "story_thread", True),
            ("required_obligation_refs", self.required_obligation_refs, "obligation", True),
            ("required_fact_refs", self.required_fact_refs, "fact", True),
            ("key_character_refs", self.key_character_refs, "character", False),
        )
        for name, values, object_type, nonempty in ref_fields:
            refs = canonical_domain_refs(values, name, nonempty=nonempty)
            if any(item.object_type != object_type for item in refs):
                raise ProductionModelError(f"{name} has the wrong object type")
            object.__setattr__(self, name, refs)
        tags = canonical_ids(self.genre_tags, "genre_tags", nonempty=True)
        object.__setattr__(self, "genre_tags", tags)
        identifier(self.editing_profile, "editing_profile")
        identifier(self.teaser_strategy, "teaser_strategy")
        if type(self.target_duration_seconds) is not DurationRangeSeconds:  # noqa: E721
            raise ProductionModelError("target duration is invalid")
        requirements = cast(
            tuple[MaterialRequirement, ...],
            canonical_values(
                self.material_requirements,
                MaterialRequirement,
                "material_requirements",
                nonempty=True,
            ),
        )
        if len({item.requirement_id for item in requirements}) != len(requirements):
            raise ProductionModelError("material requirements must have unique IDs")
        if type(self.material_support) is not MaterialSupport:  # noqa: E721
            raise ProductionModelError("material_support must be evaluator output")
        proof_ids = {item.requirement_id for item in self.material_support.requirement_proofs}
        if proof_ids != {item.requirement_id for item in requirements}:
            raise ProductionModelError("material support does not exactly cover requirements")
        for requirement in requirements:
            proof = next(
                item
                for item in self.material_support.requirement_proofs
                if item.requirement_id == requirement.requirement_id
            )
            if proof.physical_requirements_hash != requirement.physical_requirements_hash:
                raise ProductionModelError("material proof changed physical requirements")
        if type(self.dependency_projection) is not DependencyProjection:  # noqa: E721
            raise ProductionModelError("dependency_projection is invalid")
        tainted = canonical_ids(self.tainted_by, "tainted_by")
        expected_taint = set(self.dependency_projection.narrative_taint_seed_ids) | set(
            self.dependency_projection.candidate_taint_seed_ids
        )
        if set(tainted) != expected_taint:
            raise ProductionModelError("Proposal tainted_by does not match dependency projection")
        object.__setattr__(self, "material_requirements", requirements)
        object.__setattr__(self, "tainted_by", tainted)

    def to_mapping(self) -> dict[str, object]:
        return {
            "dependency_projection": self.dependency_projection.to_mapping(),
            "editing_profile": self.editing_profile,
            "genre_tags": list(self.genre_tags),
            "key_character_refs": [item.to_mapping() for item in self.key_character_refs],
            "material_requirements": [item.to_mapping() for item in self.material_requirements],
            "material_support": self.material_support.to_mapping(),
            "narrative_claim": self.narrative_claim,
            "proposal_id": self.proposal_id,
            "required_fact_refs": [item.to_mapping() for item in self.required_fact_refs],
            "required_obligation_refs": [
                item.to_mapping() for item in self.required_obligation_refs
            ],
            "story_id": self.story_id,
            "tainted_by": list(self.tainted_by),
            "target_duration_seconds": self.target_duration_seconds.to_mapping(),
            "teaser_strategy": self.teaser_strategy,
            "thread_refs": [item.to_mapping() for item in self.thread_refs],
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class ProposalDisposition(CanonicalModel):
    proposal_id: str
    disposition: str
    rule_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier(self.proposal_id, "proposal disposition ID")
        if self.disposition not in {"accepted", "rejected", "indeterminate"}:
            raise ProductionModelError("proposal disposition is unknown")
        rules = canonical_ids(self.rule_ids, "proposal disposition rules")
        if self.disposition != "accepted" and not rules:
            raise ProductionModelError("failed Proposal disposition requires rule IDs")
        object.__setattr__(self, "rule_ids", rules)

    def to_mapping(self) -> dict[str, object]:
        return {
            "disposition": self.disposition,
            "proposal_id": self.proposal_id,
            "rule_ids": list(self.rule_ids),
        }


@dataclass(frozen=True, slots=True)
class ProposalSet(CanonicalModel):
    proposal_set_id: str
    job_policy_version: str
    proposals: tuple[Proposal, ...]
    dispositions: tuple[ProposalDisposition, ...]

    def __post_init__(self) -> None:
        identifier(self.proposal_set_id, "proposal_set_id")
        safe_token(self.job_policy_version, "job_policy_version")
        proposals = tuple(self.proposals)  # Proposal order is authoritative, not set-sorted.
        if not proposals or any(type(item) is not Proposal for item in proposals):  # noqa: E721
            raise ProductionModelError("proposals must be a non-empty Proposal sequence")
        if len({item.proposal_id for item in proposals}) != len(proposals):
            raise ProductionModelError("proposals must have unique IDs")
        dispositions = cast(
            tuple[ProposalDisposition, ...],
            canonical_values(
                self.dispositions,
                ProposalDisposition,
                "proposal dispositions",
                nonempty=True,
            ),
        )
        if {item.proposal_id for item in dispositions} != {item.proposal_id for item in proposals}:
            raise ProductionModelError("ProposalSet dispositions must cover every draft exactly")
        object.__setattr__(self, "proposals", proposals)
        object.__setattr__(self, "dispositions", dispositions)

    def disposition_for(self, proposal_id: str) -> ProposalDisposition:
        return next(item for item in self.dispositions if item.proposal_id == proposal_id)

    def to_mapping(self) -> dict[str, object]:
        return {
            "dispositions": [item.to_mapping() for item in self.dispositions],
            "job_policy_version": self.job_policy_version,
            "proposal_set_id": self.proposal_set_id,
            "proposals": [item.to_mapping() for item in self.proposals],
        }


@dataclass(frozen=True, slots=True)
class PortfolioPolicy(CanonicalModel):
    policy_id: str
    selected_story_count: int
    completion_policy: str

    def __post_init__(self) -> None:
        identifier(self.policy_id, "portfolio policy_id")
        integer(self.selected_story_count, "selected_story_count", minimum=1)
        if self.completion_policy not in {"independent_outputs", "all_or_nothing"}:
            raise ProductionModelError("completion_policy is unknown")

    def to_mapping(self) -> dict[str, object]:
        return {
            "completion_policy": self.completion_policy,
            "policy_id": self.policy_id,
            "selected_story_count": self.selected_story_count,
        }


@dataclass(frozen=True, slots=True)
class PortfolioSelectionRecord(CanonicalModel):
    story_id: str
    proposal_id: str
    proposal_index: int
    hard_constraint_results: tuple[RuleResult, ...]

    def __post_init__(self) -> None:
        identifier(self.story_id, "selection story_id")
        identifier(self.proposal_id, "selection proposal_id")
        integer(self.proposal_index, "proposal_index")
        results = cast(
            tuple[RuleResult, ...],
            canonical_values(
                self.hard_constraint_results,
                RuleResult,
                "hard_constraint_results",
                nonempty=True,
            ),
        )
        if any(item.status != "pass" for item in results):
            raise ProductionModelError("selected Proposal has a failed hard constraint")
        object.__setattr__(self, "hard_constraint_results", results)

    def to_mapping(self) -> dict[str, object]:
        return {
            "hard_constraint_results": [
                item.to_mapping() for item in self.hard_constraint_results
            ],
            "proposal_id": self.proposal_id,
            "proposal_index": self.proposal_index,
            "selected": True,
            "story_id": self.story_id,
        }


@dataclass(frozen=True, slots=True, init=False)
class Portfolio(EvaluatorOwnedModel):
    portfolio_id: str
    proposal_set_ref: ArtifactRef
    job_policy_ref: ArtifactRef
    completion_policy: str
    target_story_ids: tuple[str, ...]
    target_story_ids_hash: str
    selection_records: tuple[PortfolioSelectionRecord, ...]
    feasible_tuple_hash: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "completion_policy": self.completion_policy,
            "feasible_tuple_hash": self.feasible_tuple_hash,
            "job_policy_ref": self.job_policy_ref.to_mapping(),
            "portfolio_id": self.portfolio_id,
            "proposal_set_ref": self.proposal_set_ref.to_mapping(),
            "selection_records": [item.to_mapping() for item in self.selection_records],
            "target_story_ids": list(self.target_story_ids),
            "target_story_ids_hash": self.target_story_ids_hash,
        }


class PortfolioCompiler:
    """Compile the lexicographically first fully feasible exact-size tuple."""

    @staticmethod
    def compile(
        *,
        portfolio_id: str,
        proposal_set_ref: ArtifactRef,
        proposal_set: ProposalSet,
        job_policy_ref: ArtifactRef,
        job_policy: PortfolioPolicy,
        hard_constraint_results: Sequence[tuple[str, Sequence[RuleResult]]],
    ) -> Portfolio:
        identifier(portfolio_id, "portfolio_id")
        if proposal_set_ref.content_hash != proposal_set.canonical_hash:
            raise ProductionModelError("proposal_set_ref does not bind the exact ProposalSet")
        if job_policy_ref.content_hash != job_policy.canonical_hash:
            raise ProductionModelError("job_policy_ref does not bind the exact Portfolio policy")
        result_by_id = {proposal_id: tuple(values) for proposal_id, values in hard_constraint_results}
        if set(result_by_id) != {item.proposal_id for item in proposal_set.proposals}:
            raise ProductionModelError("hard constraint results do not cover exact ProposalSet")
        feasible: list[int] = []
        for index, proposal in enumerate(proposal_set.proposals):
            results = result_by_id[proposal.proposal_id]
            if not results or any(type(item) is not RuleResult for item in results):  # noqa: E721
                raise ProductionModelError("hard constraint results are incomplete")
            disposition = proposal_set.disposition_for(proposal.proposal_id)
            if (
                disposition.disposition == "accepted"
                and proposal.material_support.status == "supported"
                and not proposal.tainted_by
                and all(item.status == "pass" for item in results)
            ):
                feasible.append(index)
        chosen = next(
            combinations(feasible, job_policy.selected_story_count),
            None,
        )
        if chosen is None:
            raise ProductionModelError("no fully feasible Portfolio of the frozen size exists")
        records = tuple(
            PortfolioSelectionRecord(
                proposal_set.proposals[index].story_id,
                proposal_set.proposals[index].proposal_id,
                index,
                tuple(sorted(result_by_id[proposal_set.proposals[index].proposal_id], key=jcs_key)),
            )
            for index in chosen
        )
        targets = tuple(item.story_id for item in records)
        instance = object.__new__(Portfolio)
        object.__setattr__(instance, "portfolio_id", portfolio_id)
        object.__setattr__(instance, "proposal_set_ref", proposal_set_ref)
        object.__setattr__(instance, "job_policy_ref", job_policy_ref)
        object.__setattr__(instance, "completion_policy", job_policy.completion_policy)
        object.__setattr__(instance, "target_story_ids", targets)
        object.__setattr__(instance, "target_story_ids_hash", canonical_json_hash(list(targets)))
        object.__setattr__(instance, "selection_records", records)
        object.__setattr__(instance, "feasible_tuple_hash", canonical_json_hash(list(chosen)))
        return instance


@dataclass(frozen=True, slots=True)
class SourceUsageRow(CanonicalModel):
    story_id: str
    priority_index: int
    status: str

    def __post_init__(self) -> None:
        identifier(self.story_id, "usage story_id")
        integer(self.priority_index, "usage priority_index")
        if self.status != "pending":
            raise ProductionModelError("initial SourceUsage row must be pending")

    def to_mapping(self) -> dict[str, object]:
        return {
            "priority_index": self.priority_index,
            "status": self.status,
            "story_id": self.story_id,
        }


@dataclass(frozen=True, slots=True)
class SourceUsageLedger(CanonicalModel):
    source_usage_ledger_id: str
    rows: tuple[SourceUsageRow, ...]
    next_priority_index: int
    finalized: bool

    def __post_init__(self) -> None:
        identifier(self.source_usage_ledger_id, "source_usage_ledger_id")
        rows = tuple(self.rows)  # target order is authoritative.
        if not rows or any(type(item) is not SourceUsageRow for item in rows):  # noqa: E721
            raise ProductionModelError("SourceUsageLedger rows are invalid")
        if tuple(item.priority_index for item in rows) != tuple(range(len(rows))):
            raise ProductionModelError("SourceUsageLedger priority indices are not exact")
        if len({item.story_id for item in rows}) != len(rows):
            raise ProductionModelError("SourceUsageLedger has duplicate Story rows")
        if self.next_priority_index != 0 or self.finalized is not False:
            raise ProductionModelError("initial SourceUsageLedger must be pending and unfinalized")
        object.__setattr__(self, "rows", rows)

    @classmethod
    def for_portfolio(cls, ledger_id: str, portfolio: Portfolio) -> SourceUsageLedger:
        return cls(
            ledger_id,
            tuple(SourceUsageRow(story_id, index, "pending") for index, story_id in enumerate(portfolio.target_story_ids)),
            0,
            False,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "finalized": self.finalized,
            "next_priority_index": self.next_priority_index,
            "rows": [item.to_mapping() for item in self.rows],
            "source_usage_ledger_id": self.source_usage_ledger_id,
        }


@dataclass(frozen=True, slots=True, init=False)
class PortfolioAdmission(EvaluatorOwnedModel):
    admission_id: str
    pending_set_hash: str
    portfolio_ref: ArtifactRef
    target_story_ids: tuple[str, ...]
    target_story_ids_hash: str
    source_usage_ledger_ref: ArtifactRef
    frozen_by_admission_id: str
    next_action: str
    rule_results: tuple[RuleResult, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "admission_id": self.admission_id,
            "frozen_by_admission_id": self.frozen_by_admission_id,
            "kind": "portfolio",
            "next_action": self.next_action,
            "pending_set_hash": self.pending_set_hash,
            "portfolio_ref": self.portfolio_ref.to_mapping(),
            "rule_results": [item.to_mapping() for item in self.rule_results],
            "source_usage_ledger_ref": self.source_usage_ledger_ref.to_mapping(),
            "target_story_ids": list(self.target_story_ids),
            "target_story_ids_hash": self.target_story_ids_hash,
        }


_PORTFOLIO_RULES: Final = {
    "SD-PROP-001",
    "SD-MAT-001",
    "SD-MAT-002",
    "SD-TAINT-001",
    "SD-PORT-001",
    "SD-PORT-003",
    "SD-OBJ-001",
    "SD-FREEZE-001",
    "SD-USAGE-001",
}
_STAGE2_MEMBER_TYPES: Final = {
    "candidate_catalog",
    "proposal_set",
    "portfolio",
    "source_usage_ledger",
}


class PortfolioAdmissionEvaluator:
    """Mint continue only after exact Catalog/Proposal/Portfolio/Usage joins."""

    @staticmethod
    def evaluate(
        *,
        admission_id: str,
        pending_set: PendingBusinessSet,
        candidate_catalog: CandidateCatalog,
        proposal_set: ProposalSet,
        portfolio: Portfolio,
        source_usage_ledger: SourceUsageLedger,
    ) -> PortfolioAdmission:
        identifier(admission_id, "admission_id")
        if type(pending_set) is not PendingBusinessSet or pending_set.admission_kind != "portfolio":  # noqa: E721
            raise ProductionModelError("Portfolio evaluator requires portfolio pending set")
        pending_set.require_exact_types(_STAGE2_MEMBER_TYPES)
        catalog_ref = pending_set.require_member("candidate_catalog", candidate_catalog)
        proposal_ref = pending_set.require_member("proposal_set", proposal_set)
        portfolio_ref = pending_set.require_member("portfolio", portfolio)
        usage_ref = pending_set.require_member("source_usage_ledger", source_usage_ledger)
        if portfolio.proposal_set_ref != proposal_ref:
            raise ProductionModelError("Portfolio does not bind exact pending ProposalSet")
        candidate_ids = {item.candidate_id for item in candidate_catalog.candidates}
        proposal_by_id = {item.proposal_id: item for item in proposal_set.proposals}
        for record in portfolio.selection_records:
            if record.proposal_index >= len(proposal_set.proposals):
                raise ProductionModelError("Portfolio proposal index is outside exact ProposalSet")
            proposal = proposal_set.proposals[record.proposal_index]
            if proposal.proposal_id != record.proposal_id or proposal.story_id != record.story_id:
                raise ProductionModelError("Portfolio selection record does not join ProposalSet")
            if proposal_by_id[record.proposal_id].material_support.status != "supported":
                raise ProductionModelError("Portfolio selected an unsupported Proposal")
            recomputed_support = MaterialSupportEvaluator.evaluate(
                requirements=proposal.material_requirements,
                candidate_catalog_ref=catalog_ref,
                candidate_catalog=candidate_catalog,
            )
            if recomputed_support != proposal.material_support:
                raise ProductionModelError("Proposal material support was not evaluator-derived")
            for proof in proposal.material_support.requirement_proofs:
                if any(ref.object_id not in candidate_ids for ref in proof.safe_candidate_refs):
                    raise ProductionModelError("material support refers outside CandidateCatalog")
        usage_targets = tuple(item.story_id for item in source_usage_ledger.rows)
        if usage_targets != portfolio.target_story_ids:
            raise ProductionModelError("SourceUsageLedger does not exactly match frozen targets")
        rules = computed_rule_results(_PORTFOLIO_RULES, pending_set.canonical_hash)
        instance = object.__new__(PortfolioAdmission)
        object.__setattr__(instance, "admission_id", admission_id)
        object.__setattr__(instance, "pending_set_hash", pending_set.canonical_hash)
        object.__setattr__(instance, "portfolio_ref", portfolio_ref)
        object.__setattr__(instance, "target_story_ids", portfolio.target_story_ids)
        object.__setattr__(instance, "target_story_ids_hash", portfolio.target_story_ids_hash)
        object.__setattr__(instance, "source_usage_ledger_ref", usage_ref)
        object.__setattr__(instance, "frozen_by_admission_id", admission_id)
        object.__setattr__(instance, "next_action", "continue")
        object.__setattr__(instance, "rule_results", rules)
        return instance


__all__ = [
    "Candidate",
    "CandidateCapabilityAssessment",
    "CandidateCapabilityEvaluator",
    "CandidateCapabilityPolicy",
    "CandidateCatalog",
    "CandidateMeasurementPolicy",
    "CapabilityPredicate",
    "CapabilityRule",
    "CommittedVlmObservation",
    "DeclaredSpan",
    "DependencyProjection",
    "EditingMode",
    "MaterialRequirement",
    "MaterialSupport",
    "MaterialSupportEvaluator",
    "NarrativeFunction",
    "OwnerBoundVlmObservationRef",
    "PhysicalRequirement",
    "Portfolio",
    "PortfolioAdmission",
    "PortfolioAdmissionEvaluator",
    "PortfolioCompiler",
    "PortfolioPolicy",
    "PortfolioSelectionRecord",
    "Proposal",
    "ProposalDisposition",
    "ProposalSet",
    "RequirementProof",
    "SemanticAnchor",
    "SemanticMeasurement",
    "SourceAuthorizationRef",
    "SourceUsageLedger",
    "SourceUsageRow",
    "TickDurationProof",
    "physical_tuple",
]
