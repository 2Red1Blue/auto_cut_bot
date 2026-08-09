"""bus-based Stage 基类 — 全部插件使用的正式接口。

接口: __init__(config), prepare(bus)→list[Task],
execute(bus,tasks)→list[Artifact], validate(bus,refs)→bool

Stage 通过 ArtifactBus 读写产物; 编排器用 BusStageAdapter 将
本接口包装为统一生命周期接口后调度。
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from autocut_core.errors import ArtifactNotFoundError, StageExecutionError
from autocut_core.logging import fields, get_logger
from autocut_core.telemetry import get_tracer

if TYPE_CHECKING:
    from autocut_core.config import PipelineConfig
    from autocut_core.contracts.types import Artifact, ArtifactBus

logger = get_logger(__name__)


@dataclass(frozen=True)
class StageContract:
    """Stage 输入/输出/人工节点合同声明。

    stage_name 必须与 registry._PIPELINE_ORDER 中的规范名一致 —
    ArtifactBus 的产物键与 bus.latest() 解析都依赖它。
    """
    stage_name: str
    input_artifacts: list[str] = field(default_factory=list)
    output_artifacts: list[str] = field(default_factory=list)
    is_human_node: bool = False
    description: str = ""
    db_reads: list[str] = field(default_factory=list)
    db_writes: list[str] = field(default_factory=list)


@dataclass
class Task:
    """prepare() 产出的单个子任务描述。

    type: 任务类型 (如 window_analysis); payload: 任务参数字典
    (与语义批处理 manifest 的 job 结构同构)。
    """
    type: str
    payload: dict[str, Any]


class Stage(ABC):
    """bus-based Stage 基类。

    全部插件继承此类; 编排器通过 BusStageAdapter 包装后
    以 ctx-based 接口调度。生命周期: prepare(bus)→任务列表 →
    execute(bus, tasks)→产物列表 → validate(bus, refs)→bool。
    """

    def __init__(self, config: "PipelineConfig") -> None:
        self.config = config

    @property
    @abstractmethod
    def contract(self) -> StageContract:
        """声明本 Stage 的输入/输出产物与人工节点属性。"""
        ...

    @abstractmethod
    def prepare(self, bus: "ArtifactBus") -> list[Task]:
        """从 bus 读取上游产物, 规划本次要执行的子任务列表。"""
        t = get_tracer()
        if t is not None:
            t.trace(f"{type(self).__module__}.{type(self).__qualname__}.prepare")
        ...

    @abstractmethod
    def execute(self, bus: "ArtifactBus", tasks: list[Task]) -> list["Artifact"]:
        """执行子任务并把新产物发布到 bus, 返回产物列表。"""
        t = get_tracer()
        if t is not None:
            t.trace(f"{type(self).__module__}.{type(self).__qualname__}.execute")
        ...

    def validate(self, bus: "ArtifactBus", refs: list["Artifact"]) -> bool:
        """执行后合同校验 — 默认通过, 子类可覆盖做业务校验。"""
        t = get_tracer()
        if t is not None:
            t.trace(f"{type(self).__module__}.{type(self).__qualname__}.validate")
        return True

    # ── 通用工具方法 — 委托旧脚本执行的插件共用 ──────────────────

    def run_legacy_script(
        self,
        cmd: list[str],
        root: Path,
        *,
        label: str | None = None,
        accepted: tuple[int, ...] = (0,),
    ) -> None:
        """在 job 根目录下执行子进程命令, 记录带时间戳的结构化日志。

        日志 tag 默认取 contract.stage_name, 可用 label 覆盖;
        退出码不在 accepted 集合内时抛 StageExecutionError。
        """
        tag = label if label is not None else self.contract.stage_name
        logger.info(
            "执行子进程: %s", " ".join(cmd), extra=fields(stage=tag)
        )
        completed = subprocess.run(cmd, cwd=str(root), check=False)
        if completed.returncode not in accepted:
            raise StageExecutionError(
                f"{tag} 失败 (exit {completed.returncode})"
            )

    def resolve_artifact_path(
        self, bus: "ArtifactBus", stage: str, name: str
    ) -> str:
        """从 ArtifactBus 解析上游产物的磁盘路径。

        优先取该 stage 最新产物, 否则按名称解析; 产物内容为含 "path" 键的
        字典时返回其中的路径, 否则返回产物自身路径。
        找不到产物时抛 ArtifactNotFoundError。
        """
        ref = bus.latest(stage) or bus.resolve(stage, name)
        if ref is None:
            raise ArtifactNotFoundError(f"产物 {stage}/{name} 未找到")
        data = bus.get(ref)
        if isinstance(data, dict) and "path" in data:
            return data["path"]
        return str(ref.path)
