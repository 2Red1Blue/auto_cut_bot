"""orchestrator/ — 流水线调度层。

包含:
  - pipeline.py: autocut CLI 入口与 PipelineOrchestrator 编排器
    (按 _PIPELINE_ORDER 调度 Stage, 维护 project.json 检查点);
  - auto.py: 四个人工节点 (story_approval / story_plans_preflight /
    story_plans_qc_admission / story_qc_review) 在 auto 模式下的
    自动决策函数;
  - recovery.py: 自动恢复回路 — 被拒绝的 Story 回溯到上游 Stage
    重新生成 (RecoveryPlan / RecoveryRecord / RecoveryResolver /
    RecoveryLogger).
"""

from __future__ import annotations

from autocut_core.orchestrator.recovery import (
    DEFAULT_QC_ROUTE,
    GLOBAL_MAX_ATTEMPTS,
    QC_FINDING_ROUTES,
    QC_MAX_ATTEMPTS,
    RecoveryLogger,
    RecoveryPlan,
    RecoveryRecord,
    RecoveryResolver,
    build_recovery_error_context,
    recovery_stage_range,
)

__all__ = [
    "RecoveryPlan",
    "RecoveryRecord",
    "RecoveryResolver",
    "RecoveryLogger",
    "QC_FINDING_ROUTES",
    "DEFAULT_QC_ROUTE",
    "GLOBAL_MAX_ATTEMPTS",
    "QC_MAX_ATTEMPTS",
    "build_recovery_error_context",
    "recovery_stage_range",
]
