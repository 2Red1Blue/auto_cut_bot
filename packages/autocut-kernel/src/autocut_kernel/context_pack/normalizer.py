"""Strict projection of captured API JSON into narrative-only context.

The normalizer only knows the explicit batch response envelope.  Unknown data
is ignored, not forwarded: notably subtitles, shots, highlights and raw
full-series synopses cannot leak into a VLM prompt through a new API field.
"""

from __future__ import annotations

from typing import cast

from .models import (
    ExternalContextSnapshot,
    NormalizedCharacter,
    NormalizedEpisode,
    NormalizedNarrativeContext,
    NormalizedRelationship,
)


class ExternalContextNormalizationError(ValueError):
    """The immutable snapshot does not satisfy the configured API envelope."""


def _mapping(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise ExternalContextNormalizationError(f"{name} must be an object")
    return cast(dict[str, object], value)


def _list(value: object, name: str) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise ExternalContextNormalizationError(f"{name} must be an array")
    return cast(list[object], value)


def _optional_text(value: object, maximum: int) -> str | None:
    if type(value) is not str:  # noqa: E721
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
        return None
    return normalized


def _text(value: object, name: str, maximum: int) -> str:
    normalized = _optional_text(value, maximum)
    if normalized is None:
        raise ExternalContextNormalizationError(f"{name} must be a non-empty text field")
    return normalized


def _field(mapping: dict[str, object], *names: str) -> object:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _batch_item(raw: object, series_external_id: str, name: str) -> dict[str, object]:
    root = _mapping(raw, name)
    data = _mapping(root.get("data", root), f"{name}.data")
    if series_external_id in data:
        return _mapping(data[series_external_id], f"{name}.data[series]")
    # Direct single-series responses are legal only if they identify the same
    # series.  This avoids treating an arbitrary envelope as a valid match.
    found = _field(data, "bookId", "book_id", "seriesId", "series_id")
    if found is not None and str(found) == series_external_id:
        return data
    raise ExternalContextNormalizationError(f"{name} does not contain the requested series")


def _character_ref(item: dict[str, object]) -> str | None:
    raw = _field(item, "characterId", "character_id", "id")
    return _optional_text(raw, 256)


def _normalize_characters(asset: dict[str, object]) -> tuple[NormalizedCharacter, ...]:
    raw: object = asset.get("characters", [])
    if raw is None:
        raw = []
    values: list[NormalizedCharacter] = []
    for value in _list(raw, "asset.characters"):
        item = _mapping(value, "asset.characters[]")
        ref = _character_ref(item)
        name = _optional_text(_field(item, "name", "characterName"), 160)
        if ref is None or name is None:
            continue
        aliases_raw: object = item.get("aliases", [])
        aliases = ()
        if type(aliases_raw) is list:  # noqa: E721
            alias_values = cast(list[object], aliases_raw)
            aliases = tuple(
                sorted({text for entry in alias_values if (text := _optional_text(entry, 160))})
            )
        role = _optional_text(_field(item, "role", "characterRole"), 280)
        values.append(NormalizedCharacter(ref, name, aliases, role))
    result = tuple(sorted(values, key=lambda value: value.character_ref))
    if len({value.character_ref for value in result}) != len(result):
        raise ExternalContextNormalizationError("characters contain duplicate stable IDs")
    return result


def _normalize_relationships(
    asset: dict[str, object], known_character_refs: set[str]
) -> tuple[NormalizedRelationship, ...]:
    raw: object = asset.get("relationships", [])
    if raw is None:
        raw = []
    values: list[NormalizedRelationship] = []
    for value in _list(raw, "asset.relationships"):
        item = _mapping(value, "asset.relationships[]")
        known_from = _field(item, "knownFromEpisodeOrdinal", "known_from_external_episode_ordinal")
        if type(known_from) is not int or known_from < 1:  # noqa: E721
            # An unbounded relationship may reveal future information.
            continue
        relationship_ref = _optional_text(_field(item, "relationshipId", "relationship_id", "id"), 256)
        subject = _optional_text(_field(item, "sourceCharacterId", "source_character_ref", "sourceId"), 256)
        object_ = _optional_text(_field(item, "targetCharacterId", "target_character_ref", "targetId"), 256)
        description = _optional_text(_field(item, "description", "desc"), 320)
        if (
            relationship_ref is None
            or subject is None
            or object_ is None
            or description is None
            or subject not in known_character_refs
            or object_ not in known_character_refs
        ):
            continue
        values.append(NormalizedRelationship(relationship_ref, subject, object_, description, known_from))
    result = tuple(sorted(values, key=lambda value: value.relationship_ref))
    if len({value.relationship_ref for value in result}) != len(result):
        raise ExternalContextNormalizationError("relationships contain duplicate stable IDs")
    return result


def _episode_character_refs(item: dict[str, object], known_character_refs: set[str]) -> tuple[str, ...]:
    raw: object = item.get("characters", [])
    if type(raw) is not list:  # noqa: E721
        return ()
    result: set[str] = set()
    for value in cast(list[object], raw):
        if type(value) is str and value in known_character_refs:
            result.add(value)
        elif type(value) is dict:  # noqa: E721
            ref = _character_ref(cast(dict[str, object], value))
            if ref is not None and ref in known_character_refs:
                result.add(ref)
    return tuple(sorted(result))


def _normalize_episodes(
    episode_response: dict[str, object], known_character_refs: set[str]
) -> tuple[NormalizedEpisode, ...]:
    raw: object = episode_response.get("episodes", [])
    values: list[NormalizedEpisode] = []
    for value in _list(raw, "episodes.episodes"):
        item = _mapping(value, "episodes.episodes[]")
        episode_id = _text(_field(item, "episodeId", "episode_id"), "episodeId", 256)
        chapter = _optional_text(_field(item, "chapterId", "chapter_id"), 256)
        ordinal = _field(item, "episodeOrdinal", "episode_ordinal", "chapterOrdinal", "chapter_ordinal")
        values.append(
            NormalizedEpisode(
                external_episode_id=episode_id,
                external_chapter_id=chapter,
                external_episode_ordinal=ordinal if type(ordinal) is int and ordinal >= 1 else None,  # noqa: E721
                title=_optional_text(item.get("title"), 280),
                summary=_optional_text(item.get("summary"), 900),
                character_refs=_episode_character_refs(item, known_character_refs),
            )
        )
    result = tuple(sorted(values, key=lambda value: value.external_episode_id))
    if len({value.external_episode_id for value in result}) != len(result):
        raise ExternalContextNormalizationError("episodes contain duplicate episode IDs")
    return result


def normalize_narrative_context(
    snapshot: ExternalContextSnapshot,
    *,
    asset_response: object,
    episode_response: object,
) -> NormalizedNarrativeContext:
    """Normalize a complete snapshot without forwarding unapproved fields.

    `overallSynopsis` is intentionally not read.  A provider may optionally
    supply `stablePremise`; its separate name is the assertion that it was
    classified upstream as non-spoiler material.
    """

    if type(snapshot) is not ExternalContextSnapshot:  # noqa: E721
        raise TypeError("snapshot must be an exact ExternalContextSnapshot")
    assets = _batch_item(asset_response, snapshot.series_external_id, "asset_response")
    episodes_response = _batch_item(episode_response, snapshot.series_external_id, "episode_response")
    characters = _normalize_characters(assets)
    character_refs = {item.character_ref for item in characters}
    relationships = _normalize_relationships(assets, character_refs)
    episodes = _normalize_episodes(episodes_response, character_refs)
    themes_raw = _field(assets, "themeTags", "keywords", "tags")
    themes = ()
    if type(themes_raw) is list:  # noqa: E721
        theme_values = cast(list[object], themes_raw)
        themes = tuple(
            sorted({text for item in theme_values if (text := _optional_text(item, 80))})
        )
    return NormalizedNarrativeContext(
        snapshot_hash=snapshot.canonical_hash,
        series_external_id=snapshot.series_external_id,
        series_title=_optional_text(_field(assets, "bookName", "book_name", "title"), 280),
        language=_optional_text(_field(assets, "language", "languageCode"), 64),
        stable_premise=_optional_text(_field(assets, "stablePremise", "stable_premise"), 420),
        themes=themes,
        characters=characters,
        relationships=relationships,
        episodes=episodes,
    )
