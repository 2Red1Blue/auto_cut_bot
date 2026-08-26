"""Real semantic Stage 1 -> Stage 2 over fake persistence/provider I/O only.

These tests exercise compilers, evaluators, strict readers and attempt recovery.
They do not claim PostgreSQL transaction or actual Ark runtime acceptance.
"""

import json
from dataclasses import replace
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from autocut_kernel.pipeline import compile_story_portfolio_command as command_module
from autocut_kernel.pipeline.build_narrative_graph_command import BuildNarrativeGraphCommand
from autocut_kernel.pipeline.compile_story_portfolio_command import (
    COMMAND_NAME,
    CompileStoryPortfolioCommand,
    read_committed_story_portfolio,
)
from autocut_kernel.pipeline.compile_story_portfolio_request import CompileStoryPortfolioRequest
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.portfolio_admission import SD_RULE_IDS
from autocut_kernel.semantic_chain.portfolio_values import InitialSourceUsageLedger, StorySelection
from autocut_kernel.semantic_chain.story_design_compiler import _member
from autocut_kernel.semantic_chain.story_design_context import story_design_input_binding
from autocut_kernel.semantic_chain.story_design_result import STAGE2_MEMBER_TYPES
from autocut_kernel.store.errors import BlobIntegrityError, IdempotencyConflictError
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm.provider_port import (
    ProviderCompleted,
    ProviderFailed,
    ProviderFailureDisposition,
)
from autocut_kernel.vlm.retry_policy import GenerationRetryPolicy

from tests.semantic_chain.test_build_narrative_graph_command import (
    MemoryNarrativeGraphStore,
    ScriptedDraftProvider,
    SimulatedCrash,
)
from tests.semantic_chain.test_candidate_projection import _command_request, _draft_raw
from tests.semantic_chain.test_material_support import material_case
from tests.semantic_chain.test_story_design_draft import POLICY


class MemoryStoryPortfolioStore(MemoryNarrativeGraphStore):
    """Two separate command slots; predecessor operations are read-only routed."""

    def __init__(self, inputs, predecessor):
        super().__init__(inputs)
        self.predecessor = predecessor
        self.slot = UUID(int=9100)
        self.counter = 20000
        self.command_name = COMMAND_NAME

    def read_committed_artifact_set(self, job, **expected):
        if expected["command_slot_id"] == self.predecessor.slot:
            return self.predecessor.read_committed_artifact_set(job, **expected)
        return super().read_committed_artifact_set(job, **expected)

    def read_committed_generation_attempt_chain(self, job, **expected):
        if expected["command_slot_id"] == self.predecessor.slot:
            return self.predecessor.read_committed_generation_attempt_chain(job, **expected)
        return super().read_committed_generation_attempt_chain(job, **expected)

    def read_immutable_blob(self, job, reference):
        if reference.object_id not in self.blobs:
            return self.predecessor.read_immutable_blob(job, reference)
        return super().read_immutable_blob(job, reference)


def command_case(*, raw=None, job_change=None, max_attempts=2, backoff=(0,)):
    case = material_case()
    inputs = case["inputs"]
    predecessor = MemoryNarrativeGraphStore(inputs)
    stage1_request = _command_request(inputs)
    stage1 = BuildNarrativeGraphCommand(predecessor, ScriptedDraftProvider(_draft_raw(inputs))).execute(stage1_request)
    assert stage1.outcome.state == "succeeded"
    assert stage1.committed.values == case["stage1"]
    job = case["job_policy"] if job_change is None else replace(case["job_policy"], **job_change)
    request = CompileStoryPortfolioRequest(
        stage1_request, stage1.outcome, "stage2-test-command", 1, stage1_request.generation,
        1_000_000, POLICY, case["candidate_policy"], job, case["story_policy"],
        GenerationRetryPolicy("generation-retry-v1", max_attempts, backoff),
    )
    binding = story_design_input_binding(case["stage1"], case["projection"], job_policy=job,
                                        story_policy=case["story_policy"], candidate_policy=case["candidate_policy"])
    draft = replace(case["draft"], input_binding_sha256=binding)
    raw = canonical_json_bytes(draft.to_mapping()) if raw is None else raw
    store = MemoryStoryPortfolioStore(inputs, predecessor)
    return store, ScriptedDraftProvider(raw), request, raw


