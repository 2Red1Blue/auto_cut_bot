"""Read-only Stage 3 reconstruction for the Stage 4 recipe consumer."""

from __future__ import annotations

from uuid import uuid4

import pytest
from autocut_kernel.store import CommandOutcome, Job
from test_stage3_blueprint_stage import case, context

from auto_cut_bot.pipeline.runtime.errors import PipelineRunValidationError
from auto_cut_bot.pipeline.runtime.semantic_predecessors import read_stage3_recipe_predecessor
from auto_cut_bot.pipeline.runtime.stage3_blueprint_stage import (
    stage3_blueprint_kernel_idempotency_key,
)


def _read(store, ctx):
    return read_stage3_recipe_predecessor(
        store, job=Job(ctx.run_id, ctx.request.profile), run_id=ctx.run_id,
        execution_profile_hash=ctx.execution_profile_hash,
        vlm_policy=ctx.execution_profile.to_doubao_policy(),
        stage1_policy=ctx.execution_profile.build_stage1_command_policy(),
        stage2_policy=ctx.execution_profile.build_stage2_command_policy(),
        stage3_policy=ctx.execution_profile.build_stage3_command_policy(),
    )


def _stage3_outcome(store, outcome):
    previous = store.read_outcome

    def read_outcome(job, key):
        if key.startswith("stage3-blueprint:"):
            store.outcome_calls.append((job, key))
            return outcome
        return previous(job, key)

    store.read_outcome = read_outcome


def test_returns_exact_succeeded_pair_without_payload_or_latest_reads():
    _, _, store, _ = case()
    ctx = context()
    succeeded = CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4())
    _stage3_outcome(store, succeeded)

    pair = _read(store, ctx)

    assert pair is not None
    request, outcome = pair
    assert outcome is succeeded
    assert request.idempotency_key == stage3_blueprint_kernel_idempotency_key(
        run_id=ctx.run_id, execution_profile_hash=ctx.execution_profile_hash,
        stage2_idempotency_key=request.stage2_request.idempotency_key,
    )
    assert store.outcome_calls[-1][1] == request.idempotency_key


@pytest.mark.parametrize("state", [None, "pending", "running"])
def test_returns_none_for_missing_or_nonterminal_stage3(state):
    _, _, store, _ = case()
    _stage3_outcome(store, None if state is None else CommandOutcome(uuid4(), state))
    assert _read(store, context()) is None


@pytest.mark.parametrize("state", ["denied", "failed", "unknown"])
def test_rejects_terminal_or_malformed_stage3(state):
    _, _, store, _ = case()
    _stage3_outcome(store, CommandOutcome(uuid4(), state, receipt_id=uuid4()))
    with pytest.raises(PipelineRunValidationError, match="Stage 3 predecessor outcome|terminal"):
        _read(store, context())
