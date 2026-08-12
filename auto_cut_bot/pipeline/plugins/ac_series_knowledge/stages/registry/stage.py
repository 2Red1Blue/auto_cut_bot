"""RegistryStage — Series Registry 准入与修复链。

执行链 (严格顺序依赖, 在语义批次内部依次应用):
    admission → alias_repair → identity_repair →
    reference_repair → relationship_repair → recovery

准入判定新实体能否进入剧集知识库; 修复链逐步消除别名冲突、
身份歧义、引用错误与关系错误; recovery 回收隔离区实体。

输入: chapter_digests (+ episode_digests, event_cards)
输出: series_registry
"""

from __future__ import annotations

import argparse
from pathlib import Path

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.io import update_project_stage
from autocut_core.semantic.batch_orchestrator import run_batch
from autocut_core.semantic.prep.registry_prep import prepare_registry


class RegistryStage(Stage):
    """Series Registry 准入 + 五段修复链。

    输入: chapter_digests (上游 ChapterDigestsStage 产出)
    输出: series_registry (series-registry.json,
          附带 admission / quarantine 产物路径)
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="series_registry",
            input_artifacts=["chapter_digests", "episode_digests", "event_cards"],
            output_artifacts=["series_registry"],
            description="Series Registry 准入与修复链 (admission → 四段 repair → recovery)",
            db_reads=["subject_episodes"],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """解析三个上游产物路径 — 章节摘要为主输入,
        集摘要与事件卡为准入/修复链的 CLI 必需参数。"""
        return [Task(type="semantic_batch", payload={
            "episode_digests": self.resolve_artifact_path(
                bus, "episode_digests", "episode_digests"
            ),
            "chapter_digests": self.resolve_artifact_path(
                bus, "chapter_digests", "chapter_digests"
            ),
            "event_cards": self.resolve_artifact_path(bus, "event_cards", "event_cards"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """两步执行: 生成 registry 批次 → 批次内按序应用准入与修复链。"""
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        p = tasks[0].payload

        # 1. 生成 registry 语义批次 (直接 import semantic/prep, 不再通过 wrapper)
        args = argparse.Namespace()
        args.job_root = root
        args.backend = cfg.backend
        args.episode_digests = Path(p["episode_digests"])
        args.chapter_digests = Path(p["chapter_digests"])
        args.event_cards = Path(p["event_cards"])
        args.max_context_chars = 600000
        batch_path = prepare_registry(args)

        # 2. 语义批次执行 (内部按序应用 admission → repair 链 → recovery)
        run_batch(
            batch_path,
            backend=cfg.backend,
            workers=cfg.workers,
            requests_per_minute=cfg.requests_per_minute,
            semantic_retries=cfg.semantic_retries,
        )

        registry_path = root / "series-registry.json"
        ref = bus.put("series_registry", {
            "path": str(registry_path),
            "registry_admission": str(root / "series-registry-admission.json"),
            "registry_quarantine": str(root / "series-registry-quarantine.json"),
        }, stage="series_registry")
        update_project_stage(root / "project.json", "series_registry", "completed",
                             outputs={"series_registry": str(registry_path)})
        return [ref]
