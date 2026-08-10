"""Field classification registry for multi-source conflict resolution.

Each field from the 10 autocut tables is classified into one of 4 categories
that determine the merge strategy when two sources disagree on the same value.

Categories
----------
measurable
    客观可测量 — fields that are objectively measurable and not subject to
    interpretation. Examples: duration, timestamps, episode counts, hashes,
    auto-increment IDs. Strategy: prefer API source, auto-resolve.
author_intent
    作者意图语义 — fields that capture author/creator intent, semantic
    interpretation, or narrative meaning. Examples: persona, relationship
    descriptions, theme, genre, mood, tone. Strategy: prefer LLM, create
    conflict (pending).
api_unique
    API 独有增量 — fields that only the API provides or that represent
    API-generated metadata not present in other sources. Examples:
    voice_timbre, visual tags, highlight scores. Strategy: prefer API,
    auto-resolve.
video_verifiable
    视频可验证 — fields that can be independently verified by watching the
    video (or via VLM analysis). Examples: character appearance, location,
    action descriptions, time of day. Strategy: flag for VLM, create
    conflict (high severity).

Tables
------
books, subjects, episodes, scenes, subtitles, shots, boundaries,
relationships, speaker_mappings, subject_episodes
"""

from __future__ import annotations

from typing import Final

# ── Category literals ──────────────────────────────────────────────────────────

CATEGORY_MEASURABLE: Final = "measurable"
CATEGORY_AUTHOR_INTENT: Final = "author_intent"
CATEGORY_API_UNIQUE: Final = "api_unique"
CATEGORY_VIDEO_VERIFIABLE: Final = "video_verifiable"

ALL_CATEGORIES: Final = (
    CATEGORY_MEASURABLE,
    CATEGORY_AUTHOR_INTENT,
    CATEGORY_API_UNIQUE,
    CATEGORY_VIDEO_VERIFIABLE,
)

# ── Preferred source per category ─────────────────────────────────────────────

PREFERRED_SOURCE: Final[dict[str, str]] = {
    CATEGORY_MEASURABLE: "api",
    CATEGORY_AUTHOR_INTENT: "llm",
    CATEGORY_API_UNIQUE: "api",
    CATEGORY_VIDEO_VERIFIABLE: "vlm",
}

# ── Field registry — every field from all 10 tables ───────────────────────────

