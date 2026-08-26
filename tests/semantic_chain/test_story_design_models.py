"""Synthetic proposal/policy values, not committed inputs or accepted Stories."""

import hashlib
import json
from dataclasses import FrozenInstanceError, fields, replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_hash
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.story_design_models import (
    PHYSICAL_REQUIREMENT_MODES,
    EditingProfileReference,
    IntegerRange,
    JobPolicy,
    MaterialRequirement,
    PhysicalRequirement,
    ProposalDraft,
    SourceConstraints,
    StoryDesignModelError,
    StoryDesignPolicy,
)
from autocut_kernel.store import ArtifactScope

SCOPE = ArtifactScope("pipeline", "job", "synthetic-story-values")
GRAPH = SemanticMemberIdentity("narrative_graph", "graph", 1, SCOPE, "sha256:" + "a" * 64)
SOURCE = SemanticMemberIdentity(
    "whole_series_source_manifest", "sources", 1, SCOPE, "sha256:" + "b" * 64
)
SOURCE_REF = SemanticObjectRef(SOURCE, "source", "source-一")
OBLIGATION = SemanticObjectRef(GRAPH, "obligation", "obligation-一")
PHYSICAL = tuple(PhysicalRequirement(*pair) for pair in PHYSICAL_REQUIREMENT_MODES)
EDITING = EditingProfileReference("synthetic-dramatic", "1.0.0")
CONSTRAINTS = SourceConstraints((SOURCE_REF,), (), "render_source")


def _proposal(proposal_id="proposal-一"):
    return ProposalDraft(
        proposal_id, "发现证据", "主角找到关键证据",
        (SemanticObjectRef(GRAPH, "story_thread", "thread-一"),), (OBLIGATION,),
        (SemanticObjectRef(GRAPH, "fact", "fact-一"),), (),
        ("suspense",), EDITING, IntegerRange(30, 60), "cold_open", "谁留下了证据？",
        (MaterialRequirement("requirement-一", OBLIGATION, 12, PHYSICAL, CONSTRAINTS),),
    )


def _story_policy():
    return StoryDesignPolicy(
        "synthetic-story-policy", "1.0.0", ("suspense",), (EDITING,),
        ("cold_open",), PHYSICAL, "first_feasible_lexicographic_v1",
    )


def _job_policy():
    return JobPolicy(
        "synthetic-job-policy", "1.0.0", _story_policy().canonical_hash,
        IntegerRange(2, 8), 1, 1000, IntegerRange(30, 60), "forbid", CONSTRAINTS,
        "all_or_nothing",
    )


def _values():
    return (IntegerRange(1, 9), EDITING, PHYSICAL[0], CONSTRAINTS,
            _proposal().material_requirements[0], _story_policy(), _job_policy(), _proposal())


@pytest.mark.parametrize("value", _values())
def test_closed_roundtrip_fresh_mapping_and_frozen_values(value):
    mapping = value.to_mapping()
    assert type(value).from_mapping(mapping) == value
    assert type(value).from_mapping(json.loads(json.dumps(mapping))) == value
    mapping["unknown"] = "not retained"
    assert "unknown" not in value.to_mapping()
    with pytest.raises(FrozenInstanceError):
        setattr(value, fields(value)[0].name, "mutation")
    hash(value)


@pytest.mark.parametrize("value", _values())
def test_each_mapping_field_is_required_and_extra_fields_rejected(value):
    mapping = value.to_mapping()
    for name in mapping:
        changed = dict(mapping)
        del changed[name]
        with pytest.raises(ValueError):
            type(value).from_mapping(changed)
    with pytest.raises(ValueError):
        type(value).from_mapping({**mapping, "ready": True})
    with pytest.raises(ValueError):
        type(value).from_mapping(tuple(mapping.items()))


