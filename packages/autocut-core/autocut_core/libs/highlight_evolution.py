"""Highlight Skill Evolution — 高光识别 skill 自动进化机制。

数据流:
    vlm_analysis → candidates (type=highlight)
        ↓
    compare_highlights (对比 VLM vs API 高光标记)
        ↓
    analyze_missed_highlight (Agent 分析漏识别原因)
        ↓
    ├─ A. prompt 定义不清 → evolve_highlight_skill 更新 skill 文件
    ├─ B. 评分标准偏严 → evolve_highlight_skill 调整评分权重
    ├─ C. 需要跨窗口上下文 → 标记为 series_registry 补充
    └─ D. API 误判 → 记录但不更新 skill

进化节奏:
    - 积累足够 case (>=5) 后批量分析
    - 连续 2 轮无新漏识别 → skill 稳定
    - 不是每个窗口都进化，而是积累后批量触发
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autocut_core.logging import get_logger

logger = get_logger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────

# 触发 skill 进化的最小累计漏识别数
MIN_MISSED_CASES_TO_EVOLVE = 5

# 连续无新漏识别轮数后 skill 稳定
STABLE_ROUNDS_THRESHOLD = 2

# 时间窗口重叠 IoU 阈值（用于判断 VLM candidate 与 API highlight 是否匹配）
HIGHLIGHT_MATCH_IOU_THRESHOLD = 0.3

# skill 文件路径（相对于 project root）
DEFAULT_SKILL_PATH = "skills/ac_story_generation/references/highlight-recognition.md"

# 默认 skill 模板（当 skill 文件不存在时使用）
DEFAULT_HIGHLIGHT_SKILL_TEMPLATE = """## 核心定义

高光 (Highlight) 是指视频中具有以下特征的片段:

1. **情感强度**: 情感冲突激烈、角色情绪爆发、关系转折
2. **剧情关键**: 推动剧情发展的关键转折点、真相揭示
3. **视觉冲击**: 动作场面、特效场面、视觉震撼
4. **对白精彩**: 经典台词、关键对话、情感独白
5. **观众共鸣**: 容易引发观众情感共鸣的片段

## 判别标准

- strength 1-3: 有一定情感或剧情价值，但非核心
- strength 4-6: 明显的情感冲突或剧情推进
- strength 7-8: 关键转折点或情感爆发
- strength 9-10: 全剧最高光时刻，必须包含

## 评分维度

1. 情感强度 (0-10)
2. 剧情重要性 (0-10)
3. 视觉冲击力 (0-10)
4. 对白质量 (0-10)
5. 观众共鸣度 (0-10)

综合评分 = 各项加权平均后取整。

## 输出格式

