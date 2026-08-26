"""Shared exact audit over a real in-memory Stage1 retry/success chain."""

from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import sha256_bytes
from autocut_kernel.pipeline.build_narrative_graph_command import (
    BuildNarrativeGraphCommand,
    _execution_plan,
    read_committed_narrative_graph,
)
from autocut_kernel.pipeline.build_narrative_graph_request import prepare_stage1_request
from autocut_kernel.pipeline.draft_generation_lifecycle import (
    read_committed_draft_audit,
    read_draft_response_bytes,
)
from autocut_kernel.store.errors import BlobIntegrityError
from autocut_kernel.vlm.provider_port import (
    ProviderCompleted,
    ProviderFailed,
    ProviderFailureDisposition,
)

from tests.semantic_chain.test_build_narrative_graph_command import _case


@pytest.fixture(scope="module")
def committed():
    request, store, provider, raw = _case()
    provider.dispatch_results = [
        ProviderFailed("temporary", "{}", "response-1", ProviderFailureDisposition.RETRYABLE),
        ProviderCompleted(raw, "response-2"),
    ]
    command = BuildNarrativeGraphCommand(store, provider)
    assert command.execute(request).outcome.state == "running"
    partial = b'{"unfinished response"'
    ref = store.put_immutable_blob(request.job, content=partial, content_hash=sha256_bytes(partial),
                                   media_type="application/json")
    store.attempts[0] = replace(store.attempts[0], raw_response=ref)
    result = command.execute(request)
    assert result.outcome.state == "succeeded"
    plan = _execution_plan(prepare_stage1_request(request, store.inputs))
    return request, store, plan, result.outcome, raw, ref


@pytest.fixture
def case(committed):
    return deepcopy(committed)


def test_shared_audit_reads_every_request_and_retained_response_without_writes(case, monkeypatch):
    request, store, plan, outcome, raw, partial = case

    def forbidden(*args, **kwargs):
        raise RuntimeError("audit must never write or generate")

    for method in ("claim_command", "put_immutable_blob", "reserve_generation_attempt", "commit_generation_success"):
        monkeypatch.setattr(store, method, forbidden)
    store.events.clear()
    chain, actual = read_committed_draft_audit(store, plan, outcome)
    assert chain == tuple(store.attempts) and actual == raw
    read_ids = {event[1].object_id for event in store.events if event[0] == "read_blob"}
    assert {attempt.request_payload.object_id for attempt in chain} <= read_ids
    assert partial.object_id in read_ids and chain[-1].raw_response.object_id in read_ids
    assert read_committed_narrative_graph(store, request, outcome).attempts == chain


@pytest.mark.parametrize("change", ["missing", "reorder", "duplicate", "list", "ordinal", "previous", "state", "disposition", "final_receipt", "final_set"])
def test_shared_audit_itself_rejects_malformed_chain_not_just_store_assertions(case, monkeypatch, change):
    _, store, plan, outcome, _, _ = case
    first, final = store.attempts
    chains = {
        "missing": (final,), "reorder": (final, first), "duplicate": (first, first),
        "list": [first, final], "ordinal": (replace(first, attempt_ordinal=2, previous_attempt_id=UUID(int=998)), final),
        "previous": (first, replace(final, previous_attempt_id=UUID(int=998))),
        "state": (replace(first, state="responded", failure_code=None,
                          failure_detail_json=None, failure_disposition=None), final),
        "disposition": (replace(first, failure_disposition="repairable"), final),
        "final_receipt": (first, replace(final, receipt_id=UUID(int=999))),
        "final_set": (first, replace(final, artifact_set_id=UUID(int=999))),
    }
    monkeypatch.setattr(store, "read_committed_generation_attempt_chain", lambda *args, **kwargs: chains[change])
    with pytest.raises(ValueError):
        read_committed_draft_audit(store, plan, outcome)


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("field", ["request_payload", "raw_response"])
def test_every_attempt_blob_is_rechecked(case, position, field):
    _, store, plan, outcome, _, _ = case
    reference = getattr(store.attempts[position], field)
    store.blobs[reference.object_id] = (reference, plan.job, b"corrupt")
    with pytest.raises(BlobIntegrityError):
        read_committed_draft_audit(store, plan, outcome)


@pytest.mark.parametrize("position", [0, 1])
def test_wrong_raw_media_type_is_rejected_even_with_valid_blob_hash(case, position):
    _, store, plan, outcome, _, _ = case
    original = store.attempts[position].raw_response
    payload = store.read_immutable_blob(plan.job, original)
    wrong = store.put_immutable_blob(plan.job, content=payload, content_hash=sha256_bytes(payload), media_type="text/plain")
    store.attempts[position] = replace(store.attempts[position], raw_response=wrong)
    with pytest.raises(ValueError, match="raw JSON"):
        read_committed_draft_audit(store, plan, outcome)


@pytest.mark.parametrize("change", ["state", "job", "failure", "freshness", "request_policy"])
def test_frozen_identity_and_terminal_outcome_are_not_optional(case, change):
    _, store, plan, outcome, _, _ = case
    if change == "request_policy":
        plan = replace(plan, provider_id="foreign-provider")
    else:
        modifications = {"state": {"state": "running"}, "job": {"job_id": None},
                         "failure": {"failure_code": "foreign"}, "freshness": {"is_fresh_claim": 1}}
        outcome = replace(outcome, **modifications[change])
    with pytest.raises(ValueError):
        read_committed_draft_audit(store, plan, outcome)


def test_response_hash_is_checked_even_if_store_reader_returns_unchecked_bytes(case, monkeypatch):
    _, store, plan, outcome, raw, _ = case
    attempt = store.attempts[-1]
    original_reader = store.read_immutable_blob

    def wrong(job, reference):
        if reference == attempt.raw_response:
            return raw + b" "
        return original_reader(job, reference)

    monkeypatch.setattr(store, "read_immutable_blob", wrong)
    with pytest.raises(ValueError, match="audited Blob"):
        read_draft_response_bytes(store, plan, outcome, attempt)


def test_stage1_replay_now_rejects_corrupt_retained_failed_raw(case):
    request, store, _, outcome, _, partial = case
    store.blobs[partial.object_id] = (partial, request.job, b"damaged")
    with pytest.raises(BlobIntegrityError):
        read_committed_narrative_graph(store, request, outcome)
