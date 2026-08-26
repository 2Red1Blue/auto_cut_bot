"""Actual Stage 1/2 predecessor reads through the audited Stage 3 Command.

The persistence double only simulates slot/attempt/blob semantics.  The test
still uses the real committed readers, editorial compiler and independent
Admission evaluator; it makes no runtime/provider or database claim.
"""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from autocut_kernel.pipeline import build_editorial_blueprint_command as command_module
from autocut_kernel.pipeline.build_editorial_blueprint_command import (
    COMMAND_NAME,
    BuildEditorialBlueprintCommand,
    read_committed_editorial_blueprints,
)
from autocut_kernel.pipeline.build_editorial_blueprint_request import prepare_stage3_request
from autocut_kernel.pipeline.compile_story_portfolio_command import CompileStoryPortfolioCommand
from autocut_kernel.semantic_chain.editorial_admission import SS_BATCH_RULE_IDS, SS_STORY_RULE_IDS
from autocut_kernel.semantic_chain.editorial_result import decode_editorial_members
from autocut_kernel.store.errors import BlobIntegrityError, IdempotencyConflictError
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm.provider_port import (
    ProviderCompleted,
    ProviderFailed,
    ProviderFailureDisposition,
)
from autocut_kernel.vlm.retry_policy import GenerationRetryPolicy

from tests.semantic_chain.test_build_editorial_blueprint_request import _policy
from tests.semantic_chain.test_build_narrative_graph_command import (
    MemoryNarrativeGraphStore,
    ScriptedDraftProvider,
    SimulatedCrash,
)
from tests.semantic_chain.test_compile_story_portfolio_command import command_case


class MemoryEditorialBlueprintStore(MemoryNarrativeGraphStore):
    """A third exact generation slot, routed read-only to its two predecessors."""

    def __init__(self, inputs, predecessor) -> None:
        super().__init__(inputs)
        self.predecessor = predecessor
        self.slot = UUID(int=9200)
        self.counter = 30_000
        self.command_name = COMMAND_NAME

    def read_committed_artifact_set(self, job, **expected):
        if expected["command_slot_id"] != self.slot:
            return self.predecessor.read_committed_artifact_set(job, **expected)
        return super().read_committed_artifact_set(job, **expected)

    def read_committed_generation_attempt_chain(self, job, **expected):
        if expected["command_slot_id"] != self.slot:
            return self.predecessor.read_committed_generation_attempt_chain(job, **expected)
        return super().read_committed_generation_attempt_chain(job, **expected)

    def read_immutable_blob(self, job, reference):
        if reference.object_id not in self.blobs:
            return self.predecessor.read_immutable_blob(job, reference)
        return super().read_immutable_blob(job, reference)


def _raw_for(request, inputs) -> bytes:
    """Construct a complete draft only from this fixture's exact Stage 2 values."""
    prepared = prepare_stage3_request(request, inputs)
    stage2 = inputs.portfolio.values
    catalog = {
        candidate.candidate_id: candidate
        for candidate in stage2.business.candidate_catalog.candidates
    }
    stories: list[dict[str, object]] = []
    for selection in stage2.business.portfolio.selections:
        support = stage2.business.proposal_set.proposals[selection.proposal_index]
        proposal = support.proposal
        beats: list[dict[str, object]] = []
        for material, evidence in zip(
            proposal.material_requirements, support.requirements, strict=True,
        ):
            candidate_ref = evidence.alternatives[0].candidate_ref
            candidate = catalog[candidate_ref.object_id]
            beats.append({
                "narrative_role": "reveal",
                "narrative_function": candidate.narrative_functions[0],
                "summary": "保留完整事件和必选事实",
                "required_obligation_refs": [material.obligation_ref.to_mapping()],
                "required_fact_refs": [ref.to_mapping() for ref in evidence.required_fact_refs],
                "evidence_requirements": [{
                    "source_material_requirement_id": material.requirement_id,
                    "satisfaction": "one_of",
                    "alternative_sets": [{
                        "alternative_id": "direct",
                        "event_refs": [candidate.anchor_event.event_card_ref.to_mapping()],
                        "candidate_refs": [candidate_ref.to_mapping()],
                    }],
                }],
                "candidate_preferences": [candidate_ref.to_mapping()],
                "span_policy": {"preferred": "tight", "allowed": ["tight"], "fallback_order": ["tight"]},
                "duration_seconds": {
                    "min": material.minimum_usable_seconds,
                    "target": material.minimum_usable_seconds,
                    "max": proposal.target_duration_seconds.maximum,
                },
            })
        bounds = proposal.target_duration_seconds
        stories.append({
            "story_id": selection.story_id,
            "proposal_ref": selection.proposal_ref.to_mapping(),
            "beats": beats,
            "ordering_constraints": [],
            "story_duration_seconds": {
                "min": bounds.minimum, "target": bounds.minimum, "max": bounds.maximum,
            },
            "editing_intent": {"pacing": "balanced", "continuity_priority": "high"},
            "teaser_intent": {
                "strategy": proposal.teaser_strategy,
                "duration_seconds": {"min": 1, "max": 1},
            },
        })
    return canonical_json_bytes({
        "schema_version": "stage3-editorial-blueprint-draft-v1",
        "input_binding_sha256": prepared.input_binding_sha256,
        "stories": stories,
    })


