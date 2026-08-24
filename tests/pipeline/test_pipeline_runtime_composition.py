from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg import OperationalError
from runtime_profile_fixture import media_preflight_policy

from auto_cut_bot.pipeline.runtime import PipelineRunRequest, PipelineRunValidationError
from auto_cut_bot.pipeline.runtime.composition import (
    PIPELINE_ARK_API_KEY_ENV,
    PIPELINE_ARK_BASE_URL_ENV,
    PIPELINE_ARK_MAX_OUTPUT_TOKENS_ENV,
    PIPELINE_ARK_MODEL_ID_ENV,
    PIPELINE_ARK_PROJECT_ID_ENV,
    PIPELINE_ARK_TENANT_ID_ENV,
    PIPELINE_MEDIA_PREFLIGHT_POLICY_ENV,
    PIPELINE_POSTGRES_DSN_ENV,
    PIPELINE_SOURCE_CATALOG_ENV,
    PIPELINE_SOURCE_ROOTS_ENV,
    ConfiguredSourceCatalog,
    PipelineRuntimeConfigurationError,
    compose_pipeline_runtime_from_environment,
)
from auto_cut_bot.pipeline.runtime.worker import DurablePipelineWorker


def _catalog(path: Path) -> str:
    return json.dumps(
        [
            {
                "authorization_id": "authorization:episode-1",
                "authorized_path": str(path),
                "expected_source_count": 1,
                "series_id": "series-1",
            }
        ]
    )


def _environment(path: Path, *, api_key: str = "ark-secret-value") -> dict[str, str]:
    return {
        PIPELINE_POSTGRES_DSN_ENV: "postgresql://control.invalid/runtime",
        PIPELINE_SOURCE_CATALOG_ENV: _catalog(path),
        PIPELINE_ARK_API_KEY_ENV: api_key,
        PIPELINE_ARK_TENANT_ID_ENV: "tenant-1",
        PIPELINE_ARK_PROJECT_ID_ENV: "project-1",
        PIPELINE_ARK_MODEL_ID_ENV: "doubao-seed-2-1-pro-260628",
        PIPELINE_ARK_MAX_OUTPUT_TOKENS_ENV: "16384",
        PIPELINE_ARK_BASE_URL_ENV: "https://ark.example.invalid/api/v3",
        PIPELINE_MEDIA_PREFLIGHT_POLICY_ENV: json.dumps(_media_policy(path)),
    }


def _media_policy(path: Path) -> dict[str, object]:
    del path
    return media_preflight_policy().to_mapping()


def test_closed_source_catalog_requires_an_exact_path_and_preserves_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "episode"
    catalog = ConfiguredSourceCatalog.from_json(_catalog(source))
    exact = PipelineRunRequest("test", source_root=str(source))
    child = PipelineRunRequest("test", source_root=str(source / "nested"))
    reference = PipelineRunRequest(
        "shadow", source_reference="authorization:episode-1"
    )

    assert catalog.allows(exact)
    assert catalog.allows(reference)
    assert not catalog.allows(child)
    resolved = catalog.resolve(SimpleNamespace(request=exact))
    assert resolved.root == source.resolve()
    assert resolved.authorization_id == "authorization:episode-1"
    assert resolved.series_id == "series-1"
    assert resolved.expected_source_count == 1


@pytest.mark.parametrize(
    "catalog",
    [
        "[]",
        '[{"authorized_path":"/tmp/open"}]',
        (
            '[{"authorization_id":"auth","authorized_path":"relative",'
            '"expected_source_count":1,"series_id":"series"}]'
        ),
    ],
)
def test_source_catalog_rejects_open_or_incomplete_entries(catalog: str) -> None:
    with pytest.raises(PipelineRuntimeConfigurationError):
        ConfiguredSourceCatalog.from_json(catalog)


