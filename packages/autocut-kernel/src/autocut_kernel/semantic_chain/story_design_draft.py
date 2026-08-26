"""Bounded Stage 2 proposal draft wire, without committed-input authority.

The caller must derive expected_input_binding_sha256 from the exact audited
inputs; matching a supplied digest does not verify that provenance. Directly
constructed drafts are equally untrusted. A Command must decode its audited raw
response again, and a compiler must verify object existence and all policies.
Proposal order is preserved: it will define lexicographic portfolio selection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import cast

from ..contracts.compiler.canonical import canonical_json_hash, load_canonical_json_bytes
from .story_design_models import PHYSICAL_REQUIREMENT_MODES, ProposalDraft, StoryDesignModelError

STORY_DESIGN_DRAFT_SCHEMA_VERSION = "stage2-story-design-draft-v1"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SAFE_INTEGER = 2**53 - 1
# Implementation resource ceilings, not default deployment/Job policies. Every
# limit is explicitly supplied and hashed; no payload is ever truncated.
_LIMIT_CEILINGS = {
    "max_response_bytes": 16 * 1024 * 1024,
    "max_json_depth": 64,
    "max_proposals": 256,
    "max_material_requirements_per_proposal": 128,
    "max_total_material_requirements": 1024,
    "max_references_per_field": 1024,
    "max_total_references": 8192,
    "max_genre_tags": 64,
    "max_text_characters": 64 * 1024,
    "max_total_text_characters": 2 * 1024 * 1024,
}
_REF_FIELDS = frozenset({
    "thread_refs", "required_obligation_refs", "required_fact_refs", "key_character_refs",
    "allowed_source_refs", "forbidden_source_refs",
})


class StoryDesignDraftError(StoryDesignModelError):
    """A draft fails its closed wire, expected binding or explicit limits."""


def _closed(value: object, names: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise StoryDesignDraftError("draft value must be a closed object")
    mapping = cast(dict[str, object], value)
    if any(type(key) is not str for key in mapping) or set(mapping) != set(names):  # noqa: E721
        raise StoryDesignDraftError("draft object has missing or unknown fields")
    return mapping


def _binding(value: object) -> str:
    if (type(value) is not str or _SHA256.fullmatch(value) is None  # noqa: E721
            or value == "sha256:" + "0" * 64):
        raise StoryDesignDraftError("input binding must be a nonzero lowercase sha256")
    return value


@dataclass(frozen=True, slots=True)
class StoryDesignDraftPolicy:
    max_response_bytes: int
    max_json_depth: int
    max_proposals: int
    max_material_requirements_per_proposal: int
    max_total_material_requirements: int
    max_references_per_field: int
    max_total_references: int
    max_genre_tags: int
    max_text_characters: int
    max_total_text_characters: int

    def __post_init__(self) -> None:
        for field in fields(self):
            value = cast(object, getattr(self, field.name))
            if type(value) is not int or not 1 <= value <= _LIMIT_CEILINGS[field.name]:  # noqa: E721
                raise StoryDesignDraftError("draft limit is invalid or exceeds its hard ceiling")
        if self.max_text_characters > self.max_total_text_characters:
            raise StoryDesignDraftError("per-string limit exceeds total text limit")

    def to_mapping(self) -> dict[str, object]:
        return {field.name: cast(object, getattr(self, field.name)) for field in fields(self)}

    @classmethod
    def from_mapping(cls, value: object) -> StoryDesignDraftPolicy:
        data = _closed(value, tuple(_LIMIT_CEILINGS))
        return cls(**{key: cast(int, item) for key, item in data.items()})

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ProposalDraftSet:
    input_binding_sha256: str
    proposals: tuple[ProposalDraft, ...]

    def __post_init__(self) -> None:
        _binding(self.input_binding_sha256)
        if type(self.proposals) is not tuple or any(  # noqa: E721
            type(item) is not ProposalDraft for item in self.proposals
        ):
            raise StoryDesignDraftError("proposals must be an immutable typed tuple")
        if len({item.proposal_id for item in self.proposals}) != len(self.proposals):
            raise StoryDesignDraftError("proposal IDs must be unique")
        graph_owners = {ref.member_ref for item in self.proposals for ref in item.narrative_refs}
        source_owners = {ref.member_ref for item in self.proposals for ref in item.source_refs}
        if len(graph_owners) > 1 or len(source_owners) > 1:
            raise StoryDesignDraftError("proposal set mixes exact narrative/source owners")
        if len({owner.scope for owner in graph_owners | source_owners}) > 1:
            raise StoryDesignDraftError("proposal set mixes scopes")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": STORY_DESIGN_DRAFT_SCHEMA_VERSION,
            "input_binding_sha256": self.input_binding_sha256,
            "proposals": [item.to_mapping() for item in self.proposals],
        }

    @classmethod
    def from_mapping(cls, value: object) -> ProposalDraftSet:
        data = _closed(value, ("schema_version", "input_binding_sha256", "proposals"))
        if type(data["schema_version"]) is not str or data["schema_version"] != STORY_DESIGN_DRAFT_SCHEMA_VERSION:  # noqa: E721
            raise StoryDesignDraftError("unsupported proposal draft schema version")
        if type(data["proposals"]) is not list:  # noqa: E721
            raise StoryDesignDraftError("proposals must be an array")
        return cls(_binding(data["input_binding_sha256"]), tuple(
            ProposalDraft.from_mapping(item) for item in cast(list[object], data["proposals"])
        ))

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


def _bounded_value(raw: bytes, policy: StoryDesignDraftPolicy) -> object:
    if type(raw) is not bytes or not 0 < len(raw) <= policy.max_response_bytes:  # noqa: E721
        raise StoryDesignDraftError("draft response violates byte bound")
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
                raise StoryDesignDraftError("draft exceeds JSON depth bound")
        elif byte in (93, 125):
            depth -= 1
    try:
        value, _canonical = load_canonical_json_bytes(raw, origin="Stage 2 proposal draft")
    except (ValueError, RecursionError) as error:
        raise StoryDesignDraftError("draft must be strict UTF-8 JSON without floats") from error
    return cast(object, value)


def _check_limits(value: object, policy: StoryDesignDraftPolicy) -> None:
    pending = [value]
    text_count = reference_count = requirement_count = 0
    while pending:
        current = pending.pop()
        if type(current) is str:
            text_count += len(current)
            if len(current) > policy.max_text_characters:
                raise StoryDesignDraftError("draft string exceeds text bound")
        elif type(current) is list:
            pending.extend(cast(list[object], current))
        elif type(current) is dict:
            mapping = cast(dict[str, object], current)
            # Includes keys and all ref/owner text: repeated owner identities also
            # consume the explicit context budget, not just natural language.
            text_count += sum(len(key) for key in mapping)
            for key, item in mapping.items():
                if type(item) is list:
                    count = len(cast(list[object], item))
                    if key in _REF_FIELDS:
                        reference_count += count
                        if count > policy.max_references_per_field:
                            raise StoryDesignDraftError("draft reference field exceeds bound")
                    bound = {
                        "proposals": policy.max_proposals,
                        "material_requirements": policy.max_material_requirements_per_proposal,
                        "physical_requirements": 3,
                        "genre_tags": policy.max_genre_tags,
                    }.get(key)
                    if bound is not None and count > bound:
                        raise StoryDesignDraftError("draft array exceeds bound")
                    if key == "material_requirements":
                        requirement_count += count
                if key == "obligation_ref":
                    reference_count += 1
                pending.append(cast(object, item))
        if text_count > policy.max_total_text_characters:
            raise StoryDesignDraftError("draft total text exceeds bound")
        if reference_count > policy.max_total_references:
            raise StoryDesignDraftError("draft total references exceed bound")
        if requirement_count > policy.max_total_material_requirements:
            raise StoryDesignDraftError("draft total material requirements exceed bound")


def decode_story_design_draft(
    raw: bytes, *, expected_input_binding_sha256: str, policy: StoryDesignDraftPolicy
) -> ProposalDraftSet:
    """Check untrusted wire only; never produce support, selection or rule results."""
    if type(policy) is not StoryDesignDraftPolicy:  # noqa: E721
        raise StoryDesignDraftError("an explicit typed draft policy is required")
    binding = _binding(expected_input_binding_sha256)
    value = _bounded_value(raw, policy)
    _check_limits(value, policy)
    try:
        draft = ProposalDraftSet.from_mapping(value)
    except ValueError as error:
        raise StoryDesignDraftError("draft violates its closed proposal contract") from error
    if draft.input_binding_sha256 != binding:
        raise StoryDesignDraftError("draft does not match exact input binding")
    return draft


def story_design_draft_response_schema(policy: StoryDesignDraftPolicy) -> dict[str, object]:
    """Fresh closed response schema. Cross-field/total budgets remain decoder checks."""
    if type(policy) is not StoryDesignDraftPolicy:  # noqa: E721
        raise StoryDesignDraftError("an explicit typed draft policy is required")

    def obj(properties: dict[str, object]) -> dict[str, object]:
        return {"type": "object", "properties": properties, "required": list(properties),
                "additionalProperties": False}

    def text() -> dict[str, object]:
        return {"type": "string", "minLength": 1, "maxLength": policy.max_text_characters,
                "pattern": r"\S"}

    def integer() -> dict[str, object]:
        return {"type": "integer", "minimum": 1, "maximum": _SAFE_INTEGER}

    def digest() -> dict[str, object]:
        return {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}

    def array(item: dict[str, object], maximum: int, *, minimum: int = 0) -> dict[str, object]:
        return {"type": "array", "items": item, "minItems": minimum,
                "maxItems": maximum, "uniqueItems": True}

    def ref(kind: str, artifact_type: str = "narrative_graph") -> dict[str, object]:
        return obj({
            "member_ref": obj({
                "artifact_type": {"const": artifact_type}, "logical_id": text(),
                "revision": integer(), "content_hash": digest(),
                "scope": obj({"namespace": text(), "kind": text(), "key": text()}),
            }),
            "object_type": {"const": kind}, "object_id": text(),
        })

    def refs(kind: str, artifact_type: str = "narrative_graph") -> dict[str, object]:
        return array(ref(kind, artifact_type), policy.max_references_per_field)

    def constraints() -> dict[str, object]:
        return obj({
            "allowed_source_refs": refs("source", "whole_series_source_manifest"),
            "forbidden_source_refs": refs("source", "whole_series_source_manifest"),
            "authorization_purpose": {"const": "render_source"},
        })

    physical: dict[str, object] = {
        "oneOf": [obj({"requirement_kind": {"const": kind}, "mode": {"const": mode}})
                  for kind, mode in PHYSICAL_REQUIREMENT_MODES]
    }
    requirement = obj({
        "requirement_id": text(), "obligation_ref": ref("obligation"),
        "minimum_usable_seconds": integer(), "physical_requirements": array(physical, 3),
        "source_constraints": constraints(),
    })
    proposal = obj({
        "proposal_id": text(), "title": text(), "narrative_claim": text(),
        "thread_refs": refs("story_thread"), "required_obligation_refs": refs("obligation"),
        "required_fact_refs": refs("fact"), "key_character_refs": refs("character"),
        "genre_tags": array(text(), policy.max_genre_tags, minimum=1),
        "editing_profile": obj({"profile_id": text(), "profile_version": text()}),
        "target_duration_seconds": obj({"min": integer(), "max": integer()}),
        "teaser_strategy": text(), "audience_hook": text(),
        "material_requirements": array(requirement, policy.max_material_requirements_per_proposal),
    })
    schema = obj({
        "schema_version": {"const": STORY_DESIGN_DRAFT_SCHEMA_VERSION},
        "input_binding_sha256": {**digest(), "not": {"const": "sha256:" + "0" * 64}},
        "proposals": array(proposal, policy.max_proposals),
    })
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
