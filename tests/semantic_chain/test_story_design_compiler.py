"""Pure four-member composition tests; synthetic support is not Store truth."""

from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.candidate_duration import ConservativeDuration
from autocut_kernel.semantic_chain.material_support_models import (
    ExclusionReasonCount,
    FactCarryWitness,
    MaterialSupportEvaluation,
    ProposalMaterialSupport,
    RequirementAlternativeProof,
    RequirementMaterialSupport,
)
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.portfolio_values import InitialSourceUsageLedger, StoryPortfolio
from autocut_kernel.semantic_chain.story_design_compiler import (
    STAGE2_BUSINESS_MEMBER_TYPES,
    compose_story_design_members,
    material_search_universe,
    select_material_portfolio,
)
from autocut_kernel.semantic_chain.story_design_draft import ProposalDraftSet
from autocut_kernel.semantic_chain.story_design_models import IntegerRange, SourceConstraints

from tests.semantic_chain.test_candidate_catalog import _candidate, catalog_for
from tests.semantic_chain.test_story_design_context import projection_for
from tests.semantic_chain.test_story_design_models import _job_policy, _proposal


def composition_case(*, context_only=False):
    candidate = _candidate()
    if context_only:
        event = candidate.anchor_event
        event_id = "sha256:" + "9" * 64
        context = replace(event, vlm_event_ref=replace(event.vlm_event_ref, object_id=event_id),
                          graph_event_ref=replace(event.graph_event_ref, object_id=event_id),
                          event_card_ref=replace(event.event_card_ref, object_id=event_id))
        candidate = replace(candidate, context_events=(context,))
    candidate = replace(candidate, support=replace(candidate.support, conservative_duration=ConservativeDuration(20, 1)))
    catalog = catalog_for(candidate)
    projection = projection_for(catalog, catalog.narrative_graph_member_ref.scope)
    graph = catalog.narrative_graph_member_ref
    fact = SemanticObjectRef(graph, "fact", candidate.measurements[0].fact_refs[0].object_id)
    obligation = SemanticObjectRef(graph, "obligation", "test-obligation")
    source_constraints = SourceConstraints((), (), "render_source")
    template = _proposal()
    requirement = replace(template.material_requirements[0], obligation_ref=obligation,
                          source_constraints=source_constraints)
    proposal = replace(template, thread_refs=(), key_character_refs=(),
                       required_fact_refs=(fact,), required_obligation_refs=(obligation,),
                       material_requirements=(requirement,))
    catalog_ref = SemanticMemberIdentity.from_artifact_member(projection.member)
    witness = FactCarryWitness(fact, candidate.measurements[0].fact_refs[0],
                               (candidate.anchor_event.event_card_ref,))
    alternative = RequirementAlternativeProof(SemanticObjectRef(catalog_ref, "candidate", candidate.candidate_id),
                                              candidate.source_ref, (witness,), candidate.support.conservative_duration)
    row = RequirementMaterialSupport(requirement.requirement_id, (fact,), requirement.minimum_usable_seconds,
                                     requirement.physical_requirements_hash, (alternative,), (), (), 1)
    proposals = tuple(ProposalMaterialSupport(index, replace(proposal, proposal_id=f"p{index}"),
                                              (row,), (), False) for index in range(3))
    binding = "sha256:" + "1" * 64
    draft = ProposalDraftSet(binding, tuple(item.proposal for item in proposals))
    support = MaterialSupportEvaluation(binding, draft.canonical_hash, catalog_ref,
                                         catalog.source_grant_sha256, proposals)
    job = replace(_job_policy(), proposal_count=IntegerRange(3, 3), selected_story_count=1,
                  max_search_states=100, source_constraints=source_constraints)
    return projection, support, job


def unsupported(row, *, unknown=False):
    requirement = replace(row.requirements[0], alternatives=(), exclusion_reason_counts=(
        ExclusionReasonCount("dependency_frontier_unknown" if unknown else "duration_insufficient", 1),
    ))
    return replace(row, requirements=(requirement,), dependency_unknown=unknown)