def test_environment_composes_only_doubao_profile_and_defaults_kernel_dsn(
    tmp_path: Path,
) -> None:
    runtime = compose_pipeline_runtime_from_environment(_environment(tmp_path))

    assert runtime is not None
    assert runtime.execution_profile.kind == "doubao_vlm"
    assert runtime.execution_profile.schema_version == "pipeline-execution-profile-v3"
    assert runtime.execution_profile.provider_id == "doubao-ark-responses-stream"
    assert runtime.execution_profile.model_id == "doubao-seed-2-1-pro-260628"
    assert json.loads(runtime.execution_profile.request_parameters_json or "{}")[
        "max_output_tokens"
    ] == 16_384
    assert (
        runtime.execution_profile.to_media_preflight_policy().to_mapping()
        == _media_policy(tmp_path)
    )
    assert "qwen" not in runtime.execution_profile.canonical_json.casefold()


def test_environment_uses_registered_ark_base_url_when_not_overridden(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    del environment[PIPELINE_ARK_BASE_URL_ENV]

    runtime = compose_pipeline_runtime_from_environment(environment)

    assert runtime is not None


def test_environment_rejects_partial_or_permissive_roots_without_exposing_secret(
    tmp_path: Path,
) -> None:
    secret = "ark-secret-that-must-not-escape"
    partial = {
        PIPELINE_POSTGRES_DSN_ENV: "postgresql://control.invalid/runtime",
        PIPELINE_SOURCE_ROOTS_ENV: str(tmp_path),
        PIPELINE_ARK_API_KEY_ENV: secret,
    }

    with pytest.raises(PipelineRuntimeConfigurationError) as error:
        compose_pipeline_runtime_from_environment(partial)

    assert secret not in str(error.value)
    assert PIPELINE_SOURCE_CATALOG_ENV in str(error.value)


def test_environment_rejects_qwen_as_a_doubao_fallback(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment[PIPELINE_ARK_MODEL_ID_ENV] = "qwen-vl-max"

    with pytest.raises(
        PipelineRuntimeConfigurationError,
        match="Doubao/provider/media-preflight configuration",
    ):
        compose_pipeline_runtime_from_environment(environment)


@pytest.mark.asyncio
async def test_worker_run_forever_polls_until_stop_event() -> None:
    worker = object.__new__(DurablePipelineWorker)
    stop_event = asyncio.Event()
    calls = 0

    async def run_once(*, stop_event: asyncio.Event | None = None) -> int:
        nonlocal calls
        assert stop_event is not None
        calls += 1
        if calls == 2:
            stop_event.set()
        return 0

    worker.run_once = run_once  # type: ignore[method-assign]
    await worker.run_forever(stop_event, poll_interval_seconds=0.001)

    assert calls == 2


@pytest.mark.asyncio
async def test_worker_run_forever_rejects_invalid_poll_interval() -> None:
    worker = object.__new__(DurablePipelineWorker)

    with pytest.raises(PipelineRunValidationError, match="positive"):
        await worker.run_forever(asyncio.Event(), poll_interval_seconds=0)


@pytest.mark.asyncio
async def test_worker_run_forever_backs_off_and_recovers_one_shot_database_error() -> None:
    worker = object.__new__(DurablePipelineWorker)
    stop_event = asyncio.Event()
    calls = 0

    async def run_once(*, stop_event: asyncio.Event | None = None) -> int:
        nonlocal calls
        assert stop_event is not None
        calls += 1
        if calls == 1:
            raise OperationalError("transient database failure")
        stop_event.set()
        return 0

    worker.run_once = run_once  # type: ignore[method-assign]
    await worker.run_forever(stop_event, poll_interval_seconds=0.001)

    assert calls == 2


@pytest.mark.asyncio
async def test_worker_run_forever_exposes_fatal_runtime_error() -> None:
    worker = object.__new__(DurablePipelineWorker)

    async def run_once(*, stop_event: asyncio.Event | None = None) -> int:
        assert stop_event is not None
        raise RuntimeError("worker invariant failed")

    worker.run_once = run_once  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="invariant failed"):
        await worker.run_forever(asyncio.Event(), poll_interval_seconds=0.001)
