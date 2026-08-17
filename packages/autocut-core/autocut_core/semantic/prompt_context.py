"""prompt_context — VLM prompt injection strategies for the VLM-first architecture.

Provides functions for building context injection strings that are prepended
to the VLM video analysis prompt. Follows the injection policy defined in
docs/design/vlm-first-architecture.md, section 四.

Key principles:
  - Only cross-window information is injected (synopsis, themes, relationships)
  - Never inject visual traits, dialogue text, or scene descriptions (VLM sees these directly)
  - Episode summary injection is agent-driven (only when confidence is low)
  - Character reference injection is triggered when intro cards are missing (trigger #6)
"""

from __future__ import annotations

from typing import Any


# ── Section builders ──────────────────────────────────────────────────────────


def _format_synopsis_section(synopsis: str) -> str:
    """Format the synopsis section for VLM prompt injection."""
    return f"【全剧背景】\n{synopsis}" if synopsis else ""


def _format_themes_section(themes: list[str] | str) -> str:
    """Format the themes section for VLM prompt injection."""
    if not themes:
        return ""
    if isinstance(themes, list):
        theme_str = "、".join(str(t) for t in themes)
    else:
        theme_str = str(themes)
    return f"【主题关键词】{theme_str}"


def _format_relationships_section(
    relationships: list[dict[str, Any]] | list[str],
) -> str:
    """Format the character relationships section for VLM prompt injection."""
    if not relationships:
        return ""
    if not isinstance(relationships, list):
        return ""
    lines: list[str] = []
    for rel in relationships:
        if isinstance(rel, dict):
            source = rel.get("source") or rel.get("source_name", "")
            target = rel.get("target") or rel.get("target_name", "")
            desc = rel.get("desc") or rel.get("description", "")
            if source and target:
                lines.append(f"  - {source} ↔ {target}" + (f"：{desc}" if desc else ""))
            elif source and desc:
                lines.append(f"  - {source}：{desc}")
        elif isinstance(rel, str):
            lines.append(f"  - {rel}")
    if lines:
        return "【角色关系参考】\n" + "\n".join(lines)
    return ""


# ── Public API ────────────────────────────────────────────────────────────────


def build_global_context_injection(book_id: str, db_client: Any) -> str:
    """Build a Chinese prompt injection string from the global_context table.

    Reads synopsis, themes, and character relationships from the
    ``global_context`` table and formats them as a human-readable Chinese
    injection block. This is prepended to the VLM video analysis prompt.

    Args:
        book_id: The book identifier.
        db_client: A StageDBClient instance. Must have
            ``query_global_context(book_id)``.

    Returns:
        A formatted Chinese string, or an empty string if no data is found.
    """
    if not db_client or not db_client.is_available:
        return ""

    try:
        ctx = db_client.query_global_context(book_id)
    except Exception:
        return ""

    if ctx is None:
        return ""

    synopsis = ctx.get("synopsis") or ""
    themes = ctx.get("themes") or []
    relationships = ctx.get("relationships") or []

    if not synopsis and not themes and not relationships:
        return ""

    parts: list[str] = []

    parts.append(_format_synopsis_section(synopsis))
    parts.append(_format_themes_section(themes))
    parts.append(_format_relationships_section(relationships))

    parts = [p for p in parts if p]

    if not parts:
        return ""

    # ── 尾部注意 ──
    parts.append(
        "注意：以上信息帮助你理解剧情背景，但请以实际视频画面为准。"
        "如果画面和背景信息有冲突，以画面为准。"
    )

    return "\n\n".join(parts)


def should_inject_episode_summary(
    window_id: str, confidence_report: dict[str, Any] | None
) -> bool:
    """Decide whether to inject episode summary into the VLM prompt.

    Inject only when VLM confidence is low, to avoid polluting the prompt
    with potentially conflicting information when VLM is already confident.

    Decision criteria:
      - Low-confidence dialogue ratio > 20%
      - No hard subtitles detected (source_accuracy.agreement != "vlm_override")
      - Multiple unidentified characters (> 2 without intro cards)

    Args:
        window_id: The window identifier.
        confidence_report: The confidence report from confidence_check stage.
            Expected keys: dialogue_confidence_stats, has_hard_subtitles,
            characters_seen, unidentified_characters.

    Returns:
        True if episode summary should be injected.
    """
    if not confidence_report:
        return False

    _ = window_id  # reserved for caller tracing

    # ── Criterion 1: Low-confidence dialogue ratio > 20% ──
    stats = confidence_report.get("dialogue_confidence_stats", {})
    if isinstance(stats, dict):
        total = stats.get("total", 0)
        low = stats.get("low", 0)
        if total > 0 and (low / total) > 0.2:
            return True

    # ── Criterion 2: No hard subtitles ──
    has_hard_subtitles = confidence_report.get("has_hard_subtitles", True)
    if not has_hard_subtitles:
        return True

    # ── Criterion 3: Multiple unidentified characters ──
    unidentified = confidence_report.get("unidentified_characters", [])
    if isinstance(unidentified, list) and len(unidentified) > 2:
        return True

    return False


def build_character_reference_injection(
    window_id: str, book_id: str, db_client: Any
) -> str:
    """Build a character reference injection string for a specific window.

    Reads character names from global_context.relationships and formats
    a Chinese reference table for VLM prompt injection. This is triggered
    by trigger #6 in the confidence_check stage: when characters appear
    without an intro card (source != "title_card") and more than 2
    characters are present.

    Args:
        window_id: The window identifier (e.g. "ep1_w0").
        book_id: The book identifier for querying global_context.
        db_client: A StageDBClient instance. Must have
            ``query_global_context(book_id)``.

    Returns:
        A formatted Chinese string like:

        【角色参考】以下角色可能出现在本窗口（窗口 ep1_w0）：
          - 张三
          - 李四
          - 王五

        or an empty string if no relationship data is found.
    """
    if not db_client or not db_client.is_available:
        return ""

    try:
        ctx = db_client.query_global_context(book_id)
    except Exception:
        return ""

    if ctx is None:
        return ""

    relationships = ctx.get("relationships") or []
    if not relationships:
        return ""

    # Collect unique character names with descriptions from relationships
    chars: dict[str, str] = {}
    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        source = rel.get("source") or rel.get("source_name", "")
        target = rel.get("target") or rel.get("target_name", "")
        desc = rel.get("desc") or rel.get("description", "")

        if source and source not in chars:
            source_desc = rel.get("source_desc", "") or (desc if not target else "")
            chars[source] = source_desc
        if target and target not in chars:
            target_desc = rel.get("target_desc", "")
            chars[target] = target_desc

    if not chars:
        return ""

    lines: list[str] = [
        f"【角色参考】以下角色可能出现在本窗口（窗口 {window_id}）："
    ]
    for name, desc in sorted(chars.items()):
        if desc:
            lines.append(f"  - {name}：{desc}")
        else:
            lines.append(f"  - {name}")

    return "\n".join(lines)