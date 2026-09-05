"""Whole-draft policy/reference checks before material feasibility search.

This is only a compiler building block: it grants neither candidate eligibility
nor admission. Unsupported but well-formed proposals must remain in the later
ProposalSet; malformed/foreign proposals must not be filtered out here.
"""

from __future__ import annotations

import re

from .member_refs import SemanticObjectRef
from .narrative_models import NarrativeGraph, ObligationAttributes
from .story_design_draft import ProposalDraftSet
from .story_design_models import JobPolicy, SourceConstraints, StoryDesignPolicy


_DIAGNOSTIC_RULE_IDS = frozenset({
    "SD-IN-001", "SD-IN-002", "SD-PROP-001", "SD-REF-001", "SD-ENUM-001",
    "SD-DUR-001", "SD-MAT-001", "SD-PHYS-DEFER-001",
})
_DIAGNOSTIC_ERROR_CODES = frozenset({
    "STORY_PROPOSAL_VALIDATION_FAILED", "GRAPH_REFERENCE_TYPE_MISMATCH",
    "GRAPH_REFERENCE_FOREIGN_OWNER", "GRAPH_REFERENCE_NOT_FOUND",
    "SOURCE_REFERENCE_FOREIGN_OWNER", "SOURCE_REFERENCE_NOT_FOUND",
    "REQUIRED_FACT_CLOSURE_MISMATCH",
})
_DIAGNOSTIC_OBJECT_TYPES = frozenset({
    "entity", "character", "fact", "event", "beat", "story_thread", "obligation",
    "character_state", "relationship", "question", "foreshadow", "source",
})
_DIAGNOSTIC_PATH = re.compile(
    r"\$(?:\.proposals\[[0-9]+\](?:\.(?:thread_refs|required_obligation_refs|"
    r"required_fact_refs|key_character_refs)(?:\[[0-9]+\])?|"
    r"\.material_requirements\[[0-9]+\]\.source_constraints\."
    r"(?:allowed_source_refs|forbidden_source_refs)\[[0-9]+\])|"
    r"\.job_policy\.source_constraints\."
    r"(?:allowed_source_refs|forbidden_source_refs)\[[0-9]+\])?\Z"
)


class StoryProposalValidationError(ValueError):
    def __init__(
        self, rule_id: str, detail: str, proposal_index: int | None = None, *,
        json_path: str = "$", error_code: str = "STORY_PROPOSAL_VALIDATION_FAILED",
        expected_object_type: str | None = None, actual_object_type: str | None = None,
        missing_count: int | None = None, unexpected_count: int | None = None,
    ) -> None:
        self.rule_id = rule_id
        self.proposal_index = proposal_index
        self.json_path = json_path
        self.error_code = error_code
        self.expected_object_type = expected_object_type
        self.actual_object_type = actual_object_type
        self.missing_count = missing_count
        self.unexpected_count = unexpected_count
        super().__init__(detail)

    def to_diagnostic(self) -> dict[str, object]:
        """Export only bounded metadata, never exception text or model identities.

        Keep the old exception constructor permissive. The diagnostic boundary
        additionally limits strings to known codes/types and structural paths,
        even if another caller supplied arbitrary exception metadata.
        """
        def known(value: object, allowed: frozenset[str]) -> str | None:
            return value if type(value) is str and value in allowed else None

        def count(value: object) -> int | None:
            return value if type(value) is int and 0 <= value <= 2**53 - 1 else None

        return {
            "rule_id": known(self.rule_id, _DIAGNOSTIC_RULE_IDS),
            "proposal_index": count(self.proposal_index),
            "json_path": self.json_path if (
                type(self.json_path) is str and len(self.json_path) <= 256
                and _DIAGNOSTIC_PATH.fullmatch(self.json_path)
            ) else "$",
            "error_code": known(self.error_code, _DIAGNOSTIC_ERROR_CODES)
            or "STORY_PROPOSAL_VALIDATION_FAILED",
            "expected_object_type": known(self.expected_object_type, _DIAGNOSTIC_OBJECT_TYPES),
            "actual_object_type": known(self.actual_object_type, _DIAGNOSTIC_OBJECT_TYPES),
            "missing_count": count(self.missing_count),
            "unexpected_count": count(self.unexpected_count),
        }


