"""Whole-proposal checks over synthetic decoded Stage1 values, no live provider or DB."""

import json
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
    diagnostic = error.value.to_diagnostic()
    assert diagnostic["error_code"] == "REQUIRED_FACT_CLOSURE_MISMATCH"
    assert diagnostic["json_path"] == "$.proposals[0].required_fact_refs"
    assert diagnostic["missing_count"] == (0 if extra else len(proposal.required_fact_refs))
    assert diagnostic["unexpected_count"] == (1 if extra else 0)


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


def test_person_entity_mislabeled_character_reports_actual_type_without_rewriting(case):
    draft, kwargs = case
    person_ids = {
        node.node_id for node in kwargs["graph"].nodes
        if node.node_type == "entity" and node.attributes.entity_kind == "person"
    }
    person = next(ref for ref in kwargs["graph_object_refs"] if ref.object_id in person_ids)
    wrong_character = replace(person, object_type="character")
    changed = replace(draft, proposals=(
        replace(draft.proposals[0], key_character_refs=(wrong_character,)),
    ))
    before = changed.to_mapping()
    with pytest.raises(StoryProposalValidationError) as caught:
        validate_story_proposals(changed, **kwargs)
    assert caught.value.to_diagnostic() == {
        "rule_id": "SD-REF-001", "proposal_index": 0,
        "json_path": "$.proposals[0].key_character_refs[0]",
        "error_code": "GRAPH_REFERENCE_TYPE_MISMATCH",
        "expected_object_type": "character", "actual_object_type": "entity",
        "missing_count": None, "unexpected_count": None,
    }
    assert changed.to_mapping() == before
    assert wrong_character.object_type == "character"
    assert person.object_type == "entity"


@pytest.mark.parametrize("same_id", [False, True])
def test_foreign_graph_owner_is_not_reported_as_local_missing_or_type_mismatch(case, same_id):
    draft, kwargs = case
    person = next(ref for ref in kwargs["graph_object_refs"] if ref.object_type == "entity")
    foreign = replace(
        person, object_type="character", object_id=person.object_id if same_id else "absent",
        member_ref=replace(person.member_ref, logical_id="foreign-graph"),
    )
    # Keep this synthetic DTO structurally valid with one owner; missing material
    # is deliberately secondary to the existing Graph reference rule.
    proposal = replace(
        draft.proposals[0], thread_refs=(), required_obligation_refs=(), required_fact_refs=(),
        key_character_refs=(foreign,), material_requirements=(),
    )
    with pytest.raises(StoryProposalValidationError) as caught:
        validate_story_proposals(replace(draft, proposals=(proposal,)), **kwargs)
    diagnostic = caught.value.to_diagnostic()
    assert diagnostic["rule_id"] == "SD-REF-001"
    assert diagnostic["error_code"] == "GRAPH_REFERENCE_FOREIGN_OWNER"
    assert diagnostic["json_path"] == "$.proposals[0].key_character_refs[0]"
    assert diagnostic["expected_object_type"] == "character"
    assert diagnostic["actual_object_type"] is None


def test_graph_failure_path_preserves_original_field_and_reference_order(case):
    draft, kwargs = case
    original = draft.proposals[0]
    missing_thread = replace(original.thread_refs[0], object_id="absent-thread")
    missing_fact = replace(original.required_fact_refs[0], object_id="absent-fact")
    proposal = replace(
        original, proposal_id="p1", thread_refs=(*original.thread_refs, missing_thread),
        required_fact_refs=(missing_fact,), genre_tags=("outside-policy",),
    )
    changed = replace(draft, proposals=(original, proposal))
    with pytest.raises(StoryProposalValidationError) as caught:
        validate_story_proposals(changed, **kwargs)
    diagnostic = caught.value.to_diagnostic()
    assert diagnostic["rule_id"] == "SD-REF-001"
    assert diagnostic["error_code"] == "GRAPH_REFERENCE_NOT_FOUND"
    assert diagnostic["json_path"] == f"$.proposals[1].thread_refs[{len(original.thread_refs)}]"
    assert diagnostic["expected_object_type"] == "story_thread"
    assert diagnostic["actual_object_type"] is None


def test_fact_closure_reports_both_missing_and_unexpected_counts(case):
    draft, kwargs = case
    original = draft.proposals[0]
    unexpected = next(
        ref for ref in kwargs["graph_object_refs"]
        if ref.object_type == "fact" and ref not in original.required_fact_refs
    )
    changed = replace(draft, proposals=(replace(original, required_fact_refs=(unexpected,)),))
    with pytest.raises(StoryProposalValidationError) as caught:
        validate_story_proposals(changed, **kwargs)
    diagnostic = caught.value.to_diagnostic()
    assert diagnostic["rule_id"] == "SD-MAT-001"
    assert diagnostic["error_code"] == "REQUIRED_FACT_CLOSURE_MISMATCH"
    assert diagnostic["missing_count"] == len(original.required_fact_refs)
    assert diagnostic["unexpected_count"] == 1
    assert unexpected.object_id not in json.dumps(diagnostic)


@pytest.mark.parametrize("foreign_owner", [False, True])
def test_material_source_failure_reports_exact_requirement_path(case, foreign_owner):
    draft, kwargs = case
    original = draft.proposals[0]
    source = kwargs["source_refs"][0]
    invalid_source = (
        replace(source, member_ref=replace(source.member_ref, logical_id="foreign-source"))
        if foreign_owner else replace(source, object_id="absent-source")
    )
    requirement = replace(
        original.material_requirements[0],
        source_constraints=SourceConstraints((invalid_source,), (), "render_source"),
    )
    with pytest.raises(StoryProposalValidationError) as caught:
        validate_story_proposals(replace(draft, proposals=(
            replace(original, material_requirements=(requirement,)),
        )), **kwargs)
    diagnostic = caught.value.to_diagnostic()
    assert diagnostic["json_path"] == (
        "$.proposals[0].material_requirements[0].source_constraints.allowed_source_refs[0]"
    )
    assert diagnostic["error_code"] == (
        "SOURCE_REFERENCE_FOREIGN_OWNER" if foreign_owner else "SOURCE_REFERENCE_NOT_FOUND"
    )


def test_legacy_error_constructor_keeps_detail_but_diagnostic_excludes_it():
    error = StoryProposalValidationError("SD-REF-001", "private-prompt-and-signed-url", 2)
    assert str(error) == "private-prompt-and-signed-url"
    assert error.args == ("private-prompt-and-signed-url",)
    assert error.rule_id == "SD-REF-001"
    assert error.proposal_index == 2
    assert error.to_diagnostic()["proposal_index"] == 2
    assert "private-prompt-and-signed-url" not in json.dumps(error.to_diagnostic())


def test_diagnostic_boundary_rejects_untrusted_strings_and_non_integer_counts():
    private = "private-prompt-and-signed-url"
    error = StoryProposalValidationError(
        private, private, True, json_path=f"$.proposals[0].{private}", error_code=private,
        expected_object_type=private, actual_object_type=private,
        missing_count=-1, unexpected_count=True,
    )
    assert error.to_diagnostic() == {
        "rule_id": None, "proposal_index": None, "json_path": "$",
        "error_code": "STORY_PROPOSAL_VALIDATION_FAILED",
        "expected_object_type": None, "actual_object_type": None,
        "missing_count": None, "unexpected_count": None,
    }
