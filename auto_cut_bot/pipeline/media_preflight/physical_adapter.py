"""Claim-owned adapter for speech-free physical prelude production.

The Kernel owns the exact predecessor reread, generic claim, and private source
lease.  This adapter translates that verified lease to the local physical
detector port and returns only the four-field frozen Kernel producer result.
"""

from __future__ import annotations

import json

from autocut_kernel.media.physical_root import PhysicalRootMediaEvidence
from autocut_kernel.pipeline.physical_media_contract import (
    PhysicalMediaEvidenceCommandError,
    ProducedPhysicalMediaEvidence,
    ResolvedPreparePhysicalMediaEvidenceRequest,
)
from autocut_kernel.pipeline.prepare_timed_media_evidence_command import (
    TimedMediaEvidenceProducerError,
)
from autocut_kernel.store.models import VerifiedMaterializedBlob

from .models import (
    LocalMediaPreflightError,
    LocalMediaToolError,
)
from .physical_models import PhysicalMediaPolicy, PhysicalMediaRequest
from .port import LocalMediaPreflightPort


class ClaimOwnedPhysicalMediaProducer:
    """Adapt one Kernel-verified source lease to six physical local detectors."""

    def __init__(self, port: LocalMediaPreflightPort, policy: PhysicalMediaPolicy) -> None:
        if type(policy) is not PhysicalMediaPolicy:  # noqa: E721
            raise PhysicalMediaEvidenceCommandError("physical producer requires an exact policy")
        self._port = port
        self._policy = policy

    def prepare(
        self,
        request: ResolvedPreparePhysicalMediaEvidenceRequest,
        source: VerifiedMaterializedBlob,
    ) -> ProducedPhysicalMediaEvidence:
        if type(request) is not ResolvedPreparePhysicalMediaEvidenceRequest:  # noqa: E721
            raise TimedMediaEvidenceProducerError(
                "PHYSICAL_REQUEST_INVALID", "physical producer request is not exact"
            )
        if source.reference != request.source_blob:
            raise TimedMediaEvidenceProducerError(
                "COMMITTED_SOURCE_BLOB_MISMATCH",
                "Kernel materialization does not match the committed BlobRef",
            )
        if self._policy.canonical_hash != request.physical_policy_sha256:
            raise TimedMediaEvidenceProducerError(
                "PHYSICAL_POLICY_IDENTITY_MISMATCH",
                "installed physical policy does not match the frozen request",
            )
        try:
            local = self._port.prepare_physical(
                PhysicalMediaRequest(
                    source_path=str(source.path),
                    episode_id=f"episode-{request.source.episode_index:04d}",
                    source_id=request.source.window_manifest.source_id,
                    source_sha256=request.source.source_blob.content_hash,
                    source_provenance_sha256=request.source_provenance_sha256,
                    source_manifest_sha256=request.source_manifest_sha256,
                    root_input_manifest_sha256=request.root_input_manifest_sha256,
                    physical_root_id=request.physical_root_id,
                    frame_pts_index=request.source.frame_pts_index,
                    audio_sample_boundaries=request.source.audio_sample_boundaries,
                    frame_detector_sha256=request.source.frame_detector_sha256,
                    audio_detector_sha256=request.source.audio_detector_sha256,
                    policy=self._policy,
                ),
                kernel_max_source_bytes=request.source.materialization_limits.max_source_bytes,
                service_max_request_bytes=(
                    request.source.materialization_limits.timed_speech_max_request_bytes
                ),
            )
        except LocalMediaToolError as error:
            raise TimedMediaEvidenceProducerError(error.code, str(error), outcome="failed") from error
        except LocalMediaPreflightError as error:
            raise TimedMediaEvidenceProducerError(error.code, str(error)) from error
        root = PhysicalRootMediaEvidence(
            physical_root_id=request.physical_root_id,
            source_id=request.source.window_manifest.source_id,
            source_sha256=request.source.source_blob.content_hash,
            source_manifest_sha256=request.source_manifest_sha256,
            root_input_manifest_sha256=request.root_input_manifest_sha256,
            frame_pts_index=local.frame_pts_index,
            shot_boundaries=local.shot_boundaries,
            scene_boundaries=local.scene_boundaries,
            audio_sample_boundaries=local.audio_sample_boundaries,
            visual_validity=local.visual_validity,
            subtitle_cues=local.subtitle_cues,
        )
        policy_json = json.dumps(
            self._policy.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        provenance_json = json.dumps(
            local.provenance_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        metadata_bytes = len(policy_json.encode("utf-8")) + len(provenance_json.encode("utf-8"))
        if metadata_bytes > request.max_metadata_bytes:
            raise TimedMediaEvidenceProducerError(
                "PHYSICAL_METADATA_LIMIT_EXCEEDED",
                "physical producer metadata exceeds the frozen metadata-byte limit",
            )
        return ProducedPhysicalMediaEvidence(
            root,
            local.calibration_bindings,
            policy_json,
            provenance_json,
        )


__all__ = ["ClaimOwnedPhysicalMediaProducer"]
