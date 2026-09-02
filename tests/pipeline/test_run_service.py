from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from autocut_kernel.store import RuntimeStoreError
from autocut_kernel.vlm import GENERATION_RETRY_STRATEGY_VERSION, GenerationRetryPolicy
from psycopg import OperationalError

from auto_cut_bot.pipeline.debug import FileModelIoDebugSink
from auto_cut_bot.pipeline.runtime import (
    DurablePipelineRunService,
    DurablePipelineWorker,
    FullStageVlmRecomputeBinder,
    IdempotencyConflictError,
    MediaPreflightRecomputeRequest,
    OutboxLease,
    PipelineCommand,
    PipelineExecutionProfile,
    PipelineRunNotFoundError,
    PipelineRunRequest,
    PipelineRunSnapshot,
    PipelineRunValidationError,
    PipelineStageContext,
    PipelineStageIsolationError,
    PipelineStageReconciler,
    PipelineStageRegistry,
    PipelineStageResult,
    PipelineStageRunner,
    ResumeNotAllowedError,
    RunClaim,
    SourceDeniedError,
    StaleRunVersionError,
    VlmFullStageRecomputeRequest,
)
from auto_cut_bot.pipeline.runtime.semantic_authority import (
    load_installed_semantic_run_authority,
)
from auto_cut_bot.pipeline.vlm import (
    DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION,
    DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION,
    DoubaoVlmRequestPolicy,
)
from tests.pipeline.runtime_profile_fixture import (
    execution_profile as frozen_execution_profile,
)
from tests.pipeline.runtime_profile_fixture import (
    media_preflight_policy,
    stage1_command_policy,
    stage2_command_policy,
    stage3_command_policy,
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
        recompute_request: VlmFullStageRecomputeRequest | MediaPreflightRecomputeRequest | None = None,
        defer_activation: bool = False,
    ) -> RunClaim:
        existing_id = self.by_key.get(idempotency_key)
        if existing_id is not None:
            existing = self.by_run_id[existing_id]
            if (
                existing.request_hash != request_hash
                or existing.recompute_request != recompute_request
            ):
                raise IdempotencyConflictError("idempotency key already binds another request")
            return RunClaim(existing, replayed=True)
        # These are control-plane test plans, not semantic acceptance evidence.
        stages = (
            "source_prep",
            "vlm",
            "stage1_narrative",
            "stage2_portfolio",
            "stage3_blueprint",
            "media_preflight",
        )
        if type(recompute_request) is MediaPreflightRecomputeRequest:  # noqa: E721
            stages = ("media_preflight",)
        elif execution_profile.is_semantic_only:
            stages = ("source_prep", "context_prepare", "vlm")
        elif execution_profile.is_semantic_story:
            stages = (
                "source_prep",
                "context_prepare",
                "vlm",
                "stage1_narrative",
                "stage2_portfolio",
                "stage3_blueprint",
            )
        elif execution_profile.schema_version == "pipeline-execution-profile-v2":
            stages = ("source_prep", "vlm")
        elif execution_profile.schema_version == "pipeline-execution-profile-v5":
            stages = ("source_prep", "vlm", "media_preflight")
        command_status = "awaiting_binding" if defer_activation else "pending"
        commands = tuple(
            PipelineCommand(
                f"command-{index}",
                stage,
                command_status,
                blocking_command_id=None,
            )
            for index, stage in enumerate(stages, start=1)
        )
        snapshot = PipelineRunSnapshot(
            run_id=run_id,
            request=request,
            request_hash=request_hash,
            status="accepted",
            commands=commands,
            version=0,
            execution_profile=execution_profile,
            recompute_request=recompute_request,
        )
        self.by_key[idempotency_key] = run_id
        self.by_run_id[run_id] = snapshot
        return RunClaim(snapshot, replayed=False)

    async def activate_recompute(
        self, run_id: str, *, expected_version: int, binding_id: str
    ) -> PipelineRunSnapshot:
        snapshot = self.by_run_id.get(run_id)
        if snapshot is None:
            raise PipelineRunNotFoundError(run_id)
        if snapshot.version != expected_version:
            raise StaleRunVersionError(run_id)
        commands = tuple(
            replace(
                command,
                status="pending",
                version=command.version + 1,
                blocking_command_id=None,
                lease_id=None,
            )
            if command.stage == "media_preflight"
            and command.status == "binding"
            and command.lease_id == binding_id
            else command
            for command in snapshot.commands
        )
        if commands == snapshot.commands:
            raise PipelineRunValidationError("media recompute is already active or not bind-pending")
        activated = replace(snapshot, commands=commands, version=snapshot.version + 1)
        self.by_run_id[run_id] = activated
        return activated

    async def claim_recompute_binding(
        self,
        run_id: str,
        *,
        expected_version: int,
        binding_id: str,
        lease_seconds: int = 300,
    ) -> PipelineRunSnapshot | None:
        snapshot = self.by_run_id.get(run_id)
        if snapshot is None:
            raise PipelineRunNotFoundError(run_id)
        if snapshot.version != expected_version:
            raise StaleRunVersionError(run_id)
        commands = tuple(
            replace(command, status="binding", lease_id=binding_id, version=command.version + 1)
            if command.stage == "media_preflight" and command.status == "awaiting_binding"
            else command
            for command in snapshot.commands
        )
        if commands == snapshot.commands:
            return None
        claimed = replace(snapshot, commands=commands, version=snapshot.version + 1)
        self.by_run_id[run_id] = claimed
        return claimed

    async def release_recompute_binding(
        self, run_id: str, *, expected_version: int, binding_id: str
    ) -> PipelineRunSnapshot:
        snapshot = self.by_run_id.get(run_id)
        if snapshot is None:
            raise PipelineRunNotFoundError(run_id)
        if snapshot.version != expected_version:
            raise StaleRunVersionError(run_id)
        commands = tuple(
            replace(command, status="awaiting_binding", lease_id=None, version=command.version + 1)
            if command.stage == "media_preflight"
            and command.status == "binding"
            and command.lease_id == binding_id
            else command
            for command in snapshot.commands
        )
        if commands == snapshot.commands:
            raise PipelineRunValidationError("binding lease is not owned by this retry")
        released = replace(snapshot, commands=commands, version=snapshot.version + 1)
        self.by_run_id[run_id] = released
        return released

    async def read_run(self, run_id: str) -> PipelineRunSnapshot | None:
        return self.by_run_id.get(run_id)

    async def claim_resume(self, run_id: str, *, expected_version: int) -> PipelineRunSnapshot:
        snapshot = self.by_run_id.get(run_id)
        if snapshot is None:
            raise PipelineRunNotFoundError(run_id)
        if snapshot.version != expected_version:
            raise StaleRunVersionError(run_id)
        if snapshot.status in ("accepted", "running") and any(
            command.status == "pending" for command in snapshot.commands
        ):
            resumed = replace(snapshot, version=snapshot.version + 1)
        elif snapshot.status == "awaiting_calibration":
            waiting = tuple(
                replace(command, status="pending", version=command.version + 1)
                if command.status == "awaiting_calibration"
                else command
                for command in snapshot.commands
            )
            if not any(command.status == "pending" for command in waiting):
                raise ResumeNotAllowedError(run_id)
            resumed = replace(
                snapshot, status="accepted", commands=waiting, version=snapshot.version + 1
            )
        else:
            raise ResumeNotAllowedError(run_id)
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