@pytest.mark.parametrize("bad", [True, False, 1.0, "1", 0, -1, 2**53, None])
def test_positive_safe_integer_fields_reject_nonintegers_and_bounds(bad):
    with pytest.raises(ValueError):
        IntegerRange(bad, 100)
    with pytest.raises(ValueError):
        IntegerRange(1, bad)
    with pytest.raises(ValueError):
        replace(_job_policy(), selected_story_count=bad)
    with pytest.raises(ValueError):
        replace(_job_policy(), max_search_states=bad)
    with pytest.raises(ValueError):
        replace(_proposal().material_requirements[0], minimum_usable_seconds=bad)


def test_ranges_and_selected_count_are_distinct_not_shared_defaults():
    policy = _job_policy()
    assert policy.proposal_count == IntegerRange(2, 8)
    assert policy.selected_story_count == 1
    assert policy.target_duration_seconds == IntegerRange(30, 60)
    with pytest.raises(ValueError):
        IntegerRange(10, 9)
    with pytest.raises(ValueError):
        replace(policy, selected_story_count=9)
    assert replace(policy, selected_story_count=8).selected_story_count == 8


@pytest.mark.parametrize("bad", ["", "  ", "\ud800", None, 123, True])
def test_all_string_fields_validate_actual_nonempty_utf8(bad):
    for value in _values():
        for field in fields(value):
            if type(getattr(value, field.name)) is str:
                with pytest.raises(ValueError):
                    replace(value, **{field.name: bad})


def test_every_tuple_field_rejects_mutable_list_and_duplicate_values():
    for value in _values():
        for field in fields(value):
            items = getattr(value, field.name)
            if type(items) is tuple:
                with pytest.raises(ValueError):
                    replace(value, **{field.name: list(items)})
                if items:
                    with pytest.raises(ValueError):
                        replace(value, **{field.name: items + items})


@pytest.mark.parametrize("changes", [
    {"allowed_genre_tags": ()}, {"editing_profiles": ()}, {"teaser_strategies": ()},
    {"selection_strategy": "best_score"}, {"editing_profiles": ("dramatic",)},
    {"required_physical_requirements": tuple(reversed(PHYSICAL))},
])
def test_story_policy_allowlists_and_strategy_are_explicit(changes):
    with pytest.raises(ValueError):
        replace(_story_policy(), **changes)


def test_empty_physical_policy_is_explicit_not_an_implicit_default():
    policy = replace(_story_policy(), required_physical_requirements=())
    assert policy.to_mapping()["required_physical_requirements"] == []
    assert policy.canonical_hash != _story_policy().canonical_hash
    requirement = replace(_proposal().material_requirements[0], physical_requirements=())
    assert requirement.physical_requirements_hash == canonical_json_hash([])


@pytest.mark.parametrize("changes", [
    {"source_reuse_policy": "auto"}, {"completion_policy": "partial"},
    {"story_design_policy_sha256": "sha256:" + "0" * 64},
    {"story_design_policy_sha256": "sha256:" + "A" * 64},
    {"source_constraints": {}}, {"proposal_count": {"min": 1, "max": 2}},
    {"target_duration_seconds": 30},
])
def test_job_policy_has_no_legacy_union_or_fallback(changes):
    with pytest.raises(ValueError):
        replace(_job_policy(), **changes)


def test_three_physical_pairs_and_independent_array_only_hash_oracle():
    expected = [
        {"requirement_kind": "dialogue_integrity", "mode": "complete"},
        {"requirement_kind": "subtitle_clearance", "mode": "protect_detected_cues"},
        {"requirement_kind": "visual_validity", "mode": "endpoint_and_stable_region"},
    ]
    requirement = _proposal().material_requirements[0]
    raw = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
    assert requirement.physical_requirements_hash == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert replace(requirement, requirement_id="other").physical_requirements_hash == requirement.physical_requirements_hash
    assert replace(requirement, minimum_usable_seconds=13).physical_requirements_hash == requirement.physical_requirements_hash
    assert [item.to_mapping() for item in PHYSICAL] == expected
    for kind, _mode in PHYSICAL_REQUIREMENT_MODES:
        with pytest.raises(ValueError):
            PhysicalRequirement(kind, "fulfilled")
    with pytest.raises(ValueError):
        PhysicalRequirement("unknown", "complete")
    with pytest.raises(ValueError):
        replace(requirement, physical_requirements=tuple(reversed(PHYSICAL)))


