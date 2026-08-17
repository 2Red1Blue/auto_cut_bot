"""SourceTranscriptsTool — FunASR 语音转写, 生成字幕数据。

Agent-native: Agent 调用 FunASR 服务进行语音识别, 产出字幕 JSON。
与 Platform API 字幕进行多源合并 (merge_operator)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import ToolContext
from auto_cut_bot.agent.runtime.state import get_db_client, mark_stage_complete


@tool_parameters({
    "type": "object",
    "properties": {
        "job_root": {
            "type": "string",
            "description": "Pipeline job root directory.",
        },
        "audio_path": {
            "type": "string",
            "description": "Path to audio file or directory of audio files.",
        },
        "language": {
            "type": "string",
            "description": "ASR language: zh, en, ja, ko, auto",
            "default": "zh",
        },
        "funasr_url": {
            "type": "string",
            "description": "FunASR service URL (default: http://localhost:8001/recognition)",
            "default": "http://localhost:8001/recognition",
        },
        "merge_with_api": {
            "type": "boolean",
            "description": "Whether to merge ASR results with existing API subtitles using merge_operator.",
            "default": True,
        },
    },
    "required": ["job_root", "audio_path"],
})
class SourceTranscriptsTool(Tool):
    """FunASR 语音转写工具。

    调用 FunASR HTTP 服务进行语音识别, 生成字幕数据。
    与 Platform API 字幕进行多源合并, 解决冲突。
    """
    _scopes = {"subagent"}


    name = "source_transcripts"
    description = (
        "使用 FunASR 进行语音转写, 生成字幕 JSON。"
        "支持中文/英文/日文/韩文。"
        "自动与 API 字幕进行多源合并 (merge_operator)。"
    )

    async def execute(self, **kwargs: Any) -> ToolResult:
        job_root = kwargs["job_root"]
        audio_path = kwargs.get("audio_path", "")
        language = kwargs.get("language", "zh")
        funasr_url = kwargs.get("funasr_url", "http://localhost:8001/recognition")
        merge_with_api = kwargs.get("merge_with_api", True)

        job_root = Path(job_root).expanduser().resolve()
        audio_path = Path(audio_path).expanduser().resolve()

        if not audio_path.exists():
            return ToolResult.error(f"Audio path not found: {audio_path}")

        # Collect audio files
        audio_files = []
        if audio_path.is_file():
            audio_files = [audio_path]
        elif audio_path.is_dir():
            audio_files = list(audio_path.glob("*.mp3")) + list(audio_path.glob("*.wav")) + list(audio_path.glob("*.m4a"))

        if not audio_files:
            return ToolResult.error(f"No audio files found in: {audio_path}")

        results = []
        import json
        import aiohttp

        async with aiohttp.ClientSession() as session:
            for audio_file in audio_files:
                try:
                    with open(audio_file, "rb") as f:
                        form = aiohttp.FormData()
                        form.add_field("file", f, filename=audio_file.name)
                        async with session.post(funasr_url, data=form) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                data["source_file"] = str(audio_file)
                                results.append(data)
                            else:
                                results.append({"error": f"FunASR returned {resp.status}", "file": str(audio_file)})
                except Exception as e:
                    results.append({"error": str(e), "file": str(audio_file)})

        # Save results
        output_path = job_root / "source_transcripts.json"
        output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))

        # Merge with API subtitles if available
        merge_results = None
        if merge_with_api:
            try:
                from auto_cut_bot.agent.runtime.contracts.merge_operator import merge, merge_summary
                api_subtitles_path = job_root / "api_subtitles.json"
                api_subtitles = json.loads(api_subtitles_path.read_text()) if api_subtitles_path.exists() else []
                merge_results = []
                for asr, api in zip(results, api_subtitles):
                    result = merge(
                        asr, api,
                        "asr", "platform_api",
                        "subtitles",
                        mode="auto",
                    )
                    merge_results.append(merge_summary(result))
            except Exception:
                pass

        # DB write
        db = get_db_client(str(job_root))
        if db:
            for r in results:
                if "text" in r:
                    db.insert_subtitles(
                        r.get("book_id", ""),
                        r.get("episode_id", 0),
                        [{
                            "start_time": s.get("start", 0),
                            "end_time": s.get("end", 0),
                            "text": s.get("text", ""),
                            "speaker": s.get("speaker", ""),
                            "confidence": s.get("confidence"),
                            "source": "asr",
                        } for s in r.get("sentences", [])],
                        source="asr",
                    )

        mark_stage_complete(None, self.name, {"source_transcripts": str(output_path)})

        return ToolResult(
            success=True,
            output={
                "files_processed": len(audio_files),
                "results": len(results),
                "output": str(output_path),
                "merge_summary": merge_results,
            },
        )