class FakeFullStageBinder:
    def __init__(self) -> None:
        self.calls: list[tuple[PipelineRunSnapshot, str]] = []

    async def bind(
        self,
        *,
        base: PipelineRunSnapshot,
        target_run_id: str,
        request: VlmFullStageRecomputeRequest,
    ) -> None:
        assert request.base_run_id == base.run_id
        self.calls.append((base, target_run_id))


class FakeMediaPreflightBinder:
    def __init__(self) -> None:
        self.calls: list[tuple[PipelineRunSnapshot, str, int]] = []

    async def bind(
        self,
        *,
        base: PipelineRunSnapshot,
        target_run_id: str,
        request: MediaPreflightRecomputeRequest,
    ) -> None:
        assert request.base_run_id == base.run_id
        self.calls.append((base, target_run_id, request.selected_episode_index))


class FailOnceMediaPreflightBinder(FakeMediaPreflightBinder):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def bind(
        self,
        *,
        base: PipelineRunSnapshot,
        target_run_id: str,
        request: MediaPreflightRecomputeRequest,
    ) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("media evidence provider unavailable")
        await super().bind(base=base, target_run_id=target_run_id, request=request)


class ConcurrentMediaPreflightBinder(FakeMediaPreflightBinder):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active_calls = 0

    async def bind(
        self,
        *,
        base: PipelineRunSnapshot,
        target_run_id: str,
        request: MediaPreflightRecomputeRequest,
    ) -> None:
        self.active_calls += 1
        self.started.set()
        await self.release.wait()
        await super().bind(base=base, target_run_id=target_run_id, request=request)


