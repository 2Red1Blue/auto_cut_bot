"""Shared read-only source/accepted-calibration binding for build and startup.

Decoded sources are not capabilities. The controlled caller supplies a real Store;
a typed fake reader does not prove database acceptance or build provenance.
"""

from __future__ import annotations

from typing import Protocol

from ..media.calibration_record import (
    CalibrationRecordIdentity,
    verify_calibration_record_artifact_set,
)
from ..media.runtime_measurement_identity import RuntimeMeasurementIdentity
from ..media.types import sha256_prefixed
from ..store.models import (
    CommittedArtifactMemberReference,
    PersistedCalibrationRecordAnchor,
    PersistedRuntimeCalibrationCapability,
)
from .authority_profiles import (
    LocalRunProfileSource,
    RuntimeCalibrationPolicySource,
    ShadowCalibrationProfileSource,
)


class CalibrationBindingError(ValueError):
    """An accepted record does not close over its exact source projection."""


class CalibrationRecordAnchorReader(Protocol):
    def read_calibration_record_anchor(
        self,
        aggregate_reference: CommittedArtifactMemberReference,
        validation_reference: CommittedArtifactMemberReference,
        *,
        expected_profile_source_sha256: str,
        expected_registry_snapshot_sha256: str,
    ) -> PersistedCalibrationRecordAnchor: ...


class RuntimeCalibrationCapabilityReader(Protocol):
    def read_runtime_calibration_capability(
        self,
        *,
        profile_source_sha256: str,
        registry_snapshot_sha256: str,
        measurement_identity: RuntimeMeasurementIdentity,
    ) -> PersistedRuntimeCalibrationCapability: ...


def bind_runtime_calibration_capability(
    *,
    policy: RuntimeCalibrationPolicySource,
    measurement_identity: RuntimeMeasurementIdentity,
    store: RuntimeCalibrationCapabilityReader,
) -> PersistedRuntimeCalibrationCapability:
    """Resolve one exact v2 capability; a v1 record is never an admission fallback."""
    if type(policy) is not RuntimeCalibrationPolicySource:  # noqa: E721
        raise CalibrationBindingError("runtime capability requires an exact static policy")
    if type(measurement_identity) is not RuntimeMeasurementIdentity:  # noqa: E721
        raise CalibrationBindingError("runtime capability requires an exact measured identity")
    if not policy.accepts(measurement_identity):
        raise CalibrationBindingError("measured runtime identity is not allowed by static policy")
    capability = store.read_runtime_calibration_capability(
        profile_source_sha256=policy.profile_source_sha256,
        registry_snapshot_sha256=policy.registry_snapshot_sha256,
        measurement_identity=measurement_identity,
    )
    if type(capability) is not PersistedRuntimeCalibrationCapability:  # noqa: E721
        raise CalibrationBindingError("reader did not return an exact v2 runtime capability")
    if capability.measurement_identity != measurement_identity:
        raise CalibrationBindingError("accepted runtime capability differs from live measured identity")
    record_identity = capability.anchor.record.aggregate.identity
    if (
        record_identity.profile_source_sha256 != policy.profile_source_sha256
        or record_identity.registry_snapshot_sha256 != policy.registry_snapshot_sha256
    ):
        raise CalibrationBindingError("accepted runtime capability differs from static profile/registry")
    return capability


def bind_profile_calibration(
    *,
    local_run: LocalRunProfileSource,
    shadow: ShadowCalibrationProfileSource,
    predecessor_registry_sha256: str,
    store: CalibrationRecordAnchorReader,
) -> PersistedCalibrationRecordAnchor:
    """Read exact refs and compare all producer, bound, clock and corpus identities."""
    if type(local_run) is not LocalRunProfileSource or type(shadow) is not ShadowCalibrationProfileSource:  # noqa: E721
        raise CalibrationBindingError("requires exact decoded local-run and shadow sources")
    try:
        sha256_prefixed(predecessor_registry_sha256, "predecessor_registry_sha256")
    except ValueError as error:
        raise CalibrationBindingError("requires a valid predecessor Registry identity") from error
    if predecessor_registry_sha256 == "sha256:" + "0" * 64:
        raise CalibrationBindingError("predecessor Registry identity must not be a placeholder")
    calibration = local_run.calibration
    registry_hash = predecessor_registry_sha256
    anchor = store.read_calibration_record_anchor(
        calibration.record_ref, calibration.validation_receipt_ref,
        expected_profile_source_sha256=shadow.source_sha256,
        expected_registry_snapshot_sha256=registry_hash,
    )
    if type(anchor) is not PersistedCalibrationRecordAnchor:  # noqa: E721
        raise CalibrationBindingError("reader did not return an accepted record anchor")
    record = anchor.record
    verify_calibration_record_artifact_set(record.members)
    if (
        anchor.aggregate.reference != calibration.record_ref
        or anchor.validation.reference != calibration.validation_receipt_ref
        or anchor.aggregate.payload_json != record.members[0].payload_json
        or anchor.validation.payload_json != record.members[3].payload_json
    ):
        raise CalibrationBindingError("accepted anchor differs from exact local-run references")
    policies, clock = shadow.timing_policies, shadow.source_clock_policy
    expected_identity = CalibrationRecordIdentity(
        shadow.source_sha256, registry_hash, shadow.calibration_corpus.corpus_set_sha256,
        shadow.native_timed_speech.native_port_identity_sha256, clock.clock_id, clock.time_base,
        policies.timed_speech_policy_sha256, policies.word_gap_policy_sha256,
        policies.vad_merge_policy_sha256, policies.alignment_policy_sha256,
        policies.acceptance_policy_sha256,
    )
    if record.aggregate.identity != expected_identity:
        raise CalibrationBindingError("accepted calibration identity differs from locked predecessor")
    expected_evidence = tuple(
        (ordinal, member.corpus_member_reference_sha256, member.expected_anchor_reference_sha256)
        for ordinal, member in enumerate(shadow.calibration_corpus.members)
    )
    for child, producer, child_hash, bound in zip(
        (record.asr, record.vad), shadow.native_timed_speech.producers,
        (calibration.asr_producer_record_sha256, calibration.vad_producer_record_sha256),
        (calibration.asr_timing_error_bound_tick, calibration.vad_timing_error_bound_tick), strict=True,
    ):
        evidence = tuple(
            (member.ordinal, member.corpus_member_reference_sha256, member.expected_anchor_reference_sha256)
            for member in child.evidence_members
        )
        if (
            child.producer_identity.to_mapping() != producer.common_mapping()
            or child.content_hash != child_hash
            or child.accepted_bound_tick != bound
            or evidence != expected_evidence
        ):
            raise CalibrationBindingError("accepted producer, bound or corpus differs from locked source")
    return anchor
