"""Exact bounded three-member physical replay, without producers or speech."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..media.physical_root_codec import decode_physical_root_media_evidence_json
from ..media.root_evidence_codec import decode_media_evidence_json
from ..media.stage4_predecessor import (
    CommittedVideoToAudioClockMapCertificate,
    PresentationTimelineProbe,
)
from ..media.timed_evidence_codec import decode_calibration_binding
from ..store.models import (
    BlobRef,
    CommandOutcome,
    Job,
    MaterializationLimits,
    PersistedCommittedArtifactSet,
    VerifiedMaterializedBlob,
    canonical_recipe_scope,
)
from .physical_media_contract import (
    PHYSICAL_ROOT_MEDIA_TYPE,
    PREPARE_PHYSICAL_MEDIA_EVIDENCE_COMMAND,
    PreparePhysicalMediaEvidenceRequest,
    ProducedPhysicalMediaEvidence,
    ResolvedPreparePhysicalMediaEvidenceRequest,
    physical_array,
    physical_json,
    physical_object,
    positive_limit,
)
from .prepare_physical_media_evidence_command import (
    physical_member_layout,
    physical_metadata_payloads,
    resolve_physical_media_request,
    validate_produced_physical_media_evidence,
)
from .prepare_timed_media_evidence_command import CommittedMediaInputsStore


class PhysicalMediaReadError(ValueError):
    """Committed physical facts cannot be safely and independently replayed."""


@dataclass(frozen=True, slots=True)
class PhysicalMediaReadLimits:
    max_evidence_bytes: int
    max_metadata_bytes: int
    materialization: MaterializationLimits

    def __post_init__(self) -> None:
        positive_limit(self.max_evidence_bytes, "read evidence bytes")
        positive_limit(self.max_metadata_bytes, "read metadata bytes")
        if (type(self.materialization) is not MaterializationLimits
                or self.max_evidence_bytes > self.materialization.effective_max_source_bytes):
            raise PhysicalMediaReadError("physical reader requires explicit sufficient materialization limits")


class PhysicalMediaReadStore(CommittedMediaInputsStore, Protocol):
    def read_committed_artifact_set(self, job: Job, *, command_slot_id: UUID, receipt_id: UUID,
                                    artifact_set_id: UUID, expected_request_hash: str,
                                    expected_command_name: str, expected_execution_kind: str) -> PersistedCommittedArtifactSet: ...

    def materialize_immutable_blob(self, job: Job, reference: BlobRef,
                                   limits: MaterializationLimits) -> VerifiedMaterializedBlob: ...


@dataclass(frozen=True, slots=True)
class PersistedPhysicalMediaEvidence:
    """Content projection only: direct construction is not Store admission."""

    record: PersistedCommittedArtifactSet
    resolved: ResolvedPreparePhysicalMediaEvidenceRequest
    produced: ProducedPhysicalMediaEvidence
    probe: PresentationTimelineProbe
    certificate: CommittedVideoToAudioClockMapCertificate


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise PhysicalMediaReadError("physical persisted identifier must be text")
    return value


def _blob(value: object) -> BlobRef:
    data = physical_object(value, ("object_id", "content_hash", "byte_length", "media_type"))
    reference = BlobRef(UUID(_text(data["object_id"])), _text(data["content_hash"]),
                        positive_limit(data["byte_length"], "root blob length"), _text(data["media_type"]))
    if reference.media_type != PHYSICAL_ROOT_MEDIA_TYPE or str(reference.object_id) != data["object_id"]:
        raise PhysicalMediaReadError("physical blob type or UUID representation differs")
    return reference


def _read_root_blob(store: PhysicalMediaReadStore, job: Job, reference: BlobRef,
                    limits: PhysicalMediaReadLimits) -> bytes:
    lease = store.materialize_immutable_blob(job, reference, limits.materialization)
    try:
        if lease.reference != reference:
            raise PhysicalMediaReadError("physical materialized reference differs")
        with lease.path.open("rb") as stream:
            raw = stream.read(reference.byte_length + 1)
        if len(raw) != reference.byte_length or "sha256:" + hashlib.sha256(raw).hexdigest() != reference.content_hash:
            raise PhysicalMediaReadError("physical blob bytes differ from committed length/hash")
        return raw
    finally:
        lease.close()


def read_committed_physical_media_evidence(
    store: PhysicalMediaReadStore, request: PreparePhysicalMediaEvidenceRequest, outcome: CommandOutcome,
    *, limits: PhysicalMediaReadLimits,
) -> PersistedPhysicalMediaEvidence:
    try:
        return _read(store, request, outcome, limits)
    except PhysicalMediaReadError:
        raise
    except (ValueError, TypeError, KeyError) as error:
        raise PhysicalMediaReadError("committed physical evidence is invalid") from error


def _read(store: PhysicalMediaReadStore, request: PreparePhysicalMediaEvidenceRequest,
          outcome: CommandOutcome, limits: PhysicalMediaReadLimits) -> PersistedPhysicalMediaEvidence:
    if type(limits) is not PhysicalMediaReadLimits or type(request) is not PreparePhysicalMediaEvidenceRequest:
        raise PhysicalMediaReadError("reader requires exact request and explicit limits")
    if (type(outcome) is not CommandOutcome or outcome.state != "succeeded"
            or any(type(value) is not UUID for value in (outcome.job_id, outcome.command_slot_id, outcome.receipt_id, outcome.artifact_set_id))
            or outcome.failure_code is not None or outcome.failure_detail_json is not None):
        raise PhysicalMediaReadError("reader requires an exact succeeded physical outcome")
    assert outcome.receipt_id is not None and outcome.artifact_set_id is not None
    resolved = resolve_physical_media_request(store, request)
    record = store.read_committed_artifact_set(resolved.job,
        command_slot_id=outcome.command_slot_id, receipt_id=outcome.receipt_id, artifact_set_id=outcome.artifact_set_id,
        expected_request_hash=resolved.request_hash, expected_command_name=PREPARE_PHYSICAL_MEDIA_EVIDENCE_COMMAND,
        expected_execution_kind="deterministic")
    if type(record) is not PersistedCommittedArtifactSet or (
        record.job, record.job_id, record.command_slot_id, record.receipt_id, record.artifact_set_id,
        record.request_hash, record.command_name, record.execution_kind,
    ) != (resolved.job, outcome.job_id, outcome.command_slot_id, outcome.receipt_id, outcome.artifact_set_id,
          resolved.request_hash, PREPARE_PHYSICAL_MEDIA_EVIDENCE_COMMAND, "deterministic"):
        raise PhysicalMediaReadError("physical Store record differs from exact outcome/request")
    if len(record.members) != 3:
        raise PhysicalMediaReadError("physical prelude requires exactly three members")
    metadata_limit = min(request.max_metadata_bytes, limits.max_metadata_bytes)
    total = 0
    for ordinal, (member, (kind, logical_id)) in enumerate(zip(record.members, physical_member_layout(resolved), strict=True)):
        ref = member.reference
        if (ref.member_ordinal, ref.artifact_type, ref.logical_id, ref.revision, ref.scope) != (
            ordinal, kind, logical_id, 1, canonical_recipe_scope(resolved.job),
        ):
            raise PhysicalMediaReadError("physical member order/type/id/revision/scope differs")
        total += len(member.payload_json.encode("utf-8"))
        if total > metadata_limit:
            raise PhysicalMediaReadError("physical total metadata exceeds explicit read byte cap")
    payloads = tuple(decode_media_evidence_json(item.payload_json.encode("utf-8"), max_bytes=metadata_limit)
                     for item in record.members)
    root_data = physical_object(payloads[0], (
        "strategy_version", "request", "request_hash", "blob", "physical_root_id", "physical_root_sha256",
        "source_manifest_sha256", "source_provenance_sha256", "producer_policy", "producer_policy_sha256",
        "producer_provenance", "producer_provenance_sha256", "calibration_bindings", "video_to_audio_presentation_map_sha256",
    ))
    if (physical_json(root_data["request"]) != physical_json(resolved.canonical_payload())
            or root_data["request_hash"] != resolved.request_hash):
        raise PhysicalMediaReadError("physical persisted request identity differs")
    reference = _blob(root_data["blob"])
    evidence_limit = min(request.max_evidence_bytes, limits.max_evidence_bytes)
    if reference.byte_length > evidence_limit:
        raise PhysicalMediaReadError("physical root exceeds explicit read evidence cap")
    root = decode_physical_root_media_evidence_json(_read_root_blob(store, resolved.job, reference, limits), max_bytes=evidence_limit)
    produced = ProducedPhysicalMediaEvidence(root,
        tuple(decode_calibration_binding(value) for value in physical_array(root_data["calibration_bindings"])),
        physical_json(root_data["producer_policy"]), physical_json(root_data["producer_provenance"]))
    validate_produced_physical_media_evidence(resolved, produced)
    expected, probe, certificate = physical_metadata_payloads(resolved, produced, reference)
    # Mapping equality equates True and 1; persisted wire types must remain
    # exact even when a tamperer recomputes every member/set hash.
    if physical_json(payloads) != physical_json(expected):
        raise PhysicalMediaReadError("physical metadata/probe/certificate differs from independent replay")
    return PersistedPhysicalMediaEvidence(record, resolved, produced, probe, certificate)
