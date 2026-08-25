from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest
from autocut_kernel.store import RuntimeStoreError
from autocut_kernel.vlm import GENERATION_RETRY_STRATEGY_VERSION
from psycopg import OperationalError
from runtime_profile_fixture import (
    execution_profile as frozen_execution_profile,
)
from runtime_profile_fixture import media_preflight_policy

from auto_cut_bot.pipeline.runtime import (
    DurablePipelineRunService,
    DurablePipelineWorker,
    IdempotencyConflictError,
    OutboxLease,
    PipelineCommand,
    PipelineExecutionProfile,
    PipelineRunNotFoundError,
    PipelineRunRequest,
    PipelineRunSnapshot,
    PipelineRunValidationError,
    PipelineStageContext,
    PipelineStageReconciler,
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
        execution_profile: PipelineExecutionProfile,
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
            commands=(
                PipelineCommand("command-1", "source_prep", "pending"),
                PipelineCommand("command-2", "vlm", "pending"),
            ),
            version=0,
            execution_profile=execution_profile,
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


def execution_profile(*, model_id: str = "doubao-seed-2-1-pro-260628") -> PipelineExecutionProfile:
    return frozen_execution_profile(model_id=model_id)


def _v2_execution_profile() -> PipelineExecutionProfile:
    mapping = execution_profile().to_mapping()
    mapping["schema_version"] = "pipeline-execution-profile-v2"
    mapping["parse_policy"] = {
        "max_observations": 64,
        "max_response_bytes": 64_000,
        "max_summary_characters": 512,
        "max_total_summary_characters": 8_192,
        "minimum_confidence": "0.80",
    }
    del mapping["media_preflight_policy"]
    del mapping["media_preflight_policy_hash"]
    del mapping["materialization_limits"]
    return PipelineExecutionProfile.from_mapping(mapping)


def _snapshot(command: PipelineCommand) -> PipelineRunSnapshot:
    return PipelineRunSnapshot(
        "pipeline_run_" + "a" * 32,
        request(),
        request().request_hash,
        "running" if command.status != "pending" else "accepted",
        (command,),
        0,
        execution_profile(),
    )


@pytest.mark.asyncio
async def test_submit_is_durable_before_enqueue_and_replays_same_run() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    profile = execution_profile()
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(),
        execution_profile=profile,
    )

    first = await service.submit(request(), "request-1")
    replay = await service.submit(request(), "request-1")

    assert first.snapshot.run_id == replay.snapshot.run_id
    assert first.replayed is False
    assert replay.replayed is True
    assert scheduler.enqueued == [first.snapshot.run_id, first.snapshot.run_id]
    assert store.by_run_id[first.snapshot.run_id].status == "accepted"
    assert first.snapshot.execution_profile == profile
    assert first.snapshot.execution_profile_hash == profile.canonical_hash
    assert first.snapshot.to_mapping()["execution_profile_hash"] == profile.canonical_hash


@pytest.mark.asyncio
async def test_idempotency_replay_keeps_frozen_profile_while_new_key_uses_new_default() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    first_profile = execution_profile(model_id="doubao-model-v1")
    changed_profile = execution_profile(model_id="doubao-model-v2")
    first_service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(),
        execution_profile=first_profile,
    )
    changed_service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(),
        execution_profile=changed_profile,
    )

    first = await first_service.submit(request(), "request-frozen")
    replay = await changed_service.submit(request(), "request-frozen")
    changed = await changed_service.submit(request(), "request-new-profile")

    assert replay.replayed is True
    assert replay.snapshot.execution_profile == first.snapshot.execution_profile == first_profile
    assert replay.snapshot.execution_profile_hash == first_profile.canonical_hash
    assert changed.snapshot.execution_profile == changed_profile
    assert changed.snapshot.execution_profile_hash == changed_profile.canonical_hash
    assert first.snapshot.request_hash == changed.snapshot.request_hash
    assert first.snapshot.execution_profile_hash != changed.snapshot.execution_profile_hash


def test_execution_profile_is_closed_canonical_immutable_and_hash_stable() -> None:
    profile = execution_profile()
    reconstructed = PipelineExecutionProfile.from_mapping(profile.to_mapping())

    assert reconstructed == profile
    assert reconstructed.canonical_json == profile.canonical_json
    assert reconstructed.canonical_hash == profile.canonical_hash
    assert profile.canonical_hash.startswith("sha256:")
    assert (
        PipelineExecutionProfile.from_policies(
            profile.to_doubao_policy(),
            profile.to_media_preflight_policy(),
            retry_policy=profile.to_generation_retry_policy(),
            materialization_limits=profile.to_materialization_limits(),
        )
        == profile
    )
    with pytest.raises(FrozenInstanceError):
        profile.model_id = "mutated"  # type: ignore[misc]

    mapping = profile.to_mapping()
    mapping["unknown"] = True
    with pytest.raises(PipelineRunValidationError, match="unsupported fields"):
        PipelineExecutionProfile.from_mapping(mapping)

    with pytest.raises(PipelineRunValidationError, match="canonical JSON"):
        replace(profile, request_parameters_json='{ "temperature": 0 }')


