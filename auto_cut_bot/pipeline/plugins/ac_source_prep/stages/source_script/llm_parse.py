"""Shared LLM utilities for script parsing — prompts, LLM calls, validation, chunk merging.

Extracted from stage.py to be reusable across pipeline stages.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from autocut_core import PipelineConfig
from state_graph import get_logger

logger = get_logger(__name__)

# ── 配置常量 ──────────────────────────────────────────────────────────────────

DEFAULT_SCRIPT_MODEL = "qwen3.7-max"
DEFAULT_MAX_TOKENS = 131072  # 适配大剧本结构化输出 (ac_auto_cut sync)
CHUNK_SIZE = 60000         # 字符 (~64K tokens for Chinese text)
CHUNK_OVERLAP = 4000       # 字符 (~4K tokens, ~2-3 scenes)
MAX_RETRIES = 3
BASE_DELAY_SECONDS = 2.0
CONFIDENCE_THRESHOLD = 0.7  # 低于此分数的段落触发二次解析
REPARSE_CONTEXT_LINES = 5   # 重解析时额外提供的上下文行数

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
- confidence: float between 0.0 and 1.0 indicating your confidence in the parse quality of this scene.
  Use 0.9-1.0 for clear, unambiguous scenes with standard formatting.
  Use 0.7-0.89 for scenes with some ambiguity (e.g., unusual formatting, missing headers).
  Use 0.5-0.69 for scenes where you are uncertain about boundaries or content.
  Use below 0.5 for scenes you are guessing at.

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
      "meta_tags": {},
      "confidence": 0.95
    }]
  }]
}"""


# ── LLM 调用 ──────────────────────────────────────────────────────────────────


