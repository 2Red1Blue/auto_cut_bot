from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from autocut_kernel.media import (
    SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE,
    SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA,
    CalibrationAnchor,
    CalibrationAnchorMatch,
    CalibrationMeasurementSummary,
    CalibrationObservation,
    CalibrationProducer,
    ProducerCalibrationMeasurement,
    ShadowCalibrationAsrObservation,
    ShadowCalibrationAudioClock,
    ShadowCalibrationContainer,
    ShadowCalibrationInvocation,
    ShadowCalibrationPolicies,
    ShadowCalibrationProducerIdentity,
    ShadowCalibrationProjection,
    ShadowCalibrationRawBlob,
    ShadowCalibrationRawContext,
    ShadowCalibrationRequestMapping,
    ShadowCalibrationSource,
    ShadowCalibrationSourceByteLimits,
    ShadowCalibrationTranscriptCapability,
    ShadowCalibrationWordGapSegment,
    TickRange,
    TimeBase,
)
from autocut_kernel.pipeline import (
    MeasureShadowCalibrationCommand,
    MeasureShadowCalibrationRequest,
    ShadowCalibrationCorpusMember,
    ShadowCalibrationInputs,
    ShadowCalibrationPortResult,
    ShadowCalibrationProducerError,
    ShadowCalibrationProducerFailureCode,
)
from autocut_kernel.store import (
    BlobRef,
    CommandClaim,
    CommandOutcome,
    ShadowMeasurementAttempt,
    ShadowMeasurementMember,
    ShadowMeasurementMemberLease,
    ShadowMeasurementPlan,
    ShadowMeasurementRecoveryLease,
    ShadowMeasurementRetryAuthorization,
    ShadowMeasurementStagedResponse,
    ShadowMeasurementTerminalDenialRequest,
    ShadowMeasurementTerminalDenialResult,
)


def _sha(number: int) -> str:
    return f"sha256:{number:064x}"


TIME_BASE = TimeBase(1, 1_000)
CLOCK = ShadowCalibrationAudioClock("shadow-source-audio-clock", TIME_BASE, 0, 1_000)
SOURCE = ShadowCalibrationSource(
    "corpus-0001",
    _sha(1),
    _sha(10),
    "0f02e85b-d8c6-4d1b-a86b-3dc0f64d2f34",
    _sha(1),
    4_096,
    "video/mp4",
)
SOURCE_LIMITS = ShadowCalibrationSourceByteLimits(8_192, 4_096, 4_096)
CONTAINER = ShadowCalibrationContainer("video/mp4", ".mp4")
POLICIES = ShadowCalibrationPolicies(_sha(2), _sha(3), _sha(4), 100, 50)
CAPABILITY = ShadowCalibrationTranscriptCapability(
    "sensevoice_word_guard_v1",
    "complete",
    "utterance_gap_protected_range",
    "not_applicable",
    "complete",
    "required",
)
ASR_IDENTITY = ShadowCalibrationProducerIdentity(
    CalibrationProducer.ASR,
    "sensevoice-shadow",
    "1.0.0",
    _sha(5),
    _sha(6),
    _sha(7),
    "iic/SenseVoiceSmall",
    "main",
    _sha(8),
    "sensevoice-word-timestamp",
    _sha(9),
)
VAD_IDENTITY = ShadowCalibrationProducerIdentity(
    CalibrationProducer.VAD,
    "fsmn-shadow",
    "1.0.0",
    _sha(5),
    _sha(6),
    _sha(7),
    "iic/fsmn-vad",
    "main",
    _sha(11),
    "fsmn-vad-direct",
    _sha(9),
)
NATIVE_IDENTITY = _sha(12)


def _inputs() -> ShadowCalibrationInputs:
    return ShadowCalibrationInputs(
        _sha(20),
        _sha(21),
        _sha(22),
        NATIVE_IDENTITY,
        POLICIES.word_gap_policy_sha256,
        POLICIES.vad_merge_policy_sha256,
        _sha(23),
        _sha(24),
        ASR_IDENTITY.producer_id,
        VAD_IDENTITY.producer_id,
        CLOCK.clock_id,
        TIME_BASE,
    )


