"""Durable command for production timed-media evidence preparation.

The command owns the Store claim before any local detector is invoked.  A
replay therefore returns the committed Receipt without repeating Whisper,
FFmpeg, VAD, scene, visual, or subtitle work.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal, Protocol, cast
from uuid import UUID

from ..media import (
    AdaptiveEvidenceWindowPolicy,
    AudioSampleBoundarySet,
    CalibrationBinding,
    CandidateEvidenceWindow,
    CandidateEvidenceWindowPlan,
    CandidateTimedEvidenceSet,
    CandidateWindowAssessment,
    CandidateWindowOutcome,
    FramePtsIndexSet,
    PresentationTimelineProbe,
    RootMediaEvidenceBundle,
    SentenceCompleteness,
    Stage4PredecessorError,
    TranscriptSourceOutcome,
    admit_timed_speech_profile,
    advance_candidate_evidence_window,
    derive_presentation_timeline_facts,
    plan_candidate_evidence_window,
)
from ..media.root_evidence import CanonicalEvidence
from ..media.timed_evidence import validate_calibration_bindings
from ..media.types import canonical_sha256
from ..registry.timed_speech import (
    AuthorityRegistrySnapshot,
    BootstrappedTimedSpeechProfile,
    StoreAnchoredTimedSpeechProfileResolver,
    TimedSpeechRegistryError,
)
from ..source_manifest import (
    SourceManifestDecodeError,
    decode_source_manifest,
)
from ..store import (
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
    StoreValidationError,
)
from ..store.models import (
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedSemanticInputsRequest,
    MaterializationError,
    MaterializationLimits,
    PersistedWholeSeriesSourceManifest,
    VerifiedMaterializedBlob,
    WholeSeriesSourceManifestReference,
    canonical_recipe_scope,
)
from ..vlm import VlmSemanticPack, WindowManifest

PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND = "PrepareTimedMediaEvidence@2.1.3"
TIMED_MEDIA_EVIDENCE_STRATEGY_VERSION = "whole-episode-conjunctive-evidence-v1"
TIMED_SPEECH_BUSY_RETRY_COUNT = 1
TIMED_SPEECH_BUSY_RETRY_DELAY_SECONDS = 1


class TimedMediaEvidenceCommandError(ValueError):
    """A closed request or producer result is invalid."""


class TimedMediaEvidenceProducerError(RuntimeError):
    """Stable producer failure consumed by the durable command boundary."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        outcome: Literal["denied", "failed"] = "denied",
    ) -> None:
        if not code or not detail or outcome not in ("denied", "failed"):
            raise ValueError("producer failure requires closed diagnostics")
        super().__init__(detail)
        self.code: str = code
        self.detail: str = detail
        self.outcome: Literal["denied", "failed"] = outcome


@dataclass(frozen=True, slots=True)
class ProducedTimedMediaEvidence:
    """Producer output containing facts and calibration, never admission."""

    producer_policy_sha256: str
    root_bundle: RootMediaEvidenceBundle
    calibration_bindings: tuple[CalibrationBinding, ...]
    producer_policy_json: str
    producer_provenance_json: str

    def __post_init__(self) -> None:
        if not _is_sha256(self.producer_policy_sha256):
            raise TimedMediaEvidenceCommandError(
                "producer_policy_sha256 must be a lowercase sha256 digest"
            )
        if type(self.root_bundle) is not RootMediaEvidenceBundle:  # noqa: E721
            raise TimedMediaEvidenceCommandError("root_bundle must be a RootMediaEvidenceBundle")
        bindings = tuple(self.calibration_bindings)
        if not bindings or any(type(item) is not CalibrationBinding for item in bindings):  # noqa: E721
            raise TimedMediaEvidenceCommandError(
                "calibration_bindings must contain exact CalibrationBinding values"
            )
        object.__setattr__(self, "calibration_bindings", bindings)
        _validate_canonical_mapping_json(
            self.producer_policy_json,
            self.producer_policy_sha256,
            "producer policy",
        )
        _validate_producer_provenance_json(self.producer_provenance_json)

    @property
    def producer_provenance_sha256(self) -> str:
        return canonical_sha256(json.loads(self.producer_provenance_json))


class TimedMediaEvidenceProducerPort(Protocol):
    """Application-owned local detector adapter invoked only after claim."""

    def prepare(
        self,
        request: ResolvedPrepareTimedMediaEvidenceRequest,
        source: VerifiedMaterializedBlob,
    ) -> ProducedTimedMediaEvidence: ...


class TimedMediaEvidenceStore(Protocol):
    def read_committed_semantic_inputs(
        self, request: CommittedSemanticInputsRequest,
    ) -> CommittedSemanticInputs: ...

    def read_outcome(self, job: Job, idempotency_key: str) -> CommandOutcome | None: ...

    def read_bootstrapped_timed_speech_profile(
        self,
        snapshot: AuthorityRegistrySnapshot,
    ) -> BootstrappedTimedSpeechProfile: ...

    def read_whole_series_source_manifest(
        self,
        job: Job,
        artifact_set_id: UUID,
    ) -> PersistedWholeSeriesSourceManifest: ...

    def claim_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def materialize_immutable_blob(
        self,
        job: Job,
        reference: BlobRef,
        limits: MaterializationLimits,
    ) -> VerifiedMaterializedBlob: ...

    def put_immutable_blob(
        self,
        job: Job,
        *,
        content: bytes,
        content_hash: str,
        media_type: str,
    ) -> BlobRef: ...

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome: ...

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome: ...


