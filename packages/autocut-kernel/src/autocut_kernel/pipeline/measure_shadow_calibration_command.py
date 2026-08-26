"""Durable shadow-calibration measurement over decoder-verified native evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import UUID

from ..media import (
    CalibrationAnchor,
    CalibrationAnchorMatch,
    CalibrationObservation,
    CalibrationRecordError,
    ProducerCalibrationMeasurement,
    ShadowCalibrationInvocation,
    ShadowCalibrationProjection,
    ShadowCalibrationRawBlob,
    ShadowCalibrationRawContext,
    ShadowCalibrationRawEvidenceError,
    TimeBase,
    decode_shadow_calibration_raw_response,
)
from ..media.types import TickRange, canonical_sha256, sha256_prefixed
from ..store import (
    CommandClaim,
    CommandOutcome,
    Job,
    ShadowMeasurementAttempt,
    ShadowMeasurementMemberLease,
    ShadowMeasurementMemberPlan,
    ShadowMeasurementPlan,
    ShadowMeasurementRecoveryLease,
    ShadowMeasurementRetryAuthorization,
    ShadowMeasurementStagedResponse,
    ShadowMeasurementTerminalDenialRequest,
    ShadowMeasurementTerminalDenialResult,
)

MEASURE_SHADOW_CALIBRATION_COMMAND = "MeasureShadowCalibrationCommand@2.1.3"
SHADOW_CALIBRATION_MEASUREMENT_PROTOCOL = "shadow-calibration-measurement-v1"


class ShadowCalibrationCommandError(ValueError):
    """Typed shadow-calibration evidence does not close over the locked request."""


class ShadowCalibrationStoreError(RuntimeError):
    """The Store cannot prove that its BlobRef binds the supplied raw evidence."""


class ShadowCalibrationProducerFailureCode(str, Enum):
    REJECTED = "SHADOW_CALIBRATION_NATIVE_REJECTED"
    UNAVAILABLE = "SHADOW_CALIBRATION_NATIVE_UNAVAILABLE"


class ShadowCalibrationProducerError(RuntimeError):
    def __init__(self, code: ShadowCalibrationProducerFailureCode) -> None:
        if type(code) is not ShadowCalibrationProducerFailureCode:  # noqa: E721
            raise ValueError("producer failure code must be exact")
        super().__init__(code.value)
        self.code = code


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise ShadowCalibrationCommandError(f"{field_name} must be non-empty text")
    return value


def _sha(value: object, field_name: str) -> str:
    try:
        return sha256_prefixed(value, field_name)
    except ValueError as error:
        raise ShadowCalibrationCommandError(str(error)) from error


@dataclass(frozen=True, slots=True)
class ShadowCalibrationInputs:
    profile_source_sha256: str
    registry_snapshot_sha256: str
    calibration_corpus_set_sha256: str
    native_port_identity_sha256: str
    word_gap_policy_sha256: str
    vad_merge_policy_sha256: str
    alignment_policy_sha256: str
    acceptance_policy_sha256: str
    asr_producer_id: str
    vad_producer_id: str
    source_clock_id: str
    source_time_base: TimeBase

    def __post_init__(self) -> None:
        for name in (
            "profile_source_sha256",
            "registry_snapshot_sha256",
            "calibration_corpus_set_sha256",
            "native_port_identity_sha256",
            "word_gap_policy_sha256",
            "vad_merge_policy_sha256",
            "alignment_policy_sha256",
            "acceptance_policy_sha256",
        ):
            _sha(getattr(self, name), f"shadow_inputs.{name}")
        _text(self.asr_producer_id, "shadow_inputs.asr_producer_id")
        _text(self.vad_producer_id, "shadow_inputs.vad_producer_id")
        if self.asr_producer_id == self.vad_producer_id:
            raise ShadowCalibrationCommandError(
                "locked inputs require distinct ASR and VAD producers"
            )
        _text(self.source_clock_id, "shadow_inputs.source_clock_id")
        if type(self.source_time_base) is not TimeBase:  # noqa: E721
            raise ShadowCalibrationCommandError("shadow_inputs.source_time_base must be exact")

    def to_mapping(self) -> dict[str, object]:
        return {
            "acceptance_policy_sha256": self.acceptance_policy_sha256,
            "alignment_policy_sha256": self.alignment_policy_sha256,
            "asr_producer_id": self.asr_producer_id,
            "calibration_corpus_set_sha256": self.calibration_corpus_set_sha256,
            "native_port_identity_sha256": self.native_port_identity_sha256,
            "profile_source_sha256": self.profile_source_sha256,
            "registry_snapshot_sha256": self.registry_snapshot_sha256,
            "source_clock_id": self.source_clock_id,
            "source_time_base": _time_base_mapping(self.source_time_base),
            "vad_merge_policy_sha256": self.vad_merge_policy_sha256,
            "vad_producer_id": self.vad_producer_id,
            "word_gap_policy_sha256": self.word_gap_policy_sha256,
        }


@dataclass(frozen=True, slots=True)
class ShadowCalibrationCorpusMember:
    """One locked source member, invocation and independent anchor context."""

    corpus_member_reference_sha256: str
    expected_anchor_reference_sha256: str
    raw_context: ShadowCalibrationRawContext
    native_invocation: ShadowCalibrationInvocation

    def __post_init__(self) -> None:
        _sha(self.corpus_member_reference_sha256, "corpus_member.corpus_member_reference_sha256")
        _sha(
            self.expected_anchor_reference_sha256, "corpus_member.expected_anchor_reference_sha256"
        )
        if type(self.raw_context) is not ShadowCalibrationRawContext:  # noqa: E721
            raise ShadowCalibrationCommandError("corpus_member.raw_context must be exact")
        if type(self.native_invocation) is not ShadowCalibrationInvocation:  # noqa: E721
            raise ShadowCalibrationCommandError("corpus_member.native_invocation must be exact")
        if (
            self.raw_context.source.corpus_member_reference_sha256
            != self.corpus_member_reference_sha256
            or self.native_invocation.corpus_member_reference_sha256
            != self.corpus_member_reference_sha256
        ):
            raise ShadowCalibrationCommandError("corpus member invocation/source reference drift")


@dataclass(frozen=True, slots=True)
class MeasureShadowCalibrationRequest:
    shadow_inputs: ShadowCalibrationInputs
    corpus_members: tuple[ShadowCalibrationCorpusMember, ...]

    def __post_init__(self) -> None:
        if type(self.shadow_inputs) is not ShadowCalibrationInputs:  # noqa: E721
            raise ShadowCalibrationCommandError("request.shadow_inputs must be exact")
        if type(self.corpus_members) is not tuple or not self.corpus_members:  # noqa: E721
            raise ShadowCalibrationCommandError("request.corpus_members must be a non-empty tuple")
        if any(type(item) is not ShadowCalibrationCorpusMember for item in self.corpus_members):  # noqa: E721
            raise ShadowCalibrationCommandError(
                "request.corpus_members must contain exact typed members"
            )
        refs = tuple(item.corpus_member_reference_sha256 for item in self.corpus_members)
        if len(refs) != len(set(refs)):
            raise ShadowCalibrationCommandError("request.corpus_members must not duplicate members")
        for member in self.corpus_members:
            self._validate_member(member)

    def _validate_member(self, member: ShadowCalibrationCorpusMember) -> None:
        inputs, context, invocation = (
            self.shadow_inputs,
            member.raw_context,
            member.native_invocation,
        )
        mapping = invocation.request_mapping
        if (
            context.native_profile_identity_sha256 != inputs.native_port_identity_sha256
            or context.policies.word_gap_policy_sha256 != inputs.word_gap_policy_sha256
            or context.policies.vad_merge_policy_sha256 != inputs.vad_merge_policy_sha256
            or context.asr_identity.producer_id != inputs.asr_producer_id
            or context.vad_identity.producer_id != inputs.vad_producer_id
            or context.audio_clock.clock_id != inputs.source_clock_id
            or context.audio_clock.time_base != inputs.source_time_base
        ):
            raise ShadowCalibrationCommandError("corpus member context drifts from locked inputs")
        if (
            mapping.source != context.source
            or mapping.source_byte_limits != context.source_byte_limits
            or mapping.container != context.container
            or mapping.audio_clock != context.audio_clock
            or mapping.requested_range != context.audio_clock.full_range
            or mapping.native_profile_identity_sha256 != context.native_profile_identity_sha256
            or mapping.transcript_capability != context.transcript_capability
            or mapping.timed_speech_policy_sha256 != context.policies.timed_speech_policy_sha256
            or mapping.word_gap_policy_sha256 != context.policies.word_gap_policy_sha256
            or mapping.vad_merge_policy_sha256 != context.policies.vad_merge_policy_sha256
            or mapping.word_gap_ms != context.policies.word_gap_ms
            or mapping.vad_merge_gap_ms != context.policies.vad_merge_gap_ms
            or mapping.producer_identities != context.producer_identities
        ):
            raise ShadowCalibrationCommandError(
                "corpus member invocation drifts from locked context"
            )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "command": MEASURE_SHADOW_CALIBRATION_COMMAND,
            "corpus_members": [
                {
                    "corpus_member_reference_sha256": member.corpus_member_reference_sha256,
                    "expected_anchor_reference_sha256": member.expected_anchor_reference_sha256,
                    "native_invocation": _invocation_mapping(member.native_invocation),
                    "raw_context": _raw_context_mapping(member.raw_context),
                }
                for member in self.corpus_members
            ],
            "measurement_protocol": SHADOW_CALIBRATION_MEASUREMENT_PROTOCOL,
            "shadow_inputs": self.shadow_inputs.to_mapping(),
        }

    @property
    def request_hash(self) -> str:
        return canonical_sha256(self.canonical_payload())

    @property
    def calibration_run_key(self) -> str:
        return self.request_hash.removeprefix("sha256:")

    @property
    def job(self) -> Job:
        return Job(self.calibration_run_key, "shadow")

    @property
    def idempotency_key(self) -> str:
        return f"shadow-calibration:{self.calibration_run_key}"

@dataclass(frozen=True, slots=True)
class ShadowCalibrationPortResult:
    """Port echo plus raw bytes and an untrusted claimed projection, never matches."""

    invocation: ShadowCalibrationInvocation
    raw_blob: ShadowCalibrationRawBlob
    projection: ShadowCalibrationProjection

    def __post_init__(self) -> None:
        if type(self.invocation) is not ShadowCalibrationInvocation:  # noqa: E721
            raise ShadowCalibrationCommandError("port_result.invocation must be exact")
        if type(self.raw_blob) is not ShadowCalibrationRawBlob:  # noqa: E721
            raise ShadowCalibrationCommandError("port_result.raw_blob must be exact")
        if type(self.projection) is not ShadowCalibrationProjection:  # noqa: E721
            raise ShadowCalibrationCommandError("port_result.projection must be exact")


class ShadowCalibrationMeasurementPort(Protocol):
    def measure(
        self, request: MeasureShadowCalibrationRequest, member: ShadowCalibrationCorpusMember
    ) -> ShadowCalibrationPortResult: ...


class ShadowCalibrationMeasurementStore(Protocol):
    def claim_or_read_shadow_measurement_attempt(
        self, claim: CommandClaim, plan: ShadowMeasurementPlan
    ) -> ShadowMeasurementAttempt: ...
    def acquire_shadow_measurement_member_lease(
        self, attempt_id: UUID, member_reference_sha256: str, *, expected_version: int
    ) -> ShadowMeasurementMemberLease | None: ...
    def stage_shadow_measurement_member_response(
        self,
        attempt_id: UUID,
        member_reference_sha256: str,
        *,
        expected_version: int,
        lease_token: str,
        staged: ShadowMeasurementStagedResponse,
    ) -> ShadowMeasurementAttempt: ...
    def acquire_shadow_measurement_recovery_lease(
        self, attempt_id: UUID, *, expected_version: int
    ) -> ShadowMeasurementRecoveryLease | None: ...
    def mark_shadow_measurement_member_indeterminate(
        self,
        attempt_id: UUID,
        member_reference_sha256: str,
        *,
        expected_version: int,
        recovery_lease_token: str,
        code: str = "NATIVE_OUTCOME_UNKNOWN",
    ) -> ShadowMeasurementAttempt: ...
    def reserve_shadow_measurement_successor(
        self,
        previous_attempt_id: UUID,
        authorization: ShadowMeasurementRetryAuthorization,
    ) -> ShadowMeasurementAttempt: ...
    def finalize_shadow_measurement_success(
        self, attempt_id: UUID, *, expected_version: int
    ) -> CommandOutcome: ...
    def commit_shadow_measurement_terminal_denial(
        self, request: ShadowMeasurementTerminalDenialRequest
    ) -> ShadowMeasurementTerminalDenialResult: ...


class MeasureShadowCalibrationCommand:
    """Commit only a decoder-verified two-member shadow measurement ArtifactSet."""

    def __init__(
        self, store: ShadowCalibrationMeasurementStore, port: ShadowCalibrationMeasurementPort
    ) -> None:
        self._store, self._port = store, port

    def execute(
        self,
        request: MeasureShadowCalibrationRequest,
        *,
        retry_authorization: ShadowMeasurementRetryAuthorization | None = None,
    ) -> CommandOutcome:
        if type(request) is not MeasureShadowCalibrationRequest:  # noqa: E721
            raise ShadowCalibrationCommandError(
                "request must be an exact MeasureShadowCalibrationRequest"
            )
        if retry_authorization is not None and type(retry_authorization) is not ShadowMeasurementRetryAuthorization:  # noqa: E721
            raise ShadowCalibrationCommandError("retry_authorization must be exact when supplied")
        plan = self._plan(request)
        attempt = self._store.claim_or_read_shadow_measurement_attempt(plan.claim, plan)
        if attempt.outcome.state != "running":
            return attempt.outcome
        if attempt.state == "indeterminate":
            if retry_authorization is None:
                return attempt.outcome
            attempt = self._store.reserve_shadow_measurement_successor(
                attempt.attempt_id, retry_authorization
            )
        return self._collect_or_finalize(request, attempt)

    @staticmethod
    def _plan(request: MeasureShadowCalibrationRequest) -> ShadowMeasurementPlan:
        claim = CommandClaim(
            request.job,
            request.idempotency_key,
            MEASURE_SHADOW_CALIBRATION_COMMAND,
            request.request_hash,
            execution_kind="deterministic",
        )
        return ShadowMeasurementPlan(
            claim,
            _json(request.canonical_payload()),
            tuple(
                ShadowMeasurementMemberPlan(
                    member.corpus_member_reference_sha256,
                    ordinal,
                    _json(_invocation_mapping(member.native_invocation)),
                    _json(_raw_context_mapping(member.raw_context)),
                    member.expected_anchor_reference_sha256,
                )
                for ordinal, member in enumerate(request.corpus_members)
            ),
        )

    def _collect_or_finalize(
        self, request: MeasureShadowCalibrationRequest, attempt: ShadowMeasurementAttempt
    ) -> CommandOutcome:
        """Advance only durable pending/staged state; never repeat an invoking call."""

        if attempt.state == "committed" or attempt.outcome.state != "running":
            return attempt.outcome
        if attempt.state == "ready":
            return self._store.finalize_shadow_measurement_success(
                attempt.attempt_id, expected_version=attempt.version
            )
        if attempt.state == "indeterminate":
            return attempt.outcome

        for member in attempt.members:
            if member.state == "staged":
                continue
            if member.state == "invoking":
                return self._recover_unknown_member(attempt, member.corpus_member_reference_sha256)
            if member.state == "indeterminate":
                return attempt.outcome
            lease = self._store.acquire_shadow_measurement_member_lease(
                attempt.attempt_id,
                member.corpus_member_reference_sha256,
                expected_version=member.version,
            )
            if lease is None:
                return attempt.outcome
            # The Store transitions to invoking before this call. Any exception that
            # prevents a durable stage is deliberately non-terminal: recovery must
            # classify it as unknown after lease expiry instead of guessing whether
            # native inference began.
            try:
                result = self._port.measure(request, request.corpus_members[member.member_ordinal])
                _, projection, raw_blob = self._decode_port_result(
                    request, request.corpus_members[member.member_ordinal], result
                )
            except ShadowCalibrationProducerError as error:
                if error.code is ShadowCalibrationProducerFailureCode.REJECTED:
                    return self._terminal_deny(
                        attempt, lease, member.corpus_member_reference_sha256, error.code.value
                    )
                # A known unavailable port has no Store-supported release operation
                # in this protocol version. Return its running aggregate; recovery
                # alone decides whether the leased call later becomes unknown.
                return attempt.outcome
            except (
                CalibrationRecordError,
                ShadowCalibrationCommandError,
                ShadowCalibrationRawEvidenceError,
            ):
                return self._terminal_deny(
                    attempt,
                    lease,
                    member.corpus_member_reference_sha256,
                    "SHADOW_CALIBRATION_INVALID",
                )
            attempt = self._store.stage_shadow_measurement_member_response(
                attempt.attempt_id,
                member.corpus_member_reference_sha256,
                expected_version=lease.member.version,
                lease_token=lease.lease_token,
                staged=ShadowMeasurementStagedResponse(
                    raw_blob.raw,
                    raw_blob.content_sha256,
                    raw_blob.media_type,
                    _json(_projection_mapping(projection)),
                ),
            )
            if attempt.state == "ready":
                return self._store.finalize_shadow_measurement_success(
                    attempt.attempt_id, expected_version=attempt.version
                )
        return attempt.outcome

    def _terminal_deny(
        self,
        attempt: ShadowMeasurementAttempt,
        lease: ShadowMeasurementMemberLease,
        member_reference_sha256: str,
        failure_code: str,
    ) -> CommandOutcome:
        """Commit decoder-proven invalid evidence through the shadow-only Store path."""

        denial = self._store.commit_shadow_measurement_terminal_denial(
            ShadowMeasurementTerminalDenialRequest(
                attempt.attempt_id,
                attempt.command_slot_id,
                attempt.job,
                attempt.plan_hash,
                member_reference_sha256,
                lease.attempt_version,
                lease.member.version,
                lease.lease_token,
                failure_code,
                _json({"reason": "decoder-proven shadow native evidence is invalid"}),
            )
        )
        return denial.outcome

    def _recover_unknown_member(
        self, attempt: ShadowMeasurementAttempt, member_reference_sha256: str
    ) -> CommandOutcome:
        """Conservatively mark expired invocation state indeterminate, never replay it."""

        member = next(
            item
            for item in attempt.members
            if item.corpus_member_reference_sha256 == member_reference_sha256
        )
        if member.lease_expires_at is None or member.lease_expires_at > datetime.now(timezone.utc):
            return attempt.outcome
        recovery = self._store.acquire_shadow_measurement_recovery_lease(
            attempt.attempt_id, expected_version=attempt.version
        )
        if recovery is None:
            return attempt.outcome
        self._store.mark_shadow_measurement_member_indeterminate(
            attempt.attempt_id,
            member_reference_sha256,
            expected_version=member.version,
            recovery_lease_token=recovery.lease_token,
        )
        return attempt.outcome

    @staticmethod
    def _decode_port_result(
        request: MeasureShadowCalibrationRequest,
        member: ShadowCalibrationCorpusMember,
        result: ShadowCalibrationPortResult,
    ) -> tuple[ShadowCalibrationInvocation, ShadowCalibrationProjection, ShadowCalibrationRawBlob]:
        if type(result) is not ShadowCalibrationPortResult:  # noqa: E721
            raise ShadowCalibrationCommandError("native port returned another result type")
        if result.invocation != member.native_invocation:
            raise ShadowCalibrationCommandError(
                "native port invocation does not equal the locked invocation"
            )
        decoded = decode_shadow_calibration_raw_response(
            result.raw_blob, member.native_invocation, member.raw_context, result.projection
        )
        if (
            decoded.projection != result.projection
            or result.projection.reported_native_identity_sha256
            != request.shadow_inputs.native_port_identity_sha256
        ):
            raise ShadowCalibrationCommandError(
                "native projection does not bind locked decoder identity"
            )
        return result.invocation, decoded.projection, result.raw_blob



def _time_base_mapping(time_base: TimeBase) -> dict[str, int]:
    return {"denominator": time_base.denominator, "numerator": time_base.numerator}


def _range_mapping(value: TickRange) -> dict[str, int]:
    return {"in_tick": value.start_pts, "out_tick": value.end_pts}


def _anchor_mapping(anchor: CalibrationAnchor) -> dict[str, object]:
    return {
        "anchor_id": anchor.anchor_id,
        "clock_id": anchor.clock_id,
        "expected_range": _range_mapping(anchor.expected_range),
        "producer": anchor.producer.value,
        "producer_id": anchor.producer_id,
        "time_base": _time_base_mapping(anchor.time_base),
    }


def _observation_mapping(observation: CalibrationObservation) -> dict[str, object]:
    return {
        "clock_id": observation.clock_id,
        "inference_kind": observation.inference_kind,
        "observation_id": observation.observation_id,
        "observed_range": _range_mapping(observation.observed_range),
        "producer": observation.producer.value,
        "producer_id": observation.producer_id,
        "time_base": _time_base_mapping(observation.time_base),
    }


def _match_mapping(match: CalibrationAnchorMatch) -> dict[str, object]:
    return {
        "absolute_tick": match.absolute_tick,
        "anchor": _anchor_mapping(match.anchor),
        "early_tick": match.early_tick,
        "late_tick": match.late_tick,
        "observation": _observation_mapping(match.observation),
    }


def _measurement_mapping(measurement: ProducerCalibrationMeasurement) -> dict[str, object]:
    return {
        "absolute_maximum_tick": measurement.absolute_maximum_tick,
        "accepted_bound_tick": measurement.accepted_bound_tick,
        "clock_id": measurement.clock_id,
        "early_maximum_tick": measurement.early_maximum_tick,
        "inference_kind": measurement.inference_kind,
        "late_maximum_tick": measurement.late_maximum_tick,
        "matches": [_match_mapping(match) for match in measurement.matches],
        "matched_anchor_count": len(measurement.matches),
        "producer": measurement.producer.value,
        "producer_id": measurement.producer_id,
        "time_base": _time_base_mapping(measurement.time_base),
    }


def _projection_mapping(projection: ShadowCalibrationProjection) -> dict[str, object]:
    return {
        "asr_observations": [
            {"observation": _observation_mapping(item.observation), "text": item.text}
            for item in projection.asr_observations
        ],
        "native_request_identity_sha256": projection.native_request_identity_sha256,
        "reported_native_identity_sha256": projection.reported_native_identity_sha256,
        "summary": {
            "asr": _measurement_mapping(projection.summary.asr),
            "vad": _measurement_mapping(projection.summary.vad),
        },
        "vad_observations": [_observation_mapping(item) for item in projection.vad_observations],
        "word_gap_segments": [
            {
                "observed_range": _range_mapping(item.observed_range),
                "segment_id": item.segment_id,
                "text": item.text,
            }
            for item in projection.word_gap_segments
        ],
    }


def _invocation_mapping(invocation: ShadowCalibrationInvocation) -> dict[str, object]:
    return {
        "corpus_member_reference_sha256": invocation.corpus_member_reference_sha256,
        "request_identity_sha256": invocation.request_identity_sha256,
        "request_mapping": invocation.request_mapping.to_mapping(),
        "request_mapping_sha256": invocation.request_mapping_sha256,
    }


def _raw_context_mapping(context: ShadowCalibrationRawContext) -> dict[str, object]:
    return {
        "asr_anchors": [_anchor_mapping(item) for item in context.asr_anchors],
        "asr_identity": context.asr_identity.to_mapping(),
        "audio_clock": context.audio_clock.to_mapping(),
        "container": context.container.to_mapping(),
        "native_profile_identity_sha256": context.native_profile_identity_sha256,
        "policies": {
            "timed_speech_policy_sha256": context.policies.timed_speech_policy_sha256,
            "vad_merge_gap_ms": context.policies.vad_merge_gap_ms,
            "vad_merge_policy_sha256": context.policies.vad_merge_policy_sha256,
            "word_gap_ms": context.policies.word_gap_ms,
            "word_gap_policy_sha256": context.policies.word_gap_policy_sha256,
        },
        "source": context.source.to_mapping(),
        "source_byte_limits": context.source_byte_limits.to_mapping(),
        "transcript_capability": context.transcript_capability.to_mapping(),
        "vad_anchors": [_anchor_mapping(item) for item in context.vad_anchors],
        "vad_identity": context.vad_identity.to_mapping(),
    }


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