def request(source_root: str = "/authorized/source") -> PipelineRunRequest:
    return PipelineRunRequest.from_mapping({"profile": "test", "source_root": source_root})


def execution_profile(*, model_id: str = "doubao-seed-2-1-pro-260628") -> PipelineExecutionProfile:
    return frozen_execution_profile(model_id=model_id)


def semantic_execution_profile() -> PipelineExecutionProfile:
    return PipelineExecutionProfile.from_semantic_policies(
        DoubaoVlmRequestPolicy("doubao-seed-2-1-pro-260628"),
        retry_policy=GenerationRetryPolicy(GENERATION_RETRY_STRATEGY_VERSION, 3, (1, 2)),
    )


def semantic_story_execution_profile() -> PipelineExecutionProfile:
    semantic = load_installed_semantic_run_authority()
    return PipelineExecutionProfile.from_semantic_story_policies(
        semantic.vlm_policy,
        retry_policy=semantic.retry_policy,
        stage1_policy=stage1_command_policy(),
        stage2_policy=stage2_command_policy(),
        stage3_policy=stage3_command_policy(),
    )


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
    del mapping["stage1_command_policy"]
    del mapping["stage2_command_policy"]
    del mapping["stage3_command_policy"]
    del mapping["evidence_read_limits"]
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


def _v5_execution_profile() -> PipelineExecutionProfile:
    mapping = execution_profile().to_mapping()
    mapping["schema_version"] = "pipeline-execution-profile-v5"
    del mapping["stage1_command_policy"]
    del mapping["stage2_command_policy"]
    del mapping["stage3_command_policy"]
    del mapping["evidence_read_limits"]
    return PipelineExecutionProfile.from_mapping(mapping)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("submit", "resume", "reconstruct"))
async def test_persisted_v5_cannot_reenter_runtime_or_gain_a_stage1_default(operation):
    store = FakeRunStore()
    old_profile = _v5_execution_profile()
    persisted = await store.claim_run(
        run_id="pipeline_run_" + "e" * 32,
        idempotency_key="historical-v5",
        request=request(),
        request_hash=request().request_hash,
        execution_profile=old_profile,
    )
    scheduler = FakeScheduler()
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(),
        execution_profile=execution_profile(),
    )
    with pytest.raises(PipelineRunValidationError, match="frozen media-preflight"):
        if operation == "submit":
            await service.submit(request(), "historical-v5")
        elif operation == "resume":
            await service.resume(persisted.snapshot.run_id, expected_version=0)
        else:
            await service.reconstruct()
    assert store.by_run_id[persisted.snapshot.run_id] == persisted.snapshot
    assert scheduler.enqueued == []
    assert (await service.status(persisted.snapshot.run_id)).execution_profile == old_profile


@pytest.mark.asyncio
async def test_new_run_cannot_use_historical_v5_profile_before_persistence():
    store = FakeRunStore()
    scheduler = FakeScheduler()
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(),
        execution_profile=_v5_execution_profile(),
    )
    with pytest.raises(PipelineRunValidationError, match="frozen media-preflight"):
        await service.submit(request(), "historical-v5-new")
    assert store.by_run_id == {} and scheduler.enqueued == []


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
    assert tuple(command.stage for command in first.snapshot.commands) == (
        "source_prep",
        "vlm",
        "stage1_narrative",
        "stage2_portfolio",
        "stage3_blueprint",
        "media_preflight",
    )


@pytest.mark.asyncio
async def test_full_vlm_recompute_creates_distinct_semantic_run_after_source_binding() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    profile = semantic_execution_profile()
    base = PipelineRunSnapshot(
        "pipeline_run_" + "b" * 32,
        request(),
        request().request_hash,
        "succeeded",
        (
            PipelineCommand("source", "source_prep", "succeeded", uuid4()),
            PipelineCommand("context", "context_prepare", "succeeded", uuid4()),
            PipelineCommand("vlm", "vlm", "succeeded", uuid4()),
        ),
        7,
        profile,
    )
    store.by_run_id[base.run_id] = base
    binder = FakeFullStageBinder()
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(False),
        execution_profile=profile,
        full_stage_vlm_recompute_binder=binder,
    )
    recompute = VlmFullStageRecomputeRequest(base.run_id, 7)

    first = await service.recompute_full_vlm_stage(recompute, "recompute-1")
    replay = await service.recompute_full_vlm_stage(recompute, "recompute-1")

    assert first.replayed is False and replay.replayed is True
    assert first.snapshot.run_id != base.run_id
    assert tuple(command.stage for command in first.snapshot.commands) == (
        "source_prep",
        "context_prepare",
        "vlm",
    )
    assert [target for _base, target in binder.calls] == [
        first.snapshot.run_id,
        first.snapshot.run_id,
    ]
    # Recompute authorization is the exact successful source binding, not a
    # filesystem catalog lookup on the old host.
    assert scheduler.enqueued == [first.snapshot.run_id, first.snapshot.run_id]


