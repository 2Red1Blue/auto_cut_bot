"""Admission helper for complete VLM re-runs over persisted SourcePrep evidence."""

from __future__ import annotations

import asyncio
from typing import Protocol

from autocut_kernel.context_pack import (
    ContextSelectionPolicy,
    video_only_window_context_pack,
)
from autocut_kernel.store import (
    ArtifactScope,
    CommandOutcome,
    CommandSuccess,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedSemanticInputsRequest,
    Job,
    PersistedVlmSemanticPack,
    SourceReuseBinding,
)

from auto_cut_bot.pipeline.context_prepare import (
    ContextPrepareStore,
    find_committed_window_context_packs,
)
from auto_cut_bot.pipeline.source_prep import (
    BindWholeSeriesSourcesCommand,
    BindWholeSeriesSourcesRequest,
    SourcePrepStore,
    read_persisted_prepared_sources_bundle,
)

from .errors import PipelineRunValidationError
from .media_preflight_stage import media_preflight_vlm_batch_kernel_idempotency_key
from .models import (
    MediaPreflightRecomputeRequest,
    PipelineRunSnapshot,
    VlmFullStageRecomputeRequest,
    validate_run_id,
)
from .source_prep_stage import source_prep_kernel_idempotency_key
from .vlm_stage import requires_window_context_pack


class VlmRecomputeSourceStore(SourcePrepStore, ContextPrepareStore, Protocol):
    """Kernel capability needed to create a target-Job source binding."""

    def commit_source_reuse_success(
        self,
        success: CommandSuccess,
        *,
        binding: SourceReuseBinding,
    ) -> CommandOutcome: ...


class FullStageVlmRecomputeBinderPort(Protocol):
    async def bind(
        self,
        *,
        base: PipelineRunSnapshot,
        target_run_id: str,
        request: VlmFullStageRecomputeRequest,
    ) -> None: ...


class MediaPreflightRecomputeBinderPort(Protocol):
    """Bind exact source/semantic/media predecessors for a media successor Run."""

    async def bind(
        self,
        *,
        base: PipelineRunSnapshot,
        target_run_id: str,
        request: MediaPreflightRecomputeRequest,
    ) -> None: ...


class MediaPreflightRecomputeStore(SourcePrepStore, ContextPrepareStore, Protocol):
    """Read-only exact predecessor capabilities needed before activation."""

    def read_committed_vlm_semantic_pack_set_reference(
        self,
        job: Job,
        idempotency_key: str,
    ) -> CommittedArtifactMemberReference: ...

    def read_committed_semantic_inputs(
        self,
        request: CommittedSemanticInputsRequest,
    ) -> CommittedSemanticInputs: ...


