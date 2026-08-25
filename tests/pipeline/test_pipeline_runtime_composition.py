from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from autocut_kernel.source_manifest import SourceOperationPolicy
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
    PIPELINE_KERNEL_POSTGRES_DSN_ENV,
    PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV,
    PIPELINE_MEDIA_PREFLIGHT_POLICY_ENV,
    PIPELINE_MEDIA_PREFLIGHT_STAGING_ROOT_ENV,
    PIPELINE_POSTGRES_DSN_ENV,
    PIPELINE_SOURCE_CATALOG_ENV,
    PIPELINE_SOURCE_ROOTS_ENV,
    ConfiguredSourceCatalog,
    PipelineRuntimeConfigurationError,
    compose_pipeline_highlight_read_service_from_environment,
    compose_pipeline_runtime_from_environment,
)
from auto_cut_bot.pipeline.runtime.worker import DurablePipelineWorker


@pytest.fixture(autouse=True)
def _funasr_shared_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUNASR_SHARED_TOKEN", "runtime-composition-test-secret")


def _catalog(path: Path) -> str:
    policy = SourceOperationPolicy(
        "authorization:episode-1",
        "series-1",
        1,
        ("semantic_analysis", "render_source"),
    )
    return json.dumps(
        [
            {
                "authorization_id": "authorization:episode-1",
                "authorization_policy_sha256": policy.policy_sha256,
                "authorized_path": str(path),
                "authorized_purposes": list(policy.authorized_purposes),
                "expected_source_count": 1,
                "series_id": "series-1",
            }
        ]
    )


def _environment(path: Path, *, api_key: str = "ark-secret-value") -> dict[str, str]:
    staging_root = path / "verified-media-staging"
    staging_root.mkdir(mode=0o700)
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
        PIPELINE_MEDIA_PREFLIGHT_STAGING_ROOT_ENV: str(staging_root),
        PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV: json.dumps(
            {
                "max_source_bytes": 8 * 1024 * 1024,
                "timed_speech_max_request_bytes": 8 * 1024 * 1024,
                "copy_chunk_bytes": 64 * 1024,
                "staging_quota_bytes": 16 * 1024 * 1024,
            }
        ),
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
    reference = PipelineRunRequest("shadow", source_reference="authorization:episode-1")

    assert catalog.allows(exact)
    assert catalog.allows(reference)
    assert not catalog.allows(child)
    resolved = catalog.resolve(SimpleNamespace(request=exact))
    assert resolved.root == source.resolve()
    assert resolved.policy.authorization_id == "authorization:episode-1"
    assert resolved.policy.series_id == "series-1"
    assert resolved.policy.expected_source_count == 1
    assert resolved.policy.authorized_purposes == (
        "semantic_analysis",
        "render_source",
    )


@pytest.mark.parametrize(
    "purposes",
    [
        [],
        ["semantic_analysis", "semantic_analysis"],
        ["render_source", "semantic_analysis"],
        ["semantic-analysis"],
        ["semantic_analysis", 1],
    ],
)
def test_source_catalog_rejects_noncanonical_authorized_purposes(
    tmp_path: Path,
    purposes: list[object],
) -> None:
    policy = SourceOperationPolicy(
        "authorization:episode-1",
        "series-1",
        1,
        ("semantic_analysis", "render_source"),
    )
    raw = json.dumps(
        [
            {
                "authorization_id": policy.authorization_id,
                "authorization_policy_sha256": policy.policy_sha256,
                "authorized_path": str(tmp_path),
                "authorized_purposes": purposes,
                "expected_source_count": policy.expected_source_count,
                "series_id": policy.series_id,
            }
        ]
    )

    with pytest.raises(PipelineRuntimeConfigurationError):
        ConfiguredSourceCatalog.from_json(raw)


def test_source_catalog_rejects_duplicate_json_keys_and_policy_hash_mismatch(
    tmp_path: Path,
) -> None:
    valid = json.loads(_catalog(tmp_path))
    assert isinstance(valid, list)
    mismatched = valid[0]
    assert isinstance(mismatched, dict)
    mismatched["authorization_policy_sha256"] = "sha256:" + "0" * 64

    with pytest.raises(PipelineRuntimeConfigurationError, match="hash mismatch"):
        ConfiguredSourceCatalog.from_json(json.dumps(valid))
    with pytest.raises(PipelineRuntimeConfigurationError, match="valid JSON"):
        ConfiguredSourceCatalog.from_json(
            '[{"authorization_id":"first","authorization_id":"second"}]'
        )


