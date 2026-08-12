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
    name="可莉 (Klee) — 剪辑编排 Agent",
    soul=_load_text("editor/SOUL.md"),
    rules=_load_text("editor/AGENTS.md"),
    tools=(
        # ── 辅助工具 ──
        ToolMount("db_query", description="自主 SQL 查询数据库"),
        ToolMount("database_write", description="写入数据库"),

        # ── Phase 1: Source Preparation (VLM-First) ──
        ToolMount("source_windows", requires_pipeline=True, description="视频切窗 + 480p CRF32 压缩"),
        ToolMount("global_context", requires_pipeline=True, description="从 API/剧本提取全剧级上下文"),
        ToolMount("source_transcripts", requires_pipeline=True, description="ASR 转录（条件触发）"),
        ToolMount("window_analysis", requires_pipeline=True, description="VLM 逐窗语义分析（主要信息源）"),
        ToolMount("confidence_check", requires_pipeline=True, description="VLM 输出质量门控，按需触发 ASR"),
        ToolMount("event_cards", requires_pipeline=True, description="从 VLM visual_events 跨窗口聚合事件卡"),
        ToolMount("episode_digests", requires_pipeline=True, description="单集摘要"),
        ToolMount("chapter_digests", requires_pipeline=True, description="章节摘要"),
        ToolMount("series_registry", requires_pipeline=True, description="全剧注册表（角色统一、关系网、故事线）"),
        ToolMount("series_assignment", requires_pipeline=True, description="章节分配"),

        # ── Phase 2: Story Generation ──
        ToolMount("series_bible", requires_pipeline=True, description="全剧圣经"),
        ToolMount("story_catalog", requires_pipeline=True, description="故事目录"),
        ToolMount("story_portfolio", requires_pipeline=True, description="故事组合"),
        ToolMount("story_treatments", requires_pipeline=True, description="故事大纲"),
        ToolMount("story_scripts", requires_pipeline=True, description="故事脚本"),
        ToolMount("story_preflight", requires_pipeline=True, description="素材可行性预检"),
        ToolMount("story_approval", requires_pipeline=True, description="人工审批 [HITL gate]"),
        ToolMount("story_evidence", requires_pipeline=True, description="证据收集"),
        ToolMount("span_candidates", requires_pipeline=True, description="候选片段"),
        ToolMount("story_plans_preflight", requires_pipeline=True, description="计划预检 [HITL gate]"),
        ToolMount("story_plans", requires_pipeline=True, description="剪辑计划"),
        ToolMount("story_plans_materialize", requires_pipeline=True, description="计划物化"),
        ToolMount("story_plans_qc_admission", requires_pipeline=True, description="QC 准入 [HITL gate]"),

        # ── Phase 3: Production ──
        ToolMount("story_qc", requires_pipeline=True, description="质量检测"),
        ToolMount("story_qc_review", requires_pipeline=True, description="QC 审核 [HITL gate]"),
        ToolMount("story_render", requires_pipeline=True, description="渲染输出"),

        # ── Domain Agents（粗粒度，一次调用跑一个 Phase）──
        ToolMount("source_agent", requires_pipeline=True, description="素材准备：一键执行 source_windows → series_assignment"),
        ToolMount("story_agent", requires_pipeline=True, description="故事生成：一键执行 series_bible → story_plans_qc_admission"),
        ToolMount("production_agent", requires_pipeline=True, description="生产：一键执行 story_qc → story_render"),
    ),
    subagents=("reviewer",),  # 可以委派审核 Agent
)


REVIEWER_SPEC = AgentSpec(
    code="reviewer",
    name="琴 (Jean) — 独立审核 Agent",
    soul=_load_text("reviewer/SOUL.md"),
    rules=_load_text("reviewer/AGENTS.md"),
    tools=(
        ToolMount("db_query", read_only=True, description="只读数据库查询"),
    ),
    subagents=(),  # 不能委派别人
    model_override=None,  # 可以用更便宜的模型
)