@dataclass(frozen=True, slots=True)
class PrepareTimedMediaEvidenceRequest:
    """All committed identities required for one episode's local evidence."""

    job: Job
    idempotency_key: str
    episode_index: int
    artifact_scope: ArtifactScope
    artifact_revision: int
    source_blob: BlobRef
    source_manifest_reference: WholeSeriesSourceManifestReference
    source_manifest_receipt_id: UUID
    source_manifest_artifact_set_id: UUID
    source_manifest_command_slot_id: UUID
    source_provenance_sha256: str
    semantic_inputs_request: CommittedSemanticInputsRequest
    window_manifest: WindowManifest
    semantic_pack: VlmSemanticPack
    frame_pts_index: FramePtsIndexSet
    audio_sample_boundaries: AudioSampleBoundarySet
    frame_detector_sha256: str
    audio_detector_sha256: str
    adaptive_policy: AdaptiveEvidenceWindowPolicy
    producer_policy_sha256: str
    materialization_limits: MaterializationLimits

    def __post_init__(self) -> None:
        if type(self.job) is not Job or type(self.source_blob) is not BlobRef:  # noqa: E721
            raise TimedMediaEvidenceCommandError("job/source_blob has an invalid type")
        if not self.idempotency_key or self.idempotency_key != self.idempotency_key.strip():
            raise TimedMediaEvidenceCommandError("idempotency_key must be canonical text")
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise TimedMediaEvidenceCommandError("episode_index must be non-negative")
        if self.artifact_scope != canonical_recipe_scope(self.job):
            raise TimedMediaEvidenceCommandError("artifact_scope must be the canonical Job scope")
        if type(self.artifact_revision) is not int or self.artifact_revision < 1:  # noqa: E721
            raise TimedMediaEvidenceCommandError("artifact_revision must be positive")
        for field_name in (
            "source_provenance_sha256",
            "producer_policy_sha256",
        ):
            if not _is_sha256(getattr(self, field_name)):
                raise TimedMediaEvidenceCommandError(
                    f"{field_name} must be a lowercase sha256 digest"
                )
        if type(self.window_manifest) is not WindowManifest:  # noqa: E721
            raise TimedMediaEvidenceCommandError("window_manifest must be exact")
        if type(self.semantic_pack) is not VlmSemanticPack:  # noqa: E721
            raise TimedMediaEvidenceCommandError("semantic_pack must be exact")
        if type(self.frame_pts_index) is not FramePtsIndexSet:  # noqa: E721
            raise TimedMediaEvidenceCommandError("frame_pts_index must be exact")
        if type(self.audio_sample_boundaries) is not AudioSampleBoundarySet:  # noqa: E721
            raise TimedMediaEvidenceCommandError("audio boundaries must be exact")
        if not _is_sha256(self.frame_detector_sha256) or not _is_sha256(self.audio_detector_sha256):
            raise TimedMediaEvidenceCommandError("physical detector identities must be sha256")
        if type(self.adaptive_policy) is not AdaptiveEvidenceWindowPolicy:  # noqa: E721
            raise TimedMediaEvidenceCommandError("adaptive_policy must be exact")
        if type(self.materialization_limits) is not MaterializationLimits:  # noqa: E721
            raise TimedMediaEvidenceCommandError("materialization_limits must be exact")
        if type(self.source_manifest_reference) is not WholeSeriesSourceManifestReference:  # noqa: E721
            raise TimedMediaEvidenceCommandError("source manifest reference must be exact")
        if self.source_manifest_reference.scope != canonical_recipe_scope(self.job):
            raise TimedMediaEvidenceCommandError("source manifest reference has a non-canonical Job scope")
        for field_name in (
            "source_manifest_receipt_id",
            "source_manifest_artifact_set_id",
            "source_manifest_command_slot_id",
        ):
            if not isinstance(getattr(self, field_name), UUID):
                raise TimedMediaEvidenceCommandError(f"{field_name} must be a UUID")
        semantic = self.semantic_inputs_request
        if type(semantic) is not CommittedSemanticInputsRequest:  # noqa: E721
            raise TimedMediaEvidenceCommandError("semantic_inputs_request must be exact")
        source = self.source_manifest_reference
        if semantic.job != self.job or semantic.source_manifest != CommittedArtifactMemberReference(
            self.source_manifest_receipt_id, self.source_manifest_artifact_set_id,
            0, source.scope, source.artifact_type, source.logical_id,
            source.revision, source.content_hash,
        ):
            raise TimedMediaEvidenceCommandError("semantic inputs must bind the exact Source member and Job")
        if semantic.vlm_semantic_pack_set.scope != canonical_recipe_scope(self.job):
            raise TimedMediaEvidenceCommandError("semantic VLM aggregate must bind the canonical Job scope")
        manifest = self.window_manifest
        if (
            manifest.source_sha256 != self.source_blob.content_hash
            or manifest.frame_pts_index_set_sha256 != self.frame_pts_index.canonical_hash
            or self.semantic_pack.window_manifest_sha256 != manifest.canonical_hash
            or self.adaptive_policy.time_base != manifest.source_time_base
        ):
            raise TimedMediaEvidenceCommandError("source/VLM/frame/policy identities do not close")
        audio = self.audio_sample_boundaries.context
        if audio.source_id != manifest.source_id or audio.source_sha256 != manifest.source_sha256:
            raise TimedMediaEvidenceCommandError("audio boundaries do not bind the exact source")
    @property
    def source_manifest_sha256(self) -> str:
        return self.source_manifest_reference.content_hash

    def root_input_manifest_sha256(self, presentation_timeline_probe: object) -> str:
        canonical_hash = getattr(presentation_timeline_probe, "canonical_hash", None)
        if type(canonical_hash) is not str:
            raise TimedMediaEvidenceCommandError("committed presentation timeline probe is invalid")
        return canonical_sha256(
            {
                "adaptive_policy_sha256": self.adaptive_policy.canonical_hash,
                "episode_index": self.episode_index,
                "frame_detector_sha256": self.frame_detector_sha256,
                "semantic_pack_sha256": self.semantic_pack.canonical_hash,
                "producer_policy_sha256": self.producer_policy_sha256,
                "materialization_policy_sha256": self.materialization_limits.evidence_policy_sha256,
                "max_source_bytes": self.materialization_limits.max_source_bytes,
                "timed_speech_max_request_bytes": (
                    self.materialization_limits.timed_speech_max_request_bytes
                ),
                "effective_max_source_bytes": (
                    self.materialization_limits.effective_max_source_bytes
                ),
                "source_blob": _blob_mapping(self.source_blob),
                "source_manifest_sha256": self.source_manifest_sha256,
                "source_provenance_sha256": self.source_provenance_sha256,
                "presentation_timeline_probe_sha256": canonical_hash,
                "audio_detector_sha256": self.audio_detector_sha256,
                "strategy_version": TIMED_MEDIA_EVIDENCE_STRATEGY_VERSION,
                "window_manifest_sha256": self.window_manifest.canonical_hash,
            }
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "artifact_revision": self.artifact_revision,
            "artifact_scope": _scope_mapping(self.artifact_scope),
            "audio_sample_boundary_set_sha256": self.audio_sample_boundaries.canonical_hash,
            "audio_detector_sha256": self.audio_detector_sha256,
            "episode_index": self.episode_index,
            "frame_pts_index_set_sha256": self.frame_pts_index.canonical_hash,
            "frame_detector_sha256": self.frame_detector_sha256,
            "idempotency_key": self.idempotency_key,
            "job": {"job_key": self.job.job_key, "profile": self.job.profile},
            "semantic_pack_sha256": self.semantic_pack.canonical_hash,
            "semantic_inputs_request": {
                "job": {"job_key": self.semantic_inputs_request.job.job_key,
                        "profile": self.semantic_inputs_request.job.profile},
                "source_manifest": self.semantic_inputs_request.source_manifest.to_mapping(),
                "vlm_semantic_pack_set": self.semantic_inputs_request.vlm_semantic_pack_set.to_mapping(),
            },
            "producer_policy_sha256": self.producer_policy_sha256,
            "materialization_policy_sha256": self.materialization_limits.evidence_policy_sha256,
            "max_source_bytes": self.materialization_limits.max_source_bytes,
            "timed_speech_max_request_bytes": (
                self.materialization_limits.timed_speech_max_request_bytes
            ),
            "effective_max_source_bytes": self.materialization_limits.effective_max_source_bytes,
            "source_manifest_artifact_set_id": str(self.source_manifest_artifact_set_id),
            "source_manifest_command_slot_id": str(self.source_manifest_command_slot_id),
            "source_manifest_receipt_id": str(self.source_manifest_receipt_id),
            "source_manifest_reference": {
                "artifact_type": self.source_manifest_reference.artifact_type,
                "content_hash": self.source_manifest_reference.content_hash,
                "logical_id": self.source_manifest_reference.logical_id,
                "revision": self.source_manifest_reference.revision,
                "scope": _scope_mapping(self.source_manifest_reference.scope),
            },
            "source_blob": _blob_mapping(self.source_blob),
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_provenance_sha256": self.source_provenance_sha256,
            "strategy_version": TIMED_MEDIA_EVIDENCE_STRATEGY_VERSION,
            "window_manifest_sha256": self.window_manifest.canonical_hash,
        }

    @property
    def request_hash(self) -> str:
        return canonical_sha256(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ResolvedPrepareTimedMediaEvidenceRequest:
    """A request closed over the probe reread from the committed source manifest."""

    request: PrepareTimedMediaEvidenceRequest
    presentation_timeline_probe: PresentationTimelineProbe

    @property
    def job(self) -> Job:
        return self.request.job

    @property
    def idempotency_key(self) -> str:
        return self.request.idempotency_key

    @property
    def episode_index(self) -> int:
        return self.request.episode_index

    @property
    def artifact_scope(self) -> ArtifactScope:
        return self.request.artifact_scope

    @property
    def artifact_revision(self) -> int:
        return self.request.artifact_revision

    @property
    def source_blob(self) -> BlobRef:
        return self.request.source_blob

    @property
    def source_manifest_sha256(self) -> str:
        return self.request.source_manifest_sha256

    @property
    def source_provenance_sha256(self) -> str:
        return self.request.source_provenance_sha256

    @property
    def window_manifest(self) -> WindowManifest:
        return self.request.window_manifest

    @property
    def semantic_pack(self) -> VlmSemanticPack:
        return self.request.semantic_pack

    @property
    def frame_pts_index(self) -> FramePtsIndexSet:
        return self.request.frame_pts_index

    @property
    def audio_sample_boundaries(self) -> AudioSampleBoundarySet:
        return self.request.audio_sample_boundaries

    @property
    def frame_detector_sha256(self) -> str:
        return self.request.frame_detector_sha256

    @property
    def audio_detector_sha256(self) -> str:
        return self.request.audio_detector_sha256

    @property
    def adaptive_policy(self) -> AdaptiveEvidenceWindowPolicy:
        return self.request.adaptive_policy

    @property
    def producer_policy_sha256(self) -> str:
        return self.request.producer_policy_sha256

    @property
    def materialization_limits(self) -> MaterializationLimits:
        return self.request.materialization_limits

    @property
    def root_input_manifest_sha256(self) -> str:
        return self.request.root_input_manifest_sha256(self.presentation_timeline_probe)

    def canonical_payload(self) -> dict[str, object]:
        return {
            **self.request.canonical_payload(),
            "presentation_timeline_probe_sha256": getattr(
                self.presentation_timeline_probe, "canonical_hash"
            ),
            "root_input_manifest_sha256": self.root_input_manifest_sha256,
        }

@dataclass(frozen=True, slots=True)
class PrepareTimedMediaEvidenceResult:
    outcome: CommandOutcome
    root_bundle_sha256: str | None = None
    candidate_count: int = 0


class PrepareTimedMediaEvidenceCommand:
    """Claim, run all local producers, validate conjunction, and commit once."""

    def __init__(
        self,
        store: TimedMediaEvidenceStore,
        producer: TimedMediaEvidenceProducerPort,
        authority_profile_resolver: StoreAnchoredTimedSpeechProfileResolver,
    ) -> None:
        if type(authority_profile_resolver) is not StoreAnchoredTimedSpeechProfileResolver:  # noqa: E721
            raise TimedMediaEvidenceCommandError(
                "timed-media evidence requires an explicit store-anchored timed speech resolver"
            )
        self._store = store
        self._producer = producer
        self._authority_profile_resolver = authority_profile_resolver

    def execute(
        self,
        request: PrepareTimedMediaEvidenceRequest,
    ) -> PrepareTimedMediaEvidenceResult:
        resolved_request = resolve_committed_timed_media_request(self._store, request)
        claimed = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND,
                timed_media_request_hash(resolved_request, self._authority_profile_resolver.snapshot),
                execution_kind="deterministic",
            )
        )
        if not claimed.is_fresh_claim:
            return PrepareTimedMediaEvidenceResult(claimed)
        source: VerifiedMaterializedBlob | None = None
        try:
            resolved_profile = self._read_timed_speech_profile_registry()
            if (
                resolved_request.source_blob.byte_length
                > resolved_request.materialization_limits.effective_max_source_bytes
            ):
                raise TimedMediaEvidenceProducerError(
                    "MEDIA_SOURCE_BYTE_LIMIT_EXCEEDED",
                    "committed source exceeds the frozen effective source-byte limit",
                )
            source = self._store.materialize_immutable_blob(
                resolved_request.job,
                resolved_request.source_blob,
                resolved_request.materialization_limits,
            )
            busy_attempts = 0
            while True:
                try:
                    produced = self._producer.prepare(resolved_request, source)
                    break
                except TimedMediaEvidenceProducerError as error:
                    if (
                        error.code != "TIMED_SPEECH_BUSY"
                        or busy_attempts >= TIMED_SPEECH_BUSY_RETRY_COUNT
                    ):
                        raise
                    busy_attempts += 1
                    time.sleep(TIMED_SPEECH_BUSY_RETRY_DELAY_SECONDS)
            validate_produced_timed_media_evidence(resolved_request, produced)
            plans, candidates = close_timed_media_candidates(resolved_request, produced)
            artifacts = self._persist_artifacts(
                resolved_request, produced, plans, candidates, resolved_profile
            )
            source.close()
            source = None
            outcome = self._store.commit_command_success(
                CommandSuccess(claimed.command_slot_id, _artifact_set_hash(artifacts), artifacts)
            )
            return PrepareTimedMediaEvidenceResult(
                outcome,
                produced.root_bundle.canonical_hash,
                len(candidates),
            )
        except TimedMediaEvidenceProducerError as error:
            return PrepareTimedMediaEvidenceResult(
                self._reject(claimed, error.code, error.detail, outcome=error.outcome)
            )
        except MaterializationError as error:
            return PrepareTimedMediaEvidenceResult(
                self._reject(claimed, error.code, error.detail, outcome=error.outcome)
            )
        except (TimedMediaEvidenceCommandError, ValueError) as error:
            return PrepareTimedMediaEvidenceResult(
                self._reject(claimed, "TIMED_MEDIA_EVIDENCE_INVALID", str(error))
            )
        except Exception:
            return PrepareTimedMediaEvidenceResult(
                self._reject(
                    claimed,
                    "TIMED_MEDIA_EVIDENCE_INFRASTRUCTURE_FAILED",
                    "local media evidence infrastructure failed",
                    outcome="failed",
                )
            )
        finally:
            if source is not None:
                source.close()

    def _read_timed_speech_profile_registry(
        self,
    ) -> BootstrappedTimedSpeechProfile:
        """Resolve the composition-selected anchor only after a fresh claim."""
        try:
            resolved = self._authority_profile_resolver.resolve(self._store)
        except (TimedSpeechRegistryError, StoreValidationError, ValueError, TypeError) as error:
            raise TimedMediaEvidenceCommandError(
                "authority anchored timed speech profile is unavailable"
            ) from error
        return resolved

    def _persist_artifacts(
        self,
        request: ResolvedPrepareTimedMediaEvidenceRequest,
        produced: ProducedTimedMediaEvidence,
        plans: tuple[CandidateEvidenceWindowPlan, ...],
        candidates: tuple[CandidateTimedEvidenceSet, ...],
        resolved_profile: BootstrappedTimedSpeechProfile,
    ) -> tuple[ArtifactMember, ...]:
        root_blob = self._put_json_blob(
            request.job,
            produced.root_bundle,
            "application/vnd.autocut.root-media-evidence+json",
        )
        plan_payload = {
            "plans": [item.to_mapping() for item in plans],
            "schema_version": "candidate-evidence-window-plans-v1",
        }
        plan_blob = self._put_mapping_blob(
            request.job,
            plan_payload,
            "application/vnd.autocut.candidate-window-plans+json",
        )
        candidate_blobs = tuple(
            self._put_json_blob(
                request.job,
                candidate,
                "application/vnd.autocut.candidate-timed-evidence+json",
            )
            for candidate in candidates
        )
        provenance_blob = self._put_mapping_blob(
            request.job,
            json.loads(produced.producer_provenance_json),
            "application/vnd.autocut.local-media-producer-provenance+json",
        )
        policy_blob = self._put_mapping_blob(
            request.job,
            json.loads(produced.producer_policy_json),
            "application/vnd.autocut.local-media-preflight-policy+json",
        )
        root_payload = {
            "blob": _blob_mapping(root_blob),
            "calibration_bindings": [item.to_mapping() for item in produced.calibration_bindings],
            "episode_index": request.episode_index,
            "producer_provenance_blob": _blob_mapping(provenance_blob),
            "producer_provenance_sha256": produced.producer_provenance_sha256,
            "producer_policy_blob": _blob_mapping(policy_blob),
            "producer_policy_sha256": produced.producer_policy_sha256,
            "root_bundle_sha256": produced.root_bundle.canonical_hash,
            "source_manifest_sha256": request.source_manifest_sha256,
            "source_provenance_sha256": request.source_provenance_sha256,
        }
        index_payload = {
            "candidate_blobs": [_blob_mapping(item) for item in candidate_blobs],
            "candidate_count": len(candidate_blobs),
            "candidate_index_state": "populated" if candidate_blobs else "empty",
            "candidate_set_sha256": [item.canonical_hash for item in candidates],
            "episode_index": request.episode_index,
            "plan_blob": _blob_mapping(plan_blob),
            "plan_set_sha256": canonical_sha256(plan_payload),
            "schema_version": "candidate-timed-evidence-index-v1",
            "semantic_pack_sha256": request.semantic_pack.canonical_hash,
            "presentation_map_facts_sha256": request.presentation_timeline_probe.canonical_hash,
            "presentation_timeline_probe_sha256": request.presentation_timeline_probe.canonical_hash,
        }
        try:
            admission = admit_timed_speech_profile(
                resolved_profile.entry,
                resolved_profile.reference.content_hash,
                produced.root_bundle,
                produced.calibration_bindings,
            )
            audio_binding = next(
                item
                for item in produced.calibration_bindings
                if item.producer_id == produced.root_bundle.audio_sample_boundaries.context.producer_id
            )
            probe, certificate = derive_presentation_timeline_facts(
                produced.root_bundle,
                probe=request.presentation_timeline_probe,
                source_manifest_sha256=request.source_manifest_sha256,
                audio_snap_calibration=audio_binding,
            )
        except (Stage4PredecessorError, StopIteration) as error:
            raise TimedMediaEvidenceCommandError(
                "Stage 4 predecessor facts do not close against committed preflight evidence"
            ) from error
        admission_payload = {
            **admission.to_mapping(),
            "registry_member_reference": resolved_profile.reference.to_mapping(),
        }
        root_payload["video_to_audio_presentation_map_sha256"] = certificate.canonical_hash
        index_payload["video_to_audio_presentation_map_sha256"] = certificate.canonical_hash
        return (
            _artifact(
                request,
                "root_media_evidence_bundle",
                f"root_media_evidence_episode_{request.episode_index:04d}",
                root_payload,
            ),
            _artifact(
                request,
                "candidate_timed_evidence_index",
                f"candidate_timed_evidence_episode_{request.episode_index:04d}",
                index_payload,
            ),
            _artifact(
                request,
                "timed_speech_profile_admission",
                f"timed_speech_profile_admission_episode_{request.episode_index:04d}",
                admission_payload,
            ),
            _artifact(
                request,
                "presentation_timeline_probe",
                f"presentation_timeline_probe_episode_{request.episode_index:04d}",
                probe.to_mapping(),
            ),
            _artifact(
                request,
                "committed_video_to_audio_clock_map_certificate",
                f"video_to_audio_clock_map_episode_{request.episode_index:04d}",
                certificate.to_mapping(),
            ),
        )

    def _put_json_blob(
        self,
        job: Job,
        evidence: CanonicalEvidence,
        media_type: str,
    ) -> BlobRef:
        return self._put_mapping_blob(job, evidence.to_mapping(), media_type)

    def _put_mapping_blob(
        self,
        job: Job,
        value: object,
        media_type: str,
    ) -> BlobRef:
        content = _json(value).encode("utf-8")
        return self._store.put_immutable_blob(
            job,
            content=content,
            content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
            media_type=media_type,
        )

    def _reject(
        self,
        claimed: CommandOutcome,
        code: str,
        detail: str,
        *,
        outcome: Literal["denied", "failed"] = "denied",
    ) -> CommandOutcome:
        return self._store.commit_command_rejection(
            CommandRejection(
                claimed.command_slot_id,
                code,
                _json({"code": code, "detail": detail}),
                outcome=outcome,
            )
        )


