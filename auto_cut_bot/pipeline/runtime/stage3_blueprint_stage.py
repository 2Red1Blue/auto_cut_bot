"""Thin Runtime adapter: exact committed Portfolio -> shared Blueprint Command."""

from __future__ import annotations

import asyncio
import re

from autocut_kernel.contracts.compiler.canonical import canonical_json_hash
from autocut_kernel.pipeline.build_editorial_blueprint_command import (
    BuildEditorialBlueprintCommand,
    BuildEditorialBlueprintResult,
)
from autocut_kernel.pipeline.build_editorial_blueprint_request import BuildEditorialBlueprintRequest
from autocut_kernel.registry.installed_local_run import LocalRunResource
from autocut_kernel.semantic_chain.draft_provider import DraftProviderPort
from autocut_kernel.store import CommandOutcome, Job

from .errors import PipelineRunValidationError
from .models import PipelineStageContext, PipelineStageResult, validate_run_id
from .semantic_predecessors import Stage1NarrativePipelineStore, read_stage2_pipeline_request


def stage3_blueprint_kernel_idempotency_key(
    *, run_id: str, execution_profile_hash: str, stage2_idempotency_key: str,
) -> str:
    validate_run_id(run_id)
    if (type(execution_profile_hash) is not str  # noqa: E721
            or re.fullmatch(r"sha256:[0-9a-f]{64}", execution_profile_hash) is None
            or type(stage2_idempotency_key) is not str  # noqa: E721
            or re.fullmatch(r"stage2-portfolio:[0-9a-f]{64}", stage2_idempotency_key) is None):
        raise PipelineRunValidationError("Stage 3 identity requires exact profile and Stage 2 request keys")
    digest = canonical_json_hash({"run_id": run_id, "execution_profile_hash": execution_profile_hash,
                                  "stage2_idempotency_key": stage2_idempotency_key})
    return "stage3-blueprint:" + digest.removeprefix("sha256:")


class Stage3BlueprintPipelineStage:
    def __init__(
        self, store: Stage1NarrativePipelineStore, provider: DraftProviderPort, *,
        command: BuildEditorialBlueprintCommand | None = None,
        installed_profile: LocalRunResource | None = None,
    ) -> None:
        if not callable(getattr(provider, "dispatch", None)) or not callable(getattr(provider, "reconcile", None)):
            raise PipelineRunValidationError("Stage 3 requires a text generation provider")
        if installed_profile is not None and type(installed_profile) is not LocalRunResource:  # noqa: E721
            raise PipelineRunValidationError("Stage 3 requires an exact installed local-run resource")
        self._store = store
        self._command = command or BuildEditorialBlueprintCommand(store, provider)
        # The absent-resource seam is for unit tests, never standard composition.
        self._installed_profile = installed_profile

    def _request(self, context: PipelineStageContext) -> BuildEditorialBlueprintRequest | None:
        if type(context) is not PipelineStageContext or context.command.stage != "stage3_blueprint":  # noqa: E721
            raise PipelineRunValidationError("Stage 3 adapter requires its exact stage context")
        validate_run_id(context.run_id)
        job = Job(context.run_id, context.request.profile)
        stage1_policy = context.execution_profile.build_stage1_command_policy()
        stage2_policy = context.execution_profile.build_stage2_command_policy()
        stage3_policy = context.execution_profile.build_stage3_command_policy()
        installed = self._installed_profile
        if installed is not None:
            if (stage1_policy != installed.narrative.command_policy
                    or stage1_policy.canonical_hash != installed.narrative.reference.stage1_command_policy_sha256
                    or stage2_policy != installed.local_run.stage2_command_policy
                    or stage2_policy.canonical_hash != installed.local_run.stage2_command_policy_sha256
                    or stage3_policy != installed.local_run.stage3_command_policy
                    or stage3_policy.canonical_hash != installed.local_run.stage3_command_policy_sha256):
                raise PipelineRunValidationError("persisted semantic policies differ from installed Stage 1/2/3 policies")
        predecessor = read_stage2_pipeline_request(
            self._store, job=job, run_id=context.run_id,
            execution_profile_hash=context.execution_profile_hash,
            stage1_policy=stage1_policy, stage2_policy=stage2_policy,
        )
        if predecessor is None:
            return None
        outcome = self._store.read_outcome(job, predecessor.idempotency_key)
        if outcome is None:
            return None
        if type(outcome) is not CommandOutcome:  # noqa: E721
            raise PipelineRunValidationError("Stage 2 predecessor outcome is unsupported")
        if outcome.state in ("pending", "running"):
            return None
        if outcome.state in ("denied", "failed"):
            raise PipelineRunValidationError("Stage 3 cannot execute after a terminal Stage 2 predecessor")
        if outcome.state != "succeeded":
            raise PipelineRunValidationError("Stage 2 predecessor outcome is unsupported")
        return stage3_policy.build_request(
            predecessor, outcome, stage3_blueprint_kernel_idempotency_key(
                run_id=context.run_id, execution_profile_hash=context.execution_profile_hash,
                stage2_idempotency_key=predecessor.idempotency_key,
            ),
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
    def _project(context: PipelineStageContext, result: BuildEditorialBlueprintResult) -> PipelineStageResult:
        if type(result) is not BuildEditorialBlueprintResult or type(result.outcome) is not CommandOutcome:  # noqa: E721
            raise PipelineRunValidationError("Kernel returned an invalid Stage 3 outcome")
        outcome = result.outcome
        if outcome.state in ("pending", "running"):
            return PipelineStageResult(context.command.command_id, "indeterminate")
        if outcome.state not in ("succeeded", "denied", "failed") or outcome.receipt_id is None:
            raise PipelineRunValidationError("Kernel returned an unsupported Stage 3 outcome")
        return PipelineStageResult(context.command.command_id, outcome.state, outcome.receipt_id)


__all__ = ("Stage3BlueprintPipelineStage", "stage3_blueprint_kernel_idempotency_key")
