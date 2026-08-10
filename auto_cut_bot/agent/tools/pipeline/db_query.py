"""DBQueryTool — Agent 只读查询 PostgreSQL（对话中触达结构化数据）。

Agent-native 设计:
1. Agent 在对话/规划中需要查询已入库的素材/故事数据时，调用此 tool
2. 只读，绝不写入；与 database_write 互补（写由 Agent 审核后触发）
3. 基于 StageDBClient 的 query_* / get_* 方法，返回结构化 JSON
4. `_scopes` 含 core —— 主 Agent 对话可见，可直接触发查 DB
"""

from __future__ import annotations

import json
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import ToolContext
from auto_cut_bot.pipeline.state import get_db_client

_OPERATIONS = (
    "book", "subjects", "relationships", "episodes", "subtitles",
    "shots", "scenes", "boundaries", "book_context", "window_context",
    "free_boundaries", "pending_conflicts",
)


@tool_parameters({
    "type": "object",
    "properties": {
        "job_root": {
            "type": "string",
            "description": "Pipeline job root directory (contains config.yaml).",
        },
        "operation": {
            "type": "string",
            "enum": list(_OPERATIONS),
            "description": (
                "Which data to query from PostgreSQL. "
                "book=单本书; subjects=角色; episodes=集; scenes=场景; "
                "subtitles=字幕; shots=镜头; boundaries=边界; "
                "relationships=关系; book_context=全书完整上下文(生成用); "
                "window_context=视频窗口上下文; "
                "pending_conflicts=待人工裁决的多源冲突队列"
            ),
        },
        "book_id": {
            "type": "string",
            "description": "Book ID (e.g. '42000023011'). Required for most operations.",
        },
        "episode_id": {
            "type": "integer",
            "description": "Optional episode filter for episode-scoped queries.",
        },
        "limit": {
            "type": "integer",
            "description": "Optional row limit (default 100, max 500).",
        },
    },
    "required": ["job_root", "operation"],
})
class DBQueryTool(Tool):
    """Agent 只读查询 PostgreSQL 数据库中的流水线产物。"""

    name = "db_query"
    description = (
        "只读查询 PostgreSQL 数据库中已入库的流水线产物（books/subjects/episodes/scenes/"
        "subtitles/shots/boundaries/relationships）。"
        "用于 Agent 在对话或规划中需要查阅某本书的素材、角色、剧情结构、待裁决冲突时。"
        "只读安全，绝不修改数据。常见用法：db_query(operation='book_context', book_id='...')"
        "获取全书结构化上下文；db_query(operation='pending_conflicts', book_id='...')"
        "查看多源冲突队列。"
    )

    _scopes = {"core", "pipeline", "subagent"}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> ToolResult:
        job_root = kwargs["job_root"]
        operation = kwargs["operation"]
        book_id = kwargs.get("book_id")
        episode_id = kwargs.get("episode_id")
        limit = kwargs.get("limit", 100)
        if limit < 1:
            limit = 100
        if limit > 500:
            limit = 500

        db = get_db_client(job_root)
        if db is None:
            return ToolResult.error(
                f"db_query: DB not available (no db_url configured or driver missing) "
                f"for operation={operation}"
            )

        try:
            payload = self._dispatch(db, operation, book_id, episode_id, limit)
            db.close()
            return ToolResult(self._render(operation, payload))
        except Exception as e:  # noqa: BLE001
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
            return ToolResult.error(f"db_query: operation={operation} failed: {e}")

    @staticmethod
    def _render(operation: str, payload: Any) -> str:
        """Render the query payload as a string for the agent.

        Prefer compact JSON; fall back to repr when the payload is not
        JSON-serializable (e.g. contains datetime objects).
        """
        try:
            body = json.dumps(
                {"operation": operation, "data": payload},
                ensure_ascii=False,
                default=str,
            )
        except TypeError:  # pragma: no cover — defensive
            body = f"operation={operation} data={payload!r}"
        return body

    def _dispatch(
        self,
        db: Any,
        operation: str,
        book_id: str | None,
        episode_id: int | None,
        limit: int,
    ) -> Any:
        if not book_id:
            return {"error": "book_id required"}

        if operation == "book":
            return db.query_book(book_id)
        if operation == "subjects":
            return db.query_subjects(book_id)
        if operation == "relationships":
            return db.query_relationships(book_id)
        if operation == "episodes":
            return db.query_episodes(book_id)
        if operation == "scenes":
            return db.query_scenes(book_id, episode_id=episode_id)
        if operation == "subtitles":
            # episode_id required for subtitles
            if episode_id is None:
                return {"error": "episode_id required for subtitles"}
            return db.query_subtitles(book_id, episode_id, limit=limit)
        if operation == "shots":
            if episode_id is None:
                return {"error": "episode_id required for shots"}
            return db.query_shots(book_id, episode_id)
        if operation == "boundaries":
            return db.query_boundaries(book_id, episode_id=episode_id)
        if operation == "book_context":
            return db.get_book_context(book_id)
        if operation == "window_context":
            return db.get_window_context(book_id)
        if operation == "free_boundaries":
            return db.get_free_boundaries(book_id)
        if operation == "pending_conflicts":
            return db.get_pending_conflicts(book_id)
        return {"error": f"unknown operation: {operation}"}


__all__ = ["DBQueryTool"]
