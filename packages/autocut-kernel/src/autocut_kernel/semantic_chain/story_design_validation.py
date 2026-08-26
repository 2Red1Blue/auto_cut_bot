"""Whole-draft policy/reference checks before material feasibility search.

This is only a compiler building block: it grants neither candidate eligibility
nor admission. Unsupported but well-formed proposals must remain in the later
ProposalSet; malformed/foreign proposals must not be filtered out here.
"""

from __future__ import annotations

from .member_refs import SemanticObjectRef
from .narrative_models import NarrativeGraph, ObligationAttributes
from .story_design_draft import ProposalDraftSet
from .story_design_models import JobPolicy, SourceConstraints, StoryDesignPolicy


class StoryProposalValidationError(ValueError):
    def __init__(self, rule_id: str, detail: str, proposal_index: int | None = None) -> None:
        self.rule_id = rule_id
        self.proposal_index = proposal_index
        super().__init__(detail)


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
    obligations = {
        node.node_id: node.attributes for node in graph.nodes
        if type(node.attributes) is ObligationAttributes
    }

    def constraints(value: SourceConstraints, index: int | None) -> None:
        if not set((*value.allowed_source_refs, *value.forbidden_source_refs)) <= sources:
            raise StoryProposalValidationError("SD-REF-001", "source constraint names a foreign or absent Source", index)

    constraints(job_policy.source_constraints, None)
    for index, proposal in enumerate(draft.proposals):
        if not set(proposal.narrative_refs) <= graph_refs:
            raise StoryProposalValidationError("SD-REF-001", "proposal names a foreign or absent Graph object", index)
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
        if {ref.object_id for ref in proposal.required_fact_refs} != obligation_facts:
            raise StoryProposalValidationError(
                "SD-MAT-001", "required facts must exactly cover the declared material obligations", index,
            )
        for requirement in proposal.material_requirements:
            constraints(requirement.source_constraints, index)
            if not set(story_policy.required_physical_requirements) <= set(requirement.physical_requirements):
                raise StoryProposalValidationError("SD-PHYS-DEFER-001", "material requirement omits frozen deferred physical checks", index)