_FIELD_REGISTRY: dict[str, dict[str, str]] = {
    # ── 1. books ──────────────────────────────────────────────────────────
    "books": {
        "book_id": CATEGORY_MEASURABLE,
        "book_name": CATEGORY_MEASURABLE,
        "total_episodes": CATEGORY_MEASURABLE,
        "source_type": CATEGORY_MEASURABLE,
        "overall_synopsis": CATEGORY_AUTHOR_INTENT,
        "genre": CATEGORY_AUTHOR_INTENT,
        "sub_genre": CATEGORY_AUTHOR_INTENT,
        "mood": CATEGORY_AUTHOR_INTENT,
        "era": CATEGORY_AUTHOR_INTENT,
        "language": CATEGORY_MEASURABLE,
        "tags": CATEGORY_API_UNIQUE,
        "script_parsed": CATEGORY_AUTHOR_INTENT,
        "script_sha": CATEGORY_MEASURABLE,
        "script_raw_path": CATEGORY_MEASURABLE,
        "created_at": CATEGORY_MEASURABLE,
        "updated_at": CATEGORY_MEASURABLE,
    },
    # ── 2. subjects ───────────────────────────────────────────────────────
    "subjects": {
        "id": CATEGORY_MEASURABLE,
        "book_id": CATEGORY_MEASURABLE,
        "name": CATEGORY_MEASURABLE,
        "aliases": CATEGORY_MEASURABLE,
        "persona": CATEGORY_AUTHOR_INTENT,
        "personality": CATEGORY_AUTHOR_INTENT,
        "traits": CATEGORY_AUTHOR_INTENT,
        "tone": CATEGORY_AUTHOR_INTENT,
        "voice_timbre": CATEGORY_API_UNIQUE,
        "visual_features": CATEGORY_API_UNIQUE,
        "relationship": CATEGORY_AUTHOR_INTENT,
        "role": CATEGORY_AUTHOR_INTENT,
        "first_episode": CATEGORY_MEASURABLE,
        "last_episode": CATEGORY_MEASURABLE,
        "source": CATEGORY_MEASURABLE,
        "vlm_verified": CATEGORY_MEASURABLE,
        "vlm_verified_at": CATEGORY_MEASURABLE,
        "created_at": CATEGORY_MEASURABLE,
    },
    # ── 3. episodes ───────────────────────────────────────────────────────
    "episodes": {
        "episode_id": CATEGORY_MEASURABLE,
        "book_id": CATEGORY_MEASURABLE,
        "chapter_id": CATEGORY_MEASURABLE,
        "title": CATEGORY_MEASURABLE,
        "summary": CATEGORY_AUTHOR_INTENT,
        "is_free": CATEGORY_MEASURABLE,
        "scene_count": CATEGORY_MEASURABLE,
        "duration": CATEGORY_MEASURABLE,
        "source": CATEGORY_MEASURABLE,
        "vlm_verified": CATEGORY_MEASURABLE,
    },
    # ── 4. scenes ─────────────────────────────────────────────────────────
    "scenes": {
        "scene_id": CATEGORY_MEASURABLE,
        "book_id": CATEGORY_MEASURABLE,
        "episode_id": CATEGORY_MEASURABLE,
        "scene_order": CATEGORY_MEASURABLE,
        "heading": CATEGORY_AUTHOR_INTENT,
        "location": CATEGORY_VIDEO_VERIFIABLE,
        "time_of_day": CATEGORY_VIDEO_VERIFIABLE,
        "is_flashback": CATEGORY_AUTHOR_INTENT,
        "flashback_label": CATEGORY_AUTHOR_INTENT,
        "characters_present": CATEGORY_VIDEO_VERIFIABLE,
        "dialogues": CATEGORY_MEASURABLE,
        "raw_description": CATEGORY_MEASURABLE,
        "distilled_summary": CATEGORY_AUTHOR_INTENT,
        "meta_tags": CATEGORY_AUTHOR_INTENT,
        "start_time": CATEGORY_MEASURABLE,
        "end_time": CATEGORY_MEASURABLE,
        "alignment_confidence": CATEGORY_MEASURABLE,
        "alignment_source": CATEGORY_MEASURABLE,
        "source": CATEGORY_MEASURABLE,
        "detected_in_video": CATEGORY_MEASURABLE,
        "vlm_verified": CATEGORY_MEASURABLE,
        "vlm_verified_at": CATEGORY_MEASURABLE,
    },
    # ── 5. subtitles ──────────────────────────────────────────────────────
    "subtitles": {
        "id": CATEGORY_MEASURABLE,
        "book_id": CATEGORY_MEASURABLE,
        "episode_id": CATEGORY_MEASURABLE,
        "start_time": CATEGORY_MEASURABLE,
        "end_time": CATEGORY_MEASURABLE,
        "speaker": CATEGORY_AUTHOR_INTENT,
        "text": CATEGORY_MEASURABLE,
        "tone": CATEGORY_AUTHOR_INTENT,
        "emotion": CATEGORY_AUTHOR_INTENT,
        "group_id": CATEGORY_MEASURABLE,
        "group_tone": CATEGORY_AUTHOR_INTENT,
        "source": CATEGORY_MEASURABLE,
        "confidence": CATEGORY_MEASURABLE,
        "cer_estimate": CATEGORY_MEASURABLE,
    },
    # ── 6. shots ──────────────────────────────────────────────────────────
    "shots": {
        "id": CATEGORY_MEASURABLE,
        "book_id": CATEGORY_MEASURABLE,
        "episode_id": CATEGORY_MEASURABLE,
        "start_time": CATEGORY_MEASURABLE,
        "end_time": CATEGORY_MEASURABLE,
        "scene": CATEGORY_AUTHOR_INTENT,
        "subjects": CATEGORY_VIDEO_VERIFIABLE,
        "actions": CATEGORY_VIDEO_VERIFIABLE,
        "is_highlight": CATEGORY_AUTHOR_INTENT,
        "highlight_score": CATEGORY_API_UNIQUE,
        "highlight_reason": CATEGORY_API_UNIQUE,
        "related_srt_range": CATEGORY_MEASURABLE,
        "source": CATEGORY_MEASURABLE,
    },
    # ── 7. boundaries ─────────────────────────────────────────────────────
    "boundaries": {
        "boundary_id": CATEGORY_MEASURABLE,
        "book_id": CATEGORY_MEASURABLE,
        "episode_id": CATEGORY_MEASURABLE,
        "event_type": CATEGORY_AUTHOR_INTENT,
        "start_time": CATEGORY_MEASURABLE,
        "end_time": CATEGORY_MEASURABLE,
        "description": CATEGORY_AUTHOR_INTENT,
        "subjects": CATEGORY_VIDEO_VERIFIABLE,
        "source_table": CATEGORY_MEASURABLE,
        "source_id": CATEGORY_MEASURABLE,
        "confidence": CATEGORY_MEASURABLE,
        "precision": CATEGORY_MEASURABLE,
        "verified_by": CATEGORY_MEASURABLE,
        "corrected_at": CATEGORY_MEASURABLE,
    },
    # ── 8. relationships ──────────────────────────────────────────────────
    "relationships": {
        "id": CATEGORY_MEASURABLE,
        "book_id": CATEGORY_MEASURABLE,
        "source_subject_id": CATEGORY_MEASURABLE,
        "target_subject_id": CATEGORY_MEASURABLE,
        "description": CATEGORY_AUTHOR_INTENT,
        "source": CATEGORY_MEASURABLE,
        "created_at": CATEGORY_MEASURABLE,
    },
    # ── 9. speaker_mappings ───────────────────────────────────────────────
    "speaker_mappings": {
        "id": CATEGORY_MEASURABLE,
        "book_id": CATEGORY_MEASURABLE,
        "episode_id": CATEGORY_MEASURABLE,
        "speaker_label": CATEGORY_MEASURABLE,
        "mapped_subject_id": CATEGORY_MEASURABLE,
        "confidence": CATEGORY_MEASURABLE,
        "resolved_by": CATEGORY_MEASURABLE,
        "resolved_at": CATEGORY_MEASURABLE,
        "created_at": CATEGORY_MEASURABLE,
    },
    # ── 10. subject_episodes ──────────────────────────────────────────────
    "subject_episodes": {
        "id": CATEGORY_MEASURABLE,
        "subject_id": CATEGORY_MEASURABLE,
        "book_id": CATEGORY_MEASURABLE,
        "episode_id": CATEGORY_MEASURABLE,
        "relationship": CATEGORY_AUTHOR_INTENT,
        "visual_features": CATEGORY_API_UNIQUE,
        "appears_in_episode": CATEGORY_VIDEO_VERIFIABLE,
        "source": CATEGORY_MEASURABLE,
        "created_at": CATEGORY_MEASURABLE,
    },
}


