"""Pure, replayable external-narrative context projection contracts.

This package is intentionally unable to fetch HTTP, inspect source files or
format a VLM request.  It turns already captured external JSON plus an owner
episode binding into the only model-visible narrative context value.
"""

from .models import (
    EpisodeContextBinding,
    EpisodeContextBindingSet,
    ExternalContextSnapshot,
    NormalizedCharacter,
    NormalizedEpisode,
    NormalizedNarrativeContext,
    NormalizedRelationship,
    OwnerEpisodeMap,
    OwnerEpisodeMapSet,
    WindowContextPack,
)
from .normalizer import ExternalContextNormalizationError, normalize_narrative_context
from .selector import (
    CONTEXT_SELECTION_POLICY_V1,
    ContextSelectionPolicy,
    build_window_context_pack,
    video_only_window_context_pack,
)

__all__ = [
    "CONTEXT_SELECTION_POLICY_V1",
    "ContextSelectionPolicy",
    "EpisodeContextBinding",
    "EpisodeContextBindingSet",
    "ExternalContextNormalizationError",
    "ExternalContextSnapshot",
    "NormalizedCharacter",
    "NormalizedEpisode",
    "NormalizedNarrativeContext",
    "NormalizedRelationship",
    "OwnerEpisodeMap",
    "OwnerEpisodeMapSet",
    "WindowContextPack",
    "build_window_context_pack",
    "video_only_window_context_pack",
    "normalize_narrative_context",
]
