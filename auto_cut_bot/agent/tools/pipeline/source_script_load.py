"""SourceScriptLoadTool — 加载剧本文件，返回全文给 Agent。

Agent 调用此 tool 拿到剧本全文后，在自己的对话上下文中完成解析。
Agent 的 LLM 就是解析器——不需要中间 tool 调 litellm。

解析完成后，Agent 调用 source_script_save 持久化结果。

当剧本超过阈值时，自动生成 chunk_plan 并推荐 mapreduce 策略，
让 Agent 通过 source_script_chunk_parse 逐片解析。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters

# ── 常量 ──────────────────────────────────────────────────────────────────────

SCRIPT_SIZE_THRESHOLD = 50000  # 字符数阈值，超过此值启用 mapreduce 策略

# 集边界标记模式（与 stage.py 保持一致）
EPISODE_BOUNDARY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"第\s*(\d+)\s*集"),
    re.compile(r"Episode\s+(\d+)", re.IGNORECASE),
    re.compile(r"EP\s*(\d+)", re.IGNORECASE),
    re.compile(r"第\s*(\d+)\s*章"),
]


@tool_parameters({
    "type": "object",
    "properties": {
        "job_root": {
            "type": "string",
            "description": "Root directory for the pipeline job (absolute path).",
        },
        "force_reparse": {
            "type": "boolean",
            "description": "Skip cache and force re-parse, even if cached result exists. Use when previous result was truncated or incorrect.",
        },
    },
    "required": ["job_root"],
})
class SourceScriptLoadTool(Tool):
    """Load the script file and return full text to the agent.

    The agent reads the script, analyzes its structure using LLM
    (not regex), and parses it into structured JSON in its own
    conversation context. The agent then calls source_script_save
    to persist the result.

    This is the agent-native approach: the agent IS the parser.
    """

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "source_script_load"

    @property
    def description(self) -> str:
        return (
            "Load the script file from a pipeline job directory. "
            "Returns the full script text for the agent to parse in its own "
            "context. After parsing, call source_script_save to persist results."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        """Load script file and return text to agent.

        The agent receives the full script text and parse instructions.
        It should:
        1. Analyze the script structure (language, format, episode count)
        2. Parse in segments if needed (e.g., 3 rounds of 15 episodes each)
        3. Validate each segment (episode count, continuity, scene distribution)
        4. Call source_script_save with the complete parsed episodes
        """
        from autocut_core.config import PipelineConfig

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        force_reparse = kwargs.get("force_reparse", False)

        # Build config
        cfg = PipelineConfig(job_root=job_root)

        # Find and read script file
        script_path = _find_script_file(job_root, cfg)
        if script_path is None:
            return ToolResult(
                "SCRIPT_NOT_FOUND\n\n"
                "No script file found in the job directory. "
                "Place a .txt or .docx script file in the job root, or set "
                "script_path in config.\n\n"
                "Expected locations:\n"
                f"  - {job_root}/*.txt\n"
                f"  - {job_root}/*.docx\n"
                f"  - {job_root}/script/*"
            )

        script_text = _read_script(script_path)
        script_sha = hashlib.sha256(script_text.encode("utf-8")).hexdigest()

        # Check cache (file-based, agent-native — no ArtifactBus dependency)
        cache_dir = job_root / ".cache" / "source_script"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{script_sha[:16]}.json"
        if not force_reparse and cache_file.exists():
            cached = json.loads(cache_file.read_text())
            episodes = cached.get("episodes", [])
            meta = cached.get("_parse_meta", cached.get("parse_metadata", {}))
            return ToolResult(
                "CACHE_HIT\n\n"
                f"Script already parsed: {len(episodes)} episodes, "
                f"{sum(len(e.get('scenes', [])) for e in episodes)} scenes.\n"
                f"Cache key: {script_sha[:16]}\n"
                f"Parse metadata: {json.dumps(meta, ensure_ascii=False)}\n\n"
                "If this result is incorrect (e.g., truncated), re-call "
                "with force_reparse=true."
            )

        # Detect format
        format_hint = _detect_format(script_text)

        # Compute size metrics
        total_chars = len(script_text)
        estimated_episodes = _count_episode_markers(script_text)

        # Determine strategy
        if total_chars >= SCRIPT_SIZE_THRESHOLD:
            chunk_plan = _compute_chunk_plan(script_text)
            strategy = "mapreduce"
            instructions = _build_agent_instructions(
                script_text, format_hint, script_path, strategy="mapreduce"
            )
            return ToolResult(
                "SCRIPT_LOADED\n\n"
                f"File: {script_path.name}\n"
                f"Characters: {total_chars}\n"
                f"Estimated Episodes: {estimated_episodes}\n"
                f"Format: {format_hint}\n"
                f"SHA256: {script_sha[:16]}\n"
                f"Strategy: mapreduce\n"
                f"Total Chunks: {len(chunk_plan)}\n\n"
                f"{instructions}\n\n"
                "--- CHUNK PLAN ---\n\n"
                f"{json.dumps(chunk_plan, ensure_ascii=False, indent=2)}\n\n"
                "--- SCRIPT TEXT BELOW ---\n\n"
                f"{script_text}"
            )
        else:
            strategy = "direct"
            instructions = _build_agent_instructions(
                script_text, format_hint, script_path, strategy="direct"
            )
            return ToolResult(
                "SCRIPT_LOADED\n\n"
                f"File: {script_path.name}\n"
                f"Characters: {total_chars}\n"
                f"Estimated Episodes: {estimated_episodes}\n"
                f"Format: {format_hint}\n"
                f"SHA256: {script_sha[:16]}\n"
                f"Strategy: direct\n\n"
                f"{instructions}\n\n"
                "--- SCRIPT TEXT BELOW ---\n\n"
                f"{script_text}"
            )


# ── 复用 stage.py 的工具函数 ──────────────────────────────────────────────────


def _find_script_file(job_root: Path, cfg: Any) -> Path | None:
    """Find script file in job_root."""
    # Explicit path from config
    explicit = cfg.extra.get("script_path") if hasattr(cfg, "extra") else None
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_file():
            return path

    # Search job_root for .txt / .docx
    candidates = sorted(job_root.glob("*.txt")) + sorted(job_root.glob("*.docx"))
    if candidates:
        return candidates[0]

    # Search job_root/script/
    script_dir = job_root / "script"
    if script_dir.is_dir():
        candidates = sorted(script_dir.glob("*"))
        if candidates:
            return candidates[0]

    return None


def _read_script(path: Path) -> str:
    """Read script file, supporting .txt and .docx."""
    if path.suffix == ".docx":
        try:
            from docx import Document
        except ImportError:
            raise ImportError("Reading .docx requires python-docx. Run: pip install python-docx")
        doc = Document(str(path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)
    return path.read_text(encoding="utf-8-sig")


def _detect_format(text: str, sample_size: int = 5000) -> str:
    """Detect script format from text sample."""
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


# ── 集边界检测 ────────────────────────────────────────────────────────────────


def _find_episode_boundaries(text: str) -> list[int]:
    """在文本中查找所有集边界的字符位置。

    与 stage.py 中的同名函数保持逻辑一致。
    """
    positions: list[int] = []
    for pattern in EPISODE_BOUNDARY_PATTERNS:
        for match in pattern.finditer(text):
            positions.append(match.start())
    return sorted(set(positions))


def _count_episode_markers(script_text: str) -> int:
    """统计剧本中的集数标记，返回估算的集数。

    使用正则表达式匹配以下模式：
    - 第X集
    - Episode X
    - EP X
    - 第X章
    """
    nums: set[int] = set()
    for pattern in EPISODE_BOUNDARY_PATTERNS:
        for match in pattern.finditer(script_text):
            # 所有模式都有至少一个捕获组，取第一个非空组
            for group in match.groups():
                if group is not None:
                    nums.add(int(group))
                    break
    return max(nums) if nums else 0


# ── 分片计划 ──────────────────────────────────────────────────────────────────


def _compute_chunk_plan(
    script_text: str,
    target_chars_per_chunk: int = 28000,
) -> list[dict[str, Any]]:
    """根据集边界将剧本拆分为多个 chunk，返回 chunk 计划。

    每个 chunk 尽量在集边界处切分，目标大小约为 target_chars_per_chunk 字符。
    如果两个连续集边界之间的文本超过 1.5 倍目标大小，则在该区间内按字符切分。

    Returns:
        List of dicts with keys:
        - chunk_id: str, 从 "chunk_1" 开始编号
        - episode_range: str, 如 "1-3" 表示包含第 1-3 集
        - char_start: int, 在原文中的起始字符位置
        - char_end: int, 在原文中的结束字符位置（不含）
        - char_count: int, chunk 字符数
    """
    boundaries = _find_episode_boundaries(script_text)
    if not boundaries:
        # 没有检测到集边界，按字符大小切分
        return _fallback_char_chunks(script_text, target_chars_per_chunk)

    # 统计每个边界对应的集号
    boundary_episodes: list[int] = []
    for idx, pos in enumerate(boundaries):
        # 提取边界位置附近的文本片段用于匹配
        snippet_start = max(0, pos - 5)
        snippet_end = min(len(script_text), pos + 50)
        snippet = script_text[snippet_start:snippet_end]
        for pattern in EPISODE_BOUNDARY_PATTERNS:
            m = pattern.search(snippet)
            if m:
                for group in m.groups():
                    if group is not None:
                        boundary_episodes.append(int(group))
                        break
                break
        else:
            # 放宽匹配范围
            wide_snippet = script_text[max(0, pos - 30):min(len(script_text), pos + 100)]
            for pattern in EPISODE_BOUNDARY_PATTERNS:
                m = pattern.search(wide_snippet)
                if m:
                    for group in m.groups():
                        if group is not None:
                            boundary_episodes.append(int(group))
                            break
                    break
            else:
                boundary_episodes.append(-1)

    # 构建 chunk 计划
    chunks: list[dict[str, Any]] = []
    start = 0
    chunk_idx = 0
    current_ep_start: int | None = None

    for i, boundary in enumerate(boundaries):
        if boundary <= start:
            continue

        # 确定当前 chunk 的集范围
        if current_ep_start is None:
            current_ep_start = boundary_episodes[i] if boundary_episodes[i] > 0 else 1

        segment_size = boundary - start
        if segment_size > target_chars_per_chunk * 1.5:
            # 该区间太大，需要按字符大小进一步切分
            sub_chunks = _fallback_char_chunks(
                script_text[start:boundary], target_chars_per_chunk, offset=start
            )
            for sc in sub_chunks:
                chunk_idx += 1
                sub_ep_end = boundary_episodes[i] if boundary_episodes[i] > 0 else current_ep_start
                chunks.append({
                    "chunk_id": f"chunk_{chunk_idx}",
                    "episode_range": (
                        str(current_ep_start) if current_ep_start == sub_ep_end
                        else f"{current_ep_start}-{sub_ep_end}"
                    ),
                    "char_start": sc["char_start"],
                    "char_end": sc["char_end"],
                    "char_count": sc["char_count"],
                })
                current_ep_start = sub_ep_end + 1
            start = boundary
            current_ep_start = boundary_episodes[i] + 1 if boundary_episodes[i] > 0 else None
            continue

        if segment_size >= target_chars_per_chunk * 0.5 or (
            i + 1 < len(boundaries) and (boundaries[i + 1] - start) > target_chars_per_chunk * 1.3
        ):
            # 当前区间足够大，或者下一个边界还很远，在此处切分
            chunk_idx += 1
            ep_end = boundary_episodes[i] if boundary_episodes[i] > 0 else current_ep_start
            chunks.append({
                "chunk_id": f"chunk_{chunk_idx}",
                "episode_range": (
                    str(current_ep_start) if current_ep_start == ep_end
                    else f"{current_ep_start}-{ep_end}"
                ),
                "char_start": start,
                "char_end": boundary,
                "char_count": boundary - start,
            })
            start = boundary
            current_ep_start = boundary_episodes[i] + 1 if boundary_episodes[i] > 0 else None

    # 处理最后一段
    if start < len(script_text):
        remaining = len(script_text) - start
        if remaining > target_chars_per_chunk * 1.5:
            sub_chunks = _fallback_char_chunks(
                script_text[start:], target_chars_per_chunk, offset=start
            )
            for sc in sub_chunks:
                chunk_idx += 1
                chunks.append({
                    "chunk_id": f"chunk_{chunk_idx}",
                    "episode_range": str(current_ep_start) if current_ep_start else "?",
                    "char_start": sc["char_start"],
                    "char_end": sc["char_end"],
                    "char_count": sc["char_count"],
                })
        else:
            chunk_idx += 1
            chunks.append({
                "chunk_id": f"chunk_{chunk_idx}",
                "episode_range": (
                    str(current_ep_start) if current_ep_start else "?"
                ),
                "char_start": start,
                "char_end": len(script_text),
                "char_count": remaining,
            })

    return chunks


def _fallback_char_chunks(
    text: str,
    target_chars: int,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """当没有集边界时，按字符数切分。

    Args:
        text: 要切分的文本
        target_chars: 目标 chunk 大小
        offset: 起始字符偏移量（用于计算绝对位置）

    Returns:
        list of chunk dicts
    """
    chunks: list[dict[str, Any]] = []
    idx = 0
    pos = 0
    while pos < len(text):
        idx += 1
        end = min(pos + target_chars, len(text))
        chunks.append({
            "chunk_id": f"chunk_{idx}",
            "episode_range": "?",
            "char_start": offset + pos,
            "char_end": offset + end,
            "char_count": end - pos,
        })
        pos = end
    return chunks


# ── Agent 指令构建 ────────────────────────────────────────────────────────────


def _build_agent_instructions(
    script_text: str, format_hint: str, script_path: Path, strategy: str = "direct"
) -> str:
    """Build parsing instructions for the agent.

    Args:
        script_text: 剧本全文
        format_hint: 格式提示
        script_path: 剧本文件路径
        strategy: "direct" 或 "mapreduce"
    """

    format_hints = {
        "chinese_numbered": "Scene format: '1-2 墓地 雨夜 外' (numbered headers). Parse scene_id as 'S{episode}E{scene_order}'.",
        "english_scene": "Scene format: 'Scene 2: The Graveyard'. Parse scene_id from scene number.",
        "screenplay": "Screenplay format: 'SCENE 1-2 - INT. WORKSHOP - DAY'. Extract INT/EXT and time of day.",
        "unknown": "Scene format not auto-detected. Analyze the first 5000 characters to determine the format.",
    }

    if strategy == "mapreduce":
        return _build_mapreduce_instructions(format_hint, format_hints, script_path)
    else:
        return _build_direct_instructions(script_text, format_hint, format_hints, script_path)


def _build_direct_instructions(
    script_text: str,
    format_hint: str,
    format_hints: dict[str, str],
    script_path: Path,
) -> str:
    """Build instructions for direct (in-context) parsing strategy."""
    estimated = _count_episode_markers(script_text)
    return (
        "## PARSING INSTRUCTIONS\n\n"
        "You are a script parser. Parse this script into structured JSON.\n\n"
        "### Strategy: DIRECT\n"
        "The script is small enough to parse in your context. "
        "Parse all episodes directly.\n\n"
        "### Steps:\n"
        "1. **Analyze structure**: Determine language, format, episode count. "
        f"Estimated episodes from markers: {estimated}.\n"
        "2. **Parse in segments** if needed: output episodes in "
        "batches (e.g., 1-15, 16-30, 31-45) to avoid output truncation.\n"
        "3. **Validate each segment**: check episode count, continuity, "
        "scene distribution. If a segment is wrong, re-parse it.\n"
        "4. **Merge all segments** into a complete episodes list.\n"
        "5. **Call source_script_save** with the merged episodes.\n\n"
        f"### Format hint: {format_hint}\n"
        f"{format_hints.get(format_hint, '')}\n\n"
        "### Output schema per episode:\n"
        '{\n'
        '  "episode_number": int,\n'
        '  "title": "optional",\n'
        '  "scenes": [{\n'
        '    "scene_id": "S{ep}E{order}",\n'
        '    "scene_order": int,\n'
        '    "heading": "original header text",\n'
        '    "location": "location name",\n'
        '    "time_of_day": "日/夜/晨/暮/日内/夜外",\n'
        '    "is_flashback": false,\n'
        '    "characters_present": ["name1", "name2"],\n'
        '    "dialogues": [{"character": "name", "text": "...", "sequence": 1}],\n'
        '    "raw_description": "narrative text between dialogues",\n'
        '    "meta_tags": {}\n'
        '  }]\n'
        '}\n\n'
        "### Call save when done:\n"
        "After parsing all episodes, call:\n"
        f"source_script_save(job_root='{script_path.parent}', "
        'episodes=[...], parse_meta={...})'
    )


def _build_mapreduce_instructions(
    format_hint: str,
    format_hints: dict[str, str],
    script_path: Path,
) -> str:
    """Build instructions for mapreduce (chunked) parsing strategy."""
    return (
        "## PARSING INSTRUCTIONS\n\n"
        "You are a script parser. Parse this script into structured JSON.\n\n"
        "### Strategy: MAPREDUCE\n"
        "This script is large. Use source_script_chunk_parse for each chunk "
        "in the chunk_plan below. Each chunk covers a specific episode range "
        "and contains a manageable amount of text.\n\n"
        "### Steps:\n"
        "1. **Review the chunk_plan** above to understand the episode ranges "
        "and character offsets.\n"
        "2. **For each chunk** in the chunk_plan, call "
        "source_script_chunk_parse with the chunk details. Use the char_start "
        "and char_end offsets to extract the relevant text from the full script.\n"
        "3. **Collect all results** from source_script_chunk_parse calls.\n"
        "4. **Merge all episodes** into a single list, preserving episode order.\n"
        "5. **Validate continuity**: check that episode numbers are sequential "
        "and no scenes are missing.\n"
        "6. **Call source_script_save** with the merged episodes.\n\n"
        f"### Format hint: {format_hint}\n"
        f"{format_hints.get(format_hint, '')}\n\n"
        "### Output schema per episode:\n"
        '{\n'
        '  "episode_number": int,\n'
        '  "title": "optional",\n'
        '  "scenes": [{\n'
        '    "scene_id": "S{ep}E{order}",\n'
        '    "scene_order": int,\n'
        '    "heading": "original header text",\n'
        '    "location": "location name",\n'
        '    "time_of_day": "日/夜/晨/暮/日内/夜外",\n'
        '    "is_flashback": false,\n'
        '    "characters_present": ["name1", "name2"],\n'
        '    "dialogues": [{"character": "name", "text": "...", "sequence": 1}],\n'
        '    "raw_description": "narrative text between dialogues",\n'
        '    "meta_tags": {}\n'
        '  }]\n'
        '}\n\n'
        "### Call save when done:\n"
        "After parsing all episodes, call:\n"
        f"source_script_save(job_root='{script_path.parent}', "
        'episodes=[...], parse_meta={...})'
    )