def test_source_constraints_are_exact_owner_bound_disjoint_and_not_a_grant():
    assert SourceConstraints((), (), "render_source").allowed_source_refs == ()
    bad_sources = (
        replace(SOURCE_REF, object_type="source_window"),
        replace(SOURCE_REF, member_ref=GRAPH),
    )
    for ref in bad_sources:
        with pytest.raises(ValueError):
            replace(CONSTRAINTS, allowed_source_refs=(ref,))
    for purpose in ("render", "semantic_analysis", "accepted"):
        with pytest.raises(ValueError):
            replace(CONSTRAINTS, authorization_purpose=purpose)
    with pytest.raises(ValueError):
        replace(CONSTRAINTS, forbidden_source_refs=(SOURCE_REF,))
    foreign = replace(SOURCE_REF, member_ref=replace(SOURCE, revision=2), object_id="other")
    with pytest.raises(ValueError):
        replace(CONSTRAINTS, forbidden_source_refs=(foreign,))


@pytest.mark.parametrize("field,kind", [
    ("thread_refs", "story_thread"), ("required_obligation_refs", "obligation"),
    ("required_fact_refs", "fact"), ("key_character_refs", "character"),
])
def test_narrative_refs_require_exact_graph_owner_and_correct_object_kind(field, kind):
    good = SemanticObjectRef(GRAPH, kind, "additional")
    proposal = replace(_proposal(), **{field: getattr(_proposal(), field) + (good,)})
    assert good in proposal.narrative_refs
    for ref in (replace(good, object_type="event"), replace(good, member_ref=SOURCE),
                replace(good, member_ref=replace(GRAPH, revision=2))):
        with pytest.raises(ValueError):
            replace(_proposal(), **{field: (ref,)})


def test_material_obligation_membership_duplicate_ids_and_source_scope():
    proposal = _proposal()
    requirement = proposal.material_requirements[0]
    with pytest.raises(ValueError):
        replace(proposal, required_obligation_refs=())
    with pytest.raises(ValueError):
        replace(proposal, material_requirements=(requirement, replace(requirement, minimum_usable_seconds=13)))
    foreign_source = replace(SOURCE_REF, member_ref=replace(
        SOURCE, scope=ArtifactScope("pipeline", "job", "foreign"),
    ))
    with pytest.raises(ValueError):
        replace(requirement, source_constraints=SourceConstraints((foreign_source,), (), "render_source"))
    with pytest.raises(ValueError):
        replace(requirement, obligation_ref=replace(OBLIGATION, object_type="fact"))


def test_deep_mapping_mutation_does_not_change_original_values():
    proposal = _proposal()
    mapping = proposal.to_mapping()
    mapping["required_fact_refs"][0]["member_ref"]["scope"]["key"] = "rewritten"
    mapping["material_requirements"][0]["physical_requirements"][0]["mode"] = "pass"
    mapping["editing_profile"]["profile_version"] = "changed"
    assert ProposalDraft.from_mapping(proposal.to_mapping()) == proposal
    assert proposal.required_fact_refs[0].member_ref.scope == SCOPE


@pytest.mark.parametrize("field", ["story_id", "material_support", "tainted_by", "rule_results", "selected"])
def test_proposals_cannot_claim_story_identity_or_compiler_results(field):
    with pytest.raises(StoryDesignModelError):
        ProposalDraft.from_mapping({**_proposal().to_mapping(), field: "claimed"})


def test_policy_hash_binds_every_field_and_no_objective_weights():
    for value in (_story_policy(), _job_policy()):
        assert value.canonical_hash == canonical_json_hash(value.to_mapping())
        for key in value.to_mapping():
            mutated = value.to_mapping()
            mutated[key] = {"changed": mutated[key]}
            assert canonical_json_hash(mutated) != value.canonical_hash
    with pytest.raises(ValueError):
        StoryDesignPolicy.from_mapping({**_story_policy().to_mapping(), "objective_policy": {}})
