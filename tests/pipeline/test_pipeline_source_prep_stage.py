from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from autocut_kernel.store import ArtifactScope, CommandOutcome, Job

from auto_cut_bot.pipeline.runtime import (
    PipelineCommand,
    PipelineRunRequest,
    PipelineStageContext,
    SourcePrepPipelineStage,
)
from auto_cut_bot.pipeline.runtime.source_prep_stage import (
    source_prep_kernel_idempotency_key,
)
from auto_cut_bot.pipeline.source_prep import (
    AuthorizedSeriesSourceRoot,
    PrepareWholeSeriesSourcesResult,
    SourceManifestDecodeError,
    read_persisted_prepared_sources,
)


class RootResolver:
    def resolve(self, context: PipelineStageContext) -> AuthorizedSeriesSourceRoot:
        assert context.request.source_root is not None
        return AuthorizedSeriesSourceRoot(
            Path(context.request.source_root),
            "http-authority",
            "series-1",
            1,
        )


class Store:
    def __init__(self, outcome: CommandOutcome | None = None) -> None:
        self.outcome = outcome
        self.reads: list[tuple[Job, str]] = []

    def read_outcome(self, job: Job, idempotency_key: str) -> CommandOutcome | None:
        self.reads.append((job, idempotency_key))
        return self.outcome

    def read_whole_series_source_manifest(self, job: Job, artifact_set_id):
        del job, artifact_set_id
        raise AssertionError("reader must reject non-success before persistence access")


class Command:
    def __init__(self, outcome: CommandOutcome) -> None:
        self.outcome = outcome
        self.requests = []
        self.resume_requests = []

    def execute(self, request):
        self.requests.append(request)
        return PrepareWholeSeriesSourcesResult(self.outcome)

    def resume(self, request):
        self.resume_requests.append(request)
        return PrepareWholeSeriesSourcesResult(self.outcome)


def context(tmp_path: Path, *, status: str = "running") -> PipelineStageContext:
    request = PipelineRunRequest("test", source_root=str(tmp_path.resolve()))
    return PipelineStageContext(
        "pipeline_run_" + "a" * 32,
        request,
        PipelineCommand(
            "00000000-0000-0000-0000-000000000001",
            "source_prep",
            status,  # type: ignore[arg-type]
            None,
            1,
            "lease-1" if status == "running" else None,
        ),
    )


@pytest.mark.asyncio
async def test_stage_uses_deterministic_kernel_job_and_run_bound_key(tmp_path: Path) -> None:
    receipt_id = uuid4()
    outcome = CommandOutcome(uuid4(), "succeeded", receipt_id=receipt_id)
    command = Command(outcome)
    stage = SourcePrepPipelineStage(Store(), RootResolver(), command=command)  # type: ignore[arg-type]
    stage_context = context(tmp_path)

    result = await stage.execute(stage_context)

    assert result.receipt_id == receipt_id
    assert result.outcome == "succeeded"
    assert len(command.requests) == 1
    request = command.requests[0]
    assert request.job == Job(stage_context.run_id, "test")
    assert request.idempotency_key == source_prep_kernel_idempotency_key(
        stage_context.run_id
    )
    assert request.artifact_scope == ArtifactScope("pipeline", "job", stage_context.run_id)


@pytest.mark.asyncio
async def test_running_kernel_source_command_uses_safe_resume_and_projects_receipt(
    tmp_path: Path,
) -> None:
    running = CommandOutcome(uuid4(), "running")
    store = Store(running)
    command = Command(CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4()))
    stage = SourcePrepPipelineStage(store, RootResolver(), command=command)  # type: ignore[arg-type]
    stage_context = context(tmp_path, status="indeterminate")

    result = await stage.reconcile(stage_context)

    assert result is not None
    assert result.outcome == "succeeded"
    assert result.receipt_id == command.outcome.receipt_id
    assert store.reads == []
    assert command.requests == []
    assert len(command.resume_requests) == 1
    assert command.resume_requests[0].idempotency_key == (
        source_prep_kernel_idempotency_key(stage_context.run_id)
    )


@pytest.mark.asyncio
async def test_reconcile_projects_exact_terminal_kernel_receipt(tmp_path: Path) -> None:
    receipt_id = uuid4()
    denied = CommandOutcome(uuid4(), "denied", receipt_id=receipt_id)
    store = Store(denied)
    stage = SourcePrepPipelineStage(
        store,
        RootResolver(),
        command=Command(denied),  # type: ignore[arg-type]
    )
    stage_context = context(tmp_path, status="indeterminate")

    result = await stage.reconcile(stage_context)

    assert result is not None
    assert result.outcome == "denied"
    assert result.receipt_id == receipt_id


def test_persisted_reader_rejects_nonterminal_without_reading_or_probing() -> None:
    store = Store()
    with pytest.raises(SourceManifestDecodeError, match="succeeded Kernel Receipt"):
        read_persisted_prepared_sources(
            store,  # type: ignore[arg-type]
            job=Job("pipeline_run_" + "a" * 32, "test"),
            outcome=CommandOutcome(uuid4(), "running"),
            artifact_scope=ArtifactScope("pipeline", "job", "pipeline_run_" + "a" * 32),
            artifact_revision=1,
        )
    assert store.reads == []


def test_source_prep_kernel_key_is_stable_and_rejects_invalid_run_id() -> None:
    run_id = "pipeline_run_" + "b" * 32

    assert source_prep_kernel_idempotency_key(run_id) == (
        "source-prep-kernel-v1:" + run_id
    )
    assert source_prep_kernel_idempotency_key(run_id) == (
        source_prep_kernel_idempotency_key(run_id)
    )
    with pytest.raises(ValueError, match="run_id"):
        source_prep_kernel_idempotency_key("not-a-pipeline-run")
