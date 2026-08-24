from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from auto_cut_bot.pipeline.runtime import (
    DurablePipelineRunService,
    IdempotencyConflictError,
    PipelineCommand,
    PipelineRunNotFoundError,
    PipelineRunRequest,
    PipelineRunSnapshot,
    PipelineRunValidationError,
    PipelineStageRegistry,
    PipelineStageResult,
    PipelineStageRunner,
    ResumeNotAllowedError,
    RunClaim,
    SourceDeniedError,
    StaleRunVersionError,
)


class FakeRunStore:
    def __init__(self) -> None:
        self.by_run_id: dict[str, PipelineRunSnapshot] = {}
        self.by_key: dict[str, str] = {}

    async def claim_run(
        self,
        *,
        run_id: str,
        idempotency_key: str,
        request: PipelineRunRequest,
        request_hash: str,
    ) -> RunClaim:
        existing_id = self.by_key.get(idempotency_key)
        if existing_id is not None:
            existing = self.by_run_id[existing_id]
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError("idempotency key already binds another request")
            return RunClaim(existing, replayed=True)
        snapshot = PipelineRunSnapshot(
            run_id=run_id,
            request=request,
            request_hash=request_hash,
            status="accepted",
            commands=(PipelineCommand("command-1", "source_prep", "pending"),),
            version=0,
        )
        self.by_key[idempotency_key] = run_id
        self.by_run_id[run_id] = snapshot
        return RunClaim(snapshot, replayed=False)

    async def read_run(self, run_id: str) -> PipelineRunSnapshot | None:
        return self.by_run_id.get(run_id)

    async def claim_resume(self, run_id: str, *, expected_version: int) -> PipelineRunSnapshot:
        snapshot = self.by_run_id.get(run_id)
        if snapshot is None:
            raise PipelineRunNotFoundError(run_id)
        if snapshot.version != expected_version:
            raise StaleRunVersionError(run_id)
        if snapshot.status not in ("accepted", "running") or not any(
            command.status == "pending" for command in snapshot.commands
        ):
            raise ResumeNotAllowedError(run_id)
        resumed = replace(snapshot, version=snapshot.version + 1)
        self.by_run_id[run_id] = resumed
        return resumed

    async def list_reconstructible_runs(self) -> tuple[PipelineRunSnapshot, ...]:
        return tuple(
            snapshot
            for snapshot in self.by_run_id.values()
            if snapshot.status in ("accepted", "running")
        )


class FakeScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, run_id: str) -> None:
        self.enqueued.append(run_id)


class FailOnceScheduler(FakeScheduler):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def enqueue(self, run_id: str) -> None:
        self.enqueued.append(run_id)
        if not self.failed:
            self.failed = True
            raise RuntimeError("durable queue unavailable")


class FakeAuthorizer:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def allows(self, request: PipelineRunRequest) -> bool:
        return self.allowed


def request(source_root: str = "/authorized/source") -> PipelineRunRequest:
    return PipelineRunRequest.from_mapping({"profile": "test", "source_root": source_root})


@pytest.mark.asyncio
async def test_submit_is_durable_before_enqueue_and_replays_same_run() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    service = DurablePipelineRunService(store, scheduler, FakeAuthorizer())

    first = await service.submit(request(), "request-1")
    replay = await service.submit(request(), "request-1")

    assert first.snapshot.run_id == replay.snapshot.run_id
    assert first.replayed is False
    assert replay.replayed is True
    assert scheduler.enqueued == [first.snapshot.run_id, first.snapshot.run_id]
    assert store.by_run_id[first.snapshot.run_id].status == "accepted"


@pytest.mark.asyncio
async def test_replay_repairs_claim_succeeded_enqueue_failed_window() -> None:
    store = FakeRunStore()
    scheduler = FailOnceScheduler()
    service = DurablePipelineRunService(store, scheduler, FakeAuthorizer())

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await service.submit(request(), "request-1")
    persisted_run_id = next(iter(store.by_run_id))

    replay = await service.submit(request(), "request-1")

    assert replay.replayed is True
    assert replay.snapshot.run_id == persisted_run_id
    assert scheduler.enqueued == [persisted_run_id, persisted_run_id]


@pytest.mark.asyncio
async def test_same_key_with_different_canonical_request_is_conflict() -> None:
    service = DurablePipelineRunService(FakeRunStore(), FakeScheduler(), FakeAuthorizer())
    await service.submit(request("/authorized/a"), "request-1")

    with pytest.raises(IdempotencyConflictError):
        await service.submit(request("/authorized/b"), "request-1")


