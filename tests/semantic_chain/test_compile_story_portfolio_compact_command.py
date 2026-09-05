"""Real compiler/reader/Stage 3 flow with synthetic provider and Store I/O."""

import json
from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.pipeline import build_editorial_blueprint_command as stage3_module
from autocut_kernel.pipeline.build_editorial_blueprint_command import BuildEditorialBlueprintCommand
from autocut_kernel.pipeline.compile_story_portfolio_command import CompileStoryPortfolioCommand
from autocut_kernel.pipeline.compile_story_portfolio_request import prepare_stage2_request
from autocut_kernel.pipeline.story_design_inputs import read_committed_story_design_inputs
from autocut_kernel.semantic_chain.member_refs import SemanticObjectRef
from autocut_kernel.semantic_chain.story_design_boundary import STAGE2_COMPACT_PROMPT
from autocut_kernel.semantic_chain.story_design_compact import (
    COMPACT_PROMPT_VERSION,
    build_story_design_compact_context,
)
from autocut_kernel.semantic_chain.story_design_compact_migration import migrate_story_design_v1_to_compact

from tests.semantic_chain.test_build_editorial_blueprint_command import MemoryEditorialBlueprintStore, _raw_for
from tests.semantic_chain.test_build_editorial_blueprint_request import _policy
from tests.semantic_chain.test_build_narrative_graph_command import ScriptedDraftProvider
from tests.semantic_chain.test_compile_story_portfolio_command import command_case


def compact_command_case():
    store, _, request, raw = command_case(
        job_change={"selected_story_count": 2, "source_reuse_policy": "allow"},
    )
    request = replace(request, generation=replace(
        request.generation, prompt_version=COMPACT_PROMPT_VERSION,
        prompt_template=STAGE2_COMPACT_PROMPT,
    ))
    inputs = read_committed_story_design_inputs(
        store, stage1_request=request.stage1_request, stage1_outcome=request.stage1_outcome,
    )
    prepared = prepare_stage2_request(request, inputs)
    context = build_story_design_compact_context(
        inputs.semantic, inputs.narrative.values, prepared.projection,
        job_policy=request.job_policy, story_policy=request.story_policy,
        candidate_policy=request.candidate_policy,
    )
    migration = migrate_story_design_v1_to_compact(raw, context=context, policy=request.draft_policy)
    wire = json.loads(migration.wire_bytes)
    person = next(node for node in context.graph.nodes
                  if node.node_type == "entity" and node.attributes.entity_kind == "person")
    person_ref = SemanticObjectRef(context.graph_owner, "entity", person.node_id)
    wire["proposals"][0]["key_subject_refs"] = [context.alias_for(person_ref)]
    return store, request, canonical_json_bytes(wire), person_ref


def test_compact_person_survives_atomic_commit_replay_and_stage3():
    store, request, raw, person_ref = compact_command_case()
    provider = ScriptedDraftProvider(raw)
    command = CompileStoryPortfolioCommand(store, provider)
    result = command.execute(request)
    assert result.outcome.state == "succeeded", result.outcome.failure_detail_json
    proposal = result.committed.values.business.proposal_set.proposals[0].proposal
    assert proposal.subject_refs == (person_ref,)
    assert "key_subject_refs" in proposal.to_mapping()
    assert "key_character_refs" not in proposal.to_mapping()
    assert result.committed.values.admission.evaluation_strategy_version == "stage2-sd-compact-v2"
    assert command.execute(request).committed == result.committed
    assert len(provider.dispatches) == 1
    stage3_store = MemoryEditorialBlueprintStore(store.inputs, store)
    stage3_request = _policy(request).build_request(request, result.outcome, "compact-stage3")
    stage3_inputs = stage3_module._inputs(stage3_store, stage3_request)
    stage3_raw = _raw_for(stage3_request, stage3_inputs)
    stage3_provider = ScriptedDraftProvider(stage3_raw)
    stage3 = BuildEditorialBlueprintCommand(stage3_store, stage3_provider)
    scripted = stage3.execute(stage3_request)
    assert scripted.outcome.state == "succeeded", scripted.outcome.failure_detail_json
    assert stage3.execute(stage3_request).committed == scripted.committed
    assert len(stage3_provider.dispatches) == 1
    assert len(provider.dispatches) == 1


@pytest.mark.parametrize("field,bad_ref", [("key_subject_refs", "f1"), ("obligation_refs", "o999999")])
def test_compact_invalid_refs_are_durable_denial_without_new_upstream_call(field, bad_ref):
    store, request, raw, _ = compact_command_case()
    wire = json.loads(raw)
    wire["proposals"][0][field] = [bad_ref]
    provider = ScriptedDraftProvider(canonical_json_bytes(wire))
    command = CompileStoryPortfolioCommand(store, provider)
    result = command.execute(request)
    assert result.outcome.state == "denied"
    assert command.execute(request).outcome == result.outcome
    assert store.record is None
    assert len(provider.dispatches) == 1
