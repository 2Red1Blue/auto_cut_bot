"""Fresh-claim physical detection and atomic three-member persistence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from ..media.stage4_predecessor import (
    CommittedVideoToAudioClockMapCertificate,
    PresentationTimelineProbe,
    derive_presentation_timeline_facts,
)
from ..media.timed_evidence import validate_calibration_bindings
from ..store.models import (
    ArtifactMember,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
    MaterializationError,
    MaterializationLimits,
    VerifiedMaterializedBlob,
    artifact_set_hash,
    canonical_payload_hash,
    canonical_recipe_scope,
)
from .physical_media_contract import (
    PHYSICAL_EVIDENCE_STRATEGY_VERSION,
    PHYSICAL_KIND_ORDER,
    PHYSICAL_ROOT_MEDIA_TYPE,
    PREPARE_PHYSICAL_MEDIA_EVIDENCE_COMMAND,
    PhysicalMediaEvidenceCommandError,
    PhysicalMediaEvidenceProducerPort,
    PreparePhysicalMediaEvidenceRequest,
    ProducedPhysicalMediaEvidence,
    ResolvedPreparePhysicalMediaEvidenceRequest,
    canonical_physical_mapping,
    physical_array,
    physical_json,
    physical_object,
    positive_limit,
)
from .prepare_timed_media_evidence_command import (
    CommittedMediaInputsStore,
    TimedMediaEvidenceProducerError,
    resolve_committed_timed_media_request,
)


class PhysicalMediaEvidenceStore(CommittedMediaInputsStore, Protocol):
    def claim_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def materialize_immutable_blob(self, job: Job, reference: BlobRef,
                                   limits: MaterializationLimits) -> VerifiedMaterializedBlob: ...

    def put_immutable_blob(self, job: Job, *, content: bytes,
                           content_hash: str, media_type: str) -> BlobRef: ...

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome: ...

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome: ...


@dataclass(frozen=True, slots=True)
class PreparePhysicalMediaEvidenceResult:
    outcome: CommandOutcome
    physical_root_sha256: str | None = None


def resolve_physical_media_request(store: CommittedMediaInputsStore,
                                   request: PreparePhysicalMediaEvidenceRequest) -> ResolvedPreparePhysicalMediaEvidenceRequest:
    if type(request) is not PreparePhysicalMediaEvidenceRequest:
        raise PhysicalMediaEvidenceCommandError("physical request must be exact")
    return ResolvedPreparePhysicalMediaEvidenceRequest(request, resolve_committed_timed_media_request(store, request.parent))


def validate_produced_physical_media_evidence(
    request: ResolvedPreparePhysicalMediaEvidenceRequest, produced: ProducedPhysicalMediaEvidence,
) -> None:
    if type(produced) is not ProducedPhysicalMediaEvidence:
        raise PhysicalMediaEvidenceCommandError("physical producer result must be exact")
    if sum(len(raw.encode("utf-8")) for raw in (
        produced.producer_policy_json, produced.producer_provenance_json,
    )) > request.max_metadata_bytes:
        raise PhysicalMediaEvidenceCommandError("physical metadata exceeds frozen byte cap")
    root = produced.physical_root
    contexts = tuple(item.context for item in (root.frame_pts_index, root.audio_sample_boundaries,
                    root.shot_boundaries, root.scene_boundaries, root.visual_validity, root.subtitle_cues))
    bindings = validate_calibration_bindings(produced.calibration_bindings, contexts)
    if produced.producer_policy_sha256 != request.physical_policy_sha256:
        raise PhysicalMediaEvidenceCommandError("physical producer policy identity changed")
    provenance = canonical_physical_mapping(produced.producer_provenance_json)
    if provenance["source_provenance_sha256"] != request.source_provenance_sha256:
        raise PhysicalMediaEvidenceCommandError("physical provenance differs from committed source")
    identities = physical_array(provenance["producer_identities"])
    calibrations = physical_array(produced.policy_mapping().get("calibrations"))
    if len(calibrations) != 6:
        raise PhysicalMediaEvidenceCommandError("frozen physical policy requires six calibrations")
    for kind, context, binding, identity_raw, calibration_raw in zip(
        PHYSICAL_KIND_ORDER, contexts, bindings, identities, calibrations, strict=True,
    ):
        identity = physical_object(identity_raw)
        calibration = physical_object(calibration_raw, (
            "producer_kind", "producer_id", "producer_version", "generation_policy_sha256",
            "detector_sha256", "calibration_policy_sha256", "calibration_record_sha256", "timing_error_bound_microseconds",
        ))
        micros = positive_limit(calibration["timing_error_bound_microseconds"], "calibrated microseconds")
        # Exact ceil equivalence, without a second rounding implementation.
        numerator = micros * context.time_base.denominator
        denominator = context.time_base.numerator * 1_000_000
        if not (binding.timing_error_bound_tick - 1) * denominator < numerator <= binding.timing_error_bound_tick * denominator:
            raise PhysicalMediaEvidenceCommandError("physical calibrated timing bound differs")
        if (binding.producer_id, binding.policy_sha256, binding.time_base) != (
            context.producer_id, context.generation_policy_sha256, context.time_base,
        ):
            raise PhysicalMediaEvidenceCommandError("physical calibration role/context order differs")
        expected = {
            "producer_kind": kind, "producer_id": binding.producer_id,
            "producer_version": binding.producer_version, "producer_policy_sha256": binding.policy_sha256,
            "detector_sha256": binding.detector_sha256, "calibration_record_sha256": binding.calibration_record_sha256,
            "timing_error_bound_tick": binding.timing_error_bound_tick, "adapter_sha256": binding.adapter_sha256,
            "calibration_policy_sha256": calibration["calibration_policy_sha256"],
        }
        if identity != expected or calibration != {
            "producer_kind": kind, "producer_id": binding.producer_id, "producer_version": binding.producer_version,
            "generation_policy_sha256": binding.policy_sha256, "detector_sha256": binding.detector_sha256,
            "calibration_record_sha256": binding.calibration_record_sha256,
            "calibration_policy_sha256": expected["calibration_policy_sha256"], "timing_error_bound_microseconds": micros,
        }:
            raise PhysicalMediaEvidenceCommandError("physical identity/calibration/policy role differs")
    if (bindings[0].detector_sha256 != request.source.frame_detector_sha256
            or bindings[1].detector_sha256 != request.source.audio_detector_sha256):
        raise PhysicalMediaEvidenceCommandError("physical producer replaced committed frame/audio detectors")
    if (root.physical_root_id != request.physical_root_id
            or root.source_id != request.source.window_manifest.source_id
            or root.source_sha256 != request.source_blob.content_hash
            or root.source_manifest_sha256 != request.source_manifest_sha256
            or root.root_input_manifest_sha256 != request.root_input_manifest_sha256
            or root.frame_pts_index != request.source.frame_pts_index
            or root.audio_sample_boundaries != request.source.audio_sample_boundaries):
        raise PhysicalMediaEvidenceCommandError("physical output does not bind committed request evidence")


def physical_member_layout(request: ResolvedPreparePhysicalMediaEvidenceRequest) -> tuple[tuple[str, str], ...]:
    prefix = f"physical_{request.request_hash[7:]}"
    return (("physical_root_media_evidence", f"{prefix}_root"),
            ("presentation_timeline_probe", f"{prefix}_probe"),
            ("committed_video_to_audio_clock_map_certificate", f"{prefix}_certificate"))


def physical_metadata_payloads(
    request: ResolvedPreparePhysicalMediaEvidenceRequest, produced: ProducedPhysicalMediaEvidence, blob: BlobRef,
) -> tuple[tuple[dict[str, object], ...], PresentationTimelineProbe, CommittedVideoToAudioClockMapCertificate]:
    probe, certificate = derive_presentation_timeline_facts(
        produced.physical_root, probe=request.source.presentation_timeline_probe,
        source_manifest_sha256=request.source_manifest_sha256, audio_snap_calibration=produced.calibration_bindings[1],
    )
    root: dict[str, object] = {
        "strategy_version": PHYSICAL_EVIDENCE_STRATEGY_VERSION, "request": request.canonical_payload(),
        "request_hash": request.request_hash, "blob": {"object_id": str(blob.object_id),
        "content_hash": blob.content_hash, "byte_length": blob.byte_length, "media_type": blob.media_type},
        "physical_root_id": produced.physical_root.physical_root_id,
        "physical_root_sha256": produced.physical_root.canonical_hash,
        "source_manifest_sha256": request.source_manifest_sha256,
        "source_provenance_sha256": request.source_provenance_sha256,
        "producer_policy": produced.policy_mapping(), "producer_policy_sha256": produced.producer_policy_sha256,
        "producer_provenance": canonical_physical_mapping(produced.producer_provenance_json),
        "producer_provenance_sha256": produced.producer_provenance_sha256,
        "calibration_bindings": [item.to_mapping() for item in produced.calibration_bindings],
        "video_to_audio_presentation_map_sha256": certificate.canonical_hash,
    }
    return (root, probe.to_mapping(), certificate.to_mapping()), probe, certificate


class PreparePhysicalMediaEvidenceCommand:
    def __init__(self, store: PhysicalMediaEvidenceStore, producer: PhysicalMediaEvidenceProducerPort) -> None:
        self._store, self._producer = store, producer

    def execute(self, request: PreparePhysicalMediaEvidenceRequest) -> PreparePhysicalMediaEvidenceResult:
        resolved = resolve_physical_media_request(self._store, request)
        claimed = self._store.claim_command(CommandClaim(resolved.job, resolved.idempotency_key,
            PREPARE_PHYSICAL_MEDIA_EVIDENCE_COMMAND, resolved.request_hash, execution_kind="deterministic"))
        if not claimed.is_fresh_claim:
            return PreparePhysicalMediaEvidenceResult(claimed)
        source: VerifiedMaterializedBlob | None = None
        try:
            if resolved.source_blob.byte_length > resolved.source.materialization_limits.effective_max_source_bytes:
                raise PhysicalMediaEvidenceCommandError("source exceeds frozen materialization byte cap")
            source = self._store.materialize_immutable_blob(resolved.job, resolved.source_blob, resolved.source.materialization_limits)
            if source.reference != resolved.source_blob:
                raise PhysicalMediaEvidenceCommandError("source lease reference differs")
            produced = self._producer.prepare(resolved, source)
            validate_produced_physical_media_evidence(resolved, produced)
            artifacts = self._persist_artifacts(resolved, produced)
            success = CommandSuccess(claimed.command_slot_id, artifact_set_hash(artifacts), artifacts)
        except (TimedMediaEvidenceProducerError, MaterializationError) as error:
            return PreparePhysicalMediaEvidenceResult(self._reject(claimed, error.code, error.detail, outcome=error.outcome))
        except ValueError as error:
            return PreparePhysicalMediaEvidenceResult(self._reject(claimed, "PHYSICAL_MEDIA_EVIDENCE_INVALID", str(error)))
        except Exception:
            return PreparePhysicalMediaEvidenceResult(self._reject(claimed, "PHYSICAL_MEDIA_EVIDENCE_INFRASTRUCTURE_FAILED",
                "physical infrastructure failed", outcome="failed"))
        finally:
            if source is not None:
                source.close()
        # Ambiguous commit is not a producer failure and must never write a
        # replacement rejection. Same-key reconciliation owns its outcome.
        outcome = self._store.commit_command_success(success)
        return PreparePhysicalMediaEvidenceResult(outcome, produced.physical_root.canonical_hash)

    def _persist_artifacts(self, request: ResolvedPreparePhysicalMediaEvidenceRequest,
                           produced: ProducedPhysicalMediaEvidence) -> tuple[ArtifactMember, ...]:
        raw = physical_json(produced.physical_root.to_mapping()).encode("utf-8")
        if len(raw) > request.max_evidence_bytes:
            raise PhysicalMediaEvidenceCommandError("physical evidence exceeds frozen byte cap")
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        # A fixed-width UUID lets us validate the exact metadata byte budget
        # before staging; this temporary value is never a persisted reference.
        provisional = BlobRef(UUID(int=0), digest, len(raw), PHYSICAL_ROOT_MEDIA_TYPE)
        payloads, _, _ = physical_metadata_payloads(request, produced, provisional)
        if sum(len(physical_json(item).encode("utf-8")) for item in payloads) > request.max_metadata_bytes:
            raise PhysicalMediaEvidenceCommandError("physical total metadata exceeds frozen byte cap")
        blob = self._store.put_immutable_blob(request.job, content=raw, content_hash=digest, media_type=PHYSICAL_ROOT_MEDIA_TYPE)
        if (blob.content_hash, blob.byte_length, blob.media_type) != (digest, len(raw), PHYSICAL_ROOT_MEDIA_TYPE):
            raise PhysicalMediaEvidenceCommandError("stored physical blob reference differs")
        payloads[0]["blob"] = {"object_id": str(blob.object_id), "content_hash": blob.content_hash,
                               "byte_length": blob.byte_length, "media_type": blob.media_type}
        return tuple(ArtifactMember(kind, logical_id, 1, canonical_recipe_scope(request.job),
                     canonical_payload_hash(physical_json(payload)), physical_json(payload))
                     for (kind, logical_id), payload in zip(physical_member_layout(request), payloads, strict=True))

    def _reject(self, claimed: CommandOutcome, code: str, detail: str, *,
                outcome: Literal["denied", "failed"] = "denied") -> CommandOutcome:
        return self._store.commit_command_rejection(CommandRejection(claimed.command_slot_id, code,
            physical_json({"code": code, "detail": detail}), outcome=outcome))