def _anchors() -> tuple[tuple[CalibrationAnchor, ...], tuple[CalibrationAnchor, ...]]:
    return (
        (
            CalibrationAnchor(
                "asr-anchor-0",
                CalibrationProducer.ASR,
                ASR_IDENTITY.producer_id,
                CLOCK.clock_id,
                TIME_BASE,
                TickRange(101, 220),
            ),
            CalibrationAnchor(
                "asr-anchor-1",
                CalibrationProducer.ASR,
                ASR_IDENTITY.producer_id,
                CLOCK.clock_id,
                TIME_BASE,
                TickRange(400, 561),
            ),
        ),
        (
            CalibrationAnchor(
                "vad-anchor-0",
                CalibrationProducer.VAD,
                VAD_IDENTITY.producer_id,
                CLOCK.clock_id,
                TIME_BASE,
                TickRange(81, 600),
            ),
        ),
    )


def _context(source: ShadowCalibrationSource = SOURCE) -> ShadowCalibrationRawContext:
    asr, vad = _anchors()
    return ShadowCalibrationRawContext(
        source,
        SOURCE_LIMITS,
        CONTAINER,
        CLOCK,
        POLICIES,
        NATIVE_IDENTITY,
        CAPABILITY,
        ASR_IDENTITY,
        VAD_IDENTITY,
        asr,
        vad,
    )


def _invocation(source: ShadowCalibrationSource = SOURCE) -> ShadowCalibrationInvocation:
    mapping = ShadowCalibrationRequestMapping(
        source,
        SOURCE_LIMITS,
        CONTAINER,
        CLOCK,
        CLOCK.full_range,
        NATIVE_IDENTITY,
        32_768,
        CAPABILITY,
        POLICIES.timed_speech_policy_sha256,
        POLICIES.word_gap_policy_sha256,
        POLICIES.vad_merge_policy_sha256,
        POLICIES.word_gap_ms,
        POLICIES.vad_merge_gap_ms,
        (ASR_IDENTITY, VAD_IDENTITY),
    )
    return ShadowCalibrationInvocation(
        source.corpus_member_reference_sha256, mapping.sha256, mapping, mapping.sha256
    )


def _request() -> MeasureShadowCalibrationRequest:
    return MeasureShadowCalibrationRequest(
        _inputs(),
        (
            ShadowCalibrationCorpusMember(
                SOURCE.corpus_member_reference_sha256, _sha(25), _context(), _invocation()
            ),
        ),
    )


SECOND_SOURCE = ShadowCalibrationSource(
    "corpus-0002",
    _sha(26),
    _sha(27),
    "73ad6766-3a13-49c3-9186-b7ccca505053",
    _sha(26),
    4_096,
    "video/mp4",
)


def _two_member_request() -> MeasureShadowCalibrationRequest:
    second_context = _context(SECOND_SOURCE)
    second_invocation = _invocation(SECOND_SOURCE)
    return MeasureShadowCalibrationRequest(
        _inputs(),
        (
            ShadowCalibrationCorpusMember(
                SOURCE.corpus_member_reference_sha256, _sha(25), _context(), _invocation()
            ),
            ShadowCalibrationCorpusMember(
                SECOND_SOURCE.corpus_member_reference_sha256,
                _sha(28),
                second_context,
                second_invocation,
            ),
        ),
    )


def _observation(
    identifier: str, producer: CalibrationProducer, producer_id: str, kind: str, interval: TickRange
) -> CalibrationObservation:
    return CalibrationObservation(
        identifier, producer, producer_id, kind, CLOCK.clock_id, TIME_BASE, interval
    )


def _measurement(
    producer: CalibrationProducer,
    producer_id: str,
    kind: str,
    anchors: tuple[CalibrationAnchor, ...],
    observations: tuple[CalibrationObservation, ...],
) -> ProducerCalibrationMeasurement:
    matches = tuple(
        CalibrationAnchorMatch(anchor, observation)
        for anchor, observation in zip(anchors, observations, strict=True)
    )
    return ProducerCalibrationMeasurement(
        producer,
        producer_id,
        kind,
        CLOCK.clock_id,
        TIME_BASE,
        matches,
        max(match.absolute_tick for match in matches),
    )