```json
{
  "id": "highlight-<source_id>-<序号>",
  "start": <float>,
  "end": <float>,
  "type": "highlight",
  "strength": <1-10>,
  "reason": "<高光理由>",
  "anchor": "<核心锚点描述>",
  "lead_in": "<铺垫上下文>",
  "payoff_or_open_question": "<情感 payoff 或悬念>",
  "dialogue_excerpt": "<关键对白摘录>"
}
```"""

# 漏识别原因分类
MISS_CAUSE_A = "A"  # prompt 定义不清
MISS_CAUSE_B = "B"  # 评分标准偏严
MISS_CAUSE_C = "C"  # 需要跨窗口上下文
MISS_CAUSE_D = "D"  # API 误判

MISS_CAUSE_LABELS: dict[str, str] = {
    MISS_CAUSE_A: "prompt 定义不清",
    MISS_CAUSE_B: "评分标准偏严",
    MISS_CAUSE_C: "需要跨窗口上下文",
    MISS_CAUSE_D: "API 误判",
}

# ── 辅助函数 ──────────────────────────────────────────────────────────────────


def _time_range_overlap(
    a_start: float, a_end: float, b_start: float, b_end: float
) -> float:
    """计算两个时间区间的 IoU (Intersection over Union)。

    IoU = intersection / union
    其中 union = (a_len + b_len - intersection)

    用于判断 VLM 高光与 API 高光是否为同一片段。
    阈值 0.3: IoU >= 0.3 视为匹配。
    """
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    if hi <= lo:
        return 0.0
    intersection = hi - lo
    union = (a_end - a_start) + (b_end - b_start) - intersection
    return intersection / union if union > 0 else 0.0


def _epoch_seconds(ts: Any) -> float | None:
    """将时间戳 (ISO 8601 字符串或 datetime) 转换为 epoch 秒。"""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, datetime):
        return ts.timestamp()
    if isinstance(ts, str):
        # 尝试解析 ISO 8601
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return None
    return None


# ── 公共 API ──────────────────────────────────────────────────────────────────


def _find_nearby_by_iou(
    source_ranges: tuple[float, float],
    target_items: list[dict[str, Any]],
    target_key: str,
    iou_threshold: float,
) -> list[dict[str, Any]]:
    """Find target items whose time range has IoU > 0 but < iou_threshold with source_ranges.

    Args:
        source_ranges: (start, end) of the reference item.
        target_items: List of items to search through. Each item should have
            start/start_time and end/end_time fields.
        target_key: The key name to use in result dicts (e.g., "vlm", "api").
        iou_threshold: IoU threshold for matching.

    Returns:
        List of {target_key: item, "iou": float} sorted by IoU descending.
    """
    s_start, s_end = source_ranges
    nearby: list[dict[str, Any]] = []
    for item in target_items:
        t_start = float(item.get("start", item.get("start_time", 0)))
        t_end = float(item.get("end", item.get("end_time", 0)))
        if t_end <= t_start:
            continue
        iou = _time_range_overlap(s_start, s_end, t_start, t_end)
        if 0 < iou < iou_threshold:
            nearby.append({target_key: item, "iou": round(iou, 4)})
    nearby.sort(key=lambda x: x["iou"], reverse=True)
    return nearby


def compare_highlights(
    vlm_candidates: list[dict[str, Any]],
    api_highlights: list[dict[str, Any]],
    *,
    iou_threshold: float = HIGHLIGHT_MATCH_IOU_THRESHOLD,
) -> dict[str, list[dict[str, Any]]]:
    """对比 VLM candidates (type=highlight) 与 API highlight 标记。

    Args:
        vlm_candidates: VLM 输出的 candidates 列表，只处理 type="highlight" 的项。
        api_highlights: API 分镜中 is_highlight=True 的 shots 列表。
            每个 dict 应包含 start_time, end_time, 可选 highlight_score,
            highlight_reason, scene, subjects 等。
        iou_threshold: 时间窗口重叠 IoU 阈值，超过此值视为匹配。

    Returns:
        {
            "matched": [
                {"vlm": {...}, "api": {...}, "iou": float},
                ...
            ],
            "missed": [
                {"api": {...}, "vlm_candidates_nearby": [...]},
                ...
            ],
            "false_positives": [
                {"vlm": {...}, "api_shots_nearby": [...]},
                ...
            ],
        }
    """
    # 过滤 type=highlight 的 VLM candidates
    vlm_highlights = [
        c for c in vlm_candidates
        if isinstance(c, dict) and c.get("type") == "highlight"
    ]

    # 过滤 is_highlight=True 的 API shots
    api_marks = [
        s for s in api_highlights
        if isinstance(s, dict) and s.get("is_highlight", False)
    ]

    matched: list[dict[str, Any]] = []
    used_vlm: set[int] = set()
    used_api: set[int] = set()

    # 贪心匹配：按 IoU 降序，每对只匹配一次
    pairs: list[tuple[float, int, int]] = []
    for vi, vlm in enumerate(vlm_highlights):
        v_start = float(vlm.get("start", 0))
        v_end = float(vlm.get("end", 0))
        if v_end <= v_start:
            continue
        for ai, api in enumerate(api_marks):
            a_start = float(api.get("start_time", 0))
            a_end = float(api.get("end_time", 0))
            if a_end <= a_start:
                continue
            iou = _time_range_overlap(v_start, v_end, a_start, a_end)
            if iou >= iou_threshold:
                pairs.append((iou, vi, ai))

    pairs.sort(key=lambda x: x[0], reverse=True)

    for iou, vi, ai in pairs:
        if vi in used_vlm or ai in used_api:
            continue
        used_vlm.add(vi)
        used_api.add(ai)
        matched.append({
            "vlm": vlm_highlights[vi],
            "api": api_marks[ai],
            "iou": round(iou, 4),
        })

    # 未匹配的 API highlights → missed
    missed: list[dict[str, Any]] = []
    for ai, api in enumerate(api_marks):
        if ai in used_api:
            continue
        a_start = float(api.get("start_time", 0))
        a_end = float(api.get("end_time", 0))
        # 找附近 VLM candidates（IoU > 0 但低于阈值）
        nearby = _find_nearby_by_iou(
            (a_start, a_end), vlm_highlights, "vlm", iou_threshold
        )
        missed.append({
            "api": api,
            "vlm_candidates_nearby": nearby,
        })

    # 未匹配的 VLM highlights → false positives
    false_positives: list[dict[str, Any]] = []
    for vi, vlm in enumerate(vlm_highlights):
        if vi in used_vlm:
            continue
        v_start = float(vlm.get("start", 0))
        v_end = float(vlm.get("end", 0))
        nearby = _find_nearby_by_iou(
            (v_start, v_end), api_marks, "api", iou_threshold
        )
        false_positives.append({
            "vlm": vlm,
            "api_shots_nearby": nearby,
        })

    return {
        "matched": matched,
        "missed": missed,
        "false_positives": false_positives,
    }


def annotate_highlights_with_scene_boundaries(
    highlights: list[dict[str, Any]],
    scene_boundaries: dict[str, Any],
    episode_id: str,
    *,
    tolerance: float = 0.5,
    lead_in: float = 0.3,
    lead_out: float = 0.0,
    silence_intervals: list[dict[str, float]] | None = None,
    speech_intervals: dict[str, Any] | None = None,
    audio_max_shift: float = 3.0,
) -> list[dict[str, Any]]:
    """Snap highlight time ranges to nearest PySceneDetect boundaries with audio gating.

    For each highlight:
    - Preserve existing ``original_start`` / ``original_end`` if already set
      (e.g. by ``apply_scene_boundary_fusion`` in step 2.5)
    - Snap to scene boundary via ``snap_highlight_start``/``snap_highlight_end``
      - If silence_intervals provided: audio-gated snap (prevents snapping into speech)
      - If silence_intervals is None: pure visual snap (backward compatible)
    - Store snapped values as ``precise_start`` / ``precise_end``

    Args:
        highlights: List of highlight dicts, each with ``start``/``end`` fields.
        scene_boundaries: PySceneDetect output dict with ``episodes`` key.
        episode_id: Episode identifier (maps to a key under ``episodes``).
        tolerance: Max allowed offset in seconds (default 0.5).
        lead_in: Cut-point offset in seconds, skips transition residue (default 0.3).
        lead_out: Cut-point offset for end side (default 0.0).
        silence_intervals: Silence data dict (with episodes key) for audio gating,
            same format as used by apply_scene_boundary_fusion. None = pure visual mode.
        speech_intervals: VAD speech intervals dict (with episodes key) from Demucs+Silero,
            takes priority over silence_intervals when both are provided.
        audio_max_shift: Max audio-adjustment distance in seconds (default 3.0).

    Returns:
        The same list of dicts, mutated in-place with added fields.
    """
    from autocut_core.semantic.scene_boundary_fusion import (
        snap_highlight_start,
        snap_highlight_end,
        extract_cut_points,
    )

    episodes = scene_boundaries.get("episodes", {})
    scene_data = episodes.get(episode_id)

    if not scene_data:
        logger.debug(
            "annotate_highlights: no scene data for episode %s, skipping snap",
            episode_id,
        )
        return highlights

    cut_points = extract_cut_points(scene_data)

    # 从 silence_data 提取当前集的静音区间（与 apply_scene_boundary_fusion 保持一致）
    ep_silence = None
    if silence_intervals is not None:
        from autocut_core.semantic.scene_boundary_fusion import _extract_silence_intervals
        ep_silence = _extract_silence_intervals(silence_intervals, episode_id)

    # 从 VAD speech_intervals 提取当前集的语音区间（优先于 silence_intervals）
    ep_speech = None
    if speech_intervals is not None:
        from autocut_core.semantic.scene_boundary_fusion import _extract_speech_intervals
        ep_speech = _extract_speech_intervals(speech_intervals, episode_id)

    if not cut_points:
        logger.debug(
            "annotate_highlights: no cut points for episode %s, skipping snap",
            episode_id,
        )
        return highlights

    for item in highlights:
        if not isinstance(item, dict):
            continue
        if "start" not in item or "end" not in item:
            continue

        # 不覆盖已有的 original_* 值 (step 2.5 可能已设置真实 VLM 原始值)
        if "original_start" not in item:
            item["original_start"] = item["start"]
        if "original_end" not in item:
            item["original_end"] = item["end"]

        # 使用 forward-snap + lead_in 策略 + 音频门控（VAD语音区间优先，silencedetect为fallback）
        item["precise_start"] = snap_highlight_start(
            item["start"], cut_points,
            tolerance=tolerance, lead_in=lead_in,
            silence_intervals=ep_silence,
            speech_intervals=ep_speech,
            max_shift=audio_max_shift,
        )
        item["precise_end"] = snap_highlight_end(
            item["end"], cut_points,
            tolerance=tolerance, lead_out=lead_out,
            silence_intervals=ep_silence,
            speech_intervals=ep_speech,
            max_shift=audio_max_shift,
        )

    return highlights


def merge_vlm_api_highlights(
    vlm_highlights: list[dict[str, Any]],
    api_highlights: list[dict[str, Any]],
    *,
    iou_threshold: float = 0.3,
) -> dict[str, list[dict[str, Any]]]:
    """Merge VLM and API highlights by IoU matching.

    Matched pairs (IoU >= threshold) are placed in ``merged`` with source
    ``"vlm+api"``.  Unmatched VLM items go to ``vlm_only`` and unmatched API
    items go to ``api_only`` (labelled for skill evolution analysis).

    Uses the existing ``_find_nearby_by_iou`` to find nearby-but-not-matching
    pairs for context.

    Args:
        vlm_highlights: VLM-supplied highlight items (each with ``start``/``end``).
        api_highlights: API-supplied highlight items (each with
            ``start_time``/``end_time``).
        iou_threshold: IoU threshold for a match (default 0.3).

    Returns:
        {
            "merged": [
                {"source": "vlm+api", "vlm": {...}, "api": {...}, "iou": float},
                ...
            ],
            "vlm_only": [
                {"source": "vlm", "vlm": {...}, "api_shots_nearby": [...]},
                ...
            ],
            "api_only": [
                {"source": "api", "api": {...}, "vlm_candidates_nearby": [...]},
                ...
            ],
        }
    """
    merged: list[dict[str, Any]] = []
    used_vlm: set[int] = set()
    used_api: set[int] = set()

    # Build all IoU pairs
    pairs: list[tuple[float, int, int]] = []
    for vi, vlm in enumerate(vlm_highlights):
        v_start = float(vlm.get("start", 0))
        v_end = float(vlm.get("end", 0))
        if v_end <= v_start:
            continue
        for ai, api in enumerate(api_highlights):
            a_start = float(api.get("start_time", 0))
            a_end = float(api.get("end_time", 0))
            if a_end <= a_start:
                continue
            iou = _time_range_overlap(v_start, v_end, a_start, a_end)
            if iou >= iou_threshold:
                pairs.append((iou, vi, ai))

    pairs.sort(key=lambda x: x[0], reverse=True)

    for iou, vi, ai in pairs:
        if vi in used_vlm or ai in used_api:
            continue
        used_vlm.add(vi)
        used_api.add(ai)
        merged.append({
            "source": "vlm+api",
            "vlm": vlm_highlights[vi],
            "api": api_highlights[ai],
            "iou": round(iou, 4),
        })

    # Unmatched VLM items
    vlm_only: list[dict[str, Any]] = []
    for vi, vlm in enumerate(vlm_highlights):
        if vi in used_vlm:
            continue
        v_start = float(vlm.get("start", 0))
        v_end = float(vlm.get("end", 0))
        nearby = _find_nearby_by_iou(
            (v_start, v_end), api_highlights, "api", iou_threshold
        )
        vlm_only.append({
            "source": "vlm",
            "vlm": vlm,
            "api_shots_nearby": nearby,
        })

    # Unmatched API items
    api_only: list[dict[str, Any]] = []
    for ai, api in enumerate(api_highlights):
        if ai in used_api:
            continue
        a_start = float(api.get("start_time", 0))
        a_end = float(api.get("end_time", 0))
        nearby = _find_nearby_by_iou(
            (a_start, a_end), vlm_highlights, "vlm", iou_threshold
        )
        api_only.append({
            "source": "api",
            "api": api,
            "vlm_candidates_nearby": nearby,
        })

    return {
        "merged": merged,
        "vlm_only": vlm_only,
        "api_only": api_only,
    }


def build_ranking_context(
    book_id: str,
    db_client: Any,
) -> str:
    """Read all highlights from the shots table for a book and format a
    ranking prompt for the Agent.

    The prompt includes:
    - All highlight entries with episode, time range, scene, subjects, score,
      and reason.
    - Ranking criteria (emotional intensity, plot importance, visual impact,
      dialogue quality, audience resonance).

    Args:
        book_id: Book identifier.
        db_client: A ``StageDBClient`` instance (or compatible object with a
            ``query_highlights(book_id)`` method).

    Returns:
        A formatted Chinese string suitable for the Agent's ranking prompt.
    """
    highlights = db_client.query_highlights(book_id)

    if not highlights:
        return (
            "【高光排名任务】\n\n"
            "当前书号 {book_id} 暂无高光记录，请先运行高光识别阶段。\n"
        ).format(book_id=book_id)

    lines: list[str] = [
        "【高光排名任务】\n",
        "以下是从全剧识别出的所有高光片段。请根据以下标准进行全局排名：\n",
        "排名标准：",
        "  1. 情感强度 —— 情感冲突的激烈程度、角色情绪爆发、关系转折",
        "  2. 剧情重要性 —— 对剧情发展的推动作用、关键转折点",
        "  3. 视觉冲击力 —— 动作场面、特效场面、视觉震撼",
        "  4. 对白质量 —— 经典台词、关键对话、情感独白",
        "  5. 观众共鸣度 —— 引发观众情感共鸣的能力",
        "",
        "输出格式：为每个高光分配 global_rank（1=最佳），rank_score（0-100），",
        "以及 rank_criteria（每个维度的评分 json）。",
        "",
        "---",
        "待排名高光片段：",
        "",
    ]

    for i, h in enumerate(highlights, 1):
        episode = h.get("episode_id", "?")
        start = h.get("start_time", 0)
        end = h.get("end_time", 0)
        scene = h.get("scene", "未知场景")
        subjects = h.get("subjects", [])
        if isinstance(subjects, str):
            import json
            try:
                subjects = json.loads(subjects)
            except (json.JSONDecodeError, TypeError):
                subjects = [subjects]
        subjects_str = ", ".join(subjects) if subjects else "未知角色"
        score = h.get("highlight_score", "?")
        reason = h.get("highlight_reason", "无")

        lines.append(
            f"[{i}] 第{episode}集 | {start:.1f}s-{end:.1f}s | "
            f"场景: {scene} | 角色: {subjects_str} | "
            f"原始评分: {score} | 理由: {reason}"
        )

    lines.append("")
    lines.append("---")
    lines.append("请基于以上信息，对 {n} 个高光片段进行全局排名。".format(n=len(highlights)))

    return "\n".join(lines)


def _check_cause_d_api_misjudgment(
    api: dict[str, Any],
) -> dict[str, Any] | None:
    """Check if the missed highlight is due to API misjudgment.

    Returns a cause dict if the API score is too low or the reason is too short,
    otherwise None.
    """
    highlight_score = api.get("highlight_score")
    highlight_reason = api.get("highlight_reason", "")
    subjects = api.get("subjects", [])
    scene = api.get("scene", "")

    if isinstance(highlight_score, (int, float)) and float(highlight_score) < 3:
        return {
            "cause": MISS_CAUSE_D,
            "reason": (
                f"API 高光评分过低 (score={highlight_score})，"
                f"scenes={scene or '未知'}，subjects={subjects}。"
                f"VLM 未识别该段为高光，可能 API 标记价值有限。"
            ),
            "suggestion": "忽略此 API 标记，不更新 skill。",
            "confidence": 0.8,
        }

    if not highlight_reason or len(highlight_reason.strip()) < 10:
        return {
            "cause": MISS_CAUSE_D,
            "reason": (
                f"API 高光缺少明确理由 (reason='{highlight_reason}')，"
                f"scene={scene or '未知'}。VLM 未识别可能因为该段确实缺乏高光特征。"
            ),
            "suggestion": "忽略此 API 标记，不更新 skill。",
            "confidence": 0.7,
        }

    return None


def _check_cause_c_cross_window(
    api: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any] | None:
    """Check if the missed highlight is due to cross-window context insufficiency.

    Returns a cause dict if the highlight is near a window boundary and the
    window has few story beats, otherwise None.
    """
    api_start = float(api.get("start_time", 0))
    api_end = float(api.get("end_time", 0))
    story_beats = context.get("story_beats", [])
    window_start = context.get("window_start", 0)
    window_end = context.get("window_end", 0)

    is_at_boundary = (
        (api_start <= window_start + 5)
        or (api_end >= window_end - 5)
    )

    if is_at_boundary and len(story_beats) <= 2:
        return {
            "cause": MISS_CAUSE_C,
            "reason": (
                f"高光段 ({api_start}-{api_end}) 位于窗口边界，"
                f"窗口内 story_beats 仅有 {len(story_beats)} 个，"
                f"缺乏足够上下文。可能需要在 series_registry 阶段补充跨窗口信息。"
            ),
            "suggestion": (
                "在 series_registry 阶段，将相邻窗口的 story_beats 和 "
                "character_relationships 注入后重新评估此段。"
            ),
            "confidence": 0.75,
        }

    return None


def _check_cause_b_scoring_strictness(
    api: dict[str, Any],
    nearby: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Check if the missed highlight is due to scoring threshold strictness.

    Returns a cause dict if a nearby VLM candidate has strength >= 5 but was
    not matched as a highlight, otherwise None.
    """
    highlight_reason = api.get("highlight_reason", "")

    if not nearby:
        return None

    best_nearby = nearby[0]
    vlm_reason = best_nearby.get("vlm", {}).get("reason", "")
    vlm_strength = best_nearby.get("vlm", {}).get("strength", 0)
    iou = best_nearby.get("iou", 0)

    if isinstance(vlm_strength, (int, float)) and float(vlm_strength) >= 5:
        return {
            "cause": MISS_CAUSE_B,
            "reason": (
                f"VLM 识别到了附近候选 (strength={vlm_strength}, iou={iou:.2f})，"
                f"但未达到高光阈值。VLM 理由: '{vlm_reason}'。"
                f"API 理由: '{highlight_reason}'。"
            ),
            "suggestion": (
                f"考虑降低 strength 阈值或放宽时间匹配要求。"
                f"当前 IoU={iou:.2f} 低于阈值，可能因为时间边界精度差异。"
            ),
            "confidence": 0.7,
        }

    return None


