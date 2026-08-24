"""Durable adapter from an HTTP source_prep command to the Kernel command."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
from typing import Protocol

from autocut_kernel.store import ArtifactScope, CommandOutcome, Job

from auto_cut_bot.pipeline.source_prep import (
    AuthorizedSeriesSourceRoot,
    PrepareWholeSeriesSourcesCommand,
    PrepareWholeSeriesSourcesRequest,
    SourcePrepStore,
)

from .errors import PipelineRunValidationError
from .models import PipelineStageContext, PipelineStageResult, validate_run_id

_SOURCE_PREP_KERNEL_IDEMPOTENCY_VERSION = "source-prep-kernel-v1"


def source_prep_kernel_idempotency_key(run_id: str) -> str:
    """Return the stable Kernel command identity for one persisted HTTP run."""

    validate_run_id(run_id)
    return f"{_SOURCE_PREP_KERNEL_IDEMPOTENCY_VERSION}:{run_id}"


class SourcePrepRootResolver(Protocol):
    """Resolve the already-authorized source identity without probing media."""

    def resolve(self, context: PipelineStageContext) -> AuthorizedSeriesSourceRoot: ...


class SourcePrepPipelineStage:
    """Project only a terminal Kernel Receipt; leave running work indeterminate."""

    def __init__(
        self,
        store: SourcePrepStore,
        root_resolver: SourcePrepRootResolver,
        *,
        command: PrepareWholeSeriesSourcesCommand | None = None,
    ) -> None:
        if not callable(getattr(root_resolver, "resolve", None)):
            raise PipelineRunValidationError("source prep root resolver is required")
        self._root_resolver = root_resolver
        self._command = command or PrepareWholeSeriesSourcesCommand(store)

    @staticmethod
    def _job(context: PipelineStageContext) -> Job:
        if type(context) is not PipelineStageContext:  # noqa: E721
            raise PipelineRunValidationError(
                "source prep adapter requires an exact stage context"
            )
        if context.command.stage != "source_prep":
            raise PipelineRunValidationError("source prep adapter received another stage")
        validate_run_id(context.run_id)
        return Job(context.run_id, context.request.profile)

    def _request(self, context: PipelineStageContext) -> PrepareWholeSeriesSourcesRequest:
        job = self._job(context)
        return PrepareWholeSeriesSourcesRequest(
            job=job,
            idempotency_key=source_prep_kernel_idempotency_key(context.run_id),
            artifact_scope=ArtifactScope("pipeline", "job", context.run_id),
            artifact_revision=1,
            source_root=self._root_resolver.resolve(context),
        )

    async def execute(self, context: PipelineStageContext) -> PipelineStageResult:
        request = self._request(context)
        result = await asyncio.to_thread(self._command.execute, request)
        return self._project(context, result.outcome)

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult | None:
        request = self._request(context)
        result = await asyncio.to_thread(self._command.resume, request)
        projected = self._project(context, result.outcome)
        return None if projected.outcome == "indeterminate" else projected

    @staticmethod
    def _project(
        context: PipelineStageContext,
        outcome: CommandOutcome,
    ) -> PipelineStageResult:
        if outcome.state in ("pending", "running"):
            return PipelineStageResult(context.command.command_id, "indeterminate")
        if outcome.state not in ("succeeded", "denied", "failed"):
            raise PipelineRunValidationError("Kernel returned an unsupported source outcome")
        if outcome.receipt_id is None:
            raise PipelineRunValidationError("terminal Kernel source outcome lost its Receipt")
        return PipelineStageResult(
            context.command.command_id,
            outcome.state,
            outcome.receipt_id,
        )


__all__ = (
    "SourcePrepPipelineStage",
    "SourcePrepRootResolver",
    "source_prep_kernel_idempotency_key",
)