def test_complete_acyclic_business_chain_preserves_unselected_proposals():
    projection, support, job = composition_case()
    support = replace(support, proposals=(unsupported(support.proposals[0]), *support.proposals[1:]))
    result = compose_story_design_members(projection, support, job_policy=job)
    assert result.search.proposal_indexes == (1,)
    assert tuple(member.artifact_type for member in result.business_members) == STAGE2_BUSINESS_MEMBER_TYPES
    assert result.proposal_set == support
    assert len(result.proposal_set.proposals) == 3
    import json
    candidate, proposals, portfolio, ledger = result.business_members
    assert candidate == projection.member
    assert MaterialSupportEvaluation.from_mapping(json.loads(proposals.payload_json)) == support
    selected = StoryPortfolio.from_mapping(json.loads(portfolio.payload_json))
    usage = InitialSourceUsageLedger.from_mapping(json.loads(ledger.payload_json))
    assert selected.proposal_set_ref == SemanticMemberIdentity.from_artifact_member(proposals)
    assert usage.portfolio_ref == SemanticMemberIdentity.from_artifact_member(portfolio)
    assert usage.target_story_ids == selected.target_story_ids
    assert "selected" not in json.loads(proposals.payload_json)
    assert compose_story_design_members(projection, support, job_policy=job) == result


def test_tainted_proposal_is_preserved_but_cannot_be_selected():
    projection, support, job = composition_case()
    seed = SemanticObjectRef(projection.catalog.coverage_ledger_member_ref, "taint_seed", "test-seed")
    first = replace(support.proposals[0], narrative_taint_seed_refs=(seed,))
    support = replace(support, proposals=(first, *support.proposals[1:]))
    universe = material_search_universe(support)
    assert len(universe) == 3 and universe[0].proposal_index == 0
    assert not universe[0].requirements[0].alternatives
    assert compose_story_design_members(projection, support, job_policy=job).search.proposal_indexes == (1,)


def test_unknown_earlier_proposal_is_not_skipped_for_later_known_feasible():
    projection, support, job = composition_case()
    support = replace(support, proposals=(unsupported(support.proposals[0], unknown=True), *support.proposals[1:]))
    result = compose_story_design_members(projection, support, job_policy=job)
    assert result.search.status == "indeterminate"
    assert result.search.visited_states == 0
    assert result.business_members == ()
    assert result.search.proposal_indexes == result.search.assignment == ()
    assert result.proposal_set == support


def test_known_unsupported_wins_over_unknown_and_does_not_block_other_proposals():
    _, support, job = composition_case()
    first = replace(unsupported(support.proposals[0]), dependency_unknown=True)
    support = replace(support, proposals=(first, *support.proposals[1:]))
    assert select_material_portfolio(support, job_policy=job).proposal_indexes == (1,)


@pytest.mark.parametrize("mode", ["infeasible", "budget"])
def test_no_partial_business_members_or_lowered_target_count_on_failure(mode):
    projection, support, job = composition_case()
    if mode == "infeasible":
        job = replace(job, selected_story_count=2, source_reuse_policy="forbid")
    else:
        job = replace(job, max_search_states=1)
    result = compose_story_design_members(projection, support, job_policy=job)
    assert result.search.status == ("infeasible" if mode == "infeasible" else "indeterminate")
    assert result.business_members == ()
    assert len(result.proposal_set.proposals) == 3
    assert not result.search.proposal_indexes


@pytest.mark.parametrize("target", ["catalog", "grant", "count", "payload"])
def test_composition_rejects_mixed_identity_or_original_count(target):
    projection, support, job = composition_case()
    if target == "catalog":
        projection = projection_for(projection.catalog, projection.member.scope, revision=2)
    elif target == "grant":
        support = replace(support, source_grant_sha256="sha256:" + "7" * 64)
    elif target == "count":
        job = replace(job, proposal_count=IntegerRange(4, 4))
    else:
        projection = replace(projection, member=replace(projection.member,
                             payload_json=canonical_json_bytes({"foreign": True}).decode()))
    with pytest.raises(ValueError):
        compose_story_design_members(projection, support, job_policy=job)