def test_execution_profile_binds_every_closed_materialization_limit() -> None:
    profile = execution_profile()
    mapping = profile.to_mapping()
    limits = mapping["materialization_limits"]
    assert isinstance(limits, dict)
    limits["staging_quota_bytes"] = int(limits["staging_quota_bytes"]) + 1
    changed = PipelineExecutionProfile.from_mapping(mapping)

    assert changed.canonical_hash != profile.canonical_hash
    assert changed.to_materialization_limits().staging_quota_bytes == 16 * 1024 * 1024 + 1

    missing = profile.to_mapping()
    del missing["materialization_limits"]
    with pytest.raises(PipelineRunValidationError, match="missing fields"):
        PipelineExecutionProfile.from_mapping(missing)


def test_execution_profile_v1_remains_one_attempt_and_v2_binds_retry_budget() -> None:
    v4 = execution_profile()
    v2 = _v2_execution_profile()
    v1_mapping = v2.to_mapping()
    v1_mapping["schema_version"] = "pipeline-execution-profile-v1"
    del v1_mapping["generation_retry_policy"]

    v1 = PipelineExecutionProfile.from_mapping(v1_mapping)

    assert v1.to_generation_retry_policy().max_attempts == 1
    assert v1.to_generation_retry_policy().backoff_seconds == ()
    assert v2.to_generation_retry_policy().max_attempts == 3
    assert v2.to_generation_retry_policy().backoff_seconds == (2, 8)
    assert v4.to_media_preflight_policy().canonical_hash == v4.media_preflight_policy_hash
    with pytest.raises(PipelineRunValidationError, match="no frozen media-preflight"):
        v2.to_media_preflight_policy()
    assert v1.canonical_hash != v2.canonical_hash

    invalid_v2 = v2.to_mapping()
    invalid_v2["generation_retry_policy"] = {
        "strategy_version": GENERATION_RETRY_STRATEGY_VERSION,
        "max_attempts": 4,
        "backoff_seconds": [0, 0, 0],
    }
    with pytest.raises(PipelineRunValidationError, match="retry policy is invalid"):
        PipelineExecutionProfile.from_mapping(invalid_v2)


def test_execution_profile_rejects_media_policy_hash_or_capability_tampering() -> None:
    profile = execution_profile()
    tampered = profile.to_mapping()
    tampered["media_preflight_policy_hash"] = "sha256:" + "0" * 64

    with pytest.raises(PipelineRunValidationError, match="hash does not bind"):
        PipelineExecutionProfile.from_mapping(tampered)

    with pytest.raises(PipelineRunValidationError, match="requires exact word timing"):
        PipelineExecutionProfile.from_policies(
            profile.to_doubao_policy(),
            media_preflight_policy(word_timing_capability="sentence_only"),
            retry_policy=profile.to_generation_retry_policy(),
            materialization_limits=profile.to_materialization_limits(),
        )


@pytest.mark.asyncio
async def test_new_run_rejects_v2_execution_profile_before_persistence() -> None:
    store = FakeRunStore()
    service = DurablePipelineRunService(
        store,
        FakeScheduler(),
        FakeAuthorizer(),
        execution_profile=_v2_execution_profile(),
    )

    with pytest.raises(PipelineRunValidationError, match="frozen media-preflight"):
        await service.submit(request(), "legacy-v2-new-run")

    assert store.by_run_id == {}


@pytest.mark.asyncio
async def test_idempotency_replay_rejects_persisted_v2_profile() -> None:
    store = FakeRunStore()
    old_profile = _v2_execution_profile()
    old_claim = await store.claim_run(
        run_id="pipeline_run_" + "b" * 32,
        idempotency_key="legacy-v2-replay",
        request=request(),
        request_hash=request().request_hash,
        execution_profile=old_profile,
    )
    assert old_claim.snapshot.execution_profile == old_profile
    service = DurablePipelineRunService(
        store,
        FakeScheduler(),
        FakeAuthorizer(),
        execution_profile=execution_profile(),
    )

    with pytest.raises(PipelineRunValidationError, match="persisted pipeline run"):
        await service.submit(request(), "legacy-v2-replay")