def command_case_stage3(*, raw: bytes | None = None, max_attempts: int = 2, backoff=(0,)):
    stage2_store, stage2_provider, stage2_request, _ = command_case(
        job_change={"selected_story_count": 2, "source_reuse_policy": "allow"},
    )
    stage2 = CompileStoryPortfolioCommand(stage2_store, stage2_provider).execute(stage2_request)
    assert stage2.outcome.state == "succeeded"
    store = MemoryEditorialBlueprintStore(stage2_store.inputs, stage2_store)
    policy = _policy(stage2_request)
    policy = replace(
        policy,
        retry_policy=GenerationRetryPolicy("generation-retry-v1", max_attempts, backoff),
    )
    request = policy.build_request(stage2_request, stage2.outcome, "stage3:test-command")
    inputs = command_module._inputs(store, request)
    raw = _raw_for(request, inputs) if raw is None else raw
    return store, ScriptedDraftProvider(raw), request, raw


def test_actual_seven_member_commit_and_replay_never_regenerates(monkeypatch) -> None:
    store, provider, request, raw = command_case_stage3()
    result = BuildEditorialBlueprintCommand(store, provider).execute(request)
    assert result.outcome.state == "succeeded" and result.committed is not None
    assert result.committed.record is store.record
    assert tuple(member.artifact_type for member in store.record.artifacts) == (
        "editorial_blueprint", "evidence_closure_set", "context_manifest",
        "editorial_blueprint", "evidence_closure_set", "context_manifest",
        "semantic_feasibility_admission",
    )
    assert tuple(check.rule_id for check in result.committed.values.admission.checks) == SS_BATCH_RULE_IDS
    assert all(
        tuple(check.rule_id for check in story.checks) == SS_STORY_RULE_IDS
        for story in result.committed.values.admission.stories
    )
    assert all(
        check.status == "pass"
        for story in result.committed.values.admission.stories
        for check in story.checks
    )
    assert result.committed.values.admission.raw_draft_sha256 == sha256_bytes(raw)
    assert len(store.successes) == len(provider.dispatches) == 1

    def forbidden(*args, **kwargs):
        raise AssertionError("replay must not regenerate or compile a replacement batch")

    monkeypatch.setattr(command_module, "project_editorial_blueprints", forbidden)
    replay_provider = ScriptedDraftProvider(b"{}")
    replay = BuildEditorialBlueprintCommand(store, replay_provider).execute(request)
    assert replay.committed == result.committed
    assert not replay_provider.dispatches and not replay_provider.reconciles


@pytest.mark.parametrize("raw", (b"{}", b"{broken", b'{"stories":[]}'))
def test_malformed_draft_is_terminal_denial_with_no_partial_story_members(raw: bytes) -> None:
    store, provider, request, _ = command_case_stage3(raw=raw)
    result = BuildEditorialBlueprintCommand(store, provider).execute(request)
    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "STAGE3_DRAFT_OR_COMPILATION_REJECTED"
    assert result.committed is None and store.record is None and not store.successes
    assert len(provider.dispatches) == len(store.attempts) == 1


