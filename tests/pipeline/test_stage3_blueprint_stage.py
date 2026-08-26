"""Provider-free HTTP adapter checks; spies do not prove semantic Admission."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import cast
from uuid import uuid4

import pytest
from autocut_kernel.pipeline.build_editorial_blueprint_command import (
    BuildEditorialBlueprintCommand,
    BuildEditorialBlueprintResult,
)
from autocut_kernel.pipeline.build_editorial_blueprint_request import BuildEditorialBlueprintRequest
from autocut_kernel.semantic_chain.draft_provider import DraftProviderPort
from autocut_kernel.store import CommandOutcome

from auto_cut_bot.pipeline.runtime.errors import PipelineRunValidationError
from auto_cut_bot.pipeline.runtime.models import (
    PipelineCommand,
    PipelineRunRequest,
    PipelineStageContext,
)
from auto_cut_bot.pipeline.runtime.stage3_blueprint_stage import (
    Stage3BlueprintPipelineStage,
    stage3_blueprint_kernel_idempotency_key,
)
from tests.pipeline.installed_profile_fixture import synthetic_installed_resource
from tests.pipeline.runtime_profile_fixture import execution_profile
from tests.pipeline.test_stage1_narrative_stage import (
    RUN_ID,
    _aggregate,
    _bundle,
    _Provider,
    _source_record,
    _source_success,
)
from tests.pipeline.test_stage2_portfolio_stage import StoryStore


class BlueprintStore(StoryStore):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.stage2_outcome = None

    def read_outcome(self, job, idempotency_key):
        if idempotency_key.startswith("stage2-portfolio:"):
            self.outcome_calls.append((job, idempotency_key))
            self.thread_ids.append(threading.get_ident())
            return self.stage2_outcome
        return super().read_outcome(job, idempotency_key)


class BlueprintCommand:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []
        self.thread_ids = []

    def execute(self, request):
        assert type(request) is BuildEditorialBlueprintRequest
        self.requests.append(request)
        self.thread_ids.append(threading.get_ident())
        return BuildEditorialBlueprintResult(self.outcome)


def context(**kwargs):
    installed = synthetic_installed_resource()
    kwargs.setdefault("stage1_policy", installed.narrative.command_policy)
    kwargs.setdefault("stage2_policy", installed.local_run.stage2_command_policy)
    kwargs.setdefault("stage3_policy", installed.local_run.stage3_command_policy)
    return PipelineStageContext(
        RUN_ID, PipelineRunRequest("test", source_reference="authorized-source"),
        PipelineCommand("stage3-control", "stage3_blueprint", "running", version=1, lease_id="lease"),
        execution_profile(**kwargs),
    )


def case():
    bundle, _ = _bundle(purposes=("semantic_analysis", "render_source"))

    def success():
        return CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4(),
                              job_id=bundle.kernel_job_id)

    store = BlueprintStore(source_outcome=_source_success(bundle), source_record=_source_record(bundle),
                           vlm_outcome=success(), aggregate=_aggregate())
    store.stage1_outcome, store.stage2_outcome = success(), success()
    spy = BlueprintCommand(success())
    stage = Stage3BlueprintPipelineStage(
        store, cast(DraftProviderPort, _Provider()), command=cast(BuildEditorialBlueprintCommand, spy),
        installed_profile=synthetic_installed_resource(),
    )
    return stage, spy, store, bundle


def test_namespace_key_binds_run_profile_and_stage2_request():
    args = {"run_id": RUN_ID, "execution_profile_hash": "sha256:" + "a" * 64,
            "stage2_idempotency_key": "stage2-portfolio:" + "b" * 64}
    key = stage3_blueprint_kernel_idempotency_key(**args)
    assert key.startswith("stage3-blueprint:") and key == stage3_blueprint_kernel_idempotency_key(**args)
    for name, value in (("run_id", "pipeline_run_" + "c" * 32),
                        ("execution_profile_hash", "sha256:" + "c" * 64),
                        ("stage2_idempotency_key", "stage2-portfolio:" + "c" * 64)):
        assert key != stage3_blueprint_kernel_idempotency_key(**{**args, name: value})
    for name in args:
        with pytest.raises(PipelineRunValidationError):
            stage3_blueprint_kernel_idempotency_key(**{**args, name: "arbitrary"})


@pytest.mark.asyncio
async def test_exact_nested_predecessors_policies_and_off_loop_command():
    stage, spy, store, bundle = case()
    ctx = context()
    result = await stage.execute(ctx)
    assert result.outcome == "succeeded" and result.receipt_id == spy.outcome.receipt_id
    request = spy.requests[0]
    assert request.stage2_outcome.job_id == bundle.kernel_job_id
    assert request.stage2_outcome.receipt_id == store.stage2_outcome.receipt_id
    assert request.stage2_outcome.artifact_set_id == store.stage2_outcome.artifact_set_id
    assert request.stage2_request.stage1_outcome.receipt_id == store.stage1_outcome.receipt_id
    assert request.stage2_request.stage1_request.inputs.source_manifest.receipt_id == bundle.receipt_id
    assert request.stage2_request.stage1_request.inputs.vlm_semantic_pack_set == store.aggregate
    assert request.command_policy == ctx.execution_profile.build_stage3_command_policy()
    assert request.stage2_request.command_policy == ctx.execution_profile.build_stage2_command_policy()
    assert request.stage2_request.stage1_request.command_policy == ctx.execution_profile.build_stage1_command_policy()
    assert request.idempotency_key == stage3_blueprint_kernel_idempotency_key(
        run_id=ctx.run_id, execution_profile_hash=ctx.execution_profile_hash,
        stage2_idempotency_key=request.stage2_request.idempotency_key,
    )
    assert len(store.outcome_calls) == 4
    assert store.outcome_calls[-1][1] == request.stage2_request.idempotency_key
    assert threading.get_ident() not in store.thread_ids + spy.thread_ids
    assert await stage.reconcile(ctx) == result
    assert spy.requests[0].to_mapping() == spy.requests[1].to_mapping()


@pytest.mark.asyncio
@pytest.mark.parametrize("predecessor", ["source", "vlm", "stage1", "stage2"])
@pytest.mark.parametrize("state", [None, "pending", "running"])
async def test_unready_predecessor_never_generates(predecessor, state):
    stage, spy, store, _ = case()
    setattr(store, f"{predecessor}_outcome", None if state is None else CommandOutcome(uuid4(), state))
    assert (await stage.execute(context())).outcome == "indeterminate"
    assert await stage.reconcile(context()) is None
    assert not spy.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("predecessor", ["source", "vlm", "stage1", "stage2"])
@pytest.mark.parametrize("state", ["denied", "failed"])
async def test_terminal_predecessor_is_not_partial_success(predecessor, state):
    stage, spy, store, _ = case()
    setattr(store, f"{predecessor}_outcome", CommandOutcome(uuid4(), state, receipt_id=uuid4()))
    with pytest.raises(PipelineRunValidationError, match="terminal"):
        await stage.execute(context())
    assert not spy.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["job_id", "receipt_id", "artifact_set_id"])
async def test_incomplete_succeeded_identity_never_generates(field):
    stage, spy, store, _ = case()
    store.stage2_outcome = replace(store.stage2_outcome, **{field: None})
    with pytest.raises(ValueError, match="exact succeeded"):
        await stage.execute(context())
    assert not spy.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [1, 2, 3])
async def test_every_installed_semantic_policy_checked_before_store_access(number):
    stage, spy, store, _ = case()
    profile = context().execution_profile
    policy = getattr(profile, f"build_stage{number}_command_policy")()
    changed = replace(policy, artifact_revision=2)
    with pytest.raises(PipelineRunValidationError, match="differ from installed"):
        await stage.execute(context(**{f"stage{number}_policy": changed}))
    assert not store.outcome_calls and not spy.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "running", "denied", "failed"])
async def test_kernel_outcome_is_not_promoted(state):
    stage, spy, _, _ = case()
    spy.outcome = CommandOutcome(uuid4(), state, receipt_id=uuid4() if state in ("denied", "failed") else None)
    result = await stage.execute(context())
    if state in ("pending", "running"):
        assert result.outcome == "indeterminate" and await stage.reconcile(context()) is None
    else:
        assert result.outcome == state and result.receipt_id == spy.outcome.receipt_id


@pytest.mark.asyncio
async def test_wrong_stage_context_never_reads_or_generates():
    stage, spy, store, _ = case()
    ctx = replace(context(), command=PipelineCommand("other", "stage2_portfolio", "pending"))
    with pytest.raises(PipelineRunValidationError, match="exact stage context"):
        await stage.execute(ctx)
    assert not spy.requests and not store.outcome_calls
