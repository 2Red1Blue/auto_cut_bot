"""Generic durable text-generation lifecycle without a business compiler."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.pipeline.build_narrative_graph_request import prepare_stage1_request
from autocut_kernel.pipeline.draft_generation_lifecycle import (
    DraftExecutionPlan,
    DraftGenerationLifecycle,
    assert_draft_attempt,
    read_draft_request_bytes,
)
from autocut_kernel.vlm.provider_port import (
    ProviderCompleted,
    ProviderFailed,
    ProviderFailureDisposition,
    ProviderIndeterminate,
)

from tests.semantic_chain.test_build_narrative_graph_command import _case

GENERIC_COMMAND = "CompileStoryPortfolioCommand"
GENERIC_NAMESPACE = "CompileStoryPortfolio"


def _plan(*, max_attempts: int = 2, backoff: tuple[int, ...] = (0,)):
    request, store, provider, raw = _case(max_attempts=max_attempts, backoff=backoff)
    prepared = prepare_stage1_request(request, store.inputs)
    store.command_name = GENERIC_COMMAND
    return (
        DraftExecutionPlan(
            GENERIC_COMMAND,
            GENERIC_NAMESPACE,
            request.job,
            request.idempotency_key,
            prepared.request_hash,
            prepared.request_payload,
            prepared.provider_payload,
            request.generation.provider_id,
            request.generation.model_id,
            request.generation.adapter_strategy_version,
            request.retry_policy,
            frozenset({"GENERIC_DRAFT_REJECTED"}),
        ),
        store,
        provider,
        raw,
    )


def _retryable(code: str) -> ProviderFailed:
    return ProviderFailed(
        code,
        '{"cause":"overloaded","retryable":true}',
        disposition=ProviderFailureDisposition.RETRYABLE,
    )


def test_completed_raw_response_is_durable_state_without_compiling_business_members():
    plan, store, provider, raw = _plan()
    state = DraftGenerationLifecycle(store, provider).execute(plan)
    assert state.outcome.state == "running"
    assert state.attempt is not None and state.attempt.state == "responded"
    assert state.attempt.provider_request_id == "response-1"
    assert store.read_immutable_blob(plan.job, state.attempt.raw_response) == raw
    assert not store.successes and not store.rejections and store.record is None
    assert_draft_attempt(plan, state.outcome, state.attempt)
    read_draft_request_bytes(store, plan, state.attempt)
    assert provider.dispatches[0].provider_idempotency_key == plan.provider_idempotency_key_for(1)


def test_unknown_provider_outcome_reconciles_original_attempt_without_successor():
    plan, store, provider, raw = _plan(max_attempts=3, backoff=(0, 0))
    provider.dispatch_results = [ProviderIndeterminate("TIMEOUT", "response-1")]
    first = DraftGenerationLifecycle(store, provider).execute(plan)
    assert first.attempt is not None and first.attempt.state == "indeterminate"
    assert len(store.attempts) == len(provider.dispatches) == 1
    store.advance(61)
    provider.reconcile_results = [ProviderCompleted(raw, "response-1")]
    recovered = DraftGenerationLifecycle(store, provider).execute(plan)
    assert recovered.attempt is not None and recovered.attempt.state == "reconciled"
    assert recovered.attempt.attempt_id == first.attempt.attempt_id
    assert len(store.attempts) == len(provider.reconciles) == 1
    assert provider.reconciles[0].provider_idempotency_key == plan.provider_idempotency_key_for(1)
    assert not store.successes and not store.rejections


def test_retryable_provider_failures_follow_store_backoff_then_terminal_causal_receipt():
    plan, store, provider, _raw = _plan(max_attempts=2, backoff=(3,))
    provider.emit_request_id = False
    provider.dispatch_results = [_retryable("FIRST"), _retryable("SECOND")]
    lifecycle = DraftGenerationLifecycle(store, provider)
    first = lifecycle.execute(plan)
    assert first.attempt is not None and first.attempt.state == "reserved"
    assert first.attempt.attempt_ordinal == 2 and first.attempt.retry_backoff_seconds == 3
    blocked = lifecycle.execute(plan)
    assert blocked.attempt is not None and blocked.attempt.attempt_id == first.attempt.attempt_id
    assert len(provider.dispatches) == 1
    store.advance(3)
    terminal = lifecycle.execute(plan)
    assert terminal.outcome.state == "failed"
    assert terminal.outcome.failure_code == "RETRY_BUDGET_EXHAUSTED"
    detail = json.loads(terminal.outcome.failure_detail_json)
    assert [item["failure_code"] for item in detail["attempts"]] == ["FIRST", "SECOND"]
    assert [item["attempt_ordinal"] for item in detail["attempts"]] == [1, 2]
    assert store.record is None and not store.successes and len(store.rejections) == 1


def test_command_rejection_retains_raw_response_and_replays_without_regeneration():
    plan, store, provider, raw = _plan()
    lifecycle = DraftGenerationLifecycle(store, provider)
    ready = lifecycle.execute(plan)
    assert ready.attempt is not None and ready.attempt.state == "responded"
    terminal = lifecycle.reject(
        plan,
        ready.outcome,
        ready.attempt,
        "GENERIC_DRAFT_REJECTED",
        {"reason": "strict compiler rejection"},
    )
    assert terminal.outcome.state == "denied"
    assert terminal.attempt is not None and terminal.attempt.state == "failed"
    assert store.read_immutable_blob(plan.job, terminal.attempt.raw_response) == raw
    assert len(provider.dispatches) == len(store.rejections) == 1
    replay = lifecycle.execute(plan)
    assert replay.outcome == terminal.outcome and replay.attempt is None
    assert len(provider.dispatches) == 1


def test_foreign_attempt_identity_and_unregistered_denial_code_are_rejected():
    plan, store, provider, _raw = _plan()
    ready = DraftGenerationLifecycle(store, provider).execute(plan)
    assert ready.attempt is not None
    with pytest.raises(ValueError, match="exact Job/Command/request/policy"):
        assert_draft_attempt(
            plan,
            ready.outcome,
            replace(ready.attempt, provider_idempotency_key="foreign"),
        )
    with pytest.raises(ValueError, match="not registered"):
        DraftGenerationLifecycle(store, provider).reject(
            plan, ready.outcome, ready.attempt, "FOREIGN_REJECTION", {"reason": "no"}
        )


def test_semantic_rejection_retains_original_canonical_json_encoding():
    plan, store, provider, _ = _plan()
    lifecycle = DraftGenerationLifecycle(store, provider)
    ready = lifecycle.execute(plan)
    detail = {"\ue000": "BMP key", "\U00010000": "supplementary key"}
    rejected = lifecycle.reject(plan, ready.outcome, ready.attempt, "GENERIC_DRAFT_REJECTED", detail)
    assert rejected.attempt.failure_detail_json == canonical_json_bytes(detail).decode("utf-8")
    assert rejected.attempt.failure_detail_json != json.dumps(detail, ensure_ascii=False, sort_keys=True,
                                                             separators=(",", ":"))


def test_semantic_rejection_does_not_relax_canonical_float_constraint():
    plan, store, provider, _ = _plan()
    lifecycle = DraftGenerationLifecycle(store, provider)
    ready = lifecycle.execute(plan)
    with pytest.raises(ValueError, match="float"):
        lifecycle.reject(plan, ready.outcome, ready.attempt, "GENERIC_DRAFT_REJECTED", {"score": 0.5})
    assert store.attempts[-1].state == "responded" and not store.rejections
