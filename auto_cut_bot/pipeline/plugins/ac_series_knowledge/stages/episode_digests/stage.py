"""EpisodeDigestsStage — 逐集语义摘要生成。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.io import (
    atomic_write_jsonl, load_json, update_project_stage,
)
from autocut_core.semantic.batch_orchestrator import run_batch
from autocut_core.semantic.prep.episodes import prepare_episodes

_EPISODE_DIGEST_TASK = "episode_digest"


class EpisodeDigestsStage(Stage):
    """逐集语义摘要生成。

    输入: source_manifest, window_manifest, window_summaries, event_cards, highlight_hook_catalog
    输出: episode_digests
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="episode_digests",
            input_artifacts=["source_windows", "window_analysis", "event_cards"],
            output_artifacts=["episode_digests"],
            description="逐集语义摘要 (LLM 归纳每集剧情)",
            db_reads=[],
            db_writes=["episodes", "books", "scenes"],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        root = self.config.job_root
        if root is None:
            raise RuntimeError("job_root 未设置")
        return [Task(type="semantic_batch", payload={
            "source_manifest": self.resolve_artifact_path(bus, "source_windows", "source_manifest"),
            "window_manifest": self.resolve_artifact_path(bus, "source_windows", "window_manifest"),
            "window_summaries": self.resolve_artifact_path(bus, "window_analysis", "window_summaries"),
            "event_cards": self.resolve_artifact_path(bus, "event_cards", "event_cards"),
            "catalog": self.resolve_artifact_path(bus, "event_cards", "highlight_hook_catalog"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        cfg = self.config
        root: Path = cfg.job_root  # type: ignore[assignment]
        p = tasks[0].payload

        # 1. 准备批次 (直接 import semantic/prep, 不再通过 wrapper)
        args = argparse.Namespace()
        args.job_root = root
        args.backend = cfg.backend
        args.source_manifest = Path(p["source_manifest"])
        args.window_manifest = Path(p["window_manifest"])
        args.window_summaries = Path(p["window_summaries"])
        args.event_cards = Path(p["event_cards"])
        args.candidate_catalog = Path(p["catalog"])
        args.max_context_chars = 600000
        batch_path = prepare_episodes(args)

        # 2. LLM 推理
        run_batch(
            batch_path,
            backend=cfg.backend,
            workers=cfg.workers,
            requests_per_minute=cfg.requests_per_minute,
            semantic_retries=cfg.semantic_retries,
        )

        # 3. 内联组装 (不再调用 assemble_story_artifacts.py episodes)
        manifest = load_json(batch_path)
        records = _collect_digest_records(manifest, _EPISODE_DIGEST_TASK)
        output = root / "episode-digests.jsonl"
        atomic_write_jsonl(output, records)

        ref = bus.put("episode_digests", {"path": str(output)}, stage="episode_digests")
        update_project_stage(root / "project.json", "episode_digests", "completed",
                             outputs={"episode_digests": str(output)})
        return [ref]


def _collect_digest_records(manifest: dict[str, Any], task: str) -> list[dict[str, Any]]:
    """从语义批处理 manifest 收集指定 task 的输出记录。

    行为与 assemble_story_artifacts.py collect() 一致。
    """
    records: list[dict[str, Any]] = []
    for job in manifest.get("jobs", []):
        if not isinstance(job, dict) or job.get("task") != task:
            continue
        output = job.get("output")
        if not isinstance(output, str):
            raise ValueError(f"{task} job 缺少 output 字段: {job.get('id', '?')}")
        path = Path(output).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"{task} 产物缺失: {path}")
        value = load_json(path)
        records.append(value)

    if not records:
        raise ValueError(f"manifest 中无已完成的 {task} 输出")

    if task == "episode_digest":
        records.sort(key=lambda item: item["episode"])
    elif task == "chapter_digest":
        records.sort(key=lambda item: item["episodes"][0])
    return records