def test_execution_profile_rejects_open_or_unclosed_embedded_json() -> None:
    profile = execution_profile()
    with pytest.raises(PipelineRunValidationError, match="response_schema_json.*closed"):
        replace(profile, response_schema_json='{"type":"object"}')
    with pytest.raises(PipelineRunValidationError, match="request_parameters_json.*closed"):
        replace(
            profile,
            request_parameters_json=(
                '{"adapter_strategy_version":"doubao-ark-files-responses-stream-v1",'
                '"max_output_tokens":4096,"temperature":0,"unknown":1,"video_fps":1}'
            ),
        )
    with pytest.raises(PipelineRunValidationError, match="parse_policy_json.*closed"):
        replace(profile, parse_policy_json='{"minimum_confidence":"0.80"}')


def test_execution_profile_rejects_unregistered_or_object_shaped_schema_tampering() -> None:
    profile = execution_profile()
    with pytest.raises(PipelineRunValidationError, match="registered Doubao policy"):
        replace(profile, vlm_stage_strategy_version="toy-stage-strategy")
    with pytest.raises(PipelineRunValidationError, match="registered Doubao policy"):
        replace(profile, kernel_parser_strategy_version="toy-parser-strategy")
    with pytest.raises(PipelineRunValidationError, match="registered Doubao policy"):
        replace(
            profile,
            response_schema_json=(
                '{"additionalProperties":false,"properties":{"child":'
                '{"additionalProperties":false,"properties":{}}},"type":"object"}'
            ),
        )


@pytest.mark.asyncio
async def test_replay_repairs_claim_succeeded_enqueue_failed_window() -> None:
    store = FakeRunStore()
    scheduler = FailOnceScheduler()
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(),
        execution_profile=execution_profile(),
    )

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await service.submit(request(), "request-1")
    persisted_run_id = next(iter(store.by_run_id))

    replay = await service.submit(request(), "request-1")

    assert replay.replayed is True
    assert replay.snapshot.run_id == persisted_run_id
    assert scheduler.enqueued == [persisted_run_id, persisted_run_id]


@pytest.mark.asyncio
async def test_same_key_with_different_canonical_request_is_conflict() -> None:
    service = DurablePipelineRunService(
        FakeRunStore(),
        FakeScheduler(),
        FakeAuthorizer(),
        execution_profile=execution_profile(),
    )
    await service.submit(request("/authorized/a"), "request-1")

    with pytest.raises(IdempotencyConflictError):
        await service.submit(request("/authorized/b"), "request-1")


@pytest.mark.asyncio
async def test_status_and_resume_use_the_same_persisted_run() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(),
        execution_profile=execution_profile(),
    )
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
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(),
        execution_profile=execution_profile(),
    )
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
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(),
        execution_profile=execution_profile(),
    )
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
    first_service = DurablePipelineRunService(
        store,
        first_scheduler,
        FakeAuthorizer(),
        execution_profile=execution_profile(),
    )
    submitted = await first_service.submit(request(), "request-1")

    restarted_scheduler = FakeScheduler()
    restarted_service = DurablePipelineRunService(store, restarted_scheduler, FakeAuthorizer())
    reconstructed = await restarted_service.reconstruct()

    assert reconstructed == (submitted.snapshot.run_id,)
    assert restarted_scheduler.enqueued == [submitted.snapshot.run_id]


@pytest.mark.asyncio
async def test_unauthorized_source_is_denied_before_persistence() -> None:
    store = FakeRunStore()
    service = DurablePipelineRunService(
        store,
        FakeScheduler(),
        FakeAuthorizer(False),
        execution_profile=execution_profile(),
    )

    with pytest.raises(SourceDeniedError):
        await service.submit(request(), "request-1")

    assert store.by_run_id == {}


class FakeStage:
    def __init__(self) -> None:
        self.commands: list[PipelineCommand] = []

    async def execute(self, context: PipelineStageContext) -> PipelineStageResult:
        self.commands.append(context.command)
        return PipelineStageResult(context.command.command_id, "indeterminate")


class FakeCommandClaimStore:
    def __init__(self, command: PipelineCommand | None = None) -> None:
        self.command = command or PipelineCommand("command-1", "source_prep", "pending")
        self.claims = 0
        self.results: list[PipelineStageResult] = []

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

    async def record_result(
        self,
        run_id: str,
        *,
        result: PipelineStageResult,
        expected_version: int,
        lease_id: str,
    ) -> None:
        del run_id
        if (
            self.command.status != "running"
            or self.command.version != expected_version
            or self.command.lease_id != lease_id
        ):
            raise StaleRunVersionError("pipeline_run_" + "a" * 32)
        self.results.append(result)
        self.command = replace(
            self.command,
            status=result.outcome,
            receipt_id=result.receipt_id,
            version=expected_version + 1,
            lease_id=None,
        )

    async def renew_running_lease(
        self,
        run_id: str,
        *,
        command_id: str,
        expected_version: int,
        lease_id: str,
    ) -> PipelineCommand:
        del run_id
        if (
            self.command.command_id != command_id
            or self.command.status != "running"
            or self.command.version != expected_version
            or self.command.lease_id != lease_id
        ):
            raise StaleRunVersionError("pipeline_run_" + "a" * 32)
        self.command = replace(self.command, version=expected_version + 1)
        return self.command

    async def read_indeterminate(
        self,
        run_id: str,
        *,
        expected_version: int,
    ) -> PipelineCommand | None:
        del run_id
        if self.command.status != "indeterminate" or self.command.version != expected_version:
            return None
        return self.command

    async def record_reconciled_result(
        self,
        run_id: str,
        *,
        result: PipelineStageResult,
        expected_version: int,
    ) -> None:
        del run_id
        if self.command.status != "indeterminate" or self.command.version != expected_version:
            raise StaleRunVersionError("pipeline_run_" + "a" * 32)
        self.results.append(result)
        self.command = replace(
            self.command,
            status=result.outcome,
            receipt_id=result.receipt_id,
            version=expected_version + 1,
        )


