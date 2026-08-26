"""Real local codecs over synthetic raw responses and an explicit fake journal.

No PostgreSQL, transport, native model, calibration acceptance or installation.
The fake models atomic outcomes/CAS but is not evidence of database race safety.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from autocut_kernel.media.local_speech_window_busy import LocalSpeechWindowBusyProof
from autocut_kernel.media.local_speech_window_codec import decode_local_speech_window_response
from autocut_kernel.media.local_speech_window_projection import project_local_speech_window
from autocut_kernel.media.shadow_local_calibration import build_shadow_local_request
from autocut_kernel.media.shadow_local_measurement_set import (
    ShadowLocalMeasurementManifest,
    ShadowLocalMeasurementManifestMember,
    ShadowLocalMeasurementResults,
)
from autocut_kernel.media.shadow_local_service_profile import build_shadow_local_service_profile
from autocut_kernel.pipeline.local_speech_window_port import (
    LocalSpeechWindowInvalidResponseError,
    LocalSpeechWindowPreDispatchBusyError,
    ReceivedLocalSpeechWindow,
)
from autocut_kernel.pipeline.measure_shadow_local_calibration_command import (
    MeasureShadowLocalCalibrationCommand,
    MeasureShadowLocalCalibrationRequest,
    ShadowLocalCalibrationCommandError,
    ShadowLocalMeasurementLimits,
    ShadowLocalSourceBinding,
)
from autocut_kernel.store.models import (
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CommandOutcome,
    MaterializationLimits,
    ShadowLocalMeasurementAttempt,
    ShadowLocalMeasurementMember,
    ShadowLocalMeasurementMemberLease,
    ShadowLocalMeasurementNotStartedProof,
    ShadowLocalMeasurementPlan,
    ShadowLocalMeasurementRecoveryLease,
    ShadowLocalMeasurementRetryAuthorization,
    ShadowLocalMeasurementTerminalDenialResult,
    canonical_payload_hash,
)

from tests.media.test_shadow_local_calibration_projection import native_raw
from tests.media.test_shadow_local_measurement_set import measurement_set_case


def _json(value, *, media=False):
    return json.dumps(value, ensure_ascii=media, sort_keys=True, separators=(",", ":"))


def _hash(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def local_measurement_request():
    """Complete synthetic profile/corpus, never an installed or accepted source."""
    manifest, _, _ = measurement_set_case()
    base = manifest.members[0].case
    profile = build_shadow_local_service_profile({
        "schema_version": "funasr-shadow-local-calibration-profile-v1",
        "provider_id": "synthetic-shadow-local", "provider_version": "测试-1",
        "service_sha256": base.producer_identities[0].service_sha256,
        "funasr_version": "synthetic-funasr", "torch_version": "synthetic-torch",
        "device": "cpu", "word_timing_capability": "required", "max_request_bytes": 100_000,
        "timed_speech_policy_sha256": base.policies.timed_speech_policy_sha256,
        "word_gap_policy_sha256": base.policies.word_gap_policy_sha256,
        "vad_merge_policy_sha256": base.policies.vad_merge_policy_sha256,
        "utterance_gap_milliseconds": base.policies.word_gap_ms,
        "vad_merge_gap_milliseconds": base.policies.vad_merge_gap_ms,
        "decoder_identity_sha256": base.extraction.decoder_identity_sha256,
        "producers": [producer.to_mapping() for producer in base.producer_identities],
    })
    members = []
    for member in manifest.members:
        case = replace(member.case, source=replace(member.case.source, blob_byte_length=6),
                       policy=replace(member.case.policy, service_profile_sha256=profile.canonical_hash),
                       native_profile_identity_sha256=profile.native_port_identity_sha256)
        members.append(ShadowLocalMeasurementManifestMember(member.ordinal, case,
            build_shadow_local_request(case, max_response_bytes=100_000)))
    owner = UUID("11111111-1111-4111-8111-111111111111")
    blob = BlobRef(UUID(base.source.blob_id), base.source.blob_sha256, 6, base.source.blob_media_type)
    return MeasureShadowLocalCalibrationRequest(profile, ShadowLocalMeasurementManifest(tuple(members)),
        (ShadowLocalSourceBinding(owner, blob),) * 2,
        ShadowLocalMeasurementLimits(MaterializationLimits(100_000, 100_000, 4096, 100_000),
                                     100_000, 100_000, 200_000), 3)


class FakeSourceLease:
    def __init__(self, store, reference):
        self.store, self.reference = store, reference
        self.path = store.directory / f"synthetic-lease-{store.materializations}.mp4"
        self.path.write_bytes(b"source")
        assert _hash(self.path.read_bytes()) == reference.content_hash
        self.closed = False

    def close(self):
        assert not self.closed
        self.closed = True
        self.path.unlink()
        self.store.closed += 1


class FakeLocalJournal:
    def __init__(self, directory, request):
        self.directory = directory
        self.source_claims = {(item.source_job_id, item.source_blob) for item in request.source_bindings}
        self.attempts = {}
        self.current = None
        self.plan = None
        self.raw = {}
        self.tokens = {}
        self.materializations = self.closed = self.dispatch_leases = self.finalizations = self.denials = 0
        self.raw_reads = 0
        self.artifacts = ()
        self.stage_fault_before = self.stage_fault_after = self.finalize_fault_after = False
        self.refuse_lease = False

    def claim_or_read_shadow_local_measurement_attempt(self, plan):
        assert type(plan) is ShadowLocalMeasurementPlan
        assert all((member.source_job_id, member.source_blob) in self.source_claims for member in plan.members)
        if self.current is not None:
            assert self.plan == plan
            return self.attempts[self.current]
        self.plan = plan
        attempt_id = uuid4()
        members = tuple(ShadowLocalMeasurementMember(attempt_id, member.case_sha256, member.request_sha256,
            member.member_ordinal, member.canonical_case_json, member.canonical_request_json,
            member.source_job_id, member.source_blob, member.source_blob_reference_sha256,
            member.binding_sha256, member.service_profile_sha256, member.max_response_bytes, "pending", 0)
            for member in plan.members)
        outcome = CommandOutcome(uuid4(), "running", job_id=uuid4())
        attempt = ShadowLocalMeasurementAttempt(attempt_id, outcome.command_slot_id, plan.claim.job,
            plan.claim.request_hash, plan.canonical_plan_json, 1, None, "prepared", 0, members, outcome)
        self.current = attempt_id
        self.attempts[attempt_id] = attempt
        return attempt

    def read_shadow_local_measurement_attempt(self, attempt_id):
        return self.attempts[attempt_id]

    def materialize_shadow_local_measurement_source(self, attempt_id, case_sha256, *, limits):
        member = self._member(attempt_id, case_sha256)
        assert (member.source_job_id, member.source_blob) in self.source_claims
        assert member.source_blob.byte_length <= limits.effective_max_source_bytes
        self.materializations += 1
        return FakeSourceLease(self, member.source_blob)

    def _member(self, attempt_id, case_sha256):
        return next(member for member in self.attempts[attempt_id].members if member.case_sha256 == case_sha256)

    def _update(self, member, state):
        old = self.attempts[member.attempt_id]
        members = tuple(member if item.case_sha256 == member.case_sha256 else item for item in old.members)
        result = replace(old, members=members, state=state, version=old.version + 1)
        self.attempts[member.attempt_id] = result
        return result

    def acquire_shadow_local_measurement_member_lease(self, attempt_id, case_sha256, *, expected_version):
        member = self._member(attempt_id, case_sha256)
        if self.refuse_lease or member.state != "pending" or member.version != expected_version:
            return None
        assert all(item.state == "staged" for item in self.attempts[attempt_id].members[:member.member_ordinal])
        member = replace(member, state="invoking", version=member.version + 1,
                         lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1))
        attempt = self._update(member, "collecting")
        token = str(uuid4())
        self.tokens[(attempt_id, case_sha256)] = token
        self.dispatch_leases += 1
        return ShadowLocalMeasurementMemberLease(member, attempt.version, token)

    def _leased(self, attempt_id, case_sha256, version, token):
        member = self._member(attempt_id, case_sha256)
        assert member.state == "invoking" and member.version == version
        assert self.tokens[(attempt_id, case_sha256)] == token
        return member

    def _blob(self, job, raw, media_type):
        reference = BlobRef(uuid4(), _hash(raw), len(raw), media_type)
        self.raw[(job, reference)] = raw
        return reference

    def stage_shadow_local_measurement_member_response(self, attempt_id, case_sha256, *, expected_version, lease_token, staged):
        if self.stage_fault_before:
            self.stage_fault_before = False
            raise ValueError("synthetic stage outcome unknown before commit")
        member = self._leased(attempt_id, case_sha256, expected_version, lease_token)
        blob = self._blob(self.attempts[attempt_id].job, staged.raw_bytes, staged.media_type)
        member = replace(member, state="staged", version=member.version + 1,
                         raw_blob=blob, evidence_json=staged.evidence_json, lease_expires_at=None)
        attempt = self._update(member, "collecting")
        if all(item.state == "staged" for item in attempt.members):
            attempt = replace(attempt, state="ready")
            self.attempts[attempt_id] = attempt
        if self.stage_fault_after:
            self.stage_fault_after = False
            raise ValueError("synthetic stage outcome unknown after commit")
        return attempt

    def stage_shadow_local_measurement_not_started(self, attempt_id, case_sha256, *, expected_version, lease_token, proof):
        assert type(proof) is ShadowLocalMeasurementNotStartedProof
        member = self._leased(attempt_id, case_sha256, expected_version, lease_token)
        blob = self._blob(self.attempts[attempt_id].job, proof.raw_bytes, proof.media_type)
        return self._update(replace(member, state="not_started", version=member.version + 1,
            busy_proof_blob=blob, busy_proof_json=proof.proof_json, lease_expires_at=None), "indeterminate")

    def acquire_shadow_local_measurement_recovery_lease(self, attempt_id, *, expected_version):
        attempt = self.attempts[attempt_id]
        assert attempt.version == expected_version
        attempt = replace(attempt, version=attempt.version + 1)
        self.attempts[attempt_id] = attempt
        return ShadowLocalMeasurementRecoveryLease(attempt, "fake-recovery-token")

    def mark_shadow_local_measurement_member_indeterminate(self, attempt_id, case_sha256, *, expected_version, recovery_lease_token):
        assert recovery_lease_token == "fake-recovery-token"
        member = self._member(attempt_id, case_sha256)
        assert member.state == "invoking" and member.version == expected_version
        assert member.lease_expires_at < datetime.now(timezone.utc)
        return self._update(replace(member, state="indeterminate", version=member.version + 1,
                                    lease_expires_at=None), "indeterminate")

    def expire(self):
        attempt = self.attempts[self.current]
        members = tuple(replace(member, lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
                        if member.state == "invoking" else member for member in attempt.members)
        self.attempts[self.current] = replace(attempt, members=members)

    def reserve_shadow_local_measurement_successor(self, previous_attempt_id, authorization):
        previous = self.attempts[previous_attempt_id]
        assert authorization.predecessor_version == previous.version
        assert previous.state == "indeterminate" and previous.outcome.state == "running"
        attempt_id = uuid4()
        members = []
        for member in previous.members:
            state = member.state
            if state in ("indeterminate", "not_started"):
                assert member.case_sha256 == authorization.member_case_sha256
                state = "pending"
            members.append(replace(member, attempt_id=attempt_id, state=state, version=0,
                                   busy_proof_blob=None, busy_proof_json=None))
        attempt = replace(previous, attempt_id=attempt_id, previous_attempt_id=previous.attempt_id,
            attempt_ordinal=authorization.next_attempt_ordinal, state="prepared", version=0, members=tuple(members))
        self.current = attempt_id
        self.attempts[attempt_id] = attempt
        return attempt

    def commit_shadow_local_measurement_terminal_denial(self, request):
        old = self.attempts[request.attempt_id]
        member = self._leased(request.attempt_id, request.member_case_sha256,
                              request.expected_member_version, request.member_lease_token)
        assert old.version == request.expected_attempt_version
        assert all(item.state not in ("invoking", "indeterminate") for item in old.members if item != member)
        attempt = self._update(replace(member, state="rejected", version=member.version + 1,
                                      lease_expires_at=None), "denied")
        outcome = replace(old.outcome, state="denied", receipt_id=uuid4())
        attempt = replace(attempt, outcome=outcome)
        self.attempts[request.attempt_id] = attempt
        self.denials += 1
        return ShadowLocalMeasurementTerminalDenialResult(attempt, outcome)

    def read_immutable_blob(self, job, reference):
        self.raw_reads += 1
        return self.raw[(job, reference)]

    def finalize_shadow_local_measurement_success(self, attempt_id, *, expected_version):
        attempt = self.attempts[attempt_id]
        assert attempt.state == "ready" and attempt.version == expected_version
        assert all(member.state == "staged" for member in attempt.members)
        # Test-only stand-in for the dedicated atomic writer. This is not a
        # production artifact compiler or an independent database acceptance.
        request = MeasureShadowLocalCalibrationRequest.from_mapping(json.loads(attempt.canonical_plan_json))
        result_mapping = {"schema_version": "shadow-local-measurement-results-v1",
            "manifest_sha256": request.manifest.canonical_hash,
            "members": [{"ordinal": item.member_ordinal, "case_sha256": item.case_sha256,
                         "request_sha256": item.request_sha256, "evidence": json.loads(item.evidence_json)}
                        for item in attempt.members]}
        result = ShadowLocalMeasurementResults.from_mapping(result_mapping, manifest=request.manifest,
            raw_responses={(item.member_ordinal, item.case_sha256): self.raw[(attempt.job, item.raw_blob)]
                           for item in attempt.members})
        summary = attempt.plan_hash.removeprefix("sha256:")
        scope = ArtifactScope("autocut_calibration", "shadow_local_run", summary)

        def artifact(suffix, payload):
            encoded = _json(payload)
            return ArtifactMember(f"shadow_local_measurement_{suffix}",
                f"shadow-local-measurement:{summary}:{suffix}", 1, scope, canonical_payload_hash(encoded), encoded)

        manifest = artifact("manifest", {"schema_version": "shadow-local-durable-measurement-manifest-v1",
            "measurement_request_sha256": attempt.plan_hash, "request": request.canonical_payload()})
        results = artifact("results", {"schema_version": "shadow-local-durable-measurement-results-v1",
            "manifest_artifact_sha256": manifest.content_hash, "results": result.to_mapping(),
            "raw_responses": [{"ordinal": item.member_ordinal, "case_sha256": item.case_sha256,
                "request_sha256": item.request_sha256, "blob": {"object_id": str(item.raw_blob.object_id),
                    "content_hash": item.raw_blob.content_hash, "byte_length": item.raw_blob.byte_length,
                    "media_type": item.raw_blob.media_type}} for item in attempt.members]})
        self.artifacts = (manifest, results)
        outcome = replace(attempt.outcome, state="succeeded", receipt_id=uuid4(), artifact_set_id=uuid4())
        self.attempts[attempt_id] = replace(attempt, state="committed", outcome=outcome, version=attempt.version + 1)
        self.finalizations += 1
        if self.finalize_fault_after:
            self.finalize_fault_after = False
            raise ValueError("synthetic finalization committed but acknowledgement lost")
        return outcome


class FakeWindowPort:
    def __init__(self, actions=()):
        self.actions = list(actions)
        self.calls = []

    def produce(self, source_path, request):
        assert source_path.read_bytes() == b"source"
        self.calls.append(request.canonical_hash)
        action = self.actions.pop(0) if self.actions else "valid"
        if action == "unknown":
            raise TimeoutError("synthetic native outcome unknown")
        if action in ("busy", "foreign_busy"):
            locked = request if action == "busy" else replace(request, binding_sha256=_hash(b"foreign"))
            proof = LocalSpeechWindowBusyProof(locked.canonical_hash, locked.binding_sha256,
                                               locked.policy.service_profile_sha256)
            raise LocalSpeechWindowPreDispatchBusyError(proof, proof.to_bytes())
        raw = native_raw(request)
        if action in ("invalid", "empty", "valid_carrier", "foreign_carrier"):
            if action == "invalid":
                raw = b"{ invalid raw response }"
            elif action == "empty":
                raw = b""
            locked = request if action != "foreign_carrier" else replace(request, binding_sha256=_hash(b"foreign"))
            raise LocalSpeechWindowInvalidResponseError(locked, raw)
        return ReceivedLocalSpeechWindow(project_local_speech_window(
            decode_local_speech_window_response(raw, request)), raw)


def _setup(tmp_path, actions=()):
    request = local_measurement_request()
    store = FakeLocalJournal(tmp_path, request)
    port = FakeWindowPort(actions)
    return request, store, port, MeasureShadowLocalCalibrationCommand(store, port)


def _authorization(attempt):
    member = next(item for item in attempt.members if item.state in ("indeterminate", "not_started"))
    return ShadowLocalMeasurementRetryAuthorization(_hash(b"explicit-test-only-retry-decision"), attempt.plan_hash,
        attempt.attempt_id, attempt.version, member.case_sha256, attempt.attempt_ordinal + 1,
        "REQUEST_NOT_STARTED" if member.state == "not_started" else "NATIVE_OUTCOME_UNKNOWN")


def test_closed_request_profile_source_and_unicode_hash_roundtrip():
    request = local_measurement_request()
    assert MeasureShadowLocalCalibrationRequest.from_mapping(request.canonical_payload()) == request
    plan = request.to_plan()
    assert plan.claim.request_hash == canonical_payload_hash(plan.canonical_plan_json)
    assert "源一" in plan.canonical_plan_json and "\\u6e90" in plan.members[0].canonical_case_json
    assert request.request_hash != _hash(_json(request.canonical_payload(), media=True).encode())
    assert request.manifest.members[0].case.extraction.time_base != request.manifest.members[1].case.extraction.time_base


@pytest.mark.parametrize("mutation", ["extra", "bool", "float", "source_hash", "source_ref", "member_order", "profile", "budget"])
def test_request_rejects_closed_identity_and_budget_drift(mutation):
    request = local_measurement_request()
    wire = request.canonical_payload()
    if mutation == "extra":
        wire["accepted"] = True
    elif mutation in ("bool", "float"):
        wire["corpus_members"][0]["ordinal"] = False if mutation == "bool" else 0.0
    elif mutation == "source_hash":
        wire["shadow_local_inputs"]["source_bindings"][0]["source_blob"]["content_hash"] = _hash(b"foreign")
    elif mutation == "source_ref":
        wire["shadow_local_inputs"]["source_bindings"][0]["source_blob_reference_sha256"] = _hash(b"foreign")
    elif mutation == "member_order":
        wire["corpus_members"].reverse()
    elif mutation == "profile":
        wire["shadow_local_inputs"]["service_profile"]["decoder_identity_sha256"] = _hash(b"foreign")
    else:
        wire["shadow_local_inputs"]["limits"]["max_total_response_bytes"] = 100_000
    with pytest.raises(ValueError):
        MeasureShadowLocalCalibrationRequest.from_mapping(wire)


def test_direct_policy_mismatch_and_plan_overflow_fail_before_claim():
    request = local_measurement_request()
    with pytest.raises(ValueError, match="profile"):
        replace(request, service_profile=replace(request.service_profile, device="different-device"))
    with pytest.raises(ValueError, match="plan"):
        replace(request, limits=replace(request.limits, max_plan_bytes=1))
    with pytest.raises(ValueError):
        replace(request, max_attempt_count=True)
    with pytest.raises(ValueError):
        replace(request, source_bindings=list(request.source_bindings))


def test_normal_two_clock_corpus_and_terminal_replay_never_redispatch(tmp_path):
    request, store, port, command = _setup(tmp_path)
    outcome = command.execute(request)
    assert outcome.state == "succeeded" and store.finalizations == 1
    assert len(port.calls) == store.materializations == store.closed == 2
    assert command.execute(request) == outcome
    assert len(port.calls) == 2 and store.finalizations == 1
    assert len(store.artifacts) == 2
    manifest, results = (json.loads(item.payload_json) for item in store.artifacts)
    assert manifest["measurement_request_sha256"] == request.request_hash
    assert results["manifest_artifact_sha256"] == store.artifacts[0].content_hash
    assert results["results"]["manifest_sha256"] == request.manifest.canonical_hash
    assert [item["ordinal"] for item in results["raw_responses"]] == [0, 1]
    assert "accepted_bound" not in _json(results)


@pytest.mark.parametrize("action", ["invalid", "empty"])
def test_invalid_second_response_denies_after_staged_first_without_set(tmp_path, action):
    request, store, port, command = _setup(tmp_path, ("valid", action))
    outcome = command.execute(request)
    assert outcome.state == "denied" and outcome.artifact_set_id is None
    assert [item.state for item in store.attempts[store.current].members] == ["staged", "rejected"]
    assert len(store.raw) == 1 and not store.artifacts and store.denials == 1
    assert command.execute(request) == outcome and len(port.calls) == 2
    assert store.closed == 2


def test_invalid_carrier_containing_valid_raw_stages_not_denies(tmp_path):
    request, store, port, command = _setup(tmp_path, ("valid_carrier", "valid_carrier"))
    assert command.execute(request).state == "succeeded"
    assert len(port.calls) == 2 and store.denials == 0


@pytest.mark.parametrize("action", ["unknown", "foreign_busy", "foreign_carrier"])
def test_unknown_stops_serial_dispatch_and_expired_recovery_never_retries(tmp_path, action):
    request, store, port, command = _setup(tmp_path, (action,))
    assert command.execute(request).state == "running"
    assert [item.state for item in store.attempts[store.current].members] == ["invoking", "pending"]
    assert command.execute(request).state == "running" and len(port.calls) == 1
    store.expire()
    assert command.execute(request).state == "running"
    assert store.attempts[store.current].state == "indeterminate"
    assert command.execute(request).state == "running" and len(port.calls) == 1
    assert store.denials == 0 and store.closed == 1


def test_explicit_unknown_successor_continues_later_pending_under_same_slot(tmp_path):
    request, store, port, command = _setup(tmp_path, ("unknown",))
    command.execute(request)
    store.expire()
    command.execute(request)
    predecessor = store.attempts[store.current]
    outcome = command.execute(request, retry_authorization=_authorization(predecessor))
    assert outcome.state == "succeeded" and outcome.command_slot_id == predecessor.command_slot_id
    assert len(port.calls) == 3 and port.calls[0] == port.calls[1] != port.calls[2]
    assert store.attempts[store.current].previous_attempt_id == predecessor.attempt_id


def test_busy_persists_exact_proof_and_successor_inherits_staged_without_redispatch(tmp_path):
    request, store, port, command = _setup(tmp_path, ("valid", "busy"))
    assert command.execute(request).state == "running"
    predecessor = store.attempts[store.current]
    assert [item.state for item in predecessor.members] == ["staged", "not_started"]
    busy = predecessor.members[1]
    assert store.raw[(predecessor.job, busy.busy_proof_blob)] == busy.busy_proof_json.encode()
    assert command.execute(request).state == "running" and len(port.calls) == 2
    assert command.execute(request, retry_authorization=_authorization(predecessor)).state == "succeeded"
    assert port.calls == [request.manifest.members[0].request.canonical_hash,
                          request.manifest.members[1].request.canonical_hash,
                          request.manifest.members[1].request.canonical_hash]
    assert store.attempts[store.current].members[0].raw_blob == predecessor.members[0].raw_blob
    assert store.raw_reads >= 1 and store.denials == 0


@pytest.mark.parametrize("mutation", ["version", "member", "reason", "ordinal", "budget"])
def test_retry_must_bind_exact_authorized_predecessor_and_budget(tmp_path, mutation):
    request, store, port, command = _setup(tmp_path, ("busy",))
    if mutation == "budget":
        request = replace(request, max_attempt_count=1)
    command.execute(request)
    authorization = _authorization(store.attempts[store.current])
    if mutation == "version":
        authorization = replace(authorization, predecessor_version=0)
    elif mutation == "member":
        authorization = replace(authorization, member_case_sha256=request.manifest.members[1].case.canonical_hash)
    elif mutation == "reason":
        authorization = replace(authorization, reason_code="NATIVE_OUTCOME_UNKNOWN")
    elif mutation == "ordinal":
        authorization = replace(authorization, next_attempt_ordinal=3)
    with pytest.raises(ShadowLocalCalibrationCommandError, match="retry"):
        command.execute(request, retry_authorization=authorization)
    assert len(port.calls) == 1 and len(store.attempts) == 1


@pytest.mark.parametrize("when", ["before", "after"])
def test_stage_commit_uncertainty_never_becomes_denial_or_duplicate_dispatch(tmp_path, when):
    request, store, port, command = _setup(tmp_path)
    setattr(store, f"stage_fault_{when}", True)
    with pytest.raises(ValueError, match="stage outcome unknown"):
        command.execute(request)
    assert store.denials == 0 and len(port.calls) == 1 and store.closed == 1
    outcome = command.execute(request)
    if when == "after":
        assert outcome.state == "succeeded" and len(port.calls) == 2 and store.raw_reads == 1
    else:
        assert outcome.state == "running" and len(port.calls) == 1


def test_uncertain_success_commit_replays_exact_terminal_without_denial(tmp_path):
    request, store, port, command = _setup(tmp_path)
    store.finalize_fault_after = True
    with pytest.raises(ValueError, match="acknowledgement lost"):
        command.execute(request)
    assert command.execute(request).state == "succeeded"
    assert store.finalizations == 1 and store.denials == 0 and len(port.calls) == 2


def test_lost_lease_and_foreign_source_owner_make_zero_native_calls(tmp_path):
    request, store, port, command = _setup(tmp_path)
    store.refuse_lease = True
    assert command.execute(request).state == "running" and not port.calls
    assert store.closed == 1
    store.source_claims.clear()
    with pytest.raises(AssertionError):
        command.execute(request)
    assert not port.calls


def test_staged_projection_tamper_is_not_rewritten_as_native_denial(tmp_path):
    request, store, port, command = _setup(tmp_path)
    store.stage_fault_after = True
    with pytest.raises(ValueError):
        command.execute(request)
    attempt = store.attempts[store.current]
    member = attempt.members[0]
    wire = json.loads(member.evidence_json)
    wire["projection"]["asr_matches"][0]["absolute_tick"] = 99
    store.attempts[store.current] = replace(attempt, members=(replace(member,
        evidence_json=_json(wire, media=True)), attempt.members[1]))
    with pytest.raises(ShadowLocalCalibrationCommandError, match="independent raw replay"):
        command.execute(request)
    assert store.denials == 0 and len(port.calls) == 1


@pytest.mark.parametrize("mutation", ["slot", "job", "inherited_blob"])
def test_successor_cannot_change_slot_job_or_inherited_staged_identity(tmp_path, monkeypatch, mutation):
    request, store, port, command = _setup(tmp_path, ("valid", "busy"))
    command.execute(request)
    predecessor = store.attempts[store.current]
    reserve = store.reserve_shadow_local_measurement_successor

    def substituted(previous_attempt_id, authorization):
        successor = reserve(previous_attempt_id, authorization)
        if mutation == "slot":
            slot = uuid4()
            return replace(successor, command_slot_id=slot,
                           outcome=replace(successor.outcome, command_slot_id=slot))
        if mutation == "job":
            return replace(successor, outcome=replace(successor.outcome, job_id=uuid4()))
        first = successor.members[0]
        return replace(successor, members=(replace(first,
            raw_blob=replace(first.raw_blob, object_id=uuid4())), successor.members[1]))

    monkeypatch.setattr(store, "reserve_shadow_local_measurement_successor", substituted)
    with pytest.raises(ShadowLocalCalibrationCommandError, match="successor"):
        command.execute(request, retry_authorization=_authorization(predecessor))
    assert len(port.calls) == 2 and store.denials == 0


def test_foreign_lease_member_ordinal_fails_before_native(tmp_path, monkeypatch):
    request, store, port, command = _setup(tmp_path)
    acquire = store.acquire_shadow_local_measurement_member_lease

    def substituted(attempt_id, case_sha256, *, expected_version):
        lease = acquire(attempt_id, case_sha256, expected_version=expected_version)
        return replace(lease, member=replace(lease.member, member_ordinal=1))

    monkeypatch.setattr(store, "acquire_shadow_local_measurement_member_lease", substituted)
    with pytest.raises(ShadowLocalCalibrationCommandError, match="native lease"):
        command.execute(request)
    assert not port.calls and store.closed == 1


def test_uncertain_denial_commit_propagates_without_fabricating_receipt(tmp_path, monkeypatch):
    request, store, port, command = _setup(tmp_path, ("invalid",))

    def uncertain(_request):
        raise ValueError("synthetic denial commit outcome unknown")

    monkeypatch.setattr(store, "commit_shadow_local_measurement_terminal_denial", uncertain)
    with pytest.raises(ValueError, match="denial commit outcome unknown"):
        command.execute(request)
    assert store.attempts[store.current].outcome.state == "running"
    assert not store.artifacts and len(port.calls) == 1 and store.closed == 1


def test_lost_successor_acknowledgement_replays_same_authorization_without_new_attempt(tmp_path, monkeypatch):
    request, store, port, command = _setup(tmp_path, ("busy",))
    command.execute(request)
    authorization = _authorization(store.attempts[store.current])
    reserve = store.reserve_shadow_local_measurement_successor

    def lost_ack(previous_attempt_id, decision):
        reserve(previous_attempt_id, decision)
        raise ValueError("synthetic successor acknowledgement lost")

    monkeypatch.setattr(store, "reserve_shadow_local_measurement_successor", lost_ack)
    with pytest.raises(ValueError, match="successor acknowledgement lost"):
        command.execute(request, retry_authorization=authorization)
    assert len(store.attempts) == 2 and len(port.calls) == 1
    outcome = command.execute(request, retry_authorization=authorization)
    assert outcome.state == "succeeded" and len(store.attempts) == 2
    assert len(port.calls) == 3 and store.denials == 0