@pytest.mark.asyncio
async def test_status_and_resume_use_the_same_persisted_run() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    service = DurablePipelineRunService(store, scheduler, FakeAuthorizer())
    submitted = await service.submit(request(), "request-1")

    status = await service.status(submitted.snapshot.run_id)
    resumed = await service.resume(submitted.snapshot.run_id, expected_version=0)

    assert status == submitted.snapshot
    assert resumed.run_id == submitted.snapshot.run_id
    assert resumed.version == 1
    assert scheduler.enqueued == [submitted.snapshot.run_id, submitted.snapshot.run_id]
    assert len(store.by_run_id) == 1


@pytest.mark.asyncio
async def test_resume_rejects_terminal_run_without_creating_a_job() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    service = DurablePipelineRunService(store, scheduler, FakeAuthorizer())
    submitted = await service.submit(request(), "request-1")
    run_id = submitted.snapshot.run_id
    store.by_run_id[run_id] = replace(
        submitted.snapshot,
        status="succeeded",
        commands=(PipelineCommand("command-1", "source_prep", "succeeded", uuid4()),),
    )

    with pytest.raises(ResumeNotAllowedError):
        await service.resume(run_id, expected_version=0)

    assert len(store.by_run_id) == 1
    assert scheduler.enqueued == [run_id]


@pytest.mark.asyncio
async def test_concurrent_resume_uses_caller_version_as_cas() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    service = DurablePipelineRunService(store, scheduler, FakeAuthorizer())
    run_id = (await service.submit(request(), "request-1")).snapshot.run_id

    outcomes = await asyncio.gather(
        service.resume(run_id, expected_version=0),
        service.resume(run_id, expected_version=0),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, PipelineRunSnapshot) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, StaleRunVersionError) for outcome in outcomes) == 1
    assert scheduler.enqueued == [run_id, run_id]


@pytest.mark.asyncio
async def test_restart_reconstructs_from_store_not_memory_tasks() -> None:
    store = FakeRunStore()
    first_scheduler = FakeScheduler()
    first_service = DurablePipelineRunService(store, first_scheduler, FakeAuthorizer())
    submitted = await first_service.submit(request(), "request-1")

    restarted_scheduler = FakeScheduler()
    restarted_service = DurablePipelineRunService(store, restarted_scheduler, FakeAuthorizer())
    reconstructed = await restarted_service.reconstruct()

    assert reconstructed == (submitted.snapshot.run_id,)
    assert restarted_scheduler.enqueued == [submitted.snapshot.run_id]


@pytest.mark.asyncio
async def test_unauthorized_source_is_denied_before_persistence() -> None:
    store = FakeRunStore()
    service = DurablePipelineRunService(store, FakeScheduler(), FakeAuthorizer(False))

    with pytest.raises(SourceDeniedError):
        await service.submit(request(), "request-1")

    assert store.by_run_id == {}


class FakeStage:
    def __init__(self) -> None:
        self.commands: list[PipelineCommand] = []

    async def execute(self, command: PipelineCommand) -> PipelineStageResult:
        self.commands.append(command)
        return PipelineStageResult(command.command_id, "indeterminate")


class FakeCommandClaimStore:
    def __init__(self, command: PipelineCommand | None = None) -> None:
        self.command = command or PipelineCommand("command-1", "source_prep", "pending")
        self.claims = 0

    async def claim_next_pending(
        self,
        run_id: str,
        *,
        expected_version: int,
        lease_id: str,
    ) -> PipelineCommand | None:
        del run_id
        self.claims += 1
        if self.command.status != "pending" or self.command.version != expected_version:
            return None
        self.command = replace(
            self.command,
            status="running",
            version=expected_version + 1,
            lease_id=lease_id,
        )
        return self.command


@pytest.mark.asyncio
async def test_fake_stage_uses_closed_registry_port_without_inventing_success() -> None:
    stage = FakeStage()
    registry = PipelineStageRegistry.from_ports(("source_prep", stage), ("vlm", FakeStage()))
    command_store = FakeCommandClaimStore()
    runner = PipelineStageRunner(registry, command_store)

    result = await runner.claim_and_execute(
        "pipeline_run_" + "a" * 32,
        expected_version=0,
        lease_id="lease-1",
    )

    assert result == PipelineStageResult("command-1", "indeterminate")
    assert stage.commands == [
        PipelineCommand("command-1", "source_prep", "running", None, 1, "lease-1")
    ]