def test_source_catalog_rejects_unknown_fields(tmp_path: Path) -> None:
    decoded = json.loads(_catalog(tmp_path))
    assert isinstance(decoded, list) and isinstance(decoded[0], dict)
    decoded[0]["implicit_authority"] = True

    with pytest.raises(PipelineRuntimeConfigurationError, match="closed catalog fields"):
        ConfiguredSourceCatalog.from_json(json.dumps(decoded))


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
    assert runtime.execution_profile.schema_version == "pipeline-execution-profile-v5"
    assert runtime.execution_profile.provider_id == "doubao-ark-responses-stream"
    assert runtime.execution_profile.model_id == "doubao-seed-2-1-pro-260628"
    assert (
        json.loads(runtime.execution_profile.request_parameters_json or "{}")["max_output_tokens"]
        == 16_384
    )
    assert runtime.execution_profile.to_media_preflight_policy().to_mapping() == _media_policy(
        tmp_path
    )
    assert "qwen" not in runtime.execution_profile.canonical_json.casefold()
    assert str(tmp_path / "verified-media-staging") not in runtime.execution_profile.canonical_json


def test_highlight_read_composition_uses_only_configured_read_dsns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from auto_cut_bot.pipeline.runtime import composition
    from auto_cut_bot.pipeline.runtime.highlight_projection import PipelineHighlightReadService

    connection_attempts: list[str] = []

    def reject_connection(dsn: str) -> object:
        connection_attempts.append(dsn)
        raise AssertionError("read service composition must not connect")

    monkeypatch.setattr(composition.psycopg, "connect", reject_connection)

    assert compose_pipeline_highlight_read_service_from_environment({}) is None
    service = compose_pipeline_highlight_read_service_from_environment({
        PIPELINE_POSTGRES_DSN_ENV: "postgresql://control.invalid/runtime",
        PIPELINE_KERNEL_POSTGRES_DSN_ENV: "postgresql://kernel.invalid/runtime",
    })

    assert isinstance(service, PipelineHighlightReadService)
    assert connection_attempts == []


def test_direct_v3_profile_cannot_relabel_v4_parse_fields_as_executable(
    tmp_path: Path,
) -> None:
    runtime = compose_pipeline_runtime_from_environment(_environment(tmp_path))
    assert runtime is not None
    profile = runtime.execution_profile

    with pytest.raises(
        PipelineRunValidationError,
        match="only be reconstructed from persisted mappings",
    ):
        replace(profile, schema_version="pipeline-execution-profile-v3")


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


@pytest.mark.parametrize(
    "required_name",
    [
        PIPELINE_MEDIA_PREFLIGHT_STAGING_ROOT_ENV,
        PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV,
    ],
)
def test_media_preflight_composition_has_no_materialization_defaults(
    tmp_path: Path,
    required_name: str,
) -> None:
    environment = _environment(tmp_path)
    del environment[required_name]

    with pytest.raises(PipelineRuntimeConfigurationError, match=required_name):
        compose_pipeline_runtime_from_environment(environment)


def test_media_preflight_composition_rejects_unsafe_staging_root_before_registration(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    staging_root = Path(environment[PIPELINE_MEDIA_PREFLIGHT_STAGING_ROOT_ENV])
    os.chmod(staging_root, 0o755)

    with pytest.raises(PipelineRuntimeConfigurationError, match="private 0700"):
        compose_pipeline_runtime_from_environment(environment)


def test_media_preflight_composition_rejects_open_materialization_policy(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    environment[PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV] = json.dumps(
        {
            "max_source_bytes": 8 * 1024 * 1024,
            "timed_speech_max_request_bytes": 8 * 1024 * 1024,
            "copy_chunk_bytes": 64 * 1024,
            "staging_quota_bytes": 16 * 1024 * 1024,
            "implicit_default": 1,
        }
    )

    with pytest.raises(PipelineRuntimeConfigurationError, match="closed materialization"):
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