def test_actual_five_member_commit_and_restart_replay_do_not_regenerate(monkeypatch):
    store, provider, request, raw = command_case()
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    assert result.outcome.state == "succeeded" and result.committed is not None
    assert result.committed.record is store.record
    assert tuple(member.artifact_type for member in store.record.artifacts) == STAGE2_MEMBER_TYPES
    admission = result.committed.values.admission
    assert {check.rule_id for check in admission.rule_results} == set(SD_RULE_IDS)
    assert all(check.status == "pass" for check in admission.rule_results)
    assert admission.raw_draft_sha256 == sha256_bytes(raw)
    assert len(result.committed.values.business.proposal_set.proposals) == 2
    assert len(admission.target_story_ids) == request.job_policy.selected_story_count
    assert len(store.successes) == len(provider.dispatches) == 1
    assert not provider.reconciles and not store.rejections
    assert store.predecessor.claim.command_name == "BuildNarrativeGraphCommand"
    assert len(store.predecessor.successes) == 1

    def forbidden(*args, **kwargs):
        raise AssertionError("replay must not execute or compile replacement outputs")

    monkeypatch.setattr(command_module, "compile_story_design", forbidden)
    unavailable = ScriptedDraftProvider(b"{}")
    unavailable.strategy_version = "not-installed"
    replay = CompileStoryPortfolioCommand(store, unavailable).execute(request)
    assert replay.committed.record.references == result.committed.record.references
    assert replay.committed == result.committed
    assert not unavailable.dispatches and not unavailable.reconciles


@pytest.mark.parametrize("raw", [b"{}", b"{broken", b'{"status":"pass"}'])
def test_invalid_draft_is_durable_denial_without_partial_members_or_semantic_retry(raw):
    store, provider, request, _ = command_case(raw=raw)
    command = CompileStoryPortfolioCommand(store, provider)
    result = command.execute(request)
    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "STAGE2_DRAFT_OR_COMPILATION_REJECTED"
    assert result.outcome.artifact_set_id is None and result.committed is None
    assert store.record is None and not store.successes
    assert store.read_immutable_blob(request.job, store.attempts[-1].raw_response) == raw
    assert command.execute(request).outcome == result.outcome
    assert len(provider.dispatches) == len(store.attempts) == 1


@pytest.mark.parametrize("job_change,code", [
    ({"selected_story_count": 2, "source_reuse_policy": "forbid"}, "STAGE2_PORTFOLIO_INFEASIBLE"),
    ({"max_search_states": 1}, "STAGE2_MATERIAL_INDETERMINATE"),
])
def test_search_failure_keeps_diagnostics_and_never_lowers_target_count(job_change, code):
    store, provider, request, _ = command_case(job_change=job_change)
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    assert result.outcome.state == "denied" and result.outcome.failure_code == code
    assert store.record is None and not store.successes
    detail = json.loads(result.outcome.failure_detail_json)["attempts"][-1]["failure_detail"]
    assert len(detail["proposal_set"]["proposals"]) == 2
    assert "visited_states" in detail
    assert not result.outcome.artifact_set_id


@pytest.mark.parametrize("crash", ["crash_after_response", "crash_after_success"])
def test_restart_after_raw_or_atomic_commit_never_dispatches_twice(crash):
    store, provider, request, _ = command_case()
    setattr(store, crash, True)
    with pytest.raises(SimulatedCrash):
        CompileStoryPortfolioCommand(store, provider).execute(request)
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    assert result.outcome.state == "succeeded"
    assert len(store.attempts) == len(provider.dispatches) == len(store.successes) == 1


def test_restart_after_denial_audit_preserves_denied_receipt():
    store, provider, request, _ = command_case(raw=b"{}")
    store.crash_after_failure = True
    with pytest.raises(SimulatedCrash):
        CompileStoryPortfolioCommand(store, provider).execute(request)
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "STAGE2_DRAFT_OR_COMPILATION_REJECTED"
    assert len(provider.dispatches) == len(store.attempts) == 1


def test_unknown_response_reconciles_same_invocation_and_preserved_callback_id():
    store, provider, request, _ = command_case()
    provider.dispatch_results = [TimeoutError("stream interrupted after provider created response")]
    command = CompileStoryPortfolioCommand(store, provider)
    pending = command.execute(request)
    assert pending.attempt.state == "indeterminate"
    assert pending.attempt.provider_request_id == "response-1"
    result = command.execute(request)
    assert result.outcome.state == "succeeded"
    assert len(provider.dispatches) == len(provider.reconciles) == len(store.attempts) == 1
    assert provider.reconciles[0].provider_request_id == "response-1"
    assert provider.reconciles[0].provider_idempotency_key == provider.dispatches[0].provider_idempotency_key