def _check_cause_a_prompt_definition(
    api: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any] | None:
    """Check if the missed highlight is due to prompt definition ambiguity.

    Always returns a cause dict (default/fallback cause). If the window summary
    contains highlight keywords, provides a higher-confidence reason; otherwise
    returns a lower-confidence unknown reason.
    """
    highlight_reason = api.get("highlight_reason", "")
    subjects = api.get("subjects", [])
    scene = api.get("scene", "")
    window_summary = context.get("window_summary", "")
    story_beats = context.get("story_beats", [])

    highlight_keywords = [
        "高光", "高潮", "冲突", "转折", "情感爆发", "对峙", "决裂",
        "告白", "牺牲", "背叛", "反转", "揭示", "意外", "决战",
        "climax", "conflict", "confrontation", "betrayal", "reveal",
        "sacrifice", "emotional", "peak", "turning point",
    ]
    has_keyword = any(
        kw in window_summary.lower()
        or any(kw in str(b.get("summary", "")).lower() for b in story_beats)
        for kw in highlight_keywords
    )

    if has_keyword:
        return {
            "cause": MISS_CAUSE_A,
            "reason": (
                f"窗口摘要包含高光关键词但 VLM 未识别为高光候选。"
                f"API 标记理由: '{highlight_reason}'。"
                f"窗口摘要: '{window_summary[:200]}...' (截断)。"
                f"可能 VLM 的 highlight 定义不够明确，未能将此类型场景识别为高光。"
            ),
            "suggestion": (
                "在 highlight-recognition skill 中补充此类型场景的定义，"
                "给出更具体的判别标准和示例。"
            ),
            "confidence": 0.65,
        }

    return {
        "cause": MISS_CAUSE_A,
        "reason": (
            f"VLM 完全未识别此段为高光。"
            f"API 标记: scene={scene}, subjects={subjects}, "
            f"reason='{highlight_reason}'。"
            f"窗口摘要: '{window_summary[:200]}...' (截断)。"
        ),
        "suggestion": (
            "在 highlight-recognition skill 中补充此类场景的判别标准，"
            "使 VLM 能识别此类高光模式。"
        ),
        "confidence": 0.5,
    }


