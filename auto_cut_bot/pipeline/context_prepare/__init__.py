"""Runtime-only external narrative fetch and context-pack preparation."""

from .api import (
    DEFAULT_ASSET_RESOURCE_PATH,
    DEFAULT_EPISODE_RESOURCE_PATH,
    ExternalNarrativeApiClient,
    ExternalNarrativeApiConfig,
    FetchedExternalNarrativeContext,
)
from .command import (
    COMMAND_NAME,
    CONTEXT_PACK_SET_ARTIFACT_TYPE,
    CONTEXT_PACK_SET_LOGICAL_ID,
    CommittedWindowContextPacks,
    ContextPrepareStore,
    PreparedWindowContextSet,
    PrepareWindowContextCommand,
    PrepareWindowContextRequest,
    PrepareWindowContextResult,
    find_committed_window_context_packs,
    read_committed_window_context_packs,
)
from .prepare import PreparedWindowContext, prepare_window_context

__all__ = [
    "DEFAULT_ASSET_RESOURCE_PATH",
    "DEFAULT_EPISODE_RESOURCE_PATH",
    "ExternalNarrativeApiClient",
    "ExternalNarrativeApiConfig",
    "FetchedExternalNarrativeContext",
    "COMMAND_NAME",
    "CommittedWindowContextPacks",
    "CONTEXT_PACK_SET_ARTIFACT_TYPE",
    "CONTEXT_PACK_SET_LOGICAL_ID",
    "ContextPrepareStore",
    "PrepareWindowContextCommand",
    "PrepareWindowContextRequest",
    "PrepareWindowContextResult",
    "PreparedWindowContextSet",
    "find_committed_window_context_packs",
    "read_committed_window_context_packs",
    "PreparedWindowContext",
    "prepare_window_context",
]