def resolve_committed_timed_media_request(
    store: TimedMediaEvidenceStore,
    request: PrepareTimedMediaEvidenceRequest,
) -> ResolvedPrepareTimedMediaEvidenceRequest:
    """Reread exact Source/VLM owners before claiming any detector work."""

    if type(request) is not PrepareTimedMediaEvidenceRequest:  # noqa: E721
        raise TimedMediaEvidenceCommandError("timed media request must be exact")
    try:
        persisted = store.read_whole_series_source_manifest(
            request.job,
            request.source_manifest_artifact_set_id,
        )
        if (
            persisted.reference != request.source_manifest_reference
            or persisted.receipt_id != request.source_manifest_receipt_id
            or persisted.artifact_set_id != request.source_manifest_artifact_set_id
            or persisted.command_slot_id != request.source_manifest_command_slot_id
            or persisted.source_job != request.job
        ):
            raise TimedMediaEvidenceCommandError(
                "committed source manifest member does not match the requested immutable handle"
            )
        decoded = decode_source_manifest(persisted.payload_json, persisted.proxy_blobs)
        if request.source_provenance_sha256 != canonical_sha256(
            _persisted_source_manifest_provenance_mapping(persisted)
        ):
            raise TimedMediaEvidenceCommandError(
                "requested source provenance does not match the committed source manifest"
            )
        episode = decoded.episodes[request.episode_index]
        probe = episode.media_probe.presentation_timeline_probe
        if probe is None:
            raise TimedMediaEvidenceCommandError(
                "V2 preflight requires persisted source presentation timeline facts"
            )
        if (
            episode.proxy_blob.object_id != request.source_blob.object_id
            or episode.proxy_blob.content_hash != request.source_blob.content_hash
            or episode.proxy_blob.byte_length != request.source_blob.byte_length
            or episode.proxy_blob.media_type != request.source_blob.media_type
            or episode.manifest != request.window_manifest
            or episode.manifest.frame_pts_index_set != request.frame_pts_index
            or episode.media_probe.audio_sample_boundaries != request.audio_sample_boundaries
            or episode.media_probe.frame_detector_sha256 != request.frame_detector_sha256
            or episode.media_probe.audio_detector_sha256 != request.audio_detector_sha256
        ):
            raise TimedMediaEvidenceCommandError(
                "requested source episode facts do not match the committed source manifest"
            )
        facts = probe
        if (
            facts.source_id != request.window_manifest.source_id
            or facts.source_sha256 != request.source_blob.content_hash
            or facts.source_blob_content_hash != request.source_blob.content_hash
            or facts.source_blob_byte_length != request.source_blob.byte_length
            or facts.source_blob_media_type != request.source_blob.media_type
            or facts.frame_pts_index_set_sha256 != request.frame_pts_index.canonical_hash
            or facts.audio_sample_boundary_set_sha256
            != request.audio_sample_boundaries.canonical_hash
            or facts.window_manifest_sha256 != request.window_manifest.canonical_hash
        ):
            raise TimedMediaEvidenceCommandError(
                "persisted source presentation facts do not close request identities"
            )
        semantic = store.read_committed_semantic_inputs(request.semantic_inputs_request)
        if (
            type(semantic) is not CommittedSemanticInputs  # noqa: E721
            or semantic.source_manifest != persisted
            or semantic.source_grant != decoded.census
            or semantic.vlm_semantic_pack_set != request.semantic_inputs_request.vlm_semantic_pack_set
        ):
            raise TimedMediaEvidenceCommandError("committed semantic Source/VLM aggregate differs")
        semantic.source_grant.require_purpose("semantic_analysis")
        semantic.source_grant.require_purpose("render_source")
        matches = tuple(item for item in semantic.inputs
                        if item.source_window.window_manifest_sha256 == episode.manifest.canonical_hash)
        if len(matches) != 1:
            raise TimedMediaEvidenceCommandError("committed VLM input lost the exact source window")
        selected = matches[0]
        window, pack = selected.source_window, selected.semantic_pack
        child = pack.source_child
        selected.request_identity.assert_manifest_binding(episode.manifest, episode.manifest_set)
        if (
            window.episode_index != request.episode_index
            or window.stream_index != episode.manifest.stream_index
            or window.core_start_pts != episode.manifest.core_range.start_pts
            or window.core_end_pts != episode.manifest.core_range.end_pts
            or window.source_id != episode.manifest.source_id
            or window.source_sha256 != episode.manifest.source_sha256
            or window.source_clock_id != episode.manifest.source_clock_id
            or window.window_manifest_set_sha256 != episode.manifest_set.canonical_hash
            or window.proxy_blob != request.source_blob
            or child.source_job != request.job or child.kernel_job_id != persisted.job_id
            or child.episode_index != request.episode_index
            or child.source_manifest_sha256 != request.source_manifest_sha256
            or child.source_provenance_sha256 != request.source_provenance_sha256
            or child.window_manifest_sha256 != episode.manifest.canonical_hash
            or child.window_manifest_set_sha256 != episode.manifest_set.canonical_hash
            or child.request_identity_sha256 != selected.request_identity.canonical_hash
            or child.request_identity_sha256 != request.semantic_pack.request_identity_sha256
            or selected.raw_response.content_hash != request.semantic_pack.raw_response_sha256
            or pack.semantic_pack.canonical_hash != request.semantic_pack.canonical_hash
            or _json(pack.semantic_pack.to_mapping()) != _json(request.semantic_pack.to_mapping())
            or selected.response_record.receipt_id != child.receipt_id
            or selected.response_record.artifact_set_id != child.artifact_set_id
            or selected.response_record.scope != request.artifact_scope
            or (selected.response_record.member_ordinal, selected.response_record.artifact_type,
                selected.response_record.logical_id, selected.response_record.revision)
            != (1, "vlm_response_record", f"vlm_response_{episode.manifest.canonical_hash[7:31]}",
                child.reference.revision)
        ):
            raise TimedMediaEvidenceCommandError("committed VLM pack does not bind the exact Source/episode/request owner")
        return ResolvedPrepareTimedMediaEvidenceRequest(request, facts)
    except IndexError as error:
        raise TimedMediaEvidenceCommandError(
            "requested source episode is absent from the committed source manifest"
        ) from error
    except TimedMediaEvidenceCommandError:
        raise
    except (SourceManifestDecodeError, StoreValidationError, ValueError, TypeError) as error:
        raise TimedMediaEvidenceCommandError(
            "committed Source/VLM inputs are unavailable or invalid for preflight"
        ) from error


