from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from autocut_kernel.media import (
    CalibrationAnchor,
    CalibrationAnchorMatch,
    CalibrationObservation,
    CalibrationProducer,
    TimeBase,
)
from autocut_kernel.media.types import TickRange
from autocut_kernel.pipeline import (
    MeasureShadowCalibrationCommand,
    MeasureShadowCalibrationRequest,
    ShadowCalibrationCorpusMember,
    ShadowCalibrationCoverage,
    ShadowCalibrationInputs,
    ShadowCalibrationNativeResponseMediaType,
    ShadowCalibrationPortResult,
)
from autocut_kernel.store import (
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
HASH_E = "sha256:" + "e" * 64
HASH_F = "sha256:" + "f" * 64
TIME_BASE = TimeBase(1, 1_000)
CLOCK_ID = "shadow-source-audio-clock"


def _inputs() -> ShadowCalibrationInputs:
    return ShadowCalibrationInputs(
        HASH_A,
        HASH_B,
        HASH_C,
        HASH_D,
        HASH_E,
        HASH_F,
        HASH_A,
        HASH_B,
        "sensevoice-shadow",
        "fsmn-shadow",
        CLOCK_ID,
        TIME_BASE,
    )


def _anchor(
    producer: CalibrationProducer,
    producer_id: str,
    start: int,
    end: int,
) -> CalibrationAnchor:
    return CalibrationAnchor(f"{producer.value}-anchor", producer, producer_id, CLOCK_ID, TIME_BASE, TickRange(start, end))


def _request() -> MeasureShadowCalibrationRequest:
    inputs = _inputs()
    return MeasureShadowCalibrationRequest(
        inputs,
        (
            ShadowCalibrationCorpusMember(
                HASH_D,
                HASH_E,
                (_anchor(CalibrationProducer.ASR, inputs.asr_producer_id, 10, 20),),
                (_anchor(CalibrationProducer.VAD, inputs.vad_producer_id, 30, 40),),
            ),
        ),
    )


def _match(anchor: CalibrationAnchor, start: int, end: int) -> CalibrationAnchorMatch:
    inference = "sensevoice-word-timestamp" if anchor.producer is CalibrationProducer.ASR else "fsmn-vad-direct"
    return CalibrationAnchorMatch(
        anchor,
        CalibrationObservation(
            f"{anchor.anchor_id}-observation",
            anchor.producer,
            anchor.producer_id,
            inference,
            anchor.clock_id,
            anchor.time_base,
            TickRange(start, end),
        ),
    )


def _complete_result(request: MeasureShadowCalibrationRequest) -> ShadowCalibrationPortResult:
    member = request.corpus_members[0]
    return ShadowCalibrationPortResult(
        member.corpus_member_reference_sha256,
        b'{"native":"response"}',
        ShadowCalibrationNativeResponseMediaType.JSON,
        request.shadow_inputs.native_port_identity_sha256,
        ShadowCalibrationCoverage.COMPLETE,
        (_match(member.asr_anchors[0], 9, 20),),
        (_match(member.vad_anchors[0], 31, 40),),
    )


@dataclass
class _Store:
    slot_id: UUID = field(default_factory=uuid4)
    terminal: CommandOutcome | None = None
    claims: list[CommandClaim] = field(default_factory=list)
    successes: list[CommandSuccess] = field(default_factory=list)
    rejections: list[CommandRejection] = field(default_factory=list)
    blobs: list[BlobRef] = field(default_factory=list)

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        self.claims.append(claim)
        if self.terminal is not None:
            return self.terminal
        return CommandOutcome(self.slot_id, "running", is_fresh_claim=True)

    def put_immutable_blob(
        self,
        job: Job,
        *,
        content: bytes,
        content_hash: str,
        media_type: str,
    ) -> BlobRef:
        blob = BlobRef(uuid4(), content_hash, len(content), media_type)
        self.blobs.append(blob)
        return blob

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        self.successes.append(success)
        self.terminal = CommandOutcome(success.command_slot_id, "succeeded")
        return self.terminal

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        self.rejections.append(rejection)
        self.terminal = CommandOutcome(rejection.command_slot_id, rejection.outcome)
        return self.terminal


@dataclass
class _Port:
    result: ShadowCalibrationPortResult
    calls: int = 0

    def measure(
        self,
        request: MeasureShadowCalibrationRequest,
        member: ShadowCalibrationCorpusMember,
    ) -> ShadowCalibrationPortResult:
        self.calls += 1
        return self.result


def test_success_commits_exactly_two_ordered_non_authority_measurement_artifacts() -> None:
    request = _request()
    store = _Store()
    port = _Port(_complete_result(request))

    outcome = MeasureShadowCalibrationCommand(store, port).execute(request)

    assert outcome.state == "succeeded"
    assert port.calls == 1
    assert not store.rejections
    assert len(store.blobs) == 1
    success = store.successes[0]
    assert [item.artifact_type for item in success.artifacts] == [
        "calibration_measurement_manifest",
        "calibration_measurement_results",
    ]
    assert [item.logical_id for item in success.artifacts] == ["measurement-manifest", "measurement-results"]
    assert all(item.revision == 1 for item in success.artifacts)
    assert all(item.scope.namespace != "autocut_authority" for item in success.artifacts)
    manifest, results = (json.loads(item.payload_json) for item in success.artifacts)
    assert manifest["measurement_request_sha256"] == request.request_hash
    assert manifest["native_response_blobs"][0]["native_response_blob"]["content_hash"] == store.blobs[0].content_hash
    assert results["measurement_manifest_sha256"] == success.artifacts[0].content_hash
    assert results["members"][0]["native_response_blob"]["content_hash"] == store.blobs[0].content_hash


@pytest.mark.parametrize(
    "coverage",
    (ShadowCalibrationCoverage.PARTIAL, ShadowCalibrationCoverage.INDETERMINATE),
)
def test_partial_or_indeterminate_evidence_is_terminal_denial_without_partial_artifacts(
    coverage: ShadowCalibrationCoverage,
) -> None:
    request = _request()
    partial = _complete_result(request)
    partial = ShadowCalibrationPortResult(
        partial.corpus_member_reference_sha256,
        partial.raw_native_response,
        partial.raw_native_response_media_type,
        partial.reported_native_identity_sha256,
        coverage,
        partial.asr_matches,
        partial.vad_matches,
    )
    store = _Store()

    outcome = MeasureShadowCalibrationCommand(store, _Port(partial)).execute(request)

    assert outcome.state == "denied"
    assert not store.successes
    assert store.rejections[0].failure_code == "SHADOW_CALIBRATION_INVALID"


def test_anchor_substitution_is_denied_without_partial_artifacts() -> None:
    request = _request()
    valid = _complete_result(request)
    inputs = request.shadow_inputs
    replacement_anchor = _anchor(CalibrationProducer.ASR, inputs.asr_producer_id, 11, 21)
    substituted = ShadowCalibrationPortResult(
        valid.corpus_member_reference_sha256,
        valid.raw_native_response,
        valid.raw_native_response_media_type,
        valid.reported_native_identity_sha256,
        ShadowCalibrationCoverage.COMPLETE,
        (_match(replacement_anchor, 10, 21),),
        valid.vad_matches,
    )
    store = _Store()

    outcome = MeasureShadowCalibrationCommand(store, _Port(substituted)).execute(request)

    assert outcome.state == "denied"
    assert not store.successes


def test_native_identity_substitution_is_denied_before_raw_bytes_are_persisted() -> None:
    request = _request()
    valid = _complete_result(request)
    substituted = ShadowCalibrationPortResult(
        valid.corpus_member_reference_sha256,
        valid.raw_native_response,
        valid.raw_native_response_media_type,
        HASH_A,
        ShadowCalibrationCoverage.COMPLETE,
        valid.asr_matches,
        valid.vad_matches,
    )
    store = _Store()

    outcome = MeasureShadowCalibrationCommand(store, _Port(substituted)).execute(request)

    assert outcome.state == "denied"
    assert not store.successes
    assert not store.blobs


class _UnexpectedPort:
    def measure(
        self,
        request: MeasureShadowCalibrationRequest,
        member: ShadowCalibrationCorpusMember,
    ) -> ShadowCalibrationPortResult:
        raise ValueError("sensitive native exception")


def test_unexpected_port_value_error_is_fixed_terminal_failure_without_sensitive_detail() -> None:
    store = _Store()

    outcome = MeasureShadowCalibrationCommand(store, _UnexpectedPort()).execute(_request())

    assert outcome.state == "failed"
    rejection = store.rejections[0]
    assert rejection.failure_code == "SHADOW_CALIBRATION_MEASUREMENT_FAILED"
    assert "sensitive" not in rejection.failure_detail_json


def test_replay_returns_terminal_outcome_without_second_port_call_or_artifact_set() -> None:
    request = _request()
    store = _Store()
    port = _Port(_complete_result(request))
    command = MeasureShadowCalibrationCommand(store, port)

    first = command.execute(request)
    replay = command.execute(request)

    assert first == replay
    assert port.calls == 1
    assert len(store.successes) == 1
