"""Real HTTP Pipeline adapter for committed local timed-media evidence."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Protocol

from autocut_kernel.pipeline import (
    FinalizeTimedMediaEvidenceBatchCommand,
    FinalizeTimedMediaEvidenceBatchRequest,
    FinalizeTimedMediaEvidenceBatchResult,
    PrepareTimedMediaEvidenceCommand,
    PrepareTimedMediaEvidenceRequest,
    PrepareTimedMediaEvidenceResult,
    ProducedTimedMediaEvidence,
    TimedMediaEvidenceBatchChild,
    TimedMediaEvidenceProducerError,
    TimedMediaEvidenceStore,
)
from autocut_kernel.store import (
    ArtifactScope,
    CommandOutcome,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedSemanticInputsRequest,
    Job,
    PersistedVlmSemanticPack,
    SemanticInputUnavailableError,
)
from autocut_kernel.store.models import (
    VerifiedMaterializedBlob,
    canonical_recipe_scope,
)

from auto_cut_bot.pipeline.media_preflight import (
    LocalMediaPreflightError,
    LocalMediaPreflightPolicy,
    LocalMediaPreflightPort,
    LocalMediaPreflightRequest,
    LocalMediaToolError,
)
from auto_cut_bot.pipeline.source_prep import (
    PersistedPreparedSources,
    SourcePrepStore,
    read_persisted_prepared_sources_bundle,
)

from .errors import PipelineRunValidationError
from .models import PipelineStageContext, PipelineStageResult, validate_run_id
from .source_prep_stage import (
    require_committed_source_operation,
    source_prep_kernel_idempotency_key,
)
from .vlm_stage import vlm_batch_kernel_idempotency_key

MEDIA_PREFLIGHT_EPISODE_STRATEGY_VERSION = "all-episodes-local-evidence-sequential-v1"
_ARTIFACT_REVISION = 1


class MediaPreflightPipelineStore(SourcePrepStore, TimedMediaEvidenceStore, Protocol):
    def read_committed_vlm_semantic_pack_set_reference(
        self,
        job: Job,
        idempotency_key: str,
    ) -> CommittedArtifactMemberReference: ...

    def read_committed_semantic_inputs(
        self,
        request: CommittedSemanticInputsRequest,
    ) -> CommittedSemanticInputs: ...


class _ClaimOwnedLocalProducer:
    """Adapt a Kernel-owned verified-file lease to the local detector port."""

    def __init__(
        self,
        port: LocalMediaPreflightPort,
        policy: LocalMediaPreflightPolicy,
    ) -> None:
        self._port = port
        self._policy = policy

    def prepare(
        self,
        request: PrepareTimedMediaEvidenceRequest,
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


class MediaPreflightPipelineStage:
    """Prepare every committed episode, then commit one aggregate stage Receipt."""

    def __init__(
        self,
        store: MediaPreflightPipelineStore,
        port: LocalMediaPreflightPort,
    ) -> None:
        self._store = store
        self._port = port
        self._finalizer = FinalizeTimedMediaEvidenceBatchCommand(store)

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
    ) -> tuple[PersistedPreparedSources, tuple[PrepareTimedMediaEvidenceRequest, ...]] | None:
        materialization_limits = context.execution_profile.to_materialization_limits()
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
        require_committed_source_operation(source_bundle, "render_source")
        require_committed_source_operation(source_bundle, "semantic_analysis")
        vlm_batch_key = vlm_batch_kernel_idempotency_key(
            run_id=context.run_id,
            source_bundle=source_bundle,
            execution_profile_hash=context.execution_profile_hash,
        )
        try:
            vlm_semantic_pack_set = (
                self._store.read_committed_vlm_semantic_pack_set_reference(
                    job,
                    vlm_batch_key,
                )
            )
        except SemanticInputUnavailableError as error:
            raise PipelineRunValidationError(
                "media-preflight requires one exact committed VLM SemanticPackSet"
            ) from error
        source_reference = source_bundle.artifact_reference
        committed = self._store.read_committed_semantic_inputs(
            CommittedSemanticInputsRequest(
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
        )
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
            key = media_preflight_kernel_idempotency_key(
                run_id=context.run_id,
                episode_index=episode_index,
                source_bundle=source_bundle,
                semantic_pack=persisted,
                producer_policy_sha256=policy.canonical_hash,
                adaptive_policy_sha256=adaptive.canonical_hash,
                materialization_policy_sha256=materialization_limits.policy_sha256,
            )
            requests.append(
                PrepareTimedMediaEvidenceRequest(
                    job=job,
                    idempotency_key=key,
                    episode_index=episode_index,
                    artifact_scope=canonical_recipe_scope(job),
                    artifact_revision=_ARTIFACT_REVISION,
                    source_blob=episode.proxy_blob,
                    source_manifest_sha256=source_bundle.artifact_reference.content_hash,
                    source_provenance_sha256=source_bundle.canonical_hash,
                    window_manifest=episode.manifest,
                    semantic_pack=persisted.semantic_pack,
                    frame_pts_index=episode.manifest.frame_pts_index_set,
                    audio_sample_boundaries=episode.media_probe.audio_sample_boundaries,
                    frame_detector_sha256=episode.media_probe.frame_detector_sha256,
                    audio_detector_sha256=episode.media_probe.audio_detector_sha256,
                    adaptive_policy=adaptive,
                    producer_policy_sha256=policy.canonical_hash,
                    materialization_limits=materialization_limits,
                )
            )
        if not requests:
            raise PipelineRunValidationError(
                "media-preflight requires at least one committed episode"
            )
        return source_bundle, tuple(requests)

    async def execute(self, context: PipelineStageContext) -> PipelineStageResult:
        policy = context.execution_profile.to_media_preflight_policy()
        prepared = await asyncio.to_thread(self._requests, context, policy)
        if prepared is None:
            return PipelineStageResult(context.command.command_id, "indeterminate")
        result = await self._execute_batch(context, *prepared, policy)
        return self._project(context, result.outcome)

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult | None:
        policy = context.execution_profile.to_media_preflight_policy()
        prepared = await asyncio.to_thread(self._requests, context, policy)
        if prepared is None:
            return None
        result = await self._execute_batch(context, *prepared, policy)
        projected = self._project(context, result.outcome)
        return None if projected.outcome == "indeterminate" else projected

    async def _execute_batch(
        self,
        context: PipelineStageContext,
        source_bundle: PersistedPreparedSources,
        requests: tuple[PrepareTimedMediaEvidenceRequest, ...],
        policy: LocalMediaPreflightPolicy,
    ) -> FinalizeTimedMediaEvidenceBatchResult | PrepareTimedMediaEvidenceResult:
        command = PrepareTimedMediaEvidenceCommand(
            self._store,
            _ClaimOwnedLocalProducer(self._port, policy),
        )
        children: list[TimedMediaEvidenceBatchChild] = []
        for request in requests:
            result = await asyncio.to_thread(command.execute, request)
            outcome = result.outcome
            if outcome.state in ("pending", "running"):
                return result
            if outcome.state not in ("succeeded", "denied", "failed"):
                raise PipelineRunValidationError(
                    "Kernel returned an unsupported media-preflight child outcome"
                )
            if outcome.receipt_id is None:
                raise PipelineRunValidationError("terminal media-preflight child lost its Receipt")
            if outcome.state in ("denied", "failed"):
                return result
            if outcome.artifact_set_id is None:
                raise PipelineRunValidationError(
                    "succeeded media-preflight child lost its ArtifactSet"
                )
            children.append(
                TimedMediaEvidenceBatchChild(
                    request.episode_index,
                    request.idempotency_key,
                    outcome.receipt_id,
                    outcome.artifact_set_id,
                )
            )
        job = Job(context.run_id, context.request.profile)
        finalizer = FinalizeTimedMediaEvidenceBatchRequest(
            job,
            self._batch_idempotency_key(context, source_bundle, policy),
            canonical_recipe_scope(job),
            _ARTIFACT_REVISION,
            source_bundle.artifact_reference.content_hash,
            source_bundle.canonical_hash,
            tuple(children),
        )
        return await asyncio.to_thread(self._finalizer.execute, finalizer)

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
                "run_id": context.run_id,
                "source_manifest_sha256": source_bundle.artifact_reference.content_hash,
                "source_provenance_sha256": source_bundle.canonical_hash,
                "strategy_version": MEDIA_PREFLIGHT_EPISODE_STRATEGY_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "media-preflight-batch:" + hashlib.sha256(encoded).hexdigest()

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
    "MEDIA_PREFLIGHT_EPISODE_STRATEGY_VERSION",
    "MediaPreflightPipelineStage",
    "MediaPreflightPipelineStore",
    "media_preflight_kernel_idempotency_key",
)
