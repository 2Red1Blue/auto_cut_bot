"""autocut_core.semantic.prep.global_segmenter - Global intelligent chapter segmentation.

全剧一次性LLM调用，基于每集一句话摘要划分自然叙事章节，避免机械等分切裂故事弧。
"""
from __future__ import annotations
import argparse
import json
import logging
from pathlib import Path
from typing import Any
from autocut_core.io import atomic_write_json, load_jsonl, load_json, write_json
from autocut_core.semantic.prep.utils import batch_payload, write_context

logger = logging.getLogger(__name__)

SEGMENTER_SYSTEM_PROMPT = """你是专业短剧叙事分析师，擅长识别短剧的叙事节奏和故事弧断点。
短剧的编剧规律通常是3-8集一个完整小爽点/小高潮，节奏模式为：潜伏受辱 → 矛盾激化 → 身份暴露/反转 → 当众打脸/爽点兑现。

你的任务是基于提供的每集剧情摘要，将全剧划分为自然章节，遵循以下规则：
1. 每章3-8集，优先选择4-6集的完整小弧
2. 断点必须选在自然叙事节点：
   - 悬念揭晓/阶段性高潮结束后
   - 大的时间跳跃/场景切换
   - 某条故事线阶段性收尾，新故事线开启
3. 必须覆盖所有剧集，不能有遗漏或重叠，章节必须连续
4. 每章给出简短标题、核心冲突、断点理由，帮助后续摘要理解叙事重心

请严格按照要求的JSON格式输出，不要输出任何额外说明文字。
"""

SEGMENTER_USER_PROMPT_TEMPLATE = """## 剧集信息
剧名：{drama_title}
总集数：{total_eps}

## 每集摘要
{episode_summaries}

## 输出要求
请输出严格的JSON格式，结构如下：
```json
{{
  "series_core_threads": [
    {{"thread_id": "T01", "name": "主线名称", "description": "主线简要描述"}}
  ],
  "chapters": [
    {{
      "chapter_index": 1,
      "start_ep": 1,
      "end_ep": 5,
      "title": "章节简短标题",
      "arc_type": "setup/escalation/reveal/payoff/coda",
      "core_conflict": "本章核心冲突一句话",
      "climax_episode": 5,
      "boundary_reason": "为什么在EP5结尾断章"
    }}
  ]
}}
```
- 全局主线2-3条即可，不要过多
- arc_type可选值：setup(铺垫)、escalation(升级)、reveal(反转揭晓)、payoff(高潮兑现)、coda(收尾)
- 输出必须是纯JSON，不要包裹markdown标记，不要有额外说明
"""


def _format_episode_summaries(episodes: list[dict[str, Any]]) -> str:
    """Format episodes into a compact summary list for LLM input."""
    lines = []
    for ep in sorted(episodes, key=lambda x: x["episode"]):
        ep_num = ep["episode"]
        # Take the first 120 chars of summary as the 1-line description
        summary = (ep.get("summary", "") or "").strip().replace("\n", " ")
        if len(summary) > 120:
            summary = summary[:117] + "..."
        opening = (ep.get("opening_state", "") or "").strip()
        ending = (ep.get("ending_state", "") or "").strip()
        line = f"EP{ep_num:02d}: {summary}"
        if ending:
            end_short = ending[:60] + "..." if len(ending) > 60 else ending
            line += f" [结尾：{end_short}]"
        lines.append(line)
    return "\n".join(lines)


def _validate_chapter_boundaries(chapters: list[dict[str, Any]], total_eps: int, min_eps: int = 3, max_eps: int = 8) -> bool:
    """Validate that chapter boundaries are valid:
    - Each chapter has between min_eps and max_eps episodes
    - Chapters cover all episodes 1..total_eps without gaps or overlaps
    - Episode ranges are in increasing order
    """
    if not chapters:
        return False
    expected_start = 1
    required_fields = {"start_ep", "end_ep", "title", "core_conflict"}
    for ch in chapters:
        if not isinstance(ch, dict) or not required_fields.issubset(ch.keys()):
            return False
        start = ch.get("start_ep")
        end = ch.get("end_ep")
        if not isinstance(start, int) or not isinstance(end, int):
            return False
        if start != expected_start:
            logger.debug(f"Chapter start mismatch: expected {expected_start}, got {start}")
            return False
        if end < start:
            return False
        length = end - start + 1
        if length < min_eps or length > max_eps:
            logger.debug(f"Chapter length {length} out of range [{min_eps}, {max_eps}]")
            return False
        expected_start = end + 1
    return expected_start == total_eps + 1