class FullStageVlmRecomputeBinder:
    """Bind exact prepared media before a new pipeline Run can be scheduled.

    The public recompute service invokes this before inserting/enqueuing the
    target control-plane Run.  Therefore no worker can ever see a new target
    Run whose ``source_prep`` command might fall back to the old host path.
    A crash after a successful binding but before Run insertion leaves only an
    unreachable immutable Kernel artifact; it cannot produce or publish work.
    """

    def __init__(
        self,
        store: VlmRecomputeSourceStore,
        *,
        context_policy: ContextSelectionPolicy | None = None,
    ) -> None:
        self._store = store
        self._command = BindWholeSeriesSourcesCommand(store)
        self._context_policy = context_policy or ContextSelectionPolicy()

    async def bind(
        self,
        *,
        base: PipelineRunSnapshot,
        target_run_id: str,
        request: VlmFullStageRecomputeRequest,
    ) -> None:
        await asyncio.to_thread(
            self._bind_sync,
            base=base,
            target_run_id=target_run_id,
            request=request,
        )

    def _bind_sync(
        self,
        *,
        base: PipelineRunSnapshot,
        target_run_id: str,
        request: VlmFullStageRecomputeRequest,
    ) -> None:
        validate_run_id(target_run_id)
        if type(request) is not VlmFullStageRecomputeRequest:  # noqa: E721
            raise PipelineRunValidationError("recompute binding requires a canonical request")
        source_command = next(
            (command for command in base.commands if command.stage == "source_prep"), None
        )
        if source_command is None or source_command.status != "succeeded":
            raise PipelineRunValidationError("base run lacks a succeeded source_prep command")
        origin_job = Job(base.run_id, base.request.profile)
        origin_outcome = self._store.read_outcome(
            origin_job, source_prep_kernel_idempotency_key(base.run_id)
        )
        if origin_outcome is None or origin_outcome.state != "succeeded":
            raise PipelineRunValidationError("base source-prep Kernel Receipt is unavailable")
        origin = read_persisted_prepared_sources_bundle(
            self._store,
            job=origin_job,
            outcome=origin_outcome,
            artifact_scope=ArtifactScope("pipeline", "job", base.run_id),
            artifact_revision=1,
        )
        if (
            base.execution_profile.is_semantic_story
            and request.completion_scope == "selected_only"
            and len(origin.prepared.episodes) != 1
        ):
            raise PipelineRunValidationError(
                "semantic-story selected recompute requires a one-episode source census"
            )
        selected_index = request.selected_episode_index
        if selected_index is not None and selected_index >= len(origin.prepared.episodes):
            raise PipelineRunValidationError(
                "selected episode is outside the committed source census"
            )
        committed_context = find_committed_window_context_packs(
            self._store,
            job=origin_job,
            artifact_scope=ArtifactScope("pipeline", "job", base.run_id),
            artifact_revision=1,
            source_bundle=origin,
        )
        if committed_context is None:
            raise PipelineRunValidationError("base run lacks a committed WindowContextPackSet")
        expected_context = video_only_window_context_pack(
            self._context_policy,
            "EXTERNAL_CONTEXT_NOT_CONFIGURED",
        )
        if any(pack != expected_context for pack in committed_context.packs):
            raise PipelineRunValidationError(
                "recompute v1 requires the exact configured video-only context pack"
            )
        target_job = Job(target_run_id, base.request.profile)
        result = self._command.execute(
            BindWholeSeriesSourcesRequest(
                job=target_job,
                # Reuse the ordinary source-prep key deliberately: the target
                # stage can project this exact binding and no code needs a
                # cross-Job Blob read escape hatch.
                idempotency_key=source_prep_kernel_idempotency_key(target_run_id),
                artifact_scope=ArtifactScope("pipeline", "job", target_run_id),
                artifact_revision=1,
                origin_job=origin_job,
                origin_outcome=origin_outcome,
                target_policy=origin.prepared.census.policy,
            )
        )
        if result.outcome.state != "succeeded" or result.sources is None:
            raise PipelineRunValidationError("target SourcePrep binding was not committed")
        if result.sources.prepared != origin.prepared:
            raise PipelineRunValidationError(
                "target SourcePrep binding changed frozen source evidence"
            )


