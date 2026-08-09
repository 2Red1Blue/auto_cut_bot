"""source_script Stage — 加载完整剧本, LLM 解析为结构化场景, 字幕时间对齐, 写入 scenes 表。

流水线位置: Phase 1 第 4 步 (source_windows → window_analysis → source_metadata → source_script)。
职责: 读取原始剧本文件 → deepseek-v4-flash-260425 做集切分+场景解析 → 用 API 字幕时间戳
      给场景打时间锚点 → UPSERT scenes 表 → 发布 source_script 产物。

输入: source_metadata (来自上游 source_metadata Stage)
输出: source_script (结构化剧本 JSON, 含时间锚点 + 对齐报告)
"""

from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from autocut_core import (
    ArtifactBus,
    Artifact,
    PipelineConfig,
    Stage,
    StageContract,
    Task,
    atomic_write_json,
    get_logger,
    update_project_stage,
)
from autocut_core.db.client import StageDBClient

logger = get_logger(__name__)

# ── 配置常量 ──────────────────────────────────────────────────────────────────

DEFAULT_SCRIPT_MODEL = "qwen3.7-max"
CHUNK_SIZE = 60000         # 字符 (~64K tokens for Chinese text)
CHUNK_OVERLAP = 4000       # 字符 (~4K tokens, ~2-3 scenes)
MAX_RETRIES = 3
BASE_DELAY_SECONDS = 2.0
EXACT_THRESHOLD = 0.95
FUZZY_THRESHOLD = 0.60
INFERRED_THRESHOLD = 0.40
MIN_ALIGNMENT_RATE = 0.50

# 集边界标记模式
EPISODE_BOUNDARY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"第\s*(\d+)\s*集"),
    re.compile(r"Episode\s+(\d+)", re.IGNORECASE),
    re.compile(r"EP\s*(\d+)", re.IGNORECASE),
    re.compile(r"第\s*(\d+)\s*章"),
]

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

# ── Stage 入口 ────────────────────────────────────────────────────────────────


