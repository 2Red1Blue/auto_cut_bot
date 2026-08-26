"""Whole-proposal checks over real decoded Stage1 values, no provider or DB."""

from dataclasses import replace

import pytest
from autocut_kernel.pipeline.build_narrative_graph_command import BuildNarrativeGraphCommand
from autocut_kernel.semantic_chain.member_refs import SemanticObjectRef
from autocut_kernel.semantic_chain.story_design_draft import ProposalDraftSet
from autocut_kernel.semantic_chain.story_design_models import (
    EditingProfileReference,
    IntegerRange,
    JobPolicy,
    MaterialRequirement,
    PhysicalRequirement,
    ProposalDraft,
    SourceConstraints,
    StoryDesignPolicy,
)
from autocut_kernel.semantic_chain.story_design_validation import (
    StoryProposalValidationError,
    validate_story_proposals,
)

from tests.semantic_chain.test_story_design_inputs import render_case


@pytest.fixture
def case():
    request, store, provider = render_case()
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    values = result.committed.values
    graph = values.coverage.narrative_graph
    owner = values.coverage.identity("narrative_graph")
    refs = tuple(SemanticObjectRef(owner, node.node_type, node.node_id) for node in graph.nodes)
    source = SemanticObjectRef(values.dependency_proof.source_member_ref, "source", store.inputs.source_grant.sources[0].source_id)
    constraints = SourceConstraints((source,), (), "render_source")
    physical = (PhysicalRequirement("dialogue_integrity", "complete"),)
    profile = EditingProfileReference("test-explicit", "v1")
    policy = StoryDesignPolicy("test", "v1", ("drama",), (profile,), ("none",), physical, "first_feasible_lexicographic_v1")
    job = JobPolicy("test", "v1", policy.canonical_hash, IntegerRange(1, 3), 1, 1000, IntegerRange(10, 40), "forbid", constraints, "all_or_nothing")
    obligation = next(ref for ref in refs if ref.object_type == "obligation")
    required_fact_ids = next(node.attributes.required_fact_ids for node in graph.nodes if node.node_id == obligation.object_id)
    proposal = ProposalDraft("p0", "title", "claim", tuple(ref for ref in refs if ref.object_type == "story_thread"),
                             (obligation,), tuple(ref for ref in refs if ref.object_id in required_fact_ids), (), ("drama",), profile,
                             IntegerRange(15, 30), "none", "hook", (MaterialRequirement("r", obligation, 1, physical, constraints),))
    draft = ProposalDraftSet("sha256:" + "a" * 64, (proposal,))
    return draft, dict(graph=graph, graph_object_refs=refs, source_refs=(source,), job_policy=job, story_policy=policy)


def test_validates_without_rewriting_draft(case):
    draft, kwargs = case
    before = draft.to_mapping()
    assert validate_story_proposals(draft, **kwargs) is None
    assert draft.to_mapping() == before


def test_graph_hash_is_computed_once_not_once_per_reference(case, monkeypatch):
    draft, kwargs = case
    graph = kwargs["graph"]
    expected = graph.canonical_hash
    calls = []

    def digest(value):
        calls.append(value)
        return expected

    monkeypatch.setattr(type(graph), "canonical_hash", property(digest))
    validate_story_proposals(draft, **kwargs)
    assert calls == [graph]


@pytest.mark.parametrize("extra", [False, True])
def test_required_facts_cannot_be_missing_or_outside_material_obligations(case, extra):
    draft, kwargs = case
    proposal = draft.proposals[0]
    facts = ()
    if extra:
        unrequired = next(ref for ref in kwargs["graph_object_refs"] if ref.object_type == "fact" and ref not in proposal.required_fact_refs)
        facts = (*proposal.required_fact_refs, unrequired)
    with pytest.raises(StoryProposalValidationError) as error:
        validate_story_proposals(replace(draft, proposals=(replace(proposal, required_fact_refs=facts),)), **kwargs)
    assert error.value.rule_id == "SD-MAT-001"


@pytest.mark.parametrize(("mutation", "rule"), [
    ("genre", "SD-ENUM-001"), ("profile", "SD-ENUM-001"), ("teaser", "SD-ENUM-001"),
    ("duration", "SD-DUR-001"), ("fact", "SD-REF-001"), ("source", "SD-REF-001"),
    ("material", "SD-MAT-001"), ("physical", "SD-PHYS-DEFER-001"),
])
def test_one_bad_proposal_rejects_whole_draft_without_filtering(case, mutation, rule):
    draft, kwargs = case
    original = draft.proposals[0]
    changed = replace(original, proposal_id="p1")
    if mutation == "genre":
        changed = replace(changed, genre_tags=("undeclared",))
    elif mutation == "profile":
        changed = replace(changed, editing_profile=EditingProfileReference("test-explicit", "v2"))
    elif mutation == "teaser":
        changed = replace(changed, teaser_strategy="undeclared")
    elif mutation == "duration":
        changed = replace(changed, target_duration_seconds=IntegerRange(15, 41))
    elif mutation == "fact":
        changed = replace(changed, required_fact_refs=(replace(original.required_fact_refs[0], object_id="unknown"),))
    elif mutation == "source":
        changed = replace(changed, material_requirements=(replace(original.material_requirements[0], source_constraints=SourceConstraints((replace(kwargs["source_refs"][0], object_id="unknown"),), (), "render_source")),))
    elif mutation == "material":
        changed = replace(changed, material_requirements=())
    else:
        changed = replace(changed, material_requirements=(replace(original.material_requirements[0], physical_requirements=()),))
    both = replace(draft, proposals=(original, changed))
    with pytest.raises(StoryProposalValidationError) as error:
        validate_story_proposals(both, **kwargs)
    assert error.value.rule_id == rule and error.value.proposal_index == 1
    assert len(both.proposals) == 2


@pytest.mark.parametrize("target", ["graph_missing", "graph_hash", "source_scope", "policy_hash", "count"])
def test_input_and_policy_binding(case, target):
    draft, kwargs = case
    if target == "graph_missing":
        kwargs["graph_object_refs"] = kwargs["graph_object_refs"][:-1]
    elif target == "graph_hash":
        kwargs["graph_object_refs"] = tuple(replace(ref, member_ref=replace(ref.member_ref, content_hash="sha256:" + "f" * 64)) for ref in kwargs["graph_object_refs"])
    elif target == "source_scope":
        source = kwargs["source_refs"][0]
        kwargs["source_refs"] = (replace(source, member_ref=replace(source.member_ref, scope=replace(source.member_ref.scope, key="foreign"))),)
    elif target == "policy_hash":
        kwargs["story_policy"] = replace(kwargs["story_policy"], policy_version="v2")
    else:
        kwargs["job_policy"] = replace(kwargs["job_policy"], selected_story_count=2)
    with pytest.raises(StoryProposalValidationError):
        validate_story_proposals(draft, **kwargs)
