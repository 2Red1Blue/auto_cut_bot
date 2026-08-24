from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from autocut_kernel.store import CommandOutcome
from runtime_profile_fixture import execution_profile, media_preflight_policy

from auto_cut_bot.pipeline.runtime import (
    PipelineCommand,
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineRunValidationError,
    PipelineStageContext,
)
from auto_cut_bot.pipeline.runtime.media_preflight_stage import MediaPreflightPipelineStage


@pytest.mark.asyncio
async def test_execute_and_restart_reconcile_restore_policy_only_from_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = media_preflight_policy(asr_model_revision="frozen-revision")
    changed_environment = media_preflight_policy(
        asr_model_revision="changed-after-restart"
    )
    profile = execution_profile(media_policy=frozen)
    captured = []
    receipt_id = uuid4()

    def requests(self, context, policy):  # type: ignore[no-untyped-def]
        del self, context
        captured.append(policy)
        return object(), ()

    async def execute_batch(self, context, source_bundle, requests, policy):  # type: ignore[no-untyped-def]
        del self, context, source_bundle, requests
        assert policy.canonical_hash != changed_environment.canonical_hash
        return SimpleNamespace(
            outcome=CommandOutcome(uuid4(), "failed", receipt_id=receipt_id)
        )

    monkeypatch.setattr(MediaPreflightPipelineStage, "_requests", requests)
    monkeypatch.setattr(MediaPreflightPipelineStage, "_execute_batch", execute_batch)

    execute_context = PipelineStageContext(
        "pipeline_run_" + "a" * 32,
        PipelineRunRequest("test", source_root="/authorized/source"),
        PipelineCommand("media-command", "media_preflight", "pending"),
        profile,
    )
    reconcile_context = PipelineStageContext(
        execute_context.run_id,
        execute_context.request,
        PipelineCommand("media-command", "media_preflight", "indeterminate", version=2),
        profile,
    )
    first_process = MediaPreflightPipelineStage(MagicMock(), MagicMock())
    restarted_process = MediaPreflightPipelineStage(MagicMock(), MagicMock())

    executed = await first_process.execute(execute_context)
    reconciled = await restarted_process.reconcile(reconcile_context)

    assert executed.outcome == "failed"
    assert reconciled is not None and reconciled.outcome == "failed"
    assert [policy.to_mapping() for policy in captured] == [
        frozen.to_mapping(),
        frozen.to_mapping(),
    ]
    assert all(policy.word_timing_capability == "required" for policy in captured)


def test_media_preflight_context_rejects_legacy_v2_profile() -> None:
    mapping = execution_profile().to_mapping()
    mapping["schema_version"] = "pipeline-execution-profile-v2"
    del mapping["media_preflight_policy"]
    del mapping["media_preflight_policy_hash"]
    v2 = PipelineExecutionProfile.from_mapping(mapping)

    with pytest.raises(PipelineRunValidationError, match="without its frozen policy"):
        PipelineStageContext(
            "pipeline_run_" + "b" * 32,
            PipelineRunRequest("test", source_root="/authorized/source"),
            PipelineCommand("media-command", "media_preflight", "pending"),
            v2,
        )