class SourceScriptStage(Stage):
    """加载完整剧本 → LLM 解析 → 字幕时间对齐 → 写入 scenes 表。

    输入: source_metadata (含 book_id, 字幕时间戳)
    输出: source_script (结构化剧本 + 时间锚点 + 对齐报告)

    剧本文件不存在时 status='unavailable', 不阻塞流水线。
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="source_script",
            input_artifacts=["source_metadata"],
            output_artifacts=["source_script"],
            description="LLM 解析剧本 → 结构化场景 → 字幕时间对齐 → 写入 scenes 表",
            db_reads=["books", "subtitles"],
            db_writes=["scenes"],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """从 bus 读取 source_metadata, 规划解析任务。"""
        metadata_path = self.resolve_artifact_path(bus, "source_metadata", "source_metadata")
        return [Task(type="script_parse", payload={"metadata_path": metadata_path})]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """执行流程: 加载剧本 → LLM 解析 → 字幕对齐 → 写 DB → 发布产物。"""
        cfg = self.config
        root: Path = cfg.job_root  # type: ignore[assignment]

        # 1. 加载 source_metadata
        metadata_path = Path(tasks[0].payload["metadata_path"])
        metadata = _load_json(metadata_path)
        book_id = _extract_book_id(metadata, cfg)

        # 2. 定位剧本文件
        script_path = _find_script_file(root, cfg)
        if script_path is None:
            logger.warning("剧本文件未找到, source_script 标记为 unavailable")
            return _publish_unavailable(bus, root, book_id)

        script_text = script_path.read_text(encoding="utf-8-sig") if script_path.suffix != ".docx" else _read_docx(script_path)
        script_sha = hashlib.sha256(script_text.encode("utf-8")).hexdigest()

        # 3. 检查缓存
        cached = _load_cache(root, script_sha)
        if cached is not None:
            logger.info("剧本缓存命中 sha=%s", script_sha[:16])
            parsed = cached
        else:
            # 4. LLM 解析: 分片 → 逐片解析 → 合并去重
            parsed = _parse_script(script_text, cfg)
            _save_cache(root, script_sha, parsed)

        # 5. 字幕时间对齐
        db = StageDBClient(db_url=cfg.db_url, schema=cfg.db_schema)
        parsed = _align_scenes_with_subtitles(parsed, book_id, db)

        # 6. 写入 scenes 表
        _write_scenes_to_db(parsed, book_id, db)

        # 6.5. 写入派生表 (角色/对话/场景/分镜)
        _write_derived_tables(parsed, book_id, db)

        # 7. 发布产物
        return _publish_result(bus, root, parsed, script_sha, book_id)


# ── 剧本文件定位 ──────────────────────────────────────────────────────────────


def _find_script_file(job_root: Path, cfg: PipelineConfig) -> Path | None:
    """在 job_root 下查找剧本文件。

    优先级:
      1. cfg.extra["script_path"] 显式指定
      2. job_root 下的 *.txt, *.docx 文件
      3. job_root/script/ 目录下的文件
    """
    explicit = cfg.extra.get("script_path")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_file():
            return path

    # 搜索 job_root 下的 .txt / .docx 文件
    candidates = sorted(job_root.glob("*.txt")) + sorted(job_root.glob("*.docx"))
    if candidates:
        return candidates[0]

    # 搜索 job_root/script/ 目录
    script_dir = job_root / "script"
    if script_dir.is_dir():
        candidates = sorted(script_dir.glob("*"))
        if candidates:
            return candidates[0]

    return None


def _read_docx(path: Path) -> str:
    """读取 .docx 文件，提取纯文本段落。

    依赖 python-docx；未安装时抛 ImportError 并给出安装提示。
    """
    try:
        from docx import Document  # type: ignore[import-not-found]
    except ImportError:
        raise ImportError(
            "读取 .docx 剧本需要 python-docx。请执行: pip install python-docx"
        )
    doc = Document(str(path))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs)


def _extract_book_id(metadata: dict[str, Any], cfg: PipelineConfig) -> str:
    """从 source_metadata 中提取 book_id。"""
    book_id = metadata.get("book_id") or cfg.extra.get("book_id")
    if not book_id:
        raise ValueError("source_metadata 中未找到 book_id, 且 cfg.extra 中也未配置")
    return str(book_id)


# ── LLM 解析 ──────────────────────────────────────────────────────────────────


def _parse_script(script_text: str, cfg: PipelineConfig) -> dict[str, Any]:
    """完整剧本解析: 分片 → 逐片 LLM 解析 → 合并去重。

    当剧本长度 <= CHUNK_SIZE 时直接单次解析;
    超过时分片处理, 优先在集边界切分。
    """
    if len(script_text) <= CHUNK_SIZE:
        return _parse_single_chunk(script_text, cfg)

    chunks = _chunk_at_boundaries(script_text)
    logger.info("剧本分片: %d chunks (总长 %d 字符)", len(chunks), len(script_text))

    chunk_results: list[dict[str, Any]] = []
    for i, chunk in enumerate(chunks):
        result = _parse_single_chunk(chunk, cfg)
        chunk_results.append(result)

    merged = _merge_chunk_results(chunk_results)
    merged["parse_metadata"] = {
        "chunk_count": len(chunks),
        "total_chars": len(script_text),
        "status": "success",
    }
    return merged


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
                import time
                time.sleep(BASE_DELAY_SECONDS * (2 ** attempt))

        except Exception as exc:
            logger.error("LLM 解析第 %d 次异常: %s", attempt + 1, exc)
            if attempt < MAX_RETRIES:
                import time
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


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 响应中提取 JSON 对象。

    策略:
      1. 尝试直接解析全文
      2. 尝试提取 ```json ... ``` 代码块
      3. 尝试提取第一个 { 到最后一个 } 之间的内容
    """
    text = text.strip()

    # 策略 1: 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 策略 2: 提取 ```json ... ``` 代码块
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 策略 3: 提取第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"无法从 LLM 响应中提取 JSON: {text[:200]}...")


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


# ── 分片逻辑 ──────────────────────────────────────────────────────────────────