def timed_media_request_hash(
    resolved: ResolvedPrepareTimedMediaEvidenceRequest,
    snapshot: AuthorityRegistrySnapshot,
) -> str:
    """The actual deterministic Command identity, shared by commit and read."""
    if type(resolved) is not ResolvedPrepareTimedMediaEvidenceRequest or type(snapshot) is not AuthorityRegistrySnapshot:  # noqa: E721
        raise TimedMediaEvidenceCommandError("timed media hash requires resolved inputs and exact snapshot")
    return canonical_sha256({
        "authority_registry_set_sha256": snapshot.registry_set_sha256,
        "authority_profile_key": snapshot.enabled_profile.value,
        "request": resolved.canonical_payload(),
    })


def validate_produced_timed_media_evidence(
    request: ResolvedPrepareTimedMediaEvidenceRequest,
    produced: ProducedTimedMediaEvidence,
) -> None:
    if type(produced) is not ProducedTimedMediaEvidence:  # noqa: E721
        raise TimedMediaEvidenceCommandError("producer returned another result type")
    root = produced.root_bundle
    validate_calibration_bindings(produced.calibration_bindings, tuple(item.context for item in (
        root.transcript, root.speech_activity, root.audio_sample_boundaries, root.frame_pts_index,
        root.shot_boundaries, root.scene_boundaries, root.visual_validity, root.subtitle_cues,
    )))
    if produced.producer_policy_sha256 != request.producer_policy_sha256:
        raise TimedMediaEvidenceCommandError("producer policy identity changed")
    provenance = json.loads(produced.producer_provenance_json)
    if provenance["source_provenance_sha256"] != request.source_provenance_sha256:
        raise TimedMediaEvidenceCommandError(
            "producer provenance does not bind the committed source provenance"
        )
    provenance_bindings = {
        (
            item["producer_id"],
            item["producer_policy_sha256"],
            item["detector_sha256"],
            item["calibration_record_sha256"],
            item["producer_version"],
            item["timing_error_bound_tick"],
            item.get("adapter_sha256"),
        )
        for item in provenance["producer_identities"]
    }
    provenance_identities = provenance["producer_identities"]
    if (
        provenance_identities[0]["detector_sha256"] != request.frame_detector_sha256
        or provenance_identities[1]["detector_sha256"] != request.audio_detector_sha256
    ):
        raise TimedMediaEvidenceCommandError(
            "producer provenance replaced committed physical detector identities"
        )
    committed_bindings = {
        (
            item.producer_id,
            item.policy_sha256,
            item.detector_sha256,
            item.calibration_record_sha256,
            item.producer_version,
            item.timing_error_bound_tick,
            item.adapter_sha256,
        )
        for item in produced.calibration_bindings
    }
    if provenance_bindings != committed_bindings:
        raise TimedMediaEvidenceCommandError(
            "producer provenance does not bind the committed calibration set"
        )
    if (
        root.source_id != request.window_manifest.source_id
        or root.source_sha256 != request.window_manifest.source_sha256
        or root.source_manifest_sha256 != request.source_manifest_sha256
        or root.root_input_manifest_sha256 != request.root_input_manifest_sha256
        or root.frame_pts_index != request.frame_pts_index
        or root.audio_sample_boundaries != request.audio_sample_boundaries
    ):
        raise TimedMediaEvidenceCommandError(
            "producer output does not bind the committed request evidence"
        )

