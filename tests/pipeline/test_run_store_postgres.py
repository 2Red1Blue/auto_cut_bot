from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer

from auto_cut_bot.api.server import create_app
from auto_cut_bot.pipeline.runtime import (
    DurablePipelineRunService,
    IdempotencyConflictError,
    PipelineRunRequest,
    PipelineStageReconciler,
    PipelineStageResult,
    PostgresPipelineRunStore,
    PostgresPipelineScheduler,
    SourceDeniedError,
    StaleRunVersionError,
)
from auto_cut_bot.pipeline.runtime.composition import (
    PIPELINE_POSTGRES_DSN_ENV,
    PIPELINE_SOURCE_ROOTS_ENV,
    ConfiguredSourceAuthority,
)

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set AUTOCUT_TEST_POSTGRES_DSN to a disposable PostgreSQL database",
)


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            for migration in sorted(
                Path("packages/autocut-kernel/migrations").glob("*.sql")
            ):
                cursor.execute(migration.read_text(encoding="utf-8"))


def _composition(source_root: Path):
    assert DSN is not None

    def factory():
        return psycopg.connect(DSN)

    store = PostgresPipelineRunStore(factory)
    scheduler = PostgresPipelineScheduler(factory)
    authority = ConfiguredSourceAuthority((source_root,), frozenset({"source:fixture-1"}))
    return DurablePipelineRunService(store, scheduler, authority), store, scheduler


@pytest.mark.asyncio
async def test_claim_and_outbox_are_atomic_and_reconstruct_after_restart(tmp_path: Path) -> None:
    service, store, scheduler = _composition(tmp_path)
    request = PipelineRunRequest.from_mapping(
        {"profile": "test", "source_root": str(tmp_path / "input")}
    )

    first = await service.submit(request, "postgres-request-1")
    replay = await service.submit(request, "postgres-request-1")

    assert replay.replayed is True
    assert replay.snapshot.run_id == first.snapshot.run_id
    assert replay.snapshot.commands[0].stage == "source_prep"
    assert replay.snapshot.commands[0].status == "pending"
    assert replay.snapshot.commands[0].receipt_id is None
    assert await scheduler.pending_run_ids() == (first.snapshot.run_id,)

    restarted = DurablePipelineRunService(
        PostgresPipelineRunStore(store.connection_factory),
        PostgresPipelineScheduler(store.connection_factory),
        ConfiguredSourceAuthority((tmp_path,), frozenset()),
    )
    assert await restarted.status(first.snapshot.run_id) == first.snapshot
    assert await restarted.reconstruct() == (first.snapshot.run_id,)


@pytest.mark.asyncio
async def test_postgres_idempotency_conflict_and_resume_cas(tmp_path: Path) -> None:
    service, _store, scheduler = _composition(tmp_path)
    first_request = PipelineRunRequest.from_mapping(
        {"profile": "shadow", "source_reference": "source:fixture-1"}
    )
    first = await service.submit(first_request, "postgres-request-1")

    with pytest.raises(IdempotencyConflictError):
        await service.submit(
            PipelineRunRequest.from_mapping(
                {"profile": "test", "source_root": str(tmp_path / "different")}
            ),
            "postgres-request-1",
        )

    resumed = await service.resume(first.snapshot.run_id, expected_version=0)
    assert resumed.run_id == first.snapshot.run_id
    assert resumed.version == 1
    assert resumed.commands[0].status == "pending"
    with pytest.raises(StaleRunVersionError):
        await service.resume(first.snapshot.run_id, expected_version=0)
    assert await scheduler.pending_run_ids() == (first.snapshot.run_id,)


@pytest.mark.asyncio
async def test_source_authority_denies_before_postgres_claim(tmp_path: Path) -> None:
    service, store, _scheduler = _composition(tmp_path)

    with pytest.raises(SourceDeniedError):
        await service.submit(
            PipelineRunRequest.from_mapping(
                {"profile": "test", "source_root": str(tmp_path.parent / "outside")}
            ),
            "postgres-request-1",
        )

    assert await store.list_reconstructible_runs() == ()


@pytest.mark.asyncio
async def test_leased_result_and_receipt_are_one_cas_projection(tmp_path: Path) -> None:
    service, store, _scheduler = _composition(tmp_path)
    submitted = await service.submit(
        PipelineRunRequest.from_mapping(
            {"profile": "test", "source_root": str(tmp_path / "input")}
        ),
        "postgres-request-1",
    )
    run_id = submitted.snapshot.run_id
    command = await store.claim_next_pending(
        run_id,
        expected_version=0,
        lease_id="worker-lease-1",
    )
    assert command is not None
    receipt_id = uuid4()

    await store.record_result(
        run_id,
        result=PipelineStageResult(command.command_id, "succeeded", receipt_id),
        expected_version=1,
        lease_id="worker-lease-1",
    )

    restarted = PostgresPipelineRunStore(store.connection_factory)
    projected = await restarted.read_run(run_id)
    assert projected is not None
    assert projected.status == "succeeded"
    assert projected.commands[0].status == "succeeded"
    assert projected.commands[0].receipt_id == receipt_id
    with pytest.raises(StaleRunVersionError):
        await restarted.record_result(
            run_id,
            result=PipelineStageResult(command.command_id, "succeeded", receipt_id),
            expected_version=1,
            lease_id="worker-lease-1",
        )


