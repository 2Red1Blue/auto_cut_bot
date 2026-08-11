"""AgentSpec — 声明式 Agent 定义，参考 Z3r0 架构。

每个 Agent 由 4 层组成:
  SOUL.md   → 身份（我是谁）
  AGENTS.md → 行为规则（我怎么做事）
  AgentSpec → 能力声明（我能做什么 + 可委派谁）
  AgentConfig → 模型配置（我用什么模型）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolMount:
    """声明一个 Agent 可用的工具。

    不同于全局工具列表，ToolMount 可以带门控条件:
    - requires_pipeline: 需要 pipeline 上下文（job_root 等）
    - read_only: 只读工具，审核 Agent 用
    """
    tool_name: str
    read_only: bool = False
    requires_pipeline: bool = False
    description: str = ""


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Agent 的完整声明。

    不可变 — 所有 Agent 共享同一个 Spec 实例。
    """
    code: str                                    # "editor", "reviewer"
    name: str                                     # "剪辑编排 Agent"
    soul: str                                     # SOUL.md 内容（身份）
    rules: str                                    # AGENTS.md 内容（行为规则）
    tools: tuple[ToolMount, ...] = ()             # 按角色声明的工具
    subagents: tuple[str, ...] = ()               # 可委派的 Agent code 列表
    model_override: str | None = None             # 独立模型（None=继承主 Agent）


# ── 预定义 Agent Specs ──────────────────────────────────────────────────────


def _load_text(path: str) -> str:
    p = Path(__file__).parent / path
    return p.read_text(encoding="utf-8") if p.is_file() else ""


EDITOR_SPEC = AgentSpec(
    code="editor",
    name="剪辑编排 Agent",
    soul=_load_text("editor/SOUL.md"),
    rules=_load_text("editor/AGENTS.md"),
    tools=(
        ToolMount("db_query", description="自主 SQL 查询数据库"),
        ToolMount("database_write", description="写入数据库"),
        ToolMount("source_script_load", requires_pipeline=True, description="加载剧本"),
        ToolMount("source_script_save", requires_pipeline=True, description="保存解析结果"),
        ToolMount("source_script_chunk_parse", requires_pipeline=True, description="分块解析"),
        ToolMount("source_windows", requires_pipeline=True, description="视频切窗"),
        ToolMount("source_metadata", requires_pipeline=True, description="API 元数据"),
        ToolMount("window_analysis", requires_pipeline=True, description="VLM 窗口分析"),
        ToolMount("asr_transcript", requires_pipeline=True, description="ASR 转录"),
        ToolMount("event_cards", requires_pipeline=True, description="事件卡片"),
        ToolMount("episode_digests", requires_pipeline=True, description="集摘要"),
        ToolMount("chapter_digests", requires_pipeline=True, description="章摘要"),
        ToolMount("series_registry", requires_pipeline=True, description="剧集注册"),
        ToolMount("series_assignment", requires_pipeline=True, description="剧集分配"),
        ToolMount("series_bible", requires_pipeline=True, description="剧集圣经"),
        ToolMount("story_catalog", requires_pipeline=True, description="故事目录"),
        ToolMount("story_portfolio", requires_pipeline=True, description="故事组合"),
        ToolMount("story_treatments", requires_pipeline=True, description="故事处理"),
        ToolMount("story_scripts", requires_pipeline=True, description="故事脚本"),
        ToolMount("story_preflight", requires_pipeline=True, description="预检"),
        ToolMount("story_approval", requires_pipeline=True, description="审批"),
        ToolMount("story_evidence", requires_pipeline=True, description="证据"),
        ToolMount("span_candidates", requires_pipeline=True, description="候选片段"),
        ToolMount("story_plans", requires_pipeline=True, description="故事计划"),
        ToolMount("story_plans_materialize", requires_pipeline=True, description="计划实现"),
        ToolMount("story_qc", requires_pipeline=True, description="质量检查"),
        ToolMount("story_qc_review", requires_pipeline=True, description="质量审核"),
        ToolMount("story_render", requires_pipeline=True, description="渲染"),
        ToolMount("pipeline_orchestrator", requires_pipeline=True, description="流水线编排"),
    ),
    subagents=("reviewer",),  # 可以委派审核 Agent
)


REVIEWER_SPEC = AgentSpec(
    code="reviewer",
    name="独立审核 Agent",
    soul=_load_text("reviewer/SOUL.md"),
    rules=_load_text("reviewer/AGENTS.md"),
    tools=(
        ToolMount("db_query", read_only=True, description="只读数据库查询"),
    ),
    subagents=(),  # 不能委派别人
    model_override=None,  # 可以用更便宜的模型
)