def close_timed_media_candidates(
    request: ResolvedPrepareTimedMediaEvidenceRequest,
    produced: ProducedTimedMediaEvidence,
) -> tuple[tuple[CandidateEvidenceWindowPlan, ...], tuple[CandidateTimedEvidenceSet, ...]]:
    root = produced.root_bundle
    plans: list[CandidateEvidenceWindowPlan] = []
    candidates: list[CandidateTimedEvidenceSet] = []
    for candidate in request.semantic_pack.candidate_hypotheses:
        plan = plan_candidate_evidence_window(
            candidate,
            request.semantic_pack,
            request.window_manifest,
            request.frame_pts_index,
            request.adaptive_policy,
        )
        while plan.outcome is CandidateWindowOutcome.AWAITING_EVIDENCE:
            assessment = _assess_window(
                plan.final_window,
                root,
                request.adaptive_policy.boundary_touch_margin_pts,
            )
            plan = advance_candidate_evidence_window(
                plan,
                assessment,
                request.frame_pts_index,
                request.adaptive_policy,
            )
        if plan.outcome is not CandidateWindowOutcome.COMPLETE:
            raise TimedMediaEvidenceCommandError(
                "candidate evidence window did not close within its policy budget"
            )
        final_assessment = plan.final_assessment
        if final_assessment is None:
            raise TimedMediaEvidenceCommandError(
                "complete candidate plan lost its final assessment"
            )
        plans.append(plan)
        candidates.append(
            CandidateTimedEvidenceSet(
                candidate_window=plan.final_window,
                window_assessment=final_assessment,
                transcript=root.transcript,
                speech_activity=root.speech_activity,
                audio_sample_boundaries=root.audio_sample_boundaries,
                frame_pts_index=root.frame_pts_index,
                shot_boundaries=root.shot_boundaries,
                scene_boundaries=root.scene_boundaries,
                visual_validity=root.visual_validity,
                subtitle_cues=root.subtitle_cues,
                calibration_bindings=produced.calibration_bindings,
            )
        )
    return tuple(plans), tuple(candidates)


