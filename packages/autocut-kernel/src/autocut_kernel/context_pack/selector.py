"""Deterministic, spoiler-safe WindowContextPack selection."""

from __future__ import annotations

from dataclasses import dataclass

from ..media.types import canonical_sha256
from .models import (
    EpisodeContextBinding,
    NormalizedNarrativeContext,
    WindowContextPack,
)

CONTEXT_SELECTION_POLICY_V1 = "context-selection-v1"


@dataclass(frozen=True, slots=True)
class ContextSelectionPolicy:
    version: str = CONTEXT_SELECTION_POLICY_V1
    max_characters: int = 8
    max_relationships: int = 16
    max_themes: int = 6
    max_item_characters: int = 280
    max_total_utf8_bytes: int = 8 * 1024
    max_estimated_tokens: int = 2_000

    def __post_init__(self) -> None:
        if self.version != CONTEXT_SELECTION_POLICY_V1:
            raise ValueError("unregistered context selection policy")
        for name in (
            "max_characters", "max_relationships", "max_themes", "max_item_characters",
            "max_total_utf8_bytes", "max_estimated_tokens",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:  # noqa: E721
                raise ValueError(f"{name} must be a positive integer")

    def to_mapping(self) -> dict[str, object]:
        return {
            "max_characters": self.max_characters,
            "max_estimated_tokens": self.max_estimated_tokens,
            "max_item_characters": self.max_item_characters,
            "max_relationships": self.max_relationships,
            "max_themes": self.max_themes,
            "max_total_utf8_bytes": self.max_total_utf8_bytes,
            "version": self.version,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def _estimate_tokens(text: str) -> int:
    """Stable conservative estimator, independent of provider tokenization."""
    cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    non_cjk_words = len([part for part in text.replace("\n", " ").split(" ") if part])
    return max(1, cjk + non_cjk_words)


def _shorten(text: str, maximum: int) -> str | None:
    if len(text) > maximum:
        return None
    return text


def video_only_window_context_pack(policy: ContextSelectionPolicy, reason: str) -> WindowContextPack:
    return WindowContextPack(
        mode="video_only",
        source_binding_hash=None,
        normalized_context_hash=None,
        selection_policy_version=policy.version,
        selection_policy_hash=policy.canonical_hash,
        known_through_external_episode_ordinal=None,
        selected_refs=(),
        suppressed_reason_counts=(),
        rendered_context="外部剧情辅助不可用；仅依据随附视频观察。",
        video_only_reason_code=reason,
    )


def build_window_context_pack(
    normalized: NormalizedNarrativeContext,
    binding: EpisodeContextBinding,
    *,
    local_source_id: str,
    local_source_sha256: str,
    local_episode_index: int,
    policy: ContextSelectionPolicy | None = None,
) -> WindowContextPack:
    """Select model-visible context only from a complete, explicit binding.

    Binding defects deliberately produce a reproducible `video_only` pack.  A
    VLM window remains runnable, but nothing external can be silently guessed.
    """

    if type(normalized) is not NormalizedNarrativeContext:  # noqa: E721
        raise TypeError("normalized must be an exact NormalizedNarrativeContext")
    if type(binding) is not EpisodeContextBinding:  # noqa: E721
        raise TypeError("binding must be an exact EpisodeContextBinding")
    active_policy = policy or ContextSelectionPolicy()
    if type(active_policy) is not ContextSelectionPolicy:  # noqa: E721
        raise TypeError("policy must be an exact ContextSelectionPolicy")
    if (
        binding.local_source_id != local_source_id
        or binding.local_source_sha256 != local_source_sha256
        or binding.local_episode_index != local_episode_index
    ):
        return video_only_window_context_pack(active_policy, "EXTERNAL_EPISODE_BINDING_MISMATCH")
    if binding.series_external_id != normalized.series_external_id:
        return video_only_window_context_pack(active_policy, "EXTERNAL_SERIES_BINDING_MISMATCH")
    current = next(
        (item for item in normalized.episodes if item.external_episode_id == binding.external_episode_id),
        None,
    )
    if current is None or (
        binding.external_chapter_id is not None
        and current.external_chapter_id != binding.external_chapter_id
    ):
        return video_only_window_context_pack(active_policy, "EXTERNAL_EPISODE_BINDING_MISSING")
    if (
        current.external_episode_ordinal is not None
        and current.external_episode_ordinal != binding.external_episode_ordinal
    ):
        return video_only_window_context_pack(active_policy, "EXTERNAL_EPISODE_ORDINAL_CONFLICT")

    suppressed: dict[str, int] = {}
    items: list[tuple[str, str]] = []

    def add(ref: str, line: str) -> None:
        if _shorten(line, active_policy.max_item_characters) is None:
            suppressed["ITEM_CHAR_LIMIT"] = suppressed.get("ITEM_CHAR_LIMIT", 0) + 1
            return
        proposed = "剧情辅助（不是视频证据；冲突时以视频为准）：\n" + "\n".join(
            [entry for _, entry in items] + [line]
        )
        if (
            len(proposed.encode("utf-8")) > active_policy.max_total_utf8_bytes
            or _estimate_tokens(proposed) > active_policy.max_estimated_tokens
        ):
            suppressed["PACK_BUDGET"] = suppressed.get("PACK_BUDGET", 0) + 1
            return
        items.append((ref, line))

    if normalized.series_title:
        add(f"series:{normalized.series_external_id}", f"作品：{normalized.series_title}")
    if normalized.stable_premise:
        add(f"premise:{normalized.series_external_id}", f"稳定前提：{normalized.stable_premise}")
    if current.title:
        add(f"episode:{current.external_episode_id}", f"当前集标题：{current.title}")
    if current.summary:
        add(f"episode:{current.external_episode_id}", f"当前集摘要：{current.summary}")

    character_by_ref = {item.character_ref: item for item in normalized.characters}
    selected_character_refs = current.character_refs[:active_policy.max_characters]
    for ref in selected_character_refs:
        character = character_by_ref[ref]
        role = f"；定位：{character.role}" if character.role else ""
        aliases = f"；别名：{'、'.join(character.aliases)}" if character.aliases else ""
        add(f"character:{ref}", f"人物：{character.name}{aliases}{role}")
    if len(current.character_refs) > len(selected_character_refs):
        suppressed["CHARACTER_LIMIT"] = len(current.character_refs) - len(selected_character_refs)

    visible_character_refs = set(selected_character_refs)
    relations = tuple(
        item for item in normalized.relationships
        if item.known_from_external_episode_ordinal <= binding.external_episode_ordinal
        and item.subject_character_ref in visible_character_refs
        and item.object_character_ref in visible_character_refs
    )[:active_policy.max_relationships]
    for relationship in relations:
        subject = character_by_ref[relationship.subject_character_ref].name
        object_ = character_by_ref[relationship.object_character_ref].name
        add(
            f"relationship:{relationship.relationship_ref}",
            f"已知关系：{subject} 与 {object_}：{relationship.description}",
        )
    allowed_relation_count = sum(
        item.known_from_external_episode_ordinal <= binding.external_episode_ordinal
        for item in normalized.relationships
    )
    if allowed_relation_count > len(relations):
        suppressed["RELATIONSHIP_LIMIT_OR_SCOPE"] = allowed_relation_count - len(relations)

    for theme in normalized.themes[:active_policy.max_themes]:
        add(f"theme:{theme}", f"主题标签：{theme}")
    if len(normalized.themes) > active_policy.max_themes:
        suppressed["THEME_LIMIT"] = len(normalized.themes) - active_policy.max_themes

    rendered = "剧情辅助（不是视频证据；冲突时以视频为准）：\n" + "\n".join(line for _, line in items)
    return WindowContextPack(
        mode="api_assisted",
        source_binding_hash=binding.canonical_hash,
        normalized_context_hash=normalized.canonical_hash,
        selection_policy_version=active_policy.version,
        selection_policy_hash=active_policy.canonical_hash,
        known_through_external_episode_ordinal=binding.external_episode_ordinal,
        selected_refs=tuple(sorted({ref for ref, _ in items})),
        suppressed_reason_counts=tuple(sorted(suppressed.items())),
        rendered_context=rendered,
    )
