"""Bind locked local-run inputs to the Store's immutable accepted record.

This build-side comparison neither accepts caller record bytes nor grants a
runtime capability. Production must obtain the context from the verified Git
builder and inject the real Store reader. A typed context/fake reader alone
does not establish provenance, calibration acceptance, or bootstrap authority.
"""

from __future__ import annotations

from typing import Protocol

from autocut_kernel.media.calibration_record import (
    CalibrationRecordIdentity,
    verify_calibration_record_artifact_set,
)
from autocut_kernel.store.models import (
    CommittedArtifactMemberReference,
    PersistedCalibrationRecordAnchor,
)

from .errors import GateViolation
from .local_run_context import LockedLocalRunSourceContext


class CalibrationRecordAnchorReader(Protocol):
    def read_calibration_record_anchor(
        self,
        aggregate_reference: CommittedArtifactMemberReference,
        validation_reference: CommittedArtifactMemberReference,
        *,
        expected_profile_source_sha256: str,
        expected_registry_snapshot_sha256: str,
    ) -> PersistedCalibrationRecordAnchor: ...


def bind_local_run_calibration(
    *, context: LockedLocalRunSourceContext, store: CalibrationRecordAnchorReader,
) -> PersistedCalibrationRecordAnchor:
    """Read exact accepted refs and close all locked source/record projections.

    Reader failure propagates unchanged: absence/denial is never repaired with
    a latest-head lookup, an in-memory record, or another native measurement.
    """
    if type(context) is not LockedLocalRunSourceContext:  # noqa: E721
        raise GateViolation("AUTH-LOCAL-CALIBRATION", "requires a locked local-run source context")
    shadow = context.predecessor.profiles.shadow
    calibration = context.local_run.calibration
    registry_hash = context.predecessor.compilation.registry_set.source_hash
    anchor = store.read_calibration_record_anchor(
        calibration.record_ref, calibration.validation_receipt_ref,
        expected_profile_source_sha256=shadow.source_sha256,
        expected_registry_snapshot_sha256=registry_hash,
    )
    if type(anchor) is not PersistedCalibrationRecordAnchor:  # noqa: E721
        raise GateViolation("AUTH-LOCAL-CALIBRATION", "reader did not return an accepted record anchor")
    record = anchor.record
    verify_calibration_record_artifact_set(record.members)
    if (
        anchor.aggregate.reference != calibration.record_ref
        or anchor.validation.reference != calibration.validation_receipt_ref
        or anchor.aggregate.payload_json != record.members[0].payload_json
        or anchor.validation.payload_json != record.members[3].payload_json
    ):
        raise GateViolation("AUTH-LOCAL-CALIBRATION", "accepted anchor differs from exact local-run references")
    policies, clock = shadow.timing_policies, shadow.source_clock_policy
    expected_identity = CalibrationRecordIdentity(
        shadow.source_sha256, registry_hash, shadow.calibration_corpus.corpus_set_sha256,
        shadow.native_timed_speech.native_port_identity_sha256, clock.clock_id, clock.time_base,
        policies.timed_speech_policy_sha256, policies.word_gap_policy_sha256,
        policies.vad_merge_policy_sha256, policies.alignment_policy_sha256,
        policies.acceptance_policy_sha256,
    )
    if record.aggregate.identity != expected_identity:
        raise GateViolation("AUTH-LOCAL-CALIBRATION", "accepted calibration identity differs from locked predecessor")
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
            raise GateViolation("AUTH-LOCAL-CALIBRATION", "accepted producer, bound or corpus differs from locked source")
    return anchor