def _assess_window(
    window: CandidateEvidenceWindow,
    root: RootMediaEvidenceBundle,
    margin_tick: int,
) -> CandidateWindowAssessment:
    video_context = root.frame_pts_index.context
    start = _physical(window.current_range.start_pts, video_context)
    end = _physical(window.current_range.end_pts, video_context)
    margin = Fraction(
        margin_tick * window.source_time_base.numerator, window.source_time_base.denominator
    )
    left_end = min(end, start + margin)
    right_start = max(start, end - margin)
    at_source_start = window.current_range.start_pts == window.source_range.start_pts
    at_source_end = window.current_range.end_pts == window.source_range.end_pts
    transcript_records = (*root.transcript.words, *root.transcript.sentences)
    speech_records = root.speech_activity.segments

    def interval(record: object, context: object) -> tuple[Fraction, Fraction]:
        origin = getattr(context, "origin_tick")
        time_base = getattr(context, "time_base")
        return (
            Fraction(
                (getattr(record, "in_tick") - origin) * time_base.numerator, time_base.denominator
            ),
            Fraction(
                (getattr(record, "out_tick") - origin) * time_base.numerator, time_base.denominator
            ),
        )

    transcript_intervals = tuple(
        interval(item, root.transcript.context) for item in transcript_records
    )
    speech_intervals = tuple(
        interval(item, root.speech_activity.context) for item in speech_records
    )

    def overlaps(item: tuple[Fraction, Fraction], low: Fraction, high: Fraction) -> bool:
        return item[0] < high and low < item[1]

    left_truncated = any(item[0] < start < item[1] for item in transcript_intervals)
    right_truncated = any(item[0] < end < item[1] for item in transcript_intervals)
    if root.transcript.source_outcome is TranscriptSourceOutcome.NO_SPEECH:
        sentence = SentenceCompleteness.NOT_APPLICABLE
    elif root.transcript.source_outcome is TranscriptSourceOutcome.NO_LEXICAL_CONTENT:
        sentence = SentenceCompleteness.UNKNOWN
    elif root.transcript.completeness.sentence.value == "complete":
        sentence = SentenceCompleteness.COMPLETE
    elif root.transcript.completeness.sentence.value == "partial":
        sentence = SentenceCompleteness.PARTIAL
    else:
        sentence = SentenceCompleteness.UNKNOWN
    return CandidateWindowAssessment(
        candidate_window_sha256=window.canonical_hash,
        transcript_left_boundary_touch=not at_source_start
        and any(overlaps(item, start, left_end) for item in transcript_intervals),
        transcript_right_boundary_touch=not at_source_end
        and any(overlaps(item, right_start, end) for item in transcript_intervals),
        speech_left_boundary_touch=not at_source_start
        and any(overlaps(item, start, left_end) for item in speech_intervals),
        speech_right_boundary_touch=not at_source_end
        and any(overlaps(item, right_start, end) for item in speech_intervals),
        left_truncated=left_truncated,
        right_truncated=right_truncated,
        sentence_completeness=sentence,
    )


