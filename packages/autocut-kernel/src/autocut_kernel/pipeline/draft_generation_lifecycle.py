"""Durable provider-attempt lifecycle shared by text-draft commands.

This module owns only generic generation transport and causal terminal
receipts.  A command-specific owner decodes a durable raw response and either
commits its business result or calls :meth:`DraftGenerationLifecycle.reject`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash, sha256_bytes
from ..semantic_chain.draft_provider import DraftDispatchRequest, DraftProviderPort
from ..store.models import (
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    GenerationAttempt,
    Job,
)
from ..vlm.provider_port import (
    ProviderCompleted,
    ProviderFailed,
    ProviderIndeterminate,
    ProviderPending,
    ProviderReconcileQuery,
)
from ..vlm.retry_policy import GenerationRetryPolicy
from .committed_outcome import succeeded_outcome_mapping
from .generate_vlm_evidence_command import GenerationStore


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise ValueError(f"{name} must be non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class DraftExecutionPlan:
    """All generic durable identity needed before a provider may be called."""

    command_name: str
    provider_key_namespace: str
    job: Job
    idempotency_key: str
    request_hash: str
    request_payload: bytes
    provider_payload: bytes
    provider_id: str
    model_id: str
    adapter_strategy_version: str
    retry_policy: GenerationRetryPolicy
    denial_codes: frozenset[str]

    def __post_init__(self) -> None:
        for name in (
            "command_name",
            "provider_key_namespace",
            "idempotency_key",
            "request_hash",
            "provider_id",
            "model_id",
            "adapter_strategy_version",
        ):
            _text(getattr(self, name), name)
        if type(self.job) is not Job:  # noqa: E721
            raise ValueError("generation plan requires an exact Job")
        if (
            type(self.request_payload) is not bytes
            or not self.request_payload
            or type(self.provider_payload) is not bytes
            or not self.provider_payload
        ):
            raise ValueError("generation plan payloads must be non-empty exact bytes")
        if self.request_hash != sha256_bytes(self.request_payload):
            raise ValueError("generation plan request_hash must bind request_payload")
        if type(self.retry_policy) is not GenerationRetryPolicy:  # noqa: E721
            raise ValueError("generation plan requires an exact retry policy")
        if type(self.denial_codes) is not frozenset or not self.denial_codes:  # noqa: E721
            raise ValueError("generation plan denial_codes must be a non-empty frozenset")
        if any(type(code) is not str or not code.strip() for code in self.denial_codes):  # noqa: E721
            raise ValueError("generation plan denial_codes must contain non-empty text")
        if len(self.denial_codes) != len(set(self.denial_codes)):
            raise ValueError("generation plan denial_codes must be unique")

    def provider_idempotency_key_for(self, ordinal: int) -> str:
        if type(ordinal) is not int or not 1 <= ordinal <= self.retry_policy.max_attempts:  # noqa: E721
            raise ValueError("attempt ordinal exceeds retry policy")
        return canonical_json_hash(
            {
                "command": self.provider_key_namespace,
                "job_key": self.job.job_key,
                "idempotency_key": self.idempotency_key,
                "request_hash": self.request_hash,
                "attempt_ordinal": ordinal,
            }
        )


@dataclass(frozen=True, slots=True)
class DraftExecutionState:
    """The durable transport state; no business result is implied."""

    outcome: CommandOutcome
    attempt: GenerationAttempt | None = None

    def __post_init__(self) -> None:
        if type(self.outcome) is not CommandOutcome:  # noqa: E721
            raise ValueError("draft execution state requires an exact CommandOutcome")
        if self.attempt is not None and type(self.attempt) is not GenerationAttempt:  # noqa: E721
            raise ValueError("draft execution state attempt must be exact when present")


def assert_draft_attempt(
    plan: DraftExecutionPlan, outcome: CommandOutcome, attempt: GenerationAttempt,
) -> None:
    """Reject a persisted attempt that differs from the frozen command plan."""
    if (
        type(plan) is not DraftExecutionPlan  # noqa: E721
        or type(outcome) is not CommandOutcome  # noqa: E721
        or type(attempt) is not GenerationAttempt  # noqa: E721
        or outcome.job_id is None
        or attempt.job_id != outcome.job_id
        or attempt.command_slot_id != outcome.command_slot_id
        or attempt.request_hash != plan.request_hash
        or attempt.provider_id != plan.provider_id
        or attempt.provider_idempotency_key
        != plan.provider_idempotency_key_for(attempt.attempt_ordinal)
        or attempt.request_payload.content_hash != sha256_bytes(plan.request_payload)
        or attempt.request_payload.byte_length != len(plan.request_payload)
        or attempt.request_payload.media_type != "application/json"
        or attempt.retry_policy_hash != plan.retry_policy.canonical_hash
        or attempt.max_attempts != plan.retry_policy.max_attempts
        or attempt.retry_backoff_seconds
        != (
            0
            if attempt.attempt_ordinal == 1
            else plan.retry_policy.backoff_after(attempt.attempt_ordinal - 1)
        )
    ):
        raise ValueError("generation Attempt differs from its exact Job/Command/request/policy")


def read_draft_request_bytes(
    store: GenerationStore, plan: DraftExecutionPlan, attempt: GenerationAttempt,
) -> None:
    if store.read_immutable_blob(plan.job, attempt.request_payload) != plan.request_payload:
        raise ValueError("durable generation request differs from its frozen plan")


class CommittedDraftAuditStore(GenerationStore, Protocol):
    def read_committed_generation_attempt_chain(
        self, job: Job, *, command_slot_id: UUID, receipt_id: UUID, artifact_set_id: UUID,
        expected_request_hash: str,
    ) -> tuple[GenerationAttempt, ...]: ...


def read_draft_response_bytes(
    store: GenerationStore, plan: DraftExecutionPlan, outcome: CommandOutcome,
    attempt: GenerationAttempt,
) -> bytes:
    """Audit the actual immutable request/response, without parsing draft meaning."""
    assert_draft_attempt(plan, outcome, attempt)
    read_draft_request_bytes(store, plan, attempt)
    reference = attempt.raw_response
    if reference is None or reference.media_type != "application/json":
        raise ValueError("generation requires the exact durable raw JSON response")
    raw = store.read_immutable_blob(plan.job, reference)
    if (type(raw) is not bytes or sha256_bytes(raw) != reference.content_hash  # noqa: E721
            or len(raw) != reference.byte_length):
        raise ValueError("generation raw response differs from its audited Blob")
    return raw


def read_committed_draft_audit(
    store: CommittedDraftAuditStore, plan: DraftExecutionPlan, outcome: CommandOutcome,
) -> tuple[tuple[GenerationAttempt, ...], bytes]:
    """Replay the whole committed retry chain and return only its final raw draft.

    Earlier failed attempts can retain invalid/partial model output. Verify all
    such immutable bytes but never parse them as successful business meaning.
    This function performs reads only; it cannot claim, regenerate or repair.
    """
    succeeded_outcome_mapping(outcome)
    # The closed transport check above requires all four canonical UUID values.
    if outcome.receipt_id is None or outcome.artifact_set_id is None:
        raise ValueError("committed generation lacks its exact Receipt/Set")
    chain = store.read_committed_generation_attempt_chain(
        plan.job, command_slot_id=outcome.command_slot_id, receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id, expected_request_hash=plan.request_hash,
    )
    if (type(chain) is not tuple or not chain or len(chain) > plan.retry_policy.max_attempts  # noqa: E721
            or any(type(attempt) is not GenerationAttempt for attempt in chain)
            or len({attempt.attempt_id for attempt in chain}) != len(chain)
            or chain[-1].state != "committed"):
        raise ValueError("generation has no complete committed attempt audit")
    for ordinal, attempt in enumerate(chain, start=1):
        assert_draft_attempt(plan, outcome, attempt)
        if (attempt.attempt_ordinal != ordinal
                or attempt.previous_attempt_id != (None if ordinal == 1 else chain[ordinal - 2].attempt_id)
                or (ordinal < len(chain) and (attempt.state != "failed" or attempt.failure_disposition != "retryable"))):
            raise ValueError("generation audit is not a complete ordered retry chain")
        if ordinal < len(chain):
            if attempt.raw_response is None:
                read_draft_request_bytes(store, plan, attempt)
            else:
                read_draft_response_bytes(store, plan, outcome, attempt)
    final = chain[-1]
    if final.receipt_id != outcome.receipt_id or final.artifact_set_id != outcome.artifact_set_id:
        raise ValueError("final generation Attempt belongs to another committed result")
    return chain, read_draft_response_bytes(store, plan, outcome, final)


class DraftGenerationLifecycle:
    """Run or reconcile one durable text-generation attempt without compiling it."""

    def __init__(self, store: GenerationStore, provider: DraftProviderPort) -> None:
        self._store = store
        self._provider = provider

    def execute(self, plan: DraftExecutionPlan) -> DraftExecutionState:
        outcome = self._store.claim_command(
            CommandClaim(
                plan.job,
                plan.idempotency_key,
                plan.command_name,
                plan.request_hash,
                execution_kind="generation",
            )
        )
        if outcome.state in ("failed", "denied", "succeeded"):
            return DraftExecutionState(outcome)
        if self._provider.strategy_version != plan.adapter_strategy_version:
            raise ValueError("generation provider strategy differs from its frozen request")
        attempt = self._store.read_generation_attempt_for_slot(plan.job, outcome.command_slot_id)
        if attempt is None:
            blob = self._store.put_immutable_blob(
                plan.job,
                content=plan.request_payload,
                content_hash=sha256_bytes(plan.request_payload),
                media_type="application/json",
            )
            attempt = self._store.reserve_generation_attempt(
                outcome.command_slot_id,
                plan.request_hash,
                provider_id=plan.provider_id,
                provider_idempotency_key=plan.provider_idempotency_key_for(1),
                request_payload=blob,
                retry_policy_hash=plan.retry_policy.canonical_hash,
                max_attempts=plan.retry_policy.max_attempts,
            )
        assert_draft_attempt(plan, outcome, attempt)
        read_draft_request_bytes(self._store, plan, attempt)
        if attempt.state == "committed":
            return DraftExecutionState(
                CommandOutcome(
                    outcome.command_slot_id,
                    "succeeded",
                    receipt_id=attempt.receipt_id,
                    artifact_set_id=attempt.artifact_set_id,
                    job_id=attempt.job_id,
                ),
                attempt,
            )
        if attempt.state == "failed":
            return self._recover(plan, outcome, attempt)
        if attempt.state in ("responded", "reconciled"):
            return DraftExecutionState(outcome, attempt)
        if attempt.state == "reserved":
            leased = self._store.dispatch_generation_attempt(
                attempt.attempt_id, expected_version=attempt.version
            )
            if leased is None:
                return DraftExecutionState(outcome, attempt)
            active_attempt = leased

            def save_request_id(provider_request_id: str) -> None:
                nonlocal active_attempt
                active_attempt = self._store.record_generation_provider_request_id(
                    active_attempt.attempt_id,
                    expected_version=active_attempt.version,
                    provider_request_id=provider_request_id,
                    dispatch_lease_token=self._lease(active_attempt),
                )

            try:
                result = self._provider.dispatch(
                    DraftDispatchRequest(
                        plan.provider_id,
                        plan.model_id,
                        active_attempt.provider_idempotency_key,
                        plan.provider_payload,
                        sha256_bytes(plan.provider_payload),
                        save_request_id,
                    )
                )
            except Exception:
                return self._unknown(outcome, active_attempt)
            return self._handle(plan, outcome, active_attempt, result)
        if attempt.state in ("dispatched", "indeterminate"):
            leased = self._store.acquire_generation_reconcile_lease(
                attempt.attempt_id, expected_version=attempt.version
            )
            if leased is None:
                return DraftExecutionState(outcome, attempt)
            try:
                result = self._provider.reconcile(
                    ProviderReconcileQuery(
                        leased.provider_id,
                        plan.model_id,
                        leased.provider_idempotency_key,
                        leased.provider_request_id,
                    )
                )
            except Exception:
                return self._unknown(outcome, leased)
            return self._handle(plan, outcome, leased, result)
        raise ValueError("generation lifecycle encountered an unregistered attempt state")

    def reject(
        self,
        plan: DraftExecutionPlan,
        outcome: CommandOutcome,
        attempt: GenerationAttempt,
        code: str,
        detail: dict[str, object],
    ) -> DraftExecutionState:
        if code not in plan.denial_codes:
            raise ValueError("command rejection code is not registered by its generation plan")
        assert_draft_attempt(plan, outcome, attempt)
        failed = self._store.fail_generation_attempt(
            attempt.attempt_id,
            expected_version=attempt.version,
            failure_code=code,
            failure_detail_json=canonical_json_bytes(detail).decode("utf-8"),
            failure_disposition="repairable",
        )
        return self._terminal(plan, outcome, failed, code, "denied")

    @staticmethod
    def _lease(attempt: GenerationAttempt) -> str:
        if attempt.dispatch_lease_token is None:
            raise ValueError("generation provider operation requires a durable lease")
        return attempt.dispatch_lease_token

    def _unknown(
        self, outcome: CommandOutcome, attempt: GenerationAttempt, request_id: str | None = None,
    ) -> DraftExecutionState:
        updated = self._store.mark_generation_indeterminate(
            attempt.attempt_id,
            expected_version=attempt.version,
            dispatch_lease_token=self._lease(attempt),
            provider_request_id=request_id or attempt.provider_request_id,
        )
        return DraftExecutionState(outcome, updated)

    def _handle(
        self,
        plan: DraftExecutionPlan,
        outcome: CommandOutcome,
        attempt: GenerationAttempt,
        result: object,
    ) -> DraftExecutionState:
        if isinstance(result, ProviderCompleted):
            blob: BlobRef = self._store.put_immutable_blob(
                plan.job,
                content=result.raw_response,
                content_hash=sha256_bytes(result.raw_response),
                media_type="application/json",
            )
            record = (
                self._store.record_generation_response
                if attempt.state == "dispatched"
                else self._store.reconcile_generation_response
            )
            updated = record(
                attempt.attempt_id,
                expected_version=attempt.version,
                raw_response=blob,
                dispatch_lease_token=self._lease(attempt),
                provider_request_id=result.provider_request_id,
            )
            return DraftExecutionState(outcome, updated)
        if isinstance(result, ProviderFailed):
            failed = self._store.fail_generation_attempt(
                attempt.attempt_id,
                expected_version=attempt.version,
                failure_code=result.failure_code,
                failure_detail_json=result.failure_detail_json,
                failure_disposition=result.disposition.value,
                provider_request_id=result.provider_request_id,
                dispatch_lease_token=self._lease(attempt),
            )
            return self._recover(plan, outcome, failed)
        if isinstance(result, (ProviderPending, ProviderIndeterminate)):
            return self._unknown(outcome, attempt, result.provider_request_id)
        return self._unknown(outcome, attempt)

    def _recover(
        self, plan: DraftExecutionPlan, outcome: CommandOutcome, attempt: GenerationAttempt,
    ) -> DraftExecutionState:
        if attempt.failure_disposition == "retryable" and attempt.attempt_ordinal < attempt.max_attempts:
            successor = self._store.reserve_next_generation_attempt(
                attempt.attempt_id,
                expected_version=attempt.version,
                provider_idempotency_key=plan.provider_idempotency_key_for(attempt.attempt_ordinal + 1),
            )
            assert_draft_attempt(plan, outcome, successor)
            return DraftExecutionState(outcome, successor)
        code = "RETRY_BUDGET_EXHAUSTED" if attempt.failure_disposition == "retryable" else attempt.failure_code
        if code is None:
            raise ValueError("failed generation Attempt lacks its causal reason")
        state: Literal["failed", "denied"] = "denied" if code in plan.denial_codes else "failed"
        return self._terminal(plan, outcome, attempt, code, state)

    def _terminal(
        self,
        plan: DraftExecutionPlan,
        outcome: CommandOutcome,
        attempt: GenerationAttempt,
        code: str,
        state: Literal["failed", "denied"],
    ) -> DraftExecutionState:
        chain = self._store.read_generation_attempt_chain(plan.job, outcome.command_slot_id)
        if not chain or chain[-1].attempt_id != attempt.attempt_id:
            raise ValueError("generation rejection lost its exact final Attempt")
        for item in chain:
            assert_draft_attempt(plan, outcome, item)
            if item.state != "failed":
                raise ValueError("terminal generation rejection contains a nonfailed Attempt")
        detail = json.dumps(
            {
                "terminal_reason": code,
                "attempts": [
                    {
                        "attempt_id": str(item.attempt_id),
                        "attempt_ordinal": item.attempt_ordinal,
                        "provider_request_id": item.provider_request_id,
                        "failure_code": item.failure_code,
                        "failure_disposition": item.failure_disposition,
                        "failure_detail": json.loads(item.failure_detail_json or "{}"),
                    }
                    for item in chain
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        terminal = self._store.commit_generation_rejection(
            attempt.attempt_id,
            expected_version=attempt.version,
            rejection=CommandRejection(outcome.command_slot_id, code, detail, outcome=state),
        )
        return DraftExecutionState(terminal, attempt)


__all__ = [
    "CommittedDraftAuditStore",
    "DraftExecutionPlan",
    "DraftExecutionState",
    "DraftGenerationLifecycle",
    "assert_draft_attempt",
    "read_committed_draft_audit",
    "read_draft_request_bytes",
    "read_draft_response_bytes",
]
