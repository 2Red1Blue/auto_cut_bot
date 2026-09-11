"""Composition-root tests for the shadow-bootstrap observation entry."""

from __future__ import annotations

from pathlib import Path

import pytest

from auto_cut_bot.pipeline.runtime.composition import (
    PipelineRuntimeConfigurationError,
)
from auto_cut_bot.pipeline.runtime.shadow_bootstrap_composition import (
    compose_shadow_bootstrap_entry_from_environment,
)
from auto_cut_bot.pipeline.runtime.shadow_bootstrap_entry import (
    ShadowBootstrapObservationEntryService,
)

_ENDPOINT = "http://127.0.0.1:18765/v1/shadow-bootstrap-timed-observation"
_LIMITS = (
    '{"max_source_bytes":2147483648,"timed_speech_max_request_bytes":2147483648,'
    '"copy_chunk_bytes":1048576,"staging_quota_bytes":6442450944}'
)


def _environment(tmp_path: Path) -> dict[str, str]:
    staging = tmp_path / "staging"
    staging.mkdir()
    staging.chmod(0o700)
    return {
        "AUTO_CUT_BOT_PIPELINE_KERNEL_POSTGRES_DSN": "postgresql://autocut@127.0.0.1:1/autocut",
        "AUTO_CUT_BOT_SHADOW_BOOTSTRAP_OBSERVATION_ENDPOINT": _ENDPOINT,
        "AUTO_CUT_BOT_SHADOW_BOOTSTRAP_OBSERVATION_MAX_RESPONSE_BYTES": "16777216",
        "FUNASR_SHARED_TOKEN": "local-loopback-token",
        "AUTO_CUT_BOT_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_JSON": _LIMITS,
        "AUTO_CUT_BOT_MEDIA_PREFLIGHT_STAGING_ROOT": str(staging),
    }


def test_unconfigured_environment_composes_nothing() -> None:
    assert compose_shadow_bootstrap_entry_from_environment({}) is None


def test_partial_configuration_fails_closed(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    del environment["AUTO_CUT_BOT_SHADOW_BOOTSTRAP_OBSERVATION_ENDPOINT"]
    with pytest.raises(PipelineRuntimeConfigurationError, match="incomplete"):
        compose_shadow_bootstrap_entry_from_environment(environment)


def test_missing_store_dsn_fails_closed(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    del environment["AUTO_CUT_BOT_PIPELINE_KERNEL_POSTGRES_DSN"]
    with pytest.raises(PipelineRuntimeConfigurationError, match="incomplete"):
        compose_shadow_bootstrap_entry_from_environment(environment)


def test_ordinary_media_deployment_without_bootstrap_env_composes_nothing(
    tmp_path: Path,
) -> None:
    """A configured media runtime must not gain or require the bootstrap route."""
    environment = _environment(tmp_path)
    del environment["AUTO_CUT_BOT_SHADOW_BOOTSTRAP_OBSERVATION_ENDPOINT"]
    del environment["AUTO_CUT_BOT_SHADOW_BOOTSTRAP_OBSERVATION_MAX_RESPONSE_BYTES"]
    assert compose_shadow_bootstrap_entry_from_environment(environment) is None


def test_complete_configuration_composes_entry(tmp_path: Path) -> None:
    entry = compose_shadow_bootstrap_entry_from_environment(_environment(tmp_path))
    assert type(entry) is ShadowBootstrapObservationEntryService


def test_non_loopback_endpoint_is_rejected(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment["AUTO_CUT_BOT_SHADOW_BOOTSTRAP_OBSERVATION_ENDPOINT"] = (
        "http://10.0.0.5:18765/v1/shadow-bootstrap-timed-observation"
    )
    with pytest.raises(PipelineRuntimeConfigurationError, match="invalid"):
        compose_shadow_bootstrap_entry_from_environment(environment)


def test_wrong_route_path_is_rejected(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment["AUTO_CUT_BOT_SHADOW_BOOTSTRAP_OBSERVATION_ENDPOINT"] = (
        "http://127.0.0.1:18765/v1/timed-speech/windows"
    )
    with pytest.raises(PipelineRuntimeConfigurationError, match="invalid"):
        compose_shadow_bootstrap_entry_from_environment(environment)


@pytest.mark.parametrize("raw", ["0", "-1", "12.5", "abc"])
def test_non_positive_max_response_bytes_is_rejected(
    tmp_path: Path, raw: str
) -> None:
    environment = _environment(tmp_path)
    environment["AUTO_CUT_BOT_SHADOW_BOOTSTRAP_OBSERVATION_MAX_RESPONSE_BYTES"] = raw
    with pytest.raises(PipelineRuntimeConfigurationError):
        compose_shadow_bootstrap_entry_from_environment(environment)


