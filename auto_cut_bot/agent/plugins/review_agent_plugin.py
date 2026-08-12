"""独立审核 Agent 插件 — 完全隔离的 LLM 上下文。

与主 Agent 零共享:
- 独立的 system prompt（从 ac_review skill 加载 SOUL + AGENTS + contracts）
- 独立的工具集（只有 db_query，只读）
- 独立的 session（不污染主 Agent 历史）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from state_graph.agent.entities import NodeResult
from state_graph.agent.ports import INodePlugin


@dataclass
class ReviewVerdict:
    status: str = "rejected"
    score: int = 0
    reasons: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "score": self.score, "reasons": self.reasons}


class ReviewAgentPlugin(INodePlugin):
    """独立审核 Agent — 完全隔离的 LLM 上下文。"""

    def __init__(self) -> None:
        self._skill_path = self._resolve_skill_path()

    @staticmethod
    def _resolve_skill_path() -> Path:
        candidates = [
            Path(__file__).resolve().parent.parent.parent / "skills" / "ac_review",
            Path.home() / ".auto_cut_bot" / "skills" / "ac_review",
        ]
        for p in candidates:
            if p.is_dir():
                return p
        import logging
        logging.getLogger(__name__).warning(
            "ac_review skill directory not found at %s or %s, using fallback identity",
            candidates[0], candidates[1],
        )
        return candidates[0]

    async def execute(self, state: dict[str, Any]) -> NodeResult:
        book_id = state.get("book_id", "")
        if not book_id:
            return NodeResult(status="failed", error="review_gate: book_id not found")
        try:
            system_prompt = self._load_review_identity()
            review_prompt = self._build_review_task(book_id)
            verdict = await self._run_review_agent(system_prompt, review_prompt)
            state["_review_verdict"] = verdict.to_dict()
            if verdict.status == "approved":
                return NodeResult(status="completed", output={"review": verdict.to_dict()})
            return NodeResult(status="waiting_human", output={
                "review": verdict.to_dict(),
                "message": f"Review rejected (score: {verdict.score})"
            })
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception("Review agent failed")
            return NodeResult(status="failed", error=f"review_gate: {exc}")

    def can_handle(self, node_type: str) -> bool:
        return node_type == "review_gate"

    def _load_review_identity(self) -> str:
        parts = []
        for name in ["SOUL.md", "AGENTS.md"]:
            p = self._skill_path / name
            if p.is_file():
                parts.append(p.read_text(encoding="utf-8").strip())
        contracts_dir = self._skill_path / "contracts"
        if contracts_dir.is_dir():
            for cf in sorted(contracts_dir.glob("*.md")):
                parts.append(f"\n## {cf.stem}\n")
                parts.append(cf.read_text(encoding="utf-8").strip())
        if not parts:
            return "你是独立审核者。只读 DB 做规则检查。不信任主 Agent 的决策。"
        return "\n\n".join(parts)

    def _build_review_task(self, book_id: str) -> str:
        return (
            f"审核 book_id={book_id} 的故事计划。\n"
            "只能使用 db_query 工具查询数据库。\n"
            "返回 JSON: {\"status\": \"approved|rejected\", \"score\": 0-100, \"reasons\": [...]}"
        )

    async def _run_review_agent(self, system_prompt: str, review_prompt: str) -> ReviewVerdict:
        try:
            from auto_cut_bot.agent.subagent import SubagentManager
            from auto_cut_bot.agent.tools.context import current_request_context
            from auto_cut_bot.agent.tools.base import ToolResult
            from auto_cut_bot.security.workspace_access import current_workspace_scope
            from auto_cut_bot.agents.registry import AgentBuilder

            request_ctx = current_request_context()
            if request_ctx is None or request_ctx.runtime is None:
                return ReviewVerdict(reasons=[{
                    "severity": "critical", "check": "context",
                    "detail": "No active request context"
                }])

            # Use AgentBuilder to get reviewer's identity (SOUL + AGENTS + contracts)
            try:
                instance = AgentBuilder.build("reviewer", has_review_context=True)
                identity = instance.instructions
            except ValueError:
                import logging
                logging.getLogger(__name__).warning(
                    "AgentBuilder.build('reviewer') failed, falling back to skill path identity"
                )
                identity = system_prompt

            full_task = f"{identity}\n\n{review_prompt}"
            manager = SubagentManager()
            result = await manager.run_inline(
                task=full_task, label="review-agent", runtime=request_ctx.runtime,
                origin_channel=request_ctx.channel, origin_chat_id=request_ctx.chat_id,
                session_key=f"{request_ctx.session_key}:review",  # ← 独立 session
                origin_message_id=request_ctx.message_id,
                workspace_scope=current_workspace_scope(),
            )
            if isinstance(result, ToolResult):
                result = str(result)
            return self._parse_verdict(result)
        except ImportError:
            return ReviewVerdict(reasons=[{
                "severity": "critical", "check": "import",
                "detail": "SubagentManager unavailable"
            }])

    def _parse_verdict(self, content: str) -> ReviewVerdict:
        try:
            if "```json" in content:
                start = content.index("```json") + 7
                end = content.index("```", start)
                data = json.loads(content[start:end].strip())
            else:
                data = json.loads(content.strip())
            return ReviewVerdict(
                status=data.get("status", "rejected"),
                score=data.get("score", 0),
                reasons=data.get("reasons", []),
            )
        except (json.JSONDecodeError, ValueError):
            return ReviewVerdict(reasons=[{
                "severity": "critical", "check": "parse",
                "detail": f"Failed to parse: {content[:200]}"
            }])
