from uuid import uuid4

import pytest

from auto_cut_bot.pipeline.runtime.models import (
    PipelineCommand,
    PipelineRunRequest,
    PipelineRunSnapshot,
    PipelineRunValidationError,
    PipelineStageResult,
)
from auto_cut_bot.pipeline.runtime.postgres import _terminal_run_state


def _snapshot(status: str, media_status: str) -> PipelineRunSnapshot:
    request = PipelineRunRequest("test", source_root="/authorized/source")
    return PipelineRunSnapshot(
        "pipeline_run_" + "1" * 32,
        request,
        request.request_hash,
        status,  # type: ignore[arg-type]
        (
            PipelineCommand("source", "source_prep", "succeeded", uuid4()),
            PipelineCommand("media", "media_preflight", media_status),  # type: ignore[arg-type]
        ),
        1,
    )


@pytest.mark.parametrize(
    ("run_status", "command_status"),
    (
        ("awaiting_calibration", "awaiting_calibration"),
        ("recompute_needed", "recompute_needed"),
    ),
)
def test_calibration_wait_and_recompute_are_receiptless_target_outcomes(
    run_status: str, command_status: str,
) -> None:
    snapshot = _snapshot(run_status, command_status)

    assert snapshot.status == run_status
    assert snapshot.commands[-1].receipt_id is None
    assert snapshot.commands[-1].status == command_status
    assert PipelineStageResult(snapshot.commands[-1].command_id, command_status).receipt_id is None  # type: ignore[arg-type]


def test_calibration_wait_does_not_permit_an_executable_sibling() -> None:
    request = PipelineRunRequest("test", source_root="/authorized/source")

    with pytest.raises(PipelineRunValidationError, match="cannot retain executable"):
        PipelineRunSnapshot(
            "pipeline_run_" + "2" * 32,
            request,
            request.request_hash,
            "awaiting_calibration",
            (
                PipelineCommand("source", "source_prep", "succeeded", uuid4()),
                PipelineCommand("media", "media_preflight", "awaiting_calibration"),
                PipelineCommand("render", "render", "pending"),
            ),
            1,
        )


def test_wait_and_recompute_precede_terminal_projection() -> None:
    assert _terminal_run_state([("media_preflight", "awaiting_calibration")]) == "awaiting_calibration"
    assert _terminal_run_state([("media_preflight", "recompute_needed")]) == "recompute_needed"
    assert _terminal_run_state(
        [("media_preflight", "awaiting_calibration"), ("render", "pending")]
    ) == "running"


def test_terminal_projection_accepts_only_the_closed_semantic_plan() -> None:
    assert _terminal_run_state([
        ("source_prep", "succeeded"),
        ("vlm", "succeeded"),
    ]) == "succeeded"
    assert _terminal_run_state([
        ("source_prep", "succeeded"),
        ("vlm", "succeeded"),
        ("stage1_narrative", "succeeded"),
    ]) == "failed"