class _RecoveredStage:
    def __init__(self) -> None:
        self.calls = 0
        self.receipt_id = uuid4()

    async def reconcile(self, command) -> PipelineStageResult:
        self.calls += 1
        return PipelineStageResult(command.command_id, "succeeded", self.receipt_id)


@pytest.mark.asyncio
async def test_expired_lease_resumes_only_through_reconciler(tmp_path: Path) -> None:
    assert DSN is not None

    def factory():
        return psycopg.connect(DSN)

    store = PostgresPipelineRunStore(factory, lease_seconds=1)
    scheduler = PostgresPipelineScheduler(factory)
    service = DurablePipelineRunService(
        store,
        scheduler,
        ConfiguredSourceAuthority((tmp_path,), frozenset()),
    )
    submitted = await service.submit(
        PipelineRunRequest.from_mapping(
            {"profile": "test", "source_root": str(tmp_path / "input")}
        ),
        "postgres-request-1",
    )
    run_id = submitted.snapshot.run_id
    claimed = await store.claim_next_pending(
        run_id,
        expected_version=0,
        lease_id="crashed-worker-lease",
    )
    assert claimed is not None
    await asyncio.sleep(1.05)

    with pytest.raises(StaleRunVersionError):
        await store.record_result(
            run_id,
            result=PipelineStageResult(claimed.command_id, "succeeded", uuid4()),
            expected_version=1,
            lease_id="crashed-worker-lease",
        )
    expired = await store.expire_running_lease(
        run_id,
        expected_version=1,
        lease_id="crashed-worker-lease",
    )
    assert expired.status == "indeterminate"
    assert expired.version == 2
    with pytest.raises(StaleRunVersionError):
        await store.record_result(
            run_id,
            result=PipelineStageResult(claimed.command_id, "succeeded", uuid4()),
            expected_version=1,
            lease_id="crashed-worker-lease",
        )

    resumed = await service.resume(run_id, expected_version=2)
    assert resumed.commands[0].status == "indeterminate"
    assert resumed.version == 3
    assert await service.reconstruct() == (run_id,)

    recovered_stage = _RecoveredStage()
    reconciler = PipelineStageReconciler.from_ports(
        PostgresPipelineRunStore(factory),
        ("source_prep", recovered_stage),
    )
    result = await reconciler.reconcile(run_id, expected_version=2)
    assert result is not None
    assert recovered_stage.calls == 1
    projected = await store.read_run(run_id)
    assert projected is not None
    assert projected.status == "succeeded"
    assert projected.commands[0].receipt_id == recovered_stage.receipt_id


def _agent() -> MagicMock:
    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="unused")
    return agent


@pytest.mark.asyncio
async def test_real_http_run_status_resume_survive_app_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DSN is not None
    monkeypatch.setenv(PIPELINE_POSTGRES_DSN_ENV, DSN)
    monkeypatch.setenv(PIPELINE_SOURCE_ROOTS_ENV, str(tmp_path))
    headers = {"Idempotency-Key": "http-postgres-request-1"}
    payload = {"profile": "test", "source_root": str(tmp_path / "input")}

    first_client = TestClient(TestServer(create_app(_agent())))
    await first_client.start_server()
    created = await first_client.post("/v1/pipeline/run", headers=headers, json=payload)
    assert created.status == 202
    created_body = await created.json()
    run_id = created_body["run_id"]
    await first_client.close()

    restarted_client = TestClient(TestServer(create_app(_agent())))
    await restarted_client.start_server()
    replay = await restarted_client.post("/v1/pipeline/run", headers=headers, json=payload)
    status = await restarted_client.get("/v1/pipeline/status", params={"run_id": run_id})
    resumed = await restarted_client.post(
        "/v1/pipeline/resume",
        json={"run_id": run_id, "expected_version": 0},
    )
    try:
        assert replay.status == 202
        assert (await replay.json())["run_id"] == run_id
        assert (await replay.json())["replayed"] is True
        assert status.status == 200
        status_body = await status.json()
        assert status_body["commands"] == [
            {
                "command_id": status_body["commands"][0]["command_id"],
                "stage": "source_prep",
                "status": "pending",
                "receipt_id": None,
                "version": 0,
                "lease_id": None,
            }
        ]
        assert resumed.status == 202
        assert (await resumed.json())["version"] == 1
    finally:
        await restarted_client.close()
