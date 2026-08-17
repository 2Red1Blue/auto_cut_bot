"""BusStageAdapter — 把 bus-based Stage 包装为编排器统一调度的生命周期接口。

bus-based 接口: __init__(config), prepare(bus)→list[Task],
execute(bus,tasks)→list[Artifact], validate(bus,refs)→bool
编排器接口: prepare(ctx)→TaskPlan, execute(ctx,plan)→StageResult,
validate(ctx,result)→list[ContractViolation]

适配器向编排器暴露统一生命周期接口, 内部调用 bus-based 方法,
保证编排层只需要一套接口。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from autocut_core.contracts.types import (
    ArtifactBus, StageContext, StageResult, TaskPlan,
    ContractViolation, AttemptStatus,
)
from autocut_core.stages._base import Stage, StageContract

if TYPE_CHECKING:
    from autocut_core.config import PipelineConfig


# bus-based 任务结构 — 用于适配 TaskPlan 字典与对象形态
from dataclasses import dataclass as _dc


@_dc
class _Task:
    """内部用任务结构 — 把 TaskPlan 里的字典还原为 (type, payload)
    对象, 仅适配器内部使用。"""
    type: str
    payload: dict[str, Any]


class BusStageAdapter(Stage):
    """包装 bus-based Stage, 暴露编排器统一生命周期接口。

    用法:
        cls = registry.get("source_windows")
        stage = BusStageAdapter(cls, config, bus)
        result = stage.execute(ctx, plan)
    """

    def __init__(self, legacy_cls: type, config: "PipelineConfig", bus: ArtifactBus):
        """实例化被包装的 Stage 并持有 bus 引用 — 所有产物读写
        都通过同一个 bus 完成, 保证产物状态一致。"""
        self._legacy = legacy_cls(config)
        self._bus = bus

    @property
    def contract(self) -> StageContract:
        """透传被包装 Stage 的合同声明。"""
        return self._legacy.contract

    def prepare(self, ctx: StageContext) -> TaskPlan:
        """把 Task 对象列表转为 TaskPlan 字典列表。"""
        tasks = self._legacy.prepare(self._bus)
        return TaskPlan(tasks=[
            {"type": t.type, "payload": t.payload} for t in tasks
        ])

    def execute(self, ctx: StageContext, plan: TaskPlan) -> StageResult:
        """把 plan 还原为 Task 对象执行, 结果包装为 StageResult。

        注意: bus-based 接口无部分失败语义, 此处统一返回 SUCCESS;
        子任务失败由插件内部自行处理 (异常会向上冒泡)。
        """
        old_tasks = [_Task(type=t["type"], payload=t["payload"]) for t in plan.tasks]
        artifacts = self._legacy.execute(self._bus, old_tasks)
        return StageResult(status=AttemptStatus.SUCCESS, artifacts=artifacts)

    def validate(
        self, ctx: StageContext, result: StageResult
    ) -> list[ContractViolation]:
        """把 bool 校验结果转为违规列表 (False 时产生一条
        error 级违规, True 时为空列表)。"""
        ok = self._legacy.validate(self._bus, result.artifacts)
        if not ok:
            return [ContractViolation(
                rule_id="stage_validation",
                code="validation_failed",
                message=f"Stage {self.contract.stage_name} validation failed",
            )]
        return []
