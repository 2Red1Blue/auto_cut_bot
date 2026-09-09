"""Durable Runtime adapter for one admitted Stage 4 Recipe ArtifactSet."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Protocol, TypeAlias

from autocut_kernel.contracts.compiler.canonical import canonical_json_hash
from autocut_kernel.pipeline.committed_timed_media import TimedMediaReadLimits
from autocut_kernel.pipeline.compile_production_recipe_command import (
    AuthorityResolver,
    CompileProductionRecipeCommand,
    CompileProductionRecipeRequest,
    CompileProductionRecipeResult,
)
from autocut_kernel.pipeline.finalize_runtime_timed_media_evidence_batch_command import (
    FinalizeRuntimeTimedMediaEvidenceBatchRequest,
)
from autocut_kernel.pipeline.finalize_timed_media_evidence_batch_command import (
    FinalizeTimedMediaEvidenceBatchRequest,
)
from autocut_kernel.registry.installed_runtime import (
    InstalledLocalRunProfileResolver,
    InstalledRuntimeTimedSpeechAuthorityResolver,
)
from autocut_kernel.store import CommandOutcome, Job
from autocut_kernel.store.models import canonical_recipe_scope

from .errors import PipelineRunValidationError
from .media_preflight_stage import (
    MediaPreflightPipelineStage,
    media_evidence_read_limits,
)
from .models import PipelineStageContext, PipelineStageResult, validate_run_id
from .semantic_predecessors import (
    Stage1NarrativePipelineStore,
    read_stage3_recipe_predecessor,
)
from .stage4_authority import Stage4RecipeAuthorityProfile

STAGE4_RECIPE_COMMAND_STRATEGY: str = "stage4-recipe-pipeline-adapter-v1"


class Stage4RecipeCommand(Protocol):
    def execute(self, request: CompileProductionRecipeRequest) -> CompileProductionRecipeResult: ...


Stage4RecipeCommandFactory: TypeAlias = Callable[
    [Stage1NarrativePipelineStore, AuthorityResolver, TimedMediaReadLimits],
    Stage4RecipeCommand,
]


def stage4_recipe_kernel_idempotency_key(
    *,
    run_id: str,
    execution_profile_hash: str,
    stage3_idempotency_key: str,
    media_batch_idempotency_key: str,
    authority_profile_hash: str,
) -> str:
    """Bind exact predecessors and the independently frozen Stage 4 authority."""
    validate_run_id(run_id)
    for label, value, pattern in (
        ("execution_profile_hash", execution_profile_hash, r"sha256:[0-9a-f]{64}"),
        ("stage3_idempotency_key", stage3_idempotency_key, r"stage3-blueprint:[0-9a-f]{64}"),
        (
            "media_batch_idempotency_key",
            media_batch_idempotency_key,
            r"(?:media-preflight-batch|runtime-media-finalize):[0-9a-f]{64}",
        ),
        ("authority_profile_hash", authority_profile_hash, r"sha256:[0-9a-f]{64}"),
    ):
        if type(value) is not str or re.fullmatch(pattern, value) is None:  # noqa: E721
            raise PipelineRunValidationError(f"Stage 4 identity has invalid {label}")
    digest = canonical_json_hash(
        {
            "strategy_version": STAGE4_RECIPE_COMMAND_STRATEGY,
            "run_id": run_id,
            "execution_profile_hash": execution_profile_hash,
            "stage3_idempotency_key": stage3_idempotency_key,
            "media_batch_idempotency_key": media_batch_idempotency_key,
            "authority_profile_hash": authority_profile_hash,
        }
    )
    return "stage4-recipe:" + digest.removeprefix("sha256:")


class Stage4RecipePipelineStage:
    """Reconstruct and execute only the typed, deterministic Stage 4 command."""

    def __init__(
        self,
        store: Stage1NarrativePipelineStore,
        media_stage: MediaPreflightPipelineStage,
        authority_profile: Stage4RecipeAuthorityProfile,
        cpu_authority_resolver: InstalledLocalRunProfileResolver,
        *,
        cuda_authority_resolver: InstalledRuntimeTimedSpeechAuthorityResolver | None = None,
        command_factory: Stage4RecipeCommandFactory | None = None,
    ) -> None:
        if type(media_stage) is not MediaPreflightPipelineStage:  # noqa: E721
            raise PipelineRunValidationError("Stage 4 requires the exact media-preflight stage")
        if type(authority_profile) is not Stage4RecipeAuthorityProfile:  # noqa: E721
            raise PipelineRunValidationError("Stage 4 requires an exact authority profile")
        if type(cpu_authority_resolver) is not InstalledLocalRunProfileResolver:  # noqa: E721
            raise PipelineRunValidationError("Stage 4 requires the installed CPU authority resolver")
        if (
            cuda_authority_resolver is not None
            and type(cuda_authority_resolver) is not InstalledRuntimeTimedSpeechAuthorityResolver
        ):  # noqa: E721
            raise PipelineRunValidationError("Stage 4 CUDA authority resolver is invalid")
        self._store = store
        self._media_stage = media_stage
        self._authority_profile = authority_profile
        self._cpu_authority_resolver = cpu_authority_resolver
        self._cuda_authority_resolver = cuda_authority_resolver
        self._command_factory = command_factory or _default_command_factory

    async def _request(
        self,
        context: PipelineStageContext,
    ) -> tuple[CompileProductionRecipeRequest, AuthorityResolver] | None:
        if type(context) is not PipelineStageContext or context.command.stage != "stage4_recipe":  # noqa: E721
            raise PipelineRunValidationError("Stage 4 adapter requires its exact stage context")
        if context.recompute_request is not None:
            raise PipelineRunValidationError("Stage 4 cannot execute a partial recompute context")
        job = Job(context.run_id, context.request.profile)
        stage3 = await asyncio.to_thread(
            read_stage3_recipe_predecessor,
            self._store,
            job=job,
            run_id=context.run_id,
            execution_profile_hash=context.execution_profile_hash,
            vlm_policy=context.execution_profile.to_doubao_policy(),
            stage1_policy=context.execution_profile.build_stage1_command_policy(),
            stage2_policy=context.execution_profile.build_stage2_command_policy(),
            stage3_policy=context.execution_profile.build_stage3_command_policy(),
        )
        if stage3 is None:
            return None
        media = await self._media_stage.read_succeeded_finalizer(context)
        if media is None:
            return None
        stage3_request, stage3_outcome = stage3
        media_request, media_outcome = media
        if type(media_request) is FinalizeTimedMediaEvidenceBatchRequest:  # noqa: E721
            resolver: AuthorityResolver = self._cpu_authority_resolver
        elif type(media_request) is FinalizeRuntimeTimedMediaEvidenceBatchRequest:  # noqa: E721
            if self._cuda_authority_resolver is None:
                raise PipelineRunValidationError("Stage 4 CUDA batch has no installed authority resolver")
            resolver = self._cuda_authority_resolver
        else:
            raise PipelineRunValidationError("Stage 4 media finalizer type is unsupported")
        authority = self._authority_profile
        request = CompileProductionRecipeRequest(
            job,
            stage4_recipe_kernel_idempotency_key(
                run_id=context.run_id,
                execution_profile_hash=context.execution_profile_hash,
                stage3_idempotency_key=stage3_request.idempotency_key,
                media_batch_idempotency_key=media_request.idempotency_key,
                authority_profile_hash=authority.canonical_hash,
            ),
            canonical_recipe_scope(job),
            authority.artifact_revision,
            stage3_request,
            stage3_outcome,
            media_request,
            media_outcome,
            authority.editorial_exact_span_policy,
            authority.candidate_exact_span_policy,
            authority.compilation_limits,
        )
        return request, resolver

    async def execute(self, context: PipelineStageContext) -> PipelineStageResult:
        resolved = await self._request(context)
        if resolved is None:
            return PipelineStageResult(context.command.command_id, "indeterminate")
        request, resolver = resolved
        command = self._command_factory(
            self._store,
            resolver,
            media_evidence_read_limits(context.execution_profile),
        )
        result = await asyncio.to_thread(command.execute, request)
        return self._project(context, result)

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult | None:
        resolved = await self._request(context)
        if resolved is None:
            return None
        request, resolver = resolved
        command = self._command_factory(
            self._store,
            resolver,
            media_evidence_read_limits(context.execution_profile),
        )
        result = await asyncio.to_thread(command.execute, request)
        projected = self._project(context, result)
        return None if projected.outcome == "indeterminate" else projected

    @staticmethod
    def _project(
        context: PipelineStageContext,
        result: CompileProductionRecipeResult,
    ) -> PipelineStageResult:
        if type(result) is not CompileProductionRecipeResult or type(result.outcome) is not CommandOutcome:  # noqa: E721
            raise PipelineRunValidationError("Kernel returned an invalid Stage 4 outcome")
        outcome = result.outcome
        if outcome.state in ("pending", "running"):
            return PipelineStageResult(context.command.command_id, "indeterminate")
        if outcome.state not in ("succeeded", "denied", "failed") or outcome.receipt_id is None:
            raise PipelineRunValidationError("Kernel returned an unsupported Stage 4 outcome")
        return PipelineStageResult(context.command.command_id, outcome.state, outcome.receipt_id)


def _default_command_factory(
    store: Stage1NarrativePipelineStore,
    resolver: AuthorityResolver,
    limits: TimedMediaReadLimits,
) -> Stage4RecipeCommand:
    return CompileProductionRecipeCommand(store, resolver, limits)  # type: ignore[arg-type]


__all__ = (
    "STAGE4_RECIPE_COMMAND_STRATEGY",
    "Stage4RecipeCommand",
    "Stage4RecipeCommandFactory",
    "Stage4RecipePipelineStage",
    "stage4_recipe_kernel_idempotency_key",
)
