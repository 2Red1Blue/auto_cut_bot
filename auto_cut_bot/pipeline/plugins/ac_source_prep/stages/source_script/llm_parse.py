"""Shared LLM utilities for script parsing — prompts, LLM calls, validation, chunk merging.

Extracted from stage.py to be reusable across pipeline stages.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from autocut_core import PipelineConfig, get_logger

logger = get_logger(__name__)

# ── 配置常量 ──────────────────────────────────────────────────────────────────

DEFAULT_SCRIPT_MODEL = "qwen3.7-max"
CHUNK_SIZE = 60000         # 字符 (~64K tokens for Chinese text)
CHUNK_OVERLAP = 4000       # 字符 (~4K tokens, ~2-3 scenes)
MAX_RETRIES = 3
BASE_DELAY_SECONDS = 2.0

# ── LLM System Prompt ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a script parser. Given a Chinese drama script, you must:
1. Detect episode boundaries (marked by 第X集, Episode X, EP X, or similar patterns)
2. Parse each episode into scenes
3. For each scene, extract: scene_id, heading, location, time_of_day, is_flashback,
   characters_present, dialogues, raw_description

Scene header formats you may encounter:
- "1-2 墓地 雨夜 外" (Chinese numbered format: scene_order location time_of_day interior/exterior)
- "Scene 2: The Graveyard" (English format)
- "SCENE 1-2 - INT. WORKSHOP - DAY" (Screenplay format)
- "第二场 工坊店 日内" (Chinese character format)
- No explicit header (infer from context)

Dialogue formats:
- "Lucifer：Humans call me Satan..." (Chinese colon)
- "Lucifer: Humans call me Satan..." (English colon)
- "(Lucifer) Humans call me Satan..." (Parenthetical)

For each scene, output a JSON object with:
- scene_id: string, unique identifier like "S{episode}E{scene_order}"
- scene_order: integer within the episode
- heading: original scene header text
- location: string, the location of the scene
- time_of_day: string (e.g. "日", "夜", "晨", "暮", "日内", "夜外")
- is_flashback: boolean
- characters_present: array of strings (character names)
- dialogues: array of {character: string, text: string, sequence: integer}
- raw_description: narrative/action text between dialogues
- meta_tags: object with extra metadata (genre hints, mood, etc.)

Output MUST be valid JSON matching this schema:
{
  "episodes": [{
    "episode_number": 1,
    "title": "optional title",
    "scenes": [{
      "scene_id": "S1E1",
      "scene_order": 1,
      "heading": "1-1 墓地 雨夜 外",
      "location": "墓地",
      "time_of_day": "雨夜",
      "is_flashback": false,
      "characters_present": ["Lucifer"],
      "dialogues": [{"character": "Lucifer", "text": "...", "sequence": 1}],
      "raw_description": "Lucifer walks through the graveyard...",
      "meta_tags": {}
    }]
  }]
}"""


# ── LLM 调用 ──────────────────────────────────────────────────────────────────


def _call_llm(prompt: str, model: str, cfg: PipelineConfig) -> dict[str, Any]:
    """调用 LLM API 并解析 JSON 响应。"""
    from autocut_core.backends._base import get_backend
    from autocut_core.semantic.engine.provider import call_provider
    from autocut_core.semantic.engine.provider import parse_model_json
    from autocut_core.semantic.engine.concurrency import RateLimiter

    backend = get_backend(cfg.backend, config=cfg)
    limiter = RateLimiter(0)  # no limit for single script parsing

    class _NoopConcurrency:
        def acquire(self) -> None: pass
        def release(self, **_: Any) -> None: pass

    concurrency = _NoopConcurrency()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 65536,
        "response_format": {"type": "json_object"},
    }
    response = call_provider(
        backend,
        payload,
        timeout=120.0,
        retries=2,
        limiter=limiter,
        concurrency=concurrency,
    )
    return parse_model_json(response)


# ── 格式检测 ──────────────────────────────────────────────────────────────────


def _detect_format(text: str, sample_size: int = 5000) -> str:
    """检测剧本的主导格式 (在文本前 N 个字符中采样)。"""
    sample = text[:sample_size]

    if re.search(r"\d+-\d+\s+\S+", sample):
        return "chinese_numbered"
    if re.search(r"Scene\s+\d+:", sample, re.IGNORECASE):
        return "english_scene"
    if re.search(
        r"(INT|EXT)\.\s+\S+\s*-\s*(DAY|NIGHT|MORNING|EVENING|DUSK|DAWN)",
        sample, re.IGNORECASE,
    ):
        return "screenplay"

    return "unknown"


def _build_format_hint(format_type: str) -> str:
    """根据检测到的格式生成提示文本。"""
    hints: dict[str, str] = {
        "chinese_numbered": (
            "This script uses Chinese numbered scene format (e.g., '1-2 墓地 雨夜 外'). "
            "Parse scene_id as 'S{episode}E{scene_order}'."
        ),
        "english_scene": (
            "This script uses English scene format (e.g., 'Scene 2: The Graveyard'). "
            "Parse scene_id from the scene number."
        ),
        "screenplay": (
            "This script uses screenplay format (e.g., 'SCENE 1-2 - INT. WORKSHOP - DAY'). "
            "Extract INT/EXT and time of day."
        ),
        "unknown": "",
    }
    return hints.get(format_type, "")


