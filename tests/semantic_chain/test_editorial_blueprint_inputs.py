"""Actual Stage 1/2 readers over synthetic Source and test-only persistence.

Setup executes both real Commands with a scripted, in-process provider double.
The reader phase forbids every write/generation entry; these are not PostgreSQL
or real-provider acceptance tests, and no fake Admission is minted for setup.
"""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.pipeline.build_narrative_graph_command import BuildNarrativeGraphCommand
from autocut_kernel.pipeline.compile_story_portfolio_command import CompileStoryPortfolioCommand
from autocut_kernel.pipeline.editorial_blueprint_inputs import (
    read_committed_editorial_blueprint_inputs,
)
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity
from autocut_kernel.store.errors import BlobIntegrityError
from autocut_kernel.store.models import canonical_payload_hash

from tests.semantic_chain.test_compile_story_portfolio_command import command_case


def _forbid(*args, **kwargs):
    # Distinct from the persistence double's AssertionError for foreign refs:
    # a negative read test must never accidentally accept a forbidden write.
    raise RuntimeError("editorial input reading must not generate, claim or write")


@pytest.fixture(scope="module")
def committed_case():
    store, provider, request, _ = command_case(job_change={"selected_story_count": 2, "source_reuse_policy": "allow"})
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    assert result.outcome.state == "succeeded" and result.committed is not None
    assert len(result.committed.values.admission.target_story_ids) == 2
    return store, provider, request, result


@pytest.fixture
def case(committed_case, monkeypatch):
    store, provider, request, result = deepcopy(committed_case)
    for item in (store, store.predecessor):
        for name in (
            "claim_command", "put_immutable_blob", "reserve_generation_attempt",
            "dispatch_generation_attempt", "acquire_generation_reconcile_lease",
            "record_generation_provider_request_id", "record_generation_response",
            "reconcile_generation_response", "mark_generation_indeterminate", "fail_generation_attempt",
            "reserve_next_generation_attempt", "commit_generation_success", "commit_generation_rejection",
        ):
            monkeypatch.setattr(item, name, _forbid)
        item.events.clear()
    monkeypatch.setattr(provider, "dispatch", _forbid)
    monkeypatch.setattr(provider, "reconcile", _forbid)
    monkeypatch.setattr(BuildNarrativeGraphCommand, "execute", _forbid)
    monkeypatch.setattr(CompileStoryPortfolioCommand, "execute", _forbid)
    yield store, provider, request, result
    for item in (store, store.predecessor):
        assert {event[0] for event in item.events} <= {"inputs", "exact_set", "read_blob"}
        assert len(item.successes) == len(item.attempts) == 1 and not item.rejections
    assert len(provider.dispatches) == 1 and not provider.reconciles


def _read(case, *, request=None, outcome=None):
    store, _, original_request, result = case
    return read_committed_editorial_blueprint_inputs(
        store, stage2_request=original_request if request is None else request,
        stage2_outcome=result.outcome if outcome is None else outcome,
    )


def test_exact_admitted_predecessors_replay_all_targets_without_side_effects(case):
    store, _, request, result = case
    first = _read(case)
    second = _read(case, outcome=replace(result.outcome, is_fresh_claim=True))
    assert first == second
    assert first.semantic is store.inputs
    assert first.narrative.record is store.predecessor.record
    assert first.portfolio.record is store.record
    assert len(first.narrative.values.members) == 8
    assert len(first.portfolio.values.members) == 5
    targets = first.portfolio.values.business.portfolio.target_story_ids
    assert len(targets) == request.job_policy.selected_story_count == 2
    assert targets == first.portfolio.values.business.source_usage_ledger.target_story_ids
    assert targets == first.portfolio.values.admission.target_story_ids
    assert first.semantic.source_grant.require_purpose("render_source") is None
    assert first.portfolio.record.references == result.committed.record.references
    with pytest.raises(FrozenInstanceError):
        first.portfolio = None


@pytest.mark.parametrize("changes", [
    {"state": "running"}, {"state": "failed"}, {"state": "denied"},
    {"job_id": None}, {"receipt_id": None}, {"artifact_set_id": None},
    {"command_slot_id": None}, {"command_slot_id": "not-a-uuid"},
    {"job_id": "not-a-uuid"}, {"receipt_id": "not-a-uuid"},
    {"artifact_set_id": "not-a-uuid"}, {"is_fresh_claim": 1},
    {"failure_code": "FOREIGN_FAILURE"}, {"failure_detail_json": "{}"},
])
def test_invalid_outcome_rejected_before_any_read(case, changes):
    store, _, _, result = case
    with pytest.raises(ValueError, match="exact succeeded"):
        _read(case, outcome=replace(result.outcome, **changes))
    assert not store.events and not store.predecessor.events