def _physical(tick: int, context: object) -> Fraction:
    origin = getattr(context, "origin_tick")
    time_base = getattr(context, "time_base")
    return Fraction((tick - origin) * time_base.numerator, time_base.denominator)


def _artifact(
    request: ResolvedPrepareTimedMediaEvidenceRequest,
    artifact_type: str,
    logical_id: str,
    payload: object,
) -> ArtifactMember:
    payload_json = _json(payload)
    return ArtifactMember(
        artifact_type,
        logical_id,
        request.artifact_revision,
        request.artifact_scope,
        canonical_sha256(payload),
        payload_json,
    )


def _artifact_set_hash(artifacts: tuple[ArtifactMember, ...]) -> str:
    return canonical_sha256(
        [
            {
                "artifact_type": item.artifact_type,
                "content_hash": item.content_hash,
                "logical_id": item.logical_id,
                "payload_json": json.loads(item.payload_json),
                "revision": item.revision,
                "scope": _scope_mapping(item.scope),
            }
            for item in artifacts
        ]
    )


def _blob_mapping(reference: BlobRef) -> dict[str, object]:
    return {
        "byte_length": reference.byte_length,
        "content_hash": reference.content_hash,
        "media_type": reference.media_type,
        "object_id": str(reference.object_id),
    }


def _persisted_source_manifest_provenance_mapping(
    persisted: PersistedWholeSeriesSourceManifest,
) -> dict[str, object]:
    """Return the exact source-prep provenance mapping retained by the Store."""

    reference = persisted.reference
    source_job = persisted.source_job
    if type(source_job) is not Job:  # noqa: E721
        raise TimedMediaEvidenceCommandError("committed source manifest has no source Job")
    return {
        "artifact_reference": {
            "artifact_type": reference.artifact_type,
            "content_hash": reference.content_hash,
            "logical_id": reference.logical_id,
            "revision": reference.revision,
            "scope": _scope_mapping(reference.scope),
        },
        "artifact_set_id": str(persisted.artifact_set_id),
        "command_slot_id": str(persisted.command_slot_id),
        "kernel_job_id": str(persisted.job_id),
        "receipt_id": str(persisted.receipt_id),
        "source_job": {
            "job_key": source_job.job_key,
            "profile": source_job.profile,
        },
    }


