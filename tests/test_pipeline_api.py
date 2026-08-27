from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from auto_cut_bot.api import server as pipeline_server
from auto_cut_bot.api.server import create_app, create_pipeline_app
from auto_cut_bot.pipeline.runtime import DurablePipelineRunService, PipelineRunValidationError
from tests.pipeline.runtime_profile_fixture import execution_profile
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
    """Typed v5 request fixture with fake Store; not accepted installed authority."""
    store = FakeRunStore()
    scheduler = FakeScheduler()
    service = DurablePipelineRunService(
        store, scheduler, FakeAuthorizer(allowed), execution_profile=execution_profile(),
    )
    return service, store, scheduler


class FakePipelineRuntime:
    def __init__(self, service) -> None:
        self.service = service
        self.startup_calls = 0
        self.worker_started = asyncio.Event()
        self.worker_stopped = asyncio.Event()

    async def startup_reconstruct(self) -> tuple[str, ...]:
        self.startup_calls += 1
        return ("pipeline_run_" + "a" * 32,)

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        assert poll_interval_seconds > 0
        self.worker_started.set()
        try:
            await stop_event.wait()
        finally:
            self.worker_stopped.set()


class FailingPipelineRuntime(FakePipelineRuntime):
    async def run_forever(
        self,
        stop_event: asyncio.Event,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        del stop_event, poll_interval_seconds
        self.worker_started.set()
        raise RuntimeError("provider-secret-must-not-escape")


class DrainingPipelineRuntime(FakePipelineRuntime):
    def __init__(self, service) -> None:
        super().__init__(service)
        self.stop_seen = asyncio.Event()
        self.allow_work_to_finish = asyncio.Event()

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        assert poll_interval_seconds > 0
        self.worker_started.set()
        await stop_event.wait()
        self.stop_seen.set()
        await self.allow_work_to_finish.wait()
        self.worker_stopped.set()


@pytest.mark.asyncio
async def test_run_returns_202_replay_and_409_mismatch(aiohttp_client) -> None:
    service, store, scheduler = _service()
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
    persisted_profile = store.by_run_id[first_body["run_id"]].execution_profile
    assert persisted_profile.to_mapping()["schema_version"] == "pipeline-execution-profile-v9"
    assert persisted_profile == execution_profile()


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
async def test_injected_runtime_reconstructs_polls_and_stops_cleanly() -> None:
    service, _, _ = _service()
    runtime = FakePipelineRuntime(service)
    client = TestClient(
        TestServer(create_app(_agent(), pipeline_runtime=runtime))
    )

    await client.start_server()
    await asyncio.wait_for(runtime.worker_started.wait(), timeout=1)
    assert runtime.startup_calls == 1
    await client.close()

    assert runtime.worker_stopped.is_set()


@pytest.mark.asyncio
async def test_pipeline_only_app_needs_no_agent_and_exposes_no_chat_route(
    aiohttp_client,
) -> None:
    service, _, _ = _service()
    runtime = FakePipelineRuntime(service)
    client = await aiohttp_client(
        create_pipeline_app(api_key="pipeline-only-secret", pipeline_runtime=runtime)
    )
    await asyncio.wait_for(runtime.worker_started.wait(), timeout=1)

    missing = await client.post(
        "/v1/pipeline/run",
        headers={"Idempotency-Key": "pipeline-only-1"},
        json={"profile": "test", "source_root": "/authorized/source"},
    )
    accepted = await client.post(
        "/v1/pipeline/run",
        headers={
            "Authorization": "Bearer pipeline-only-secret",
            "Idempotency-Key": "pipeline-only-1",
        },
        json={"profile": "test", "source_root": "/authorized/source"},
    )
    chat = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer pipeline-only-secret"},
        json={},
    )

    assert missing.status == 401
    assert accepted.status == 202
    assert chat.status == 404


@pytest.mark.asyncio
async def test_pipeline_cleanup_stops_polling_then_drains_in_flight_work() -> None:
    service, _, _ = _service()
    runtime = DrainingPipelineRuntime(service)
    client = TestClient(TestServer(create_app(_agent(), pipeline_runtime=runtime)))

    await client.start_server()
    await asyncio.wait_for(runtime.worker_started.wait(), timeout=1)
    close_task = asyncio.create_task(client.close())
    await asyncio.wait_for(runtime.stop_seen.wait(), timeout=1)

    assert not close_task.done()
    assert not runtime.worker_stopped.is_set()
    runtime.allow_work_to_finish.set()
    await asyncio.wait_for(close_task, timeout=1)
    assert runtime.worker_stopped.is_set()


@pytest.mark.asyncio
async def test_environment_paid_runtime_requires_configured_http_auth(
    aiohttp_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, _ = _service()
    runtime = FakePipelineRuntime(service)
    # Isolate server authentication from installed-resource/Store activation.
    # This explicit fake proves neither calibration nor deployed authority.
    composition = MagicMock(return_value=runtime)
    monkeypatch.setattr(
        pipeline_server,
        "compose_pipeline_runtime_from_environment",
        composition,
    )
    payload = {"profile": "test", "source_root": "/authorized/source"}
    headers = {"Idempotency-Key": "paid-run-1"}

    with pytest.raises(
        ValueError,
        match="requires configured HTTP API authentication",
    ):
        create_app(_agent())
    assert runtime.startup_calls == 0
    composition.assert_called_once_with()

    authenticated_client = await aiohttp_client(
        create_app(_agent(), api_key="configured-http-secret")
    )
    missing = await authenticated_client.post(
        "/v1/pipeline/run", headers=headers, json=payload
    )
    assert missing.status == 401
    assert store.by_run_id == {}
    accepted = await authenticated_client.post(
        "/v1/pipeline/run",
        headers={**headers, "Authorization": "Bearer configured-http-secret"},
        json=payload,
    )

    assert missing.status == 401
    assert accepted.status == 202
    assert "configured-http-secret" not in str(await missing.json())
    assert composition.call_count == 2


@pytest.mark.asyncio
async def test_pipeline_worker_failure_marks_health_degraded_without_secret(
    aiohttp_client,
) -> None:
    service, _, _ = _service()
    runtime = FailingPipelineRuntime(service)
    client = await aiohttp_client(
        create_app(_agent(), pipeline_runtime=runtime)
    )
    await asyncio.wait_for(runtime.worker_started.wait(), timeout=1)
    await asyncio.sleep(0)

    health = await client.get("/health")
    body = await health.json()

    assert health.status == 503
    assert body == {
        "status": "degraded",
        "component": "pipeline_runtime",
        "reason": "pipeline worker failed",
    }
    assert "provider-secret" not in str(body)


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