@pytest.mark.asyncio
async def test_fake_stage_uses_closed_registry_port_without_inventing_success() -> None:
    stage = FakeStage()
    registry = PipelineStageRegistry.from_ports(("source_prep", stage), ("vlm", FakeStage()))
    command_store = FakeCommandClaimStore()
    runner = PipelineStageRunner(registry, command_store)

    result = await runner.claim_and_execute(_snapshot(command_store.command), lease_id="lease-1")

    assert result == PipelineStageResult("command-1", "indeterminate")
    assert stage.commands == [
        PipelineCommand("command-1", "source_prep", "running", None, 1, "lease-1")
    ]
    assert command_store.results == [PipelineStageResult("command-1", "indeterminate")]


@pytest.mark.asyncio
async def test_repeated_enqueue_claims_once_and_does_not_repeat_external_call() -> None:
    stage = FakeStage()
    command_store = FakeCommandClaimStore()
    runner = PipelineStageRunner(
        PipelineStageRegistry.from_ports(("source_prep", stage)),
        command_store,
    )
    first = await runner.claim_and_execute(_snapshot(command_store.command), lease_id="lease-1")
    replay = await runner.claim_and_execute(_snapshot(command_store.command), lease_id="lease-2")

    assert first == PipelineStageResult("command-1", "indeterminate")
    assert replay is None
    assert len(stage.commands) == 1


@pytest.mark.asyncio
async def test_legacy_unresolved_profile_never_claims_or_executes_vlm() -> None:
    stage = FakeStage()
    command_store = FakeCommandClaimStore(PipelineCommand("command-vlm", "vlm", "pending"))
    runner = PipelineStageRunner(
        PipelineStageRegistry.from_ports(("vlm", stage)),
        command_store,
    )
    legacy_snapshot = replace(
        _snapshot(command_store.command),
        execution_profile=PipelineExecutionProfile.legacy_unresolved(),
    )

    with pytest.raises(PipelineRunValidationError, match="legacy-unresolved.*VLM"):
        await runner.claim_and_execute(legacy_snapshot, lease_id="lease-legacy")

    assert command_store.claims == 0
    assert stage.commands == []


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

    result = await runner.claim_and_execute(_snapshot(command_store.command), lease_id="new-lease")

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

    result = await runner.claim_and_execute(_snapshot(command_store.command), lease_id="lease-2")

    assert result is None
    assert stage.commands == []


class FakeReconcileStage:
    def __init__(self) -> None:
        self.commands: list[PipelineCommand] = []
        self.receipt_id = uuid4()

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult:
        self.commands.append(context.command)
        return PipelineStageResult(context.command.command_id, "succeeded", self.receipt_id)


@pytest.mark.asyncio
async def test_indeterminate_command_uses_typed_reconcile_without_execute() -> None:
    command_store = FakeCommandClaimStore(
        PipelineCommand("command-1", "source_prep", "indeterminate", None, 2)
    )
    stage = FakeReconcileStage()
    reconciler = PipelineStageReconciler.from_ports(
        command_store,
        ("source_prep", stage),
    )

    result = await reconciler.reconcile(_snapshot(command_store.command))

    assert result == PipelineStageResult("command-1", "succeeded", stage.receipt_id)
    assert stage.commands == [PipelineCommand("command-1", "source_prep", "indeterminate", None, 2)]
    assert command_store.command.status == "succeeded"


@pytest.mark.asyncio
async def test_legacy_unresolved_profile_never_reconciles_vlm() -> None:
    command_store = FakeCommandClaimStore(
        PipelineCommand("command-vlm", "vlm", "indeterminate", None, 2)
    )
    stage = FakeReconcileStage()
    reconciler = PipelineStageReconciler.from_ports(command_store, ("vlm", stage))
    legacy_snapshot = replace(
        _snapshot(command_store.command),
        execution_profile=PipelineExecutionProfile.legacy_unresolved(),
    )

    with pytest.raises(PipelineRunValidationError, match="legacy-unresolved.*VLM"):
        await reconciler.reconcile(legacy_snapshot)

    assert stage.commands == []


