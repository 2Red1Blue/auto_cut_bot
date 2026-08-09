"""story_scripts Stage — 基于入选故事与讲法方案, 用 LLM 生成 Story Script。

流水线位置: 故事生成段第 4 步。三步执行:
  1. prepare_story_stages scripts — 生成语义批次清单
  2. run_semantic_batch — 并发执行批次中的 LLM 任务
  3. assemble_story_artifacts scripts — 汇总为 story-scripts/index.json

输入: story_catalog, story_portfolio, story_treatments, series_bible,
      event_cards, highlight_hook_catalog
输出: story_scripts
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from autocut_core import ArtifactBus, Artifact, Stage, StageContract, Task
from autocut_core.io import update_project_stage
from autocut_core.semantic.batch_orchestrator import run_batch
from autocut_core.semantic.prep.scripts import prepare_scripts


class ScriptsStage(Stage):
    """LLM 生成 Story Script。

    以 Portfolio 入选故事 + Treatment 讲法为输入, 逐故事生成脚本。
    输入: story_catalog, story_portfolio, story_treatments, series_bible, event_cards
    输出: story_scripts
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(stage_name="story_scripts",
            input_artifacts=["story_portfolio", "story_treatments", "series_bible", "event_cards"],
            output_artifacts=["story_scripts"],
            description="LLM 生成 Story Script",
            db_reads=["subjects"],
            db_writes=[])

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """解析六个上游产物路径, 生成单个 semantic_batch 任务。"""
        return [Task(type="semantic_batch", payload={
            "catalog": self.resolve_artifact_path(bus, "story_catalog", "story_catalog"),
            "portfolio": self.resolve_artifact_path(bus, "story_portfolio", "story_portfolio"),
            "treatments": self.resolve_artifact_path(bus, "story_treatments", "story_treatments"),
            "bible": self.resolve_artifact_path(bus, "series_bible", "series_bible"),
            "event_cards": self.resolve_artifact_path(bus, "event_cards", "event_cards"),
            "candidate": self.resolve_artifact_path(bus, "event_cards", "highlight_hook_catalog"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """依次执行 准备批次 → 并发跑批次 → 汇总结果。"""
        cfg = self.config
        root: Path = cfg.job_root  # type: ignore
        p = tasks[0].payload

        # 1. 准备批次
        ns = argparse.Namespace()
        ns.job_root = root
        ns.backend = cfg.backend
        ns.max_context_chars = 600000
        ns.story_catalog = Path(p["catalog"])
        ns.series_bible = Path(p["bible"])
        ns.candidate_catalog = Path(p["candidate"])
        ns.story_portfolio = Path(p["portfolio"])
        ns.event_cards = Path(p["event_cards"])
        ns.story_treatment_options = Path(p["treatments"]) if p.get("treatments") else None
        ns.story_portfolio_replenishment = None
        ns.target_story_id = None
        ns.lock_treatment_option = None
        batch_path = prepare_scripts(ns)

        # 2. LLM 推理
        run_batch(
            batch_path,
            backend=cfg.backend,
            workers=cfg.workers,
            requests_per_minute=cfg.requests_per_minute,
            semantic_retries=cfg.semantic_retries,
        )

        # 3. 汇总
        from autocut_core.libs.script_preflight import assemble_scripts_index
        output_dir = root / "story-scripts"
        output_dir.mkdir(parents=True, exist_ok=True)
        assemble_scripts_index(
            load_json(batch_path),
            output_dir / "index.json",
        )

        ref = bus.put("story_scripts",
              {"path": str(output_dir / "index.json")},
              stage="story_scripts")
        update_project_stage(root / "project.json", "story_scripts", "completed")
        return [ref]