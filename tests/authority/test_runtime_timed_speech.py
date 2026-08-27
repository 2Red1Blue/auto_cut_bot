"""PC CUDA runtime timed-speech projection tests."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from autocut_kernel.media.calibration_record import (
    CALIBRATION_VALIDATION_CHECKS,
    CALIBRATION_VALIDATOR_COMMAND,
    CALIBRATION_VALIDATOR_PRINCIPAL,
    CalibrationEvidenceMember,
    CalibrationMatchEvidence,
    CalibrationRecordIdentity,
    CalibrationRecordMemberPayload,
    CalibrationRecordProducerIdentity,
    CalibrationRecordRole,
    IndependentlyRecomputedCalibrationResult,
    build_calibration_record_candidate,
    calibration_validation_input_hash,
    calibration_validation_result_hash,
    validator_internal_assemble_accepted_artifact_set,
)
from autocut_kernel.media.runtime_measurement_identity import (
    MAC_CPU_RUNTIME_CAPABILITY_ID,
    RuntimeMeasurementIdentity,
)
from autocut_kernel.media.timing_compatibility import build_timing_compatibility_profile
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.registry.authority_profiles import (
    RuntimeCalibrationCapabilityPolicy,
    RuntimeCalibrationPolicySource,
    SourceClockPolicy,
    TimingPolicies,
)
from autocut_kernel.registry.runtime_timed_speech import (
    RuntimeTimedMediaAuthoritySelector,
    RuntimeTimedSpeechProjectionError,
)
from autocut_kernel.store.models import (
    ArtifactScope,
    CommittedArtifactMemberReference,
    PersistedCalibrationRecordAnchor,
    PersistedCommittedArtifactMember,
    PersistedRuntimeCalibrationCapability,
)


def _sha(number: int) -> str:
    return f"sha256:{number:064x}"


def _runtime_measurement(*, capability_id: str = "pc_cuda") -> RuntimeMeasurementIdentity:
    device: dict[str, str] = {"device_class": "cuda"}
    if capability_id == MAC_CPU_RUNTIME_CAPABILITY_ID:
        device = {"device_class": "cpu"}
    else:
        device.update(cuda_runtime_version="12.8", gpu_compute_capability="8.9")
    return RuntimeMeasurementIdentity(
        capability_id,
        build_timing_compatibility_profile(
            {
                "schema_version": "timing-compatibility-profile-v1",
                "timing_engine_compatibility_version": "timing-v1",
                "build_audit_sha256": _sha(100),
                "runtime": {"funasr_version": "1.0", "torch_version": "2.0", "device": device},
                "decode": {
                    "decoder_identity_sha256": _sha(101),
                    "resampling_identity_sha256": _sha(102),
                    "native_protocol_identity_sha256": _sha(103),
                },
                "policies": {
                    "word_timestamp_policy_sha256": _sha(104),
                    "vad_merge_policy_sha256": _sha(105),
                },
                "producers": [
                    {
                        "producer_kind": "asr", "producer_id": "asr", "producer_version": "1",
                        "model_id": "SenseVoiceSmall", "model_revision": "main",
                        "model_sha256": _sha(106), "inference_identity_sha256": _sha(107),
                    },
                    {
                        "producer_kind": "vad", "producer_id": "vad", "producer_version": "1",
                        "model_id": "fsmn-vad", "model_revision": "main",
                        "model_sha256": _sha(108), "inference_identity_sha256": _sha(109),
                    },
                ],
            }
        ),
    )


def _child(
    role: CalibrationRecordRole,
    *,
    identity: CalibrationRecordIdentity,
    producer: CalibrationRecordProducerIdentity,
) -> CalibrationRecordMemberPayload:
    match = CalibrationMatchEvidence.from_ranges(
        f"{role.value}-anchor", f"{role.value}-observation", TickRange(100, 200), TickRange(99, 202)
    )
    return CalibrationRecordMemberPayload.from_matches(
        identity,
        producer,
        (CalibrationEvidenceMember(0, _sha(110), _sha(111), _sha(112), _sha(113)),),
        (match,),
    )


def _clock() -> SourceClockPolicy:
    return SourceClockPolicy(_sha(1), "source-audio-clock", TimeBase(1, 1_000))


def _policies(measurement: RuntimeMeasurementIdentity) -> TimingPolicies:
    compatibility = measurement.timing_compatibility
    return TimingPolicies(
        compatibility.word_timestamp_policy_sha256,
        _sha(2),
        compatibility.vad_merge_policy_sha256,
        _sha(3),
        _sha(4),
        300,
        200,
    )


def _policy() -> RuntimeCalibrationPolicySource:
    return RuntimeCalibrationPolicySource(
        _sha(10), _sha(11), _sha(12), _sha(13),
        (RuntimeCalibrationCapabilityPolicy("pc_cuda", "cuda"),),
    )


def _producer(
    measurement: RuntimeMeasurementIdentity, role: CalibrationRecordRole, *, model_sha256: str | None = None,
) -> CalibrationRecordProducerIdentity:
    compatibility = measurement.timing_compatibility.producers[
        0 if role is CalibrationRecordRole.ASR else 1
    ]
    return CalibrationRecordProducerIdentity(
        role=role,
        producer_id=compatibility.producer_id,
        producer_version=compatibility.producer_version,
        generation_policy_sha256=_sha(20 if role is CalibrationRecordRole.ASR else 21),
        detector_sha256=_sha(22 if role is CalibrationRecordRole.ASR else 23),
        calibration_policy_sha256=_sha(24 if role is CalibrationRecordRole.ASR else 25),
        model_id=compatibility.model_id,
        model_revision=compatibility.model_revision,
        model_sha256=model_sha256 or compatibility.model_sha256,
        inference_kind=(
            "sensevoice-word-timestamp"
            if role is CalibrationRecordRole.ASR
            else "fsmn-vad-direct"
        ),
        # This is deliberately a historical/audit value, not an admission check.
        service_sha256=_sha(26),
    )


def _accepted_record(
    measurement: RuntimeMeasurementIdentity,
    *,
    runtime_identity_sha256: str | None = None,
    source_clock_id: str | None = None,
    timed_speech_policy_sha256: str | None = None,
    asr_model_sha256: str | None = None,
):
    clock = _clock()
    policies = _policies(measurement)
    compatibility = measurement.timing_compatibility
    identity = CalibrationRecordIdentity(
        _policy().profile_source_sha256,
        _policy().registry_snapshot_sha256,
        _sha(30),
        compatibility.native_protocol_identity_sha256,
        source_clock_id or clock.clock_id,
        clock.time_base,
        timed_speech_policy_sha256 or policies.timed_speech_policy_sha256,
        policies.word_gap_policy_sha256,
        policies.vad_merge_policy_sha256,
        policies.alignment_policy_sha256,
        policies.acceptance_policy_sha256,
        runtime_identity_sha256 or measurement.canonical_sha256,
    )
    candidate = build_calibration_record_candidate(
        profile_version="1",
        runtime_capability_id="pc_cuda",
        identity=identity,
        measurement_manifest_sha256=_sha(31),
        measurement_results_sha256=_sha(32),
        asr=_child(
            CalibrationRecordRole.ASR,
            identity=identity,
            producer=_producer(measurement, CalibrationRecordRole.ASR, model_sha256=asr_model_sha256),
        ),
        vad=_child(
            CalibrationRecordRole.VAD,
            identity=identity,
            producer=_producer(measurement, CalibrationRecordRole.VAD),
        ),
    )
    proof = IndependentlyRecomputedCalibrationResult(
        candidate,
        CALIBRATION_VALIDATION_CHECKS,
        calibration_validation_input_hash(
            profile_key=candidate.profile_key,
            identity=identity,
            measurement_manifest_sha256=candidate.aggregate.measurement_manifest_sha256,
            measurement_results_sha256=candidate.aggregate.measurement_results_sha256,
            asr=candidate.asr,
            vad=candidate.vad,
        ),
        calibration_validation_result_hash(candidate.aggregate, candidate.asr, candidate.vad),
        CALIBRATION_VALIDATOR_COMMAND,
        CALIBRATION_VALIDATOR_PRINCIPAL,
    )
    return validator_internal_assemble_accepted_artifact_set(proof)


def _capability(
    measurement: RuntimeMeasurementIdentity, **record_kwargs: object
) -> PersistedRuntimeCalibrationCapability:
    record = _accepted_record(measurement, **record_kwargs)

    def persisted(ordinal: int) -> PersistedCommittedArtifactMember:
        member = record.members[ordinal]
        reference = CommittedArtifactMemberReference(
            UUID(int=1), UUID(int=2), ordinal,
            ArtifactScope(member.scope.namespace, member.scope.kind, member.scope.key),
            member.artifact_type, member.logical_id, member.revision, member.content_hash,
        )
        return PersistedCommittedArtifactMember(reference, member.payload_json, UUID(int=3))

    return PersistedRuntimeCalibrationCapability(
        measurement, PersistedCalibrationRecordAnchor(record, persisted(0), persisted(3))
    )


def _selector(measurement: RuntimeMeasurementIdentity) -> RuntimeTimedMediaAuthoritySelector:
    return RuntimeTimedMediaAuthoritySelector(_policy(), _clock(), _policies(measurement))


def test_projects_closed_pc_cuda_runtime_timed_speech_authority() -> None:
    measurement = _runtime_measurement()
    projection = _selector(measurement).select(_capability(measurement), measurement)

    assert projection.runtime_capability_id == "pc_cuda"
    assert projection.device_class == "cuda"
    assert projection.runtime_measurement_identity_sha256 == measurement.canonical_sha256
    assert projection.timing_compatibility_sha256 == measurement.timing_compatibility_sha256
    assert projection.funasr_version == measurement.timing_compatibility.funasr_version
    assert projection.torch_version == measurement.timing_compatibility.torch_version
    assert projection.producers[0].model_sha256 == measurement.timing_compatibility.producers[0].model_sha256
    assert projection.asr_calibration_record_sha256 == _capability(measurement).anchor.record.asr.content_hash
    assert projection.vad_calibration_record_sha256 == _capability(measurement).anchor.record.vad.content_hash
    assert projection.asr_timing_error_bound_tick == _capability(measurement).anchor.record.asr.accepted_bound_tick
    assert projection.vad_timing_error_bound_tick == _capability(measurement).anchor.record.vad.accepted_bound_tick
    assert projection.canonical_hash != projection.compatibility_hash
    assert projection.to_mapping()["accepted_calibration"] == {
        "record_sha256": projection.record_sha256,
        "validation_receipt_sha256": projection.validation_receipt_sha256,
        "producers": [
            {
                **projection.producers[0].to_mapping(),
                "calibration_record_sha256": projection.asr_calibration_record_sha256,
                "timing_error_bound_tick": projection.asr_timing_error_bound_tick,
            },
            {
                **projection.producers[1].to_mapping(),
                "calibration_record_sha256": projection.vad_calibration_record_sha256,
                "timing_error_bound_tick": projection.vad_timing_error_bound_tick,
            },
        ],
    }


def test_rejects_cpu_cuda_cross_binding() -> None:
    cuda = _runtime_measurement()
    cpu = _runtime_measurement(capability_id=MAC_CPU_RUNTIME_CAPABILITY_ID)

    with pytest.raises(RuntimeTimedSpeechProjectionError, match="pc_cuda"):
        _selector(cuda).select(PersistedRuntimeCalibrationCapability(cpu, _capability(cuda).anchor), cpu)
    cpu_only_policy = RuntimeCalibrationPolicySource(
        _sha(10), _sha(11), _sha(12), _sha(13),
        (RuntimeCalibrationCapabilityPolicy("mac_cpu", "cpu"),),
    )
    with pytest.raises(RuntimeTimedSpeechProjectionError, match="static policy"):
        RuntimeTimedMediaAuthoritySelector(cpu_only_policy, _clock(), _policies(cuda)).select(
            _capability(cuda), cuda
        )


@pytest.mark.parametrize(
    ("record_kwargs", "message"),
    (
        ({"runtime_identity_sha256": _sha(40)}, "aggregate runtime identity"),
        ({"asr_model_sha256": _sha(41)}, "producer metadata"),
        ({"source_clock_id": "foreign-clock"}, "source clock"),
        ({"timed_speech_policy_sha256": _sha(42)}, "static timing policy"),
    ),
)
def test_rejects_tampered_runtime_record_closure(record_kwargs: dict[str, object], message: str) -> None:
    measurement = _runtime_measurement()

    with pytest.raises(RuntimeTimedSpeechProjectionError, match=message):
        _selector(measurement).select(_capability(measurement, **record_kwargs), measurement)


def test_audit_only_build_change_projects_when_compatibility_is_equal() -> None:
    measured_at_calibration = _runtime_measurement()
    rebuilt = RuntimeMeasurementIdentity(
        "pc_cuda",
        replace(measured_at_calibration.timing_compatibility, build_audit_sha256=_sha(99)),
    )
    assert rebuilt.build_audit_sha256 != measured_at_calibration.build_audit_sha256
    assert rebuilt.timing_compatibility_sha256 == measured_at_calibration.timing_compatibility_sha256
    assert rebuilt.canonical_sha256 == measured_at_calibration.canonical_sha256

    projection = _selector(rebuilt).select(_capability(measured_at_calibration), rebuilt)
    assert projection.build_audit_sha256 == rebuilt.build_audit_sha256
    assert projection.timing_compatibility_sha256 == measured_at_calibration.timing_compatibility_sha256
    assert projection.compatibility_hash == _selector(measured_at_calibration).select(
        _capability(measured_at_calibration), measured_at_calibration
    ).compatibility_hash
    assert projection.canonical_hash != _selector(measured_at_calibration).select(
        _capability(measured_at_calibration), measured_at_calibration
    ).canonical_hash
