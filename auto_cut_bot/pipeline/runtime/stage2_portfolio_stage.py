"""Thin Runtime adapter: admitted Stage 1 -> Kernel-owned Stage 2 Command."""

from __future__ import annotations

import asyncio

from autocut_kernel.pipeline.compile_story_portfolio_command import (
    CompileStoryPortfolioCommand,
    CompileStoryPortfolioResult,
)
from autocut_kernel.pipeline.compile_story_portfolio_request import CompileStoryPortfolioRequest
from autocut_kernel.registry.installed_local_run import LocalRunResource
from autocut_kernel.semantic_chain.draft_provider import DraftProviderPort
from autocut_kernel.store import CommandOutcome, Job

from .errors import PipelineRunValidationError
from .models import PipelineStageContext, PipelineStageResult, validate_run_id
from .semantic_predecessors import Stage1NarrativePipelineStore, read_stage2_pipeline_request


class Stage2PortfolioPipelineStage:
    def __init__(
        self, store: Stage1NarrativePipelineStore, provider: DraftProviderPort, *,
        command: CompileStoryPortfolioCommand | None = None,
        installed_profile: LocalRunResource | None = None,
    ) -> None:
        if not callable(getattr(provider, "dispatch", None)) or not callable(getattr(provider, "reconcile", None)):
            raise PipelineRunValidationError("Stage 2 requires an exact text generation provider")
        if installed_profile is not None and type(installed_profile) is not LocalRunResource:  # noqa: E721
            raise PipelineRunValidationError("Stage 2 requires an exact installed local-run resource")
        self._store = store
        self._command = command or CompileStoryPortfolioCommand(store, provider)
        # None remains an internal unit-test seam, never standard composition.
        self._installed_profile = installed_profile

    def _request(self, context: PipelineStageContext) -> CompileStoryPortfolioRequest | None:
        if type(context) is not PipelineStageContext or context.command.stage != "stage2_portfolio":  # noqa: E721
            raise PipelineRunValidationError("Stage 2 adapter requires its exact stage context")
        validate_run_id(context.run_id)
        job = Job(context.run_id, context.request.profile)
        stage1_policy = context.execution_profile.build_stage1_command_policy()
        stage2_policy = context.execution_profile.build_stage2_command_policy()
        installed = self._installed_profile
        if installed is not None:
            if (stage1_policy != installed.narrative.command_policy
                    or stage1_policy.canonical_hash != installed.narrative.reference.stage1_command_policy_sha256
                    or stage2_policy != installed.local_run.stage2_command_policy
                    or stage2_policy.canonical_hash != installed.local_run.stage2_command_policy_sha256):
                raise PipelineRunValidationError("persisted semantic policies differ from installed Stage 1/2 policies")
        return read_stage2_pipeline_request(
            self._store, job=job, run_id=context.run_id,
            execution_profile_hash=context.execution_profile_hash,
            vlm_policy=context.execution_profile.to_doubao_policy(),
            stage1_policy=stage1_policy, stage2_policy=stage2_policy,
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
    def _project(context: PipelineStageContext, result: CompileStoryPortfolioResult) -> PipelineStageResult:
        if type(result) is not CompileStoryPortfolioResult or type(result.outcome) is not CommandOutcome:  # noqa: E721
            raise PipelineRunValidationError("Kernel returned an invalid Stage 2 outcome")
        outcome = result.outcome
        if outcome.state in ("pending", "running"):
            return PipelineStageResult(context.command.command_id, "indeterminate")
        if outcome.state not in ("succeeded", "denied", "failed") or outcome.receipt_id is None:
            raise PipelineRunValidationError("Kernel returned an unsupported Stage 2 outcome")
        return PipelineStageResult(context.command.command_id, outcome.state, outcome.receipt_id)


__all__ = ("Stage2PortfolioPipelineStage",)
