"""Serial, journal-owned local measurements; never calibration acceptance.

The Protocol is deliberately separate from the complete-source journal. Its
implementation must verify source ownership before claim, enforce leases/CAS,
stage raw bytes and derived evidence atomically, and exclusively finalize the
two local artifacts. This module does not implement PostgreSQL transactions or
grant permission to install the expected pre-calibration service profile.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol, cast
from uuid import UUID

from ..media.local_speech_window_busy import decode_local_speech_window_busy_proof
from ..media.shadow_local_measurement import ShadowLocalMeasurementEvidence
from ..media.shadow_local_measurement_set import ShadowLocalMeasurementManifest
from ..media.shadow_local_service_profile import (
    ShadowLocalServiceProfile,
    decode_shadow_local_service_profile,
)
from ..store.models import (
    SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME,
    SHADOW_LOCAL_CALIBRATION_MEASUREMENT_PROTOCOL,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    Job,
    MaterializationLimits,
    ShadowLocalMeasurementAttempt,
    ShadowLocalMeasurementMember,
    ShadowLocalMeasurementMemberLease,
    ShadowLocalMeasurementMemberPlan,
    ShadowLocalMeasurementNotStartedProof,
    ShadowLocalMeasurementPlan,
    ShadowLocalMeasurementRecoveryLease,
    ShadowLocalMeasurementRetryAuthorization,
    ShadowLocalMeasurementStagedResponse,
    ShadowLocalMeasurementTerminalDenialRequest,
    ShadowLocalMeasurementTerminalDenialResult,
    VerifiedMaterializedBlob,
    canonical_payload_hash,
)
from .local_speech_window_port import (
    LocalSpeechWindowInvalidResponseError,
    LocalSpeechWindowPreDispatchBusyError,
    LocalSpeechWindowProducerPort,
    ReceivedLocalSpeechWindow,
)


class ShadowLocalCalibrationCommandError(ValueError):
    """Input/journal content fails closure; not a native rejection Receipt."""


def _json(value: object, *, media: bool = False) -> str:
    return json.dumps(value, ensure_ascii=media, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _object(value: object, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict:
        raise ShadowLocalCalibrationCommandError("expected a closed object")
    mapping = cast(dict[object, object], value)
    if any(type(key) is not str for key in mapping) or set(mapping) != fields:
        raise ShadowLocalCalibrationCommandError("object has missing or unknown fields")
    return cast(dict[str, object], value)


def _positive(value: object) -> int:
    if type(value) is not int or not 0 < value <= 9_007_199_254_740_991:
        raise ShadowLocalCalibrationCommandError("limit must be an exact positive safe integer")
    return value


def _text(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ShadowLocalCalibrationCommandError("expected nonempty text")
    value.encode("utf-8", errors="strict")
    return value


def _uuid(value: object) -> UUID:
    result = UUID(_text(value))
    if str(result) != value:
        raise ShadowLocalCalibrationCommandError("UUID must use canonical spelling")
    return result


def _blob_mapping(blob: BlobRef) -> dict[str, object]:
    return {"object_id": str(blob.object_id), "content_hash": blob.content_hash,
            "byte_length": blob.byte_length, "media_type": blob.media_type}


@dataclass(frozen=True, slots=True)
class ShadowLocalSourceBinding:
    """An exact original owner/reference, not proof that the owner succeeded."""

    source_job_id: UUID
    source_blob: BlobRef

    def __post_init__(self) -> None:
        if type(self.source_job_id) is not UUID or type(self.source_blob) is not BlobRef:
            raise ShadowLocalCalibrationCommandError("source requires exact UUID and BlobRef")
        if type(self.source_blob.object_id) is not UUID:
            raise ShadowLocalCalibrationCommandError("source BlobRef requires an exact UUID")
        _positive(self.source_blob.byte_length)
        _text(self.source_blob.media_type)

    @property
    def source_blob_reference_sha256(self) -> str:
        return canonical_payload_hash(_json({"source_job_id": str(self.source_job_id),
                                             "source_blob": _blob_mapping(self.source_blob)}))

    def to_mapping(self) -> dict[str, object]:
        return {"source_job_id": str(self.source_job_id), "source_blob": _blob_mapping(self.source_blob),
                "source_blob_reference_sha256": self.source_blob_reference_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> ShadowLocalSourceBinding:
        raw = _object(value, {"source_job_id", "source_blob", "source_blob_reference_sha256"})
        blob = _object(raw["source_blob"], {"object_id", "content_hash", "byte_length", "media_type"})
        result = cls(_uuid(raw["source_job_id"]), BlobRef(
            _uuid(blob["object_id"]), _text(blob["content_hash"]),
            _positive(blob["byte_length"]), _text(blob["media_type"]),
        ))
        if _json(raw) != _json(result.to_mapping()):
            raise ShadowLocalCalibrationCommandError("source reference identity drift")
        return result


@dataclass(frozen=True, slots=True)
class ShadowLocalMeasurementLimits:
    materialization: MaterializationLimits
    max_plan_bytes: int
    max_response_bytes: int
    max_total_response_bytes: int

    def __post_init__(self) -> None:
        if type(self.materialization) is not MaterializationLimits:
            raise ShadowLocalCalibrationCommandError("materialization limits must be explicit")
        for value in (self.max_plan_bytes, self.max_response_bytes, self.max_total_response_bytes,
                      self.materialization.max_source_bytes, self.materialization.timed_speech_max_request_bytes,
                      self.materialization.copy_chunk_bytes, self.materialization.staging_quota_bytes):
            _positive(value)
        if self.max_response_bytes > self.max_total_response_bytes:
            raise ShadowLocalCalibrationCommandError("individual response bound exceeds total budget")

    def to_mapping(self) -> dict[str, object]:
        transfer = self.materialization
        return {"materialization": {"max_source_bytes": transfer.max_source_bytes,
                "timed_speech_max_request_bytes": transfer.timed_speech_max_request_bytes,
                "copy_chunk_bytes": transfer.copy_chunk_bytes, "staging_quota_bytes": transfer.staging_quota_bytes},
                "max_plan_bytes": self.max_plan_bytes, "max_response_bytes": self.max_response_bytes,
                "max_total_response_bytes": self.max_total_response_bytes}

    @classmethod
    def from_mapping(cls, value: object) -> ShadowLocalMeasurementLimits:
        raw = _object(value, {"materialization", "max_plan_bytes", "max_response_bytes", "max_total_response_bytes"})
        transfer = _object(raw["materialization"], {"max_source_bytes", "timed_speech_max_request_bytes",
                                                   "copy_chunk_bytes", "staging_quota_bytes"})
        return cls(MaterializationLimits(
            _positive(transfer["max_source_bytes"]), _positive(transfer["timed_speech_max_request_bytes"]),
            _positive(transfer["copy_chunk_bytes"]), _positive(transfer["staging_quota_bytes"])),
            _positive(raw["max_plan_bytes"]), _positive(raw["max_response_bytes"]),
            _positive(raw["max_total_response_bytes"]))


@dataclass(frozen=True, slots=True)
class MeasureShadowLocalCalibrationRequest:
    service_profile: ShadowLocalServiceProfile
    manifest: ShadowLocalMeasurementManifest
    source_bindings: tuple[ShadowLocalSourceBinding, ...]
    limits: ShadowLocalMeasurementLimits
    max_attempt_count: int

    def __post_init__(self) -> None:
        if (type(self.service_profile) is not ShadowLocalServiceProfile
                or type(self.manifest) is not ShadowLocalMeasurementManifest
                or type(self.limits) is not ShadowLocalMeasurementLimits):
            raise ShadowLocalCalibrationCommandError("request requires exact local profile, manifest and limits")
        _positive(self.max_attempt_count)
        if (type(self.source_bindings) is not tuple
                or len(self.source_bindings) != len(self.manifest.members)
                or any(type(binding) is not ShadowLocalSourceBinding for binding in self.source_bindings)):
            raise ShadowLocalCalibrationCommandError("request requires ordered exact source bindings")
        profile = self.service_profile
        for member, binding in zip(self.manifest.members, self.source_bindings, strict=True):
            case, request = member.case, member.request
            source, blob = case.source, binding.source_blob
            if (source.blob_id, source.blob_sha256, source.blob_byte_length, source.blob_media_type) != (
                str(blob.object_id), blob.content_hash, blob.byte_length, blob.media_type
            ):
                raise ShadowLocalCalibrationCommandError("source binding differs from exact case")
            if (case.policy.service_profile_sha256 != profile.canonical_hash
                    or case.native_profile_identity_sha256 != profile.native_port_identity_sha256
                    or case.extraction.decoder_identity_sha256 != profile.decoder_identity_sha256
                    or case.producer_identities != profile.producers
                    or (case.policies.timed_speech_policy_sha256, case.policies.word_gap_policy_sha256,
                        case.policies.vad_merge_policy_sha256, case.policies.word_gap_ms, case.policies.vad_merge_gap_ms)
                    != (profile.timed_speech_policy_sha256, profile.word_gap_policy_sha256,
                        profile.vad_merge_policy_sha256, profile.utterance_gap_milliseconds,
                        profile.vad_merge_gap_milliseconds)):
                raise ShadowLocalCalibrationCommandError("case differs from complete expected local service profile")
            if (case.extraction.max_source_bytes > min(profile.max_request_bytes,
                    self.limits.materialization.effective_max_source_bytes)
                    or blob.byte_length > self.limits.materialization.staging_quota_bytes
                    or request.max_response_bytes > self.limits.max_response_bytes):
                raise ShadowLocalCalibrationCommandError("case exceeds frozen source/response limits")
        # Reserve all per-member ceilings up front; no valid final response may
        # discover that a prior member spent its share of the corpus budget.
        if sum(member.request.max_response_bytes for member in self.manifest.members) > self.limits.max_total_response_bytes:
            raise ShadowLocalCalibrationCommandError("corpus response reservations exceed total budget")
        if len(_json(self.canonical_payload()).encode("utf-8")) > self.limits.max_plan_bytes:
            raise ShadowLocalCalibrationCommandError("canonical plan exceeds explicit byte budget")

    def member_plans(self) -> tuple[ShadowLocalMeasurementMemberPlan, ...]:
        return tuple(ShadowLocalMeasurementMemberPlan(
            member.ordinal, member.case.canonical_hash, member.request.canonical_hash,
            _json(member.case.to_mapping(), media=True), _json(member.request.to_mapping(), media=True),
            binding.source_job_id, binding.source_blob, binding.source_blob_reference_sha256,
            member.request.binding_sha256, member.request.policy.service_profile_sha256,
            member.request.max_response_bytes,
        ) for member, binding in zip(self.manifest.members, self.source_bindings, strict=True))

    def canonical_payload(self) -> dict[str, object]:
        return {"command": SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME,
                "measurement_protocol": SHADOW_LOCAL_CALIBRATION_MEASUREMENT_PROTOCOL,
                "shadow_local_inputs": {"service_profile": self.service_profile.to_mapping(),
                    "manifest": self.manifest.to_mapping(),
                    "source_bindings": [binding.to_mapping() for binding in self.source_bindings],
                    "limits": self.limits.to_mapping(), "max_attempt_count": self.max_attempt_count},
                "corpus_members": [member.to_plan_mapping() for member in self.member_plans()]}

    @property
    def request_hash(self) -> str:
        return canonical_payload_hash(_json(self.canonical_payload()))

    @property
    def job(self) -> Job:
        return Job(f"shadow-local:{self.request_hash.removeprefix('sha256:')}", "shadow")

    @property
    def idempotency_key(self) -> str:
        return f"shadow-local-measurement:{self.request_hash.removeprefix('sha256:')}"

    def to_plan(self) -> ShadowLocalMeasurementPlan:
        return ShadowLocalMeasurementPlan(CommandClaim(self.job, self.idempotency_key,
            SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME, self.request_hash,
            execution_kind="deterministic"), _json(self.canonical_payload()), self.member_plans())

    @classmethod
    def from_mapping(cls, value: object) -> MeasureShadowLocalCalibrationRequest:
        raw = _object(value, {"command", "measurement_protocol", "shadow_local_inputs", "corpus_members"})
        inputs = _object(raw["shadow_local_inputs"], {"service_profile", "manifest", "source_bindings",
                                                     "limits", "max_attempt_count"})
        if type(inputs["source_bindings"]) is not list:
            raise ShadowLocalCalibrationCommandError("source bindings must be a wire array")
        request = cls(decode_shadow_local_service_profile(inputs["service_profile"]),
            ShadowLocalMeasurementManifest.from_mapping(inputs["manifest"]),
            tuple(ShadowLocalSourceBinding.from_mapping(item) for item in cast(list[object], inputs["source_bindings"])),
            ShadowLocalMeasurementLimits.from_mapping(inputs["limits"]), _positive(inputs["max_attempt_count"]))
        if _json(raw) != _json(request.canonical_payload()):
            raise ShadowLocalCalibrationCommandError("request differs from complete canonical reconstruction")
        return request


class ShadowLocalCalibrationMeasurementStore(Protocol):
    """Dedicated journal operations, not implemented by a generic Store fallback.

    Claim verifies all original succeeded source owners and exact BlobRefs.
    Lease acquisition must enforce serial prefix order, including across workers.
    Successor preserves staged rows, resumes untouched pending rows, and retries
    only the explicitly authorized unknown/not-started row within the plan limit.
    Finalization validates the entire journal and writes exactly the two frozen
    local artifacts and Receipt atomically; it must never accept caller artifacts.
    """

    def claim_or_read_shadow_local_measurement_attempt(self, plan: ShadowLocalMeasurementPlan) -> ShadowLocalMeasurementAttempt: ...
    def read_shadow_local_measurement_attempt(self, attempt_id: UUID) -> ShadowLocalMeasurementAttempt: ...
    def materialize_shadow_local_measurement_source(self, attempt_id: UUID, case_sha256: str, *, limits: MaterializationLimits) -> VerifiedMaterializedBlob: ...
    def acquire_shadow_local_measurement_member_lease(self, attempt_id: UUID, case_sha256: str, *, expected_version: int) -> ShadowLocalMeasurementMemberLease | None: ...
    def stage_shadow_local_measurement_member_response(self, attempt_id: UUID, case_sha256: str, *, expected_version: int, lease_token: str, staged: ShadowLocalMeasurementStagedResponse) -> ShadowLocalMeasurementAttempt: ...
    def stage_shadow_local_measurement_not_started(self, attempt_id: UUID, case_sha256: str, *, expected_version: int, lease_token: str, proof: ShadowLocalMeasurementNotStartedProof) -> ShadowLocalMeasurementAttempt: ...
    def acquire_shadow_local_measurement_recovery_lease(self, attempt_id: UUID, *, expected_version: int) -> ShadowLocalMeasurementRecoveryLease | None: ...
    def mark_shadow_local_measurement_member_indeterminate(self, attempt_id: UUID, case_sha256: str, *, expected_version: int, recovery_lease_token: str) -> ShadowLocalMeasurementAttempt: ...
    def reserve_shadow_local_measurement_successor(self, previous_attempt_id: UUID, authorization: ShadowLocalMeasurementRetryAuthorization) -> ShadowLocalMeasurementAttempt: ...
    def commit_shadow_local_measurement_terminal_denial(self, request: ShadowLocalMeasurementTerminalDenialRequest) -> ShadowLocalMeasurementTerminalDenialResult: ...
    def finalize_shadow_local_measurement_success(self, attempt_id: UUID, *, expected_version: int) -> CommandOutcome: ...
    def read_immutable_blob(self, job: Job, reference: BlobRef) -> bytes: ...


class MeasureShadowLocalCalibrationCommand:
    def __init__(self, store: ShadowLocalCalibrationMeasurementStore, port: LocalSpeechWindowProducerPort) -> None:
        self._store, self._port = store, port

    def execute(self, request: MeasureShadowLocalCalibrationRequest, *,
                retry_authorization: ShadowLocalMeasurementRetryAuthorization | None = None) -> CommandOutcome:
        if type(request) is not MeasureShadowLocalCalibrationRequest:
            raise ShadowLocalCalibrationCommandError("request must be the exact local measurement request")
        if retry_authorization is not None and type(retry_authorization) is not ShadowLocalMeasurementRetryAuthorization:
            raise ShadowLocalCalibrationCommandError("retry authorization must be exact")
        plan = request.to_plan()
        attempt = self._store.claim_or_read_shadow_local_measurement_attempt(plan)
        self._assert_attempt(plan, attempt)
        if attempt.outcome.state != "running":
            return attempt.outcome
        if retry_authorization is not None:
            if retry_authorization.predecessor_attempt_id == attempt.attempt_id:
                if attempt.state != "indeterminate":
                    raise ShadowLocalCalibrationCommandError("retry requires an exact indeterminate predecessor")
                predecessor = attempt
                self._assert_retry(request, predecessor, retry_authorization)
                attempt = self._store.reserve_shadow_local_measurement_successor(predecessor.attempt_id, retry_authorization)
                self._assert_attempt(plan, attempt)
            else:
                # The successor reservation may have committed before its caller
                # lost the acknowledgement. Replaying the same authorization
                # resumes that exact successor; it never creates another one.
                if attempt.previous_attempt_id != retry_authorization.predecessor_attempt_id:
                    raise ShadowLocalCalibrationCommandError("retry does not identify this successor")
                predecessor = self._store.read_shadow_local_measurement_attempt(retry_authorization.predecessor_attempt_id)
                self._assert_attempt(plan, predecessor)
                self._assert_retry(request, predecessor, retry_authorization)
            self._assert_successor(predecessor, attempt)
        if attempt.state == "indeterminate":
            return attempt.outcome
        for ordinal in range(len(plan.members)):
            self._assert_attempt(plan, attempt)
            if attempt.outcome.state != "running" or attempt.state == "indeterminate":
                return attempt.outcome
            member = attempt.members[ordinal]
            if member.state == "staged":
                self._replay_staged(request, attempt, member)
                continue
            if member.state == "invoking":
                return self._recover(attempt, member)
            if member.state != "pending":
                return attempt.outcome
            source = self._store.materialize_shadow_local_measurement_source(
                attempt.attempt_id, member.case_sha256, limits=request.limits.materialization)
            try:
                if source.reference != member.source_blob:
                    raise ShadowLocalCalibrationCommandError("materialization returned a foreign source")
                lease = self._store.acquire_shadow_local_measurement_member_lease(
                    attempt.attempt_id, member.case_sha256, expected_version=member.version)
                if lease is None:
                    return self._store.read_shadow_local_measurement_attempt(attempt.attempt_id).outcome
                self._assert_lease(member, lease)
                attempt = self._dispatch(request, attempt, lease, source)
            finally:
                source.close()
            # Unknown invocation stops this serial collector before any later
            # pending member. There is no exception-driven in-process retry.
            if attempt.members[ordinal].state != "staged":
                return attempt.outcome
        self._assert_attempt(plan, attempt)
        if attempt.state != "ready":
            raise ShadowLocalCalibrationCommandError("complete staged corpus did not become ready")
        # Never put stage/finalize inside a raw decoder rejection catch.
        return self._store.finalize_shadow_local_measurement_success(attempt.attempt_id, expected_version=attempt.version)

    @staticmethod
    def _assert_attempt(plan: ShadowLocalMeasurementPlan, attempt: ShadowLocalMeasurementAttempt) -> None:
        if (type(attempt) is not ShadowLocalMeasurementAttempt or attempt.job != plan.claim.job
                or attempt.plan_hash != plan.claim.request_hash or attempt.canonical_plan_json != plan.canonical_plan_json
                or len(attempt.members) != len(plan.members) or attempt.outcome.job_id is None):
            raise ShadowLocalCalibrationCommandError("journal does not bind the exact plan and Job")
        for expected, member in zip(plan.members, attempt.members, strict=True):
            actual = ShadowLocalMeasurementMemberPlan(member.member_ordinal, member.case_sha256,
                member.request_sha256, member.canonical_case_json, member.canonical_request_json,
                member.source_job_id, member.source_blob, member.source_blob_reference_sha256,
                member.binding_sha256, member.service_profile_sha256, member.max_response_bytes)
            if actual != expected:
                raise ShadowLocalCalibrationCommandError("journal member identity differs from immutable plan")
        if (attempt.state == "committed") != (attempt.outcome.state == "succeeded") or (
            (attempt.state == "denied") != (attempt.outcome.state == "denied")
        ) or attempt.outcome.state not in ("running", "succeeded", "denied"):
            raise ShadowLocalCalibrationCommandError("journal state contradicts command outcome")

    @staticmethod
    def _assert_lease(member: ShadowLocalMeasurementMember, lease: ShadowLocalMeasurementMemberLease) -> None:
        if (type(lease) is not ShadowLocalMeasurementMemberLease
                or lease.member.version != member.version + 1
                or lease.member.lease_expires_at is None
                or lease.member.lease_expires_at.tzinfo is None
                or replace(lease.member, state=member.state, version=member.version,
                           lease_expires_at=member.lease_expires_at) != member):
            raise ShadowLocalCalibrationCommandError("native lease does not bind the exact pending member")

    @staticmethod
    def _assert_successor(previous: ShadowLocalMeasurementAttempt, current: ShadowLocalMeasurementAttempt) -> None:
        if (current.previous_attempt_id != previous.attempt_id
                or current.attempt_ordinal != previous.attempt_ordinal + 1
                or current.command_slot_id != previous.command_slot_id
                or current.outcome.job_id != previous.outcome.job_id):
            raise ShadowLocalCalibrationCommandError("successor changes the exact predecessor slot/Job chain")
        for old, new in zip(previous.members, current.members, strict=True):
            if old.state in ("staged", "pending"):
                # A resumed successor may already have advanced a pending row,
                # but inherited staged bytes must remain byte-for-byte identical.
                if old.state == "staged" and (
                    new.state != "staged" or new.raw_blob != old.raw_blob or new.evidence_json != old.evidence_json
                ):
                    raise ShadowLocalCalibrationCommandError("successor rewrites or redispatches inherited evidence")

    @staticmethod
    def _assert_retry(request: MeasureShadowLocalCalibrationRequest, attempt: ShadowLocalMeasurementAttempt,
                      authorization: ShadowLocalMeasurementRetryAuthorization) -> None:
        eligible = tuple(member for member in attempt.members if member.state in ("indeterminate", "not_started"))
        if (attempt.attempt_ordinal >= request.max_attempt_count
                or authorization.predecessor_attempt_id != attempt.attempt_id
                or authorization.predecessor_version != attempt.version
                or authorization.predecessor_plan_hash != attempt.plan_hash
                or authorization.next_attempt_ordinal != attempt.attempt_ordinal + 1
                or len(eligible) != 1 or eligible[0].case_sha256 != authorization.member_case_sha256
                or any(member.state in ("invoking", "rejected") for member in attempt.members)):
            raise ShadowLocalCalibrationCommandError("retry does not bind one bounded exact predecessor")
        expected_reason = "REQUEST_NOT_STARTED" if eligible[0].state == "not_started" else "NATIVE_OUTCOME_UNKNOWN"
        if authorization.reason_code != expected_reason:
            raise ShadowLocalCalibrationCommandError("retry reason differs from durable member state")

    def _dispatch(self, request: MeasureShadowLocalCalibrationRequest, attempt: ShadowLocalMeasurementAttempt,
                  lease: ShadowLocalMeasurementMemberLease, source: VerifiedMaterializedBlob) -> ShadowLocalMeasurementAttempt:
        member = request.manifest.members[lease.member.member_ordinal]
        try:
            received = self._port.produce(source.path, member.request)
        except LocalSpeechWindowPreDispatchBusyError as error:
            # A foreign/malformed carrier is not evidence of invalid native
            # output, nor proof that this locked invocation was not dispatched.
            try:
                proof = decode_local_speech_window_busy_proof(error.raw_response, member.request)
            except (TypeError, ValueError):
                return self._store.read_shadow_local_measurement_attempt(attempt.attempt_id)
            return self._store.stage_shadow_local_measurement_not_started(attempt.attempt_id,
                member.case.canonical_hash, expected_version=lease.member.version, lease_token=lease.lease_token,
                proof=ShadowLocalMeasurementNotStartedProof(error.raw_response,
                    "sha256:" + hashlib.sha256(error.raw_response).hexdigest(), "application/json",
                    _json(proof.to_mapping(), media=True)))
        except LocalSpeechWindowInvalidResponseError as error:
            if (error.request_sha256 != member.request.canonical_hash
                    or _json(error.request.to_mapping(), media=True) != _json(member.request.to_mapping(), media=True)):
                return self._store.read_shadow_local_measurement_attempt(attempt.attempt_id)
            raw = error.raw_response
        except Exception:
            # Only the provider call is caught. No Store/decoder/finalizer
            # exception can be relabelled a native invalid-response denial.
            return self._store.read_shadow_local_measurement_attempt(attempt.attempt_id)
        else:
            if type(received) is not ReceivedLocalSpeechWindow:
                return self._store.read_shadow_local_measurement_attempt(attempt.attempt_id)
            raw = received.raw_response
        if type(raw) is not bytes or len(raw) > member.request.max_response_bytes:
            return self._store.read_shadow_local_measurement_attempt(attempt.attempt_id)
        try:
            measured = ShadowLocalMeasurementEvidence(member.case, member.request, raw)
        except ValueError:
            denial = self._store.commit_shadow_local_measurement_terminal_denial(
                ShadowLocalMeasurementTerminalDenialRequest(attempt.attempt_id, attempt.command_slot_id,
                    attempt.job, attempt.plan_hash, member.case.canonical_hash, lease.attempt_version,
                    lease.member.version, lease.lease_token, "SHADOW_LOCAL_CALIBRATION_INVALID_RAW",
                    _json({"reason": "independent_local_raw_replay_failed", "request_sha256": member.request.canonical_hash,
                           "raw_response_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                           "raw_response_byte_length": len(raw)})))
            return denial.attempt
        return self._store.stage_shadow_local_measurement_member_response(attempt.attempt_id,
            member.case.canonical_hash, expected_version=lease.member.version, lease_token=lease.lease_token,
            staged=ShadowLocalMeasurementStagedResponse(raw, measured.raw_response_sha256,
                "application/json", _json(measured.to_mapping(), media=True)))

    def _replay_staged(self, request: MeasureShadowLocalCalibrationRequest, attempt: ShadowLocalMeasurementAttempt,
                       member: ShadowLocalMeasurementMember) -> None:
        blob = member.raw_blob
        if blob is None or member.evidence_json is None or blob.byte_length > member.max_response_bytes or blob.media_type != "application/json":
            raise ShadowLocalCalibrationCommandError("staged response metadata does not close")
        raw = self._store.read_immutable_blob(attempt.job, blob)
        if (type(raw) is not bytes or len(raw) != blob.byte_length
                or "sha256:" + hashlib.sha256(raw).hexdigest() != blob.content_hash):
            raise ShadowLocalCalibrationCommandError("staged raw response identity drift")
        expected = request.manifest.members[member.member_ordinal]
        measured = ShadowLocalMeasurementEvidence(expected.case, expected.request, raw)
        if _json(measured.to_mapping(), media=True) != member.evidence_json:
            raise ShadowLocalCalibrationCommandError("staged evidence differs from independent raw replay")

    def _recover(self, attempt: ShadowLocalMeasurementAttempt, member: ShadowLocalMeasurementMember) -> CommandOutcome:
        if member.lease_expires_at is None or member.lease_expires_at > datetime.now(timezone.utc):
            return attempt.outcome
        recovery = self._store.acquire_shadow_local_measurement_recovery_lease(attempt.attempt_id, expected_version=attempt.version)
        if recovery is None:
            return self._store.read_shadow_local_measurement_attempt(attempt.attempt_id).outcome
        recovered = self._store.mark_shadow_local_measurement_member_indeterminate(attempt.attempt_id,
            member.case_sha256, expected_version=member.version, recovery_lease_token=recovery.lease_token)
        return recovered.outcome
