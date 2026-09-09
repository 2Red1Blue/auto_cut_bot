"""Read-only reconstruction coverage for media-preflight finalizers."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from autocut_kernel.store import CommandOutcome, Job

from auto_cut_bot.pipeline.runtime import (
    PipelineCommand,
    PipelineRunRequest,
    PipelineRunValidationError,
    PipelineStageContext,
)
from auto_cut_bot.pipeline.runtime.media_preflight_stage import MediaPreflightPipelineStage
from tests.pipeline.runtime_profile_fixture import execution_profile

RUN_ID = "pipeline_run_" + "a" * 32


def _context() -> PipelineStageContext:
    return PipelineStageContext(
        RUN_ID,
        PipelineRunRequest("test", source_reference="authorized-source"),
        PipelineCommand("media-command", "media_preflight", "running", lease_id="lease"),
        execution_profile(),
    )


def _succeeded_outcome() -> CommandOutcome:
    return CommandOutcome(
        uuid4(),
        "succeeded",
        receipt_id=uuid4(),
        artifact_set_id=uuid4(),
        job_id=uuid4(),
    )


class _Store:
    def __init__(self, outcomes: dict[str, CommandOutcome | None]) -> None:
        self._outcomes = outcomes
        self.reads: list[str] = []

    def read_outcome(self, _job: Job, idempotency_key: str) -> CommandOutcome | None:
        self.reads.append(idempotency_key)
        return self._outcomes.get(idempotency_key)


class _CpuChild:
    def __init__(self, request: object, outcome: CommandOutcome) -> None:
        self.request = request
        self.outcome = outcome


class _RuntimeRequest:
    def __init__(self, request: object, measurement: object) -> None:
        self.timed_media_request = request
        self.runtime_measurement_identity = measurement


class _RuntimeChild:
    def __init__(self, request: object, outcome: CommandOutcome) -> None:
        self.request = request
        self.outcome = outcome


class _FinalizerRequest:
    def __init__(
        self,
        job: Job,
        idempotency_key: str,
        artifact_scope: object,
        artifact_revision: int,
        children: tuple[object, ...],
    ) -> None:
        self.job = job
        self.idempotency_key = idempotency_key
        self.artifact_scope = artifact_scope
        self.artifact_revision = artifact_revision
        self.children = children


def _stage(
    monkeypatch: pytest.MonkeyPatch,
    store: _Store,
    *,
    runtime: object | None,
    prepared: object,
) -> MediaPreflightPipelineStage:
    stage = object.__new__(MediaPreflightPipelineStage)
    stage._store = store  # pyright: ignore[reportPrivateUsage]
    stage._validate_execution_profile = lambda *_args: object()  # pyright: ignore[reportPrivateUsage]

    async def authority(*_args: object) -> object | None:
        return runtime

    stage._runtime_cuda_authority = authority  # pyright: ignore[reportPrivateUsage]
    stage._requests = lambda *_args, **_kwargs: prepared  # pyright: ignore[reportPrivateUsage]
    stage._batch_idempotency_key = lambda *_args: "cpu-finalizer"  # pyright: ignore[reportPrivateUsage]
    stage._runtime_batch_idempotency_key = lambda *_args: "cuda-finalizer"  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.media_preflight_stage.TimedMediaEvidenceBatchChild",
        _CpuChild,
    )
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.media_preflight_stage.RuntimeTimedMediaEvidenceBatchChild",
        _RuntimeChild,
    )
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.media_preflight_stage.PrepareRuntimeTimedMediaEvidenceRequest",
        _RuntimeRequest,
    )
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.media_preflight_stage.FinalizeTimedMediaEvidenceBatchRequest",
        _FinalizerRequest,
    )
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.media_preflight_stage.FinalizeRuntimeTimedMediaEvidenceBatchRequest",
        _FinalizerRequest,
    )
    return stage


@pytest.mark.asyncio
async def test_reader_reconstructs_cpu_finalizer_without_executing_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = Job(RUN_ID, "test")
    child = SimpleNamespace(job=job, idempotency_key="cpu-child", episode_index=0)
    final_outcome = _succeeded_outcome()
    store = _Store({"cpu-child": _succeeded_outcome(), "cpu-finalizer": final_outcome})
    stage = _stage(
        monkeypatch,
        store,
        runtime=None,
        prepared=(object(), (child,), (child,)),
    )

    reconstructed = await stage.read_succeeded_finalizer(_context())

    assert reconstructed is not None
    request, outcome = reconstructed
    assert type(request) is _FinalizerRequest  # noqa: E721
    assert request.idempotency_key == "cpu-finalizer"
    assert request.children[0].request is child
    assert outcome is final_outcome
    assert store.reads == ["cpu-child", "cpu-finalizer"]


@pytest.mark.asyncio
async def test_reader_reconstructs_cuda_finalizer_with_runtime_request_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = Job(RUN_ID, "test")
    child = SimpleNamespace(job=job, idempotency_key="cuda-child", episode_index=0)
    final_outcome = _succeeded_outcome()
    runtime = SimpleNamespace(measurement=object(), policy=object())
    store = _Store({"cuda-child": _succeeded_outcome(), "cuda-finalizer": final_outcome})
    stage = _stage(
        monkeypatch,
        store,
        runtime=runtime,
        prepared=(object(), (child,), (child,)),
    )

    reconstructed = await stage.read_succeeded_finalizer(_context())

    assert reconstructed is not None
    request, outcome = reconstructed
    assert type(request) is _FinalizerRequest  # noqa: E721
    runtime_request = request.children[0].request
    assert runtime_request.timed_media_request is child
    assert runtime_request.runtime_measurement_identity is runtime.measurement
    assert outcome is final_outcome


@pytest.mark.asyncio
async def test_reader_returns_none_for_missing_or_nonterminal_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store({})
    stage = _stage(monkeypatch, store, runtime=None, prepared=None)

    assert await stage.read_succeeded_finalizer(_context()) is None
    assert store.reads == []


@pytest.mark.asyncio
async def test_reader_rejects_terminal_child_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = Job(RUN_ID, "test")
    child = SimpleNamespace(job=job, idempotency_key="failed-child", episode_index=0)
    store = _Store({"failed-child": CommandOutcome(uuid4(), "failed", receipt_id=uuid4())})
    stage = _stage(
        monkeypatch,
        store,
        runtime=None,
        prepared=(object(), (child,), (child,)),
    )

    with pytest.raises(PipelineRunValidationError, match="terminal child predecessor"):
        await stage.read_succeeded_finalizer(_context())
