"""Durable adapter from committed source preparation to Kernel VLM commands."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from typing import Protocol

from autocut_kernel.pipeline import (
    FinalizeVlmBatchCommand,
    FinalizeVlmBatchRequest,
    FinalizeVlmBatchResult,
    GenerateVlmEvidenceCommand,
    GenerateVlmEvidenceRequest,
    GenerateVlmEvidenceResult,
    GenerationStore,
    VlmBatchChildOutcome,
    VlmBatchFinalizerStore,
)
from autocut_kernel.store import ArtifactScope, CommandOutcome, Job
from autocut_kernel.store.models import canonical_recipe_scope
from autocut_kernel.vlm import VlmProviderPort

from auto_cut_bot.pipeline.source_prep import (
    PersistedPreparedSources,
    SourcePrepStore,
    read_persisted_prepared_sources_bundle,
)
from auto_cut_bot.pipeline.vlm import DoubaoVlmRequestPolicy, build_doubao_vlm_request

from .errors import PipelineRunValidationError
from .models import PipelineStageContext, PipelineStageResult, validate_run_id
from .source_prep_stage import source_prep_kernel_idempotency_key

VLM_EPISODE_SELECTION_STRATEGY_VERSION = "all-committed-episodes-sequential-v1"
_ARTIFACT_REVISION = 1


class VlmPipelineStore(
    SourcePrepStore,
    GenerationStore,
    VlmBatchFinalizerStore,
    Protocol,
):
    """The shared Kernel Store capabilities required by this adapter."""


def vlm_kernel_idempotency_key(
    *,
    run_id: str,
    episode_index: int,
    source_bundle: PersistedPreparedSources,
    policy: DoubaoVlmRequestPolicy,
    execution_profile_hash: str,
) -> str:
    validate_run_id(run_id)
    if type(episode_index) is not int or episode_index < 0:  # noqa: E721
        raise PipelineRunValidationError("VLM episode index must be non-negative")
    if type(source_bundle) is not PersistedPreparedSources:  # noqa: E721
        raise PipelineRunValidationError(
            "VLM idempotency requires exact persisted source provenance"
        )
    payload = {
        "episode_index": episode_index,
        "execution_profile_sha256": execution_profile_hash,
        "policy_sha256": policy.canonical_hash,
        "run_id": run_id,
        "selection_strategy_version": VLM_EPISODE_SELECTION_STRATEGY_VERSION,
        "source_manifest_sha256": source_bundle.artifact_reference.content_hash,
        "source_provenance_sha256": source_bundle.canonical_hash,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "vlm:" + hashlib.sha256(encoded).hexdigest()


class VlmPipelineStage:
    """Process committed episodes sequentially, then project a batch Receipt.

    Per-episode Receipts are never presented as aggregate completion evidence.
    The final control-plane result is owned by ``FinalizeVlmBatchCommand``.
    """

    def __init__(
        self,
        store: VlmPipelineStore,
        provider: VlmProviderPort,
        *,
        command: GenerateVlmEvidenceCommand | None = None,
        finalizer: FinalizeVlmBatchCommand | None = None,
    ) -> None:
        if not callable(getattr(provider, "dispatch", None)) or not callable(
            getattr(provider, "reconcile", None)
        ):
            raise PipelineRunValidationError("VLM provider is required")
        self._store = store
        self._command = command or GenerateVlmEvidenceCommand(store, provider)
        self._finalizer = finalizer or FinalizeVlmBatchCommand(store)

    @staticmethod
    def _job(context: PipelineStageContext) -> Job:
        if type(context) is not PipelineStageContext:  # noqa: E721
            raise PipelineRunValidationError("VLM adapter requires an exact stage context")
        if context.command.stage != "vlm":
            raise PipelineRunValidationError("VLM adapter received another stage")
        if context.execution_profile.is_legacy_unresolved:
            raise PipelineRunValidationError(
                "legacy-unresolved execution profile cannot execute VLM"
            )
        validate_run_id(context.run_id)
        return Job(context.run_id, context.request.profile)

    def _requests(
        self,
        context: PipelineStageContext,
    ) -> tuple[PersistedPreparedSources, tuple[GenerateVlmEvidenceRequest, ...]] | None:
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
            raise PipelineRunValidationError("Kernel returned an unsupported source outcome")
        source_bundle = read_persisted_prepared_sources_bundle(
            self._store,
            job=job,
            outcome=source_outcome,
            artifact_scope=ArtifactScope("pipeline", "job", context.run_id),
            artifact_revision=_ARTIFACT_REVISION,
        )
        if type(source_bundle) is not PersistedPreparedSources:  # noqa: E721
            raise PipelineRunValidationError(
                "VLM source reader lost exact persisted provenance"
            )
        if not source_bundle.prepared.episodes:
            raise PipelineRunValidationError(
                "VLM stage requires at least one committed source episode"
            )
        policy = context.execution_profile.to_doubao_policy()
        retry_policy = context.execution_profile.to_generation_retry_policy()
        return source_bundle, tuple(
            replace(
                build_doubao_vlm_request(
                    source_bundle=source_bundle,
                    episode_index=episode_index,
                    job=job,
                    artifact_revision=_ARTIFACT_REVISION,
                    idempotency_key=vlm_kernel_idempotency_key(
                        run_id=context.run_id,
                        episode_index=episode_index,
                        source_bundle=source_bundle,
                        policy=policy,
                        execution_profile_hash=context.execution_profile_hash,
                    ),
                    policy=policy,
                    retry_policy=retry_policy,
                ),
                episode_index=episode_index,
                source_manifest_sha256=source_bundle.artifact_reference.content_hash,
            )
            for episode_index in range(len(source_bundle.prepared.episodes))
        )

    async def execute(self, context: PipelineStageContext) -> PipelineStageResult:
        prepared = await asyncio.to_thread(self._requests, context)
        if prepared is None:
            return PipelineStageResult(context.command.command_id, "indeterminate")
        result = await self._execute_batch(context, *prepared)
        return self._project(context, result.outcome)

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult | None:
        prepared = await asyncio.to_thread(self._requests, context)
        if prepared is None:
            return None
        result = await self._execute_batch(context, *prepared)
        projected = self._project(context, result.outcome)
        if projected.outcome == "indeterminate":
            return None
        return projected

    async def _execute_batch(
        self,
        context: PipelineStageContext,
        source_bundle: PersistedPreparedSources,
        requests: tuple[GenerateVlmEvidenceRequest, ...],
    ) -> FinalizeVlmBatchResult | GenerateVlmEvidenceResult:
        children: list[VlmBatchChildOutcome] = []
        for episode_index, request in enumerate(requests):
            result = await asyncio.to_thread(self._command.execute, request)
            outcome = result.outcome
            if outcome.state in ("pending", "running"):
                return result
            if outcome.state not in ("succeeded", "denied", "failed"):
                raise PipelineRunValidationError(
                    "Kernel returned an unsupported VLM child outcome"
                )
            if outcome.receipt_id is None:
                raise PipelineRunValidationError(
                    "terminal Kernel VLM child lost its Receipt"
                )
            if outcome.state in ("denied", "failed"):
                # A causal child Receipt truthfully blocks all-or-nothing batch
                # success. Rejected Generate commands have no ArtifactSet-backed
                # request record, so they are never passed to the success-only
                # aggregate finalizer and later episodes are not dispatched.
                return result
            children.append(
                VlmBatchChildOutcome(
                    episode_index=episode_index,
                    idempotency_key=request.idempotency_key,
                    window_manifest_sha256=request.manifest.canonical_hash,
                    source_manifest_sha256=source_bundle.artifact_reference.content_hash,
                    source_provenance_sha256=source_bundle.canonical_hash,
                    request_hash=request.request_hash,
                    state=outcome.state,
                    receipt_id=outcome.receipt_id,
                    artifact_set_id=outcome.artifact_set_id,
                )
            )
        finalizer_request = FinalizeVlmBatchRequest(
            job=Job(context.run_id, context.request.profile),
            idempotency_key=self._batch_idempotency_key(context, source_bundle),
            artifact_scope=canonical_recipe_scope(
                Job(context.run_id, context.request.profile)
            ),
            artifact_revision=_ARTIFACT_REVISION,
            declared_episode_count=len(requests),
            source_manifest_sha256=source_bundle.artifact_reference.content_hash,
            source_provenance_sha256=source_bundle.canonical_hash,
            children=tuple(children),
        )
        return await asyncio.to_thread(self._finalizer.execute, finalizer_request)

    @staticmethod
    def _batch_idempotency_key(
        context: PipelineStageContext,
        source_bundle: PersistedPreparedSources,
    ) -> str:
        encoded = json.dumps(
            {
                "execution_profile_sha256": context.execution_profile_hash,
                "run_id": context.run_id,
                "selection_strategy_version": VLM_EPISODE_SELECTION_STRATEGY_VERSION,
                "source_manifest_sha256": source_bundle.artifact_reference.content_hash,
                "source_provenance_sha256": source_bundle.canonical_hash,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "vlm-batch:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _project(
        context: PipelineStageContext,
        outcome: CommandOutcome,
    ) -> PipelineStageResult:
        if type(outcome) is not CommandOutcome:  # noqa: E721
            raise PipelineRunValidationError("Kernel returned an invalid VLM outcome")
        if outcome.state in ("pending", "running"):
            return PipelineStageResult(context.command.command_id, "indeterminate")
        if outcome.state not in ("succeeded", "denied", "failed"):
            raise PipelineRunValidationError("Kernel returned an unsupported VLM outcome")
        if outcome.receipt_id is None:
            raise PipelineRunValidationError("terminal Kernel VLM outcome lost its Receipt")
        return PipelineStageResult(
            context.command.command_id,
            outcome.state,
            outcome.receipt_id,
        )


__all__ = (
    "VLM_EPISODE_SELECTION_STRATEGY_VERSION",
    "VlmPipelineStage",
    "VlmPipelineStore",
    "vlm_kernel_idempotency_key",
)
