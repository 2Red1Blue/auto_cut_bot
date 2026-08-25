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
from ..media.types import canonical_sha256
from ..registry.timed_speech import (
    DEFAULT_TIMED_SPEECH_AUTHORITY_SNAPSHOT,
    AuthorityRegistrySnapshot,
    BootstrappedTimedSpeechProfile,
    StoreAnchoredTimedSpeechProfileResolver,
    TimedSpeechProfileResolver,
    TimedSpeechRegistryError,
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
    MaterializationError,
    MaterializationLimits,
    VerifiedMaterializedBlob,
    canonical_recipe_scope,
)
from ..vlm import VlmSemanticPack, WindowManifest

PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND = "PrepareTimedMediaEvidence@2.1.3"
TIMED_MEDIA_EVIDENCE_STRATEGY_VERSION = "whole-episode-conjunctive-evidence-v1"
TIMED_MEDIA_EVIDENCE_BATCH_STRATEGY_VERSION = "timed-media-evidence-batch-v1"
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
        request: PrepareTimedMediaEvidenceRequest,
        source: VerifiedMaterializedBlob,
    ) -> ProducedTimedMediaEvidence: ...


class TimedMediaEvidenceStore(Protocol):
    def read_outcome(self, job: Job, idempotency_key: str) -> CommandOutcome | None: ...

    def read_bootstrapped_timed_speech_profile(
        self,
        snapshot: AuthorityRegistrySnapshot,
    ) -> BootstrappedTimedSpeechProfile: ...

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
    source_manifest_sha256: str
    source_provenance_sha256: str
    window_manifest: WindowManifest
    semantic_pack: VlmSemanticPack
    frame_pts_index: FramePtsIndexSet
    audio_sample_boundaries: AudioSampleBoundarySet
    frame_detector_sha256: str
    audio_detector_sha256: str
    adaptive_policy: AdaptiveEvidenceWindowPolicy
    producer_policy_sha256: str
    materialization_limits: MaterializationLimits
    presentation_timeline_probe: PresentationTimelineProbe

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
            "source_manifest_sha256",
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
        if type(self.presentation_timeline_probe) is not PresentationTimelineProbe:  # noqa: E721
            raise TimedMediaEvidenceCommandError(
                "preflight requires exact source-prep presentation map facts; no synthetic map is permitted"
            )
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
        facts = self.presentation_timeline_probe
        if (
            facts.source_id != manifest.source_id
            or facts.source_sha256 != self.source_blob.content_hash
            or facts.source_blob_content_hash != self.source_blob.content_hash
            or facts.source_blob_byte_length != self.source_blob.byte_length
            or facts.source_blob_media_type != self.source_blob.media_type
            or facts.frame_pts_index_set_sha256 != self.frame_pts_index.canonical_hash
            or facts.audio_sample_boundary_set_sha256 != self.audio_sample_boundaries.canonical_hash
            or facts.video.clock_id != self.frame_pts_index.context.clock_id
            or facts.video.time_base != self.frame_pts_index.context.time_base
            or facts.video.origin_tick != self.frame_pts_index.context.origin_tick
            or facts.video.end_tick != self.frame_pts_index.context.end_tick
            or facts.audio.clock_id != self.audio_sample_boundaries.context.clock_id
            or facts.audio.time_base != self.audio_sample_boundaries.context.time_base
            or facts.audio.origin_tick != self.audio_sample_boundaries.context.origin_tick
            or facts.audio.end_tick != self.audio_sample_boundaries.context.end_tick
            or facts.window_manifest_sha256 != manifest.canonical_hash
        ):
            raise TimedMediaEvidenceCommandError("source-prep presentation facts do not close request identities")

    @property
    def root_input_manifest_sha256(self) -> str:
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
                "presentation_timeline_probe_sha256": self.presentation_timeline_probe.canonical_hash,
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
            "producer_policy_sha256": self.producer_policy_sha256,
            "presentation_timeline_probe_sha256": self.presentation_timeline_probe.canonical_hash,
            "materialization_policy_sha256": self.materialization_limits.evidence_policy_sha256,
            "max_source_bytes": self.materialization_limits.max_source_bytes,
            "timed_speech_max_request_bytes": (
                self.materialization_limits.timed_speech_max_request_bytes
            ),
            "effective_max_source_bytes": self.materialization_limits.effective_max_source_bytes,
            "root_input_manifest_sha256": self.root_input_manifest_sha256,
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
class PrepareTimedMediaEvidenceResult:
    outcome: CommandOutcome
    root_bundle_sha256: str | None = None
    candidate_count: int = 0


