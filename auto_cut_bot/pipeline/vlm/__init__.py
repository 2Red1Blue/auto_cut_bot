"""Runtime adapters for the provider-neutral AutoCut VLM kernel."""

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
    "IdentityProxyWindow",
    "IdentityProxyWindowBuilder",
    "QWEN_ADAPTER_STRATEGY_VERSION",
    "QwenVlmProvider",
    "QwenVlmProviderConfig",
    "VLM_PROMPT_VERSION",
    "VLM_RESPONSE_SCHEMA",
    "build_vlm_prompt",
    "vlm_response_schema_json",
]
