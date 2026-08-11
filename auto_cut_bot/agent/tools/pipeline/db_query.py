"""DBQueryTool — Agent 只读查询 PostgreSQL，支持自主 SQL 和 Schema 发现。

Agent-native 设计:
1. operation="raw" — Agent 写 SELECT，系统保护安全（只读/LIMIT/参数化/超时）
2. operation="schema" — Agent 发现有哪些表/字段可查
3. operation="book"/"scenes"/... — 向后兼容的预设查询
4. 只读，绝不写入
"""

from __future__ import annotations

import json
import re
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.context import ToolContext
from auto_cut_bot.pipeline.state import get_db_client

_OPERATIONS = (
    "raw", "schema",
    "book", "subjects", "relationships", "episodes", "subtitles",
    "shots", "scenes", "boundaries", "book_context", "window_context",
    "free_boundaries", "pending_conflicts",
)

# 只允许 SELECT，禁止任何写入/DDL
_SQL_BLACKLIST = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"COPY|VACUUM|REINDEX|DISCARD|EXECUTE|CALL|DO|SET|BEGIN|COMMIT|ROLLBACK)\b",
    re.IGNORECASE,
)
_MAX_LIMIT = 2000  # Agent 可请求的最大行数
_DEFAULT_LIMIT = 500  # 不指定时的默认值
_MAX_QUERY_SECONDS = 5


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
                "raw=Agent写SELECT自主查询; schema=发现表结构; "
                "book/subjects/relationships/episodes/scenes/subtitles/"
                "shots/boundaries=预设查询; book_context=全书上下文; "
                "window_context=视频窗口; pending_conflicts=冲突队列"
            ),
        },
        "sql": {
            "type": "string",
            "description": (
                "SELECT statement (only for operation='raw'). "
                "Use $1, $2, ... for parameterized values. "
                "LIMIT enforced to 500 max. Only SELECT allowed."
            ),
        },
        "params": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Parameter values for $1, $2, ... in the SQL (only for operation='raw').",
        },
        "book_id": {
            "type": "string",
            "description": "Book ID. Required for most preset operations.",
        },
        "episode_id": {
            "type": "integer",
            "description": "Optional episode filter.",
        },
        "limit": {
            "type": "integer",
            "description": "Row limit (default 100, max 500).",
        },
    },
    "required": ["job_root", "operation"],
})
class DBQueryTool(Tool):
    """Agent 只读查询 PostgreSQL，支持自主 SQL 和 Schema 发现。"""

    name = "db_query"
    description = (
        "只读查询 PostgreSQL 数据库。支持三种模式：\n"
        "1. raw: Agent 自主写 SELECT 查询（参数化，只读，限500行，5秒超时）\n"
        "2. schema: 发现数据库有哪些表、字段、关系\n"
        "3. book/scenes/subjects/...: 预设查询\n"
        "所有操作只读安全，绝不修改数据。"
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
        limit = min(max(kwargs.get("limit", 100), 1), _MAX_LIMIT)

        db = get_db_client(job_root)
        if db is None:
            return ToolResult.error(
                f"db_query: DB not available for operation={operation}"
            )

        try:
            if operation == "raw":
                payload = self._execute_raw(db, kwargs.get("sql", ""), kwargs.get("params", []))
                db.close()
                return ToolResult(self._render_compact(operation, payload))
            elif operation == "schema":
                payload = self._get_schema(db)
            else:
                payload = self._dispatch(db, operation, book_id, episode_id, limit)
            # 预设查询也走列式压缩（统一的 _render 入口）
            db.close()
            return ToolResult(self._render(operation, payload))
        except Exception as e:
            try:
                db.close()
            except Exception:
                pass
            return ToolResult.error(f"db_query: operation={operation} failed: {e}")

    # ── 自主 SQL 查询 ──────────────────────────────────────────────────────

    def _execute_raw(self, db: Any, sql: str, params: list[str]) -> Any:
        """执行 Agent 自主编写的 SELECT 语句，带安全保护。"""
        if not sql or not sql.strip():
            return {"error": "sql parameter required for operation='raw'"}

        # 安全校验 1: 只允许 SELECT
        if _SQL_BLACKLIST.search(sql):
            blocked = _SQL_BLACKLIST.findall(sql)
            return {
                "error": f"SQL contains forbidden keywords: {blocked}. Only SELECT allowed.",
                "hint": "Use operation='schema' to discover available tables and columns.",
            }

        # 安全校验 2: 强制 LIMIT
        if "limit" not in sql.lower():
            sql = f"{sql.rstrip(';')} LIMIT {_MAX_LIMIT}"

        # 安全校验 3: 参数化
        safe_params: list[Any] = []
        for p in params:
            try:
                safe_params.append(int(p))
            except ValueError:
                try:
                    safe_params.append(float(p))
                except ValueError:
                    safe_params.append(str(p))

        # 安全校验 4: 设置超时
        try:
            db._conn.execute(f"SET statement_timeout = '{_MAX_QUERY_SECONDS}s'")
        except Exception:
            pass

        try:
            rows = db._conn.execute(sql, safe_params).fetchall()
            columns = [desc[0] for desc in db._conn.description] if db._conn.description else []
            return {
                "columns": columns,
                "rows": [dict(zip(columns, row)) for row in rows],
                "row_count": len(rows),
            }
        except Exception as e:
            return {
                "error": str(e),
                "hint": "Use operation='schema' to see available tables and columns.",
                "sql": sql,
            }

    # ── Schema 发现 ────────────────────────────────────────────────────────

    def _get_schema(self, db: Any) -> dict[str, Any]:
        """返回数据库 schema，让 Agent 知道有哪些表和字段可查。"""
        try:
            tables_result = db._conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='autocut' ORDER BY table_name"
            ).fetchall()
            tables = [row[0] for row in tables_result]
        except Exception:
            tables = ["scenes", "subjects", "episodes", "books", "subtitles",
                      "shots", "boundaries", "relationships", "subject_episodes",
                      "speaker_mappings", "provenance"]

        schema: dict[str, list[dict[str, str]]] = {}
        for table in tables:
            try:
                cols = db._conn.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema='autocut' AND table_name=$1 "
                    "ORDER BY ordinal_position",
                    [table],
                ).fetchall()
                schema[table] = [{"column": c[0], "type": c[1]} for c in cols]
            except Exception:
                schema[table] = []

        return {
            "tables": schema,
            "limits": {
                "max_rows": _MAX_LIMIT,
                "default_rows": _DEFAULT_LIMIT,
                "timeout_seconds": _MAX_QUERY_SECONDS,
                "pagination": "Use LIMIT and OFFSET for large result sets. Example: LIMIT 500 OFFSET 1500 for page 4.",
            },
            "rules": [
                "Only SELECT allowed. INSERT/UPDATE/DELETE/DROP are blocked.",
                "Use $1, $2, ... for parameterized values. Never concatenate values into SQL.",
                "Arrays use PostgreSQL syntax: characters_present @> ARRAY['Alice']",
                "JSONB fields: data->>'field_name' or data @> '{\"key\":\"value\"}'",
                "LIMIT enforced to max 2000 rows. Use OFFSET for pagination.",
                "Query timeout: 5 seconds.",
            ],
            "relationships": {
                "scenes.book_id → books.id": "scenes belong to books",
                "scenes.episode_id → episodes.id": "scenes belong to episodes",
                "subjects.book_id → books.id": "subjects belong to books",
                "subtitles.episode_id → episodes.id": "subtitles belong to episodes",
                "shots.episode_id → episodes.id": "shots belong to episodes",
                "boundaries.episode_id → episodes.id": "boundaries belong to episodes",
                "relationships.book_id → books.id": "relationships belong to books",
            },
        }

    # ── 预设查询 (向后兼容) ────────────────────────────────────────────────

    def _dispatch(self, db, operation, book_id, episode_id, limit):
        if not book_id:
            return {"error": "book_id required"}
        ops = {
            "book": lambda: db.query_book(book_id),
            "subjects": lambda: db.query_subjects(book_id),
            "relationships": lambda: db.query_relationships(book_id),
            "episodes": lambda: db.query_episodes(book_id),
            "scenes": lambda: db.query_scenes(book_id, episode_id=episode_id),
            "subtitles": lambda: db.query_subtitles(book_id, episode_id or 0, limit=limit),
            "shots": lambda: db.query_shots(book_id, episode_id or 0),
            "boundaries": lambda: db.query_boundaries(book_id, episode_id=episode_id),
            "book_context": lambda: db.get_book_context(book_id),
            "window_context": lambda: db.get_window_context(book_id),
            "free_boundaries": lambda: db.get_free_boundaries(book_id),
            "pending_conflicts": lambda: db.get_pending_conflicts(book_id),
        }
        fn = ops.get(operation)
        if fn is None:
            return {"error": f"unknown operation: {operation}"}
        return fn()

    @staticmethod
    def _render(operation: str, payload: Any) -> str:
        """统一渲染：大结果集自动切换列式压缩，节省 token。

        规则:
        - dict 类型且有 rows/columns → 直接走 _render_compact
        - list[dict] 类型且 >20 项 → 提取 columns 后转列式
        - 其他 → 行式 JSON
        """
        # 已经是 raw 查询的 rows/columns 格式 → 走 compact
        if isinstance(payload, dict) and "rows" in payload:
            return DBQueryTool._render_compact(operation, payload)

        # 预设查询返回的 list[dict] → 大结果集转列式
        if isinstance(payload, list) and len(payload) > 20:
            if all(isinstance(item, dict) for item in payload):
                # 提取所有 key 作为 columns（保持首条顺序）
                columns = list(payload[0].keys()) if payload else []
                compact_rows = [[row.get(c) for c in columns] for row in payload]
                compact = {
                    "columns": columns,
                    "rows": compact_rows,
                    "row_count": len(payload),
                    "format": "columnar",
                    "hint": f"Each row is a list matching columns order. {len(payload)} rows total.",
                }
                return json.dumps({"operation": operation, "data": compact}, ensure_ascii=False, default=str)

        # 其他类型 → 行式 JSON
        try:
            body = json.dumps({"operation": operation, "data": payload}, ensure_ascii=False, default=str)
        except TypeError:
            body = f"operation={operation} data={payload!r}"
        return body

    @staticmethod
    def _render_compact(operation: str, payload: Any) -> str:
        """紧凑模式：列式存储，不重复 key，节省 ~50% token。

        行式 (浪费): [{"scene_id":"S1","loc":"墓地"}, {"scene_id":"S2","loc":"宫殿"}]
        列式 (紧凑): {"columns":["scene_id","loc"], "rows":[["S1","墓地"],["S2","宫殿"]]}
        """
        if not isinstance(payload, dict) or "rows" not in payload:
            return DBQueryTool._render(operation, payload)

        rows = payload["rows"]
        columns = payload.get("columns", [])
        row_count = payload.get("row_count", len(rows))

        # 小结果集直接返回行式
        if row_count <= 20:
            return DBQueryTool._render(operation, payload)

        # 大结果集用列式存储
        compact_rows = []
        for row in rows:
            if isinstance(row, dict):
                compact_rows.append([row.get(c) for c in columns])
            else:
                compact_rows.append(list(row))

        compact = {
            "columns": columns,
            "rows": compact_rows,
            "row_count": row_count,
            "format": "columnar",  # ← 告诉 Agent 这是列式格式
            "hint": f"Each row is a list matching columns order. {row_count} rows total.",
        }
        return json.dumps({"operation": operation, "data": compact}, ensure_ascii=False, default=str)


__all__ = ["DBQueryTool"]