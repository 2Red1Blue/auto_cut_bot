"""Version 2 domain choices. Observed people are not character identities.

These values are never the model wire and never evidence of Store authority.
The v1 codec remains character-only; this separate codec retains actual types.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.compiler.canonical import canonical_json_hash
from .member_refs import SemanticObjectRef
from .story_design_draft import StoryDesignDraftError, _binding
from .story_design_models import (
    EditingProfileReference,
    IntegerRange,
    MaterialRequirement,
    ProposalDraft,
    StoryDesignModelError,
    _array,
    _closed,
    _one_owner,
    _text,
    _tuple,
)

COMPACT_DOMAIN_SCHEMA_VERSION = "stage2-story-design-domain-v2"


@dataclass(frozen=True, slots=True)
class ProposalDraftV2:
    proposal_id: str
    title: str
    narrative_claim: str
    thread_refs: tuple[SemanticObjectRef, ...]
    required_obligation_refs: tuple[SemanticObjectRef, ...]
    required_fact_refs: tuple[SemanticObjectRef, ...]
    key_subject_refs: tuple[SemanticObjectRef, ...]
    genre_tags: tuple[str, ...]
    editing_profile: EditingProfileReference
    target_duration_seconds: IntegerRange
    teaser_strategy: str
    audience_hook: str
    material_requirements: tuple[MaterialRequirement, ...]

    def _shared_structure(self) -> ProposalDraft:
        # Reuse unchanged editorial/material structure checks; never relabel
        # subjects or pass observed entities into the character-only codec.
        return ProposalDraft(
            self.proposal_id, self.title, self.narrative_claim, self.thread_refs,
            self.required_obligation_refs, self.required_fact_refs, (), self.genre_tags,
            self.editing_profile, self.target_duration_seconds, self.teaser_strategy,
            self.audience_hook, self.material_requirements,
        )

    def __post_init__(self) -> None:
        self._shared_structure()
        for ref in _tuple(self.key_subject_refs, SemanticObjectRef):
            if (ref.member_ref.artifact_type != "narrative_graph"
                    or ref.object_type not in ("entity", "character")):
                raise StoryDesignModelError("subject requires an exact entity or character Graph reference")
        # Whether an entity is a person is checked against the actual Graph.
        _one_owner(self.narrative_refs)

    @property
    def subject_refs(self) -> tuple[SemanticObjectRef, ...]:
        return self.key_subject_refs

    @property
    def narrative_refs(self) -> tuple[SemanticObjectRef, ...]:
        return (self.thread_refs + self.required_obligation_refs + self.required_fact_refs
                + self.key_subject_refs)

    @property
    def source_refs(self) -> tuple[SemanticObjectRef, ...]:
        return self._shared_structure().source_refs

    def to_mapping(self) -> dict[str, object]:
        result = self._shared_structure().to_mapping()
        del result["key_character_refs"]
        result["key_subject_refs"] = [ref.to_mapping() for ref in self.key_subject_refs]
        return result

    @classmethod
    def from_mapping(cls, value: object) -> ProposalDraftV2:
        data = _closed(value, (
            "proposal_id", "title", "narrative_claim", "thread_refs", "required_obligation_refs",
            "required_fact_refs", "key_subject_refs", "genre_tags", "editing_profile",
            "target_duration_seconds", "teaser_strategy", "audience_hook", "material_requirements",
        ))
        return cls(
            _text(data["proposal_id"]), _text(data["title"]), _text(data["narrative_claim"]),
            _array(data["thread_refs"], SemanticObjectRef.from_mapping),
            _array(data["required_obligation_refs"], SemanticObjectRef.from_mapping),
            _array(data["required_fact_refs"], SemanticObjectRef.from_mapping),
            _array(data["key_subject_refs"], SemanticObjectRef.from_mapping),
            _array(data["genre_tags"], _text), EditingProfileReference.from_mapping(data["editing_profile"]),
            IntegerRange.from_mapping(data["target_duration_seconds"]), _text(data["teaser_strategy"]),
            _text(data["audience_hook"]), _array(data["material_requirements"], MaterialRequirement.from_mapping),
        )


@dataclass(frozen=True, slots=True)
class ProposalDraftSetV2:
    input_binding_sha256: str
    proposals: tuple[ProposalDraftV2, ...]

    def __post_init__(self) -> None:
        _binding(self.input_binding_sha256)
        # Compact domain values cannot become an empty, codec-ambiguous
        # MaterialSupportEvaluation. Historical v1 empty drafts are unchanged.
        rows = _tuple(self.proposals, ProposalDraftV2, nonempty=True)
        if len({row.proposal_id for row in rows}) != len(rows):
            raise StoryDesignDraftError("proposal IDs must be unique")
        graph_owners = {ref.member_ref for row in rows for ref in row.narrative_refs}
        source_owners = {ref.member_ref for row in rows for ref in row.source_refs}
        if len(graph_owners) > 1 or len(source_owners) > 1:
            raise StoryDesignDraftError("proposal set mixes exact narrative/source owners")
        if len({owner.scope for owner in graph_owners | source_owners}) > 1:
            raise StoryDesignDraftError("proposal set mixes scopes")

    def to_mapping(self) -> dict[str, object]:
        return {"schema_version": COMPACT_DOMAIN_SCHEMA_VERSION,
                "input_binding_sha256": self.input_binding_sha256,
                "proposals": [row.to_mapping() for row in self.proposals]}

    @classmethod
    def from_mapping(cls, value: object) -> ProposalDraftSetV2:
        data = _closed(value, ("schema_version", "input_binding_sha256", "proposals"))
        if data["schema_version"] != COMPACT_DOMAIN_SCHEMA_VERSION:
            raise StoryDesignDraftError("unsupported v2 domain proposal schema")
        return cls(_binding(data["input_binding_sha256"]),
                   _array(data["proposals"], ProposalDraftV2.from_mapping))

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())