@pytest.mark.asyncio
async def test_media_recompute_creates_media_only_successor_and_replays_idempotently() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    profile = execution_profile()
    base_request = request()
    base = PipelineRunSnapshot(
        "pipeline_run_" + "c" * 32,
        base_request,
        base_request.request_hash,
        "failed",
        (
            PipelineCommand("source", "source_prep", "succeeded", uuid4()),
            PipelineCommand("vlm", "vlm", "succeeded", uuid4()),
            PipelineCommand("media", "media_preflight", "failed", uuid4()),
        ),
        7,
        profile,
    )
    store.by_run_id[base.run_id] = base
    binder = FakeMediaPreflightBinder()
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(False),
        execution_profile=profile,
        media_preflight_recompute_binder=binder,
    )
    recompute = MediaPreflightRecomputeRequest(
        base.run_id,
        7,
        (3,),
        retry_budget=1,
    )

    first = await service.recompute_media_preflight_stage(recompute, "media-recompute-1")
    replay = await service.recompute_media_preflight_stage(recompute, "media-recompute-1")

    assert first.replayed is False and replay.replayed is True
    assert first.snapshot.run_id != base.run_id
    assert tuple(command.stage for command in first.snapshot.commands) == ("media_preflight",)
    assert binder.calls == [(base, first.snapshot.run_id, 2)]
    assert scheduler.enqueued == [first.snapshot.run_id, first.snapshot.run_id]


@pytest.mark.asyncio
async def test_media_recompute_same_idempotency_key_cannot_rebind_selection() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    profile = execution_profile()
    base_request = request()
    base = PipelineRunSnapshot(
        "pipeline_run_" + "4" * 32,
        base_request,
        base_request.request_hash,
        "failed",
        (PipelineCommand("media", "media_preflight", "failed", uuid4()),),
        2,
        profile,
    )
    store.by_run_id[base.run_id] = base
    binder = FakeMediaPreflightBinder()
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(False),
        execution_profile=profile,
        media_preflight_recompute_binder=binder,
    )
    first = MediaPreflightRecomputeRequest(base.run_id, 2, (1,))
    second = MediaPreflightRecomputeRequest(base.run_id, 2, (2,))

    await service.recompute_media_preflight_stage(first, "media-recompute-conflict")
    with pytest.raises(IdempotencyConflictError):
        await service.recompute_media_preflight_stage(second, "media-recompute-conflict")
    assert len(binder.calls) == 1


@pytest.mark.asyncio
async def test_media_recompute_binding_failure_leaves_retryable_reservation() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    profile = execution_profile()
    base_request = request()
    base = PipelineRunSnapshot(
        "pipeline_run_" + "5" * 32,
        base_request,
        base_request.request_hash,
        "failed",
        (PipelineCommand("media", "media_preflight", "failed", uuid4()),),
        3,
        profile,
    )
    store.by_run_id[base.run_id] = base
    binder = FailOnceMediaPreflightBinder()
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(False),
        execution_profile=profile,
        media_preflight_recompute_binder=binder,
    )
    recompute = MediaPreflightRecomputeRequest(base.run_id, 3, (1,))

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await service.recompute_media_preflight_stage(recompute, "media-recompute-retry")
    reserved = next(
        snapshot
        for snapshot in store.by_run_id.values()
        if snapshot.recompute_request == recompute
    )
    assert reserved.status == "accepted"
    assert reserved.commands[0].status == "awaiting_binding"
    assert scheduler.enqueued == []

    retry = await service.recompute_media_preflight_stage(recompute, "media-recompute-retry")
    assert retry.replayed is True
    assert retry.snapshot.commands[0].status == "pending"
    assert scheduler.enqueued == [reserved.run_id]