def analyze_missed_highlight(
    missed_highlight: dict[str, Any],
    window_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """分析 VLM 漏识别 API 高光的原因。

    Args:
        missed_highlight: compare_highlights 返回的 missed 列表中的一项。
            格式: {"api": {...}, "vlm_candidates_nearby": [...]}
        window_context: 可选的窗口上下文信息，包含:
            - window_summary: 窗口摘要
            - story_beats: 该窗口的 story beats
            - dialogue_and_text: 对白列表
            - visual_events: 视觉事件列表
            - global_context: 全剧全局上下文

    Returns:
        {
            "cause": "A" | "B" | "C" | "D",
            "reason": str,        # 人类可读的原因描述
            "suggestion": str,    # 改进建议
            "confidence": float,  # 0.0-1.0
        }
    """
    api = missed_highlight.get("api", {})
    nearby = missed_highlight.get("vlm_candidates_nearby", [])
    context = window_context or {}

    checkers: list[Callable[[], dict[str, Any] | None]] = [
        lambda: _check_cause_d_api_misjudgment(api),
        lambda: _check_cause_c_cross_window(api, context),
        lambda: _check_cause_b_scoring_strictness(api, nearby),
        lambda: _check_cause_a_prompt_definition(api, context),
    ]

    for checker in checkers:
        result = checker()
        if result is not None:
            return result

    # Unreachable: _check_cause_a_prompt_definition always returns a dict
    return {
        "cause": MISS_CAUSE_A,
        "reason": "未知原因。",
        "suggestion": "在 highlight-recognition skill 中补充此类场景的判别标准。",
        "confidence": 0.0,
    }


def evolve_highlight_skill(
    accumulated_misses: list[dict[str, Any]],
    current_skill_path: str | Path | None = None,
    *,
    dry_run: bool = False,
    db_client: Any = None,
    book_id: str | None = None,
) -> dict[str, Any] | None:
    """当积累足够 case 后，批量分析并更新 highlight-recognition skill。

    Args:
        accumulated_misses: analyze_missed_highlight 的分析结果列表。
            每个元素应包含 cause, reason, suggestion, confidence 字段。
        current_skill_path: 当前 skill 文件路径。如果为 None，使用默认路径。
        dry_run: 如果为 True，只分析不实际更新文件。
        db_client: StageDBClient 实例（可选，用于记录进化）。
        book_id: 剧集 ID（可选，用于 DB 记录）。

    Returns:
        None 如果积累不足或无需进化。
        否则返回:
        {
            "skill_version": "v2",
            "changes": [...],
            "cases_analyzed": N,
            "skill_path": "...",
            "previous_version": "v1",
        }
    """
    # ── 1. 纯计算: 生成变更描述 ──
    changes = _compute_skill_changes(accumulated_misses)
    if changes is None:
        return None

    # ── 2. 读取当前 skill 文件 ──
    if current_skill_path is None:
        skill_path = Path(DEFAULT_SKILL_PATH)
    else:
        skill_path = Path(current_skill_path)

    current_content = ""
    previous_version = "v1"
    if skill_path.exists():
        current_content = skill_path.read_text(encoding="utf-8")
        for line in current_content.split("\n"):
            if line.startswith("version:") or line.startswith("# version:"):
                previous_version = line.split(":", 1)[1].strip()
                break

    # 确定新版本号
    version_parts = previous_version.lstrip("v").split(".")
    try:
        major = int(version_parts[0])
        new_version = f"v{major + 1}"
    except (ValueError, IndexError):
        new_version = "v2"

    # ── 3. 组装完整结果 ──
    result: dict[str, Any] = {
        "skill_version": new_version,
        "changes": changes["changes"],
        "cases_analyzed": changes["cases_analyzed"],
        "cause_distribution": changes["cause_distribution"],
        "skill_path": str(skill_path),
        "previous_version": previous_version,
    }

    # ── 4. I/O: 写文件 + 记录 DB ──
    _apply_skill_changes(
        result=result,
        current_content=current_content,
        new_version=new_version,
        previous_version=previous_version,
        skill_path=skill_path,
        dry_run=dry_run,
        db_client=db_client,
        book_id=book_id,
        accumulated_misses=accumulated_misses,
    )

    return result


def _compute_skill_changes(
    accumulated_misses: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Pure computation: analyze accumulated misses and generate change descriptions.

    Args:
        accumulated_misses: analyze_missed_highlight 的分析结果列表。

    Returns:
        None if not enough cases or no actionable items.
        Otherwise a dict with:
        {
            "changes": [...],
            "cases_analyzed": N,
            "cause_distribution": {...},
            "actionable": [...],
        }
    """
    if len(accumulated_misses) < MIN_MISSED_CASES_TO_EVOLVE:
        logger.info(
            "highlight_evolution: 累计漏识别 %d 条，未达到触发阈值 %d，跳过。",
            len(accumulated_misses),
            MIN_MISSED_CASES_TO_EVOLVE,
        )
        return None

    # 按原因分类统计
    cause_counts = Counter(m.get("cause") for m in accumulated_misses)

    # 过滤掉 D 类 (API 误判)，D 类不触发 skill 进化
    actionable = [
        m for m in accumulated_misses
        if m.get("cause") in (MISS_CAUSE_A, MISS_CAUSE_B, MISS_CAUSE_C)
    ]

    if not actionable:
        logger.info(
            "highlight_evolution: 所有漏识别均为 API 误判 (D 类)，无需更新 skill。"
        )
        return None

    # 生成变更描述
    changes: list[dict[str, Any]] = []
    a_cases = [m for m in actionable if m.get("cause") == MISS_CAUSE_A]
    b_cases = [m for m in actionable if m.get("cause") == MISS_CAUSE_B]
    c_cases = [m for m in actionable if m.get("cause") == MISS_CAUSE_C]

    if a_cases:
        new_scenarios = []
        for case in a_cases[:5]:
            reason = case.get("reason", "")
            if "API 标记理由:" in reason:
                api_reason = reason.split("API 标记理由:", 1)[1].split("。")[0].strip()
                new_scenarios.append(f"  - {api_reason}")
        changes.append({
            "cause": MISS_CAUSE_A,
            "description": (
                f"补充 {len(a_cases)} 个遗漏的高光场景定义到 prompt 中，"
                f"使 VLM 能识别此类高光模式。"
            ),
            "updated_section": "highlight 定义与判别标准",
            "new_scenarios": new_scenarios,
        })

    if b_cases:
        vlm_strengths = []
        for case in b_cases:
            reason = case.get("reason", "")
            if "strength=" in reason:
                try:
                    s = int(reason.split("strength=", 1)[1].split(",")[0])
                    vlm_strengths.append(s)
                except (ValueError, IndexError):
                    pass
        avg_strength = (
            sum(vlm_strengths) / len(vlm_strengths) if vlm_strengths else 0
        )
        changes.append({
            "cause": MISS_CAUSE_B,
            "description": (
                f"VLM 识别到但评分偏低，平均 strength={avg_strength:.1f}。"
                f"建议放宽评分标准或降低高光阈值。"
            ),
            "updated_section": "highlight 评分权重",
            "suggested_threshold_adjustment": max(1, 10 - int(avg_strength)),
        })

    if c_cases:
        changes.append({
            "cause": MISS_CAUSE_C,
            "description": (
                f"{len(c_cases)} 个遗漏需要跨窗口上下文，"
                f"已在 series_registry 阶段标记。"
            ),
            "updated_section": "跨窗口上下文补充",
            "action": "series_registry 补充",
        })

    return {
        "changes": changes,
        "cases_analyzed": len(accumulated_misses),
        "cause_distribution": dict(cause_counts),
        "actionable": actionable,
    }


def _apply_skill_changes(
    *,
    result: dict[str, Any],
    current_content: str,
    new_version: str,
    previous_version: str,
    skill_path: Path,
    dry_run: bool = False,
    db_client: Any = None,
    book_id: str | None = None,
    accumulated_misses: list[dict[str, Any]] | None = None,
) -> None:
    """I/O layer: write updated skill file and record to DB.

    Args:
        result: The result dict from evolve_highlight_skill (mutated in-place).
        current_content: Current skill file content.
        new_version: New version string.
        previous_version: Previous version string.
        skill_path: Path to the skill file.
        dry_run: If True, skip file write and DB recording.
        db_client: StageDBClient instance (optional).
        book_id: Book ID (optional).
        accumulated_misses: Original accumulated misses for DB recording.
    """
    changes = result["changes"]

    # 生成更新后的 skill 内容
    if not dry_run:
        new_content = _build_updated_skill_content(
            current_content, new_version, changes, previous_version
        )
        skill_path.write_text(new_content, encoding="utf-8")
        logger.info(
            "highlight_evolution: skill 文件已更新 %s -> %s (%s)",
            previous_version,
            new_version,
            skill_path,
        )
        result["skill_updated"] = True
    else:
        result["skill_updated"] = False
        result["_preview_content"] = _build_updated_skill_content(
            current_content, new_version, changes, previous_version
        )[:500] + "..."

    # 记录到 DB
    if db_client is not None and db_client.is_available and not dry_run:
        _record_evolution_to_db(
            db_client=db_client,
            book_id=book_id,
            new_version=new_version,
            accumulated_misses=accumulated_misses or [],
        )


def _record_evolution_to_db(
    *,
    db_client: Any,
    book_id: str | None,
    new_version: str,
    accumulated_misses: list[dict[str, Any]],
) -> None:
    """Record highlight evolution entries to the database."""
    import json as _json

    for miss in accumulated_misses:
        cause = miss.get("cause", "unknown")
        if cause == MISS_CAUSE_D:
            continue

        db_client.record_highlight_evolution(
            skill_version=new_version,
            window_id=miss.get("window_id", "unknown"),
            api_highlight=miss.get("api_highlight", {}),
            vlm_miss_reason=miss.get("reason", ""),
            skill_update=_json.dumps({
                "cause": cause,
                "suggestion": miss.get("suggestion", ""),
            }, ensure_ascii=False),
        )
    logger.info(
        "highlight_evolution: 已记录 %d 条进化记录到 DB",
        len([m for m in accumulated_misses if m.get("cause") != MISS_CAUSE_D]),
    )


def _build_updated_skill_content(
    current_content: str,
    new_version: str,
    changes: list[dict[str, Any]],
    previous_version: str,
) -> str:
    """构建更新后的 skill 文件内容。"""
    evolution_header = f"""# Highlight Recognition Skill

version: {new_version}
previous_version: {previous_version}
last_updated: {datetime.now(timezone.utc).isoformat()}

## Evolution History

本次进化原因:
"""
    for change in changes:
        cause_label = MISS_CAUSE_LABELS.get(change["cause"], change["cause"])
        evolution_header += f"- [{change['cause']}] {cause_label}: {change['description']}\n"

    # 如果有 A 类变更，补充具体场景
    for change in changes:
        if change["cause"] == MISS_CAUSE_A and "new_scenarios" in change:
            evolution_header += "\n新增高光场景:\n"
            for scenario in change["new_scenarios"]:
                evolution_header += f"{scenario}\n"

    evolution_header += "\n---\n\n"

    # 如果已有内容，保留原有核心定义（在第一个 --- 之后的内容）
    if current_content and "---" in current_content:
        # 保留原有内容中 "---" 之后的部分
        parts = current_content.split("---", 1)
        if len(parts) > 1:
            existing_body = parts[1].lstrip("\n")
            return evolution_header + existing_body

    # 如果没有已有内容，生成默认 skill 模板
    return evolution_header + DEFAULT_HIGHLIGHT_SKILL_TEMPLATE


# ── 便捷函数：在 series_registry 阶段调用 ─────────────────────────────────────


def run_highlight_diff_for_windows(
    vlm_candidates_by_window: dict[str, list[dict[str, Any]]],
    api_highlights_by_window: dict[str, list[dict[str, Any]]],
    window_contexts: dict[str, dict[str, Any]] | None = None,
    *,
    db_client: Any = None,
    book_id: str | None = None,
    skill_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """在 series_registry 阶段批量运行高光对比。

    对所有窗口执行 compare_highlights → analyze_missed_highlight，
    收集所有 missed 结果后判断是否需要触发 evolve_highlight_skill。

    Args:
        vlm_candidates_by_window: {window_id: [candidates]}
        api_highlights_by_window: {window_id: [api_shots_with_is_highlight]}
        window_contexts: {window_id: {window_summary, story_beats, ...}}
        db_client: StageDBClient 实例
        book_id: 剧集 ID
        skill_path: skill 文件路径
        dry_run: 如果为 True，不实际更新 skill 文件

    Returns:
        {
            "windows_processed": N,
            "total_matched": N,
            "total_missed": N,
            "total_false_positives": N,
            "all_missed_analyses": [...],
            "evolution_result": {...} | None,
        }
    """
    contexts = window_contexts or {}
    all_missed_analyses: list[dict[str, Any]] = []
    total_matched = 0
    total_missed = 0
    total_false_positives = 0

    for window_id in vlm_candidates_by_window:
        vlm_candidates = vlm_candidates_by_window.get(window_id, [])
        api_highlights = api_highlights_by_window.get(window_id, [])
        ctx = contexts.get(window_id, {})

        diff = compare_highlights(vlm_candidates, api_highlights)

        total_matched += len(diff["matched"])
        total_missed += len(diff["missed"])
        total_false_positives += len(diff["false_positives"])

        for missed in diff["missed"]:
            analysis = analyze_missed_highlight(missed, ctx)
            analysis["window_id"] = window_id
            analysis["api_highlight"] = missed["api"]
            all_missed_analyses.append(analysis)

    logger.info(
        "highlight_evolution: 处理 %d 个窗口, "
        "matched=%d, missed=%d, false_positives=%d",
        len(vlm_candidates_by_window),
        total_matched,
        total_missed,
        total_false_positives,
    )

    evolution_result = None
    if all_missed_analyses:
        evolution_result = evolve_highlight_skill(
            all_missed_analyses,
            current_skill_path=skill_path,
            dry_run=dry_run,
            db_client=db_client,
            book_id=book_id,
        )

    return {
        "windows_processed": len(vlm_candidates_by_window),
        "total_matched": total_matched,
        "total_missed": total_missed,
        "total_false_positives": total_false_positives,
        "all_missed_analyses": all_missed_analyses,
        "evolution_result": evolution_result,
    }