@pytest.mark.parametrize("crash", ("crash_after_response", "crash_after_success"))
def test_restart_after_response_or_atomic_commit_never_dispatches_twice(crash: str) -> None:
    store, provider, request, _ = command_case_stage3()
    setattr(store, crash, True)
    with pytest.raises(SimulatedCrash):
        BuildEditorialBlueprintCommand(store, provider).execute(request)
    result = BuildEditorialBlueprintCommand(store, provider).execute(request)
    assert result.outcome.state == "succeeded"
    assert len(store.attempts) == len(provider.dispatches) == len(store.successes) == 1


def test_unknown_response_reconciles_the_same_provider_invocation() -> None:
    store, provider, request, _ = command_case_stage3()
    provider.dispatch_results = [TimeoutError("response was created before disconnect")]
    command = BuildEditorialBlueprintCommand(store, provider)
    pending = command.execute(request)
    assert pending.attempt is not None and pending.attempt.state == "indeterminate"
    result = command.execute(request)
    assert result.outcome.state == "succeeded"
    assert len(provider.dispatches) == len(provider.reconciles) == len(store.attempts) == 1


def test_audit_defect_after_response_raises_without_writing_a_semantic_denial() -> None:
    store, provider, request, _ = command_case_stage3()
    store.crash_after_response = True
    with pytest.raises(SimulatedCrash):
        BuildEditorialBlueprintCommand(store, provider).execute(request)
    attempt = store.attempts[-1]
    assert attempt.raw_response is not None
    store.attempts[-1] = replace(
        attempt, raw_response=replace(attempt.raw_response, media_type="text/plain"),
    )
    with pytest.raises(ValueError, match="raw JSON response"):
        BuildEditorialBlueprintCommand(store, provider).execute(request)
    assert store.record is None and not store.successes and not store.rejections
    assert len(provider.dispatches) == 1


def test_indeterminate_budget_and_infeasible_joint_duration_are_terminal_causal_denials() -> None:
    budget_store, budget_provider, budget_request, _ = command_case_stage3()
    budget_request = replace(
        budget_request,
        feasibility_policy=replace(budget_request.feasibility_policy, max_search_states=1),
    )
    budget = BuildEditorialBlueprintCommand(budget_store, budget_provider).execute(budget_request)
    assert budget.outcome.state == "denied"
    assert budget.outcome.failure_code == "STAGE3_FEASIBILITY_REJECTED"
    assert budget.committed is None and budget_store.record is None
    budget_detail = json.loads(budget.outcome.failure_detail_json)
    assert budget_detail["attempts"][-1]["failure_detail"]["feasibility"]["status"] == "indeterminate"
    assert len(budget_provider.dispatches) == len(budget_store.attempts) == 1

    timing_store, _timing_provider, timing_request, raw = command_case_stage3()
    timing_draft = json.loads(raw)
    timing_draft["stories"][0]["beats"][0]["duration_seconds"] = {
        "min": 1, "target": 1, "max": 1,
    }
    timing_draft["stories"][0]["story_duration_seconds"] = {
        "min": 2, "target": 2, "max": 10,
    }
    timing_provider = ScriptedDraftProvider(canonical_json_bytes(timing_draft))
    timing = BuildEditorialBlueprintCommand(timing_store, timing_provider).execute(timing_request)
    assert timing.outcome.state == "denied"
    assert timing.outcome.failure_code == "STAGE3_FEASIBILITY_REJECTED"
    timing_detail = json.loads(timing.outcome.failure_detail_json)
    assert timing_detail["attempts"][-1]["failure_detail"]["feasibility"]["status"] == "infeasible"
    assert timing_store.record is None and not timing_store.successes
    assert len(timing_provider.dispatches) == len(timing_store.attempts) == 1


def test_retry_backoff_and_exhaustion_are_transport_only() -> None:
    store, provider, request, raw = command_case_stage3(backoff=(5,))
    provider.dispatch_results = [
        ProviderFailed("temporary", "{}", "response-1", ProviderFailureDisposition.RETRYABLE),
        ProviderCompleted(raw, "response-2"),
    ]
    command = BuildEditorialBlueprintCommand(store, provider)
    pending = command.execute(request)
    assert pending.attempt is not None and pending.attempt.attempt_ordinal == 2
    assert command.execute(request).attempt is not None
    assert len(provider.dispatches) == 1
    store.advance(5)
    assert command.execute(request).outcome.state == "succeeded"

    failed_store, failed_provider, failed_request, _ = command_case_stage3()
    failed_provider.dispatch_results = [
        ProviderFailed(f"retry-{index}", "{}", f"response-{index}", ProviderFailureDisposition.RETRYABLE)
        for index in (1, 2)
    ]
    failed = BuildEditorialBlueprintCommand(failed_store, failed_provider)
    failed.execute(failed_request)
    exhausted = failed.execute(failed_request)
    assert exhausted.outcome.state == "failed"
    assert exhausted.outcome.failure_code == "RETRY_BUDGET_EXHAUSTED"
    assert failed_store.record is None and not failed_store.successes


