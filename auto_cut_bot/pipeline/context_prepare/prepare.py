"""Prepare one explicit, hash-bound WindowContextPack from a captured fetch."""

from __future__ import annotations

from dataclasses import dataclass

from autocut_kernel.context_pack import (
    ContextSelectionPolicy,
    EpisodeContextBinding,
    NormalizedNarrativeContext,
    WindowContextPack,
    build_window_context_pack,
    normalize_narrative_context,
)

from .api import FetchedExternalNarrativeContext


@dataclass(frozen=True, slots=True)
class PreparedWindowContext:
    fetched: FetchedExternalNarrativeContext
    normalized: NormalizedNarrativeContext
    binding: EpisodeContextBinding
    pack: WindowContextPack

    def debug_mapping(self) -> dict[str, object]:
        return {
            "binding": self.binding.to_mapping(),
            "normalized": self.normalized.to_mapping(),
            "pack": self.pack.to_mapping(),
            "snapshot": self.fetched.snapshot.to_mapping(),
        }


def prepare_window_context(
    fetched: FetchedExternalNarrativeContext,
    binding: EpisodeContextBinding,
    *,
    local_source_id: str,
    local_source_sha256: str,
    local_episode_index: int,
    policy: ContextSelectionPolicy | None = None,
) -> PreparedWindowContext:
    if type(fetched) is not FetchedExternalNarrativeContext:  # noqa: E721
        raise TypeError("fetched must be an exact FetchedExternalNarrativeContext")
    normalized = normalize_narrative_context(
        fetched.snapshot,
        asset_response=fetched.asset_response,
        episode_response=fetched.episode_response,
    )
    pack = build_window_context_pack(
        normalized,
        binding,
        local_source_id=local_source_id,
        local_source_sha256=local_source_sha256,
        local_episode_index=local_episode_index,
        policy=policy,
    )
    return PreparedWindowContext(fetched, normalized, binding, pack)
