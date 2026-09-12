from __future__ import annotations

from auto_cut_bot.pipeline.runtime.composition import (
    PIPELINE_ARK_API_KEY_ENV,
    PIPELINE_ARK_MODEL_ID_ENV,
    PIPELINE_ARK_PROJECT_ID_ENV,
    PIPELINE_ARK_TENANT_ID_ENV,
    PIPELINE_KERNEL_POSTGRES_DSN_ENV,
)
from auto_cut_bot.pipeline.runtime.observation_composition import (
    compose_observation_entry_from_environment,
)


def test_configured_observation_composition_uses_registered_single_attempt_policy() -> None:
    entry = compose_observation_entry_from_environment({
        PIPELINE_KERNEL_POSTGRES_DSN_ENV: "postgresql://invalid-test-host/db",
        PIPELINE_ARK_API_KEY_ENV: "test-key", PIPELINE_ARK_MODEL_ID_ENV: "test-model",
        PIPELINE_ARK_TENANT_ID_ENV: "test-tenant", PIPELINE_ARK_PROJECT_ID_ENV: "test-project",
    })
    assert entry is not None


def test_observation_composition_requires_private_runtime_configuration() -> None:
    assert compose_observation_entry_from_environment({}) is None
