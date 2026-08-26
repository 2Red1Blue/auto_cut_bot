"""Untrusted Stage 2 proposal and frozen policy values, never admission.

These models check structure and local reference consistency, not committed
object existence, render authorization, semantic support or feasibility. A
compiler must independently establish those facts from audited inputs. Empty
source allowlists mean no additional restriction, not a grant of permission.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, TypeVar, cast

from ..contracts.compiler.canonical import canonical_json_hash
from .member_refs import SemanticObjectRef

_T = TypeVar("_T")
_SAFE_INTEGER = 2**53 - 1
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
PHYSICAL_REQUIREMENT_MODES: tuple[tuple[str, str], ...] = (
    ("dialogue_integrity", "complete"),
    ("subtitle_clearance", "protect_detected_cues"),
    ("visual_validity", "endpoint_and_stable_region"),
)


class StoryDesignModelError(ValueError):
    """A proposal or policy value violates its closed structural contract."""


def _text(value: object) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise StoryDesignModelError("text must be nonempty UTF-8")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise StoryDesignModelError("text must be nonempty UTF-8") from error
    return value


def _positive(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _SAFE_INTEGER:  # noqa: E721
        raise StoryDesignModelError("value must be a positive safe integer")
    return value


def _hash(value: object) -> str:
    text = _text(value)
    if _SHA256.fullmatch(text) is None or text == "sha256:" + "0" * 64:
        raise StoryDesignModelError("value must be a nonzero lowercase sha256")
    return text


def _closed(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise StoryDesignModelError("value must be a closed object")
    mapping = cast(dict[str, object], value)
    if any(type(key) is not str for key in mapping) or set(mapping) != set(keys):  # noqa: E721
        raise StoryDesignModelError("object has missing or unknown fields")
    return mapping


def _array(value: object, decoder: Callable[[object], _T]) -> tuple[_T, ...]:
    if type(value) is not list:  # noqa: E721
        raise StoryDesignModelError("wire collection must be an array")
    return tuple(decoder(item) for item in cast(list[object], value))


def _tuple(value: object, item_type: type[_T], *, nonempty: bool = False) -> tuple[_T, ...]:
    if type(value) is not tuple:  # noqa: E721
        raise StoryDesignModelError("collection must be an immutable tuple")
    items = cast(tuple[object, ...], value)
    if (nonempty and not items) or any(type(item) is not item_type for item in items):
        raise StoryDesignModelError("collection has invalid items or is empty")
    if len(set(items)) != len(items):
        raise StoryDesignModelError("collection contains duplicate items")
    return cast(tuple[_T, ...], items)


def _texts(value: object) -> tuple[str, ...]:
    items = _tuple(value, str, nonempty=True)
    for item in items:
        _text(item)
    return items


def _refs(value: object, *, artifact_type: str, object_type: str) -> tuple[SemanticObjectRef, ...]:
    refs = _tuple(value, SemanticObjectRef)
    for ref in refs:
        if ref.member_ref.artifact_type != artifact_type or ref.object_type != object_type:
            raise StoryDesignModelError("reference has the wrong owner or object type")
    return refs


def _one_owner(refs: tuple[SemanticObjectRef, ...]) -> None:
    if len({ref.member_ref for ref in refs}) > 1:
        raise StoryDesignModelError("references must name one exact member owner")


@dataclass(frozen=True, slots=True)
class IntegerRange:
    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        if _positive(self.minimum) > _positive(self.maximum):
            raise StoryDesignModelError("range minimum exceeds maximum")

    def to_mapping(self) -> dict[str, object]:
        return {"min": self.minimum, "max": self.maximum}

    @classmethod
    def from_mapping(cls, value: object) -> IntegerRange:
        data = _closed(value, ("min", "max"))
        return cls(_positive(data["min"]), _positive(data["max"]))


@dataclass(frozen=True, slots=True)
class EditingProfileReference:
    profile_id: str
    profile_version: str

    def __post_init__(self) -> None:
        _text(self.profile_id)
        _text(self.profile_version)

    def to_mapping(self) -> dict[str, object]:
        return {"profile_id": self.profile_id, "profile_version": self.profile_version}

    @classmethod
    def from_mapping(cls, value: object) -> EditingProfileReference:
        data = _closed(value, ("profile_id", "profile_version"))
        return cls(_text(data["profile_id"]), _text(data["profile_version"]))


@dataclass(frozen=True, slots=True)
class PhysicalRequirement:
    requirement_kind: str
    mode: str

    def __post_init__(self) -> None:
        if (_text(self.requirement_kind), _text(self.mode)) not in PHYSICAL_REQUIREMENT_MODES:
            raise StoryDesignModelError("unsupported physical requirement kind/mode")

    def to_mapping(self) -> dict[str, object]:
        return {"requirement_kind": self.requirement_kind, "mode": self.mode}

    @classmethod
    def from_mapping(cls, value: object) -> PhysicalRequirement:
        data = _closed(value, ("requirement_kind", "mode"))
        return cls(_text(data["requirement_kind"]), _text(data["mode"]))


@dataclass(frozen=True, slots=True)
class SourceConstraints:
    allowed_source_refs: tuple[SemanticObjectRef, ...]
    forbidden_source_refs: tuple[SemanticObjectRef, ...]
    authorization_purpose: str

    def __post_init__(self) -> None:
        for refs in (self.allowed_source_refs, self.forbidden_source_refs):
            _refs(refs, artifact_type="whole_series_source_manifest", object_type="source")
        _one_owner(self.allowed_source_refs + self.forbidden_source_refs)
        if set(self.allowed_source_refs) & set(self.forbidden_source_refs):
            raise StoryDesignModelError("allowed and forbidden sources overlap")
        if _text(self.authorization_purpose) != "render_source":
            raise StoryDesignModelError("source constraint purpose must be render_source")

    def to_mapping(self) -> dict[str, object]:
        return {
            "allowed_source_refs": [ref.to_mapping() for ref in self.allowed_source_refs],
            "forbidden_source_refs": [ref.to_mapping() for ref in self.forbidden_source_refs],
            "authorization_purpose": self.authorization_purpose,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SourceConstraints:
        data = _closed(value, (
            "allowed_source_refs", "forbidden_source_refs", "authorization_purpose",
        ))
        return cls(
            _array(data["allowed_source_refs"], SemanticObjectRef.from_mapping),
            _array(data["forbidden_source_refs"], SemanticObjectRef.from_mapping),
            _text(data["authorization_purpose"]),
        )


@dataclass(frozen=True, slots=True)
class MaterialRequirement:
    requirement_id: str
    obligation_ref: SemanticObjectRef
    minimum_usable_seconds: int
    physical_requirements: tuple[PhysicalRequirement, ...]
    source_constraints: SourceConstraints

    def __post_init__(self) -> None:
        _text(self.requirement_id)
        _refs((self.obligation_ref,), artifact_type="narrative_graph", object_type="obligation")
        _positive(self.minimum_usable_seconds)
        physical = _tuple(self.physical_requirements, PhysicalRequirement)
        kinds = tuple(item.requirement_kind for item in physical)
        if kinds != tuple(sorted(set(kinds), key=lambda kind: kind.encode("utf-8"))):
            raise StoryDesignModelError("physical requirements must be unique and kind-sorted")
        if type(self.source_constraints) is not SourceConstraints:  # noqa: E721
            raise StoryDesignModelError("source constraints must be typed")
        for ref in (
            self.source_constraints.allowed_source_refs + self.source_constraints.forbidden_source_refs
        ):
            if ref.member_ref.scope != self.obligation_ref.member_ref.scope:
                raise StoryDesignModelError("source and narrative scopes differ")

    @property
    def physical_requirements_hash(self) -> str:
        return canonical_json_hash([item.to_mapping() for item in self.physical_requirements])

    def to_mapping(self) -> dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "obligation_ref": self.obligation_ref.to_mapping(),
            "minimum_usable_seconds": self.minimum_usable_seconds,
            "physical_requirements": [item.to_mapping() for item in self.physical_requirements],
            "source_constraints": self.source_constraints.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> MaterialRequirement:
        data = _closed(value, (
            "requirement_id", "obligation_ref", "minimum_usable_seconds",
            "physical_requirements", "source_constraints",
        ))
        return cls(
            _text(data["requirement_id"]), SemanticObjectRef.from_mapping(data["obligation_ref"]),
            _positive(data["minimum_usable_seconds"]),
            _array(data["physical_requirements"], PhysicalRequirement.from_mapping),
            SourceConstraints.from_mapping(data["source_constraints"]),
        )


@dataclass(frozen=True, slots=True)
class StoryDesignPolicy:
    policy_id: str
    policy_version: str
    allowed_genre_tags: tuple[str, ...]
    editing_profiles: tuple[EditingProfileReference, ...]
    teaser_strategies: tuple[str, ...]
    required_physical_requirements: tuple[PhysicalRequirement, ...]
    selection_strategy: str

    def __post_init__(self) -> None:
        _text(self.policy_id)
        _text(self.policy_version)
        _texts(self.allowed_genre_tags)
        _tuple(self.editing_profiles, EditingProfileReference, nonempty=True)
        _texts(self.teaser_strategies)
        required = _tuple(self.required_physical_requirements, PhysicalRequirement)
        kinds = tuple(item.requirement_kind for item in required)
        if kinds != tuple(sorted(set(kinds))):
            raise StoryDesignModelError("required physical requirements must be unique and kind-sorted")
        if _text(self.selection_strategy) != "first_feasible_lexicographic_v1":
            raise StoryDesignModelError("unsupported portfolio selection strategy")

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id, "policy_version": self.policy_version,
            "allowed_genre_tags": list(self.allowed_genre_tags),
            "editing_profiles": [item.to_mapping() for item in self.editing_profiles],
            "teaser_strategies": list(self.teaser_strategies),
            "required_physical_requirements": [
                item.to_mapping() for item in self.required_physical_requirements
            ],
            "selection_strategy": self.selection_strategy,
        }

    @classmethod
    def from_mapping(cls, value: object) -> StoryDesignPolicy:
        data = _closed(value, (
            "policy_id", "policy_version", "allowed_genre_tags", "editing_profiles",
            "teaser_strategies", "required_physical_requirements", "selection_strategy",
        ))
        return cls(
            _text(data["policy_id"]), _text(data["policy_version"]),
            _array(data["allowed_genre_tags"], _text),
            _array(data["editing_profiles"], EditingProfileReference.from_mapping),
            _array(data["teaser_strategies"], _text),
            _array(data["required_physical_requirements"], PhysicalRequirement.from_mapping),
            _text(data["selection_strategy"]),
        )


@dataclass(frozen=True, slots=True)
class JobPolicy:
    policy_id: str
    policy_version: str
    story_design_policy_sha256: str
    proposal_count: IntegerRange
    selected_story_count: int
    max_search_states: int
    target_duration_seconds: IntegerRange
    source_reuse_policy: str
    source_constraints: SourceConstraints
    completion_policy: str

    def __post_init__(self) -> None:
        _text(self.policy_id)
        _text(self.policy_version)
        _hash(self.story_design_policy_sha256)
        if any(type(item) is not IntegerRange for item in (
            self.proposal_count, self.target_duration_seconds,
        )):
            raise StoryDesignModelError("policy ranges must be typed")
        if _positive(self.selected_story_count) > self.proposal_count.maximum:
            raise StoryDesignModelError("selected count exceeds maximum proposal count")
        _positive(self.max_search_states)
        if _text(self.source_reuse_policy) not in ("allow", "forbid"):
            raise StoryDesignModelError("unsupported source reuse policy")
        if type(self.source_constraints) is not SourceConstraints:  # noqa: E721
            raise StoryDesignModelError("source constraints must be typed")
        if _text(self.completion_policy) != "all_or_nothing":
            raise StoryDesignModelError("unsupported completion policy")

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id, "policy_version": self.policy_version,
            "story_design_policy_sha256": self.story_design_policy_sha256,
            "proposal_count": self.proposal_count.to_mapping(),
            "selected_story_count": self.selected_story_count,
            "max_search_states": self.max_search_states,
            "target_duration_seconds": self.target_duration_seconds.to_mapping(),
            "source_reuse_policy": self.source_reuse_policy,
            "source_constraints": self.source_constraints.to_mapping(),
            "completion_policy": self.completion_policy,
        }

    @classmethod
    def from_mapping(cls, value: object) -> JobPolicy:
        data = _closed(value, (
            "policy_id", "policy_version", "story_design_policy_sha256", "proposal_count",
            "selected_story_count", "max_search_states", "target_duration_seconds", "source_reuse_policy",
            "source_constraints", "completion_policy",
        ))
        return cls(
            _text(data["policy_id"]), _text(data["policy_version"]),
            _hash(data["story_design_policy_sha256"]), IntegerRange.from_mapping(data["proposal_count"]),
            _positive(data["selected_story_count"]),
            _positive(data["max_search_states"]),
            IntegerRange.from_mapping(data["target_duration_seconds"]),
            _text(data["source_reuse_policy"]), SourceConstraints.from_mapping(data["source_constraints"]),
            _text(data["completion_policy"]),
        )


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    proposal_id: str
    title: str
    narrative_claim: str
    thread_refs: tuple[SemanticObjectRef, ...]
    required_obligation_refs: tuple[SemanticObjectRef, ...]
    required_fact_refs: tuple[SemanticObjectRef, ...]
    key_character_refs: tuple[SemanticObjectRef, ...]
    genre_tags: tuple[str, ...]
    editing_profile: EditingProfileReference
    target_duration_seconds: IntegerRange
    teaser_strategy: str
    audience_hook: str
    material_requirements: tuple[MaterialRequirement, ...]

    def __post_init__(self) -> None:
        for value in (self.proposal_id, self.title, self.narrative_claim,
                      self.teaser_strategy, self.audience_hook):
            _text(value)
        for refs, kind in (
            (self.thread_refs, "story_thread"), (self.required_obligation_refs, "obligation"),
            (self.required_fact_refs, "fact"), (self.key_character_refs, "character"),
        ):
            _refs(refs, artifact_type="narrative_graph", object_type=kind)
        _one_owner(self.narrative_refs)
        _texts(self.genre_tags)
        if type(self.editing_profile) is not EditingProfileReference:  # noqa: E721
            raise StoryDesignModelError("editing profile must be a versioned reference")
        if type(self.target_duration_seconds) is not IntegerRange:  # noqa: E721
            raise StoryDesignModelError("target duration must be an integer interval")
        requirements = _tuple(self.material_requirements, MaterialRequirement)
        if len({item.requirement_id for item in requirements}) != len(requirements):
            raise StoryDesignModelError("material requirement IDs must be unique within proposal")
        for item in requirements:
            if item.obligation_ref not in self.required_obligation_refs:
                raise StoryDesignModelError("material obligation is absent from required obligations")
        _one_owner(self.source_refs)

    @property
    def narrative_refs(self) -> tuple[SemanticObjectRef, ...]:
        return (self.thread_refs + self.required_obligation_refs + self.required_fact_refs
                + self.key_character_refs)

    @property
    def source_refs(self) -> tuple[SemanticObjectRef, ...]:
        return tuple(ref for item in self.material_requirements for ref in (
            item.source_constraints.allowed_source_refs + item.source_constraints.forbidden_source_refs
        ))

    def to_mapping(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id, "title": self.title,
            "narrative_claim": self.narrative_claim,
            "thread_refs": [ref.to_mapping() for ref in self.thread_refs],
            "required_obligation_refs": [ref.to_mapping() for ref in self.required_obligation_refs],
            "required_fact_refs": [ref.to_mapping() for ref in self.required_fact_refs],
            "key_character_refs": [ref.to_mapping() for ref in self.key_character_refs],
            "genre_tags": list(self.genre_tags), "editing_profile": self.editing_profile.to_mapping(),
            "target_duration_seconds": self.target_duration_seconds.to_mapping(),
            "teaser_strategy": self.teaser_strategy, "audience_hook": self.audience_hook,
            "material_requirements": [item.to_mapping() for item in self.material_requirements],
        }

    @classmethod
    def from_mapping(cls, value: object) -> ProposalDraft:
        data = _closed(value, (
            "proposal_id", "title", "narrative_claim", "thread_refs", "required_obligation_refs",
            "required_fact_refs", "key_character_refs", "genre_tags", "editing_profile",
            "target_duration_seconds", "teaser_strategy", "audience_hook", "material_requirements",
        ))
        return cls(
            _text(data["proposal_id"]), _text(data["title"]), _text(data["narrative_claim"]),
            _array(data["thread_refs"], SemanticObjectRef.from_mapping),
            _array(data["required_obligation_refs"], SemanticObjectRef.from_mapping),
            _array(data["required_fact_refs"], SemanticObjectRef.from_mapping),
            _array(data["key_character_refs"], SemanticObjectRef.from_mapping),
            _array(data["genre_tags"], _text), EditingProfileReference.from_mapping(data["editing_profile"]),
            IntegerRange.from_mapping(data["target_duration_seconds"]), _text(data["teaser_strategy"]),
            _text(data["audience_hook"]),
            _array(data["material_requirements"], MaterialRequirement.from_mapping),
        )