def _projection(invocation: ShadowCalibrationInvocation | None = None) -> ShadowCalibrationProjection:
    request_invocation = invocation or _invocation()
    asr_anchors, vad_anchors = _anchors()
    asr = (
        ShadowCalibrationAsrObservation(
            _observation(
                "asr-word-00000000",
                CalibrationProducer.ASR,
                ASR_IDENTITY.producer_id,
                "sensevoice-word-timestamp",
                TickRange(100, 220),
            ),
            "a",
        ),
        ShadowCalibrationAsrObservation(
            _observation(
                "asr-word-00000001",
                CalibrationProducer.ASR,
                ASR_IDENTITY.producer_id,
                "sensevoice-word-timestamp",
                TickRange(400, 560),
            ),
            "b",
        ),
    )
    vad = (
        _observation(
            "vad-segment-00000000",
            CalibrationProducer.VAD,
            VAD_IDENTITY.producer_id,
            "fsmn-vad-direct",
            TickRange(80, 600),
        ),
    )
    return ShadowCalibrationProjection(
        NATIVE_IDENTITY,
        request_invocation.request_identity_sha256,
        asr,
        (
            ShadowCalibrationWordGapSegment("asr-segment-00000000", "a", TickRange(100, 220)),
            ShadowCalibrationWordGapSegment("asr-segment-00000001", "b", TickRange(400, 560)),
        ),
        vad,
        CalibrationMeasurementSummary(
            _measurement(
                CalibrationProducer.ASR,
                ASR_IDENTITY.producer_id,
                "sensevoice-word-timestamp",
                asr_anchors,
                tuple(item.observation for item in asr),
            ),
            _measurement(
                CalibrationProducer.VAD,
                VAD_IDENTITY.producer_id,
                "fsmn-vad-direct",
                vad_anchors,
                vad,
            ),
        ),
    )


def _raw(invocation: ShadowCalibrationInvocation | None = None) -> bytes:
    request_invocation = invocation or _invocation()
    response = {
        "schema_version": SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA,
        "request_identity_sha256": request_invocation.request_identity_sha256,
        "source": request_invocation.request_mapping.source.to_response_mapping(),
        "audio_clock": CLOCK.to_mapping(),
        "requested_range": {"in_tick": 0, "out_tick": 1_000},
        "timed_speech_policy_sha256": POLICIES.timed_speech_policy_sha256,
        "word_gap_policy_sha256": POLICIES.word_gap_policy_sha256,
        "vad_merge_policy_sha256": POLICIES.vad_merge_policy_sha256,
        "native_profile_identity_sha256": NATIVE_IDENTITY,
        "producer_identities": [ASR_IDENTITY.to_mapping(), VAD_IDENTITY.to_mapping()],
        "asr_native_output": [
            {"text": "ab", "words": ["a", "b"], "timestamp": [[100, 220], [400, 560]]}
        ],
        "vad_native_output": [{"value": [[80, 260], [300, 600]]}],
    }
    return json.dumps(response, sort_keys=True, separators=(",", ":")).encode()


def _blob(
    raw: bytes | None = None, invocation: ShadowCalibrationInvocation | None = None
) -> ShadowCalibrationRawBlob:
    material = _raw(invocation) if raw is None else raw
    return ShadowCalibrationRawBlob(
        material,
        SHADOW_CALIBRATION_RAW_RESPONSE_MEDIA_TYPE,
        len(material),
        "sha256:" + hashlib.sha256(material).hexdigest(),
    )


def _result(
    *,
    invocation: ShadowCalibrationInvocation | None = None,
    raw_blob: ShadowCalibrationRawBlob | None = None,
    projection: ShadowCalibrationProjection | None = None,
) -> ShadowCalibrationPortResult:
    request_invocation = invocation or _invocation()
    return ShadowCalibrationPortResult(
        request_invocation,
        raw_blob or _blob(invocation=request_invocation),
        projection or _projection(request_invocation),
    )


