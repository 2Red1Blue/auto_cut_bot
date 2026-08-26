"""Bounded unpartitioned Stage 3 draft batch, never admitted output.

The byte/depth guards precede the shared strict JSON decoder. Limits count the
entire response, including repeated owner references and JSON keys. Model order
is preserved: Beat ordinal is its array position, not a producer-selected key.
The caller must derive binding/targets from audited predecessors. These values
alone neither establish that provenance nor validate object existence/support.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import cast

from ..contracts.compiler.canonical import canonical_json_hash, load_canonical_json_bytes
from ..vlm.models import VlmNarrativeFunction
from .editorial_models import (
    NARRATIVE_ROLES,
    SPAN_INTENTS,
    EditorialModelError,
    StoryBlueprintDraft,
    editorial_array,
    editorial_hash,
    editorial_mapping,
    editorial_tuple,
    validate_editorial_owners,
)

EDITORIAL_DRAFT_SCHEMA_VERSION = "stage3-editorial-blueprint-draft-v1"
# Implementation ceilings, never default deployment policy. All limits must be
# explicitly supplied and hashed. No optional pruning or partition fallback.
_CEILINGS = {
    "max_response_bytes": 16 * 1024 * 1024, "max_json_depth": 64,
    "max_stories": 128, "max_beats_per_story": 128, "max_total_beats": 1024,
    "max_requirements_per_beat": 64, "max_total_requirements": 4096,
    "max_alternatives_per_requirement": 128, "max_total_alternatives": 8192,
    "max_references_per_field": 1024, "max_total_references": 65536,
    "max_ordering_constraints_per_story": 1024, "max_total_ordering_constraints": 8192,
    "max_text_characters": 65536, "max_total_text_characters": 4 * 1024 * 1024,
}
_REFERENCE_ARRAYS = frozenset({"required_obligation_refs", "required_fact_refs", "candidate_preferences",
                               "event_refs", "candidate_refs"})


class EditorialDraftError(EditorialModelError):
    """Untrusted draft violates closed content, expected targets or limits."""


@dataclass(frozen=True, slots=True)
class EditorialDraftPolicy:
    budget_unit: str
    max_response_bytes: int
    max_json_depth: int
    max_stories: int
    max_beats_per_story: int
    max_total_beats: int
    max_requirements_per_beat: int
    max_total_requirements: int
    max_alternatives_per_requirement: int
    max_total_alternatives: int
    max_references_per_field: int
    max_total_references: int
    max_ordering_constraints_per_story: int
    max_total_ordering_constraints: int
    max_text_characters: int
    max_total_text_characters: int

    def __post_init__(self) -> None:
        if type(self.budget_unit) is not str or self.budget_unit != "bytes":  # noqa: E721
            raise EditorialDraftError("editorial response budget unit must be bytes")
        for name, ceiling in _CEILINGS.items():
            value = cast(object, getattr(self, name))
            if type(value) is not int or not 1 <= value <= ceiling:  # noqa: E721
                raise EditorialDraftError("editorial limit is invalid or exceeds its hard ceiling")
        if self.max_text_characters > self.max_total_text_characters:
            raise EditorialDraftError("editorial per-string bound exceeds total text bound")

    def to_mapping(self) -> dict[str, object]:
        return {field.name: cast(object, getattr(self, field.name)) for field in fields(self)}

    @classmethod
    def from_mapping(cls, value: object) -> EditorialDraftPolicy:
        item = editorial_mapping(value, ("budget_unit", *_CEILINGS))
        return cls(cast(str, item["budget_unit"]), **{name: cast(int, item[name]) for name in _CEILINGS})

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class EditorialBlueprintDraft:
    input_binding_sha256: str
    stories: tuple[StoryBlueprintDraft, ...]

    def __post_init__(self) -> None:
        editorial_hash(self.input_binding_sha256)
        editorial_tuple(self.stories, StoryBlueprintDraft, nonempty=True)
        if len({story.story_id for story in self.stories}) != len(self.stories):
            raise EditorialDraftError("editorial batch contains duplicate Stories")
        if len({story.proposal_ref for story in self.stories}) != len(self.stories):
            raise EditorialDraftError("editorial batch repeats one selected Proposal")
        validate_editorial_owners(tuple(ref for story in self.stories for ref in story.references))

    def to_mapping(self) -> dict[str, object]:
        return {"schema_version": EDITORIAL_DRAFT_SCHEMA_VERSION, "input_binding_sha256": self.input_binding_sha256,
                "stories": [story.to_mapping() for story in self.stories]}

    @classmethod
    def from_mapping(cls, value: object) -> EditorialBlueprintDraft:
        item = editorial_mapping(value, ("schema_version", "input_binding_sha256", "stories"))
        if type(item["schema_version"]) is not str or item["schema_version"] != EDITORIAL_DRAFT_SCHEMA_VERSION:  # noqa: E721
            raise EditorialDraftError("unsupported editorial draft schema version")
        return cls(editorial_hash(item["input_binding_sha256"]), editorial_array(item["stories"], StoryBlueprintDraft.from_mapping))

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


def _targets(value: tuple[str, ...], policy: EditorialDraftPolicy) -> tuple[str, ...]:
    if type(policy) is not EditorialDraftPolicy:  # noqa: E721
        raise EditorialDraftError("an explicit typed editorial draft policy is required")
    targets = editorial_tuple(value, str, nonempty=True)
    for story in targets:
        editorial_hash(story)
    if len(targets) != len(set(targets)) or len(targets) > policy.max_stories:
        raise EditorialDraftError("frozen editorial targets are duplicated or exceed the Story bound")
    return targets


def _bounded_value(raw: bytes, policy: EditorialDraftPolicy) -> object:
    if type(raw) is not bytes or not 0 < len(raw) <= policy.max_response_bytes:  # noqa: E721
        raise EditorialDraftError("editorial response violates byte bound")
    depth, quoted, escaped = 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > policy.max_json_depth:
                raise EditorialDraftError("editorial response exceeds JSON depth bound")
        elif byte in (93, 125):
            depth -= 1
    try:
        value, _canonical = load_canonical_json_bytes(raw, origin="Stage 3 editorial draft")
    except (ValueError, RecursionError) as error:
        raise EditorialDraftError("editorial draft must be strict UTF-8 JSON without floats") from error
    return cast(object, value)


def _check_limits(value: object, policy: EditorialDraftPolicy) -> None:
    pending = [value]
    text_count = references = 0
    totals = dict.fromkeys(("beats", "evidence_requirements", "alternative_sets", "ordering_constraints"), 0)
    bounds = {
        "stories": policy.max_stories, "beats": policy.max_beats_per_story,
        "evidence_requirements": policy.max_requirements_per_beat,
        "alternative_sets": policy.max_alternatives_per_requirement,
        "ordering_constraints": policy.max_ordering_constraints_per_story,
    }
    while pending:
        current = pending.pop()
        if type(current) is str:
            text_count += len(current)
            if len(current) > policy.max_text_characters:
                raise EditorialDraftError("editorial string exceeds text bound")
        elif type(current) is list:
            pending.extend(cast(list[object], current))
        elif type(current) is dict:
            mapping = cast(dict[str, object], current)
            pending.extend(mapping.keys())
            for name, item in mapping.items():
                if type(item) is list:
                    count = len(cast(list[object], item))
                    if name in _REFERENCE_ARRAYS and count > policy.max_references_per_field:
                        raise EditorialDraftError("editorial reference field exceeds bound")
                    if name in bounds and count > bounds[name]:
                        raise EditorialDraftError("editorial array exceeds bound")
                    if name in totals:
                        totals[name] += count
                # Counts every occurrence, including proposal_ref and repeats
                # across alternatives. A digest/ID string is not a reference.
                if name == "member_ref":
                    references += 1
                pending.append(cast(object, item))
        if text_count > policy.max_total_text_characters:
            raise EditorialDraftError("editorial total text exceeds bound")
        if references > policy.max_total_references:
            raise EditorialDraftError("editorial total references exceed bound")
        if (totals["beats"] > policy.max_total_beats
                or totals["evidence_requirements"] > policy.max_total_requirements
                or totals["alternative_sets"] > policy.max_total_alternatives
                or totals["ordering_constraints"] > policy.max_total_ordering_constraints):
            raise EditorialDraftError("editorial total collection count exceeds bound")


def decode_editorial_draft(
    raw: bytes, *, expected_input_binding_sha256: str,
    expected_target_story_ids: tuple[str, ...], policy: EditorialDraftPolicy,
) -> EditorialBlueprintDraft:
    """Decode every frozen Story in order; never repair, trim or admit content."""
    targets = _targets(expected_target_story_ids, policy)
    binding = editorial_hash(expected_input_binding_sha256)
    value = _bounded_value(raw, policy)
    _check_limits(value, policy)
    try:
        draft = EditorialBlueprintDraft.from_mapping(value)
    except ValueError as error:
        raise EditorialDraftError("editorial draft violates its closed narrative contract") from error
    if draft.input_binding_sha256 != binding or tuple(story.story_id for story in draft.stories) != targets:
        raise EditorialDraftError("editorial draft differs from exact input binding/frozen target order")
    return draft


def editorial_draft_response_schema(
    policy: EditorialDraftPolicy, *, target_story_ids: tuple[str, ...],
) -> dict[str, object]:
    """Fresh bounded schema; exact target order and aggregate limits are parser checks.

    Homogeneous items retain the current provider response-schema shape. The
    explicit target enum/length restrict membership/count, while the decoder
    checks exact position (and same-owner joins) independently.
    """
    targets = _targets(target_story_ids, policy)

    def obj(properties: dict[str, object]) -> dict[str, object]:
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    def text() -> dict[str, object]:
        return {"type": "string", "minLength": 1, "maxLength": policy.max_text_characters, "pattern": r"\S"}

    def integer(minimum: int = 1, maximum: int = 2**53 - 1) -> dict[str, object]:
        return {"type": "integer", "minimum": minimum, "maximum": maximum}

    def digest() -> dict[str, object]:
        return {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$", "not": {"const": "sha256:" + "0" * 64}}

    def array(item: dict[str, object], maximum: int, minimum: int = 0, *, unique: bool = True) -> dict[str, object]:
        result: dict[str, object] = {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum}
        if unique:
            result["uniqueItems"] = True
        return result

    def ref(artifact: str, kind: str) -> dict[str, object]:
        return obj({"member_ref": obj({"artifact_type": {"const": artifact}, "logical_id": text(),
                    "revision": integer(), "scope": obj({"namespace": text(), "kind": text(), "key": text()}),
                    "content_hash": digest()}), "object_type": {"const": kind}, "object_id": text()})

    def refs(artifact: str, kind: str, minimum: int = 0) -> dict[str, object]:
        return array(ref(artifact, kind), policy.max_references_per_field, minimum)

    duration = obj({"min": integer(), "target": integer(), "max": integer()})
    span: dict[str, object] = {"type": "string", "enum": list(SPAN_INTENTS)}
    alternative = obj({"alternative_id": text(), "event_refs": refs("event_card_set", "event", 1),
                       "candidate_refs": refs("candidate_catalog", "candidate", 1)})
    requirement = obj({"source_material_requirement_id": text(), "satisfaction": {"enum": ["one_of", "all_of"]},
                       "alternative_sets": array(alternative, policy.max_alternatives_per_requirement, 1)})
    beat = obj({"narrative_role": {"enum": list(NARRATIVE_ROLES)},
                "narrative_function": {"enum": [item.value for item in VlmNarrativeFunction]}, "summary": text(),
                "required_obligation_refs": refs("narrative_graph", "obligation"),
                "required_fact_refs": refs("narrative_graph", "fact"),
                "evidence_requirements": array(requirement, policy.max_requirements_per_beat, 1),
                "candidate_preferences": refs("candidate_catalog", "candidate"),
                "span_policy": obj({"preferred": span, "allowed": array(span, 3, 1), "fallback_order": array(span, 3, 1)}),
                "duration_seconds": duration})
    ordering: dict[str, object] = {"oneOf": []}
    variants: list[dict[str, object]] = []
    for kind in ("precedes", "adjacent", "max_gap"):
        names = ("first_ordinal", "second_ordinal") if kind == "adjacent" else ("before_ordinal", "after_ordinal")
        properties: dict[str, object] = {"constraint_type": {"const": kind},
                                         **{name: integer(0, policy.max_beats_per_story - 1) for name in names}}
        if kind == "max_gap":
            properties["maximum_gap"] = obj({"tick": integer(0), "time_base": obj({"num": integer(), "den": integer()})})
        variants.append(obj(properties))
    ordering["oneOf"] = variants
    story = obj({"story_id": {"type": "string", "enum": list(targets)}, "proposal_ref": ref("proposal_set", "proposal"),
                 "beats": array(beat, policy.max_beats_per_story, 1, unique=False),
                 "ordering_constraints": array(ordering, policy.max_ordering_constraints_per_story),
                 "story_duration_seconds": duration,
                 "editing_intent": obj({"pacing": {"enum": ["slow", "balanced", "fast"]},
                                        "continuity_priority": {"enum": ["low", "medium", "high"]}}),
                 "teaser_intent": obj({"strategy": text(), "duration_seconds": obj({"min": integer(), "max": integer()})})})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **obj({
        "schema_version": {"const": EDITORIAL_DRAFT_SCHEMA_VERSION}, "input_binding_sha256": digest(),
        "stories": array(story, len(targets), len(targets)),
    })}