@pytest.mark.asyncio
async def test_media_recompute_concurrent_exact_replays_converge_on_one_activation() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    profile = execution_profile()
    base_request = request()
    base = PipelineRunSnapshot(
        "pipeline_run_" + "6" * 32,
        base_request,
        base_request.request_hash,
        "failed",
        (PipelineCommand("media", "media_preflight", "failed", uuid4()),),
        5,
        profile,
    )
    store.by_run_id[base.run_id] = base
    binder = ConcurrentMediaPreflightBinder()
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(False),
        execution_profile=profile,
        media_preflight_recompute_binder=binder,
    )
    recompute = MediaPreflightRecomputeRequest(base.run_id, 5, (1,))
    first = asyncio.create_task(
        service.recompute_media_preflight_stage(recompute, "media-recompute-race")
    )
    second = asyncio.create_task(
        service.recompute_media_preflight_stage(recompute, "media-recompute-race")
    )
    await asyncio.wait_for(binder.started.wait(), timeout=1)
    binder.release.set()
    claims = await asyncio.gather(first, second)

    assert all(claim.snapshot.run_id == claims[0].snapshot.run_id for claim in claims)
    assert {claim.snapshot.commands[0].status for claim in claims} == {"binding", "pending"}
    assert len(binder.calls) == 1
    assert scheduler.enqueued == [claims[0].snapshot.run_id]


@pytest.mark.asyncio
async def test_media_recompute_rejects_semantic_only_profile_before_binding() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    profile = semantic_execution_profile()
    base_request = request()
    base = PipelineRunSnapshot(
        "pipeline_run_" + "9" * 32,
        base_request,
        base_request.request_hash,
        "failed",
        (PipelineCommand("vlm", "vlm", "failed", uuid4()),),
        4,
        profile,
    )
    store.by_run_id[base.run_id] = base
    binder = FakeMediaPreflightBinder()
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(False),
        execution_profile=profile,
        media_preflight_recompute_binder=binder,
    )

    with pytest.raises(PipelineRunValidationError, match="media-preflight policy"):
        await service.recompute_media_preflight_stage(
            MediaPreflightRecomputeRequest(base.run_id, 4, (1,)), "media-recompute-2"
        )
    assert binder.calls == []
    assert scheduler.enqueued == []


def test_selected_only_recompute_request_is_closed_and_canonical() -> None:
    run_id = "pipeline_run_" + "e" * 32
    request_value = VlmFullStageRecomputeRequest.from_mapping(
        {
            "base_run_id": run_id,
            "expected_version": 7,
            "stage": "vlm",
            "completion_scope": "selected_only",
            "episode_numbers": [12],
        }
    )

    assert request_value.selected_episode_index == 11
    assert request_value.to_mapping()["episode_numbers"] == [12]
    assert VlmFullStageRecomputeRequest.from_mapping(request_value.to_mapping()) == request_value
    with pytest.raises(PipelineRunValidationError, match="exactly one"):
        VlmFullStageRecomputeRequest(
            run_id, 7, completion_scope="selected_only", episode_numbers=()
        )
    with pytest.raises(PipelineRunValidationError, match="strictly increasing"):
        VlmFullStageRecomputeRequest(
            run_id,
            7,
            completion_scope="selected_only",
            episode_numbers=(2, 2),
        )


@pytest.mark.asyncio
async def test_selected_only_recompute_persists_exact_selection_on_target_run() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    profile = semantic_execution_profile()
    base = PipelineRunSnapshot(
        "pipeline_run_" + "f" * 32,
        request(),
        request().request_hash,
        "failed",
        (
            PipelineCommand("source", "source_prep", "succeeded", uuid4()),
            PipelineCommand("context", "context_prepare", "succeeded", uuid4()),
            PipelineCommand("vlm", "vlm", "failed", uuid4()),
        ),
        9,
        profile,
    )
    store.by_run_id[base.run_id] = base
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(False),
        execution_profile=profile,
        full_stage_vlm_recompute_binder=FakeFullStageBinder(),
    )
    selected = VlmFullStageRecomputeRequest(
        base.run_id,
        9,
        completion_scope="selected_only",
        episode_numbers=(12,),
    )

    claim = await service.recompute_full_vlm_stage(selected, "recompute-episode-12")

    assert claim.snapshot.recompute_request == selected
    assert claim.snapshot.recompute_request.selected_episode_index == 11


