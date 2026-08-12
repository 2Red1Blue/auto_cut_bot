"""ChapterDigestsStage — 逐章语义摘要 (每 ~6 集合一)。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.io import (
    atomic_write_jsonl, collect_digest_records, load_json, update_project_stage,
)
from autocut_core.semantic.batch_orchestrator import run_batch
from autocut_core.semantic.prep.chapters import prepare_chapters


_CHAPTER_DIGEST_TASK = "chapter_digest"


class ChapterDigestsStage(Stage):
    """逐章语义摘要生成 (每 ~6 集合一)。

    输入: episode_digests (EpisodeDigestsStage 产出)
    输出: chapter_digests
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="chapter_digests",
            input_artifacts=["episode_digests"],
            output_artifacts=["chapter_digests"],
            description="逐章语义摘要 (合并多集剧情)",
            db_reads=[],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="semantic_batch", payload={
            "episode_digests": self.resolve_artifact_path(
                bus, "episode_digests", "episode_digests"
            ),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        p = tasks[0].payload

        # 1. 准备批次 (直接 import semantic/prep, 不再通过 wrapper)
        args = argparse.Namespace()
        args.job_root = root
        args.backend = cfg.backend
        args.episode_digests = Path(p["episode_digests"])
        args.episodes_per_chapter = cfg.episodes_per_chapter
        args.max_context_chars = 600000
        batch_path = prepare_chapters(args)

        # 2. LLM 推理
        run_batch(
            batch_path,
            backend=cfg.backend,
            workers=cfg.workers,
            requests_per_minute=cfg.requests_per_minute,
            semantic_retries=cfg.semantic_retries,
        )

        # 3. 内联组装 (不再调用 assemble_story_artifacts.py chapters)
        manifest = load_json(batch_path)
        records = collect_digest_records(manifest, _CHAPTER_DIGEST_TASK)
        output = root / "chapter-digests.jsonl"
        atomic_write_jsonl(output, records)

        ref = bus.put("chapter_digests", {"path": str(output)}, stage="chapter_digests")
        update_project_stage(root / "project.json", "chapter_digests", "completed",
                             outputs={"chapter_digests": str(output)})
        return [ref]
