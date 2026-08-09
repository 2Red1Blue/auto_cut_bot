"""Shot/Clip 级局部返工 — 指定目标 Stage 与片段, 仅重跑受影响的下游环节。

职责:
  - ReworkManifest: 返工请求的数据模型 (目标 Stage, 片段 ID 列表, 原因, 时间戳)
  - ReworkResolver: 给定返工清单与流水线顺序, 解析出需要重执行的 Stage 列表
  - ReworkHistory: 返工历史持久化 (.sd-cache/rework/), 支持追溯

设计原则:
  - 返工从目标 Stage 开始重跑, 下游 Stage 因输入哈希变化自然缓存失效
  - 历史版本产物 (ArtifactBus 内容寻址) 不会被删除或覆盖, 仅追加
  - 返工历史记录为不可变 JSON, 每次返工生成独立记录文件
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from autocut_core.io import atomic_write_json, load_json, utc_now
from autocut_core.logging import get_logger

logger = get_logger(__name__)

# 返工历史存储目录 (相对于 job_root/.sd-cache/)
_REWORK_DIR = "rework"


# ═══════════════════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════════════════


class ReworkManifest(BaseModel):
    """返工请求 — 指定目标 Stage 与片段, 触发局部重跑。

    字段:
        target_stage: 需重跑的起始 Stage (流水线从该 Stage 开始重新执行)
        target_clip_ids: 需重做的片段 ID 列表 (供 render recipe 单 clip 替换使用)
        rework_reason: 人工可读的返工原因
        timestamp: 返工触发时间 (ISO-8601)
        rework_id: 唯一标识 (UUID4), 用于历史追溯
    """

    model_config = ConfigDict(frozen=True)

    target_stage: str
    target_clip_ids: list[str] = Field(default_factory=list)
    rework_reason: str = ""
    timestamp: str = Field(default_factory=utc_now)
    rework_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    def to_file_dict(self) -> dict[str, Any]:
        """导出为可序列化的字典, 用于落盘。"""
        return {
            "rework_id": self.rework_id,
            "target_stage": self.target_stage,
            "target_clip_ids": self.target_clip_ids,
            "rework_reason": self.rework_reason,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_file_dict(cls, data: dict[str, Any]) -> ReworkManifest:
        """从字典反序列化, 容错缺失字段。"""
        return cls(
            rework_id=data.get("rework_id", str(uuid.uuid4())),
            target_stage=data.get("target_stage", ""),
            target_clip_ids=data.get("target_clip_ids", []),
            rework_reason=data.get("rework_reason", ""),
            timestamp=data.get("timestamp", utc_now()),
        )


# ═══════════════════════════════════════════════════════════════════════════
# 返工解析器
# ═══════════════════════════════════════════════════════════════════════════


class ReworkResolver:
    """返工解析器 — 给定返工清单与流水线顺序, 解析出需要重执行的 Stage 列表。

    策略:
      从 target_stage 开始到流水线末尾的所有 Stage 都需要重执行。
      因为目标 Stage 的产物变化会改变下游 Stage 的输入哈希,
      导致缓存自然失效。上游 Stage (target_stage 之前) 的产物
      未被修改, 缓存命中, 无需重跑。
    """

    def resolve(
        self,
        manifest: ReworkManifest,
        pipeline_order: list[str],
    ) -> list[str]:
        """解析需要重执行的 Stage 列表。

        参数:
            manifest: 返工请求
            pipeline_order: 流水线的完整 Stage 顺序

        返回:
            从 target_stage 到末尾的 Stage 名称列表
        """
        target = manifest.target_stage
        if target not in pipeline_order:
            raise ValueError(
                f"返工目标 Stage '{target}' 不在流水线顺序中: {pipeline_order}"
            )
        idx = pipeline_order.index(target)
        return pipeline_order[idx:]

    def upstream_stages(
        self,
        manifest: ReworkManifest,
        pipeline_order: list[str],
    ) -> list[str]:
        """返回 target_stage 之前的上游 Stage (这些 Stage 可复用缓存)。

        参数:
            manifest: 返工请求
            pipeline_order: 流水线的完整 Stage 顺序

        返回:
            target_stage 之前的 Stage 名称列表
        """
        target = manifest.target_stage
        if target not in pipeline_order:
            raise ValueError(
                f"返工目标 Stage '{target}' 不在流水线顺序中: {pipeline_order}"
            )
        idx = pipeline_order.index(target)
        return pipeline_order[:idx]


# ═══════════════════════════════════════════════════════════════════════════
# 返工历史持久化
# ═══════════════════════════════════════════════════════════════════════════


class ReworkHistory:
    """返工历史记录 — 持久化到 .sd-cache/rework/, 支持追溯。

    每次返工生成独立的记录文件, 索引文件维护所有返工记录的汇总。
    历史版本产物 (ArtifactBus 内容寻址) 不会被删除或覆盖。
    """

    def __init__(self, job_root: Path) -> None:
        self._root = job_root.expanduser().resolve() / ".sd-cache" / _REWORK_DIR
        self._root.mkdir(parents=True, exist_ok=True)

    def record(self, manifest: ReworkManifest) -> Path:
        """记录一次返工 — 写入独立记录文件并更新索引。

        返回记录文件的路径。
        """
        record_path = self._root / f"{manifest.rework_id}.json"
        atomic_write_json(record_path, manifest.to_file_dict())
        self._update_index(manifest)
        logger.info(
            "返工记录已保存: rework_id=%s target=%s clips=%d",
            manifest.rework_id,
            manifest.target_stage,
            len(manifest.target_clip_ids),
        )
        return record_path

    def list_records(self) -> list[ReworkManifest]:
        """列出所有返工记录, 按时间戳降序排列。"""
        index_path = self._root / "index.json"
        if not index_path.is_file():
            return []
        try:
            data = load_json(index_path)
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        records: list[ReworkManifest] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            try:
                records.append(ReworkManifest.from_file_dict(entry))
            except (TypeError, ValueError):
                continue
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records

    def latest(self) -> ReworkManifest | None:
        """返回最近一次返工记录, 无记录时返回 None。"""
        records = self.list_records()
        return records[0] if records else None

    def _update_index(self, manifest: ReworkManifest) -> None:
        """更新索引文件 — 追加新记录到列表头部。"""
        index_path = self._root / "index.json"
        existing: list[dict[str, Any]] = []
        if index_path.is_file():
            try:
                data = load_json(index_path)
                if isinstance(data, list):
                    existing = data
            except (OSError, ValueError):
                pass
        existing.insert(0, manifest.to_file_dict())
        atomic_write_json(index_path, existing)


# ═══════════════════════════════════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════════════════════════════════


def load_manifest(path: Path) -> ReworkManifest:
    """从 JSON 文件加载返工清单。

    文件格式:
        {
            "target_stage": "story_scripts",
            "target_clip_ids": ["clip-001", "clip-002"],
            "rework_reason": "shot 3 对白不满意"
        }
    缺少的字段使用默认值。
    """
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"返工清单文件不存在: {resolved}")
    data = load_json(resolved)
    if not isinstance(data, dict):
        raise ValueError(f"返工清单文件内容不是 JSON 对象: {resolved}")
    return ReworkManifest.from_file_dict(data)


def create_manifest(
    target_stage: str,
    *,
    target_clip_ids: list[str] | None = None,
    rework_reason: str = "",
) -> ReworkManifest:
    """创建返工清单的便捷工厂函数。

    参数:
        target_stage: 需重跑的起始 Stage 名称
        target_clip_ids: 需重做的片段 ID 列表
        rework_reason: 返工原因说明
    """
    return ReworkManifest(
        target_stage=target_stage,
        target_clip_ids=target_clip_ids or [],
        rework_reason=rework_reason,
    )


__all__ = [
    "ReworkManifest",
    "ReworkResolver",
    "ReworkHistory",
    "create_manifest",
    "load_manifest",
]
