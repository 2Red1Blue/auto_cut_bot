"""Semantic binding over synthetic content; no claim of catalog admission."""

from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.pipeline.build_narrative_graph_command import BuildNarrativeGraphCommand
from autocut_kernel.semantic_chain.candidate_catalog import CandidateCatalog, CandidateCatalogPolicy
from autocut_kernel.semantic_chain.candidate_projection import CandidateCatalogProjection
from autocut_kernel.semantic_chain.story_design_context import story_design_input_binding
from autocut_kernel.store.models import ArtifactMember

from tests.semantic_chain.test_story_design_inputs import render_case
from tests.semantic_chain.test_story_design_models import _job_policy, _story_policy


def projection_for(catalog, scope, revision=1):
    member = ArtifactMember("candidate_catalog", "candidate_catalog", revision, scope, catalog.canonical_hash, canonical_json_bytes(catalog.to_mapping()).decode())
    return CandidateCatalogProjection(member, catalog)


@pytest.fixture
def inputs():
    request, store, provider = render_case()
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    stage1 = result.committed.values
    policy = CandidateCatalogPolicy("candidate-catalog-v1", "0.5", ())
    # Empty but closed pending content is intentional: binding does not claim
    # that any Story can be supported, nor replace the real candidate projector.
    catalog = CandidateCatalog(canonical_json_hash({"test": "empty-catalog"}), stage1.admission.input_binding_sha256,
                               store.inputs.source_grant.canonical_hash, stage1.coverage.identity("event_card_set"),
                               stage1.coverage.identity("narrative_graph"), stage1.coverage.identity("coverage_ledger"), policy.canonical_hash, ())
    return stage1, projection_for(catalog, request.artifact_scope), dict(job_policy=_job_policy(), story_policy=_story_policy(), candidate_policy=policy)


def test_stable_binding_includes_exact_candidate_revision_and_job_policy(inputs):
    stage1, projection, policies = inputs
    first = story_design_input_binding(stage1, projection, **policies)
    assert story_design_input_binding(stage1, projection, **policies) == first
    revised = projection_for(projection.catalog, projection.member.scope, revision=2)
    assert story_design_input_binding(stage1, revised, **policies) != first
    changed = {**policies, "job_policy": replace(policies["job_policy"], max_search_states=999)}
    assert story_design_input_binding(stage1, projection, **changed) != first


@pytest.mark.parametrize("target", ["member_payload", "catalog_value", "graph_owner", "candidate_input", "candidate_policy", "story_policy", "stage1_value"])
def test_mixed_value_and_member_or_policy_rejected(inputs, target):
    stage1, projection, policies = inputs
    if target == "member_payload":
        projection = replace(projection, member=replace(projection.member, payload_json='{"foreign":true}'))
    elif target == "catalog_value":
        projection = replace(projection, catalog=replace(projection.catalog, catalog_id=canonical_json_hash({"foreign": True})))
    elif target == "graph_owner":
        catalog = replace(projection.catalog, narrative_graph_member_ref=replace(projection.catalog.narrative_graph_member_ref, content_hash="sha256:" + "f" * 64))
        projection = projection_for(catalog, projection.member.scope)
    elif target == "candidate_input":
        catalog = replace(projection.catalog, input_binding_sha256=canonical_json_hash({"foreign": True}))
        projection = projection_for(catalog, projection.member.scope)
    elif target == "candidate_policy":
        policies["candidate_policy"] = replace(policies["candidate_policy"], minimum_confidence="0.8")
    elif target == "story_policy":
        policies["story_policy"] = replace(policies["story_policy"], policy_version="v2")
    else:
        graph = replace(stage1.coverage.narrative_graph, graph_id="foreign")
        stage1 = replace(stage1, coverage=replace(stage1.coverage, narrative_graph=graph))
    with pytest.raises(ValueError):
        story_design_input_binding(stage1, projection, **policies)