def test_retryable_failure_respects_store_backoff_and_full_predecessor_chain():
    store, provider, request, raw = command_case(backoff=(5,))
    provider.dispatch_results = [
        ProviderFailed("temporary", "{}", "response-1", ProviderFailureDisposition.RETRYABLE),
        ProviderCompleted(raw, "response-2"),
    ]
    command = CompileStoryPortfolioCommand(store, provider)
    pending = command.execute(request)
    assert pending.attempt.attempt_ordinal == 2 and pending.attempt.state == "reserved"
    assert command.execute(request).attempt.state == "reserved"
    assert len(provider.dispatches) == 1
    store.advance(5)
    result = command.execute(request)
    assert result.outcome.state == "succeeded" and len(result.committed.attempts) == 2
    assert provider.dispatches[0].provider_idempotency_key != provider.dispatches[1].provider_idempotency_key


def test_retry_exhaustion_preserves_all_causes_without_business_outputs():
    store, provider, request, _ = command_case()
    provider.dispatch_results = [ProviderFailed(f"failure-{i}", "{}", f"response-{i}",
                                               ProviderFailureDisposition.RETRYABLE) for i in (1, 2)]
    command = CompileStoryPortfolioCommand(store, provider)
    command.execute(request)
    result = command.execute(request)
    assert result.outcome.state == "failed" and result.outcome.failure_code == "RETRY_BUDGET_EXHAUSTED"
    detail = json.loads(result.outcome.failure_detail_json)
    assert [item["failure_code"] for item in detail["attempts"]] == ["failure-1", "failure-2"]
    assert not store.successes and not store.record


def test_lease_loser_does_not_dispatch():
    store, provider, request, _ = command_case()
    store.dispatch_allowed = False
    assert CompileStoryPortfolioCommand(store, provider).execute(request).attempt.state == "reserved"
    assert not provider.dispatches


@pytest.mark.parametrize("target", ["request", "raw"])
def test_tampered_audit_blob_denied_before_replay(target):
    store, provider, request, _ = command_case()
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    ref = result.attempt.request_payload if target == "request" else result.attempt.raw_response
    store.blobs[ref.object_id] = (ref, request.job, b"tampered")
    with pytest.raises(BlobIntegrityError):
        read_committed_story_portfolio(store, request, result.outcome)


@pytest.mark.parametrize("field", ["draft_policy_sha256", "story_policy_sha256", "raw_draft_sha256"])
def test_rehashed_admission_substitution_is_recomputed_not_trusted(field):
    store, provider, request, _ = command_case()
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    members = list(store.record.artifacts)
    payload = json.loads(members[-1].payload_json)
    payload[field] = "sha256:" + "f" * 64
    raw = canonical_json_bytes(payload).decode()
    members[-1] = replace(members[-1], payload_json=raw, content_hash=canonical_payload_hash(raw))
    store.record = store._record(tuple(members), result.outcome.receipt_id, result.outcome.artifact_set_id)
    with pytest.raises(ValueError, match="independent audited evaluation"):
        read_committed_story_portfolio(store, request, result.outcome)


def test_foreign_job_outcome_is_not_hidden_by_dataclass_equality():
    store, provider, request, _ = command_case()
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    forged = replace(result.outcome, job_id=UUID(int=444))
    assert forged == result.outcome  # This DTO deliberately excludes job_id from equality.
    with pytest.raises(ValueError, match="exact requested identity"):
        read_committed_story_portfolio(store, request, forged)


def test_foreign_predecessor_is_rejected_before_claim_or_provider():
    store, provider, request, _ = command_case()
    request = replace(request, stage1_outcome=replace(request.stage1_outcome, job_id=UUID(int=445)))
    with pytest.raises(ValueError, match="Job"):
        CompileStoryPortfolioCommand(store, provider).execute(request)
    assert store.claim is None and not provider.dispatches and not store.attempts


def test_same_key_with_different_generation_policy_conflicts_before_provider():
    store, provider, request, _ = command_case()
    CompileStoryPortfolioCommand(store, provider).execute(request)
    changed = replace(request, generation=replace(request.generation, model_id="another-explicit-model"))
    with pytest.raises(IdempotencyConflictError):
        CompileStoryPortfolioCommand(store, provider).execute(changed)
    assert len(provider.dispatches) == 1


