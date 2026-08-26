"""Pure Runtime seam tests. Command spies do not prove semantic acceptance."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import cast
from uuid import uuid4

import pytest
from autocut_kernel.pipeline.compile_story_portfolio_command import (
    CompileStoryPortfolioCommand,
    CompileStoryPortfolioResult,
)
from autocut_kernel.pipeline.compile_story_portfolio_request import CompileStoryPortfolioRequest
from autocut_kernel.semantic_chain.draft_provider import DraftProviderPort
from autocut_kernel.store import CommandOutcome

from auto_cut_bot.pipeline.runtime.errors import PipelineRunValidationError
from auto_cut_bot.pipeline.runtime.models import PipelineCommand
from auto_cut_bot.pipeline.runtime.semantic_predecessors import read_stage1_pipeline_request
from auto_cut_bot.pipeline.runtime.stage2_portfolio_stage import (
    Stage2PortfolioPipelineStage,
    stage2_portfolio_kernel_idempotency_key,
)
from tests.pipeline.installed_profile_fixture import synthetic_installed_resource
from tests.pipeline.test_stage1_narrative_stage import (
    RUN_ID,
    _aggregate,
    _bundle,
    _CommittedStore,
    _Provider,
    _source_record,
    _source_success,
)
from tests.pipeline.test_stage1_narrative_stage import (
    _context as stage1_context,
)


class StoryStore(_CommittedStore):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.stage1_outcome = None

    def read_outcome(self, job, idempotency_key):
        if idempotency_key.startswith("stage1-narrative:"):
            self.outcome_calls.append((job, idempotency_key))
            self.thread_ids.append(threading.get_ident())
            return self.stage1_outcome
        return super().read_outcome(job, idempotency_key)


class StoryCommand:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []
        self.thread_ids = []

    def execute(self, request):
        assert type(request) is CompileStoryPortfolioRequest
        self.requests.append(request)
        self.thread_ids.append(threading.get_ident())
        return CompileStoryPortfolioResult(self.outcome)


def context(**kwargs):
    installed = synthetic_installed_resource()
    kwargs.setdefault("stage1_policy", installed.narrative.command_policy)
    kwargs.setdefault("stage2_policy", installed.local_run.stage2_command_policy)
    return replace(stage1_context(**kwargs), command=PipelineCommand(
        "stage2-control", "stage2_portfolio", "running", version=1, lease_id="lease",
    ))


def case():
    bundle, _ = _bundle(purposes=("semantic_analysis", "render_source"))
    store = StoryStore(source_outcome=_source_success(bundle), source_record=_source_record(bundle),
                       vlm_outcome=CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(),
                                                  artifact_set_id=uuid4(), job_id=bundle.kernel_job_id),
                       aggregate=_aggregate())
    store.stage1_outcome = CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(),
                                          artifact_set_id=uuid4(), job_id=bundle.kernel_job_id)
    outcome = CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4(),
                              job_id=bundle.kernel_job_id)
    spy = StoryCommand(outcome)
    stage = Stage2PortfolioPipelineStage(
        store, cast(DraftProviderPort, _Provider()), command=cast(CompileStoryPortfolioCommand, spy),
        installed_profile=synthetic_installed_resource(),
    )
    return stage, spy, store, bundle


def test_stage2_namespace_key_is_stable_and_binds_run_profile_predecessor():
    values = {"run_id": RUN_ID, "execution_profile_hash": "sha256:" + "a" * 64,
              "stage1_idempotency_key": "stage1-narrative:" + "b" * 64}
    key = stage2_portfolio_kernel_idempotency_key(**values)
    assert key.startswith("stage2-portfolio:") and key == stage2_portfolio_kernel_idempotency_key(**values)
    for name, value in (("run_id", "pipeline_run_" + "b" * 32),
                         ("execution_profile_hash", "sha256:" + "c" * 64),
                         ("stage1_idempotency_key", "stage1-narrative:" + "c" * 64)):
        assert key != stage2_portfolio_kernel_idempotency_key(**{**values, name: value})
    with pytest.raises(PipelineRunValidationError):
        stage2_portfolio_kernel_idempotency_key(**{**values, "stage1_idempotency_key": "raw-user-key"})


@pytest.mark.asyncio
async def test_exact_predecessor_and_policy_request_reconstructed_off_event_loop():
    stage, spy, store, bundle = case()
    ctx = context()
    result = await stage.execute(ctx)
    assert result.outcome == "succeeded" and result.receipt_id == spy.outcome.receipt_id
    assert len(spy.requests) == 1
    request = spy.requests[0]
    assert request.stage1_outcome.job_id == bundle.kernel_job_id
    assert request.stage1_outcome.receipt_id == store.stage1_outcome.receipt_id
    assert request.stage1_outcome.artifact_set_id == store.stage1_outcome.artifact_set_id
    assert request.stage1_request.inputs.source_manifest.receipt_id == bundle.receipt_id
    assert request.stage1_request.inputs.vlm_semantic_pack_set == store.aggregate
    assert request.command_policy == ctx.execution_profile.build_stage2_command_policy()
    assert request.stage1_request.command_policy == ctx.execution_profile.build_stage1_command_policy()
    assert request.idempotency_key == stage2_portfolio_kernel_idempotency_key(
        run_id=ctx.run_id, execution_profile_hash=ctx.execution_profile_hash,
        stage1_idempotency_key=request.stage1_request.idempotency_key,
    )
    assert len(store.outcome_calls) == 3
    assert store.outcome_calls[-1][1] == request.stage1_request.idempotency_key
    assert threading.get_ident() not in store.thread_ids + spy.thread_ids
    # Direct helper reconstructs the same request but never executes Stage1.
    assert read_stage1_pipeline_request(store, job=request.job, run_id=ctx.run_id,
                                       execution_profile_hash=ctx.execution_profile_hash,
                                       policy=ctx.execution_profile.build_stage1_command_policy()) == request.stage1_request


@pytest.mark.asyncio
async def test_reconcile_uses_same_command_and_exact_prior_identity():
    stage, spy, _, _ = case()
    ctx = context()
    executed = await stage.execute(ctx)
    reconciled = await stage.reconcile(ctx)
    assert reconciled == executed
    assert spy.requests[0].to_mapping() == spy.requests[1].to_mapping()


@pytest.mark.asyncio
@pytest.mark.parametrize("predecessor", ["source", "vlm", "stage1"])
@pytest.mark.parametrize("state", [None, "pending", "running"])
async def test_any_unready_predecessor_never_invokes_stage2(predecessor, state):
    stage, spy, store, _ = case()
    setattr(store, f"{predecessor}_outcome", None if state is None else CommandOutcome(uuid4(), state))
    assert (await stage.execute(context())).outcome == "indeterminate"
    assert await stage.reconcile(context()) is None
    assert not spy.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("predecessor", ["source", "vlm", "stage1"])
@pytest.mark.parametrize("state", ["denied", "failed"])
async def test_terminal_predecessor_cannot_become_stage2_success(predecessor, state):
    stage, spy, store, _ = case()
    setattr(store, f"{predecessor}_outcome", CommandOutcome(uuid4(), state, receipt_id=uuid4()))
    with pytest.raises(PipelineRunValidationError, match="terminal"):
        await stage.execute(context())
    assert not spy.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["job_id", "receipt_id", "artifact_set_id"])
async def test_incomplete_succeeded_predecessor_is_rejected_before_generation(field):
    stage, spy, store, _ = case()
    store.stage1_outcome = replace(store.stage1_outcome, **{field: None})
    with pytest.raises(ValueError, match="exact succeeded"):
        await stage.execute(context())
    assert not spy.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_number", [1, 2])
async def test_installed_policy_mismatch_stops_before_any_store_calls(stage_number):
    stage, spy, store, _ = case()
    base = context().execution_profile
    policy = base.build_stage1_command_policy() if stage_number == 1 else base.build_stage2_command_policy()
    changed = replace(policy, artifact_revision=2)
    with pytest.raises(PipelineRunValidationError, match="differ from installed"):
        await stage.execute(context(**{f"stage{stage_number}_policy": changed}))
    assert not store.outcome_calls and not spy.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "running", "failed", "denied"])
async def test_kernel_pending_and_terminal_receipts_are_projected_without_fake_success(state):
    stage, spy, _, _ = case()
    spy.outcome = CommandOutcome(uuid4(), state, receipt_id=uuid4() if state in ("failed", "denied") else None)
    result = await stage.execute(context())
    if state in ("pending", "running"):
        assert result.outcome == "indeterminate" and await stage.reconcile(context()) is None
    else:
        assert result.outcome == state and result.receipt_id == spy.outcome.receipt_id


@pytest.mark.asyncio
async def test_another_stage_context_cannot_invoke_portfolio_command():
    stage, spy, store, _ = case()
    with pytest.raises(PipelineRunValidationError, match="exact stage context"):
        await stage.execute(stage1_context())
    assert not spy.requests and not store.outcome_calls
