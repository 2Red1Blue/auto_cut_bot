"""autocut_core — Story-first 流水线运行时核心。

包拓扑（修正后）:
  autocut_core/    ← 唯一共享运行时 (类型、总线、编排器、合同)
  plugins/*/           ← Stage 插件 (可选安装, entry_point 注册)
  skills/*/            ← Agent 指令 + 参考文档 (纯 .md, 不含 Python 代码)

入口: autocut CLI (注册在 pyproject.toml [project.scripts])
"""

from autocut_core.config import PipelineConfig
from autocut_core.errors import (
    EXIT_CONTRACT_VIOLATION,
    EXIT_FAILURE,
    EXIT_OK,
    ArtifactNotFoundError,
    AutoDecisionError,
    ConfigError,
    ContractViolationError,
    PipelineError,
    StageExecutionError,
    StageNotFoundError,
)
from autocut_core.io import (
    atomic_write_json, atomic_write_jsonl, atomic_write_text,
    canonical_json, file_lock, is_number, json_sha256, load_json, load_jsonl,
    normalize_text, record_pipeline_failure, require_array,
    require_object, sha256_file, stable_id, unwrap_analysis,
    update_project_stage, utc_now,
)
from autocut_core.contracts.types import (
    Artifact, ArtifactBus, Attempt, AttemptStatus, Checkpoint,
    ContractViolation, StageContext, StageResult, StageStatus,
    TaskPlan,
)
# Stage 基类: bus-based 接口 — 全部插件继承此类,
# 编排器通过 BusStageAdapter 包装后以统一生命周期调度。
from autocut_core.stages._base import Stage, StageContract, Task
from autocut_core.stages.adapter import BusStageAdapter
from autocut_core.registry import HUMAN_NODES, StageRegistry
from autocut_core.interactive import InteractiveApproval
from autocut_core.cache import CacheManager
from autocut_core.logging import configure_logging, get_logger

__all__ = [
    # config
    "PipelineConfig",
    # errors — 统一异常体系与退出码约定
    "PipelineError", "ConfigError", "StageNotFoundError",
    "StageExecutionError", "ContractViolationError",
    "ArtifactNotFoundError", "AutoDecisionError",
    "EXIT_OK", "EXIT_FAILURE", "EXIT_CONTRACT_VIOLATION",
    # logging — 统一结构化日志入口
    "configure_logging", "get_logger",
    # io
    "load_json", "load_jsonl", "atomic_write_json", "atomic_write_jsonl",
    "atomic_write_text", "sha256_file", "json_sha256", "stable_id",
    "canonical_json", "normalize_text", "is_number",
    "require_object", "require_array", "unwrap_analysis",
    "record_pipeline_failure",
    "update_project_stage", "utc_now", "file_lock",
    # cache
    "CacheManager",
    # types
    "Artifact", "ArtifactBus", "StageContext", "StageResult",
    "ContractViolation", "Attempt", "AttemptStatus", "Checkpoint", "StageStatus",
    "TaskPlan",
    # stages — Stage = bus-based 基类 (_base.Stage)
    "Stage", "StageContract", "Task", "BusStageAdapter",
    # registry
    "StageRegistry", "HUMAN_NODES",
    # interactive
    "InteractiveApproval",
]