@pytest.mark.asyncio
async def test_repeated_enqueue_claims_once_and_does_not_repeat_external_call() -> None:
    stage = FakeStage()
    command_store = FakeCommandClaimStore()
    runner = PipelineStageRunner(
        PipelineStageRegistry.from_ports(("source_prep", stage)),
        command_store,
    )
    run_id = "pipeline_run_" + "a" * 32

    first = await runner.claim_and_execute(run_id, expected_version=0, lease_id="lease-1")
    replay = await runner.claim_and_execute(run_id, expected_version=0, lease_id="lease-2")

    assert first == PipelineStageResult("command-1", "indeterminate")
    assert replay is None
    assert len(stage.commands) == 1


@pytest.mark.asyncio
async def test_claim_then_worker_crash_is_not_blindly_executed_by_reenqueue() -> None:
    stage = FakeStage()
    command_store = FakeCommandClaimStore()
    run_id = "pipeline_run_" + "a" * 32
    claimed = await command_store.claim_next_pending(
        run_id,
        expected_version=0,
        lease_id="crashed-lease",
    )
    assert claimed is not None and claimed.status == "running"
    runner = PipelineStageRunner(
        PipelineStageRegistry.from_ports(("source_prep", stage)),
        command_store,
    )

    result = await runner.claim_and_execute(run_id, expected_version=0, lease_id="new-lease")

    assert result is None
    assert stage.commands == []


@pytest.mark.asyncio
async def test_indeterminate_command_requires_reconciler_and_is_not_executed() -> None:
    stage = FakeStage()
    command_store = FakeCommandClaimStore(
        PipelineCommand("command-1", "source_prep", "indeterminate", None, 1)
    )
    runner = PipelineStageRunner(
        PipelineStageRegistry.from_ports(("source_prep", stage)),
        command_store,
    )

    result = await runner.claim_and_execute(
        "pipeline_run_" + "a" * 32,
        expected_version=1,
        lease_id="lease-2",
    )

    assert result is None
    assert stage.commands == []


def test_request_decoder_is_closed_and_rejects_production() -> None:
    with pytest.raises(ValueError, match="unsupported fields"):
        PipelineRunRequest.from_mapping(
            {"profile": "test", "source_root": "/authorized", "backend": "qwen"}
        )
    with pytest.raises(ValueError, match="profile"):
        PipelineRunRequest.from_mapping({"profile": "production", "source_root": "/authorized"})
    with pytest.raises(ValueError, match="exactly one"):
        PipelineRunRequest.from_mapping(
            {
                "profile": "shadow",
                "source_root": "/authorized",
                "source_reference": "source:123",
            }
        )


def test_canonical_request_hash_is_stable_across_input_key_order() -> None:
    left = PipelineRunRequest.from_mapping({"profile": "test", "source_root": "/authorized"})
    right = PipelineRunRequest.from_mapping({"source_root": "/authorized", "profile": "test"})

    assert left.request_hash == right.request_hash
    assert left.request_hash.startswith("sha256:")


@pytest.mark.parametrize("status", ("succeeded", "denied", "failed"))
def test_terminal_command_and_stage_result_require_receipt(status: str) -> None:
    with pytest.raises(PipelineRunValidationError, match="Receipt"):
        PipelineCommand("command-1", "source_prep", status)  # type: ignore[arg-type]
    with pytest.raises(PipelineRunValidationError, match="Receipt"):
        PipelineStageResult("command-1", status)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("run_status", "command_status"),
    (
        ("succeeded", "pending"),
        ("succeeded", "denied"),
        ("denied", "succeeded"),
        ("failed", "succeeded"),
    ),
)
def test_terminal_run_requires_consistent_terminal_commands(
    run_status: str,
    command_status: str,
) -> None:
    receipt_id = None if command_status == "pending" else uuid4()
    command = PipelineCommand(
        "command-1",
        "source_prep",
        command_status,  # type: ignore[arg-type]
        receipt_id,
    )
    with pytest.raises(PipelineRunValidationError, match="terminal run|succeeded run|must contain"):
        PipelineRunSnapshot(
            "pipeline_run_" + "a" * 32,
            request(),
            request().request_hash,
            run_status,  # type: ignore[arg-type]
            (command,),
            1,
        )


@pytest.mark.parametrize("run_status", ("accepted", "running"))
def test_nonterminal_run_requires_at_least_one_nonterminal_command(run_status: str) -> None:
    command = PipelineCommand("command-1", "source_prep", "succeeded", uuid4())

    with pytest.raises(PipelineRunValidationError, match="nonterminal run"):
        PipelineRunSnapshot(
            "pipeline_run_" + "a" * 32,
            request(),
            request().request_hash,
            run_status,  # type: ignore[arg-type]
            (command,),
            1,
        )
