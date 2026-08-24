"""Runtime adapters for the provider-neutral AutoCut VLM kernel."""

from .ark_file_cache import (
    ArkFileCacheError,
    ArkFileCachePort,
    ArkFileCacheRecord,
    PostgresArkFileCache,
)
from .doubao_ark_provider import (
    DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_PROVIDER_ID,
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

__all__ = [
    "ArkFileCacheError",
    "ArkFileCachePort",
    "ArkFileCacheRecord",
    "DOUBAO_ARK_ADAPTER_STRATEGY_VERSION",
    "DOUBAO_ARK_PROVIDER_ID",
    "DoubaoArkVlmProvider",
    "DoubaoArkVlmProviderConfig",
    "IdentityProxyWindow",
    "IdentityProxyWindowBuilder",
    "PostgresArkFileCache",
    "QWEN_ADAPTER_STRATEGY_VERSION",
    "QwenVlmProvider",
    "QwenVlmProviderConfig",
    "VLM_PROMPT_VERSION",
    "VLM_RESPONSE_SCHEMA",
    "build_vlm_prompt",
    "vlm_response_schema_json",
]
