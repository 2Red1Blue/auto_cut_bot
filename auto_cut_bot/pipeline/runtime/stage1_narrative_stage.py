"""Thin HTTP-runtime adapter for the Kernel-owned Stage 1 narrative command."""

from __future__ import annotations

import asyncio

from autocut_kernel.pipeline.build_narrative_graph_command import (
    BuildNarrativeGraphCommand,
    BuildNarrativeGraphResult,
)
from autocut_kernel.pipeline.build_narrative_graph_request import (
    BuildNarrativeGraphRequest,
)
from autocut_kernel.registry.installed_local_run import LocalRunResource
from autocut_kernel.semantic_chain.draft_provider import DraftProviderPort
from autocut_kernel.semantic_chain.stage1_command_policy import Stage1CommandPolicy
from autocut_kernel.store import CommandOutcome, Job

from .errors import PipelineRunValidationError
from .models import PipelineStageContext, PipelineStageResult, validate_run_id
from .semantic_predecessors import (
    Stage1NarrativePipelineStore,
    read_stage1_pipeline_request,
    stage1_narrative_kernel_idempotency_key,
)


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
        return read_stage1_pipeline_request(
            self._store, job=job, run_id=context.run_id,
            execution_profile_hash=context.execution_profile_hash, policy=policy,
        )

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