@pytest.mark.asyncio
async def test_selected_only_recompute_preserves_semantic_story_plan() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    profile = semantic_story_execution_profile()
    base = PipelineRunSnapshot(
        "pipeline_run_" + "d" * 32,
        request(),
        request().request_hash,
        "failed",
        (
            PipelineCommand("source", "source_prep", "succeeded", uuid4()),
            PipelineCommand("context", "context_prepare", "succeeded", uuid4()),
            PipelineCommand("vlm", "vlm", "failed", uuid4()),
            PipelineCommand("stage1", "stage1_narrative", "blocked", blocking_command_id="vlm"),
            PipelineCommand("stage2", "stage2_portfolio", "blocked", blocking_command_id="vlm"),
            PipelineCommand("stage3", "stage3_blueprint", "blocked", blocking_command_id="vlm"),
        ),
        9,
        profile,
    )
    store.by_run_id[base.run_id] = base
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(False),
        execution_profile=profile,
        full_stage_vlm_recompute_binder=FakeFullStageBinder(),
    )

    claim = await service.recompute_full_vlm_stage(
        VlmFullStageRecomputeRequest(
            base.run_id,
            9,
            completion_scope="selected_only",
            episode_numbers=(1,),
        ),
        "recompute-semantic-story-episode-1",
    )

    assert tuple(command.stage for command in claim.snapshot.commands) == (
        "source_prep",
        "context_prepare",
        "vlm",
        "stage1_narrative",
        "stage2_portfolio",
        "stage3_blueprint",
    )


@pytest.mark.asyncio
async def test_semantic_story_selected_recompute_rejects_multi_episode_source_before_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = semantic_story_execution_profile()
    base = PipelineRunSnapshot(
        "pipeline_run_" + "8" * 32,
        request(),
        request().request_hash,
        "failed",
        (
            PipelineCommand("source", "source_prep", "succeeded", uuid4()),
            PipelineCommand("context", "context_prepare", "succeeded", uuid4()),
            PipelineCommand("vlm", "vlm", "failed", uuid4()),
            PipelineCommand("stage1", "stage1_narrative", "blocked", blocking_command_id="vlm"),
            PipelineCommand("stage2", "stage2_portfolio", "blocked", blocking_command_id="vlm"),
            PipelineCommand("stage3", "stage3_blueprint", "blocked", blocking_command_id="vlm"),
        ),
        9,
        profile,
    )

    class BinderStore:
        def read_outcome(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(state="succeeded")

    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.recompute.read_persisted_prepared_sources_bundle",
        lambda *_args, **_kwargs: SimpleNamespace(
            prepared=SimpleNamespace(episodes=(object(), object()))
        ),
    )
    binder = FullStageVlmRecomputeBinder(BinderStore())  # type: ignore[arg-type]

    with pytest.raises(PipelineRunValidationError, match="one-episode source census"):
        await binder.bind(
            base=base,
            target_run_id="pipeline_run_" + "9" * 32,
            request=VlmFullStageRecomputeRequest(
                base.run_id,
                base.version,
                completion_scope="selected_only",
                episode_numbers=(1,),
            ),
        )


@pytest.mark.asyncio
async def test_full_vlm_recompute_rejects_base_profile_drift_before_binding() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    base_profile = semantic_execution_profile()
    installed_profile = PipelineExecutionProfile.from_semantic_policies(
        DoubaoVlmRequestPolicy("doubao-seed-2-1-pro-260629"),
        retry_policy=GenerationRetryPolicy(GENERATION_RETRY_STRATEGY_VERSION, 3, (1, 2)),
    )
    base = PipelineRunSnapshot(
        "pipeline_run_" + "c" * 32,
        request(),
        request().request_hash,
        "succeeded",
        (PipelineCommand("source", "source_prep", "succeeded", uuid4()),),
        1,
        base_profile,
    )
    store.by_run_id[base.run_id] = base
    binder = FakeFullStageBinder()
    service = DurablePipelineRunService(
        store,
        scheduler,
        FakeAuthorizer(),
        execution_profile=installed_profile,
        full_stage_vlm_recompute_binder=binder,
    )

    with pytest.raises(PipelineRunValidationError, match="installed execution profile differs"):
        await service.recompute_full_vlm_stage(
            VlmFullStageRecomputeRequest(base.run_id, 1), "recompute-2"
        )
    assert binder.calls == [] and scheduler.enqueued == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model_kind", ("vlm", "stage1", "stage2", "stage3"))
