"""Thin HTTP-runtime adapter for the Kernel-owned Stage 1 narrative command."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Protocol

from autocut_kernel.pipeline.build_narrative_graph_command import (
    BuildNarrativeGraphCommand,
    BuildNarrativeGraphResult,
    NarrativeGraphStore,
)
from autocut_kernel.pipeline.build_narrative_graph_request import (
    BuildNarrativeGraphRequest,
)
from autocut_kernel.registry.installed_local_run import LocalRunResource
from autocut_kernel.semantic_chain.draft_provider import DraftProviderPort
from autocut_kernel.semantic_chain.stage1_command_policy import Stage1CommandPolicy
from autocut_kernel.store import (
    ArtifactScope,
    CommandOutcome,
    CommittedArtifactMemberReference,
    CommittedSemanticInputsRequest,
    Job,
    SemanticInputUnavailableError,
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

_VLM_SELECTION_STRATEGY_VERSION = "all-committed-episodes-sequential-v1"
_SOURCE_ARTIFACT_REVISION = 1


class Stage1NarrativePipelineStore(SourcePrepStore, NarrativeGraphStore, Protocol):
    """Only public committed-outcome/read seams needed by the runtime adapter."""

    def read_committed_vlm_semantic_pack_set_reference(
        self, job: Job, idempotency_key: str,
    ) -> CommittedArtifactMemberReference: ...


def stage1_narrative_kernel_idempotency_key(
    *, run_id: str, source_bundle: PersistedPreparedSources, execution_profile_hash: str,
) -> str:
    validate_run_id(run_id)
    if type(source_bundle) is not PersistedPreparedSources:  # noqa: E721
        raise PipelineRunValidationError("Stage 1 identity requires exact persisted source provenance")
    return "stage1-narrative:" + hashlib.sha256(
        (
            f"{run_id}\0{execution_profile_hash}\0{source_bundle.artifact_reference.content_hash}"
            f"\0{source_bundle.canonical_hash}\0{_VLM_SELECTION_STRATEGY_VERSION}"
        ).encode("utf-8")
    ).hexdigest()


class Stage1NarrativePipelineStage:
    """Read committed Source/VLM predecessors and delegate one Kernel command."""

    def __init__(
        self,
        store: Stage1NarrativePipelineStore,
        provider: DraftProviderPort,
        *,
        command: BuildNarrativeGraphCommand | None = None,
        installed_profile: LocalRunResource | None = None,
    ) -> None:
        if not callable(getattr(provider, "dispatch", None)) or not callable(getattr(provider, "reconcile", None)):
            raise PipelineRunValidationError("Stage 1 requires an exact text generation provider")
        self._store = store
        self._command = command or BuildNarrativeGraphCommand(store, provider)
        # None is an internal unit-test seam, never standard HTTP composition.
        if installed_profile is not None and type(installed_profile) is not LocalRunResource:  # noqa: E721
            raise PipelineRunValidationError(
                "Stage 1 requires an exact installed local-run resource"
            )
        self._installed_profile = installed_profile

    @staticmethod
    def _job(context: PipelineStageContext) -> Job:
        if type(context) is not PipelineStageContext:  # noqa: E721
            raise PipelineRunValidationError("Stage 1 adapter requires an exact stage context")
        if context.command.stage != "stage1_narrative":
            raise PipelineRunValidationError("Stage 1 adapter received another stage")
        validate_run_id(context.run_id)
        return Job(context.run_id, context.request.profile)

    def _request(self, context: PipelineStageContext) -> BuildNarrativeGraphRequest | None:
        job = self._job(context)
        policy = context.execution_profile.build_stage1_command_policy()
        if type(policy) is not Stage1CommandPolicy:  # noqa: E721
            raise PipelineRunValidationError("persisted Stage 1 policy is not exact")
        installed = self._installed_profile
        if installed is not None:
            narrative = installed.narrative
            if (
                policy != narrative.command_policy
                or policy.canonical_hash
                != narrative.reference.stage1_command_policy_sha256
            ):
                raise PipelineRunValidationError(
                    "persisted Stage 1 policy differs from installed narrative policy"
                )
        source_outcome = self._store.read_outcome(job, source_prep_kernel_idempotency_key(context.run_id))
        if source_outcome is None or source_outcome.state in ("pending", "running"):
            return None
        if source_outcome.state in ("denied", "failed"):
            raise PipelineRunValidationError(
                "Stage 1 cannot execute after a terminal source-preparation predecessor"
            )
        if source_outcome.state != "succeeded":
            raise PipelineRunValidationError("source preparation outcome is unsupported")
        source_bundle = read_persisted_prepared_sources_bundle(
            self._store, job=job, outcome=source_outcome,
            artifact_scope=ArtifactScope("pipeline", "job", context.run_id),
            artifact_revision=_SOURCE_ARTIFACT_REVISION,
        )
        require_committed_source_operation(source_bundle, "semantic_analysis")
        vlm_key = vlm_batch_kernel_idempotency_key(
            run_id=context.run_id, source_bundle=source_bundle,
            execution_profile_hash=context.execution_profile_hash,
        )
        vlm_outcome = self._store.read_outcome(job, vlm_key)
        if vlm_outcome is None or vlm_outcome.state in ("pending", "running"):
            return None
        if vlm_outcome.state in ("denied", "failed"):
            raise PipelineRunValidationError(
                "Stage 1 cannot execute after a terminal VLM predecessor"
            )
        if vlm_outcome.state != "succeeded":
            raise PipelineRunValidationError("VLM aggregate outcome is unsupported")
        try:
            aggregate = self._store.read_committed_vlm_semantic_pack_set_reference(job, vlm_key)
        except SemanticInputUnavailableError as error:
            raise PipelineRunValidationError(
                "Stage 1 requires one exact committed VLM SemanticPackSet"
            ) from error
        if type(aggregate) is not CommittedArtifactMemberReference:  # noqa: E721
            raise PipelineRunValidationError("VLM reader lost exact committed aggregate identity")
        source = CommittedArtifactMemberReference(
            source_bundle.receipt_id, source_bundle.artifact_set_id, 0,
            source_bundle.artifact_reference.scope, source_bundle.artifact_reference.artifact_type,
            source_bundle.artifact_reference.logical_id, source_bundle.artifact_reference.revision,
            source_bundle.artifact_reference.content_hash,
        )
        request = policy.build_request(
            CommittedSemanticInputsRequest(job, source, aggregate),
            stage1_narrative_kernel_idempotency_key(
                run_id=context.run_id, source_bundle=source_bundle,
                execution_profile_hash=context.execution_profile_hash,
            ),
        )
        return request

    async def execute(self, context: PipelineStageContext) -> PipelineStageResult:
        request = await asyncio.to_thread(self._request, context)
        if request is None:
            return PipelineStageResult(context.command.command_id, "indeterminate")
        result = await asyncio.to_thread(self._command.execute, request)
        return self._project(context, result)

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult | None:
        request = await asyncio.to_thread(self._request, context)
        if request is None:
            return None
        result = await asyncio.to_thread(self._command.execute, request)
        projected = self._project(context, result)
        return None if projected.outcome == "indeterminate" else projected

    @staticmethod
    def _project(context: PipelineStageContext, result: BuildNarrativeGraphResult) -> PipelineStageResult:
        if type(result) is not BuildNarrativeGraphResult or type(result.outcome) is not CommandOutcome:  # noqa: E721
            raise PipelineRunValidationError("Kernel returned an invalid Stage 1 outcome")
        outcome = result.outcome
        if outcome.state in ("pending", "running"):
            return PipelineStageResult(context.command.command_id, "indeterminate")
        if outcome.state not in ("succeeded", "denied", "failed") or outcome.receipt_id is None:
            raise PipelineRunValidationError("Kernel returned an unsupported Stage 1 outcome")
        return PipelineStageResult(context.command.command_id, outcome.state, outcome.receipt_id)


__all__ = ("Stage1NarrativePipelineStage", "Stage1NarrativePipelineStore", "stage1_narrative_kernel_idempotency_key")