class SuccessStage:
    def __init__(self) -> None:
        self.contexts: list[PipelineStageContext] = []
        self.receipt_id = uuid4()

    async def execute(self, context: PipelineStageContext) -> PipelineStageResult:
        self.contexts.append(context)
        return PipelineStageResult(
            context.command.command_id,
            "succeeded",
            self.receipt_id,
        )

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult:
        raise AssertionError(f"unexpected reconcile for {context.command.command_id}")


class WorkerStore(FakeCommandClaimStore):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot = _snapshot(self.command)

    async def read_run(self, run_id: str) -> PipelineRunSnapshot | None:
        return self.snapshot if run_id == self.snapshot.run_id else None

    async def claim_next_pending(self, *args, **kwargs):
        command = await super().claim_next_pending(*args, **kwargs)
        self.snapshot = replace(self.snapshot, status="running", commands=(self.command,))
        return command

    async def renew_running_lease(self, *args, **kwargs):
        command = await super().renew_running_lease(*args, **kwargs)
        self.snapshot = replace(self.snapshot, commands=(self.command,))
        return command

    async def record_result(self, *args, **kwargs) -> None:
        await super().record_result(*args, **kwargs)
        status = self.command.status
        self.snapshot = replace(
            self.snapshot,
            status=status if status in ("succeeded", "denied", "failed") else "running",
            commands=(self.command,),
            version=self.snapshot.version + 1,
        )

    async def expire_running_lease(self, *args, **kwargs):
        raise ResumeNotAllowedError("lease is active")


class RestartFailureStore(WorkerStore):
    def __init__(self) -> None:
        super().__init__()
        self.command = PipelineCommand(
            "command-1",
            "source_prep",
            "running",
            None,
            5,
            "dead-worker-lease",
        )
        self.snapshot = replace(
            _snapshot(self.command),
            status="running",
            commands=(self.command,),
        )
        self.expirations = 0

    async def expire_running_lease(
        self,
        run_id: str,
        *,
        expected_version: int,
        lease_id: str,
    ) -> PipelineCommand:
        assert run_id == self.snapshot.run_id
        assert expected_version == 5
        assert lease_id == "dead-worker-lease"
        self.expirations += 1
        self.command = replace(
            self.command,
            status="indeterminate",
            version=6,
            lease_id=None,
        )
        self.snapshot = replace(
            self.snapshot,
            commands=(self.command,),
            version=self.snapshot.version + 1,
        )
        return self.command

    async def record_reconciled_result(
        self,
        run_id: str,
        *,
        result: PipelineStageResult,
        expected_version: int,
    ) -> None:
        await super().record_reconciled_result(
            run_id,
            result=result,
            expected_version=expected_version,
        )
        self.snapshot = replace(
            self.snapshot,
            status=result.outcome,
            commands=(self.command,),
            version=self.snapshot.version + 1,
        )


class FailedReceiptStage:
    def __init__(self) -> None:
        self.receipt_id = uuid4()
        self.reconciled: list[PipelineStageContext] = []

    async def execute(self, context: PipelineStageContext) -> PipelineStageResult:
        raise AssertionError(f"stale command was re-executed: {context.command.command_id}")

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult:
        self.reconciled.append(context)
        return PipelineStageResult(context.command.command_id, "failed", self.receipt_id)


class WorkerScheduler:
    def __init__(self, run_id: str) -> None:
        self.lease = OutboxLease(uuid4(), run_id, 1, "outbox-lease")
        self.claimed = False
        self.acknowledged: list[OutboxLease] = []
        self.requeued: list[OutboxLease] = []

    async def enqueue(self, run_id: str) -> None:
        del run_id

    async def claim_next(self, *, lease_id: str) -> OutboxLease | None:
        del lease_id
        if self.claimed:
            return None
        self.claimed = True
        return self.lease

    async def acknowledge(self, lease: OutboxLease) -> None:
        self.acknowledged.append(lease)

    async def requeue(self, lease: OutboxLease) -> None:
        self.requeued.append(lease)

    async def renew(self, lease: OutboxLease) -> OutboxLease:
        return lease


