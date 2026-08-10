"""SourceScriptLoadTool — 加载剧本文件，返回全文给 Agent。

Agent 调用此 tool 拿到剧本全文后，在自己的对话上下文中完成解析。
Agent 的 LLM 就是解析器——不需要中间 tool 调 litellm。

解析完成后，Agent 调用 source_script_save 持久化结果。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters


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

    _scopes = {"pipeline"}

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
        from autocut_core import PipelineConfig

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

        # Check cache
        if not force_reparse:
            cached = _load_cache(job_root, script_sha)
            if cached is not None:
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

        # Build instructions for the agent
        instructions = _build_agent_instructions(script_text, format_hint, script_path)

        return ToolResult(
            f"SCRIPT_LOADED\n\n"
            f"File: {script_path.name}\n"
            f"Characters: {len(script_text)}\n"
            f"Format: {format_hint}\n"
            f"SHA256: {script_sha[:16]}\n\n"
            f"{instructions}\n\n"
            f"--- SCRIPT TEXT BELOW ---\n\n"
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
    import re
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


def _build_agent_instructions(
    script_text: str, format_hint: str, script_path: Path
) -> str:
    """Build parsing instructions for the agent."""
    import re

    # Try to estimate episode count from markers
    ep_markers = re.findall(
        r"第\s*(\d+)\s*集|Episode\s+(\d+)|EP\s*(\d+)|第\s*(\d+)\s*章",
        script_text,
        re.IGNORECASE,
    )
    estimated = "unknown"
    if ep_markers:
        nums = set()
        for m in ep_markers:
            for g in m:
                if g:
                    nums.add(int(g))
        if nums:
            estimated = str(max(nums))

    format_hints = {
        "chinese_numbered": "Scene format: '1-2 墓地 雨夜 外' (numbered headers). Parse scene_id as 'S{episode}E{scene_order}'.",
        "english_scene": "Scene format: 'Scene 2: The Graveyard'. Parse scene_id from scene number.",
        "screenplay": "Screenplay format: 'SCENE 1-2 - INT. WORKSHOP - DAY'. Extract INT/EXT and time of day.",
        "unknown": "Scene format not auto-detected. Analyze the first 5000 characters to determine the format.",
    }

    return (
        "## PARSING INSTRUCTIONS\n\n"
        "You are a script parser. Parse this script into structured JSON.\n\n"
        "### Steps:\n"
        "1. **Analyze structure**: Determine language, format, episode count. "
        f"Estimated episodes from markers: {estimated}.\n"
        "2. **Parse in segments** if the script is large: output episodes in "
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


def _load_cache(root: Path, script_sha: str) -> dict[str, Any] | None:
    """Load cached parse result."""
    cache_path = root / ".sd-cache" / "source_script" / f"{script_sha[:16]}.json"
    if cache_path.is_file():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return None