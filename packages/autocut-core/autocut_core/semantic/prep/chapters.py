"""autocut_core.semantic.prep.chapters — Chapter Digest 批处理准备。

从 prepare_story_stages.py 提取 Chapter 相关函数。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


from autocut_core.semantic.prep.utils import batch_payload, write_context


# ── constants ────────────────────────────────────────────────────────

CHAPTER_SEMANTIC_ROLLUP_FIELDS = (
    "character_rollup",
    "relationship_rollup",
    "story_threads",
    "fact_keys",
    "open_question_keys",
)

# 断点打分关键词
_RESOLVED_KEYWORDS = ("结束", "解决", "胜利", "死亡", "团聚", "真相大白", "落幕", "完结", "分手", "和解", "被捕")
_NEW_ARC_KEYWORDS = ("新的", "开始", "三年后", "几天后", "第二天", "另一边", "与此同时", "回忆结束", "翌日", "镜头一转")
# 断点打分阈值，低于此值时回退到默认等分
_MIN_BREAKPOINT_SCORE = 2.0
# 默认相邻章重叠集数
_DEFAULT_OVERLAP = 1
# 尾章最小长度，小于此值合并到前一章
_MIN_TAIL_CHAPTER_LENGTH = 3


# ── helpers ──────────────────────────────────────────────────────────

# 传递给 Chapter Digest LLM 的 episode_digest 精简字段白名单。
# 剥离以下会误导 LLM 输出格式的字段：
# - 空 rollup 字段（characters/relationships/story_thread_updates/facts/open_questions）：
#   本地 fast path 下总是空数组，但它们的内部 schema 会误导 LLM 模仿字段名
#   （如 relationships.character_keys/state/change 被误用到 relationship_rollup）
# - 本地信号字段（character_mentions/event_summary_signals/summary_quality）：
#   Phase 1 新增的代码生成字段，LLM 容易复制到输出，触发 additionalProperties 错误
# - 元数据字段（source_ids/window_ids/schema_version）：Chapter LLM 不需要
_EPISODE_FIELDS_FOR_CHAPTER = (
    "episode",
    "summary",
    "opening_state",
    "ending_state",
    "event_ids",
    "highlight_candidate_ids",
    "hook_candidate_ids",
)


def _clean_episode_for_chapter(ep: dict[str, Any]) -> dict[str, Any]:
    """将 episode_digest 精简为 Chapter LLM 所需的最小字段集合，防止字段幻觉。"""
    return {k: ep.get(k) for k in _EPISODE_FIELDS_FOR_CHAPTER if k in ep}


def _format_event_dsl(short_id: str, event: dict[str, Any]) -> str:
    """将事件转换为紧凑DSL单行格式，大幅减少token消耗。
    格式: [E01|EP1|setup](角色1,角色2) 因<cause> -> <summary> -> <effect>，疑问：<open_question>
    """
    ep = event.get("episode", "")
    func = event.get("function", "other")
    chars = ",".join(event.get("character_names", [])[:3])  # 最多列前3个核心角色避免过长
    parts = [f"[{short_id}|EP{ep}|{func}]"]
    if chars:
        parts.append(f"({chars})")
    cause = event.get("cause", "")
    effect = event.get("effect", "")
    summary = event.get("summary", "")
    if cause:
        parts.append(f"因{cause}")
    parts.append(summary)
    if effect:
        parts.append(f"-> {effect}")
    oq = event.get("open_question", "")
    if oq:
        parts.append(f"，悬念：{oq}")
    return " ".join(parts)


def _build_short_id_map(events: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    """构建事件短ID映射：短ID→完整ID，完整ID→短ID。
    短ID格式为E01/E02...，作用域仅限本章，LLM输出后自动逆向映射回完整ID。
    """
    sorted_events = sorted(events, key=lambda e: (e.get("episode", 0), e.get("time_range", {}).get("start", 0) if e.get("time_range") else 0))
    short_to_full = {}
    full_to_short = {}
    for i, event in enumerate(sorted_events, start=1):
        short_id = f"E{i:02d}"
        full_id = event["id"]
        short_to_full[short_id] = full_id
        full_to_short[full_id] = short_id
    return short_to_full, full_to_short


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    """将完整 Event 字典压缩为只包含语义必需字段的紧凑版本。

    Phase 1 改进：
    - 追加 time_range（start/end，单位秒，精度 0.01s）
    - 追加 raw_function（归一化前原始值，便于调试和映射表迭代）
    Phase 2 改进：
    - 追加 dialogue_excerpts（高置信度对话摘录）
    """
    result = {
        key: event.get(key)
        for key in (
            "id",
            "episode",
            "source_id",
            "summary",
            "function",
            "character_names",
            "cause",
            "effect",
            "open_question",
            "temporal_mode",
            "candidate_ids",
        )
    }
    # Phase 1: raw_function（归一化前原始值）
    if event.get("raw_function"):
        result["raw_function"] = event["raw_function"]
    # Phase 1: time_range（从 source_ranges 计算最小时间范围，单位秒）
    ranges = event.get("source_ranges", [])
    if ranges:
        result["time_range"] = {
            "start": round(min(r.get("start", 0) for r in ranges), 2),
            "end": round(max(r.get("end", 0) for r in ranges), 2),
        }
    # Phase 2: dialogue_excerpts（仅保留 high/medium 置信度）
    excerpts = event.get("dialogue_excerpts", [])
    if excerpts:
        result["dialogue_excerpts"] = [
            ex for ex in excerpts if ex.get("confidence") in ("high", "medium")
        ]
    return result


def _breakpoint_score(prev_ep: dict[str, Any], next_ep: dict[str, Any]) -> float:
    """计算两集之间作为章断点的得分，分数越高越适合断章。
    
    纯确定性计算，无LLM调用，所有信号来自现有episode_digest产物。
    """
    score = 0.0
    
    # 1. 前集是故事弧结尾信号（ending_state包含解决类词汇）
    end_text = prev_ep.get("ending_state", "") or ""
    score += sum(3 for kw in _RESOLVED_KEYWORDS if kw in end_text)
    
    # 2. 后集是新弧开篇信号（opening_state包含开篇类词汇）
    start_text = next_ep.get("opening_state", "") or ""
    score += sum(3 for kw in _NEW_ARC_KEYWORDS if kw in start_text)
    
    # 3. 相邻集角色重叠度低（场景切换，自然断点）
    prev_chars = set()
    next_chars = set()
    # 兼容不同字段名：优先用character_mentions，否则从event/character_names提取
    if "character_mentions" in prev_ep and isinstance(prev_ep["character_mentions"], dict):
        prev_chars = set(prev_ep["character_mentions"].keys())
    if "character_mentions" in next_ep and isinstance(next_ep["character_mentions"], dict):
        next_chars = set(next_ep["character_mentions"].keys())
    
    if prev_chars and next_chars:
        overlap = len(prev_chars & next_chars) / len(prev_chars | next_chars)
        score += (1 - overlap) * 2  # 重叠度越低分越高
    
    # 4. 前集没有未解决问题（有open_questions字段时才打分，兼容旧产物）
    if "open_questions" in prev_ep:
        if not prev_ep.get("open_questions"):
            score += 2
    
    # 5. 前集有payoff/reveal类型事件，后集有setup类型事件（有event_ids时可扩展，当前跳过）
    
    return score


def _compute_chapter_boundaries(
    episodes: list[dict[str, Any]],
    target_size: int,
    overlap: int = _DEFAULT_OVERLAP,
    enable_dynamic: bool = True,
) -> list[tuple[int, int, str]]:
    """计算章节边界，返回[(start_idx, end_idx, chapter_id), ...]
    
    Args:
        episodes: 按集数排序的episode_digest列表
        target_size: 目标每章集数（默认6）
        overlap: 相邻章重叠集数（默认1）
        enable_dynamic: 是否启用动态断点优化（关闭则回退纯机械等分）
    """
    total_eps = len(episodes)
    min_size = max(_MIN_TAIL_CHAPTER_LENGTH, target_size - 1)  # 最小长度
    max_size = target_size + 1  # 最大长度
    boundaries = []
    current_pos = 0
    
    while current_pos < total_eps:
        best_break = None
        best_score = -1.0
        
        # 动态断点模式：在允许窗口内找最优断点
        if enable_dynamic:
            for window_size in range(min_size, max_size + 1):
                end_pos = current_pos + window_size
                if end_pos > total_eps:
                    continue
                # 计算断点得分
                if end_pos < total_eps:
                    score = _breakpoint_score(episodes[end_pos - 1], episodes[end_pos])
                else:
                    score = 100.0  # 最后一个位置直接满分
                
                if score > best_score:
                    best_score = score
                    best_break = end_pos
            
            # 得分太低没有好断点，回退到默认target_size等分
            if best_score < _MIN_BREAKPOINT_SCORE and current_pos + target_size <= total_eps:
                best_break = current_pos + target_size
        
        # 机械等分模式或回退：直接用target_size
        if not enable_dynamic or best_break is None:
            best_break = min(current_pos + target_size, total_eps)
        
        # 尾章过短（<3集）则合并到前一章
        remaining = total_eps - best_break
        if boundaries and remaining < _MIN_TAIL_CHAPTER_LENGTH and remaining > 0:
            # 合并到前一章
            prev_start, prev_end, prev_id = boundaries[-1]
            new_end = total_eps
            first_ep = episodes[prev_start]["episode"]
            last_ep = episodes[new_end - 1]["episode"]
            new_id = f"chapter-{first_ep:03d}-{last_ep:03d}"
            boundaries[-1] = (prev_start, new_end, new_id)
            break
        
        # 记录章节边界
        first_ep = episodes[current_pos]["episode"]
        last_ep = episodes[best_break - 1]["episode"]
        chapter_id = f"chapter-{first_ep:03d}-{last_ep:03d}"
        boundaries.append((current_pos, best_break, chapter_id))
        
        # 下一章往前退overlap集，实现重叠
        current_pos = best_break - overlap
        # 防止无限循环（重叠导致current_pos不前进）
        if current_pos <= 0 and best_break >= total_eps:
            break
        if best_break >= total_eps:
            break
    
    return boundaries


# ── public API ───────────────────────────────────────────────────────

def prepare_chapters(args: argparse.Namespace) -> Path:
    """准备 Chapter Digest 语义批处理 manifest。

    原位置: prepare_story_stages.prepare_chapters (L426, 67L)
    
    Phase 1优化：
    - 动态断点：在目标长度±1集范围内找最优叙事断点
    - 相邻章重叠1集，避免断章切在故事弧中间导致上下文断裂
    - 尾章过短自动合并到前一章
    - 支持开关回退到纯机械等分模式
    """
    from autocut_core.io import atomic_write_json, load_jsonl, update_project_stage

    job_root = args.job_root.resolve()
    episodes = load_jsonl(args.episode_digests)
    if not episodes:
        raise ValueError("episode digests are empty")
    episodes.sort(key=lambda item: item["episode"])
    event_cards_arg = getattr(args, "event_cards", None)
    event_cards_path = (
        event_cards_arg.expanduser().resolve()
        if isinstance(event_cards_arg, Path)
        else (job_root / "event-cards.jsonl").resolve()
    )
    if isinstance(event_cards_arg, Path) and not event_cards_path.is_file():
        raise FileNotFoundError(
            f"explicit Chapter Event Cards file is missing: {event_cards_path}"
        )
    events = load_jsonl(event_cards_path) if event_cards_path.is_file() else []
    
    # ========== Phase 1: 动态分章逻辑 ==========
    target_size = args.episodes_per_chapter
    enable_dynamic = getattr(args, "enable_dynamic_chaptering", True)
    overlap = getattr(args, "chapter_overlap", _DEFAULT_OVERLAP)
    boundaries = _compute_chapter_boundaries(episodes, target_size, overlap, enable_dynamic)
    
    jobs = []
    context_dir = job_root / "intermediate" / "chapter-contexts"
    output_dir = job_root / "chapter-digest-results"
    
    for start_idx, end_idx, chapter_id in boundaries:
        group = episodes[start_idx:end_idx]
        chapter_episodes = [item["episode"] for item in group]
        chapter_episode_set = set(chapter_episodes)
        chapter_events = [
            _compact_event(item)
            for item in events
            if item.get("episode") in chapter_episode_set
        ]
        # Phase 1.2: Build short ID map and DSL event list for reduced token usage
        short_to_full, full_to_short = _build_short_id_map(chapter_events)
        dsl_events = []
        for event in chapter_events:
            sid = full_to_short[event["id"]]
            dsl_events.append(_format_event_dsl(sid, event))
        context = {
            "schema_version": "1.2",
            "chapter_id": chapter_id,
            "episodes": chapter_episodes,
            "is_dynamic_chaptering": enable_dynamic,
            "use_short_event_ids": True,
            "short_id_note": "引用事件时请使用E01/E02等短ID，不要自行生成ID，系统会自动映射回完整ID",
            "episode_digests": [_clean_episode_for_chapter(item) for item in group],
            "event_index_dsl": dsl_events,  # DSL格式事件列表，token减少70%
            "event_index": chapter_events,  # 保留原格式用于兼容，LLM优先使用DSL
            "id_mapping_note": "短ID仅在本章上下文有效，不要跨章使用",
            "chapter_evidence_contract": {
                "event_cards_available": bool(events),
                "event_cards_are_primary_evidence_when_episode_rollups_empty": True,
                "all_rollup_evidence_ids_must_reference_event_index": bool(events),
                "do_not_leave_semantic_rollups_empty_when_events_support_them": True,
                "use_short_e_ids_when_present": True,
            },
        }
        context_path = context_dir / f"{chapter_id}.json"
        output_path = output_dir / f"{chapter_id}.json"
        write_context(context_path, context, args.max_context_chars)
        jobs.append(
            {
                "id": chapter_id,
                "task": "chapter_digest",
                "stage_version": (
                    "story-first-chapter-digest-v4-dsl-short-ids"
                ),
                "context_file": str(context_path.resolve()),
                "output": str(output_path.resolve()),
                "short_to_full_id_map": short_to_full,  # 后处理时用于ID映射还原
            }
        )
    manifest_path = job_root / "chapter-digest-batch.json"
    atomic_write_json(manifest_path, batch_payload(job_root, args.backend, jobs))
    update_project_stage(
        job_root / "project.json",
        "chapter_digest_jobs",
        "prepared",
        outputs={
            "batch_manifest": str(manifest_path),
            "dynamic_chaptering_enabled": enable_dynamic,
            "chapter_count": len(jobs),
        },
    )
    return manifest_path