def classify_field(table: str, field: str) -> str:
    """Return the category for a given (table, field) pair.

    Parameters
    ----------
    table : str
        One of the 10 table names: books, subjects, episodes, scenes,
        subtitles, shots, boundaries, relationships, speaker_mappings,
        subject_episodes.
    field : str
        The field name within the table.

    Returns
    -------
    str
        One of: measurable, author_intent, api_unique, video_verifiable.

    Raises
    ------
    KeyError
        If the table or field is not registered.
    """
    return _FIELD_REGISTRY[table][field]


def get_preferred_source(category: str) -> str:
    """Return the preferred data source for a given category.

    Parameters
    ----------
    category : str
        One of: measurable, author_intent, api_unique, video_verifiable.

    Returns
    -------
    str
        The preferred source: api, llm, or vlm.

    Raises
    ------
    ValueError
        If the category is unknown.
    """
    if category not in PREFERRED_SOURCE:
        raise ValueError(
            f"Unknown category: {category!r}. Must be one of {ALL_CATEGORIES}"
        )
    return PREFERRED_SOURCE[category]


def get_fields_by_category(table: str, category: str) -> list[str]:
    """Return all fields in a table that belong to a specific category.

    Parameters
    ----------
    table : str
        Table name.
    category : str
        Category to filter by.

    Returns
    -------
    list[str]
        Sorted list of field names matching the category.
    """
    return sorted(
        field
        for field, cat in _FIELD_REGISTRY[table].items()
        if cat == category
    )


def get_all_fields(table: str) -> list[str]:
    """Return all registered fields for a table.

    Parameters
    ----------
    table : str
        Table name.

    Returns
    -------
    list[str]
        Sorted list of all field names in the table.
    """
    return sorted(_FIELD_REGISTRY[table].keys())


def get_tables() -> list[str]:
    """Return all registered table names."""
    return sorted(_FIELD_REGISTRY.keys())


__all__ = [
    "CATEGORY_MEASURABLE",
    "CATEGORY_AUTHOR_INTENT",
    "CATEGORY_API_UNIQUE",
    "CATEGORY_VIDEO_VERIFIABLE",
    "ALL_CATEGORIES",
    "PREFERRED_SOURCE",
    "classify_field",
    "get_preferred_source",
    "get_fields_by_category",
    "get_all_fields",
    "get_tables",
]