class ShortLeaseScheduler:
    def __init__(self, run_ids: tuple[str, ...], *, lease_seconds: float) -> None:
        self._pending = list(run_ids)
        self._lease_seconds = lease_seconds
        self._active: dict[object, tuple[OutboxLease, float]] = {}
        self.acknowledged: list[OutboxLease] = []
        self.claimed_run_ids: list[str] = []
        self.renewals = 0
        self.max_active = 0

    @property
    def pending_run_ids(self) -> tuple[str, ...]:
        return tuple(self._pending)

    async def enqueue(self, run_id: str) -> None:
        self._pending.append(run_id)

    async def claim_next(self, *, lease_id: str) -> OutboxLease | None:
        if not self._pending:
            return None
        lease = OutboxLease(uuid4(), self._pending.pop(0), 1, lease_id)
        self.claimed_run_ids.append(lease.run_id)
        self._active[lease.outbox_id] = (
            lease,
            asyncio.get_running_loop().time() + self._lease_seconds,
        )
        self.max_active = max(self.max_active, len(self._active))
        return lease

    async def renew(self, lease: OutboxLease) -> OutboxLease:
        current, expires_at = self._active[lease.outbox_id]
        assert current == lease
        assert asyncio.get_running_loop().time() < expires_at
        renewed = replace(lease, version=lease.version + 1)
        self._active[lease.outbox_id] = (
            renewed,
            asyncio.get_running_loop().time() + self._lease_seconds,
        )
        self.renewals += 1
        return renewed

    async def acknowledge(self, lease: OutboxLease) -> None:
        current, expires_at = self._active.pop(lease.outbox_id)
        assert current == lease
        assert asyncio.get_running_loop().time() < expires_at
        self.acknowledged.append(lease)

    async def requeue(self, lease: OutboxLease) -> None:
        current, _expires_at = self._active.pop(lease.outbox_id)
        assert current == lease
        self._pending.append(lease.run_id)


class StoppingShortLeaseScheduler(ShortLeaseScheduler):
    def __init__(
        self,
        run_ids: tuple[str, ...],
        *,
        lease_seconds: float,
        stop_event: asyncio.Event,
        stop_after_acknowledgements: int,
    ) -> None:
        super().__init__(run_ids, lease_seconds=lease_seconds)
        self._stop_event = stop_event
        self._stop_after_acknowledgements = stop_after_acknowledgements

    async def acknowledge(self, lease: OutboxLease) -> None:
        await super().acknowledge(lease)
        if len(self.acknowledged) >= self._stop_after_acknowledgements:
            self._stop_event.set()