@dataclass(frozen=True, slots=True)
class TimedMediaEvidenceBatchChild:
    episode_index: int
    idempotency_key: str
    receipt_id: UUID
    artifact_set_id: UUID

    def __post_init__(self) -> None:
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise TimedMediaEvidenceCommandError("batch episode_index must be non-negative")
        if not self.idempotency_key or self.idempotency_key != self.idempotency_key.strip():
            raise TimedMediaEvidenceCommandError("batch idempotency_key must be canonical")
        if not isinstance(self.receipt_id, UUID) or not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
            self.artifact_set_id, UUID
        ):
            raise TimedMediaEvidenceCommandError(
                "batch child requires exact Receipt and ArtifactSet identities"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_set_id": str(self.artifact_set_id),
            "episode_index": self.episode_index,
            "idempotency_key": self.idempotency_key,
            "receipt_id": str(self.receipt_id),
        }


@dataclass(frozen=True, slots=True)
class FinalizeTimedMediaEvidenceBatchRequest:
    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    source_manifest_sha256: str
    source_provenance_sha256: str
    children: tuple[TimedMediaEvidenceBatchChild, ...]

    def __post_init__(self) -> None:
        if type(self.job) is not Job or self.artifact_scope != canonical_recipe_scope(self.job):  # noqa: E721
            raise TimedMediaEvidenceCommandError("batch Job scope is invalid")
        if not self.idempotency_key or self.idempotency_key != self.idempotency_key.strip():
            raise TimedMediaEvidenceCommandError("batch idempotency_key must be canonical")
        if type(self.artifact_revision) is not int or self.artifact_revision < 1:  # noqa: E721
            raise TimedMediaEvidenceCommandError("batch artifact_revision must be positive")
        if not _is_sha256(self.source_manifest_sha256) or not _is_sha256(
            self.source_provenance_sha256
        ):
            raise TimedMediaEvidenceCommandError("batch source identities must be sha256")
        children = tuple(self.children)
        if not children or any(type(item) is not TimedMediaEvidenceBatchChild for item in children):  # noqa: E721
            raise TimedMediaEvidenceCommandError("batch children must be exact typed values")
        if tuple(item.episode_index for item in children) != tuple(range(len(children))):
            raise TimedMediaEvidenceCommandError(
                "batch children must cover all ordered episode indexes"
            )
        keys = tuple(item.idempotency_key for item in children)
        if len(keys) != len(set(keys)):
            raise TimedMediaEvidenceCommandError("batch child keys must be unique")
        object.__setattr__(self, "children", children)

    @property
    def request_hash(self) -> str:
        return canonical_sha256(
            {
                "artifact_revision": self.artifact_revision,
                "artifact_scope": _scope_mapping(self.artifact_scope),
                "children": [item.to_mapping() for item in self.children],
                "job": {"job_key": self.job.job_key, "profile": self.job.profile},
                "source_manifest_sha256": self.source_manifest_sha256,
                "source_provenance_sha256": self.source_provenance_sha256,
                "strategy_version": TIMED_MEDIA_EVIDENCE_BATCH_STRATEGY_VERSION,
            }
        )


@dataclass(frozen=True, slots=True)
class FinalizeTimedMediaEvidenceBatchResult:
    outcome: CommandOutcome
    artifact: ArtifactMember | None = None


@dataclass(frozen=True, slots=True)
class _BatchArtifactRequest:
    artifact_scope: ArtifactScope
    artifact_revision: int


class FinalizeTimedMediaEvidenceBatchCommand:
    """Commit an aggregate Receipt only after independently rereading every child."""

    def __init__(self, store: TimedMediaEvidenceStore) -> None:
        self._store = store

    def execute(
        self,
        request: FinalizeTimedMediaEvidenceBatchRequest,
    ) -> FinalizeTimedMediaEvidenceBatchResult:
        for child in request.children:
            outcome = self._store.read_outcome(request.job, child.idempotency_key)
            if (
                outcome is None
                or outcome.state != "succeeded"
                or outcome.receipt_id != child.receipt_id
                or outcome.artifact_set_id != child.artifact_set_id
            ):
                raise TimedMediaEvidenceCommandError(
                    "batch child does not match its persisted succeeded outcome"
                )
        claimed = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                "FinalizeTimedMediaEvidenceBatch@2.1.3",
                request.request_hash,
            )
        )
        if not claimed.is_fresh_claim:
            return FinalizeTimedMediaEvidenceBatchResult(claimed)
        payload = {
            "children": [item.to_mapping() for item in request.children],
            "completion_policy": "all_committed_episodes",
            "declared_episode_count": len(request.children),
            "source_manifest_sha256": request.source_manifest_sha256,
            "source_provenance_sha256": request.source_provenance_sha256,
            "strategy_version": TIMED_MEDIA_EVIDENCE_BATCH_STRATEGY_VERSION,
        }
        artifact = _artifact(
            _BatchArtifactRequest(request.artifact_scope, request.artifact_revision),
            "timed_media_evidence_batch",
            "timed_media_evidence_batch",
            payload,
        )
        committed = self._store.commit_command_success(
            CommandSuccess(
                claimed.command_slot_id,
                _artifact_set_hash((artifact,)),
                (artifact,),
            )
        )
        return FinalizeTimedMediaEvidenceBatchResult(committed, artifact)


