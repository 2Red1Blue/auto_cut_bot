"""Runtime adapters for the provider-neutral AutoCut VLM kernel."""

from .ark_file_cache import (
    ArkFileCacheError,
    ArkFileCachePort,
    ArkFileCacheRecord,
    PostgresArkFileCache,
)
from .doubao_ark_provider import (
    DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_NESTED_SCHEMA_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_PROVIDER_ID,
    DOUBAO_ARK_SUPPORTED_ADAPTER_STRATEGY_VERSIONS,
    DoubaoArkVlmProvider,
    DoubaoArkVlmProviderConfig,
)
from .identity_window import IdentityProxyWindow, IdentityProxyWindowBuilder
from .prompt import (
    VLM_PROMPT_VERSION,
    VLM_RESPONSE_SCHEMA,
    build_vlm_prompt,
    vlm_response_schema_json,
)
from .qwen_provider import (
    QWEN_ADAPTER_STRATEGY_VERSION,
    QwenVlmProvider,
    QwenVlmProviderConfig,
)
from .request_factory import (
    DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION,
    DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION,
    DOUBAO_VLM_PROBE_THEN_PARALLEL_STAGE_STRATEGY_VERSION,
    DOUBAO_VLM_REQUEST_FACTORY_STRATEGY_VERSION,
    DOUBAO_VLM_STAGE_STRATEGY_VERSION,
    DoubaoVlmRequestFactory,
    DoubaoVlmRequestPolicy,
    build_doubao_vlm_request,
)

__all__ = [
    "ArkFileCacheError",
    "ArkFileCachePort",
    "ArkFileCacheRecord",
    "DOUBAO_ARK_ADAPTER_STRATEGY_VERSION",
    "DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION",
    "DOUBAO_ARK_NESTED_SCHEMA_ADAPTER_STRATEGY_VERSION",
    "DOUBAO_ARK_PROVIDER_ID",
    "DOUBAO_ARK_SUPPORTED_ADAPTER_STRATEGY_VERSIONS",
    "DOUBAO_VLM_REQUEST_FACTORY_STRATEGY_VERSION",
    "DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION",
    "DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION",
    "DOUBAO_VLM_PROBE_THEN_PARALLEL_STAGE_STRATEGY_VERSION",
    "DOUBAO_VLM_STAGE_STRATEGY_VERSION",
    "DoubaoArkVlmProvider",
    "DoubaoArkVlmProviderConfig",
    "DoubaoVlmRequestFactory",
    "DoubaoVlmRequestPolicy",
    "IdentityProxyWindow",
    "IdentityProxyWindowBuilder",
    "PostgresArkFileCache",
    "QWEN_ADAPTER_STRATEGY_VERSION",
    "QwenVlmProvider",
    "QwenVlmProviderConfig",
    "VLM_PROMPT_VERSION",
    "VLM_RESPONSE_SCHEMA",
    "build_vlm_prompt",
    "build_doubao_vlm_request",
    "vlm_response_schema_json",
]