def _chunk_with_overlap(text: str) -> list[str]:
    """将长文本按滑动窗口分片, 相邻 chunk 之间有重叠。"""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def _find_episode_boundaries(text: str) -> list[int]:
    """在文本中查找所有集边界的字符位置。"""
    positions: list[int] = []
    for pattern in EPISODE_BOUNDARY_PATTERNS:
        for match in pattern.finditer(text):
            positions.append(match.start())
    return sorted(set(positions))


def _chunk_at_boundaries(text: str) -> list[str]:
    """在集边界处优先切分; 无法在集边界切分时回退普通滑动窗口。"""
    boundaries = _find_episode_boundaries(text)
    if not boundaries:
        return _chunk_with_overlap(text)

    chunks: list[str] = []
    start = 0

    for boundary in boundaries:
        if boundary <= start:
            continue
        if boundary - start > CHUNK_SIZE * 1.5:
            sub = text[start:boundary]
            chunks.extend(_chunk_with_overlap(sub))
        else:
            chunks.append(text[start:boundary])
        start = boundary

    if start < len(text):
        chunks.append(text[start:])

    return chunks


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


# ── 字幕时间对齐 ──────────────────────────────────────────────────────────────


def _normalize_text(text: str) -> str:
    """文本归一化: 去空格、小写、去标点。"""
    text = text.lower().strip()
    text = re.sub(r"[^\w一-鿿]", "", text)
    text = re.sub(r"\s+", "", text)
    return text


def _similarity(a: str, b: str) -> float:
    """归一化后的文本相似度 (0.0-1.0)。"""
    return SequenceMatcher(None, _normalize_text(a), _normalize_text(b)).ratio()


def _score_to_level(score: float) -> str:
    """将相似度分数映射为对齐级别。"""
    if score >= EXACT_THRESHOLD:
        return "exact"
    if score >= FUZZY_THRESHOLD:
        return "fuzzy"
    if score >= INFERRED_THRESHOLD:
        return "inferred"
    return "none"


def _find_best_alignment(
    subtitle_text: str,
    script_dialogues: list[dict[str, Any]],
) -> tuple[int | None, float, str]:
    """在剧本对话中查找最佳匹配的字幕。"""
    best_index: int | None = None
    best_score: float = 0.0

    for i, dialogue in enumerate(script_dialogues):
        text = dialogue.get("text", "") if isinstance(dialogue, dict) else ""
        score = _similarity(subtitle_text, text)
        if score > best_score:
            best_score = score
            best_index = i

    if best_score < INFERRED_THRESHOLD:
        return None, best_score, "none"

    return best_index, best_score, _score_to_level(best_score)


def _align_scenes_with_subtitles(
    parsed: dict[str, Any],
    book_id: str,
    db: StageDBClient,
) -> dict[str, Any]:
    """将字幕时间戳对齐到剧本场景, 填充 start_time/end_time。

    对每一集: 从 DB 读取该集字幕 → 遍历场景 → 对每条字幕查找最佳匹配对话 →
    将匹配到的字幕时间范围作为场景的时间锚点。
    """
    episodes = parsed.get("episodes", [])
    if not episodes:
        return parsed

    total_exact = total_fuzzy = total_inferred = total_none = 0

    for ep in episodes:
        episode_id = ep["episode_number"]
        subtitles = db.query_subtitles(book_id, episode_id)

        if not subtitles:
            # 无字幕数据, 回退到时间均匀分布
            _fallback_uniform_time(ep["scenes"])
            continue

        for scene in ep.get("scenes", []):
            dialogues = scene.get("dialogues", [])
            if not dialogues:
                # 无对话场景: 标记为 none
                scene["alignment_confidence"] = "none"
                scene["alignment_source"] = "none"
                total_none += 1
                continue

            # 对每条字幕查找最佳匹配
            best_levels: list[str] = []
            matched_times: list[tuple[float, float]] = []

            for sub in subtitles:
                sub_text = sub.get("text", "")
                _, score, level = _find_best_alignment(sub_text, dialogues)
                best_levels.append(level)
                if level != "none":
                    matched_times.append((sub["start_time"], sub["end_time"]))

            # 统计对齐级别
            for level in best_levels:
                if level == "exact":
                    total_exact += 1
                elif level == "fuzzy":
                    total_fuzzy += 1
                elif level == "inferred":
                    total_inferred += 1
                else:
                    total_none += 1

            # 确定场景的时间锚点
            if matched_times:
                scene["start_time"] = matched_times[0][0]
                scene["end_time"] = matched_times[-1][1]
                scene["alignment_confidence"] = best_levels[0] if best_levels else "none"
                scene["alignment_source"] = "subtitle_matched"
            else:
                scene["alignment_confidence"] = "none"
                scene["alignment_source"] = "none"

        # 检查是否需要全局回退
        aligned_count = total_exact + total_fuzzy
        total_count = aligned_count + total_inferred + total_none
        alignment_rate = aligned_count / total_count if total_count > 0 else 0.0

        if alignment_rate < MIN_ALIGNMENT_RATE:
            _fallback_uniform_time(ep["scenes"], subtitles)

    parsed["alignment_report"] = {
        "exact": total_exact,
        "fuzzy": total_fuzzy,
        "inferred": total_inferred,
        "none": total_none,
        "total": total_exact + total_fuzzy + total_inferred + total_none,
        "alignment_rate": (
            (total_exact + total_fuzzy) / (total_exact + total_fuzzy + total_inferred + total_none)
            if (total_exact + total_fuzzy + total_inferred + total_none) > 0
            else 0.0
        ),
    }

    return parsed