def _scope_mapping(scope: ArtifactScope) -> dict[str, str]:
    return {"key": scope.key, "kind": scope.kind, "namespace": scope.namespace}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str  # noqa: E721
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _validate_producer_provenance_json(value: object) -> None:
    if type(value) is not str:  # noqa: E721
        raise TimedMediaEvidenceCommandError("producer provenance must be canonical JSON")
    try:
        decoded: object = json.loads(value)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise TimedMediaEvidenceCommandError(
            "producer provenance must be canonical JSON"
        ) from error
    if type(decoded) is not dict:  # noqa: E721
        raise TimedMediaEvidenceCommandError("producer provenance JSON is not canonical")
    payload = cast(dict[str, object], decoded)
    if _json(payload) != value:
        raise TimedMediaEvidenceCommandError("producer provenance JSON is not canonical")
    expected = {
        "producer_identities",
        "schema_version",
        "source_provenance_sha256",
        "tool_invocations",
        "tool_trace_sha256",
    }
    if set(payload) != expected or payload["schema_version"] != (
        "local-media-producer-provenance-v1"
    ):
        raise TimedMediaEvidenceCommandError("producer provenance schema is not closed")
    if not _is_sha256(payload["source_provenance_sha256"]) or not _is_sha256(
        payload["tool_trace_sha256"]
    ):
        raise TimedMediaEvidenceCommandError("producer provenance hashes are invalid")
    invocations_raw = payload["tool_invocations"]
    identities_raw = payload["producer_identities"]
    if type(invocations_raw) is not list:  # noqa: E721
        raise TimedMediaEvidenceCommandError("producer tool trace must not be empty")
    invocations = cast(list[object], invocations_raw)
    if not invocations:
        raise TimedMediaEvidenceCommandError("producer tool trace must not be empty")
    if type(identities_raw) is not list:  # noqa: E721
        raise TimedMediaEvidenceCommandError("producer identity set must be closed")
    identities = cast(list[object], identities_raw)
    if len(identities) != 8:
        raise TimedMediaEvidenceCommandError("producer identity set must be closed")
    invocation_fields = {
        "argv_sha256",
        "executable",
        "executable_sha256",
        "producer_kind",
        "stderr_sha256",
        "stdout_sha256",
        "version_evidence_sha256",
    }
    canonical_invocations: list[dict[str, object]] = []
    allowed_invocations = {
        ("asr", "funasr-http"),
        ("probe", "ffprobe"),
        ("subtitle", "ffprobe"),
        ("vad", "funasr-http"),
        ("visual", "ffmpeg"),
    }
    for invocation_raw in invocations:
        if type(invocation_raw) is not dict:  # noqa: E721
            raise TimedMediaEvidenceCommandError("producer tool trace schema is not closed")
        invocation = cast(dict[str, object], invocation_raw)
        if set(invocation) != invocation_fields:
            raise TimedMediaEvidenceCommandError("producer tool trace schema is not closed")
        for field in invocation_fields - {"executable", "producer_kind"}:
            if not _is_sha256(invocation[field]):
                raise TimedMediaEvidenceCommandError("producer tool trace hash is invalid")
        if (invocation["producer_kind"], invocation["executable"]) not in (allowed_invocations):
            raise TimedMediaEvidenceCommandError(
                "producer tool trace executable identity is not registered"
            )
        canonical_invocations.append(invocation)
    if payload["tool_trace_sha256"] != canonical_sha256(canonical_invocations):
        raise TimedMediaEvidenceCommandError("producer tool trace hash does not close")
    identity_fields = {
        "calibration_policy_sha256",
        "calibration_record_sha256",
        "detector_sha256",
        "producer_id",
        "producer_kind",
        "producer_policy_sha256",
        "producer_version",
        "timing_error_bound_tick",
    }
    expected_kinds = (
        "frame",
        "audio",
        "asr",
        "vad",
        "shot",
        "scene",
        "visual",
        "subtitle",
    )
    for position, identity_raw in enumerate(identities):
        if type(identity_raw) is not dict:  # noqa: E721
            raise TimedMediaEvidenceCommandError("producer identity schema is not closed")
        identity = cast(dict[str, object], identity_raw)
        if set(identity) != identity_fields | {"adapter_sha256"}:
            raise TimedMediaEvidenceCommandError("producer identity schema is not closed")
        if identity["producer_kind"] != expected_kinds[position]:
            raise TimedMediaEvidenceCommandError("producer identity order is not canonical")
        bound = identity["timing_error_bound_tick"]
        if type(bound) is not int or bound <= 0:  # noqa: E721
            raise TimedMediaEvidenceCommandError("producer timing bound must be an exact positive integer")
        for field in ("producer_id", "producer_version"):
            text = identity[field]
            if type(text) is not str or not text.strip():  # noqa: E721
                raise TimedMediaEvidenceCommandError(f"producer identity {field} must be nonempty text")
            try:
                text.encode("utf-8", errors="strict")
            except UnicodeEncodeError as error:
                raise TimedMediaEvidenceCommandError(f"producer identity {field} must be valid UTF-8") from error
        for field in (
            "calibration_policy_sha256",
            "calibration_record_sha256",
            "detector_sha256",
            "producer_policy_sha256",
        ):
            if not _is_sha256(identity[field]):
                raise TimedMediaEvidenceCommandError("producer identity hash is invalid")
        adapter_sha256 = identity["adapter_sha256"]
        if identity["producer_kind"] in ("asr", "vad") and not _is_sha256(adapter_sha256):
            raise TimedMediaEvidenceCommandError("speech producer adapter hash is invalid")
        if adapter_sha256 is not None and not _is_sha256(adapter_sha256):
            raise TimedMediaEvidenceCommandError("producer identity adapter hash is invalid")


def _validate_canonical_mapping_json(
    value: object,
    expected_sha256: str,
    label: str,
) -> None:
    if type(value) is not str:  # noqa: E721
        raise TimedMediaEvidenceCommandError(f"{label} must be canonical JSON")
    try:
        decoded: object = json.loads(value)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise TimedMediaEvidenceCommandError(f"{label} must be canonical JSON") from error
    if type(decoded) is not dict:  # noqa: E721
        raise TimedMediaEvidenceCommandError(f"{label} must be a JSON object")
    payload = cast(dict[str, object], decoded)
    if _json(payload) != value or canonical_sha256(payload) != expected_sha256:
        raise TimedMediaEvidenceCommandError(f"{label} does not match its canonical hash")


__all__ = (
    "resolve_committed_timed_media_request",
    "timed_media_request_hash",
    "validate_produced_timed_media_evidence",
    "close_timed_media_candidates",
    "PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND",
    "TIMED_MEDIA_EVIDENCE_STRATEGY_VERSION",
    "PrepareTimedMediaEvidenceCommand",
    "PrepareTimedMediaEvidenceRequest",
    "PrepareTimedMediaEvidenceResult",
    "ProducedTimedMediaEvidence",
    "TimedMediaEvidenceCommandError",
    "TimedMediaEvidenceProducerError",
    "TimedMediaEvidenceProducerPort",
    "TimedMediaEvidenceStore",
)
