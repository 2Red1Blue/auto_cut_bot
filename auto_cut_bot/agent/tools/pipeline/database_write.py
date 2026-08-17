"""DatabaseWriteTool — Agent 审核后写入 PostgreSQL。

Agent-native 设计:
1. Pipeline Tool 执行 → 返回结果给 Agent
2. Agent 审核结果 → 决定是否写入 DB
3. 调用 database_write Tool 写入
4. 降级: Agent 审查失败/超时 → 自动写入 (fallback)
"""

from __future__ import annotations

import json
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import ToolContext
from auto_cut_bot.agent.runtime.state import get_db_client


@tool_parameters({
    "type": "object",
    "properties": {
        "job_root": {
            "type": "string",
            "description": "Pipeline job root directory.",
        },
        "stage": {
            "type": "string",
            "description": "Which stage produced this data (e.g. 'source_windows').",
        },
        "tables": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tables to write: books, subjects, episodes, scenes, subtitles, shots, boundaries, relationships, speaker_mappings, subject_episodes",
        },
        "data": {
            "type": "object",
            "description": "Data to write. Structure depends on the table.",
        },
        "auto_fallback": {
            "type": "boolean",
            "description": "If true, this is an auto-fallback write (Agent review failed).",
            "default": False,
        },
    },
    "required": ["job_root", "stage", "tables", "data"],
})
class DatabaseWriteTool(Tool):
    """Agent 审核后写入 PostgreSQL 数据库。

    Pipeline Tool 执行完成后，Agent 审核结果，然后调用此工具写入。
    如果 Agent 审查失败或超时，系统自动调用此工具 (auto_fallback=true)。
    """
    _scopes = {"subagent"}


    name = "database_write"
    description = (
        "将流水线 Stage 的产物写入 PostgreSQL 数据库。"
        "支持写入 10 张表: books, subjects, episodes, scenes, subtitles, "
        "shots, boundaries, relationships, speaker_mappings, subject_episodes。"
        "在调用此工具前，请先审核 Stage 产物的完整性和正确性。"
    )

    async def execute(self, **kwargs: Any) -> ToolResult:
        job_root = kwargs["job_root"]
        stage = kwargs["stage"]
        tables = kwargs["tables"]
        data = kwargs["data"]
        auto_fallback = kwargs.get("auto_fallback", False)

        db = get_db_client(job_root)
        if db is None:
            return ToolResult(
                success=False,
                output={"error": "DB not available (no db_url configured or driver missing)"},
            )

        results: dict[str, Any] = {
            "stage": stage,
            "auto_fallback": auto_fallback,
            "written": {},
        }

        try:
            for table in tables:
                method = self._table_method(db, table)
                if method is None:
                    results["written"][table] = f"unknown table: {table}"
                    continue

                count = method(db, data, stage)
                results["written"][table] = count

            db.close()
            return ToolResult(
                success=True,
                output=results,
            )

        except Exception as e:
            results["error"] = str(e)
            try:
                db.close()
            except Exception:
                pass
            return ToolResult(
                success=False,
                output=results,
            )

    def _table_method(self, db: Any, table: str) -> Any | None:
        """Map table name to StageDBClient method."""
        mapping = {
            "books": self._write_books,
            "subjects": self._write_subjects,
            "episodes": self._write_episodes,
            "scenes": self._write_scenes,
            "subtitles": self._write_subtitles,
            "shots": self._write_shots,
            "boundaries": self._write_boundaries,
            "relationships": self._write_relationships,
            "speaker_mappings": self._write_speaker_mappings,
            "subject_episodes": self._write_subject_episodes,
        }
        return mapping.get(table)

    def _write_books(self, db: Any, data: dict, stage: str) -> int:
        book_id = data.get("book_id")
        if not book_id:
            return 0
        return db.upsert_book(
            book_id=book_id,
            book_name=data.get("book_name", ""),
            total_episodes=data.get("total_episodes"),
            genre=data.get("genre"),
            overall_synopsis=data.get("overall_synopsis"),
        )

    def _write_subjects(self, db: Any, data: dict, stage: str) -> int:
        subjects = data.get("subjects", [])
        if not subjects:
            return 0
        result = db.upsert_subjects(data.get("book_id", ""), subjects)
        return len(result)

    def _write_episodes(self, db: Any, data: dict, stage: str) -> int:
        episodes = data.get("episodes", [])
        if not episodes:
            return 0
        return db.upsert_episodes(data.get("book_id", ""), episodes)

    def _write_scenes(self, db: Any, data: dict, stage: str) -> int:
        scenes = data.get("scenes", [])
        if not scenes:
            return 0
        return db.upsert_scenes(data.get("book_id", ""), scenes)

    def _write_subtitles(self, db: Any, data: dict, stage: str) -> int:
        return db.insert_subtitles(
            data.get("book_id", ""),
            data.get("episode_id", 0),
            data.get("segments", []),
        )

    def _write_shots(self, db: Any, data: dict, stage: str) -> int:
        return db.insert_shots(
            data.get("book_id", ""),
            data.get("episode_id", 0),
            data.get("shots", []),
        )

    def _write_boundaries(self, db: Any, data: dict, stage: str) -> int:
        return db.insert_boundaries(data.get("book_id", ""), data.get("boundaries", []))

    def _write_relationships(self, db: Any, data: dict, stage: str) -> int:
        return db.upsert_relationships(data.get("book_id", ""), data.get("relationships", []))

    def _write_speaker_mappings(self, db: Any, data: dict, stage: str) -> int:
        return db.upsert_speaker_mappings(
            data.get("book_id", ""),
            data.get("episode_id", 0),
            data.get("mappings", []),
        )

    def _write_subject_episodes(self, db: Any, data: dict, stage: str) -> int:
        return db.upsert_subject_episodes(data.get("book_id", ""), data.get("entries", []))