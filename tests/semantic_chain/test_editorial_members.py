"""Actual semantic compilers over scripted, in-memory predecessors; no real I/O."""

import json
from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.pipeline.compile_story_portfolio_command import CompileStoryPortfolioCommand
from autocut_kernel.pipeline.editorial_blueprint_inputs import (
    read_committed_editorial_blueprint_inputs,
)
from autocut_kernel.semantic_chain.editorial_blueprint import project_editorial_blueprints
from autocut_kernel.semantic_chain.editorial_context import build_editorial_contexts
from autocut_kernel.semantic_chain.editorial_context_models import EditorialContextPolicy
from autocut_kernel.semantic_chain.editorial_draft import EditorialBlueprintDraft
from autocut_kernel.semantic_chain.editorial_members import (
    compose_editorial_business_members,
    decode_editorial_business_members,
)
from autocut_kernel.semantic_chain.editorial_models import (
    DurationRange,
    EditingIntent,
    EditorialBeatDraft,
    EvidenceAlternative,
    EvidenceRequirementDraft,
    SpanPolicy,
    StoryBlueprintDraft,
    TeaserIntent,
)
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.story_design_models import IntegerRange
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm.models import VlmNarrativeFunction

CONTEXT_POLICY = EditorialContextPolicy("unpartitioned-batch-v1", "bytes", 4_000_000, 8_000_000, 128)


def editorial_case():
    from tests.semantic_chain.test_compile_story_portfolio_command import command_case

    store, provider, request, _ = command_case(job_change={"selected_story_count": 2, "source_reuse_policy": "allow"})
    outcome = CompileStoryPortfolioCommand(store, provider).execute(request).outcome
    inputs = read_committed_editorial_blueprint_inputs(store, stage2_request=request, stage2_outcome=outcome)
    stage1, stage2 = inputs.narrative.values, inputs.portfolio.values
    contexts = build_editorial_contexts(
        inputs.semantic, stage1, stage2, policy=CONTEXT_POLICY, scope=request.artifact_scope,
        revision=1, job_policy=request.job_policy, story_policy=request.story_policy,
        candidate_policy=request.candidate_policy,
    )
    catalog = stage2.business.candidate_catalog
    catalog_ref = SemanticMemberIdentity.from_artifact_member(stage2.members[0])
    stories = []
    for selected in stage2.business.portfolio.selections:
        support = stage2.business.proposal_set.proposals[selected.proposal_index]
        proposal = support.proposal
        beats = []
        for material, supported in zip(proposal.material_requirements, support.requirements, strict=True):
            candidate = next(value for value in catalog.candidates
                             if value.candidate_id == supported.alternatives[0].candidate_ref.object_id)
            ref = SemanticObjectRef(catalog_ref, "candidate", candidate.candidate_id)
            beats.append(EditorialBeatDraft(
                "reveal", VlmNarrativeFunction(candidate.narrative_functions[0]), "保持必选事实的完整叙事职责",
                (material.obligation_ref,), supported.required_fact_refs,
                (EvidenceRequirementDraft(material.requirement_id, "one_of", (
                    EvidenceAlternative("direct", (candidate.anchor_event.event_card_ref,), (ref,)),
                )),), (ref,), SpanPolicy("tight", ("tight",), ("tight",)),
                DurationRange(material.minimum_usable_seconds, material.minimum_usable_seconds,
                              max(material.minimum_usable_seconds, proposal.target_duration_seconds.maximum)),
            ))
        bounds = proposal.target_duration_seconds
        stories.append(StoryBlueprintDraft(
            selected.story_id, selected.proposal_ref, tuple(beats), (),
            DurationRange(bounds.minimum, bounds.minimum, bounds.maximum), EditingIntent("balanced", "high"),
            TeaserIntent(proposal.teaser_strategy, IntegerRange(1, 1)),
        ))
    draft = EditorialBlueprintDraft(contexts.input_binding_sha256, tuple(stories))
    projection = project_editorial_blueprints(stage1, stage2, draft,
                                              expected_input_binding_sha256=contexts.input_binding_sha256,
                                              strategy_version=contexts.policy.strategy)
    return inputs, contexts, draft, projection


@pytest.fixture(scope="module")
def case():
    return editorial_case()


def _rewrite(member, mapping):
    payload = canonical_json_bytes(mapping).decode()
    return replace(member, payload_json=payload, content_hash=canonical_payload_hash(payload))