def test_rehashed_later_feasible_portfolio_with_self_asserted_pass_is_rejected():
    store, provider, request, _ = command_case()
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    business = result.committed.values.business
    scope = request.artifact_scope
    later = business.proposal_set.proposals[1]
    selection = StorySelection(1, SemanticObjectRef(business.portfolio.proposal_set_ref,
                               "proposal", later.proposal.proposal_id))
    portfolio = replace(business.portfolio, selections=(selection,), requirement_assignments=tuple(
        replace(row, proposal_index=1) for row in business.portfolio.requirement_assignments
    ))
    portfolio_member = _member("portfolio", portfolio.to_mapping(), scope=scope, revision=1)
    usage = InitialSourceUsageLedger(SemanticMemberIdentity.from_artifact_member(portfolio_member),
                                     portfolio.target_story_ids)
    members = (*store.record.artifacts[:2], portfolio_member,
               _member("source_usage_ledger", usage.to_mapping(), scope=scope, revision=1))
    admission = replace(result.committed.values.admission,
                        business_members=tuple(SemanticMemberIdentity.from_artifact_member(item) for item in members),
                        target_story_ids=portfolio.target_story_ids)
    # Rewrite all subjects, targets and set hashes; retain purportedly passing
    # checks. A merely self-consistent set must not pass independent selection.
    artifacts = (*members, _member("portfolio_admission", admission.to_mapping(), scope=scope, revision=1))
    store.record = store._record(artifacts, result.outcome.receipt_id, result.outcome.artifact_set_id)
    with pytest.raises(ValueError, match="independent audited evaluation"):
        read_committed_story_portfolio(store, request, result.outcome)


@pytest.mark.parametrize("target", ["request", "raw"])
def test_rehashed_replacement_audit_blob_cannot_change_frozen_meaning(target):
    store, provider, request, raw = command_case()
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    if target == "raw":
        payload = json.loads(raw)
        payload["proposals"][0]["title"] = "forged replacement narrative"
    else:
        payload = json.loads(store.read_immutable_blob(request.job, result.attempt.request_payload))
        payload["command_request"]["generation"]["model_id"] = "forged-model"
    forged_raw = canonical_json_bytes(payload)
    ref = store.put_immutable_blob(request.job, content=forged_raw,
                                   content_hash=sha256_bytes(forged_raw), media_type="application/json")
    changes = {"raw_response" if target == "raw" else "request_payload": ref}
    store.attempts[-1] = replace(store.attempts[-1], **changes)
    with pytest.raises(ValueError):
        read_committed_story_portfolio(store, request, result.outcome)


@pytest.mark.parametrize("field", ["job_id", "command_slot_id", "provider_idempotency_key"])
def test_foreign_attempt_cannot_be_replayed(field):
    store, provider, request, _ = command_case()
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    value = "foreign-provider-key" if field == "provider_idempotency_key" else UUID(int=123456)
    store.attempts[-1] = replace(store.attempts[-1], **{field: value})
    with pytest.raises(ValueError, match="exact Job/Command/request/policy"):
        read_committed_story_portfolio(store, request, result.outcome)


def test_independent_failed_check_cannot_be_ignored_by_command(monkeypatch):
    store, provider, request, _ = command_case()
    evaluate = command_module.evaluate_story_design_business_members

    def reject_selected(*args, **kwargs):
        checks = evaluate(*args, **kwargs)
        return tuple(replace(check, status="fail", violation_codes=("noncanonical_selection",))
                     if check.rule_id == "SD-OBJ-001" else check for check in checks)

    monkeypatch.setattr(command_module, "evaluate_story_design_business_members", reject_selected)
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    assert result.outcome.state == "denied" and result.outcome.failure_code == "STAGE2_ADMISSION_REJECTED"
    assert store.record is None and not store.successes


@pytest.mark.parametrize("mutation", ["none", "corrupt", "missing", "media"])
def test_replay_validates_every_existing_prior_response_without_decoding_failed_draft(mutation):
    store, provider, request, raw = command_case()
    provider.dispatch_results = [
        ProviderFailed("temporary", "{}", "response-1", ProviderFailureDisposition.RETRYABLE),
        ProviderCompleted(raw, "response-2"),
    ]
    command = CompileStoryPortfolioCommand(store, provider)
    command.execute(request)
    partial = b'{"incomplete provider response"'
    ref = store.put_immutable_blob(request.job, content=partial, content_hash=sha256_bytes(partial),
                                   media_type="application/json")
    store.attempts[0] = replace(store.attempts[0], raw_response=ref)
    result = command.execute(request)
    assert result.outcome.state == "succeeded"  # Earlier raw need not be valid semantic JSON.
    if mutation == "none":
        assert read_committed_story_portfolio(store, request, result.outcome).record is store.record
        return
    if mutation == "corrupt":
        store.blobs[ref.object_id] = (ref, request.job, b"damaged")
    elif mutation == "missing":
        del store.blobs[ref.object_id]
    else:
        different = store.put_immutable_blob(request.job, content=partial, content_hash=sha256_bytes(partial),
                                             media_type="text/plain")
        store.attempts[0] = replace(store.attempts[0], raw_response=different)
    with pytest.raises((BlobIntegrityError, KeyError, ValueError)):
        read_committed_story_portfolio(store, request, result.outcome)
