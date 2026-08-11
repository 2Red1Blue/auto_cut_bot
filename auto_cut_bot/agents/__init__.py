"""Agent-Native V2 Agent 实体层.

参考 Z3r0 架构: SOUL.md (身份) + AGENTS.md (行为规则) + AgentSpec (能力声明) + AgentBuilder (运行时组装).
"""

from auto_cut_bot.agents.spec import AgentSpec, ToolMount, EDITOR_SPEC, REVIEWER_SPEC
from auto_cut_bot.agents.registry import AgentRegistry, AgentBuilder, AgentInstance

__all__ = [
    "AgentSpec", "ToolMount",
    "EDITOR_SPEC", "REVIEWER_SPEC",
    "AgentRegistry", "AgentBuilder", "AgentInstance",
]