def _build_prompt(
    script_text: str,
    format_type: str,
    extra_guardrails: str = "",
) -> str:
    """构造完整的 LLM prompt。"""
    parts = [SYSTEM_PROMPT]

    format_hint = _build_format_hint(format_type)
    if format_hint:
        parts.append(f"\nFormat hint: {format_hint}")

    if extra_guardrails:
        parts.append(f"\nIMPORTANT: {extra_guardrails}")

    parts.append(f"\n\nScript text:\n{script_text}")
    return "\n".join(parts)


# ── 单 chunk 解析 ─────────────────────────────────────────────────────────────


def _parse_single_chunk(chunk_text: str, cfg: PipelineConfig) -> dict[str, Any]:
    """调用 LLM 解析单个 chunk, 带验证 + 重试。"""
    model = cfg.extra.get("script_model", DEFAULT_SCRIPT_MODEL)
    expected_count = cfg.extra.get("expected_episode_count")

    last_errors: list[str] = []
    retry_guardrails = ""

    for attempt in range(MAX_RETRIES + 1):
        try:
            format_type = _detect_format(chunk_text)
            prompt = _build_prompt(chunk_text, format_type, retry_guardrails)
            result = _call_llm(prompt, model, cfg)

            errors = _validate_episode_boundaries(result, expected_count)
            if not errors:
                result["_parse_meta"] = {"attempts": attempt + 1, "status": "success"}
                return result

            last_errors = errors
            logger.warning("LLM 解析第 %d 次验证失败: %s", attempt + 1, errors)

            if attempt < MAX_RETRIES:
                retry_guardrails = _build_guardrails(expected_count or 0, errors)
                time.sleep(BASE_DELAY_SECONDS * (2 ** attempt))

        except Exception as exc:
            logger.error("LLM 解析第 %d 次异常: %s", attempt + 1, exc)
            if attempt < MAX_RETRIES:
                time.sleep(BASE_DELAY_SECONDS * (2 ** attempt))
            else:
                raise

    logger.error("全部 %d 次重试失败: %s", MAX_RETRIES, last_errors)
    return {
        "episodes": [],
        "_parse_meta": {
            "attempts": MAX_RETRIES + 1,
            "status": "parse_error",
            "errors": last_errors,
        },
    }


# ── 合并去重 ──────────────────────────────────────────────────────────────────


def _make_fingerprint(episode_number: int, scene: dict[str, Any]) -> str:
    """为场景生成内容指纹用于去重。"""
    raw = (
        f"{episode_number}:"
        f"{scene.get('scene_id', '')}:"
        f"{scene.get('location', '')}:"
    )
    dialogues = scene.get("dialogues", [])
    if dialogues and isinstance(dialogues, list):
        first_text = dialogues[0].get("text", "")[:50] if isinstance(dialogues[0], dict) else ""
        raw += first_text
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _merge_chunk_results(chunk_results: list[dict[str, Any]]) -> dict[str, Any]:
    """按内容指纹去重合并多个 chunk 的解析结果。"""
    seen: set[str] = set()
    episode_map: dict[int, list[dict[str, Any]]] = {}

    for result in chunk_results:
        for ep in result.get("episodes", []):
            ep_num = ep["episode_number"]
            if ep_num not in episode_map:
                episode_map[ep_num] = []
            for scene in ep.get("scenes", []):
                fingerprint = _make_fingerprint(ep_num, scene)
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    episode_map[ep_num].append(scene)

    merged_episodes = []
    for ep_num in sorted(episode_map.keys()):
        scenes = sorted(episode_map[ep_num], key=lambda s: s.get("scene_id", ""))
        merged_episodes.append({"episode_number": ep_num, "scenes": scenes})

    return {"episodes": merged_episodes}


# ── 验证 ──────────────────────────────────────────────────────────────────────


def _validate_episode_boundaries(
    result: dict[str, Any],
    expected_count: int | None = None,
) -> list[str]:
    """验证 LLM 输出的集边界。"""
    errors: list[str] = []
    episodes = result.get("episodes", [])

    if not episodes:
        return ["No episodes found in LLM output"]

    # 1. 集数验证
    if expected_count and len(episodes) != expected_count:
        errors.append(f"Expected {expected_count} episodes, got {len(episodes)}")

    # 2. 集号连续性
    ep_numbers = sorted([ep["episode_number"] for ep in episodes])
    if ep_numbers[0] != 1:
        errors.append(f"First episode should be 1, got {ep_numbers[0]}")
    expected = list(range(ep_numbers[0], ep_numbers[-1] + 1))
    missing = set(expected) - set(ep_numbers)
    if missing:
        errors.append(f"Missing episodes: {sorted(missing)}")

    # 3. 场景数分布验证
    scene_counts = [len(ep.get("scenes", [])) for ep in episodes]
    if len(scene_counts) > 1 and sum(scene_counts) > 0:
        mean = sum(scene_counts) / len(scene_counts)
        outliers = [
            i for i, c in enumerate(scene_counts)
            if c < 0.3 * mean or c > 3.0 * mean
        ]
        if outliers:
            errors.append(
                f"Episode(s) {outliers} have outlier scene counts: "
                f"{[scene_counts[i] for i in outliers]} (mean={mean:.1f})"
            )

    return errors


def _build_guardrails(expected_count: int, errors: list[str]) -> str:
    """根据验证错误构造重试 guardrails。"""
    parts = [
        f"The script contains exactly {expected_count} episodes. "
        f"Episode 1 starts with 第一集. "
        f"Please re-parse and ensure all episodes are detected.",
    ]
    for err in errors:
        parts.append(f"Previous error: {err}")
    return "\n".join(parts)