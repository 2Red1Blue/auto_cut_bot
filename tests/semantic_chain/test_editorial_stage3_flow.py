"""Request -> raw draft -> complete pending Blueprint batch -> material proof.

Actual Stage1/2 Commands run on test-only in-memory persistence during setup.
Stage3 below is pure: no provider, durable Command, Admission or real-run claim.
"""

from copy import deepcopy
from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.pipeline.build_editorial_blueprint_request import prepare_stage3_request
from autocut_kernel.pipeline.build_narrative_graph_command import BuildNarrativeGraphCommand
from autocut_kernel.pipeline.compile_story_portfolio_command import CompileStoryPortfolioCommand
from autocut_kernel.semantic_chain.editorial_blueprint import project_editorial_blueprints
from autocut_kernel.semantic_chain.editorial_draft import decode_editorial_draft
from autocut_kernel.semantic_chain.editorial_feasibility import (
    evaluate_editorial_feasibility,
    verify_editorial_feasibility,
)
from autocut_kernel.semantic_chain.editorial_members import (
    compose_editorial_business_members,
    decode_editorial_business_members,
)

from tests.semantic_chain.test_build_editorial_blueprint_request import stage3_case


@pytest.fixture(scope="module")
def case():
    inputs, request = stage3_case()
    prepared = prepare_stage3_request(request, inputs)
    stage2 = inputs.portfolio.values
    catalog = {candidate.candidate_id: candidate for candidate in stage2.business.candidate_catalog.candidates}
    stories = []
    for selection in stage2.business.portfolio.selections:
        support = stage2.business.proposal_set.proposals[selection.proposal_index]
        proposal = support.proposal
        beats = []
        for material, evidence in zip(proposal.material_requirements, support.requirements, strict=True):
            candidate_ref = evidence.alternatives[0].candidate_ref
            candidate = catalog[candidate_ref.object_id]
            beats.append({
                "narrative_role": "reveal", "narrative_function": candidate.narrative_functions[0],
                "summary": "保留完整事件和必选事实", "required_obligation_refs": [material.obligation_ref.to_mapping()],
                "required_fact_refs": [ref.to_mapping() for ref in evidence.required_fact_refs],
                "evidence_requirements": [{"source_material_requirement_id": material.requirement_id,
                    "satisfaction": "one_of", "alternative_sets": [{"alternative_id": "direct",
                        "event_refs": [candidate.anchor_event.event_card_ref.to_mapping()],
                        "candidate_refs": [candidate_ref.to_mapping()]}]}],
                "candidate_preferences": [candidate_ref.to_mapping()],
                "span_policy": {"preferred": "tight", "allowed": ["tight"], "fallback_order": ["tight"]},
                "duration_seconds": {"min": material.minimum_usable_seconds,
                    "target": material.minimum_usable_seconds, "max": proposal.target_duration_seconds.maximum},
            })
        bounds = proposal.target_duration_seconds
        stories.append({"story_id": selection.story_id, "proposal_ref": selection.proposal_ref.to_mapping(),
            "beats": beats, "ordering_constraints": [],
            "story_duration_seconds": {"min": bounds.minimum, "target": bounds.minimum, "max": bounds.maximum},
            "editing_intent": {"pacing": "balanced", "continuity_priority": "high"},
            "teaser_intent": {"strategy": proposal.teaser_strategy, "duration_seconds": {"min": 1, "max": 1}}})
    return inputs, request, prepared, {"schema_version": "stage3-editorial-blueprint-draft-v1",
                                     "input_binding_sha256": prepared.input_binding_sha256, "stories": stories}


def _compile(case, wire=None):
    inputs, request, prepared, original = case
    draft = decode_editorial_draft(canonical_json_bytes(original if wire is None else wire),
        expected_input_binding_sha256=prepared.input_binding_sha256,
        expected_target_story_ids=prepared.contexts.target_story_ids, policy=request.draft_policy)
    projection = project_editorial_blueprints(inputs.narrative.values, inputs.portfolio.values, draft,
        expected_input_binding_sha256=prepared.input_binding_sha256, strategy_version=request.blueprint_strategy_version)
    business = compose_editorial_business_members(prepared.contexts, projection)
    result = evaluate_editorial_feasibility(inputs.narrative.values, inputs.portfolio.values, projection,
        semantic=inputs.semantic, job_policy=request.stage2_request.job_policy, policy=request.feasibility_policy)
    return business, result


def test_full_frozen_request_flows_to_six_pending_members_and_independent_witness(case, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("pure Stage3 must not execute predecessors")
    monkeypatch.setattr(BuildNarrativeGraphCommand, "execute", forbidden)
    monkeypatch.setattr(CompileStoryPortfolioCommand, "execute", forbidden)
    inputs, request, prepared, _ = case
    business, result = _compile(case)
    assert len(business.members) == 6 and result.status == "feasible"
    assert tuple(story.story_id for story in business.projection.blueprints) == prepared.contexts.target_story_ids
    assert decode_editorial_business_members(business.members, contexts=prepared.contexts) == business
    verify_editorial_feasibility(inputs.narrative.values, inputs.portfolio.values, business.projection, result,
        semantic=inputs.semantic, job_policy=request.stage2_request.job_policy, policy=request.feasibility_policy)
    assert all(member.artifact_type != "semantic_feasibility_admission" for member in business.members)
    assert _compile(case) == (business, result)
    replay = prepare_stage3_request(replace(request, stage2_outcome=replace(request.stage2_outcome, is_fresh_claim=True)), inputs)
    assert replay.request_payload == prepared.request_payload
    assert replay.contexts == prepared.contexts


@pytest.mark.parametrize("change", ["missing_story", "reordered_story", "missing_beat", "physical_endpoint", "foreign_binding"])
def test_raw_provider_failures_never_become_partial_business_output(case, change):
    wire = deepcopy(case[3])
    if change == "missing_story":
        wire["stories"].pop()
    elif change == "reordered_story":
        wire["stories"].reverse()
    elif change == "missing_beat":
        wire["stories"][0]["beats"] = []
    elif change == "physical_endpoint":
        wire["stories"][0]["beats"][0]["video_in"] = 12
    else:
        wire["input_binding_sha256"] = "sha256:" + "e" * 64
    with pytest.raises(ValueError):
        _compile(case, wire)