class MediaPreflightRecomputeBinder:
    """Admit a target that reads exact Source/VLM evidence from its base Run.

    Media recompute deliberately does not copy or relabel VLM Artifacts.  The
    target request persists ``base_run_id`` and the Kernel child declares that
    Job as its immutable input owner.  This binder proves the complete Source
    and VLM aggregate are readable before the target command is activated.
    """

    def __init__(self, store: MediaPreflightRecomputeStore) -> None:
        self._store = store

    async def bind(
        self,
        *,
        base: PipelineRunSnapshot,
        target_run_id: str,
        request: MediaPreflightRecomputeRequest,
    ) -> None:
        await asyncio.to_thread(
            self._bind_sync,
            base=base,
            target_run_id=target_run_id,
            request=request,
        )

    def _bind_sync(
        self,
        *,
        base: PipelineRunSnapshot,
        target_run_id: str,
        request: MediaPreflightRecomputeRequest,
    ) -> None:
        validate_run_id(target_run_id)
        if type(request) is not MediaPreflightRecomputeRequest:  # noqa: E721
            raise PipelineRunValidationError(
                "media recompute binding requires a canonical request"
            )
        if request.base_run_id != base.run_id:
            raise PipelineRunValidationError("media recompute base identity changed")
        source_command = next(
            (command for command in base.commands if command.stage == "source_prep"), None
        )
        if source_command is None or source_command.status != "succeeded":
            raise PipelineRunValidationError(
                "base run lacks a succeeded source_prep command"
            )
        origin_job = Job(base.run_id, base.request.profile)
        source_outcome = self._store.read_outcome(
            origin_job,
            source_prep_kernel_idempotency_key(base.run_id),
        )
        if source_outcome is None or source_outcome.state != "succeeded":
            raise PipelineRunValidationError(
                "base source-prep Kernel Receipt is unavailable"
            )
        source_bundle = read_persisted_prepared_sources_bundle(
            self._store,
            job=origin_job,
            outcome=source_outcome,
            artifact_scope=ArtifactScope("pipeline", "job", base.run_id),
            artifact_revision=1,
        )
        selected_index = request.selected_episode_index
        if not 0 <= selected_index < len(source_bundle.prepared.episodes):
            raise PipelineRunValidationError(
                "selected media episode is outside the committed source census"
            )
        vlm_policy = base.execution_profile.to_doubao_policy()
        context_packs = None
        if requires_window_context_pack(vlm_policy):
            committed_context = find_committed_window_context_packs(
                self._store,
                job=origin_job,
                artifact_scope=ArtifactScope("pipeline", "job", base.run_id),
                artifact_revision=1,
                source_bundle=source_bundle,
            )
            if committed_context is None:
                raise PipelineRunValidationError(
                    "base run lacks a committed WindowContextPackSet"
                )
            context_packs = committed_context.packs
        batch_key = media_preflight_vlm_batch_kernel_idempotency_key(
            run_id=base.run_id,
            source_bundle=source_bundle,
            policy=vlm_policy,
            execution_profile_hash=base.execution_profile_hash,
            context_packs=context_packs,
        )
        aggregate_ref = self._store.read_committed_vlm_semantic_pack_set_reference(
            origin_job,
            batch_key,
        )
        source_ref = source_bundle.artifact_reference
        semantic_request = CommittedSemanticInputsRequest(
            origin_job,
            CommittedArtifactMemberReference(
                source_bundle.receipt_id,
                source_bundle.artifact_set_id,
                0,
                source_ref.scope,
                source_ref.artifact_type,
                source_ref.logical_id,
                source_ref.revision,
                source_ref.content_hash,
            ),
            aggregate_ref,
        )
        semantic = self._store.read_committed_semantic_inputs(semantic_request)
        episodes = source_bundle.prepared.episodes
        if (
            semantic.source_manifest.reference != source_ref
            or semantic.source_manifest.source_job != origin_job
            or semantic.vlm_semantic_pack_set != aggregate_ref
            or len(semantic.inputs) != len(episodes)
        ):
            raise PipelineRunValidationError(
                "base Source/VLM predecessor closure is incomplete"
            )
        for episode_index, (semantic_input, episode) in enumerate(
            zip(semantic.inputs, episodes, strict=True)
        ):
            persisted = semantic_input.semantic_pack
            child = persisted.source_child
            if (
                type(persisted) is not PersistedVlmSemanticPack  # noqa: E721
                or semantic_input.source_window.episode_index != episode_index
                or semantic_input.source_window.window_manifest_sha256
                != episode.manifest.canonical_hash
                or semantic_input.source_window.proxy_blob != episode.proxy_blob
                or child.source_job != origin_job
                or child.episode_index != episode_index
                or child.source_manifest_sha256 != source_ref.content_hash
                or child.source_provenance_sha256 != source_bundle.canonical_hash
                or child.window_manifest_sha256 != episode.manifest.canonical_hash
                or child.window_manifest_set_sha256 != episode.manifest_set.canonical_hash
            ):
                raise PipelineRunValidationError(
                    "base Source/VLM predecessor closure is incomplete"
                )


__all__ = (
    "FullStageVlmRecomputeBinder",
    "FullStageVlmRecomputeBinderPort",
    "MediaPreflightRecomputeBinderPort",
    "MediaPreflightRecomputeBinder",
    "MediaPreflightRecomputeStore",
    "VlmRecomputeSourceStore",
)