async def test_idempotency_replay_keeps_frozen_profile_while_new_key_uses_new_default(
    model_kind,
) -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    first_profile = execution_profile(model_id="doubao-model-v1")
    changed_profile = execution_profile(model_id="doubao-model-v2")
    if model_kind == "stage1":
        first_profile = execution_profile()
        policy = first_profile.build_stage1_command_policy()
        changed_profile = frozen_execution_profile(
            stage1_policy=replace(
                policy,
                generation=replace(policy.generation, model_id="different-stage1-model"),
            )
        )
    elif model_kind == "stage2":
        first_profile = execution_profile()
        policy = first_profile.build_stage2_command_policy()
        changed_profile = frozen_execution_profile(
            stage2_policy=replace(
                policy,
                generation=replace(policy.generation, model_id="different-stage2-model"),
            )
        )
    elif model_kind == "stage3":
        first_profile = execution_profile()
        policy = first_profile.build_stage3_command_policy()
        changed_profile = frozen_execution_profile(
            stage3_policy=replace(
                policy,
                generation=replace(policy.generation, model_id="different-stage3-model"),
            )
        )
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
            stage1_policy=profile.build_stage1_command_policy(),
            stage2_policy=profile.build_stage2_command_policy(),
            stage3_policy=stage3_command_policy(),
            evidence_read_limits=profile.to_evidence_read_limits(),
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


def test_persisted_v2_adapter_profile_reconstructs_without_silent_v3_upgrade() -> None:
    mapping = execution_profile().to_mapping()
    mapping["adapter_strategy_version"] = DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION
    mapping["vlm_stage_strategy_version"] = DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION
    mapping["request_parameters"]["adapter_strategy_version"] = (
        DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION
    )

    historical = PipelineExecutionProfile.from_mapping(mapping)

    assert historical.to_doubao_policy().adapter_strategy_version == (
        DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION
    )


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
    current = execution_profile()
    v2 = _v2_execution_profile()
    v1_mapping = v2.to_mapping()
    v1_mapping["schema_version"] = "pipeline-execution-profile-v1"
    del v1_mapping["generation_retry_policy"]

    v1 = PipelineExecutionProfile.from_mapping(v1_mapping)

    assert v1.to_generation_retry_policy().max_attempts == 1
    assert v1.to_generation_retry_policy().backoff_seconds == ()
    assert v2.to_generation_retry_policy().max_attempts == 3
    assert v2.to_generation_retry_policy().backoff_seconds == (2, 8)
    assert current.to_media_preflight_policy().canonical_hash == current.media_preflight_policy_hash
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
            stage1_policy=profile.build_stage1_command_policy(),
            stage2_policy=profile.build_stage2_command_policy(),
            stage3_policy=stage3_command_policy(),
            evidence_read_limits=profile.to_evidence_read_limits(),
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
                '{"adapter_strategy_version":"doubao-ark-files-responses-stream-v2",'
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
async def test_explicit_resume_wakes_only_awaiting_calibration_via_one_cas() -> None:
    store = FakeRunStore()
    scheduler = FakeScheduler()
    service = DurablePipelineRunService(
        store, scheduler, FakeAuthorizer(), execution_profile=execution_profile()
    )
    submitted = await service.submit(request(), "request-1")
    run_id = submitted.snapshot.run_id
    commands = tuple(
        replace(command, status="awaiting_calibration")
        if command.stage == "media_preflight"
        else replace(command, status="succeeded", receipt_id=uuid4())
        for command in submitted.snapshot.commands
    )
    store.by_run_id[run_id] = replace(
        submitted.snapshot, status="awaiting_calibration", commands=commands, version=4
    )

    resumed = await service.resume(run_id, expected_version=4)

    assert resumed.status == "accepted" and resumed.version == 5
    assert (
        next(command for command in resumed.commands if command.stage == "media_preflight").status
        == "pending"
    )
    assert scheduler.enqueued == [run_id, run_id]


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
        self.isolated_failures: list[PipelineStageIsolationError] = []

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

    async def record_isolated_failure(
        self,
        run_id: str,
        *,
        failure: PipelineStageIsolationError,
    ) -> PipelineStageResult:
        del run_id
        if (
            self.command.command_id != failure.command_id
            or self.command.stage != failure.stage
            or self.command.status != "indeterminate"
            or self.command.version != failure.command_version
        ):
            raise StaleRunVersionError("pipeline_run_" + "a" * 32)
        receipt_id = uuid4()
        result = PipelineStageResult(failure.command_id, "failed", receipt_id)
        self.isolated_failures.append(failure)
        self.results.append(result)
        self.command = replace(
            self.command,
            status="failed",
            receipt_id=receipt_id,
            version=failure.command_version + 1,
        )
        return result


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
async def test_runner_writes_one_stage_input_and_output_directory(tmp_path: Path) -> None:
    stage = FakeStage()
    command_store = FakeCommandClaimStore()
    snapshot = _snapshot(command_store.command)
    sink = FileModelIoDebugSink(tmp_path / "stage-debug")
    runner = PipelineStageRunner(
        PipelineStageRegistry.from_ports(("source_prep", stage)),
        command_store,
        debug_sink=sink,
    )

    result = await runner.claim_and_execute(snapshot, lease_id="lease-1")

    assert result == PipelineStageResult("command-1", "indeterminate")
    stage_directory = sink.root / snapshot.run_id / "source_prep"
    input_value = json.loads((stage_directory / "input.json").read_text(encoding="utf-8"))
    output_value = json.loads((stage_directory / "output.json").read_text(encoding="utf-8"))
    assert input_value["value"]["command"]["command_id"] == "command-1"
    assert output_value["value"]["result"] == {
        "command_id": "command-1",
        "outcome": "indeterminate",
        "receipt_id": None,
    }


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