def _heuristic_fallback_boundaries(
    episodes: list[dict[str, Any]],
    target_size: int,
    overlap: int,
    enable_dynamic: bool,
) -> list[tuple[int, int, str, dict[str, Any]]]:
    """Fallback to existing heuristic dynamic chaptering when LLM fails."""
    from autocut_core.semantic.prep.chapters import _compute_chapter_boundaries
    boundaries = _compute_chapter_boundaries(episodes, target_size, overlap, enable_dynamic)
    result = []
    for start_idx, end_idx, chapter_id in boundaries:
        ep_start = episodes[start_idx]["episode"]
        ep_end = episodes[end_idx - 1]["episode"]
        result.append((start_idx, end_idx, chapter_id, {
            "title": f"第{ep_start}-{ep_end}集",
            "arc_type": "unknown",
            "core_conflict": "",
            "climax_episode": ep_end,
            "boundary_reason": "启发式自动分章",
        }))
    return result


def segment_chapters(
    episode_digests: list[dict[str, Any]],
    args: argparse.Namespace,
    job_root: Path,
    llm_port: Any | None = None,
    drama_title: str = "短剧",
) -> tuple[list[tuple[int, int, str, dict[str, Any]]], list[dict[str, Any]]]:
    """Run global chapter segmentation.
    Returns list of (start_idx, end_idx, chapter_id, chapter_metadata) and core thread seeds.
    Falls back to heuristic dynamic chaptering if LLM fails or returns invalid boundaries.
    """
    episodes = sorted(episode_digests, key=lambda x: x["episode"])
    total_eps = len(episodes)
    target_size = getattr(args, "episodes_per_chapter", 6)
    overlap = getattr(args, "chapter_overlap", 1)
    enable_dynamic = getattr(args, "enable_dynamic_chaptering", True)
    enable_llm_segmenter = getattr(args, "enable_llm_chaptering", True)

    def fallback() -> tuple[list[tuple[int, int, str, dict[str, Any]]], list[dict[str, Any]]]:
        return _heuristic_fallback_boundaries(episodes, target_size, overlap, enable_dynamic), []

    # If LLM segmenter is disabled, return fallback immediately
    if not enable_llm_segmenter or llm_port is None:
        logger.info("LLM chapter segmentation disabled, using heuristic fallback")
        return fallback()

    try:
        # Build LLM prompt
        ep_summaries = _format_episode_summaries(episodes)
        user_prompt = SEGMENTER_USER_PROMPT_TEMPLATE.format(
            drama_title=drama_title,
            total_eps=total_eps,
            episode_summaries=ep_summaries,
        )
        messages = [
            {"role": "system", "content": SEGMENTER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        logger.info(f"Calling LLM for global chapter segmentation ({total_eps} episodes, ~{len(user_prompt)} chars)")
        response = llm_port.call_llm(
            prompt=user_prompt,
            model=getattr(args, "backend_model", None) or "qwen-max",
            messages=messages,
            temperature=0.1,
            max_tokens=4096,
            response_format={"type": "json_object"},
            timeout=60.0,
        )

        # Parse response
        content = response.get("content", "") if isinstance(response, dict) else str(response)
        # Strip possible markdown code blocks
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        result = json.loads(content)
        chapters = result.get("chapters", [])
        core_threads = result.get("series_core_threads", [])

        if not _validate_chapter_boundaries(chapters, total_eps):
            logger.warning("LLM returned invalid chapter boundaries, falling back to heuristic")
            return fallback()

        # Convert to (start_idx, end_idx, chapter_id, metadata) format
        boundaries = []
        for ch in chapters:
            ep_start = ch["start_ep"]
            ep_end = ch["end_ep"]
            # Find indices in episodes list
            start_idx = next(i for i, ep in enumerate(episodes) if ep["episode"] == ep_start)
            end_idx = next(i for i, ep in enumerate(episodes) if ep["episode"] == ep_end) + 1
            chapter_id = f"chapter-{ep_start:03d}-{ep_end:03d}"
            boundaries.append((start_idx, end_idx, chapter_id, ch))

        # Save segmentation result for debugging
        seg_output_path = job_root / "chapter-boundaries.json"
        write_json(seg_output_path, {
            "schema_version": "1.0",
            "method": "llm_segmenter",
            "total_episodes": total_eps,
            "core_threads": core_threads,
            "chapters": [
                {"chapter_id": cid, **meta} for _, _, cid, meta in boundaries
            ],
        })
        logger.info(f"LLM segmentation succeeded: {len(boundaries)} chapters created")
        return boundaries, core_threads

    except Exception as e:
        logger.warning(f"LLM chapter segmentation failed: {e}, falling back to heuristic")
        return fallback()