class PrepareTimedMediaEvidenceCommand:
    """Claim, run all local producers, validate conjunction, and commit once."""

    def __init__(
        self,
        store: TimedMediaEvidenceStore,
        producer: TimedMediaEvidenceProducerPort,
        authority_profile_resolver: TimedSpeechProfileResolver | None = None,
    ) -> None:
        self._store = store
        self._producer = producer
        self._authority_profile_resolver = (
            StoreAnchoredTimedSpeechProfileResolver(DEFAULT_TIMED_SPEECH_AUTHORITY_SNAPSHOT)
            if authority_profile_resolver is None
            else authority_profile_resolver
        )

    def execute(
        self,
        request: PrepareTimedMediaEvidenceRequest,
    ) -> PrepareTimedMediaEvidenceResult:
        claimed = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND,
                canonical_sha256(
                    {
                        "authority_registry_set_sha256": self._authority_profile_resolver.snapshot.registry_set_sha256,
                        "authority_profile_key": self._authority_profile_resolver.snapshot.enabled_profile.value,
                        "request": request.canonical_payload(),
                    }
                ),
            )
        )
        if not claimed.is_fresh_claim:
            return PrepareTimedMediaEvidenceResult(claimed)
        source: VerifiedMaterializedBlob | None = None
        try:
            resolved_profile = self._read_timed_speech_profile_registry()
            if (
                request.source_blob.byte_length
                > request.materialization_limits.effective_max_source_bytes
            ):
                raise TimedMediaEvidenceProducerError(
                    "MEDIA_SOURCE_BYTE_LIMIT_EXCEEDED",
                    "committed source exceeds the frozen effective source-byte limit",
                )
            source = self._store.materialize_immutable_blob(
                request.job,
                request.source_blob,
                request.materialization_limits,
            )
            busy_attempts = 0
            while True:
                try:
                    produced = self._producer.prepare(request, source)
                    break
                except TimedMediaEvidenceProducerError as error:
                    if (
                        error.code != "TIMED_SPEECH_BUSY"
                        or busy_attempts >= TIMED_SPEECH_BUSY_RETRY_COUNT
                    ):
                        raise
                    busy_attempts += 1
                    time.sleep(TIMED_SPEECH_BUSY_RETRY_DELAY_SECONDS)
            self._validate_produced(request, produced)
            plans, candidates = self._close_candidates(request, produced)
            artifacts = self._persist_artifacts(
                request, produced, plans, candidates, resolved_profile
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

    @staticmethod
    def _validate_produced(
        request: PrepareTimedMediaEvidenceRequest,
        produced: ProducedTimedMediaEvidence,
    ) -> None:
        if type(produced) is not ProducedTimedMediaEvidence:  # noqa: E721
            raise TimedMediaEvidenceCommandError("producer returned another result type")
        root = produced.root_bundle
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

    @staticmethod
    def _close_candidates(
        request: PrepareTimedMediaEvidenceRequest,
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

    def _persist_artifacts(
        self,
        request: PrepareTimedMediaEvidenceRequest,
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
    request: PrepareTimedMediaEvidenceRequest | _BatchArtifactRequest,
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
        if set(identity) not in (identity_fields, identity_fields | {"adapter_sha256"}):
            raise TimedMediaEvidenceCommandError("producer identity schema is not closed")
        if identity["producer_kind"] != expected_kinds[position]:
            raise TimedMediaEvidenceCommandError("producer identity order is not canonical")
        for field in (
            "calibration_policy_sha256",
            "calibration_record_sha256",
            "detector_sha256",
            "producer_policy_sha256",
        ):
            if not _is_sha256(identity[field]):
                raise TimedMediaEvidenceCommandError("producer identity hash is invalid")
        if "adapter_sha256" in identity and not _is_sha256(identity["adapter_sha256"]):
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
    "PREPARE_TIMED_MEDIA_EVIDENCE_COMMAND",
    "TIMED_MEDIA_EVIDENCE_BATCH_STRATEGY_VERSION",
    "TIMED_MEDIA_EVIDENCE_STRATEGY_VERSION",
    "FinalizeTimedMediaEvidenceBatchCommand",
    "FinalizeTimedMediaEvidenceBatchRequest",
    "FinalizeTimedMediaEvidenceBatchResult",
    "PrepareTimedMediaEvidenceCommand",
    "PrepareTimedMediaEvidenceRequest",
    "PrepareTimedMediaEvidenceResult",
    "ProducedTimedMediaEvidence",
    "TimedMediaEvidenceCommandError",
    "TimedMediaEvidenceBatchChild",
    "TimedMediaEvidenceProducerError",
    "TimedMediaEvidenceProducerPort",
    "TimedMediaEvidenceStore",
)
