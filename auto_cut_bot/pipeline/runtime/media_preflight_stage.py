"""Real HTTP Pipeline adapter for committed local timed-media evidence."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import Protocol, TypeVar, cast

from autocut_kernel.context_pack import WindowContextPack
from autocut_kernel.media.runtime_measurement_identity import (
    PC_CUDA_RUNTIME_CAPABILITY_ID,
    RuntimeMeasurementIdentity,
)
from autocut_kernel.pipeline import (
    FinalizeRuntimeTimedMediaEvidenceBatchCommand,
    FinalizeRuntimeTimedMediaEvidenceBatchRequest,
    FinalizeRuntimeTimedMediaEvidenceBatchResult,
    FinalizeTimedMediaEvidenceBatchCommand,
    FinalizeTimedMediaEvidenceBatchRequest,
    FinalizeTimedMediaEvidenceBatchResult,
    PrepareRuntimeTimedMediaEvidenceCommand,
    PrepareRuntimeTimedMediaEvidenceRequest,
    PrepareRuntimeTimedMediaEvidenceResult,
    PrepareTimedMediaEvidenceCommand,
    PrepareTimedMediaEvidenceRequest,
    PrepareTimedMediaEvidenceResult,
    ProducedRuntimeTimedMediaEvidence,
    ProducedTimedMediaEvidence,
    RuntimeTimedMediaEvidenceBatchChild,
    TimedMediaEvidenceBatchChild,
    TimedMediaEvidenceProducerError,
)
from autocut_kernel.pipeline.committed_timed_media import TimedMediaReadLimits, TimedMediaReadStore
from autocut_kernel.pipeline.prepare_timed_media_evidence_command import (
    ResolvedPrepareTimedMediaEvidenceRequest,
)
from autocut_kernel.registry import (
    StoreAnchoredTimedSpeechProfileResolver,
)
from autocut_kernel.registry.calibration_binding import CalibrationBindingError
from autocut_kernel.registry.installed_runtime import (
    InstalledLocalRunProfileResolver,
    InstalledRuntimeCapabilityStore,
    InstalledRuntimeTimedSpeechAuthorityResolver,
)
from autocut_kernel.registry.runtime_timed_speech import RuntimeTimedSpeechProjection
from autocut_kernel.store import (
    ArtifactScope,
    CommandOutcome,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedSemanticInputsRequest,
    Job,
    MediaEvidenceUnavailableError,
    PersistedVlmSemanticPack,
    RuntimeCalibrationIdentityMismatchError,
    RuntimeStoreError,
    SemanticInputUnavailableError,
    StoreValidationError,
)
from autocut_kernel.store.models import (
    MaterializationLimits,
    PersistedCommittedArtifactSet,
    VerifiedMaterializedBlob,
    canonical_recipe_scope,
)

from auto_cut_bot.pipeline.context_prepare import (
    ContextPrepareStore,
    find_committed_window_context_packs,
)
from auto_cut_bot.pipeline.media_preflight import (
    LocalMediaPreflightError,
    LocalMediaPreflightPolicy,
    LocalMediaPreflightPort,
    LocalMediaPreflightRequest,
    LocalMediaToolError,
    PcCudaRuntimeTimedSpeechPolicy,
    RuntimeMeasurementIdentityPort,
    RuntimeMediaPreflightRequest,
)
from auto_cut_bot.pipeline.media_preflight.installed_policy import validate_installed_media_policy
from auto_cut_bot.pipeline.media_preflight.runtime_policy import (
    project_pc_cuda_runtime_timed_speech_policy,
)
from auto_cut_bot.pipeline.source_prep import (
    PersistedPreparedSources,
    SourcePrepStore,
    read_persisted_prepared_sources_bundle,
)
from auto_cut_bot.pipeline.vlm import DoubaoVlmRequestPolicy
from auto_cut_bot.pipeline.vlm.policy_binding import (
    validate_installed_source_sampling,
    validate_installed_vlm_policy,
)

from .errors import PipelineRunValidationError
from .models import (
    MediaPreflightRecomputeRequest,
    PipelineExecutionProfile,
    PipelineStageContext,
    PipelineStageResult,
    validate_run_id,
)
from .source_prep_stage import (
    require_committed_source_operation,
    source_prep_kernel_idempotency_key,
)
from .vlm_stage import requires_window_context_pack, vlm_batch_kernel_idempotency_key

# These historical strings remain part of committed command identity. Dispatch
# concurrency is operational and cannot invalidate deterministic evidence, so
# changing the scheduler must not rotate them and force a media recomputation.
MEDIA_PREFLIGHT_EPISODE_STRATEGY_VERSION = "all-episodes-local-evidence-sequential-v1"
RUNTIME_MEDIA_PREFLIGHT_EPISODE_STRATEGY_VERSION = "all-episodes-pc-cuda-evidence-sequential-v1"
MEDIA_PREFLIGHT_EPISODE_MAX_CONCURRENCY = 3
_ARTIFACT_REVISION = 1

_RequestT = TypeVar("_RequestT")
_ResultT = TypeVar("_ResultT")


async def _execute_independent_requests(
    requests: tuple[_RequestT, ...],
    execute: Callable[[_RequestT], _ResultT],
    *,
    max_concurrency: int,
) -> tuple[_ResultT, ...]:
    """Run every independent episode while bounding native resource pressure.

    A child outcome is data returned by ``execute``; it never cancels siblings.
    Unexpected implementation exceptions still abort the invocation because
    they do not carry a durable per-episode Receipt.
    """

    if type(max_concurrency) is not int or max_concurrency < 1:  # noqa: E721
        raise PipelineRunValidationError("media-preflight concurrency must be positive")
    semaphore = asyncio.Semaphore(max_concurrency)

    async def run_one(request: _RequestT) -> _ResultT:
        async with semaphore:
            return await asyncio.to_thread(execute, request)

    return tuple(await asyncio.gather(*(run_one(request) for request in requests)))


def media_preflight_vlm_batch_kernel_idempotency_key(
    *,
    run_id: str,
    source_bundle: PersistedPreparedSources,
    policy: DoubaoVlmRequestPolicy,
    execution_profile_hash: str,
    context_packs: tuple[WindowContextPack, ...] | None,
) -> str:
    """Reproduce the already-committed VLM aggregate identity exactly."""
    if requires_window_context_pack(policy):
        if (
            type(context_packs) is not tuple  # noqa: E721
            or len(context_packs) != len(source_bundle.prepared.episodes)
            or any(type(pack) is not WindowContextPack for pack in context_packs)  # noqa: E721
        ):
            raise PipelineRunValidationError(
                "media-preflight requires one exact context pack per source episode"
            )
    elif context_packs is not None:
        raise PipelineRunValidationError(
            "non-contextual VLM batch identity cannot bind context packs"
        )
    return vlm_batch_kernel_idempotency_key(
        run_id=run_id,
        source_bundle=source_bundle,
        policy=policy,
        execution_profile_hash=execution_profile_hash,
        context_packs=context_packs,
    )


def media_evidence_read_limits(profile: PipelineExecutionProfile) -> TimedMediaReadLimits:
    """Use explicit evidence ceilings and shared host-only staging controls."""
    budget = profile.to_evidence_read_limits()
    transfer = profile.to_materialization_limits()
    if transfer.copy_chunk_bytes > budget.max_blob_bytes:
        raise PipelineRunValidationError(
            "evidence blob ceiling is smaller than the staging copy chunk"
        )
    # Evidence never goes to the timed-speech service. Reuse the Store's
    # bounded lease mechanism with its own byte ceiling, sharing only the
    # explicitly frozen host copy/quota controls with source staging.
    return TimedMediaReadLimits(
        budget.max_blob_bytes,
        budget.max_total_blob_bytes,
        profile.to_doubao_policy().parse_policy.max_candidate_hypotheses,
        MaterializationLimits(
            budget.max_blob_bytes,
            budget.max_blob_bytes,
            transfer.copy_chunk_bytes,
            transfer.staging_quota_bytes,
        ),
    )


class MediaPreflightPipelineStore(
    SourcePrepStore,
    TimedMediaReadStore,
    InstalledRuntimeCapabilityStore,
    Protocol,
):
    def read_committed_vlm_semantic_pack_set_reference(
        self,
        job: Job,
        idempotency_key: str,
    ) -> CommittedArtifactMemberReference: ...

    def read_committed_semantic_inputs(
        self,
        request: CommittedSemanticInputsRequest,
    ) -> CommittedSemanticInputs: ...

    def find_committed_context_pack_set(
        self,
        job: Job,
        *,
        artifact_scope: ArtifactScope,
        artifact_revision: int,
    ) -> PersistedCommittedArtifactSet | None: ...


class _RuntimeCudaAuthority:
    """Fresh capability projection held only during one stage invocation."""

    def __init__(
        self,
        measurement: RuntimeMeasurementIdentity,
        projection: RuntimeTimedSpeechProjection,
        policy: PcCudaRuntimeTimedSpeechPolicy,
    ) -> None:
        self.measurement = measurement
        self.projection = projection
        self.policy = policy


class _ClaimOwnedLocalProducer:
    """Adapt a Kernel-owned verified-file lease to the local detector port."""

    def __init__(
        self,
        port: LocalMediaPreflightPort,
        policy: LocalMediaPreflightPolicy,
        timed_speech_adapter_sha256: str,
    ) -> None:
        self._port = port
        self._policy = policy
        self._timed_speech_adapter_sha256 = timed_speech_adapter_sha256

    def prepare(
        self,
        request: ResolvedPrepareTimedMediaEvidenceRequest,
        source: VerifiedMaterializedBlob,
    ) -> ProducedTimedMediaEvidence:
        if source.reference != request.source_blob:
            raise TimedMediaEvidenceProducerError(
                "COMMITTED_SOURCE_BLOB_MISMATCH",
                "Kernel materialization does not match the committed BlobRef",
            )
        try:
            local = self._port.prepare(
                LocalMediaPreflightRequest(
                    source_path=source.path,
                    episode_id=f"episode-{request.episode_index:04d}",
                    source_id=request.window_manifest.source_id,
                    source_sha256=request.window_manifest.source_sha256,
                    source_provenance_sha256=request.source_provenance_sha256,
                    source_manifest_sha256=request.source_manifest_sha256,
                    root_input_manifest_sha256=request.root_input_manifest_sha256,
                    frame_pts_index=request.frame_pts_index,
                    audio_sample_boundaries=request.audio_sample_boundaries,
                    frame_detector_sha256=request.frame_detector_sha256,
                    audio_detector_sha256=request.audio_detector_sha256,
                    policy=self._policy,
                    timed_speech_adapter_sha256=self._timed_speech_adapter_sha256,
                ),
                kernel_max_source_bytes=request.materialization_limits.max_source_bytes,
                service_max_request_bytes=(
                    request.materialization_limits.timed_speech_max_request_bytes
                ),
            )
        except LocalMediaToolError as error:
            raise TimedMediaEvidenceProducerError(
                error.code,
                str(error),
                outcome="failed",
            ) from error
        except LocalMediaPreflightError as error:
            raise TimedMediaEvidenceProducerError(error.code, str(error)) from error
        return ProducedTimedMediaEvidence(
            self._policy.canonical_hash,
            local.evidence,
            local.calibration_bindings,
            json.dumps(
                self._policy.to_mapping(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            json.dumps(
                local.provenance_mapping(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )


class _ClaimOwnedRuntimeCudaProducer:
    """Adapt a claim-owned lease to the dedicated CUDA producer grammar."""

    def __init__(
        self,
        port: LocalMediaPreflightPort,
        physical_policy: LocalMediaPreflightPolicy,
        runtime_policy: PcCudaRuntimeTimedSpeechPolicy,
    ) -> None:
        self._port = port
        self._physical_policy = physical_policy
        self._runtime_policy = runtime_policy

    def prepare(
        self,
        request: ResolvedPrepareTimedMediaEvidenceRequest,
        source: VerifiedMaterializedBlob,
        projection: RuntimeTimedSpeechProjection,
    ) -> ProducedRuntimeTimedMediaEvidence:
        if source.reference != request.source_blob:
            raise TimedMediaEvidenceProducerError(
                "COMMITTED_SOURCE_BLOB_MISMATCH",
                "Kernel materialization does not match the committed BlobRef",
            )
        if projection.canonical_hash != self._runtime_policy.runtime_projection_sha256:
            raise TimedMediaEvidenceProducerError(
                "RUNTIME_PROJECTION_DRIFT",
                "runtime policy differs from command-resolved CUDA projection",
            )
        local_request = LocalMediaPreflightRequest(
            source_path=source.path,
            episode_id=f"episode-{request.episode_index:04d}",
            source_id=request.window_manifest.source_id,
            source_sha256=request.window_manifest.source_sha256,
            source_provenance_sha256=request.source_provenance_sha256,
            source_manifest_sha256=request.source_manifest_sha256,
            root_input_manifest_sha256=request.root_input_manifest_sha256,
            frame_pts_index=request.frame_pts_index,
            audio_sample_boundaries=request.audio_sample_boundaries,
            frame_detector_sha256=request.frame_detector_sha256,
            audio_detector_sha256=request.audio_detector_sha256,
            policy=self._physical_policy,
            timed_speech_adapter_sha256=projection.native_port_identity_sha256,
        )
        try:
            produced = self._port.prepare_runtime_cuda(
                RuntimeMediaPreflightRequest(local_request, self._runtime_policy),
                kernel_max_source_bytes=request.materialization_limits.max_source_bytes,
                service_max_request_bytes=request.materialization_limits.timed_speech_max_request_bytes,
            )
        except LocalMediaToolError as error:
            raise TimedMediaEvidenceProducerError(
                error.code, str(error), outcome="failed"
            ) from error
        except LocalMediaPreflightError as error:
            raise TimedMediaEvidenceProducerError(error.code, str(error)) from error
        authority = produced.runtime_policy.to_mapping()
        return ProducedRuntimeTimedMediaEvidence(
            ProducedTimedMediaEvidence(
                produced.runtime_policy.canonical_hash,
                produced.evidence,
                produced.calibration_bindings,
                json.dumps(authority, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                json.dumps(
                    produced.provenance_mapping(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                producer_provenance_schema="runtime-cuda-media-producer-provenance-v2",
            ),
            projection,
            authority,
        )


def media_preflight_kernel_idempotency_key(
    *,
    run_id: str,
    episode_index: int,
    source_bundle: PersistedPreparedSources,
    semantic_pack: PersistedVlmSemanticPack,
    producer_policy_sha256: str,
    adaptive_policy_sha256: str,
    materialization_policy_sha256: str,
) -> str:
    validate_run_id(run_id)
    if type(episode_index) is not int or episode_index < 0:  # noqa: E721
        raise PipelineRunValidationError("media-preflight episode index must be non-negative")
    payload = {
        "adaptive_policy_sha256": adaptive_policy_sha256,
        "episode_index": episode_index,
        "semantic_pack_sha256": semantic_pack.semantic_pack.canonical_hash,
        "producer_policy_sha256": producer_policy_sha256,
        "materialization_policy_sha256": materialization_policy_sha256,
        "run_id": run_id,
        "source_manifest_sha256": source_bundle.artifact_reference.content_hash,
        "source_provenance_sha256": source_bundle.canonical_hash,
        "strategy_version": MEDIA_PREFLIGHT_EPISODE_STRATEGY_VERSION,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "media-preflight:" + hashlib.sha256(encoded).hexdigest()


def runtime_media_preflight_kernel_idempotency_key(
    *,
    run_id: str,
    episode_index: int,
    source_bundle: PersistedPreparedSources,
    semantic_pack: PersistedVlmSemanticPack,
    runtime_policy: PcCudaRuntimeTimedSpeechPolicy,
    adaptive_policy_sha256: str,
    materialization_policy_sha256: str,
) -> str:
    """A CUDA child cannot collide with the historical CPU command slot."""
    payload = {
        "adaptive_policy_sha256": adaptive_policy_sha256,
        "episode_index": episode_index,
        "materialization_policy_sha256": materialization_policy_sha256,
        "runtime_policy_sha256": runtime_policy.canonical_hash,
        "semantic_pack_sha256": semantic_pack.semantic_pack.canonical_hash,
        "source_manifest_sha256": source_bundle.artifact_reference.content_hash,
        "source_provenance_sha256": source_bundle.canonical_hash,
        "strategy_version": RUNTIME_MEDIA_PREFLIGHT_EPISODE_STRATEGY_VERSION,
    }
    return (
        "runtime-media-preflight:"
        + hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
    )


class MediaPreflightPipelineStage:
    """Prepare every committed episode, then commit one aggregate stage Receipt."""

    def __init__(
        self,
        store: MediaPreflightPipelineStore,
        port: LocalMediaPreflightPort,
        authority_profile_resolver: InstalledLocalRunProfileResolver,
        runtime_authority_resolver: InstalledRuntimeTimedSpeechAuthorityResolver | None = None,
        runtime_measurement_port: RuntimeMeasurementIdentityPort | None = None,
        episode_max_concurrency: int = MEDIA_PREFLIGHT_EPISODE_MAX_CONCURRENCY,
    ) -> None:
        self._store = store
        self._port = port
        if type(authority_profile_resolver) is not InstalledLocalRunProfileResolver:  # noqa: E721
            raise PipelineRunValidationError(
                "media-preflight requires the installed accepted-profile resolver"
            )
        self._authority_profile_resolver = authority_profile_resolver
        if (runtime_authority_resolver is None) != (runtime_measurement_port is None):
            raise PipelineRunValidationError(
                "media-preflight runtime capability resolver and measurement port must be paired"
            )
        if (
            runtime_authority_resolver is not None
            and type(runtime_authority_resolver) is not InstalledRuntimeTimedSpeechAuthorityResolver
        ):  # noqa: E721
            raise PipelineRunValidationError(
                "media-preflight requires an exact installed CUDA authority resolver"
            )
        self._runtime_authority_resolver = runtime_authority_resolver
        self._runtime_measurement_port = runtime_measurement_port
        if type(episode_max_concurrency) is not int or episode_max_concurrency < 1:  # noqa: E721
            raise PipelineRunValidationError(
                "media-preflight episode_max_concurrency must be positive"
            )
        self._episode_max_concurrency = episode_max_concurrency

    @staticmethod
    def _job(context: PipelineStageContext) -> Job:
        if type(context) is not PipelineStageContext:  # noqa: E721
            raise PipelineRunValidationError(
                "media-preflight adapter requires an exact stage context"
            )
        if context.command.stage != "media_preflight":
            raise PipelineRunValidationError("media-preflight adapter received another stage")
        validate_run_id(context.run_id)
        return Job(context.run_id, context.request.profile)

    def _requests(
        self,
        context: PipelineStageContext,
        policy: LocalMediaPreflightPolicy,
        runtime: _RuntimeCudaAuthority | None,
    ) -> tuple[PersistedPreparedSources, tuple[PrepareTimedMediaEvidenceRequest, ...]] | None:
        if context.recompute_request is not None and type(  # noqa: E721
            context.recompute_request
        ) is not MediaPreflightRecomputeRequest:
            raise PipelineRunValidationError(
                "media-preflight stage does not accept a VLM recompute request"
            )
        materialization_limits = self._validate_execution_profile(context, policy)
        job = self._job(context)

        source_outcome = self._store.read_outcome(
            job,
            source_prep_kernel_idempotency_key(context.run_id),
        )
        if source_outcome is None or source_outcome.state in ("pending", "running"):
            return None
        if source_outcome.state in ("denied", "failed"):
            return None
        if source_outcome.state != "succeeded":
            raise PipelineRunValidationError("source preparation outcome is unsupported")
        source_bundle = read_persisted_prepared_sources_bundle(
            self._store,
            job=job,
            outcome=source_outcome,
            artifact_scope=ArtifactScope("pipeline", "job", context.run_id),
            artifact_revision=_ARTIFACT_REVISION,
        )
        validate_installed_source_sampling(source_bundle)
        require_committed_source_operation(source_bundle, "render_source")
        require_committed_source_operation(source_bundle, "semantic_analysis")
        vlm_policy = context.execution_profile.to_doubao_policy()
        contextual = requires_window_context_pack(vlm_policy)
        try:
            committed_context = (
                find_committed_window_context_packs(
                    cast(ContextPrepareStore, self._store),
                    job=job,
                    artifact_scope=ArtifactScope("pipeline", "job", context.run_id),
                    artifact_revision=_ARTIFACT_REVISION,
                    source_bundle=source_bundle,
                )
                if contextual
                else None
            )
        except (RuntimeStoreError, TypeError, ValueError) as error:
            raise PipelineRunValidationError(
                "media-preflight requires the committed contextual VLM PackSet"
            ) from error
        if contextual and committed_context is None:
            raise PipelineRunValidationError(
                "media-preflight requires the committed contextual VLM PackSet"
            )
        context_packs = None if committed_context is None else committed_context.packs
        vlm_batch_key = media_preflight_vlm_batch_kernel_idempotency_key(
            run_id=context.run_id,
            source_bundle=source_bundle,
            policy=vlm_policy,
            execution_profile_hash=context.execution_profile_hash,
            context_packs=context_packs,
        )
        try:
            vlm_semantic_pack_set = self._store.read_committed_vlm_semantic_pack_set_reference(
                job,
                vlm_batch_key,
            )
        except SemanticInputUnavailableError as error:
            raise PipelineRunValidationError(
                "media-preflight requires one exact committed VLM SemanticPackSet"
            ) from error
        source_reference = source_bundle.artifact_reference
        semantic_inputs_request = CommittedSemanticInputsRequest(
            job=job,
            source_manifest=CommittedArtifactMemberReference(
                receipt_id=source_bundle.receipt_id,
                artifact_set_id=source_bundle.artifact_set_id,
                member_ordinal=0,
                scope=source_reference.scope,
                artifact_type=source_reference.artifact_type,
                logical_id=source_reference.logical_id,
                revision=source_reference.revision,
                content_hash=source_reference.content_hash,
            ),
            vlm_semantic_pack_set=vlm_semantic_pack_set,
        )
        committed = self._store.read_committed_semantic_inputs(semantic_inputs_request)
        inputs_by_window = {
            item.source_window.window_manifest_sha256: item for item in committed.inputs
        }
        requests: list[PrepareTimedMediaEvidenceRequest] = []
        for episode_index, episode in enumerate(source_bundle.prepared.episodes):
            try:
                semantic_input = inputs_by_window[episode.manifest.canonical_hash]
            except KeyError as error:
                raise PipelineRunValidationError(
                    "exact committed semantic inputs lost a source window"
                ) from error
            persisted = semantic_input.semantic_pack
            child = persisted.source_child
            if (
                child.episode_index != episode_index
                or child.source_manifest_sha256 != source_bundle.artifact_reference.content_hash
                or child.source_provenance_sha256 != source_bundle.canonical_hash
                or persisted.semantic_pack.window_manifest_sha256 != episode.manifest.canonical_hash
                or child.window_manifest_set_sha256 != episode.manifest_set.canonical_hash
            ):
                raise PipelineRunValidationError(
                    "VLM evidence does not bind the exact committed source episode"
                )
            adaptive = policy.adaptive_window_policy(episode.manifest.source_time_base)
            if runtime is None:
                key = media_preflight_kernel_idempotency_key(
                    run_id=context.run_id,
                    episode_index=episode_index,
                    source_bundle=source_bundle,
                    semantic_pack=persisted,
                    producer_policy_sha256=policy.canonical_hash,
                    adaptive_policy_sha256=adaptive.canonical_hash,
                    materialization_policy_sha256=materialization_limits.policy_sha256,
                )
                producer_policy_sha256 = policy.canonical_hash
            else:
                key = runtime_media_preflight_kernel_idempotency_key(
                    run_id=context.run_id,
                    episode_index=episode_index,
                    source_bundle=source_bundle,
                    semantic_pack=persisted,
                    runtime_policy=runtime.policy,
                    adaptive_policy_sha256=adaptive.canonical_hash,
                    materialization_policy_sha256=materialization_limits.policy_sha256,
                )
                producer_policy_sha256 = runtime.policy.canonical_hash
            requests.append(
                PrepareTimedMediaEvidenceRequest(
                    job=job,
                    idempotency_key=key,
                    semantic_inputs_request=semantic_inputs_request,
                    episode_index=episode_index,
                    artifact_scope=canonical_recipe_scope(job),
                    artifact_revision=_ARTIFACT_REVISION,
                    source_blob=episode.proxy_blob,
                    source_manifest_reference=source_bundle.artifact_reference,
                    source_manifest_receipt_id=source_bundle.receipt_id,
                    source_manifest_artifact_set_id=source_bundle.artifact_set_id,
                    source_manifest_command_slot_id=source_bundle.command_slot_id,
                    source_provenance_sha256=source_bundle.canonical_hash,
                    window_manifest=episode.manifest,
                    semantic_pack=persisted.semantic_pack,
                    frame_pts_index=episode.manifest.frame_pts_index_set,
                    audio_sample_boundaries=episode.media_probe.audio_sample_boundaries,
                    frame_detector_sha256=episode.media_probe.frame_detector_sha256,
                    audio_detector_sha256=episode.media_probe.audio_detector_sha256,
                    adaptive_policy=adaptive,
                    producer_policy_sha256=producer_policy_sha256,
                    materialization_limits=materialization_limits,
                )
            )
        if not requests:
            raise PipelineRunValidationError(
                "media-preflight requires at least one committed episode"
            )
        recompute = context.recompute_request
        if recompute is not None:
            selected_index = recompute.selected_episode_index
            if selected_index >= len(requests):
                raise PipelineRunValidationError(
                    "selected media episode is outside the committed source census"
                )
            return source_bundle, (requests[selected_index],)
        return source_bundle, tuple(requests)

    def _validate_execution_profile(
        self,
        context: PipelineStageContext,
        policy: LocalMediaPreflightPolicy,
    ) -> MaterializationLimits:
        """Validate only frozen local configuration; this must not read the Store."""
        materialization_limits = context.execution_profile.to_materialization_limits()
        media_evidence_read_limits(context.execution_profile)
        resolver = self._authority_profile_resolver
        validate_installed_media_policy(resolver.resource, policy)
        validate_installed_vlm_policy(
            resolver.resource.narrative,
            context.execution_profile.to_doubao_policy(),
            context.execution_profile.to_generation_retry_policy(),
        )
        if materialization_limits.timed_speech_max_request_bytes != (
            resolver.resource.local_run.native_timed_speech.max_request_bytes
        ):
            raise PipelineRunValidationError(
                "persisted timed speech request limit differs from installed service"
            )
        return materialization_limits

    async def execute(self, context: PipelineStageContext) -> PipelineStageResult:
        policy = context.execution_profile.to_media_preflight_policy()
        await asyncio.to_thread(self._validate_execution_profile, context, policy)
        authority = await self._runtime_cuda_authority(context, policy)
        if isinstance(authority, PipelineStageResult):
            return authority
        prepared = await asyncio.to_thread(self._requests, context, policy, authority)
        if prepared is None:
            return PipelineStageResult(context.command.command_id, "indeterminate")
        result = await self._execute_batch(context, *prepared, policy, authority)
        return self._project(context, result.outcome)

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult | None:
        policy = context.execution_profile.to_media_preflight_policy()
        await asyncio.to_thread(self._validate_execution_profile, context, policy)
        authority = await self._runtime_cuda_authority(context, policy)
        if isinstance(authority, PipelineStageResult):
            return authority
        prepared = await asyncio.to_thread(self._requests, context, policy, authority)
        if prepared is None:
            return None
        result = await self._execute_batch(context, *prepared, policy, authority)
        projected = self._project(context, result.outcome)
        return None if projected.outcome == "indeterminate" else projected

    async def _runtime_cuda_authority(
        self,
        context: PipelineStageContext,
        policy: LocalMediaPreflightPolicy,
    ) -> _RuntimeCudaAuthority | PipelineStageResult | None:
        """Bind fresh local measurement before any detector/materialization claim.

        The v2 capability is intentionally an extra admission condition while
        the existing timed-speech profile Registry is still the request-level
        contract.  Missing accepted capability is actionable waiting, whereas
        a changed or malformed identity requires recalibration rather than an
        automatic fallback to the older anchor.
        """
        resolver = self._runtime_authority_resolver
        port = self._runtime_measurement_port
        if resolver is None or port is None:
            # Legacy CPU-only composition stays structurally separate. Normal
            # configured runtime always injects the CUDA authority resolver.
            return None
        try:
            identity = await asyncio.to_thread(port.read_identity)
            if identity.runtime_capability_id != PC_CUDA_RUNTIME_CAPABILITY_ID:
                # A stage composed with the CUDA authority is a CUDA run.  Do
                # not silently degrade it to the historical CPU chain merely
                # because a service endpoint points at another machine.  A
                # CPU run is assembled without this resolver/measurement port
                # and therefore retains its separate command/reader grammar.
                return PipelineStageResult(context.command.command_id, "recompute_needed")
            projection = await asyncio.to_thread(resolver.resolve, self._store, identity)
            if resolver.static_operation_policy_sha256 != policy.canonical_hash:
                raise PipelineRunValidationError(
                    "installed CUDA authority differs from frozen static media policy"
                )
            runtime_policy = project_pc_cuda_runtime_timed_speech_policy(policy, projection)
        except MediaEvidenceUnavailableError:
            return PipelineStageResult(context.command.command_id, "awaiting_calibration")
        except (
            CalibrationBindingError,
            RuntimeCalibrationIdentityMismatchError,
            StoreValidationError,
        ):
            return PipelineStageResult(context.command.command_id, "recompute_needed")
        except LocalMediaToolError as error:
            if error.code in (
                "RUNTIME_IDENTITY_INVALID",
                "RUNTIME_IDENTITY_RECOMPUTE_NEEDED",
            ):
                return PipelineStageResult(context.command.command_id, "recompute_needed")
            # A transient unavailable local service is not evidence that the
            # accepted calibration is gone; leave the command recoverable.
            return PipelineStageResult(context.command.command_id, "indeterminate")
        return _RuntimeCudaAuthority(identity, projection, runtime_policy)

    async def _execute_batch(
        self,
        context: PipelineStageContext,
        source_bundle: PersistedPreparedSources,
        requests: tuple[PrepareTimedMediaEvidenceRequest, ...],
        policy: LocalMediaPreflightPolicy,
        runtime: _RuntimeCudaAuthority | None = None,
    ) -> (
        FinalizeTimedMediaEvidenceBatchResult
        | PrepareTimedMediaEvidenceResult
        | FinalizeRuntimeTimedMediaEvidenceBatchResult
        | PrepareRuntimeTimedMediaEvidenceResult
    ):
        if runtime is not None:
            return await self._execute_runtime_cuda_batch(
                context, source_bundle, requests, policy, runtime
            )
        resolver = self._authority_profile_resolver
        # Check accepted installation before claim-owned detector work. The
        # finalizer independently replays all committed children afterwards.
        await asyncio.to_thread(resolver.resolve, self._store)
        command = PrepareTimedMediaEvidenceCommand(
            self._store,
            _ClaimOwnedLocalProducer(
                self._port,
                policy,
                resolver.resource.local_run.native_timed_speech.native_port_identity_sha256,
            ),
            StoreAnchoredTimedSpeechProfileResolver(resolver.snapshot),
        )
        results = await _execute_independent_requests(
            requests,
            command.execute,
            max_concurrency=self._episode_max_concurrency,
        )
        children: list[TimedMediaEvidenceBatchChild] = []
        unresolved: PrepareTimedMediaEvidenceResult | None = None
        terminal: PrepareTimedMediaEvidenceResult | None = None
        for request, result in zip(requests, results, strict=True):
            outcome = result.outcome
            if outcome.state in ("pending", "running"):
                unresolved = unresolved or result
                continue
            if outcome.state not in ("succeeded", "denied", "failed"):
                raise PipelineRunValidationError(
                    "Kernel returned an unsupported media-preflight child outcome"
                )
            if outcome.receipt_id is None:
                raise PipelineRunValidationError("terminal media-preflight child lost its Receipt")
            if outcome.state in ("denied", "failed"):
                terminal = terminal or result
                continue
            if outcome.artifact_set_id is None:
                raise PipelineRunValidationError(
                    "succeeded media-preflight child lost its ArtifactSet"
                )
            children.append(TimedMediaEvidenceBatchChild(request, outcome))
        if unresolved is not None:
            return unresolved
        if terminal is not None:
            return terminal
        if context.recompute_request is not None and len(source_bundle.prepared.episodes) != 1:
            # A selected child is a durable inspection/recovery result. It is
            # not a complete-series aggregate and cannot cross the Stage gate.
            if len(children) != 1:
                raise PipelineRunValidationError(
                    "selected media recompute must settle exactly one episode"
                )
            return results[0]
        job = Job(context.run_id, context.request.profile)
        finalizer = FinalizeTimedMediaEvidenceBatchRequest(
            job,
            self._batch_idempotency_key(context, source_bundle, policy),
            canonical_recipe_scope(job),
            _ARTIFACT_REVISION,
            tuple(children),
        )
        command_batch = FinalizeTimedMediaEvidenceBatchCommand(
            self._store,
            authority_profile_resolver=resolver,
            limits=media_evidence_read_limits(context.execution_profile),
        )
        return await asyncio.to_thread(command_batch.execute, finalizer)

    async def _execute_runtime_cuda_batch(
        self,
        context: PipelineStageContext,
        source_bundle: PersistedPreparedSources,
        requests: tuple[PrepareTimedMediaEvidenceRequest, ...],
        policy: LocalMediaPreflightPolicy,
        runtime: _RuntimeCudaAuthority,
    ) -> FinalizeRuntimeTimedMediaEvidenceBatchResult | PrepareRuntimeTimedMediaEvidenceResult:
        resolver = self._runtime_authority_resolver
        if resolver is None:
            raise PipelineRunValidationError("runtime CUDA batch lost its installed authority")
        command = PrepareRuntimeTimedMediaEvidenceCommand(
            self._store,
            _ClaimOwnedRuntimeCudaProducer(self._port, policy, runtime.policy),
            resolver,
        )
        runtime_requests = tuple(
            PrepareRuntimeTimedMediaEvidenceRequest(base, runtime.measurement)
            for base in requests
        )
        results = await _execute_independent_requests(
            runtime_requests,
            command.execute,
            max_concurrency=self._episode_max_concurrency,
        )
        children: list[RuntimeTimedMediaEvidenceBatchChild] = []
        unresolved: PrepareRuntimeTimedMediaEvidenceResult | None = None
        terminal: PrepareRuntimeTimedMediaEvidenceResult | None = None
        for request, result in zip(runtime_requests, results, strict=True):
            outcome = result.outcome
            if outcome.state in ("pending", "running"):
                unresolved = unresolved or result
                continue
            if outcome.state not in ("succeeded", "denied", "failed"):
                raise PipelineRunValidationError(
                    "Kernel returned an unsupported CUDA media-preflight child outcome"
                )
            if outcome.receipt_id is None:
                raise PipelineRunValidationError("terminal CUDA child lost its Receipt")
            if outcome.state in ("denied", "failed"):
                terminal = terminal or result
                continue
            if outcome.artifact_set_id is None:
                raise PipelineRunValidationError("succeeded CUDA child lost its ArtifactSet")
            children.append(RuntimeTimedMediaEvidenceBatchChild(request, outcome))
        if unresolved is not None:
            return unresolved
        if terminal is not None:
            return terminal
        if context.recompute_request is not None and len(source_bundle.prepared.episodes) != 1:
            if len(children) != 1:
                raise PipelineRunValidationError(
                    "selected CUDA media recompute must settle exactly one episode"
                )
            return results[0]
        job = Job(context.run_id, context.request.profile)
        finalizer = FinalizeRuntimeTimedMediaEvidenceBatchRequest(
            job,
            self._runtime_batch_idempotency_key(context, source_bundle, runtime.policy),
            canonical_recipe_scope(job),
            _ARTIFACT_REVISION,
            tuple(children),
        )
        batch = FinalizeRuntimeTimedMediaEvidenceBatchCommand(
            self._store,
            resolver,
            media_evidence_read_limits(context.execution_profile),
        )
        return await asyncio.to_thread(batch.execute, finalizer)

    def _batch_idempotency_key(
        self,
        context: PipelineStageContext,
        source_bundle: PersistedPreparedSources,
        policy: LocalMediaPreflightPolicy,
    ) -> str:
        encoded = json.dumps(
            {
                "producer_policy_sha256": policy.canonical_hash,
                "materialization_policy_sha256": context.execution_profile.to_materialization_limits().policy_sha256,
                "evidence_read_limits": context.execution_profile.to_evidence_read_limits().to_mapping(),
                "run_id": context.run_id,
                "source_manifest_sha256": source_bundle.artifact_reference.content_hash,
                "source_provenance_sha256": source_bundle.canonical_hash,
                "strategy_version": MEDIA_PREFLIGHT_EPISODE_STRATEGY_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "media-preflight-batch:" + hashlib.sha256(encoded).hexdigest()

    def _runtime_batch_idempotency_key(
        self,
        context: PipelineStageContext,
        source_bundle: PersistedPreparedSources,
        policy: PcCudaRuntimeTimedSpeechPolicy,
    ) -> str:
        encoded = json.dumps(
            {
                "evidence_read_limits": context.execution_profile.to_evidence_read_limits().to_mapping(),
                "materialization_policy_sha256": context.execution_profile.to_materialization_limits().policy_sha256,
                "runtime_policy_sha256": policy.canonical_hash,
                "source_manifest_sha256": source_bundle.artifact_reference.content_hash,
                "source_provenance_sha256": source_bundle.canonical_hash,
                "strategy_version": RUNTIME_MEDIA_PREFLIGHT_EPISODE_STRATEGY_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "runtime-media-finalize:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _project(
        context: PipelineStageContext,
        outcome: CommandOutcome,
    ) -> PipelineStageResult:
        if outcome.state in ("pending", "running"):
            return PipelineStageResult(context.command.command_id, "indeterminate")
        if outcome.state not in ("succeeded", "denied", "failed"):
            raise PipelineRunValidationError(
                "Kernel returned an unsupported media-preflight outcome"
            )
        if outcome.receipt_id is None:
            raise PipelineRunValidationError("terminal media-preflight outcome lost its Receipt")
        return PipelineStageResult(
            context.command.command_id,
            outcome.state,
            outcome.receipt_id,
        )


__all__ = (
    "MEDIA_PREFLIGHT_EPISODE_MAX_CONCURRENCY",
    "MEDIA_PREFLIGHT_EPISODE_STRATEGY_VERSION",
    "MediaPreflightPipelineStage",
    "MediaPreflightPipelineStore",
    "media_preflight_kernel_idempotency_key",
    "media_evidence_read_limits",
)
