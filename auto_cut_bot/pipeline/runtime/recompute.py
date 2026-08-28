"""Admission helper for complete VLM re-runs over persisted SourcePrep evidence."""

from __future__ import annotations

import asyncio
from typing import Protocol

from autocut_kernel.store import (
    ArtifactScope,
    CommandOutcome,
    CommandSuccess,
    Job,
    SourceReuseBinding,
)

from auto_cut_bot.pipeline.source_prep import (
    BindWholeSeriesSourcesCommand,
    BindWholeSeriesSourcesRequest,
    SourcePrepStore,
    read_persisted_prepared_sources_bundle,
)

from .errors import PipelineRunValidationError
from .models import PipelineRunSnapshot, validate_run_id
from .source_prep_stage import source_prep_kernel_idempotency_key


class VlmRecomputeSourceStore(SourcePrepStore, Protocol):
    """Kernel capability needed to create a target-Job source binding."""

    def commit_source_reuse_success(
        self,
        success: CommandSuccess,
        *,
        binding: SourceReuseBinding,
    ) -> CommandOutcome: ...


class FullStageVlmRecomputeBinderPort(Protocol):
    async def bind(self, *, base: PipelineRunSnapshot, target_run_id: str) -> None: ...


class FullStageVlmRecomputeBinder:
    """Bind exact prepared media before a new pipeline Run can be scheduled.

    The public recompute service invokes this before inserting/enqueuing the
    target control-plane Run.  Therefore no worker can ever see a new target
    Run whose ``source_prep`` command might fall back to the old host path.
    A crash after a successful binding but before Run insertion leaves only an
    unreachable immutable Kernel artifact; it cannot produce or publish work.
    """

    def __init__(self, store: VlmRecomputeSourceStore) -> None:
        self._store = store
        self._command = BindWholeSeriesSourcesCommand(store)

    async def bind(
        self,
        *,
        base: PipelineRunSnapshot,
        target_run_id: str,
    ) -> None:
        await asyncio.to_thread(self._bind_sync, base=base, target_run_id=target_run_id)

    def _bind_sync(self, *, base: PipelineRunSnapshot, target_run_id: str) -> None:
        validate_run_id(target_run_id)
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
            raise PipelineRunValidationError("target SourcePrep binding changed frozen source evidence")


__all__ = (
    "FullStageVlmRecomputeBinder",
    "FullStageVlmRecomputeBinderPort",
    "VlmRecomputeSourceStore",
)
