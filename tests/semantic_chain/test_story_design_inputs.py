"""Real predecessor verification over test-only persistence, no database I/O."""

from dataclasses import replace
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.pipeline.build_narrative_graph_command import BuildNarrativeGraphCommand
from autocut_kernel.pipeline.story_design_inputs import read_committed_story_design_inputs
from autocut_kernel.source_manifest import SourcePurposeDeniedError
from autocut_kernel.vlm.provider_port import ProviderCompleted

from tests.semantic_chain.test_build_narrative_graph_command import _case, _forbid
from tests.semantic_chain.test_stage1_draft import _draft


def render_case():
    request, store, provider, _ = _case()
    # This persistence double supplies the already-decoded grant. Actual Source
    # payload/authorization/hash verification belongs to the production reader.
    grant = store.inputs.source_grant
    store.inputs = replace(store.inputs, source_grant=replace(
        grant, policy=replace(grant.policy, authorized_purposes=("render_source", "semantic_analysis")),
    ))
    draft = _draft(store.inputs)
    draft["merge_proposals"] = []
    provider.dispatch_results = [ProviderCompleted(canonical_json_bytes(draft), "response-1")]
    return request, store, provider


def test_reads_exact_stage1_without_regeneration_or_writes(monkeypatch):
    request, store, provider = render_case()
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert result.outcome.state == "succeeded"
    before = len(provider.dispatches), len(store.attempts), len(store.successes)
    for name in ("claim_command", "put_immutable_blob", "commit_generation_success"):
        monkeypatch.setattr(store, name, _forbid)
    monkeypatch.setattr(provider, "dispatch", _forbid)
    inputs = read_committed_story_design_inputs(store, stage1_request=request, stage1_outcome=result.outcome)
    assert inputs.semantic is store.inputs
    assert inputs.narrative.record is store.record
    assert len(inputs.narrative.values.members) == 8
    assert (len(provider.dispatches), len(store.attempts), len(store.successes)) == before


def test_semantic_only_authorization_does_not_permit_story_source_selection():
    request, store, provider, _ = _case()
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert result.outcome.state == "succeeded"
    with pytest.raises(SourcePurposeDeniedError, match="render_source"):
        read_committed_story_design_inputs(store, stage1_request=request, stage1_outcome=result.outcome)


@pytest.mark.parametrize("state", ["running", "failed", "denied"])
def test_incomplete_or_denied_stage1_is_never_regenerated(state):
    request, store, provider, _ = _case()
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    before = len(provider.dispatches)
    with pytest.raises(ValueError, match="exact succeeded"):
        read_committed_story_design_inputs(store, stage1_request=request, stage1_outcome=replace(result.outcome, state=state))
    assert len(provider.dispatches) == before


def test_wrong_receipt_cannot_read_latest_stage1_by_convenience():
    request, store, provider, _ = _case()
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    # Test store asserts exact receipt; production Store raises its integrity error.
    with pytest.raises(AssertionError):
        read_committed_story_design_inputs(store, stage1_request=request, stage1_outcome=replace(result.outcome, receipt_id=UUID(int=99)))


def test_stage1_policy_drift_rejected_before_stage2():
    request, store, provider, _ = _case()
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    changed = replace(request, generation=replace(request.generation, prompt_version="different"))
    with pytest.raises(AssertionError):
        read_committed_story_design_inputs(store, stage1_request=changed, stage1_outcome=result.outcome)
