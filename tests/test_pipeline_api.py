from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from auto_cut_bot.api.server import create_app
from auto_cut_bot.pipeline.runtime import DurablePipelineRunService, PipelineRunValidationError
from tests.pipeline.test_run_service import FakeAuthorizer, FakeRunStore, FakeScheduler


@pytest_asyncio.fixture
async def aiohttp_client():
    clients: list[TestClient] = []

    async def make_client(app) -> TestClient:
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    try:
        yield make_client
    finally:
        for client in clients:
            await client.close()


def _agent() -> MagicMock:
    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="unused")
    return agent


def _service(*, allowed: bool = True):
    store = FakeRunStore()
    scheduler = FakeScheduler()
    return DurablePipelineRunService(store, scheduler, FakeAuthorizer(allowed)), store, scheduler


@pytest.mark.asyncio
async def test_run_returns_202_replay_and_409_mismatch(aiohttp_client) -> None:
    service, _, scheduler = _service()
    client = await aiohttp_client(create_app(_agent(), pipeline_run_service=service))
    headers = {"Idempotency-Key": "run-request-1"}
    payload = {"profile": "test", "source_root": "/authorized/source"}

    first = await client.post("/v1/pipeline/run", headers=headers, json=payload)
    replay = await client.post("/v1/pipeline/run", headers=headers, json=payload)
    conflict = await client.post(
        "/v1/pipeline/run",
        headers=headers,
        json={"profile": "test", "source_root": "/authorized/other"},
    )

    first_body = await first.json()
    replay_body = await replay.json()
    assert first.status == replay.status == 202
    assert first_body["run_id"] == replay_body["run_id"]
    assert first_body["status"] == "accepted"
    assert first_body["replayed"] is False
    assert replay_body["replayed"] is True
    assert conflict.status == 409
    assert scheduler.enqueued == [first_body["run_id"], first_body["run_id"]]


@pytest.mark.asyncio
async def test_status_and_resume_address_run_id(aiohttp_client) -> None:
    service, _, scheduler = _service()
    client = await aiohttp_client(create_app(_agent(), pipeline_run_service=service))
    created = await client.post(
        "/v1/pipeline/run",
        headers={"Idempotency-Key": "run-request-1"},
        json={"profile": "shadow", "source_reference": "source:fixture-1"},
    )
    run_id = (await created.json())["run_id"]

    status = await client.get("/v1/pipeline/status", params={"run_id": run_id})
    resumed = await client.post(
        "/v1/pipeline/resume",
        json={"run_id": run_id, "expected_version": 0},
    )

    status_body = await status.json()
    resume_body = await resumed.json()
    assert status.status == 200
    assert status_body["run_id"] == run_id
    assert status_body["profile"] == "shadow"
    assert status_body["commands"][0]["status"] == "pending"
    assert resumed.status == 202
    assert resume_body["run_id"] == run_id
    assert scheduler.enqueued == [run_id, run_id]


@pytest.mark.asyncio
async def test_pipeline_endpoints_fail_closed_without_service(aiohttp_client) -> None:
    client = await aiohttp_client(create_app(_agent()))

    response = await client.post(
        "/v1/pipeline/run",
        headers={"Idempotency-Key": "run-request-1"},
        json={"profile": "test", "source_root": "/authorized/source"},
    )

    assert response.status == 503


@pytest.mark.asyncio
async def test_run_rejects_invalid_payload_missing_key_and_unauthorized_source(
    aiohttp_client,
) -> None:
    service, _, _ = _service(allowed=False)
    client = await aiohttp_client(create_app(_agent(), pipeline_run_service=service))
    payload = {"profile": "test", "source_root": "/not-authorized"}

    missing_key = await client.post("/v1/pipeline/run", json=payload)
    invalid = await client.post(
        "/v1/pipeline/run",
        headers={"Idempotency-Key": "run-request-1"},
        json={**payload, "force": True},
    )
    denied = await client.post(
        "/v1/pipeline/run",
        headers={"Idempotency-Key": "run-request-1"},
        json=payload,
    )

    assert missing_key.status == 400
    assert invalid.status == 400
    assert denied.status == 403


@pytest.mark.asyncio
async def test_missing_run_is_404_and_bad_resume_is_400(aiohttp_client) -> None:
    service, _, _ = _service()
    client = await aiohttp_client(create_app(_agent(), pipeline_run_service=service))

    missing = await client.get("/v1/pipeline/status", params={"run_id": "pipeline_run_" + "a" * 32})
    malformed = await client.post("/v1/pipeline/resume", json={"session_id": "legacy"})

    assert missing.status == 404
    assert malformed.status == 400


class InvariantBreakingService:
    async def submit(self, *_args, **_kwargs):
        raise PipelineRunValidationError("persisted projection is corrupt")

    async def status(self, *_args, **_kwargs):
        raise PipelineRunValidationError("persisted projection is corrupt")

    async def resume(self, *_args, **_kwargs):
        raise PipelineRunValidationError("persisted projection is corrupt")


@pytest.mark.asyncio
async def test_store_invariant_failure_is_500_not_input_400(aiohttp_client) -> None:
    client = await aiohttp_client(
        create_app(_agent(), pipeline_run_service=InvariantBreakingService())
    )

    response = await client.post(
        "/v1/pipeline/run",
        headers={"Idempotency-Key": "run-request-1"},
        json={"profile": "test", "source_root": "/authorized/source"},
    )

    assert response.status == 500
    assert (await response.json())["error"]["type"] == "server_error"
