"""AgentRegistry — 发现、加载、组装 Agent 实体。

参考 Z3r0 的 registry.py:_build():
  1. 读取 SOUL.md + AGENTS.md
  2. 按环境过滤工具
  3. 组装 instructions
  4. 生成委派工具（如果有 subagents）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from auto_cut_bot.agents.spec import AgentSpec, EDITOR_SPEC, REVIEWER_SPEC


@dataclass
class AgentInstance:
    """组装好的 Agent 实例。"""
    spec: AgentSpec
    instructions: str
    tools: list[str]  # 工具名称列表
    model: str | None = None


class AgentRegistry:
    """Agent 注册表 — 管理所有 AgentSpec。"""

    _instance: "AgentRegistry | None" = None

    def __init__(self) -> None:
        self._specs: dict[str, AgentSpec] = {}
        self._register_defaults()

    @classmethod
    def instance(cls) -> "AgentRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _register_defaults(self) -> None:
        self.register(EDITOR_SPEC)
        self.register(REVIEWER_SPEC)

    def register(self, spec: AgentSpec) -> None:
        self._specs[spec.code] = spec

    def get(self, code: str) -> AgentSpec | None:
        return self._specs.get(code)

    def list_all(self) -> list[str]:
        return list(self._specs.keys())

    def get_subagents(self, code: str) -> list[AgentSpec]:
        """获取某个 Agent 可委派的子 Agent 列表。"""
        spec = self._specs.get(code)
        if not spec:
            return []
        return [self._specs[s] for s in spec.subagents if s in self._specs]


class AgentBuilder:
    """运行时组装 Agent 实例。

    参考 Z3r0 的 _build():
    - 读取 SOUL + RULES
    - 按环境过滤工具
    - 拼接 instructions
    - 如果有 subagents，生成委派工具
    """

    @staticmethod
    def build(
        code: str,
        *,
        has_pipeline_context: bool = False,
        has_review_context: bool = False,
    ) -> AgentInstance:
        """组装一个 Agent 实例。

        Args:
            code: Agent code ("editor" | "reviewer")
            has_pipeline_context: 是否有 pipeline 上下文
            has_review_context: 是否有审核数据上下文
        """
        registry = AgentRegistry.instance()
        spec = registry.get(code)
        if spec is None:
            raise ValueError(f"Unknown agent: {code}")

        # 1. 按环境过滤工具
        tools = AgentBuilder._filter_tools(spec, has_pipeline_context, has_review_context)

        # 2. 拼接 instructions
        instructions = AgentBuilder._build_instructions(spec, has_pipeline_context)

        # 3. 如果有 subagents，生成委派工具
        if spec.subagents:
            delegation_tools = AgentBuilder._build_delegation_tools(spec, registry)
            tools.extend(delegation_tools)

        return AgentInstance(
            spec=spec,
            instructions=instructions,
            tools=tools,
            model=spec.model_override,
        )

    @staticmethod
    def _filter_tools(
        spec: AgentSpec,
        has_pipeline: bool,
        has_review: bool,
    ) -> list[str]:
        """按环境条件过滤工具。"""
        tools: list[str] = []
        for mount in spec.tools:
            if mount.requires_pipeline and not has_pipeline:
                continue
            if mount.read_only and not has_review:
                continue
            tools.append(mount.tool_name)
        return tools

    @staticmethod
    def _build_instructions(
        spec: AgentSpec,
        has_pipeline: bool,
    ) -> str:
        """拼接完整的 instructions。

        参考 Z3r0 的 build_instructions():
        soul + rules + 条件注入
        """
        parts = [
            spec.soul.strip(),
            "",
            spec.rules.strip(),
        ]

        if has_pipeline:
            parts.append(
                "\n## Pipeline Context\n"
                "You have access to pipeline tools (source_script_*, window_analysis, etc). "
                "Run stages in the order described in the Skill files."
            )

        if spec.subagents:
            sub_names = ", ".join(spec.subagents)
            parts.append(
                f"\n## Delegation\n"
                f"You can delegate to: {sub_names}.\n"
                f"Use start_subagent_task() to assign work, "
                f"read_subagent_task() to check results."
            )

        return "\n\n".join(parts)

    @staticmethod
    def _build_delegation_tools(
        spec: AgentSpec,
        registry: AgentRegistry,
    ) -> list[str]:
        """生成委派工具 — 主 Agent 可以委派给子 Agent。"""
        tools: list[str] = []
        for sub_code in spec.subagents:
            sub_spec = registry.get(sub_code)
            if sub_spec:
                tools.append(f"start_subagent_task_{sub_code}")
                tools.append(f"read_subagent_task_{sub_code}")
        return tools
