"""story_catalog Stage — 从 Series Bible 与事件卡片中发现独立故事子弧。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autocut_core import ArtifactBus, Artifact, Stage, StageContract, Task
from autocut_core.io import (
    atomic_write_json, load_json, sha256_file, update_project_stage,
)
from autocut_core.semantic.batch_orchestrator import run_batch
from autocut_core.semantic.prep.catalog import prepare_catalog

_BROAD = "broad"
_CATALOG_TASK = "story_catalog"


class CatalogStage(Stage):
    """Broad Story Catalog 发现子故事。

    输入: series_bible, event_cards, highlight_hook_catalog
    输出: story_catalog
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(stage_name="story_catalog",
            input_artifacts=["series_bible", "event_cards"],
            output_artifacts=["story_catalog"],
            description="发现独立故事子弧",
            db_reads=["books"],
            db_writes=[])

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="semantic_batch", payload={
            "bible": self.resolve_artifact_path(bus, "series_bible", "series_bible"),
            "event_cards": self.resolve_artifact_path(bus, "event_cards", "event_cards"),
            "catalog": self.resolve_artifact_path(bus, "event_cards", "highlight_hook_catalog"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        cfg = self.config
        root: Path = cfg.job_root  # type: ignore
        p = tasks[0].payload

        # 1. 准备批次
        ns = argparse.Namespace()
        ns.job_root = root
        ns.backend = cfg.backend
        ns.max_context_chars = 600000
        ns.series_bible = Path(p["bible"])
        ns.event_cards = Path(p["event_cards"])
        ns.candidate_catalog = Path(p["catalog"])
        batch_path = prepare_catalog(ns)

        # 2. LLM 推理
        run_batch(
            batch_path,
            backend=cfg.backend,
            workers=cfg.workers,
            requests_per_minute=cfg.requests_per_minute,
            semantic_retries=cfg.semantic_retries,
            fail_fast=True,
        )

        # 3. 内联组装 (不再调用 assemble_broad_story_catalog.py)
        option_path = root / "story-subarc-options.json"
        output_path = root / "story-catalog.json"
        _assemble_catalog(batch_path, option_path, output_path)

        ref = bus.put("story_catalog", {"path": str(output_path)}, stage="story_catalog")
        update_project_stage(root / "project.json", "story_catalog", "completed")
        return [ref]


def _assemble_catalog(
    manifest_path: Path, option_catalog_path: Path, output_path: Path
) -> dict[str, Any]:
    """从语义批处理 manifest 组装 Broad Story Catalog。

    行为与 assemble_broad_story_catalog.py 一致。
    TODO(Full): 内联 story_granularity.validate_broad_catalog —
    当前依赖 story_granularity 模块。
    """
    manifest = load_json(manifest_path)
    option_catalog = load_json(option_catalog_path)
    option_catalog_sha = sha256_file(option_catalog_path)

    if option_catalog.get("story_granularity") != _BROAD:
        raise ValueError("option catalog is not story_granularity=broad")

    expected_ids = list(option_catalog.get("recommended_option_ids", []) or [])
    if not expected_ids:
        raise ValueError("Broad option catalog has no recommended_option_ids")

    story_by_option: dict[str, dict[str, Any]] = {}
    for job in manifest.get("jobs", []) or []:
        if not isinstance(job, dict) or job.get("task") != _CATALOG_TASK:
            continue
        option_id = job.get("subarc_option_id")
        if option_id not in expected_ids:
            raise ValueError(f"manifest contains unexpected Broad option job: {option_id!r}")
        output_value = job.get("output")
        if not isinstance(output_value, str):
            raise ValueError(f"{job.get('id')}: output path is missing")
        shard_path = Path(output_value).expanduser().resolve()
        if not shard_path.is_file():
            raise FileNotFoundError(f"Broad Catalog shard is missing: {shard_path}")
        shard = load_json(shard_path)
        stories = shard.get("stories", [])
        if len(stories) != 1:
            raise ValueError(f"{shard_path.name} must contain exactly one Story")
        story = stories[0]
        if story.get("subarc_option_id") != option_id:
            raise ValueError(
                f"{shard_path.name} option mismatch: expected={option_id}, "
                f"actual={story.get('subarc_option_id')}"
            )
        if option_id in story_by_option:
            raise ValueError(f"duplicate Broad Catalog shard for {option_id}")
        story_by_option[option_id] = story

    missing = [oid for oid in expected_ids if oid not in story_by_option]
    extra = sorted(set(story_by_option) - set(expected_ids))
    if missing or extra:
        raise ValueError(f"Broad Catalog shard set mismatch: missing={missing}, extra={extra}")

    catalog = {
        "schema_version": "1.2",
        "story_granularity": _BROAD,
        "subarc_option_catalog_sha256": option_catalog_sha,
        "stories": [story_by_option[oid] for oid in expected_ids],
    }

    story_ids = [
        s.get("story_id") for s in catalog["stories"] if isinstance(s, dict)
    ]
    if len(set(story_ids)) != len(story_ids):
        raise ValueError("Broad Catalog contains duplicate story_id")

    atomic_write_json(output_path, catalog)
    return catalog