def test_complete_real_predecessor_projection_composes_six_business_members(case):
    _, contexts, _, projection = case
    first = compose_editorial_business_members(contexts, projection)
    assert len(first.members) == 6
    assert tuple(value.artifact_type for value in first.members) == (
        "editorial_blueprint", "evidence_closure_set", "context_manifest",
    ) * 2
    assert decode_editorial_business_members(first.members, contexts=contexts) == first
    assert compose_editorial_business_members(contexts, projection) == first
    for index, story in enumerate(contexts.stories):
        payload = json.loads(first.members[index * 3].payload_json)
        assert payload["context_manifest_ref"] == SemanticMemberIdentity.from_artifact_member(story.context_member).to_mapping()
        assert payload["blueprint"] == projection.blueprints[index].to_mapping()
        assert "admission" not in payload and "artifact_id" not in payload["context_manifest_ref"]


@pytest.mark.parametrize("change", ["missing", "reordered", "duplicated", "extra", "list"])
def test_partial_reordered_or_duplicate_business_batch_is_not_accepted(case, change):
    _, contexts, _, projection = case
    members = compose_editorial_business_members(contexts, projection).members
    variants = {"missing": members[:3], "reordered": members[3:] + members[:3],
                "duplicated": members[:3] * 2, "extra": members + members[:1], "list": list(members)}
    with pytest.raises(ValueError):
        decode_editorial_business_members(variants[change], contexts=contexts)


@pytest.mark.parametrize("change", ["input", "manifest", "story", "unknown", "version"])
def test_rehashed_wrapper_tampering_cannot_change_context_binding(case, change):
    _, contexts, _, projection = case
    members = list(compose_editorial_business_members(contexts, projection).members)
    payload = json.loads(members[0].payload_json)
    if change == "input":
        payload["input_binding_sha256"] = "sha256:" + "f" * 64
    elif change == "manifest":
        payload["context_manifest_ref"] = SemanticMemberIdentity.from_artifact_member(contexts.stories[1].context_member).to_mapping()
    elif change == "story":
        payload["blueprint"] = projection.blueprints[1].to_mapping()
    elif change == "unknown":
        payload["pass"] = True
    else:
        payload["schema_version"] = "unregistered"
    members[0] = _rewrite(members[0], payload)
    with pytest.raises(ValueError):
        decode_editorial_business_members(tuple(members), contexts=contexts)


@pytest.mark.parametrize("position", range(6))
@pytest.mark.parametrize("field,value", [("revision", 2), ("content_hash", "sha256:" + "f" * 64)])
def test_exact_member_revision_and_hash_are_checked(case, position, field, value):
    _, contexts, _, projection = case
    members = list(compose_editorial_business_members(contexts, projection).members)
    members[position] = replace(members[position], **{field: value})
    with pytest.raises(ValueError):
        decode_editorial_business_members(tuple(members), contexts=contexts)


def test_input_binding_and_target_order_are_not_caller_replaceable(case):
    _, contexts, _, projection = case
    for changed in (replace(projection, input_binding_sha256="sha256:" + "e" * 64),
                    replace(projection, blueprints=tuple(reversed(projection.blueprints)))):
        with pytest.raises(ValueError):
            compose_editorial_business_members(contexts, changed)
    with pytest.raises(ValueError):
        compose_editorial_business_members(contexts.to_mapping(), projection)


def test_fully_rehashed_foreign_material_cannot_replace_the_frozen_closure(case):
    _, contexts, _, projection = case
    members = list(compose_editorial_business_members(contexts, projection).members)
    payload = json.loads(members[0].payload_json)
    blueprint = payload["blueprint"]
    requirement = blueprint["beats"][0]["evidence_requirements"][0]
    requirement["material_requirement_id"] = "foreign-material"
    requirement["evidence_requirement_id"] = canonical_json_hash({
        "schema_version": "stage3-evidence-requirement-id-v1", "story_id": blueprint["story_id"],
        "strategy_version": blueprint["strategy_version"],
        "beat_ordinal": 0, "requirement_ordinal": 0, "material_requirement_id": "foreign-material",
    })
    members[0] = _rewrite(members[0], payload)
    with pytest.raises(ValueError, match="material closure"):
        decode_editorial_business_members(tuple(members), contexts=contexts)


@pytest.mark.parametrize("kind", ["fact", "obligation"])
def test_internally_consistent_rehashed_facts_still_must_match_context(case, kind):
    _, contexts, _, projection = case
    members = list(compose_editorial_business_members(contexts, projection).members)
    payload = json.loads(members[0].payload_json)
    beat = payload["blueprint"]["beats"][0]
    material = beat["evidence_requirements"][0]
    field = "required_fact_refs" if kind == "fact" else "required_obligation_refs"
    beat[field][0]["object_id"] = f"foreign-{kind}"
    ref = material["required_fact_refs"][0] if kind == "fact" else material["obligation_ref"]
    ref["object_id"] = f"foreign-{kind}"
    members[0] = _rewrite(members[0], payload)
    with pytest.raises(ValueError, match="exact closure"):
        decode_editorial_business_members(tuple(members), contexts=contexts)
