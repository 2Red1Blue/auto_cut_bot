"""Actual Stage 1 Command over a test-only in-memory persistence double.

Real DTOs, compiler, eight-member decoder and independent KC evaluators run.
The fake simulates versions/leases/backoff and exact Blob/set joins only; it
does not prove PostgreSQL locking, crash atomicity or provider acceptance.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from autocut_kernel.pipeline import build_narrative_graph_command as command_module
from autocut_kernel.pipeline.build_narrative_graph_command import (
    COMMAND_NAME,
    BuildNarrativeGraphCommand,
    read_committed_narrative_graph,
)
from autocut_kernel.semantic_chain.coverage_admission import CoverageAdmission
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity
from autocut_kernel.semantic_chain.stage1_checks import KC_RULE_IDS
from autocut_kernel.semantic_chain.stage1_result import decode_stage1_members
from autocut_kernel.store.errors import (
    BlobIntegrityError,
    GenerationAttemptStateError,
    IdempotencyConflictError,
)
from autocut_kernel.store.models import (
    ArtifactMember,
    BlobRef,
    CommandOutcome,
    CommittedArtifactMemberReference,
    GenerationAttempt,
    PersistedCommittedArtifactMember,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
    canonical_payload_hash,
)
from autocut_kernel.vlm.provider_port import (
    ProviderCompleted,
    ProviderFailed,
    ProviderFailureDisposition,
    ProviderIndeterminate,
    ProviderPending,
)
from autocut_kernel.vlm.retry_policy import GenerationRetryPolicy

from tests.semantic_chain.test_coverage_analysis import _clean_inputs, _replace_pack
from tests.semantic_chain.test_stage1_draft import _draft
from tests.semantic_chain.test_stage1_generation_request import _request


class SimulatedCrash(BaseException):
    """Process-loss injection; unlike provider failure, Command must not catch it."""


class MemoryNarrativeGraphStore:
    """Test-only persistence semantics, never a production authority adapter."""

    def __init__(self, inputs):
        self.inputs = inputs
        self.job = inputs.source_manifest.source_job
        self.job_id = inputs.source_manifest.job_id
        self.slot = UUID(int=9000)
        self.clock = datetime(2026, 8, 26, tzinfo=timezone.utc)
        self.counter = 10000
        self.claim = None
        self.outcome = None
        self.attempts = []
        self.blobs = {}
        self.record = None
        self.receipt_attempts = {}
        self.successes = []
        self.rejections = []
        self.events = []
        self.dispatch_allowed = True
        self.reconcile_allowed = True
        self.crash_after_response = False
        self.crash_after_request_id = False
        self.crash_after_success = False
        self.crash_after_failure = False

    def _uuid(self):
        self.counter += 1
        return UUID(int=self.counter)

    def advance(self, seconds):
        self.clock += timedelta(seconds=seconds)

    def read_committed_semantic_inputs(self, request):
        self.events.append(("inputs", request))
        source = self.inputs.source_manifest
        expected_source = CommittedArtifactMemberReference(
            source.receipt_id,
            source.artifact_set_id,
            0,
            source.reference.scope,
            source.reference.artifact_type,
            source.reference.logical_id,
            source.reference.revision,
            source.reference.content_hash,
        )
        assert request.job == self.job
        assert request.source_manifest == expected_source
        assert request.vlm_semantic_pack_set == self.inputs.vlm_semantic_pack_set
        return self.inputs

    def claim_command(self, claim):
        self.events.append(("claim", claim))
        assert claim.job == self.job and claim.execution_kind == "generation"
        assert claim.command_name == COMMAND_NAME
        if self.claim is not None and self.claim != claim:
            raise IdempotencyConflictError("same key has different frozen request")
        if self.claim is None:
            self.claim = claim
            self.outcome = CommandOutcome(self.slot, "running", job_id=self.job_id)
        return self.outcome

    def put_immutable_blob(self, job, *, content, content_hash, media_type):
        assert job == self.job and type(content) is bytes
        assert sha256_bytes(content) == content_hash
        for ref, stored_job, raw in self.blobs.values():
            if stored_job == job and raw == content and ref.media_type == media_type:
                return ref
        ref = BlobRef(self._uuid(), content_hash, len(content), media_type)
        self.blobs[ref.object_id] = (ref, job, content)
        self.events.append(("put_blob", ref))
        return ref

    def read_immutable_blob(self, job, reference):
        self.events.append(("read_blob", reference))
        ref, owner, raw = self.blobs[reference.object_id]
        if (
            owner != job
            or ref != reference
            or sha256_bytes(raw) != ref.content_hash
            or len(raw) != ref.byte_length
        ):
            raise BlobIntegrityError("exact Blob/owner/bytes mismatch")
        return raw

    def reserve_generation_attempt(self, slot_id, request_hash, **kwargs):
        assert self.claim and slot_id == self.slot and request_hash == self.claim.request_hash
        assert not self.attempts
        self.read_immutable_blob(self.job, kwargs["request_payload"])
        attempt = GenerationAttempt(
            self._uuid(),
            self.job_id,
            self.slot,
            request_hash,
            kwargs["provider_id"],
            kwargs["provider_idempotency_key"],
            kwargs["request_payload"],
            "reserved",
            0,
            retry_policy_hash=kwargs["retry_policy_hash"],
            max_attempts=kwargs["max_attempts"],
            not_before_at=self.clock,
        )
        self.attempts.append(attempt)
        self.events.append(("reserve", attempt.attempt_id))
        return attempt

    def read_generation_attempt_for_slot(self, job, slot_id):
        assert job == self.job and slot_id == self.slot
        return self.attempts[-1] if self.attempts else None

    def read_generation_attempt_chain(self, job, slot_id):
        assert job == self.job and slot_id == self.slot
        return tuple(self.attempts)

    def _current(self, attempt_id, expected_version, states, lease=None):
        attempt = next(item for item in self.attempts if item.attempt_id == attempt_id)
        if attempt.version != expected_version or attempt.state not in states:
            raise GenerationAttemptStateError("stale version or illegal source state")
        if lease is not None and (
            lease != attempt.dispatch_lease_token
            or not attempt.dispatch_lease_is_active(self.clock)
        ):
            raise GenerationAttemptStateError("lease not owned or expired")
        return attempt

    def _update(self, attempt, **changes):
        updated = replace(attempt, version=attempt.version + 1, **changes)
        self.attempts[self.attempts.index(attempt)] = updated
        return updated

    @staticmethod
    def _request_id(attempt, supplied):
        if supplied is not None and attempt.provider_request_id not in (None, supplied):
            raise GenerationAttemptStateError("provider request identity changed")
        return supplied or attempt.provider_request_id

    def dispatch_generation_attempt(
        self, attempt_id, *, expected_version, provider_request_id=None
    ):
        attempt = self._current(attempt_id, expected_version, {"reserved"})
        self.events.append(("dispatch_lease", expected_version))
        if not self.dispatch_allowed or attempt.retry_delay_is_active(self.clock):
            return None
        return self._update(
            attempt,
            state="dispatched",
            provider_request_id=provider_request_id,
            dispatch_lease_token=str(self._uuid()),
            dispatch_lease_expires_at=self.clock + timedelta(seconds=60),
        )

    def acquire_generation_reconcile_lease(self, attempt_id, *, expected_version):
        attempt = self._current(attempt_id, expected_version, {"dispatched", "indeterminate"})
        self.events.append(("reconcile_lease", expected_version))
        if not self.reconcile_allowed or attempt.dispatch_lease_is_active(self.clock):
            return None
        return self._update(
            attempt,
            dispatch_lease_token=str(self._uuid()),
            dispatch_lease_expires_at=self.clock + timedelta(seconds=60),
        )

    def record_generation_provider_request_id(
        self, attempt_id, *, expected_version, provider_request_id, dispatch_lease_token
    ):
        attempt = self._current(attempt_id, expected_version, {"dispatched"}, dispatch_lease_token)
        updated = self._update(
            attempt, provider_request_id=self._request_id(attempt, provider_request_id)
        )
        self.events.append(("provider_id", (expected_version, updated.version)))
        if self.crash_after_request_id:
            self.crash_after_request_id = False
            raise SimulatedCrash("provider ID is durable, process lost")
        return updated

    def _response(
        self,
        attempt_id,
        *,
        expected_version,
        raw_response,
        dispatch_lease_token,
        provider_request_id,
        source_state,
        target_state,
    ):
        attempt = self._current(attempt_id, expected_version, {source_state}, dispatch_lease_token)
        self.read_immutable_blob(self.job, raw_response)
        updated = self._update(
            attempt,
            state=target_state,
            raw_response=raw_response,
            provider_request_id=self._request_id(attempt, provider_request_id),
            dispatch_lease_token=None,
            dispatch_lease_expires_at=None,
        )
        self.events.append(("response", (expected_version, updated.version)))
        if self.crash_after_response:
            self.crash_after_response = False
            raise SimulatedCrash("raw response committed, compiler not started")
        return updated

    def record_generation_response(self, attempt_id, **kwargs):
        return self._response(
            attempt_id, source_state="dispatched", target_state="responded", **kwargs
        )

    def reconcile_generation_response(self, attempt_id, **kwargs):
        return self._response(
            attempt_id, source_state="indeterminate", target_state="reconciled", **kwargs
        )

    def mark_generation_indeterminate(
        self, attempt_id, *, expected_version, dispatch_lease_token, provider_request_id=None
    ):
        attempt = self._current(
            attempt_id, expected_version, {"dispatched", "indeterminate"}, dispatch_lease_token
        )
        return self._update(
            attempt,
            state="indeterminate",
            provider_request_id=self._request_id(attempt, provider_request_id),
            dispatch_lease_token=None,
            dispatch_lease_expires_at=None,
        )

    def fail_generation_attempt(
        self,
        attempt_id,
        *,
        expected_version,
        failure_code,
        failure_detail_json,
        provider_request_id=None,
        failure_disposition="nonretryable",
        dispatch_lease_token=None,
    ):
        attempt = self._current(
            attempt_id,
            expected_version,
            {"reserved", "dispatched", "responded", "indeterminate", "reconciled"},
        )
        if attempt.state in {"dispatched", "indeterminate"}:
            self._current(attempt_id, expected_version, {attempt.state}, dispatch_lease_token)
            assert dispatch_lease_token is not None
        updated = self._update(
            attempt,
            state="failed",
            failure_code=failure_code,
            failure_detail_json=failure_detail_json,
            failure_disposition=failure_disposition,
            provider_request_id=self._request_id(attempt, provider_request_id),
            dispatch_lease_token=None,
            dispatch_lease_expires_at=None,
        )
        if self.crash_after_failure:
            self.crash_after_failure = False
            raise SimulatedCrash("failure audit durable, terminal receipt not written")
        return updated

    def reserve_next_generation_attempt(
        self, previous_attempt_id, *, expected_version, provider_idempotency_key
    ):
        previous = self._current(previous_attempt_id, expected_version, {"failed"})
        assert (
            previous.failure_disposition == "retryable"
            and previous.attempt_ordinal < previous.max_attempts
        )
        envelope = json.loads(self.read_immutable_blob(self.job, previous.request_payload))
        delay = envelope["retry_policy"]["backoff_seconds"][previous.attempt_ordinal - 1]
        next_attempt = GenerationAttempt(
            self._uuid(),
            previous.job_id,
            previous.command_slot_id,
            previous.request_hash,
            previous.provider_id,
            provider_idempotency_key,
            previous.request_payload,
            "reserved",
            0,
            attempt_ordinal=previous.attempt_ordinal + 1,
            previous_attempt_id=previous.attempt_id,
            retry_policy_hash=previous.retry_policy_hash,
            max_attempts=previous.max_attempts,
            not_before_at=self.clock + timedelta(seconds=delay),
            retry_backoff_seconds=delay,
        )
        self.attempts.append(next_attempt)
        self.events.append(("successor", next_attempt.attempt_id))
        return next_attempt

    def _record(self, artifacts, receipt, artifact_set):
        members = tuple(
            PersistedCommittedArtifactMember(
                CommittedArtifactMemberReference(
                    receipt,
                    artifact_set,
                    ordinal,
                    member.scope,
                    member.artifact_type,
                    member.logical_id,
                    member.revision,
                    member.content_hash,
                ),
                member.payload_json,
                self.slot,
            )
            for ordinal, member in enumerate(artifacts)
        )
        return PersistedCommittedArtifactSet(
            self.job,
            self.job_id,
            self.slot,
            receipt,
            artifact_set,
            self.claim.request_hash,
            COMMAND_NAME,
            "generation",
            artifact_set_hash(artifacts),
            members,
        )

    def commit_generation_success(self, attempt_id, *, expected_version, success):
        attempt = self._current(attempt_id, expected_version, {"responded", "reconciled"})
        assert success.command_slot_id == self.slot and success.set_hash == artifact_set_hash(
            success.artifacts
        )
        receipt, artifact_set = self._uuid(), self._uuid()
        self.record = self._record(success.artifacts, receipt, artifact_set)
        updated = self._update(
            attempt, state="committed", receipt_id=receipt, artifact_set_id=artifact_set
        )
        self.receipt_attempts = {item.attempt_id: receipt for item in self.attempts}
        self.outcome = CommandOutcome(
            self.slot,
            "succeeded",
            receipt_id=receipt,
            artifact_set_id=artifact_set,
            job_id=self.job_id,
        )
        self.successes.append(success)
        if self.crash_after_success:
            self.crash_after_success = False
            raise SimulatedCrash("business set committed, application result lost")
        return updated

    def commit_generation_rejection(self, attempt_id, *, expected_version, rejection):
        self._current(attempt_id, expected_version, {"failed"})
        assert all(item.state == "failed" for item in self.attempts)
        assert self.record is None
        receipt = self._uuid()
        self.receipt_attempts = {item.attempt_id: receipt for item in self.attempts}
        self.outcome = CommandOutcome(
            self.slot,
            rejection.outcome,
            receipt_id=receipt,
            failure_code=rejection.failure_code,
            failure_detail_json=rejection.failure_detail_json,
            job_id=self.job_id,
        )
        self.rejections.append(rejection)
        return self.outcome

    def read_committed_artifact_set(self, job, **expected):
        record = self.record
        assert record is not None and job == record.job
        assert expected == {
            "command_slot_id": record.command_slot_id,
            "receipt_id": record.receipt_id,
            "artifact_set_id": record.artifact_set_id,
            "expected_request_hash": record.request_hash,
            "expected_command_name": record.command_name,
            "expected_execution_kind": record.execution_kind,
        }
        self.events.append(("exact_set", record.artifact_set_id))
        return record

    def read_committed_generation_attempt_chain(self, job, **expected):
        record = self.record
        assert record is not None and job == record.job
        assert expected == {
            "command_slot_id": record.command_slot_id,
            "receipt_id": record.receipt_id,
            "artifact_set_id": record.artifact_set_id,
            "expected_request_hash": record.request_hash,
        }
        for ordinal, attempt in enumerate(self.attempts, 1):
            assert attempt.attempt_ordinal == ordinal
            assert self.receipt_attempts[attempt.attempt_id] == record.receipt_id
            assert attempt.state == ("committed" if ordinal == len(self.attempts) else "failed")
            assert attempt.previous_attempt_id == (
                self.attempts[ordinal - 2].attempt_id if ordinal > 1 else None
            )
        return tuple(self.attempts)


class ScriptedDraftProvider:
    """Explicit test script, not a runtime/provider adapter."""

    strategy_version = "doubao-ark-text-responses-stream-v1"

    def __init__(self, raw):
        self.dispatch_results = [ProviderCompleted(raw, "response-1")]
        self.reconcile_results = [ProviderCompleted(raw, "response-1")]
        self.dispatches = []
        self.reconciles = []
        self.emit_request_id = True

    def dispatch(self, request):
        self.dispatches.append(request)
        result = self.dispatch_results.pop(0)
        if self.emit_request_id:
            request.on_provider_request_id(f"response-{len(self.dispatches)}")
        if isinstance(result, BaseException):
            raise result
        return result

    def reconcile(self, query):
        self.reconciles.append(query)
        result = self.reconcile_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _case(*, taint=False, merge=False, raw=None, max_attempts=2, backoff=(0,)):
    _unused, request = _request()
    inputs = _clean_inputs()
    if taint:
        inputs = _replace_pack(
            inputs,
            0,
            lambda pack: replace(
                pack,
                entities=(
                    replace(
                        pack.entities[0],
                        support=replace(pack.entities[0].support, confidence=Decimal("0.1")),
                    ),
                ),
            ),
        )
    payload = _draft(inputs)
    if not merge:
        payload["merge_proposals"] = []
    raw = canonical_json_bytes(payload) if raw is None else raw
    request = replace(
        request, retry_policy=GenerationRetryPolicy("generation-retry-v1", max_attempts, backoff)
    )
    store = MemoryNarrativeGraphStore(inputs)
    provider = ScriptedDraftProvider(raw)
    return request, store, provider, raw


def _forbid(*_args, **_kwargs):
    raise AssertionError("replay must not invoke provider or producer compiler")


def test_actual_command_commits_exact_eight_members_and_seventeen_real_checks():
    request, store, provider, raw = _case()
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert result.outcome.state == "succeeded"
    assert result.committed.record is store.record
    assert len(store.record.members) == 8 and len(store.successes) == 1
    admission = result.committed.values.admission
    assert {check.rule_id for check in admission.rule_results} == set(KC_RULE_IDS)
    assert all(check.status == "pass" for check in admission.rule_results)
    assert admission.next_action == "continue" and admission.raw_draft_sha256 == sha256_bytes(raw)
    assert result.attempt.state == "committed" and len(provider.dispatches) == 1
    assert store.read_immutable_blob(request.job, result.attempt.raw_response) == raw
    envelope = json.loads(store.read_immutable_blob(request.job, result.attempt.request_payload))
    assert envelope["provider_request_json"].encode() == provider.dispatches[0].request_payload
    assert "retry_policy" not in json.loads(provider.dispatches[0].request_payload)
    assert [event[0] for event in store.events[:2]] == ["inputs", "claim"]


def test_success_replay_returns_same_store_record_without_generation_or_producer_compile(
    monkeypatch,
):
    request, store, provider, _raw = _case()
    first = BuildNarrativeGraphCommand(store, provider).execute(request)
    monkeypatch.setattr(provider, "dispatch", _forbid)
    monkeypatch.setattr(provider, "reconcile", _forbid)
    monkeypatch.setattr(command_module, "compile_stage1_coverage", _forbid)
    monkeypatch.setattr(command_module, "build_dependency_proof", _forbid)
    replay = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert replay.committed.record is first.committed.record
    assert replay.committed.record.references == first.committed.record.references
    assert len(store.attempts) == len(store.successes) == 1


def test_responded_crash_resumes_audited_raw_without_resampling():
    request, store, provider, raw = _case()
    store.crash_after_response = True
    with pytest.raises(SimulatedCrash):
        BuildNarrativeGraphCommand(store, provider).execute(request)
    assert store.attempts[-1].state == "responded" and store.record is None
    provider.dispatch_results = [AssertionError("must not resample")]
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert result.outcome.state == "succeeded" and len(provider.dispatches) == 1
    assert result.committed.values.admission.raw_draft_sha256 == sha256_bytes(raw)


def test_callback_persisted_version_is_used_for_response_and_success():
    request, store, provider, _raw = _case()
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert ("provider_id", (1, 2)) in store.events
    assert ("response", (2, 3)) in store.events
    assert result.attempt.version == 4 and result.attempt.provider_request_id == "response-1"


def test_committed_crash_replays_exact_set_without_regeneration(monkeypatch):
    request, store, provider, _raw = _case()
    store.crash_after_success = True
    with pytest.raises(SimulatedCrash):
        BuildNarrativeGraphCommand(store, provider).execute(request)
    record = store.record
    assert record is not None and store.attempts[-1].state == "committed"
    monkeypatch.setattr(provider, "dispatch", _forbid)
    monkeypatch.setattr(command_module, "compile_stage1_coverage", _forbid)
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert result.committed.record is record
    assert len(store.successes) == len(store.attempts) == 1


def test_crash_after_provider_id_recovers_same_dispatched_attempt_after_lease_expiry():
    request, store, provider, _raw = _case()
    store.crash_after_request_id = True
    with pytest.raises(SimulatedCrash):
        BuildNarrativeGraphCommand(store, provider).execute(request)
    durable = store.attempts[0]
    assert durable.state == "dispatched" and durable.provider_request_id == "response-1"
    blocked = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert blocked.outcome.state == "running" and not provider.reconciles
    store.advance(61)
    recovered = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert (
        recovered.outcome.state == "succeeded"
        and recovered.attempt.attempt_id == durable.attempt_id
    )
    assert len(provider.dispatches) == len(provider.reconciles) == 1
    assert provider.reconciles[0].provider_request_id == "response-1"


def test_dispatch_lease_loser_does_not_dispatch_or_compile():
    request, store, provider, _raw = _case()
    store.dispatch_allowed = False
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert result.outcome.state == "running" and result.attempt.state == "reserved"
    assert not provider.dispatches and not store.successes and not store.rejections


@pytest.mark.parametrize("result_kind", ["unknown", "pending", "exception", "foreign_result"])
def test_unknown_outcome_reconciles_same_attempt_without_successor(result_kind):
    request, store, provider, _raw = _case()
    provider.dispatch_results = [
        {
            "unknown": ProviderIndeterminate("TIMEOUT", "response-1"),
            "pending": ProviderPending("response-1"),
            "exception": TimeoutError("provider outcome unknown"),
            "foreign_result": object(),
        }[result_kind]
    ]
    first = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert first.attempt.state == "indeterminate" and len(store.attempts) == 1
    store.reconcile_allowed = False
    blocked = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert blocked.outcome.state == "running" and not provider.reconciles
    store.reconcile_allowed = True
    recovered = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert recovered.outcome.state == "succeeded"
    assert recovered.attempt.attempt_id == first.attempt.attempt_id
    assert len(store.attempts) == len(provider.dispatches) == len(provider.reconciles) == 1
    assert (
        provider.reconciles[0].provider_idempotency_key
        == provider.dispatches[0].provider_idempotency_key
    )
    assert provider.reconciles[0].provider_request_id == "response-1"
    assert not any(event[0] == "successor" for event in store.events)


def test_unknown_without_provider_id_remains_same_attempt_and_never_redispatches():
    request, store, provider, _raw = _case(max_attempts=3, backoff=(0, 0))
    provider.emit_request_id = False
    provider.dispatch_results = [ProviderIndeterminate("OUTCOME_UNKNOWN")]
    provider.reconcile_results = [ProviderIndeterminate("PROVIDER_REQUEST_ID_UNKNOWN")]
    first = BuildNarrativeGraphCommand(store, provider).execute(request)
    second = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert first.attempt.attempt_id == second.attempt.attempt_id
    assert second.attempt.state == "indeterminate" and second.outcome.state == "running"
    assert second.attempt.provider_request_id is None
    assert provider.reconciles[0].provider_request_id is None
    assert len(provider.dispatches) == len(provider.reconciles) == len(store.attempts) == 1
    assert not store.successes and not store.rejections


def _retryable(code="PROVIDER_HTTP_503"):
    return ProviderFailed(
        code,
        '{"retryable":true,"cause":"overloaded"}',
        disposition=ProviderFailureDisposition.RETRYABLE,
    )


def test_retryable_success_chain_respects_store_backoff_and_maximum_three_attempts():
    request, store, provider, raw = _case(max_attempts=3, backoff=(2, 8))
    provider.emit_request_id = False
    provider.dispatch_results = [
        _retryable(),
        _retryable("PROVIDER_HTTP_429"),
        ProviderCompleted(raw, "response-final"),
    ]
    first = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert first.attempt.state == "reserved" and first.attempt.attempt_ordinal == 2
    assert first.attempt.retry_backoff_seconds == 2
    BuildNarrativeGraphCommand(store, provider).execute(request)
    assert len(provider.dispatches) == 1  # fake Store clock, not a Command sleep
    store.advance(2)
    second = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert second.attempt.attempt_ordinal == 3 and second.attempt.retry_backoff_seconds == 8
    BuildNarrativeGraphCommand(store, provider).execute(request)
    assert len(provider.dispatches) == 2
    store.advance(8)
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert result.outcome.state == "succeeded"
    assert [item.state for item in result.committed.attempts] == ["failed", "failed", "committed"]
    assert len({item.provider_idempotency_key for item in store.attempts}) == 3
    assert len({item.request_payload for item in store.attempts}) == 1
    assert set(store.receipt_attempts.values()) == {result.outcome.receipt_id}
    assert len(store.successes) == 1 and len(provider.dispatches) == 3


def test_retry_budget_exhaustion_retains_full_causal_chain_without_business_set():
    request, store, provider, _raw = _case(max_attempts=3, backoff=(0, 0))
    provider.emit_request_id = False
    provider.dispatch_results = [_retryable("FIRST"), _retryable("SECOND"), _retryable("THIRD")]
    command = BuildNarrativeGraphCommand(store, provider)
    command.execute(request)
    command.execute(request)
    result = command.execute(request)
    assert (
        result.outcome.state == "failed" and result.outcome.failure_code == "RETRY_BUDGET_EXHAUSTED"
    )
    detail = json.loads(result.outcome.failure_detail_json)
    assert [item["failure_code"] for item in detail["attempts"]] == ["FIRST", "SECOND", "THIRD"]
    assert [item["attempt_ordinal"] for item in detail["attempts"]] == [1, 2, 3]
    assert all(item["failure_detail"]["cause"] == "overloaded" for item in detail["attempts"])
    assert set(store.receipt_attempts) == {item.attempt_id for item in store.attempts}
    assert store.record is None and not store.successes and len(store.rejections) == 1
    replay = command.execute(request)
    assert replay.outcome == result.outcome and len(provider.dispatches) == 3


@pytest.mark.parametrize(
    "disposition", [ProviderFailureDisposition.NONRETRYABLE, ProviderFailureDisposition.REPAIRABLE]
)
def test_nonretryable_provider_failure_has_terminal_audit_not_business_members(disposition):
    request, store, provider, _raw = _case(max_attempts=3, backoff=(0, 0))
    provider.emit_request_id = False
    provider.dispatch_results = [
        ProviderFailed(
            "PROVIDER_DENIED", '{"reason":"explicit provider failure"}', disposition=disposition
        )
    ]
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert result.outcome.state == "failed" and len(store.attempts) == 1
    assert result.attempt.failure_disposition == disposition.value
    assert store.record is None and not store.successes
    assert len(store.rejections) == 1 and set(store.receipt_attempts) == {result.attempt.attempt_id}


@pytest.mark.parametrize("kind", ["malformed", "tainted", "merge"])
def test_rejected_raw_draft_and_real_taint_preserve_raw_and_denial_audit(kind):
    request, store, provider, raw = _case(
        raw=b'{"malformed":true}' if kind == "malformed" else None,
        taint=kind == "tainted",
        merge=kind == "merge",
    )
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert result.outcome.state == "denied" and result.attempt.state == "failed"
    assert result.attempt.raw_response is not None
    assert store.read_immutable_blob(request.job, result.attempt.raw_response) == raw
    assert not store.successes and store.record is None
    detail = json.loads(result.outcome.failure_detail_json)
    assert len(detail["attempts"]) == 1
    if kind != "malformed":
        admission = CoverageAdmission.from_mapping(detail["attempts"][0]["failure_detail"])
        assert len(admission.rule_results) == 17 and admission.next_action != "continue"
        assert (
            next(item for item in admission.rule_results if item.rule_id == "KC-GATE-001").status
            == "fail"
        )
    replay = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert replay.outcome == result.outcome and len(provider.dispatches) == 1


def test_retry_then_semantic_denial_binds_every_failure_and_retains_final_raw():
    request, store, provider, _raw = _case(max_attempts=3, backoff=(0, 0))
    invalid_raw = b"not a valid draft"
    provider.emit_request_id = False
    provider.dispatch_results = [
        _retryable("FIRST_PROVIDER_FAILURE"),
        ProviderCompleted(invalid_raw, "response-second"),
    ]
    command = BuildNarrativeGraphCommand(store, provider)
    command.execute(request)
    result = command.execute(request)
    assert result.outcome.state == "denied" and len(store.attempts) == 2
    assert store.record is None and not store.successes
    detail = json.loads(result.outcome.failure_detail_json)
    assert [item["failure_code"] for item in detail["attempts"]] == [
        "FIRST_PROVIDER_FAILURE",
        "STAGE1_DRAFT_OR_COMPILATION_REJECTED",
    ]
    assert [item["failure_disposition"] for item in detail["attempts"]] == [
        "retryable",
        "repairable",
    ]
    assert store.read_immutable_blob(request.job, result.attempt.raw_response) == invalid_raw
    assert set(store.receipt_attempts.values()) == {result.outcome.receipt_id}
    assert set(store.receipt_attempts) == {attempt.attempt_id for attempt in store.attempts}


@pytest.mark.parametrize("taint", [False, True])
def test_crash_after_failure_before_receipt_keeps_denied_semantics(taint):
    request, store, provider, raw = _case(taint=taint, raw=None if taint else b"not-json")
    store.crash_after_failure = True
    with pytest.raises(SimulatedCrash):
        BuildNarrativeGraphCommand(store, provider).execute(request)
    assert store.attempts[-1].state == "failed" and store.outcome.state == "running"
    assert not store.rejections and store.record is None
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    assert result.outcome.state == "denied"
    assert store.read_immutable_blob(request.job, result.attempt.raw_response) == raw
    assert len(provider.dispatches) == len(store.attempts) == len(store.rejections) == 1


@pytest.mark.parametrize("change", ["prompt", "temperature", "coverage", "revision"])
def test_same_key_changed_frozen_intent_conflicts_before_provider(change):
    request, store, provider, _raw = _case()
    first = BuildNarrativeGraphCommand(store, provider).execute(request)
    if change == "prompt":
        changed = replace(
            request, generation=replace(request.generation, prompt_template="changed intent")
        )
    elif change == "temperature":
        changed = replace(request, generation=replace(request.generation, temperature="0.2"))
    elif change == "coverage":
        changed = replace(
            request, coverage_policy=replace(request.coverage_policy, minimum_confidence="0.6")
        )
    else:
        changed = replace(request, artifact_revision=2)
    with pytest.raises(IdempotencyConflictError):
        BuildNarrativeGraphCommand(store, provider).execute(changed)
    assert len(provider.dispatches) == len(store.successes) == 1
    assert store.record is first.committed.record


def test_frozen_provider_strategy_mismatch_cannot_dispatch_or_reconcile():
    request, store, provider, _raw = _case()
    provider.strategy_version = "foreign-strategy"
    with pytest.raises(ValueError, match="strategy"):
        BuildNarrativeGraphCommand(store, provider).execute(request)
    assert not provider.dispatches and not store.attempts


@pytest.mark.parametrize(
    "target",
    [
        "request_bytes",
        "raw_bytes",
        "request_ref",
        "raw_ref",
        "raw_media",
        "attempt_request",
        "attempt_provider_key",
        "attempt_backoff",
    ],
)
def test_exact_committed_reader_rejects_request_raw_and_attempt_identity_tampering(target):
    request, store, provider, raw = _case(backoff=(2,))
    if target == "attempt_backoff":
        provider.emit_request_id = False
        provider.dispatch_results = [_retryable(), ProviderCompleted(raw, "response-final")]
        reserved = BuildNarrativeGraphCommand(store, provider).execute(request)
        assert reserved.attempt.attempt_ordinal == 2
        store.advance(2)
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    dispatch_count = len(provider.dispatches)
    attempt = store.attempts[-1]
    if target in {"request_bytes", "raw_bytes"}:
        blob = attempt.request_payload if target == "request_bytes" else attempt.raw_response
        ref, owner, content = store.blobs[blob.object_id]
        store.blobs[blob.object_id] = (ref, owner, content + b" ")
    elif target == "request_ref":
        blob = store.put_immutable_blob(
            request.job,
            content=b"{}",
            content_hash=sha256_bytes(b"{}"),
            media_type="application/json",
        )
        store.attempts[-1] = replace(attempt, request_payload=blob)
    elif target == "raw_ref":
        # Same decoded draft but different raw byte identity must not match old
        # diagnostics/admission merely because canonical draft is unchanged.
        changed = json.dumps(json.loads(raw), indent=2).encode()
        blob = store.put_immutable_blob(
            request.job,
            content=changed,
            content_hash=sha256_bytes(changed),
            media_type="application/json",
        )
        store.attempts[-1] = replace(attempt, raw_response=blob)
    elif target == "raw_media":
        blob = store.put_immutable_blob(
            request.job, content=raw, content_hash=sha256_bytes(raw), media_type="text/plain"
        )
        store.attempts[-1] = replace(attempt, raw_response=blob)
    elif target == "attempt_request":
        store.attempts[-1] = replace(attempt, request_hash="sha256:" + "a" * 64)
    elif target == "attempt_backoff":
        assert attempt.attempt_ordinal == 2 and attempt.retry_backoff_seconds == 2
        store.attempts[-1] = replace(attempt, retry_backoff_seconds=3)
    else:
        store.attempts[-1] = replace(attempt, provider_idempotency_key="foreign-key")
    with pytest.raises((ValueError, BlobIntegrityError)):
        read_committed_narrative_graph(store, request, result.outcome)
    assert len(provider.dispatches) == dispatch_count


def test_reader_recomputes_real_checks_despite_hash_closed_forged_members_and_stored_pass(
    monkeypatch,
):
    from autocut_kernel.semantic_chain.dependency_proof import build_dependency_proof

    from tests.semantic_chain.test_coverage_verification import _rewrite

    request, store, provider, _raw = _case()
    result = BuildNarrativeGraphCommand(store, provider).execute(request)
    record = store.record
    six = _rewrite(
        record.artifacts[:6],
        "event_card_set",
        lambda payload: payload["events"][0].update(content="invented unsupported event"),
    )
    changed_event_id = json.loads(six[0].payload_json)["events"][0]["event_id"]

    def change_alias(payload):
        node = next(node for node in payload["nodes"] if node["node_id"] == changed_event_id)
        node["attributes"]["summary"] = "invented unsupported event"

    six = _rewrite(six, "narrative_graph", change_alias)
    proof = build_dependency_proof(
        store.inputs,
        graph_member=six[2],
        event_card_member=six[0],
        ledger_member=six[5],
        policy=request.dependency_policy,
        revision=request.artifact_revision,
    )
    business = (*six, proof)
    # Refresh every public content/subject hash, while deliberately retaining
    # old all-pass status values. This is not an accepted calibration factory.
    forged_admission = replace(
        result.committed.values.admission,
        business_members=tuple(
            SemanticMemberIdentity.from_artifact_member(member) for member in business
        ),
    )
    payload = canonical_json_bytes(forged_admission.to_mapping()).decode()
    admission_member = ArtifactMember(
        "coverage_admission",
        "coverage_admission",
        request.artifact_revision,
        request.artifact_scope,
        canonical_payload_hash(payload),
        payload,
    )
    artifacts = (*business, admission_member)
    decoded = decode_stage1_members(artifacts, scope=request.artifact_scope)
    assert all(item.status == "pass" for item in decoded.admission.rule_results)
    store.record = store._record(artifacts, record.receipt_id, record.artifact_set_id)
    monkeypatch.setattr(command_module, "compile_stage1_coverage", _forbid)
    monkeypatch.setattr(command_module, "build_dependency_proof", _forbid)
    with pytest.raises(ValueError, match="independent audited evaluation"):
        read_committed_narrative_graph(store, request, result.outcome)
    assert len(provider.dispatches) == 1