def test_caller_content_cannot_replace_the_frozen_request(case):
    store, _, request, result = case
    with pytest.raises(ValueError, match="frozen Stage 2 request"):
        read_committed_editorial_blueprint_inputs(store, stage2_request=request.to_mapping(), stage2_outcome=result.outcome)
    assert not store.events and not store.predecessor.events


def test_foreign_job_is_not_hidden_by_command_outcome_equality(case):
    store, _, _, result = case
    foreign = replace(result.outcome, job_id=UUID(int=998))
    assert foreign == result.outcome
    with pytest.raises(ValueError, match="different Jobs"):
        _read(case, outcome=foreign)
    assert not store.events and not store.predecessor.events


def test_matching_foreign_job_claims_cannot_replace_the_actual_stored_job(case):
    request, outcome = case[2], case[3].outcome
    changed = replace(request, stage1_outcome=replace(request.stage1_outcome, job_id=UUID(int=995)))
    with pytest.raises(ValueError, match="Job differs"):
        _read(case, request=changed, outcome=replace(outcome, job_id=UUID(int=995)))


@pytest.mark.parametrize("field", ["command_slot_id", "receipt_id", "artifact_set_id"])
def test_foreign_stage2_persistence_identity_is_not_resolved_as_latest(case, field):
    with pytest.raises(AssertionError):  # Exact persistence double rejects the foreign read.
        _read(case, outcome=replace(case[3].outcome, **{field: UUID(int=997)}))


@pytest.mark.parametrize("field", ["command_slot_id", "receipt_id", "artifact_set_id"])
def test_foreign_stage1_persistence_identity_is_not_regenerated(case, field):
    request = case[2]
    changed = replace(request, stage1_outcome=replace(request.stage1_outcome, **{field: UUID(int=996)}))
    with pytest.raises(AssertionError):
        _read(case, request=changed)


def test_changed_stage2_policy_cannot_reuse_a_succeeded_result(case):
    request = case[2]
    changed = replace(request, generation=replace(request.generation, prompt_version="foreign-version"))
    with pytest.raises(AssertionError):
        _read(case, request=changed)


@pytest.mark.parametrize("parent", [False, True])
@pytest.mark.parametrize("field", ["request_payload", "raw_response"])
def test_corrupt_durable_raw_or_request_from_either_stage_is_rejected(case, parent, field):
    store = case[0].predecessor if parent else case[0]
    ref = getattr(store.attempts[-1], field)
    store.blobs[ref.object_id] = (ref, store.job, b"corrupt immutable bytes")
    with pytest.raises(BlobIntegrityError):
        _read(case)


def _replace_members(store, result, members):
    store.record = store._record(tuple(members), result.outcome.receipt_id, result.outcome.artifact_set_id)


def _rewrite(member, payload):
    raw = canonical_json_bytes(payload).decode()
    return replace(member, content_hash=canonical_payload_hash(raw), payload_json=raw)


def test_rehashed_self_asserted_admission_cannot_replace_independent_evaluation(case):
    store, _, _, result = case
    members = list(store.record.artifacts)
    payload = json.loads(members[-1].payload_json)
    payload["raw_draft_sha256"] = "sha256:" + "f" * 64
    members[-1] = _rewrite(members[-1], payload)
    _replace_members(store, result, members)
    with pytest.raises(ValueError, match="independent audited evaluation"):
        _read(case)


def test_rehashed_usage_order_cannot_change_frozen_full_targets(case):
    store, _, _, result = case
    members = list(store.record.artifacts)
    usage = json.loads(members[3].payload_json)
    usage["rows"].reverse()
    for index, row in enumerate(usage["rows"]):
        row["priority_index"] = index
    usage["target_story_ids_hash"] = canonical_json_hash([row["story_id"] for row in usage["rows"]])
    members[3] = _rewrite(members[3], usage)
    admission = replace(result.committed.values.admission, business_members=tuple(
        SemanticMemberIdentity.from_artifact_member(member) for member in members[:4]
    ))
    members[4] = _rewrite(members[4], admission.to_mapping())
    _replace_members(store, result, members)
    with pytest.raises(ValueError, match="DAG/targets"):
        _read(case)


def test_source_grant_drift_is_not_hidden_by_prior_stage2_success(case):
    store = case[0]
    grant = store.inputs.source_grant
    changed = replace(store.inputs, source_grant=replace(
        grant, policy=replace(grant.policy, authorized_purposes=("semantic_analysis",)),
    ))
    store.inputs = store.predecessor.inputs = changed
    with pytest.raises((ValueError, AssertionError)):
        _read(case)
