"""Pure PC-CUDA timed-speech projection from an accepted runtime capability.

This is deliberately a projection boundary, not a Store lookup or a service
configuration seam.  The caller must first resolve one accepted capability;
this module then proves that its immutable record, static policy lineage, and
live CUDA measurement describe the same timed-speech authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..media.calibration_record import (
    CalibrationRecordProducerIdentity,
    CalibrationRecordRole,
    runtime_calibration_profile_key,
    verify_calibration_record_artifact_set,
)
from ..media.runtime_measurement_identity import (
    PC_CUDA_RUNTIME_CAPABILITY_ID,
    RuntimeMeasurementIdentity,
)
from ..media.types import TimeBase, canonical_sha256, sha256_prefixed
from ..store.models import PersistedRuntimeCalibrationCapability
from .authority_profiles import (
    RuntimeCalibrationPolicySource,
    SourceClockPolicy,
    TimingPolicies,
)


class RuntimeTimedSpeechProjectionError(ValueError):
    """A capability cannot safely authorize the PC CUDA timed-speech path."""


def _fail(detail: str) -> RuntimeTimedSpeechProjectionError:
    return RuntimeTimedSpeechProjectionError(
        f"runtime timed-speech projection rejected: {detail}"
    )


@dataclass(frozen=True, slots=True)
class RuntimeTimedSpeechProjection:
    """Request-facing immutable closure for one PC CUDA timed-speech call.

    ``build_audit_sha256`` is exposed for provenance only.  Admission is bound
    to ``runtime_measurement_identity_sha256`` and the derived timing
    compatibility hash, so an audit-only rebuild remains usable.
    """

    runtime_capability_id: str
    runtime_measurement_identity_sha256: str
    timing_compatibility_sha256: str
    build_audit_sha256: str
    profile_source_sha256: str
    registry_snapshot_sha256: str
    record_sha256: str
    validation_receipt_sha256: str
    asr_calibration_record_sha256: str
    vad_calibration_record_sha256: str
    asr_timing_error_bound_tick: int
    vad_timing_error_bound_tick: int
    native_port_identity_sha256: str
    source_clock_id: str
    source_time_base: TimeBase
    timed_speech_policy_sha256: str
    word_gap_policy_sha256: str
    vad_merge_policy_sha256: str
    alignment_policy_sha256: str
    acceptance_policy_sha256: str
    producers: tuple[CalibrationRecordProducerIdentity, CalibrationRecordProducerIdentity]

    def __post_init__(self) -> None:
        """Keep the physical timing bounds inside the request-facing closure.

        A caller must never recreate these bounds from the static policy.  They
        are observations accepted by the immutable ASR/VAD children of the
        selected CUDA CalibrationRecord and must therefore travel with the
        capability-specific request and its eventual Receipt.
        """
        for field_name in (
            "runtime_measurement_identity_sha256",
            "timing_compatibility_sha256",
            "build_audit_sha256",
            "profile_source_sha256",
            "registry_snapshot_sha256",
            "record_sha256",
            "validation_receipt_sha256",
            "asr_calibration_record_sha256",
            "vad_calibration_record_sha256",
            "native_port_identity_sha256",
            "timed_speech_policy_sha256",
            "word_gap_policy_sha256",
            "vad_merge_policy_sha256",
            "alignment_policy_sha256",
            "acceptance_policy_sha256",
        ):
            try:
                sha256_prefixed(getattr(self, field_name), field_name)
            except ValueError as error:
                raise _fail(f"{field_name} is not a sha256 digest") from error
        if (
            type(self.asr_timing_error_bound_tick) is not int  # noqa: E721
            or type(self.vad_timing_error_bound_tick) is not int  # noqa: E721
            or self.asr_timing_error_bound_tick < 1
            or self.vad_timing_error_bound_tick < 1
        ):
            raise _fail("accepted ASR/VAD timing bounds must be positive integers")
        if (
            self.asr_calibration_record_sha256 == self.vad_calibration_record_sha256
            or self.record_sha256 == self.validation_receipt_sha256
        ):
            raise _fail("runtime timing evidence references must be distinct")
        if (
            type(self.producers) is not tuple  # noqa: E721
            or len(self.producers) != 2
            or self.producers[0].role is not CalibrationRecordRole.ASR
            or self.producers[1].role is not CalibrationRecordRole.VAD
        ):
            raise _fail("runtime timing producers must be ordered ASR then VAD")

    @property
    def device_class(self) -> str:
        return "cuda"

    def _base_mapping(self) -> dict[str, object]:
        """Return the exact accepted closure without audit-only provenance."""
        return {
            "schema_version": "runtime-timed-speech-projection-v1",
            "runtime_capability_id": self.runtime_capability_id,
            "runtime_measurement_identity_sha256": self.runtime_measurement_identity_sha256,
            "timing_compatibility_sha256": self.timing_compatibility_sha256,
            "static_policy": {
                "profile_source_sha256": self.profile_source_sha256,
                "registry_snapshot_sha256": self.registry_snapshot_sha256,
            },
            "accepted_calibration": {
                "record_sha256": self.record_sha256,
                "validation_receipt_sha256": self.validation_receipt_sha256,
                "producers": [
                    {
                        **producer.to_mapping(),
                        "calibration_record_sha256": record_sha256,
                        "timing_error_bound_tick": bound_tick,
                    }
                    for producer, record_sha256, bound_tick in zip(
                        self.producers,
                        (
                            self.asr_calibration_record_sha256,
                            self.vad_calibration_record_sha256,
                        ),
                        (
                            self.asr_timing_error_bound_tick,
                            self.vad_timing_error_bound_tick,
                        ),
                        strict=True,
                    )
                ],
            },
            "native_port_identity_sha256": self.native_port_identity_sha256,
            "source_clock": {
                "clock_id": self.source_clock_id,
                "time_base": {
                    "numerator": self.source_time_base.numerator,
                    "denominator": self.source_time_base.denominator,
                },
            },
            "timing_policies": {
                "timed_speech_policy_sha256": self.timed_speech_policy_sha256,
                "word_gap_policy_sha256": self.word_gap_policy_sha256,
                "vad_merge_policy_sha256": self.vad_merge_policy_sha256,
                "alignment_policy_sha256": self.alignment_policy_sha256,
                "acceptance_policy_sha256": self.acceptance_policy_sha256,
            },
        }

    def compatibility_mapping(self) -> dict[str, object]:
        """The closed acceptance identity; audit-only rebuilds retain it."""
        return self._base_mapping()

    def to_mapping(self) -> dict[str, object]:
        """Complete request/Receipt projection, including build provenance."""
        return {**self._base_mapping(), "build_audit_sha256": self.build_audit_sha256}

    @property
    def compatibility_hash(self) -> str:
        return canonical_sha256(self.compatibility_mapping())

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class RuntimeTimedMediaAuthoritySelector:
    """Select the one PC CUDA authority allowed by a fixed static policy."""

    policy: RuntimeCalibrationPolicySource
    source_clock_policy: SourceClockPolicy
    timing_policies: TimingPolicies

    def __post_init__(self) -> None:
        if type(self.policy) is not RuntimeCalibrationPolicySource:  # noqa: E721
            raise _fail("requires an exact static runtime calibration policy")
        if type(self.source_clock_policy) is not SourceClockPolicy:  # noqa: E721
            raise _fail("requires an exact static source-clock policy")
        if type(self.timing_policies) is not TimingPolicies:  # noqa: E721
            raise _fail("requires exact static timing policies")

    def select(
        self,
        capability: PersistedRuntimeCalibrationCapability,
        measurement: RuntimeMeasurementIdentity,
    ) -> RuntimeTimedSpeechProjection:
        return project_runtime_timed_speech(
            capability=capability,
            measurement=measurement,
            policy=self.policy,
            source_clock_policy=self.source_clock_policy,
            timing_policies=self.timing_policies,
        )


def _require_producer_alignment(
    producer: CalibrationRecordProducerIdentity,
    measurement: RuntimeMeasurementIdentity,
    role: CalibrationRecordRole,
) -> None:
    if producer.role is not role:
        raise _fail("accepted record producers are not ordered ASR then VAD")
    expected = measurement.timing_compatibility.producers[
        0 if role is CalibrationRecordRole.ASR else 1
    ]
    if (
        producer.producer_id != expected.producer_id
        or producer.producer_version != expected.producer_version
        or producer.model_id != expected.model_id
        or producer.model_revision != expected.model_revision
        or producer.model_sha256 != expected.model_sha256
    ):
        raise _fail("accepted record producer metadata differs from timing compatibility")


def project_runtime_timed_speech(
    *,
    capability: PersistedRuntimeCalibrationCapability,
    measurement: RuntimeMeasurementIdentity,
    policy: RuntimeCalibrationPolicySource,
    source_clock_policy: SourceClockPolicy,
    timing_policies: TimingPolicies,
) -> RuntimeTimedSpeechProjection:
    """Return a closed PC CUDA request projection or reject every mismatch.

    The measurement's build audit hash is intentionally not compared with the
    accepted record's producer service hashes: those hashes preserve build
    provenance, while the measured compatibility identity is the admission
    equality.  Physical record/receipt references remain exact in the result.
    """
    if type(capability) is not PersistedRuntimeCalibrationCapability:  # noqa: E721
        raise _fail("requires an exact persisted runtime calibration capability")
    if type(policy) is not RuntimeCalibrationPolicySource:  # noqa: E721
        raise _fail("requires an exact static runtime calibration policy")
    if type(source_clock_policy) is not SourceClockPolicy:  # noqa: E721
        raise _fail("requires an exact static source-clock policy")
    if type(timing_policies) is not TimingPolicies:  # noqa: E721
        raise _fail("requires exact static timing policies")

    if (
        type(measurement) is not RuntimeMeasurementIdentity  # noqa: E721
        or measurement.runtime_capability_id != PC_CUDA_RUNTIME_CAPABILITY_ID
        or measurement.timing_compatibility.device.device_class != "cuda"
    ):
        raise _fail("only the pc_cuda capability may project timed speech")
    if not policy.accepts(measurement):
        raise _fail("PC CUDA measurement is not admitted by the static policy")
    accepted_measurement = capability.measurement_identity
    if (
        accepted_measurement.runtime_capability_id != PC_CUDA_RUNTIME_CAPABILITY_ID
        or accepted_measurement.canonical_sha256 != measurement.canonical_sha256
    ):
        raise _fail("accepted capability is not compatible with the live PC CUDA measurement")

    anchor = capability.anchor
    record = anchor.record
    try:
        verify_calibration_record_artifact_set(record.members)
    except ValueError as error:
        raise _fail("accepted calibration record closure is invalid") from error
    if (
        anchor.aggregate.reference.content_hash != record.members[0].content_hash
        or anchor.validation.reference.content_hash != record.members[3].content_hash
        or anchor.aggregate.payload_json != record.members[0].payload_json
        or anchor.validation.payload_json != record.members[3].payload_json
    ):
        raise _fail("persisted capability anchor differs from its exact record closure")

    aggregate = record.aggregate
    identity = aggregate.identity
    if record.members[0].scope.key != runtime_calibration_profile_key(
        record.members[0].scope.key.rsplit("@", 1)[-1], PC_CUDA_RUNTIME_CAPABILITY_ID
    ):
        raise _fail("accepted record is not scoped to pc_cuda")
    if identity.runtime_measurement_identity_sha256 != accepted_measurement.canonical_sha256:
        raise _fail("aggregate runtime identity differs from the selected measurement")
    if (
        identity.profile_source_sha256 != policy.profile_source_sha256
        or identity.registry_snapshot_sha256 != policy.registry_snapshot_sha256
    ):
        raise _fail("aggregate static policy lineage differs from the selected policy")
    if (
        identity.source_clock_id != source_clock_policy.clock_id
        or identity.source_time_base != source_clock_policy.time_base
    ):
        raise _fail("aggregate source clock differs from the static clock policy")
    if (
        identity.timed_speech_policy_sha256 != timing_policies.timed_speech_policy_sha256
        or identity.word_gap_policy_sha256 != timing_policies.word_gap_policy_sha256
        or identity.vad_merge_policy_sha256 != timing_policies.vad_merge_policy_sha256
        or identity.alignment_policy_sha256 != timing_policies.alignment_policy_sha256
        or identity.acceptance_policy_sha256 != timing_policies.acceptance_policy_sha256
    ):
        raise _fail("aggregate timing policy differs from the static timing policy")

    compatibility = measurement.timing_compatibility
    if (
        identity.native_port_identity_sha256
        != compatibility.native_protocol_identity_sha256
        or identity.timed_speech_policy_sha256
        != compatibility.word_timestamp_policy_sha256
        or identity.vad_merge_policy_sha256 != compatibility.vad_merge_policy_sha256
    ):
        raise _fail("aggregate timing policy differs from the measured compatibility profile")
    _require_producer_alignment(record.asr.producer_identity, measurement, CalibrationRecordRole.ASR)
    _require_producer_alignment(record.vad.producer_identity, measurement, CalibrationRecordRole.VAD)

    return RuntimeTimedSpeechProjection(
        runtime_capability_id=measurement.runtime_capability_id,
        runtime_measurement_identity_sha256=measurement.canonical_sha256,
        timing_compatibility_sha256=measurement.timing_compatibility_sha256,
        build_audit_sha256=measurement.build_audit_sha256,
        profile_source_sha256=identity.profile_source_sha256,
        registry_snapshot_sha256=identity.registry_snapshot_sha256,
        record_sha256=anchor.record_sha256,
        validation_receipt_sha256=anchor.validation_receipt_sha256,
        asr_calibration_record_sha256=record.asr.content_hash,
        vad_calibration_record_sha256=record.vad.content_hash,
        asr_timing_error_bound_tick=record.asr.accepted_bound_tick,
        vad_timing_error_bound_tick=record.vad.accepted_bound_tick,
        native_port_identity_sha256=identity.native_port_identity_sha256,
        source_clock_id=identity.source_clock_id,
        source_time_base=identity.source_time_base,
        timed_speech_policy_sha256=identity.timed_speech_policy_sha256,
        word_gap_policy_sha256=identity.word_gap_policy_sha256,
        vad_merge_policy_sha256=identity.vad_merge_policy_sha256,
        alignment_policy_sha256=identity.alignment_policy_sha256,
        acceptance_policy_sha256=identity.acceptance_policy_sha256,
        producers=(record.asr.producer_identity, record.vad.producer_identity),
    )


__all__ = [
    "RuntimeTimedMediaAuthoritySelector",
    "RuntimeTimedSpeechProjection",
    "RuntimeTimedSpeechProjectionError",
    "project_runtime_timed_speech",
]
