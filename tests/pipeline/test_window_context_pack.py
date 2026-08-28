from __future__ import annotations

import hashlib
import json

import pytest
from autocut_kernel.context_pack import (
    EpisodeContextBinding,
    EpisodeContextBindingSet,
    ExternalContextSnapshot,
    build_window_context_pack,
    normalize_narrative_context,
)


def _hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _source() -> tuple[ExternalContextSnapshot, dict[str, object], dict[str, object]]:
    assets = {
        "data": {
            "book-1": {
                "bookId": "book-1",
                "bookName": "Safe title",
                "overallSynopsis": "future ending must never appear",
                "stablePremise": "A student starts a new school.",
                "themeTags": ["romance", "school"],
                "characters": [
                    {"characterId": "c-alice", "name": "Alice", "role": "student"},
                    {"characterId": "c-bob", "name": "Bob", "role": "student"},
                ],
                "relationships": [
                    {
                        "relationshipId": "r-safe",
                        "sourceCharacterId": "c-alice",
                        "targetCharacterId": "c-bob",
                        "description": "classmates",
                        "knownFromEpisodeOrdinal": 1,
                    },
                    {
                        "relationshipId": "r-future",
                        "sourceCharacterId": "c-alice",
                        "targetCharacterId": "c-bob",
                        "description": "married in the finale",
                    },
                ],
            }
        }
    }
    episodes = {
        "data": {
            "book-1": {
                "bookId": "book-1",
                "episodes": [
                    {
                        "episodeId": "ep-1",
                        "chapterId": "chapter-1",
                        "episodeOrdinal": 1,
                        "title": "First day",
                        "summary": "Alice meets Bob.",
                        "characters": ["c-alice", "c-bob"],
                        "subtitles": [{"text": "forbidden"}],
                        "shots": [{"highlight_reason": "forbidden"}],
                    }
                ],
            }
        }
    }
    snapshot = ExternalContextSnapshot(
        "snapshot:1",
        "book-1",
        ("/assets/tmp/batch-content-assets", "/assets/tmp/batch-episodes-info"),
        "https://metadata.example",
        "default",
        _hash({"asset_response": assets, "episode_response": episodes}),
    )
    return snapshot, assets, episodes


def _binding() -> EpisodeContextBinding:
    return EpisodeContextBinding(
        "source-001",
        "sha256:" + "a" * 64,
        0,
        "book-1",
        "ep-1",
        "chapter-1",
        1,
    )


def test_normalizer_and_selector_do_not_leak_legacy_or_physical_api_fields() -> None:
    snapshot, assets, episodes = _source()
    normalized = normalize_narrative_context(
        snapshot, asset_response=assets, episode_response=episodes
    )
    pack = build_window_context_pack(
        normalized,
        _binding(),
        local_source_id="source-001",
        local_source_sha256="sha256:" + "a" * 64,
        local_episode_index=0,
    )

    assert pack.mode == "api_assisted"
    assert "A student starts a new school." in pack.rendered_context
    for forbidden in ("future ending", "married in the finale", "forbidden", "highlight_reason"):
        assert forbidden not in pack.rendered_context
    assert "relationship:r-safe" in pack.selected_refs
    assert "relationship:r-future" not in pack.selected_refs
    assert pack.canonical_hash == build_window_context_pack(
        normalized,
        _binding(),
        local_source_id="source-001",
        local_source_sha256="sha256:" + "a" * 64,
        local_episode_index=0,
    ).canonical_hash


def test_binding_mismatch_is_hashable_video_only_not_a_guessed_episode() -> None:
    snapshot, assets, episodes = _source()
    normalized = normalize_narrative_context(
        snapshot, asset_response=assets, episode_response=episodes
    )
    pack = build_window_context_pack(
        normalized,
        _binding(),
        local_source_id="source-001",
        local_source_sha256="sha256:" + "b" * 64,
        local_episode_index=0,
    )
    assert pack.mode == "video_only"
    assert pack.video_only_reason_code == "EXTERNAL_EPISODE_BINDING_MISMATCH"
    assert not pack.selected_refs


def test_normalizer_rejects_a_response_for_another_series() -> None:
    snapshot, assets, episodes = _source()
    assets["data"] = {"another-book": assets["data"]["book-1"]}  # type: ignore[index]
    with pytest.raises(ValueError, match="requested series"):
        normalize_narrative_context(snapshot, asset_response=assets, episode_response=episodes)


def test_binding_set_rejects_duplicate_local_episode_mapping() -> None:
    binding = _binding()
    with pytest.raises(ValueError, match="duplicate local"):
        EpisodeContextBindingSet("book-1", (binding, binding))


def test_external_ordinal_conflict_falls_back_to_video_only() -> None:
    snapshot, assets, episodes = _source()
    normalized = normalize_narrative_context(
        snapshot, asset_response=assets, episode_response=episodes
    )
    conflicting = EpisodeContextBinding(
        "source-001", "sha256:" + "a" * 64, 0, "book-1", "ep-1", "chapter-1", 2
    )
    pack = build_window_context_pack(
        normalized,
        conflicting,
        local_source_id="source-001",
        local_source_sha256="sha256:" + "a" * 64,
        local_episode_index=0,
    )
    assert pack.mode == "video_only"
    assert pack.video_only_reason_code == "EXTERNAL_EPISODE_ORDINAL_CONFLICT"