@dataclass
class _Store:
    attempts: dict[UUID, ShadowMeasurementAttempt] = field(default_factory=dict)
    initial_attempt_id: UUID | None = None
    blobs: list[BlobRef] = field(default_factory=list)
    finalizations: int = 0
    denials: list[ShadowMeasurementTerminalDenialRequest] = field(default_factory=list)
    stage_timeout_after_commit: bool = False

    def claim_or_read_shadow_measurement_attempt(
        self, claim: CommandClaim, plan: ShadowMeasurementPlan
    ) -> ShadowMeasurementAttempt:
        if self.initial_attempt_id is not None:
            return self.attempts[self.initial_attempt_id]
        attempt_id, slot_id = uuid4(), uuid4()
        members = tuple(
            ShadowMeasurementMember(
                attempt_id,
                member.corpus_member_reference_sha256,
                member.member_ordinal,
                member.invocation_json,
                member.context_json,
                member.expected_anchor_reference_sha256,
                "pending",
                0,
            )
            for member in plan.members
        )
        attempt = ShadowMeasurementAttempt(
            attempt_id,
            slot_id,
            claim.job,
            claim.request_hash,
            plan.canonical_plan_json,
            1,
            None,
            "prepared",
            0,
            members,
            CommandOutcome(slot_id, "running", is_fresh_claim=True),
        )
        self.attempts[attempt_id] = attempt
        self.initial_attempt_id = attempt_id
        return attempt

    @staticmethod
    def _replace_member(
        attempt: ShadowMeasurementAttempt, member: ShadowMeasurementMember
    ) -> tuple[ShadowMeasurementMember, ...]:
        return tuple(
            member if item.corpus_member_reference_sha256 == member.corpus_member_reference_sha256 else item
            for item in attempt.members
        )

    def acquire_shadow_measurement_member_lease(
        self, attempt_id: UUID, member_reference_sha256: str, *, expected_version: int
    ) -> ShadowMeasurementMemberLease | None:
        attempt = self.attempts[attempt_id]
        member = next(item for item in attempt.members if item.corpus_member_reference_sha256 == member_reference_sha256)
        if attempt.state not in ("prepared", "collecting") or member.state != "pending" or member.version != expected_version:
            return None
        leased = replace(
            member,
            state="invoking",
            version=member.version + 1,
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        current = replace(
            attempt,
            state="collecting",
            version=attempt.version + 1,
            members=self._replace_member(attempt, leased),
        )
        self.attempts[attempt_id] = current
        return ShadowMeasurementMemberLease(leased, current.version, f"lease:{member.member_ordinal}")

    def stage_shadow_measurement_member_response(
        self,
        attempt_id: UUID,
        member_reference_sha256: str,
        *,
        expected_version: int,
        lease_token: str,
        staged: ShadowMeasurementStagedResponse,
    ) -> ShadowMeasurementAttempt:
        attempt = self.attempts[attempt_id]
        member = next(item for item in attempt.members if item.corpus_member_reference_sha256 == member_reference_sha256)
        assert member.state == "invoking" and member.version == expected_version
        blob = BlobRef(uuid4(), staged.content_hash, len(staged.raw_bytes), staged.media_type)
        self.blobs.append(blob)
        persisted = replace(
            member,
            state="staged",
            version=member.version + 1,
            raw_blob=blob,
            projection_json=staged.projection_json,
            lease_expires_at=None,
        )
        members = self._replace_member(attempt, persisted)
        current = replace(
            attempt,
            state="ready" if all(item.state == "staged" for item in members) else "collecting",
            version=attempt.version + 1,
            members=members,
        )
        self.attempts[attempt_id] = current
        if self.stage_timeout_after_commit:
            self.stage_timeout_after_commit = False
            raise TimeoutError("stage commit response was lost")
        return current

    def acquire_shadow_measurement_recovery_lease(
        self, attempt_id: UUID, *, expected_version: int
    ) -> ShadowMeasurementRecoveryLease | None:
        attempt = self.attempts[attempt_id]
        if attempt.version != expected_version:
            return None
        current = replace(
            attempt,
            version=attempt.version + 1,
            recovery_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self.attempts[attempt_id] = current
        return ShadowMeasurementRecoveryLease(current, "recovery")

    def mark_shadow_measurement_member_indeterminate(
        self,
        attempt_id: UUID,
        member_reference_sha256: str,
        *,
        expected_version: int,
        recovery_lease_token: str,
        code: str = "NATIVE_OUTCOME_UNKNOWN",
    ) -> ShadowMeasurementAttempt:
        attempt = self.attempts[attempt_id]
        member = next(item for item in attempt.members if item.corpus_member_reference_sha256 == member_reference_sha256)
        assert code == "NATIVE_OUTCOME_UNKNOWN" and member.state == "invoking" and member.version == expected_version
        indeterminate = replace(member, state="indeterminate", version=member.version + 1, lease_expires_at=None)
        current = replace(
            attempt,
            state="indeterminate",
            version=attempt.version + 1,
            members=self._replace_member(attempt, indeterminate),
            recovery_lease_expires_at=None,
        )
        self.attempts[attempt_id] = current
        return current

    def reserve_shadow_measurement_successor(
        self,
        previous_attempt_id: UUID,
        authorization: ShadowMeasurementRetryAuthorization,
    ) -> ShadowMeasurementAttempt:
        previous = self.attempts[previous_attempt_id]
        assert previous.state == "indeterminate" and authorization.predecessor_plan_hash == previous.plan_hash
        attempt_id, slot_id = uuid4(), uuid4()
        members = tuple(
            ShadowMeasurementMember(
                attempt_id,
                member.corpus_member_reference_sha256,
                member.member_ordinal,
                member.invocation_json,
                member.context_json,
                member.expected_anchor_reference_sha256,
                "pending",
                0,
            )
            for member in previous.members
        )
        successor = ShadowMeasurementAttempt(
            attempt_id,
            slot_id,
            previous.job,
            previous.plan_hash,
            previous.canonical_plan_json,
            previous.attempt_ordinal + 1,
            previous.attempt_id,
            "prepared",
            0,
            members,
            CommandOutcome(slot_id, "running", is_fresh_claim=True),
        )
        self.attempts[attempt_id] = successor
        return successor

    def finalize_shadow_measurement_success(
        self, attempt_id: UUID, *, expected_version: int
    ) -> CommandOutcome:
        attempt = self.attempts[attempt_id]
        assert attempt.state == "ready" and attempt.version == expected_version
        self.finalizations += 1
        outcome = CommandOutcome(attempt.command_slot_id, "succeeded")
        self.attempts[attempt_id] = replace(attempt, state="committed", version=attempt.version + 1, outcome=outcome)
        return outcome

    def commit_shadow_measurement_terminal_denial(
        self, request: ShadowMeasurementTerminalDenialRequest
    ) -> ShadowMeasurementTerminalDenialResult:
        attempt = self.attempts[request.attempt_id]
        member = next(
            item
            for item in attempt.members
            if item.corpus_member_reference_sha256 == request.member_reference_sha256
        )
        assert (
            attempt.state == "collecting"
            and request.expected_attempt_version == attempt.version
            and member.state == "invoking"
            and request.expected_member_version == member.version
            and all(item.state == "pending" for item in attempt.members if item != member)
        )
        self.denials.append(request)
        outcome = CommandOutcome(attempt.command_slot_id, "denied", failure_code=request.failure_code)
        closed = replace(
            attempt,
            state="indeterminate",
            version=attempt.version + 1,
            members=self._replace_member(
                attempt, replace(member, state="indeterminate", version=member.version + 1, lease_expires_at=None)
            ),
            outcome=outcome,
        )
        self.attempts[attempt.attempt_id] = closed
        return ShadowMeasurementTerminalDenialResult(closed, outcome)


@dataclass
class _Port:
    result: ShadowCalibrationPortResult
    calls: int = 0

    def measure(
        self, request: MeasureShadowCalibrationRequest, member: ShadowCalibrationCorpusMember
    ) -> ShadowCalibrationPortResult:
        self.calls += 1
        return self.result


@dataclass
class _MemberPort:
    results: dict[str, ShadowCalibrationPortResult]
    calls: int = 0

    def measure(
        self, request: MeasureShadowCalibrationRequest, member: ShadowCalibrationCorpusMember
    ) -> ShadowCalibrationPortResult:
        self.calls += 1
        return self.results[member.corpus_member_reference_sha256]


@dataclass
class _UnavailablePort:
    calls: int = 0

    def measure(
        self, request: MeasureShadowCalibrationRequest, member: ShadowCalibrationCorpusMember
    ) -> ShadowCalibrationPortResult:
        self.calls += 1
        raise ShadowCalibrationProducerError(ShadowCalibrationProducerFailureCode.UNAVAILABLE)


def _member_port(request: MeasureShadowCalibrationRequest) -> _MemberPort:
    return _MemberPort(
        {
            member.corpus_member_reference_sha256: _result(invocation=member.native_invocation)
            for member in request.corpus_members
        }
    )


def test_fresh_measurement_stages_decoder_verified_evidence_then_finalizes() -> None:
    request, store = _two_member_request(), _Store()
    port = _member_port(request)

    outcome = MeasureShadowCalibrationCommand(store, port).execute(request)

    assert outcome.state == "succeeded"
    assert port.calls == 2
    assert len(store.blobs) == 2
    assert store.finalizations == 1
    assert all(member.state == "staged" for member in store.attempts[store.initial_attempt_id].members)  # type: ignore[index]


def test_staged_recovery_reads_durable_member_without_duplicate_port_call() -> None:
    request, store = _two_member_request(), _Store(stage_timeout_after_commit=True)
    port = _member_port(request)
    command = MeasureShadowCalibrationCommand(store, port)

    try:
        command.execute(request)
    except TimeoutError as error:
        assert str(error) == "stage commit response was lost"
    else:
        raise AssertionError("ambiguous stage result must propagate")
    replay = command.execute(request)

    assert replay.state == "succeeded"
    assert port.calls == 2
    assert store.finalizations == 1
    assert len(store.blobs) == 2


def test_terminal_invalid_native_evidence_uses_the_shadow_store_denial_path() -> None:
    request, store = _request(), _Store()
    payload = json.loads(_raw())
    payload["asr_native_output"][0]["timestamp"][0] = [101, 220]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    port = _Port(_result(raw_blob=_blob(raw)))

    outcome = MeasureShadowCalibrationCommand(store, port).execute(request)

    assert outcome.state == "denied"
    assert [denial.failure_code for denial in store.denials] == ["SHADOW_CALIBRATION_INVALID"]
    assert not store.blobs and store.finalizations == 0


def test_expired_unknown_invocation_becomes_indeterminate_without_automatic_retry() -> None:
    request, store = _request(), _Store()
    port = _Port(_result())
    command = MeasureShadowCalibrationCommand(store, port)
    attempt = store.claim_or_read_shadow_measurement_attempt(
        MeasureShadowCalibrationCommand._plan(request).claim, MeasureShadowCalibrationCommand._plan(request)
    )
    lease = store.acquire_shadow_measurement_member_lease(
        attempt.attempt_id,
        attempt.members[0].corpus_member_reference_sha256,
        expected_version=attempt.members[0].version,
    )
    assert lease is not None
    current = store.attempts[attempt.attempt_id]
    expired_member = replace(current.members[0], lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    store.attempts[attempt.attempt_id] = replace(current, members=(expired_member,))

    outcome = command.execute(request)

    assert outcome.state == "running"
    assert port.calls == 0
    assert store.attempts[attempt.attempt_id].state == "indeterminate"
    assert command.execute(request).state == "running"
    assert port.calls == 0
    assert len(store.attempts) == 1


def test_known_unavailable_port_is_non_terminal_and_leaves_recovery_to_the_store() -> None:
    request, store, port = _request(), _Store(), _UnavailablePort()

    outcome = MeasureShadowCalibrationCommand(store, port).execute(request)

    attempt = store.attempts[store.initial_attempt_id]  # type: ignore[index]
    assert outcome.state == "running"
    assert port.calls == 1
    assert attempt.state == "collecting"
    assert attempt.members[0].state == "invoking"
    assert not store.denials and not store.blobs


def test_explicit_retry_authorization_creates_successor_attempt() -> None:
    request, store = _request(), _Store()
    port = _Port(_result())
    command = MeasureShadowCalibrationCommand(store, port)
    plan = MeasureShadowCalibrationCommand._plan(request)
    attempt = store.claim_or_read_shadow_measurement_attempt(plan.claim, plan)
    lease = store.acquire_shadow_measurement_member_lease(
        attempt.attempt_id, attempt.members[0].corpus_member_reference_sha256, expected_version=0
    )
    assert lease is not None
    current = store.attempts[attempt.attempt_id]
    store.attempts[attempt.attempt_id] = replace(
        current,
        members=(replace(current.members[0], lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)),),
    )
    assert command.execute(request).state == "running"

    outcome = command.execute(
        request,
        retry_authorization=ShadowMeasurementRetryAuthorization(_sha(99), request.request_hash),
    )

    successor = next(item for item in store.attempts.values() if item.previous_attempt_id == attempt.attempt_id)
    assert outcome.state == "succeeded"
    assert successor.attempt_ordinal == 2
    assert successor.previous_attempt_id == attempt.attempt_id
    assert port.calls == 1


def test_two_member_failure_after_first_stage_never_finalizes_partial_artifact_set() -> None:
    request, store = _two_member_request(), _Store()
    first, second = (member.native_invocation for member in request.corpus_members)
    port = _MemberPort(
        {
            first.corpus_member_reference_sha256: _result(invocation=first),
            second.corpus_member_reference_sha256: _result(invocation=second),
        }
    )
    store.stage_timeout_after_commit = True
    command = MeasureShadowCalibrationCommand(store, port)
    with pytest.raises(TimeoutError):
        command.execute(request)

    attempt = store.attempts[store.initial_attempt_id]  # type: ignore[index]
    assert attempt.members[0].state == "staged"
    assert attempt.members[1].state == "pending"
    assert store.finalizations == 0