def _fallback_uniform_time(
    scenes: list[dict[str, Any]],
    subtitles: list[dict[str, Any]] | None = None,
) -> None:
    """回退策略: 将场景均匀分布在时间轴上。

    如果提供了字幕数据, 使用字幕的总时间范围; 否则使用场景索引 * 固定时长。
    """
    if subtitles and len(subtitles) >= 2:
        total_start = subtitles[0]["start_time"]
        total_end = subtitles[-1]["end_time"]
    else:
        total_start = 0.0
        total_end = len(scenes) * 180.0  # 默认每场景 3 分钟

    total_duration = total_end - total_start
    scene_duration = total_duration / len(scenes) if scenes else 0

    for i, scene in enumerate(scenes):
        scene["start_time"] = total_start + i * scene_duration
        scene["end_time"] = total_start + (i + 1) * scene_duration
        scene["alignment_confidence"] = "inferred"
        scene["alignment_source"] = "time_estimated"


# ── DB 写入 ───────────────────────────────────────────────────────────────────


def _write_scenes_to_db(
    parsed: dict[str, Any],
    book_id: str,
    db: StageDBClient,
) -> int:
    """将解析结果中的场景写入 scenes 表 (UPSERT)。

    每个场景映射到 scenes 表的字段:
      scene_id, book_id, episode_id, scene_order, heading,
      location, time_of_day, is_flashback, characters_present,
      dialogues, raw_description, distilled_summary, meta_tags,
      start_time, end_time, alignment_confidence, alignment_source,
      source='script'
    """
    scenes_to_upsert: list[dict[str, Any]] = []
    for ep in parsed.get("episodes", []):
        episode_id = ep["episode_number"]
        for scene in ep.get("scenes", []):
            scene_record = {
                "scene_id": scene.get("scene_id", f"S{episode_id}E{scene.get('scene_order', 0)}"),
                "book_id": book_id,
                "episode_id": episode_id,
                "scene_order": scene.get("scene_order"),
                "heading": scene.get("heading"),
                "location": scene.get("location"),
                "time_of_day": scene.get("time_of_day"),
                "is_flashback": scene.get("is_flashback", False),
                "flashback_label": scene.get("flashback_label"),
                "characters_present": scene.get("characters_present", []),
                "dialogues": scene.get("dialogues", []),
                "raw_description": scene.get("raw_description"),
                "distilled_summary": scene.get("distilled_summary"),
                "meta_tags": scene.get("meta_tags", {}),
                "start_time": scene.get("start_time"),
                "end_time": scene.get("end_time"),
                "alignment_confidence": scene.get("alignment_confidence"),
                "alignment_source": scene.get("alignment_source"),
                "source": "script",
                "detected_in_video": False,
                "vlm_verified": False,
            }
            scenes_to_upsert.append(scene_record)

    if not scenes_to_upsert:
        logger.warning("无场景数据写入 DB")
        return 0

    count = db.upsert_scenes(book_id, scenes_to_upsert)
    logger.info("写入 scenes 表: %d 条 (UPSERT)", count)
    return count


