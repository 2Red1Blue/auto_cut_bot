"""Runtime adapter for the committed external-context projection command."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Protocol

from autocut_kernel.context_pack import ContextSelectionPolicy, OwnerEpisodeMapSet
from autocut_kernel.store import ArtifactScope, CommandOutcome, Job

from auto_cut_bot.pipeline.context_prepare import (
    ContextPrepareStore,
    ExternalNarrativeApiClient,
    PrepareWindowContextCommand,
    PrepareWindowContextRequest,
)
from auto_cut_bot.pipeline.source_prep import (
    PersistedPreparedSources,
    SourcePrepStore,
    read_persisted_prepared_sources_bundle,
)

from .errors import PipelineRunValidationError
from .models import PipelineStageContext, PipelineStageResult, validate_run_id
from .source_prep_stage import source_prep_kernel_idempotency_key

_CONTEXT_PREP_IDEMPOTENCY_VERSION = "context-prepare-kernel-v1"
_ARTIFACT_REVISION = 1


class ContextPreparePipelineStore(ContextPrepareStore, SourcePrepStore, Protocol):
    """Exact Store capabilities needed by the context stage."""


def context_prepare_kernel_idempotency_key(
    *, run_id: str, source_bundle: PersistedPreparedSources, owner_maps: OwnerEpisodeMapSet,
    policy: ContextSelectionPolicy, execution_profile_hash: str,
) -> str:
    validate_run_id(run_id)
    payload = {
        "execution_profile_sha256": execution_profile_hash,
        "owner_maps_sha256": owner_maps.canonical_hash,
        "policy_sha256": policy.canonical_hash,
        "run_id": run_id,
        "source_provenance_sha256": source_bundle.canonical_hash,
        "version": _CONTEXT_PREP_IDEMPOTENCY_VERSION,
    }
    return _CONTEXT_PREP_IDEMPOTENCY_VERSION + ":" + hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


class ContextPreparePipelineStage:
    def __init__(
        self,
        store: ContextPreparePipelineStore,
        client: ExternalNarrativeApiClient,
        owner_maps: OwnerEpisodeMapSet,
        *,
        selection_policy: ContextSelectionPolicy | None = None,
        command: PrepareWindowContextCommand | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._owner_maps = owner_maps
        self._policy = selection_policy or ContextSelectionPolicy()
        self._command = command or PrepareWindowContextCommand(store, client)

    @staticmethod
    def _job(context: PipelineStageContext) -> Job:
        if type(context) is not PipelineStageContext or context.command.stage != "context_prepare":  # noqa: E721
            raise PipelineRunValidationError("context prepare adapter received another stage")
        validate_run_id(context.run_id)
        return Job(context.run_id, context.request.profile)

    def _request(self, context: PipelineStageContext) -> PrepareWindowContextRequest | None:
        job = self._job(context)
        source_outcome = self._store.read_outcome(job, source_prep_kernel_idempotency_key(context.run_id))
        if source_outcome is None or source_outcome.state in ("pending", "running", "denied", "failed"):
            return None
        source_bundle = read_persisted_prepared_sources_bundle(
            self._store,
            job=job,
            outcome=source_outcome,
            artifact_scope=ArtifactScope("pipeline", "job", context.run_id),
            artifact_revision=_ARTIFACT_REVISION,
        )
        return PrepareWindowContextRequest(
            job=job,
            idempotency_key=context_prepare_kernel_idempotency_key(
                run_id=context.run_id,
                source_bundle=source_bundle,
                owner_maps=self._owner_maps,
                policy=self._policy,
                execution_profile_hash=context.execution_profile_hash,
            ),
            artifact_scope=ArtifactScope("pipeline", "job", context.run_id),
            artifact_revision=_ARTIFACT_REVISION,
            source_bundle=source_bundle,
            owner_maps=self._owner_maps,
            selection_policy=self._policy,
        )

    async def execute(self, context: PipelineStageContext) -> PipelineStageResult:
        request = await asyncio.to_thread(self._request, context)
        if request is None:
            return PipelineStageResult(context.command.command_id, "indeterminate")
        result = await asyncio.to_thread(self._command.execute, request)
        return self._project(context, result.outcome)

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult | None:
        request = await asyncio.to_thread(self._request, context)
        if request is None:
            return None
        outcome = self._store.read_outcome(request.job, request.idempotency_key)
        if outcome is None or outcome.state in ("pending", "running"):
            return None
        return self._project(context, outcome)

    @staticmethod
    def _project(context: PipelineStageContext, outcome: CommandOutcome) -> PipelineStageResult:
        if outcome.state in ("pending", "running"):
            return PipelineStageResult(context.command.command_id, "indeterminate")
        if outcome.state not in ("succeeded", "denied", "failed") or outcome.receipt_id is None:
            raise PipelineRunValidationError("context preparation returned an invalid terminal outcome")
        return PipelineStageResult(context.command.command_id, outcome.state, outcome.receipt_id)


__all__ = [
    "ContextPreparePipelineStage",
    "ContextPreparePipelineStore",
    "context_prepare_kernel_idempotency_key",
]