class FakeCalibrationWaitReconcileStage:
    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult:
        return PipelineStageResult(context.command.command_id, "awaiting_calibration")


class IsolatedVlmMismatchReconcileStage:
    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult:
        raise PipelineStageIsolationError(
            command_id=context.command.command_id,
            command_version=context.command.version,
            stage=context.command.stage,
            failure_code="VLM_BATCH_CHILD_REQUEST_POLICY_MISMATCH",
            failure_detail={
                "declared_episode_count": 50,
                "distinct_policy_count": 2,
                "ordered_policy_hashes_sha256": "sha256:" + "a" * 64,
                "schema_version": "vlm-batch-policy-mismatch-v1",
            },
        )


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
async def test_persisted_vlm_policy_mismatch_terminalizes_only_its_command() -> None:
    command_store = FakeCommandClaimStore(
        PipelineCommand("command-vlm", "vlm", "indeterminate", None, 9)
    )
    reconciler = PipelineStageReconciler.from_ports(
        command_store,
        ("vlm", IsolatedVlmMismatchReconcileStage()),
    )

    result = await reconciler.reconcile(_snapshot(command_store.command))

    assert result is not None and result.outcome == "failed"
    assert command_store.command.status == "failed"
    assert len(command_store.isolated_failures) == 1
    assert command_store.isolated_failures[0].failure_code == (
        "VLM_BATCH_CHILD_REQUEST_POLICY_MISMATCH"
    )


@pytest.mark.asyncio
async def test_indeterminate_command_can_reconcile_to_receiptless_calibration_wait() -> None:
    command_store = FakeCommandClaimStore(
        PipelineCommand("command-1", "media_preflight", "indeterminate", None, 2)
    )
    reconciler = PipelineStageReconciler.from_ports(
        command_store,
        ("media_preflight", FakeCalibrationWaitReconcileStage()),
    )

    result = await reconciler.reconcile(_snapshot(command_store.command))

    assert result == PipelineStageResult("command-1", "awaiting_calibration")
    assert command_store.command.status == "awaiting_calibration"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("execute", "reconcile"))
async def test_historical_stage1_fails_before_any_store_claim_or_stage_call(operation):
    command = PipelineCommand(
        "command-stage1-history",
        "stage1_narrative",
        "pending" if operation == "execute" else "indeterminate",
    )

    class NoStore:
        async def claim_next_pending(self, *args, **kwargs):
            pytest.fail("historical Stage 1 profile reached a Store claim")

        async def read_indeterminate(self, *args, **kwargs):
            pytest.fail("historical Stage 1 profile reached a Store reconcile read")

    snapshot = replace(_snapshot(command), execution_profile=_v5_execution_profile())
    store = NoStore()
    execute_stage, reconcile_stage = FakeStage(), FakeReconcileStage()
    with pytest.raises(PipelineRunValidationError, match="profile v6, v7, v8 or v9"):
        if operation == "execute":
            runner = PipelineStageRunner(
                PipelineStageRegistry.from_ports(("stage1_narrative", execute_stage)),
                store,
            )
            await runner.claim_and_execute(snapshot, lease_id="historical-lease")
        else:
            reconciler = PipelineStageReconciler.from_ports(
                store, ("stage1_narrative", reconcile_stage)
            )
            await reconciler.reconcile(snapshot)
    assert execute_stage.commands == reconcile_stage.commands == []


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