class SlowWorker(DurablePipelineWorker):
    def __init__(self, *args, delay_seconds: float, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._delay_seconds = delay_seconds
        self.processed: list[str] = []

    async def _process_run(self, run_id: str) -> bool:
        await asyncio.sleep(self._delay_seconds)
        self.processed.append(run_id)
        return True


class OneShotDatabaseFailureWorker(DurablePipelineWorker):
    def __init__(self, *args, failing_run_id: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._failing_run_id = failing_run_id
        self._failed = False
        self.processed: list[str] = []

    async def _process_run(self, run_id: str) -> bool:
        if run_id == self._failing_run_id and not self._failed:
            self._failed = True
            raise OperationalError("one-shot database outage")
        self.processed.append(run_id)
        return True


class ArbitraryRuntimeWrapperFailureWorker(DurablePipelineWorker):
    async def _process_run(self, run_id: str) -> bool:
        del run_id
        try:
            raise OperationalError("database outage")
        except OperationalError as error:
            raise RuntimeError("unapproved application wrapper") from error


class ReconcileWorkerStore(WorkerStore):
    def __init__(self, failing_run_id: str, successful_run_id: str) -> None:
        super().__init__()
        self.command = PipelineCommand(
            "command-reconcile",
            "source_prep",
            "indeterminate",
            None,
            2,
        )
        failing = replace(
            _snapshot(self.command),
            run_id=failing_run_id,
            status="running",
        )
        succeeded_command = PipelineCommand(
            "command-succeeded",
            "source_prep",
            "succeeded",
            uuid4(),
            1,
        )
        successful = replace(
            _snapshot(PipelineCommand("command-succeeded", "source_prep", "pending")),
            run_id=successful_run_id,
            status="succeeded",
            commands=(succeeded_command,),
        )
        self.snapshots = {
            failing_run_id: failing,
            successful_run_id: successful,
        }

    async def read_run(self, run_id: str) -> PipelineRunSnapshot | None:
        return self.snapshots.get(run_id)

    async def record_reconciled_result(
        self,
        run_id: str,
        *,
        result: PipelineStageResult,
        expected_version: int,
    ) -> None:
        await super().record_reconciled_result(
            run_id,
            result=result,
            expected_version=expected_version,
        )
        snapshot = self.snapshots[run_id]
        self.snapshots[run_id] = replace(
            snapshot,
            status=result.outcome,
            commands=(self.command,),
            version=snapshot.version + 1,
        )


class OneShotWrappedFailureReconcileStage(FakeReconcileStage):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult:
        self.attempts += 1
        if self.attempts == 1:
            try:
                raise OperationalError("one-shot database outage")
            except OperationalError as error:
                raise RuntimeStoreError("kernel store operation failed") from error
        return await super().reconcile(context)


@pytest.mark.asyncio
async def test_bounded_worker_passes_strict_context_and_acks_terminal_run() -> None:
    store = WorkerStore()
    scheduler = WorkerScheduler(store.snapshot.run_id)
    stage = SuccessStage()
    registry = PipelineStageRegistry.from_ports(("source_prep", stage))
    runner = PipelineStageRunner(registry, store, heartbeat_seconds=60)
    reconciler = PipelineStageReconciler.from_ports(store, ("source_prep", stage))
    service = DurablePipelineRunService(store, scheduler, FakeAuthorizer())  # type: ignore[arg-type]
    worker = DurablePipelineWorker(
        worker_id="worker-1",
        service=service,
        scheduler=scheduler,
        store=store,
        runner=runner,
        reconciler=reconciler,
        concurrency=1,
        max_batch_size=1,
    )

    assert await worker.run_once() == 1
    assert len(stage.contexts) == 1
    assert stage.contexts[0].run_id == store.snapshot.run_id
    assert stage.contexts[0].request == store.snapshot.request
    assert stage.contexts[0].execution_profile == store.snapshot.execution_profile
    assert stage.contexts[0].execution_profile_hash == store.snapshot.execution_profile_hash
    assert scheduler.acknowledged == [scheduler.lease]
    assert scheduler.requeued == []


@pytest.mark.asyncio
async def test_restarted_worker_projects_failed_receipt_after_stale_running_lease() -> None:
    store = RestartFailureStore()
    scheduler = WorkerScheduler(store.snapshot.run_id)
    stage = FailedReceiptStage()
    registry = PipelineStageRegistry.from_ports(("source_prep", stage))
    worker = DurablePipelineWorker(
        worker_id="replacement-worker",
        service=DurablePipelineRunService(  # type: ignore[arg-type]
            store,
            scheduler,
            FakeAuthorizer(),
        ),
        scheduler=scheduler,
        store=store,
        runner=PipelineStageRunner(registry, store, heartbeat_seconds=60),
        reconciler=PipelineStageReconciler.from_ports(
            store,
            ("source_prep", stage),
        ),
        concurrency=1,
        max_batch_size=1,
    )

    assert await worker.run_once() == 1
    assert store.expirations == 1
    assert len(stage.reconciled) == 1
    assert stage.reconciled[0].command.status == "indeterminate"
    assert store.snapshot.status == "failed"
    assert store.snapshot.commands[0].status == "failed"
    assert store.snapshot.commands[0].receipt_id == stage.receipt_id
    assert scheduler.acknowledged == [scheduler.lease]
    assert scheduler.requeued == []


@pytest.mark.asyncio
async def test_worker_claims_only_from_free_slots_and_heartbeats_short_leases() -> None:
    run_ids = tuple(f"pipeline_run_{index:032x}" for index in range(1, 4))
    scheduler = ShortLeaseScheduler(run_ids, lease_seconds=0.03)
    store = WorkerStore()
    stage = SuccessStage()
    registry = PipelineStageRegistry.from_ports(("source_prep", stage))
    runner = PipelineStageRunner(registry, store, heartbeat_seconds=60)
    reconciler = PipelineStageReconciler.from_ports(store, ("source_prep", stage))
    service = DurablePipelineRunService(store, scheduler, FakeAuthorizer())  # type: ignore[arg-type]
    worker = SlowWorker(
        worker_id="slow-worker",
        service=service,
        scheduler=scheduler,
        store=store,
        runner=runner,
        reconciler=reconciler,
        concurrency=1,
        max_batch_size=3,
        outbox_heartbeat_seconds=0.005,
        delay_seconds=0.06,
    )

    assert await worker.run_once() == 3
    assert worker.processed == list(run_ids)
    assert scheduler.max_active == 1
    assert scheduler.renewals >= 3
    assert len(scheduler.acknowledged) == 3


@pytest.mark.asyncio
async def test_worker_shutdown_after_completion_leaves_later_work_unclaimed() -> None:
    first_run_id = "pipeline_run_" + "1" * 32
    later_run_id = "pipeline_run_" + "2" * 32
    stop_event = asyncio.Event()
    scheduler = StoppingShortLeaseScheduler(
        (first_run_id, later_run_id),
        lease_seconds=0.1,
        stop_event=stop_event,
        stop_after_acknowledgements=1,
    )
    store = WorkerStore()
    stage = SuccessStage()
    registry = PipelineStageRegistry.from_ports(("source_prep", stage))
    runner = PipelineStageRunner(registry, store, heartbeat_seconds=60)
    reconciler = PipelineStageReconciler.from_ports(store, ("source_prep", stage))
    service = DurablePipelineRunService(store, scheduler, FakeAuthorizer())  # type: ignore[arg-type]
    worker = SlowWorker(
        worker_id="draining-worker",
        service=service,
        scheduler=scheduler,
        store=store,
        runner=runner,
        reconciler=reconciler,
        concurrency=1,
        max_batch_size=2,
        delay_seconds=0.001,
    )

    await asyncio.wait_for(
        worker.run_forever(stop_event, poll_interval_seconds=0.001),
        timeout=1,
    )

    assert worker.processed == [first_run_id]
    assert scheduler.claimed_run_ids == [first_run_id]
    assert scheduler.pending_run_ids == (later_run_id,)


@pytest.mark.asyncio
async def test_recoverable_lease_failure_does_not_abort_unrelated_slot() -> None:
    failing_run_id = "pipeline_run_" + "1" * 32
    successful_run_id = "pipeline_run_" + "2" * 32
    scheduler = ShortLeaseScheduler(
        (failing_run_id, successful_run_id),
        lease_seconds=0.1,
    )
    store = WorkerStore()
    stage = SuccessStage()
    registry = PipelineStageRegistry.from_ports(("source_prep", stage))
    runner = PipelineStageRunner(registry, store, heartbeat_seconds=60)
    reconciler = PipelineStageReconciler.from_ports(store, ("source_prep", stage))
    service = DurablePipelineRunService(store, scheduler, FakeAuthorizer())  # type: ignore[arg-type]
    worker = OneShotDatabaseFailureWorker(
        worker_id="recoverable-worker",
        service=service,
        scheduler=scheduler,
        store=store,
        runner=runner,
        reconciler=reconciler,
        concurrency=2,
        max_batch_size=2,
        outbox_heartbeat_seconds=0.01,
        failing_run_id=failing_run_id,
    )

    assert await worker.run_once() == 2
    assert worker.processed == [successful_run_id]
    assert [lease.run_id for lease in scheduler.acknowledged] == [successful_run_id]


@pytest.mark.asyncio
async def test_wrapped_reconcile_database_failure_requeues_and_continues() -> None:
    failing_run_id = "pipeline_run_" + "3" * 32
    successful_run_id = "pipeline_run_" + "4" * 32
    stop_event = asyncio.Event()
    scheduler = StoppingShortLeaseScheduler(
        (failing_run_id, successful_run_id),
        lease_seconds=0.1,
        stop_event=stop_event,
        stop_after_acknowledgements=2,
    )
    store = ReconcileWorkerStore(failing_run_id, successful_run_id)
    execute_stage = SuccessStage()
    reconcile_stage = OneShotWrappedFailureReconcileStage()
    runner = PipelineStageRunner(
        PipelineStageRegistry.from_ports(("source_prep", execute_stage)),
        store,
        heartbeat_seconds=60,
    )
    reconciler = PipelineStageReconciler.from_ports(
        store,
        ("source_prep", reconcile_stage),
    )
    service = DurablePipelineRunService(store, scheduler, FakeAuthorizer())  # type: ignore[arg-type]
    worker = DurablePipelineWorker(
        worker_id="wrapped-recovery-worker",
        service=service,
        scheduler=scheduler,
        store=store,
        runner=runner,
        reconciler=reconciler,
        concurrency=1,
        max_batch_size=2,
        outbox_heartbeat_seconds=0.01,
    )

    await asyncio.wait_for(
        worker.run_forever(stop_event, poll_interval_seconds=0.001),
        timeout=1,
    )

    assert reconcile_stage.attempts == 2
    assert scheduler.claimed_run_ids == [
        failing_run_id,
        successful_run_id,
        failing_run_id,
    ]
    assert [lease.run_id for lease in scheduler.acknowledged] == [
        successful_run_id,
        failing_run_id,
    ]
    assert scheduler.pending_run_ids == ()


@pytest.mark.asyncio
async def test_arbitrary_runtime_wrapper_is_not_recoverable() -> None:
    store = WorkerStore()
    scheduler = WorkerScheduler(store.snapshot.run_id)
    stage = SuccessStage()
    registry = PipelineStageRegistry.from_ports(("source_prep", stage))
    runner = PipelineStageRunner(registry, store, heartbeat_seconds=60)
    reconciler = PipelineStageReconciler.from_ports(store, ("source_prep", stage))
    service = DurablePipelineRunService(store, scheduler, FakeAuthorizer())  # type: ignore[arg-type]
    worker = ArbitraryRuntimeWrapperFailureWorker(
        worker_id="fatal-wrapper-worker",
        service=service,
        scheduler=scheduler,
        store=store,
        runner=runner,
        reconciler=reconciler,
        concurrency=1,
        max_batch_size=1,
    )

    with pytest.raises(RuntimeError, match="unapproved application wrapper"):
        await worker.run_once()

    assert scheduler.requeued == []


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