@pytest.mark.parametrize("target", ("request", "raw"))
def test_audit_blob_tampering_is_rejected_before_replay(target: str) -> None:
    store, provider, request, _ = command_case_stage3()
    result = BuildEditorialBlueprintCommand(store, provider).execute(request)
    assert result.attempt is not None
    ref = result.attempt.request_payload if target == "request" else result.attempt.raw_response
    assert ref is not None
    store.blobs[ref.object_id] = (ref, request.job, b"tampered")
    with pytest.raises(BlobIntegrityError):
        read_committed_editorial_blueprints(store, request, result.outcome)


def test_foreign_stage2_identity_rejects_before_stage3_claim_or_provider() -> None:
    store, provider, request, _ = command_case_stage3()
    request = replace(request, stage2_outcome=replace(request.stage2_outcome, job_id=UUID(int=445)))
    with pytest.raises(ValueError, match="Jobs"):
        BuildEditorialBlueprintCommand(store, provider).execute(request)
    assert store.claim is None and not provider.dispatches and not store.attempts


def test_changed_request_same_key_conflicts_before_provider() -> None:
    store, provider, request, _ = command_case_stage3()
    BuildEditorialBlueprintCommand(store, provider).execute(request)
    changed = replace(request, generation=replace(request.generation, model_id="another-frozen-model"))
    with pytest.raises(IdempotencyConflictError):
        BuildEditorialBlueprintCommand(store, provider).execute(changed)
    assert len(provider.dispatches) == 1


def test_rehashed_self_asserted_admission_cannot_replace_independent_result() -> None:
    store, provider, request, _ = command_case_stage3()
    result = BuildEditorialBlueprintCommand(store, provider).execute(request)
    members = list(store.record.artifacts)
    payload = json.loads(members[-1].payload_json)
    payload["command_policy_sha256"] = "sha256:" + "f" * 64
    raw = canonical_json_bytes(payload).decode("utf-8")
    members[-1] = replace(
        members[-1], payload_json=raw, content_hash=canonical_payload_hash(raw),
    )
    store.record = store._record(tuple(members), result.outcome.receipt_id, result.outcome.artifact_set_id)
    with pytest.raises(ValueError, match="independent audited evaluation"):
        read_committed_editorial_blueprints(store, request, result.outcome)


def test_rehashed_out_of_order_batch_checks_are_not_a_closed_admission() -> None:
    store, provider, request, _ = command_case_stage3()
    result = BuildEditorialBlueprintCommand(store, provider).execute(request)
    members = list(store.record.artifacts)
    payload = json.loads(members[-1].payload_json)
    payload["checks"] = list(reversed(payload["checks"]))
    raw = canonical_json_bytes(payload).decode("utf-8")
    members[-1] = replace(
        members[-1], payload_json=raw, content_hash=canonical_payload_hash(raw),
    )
    store.record = store._record(tuple(members), result.outcome.receipt_id, result.outcome.artifact_set_id)
    with pytest.raises(ValueError, match="exact ordered six checks"):
        read_committed_editorial_blueprints(store, request, result.outcome)


def test_reader_rejects_foreign_outcome_job_even_when_outcome_equality_hides_it() -> None:
    store, provider, request, _ = command_case_stage3()
    result = BuildEditorialBlueprintCommand(store, provider).execute(request)
    forged = replace(result.outcome, job_id=UUID(int=444))
    assert forged == result.outcome
    with pytest.raises(ValueError, match="exact requested identity"):
        read_committed_editorial_blueprints(store, request, forged)


def test_persisted_values_remain_the_strict_seven_member_result() -> None:
    store, provider, request, _ = command_case_stage3()
    result = BuildEditorialBlueprintCommand(store, provider).execute(request)
    prepared = prepare_stage3_request(request, command_module._inputs(store, request))
    assert decode_editorial_members(store.record.artifacts, contexts=prepared.contexts) == result.committed.values
