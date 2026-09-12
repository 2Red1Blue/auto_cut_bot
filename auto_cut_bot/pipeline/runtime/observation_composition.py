"""Standalone, canary-only rich-observation composition."""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import cast

import psycopg
from autocut_kernel.pipeline.observation_generation import GenerateObservationCommand
from autocut_kernel.store import PostgresRuntimeStore
from autocut_kernel.store.postgres import DbConnection
from autocut_kernel.vlm import GENERATION_RETRY_STRATEGY_VERSION, GenerationRetryPolicy
from autocut_kernel.vlm.observation_contract import ObservationLimits

from auto_cut_bot.pipeline.vlm import PostgresArkFileCache
from auto_cut_bot.pipeline.vlm.doubao_ark_provider import DoubaoArkVlmProviderConfig
from auto_cut_bot.pipeline.vlm.observation_factory import ObservationRuntimePolicy
from auto_cut_bot.pipeline.vlm.observation_provider import ObservationArkProvider

from .composition import (
    PIPELINE_ARK_API_KEY_ENV,
    PIPELINE_ARK_BASE_URL_ENV,
    PIPELINE_ARK_MODEL_ID_ENV,
    PIPELINE_ARK_PROJECT_ID_ENV,
    PIPELINE_ARK_TENANT_ID_ENV,
    PIPELINE_KERNEL_POSTGRES_DSN_ENV,
    PIPELINE_POSTGRES_DSN_ENV,
)
from .observation_entry import ObservationEntryService, SourcePrepObservationRequestFactory

OBSERVATION_RUNTIME_VERSION = "observation-runtime-v1"
OBSERVATION_SOURCE_PROFILE = "shadow"

def compose_observation_entry_from_environment(environ: Mapping[str, str] | None = None) -> ObservationEntryService | None:
    values = os.environ if environ is None else environ
    dsn = values.get(PIPELINE_KERNEL_POSTGRES_DSN_ENV, "").strip() or values.get(PIPELINE_POSTGRES_DSN_ENV, "").strip()
    required = (PIPELINE_ARK_API_KEY_ENV, PIPELINE_ARK_MODEL_ID_ENV, PIPELINE_ARK_TENANT_ID_ENV, PIPELINE_ARK_PROJECT_ID_ENV)
    if not dsn or any(not values.get(key, "").strip() for key in required):
        return None
    factory = cast(Callable[[], DbConnection], lambda: psycopg.connect(dsn))
    config = DoubaoArkVlmProviderConfig(api_key=values[PIPELINE_ARK_API_KEY_ENV].strip(), tenant_id=values[PIPELINE_ARK_TENANT_ID_ENV].strip(), project_id=values[PIPELINE_ARK_PROJECT_ID_ENV].strip(), base_url=values.get(PIPELINE_ARK_BASE_URL_ENV, "").strip() or DoubaoArkVlmProviderConfig.__dataclass_fields__["base_url"].default, max_video_bytes=256 * 1024 * 1024)
    limits = ObservationLimits(524288, 131072, 65536, 256, 256, 256, 16)
    policy = ObservationRuntimePolicy(model_id=values[PIPELINE_ARK_MODEL_ID_ENV].strip(), provider_id=ObservationArkProvider.provider_id, limits=limits, max_output_tokens=32768, video_fps=1, task="充分描述当前视频画面中的人物、动作、表情、场景、字幕和镜头变化，并保留你的理解与不确定性。")
    retry = GenerationRetryPolicy(strategy_version=GENERATION_RETRY_STRATEGY_VERSION, max_attempts=1, backoff_seconds=())
    store = PostgresRuntimeStore(factory)
    provider = ObservationArkProvider(config, file_cache=PostgresArkFileCache(factory))
    return ObservationEntryService(store, GenerateObservationCommand(store, provider), SourcePrepObservationRequestFactory(profile=OBSERVATION_SOURCE_PROFILE, policy=policy, retry_policy=retry), source_profile=OBSERVATION_SOURCE_PROFILE)

__all__ = ["OBSERVATION_RUNTIME_VERSION", "compose_observation_entry_from_environment"]