def validate_story_proposals(
    draft: ProposalDraftSet, *, graph: NarrativeGraph,
    graph_object_refs: tuple[SemanticObjectRef, ...], source_refs: tuple[SemanticObjectRef, ...],
    job_policy: JobPolicy, story_policy: StoryDesignPolicy,
) -> None:
    """Validate all original proposals, without mutating order or dropping rows.

    Exact committed readers must supply the Graph and its complete content-bound
    object universe, plus the granted Source universe. Passing fabricated values
    here does not prove they ever existed in a Store. Material/taint/canonical
    selection and physical handoff truth remain independent checks.
    """
    if (type(draft) is not ProposalDraftSet or type(graph) is not NarrativeGraph  # noqa: E721
            or type(job_policy) is not JobPolicy or type(story_policy) is not StoryDesignPolicy):  # noqa: E721
        raise StoryProposalValidationError("SD-IN-001", "proposal validation requires exact typed inputs")
    if (type(graph_object_refs) is not tuple or type(source_refs) is not tuple  # noqa: E721
            or any(type(ref) is not SemanticObjectRef for ref in (*graph_object_refs, *source_refs))):
        raise StoryProposalValidationError("SD-IN-001", "reference universes must be exact immutable values")
    graph_owners = {ref.member_ref for ref in graph_object_refs}
    graph_hash = graph.canonical_hash
    if (len(graph_owners) != 1 or len(set(graph_object_refs)) != len(graph_object_refs)
            or any(ref.member_ref.artifact_type != "narrative_graph" or ref.member_ref.content_hash != graph_hash for ref in graph_object_refs)
            or {(ref.object_id, ref.object_type) for ref in graph_object_refs} != {(node.node_id, node.node_type) for node in graph.nodes}):
        raise StoryProposalValidationError("SD-IN-001", "Graph reference universe differs from complete exact Graph")
    source_owners = {ref.member_ref for ref in source_refs}
    if (len(source_owners) != 1 or len(set(source_refs)) != len(source_refs)
            or any(ref.member_ref.artifact_type != "whole_series_source_manifest" or ref.object_type != "source" for ref in source_refs)
            or {owner.scope for owner in graph_owners} != {owner.scope for owner in source_owners}):
        raise StoryProposalValidationError("SD-IN-001", "Source reference universe has foreign or duplicate identities")
    if job_policy.story_design_policy_sha256 != story_policy.canonical_hash:
        raise StoryProposalValidationError("SD-IN-002", "Job policy does not bind the exact StoryDesignPolicy")
    count = len(draft.proposals)
    if not job_policy.proposal_count.minimum <= count <= job_policy.proposal_count.maximum or count < job_policy.selected_story_count:
        raise StoryProposalValidationError("SD-PROP-001", "original proposal count cannot satisfy the frozen count policy")
    graph_refs, sources = set(graph_object_refs), set(source_refs)
    graph_types = {node.node_id: node.node_type for node in graph.nodes}
    obligations = {
        node.node_id: node.attributes for node in graph.nodes
        if type(node.attributes) is ObligationAttributes
    }

    def constraints(value: SourceConstraints, index: int | None, path: str) -> None:
        for field, refs in (("allowed_source_refs", value.allowed_source_refs),
                            ("forbidden_source_refs", value.forbidden_source_refs)):
            for ref_index, ref in enumerate(refs):
                if ref not in sources:
                    raise StoryProposalValidationError(
                        "SD-REF-001", "source constraint names a foreign or absent Source", index,
                        json_path=f"{path}.{field}[{ref_index}]",
                        error_code=("SOURCE_REFERENCE_FOREIGN_OWNER"
                                    if ref.member_ref not in source_owners
                                    else "SOURCE_REFERENCE_NOT_FOUND"),
                        expected_object_type="source",
                    )

    constraints(job_policy.source_constraints, None, "$.job_policy.source_constraints")
    for index, proposal in enumerate(draft.proposals):
        # Preserve narrative_refs field order and the existing Graph-first rule
        # priority, while locating the first invalid original reference exactly.
        for field, refs in (
            ("thread_refs", proposal.thread_refs),
            ("required_obligation_refs", proposal.required_obligation_refs),
            ("required_fact_refs", proposal.required_fact_refs),
            ("key_character_refs", proposal.key_character_refs),
        ):
            for ref_index, ref in enumerate(refs):
                if ref in graph_refs:
                    continue
                actual_type = None
                if ref.member_ref not in graph_owners:
                    error_code = "GRAPH_REFERENCE_FOREIGN_OWNER"
                elif ref.object_id not in graph_types:
                    error_code = "GRAPH_REFERENCE_NOT_FOUND"
                else:
                    error_code = "GRAPH_REFERENCE_TYPE_MISMATCH"
                    actual_type = graph_types[ref.object_id]
                raise StoryProposalValidationError(
                    "SD-REF-001", "proposal names a foreign or absent Graph object", index,
                    json_path=f"$.proposals[{index}].{field}[{ref_index}]",
                    error_code=error_code, expected_object_type=ref.object_type,
                    actual_object_type=actual_type,
                )
        if (not set(proposal.genre_tags) <= set(story_policy.allowed_genre_tags)
                or proposal.editing_profile not in story_policy.editing_profiles
                or proposal.teaser_strategy not in story_policy.teaser_strategies):
            raise StoryProposalValidationError("SD-ENUM-001", "proposal editorial values are outside frozen policy", index)
        duration = proposal.target_duration_seconds
        bounds = job_policy.target_duration_seconds
        if not bounds.minimum <= duration.minimum <= duration.maximum <= bounds.maximum:
            raise StoryProposalValidationError("SD-DUR-001", "proposal duration is outside frozen Job bounds", index)
        if not proposal.required_obligation_refs or not proposal.material_requirements:
            raise StoryProposalValidationError("SD-MAT-001", "proposal must retain required material obligations", index)
        if {item.obligation_ref for item in proposal.material_requirements} != set(proposal.required_obligation_refs):
            raise StoryProposalValidationError("SD-MAT-001", "a required obligation has no declared material requirement", index)
        obligation_facts = {
            fact_id for ref in proposal.required_obligation_refs
            for fact_id in obligations[ref.object_id].required_fact_ids
        }
        declared_facts = {ref.object_id for ref in proposal.required_fact_refs}
        if declared_facts != obligation_facts:
            raise StoryProposalValidationError(
                "SD-MAT-001", "required facts must exactly cover the declared material obligations", index,
                json_path=f"$.proposals[{index}].required_fact_refs",
                error_code="REQUIRED_FACT_CLOSURE_MISMATCH", expected_object_type="fact",
                missing_count=len(obligation_facts - declared_facts),
                unexpected_count=len(declared_facts - obligation_facts),
            )
        for requirement_index, requirement in enumerate(proposal.material_requirements):
            constraints(
                requirement.source_constraints, index,
                f"$.proposals[{index}].material_requirements[{requirement_index}].source_constraints",
            )
            if not set(story_policy.required_physical_requirements) <= set(requirement.physical_requirements):
                raise StoryProposalValidationError("SD-PHYS-DEFER-001", "material requirement omits frozen deferred physical checks", index)
