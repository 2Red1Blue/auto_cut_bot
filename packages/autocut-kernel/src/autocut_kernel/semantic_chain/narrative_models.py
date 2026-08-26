"""Strict Stage 1 business values, never proof of admission or commitment.

Local Graph references are checked here. Exact external member/object existence,
Source authorization, identity evidence truth, state-fact kind/window policy and
coverage remain independent compiler/evaluator responsibilities. Cycles are
preserved for diagnostics, not silently repaired. Native evidence intervals stay
coarse and retain their uncertainty; they are not editing endpoints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, TypeVar, cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..media.types import TickRange, TimeBase
from ..vlm.models import MappedSourceInterval
from .member_refs import SemanticMemberIdentity, SemanticObjectRef, SemanticReferenceError

_T = TypeVar("_T")
_SAFE = 2**53 - 1
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z")
_ENTITY_KINDS = ("person", "object", "location", "screen_text_source")
_PHASES = ("setup", "escalation", "turn", "reveal", "payoff", "consequence", "coda")
_EDGE_KINDS = (
    "supports",
    "satisfies",
    "requires",
    "precedes",
    "causes",
    "contradicts",
    "involves",
    "resolves",
)
_CARD_EVIDENCE = (("vlm_semantic_pack", "vlm_event"),)
_DIGEST_EVIDENCE = (
    *_CARD_EVIDENCE,
    ("vlm_semantic_pack", "vlm_fact"),
    ("whole_series_source_manifest", "source_window"),
    ("event_card_set", "event"),
)
_GRAPH_EVIDENCE = (
    *_DIGEST_EVIDENCE,
    ("vlm_semantic_pack", "vlm_entity"),
    ("whole_series_source_manifest", "source"),
    ("event_card_set", "source_range"),
    ("episode_digest_set", "episode_digest"),
)


class NarrativeModelError(ValueError):
    """A closed narrative value has malformed fields or broken local references."""


def _text(value: object) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise NarrativeModelError("narrative text must be a non-empty UTF-8 string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise NarrativeModelError("narrative text must be valid UTF-8") from error
    return value


def _enum(value: object, choices: tuple[str, ...]) -> str:
    result = _text(value)
    if result not in choices:
        raise NarrativeModelError("narrative enum value is unsupported")
    return result


def _integer(value: object, *, minimum: int = -_SAFE) -> int:
    if type(value) is not int or not minimum <= value <= _SAFE:  # noqa: E721
        raise NarrativeModelError("narrative integer is outside its exact bound")
    return value


def _decimal(value: object) -> str:
    result = _text(value)
    if _DECIMAL.fullmatch(result) is None or result == "-0":
        raise NarrativeModelError(
            "decimal must be canonical exact text without exponent or trailing zeros"
        )
    return result


def _tuple(value: object, item_type: type[_T], *, minimum: int = 0) -> tuple[_T, ...]:
    if type(value) is not tuple:  # noqa: E721
        raise NarrativeModelError("narrative collections must be actual tuples")
    items = cast(tuple[object, ...], value)
    if len(items) < minimum or any(type(item) is not item_type for item in items):
        raise NarrativeModelError("narrative collection has missing or mistyped members")
    return cast(tuple[_T, ...], items)


def _ids(value: object, *, minimum: int = 1) -> tuple[str, ...]:
    items = _tuple(value, str, minimum=minimum)
    for item in items:
        _text(item)
    if len(set(items)) != len(items):
        raise NarrativeModelError("narrative local references must be unique")
    return tuple(sorted(items))


def _refs(value: object, *, minimum: int = 1) -> tuple[SemanticObjectRef, ...]:
    items = _tuple(value, SemanticObjectRef, minimum=minimum)
    if len(set(items)) != len(items):
        raise NarrativeModelError("narrative evidence references must be unique")
    return tuple(sorted(items, key=lambda item: canonical_json_bytes(item.to_mapping())))


def _evidence(
    value: object, allowed: tuple[tuple[str, str], ...], *, minimum: int = 1
) -> tuple[SemanticObjectRef, ...]:
    refs = _refs(value, minimum=minimum)
    for ref in refs:
        if (ref.member_ref.artifact_type, ref.object_type) not in allowed:
            raise NarrativeModelError("evidence has an unsupported or later member/object owner")
        if (
            ref.member_ref.artifact_type == "vlm_semantic_pack"
            or ref.object_type == "source_window"
        ):
            if re.fullmatch(r"sha256:[0-9a-f]{64}", ref.object_id) is None:
                raise NarrativeModelError(
                    "observation/window evidence requires its exact global hash ID"
                )
    return refs


def _owner(ref: object, artifact_type: str, object_type: str) -> SemanticObjectRef:
    if type(ref) is not SemanticObjectRef:  # noqa: E721
        raise NarrativeModelError("narrative reference must be an exact SemanticObjectRef")
    if ref.member_ref.artifact_type != artifact_type or ref.object_type != object_type:
        raise NarrativeModelError("narrative reference has the wrong member/object owner")
    return ref


def _window(ref: object) -> SemanticObjectRef:
    result = _owner(ref, "whole_series_source_manifest", "source_window")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", result.object_id) is None:
        raise NarrativeModelError("source window object ID must be its exact manifest hash")
    return result


def _closed(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise NarrativeModelError("narrative value must be a closed JSON object")
    result = cast(dict[str, object], value)
    if any(type(key) is not str for key in result) or set(result) != set(keys):  # noqa: E721
        raise NarrativeModelError("narrative value has missing or unknown fields")
    return result


def _array(value: object, parse: Callable[[object], _T]) -> tuple[_T, ...]:
    if type(value) is not list:  # noqa: E721
        raise NarrativeModelError("narrative wire collection must be a JSON array")
    return tuple(parse(item) for item in cast(list[object], value))


def _ref(value: object) -> SemanticObjectRef:
    try:
        return SemanticObjectRef.from_mapping(value)
    except SemanticReferenceError as error:
        raise NarrativeModelError("narrative reference is malformed") from error


@dataclass(frozen=True, slots=True)
class Confidence:
    value: str
    method: str

    def __post_init__(self) -> None:
        if not Decimal(0) <= Decimal(_decimal(self.value)) <= Decimal(1):
            raise NarrativeModelError("confidence must be between zero and one")
        _enum(self.method, ("model", "rule", "source"))

    def to_mapping(self) -> dict[str, object]:
        return {"value": self.value, "method": self.method}

    @classmethod
    def from_mapping(cls, value: object) -> Confidence:
        item = _closed(value, ("value", "method"))
        return cls(_text(item["value"]), _text(item["method"]))


def _time_base(value: object) -> TimeBase:
    item = _closed(value, ("numerator", "denominator"))
    try:
        return TimeBase(
            _integer(item["numerator"], minimum=1), _integer(item["denominator"], minimum=1)
        )
    except ValueError as error:
        raise NarrativeModelError("coarse interval time base is invalid") from error


def _mapped_interval(value: object) -> MappedSourceInterval:
    # Decode the existing VLM representation without remapping or rounding it.
    item = _closed(
        value, ("coarse_range", "mapping_error_bound", "provider_uncertainty", "semantic_precision")
    )
    if item["semantic_precision"] != "coarse_only":
        raise NarrativeModelError("Source evidence must remain coarse_only")
    coarse = _closed(item["coarse_range"], ("start_pts", "end_pts", "time_base"))
    error = _closed(item["mapping_error_bound"], ("clock", "tick", "time_base"))
    uncertainty = _closed(item["provider_uncertainty"], ("clock", "tick", "time_base"))
    base = _time_base(coarse["time_base"])
    if (
        error["clock"] != "source"
        or uncertainty["clock"] != "proxy"
        or _time_base(error["time_base"]) != base
    ):
        raise NarrativeModelError("coarse interval clock/time-base binding is invalid")
    try:
        interval = TickRange(_integer(coarse["start_pts"]), _integer(coarse["end_pts"]))
    except ValueError as exc:
        raise NarrativeModelError("coarse interval is empty or reversed") from exc
    return MappedSourceInterval(
        interval,
        _integer(error["tick"], minimum=0),
        base,
        _integer(uncertainty["tick"], minimum=0),
        _time_base(uncertainty["time_base"]),
    )


@dataclass(frozen=True, slots=True)
class CoarseSourceRange:
    source_ref: SemanticObjectRef
    clock_id: str
    mapped_interval: MappedSourceInterval

    def __post_init__(self) -> None:
        _owner(self.source_ref, "whole_series_source_manifest", "source")
        _text(self.clock_id)
        if type(self.mapped_interval) is not MappedSourceInterval:  # noqa: E721
            raise NarrativeModelError("Source range requires an exact MappedSourceInterval")
        # Shared typed primitives own interval arithmetic. Revalidate only the
        # JSON-safe representation bounds (native Source ticks may be negative).
        _integer(self.mapped_interval.coarse_range.start_pts)
        _integer(self.mapped_interval.coarse_range.end_pts)
        _integer(self.mapped_interval.mapping_error_bound_source_pts, minimum=0)
        _integer(self.mapped_interval.provider_uncertainty_proxy_pts, minimum=0)
        for base in (self.mapped_interval.source_time_base, self.mapped_interval.proxy_time_base):
            _integer(base.numerator, minimum=1)
            _integer(base.denominator, minimum=1)

    def to_mapping(self) -> dict[str, object]:
        mapped = self.mapped_interval.to_mapping()
        # The VLM encoder shares this temporary dictionary with coarse_range;
        # keep the two wire fields independently mutable without touching VLM.
        error = cast(dict[str, object], mapped["mapping_error_bound"])
        error["time_base"] = dict(cast(dict[str, object], error["time_base"]))
        return {
            "source_ref": self.source_ref.to_mapping(),
            "clock_id": self.clock_id,
            "mapped_interval": mapped,
        }

    @classmethod
    def from_mapping(cls, value: object) -> CoarseSourceRange:
        item = _closed(value, ("source_ref", "clock_id", "mapped_interval"))
        return cls(
            _ref(item["source_ref"]),
            _text(item["clock_id"]),
            _mapped_interval(item["mapped_interval"]),
        )


@dataclass(frozen=True, slots=True)
class EpisodeDigest:
    episode_id: str
    ordinal: int
    summary: str
    source_window_refs: tuple[SemanticObjectRef, ...]
    evidence_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        _text(self.episode_id)
        _integer(self.ordinal, minimum=1)
        _text(self.summary)
        object.__setattr__(self, "source_window_refs", _refs(self.source_window_refs))
        object.__setattr__(self, "evidence_refs", _evidence(self.evidence_refs, _DIGEST_EVIDENCE))
        for ref in self.source_window_refs:
            _window(ref)

    def to_mapping(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "ordinal": self.ordinal,
            "summary": self.summary,
            "source_window_refs": [ref.to_mapping() for ref in self.source_window_refs],
            "evidence_refs": [ref.to_mapping() for ref in self.evidence_refs],
        }

    @classmethod
    def from_mapping(cls, value: object) -> EpisodeDigest:
        item = _closed(
            value, ("episode_id", "ordinal", "summary", "source_window_refs", "evidence_refs")
        )
        return cls(
            _text(item["episode_id"]),
            _integer(item["ordinal"], minimum=1),
            _text(item["summary"]),
            _array(item["source_window_refs"], _ref),
            _array(item["evidence_refs"], _ref),
        )


@dataclass(frozen=True, slots=True)
class EpisodeDigestSet:
    episode_digest_set_id: str
    digests: tuple[EpisodeDigest, ...]

    def __post_init__(self) -> None:
        _text(self.episode_digest_set_id)
        items = _tuple(self.digests, EpisodeDigest)
        if len({item.episode_id for item in items}) != len(items) or len(
            {item.ordinal for item in items}
        ) != len(items):
            raise NarrativeModelError("episode IDs and ordinals must each be unique")
        object.__setattr__(self, "digests", tuple(sorted(items, key=lambda item: item.ordinal)))

    def to_mapping(self) -> dict[str, object]:
        return {
            "episode_digest_set_id": self.episode_digest_set_id,
            "digests": [item.to_mapping() for item in self.digests],
        }

    @classmethod
    def from_mapping(cls, value: object) -> EpisodeDigestSet:
        item = _closed(value, ("episode_digest_set_id", "digests"))
        return cls(
            _text(item["episode_digest_set_id"]),
            _array(item["digests"], EpisodeDigest.from_mapping),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class EventCard:
    event_id: str
    episode_id: str
    content: str
    source_range_refs: tuple[CoarseSourceRange, ...]
    evidence_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        for value in (self.event_id, self.episode_id, self.content):
            _text(value)
        ranges = _tuple(self.source_range_refs, CoarseSourceRange, minimum=1)
        if len(set(ranges)) != len(ranges):
            raise NarrativeModelError("EventCard source ranges must be unique")
        # Array position owns event_id:range:index; do not silently reorder it.
        object.__setattr__(self, "evidence_refs", _evidence(self.evidence_refs, _CARD_EVIDENCE))

    def to_mapping(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "episode_id": self.episode_id,
            "content": self.content,
            "source_range_refs": [item.to_mapping() for item in self.source_range_refs],
            "evidence_refs": [ref.to_mapping() for ref in self.evidence_refs],
        }

    @classmethod
    def from_mapping(cls, value: object) -> EventCard:
        item = _closed(
            value, ("event_id", "episode_id", "content", "source_range_refs", "evidence_refs")
        )
        return cls(
            _text(item["event_id"]),
            _text(item["episode_id"]),
            _text(item["content"]),
            _array(item["source_range_refs"], CoarseSourceRange.from_mapping),
            _array(item["evidence_refs"], _ref),
        )


@dataclass(frozen=True, slots=True)
class EventCardSet:
    event_card_set_id: str
    events: tuple[EventCard, ...]

    def __post_init__(self) -> None:
        _text(self.event_card_set_id)
        events = _tuple(self.events, EventCard)
        if len({item.event_id for item in events}) != len(events):
            raise NarrativeModelError("EventCard IDs must be unique")
        object.__setattr__(self, "events", tuple(sorted(events, key=lambda item: item.event_id)))

    def to_mapping(self) -> dict[str, object]:
        return {
            "event_card_set_id": self.event_card_set_id,
            "events": [item.to_mapping() for item in self.events],
        }

    @classmethod
    def from_mapping(cls, value: object) -> EventCardSet:
        item = _closed(value, ("event_card_set_id", "events"))
        return cls(_text(item["event_card_set_id"]), _array(item["events"], EventCard.from_mapping))

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class FactTextValue:
    text: str

    def __post_init__(self) -> None:
        _text(self.text)

    def to_mapping(self) -> dict[str, object]:
        return {"kind": "text", "text": self.text}


@dataclass(frozen=True, slots=True)
class FactNumberValue:
    number: str

    def __post_init__(self) -> None:
        _decimal(self.number)

    def to_mapping(self) -> dict[str, object]:
        return {"kind": "number", "number": self.number}


@dataclass(frozen=True, slots=True)
class FactBooleanValue:
    boolean: bool

    def __post_init__(self) -> None:
        if type(self.boolean) is not bool:  # noqa: E721
            raise NarrativeModelError("Fact boolean value must be an actual boolean")

    def to_mapping(self) -> dict[str, object]:
        return {"kind": "boolean", "boolean": self.boolean}


@dataclass(frozen=True, slots=True)
class FactEntityRefValue:
    node_id: str

    def __post_init__(self) -> None:
        _text(self.node_id)

    def to_mapping(self) -> dict[str, object]:
        return {"kind": "entity_ref", "node_id": self.node_id}


FactValue = FactTextValue | FactNumberValue | FactBooleanValue | FactEntityRefValue


def _fact_value(value: object) -> FactValue:
    if type(value) is not dict:  # noqa: E721
        raise NarrativeModelError("Fact value must be a closed discriminated object")
    item = cast(dict[str, object], value)
    kind = _enum(item.get("kind"), ("text", "number", "boolean", "entity_ref"))
    field = "node_id" if kind == "entity_ref" else kind
    _closed(item, ("kind", field))
    if kind == "text":
        return FactTextValue(_text(item[field]))
    if kind == "number":
        return FactNumberValue(_text(item[field]))
    if kind == "boolean":
        return FactBooleanValue(cast(bool, item[field]))
    return FactEntityRefValue(_text(item[field]))


@dataclass(frozen=True, slots=True)
class EntityAttributes:
    entity_kind: str
    display_label: str
    visual_description: str

    def __post_init__(self) -> None:
        _enum(self.entity_kind, _ENTITY_KINDS)
        _text(self.display_label)
        _text(self.visual_description)

    def to_mapping(self) -> dict[str, object]:
        return {
            "attribute_type": "entity",
            "entity_kind": self.entity_kind,
            "display_label": self.display_label,
            "visual_description": self.visual_description,
        }


@dataclass(frozen=True, slots=True)
class FactAttributes:
    subject_node_id: str
    predicate: str
    value: FactValue
    conflict_status: str

    def __post_init__(self) -> None:
        _text(self.subject_node_id)
        _text(self.predicate)
        if type(self.value) not in (
            FactTextValue,
            FactNumberValue,
            FactBooleanValue,
            FactEntityRefValue,
        ):
            raise NarrativeModelError("Fact value must be an exact typed variant")
        _enum(self.conflict_status, ("none", "conflicted"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "attribute_type": "fact",
            "subject_node_id": self.subject_node_id,
            "predicate": self.predicate,
            "value": self.value.to_mapping(),
            "conflict_status": self.conflict_status,
        }


@dataclass(frozen=True, slots=True)
class EventAttributes:
    event_card_ref: SemanticObjectRef
    episode_id: str
    summary: str
    source_range_refs: tuple[SemanticObjectRef, ...]
    participant_node_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _owner(self.event_card_ref, "event_card_set", "event")
        _text(self.episode_id)
        _text(self.summary)
        # Range order is inherited from the referenced EventCard.
        ranges = _tuple(self.source_range_refs, SemanticObjectRef, minimum=1)
        for index, ref in enumerate(ranges):
            _owner(ref, "event_card_set", "source_range")
            if (
                ref.member_ref != self.event_card_ref.member_ref
                or ref.object_id != f"{self.event_card_ref.object_id}:range:{index}"
            ):
                raise NarrativeModelError(
                    "Event source range does not belong to its exact EventCard"
                )
        object.__setattr__(self, "participant_node_ids", _ids(self.participant_node_ids, minimum=0))

    def to_mapping(self) -> dict[str, object]:
        return {
            "attribute_type": "event",
            "event_card_ref": self.event_card_ref.to_mapping(),
            "episode_id": self.episode_id,
            "summary": self.summary,
            "source_range_refs": [ref.to_mapping() for ref in self.source_range_refs],
            "participant_node_ids": list(self.participant_node_ids),
        }


@dataclass(frozen=True, slots=True)
class BeatAttributes:
    summary: str
    phase: str
    obligation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.summary)
        _enum(self.phase, _PHASES)
        object.__setattr__(self, "obligation_ids", _ids(self.obligation_ids))

    def to_mapping(self) -> dict[str, object]:
        return {
            "attribute_type": "beat",
            "summary": self.summary,
            "phase": self.phase,
            "obligation_ids": list(self.obligation_ids),
        }


@dataclass(frozen=True, slots=True)
class ObligationAttributes:
    description: str
    required_fact_ids: tuple[str, ...]
    success_criteria: str

    def __post_init__(self) -> None:
        _text(self.description)
        _text(self.success_criteria)
        object.__setattr__(self, "required_fact_ids", _ids(self.required_fact_ids))

    def to_mapping(self) -> dict[str, object]:
        return {
            "attribute_type": "obligation",
            "description": self.description,
            "required_fact_ids": list(self.required_fact_ids),
            "success_criteria": self.success_criteria,
        }


@dataclass(frozen=True, slots=True)
class StoryThreadAttributes:
    title: str
    premise: str
    obligation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.title)
        _text(self.premise)
        object.__setattr__(self, "obligation_ids", _ids(self.obligation_ids))

    def to_mapping(self) -> dict[str, object]:
        return {
            "attribute_type": "story_thread",
            "title": self.title,
            "premise": self.premise,
            "obligation_ids": list(self.obligation_ids),
        }


@dataclass(frozen=True, slots=True)
class CharacterAttributes:
    canonical_name: str
    aliases: tuple[str, ...]
    state_fact_ids: tuple[str, ...]
    entity_node_ids: tuple[str, ...]
    identity_evidence_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        _text(self.canonical_name)
        object.__setattr__(self, "aliases", _ids(self.aliases, minimum=0))
        object.__setattr__(self, "state_fact_ids", _ids(self.state_fact_ids, minimum=0))
        object.__setattr__(self, "entity_node_ids", _ids(self.entity_node_ids))
        object.__setattr__(
            self, "identity_evidence_refs", _evidence(self.identity_evidence_refs, _GRAPH_EVIDENCE)
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "attribute_type": "character",
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "state_fact_ids": list(self.state_fact_ids),
            "entity_node_ids": list(self.entity_node_ids),
            "identity_evidence_refs": [ref.to_mapping() for ref in self.identity_evidence_refs],
        }


@dataclass(frozen=True, slots=True)
class CharacterStateAttributes:
    character_node_id: str
    source_window_ref: SemanticObjectRef
    state_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.character_node_id)
        _window(self.source_window_ref)
        object.__setattr__(self, "state_fact_ids", _ids(self.state_fact_ids))

    def to_mapping(self) -> dict[str, object]:
        return {
            "attribute_type": "character_state",
            "character_node_id": self.character_node_id,
            "source_window_ref": self.source_window_ref.to_mapping(),
            "state_fact_ids": list(self.state_fact_ids),
        }


@dataclass(frozen=True, slots=True)
class RelationshipAttributes:
    subject_node_id: str
    object_node_id: str
    relation_type: str

    def __post_init__(self) -> None:
        _text(self.subject_node_id)
        _text(self.object_node_id)
        _enum(
            self.relation_type,
            ("family", "ally", "opponent", "romantic", "authority", "dependency", "unknown"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "attribute_type": "relationship",
            "subject_node_id": self.subject_node_id,
            "object_node_id": self.object_node_id,
            "relation_type": self.relation_type,
        }


@dataclass(frozen=True, slots=True)
class QuestionAttributes:
    text: str
    status: str
    answer_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.text)
        _enum(self.status, ("open", "answered", "invalidated"))
        object.__setattr__(self, "answer_fact_ids", _ids(self.answer_fact_ids, minimum=0))

    def to_mapping(self) -> dict[str, object]:
        return {
            "attribute_type": "question",
            "text": self.text,
            "status": self.status,
            "answer_fact_ids": list(self.answer_fact_ids),
        }


@dataclass(frozen=True, slots=True)
class ForeshadowAttributes:
    setup_event_ids: tuple[str, ...]
    payoff_event_ids: tuple[str, ...]
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "setup_event_ids", _ids(self.setup_event_ids))
        object.__setattr__(self, "payoff_event_ids", _ids(self.payoff_event_ids, minimum=0))
        _enum(self.status, ("setup_only", "paid_off", "broken"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "attribute_type": "foreshadow",
            "setup_event_ids": list(self.setup_event_ids),
            "payoff_event_ids": list(self.payoff_event_ids),
            "status": self.status,
        }


GraphAttributes = (
    EntityAttributes
    | FactAttributes
    | EventAttributes
    | BeatAttributes
    | ObligationAttributes
    | StoryThreadAttributes
    | CharacterAttributes
    | CharacterStateAttributes
    | RelationshipAttributes
    | QuestionAttributes
    | ForeshadowAttributes
)
_ATTR_TYPES = {
    "entity": EntityAttributes,
    "fact": FactAttributes,
    "event": EventAttributes,
    "beat": BeatAttributes,
    "obligation": ObligationAttributes,
    "story_thread": StoryThreadAttributes,
    "character": CharacterAttributes,
    "character_state": CharacterStateAttributes,
    "relationship": RelationshipAttributes,
    "question": QuestionAttributes,
    "foreshadow": ForeshadowAttributes,
}


def _attributes(value: object) -> GraphAttributes:
    if type(value) is not dict:  # noqa: E721
        raise NarrativeModelError("Graph attributes must be a closed discriminated object")
    raw = cast(dict[str, object], value)
    kind = _enum(raw.get("attribute_type"), tuple(_ATTR_TYPES))
    fields = {
        "entity": ("entity_kind", "display_label", "visual_description"),
        "fact": ("subject_node_id", "predicate", "value", "conflict_status"),
        "event": (
            "event_card_ref",
            "episode_id",
            "summary",
            "source_range_refs",
            "participant_node_ids",
        ),
        "beat": ("summary", "phase", "obligation_ids"),
        "obligation": ("description", "required_fact_ids", "success_criteria"),
        "story_thread": ("title", "premise", "obligation_ids"),
        "character": (
            "canonical_name",
            "aliases",
            "state_fact_ids",
            "entity_node_ids",
            "identity_evidence_refs",
        ),
        "character_state": ("character_node_id", "source_window_ref", "state_fact_ids"),
        "relationship": ("subject_node_id", "object_node_id", "relation_type"),
        "question": ("text", "status", "answer_fact_ids"),
        "foreshadow": ("setup_event_ids", "payoff_event_ids", "status"),
    }
    item = _closed(raw, ("attribute_type", *fields[kind]))
    if kind == "entity":
        return EntityAttributes(
            _text(item["entity_kind"]),
            _text(item["display_label"]),
            _text(item["visual_description"]),
        )
    if kind == "fact":
        return FactAttributes(
            _text(item["subject_node_id"]),
            _text(item["predicate"]),
            _fact_value(item["value"]),
            _text(item["conflict_status"]),
        )
    if kind == "event":
        return EventAttributes(
            _ref(item["event_card_ref"]),
            _text(item["episode_id"]),
            _text(item["summary"]),
            _array(item["source_range_refs"], _ref),
            _array(item["participant_node_ids"], _text),
        )
    if kind == "beat":
        return BeatAttributes(
            _text(item["summary"]), _text(item["phase"]), _array(item["obligation_ids"], _text)
        )
    if kind == "obligation":
        return ObligationAttributes(
            _text(item["description"]),
            _array(item["required_fact_ids"], _text),
            _text(item["success_criteria"]),
        )
    if kind == "story_thread":
        return StoryThreadAttributes(
            _text(item["title"]), _text(item["premise"]), _array(item["obligation_ids"], _text)
        )
    if kind == "character":
        return CharacterAttributes(
            _text(item["canonical_name"]),
            _array(item["aliases"], _text),
            _array(item["state_fact_ids"], _text),
            _array(item["entity_node_ids"], _text),
            _array(item["identity_evidence_refs"], _ref),
        )
    if kind == "character_state":
        return CharacterStateAttributes(
            _text(item["character_node_id"]),
            _ref(item["source_window_ref"]),
            _array(item["state_fact_ids"], _text),
        )
    if kind == "relationship":
        return RelationshipAttributes(
            _text(item["subject_node_id"]),
            _text(item["object_node_id"]),
            _text(item["relation_type"]),
        )
    if kind == "question":
        return QuestionAttributes(
            _text(item["text"]), _text(item["status"]), _array(item["answer_fact_ids"], _text)
        )
    return ForeshadowAttributes(
        _array(item["setup_event_ids"], _text),
        _array(item["payoff_event_ids"], _text),
        _text(item["status"]),
    )


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    node_type: str
    label: str
    attributes: GraphAttributes
    evidence_refs: tuple[SemanticObjectRef, ...]
    confidence: Confidence

    def __post_init__(self) -> None:
        _text(self.node_id)
        _text(self.label)
        kind = _enum(self.node_type, tuple(_ATTR_TYPES))
        if type(self.attributes) is not _ATTR_TYPES[kind]:
            raise NarrativeModelError("Graph node_type must match its exact attribute variant")
        if type(self.confidence) is not Confidence:  # noqa: E721
            raise NarrativeModelError("Graph confidence must be an exact Confidence")
        object.__setattr__(
            self,
            "evidence_refs",
            _evidence(
                self.evidence_refs, _GRAPH_EVIDENCE, minimum=0 if kind == "story_thread" else 1
            ),
        )
        if (
            isinstance(self.attributes, EventAttributes)
            and self.attributes.event_card_ref.object_id != self.node_id
        ):
            raise NarrativeModelError("Graph Event ID must equal its EventCard canonical Event ID")

    def to_mapping(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "label": self.label,
            "attributes": self.attributes.to_mapping(),
            "evidence_refs": [ref.to_mapping() for ref in self.evidence_refs],
            "confidence": self.confidence.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> GraphNode:
        item = _closed(
            value, ("node_id", "node_type", "label", "attributes", "evidence_refs", "confidence")
        )
        return cls(
            _text(item["node_id"]),
            _text(item["node_type"]),
            _text(item["label"]),
            _attributes(item["attributes"]),
            _array(item["evidence_refs"], _ref),
            Confidence.from_mapping(item["confidence"]),
        )


@dataclass(frozen=True, slots=True)
class GraphEdge:
    edge_id: str
    edge_type: str
    from_node_id: str
    to_node_id: str
    evidence_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        for value in (self.edge_id, self.from_node_id, self.to_node_id):
            _text(value)
        _enum(self.edge_type, _EDGE_KINDS)
        object.__setattr__(
            self, "evidence_refs", _evidence(self.evidence_refs, _GRAPH_EVIDENCE, minimum=0)
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "edge_id": self.edge_id,
            "edge_type": self.edge_type,
            "from_node_id": self.from_node_id,
            "to_node_id": self.to_node_id,
            "evidence_refs": [ref.to_mapping() for ref in self.evidence_refs],
        }

    @classmethod
    def from_mapping(cls, value: object) -> GraphEdge:
        item = _closed(
            value, ("edge_id", "edge_type", "from_node_id", "to_node_id", "evidence_refs")
        )
        return cls(
            _text(item["edge_id"]),
            _text(item["edge_type"]),
            _text(item["from_node_id"]),
            _text(item["to_node_id"]),
            _array(item["evidence_refs"], _ref),
        )


@dataclass(frozen=True, slots=True)
class NarrativeGraph:
    graph_id: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]

    def __post_init__(self) -> None:
        _text(self.graph_id)
        nodes, edges = _tuple(self.nodes, GraphNode), _tuple(self.edges, GraphEdge)
        ids = tuple(node.node_id for node in nodes) + tuple(edge.edge_id for edge in edges)
        if len(set(ids)) != len(ids):
            raise NarrativeModelError("Graph Node/Edge IDs must be unique")
        index = {node.node_id: node for node in nodes}

        def target(node_id: str, kinds: tuple[str, ...]) -> GraphNode:
            if node_id not in index or index[node_id].node_type not in kinds:
                raise NarrativeModelError(
                    "Graph local reference has a missing or wrong-kind target"
                )
            return index[node_id]

        event_owners: set[SemanticMemberIdentity] = set()
        for node in nodes:
            attrs = node.attributes
            if isinstance(attrs, FactAttributes):
                target(attrs.subject_node_id, ("entity", "character"))
                if isinstance(attrs.value, FactEntityRefValue):
                    target(attrs.value.node_id, ("entity", "character"))
            elif isinstance(attrs, EventAttributes):
                event_owners.add(attrs.event_card_ref.member_ref)
                for node_id in attrs.participant_node_ids:
                    target(node_id, ("entity", "character"))
            elif isinstance(attrs, (BeatAttributes, StoryThreadAttributes)):
                for node_id in attrs.obligation_ids:
                    target(node_id, ("obligation",))
            elif isinstance(attrs, ObligationAttributes):
                for node_id in attrs.required_fact_ids:
                    target(node_id, ("fact",))
            elif isinstance(attrs, CharacterAttributes):
                for node_id in attrs.entity_node_ids:
                    entity = cast(EntityAttributes, target(node_id, ("entity",)).attributes)
                    if entity.entity_kind != "person":
                        raise NarrativeModelError(
                            "Character source entities must be observed persons"
                        )
                for node_id in attrs.state_fact_ids:
                    fact = cast(FactAttributes, target(node_id, ("fact",)).attributes)
                    if fact.subject_node_id not in attrs.entity_node_ids:
                        raise NarrativeModelError("Character state Fact belongs to another entity")
            elif isinstance(attrs, CharacterStateAttributes):
                character = cast(
                    CharacterAttributes, target(attrs.character_node_id, ("character",)).attributes
                )
                for node_id in attrs.state_fact_ids:
                    target(node_id, ("fact",))
                    if node_id not in character.state_fact_ids:
                        raise NarrativeModelError(
                            "CharacterState Fact is not owned by its Character"
                        )
            elif isinstance(attrs, RelationshipAttributes):
                target(attrs.subject_node_id, ("character",))
                target(attrs.object_node_id, ("character",))
            elif isinstance(attrs, QuestionAttributes):
                for node_id in attrs.answer_fact_ids:
                    target(node_id, ("fact",))
            elif isinstance(attrs, ForeshadowAttributes):
                for node_id in (*attrs.setup_event_ids, *attrs.payoff_event_ids):
                    target(node_id, ("event",))
        if len(event_owners) > 1:
            raise NarrativeModelError("Graph Events must refer to one exact EventCardSet owner")
        for edge in edges:
            if edge.from_node_id not in index or edge.to_node_id not in index:
                raise NarrativeModelError("Graph edge endpoint is missing")
        object.__setattr__(self, "nodes", tuple(sorted(nodes, key=lambda node: node.node_id)))
        object.__setattr__(self, "edges", tuple(sorted(edges, key=lambda edge: edge.edge_id)))

    def to_mapping(self) -> dict[str, object]:
        return {
            "graph_id": self.graph_id,
            "nodes": [node.to_mapping() for node in self.nodes],
            "edges": [edge.to_mapping() for edge in self.edges],
        }

    @classmethod
    def from_mapping(cls, value: object) -> NarrativeGraph:
        item = _closed(value, ("graph_id", "nodes", "edges"))
        return cls(
            _text(item["graph_id"]),
            _array(item["nodes"], GraphNode.from_mapping),
            _array(item["edges"], GraphEdge.from_mapping),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())
