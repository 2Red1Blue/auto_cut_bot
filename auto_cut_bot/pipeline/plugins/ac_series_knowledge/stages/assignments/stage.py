"""AssignmentStage — Series Assignment 合约化分配。

把每一集分配到具体 Series: 语义批次内部调用
canonicalize_series_assignment 按合约规则归一化分配结果。

输入: series_registry (+ episode_digests, chapter_digests, event_cards)
输出: series_assignment (series-assignment-batch.json,
      下游 BibleStage 直接消费该批次清单)
"""

from __future__ import annotations

from pathlib import Path

import argparse

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.io import update_project_stage
from autocut_core.semantic.batch_orchestrator import run_batch
from autocut_core.semantic.prep.assignments import prepare_assignments


class AssignmentStage(Stage):
    """Series Assignment — 剧集到 Series 的合约化分配。

    输入: series_registry (RegistryStage 产出)
    输出: series_assignment (series-assignment-batch.json,
          下游 BibleStage 直接消费该批次清单)
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="series_assignment",
            input_artifacts=[
                "series_registry", "episode_digests",
                "chapter_digests", "event_cards",
            ],
            output_artifacts=["series_assignment"],
            description="Series Assignment 合约化分配",
            db_reads=[],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """解析 registry 主产物及 admission/quarantine 附属产物路径,
        连同三个摘要/事件产物一起组装为批次输入。"""
        ref = bus.latest("series_registry") or bus.resolve("series_registry", "series_registry")
        if ref is None:
            raise RuntimeError("产物 series_registry/series_registry 未找到")
        data = bus.get(ref)
        payload: dict[str, str] = {
            "series_registry": (
                data["path"] if isinstance(data, dict) and "path" in data else str(ref.path)
            ),
            "registry_admission": (
                data.get("registry_admission", "") if isinstance(data, dict) else ""
            ),
            "registry_quarantine": (
                data.get("registry_quarantine", "") if isinstance(data, dict) else ""
            ),
            "episode_digests": self.resolve_artifact_path(
                bus, "episode_digests", "episode_digests"
            ),
            "chapter_digests": self.resolve_artifact_path(
                bus, "chapter_digests", "chapter_digests"
            ),
            "event_cards": self.resolve_artifact_path(bus, "event_cards", "event_cards"),
        }
        return [Task(type="semantic_batch", payload=payload)]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """两步执行: 生成 assignment 批次 → 批次内应用分配合约。"""
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        p = tasks[0].payload

        # 1. 生成 assignment 语义批次
        ns = argparse.Namespace()
        ns.job_root = root
        ns.backend = cfg.backend
        ns.max_context_chars = 600000
        ns.series_registry = Path(p["series_registry"])
        ns.episode_digests = Path(p["episode_digests"])
        ns.chapter_digests = Path(p["chapter_digests"])
        ns.event_cards = Path(p["event_cards"])
        ns.registry_admission = Path(p["registry_admission"]) if p.get("registry_admission") else None
        ns.registry_quarantine = Path(p["registry_quarantine"]) if p.get("registry_quarantine") else None
        batch_path = prepare_assignments(ns)

        # 2. 语义批次执行 (内部应用分配合约归一化)
        run_batch(
            batch_path,
            backend=cfg.backend,
            workers=cfg.workers,
            requests_per_minute=cfg.requests_per_minute,
            semantic_retries=cfg.semantic_retries,
        )

        ref = bus.put("series_assignment", {"path": str(batch_path)},
                      stage="series_assignment")
        update_project_stage(root / "project.json", "series_assignment", "completed",
                             outputs={"series_assignment": str(batch_path)})
        return [ref]