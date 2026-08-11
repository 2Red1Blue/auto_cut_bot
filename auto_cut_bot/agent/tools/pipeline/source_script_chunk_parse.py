"""SourceScriptChunkParseTool — 解析单个剧本 chunk，供 Agent 在 MapReduce 模式中按 chunk 调用。

在 MapReduce 流水线中，source_script_load 将剧本全文返回给 Agent 后，
Agent 将剧本切分为多个 chunk，对每个 chunk 调用此 tool 进行 LLM 解析。
解析结果由 source_script_save 合并持久化。

该 tool 复用 stage.py 中已有的 LLM 调用、验证和重试逻辑。
"""

from __future__ import annotations

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
        "chunk_id": {
            "type": "integer",
            "description": "1-based chunk identifier. Used for ordering and result tracking.",
        },
        "chunk_text": {
            "type": "string",
            "description": "The full text of this chunk to parse.",
        },
        "chunk_meta": {
            "type": "object",
            "description": (
                "Metadata about this chunk: episode_range [start_ep, end_ep], "
                "char_start, char_end (character offsets in the original script)."
            ),
            "properties": {
                "episode_range": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": "Expected episode range [start, end] for this chunk.",
                },
                "char_start": {
                    "type": "integer",
                    "description": "Starting character offset in the original script.",
                },
                "char_end": {
                    "type": "integer",
                    "description": "Ending character offset in the original script.",
                },
            },
        },
    },
    "required": ["job_root", "chunk_id", "chunk_text", "chunk_meta"],
})
class SourceScriptChunkParseTool(Tool):
    """Parse a single chunk of script text using LLM.

    Called once per chunk in MapReduce mode. The agent splits the full
    script text into overlapping chunks, then calls this tool for each
    chunk to produce structured episode/scene data.

    This tool reuses the existing _parse_single_chunk logic from the
    source_script stage, which includes LLM calling, output validation,
    and automatic retry with guardrails.
    """

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "source_script_chunk_parse"

    @property
    def description(self) -> str:
        return (
            "Parse a single chunk of script text using LLM. "
            "Call this once per chunk in MapReduce mode. "
            "The chunk_text should include enough overlap with neighboring chunks "
            "to avoid splitting episodes across chunk boundaries. "
            "Returns structured episode data with parse metadata."
        )

    @property
    def read_only(self) -> bool:
        return False

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "job_root": {
                    "type": "string",
                    "description": "Root directory for the pipeline job (absolute path).",
                },
                "chunk_id": {
                    "type": "integer",
                    "description": "1-based chunk identifier. Used for ordering and result tracking.",
                },
                "chunk_text": {
                    "type": "string",
                    "description": "The full text of this chunk to parse.",
                },
                "chunk_meta": {
                    "type": "object",
                    "description": (
                        "Metadata about this chunk: episode_range [start_ep, end_ep], "
                        "char_start, char_end (character offsets in the original script)."
                    ),
                    "properties": {
                        "episode_range": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 2,
                            "maxItems": 2,
                            "description": "Expected episode range [start, end] for this chunk.",
                        },
                        "char_start": {
                            "type": "integer",
                            "description": "Starting character offset in the original script.",
                        },
                        "char_end": {
                            "type": "integer",
                            "description": "Ending character offset in the original script.",
                        },
                    },
                },
            },
            "required": ["job_root", "chunk_id", "chunk_text", "chunk_meta"],
        }

    async def execute(self, **kwargs: Any) -> Any:
        """Parse a single chunk of script text.

        1. Import shared LLM utilities from the source_script stage.
        2. Build a PipelineConfig from job_root.
        3. Set expected_episode_count from chunk_meta.episode_range.
        4. Call _parse_single_chunk(chunk_text, cfg) — reuses existing
           LLM call + validation + retry logic.
        5. Return structured result with episode data and parse metadata.
        """
        from autocut_core import PipelineConfig

        job_root = Path(kwargs["job_root"]).expanduser().resolve()
        if not job_root.is_dir():
            return ToolResult.error(f"job_root does not exist: {job_root}")

        chunk_id = int(kwargs["chunk_id"])
        chunk_text = str(kwargs["chunk_text"])
        chunk_meta = kwargs.get("chunk_meta", {})

        if not chunk_text.strip():
            return ToolResult.error(
                f"chunk_id={chunk_id}: chunk_text is empty. "
                "Check chunk boundaries in the split logic."
            )

        episode_range = chunk_meta.get("episode_range", [])
        if len(episode_range) != 2:
            return ToolResult.error(
                f"chunk_id={chunk_id}: chunk_meta.episode_range must be "
                f"[start_episode, end_episode], got {episode_range}"
            )

        # ── Import shared LLM utilities ──────────────────────────────────────
        try:
            from auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse import (
                SYSTEM_PROMPT,
                _parse_single_chunk,
                _validate_episode_boundaries,
                _build_guardrails,
            )
        except ImportError:
            # Fallback: try ac_auto_cut path
            try:
                from base_auto_cut.plugins.ac_source_prep.stages.source_script.llm_parse import (
                    SYSTEM_PROMPT,
                    _parse_single_chunk,
                    _validate_episode_boundaries,
                    _build_guardrails,
                )
            except ImportError as exc:
                return ToolResult.error(
                    f"chunk_id={chunk_id}: Failed to import shared LLM utilities. "
                    f"Ensure the source_script stage is installed. "
                    f"Tried: auto_cut_bot.pipeline.plugins.ac_source_prep, "
                    f"base_auto_cut.plugins.ac_source_prep. "
                    f"Error: {exc}"
                )

        # ── Build PipelineConfig ──────────────────────────────────────────────
        cfg = PipelineConfig(job_root=job_root)

        # Set expected episode count from chunk_meta
        start_ep, end_ep = int(episode_range[0]), int(episode_range[1])
        expected_count = end_ep - start_ep + 1
        if hasattr(cfg, "extra") and isinstance(cfg.extra, dict):
            cfg.extra["expected_episode_count"] = expected_count
        elif hasattr(cfg, "extra"):
            cfg.extra.expected_episode_count = expected_count

        # ── Parse the chunk ───────────────────────────────────────────────────
        try:
            result = _parse_single_chunk(chunk_text, cfg)
        except Exception as exc:
            return ToolResult.error(
                f"chunk_id={chunk_id}: LLM parsing failed. "
                f"Error: {exc}\n\n"
                "Retry guidance:\n"
                "1. Check that chunk_text is valid and complete (no mid-episode truncation).\n"
                "2. If chunk is too large, split into smaller sub-chunks.\n"
                "3. Verify the LLM backend is configured correctly.\n"
                f"4. The chunk covers episodes {start_ep}-{end_ep} "
                f"({expected_count} episodes expected)."
            )

        episodes = result.get("episodes", [])
        parse_meta = result.get("_parse_meta", {})

        # ── Check for parse errors ────────────────────────────────────────────
        if parse_meta.get("status") == "parse_error":
            errors = parse_meta.get("errors", ["unknown"])
            return ToolResult.error(
                f"chunk_id={chunk_id}: All {parse_meta.get('attempts', '?')} "
                f"parse attempts failed.\n"
                "Errors:\n- " + "\n- ".join(errors) + "\n\n"
                "Retry guidance:\n"
                "1. Check if chunk boundaries split an episode; adjust overlap.\n"
                "2. Reduce chunk size if the LLM is truncating output.\n"
                "3. Verify chunk_text encoding (should be UTF-8).\n"
                f"4. This chunk should cover episodes {start_ep}-{end_ep}."
            )

        # ── Build response ────────────────────────────────────────────────────
        total_scenes = sum(len(ep.get("scenes", [])) for ep in episodes)

        response = {
            "chunk_id": chunk_id,
            "episode_range": [start_ep, end_ep],
            "episodes_parsed": len(episodes),
            "scenes_parsed": total_scenes,
            "episodes": episodes,
            "parse_meta": {
                **parse_meta,
                "chunk_id": chunk_id,
                "expected_episodes": expected_count,
                "char_start": chunk_meta.get("char_start"),
                "char_end": chunk_meta.get("char_end"),
            },
        }

        return ToolResult(
            f"CHUNK_PARSED\n\n"
            f"chunk_id={chunk_id}: Parsed {len(episodes)} episodes, "
            f"{total_scenes} scenes. "
            f"Episodes: {start_ep}-{end_ep}. "
            f"Attempts: {parse_meta.get('attempts', '?')}.\n\n"
            "--- CHUNK RESULT DATA ---\n\n"
            f"{json.dumps(response, ensure_ascii=False, indent=2)}"
        )