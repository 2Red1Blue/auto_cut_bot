from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from autocut_kernel.media.types import TimeBase
from autocut_kernel.physical_edit.candidate_exact_span import CandidateExactSpanPolicy
from autocut_kernel.physical_edit.editorial_exact_span import (
    EDITORIAL_EXACT_SPAN_STRATEGY,
    EditorialExactSpanPolicy,
)
from autocut_kernel.pipeline.compile_production_recipe_command import (
    CompileProductionRecipeResult,
    ProductionRecipeCompilationLimits,
)
from autocut_kernel.registry.installed_runtime import InstalledLocalRunProfileResolver
from autocut_kernel.store import CommandOutcome

from auto_cut_bot.pipeline.runtime import PipelineCommand, PipelineRunRequest, PipelineStageContext
from auto_cut_bot.pipeline.runtime.media_preflight_stage import MediaPreflightPipelineStage
from auto_cut_bot.pipeline.runtime.stage4_authority import Stage4RecipeAuthorityProfile
from auto_cut_bot.pipeline.runtime.stage4_recipe_stage import (
    Stage4RecipePipelineStage,
    stage4_recipe_kernel_idempotency_key,
)
from tests.pipeline.runtime_profile_fixture import execution_profile
from tests.pipeline.test_build_span_variant_set_command import _prepared_case

RUN_ID = "pipeline_run_" + "b" * 32


def _authority() -> Stage4RecipeAuthorityProfile:
    return Stage4RecipeAuthorityProfile(
        1,
        EditorialExactSpanPolicy(EDITORIAL_EXACT_SPAN_STRATEGY, 90_000, TimeBase(1, 90_000)),
        CandidateExactSpanPolicy(100_000, 100_000, 1, 1, 0),
        ProductionRecipeCompilationLimits(10_000, 8_000_000, 32_000_000),
    )


def _context() -> PipelineStageContext:
    return PipelineStageContext(
        RUN_ID,
        PipelineRunRequest("test", source_reference="authorized-source"),
        PipelineCommand("stage4-command", "stage4_recipe", "running", lease_id="lease"),
        execution_profile(),
    )


class _Command:
    def __init__(self, outcome: CommandOutcome) -> None:
        self.outcome = outcome
        self.requests: list[object] = []

    def execute(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return CompileProductionRecipeResult(self.outcome)


class _CapturedRequest:
    def __init__(self, *args: object) -> None:
        self.args = args
        self.idempotency_key = args[1]
        self.stage3_request = args[4]
        self.media_batch_request = args[6]


@pytest.mark.asyncio
async def test_stage4_rebuilds_exact_parents_and_projects_kernel_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_store, variant_request, resolver, _limits, _committed = _prepared_case(
        tmp_path, monkeypatch
    )
    del resolver
    parent = variant_request.parent_request
    stage3_request = replace(
        parent.stage3_request,
        idempotency_key="stage3-blueprint:" + "a" * 64,
    )
    media_request = replace(
        parent.media_batch_request,
        idempotency_key="media-preflight-batch:" + "c" * 64,
    )
    media = object.__new__(MediaPreflightPipelineStage)

    async def finalizer(_context):  # type: ignore[no-untyped-def]
        return media_request, parent.media_batch_outcome

    media.read_succeeded_finalizer = finalizer  # type: ignore[attr-defined]
    cpu = object.__new__(InstalledLocalRunProfileResolver)
    outcome = CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4())
    command = _Command(outcome)
    stage = Stage4RecipePipelineStage(
        fixture_store,
        media,
        _authority(),
        cpu,
        command_factory=lambda *_args: command,
    )
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.stage4_recipe_stage.read_stage3_recipe_predecessor",
        lambda *_args, **_kwargs: (stage3_request, parent.stage3_outcome),
    )
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.stage4_recipe_stage.CompileProductionRecipeRequest",
        _CapturedRequest,
    )

    result = await stage.execute(_context())

    assert result.outcome == "succeeded"
    assert result.receipt_id == outcome.receipt_id
    assert len(command.requests) == 1
    request = command.requests[0]
    assert request.stage3_request is stage3_request
    assert request.media_batch_request is media_request
    assert request.idempotency_key.startswith("stage4-recipe:")


@pytest.mark.asyncio
async def test_stage4_is_indeterminate_until_both_predecessors_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = object.__new__(MediaPreflightPipelineStage)

    async def finalizer(_context):  # type: ignore[no-untyped-def]
        raise AssertionError("media reader must not run before Stage 3 closes")

    media.read_succeeded_finalizer = finalizer  # type: ignore[attr-defined]
    stage = Stage4RecipePipelineStage(
        SimpleNamespace(),
        media,
        _authority(),
        object.__new__(InstalledLocalRunProfileResolver),
        command_factory=lambda *_args: _Command(CommandOutcome(uuid4(), "pending")),
    )
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.stage4_recipe_stage.read_stage3_recipe_predecessor",
        lambda *_args, **_kwargs: None,
    )

    result = await stage.execute(_context())

    assert result.outcome == "indeterminate"


def test_stage4_identity_is_closed_over_both_parents_and_authority() -> None:
    common = {
        "run_id": RUN_ID,
        "execution_profile_hash": "sha256:" + "a" * 64,
        "stage3_idempotency_key": "stage3-blueprint:" + "b" * 64,
        "media_batch_idempotency_key": "media-preflight-batch:" + "c" * 64,
        "authority_profile_hash": "sha256:" + "d" * 64,
    }
    first = stage4_recipe_kernel_idempotency_key(**common)
    changed = stage4_recipe_kernel_idempotency_key(
        **{**common, "authority_profile_hash": "sha256:" + "e" * 64}
    )

    assert first.startswith("stage4-recipe:")
    assert changed != first