def _call_llm(prompt: str, model: str, cfg: PipelineConfig, *, messages: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """调用 LLM API 并解析 JSON 响应。

    Args:
        prompt: User prompt text (used when messages is None).
        model: Model name.
        cfg: Pipeline configuration.
        messages: Optional pre-built messages list. If provided, used directly
                  instead of building from SYSTEM_PROMPT + prompt.
    """
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
    if messages is not None:
        msg_list = messages
    else:
        msg_list = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
    payload = {
        "model": model,
        "messages": msg_list,
        "temperature": 0.1,  # 避免大JSON输出时卡在token采样 (ac_auto_cut sync)
        "max_tokens": DEFAULT_MAX_TOKENS,
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
    """调用 LLM 解析单个 chunk, 带验证 + 重试 + 置信度评分 + 自适应重解析。"""
    model = cfg.extra.get("script_model", DEFAULT_SCRIPT_MODEL)
    expected_count = cfg.extra.get("expected_episode_count")
    confidence_threshold = cfg.extra.get("confidence_threshold", CONFIDENCE_THRESHOLD)

    last_errors: list[str] = []
    retry_guardrails = ""

    for attempt in range(MAX_RETRIES + 1):
        try:
            format_type = _detect_format(chunk_text)
            prompt = _build_prompt(chunk_text, format_type, retry_guardrails)
            result = _call_llm(prompt, model, cfg)

            # ── 置信度评分: 确保每个场景都有 confidence 字段 ──────
            result = _ensure_confidence_scores(result)

            errors = _validate_episode_boundaries(result, expected_count)
            if not errors:
                # ── 自适应重解析: 低置信度段落二次解析 ──────
                low_conf = _find_low_confidence_scenes(result, confidence_threshold)
                if low_conf:
                    logger.info(
                        "发现 %d 个低置信度场景 (threshold=%.1f), 触发重解析",
                        len(low_conf), confidence_threshold,
                    )
                    result = _reparse_low_confidence_scenes(
                        result, low_conf, chunk_text, cfg,
                    )

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


# ── 置信度评分与自适应重解析 ──────────────────────────────────────────────────


def _ensure_confidence_scores(result: dict[str, Any]) -> dict[str, Any]:
    """确保每个场景都有 confidence 字段。

    如果模型未返回 confidence，默认为 1.0（向后兼容）。
    """
    for ep in result.get("episodes", []):
        for scene in ep.get("scenes", []):
            if "confidence" not in scene:
                scene["confidence"] = 1.0
    return result


def _find_low_confidence_scenes(
    result: dict[str, Any],
    threshold: float = CONFIDENCE_THRESHOLD,
) -> list[dict[str, Any]]:
    """找出所有置信度低于阈值的场景。

    返回列表，每项包含: episode_number, scene_index, scene, confidence
    """
    low_conf: list[dict[str, Any]] = []
    for ep in result.get("episodes", []):
        ep_num = ep.get("episode_number", 0)
        for idx, scene in enumerate(ep.get("scenes", [])):
            conf = scene.get("confidence", 1.0)
            if conf < threshold:
                low_conf.append({
                    "episode_number": ep_num,
                    "scene_index": idx,
                    "scene": scene,
                    "confidence": conf,
                })
    return low_conf


def _reparse_low_confidence_scenes(
    result: dict[str, Any],
    low_conf: list[dict[str, Any]],
    original_text: str,
    cfg: PipelineConfig,
) -> dict[str, Any]:
    """对低置信度段落进行二次解析，附加周围上下文。

    策略:
    - 对每个低置信度场景，提取原文中对应区域 + 前后 N 行上下文
    - 提交给 LLM 重新解析
    - 如果二次解析仍低于阈值，标记 review_required: true
    - 替换原结果中对应场景
    """
    model = cfg.extra.get("script_model", DEFAULT_SCRIPT_MODEL)
    confidence_threshold = cfg.extra.get("confidence_threshold", CONFIDENCE_THRESHOLD)
    lines = original_text.split("\n")

    reparse_prompt = (
        "You are re-parsing a scene that was previously parsed with low confidence. "
        "Focus carefully on this specific scene. "
        "Additional context from surrounding text is provided to help you. "
        "Output ONLY the corrected scene JSON object (not a full episode list):\n"
        "{\n"
        '  "scene_id": "...",\n'
        '  "scene_order": <int>,\n'
        '  "heading": "...",\n'
        '  "location": "...",\n'
        '  "time_of_day": "...",\n'
        '  "is_flashback": <bool>,\n'
        '  "characters_present": ["..."],\n'
        '  "dialogues": [{"character": "...", "text": "...", "sequence": <int>}],\n'
        '  "raw_description": "...",\n'
        '  "meta_tags": {},\n'
        '  "confidence": <0.0-1.0>\n'
        "}"
    )

    for item in low_conf:
        scene = item["scene"]
        ep_num = item["episode_number"]
        scene_id = scene.get("scene_id", "unknown")
        original_confidence = item["confidence"]

        # 尝试从原文定位该场景的文本区域
        context_text = _extract_scene_context(
            lines, scene, context_lines=REPARSE_CONTEXT_LINES,
        )

        messages = [
            {"role": "system", "content": reparse_prompt},
            {"role": "user", "content": (
                f"Scene '{scene_id}' from episode {ep_num} was parsed with "
                f"confidence {original_confidence:.2f}.\n\n"
                "The original parse was:\n"
                f"{json.dumps(scene, ensure_ascii=False, indent=2)}\n\n"
                "Context from the original script:\n"
                f"{context_text}\n\n"
                "Please re-parse this scene with higher accuracy. "
                "Focus on scene boundaries, character names, and dialogue attribution."
            )},
        ]
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 8192,
            "response_format": {"type": "json_object"},
        }

        try:
            parsed = _call_llm(
                prompt="", model=model, cfg=cfg,
                messages=messages,
            )

            new_confidence = parsed.get("confidence", 0.0)

            if new_confidence >= confidence_threshold:
                # 二次解析成功 — 替换原场景
                parsed["_reparsed"] = True
                parsed["_original_confidence"] = original_confidence
                scene.clear()
                scene.update(parsed)
                logger.info(
                    "场景 %s 重解析成功: confidence %.2f -> %.2f",
                    scene_id, original_confidence, new_confidence,
                )
            else:
                # 二次解析仍然低置信度 — 标记为需人工审核
                scene["review_required"] = True
                scene["review_reason"] = (
                    f"Two parse attempts both scored below threshold "
                    f"({original_confidence:.2f}, {new_confidence:.2f}). "
                    f"Manual review recommended."
                )
                scene["_reparsed"] = True
                scene["_reparse_confidence"] = new_confidence
                logger.warning(
                    "场景 %s 二次解析仍低于阈值: %.2f -> %.2f, 标记 review_required",
                    scene_id, original_confidence, new_confidence,
                )

        except Exception as exc:
            # 重解析失败 — 标记为需人工审核
            scene["review_required"] = True
            scene["review_reason"] = (
                f"Re-parse failed with error: {exc}. "
                f"Original confidence: {original_confidence:.2f}. "
                f"Manual review recommended."
            )
            scene["_reparse_error"] = str(exc)
            logger.error("场景 %s 重解析异常: %s", scene_id, exc)

    return result


def _extract_scene_context(
    lines: list[str],
    scene: dict[str, Any],
    context_lines: int = REPARSE_CONTEXT_LINES,
) -> str:
    """尝试从原始文本中提取场景的上下文区域。

    使用场景 heading 或 location 进行模糊匹配，
    找到后在前后各取 context_lines 行。
    如果匹配失败，返回空字符串。
    """
    heading = scene.get("heading", "")
    location = scene.get("location", "")
    search_terms = [heading, location] if heading else [location]

    for term in search_terms:
        if not term or len(term) < 2:
            continue
        for i, line in enumerate(lines):
            if term[:8] in line or line[:8] in term:
                start = max(0, i - context_lines)
                end = min(len(lines), i + context_lines + 1)
                return "\n".join(lines[start:end])

    return ""