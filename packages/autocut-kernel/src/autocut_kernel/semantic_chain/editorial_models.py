"""Closed Stage 3 narrative intent values, never commitment or Admission.

Beat positions are local array ordinals, not model-written artifact/Beat IDs.
GapDuration is an elapsed duration, not a Source endpoint. Physical requirements
are deliberately absent: the compiler must copy them from the selected Stage 2
material requirements. Object existence, semantic support, cycles, feasibility
and admission still require independent evaluation of committed predecessors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal, TypeVar, cast

from ..contracts.compiler.canonical import canonical_json_hash
from ..media.types import TimeBase
from ..vlm.models import VlmNarrativeFunction
from .member_refs import SemanticObjectRef
from .story_design_models import IntegerRange

SpanIntent = Literal["tight", "scene", "context"]
NARRATIVE_ROLES = ("setup", "escalation", "turn", "reveal", "payoff", "consequence", "coda")
SPAN_INTENTS = ("tight", "scene", "context")
_T = TypeVar("_T")
_SAFE_INTEGER = 2**53 - 1
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")


class EditorialModelError(ValueError):
    """Malformed narrative intent, not a business admission decision."""


def editorial_text(value: object) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise EditorialModelError("editorial text must be nonempty UTF-8")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise EditorialModelError("editorial text must be nonempty UTF-8") from error
    return value


def editorial_hash(value: object) -> str:
    value = editorial_text(value)
    if _HASH.fullmatch(value) is None or value == "sha256:" + "0" * 64:
        raise EditorialModelError("editorial identity must be a nonzero lowercase sha256")
    return value


def editorial_integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _SAFE_INTEGER:  # noqa: E721
        raise EditorialModelError("editorial number must be a bounded exact integer")
    return value


def editorial_mapping(value: object, names: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise EditorialModelError("editorial value must be a closed object")
    result = cast(dict[str, object], value)
    if any(type(key) is not str for key in result) or set(result) != set(names):  # noqa: E721
        raise EditorialModelError("editorial object has missing or unknown fields")
    return result


def editorial_array(value: object, decoder: Callable[[object], _T]) -> tuple[_T, ...]:
    if type(value) is not list:  # noqa: E721
        raise EditorialModelError("editorial wire collection must be an array")
    return tuple(decoder(item) for item in cast(list[object], value))


def editorial_tuple(value: object, kind: type[_T], *, nonempty: bool = False) -> tuple[_T, ...]:
    if type(value) is not tuple:  # noqa: E721
        raise EditorialModelError("editorial collection must be an immutable tuple")
    items = cast(tuple[object, ...], value)
    if (nonempty and not items) or any(type(item) is not kind for item in items):
        raise EditorialModelError("editorial collection has invalid items or is empty")
    return cast(tuple[_T, ...], items)


def _unique(items: tuple[object, ...]) -> None:
    if len(set(items)) != len(items):
        raise EditorialModelError("editorial collection contains duplicates")


def _enum(value: object, allowed: tuple[str, ...]) -> str:
    value = editorial_text(value)
    if value not in allowed:
        raise EditorialModelError("unsupported editorial enum value")
    return value


def _ref(value: object, artifact_type: str, object_type: str) -> SemanticObjectRef:
    if (type(value) is not SemanticObjectRef or value.member_ref.artifact_type != artifact_type  # noqa: E721
            or value.object_type != object_type):
        raise EditorialModelError("editorial reference has the wrong exact owner/object type")
    editorial_hash(value.member_ref.content_hash)
    return value


def _refs(value: object, artifact_type: str, object_type: str, *, nonempty: bool = False) -> tuple[SemanticObjectRef, ...]:
    refs = editorial_tuple(value, SemanticObjectRef, nonempty=nonempty)
    _unique(refs)
    for ref in refs:
        _ref(ref, artifact_type, object_type)
    validate_editorial_owners(refs)
    return refs


def validate_editorial_owners(refs: tuple[SemanticObjectRef, ...]) -> None:
    """One scope and one exact member per artifact kind, not object existence."""
    if len({ref.member_ref.scope for ref in refs}) > 1:
        raise EditorialModelError("editorial references mix scopes")
    owners = {ref.member_ref.artifact_type: ref.member_ref for ref in refs}
    if any(owners[ref.member_ref.artifact_type] != ref.member_ref for ref in refs):
        raise EditorialModelError("editorial references mix exact member owners")


@dataclass(frozen=True, slots=True)
class DurationRange:
    minimum: int
    target: int
    maximum: int

    def __post_init__(self) -> None:
        for value in (self.minimum, self.target, self.maximum):
            editorial_integer(value, minimum=1)
        if not self.minimum <= self.target <= self.maximum:
            raise EditorialModelError("duration must satisfy min <= target <= max")

    def to_mapping(self) -> dict[str, object]:
        return {"min": self.minimum, "target": self.target, "max": self.maximum}

    @classmethod
    def from_mapping(cls, value: object) -> DurationRange:
        item = editorial_mapping(value, ("min", "target", "max"))
        return cls(*(editorial_integer(item[key], minimum=1) for key in ("min", "target", "max")))


@dataclass(frozen=True, slots=True)
class GapDuration:
    tick: int
    time_base: TimeBase

    def __post_init__(self) -> None:
        editorial_integer(self.tick)
        if type(self.time_base) is not TimeBase:  # noqa: E721
            raise EditorialModelError("gap duration requires an exact rational TimeBase")
        editorial_integer(self.time_base.numerator, minimum=1)
        editorial_integer(self.time_base.denominator, minimum=1)

    def to_mapping(self) -> dict[str, object]:
        return {"tick": self.tick, "time_base": {
            "num": self.time_base.numerator, "den": self.time_base.denominator,
        }}

    @classmethod
    def from_mapping(cls, value: object) -> GapDuration:
        item = editorial_mapping(value, ("tick", "time_base"))
        clock = editorial_mapping(item["time_base"], ("num", "den"))
        return cls(editorial_integer(item["tick"]), TimeBase(
            editorial_integer(clock["num"], minimum=1), editorial_integer(clock["den"], minimum=1),
        ))


@dataclass(frozen=True, slots=True)
class SpanPolicy:
    preferred: SpanIntent
    allowed: tuple[SpanIntent, ...]
    fallback_order: tuple[SpanIntent, ...]

    def __post_init__(self) -> None:
        _enum(self.preferred, SPAN_INTENTS)
        for items in (self.allowed, self.fallback_order):
            editorial_tuple(items, str, nonempty=True)
            _unique(items)
            for item in items:
                _enum(item, SPAN_INTENTS)
        if self.preferred not in self.allowed or set(self.fallback_order) != set(self.allowed):
            raise EditorialModelError("span preferred/allowed/fallback permutation differs")

    def to_mapping(self) -> dict[str, object]:
        return {"preferred": self.preferred, "allowed": list(self.allowed), "fallback_order": list(self.fallback_order)}

    @classmethod
    def from_mapping(cls, value: object) -> SpanPolicy:
        item = editorial_mapping(value, ("preferred", "allowed", "fallback_order"))
        return cls(cast(SpanIntent, editorial_text(item["preferred"])),
                   cast(tuple[SpanIntent, ...], editorial_array(item["allowed"], editorial_text)),
                   cast(tuple[SpanIntent, ...], editorial_array(item["fallback_order"], editorial_text)))


@dataclass(frozen=True, slots=True)
class EditingIntent:
    pacing: str
    continuity_priority: str

    def __post_init__(self) -> None:
        _enum(self.pacing, ("slow", "balanced", "fast"))
        _enum(self.continuity_priority, ("low", "medium", "high"))

    def to_mapping(self) -> dict[str, object]:
        return {"pacing": self.pacing, "continuity_priority": self.continuity_priority}

    @classmethod
    def from_mapping(cls, value: object) -> EditingIntent:
        item = editorial_mapping(value, ("pacing", "continuity_priority"))
        return cls(editorial_text(item["pacing"]), editorial_text(item["continuity_priority"]))


@dataclass(frozen=True, slots=True)
class TeaserIntent:
    strategy: str
    duration_seconds: IntegerRange

    def __post_init__(self) -> None:
        # The selected Proposal/StoryDesignPolicy owns the explicit allowlist.
        # This value does not introduce obsolete global strategy aliases.
        editorial_text(self.strategy)
        if type(self.duration_seconds) is not IntegerRange:  # noqa: E721
            raise EditorialModelError("teaser duration requires an exact IntegerRange")

    def to_mapping(self) -> dict[str, object]:
        return {"strategy": self.strategy, "duration_seconds": self.duration_seconds.to_mapping()}

    @classmethod
    def from_mapping(cls, value: object) -> TeaserIntent:
        item = editorial_mapping(value, ("strategy", "duration_seconds"))
        return cls(editorial_text(item["strategy"]), IntegerRange.from_mapping(item["duration_seconds"]))


@dataclass(frozen=True, slots=True)
class EvidenceAlternative:
    alternative_id: str
    event_refs: tuple[SemanticObjectRef, ...]
    candidate_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        editorial_text(self.alternative_id)
        _refs(self.event_refs, "event_card_set", "event", nonempty=True)
        _refs(self.candidate_refs, "candidate_catalog", "candidate", nonempty=True)
        validate_editorial_owners(self.references)

    @property
    def references(self) -> tuple[SemanticObjectRef, ...]:
        return self.event_refs + self.candidate_refs

    def to_mapping(self) -> dict[str, object]:
        return {"alternative_id": self.alternative_id, "event_refs": [ref.to_mapping() for ref in self.event_refs],
                "candidate_refs": [ref.to_mapping() for ref in self.candidate_refs]}

    @classmethod
    def from_mapping(cls, value: object) -> EvidenceAlternative:
        item = editorial_mapping(value, ("alternative_id", "event_refs", "candidate_refs"))
        return cls(editorial_text(item["alternative_id"]), editorial_array(item["event_refs"], SemanticObjectRef.from_mapping),
                   editorial_array(item["candidate_refs"], SemanticObjectRef.from_mapping))


@dataclass(frozen=True, slots=True)
class EvidenceRequirementDraft:
    source_material_requirement_id: str
    satisfaction: str
    alternative_sets: tuple[EvidenceAlternative, ...]

    def __post_init__(self) -> None:
        editorial_text(self.source_material_requirement_id)
        _enum(self.satisfaction, ("one_of", "all_of"))
        editorial_tuple(self.alternative_sets, EvidenceAlternative, nonempty=True)
        _unique(tuple(item.alternative_id for item in self.alternative_sets))
        validate_editorial_owners(self.references)

    @property
    def references(self) -> tuple[SemanticObjectRef, ...]:
        return tuple(ref for alternative in self.alternative_sets for ref in alternative.references)

    def to_mapping(self) -> dict[str, object]:
        return {"source_material_requirement_id": self.source_material_requirement_id,
                "satisfaction": self.satisfaction, "alternative_sets": [item.to_mapping() for item in self.alternative_sets]}

    @classmethod
    def from_mapping(cls, value: object) -> EvidenceRequirementDraft:
        item = editorial_mapping(value, ("source_material_requirement_id", "satisfaction", "alternative_sets"))
        return cls(editorial_text(item["source_material_requirement_id"]), editorial_text(item["satisfaction"]),
                   editorial_array(item["alternative_sets"], EvidenceAlternative.from_mapping))


def _ordinals(left: int, right: int) -> None:
    editorial_integer(left)
    editorial_integer(right)
    if left == right:
        raise EditorialModelError("ordering cannot reference the same Beat twice")


@dataclass(frozen=True, slots=True)
class Precedes:
    before_ordinal: int
    after_ordinal: int

    def __post_init__(self) -> None:
        _ordinals(self.before_ordinal, self.after_ordinal)

    def to_mapping(self) -> dict[str, object]:
        return {"constraint_type": "precedes", "before_ordinal": self.before_ordinal, "after_ordinal": self.after_ordinal}


@dataclass(frozen=True, slots=True)
class Adjacent:
    first_ordinal: int
    second_ordinal: int

    def __post_init__(self) -> None:
        _ordinals(self.first_ordinal, self.second_ordinal)

    def to_mapping(self) -> dict[str, object]:
        return {"constraint_type": "adjacent", "first_ordinal": self.first_ordinal, "second_ordinal": self.second_ordinal}


@dataclass(frozen=True, slots=True)
class MaxGap:
    before_ordinal: int
    after_ordinal: int
    maximum_gap: GapDuration

    def __post_init__(self) -> None:
        _ordinals(self.before_ordinal, self.after_ordinal)
        if type(self.maximum_gap) is not GapDuration:  # noqa: E721
            raise EditorialModelError("max_gap requires an exact elapsed GapDuration")

    def to_mapping(self) -> dict[str, object]:
        return {"constraint_type": "max_gap", "before_ordinal": self.before_ordinal, "after_ordinal": self.after_ordinal,
                "maximum_gap": self.maximum_gap.to_mapping()}


OrderingConstraint = Precedes | Adjacent | MaxGap


def decode_editorial_ordering(value: object) -> OrderingConstraint:
    if type(value) is not dict:  # noqa: E721
        raise EditorialModelError("ordering must be a closed discriminated object")
    kind = cast(dict[str, object], value).get("constraint_type")
    if type(kind) is not str or kind not in ("precedes", "adjacent", "max_gap"):  # noqa: E721
        raise EditorialModelError("unsupported ordering constraint type")
    names = ("first_ordinal", "second_ordinal") if kind == "adjacent" else ("before_ordinal", "after_ordinal")
    item = editorial_mapping(cast(dict[str, object], value), ("constraint_type", *names, *(("maximum_gap",) if kind == "max_gap" else ())))
    left, right = (editorial_integer(item[key]) for key in names)
    if kind == "adjacent":
        return Adjacent(left, right)
    if kind == "precedes":
        return Precedes(left, right)
    return MaxGap(left, right, GapDuration.from_mapping(item["maximum_gap"]))


@dataclass(frozen=True, slots=True)
class EditorialBeatDraft:
    narrative_role: str
    narrative_function: VlmNarrativeFunction
    summary: str
    required_obligation_refs: tuple[SemanticObjectRef, ...]
    required_fact_refs: tuple[SemanticObjectRef, ...]
    evidence_requirements: tuple[EvidenceRequirementDraft, ...]
    candidate_preferences: tuple[SemanticObjectRef, ...]
    span_policy: SpanPolicy
    duration_seconds: DurationRange

    def __post_init__(self) -> None:
        _enum(self.narrative_role, NARRATIVE_ROLES)
        if type(self.narrative_function) is not VlmNarrativeFunction:  # noqa: E721
            raise EditorialModelError("narrative function requires the exact VLM v3 enum")
        editorial_text(self.summary)
        _refs(self.required_obligation_refs, "narrative_graph", "obligation")
        _refs(self.required_fact_refs, "narrative_graph", "fact")
        editorial_tuple(self.evidence_requirements, EvidenceRequirementDraft, nonempty=True)
        _unique(tuple(item.source_material_requirement_id for item in self.evidence_requirements))
        _refs(self.candidate_preferences, "candidate_catalog", "candidate")
        candidates = {ref for req in self.evidence_requirements for alt in req.alternative_sets for ref in alt.candidate_refs}
        if not set(self.candidate_preferences) <= candidates:
            raise EditorialModelError("candidate preferences must refer to declared alternatives")
        if type(self.span_policy) is not SpanPolicy or type(self.duration_seconds) is not DurationRange:  # noqa: E721
            raise EditorialModelError("Beat span/duration must be exact typed values")
        validate_editorial_owners(self.references)

    @property
    def references(self) -> tuple[SemanticObjectRef, ...]:
        return (*self.required_obligation_refs, *self.required_fact_refs, *self.candidate_preferences,
                *(ref for requirement in self.evidence_requirements for ref in requirement.references))

    def to_mapping(self) -> dict[str, object]:
        return {"narrative_role": self.narrative_role, "narrative_function": self.narrative_function.value,
                "summary": self.summary, "required_obligation_refs": [ref.to_mapping() for ref in self.required_obligation_refs],
                "required_fact_refs": [ref.to_mapping() for ref in self.required_fact_refs],
                "evidence_requirements": [item.to_mapping() for item in self.evidence_requirements],
                "candidate_preferences": [ref.to_mapping() for ref in self.candidate_preferences],
                "span_policy": self.span_policy.to_mapping(), "duration_seconds": self.duration_seconds.to_mapping()}

    @classmethod
    def from_mapping(cls, value: object) -> EditorialBeatDraft:
        item = editorial_mapping(value, ("narrative_role", "narrative_function", "summary", "required_obligation_refs",
                                        "required_fact_refs", "evidence_requirements", "candidate_preferences",
                                        "span_policy", "duration_seconds"))
        return cls(editorial_text(item["narrative_role"]), VlmNarrativeFunction(editorial_text(item["narrative_function"])),
                   editorial_text(item["summary"]), editorial_array(item["required_obligation_refs"], SemanticObjectRef.from_mapping),
                   editorial_array(item["required_fact_refs"], SemanticObjectRef.from_mapping),
                   editorial_array(item["evidence_requirements"], EvidenceRequirementDraft.from_mapping),
                   editorial_array(item["candidate_preferences"], SemanticObjectRef.from_mapping),
                   SpanPolicy.from_mapping(item["span_policy"]), DurationRange.from_mapping(item["duration_seconds"]))


@dataclass(frozen=True, slots=True)
class StoryBlueprintDraft:
    story_id: str
    proposal_ref: SemanticObjectRef
    beats: tuple[EditorialBeatDraft, ...]
    ordering_constraints: tuple[OrderingConstraint, ...]
    story_duration_seconds: DurationRange
    editing_intent: EditingIntent
    teaser_intent: TeaserIntent

    def __post_init__(self) -> None:
        editorial_hash(self.story_id)
        _ref(self.proposal_ref, "proposal_set", "proposal")
        editorial_tuple(self.beats, EditorialBeatDraft, nonempty=True)
        if type(self.ordering_constraints) is not tuple or any(  # noqa: E721
            type(item) not in (Precedes, Adjacent, MaxGap) for item in self.ordering_constraints
        ):
            raise EditorialModelError("ordering requires a tuple of exact closed variants")
        _unique(self.ordering_constraints)
        for item in self.ordering_constraints:
            ends = ((item.first_ordinal, item.second_ordinal) if isinstance(item, Adjacent)
                    else (item.before_ordinal, item.after_ordinal))
            if max(ends) >= len(self.beats):
                raise EditorialModelError("ordering ordinal does not resolve in this Story")
        _unique(tuple(req.source_material_requirement_id for beat in self.beats for req in beat.evidence_requirements))
        if (type(self.story_duration_seconds) is not DurationRange or type(self.editing_intent) is not EditingIntent  # noqa: E721
                or type(self.teaser_intent) is not TeaserIntent):  # noqa: E721
            raise EditorialModelError("Story duration and intents must be exact typed values")
        validate_editorial_owners(self.references)

    @property
    def references(self) -> tuple[SemanticObjectRef, ...]:
        return (self.proposal_ref, *(ref for beat in self.beats for ref in beat.references))

    def to_mapping(self) -> dict[str, object]:
        return {"story_id": self.story_id, "proposal_ref": self.proposal_ref.to_mapping(),
                "beats": [beat.to_mapping() for beat in self.beats],
                "ordering_constraints": [item.to_mapping() for item in self.ordering_constraints],
                "story_duration_seconds": self.story_duration_seconds.to_mapping(),
                "editing_intent": self.editing_intent.to_mapping(), "teaser_intent": self.teaser_intent.to_mapping()}

    @classmethod
    def from_mapping(cls, value: object) -> StoryBlueprintDraft:
        item = editorial_mapping(value, ("story_id", "proposal_ref", "beats", "ordering_constraints",
                                        "story_duration_seconds", "editing_intent", "teaser_intent"))
        return cls(editorial_hash(item["story_id"]), SemanticObjectRef.from_mapping(item["proposal_ref"]),
                   editorial_array(item["beats"], EditorialBeatDraft.from_mapping),
                   editorial_array(item["ordering_constraints"], decode_editorial_ordering),
                   DurationRange.from_mapping(item["story_duration_seconds"]), EditingIntent.from_mapping(item["editing_intent"]),
                   TeaserIntent.from_mapping(item["teaser_intent"]))

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())