# ── 派生表写入 ─────────────────────────────────────────────────────────────────


def _extract_all_characters(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """从 LLM 解析的剧本中提取所有唯一角色。

    遍历所有集的 scenes → characters_present, 去重后返回角色列表。
    也统计角色首次/最后出现集数。
    """
    char_map: dict[str, dict[str, Any]] = {}
    for ep in parsed.get("episodes", []):
        ep_num = ep.get("episode_number", 0)
        for scene in ep.get("scenes", []):
            for char_name in scene.get("characters_present", []):
                if not char_name or not isinstance(char_name, str):
                    continue
                if char_name not in char_map:
                    char_map[char_name] = {
                        "name": char_name,
                        "source": "script",
                        "first_episode": ep_num,
                        "last_episode": ep_num,
                    }
                else:
                    entry = char_map[char_name]
                    entry["first_episode"] = min(entry["first_episode"], ep_num)
                    entry["last_episode"] = max(entry["last_episode"], ep_num)
    return list(char_map.values())


def _upsert_subject_episode(
    db: "StageDBClient",
    book_id: str,
    char_name: str,
    episode_number: int,
) -> None:
    """将角色与集的关联写入 subject_episodes 表。

    先通过 resolve_subject_id 查找角色, 若不存在则先创建再关联。
    """
    if not db.is_available:
        return
    subject_id = db.resolve_subject_id(book_id, char_name)
    if subject_id is None:
        # 角色尚未在 subjects 表中, 先创建
        name_to_id = db.upsert_subjects(
            book_id,
            [{"name": char_name, "source": "script"}],
        )
        subject_id = name_to_id.get(char_name)
    if subject_id is not None:
        db.upsert_subject_episodes(
            book_id,
            [{
                "subject_id": subject_id,
                "episode_id": episode_number,
                "appears_in_episode": True,
                "source": "script",
            }],
        )


def _write_derived_tables(
    parsed: dict[str, Any],
    book_id: str,
    db: "StageDBClient",
) -> None:
    """从 LLM 解析的剧本中提取角色、对话、场景并落表。

    1. 角色 → subjects 表 (source='script')
    2. 角色-集关联 → subject_episodes 表
    3. 场景描述 → shots 表 (粗粒度分镜)
    4. 对话 → subtitles 表 (source='script', 无精确时间戳)
    """
    if not db.is_available:
        logger.info("DB 不可用, 跳过派生表写入")
        return

    # 1. 角色 → subjects 表
    characters = _extract_all_characters(parsed)
    if characters:
        name_to_id = db.upsert_subjects(book_id, characters)
        logger.info("写入 subjects 表: %d 个角色 (source=script)", len(name_to_id))
    else:
        logger.info("未提取到角色, 跳过 subjects 写入")

    # 2. 角色-集关联 → subject_episodes 表
    for ep in parsed.get("episodes", []):
        ep_num = ep.get("episode_number", 0)
        for scene in ep.get("scenes", []):
            for char_name in scene.get("characters_present", []):
                if not char_name or not isinstance(char_name, str):
                    continue
                _upsert_subject_episode(db, book_id, char_name, ep_num)

    # 3. 场景描述 → shots 表 (粗粒度分镜)
    shot_count = 0
    for ep in parsed.get("episodes", []):
        ep_num = ep.get("episode_number", 0)
        shots_batch: list[dict[str, Any]] = []
        for scene in ep.get("scenes", []):
            shots_batch.append({
                "start_time": scene.get("start_time", 0),
                "end_time": scene.get("end_time", 0),
                "scene": scene.get("location", ""),
                "subjects": scene.get("characters_present", []),
                "source": "script",
            })
        if shots_batch:
            db.insert_shots(book_id, ep_num, shots_batch)
            shot_count += len(shots_batch)
    if shot_count > 0:
        logger.info("写入 shots 表: %d 条 (source=script)", shot_count)

    # 4. 对话 → subtitles 表 (source='script', 无精确时间戳)
    sub_count = 0
    for ep in parsed.get("episodes", []):
        ep_num = ep.get("episode_number", 0)
        sub_batch: list[dict[str, Any]] = []
        for scene in ep.get("scenes", []):
            for d in scene.get("dialogues", []):
                if not isinstance(d, dict):
                    continue
                sub_batch.append({
                    "start_time": d.get("approx_start", 0),
                    "end_time": d.get("approx_end", 0),
                    "text": d.get("text", ""),
                    "speaker": d.get("character", ""),
                    "source": "script",
                })
        if sub_batch:
            db.insert_subtitles(book_id, ep_num, sub_batch, source="script")
            sub_count += len(sub_batch)
    if sub_count > 0:
        logger.info("写入 subtitles 表: %d 条 (source=script)", sub_count)


# ── 产物发布 ──────────────────────────────────────────────────────────────────


def _publish_result(
    bus: ArtifactBus,
    root: Path,
    parsed: dict[str, Any],
    script_sha: str,
    book_id: str,
) -> list[Artifact]:
    """发布 source_script 产物到 ArtifactBus 并更新 project.json。"""
    episodes = parsed.get("episodes", [])
    episode_count = len(episodes)
    scene_count = sum(len(ep.get("scenes", [])) for ep in episodes)

    output = {
        "schema_version": "1.0",
        "status": "ok",
        "book_id": book_id,
        "source_file_sha": script_sha,
        "episodes_detected": episode_count,
        "total_scenes": scene_count,
        "alignment_report": parsed.get("alignment_report", {}),
        "parse_metadata": parsed.get("_parse_meta", parsed.get("parse_metadata", {})),
        "episodes": episodes,
    }

    # 写入落盘产物
    output_path = root / "source_script.json"
    atomic_write_json(output_path, output)

    from autocut_core.io import sha256_file

    ref = bus.put(
        "source_script",
        {"path": str(output_path)},
        stage="source_script",
    )

    update_project_stage(
        root / "project.json",
        "source_script",
        "completed",
        outputs={"source_script": sha256_file(output_path)},
    )

    return [ref]


def _publish_unavailable(
    bus: ArtifactBus,
    root: Path,
    book_id: str,
) -> list[Artifact]:
    """剧本文件不可用时的产物 — 标记 status='unavailable'。"""
    output = {
        "schema_version": "1.0",
        "status": "unavailable",
        "book_id": book_id,
        "episodes_detected": 0,
        "total_scenes": 0,
        "alignment_report": {},
        "episodes": [],
    }

    output_path = root / "source_script.json"
    atomic_write_json(output_path, output)

    from autocut_core.io import sha256_file

    ref = bus.put(
        "source_script",
        {"path": str(output_path)},
        stage="source_script",
    )

    update_project_stage(
        root / "project.json",
        "source_script",
        "completed",
        outputs={"source_script": sha256_file(output_path)},
        note="script file not found — status=unavailable",
    )

    return [ref]


# ── 缓存 ──────────────────────────────────────────────────────────────────────


def _cache_dir(root: Path) -> Path:
    """返回 source_script 的缓存目录。"""
    d = root / ".sd-cache" / "source_script"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_cache(root: Path, script_sha: str) -> dict[str, Any] | None:
    """尝试加载剧本解析缓存。"""
    cache_path = _cache_dir(root) / f"{script_sha[:16]}.json"
    if cache_path.is_file():
        try:
            return _load_json(cache_path)
        except (OSError, json.JSONDecodeError):
            logger.warning("缓存读取失败: %s", cache_path)
    return None


def _save_cache(root: Path, script_sha: str, data: dict[str, Any]) -> None:
    """保存剧本解析缓存。"""
    cache_path = _cache_dir(root) / f"{script_sha[:16]}.json"
    atomic_write_json(cache_path, data)


# ── 工具 ──────────────────────────────────────────────────────────────────────


def _load_json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件。"""
    with path.expanduser().resolve().open("r", encoding="utf-8") as f:
        return json.load(f)
