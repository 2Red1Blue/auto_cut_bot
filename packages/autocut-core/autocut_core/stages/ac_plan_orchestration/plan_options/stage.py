"""story_plans Stage — Story Plan 选项生成与语义选择。

输入: span_candidates
输出: story_plans
"""

from __future__ import annotations

import argparse
from pathlib import Path

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.io import load_json, update_project_stage, utc_now
from autocut_core.semantic.prep.plans import prepare_plans
from autocut_core.stages.ports import LLMPort, get_llm_port


class PlanOptionsStage(Stage):
    """Story Plans — 生成 Plan 选项并执行 story_plan_selection 语义选择。"""

    def __init__(self, *args, llm_port: LLMPort | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._llm_port: LLMPort | None = llm_port

    @property
    def llm_port(self) -> LLMPort:
        if self._llm_port is None:
            self._llm_port = get_llm_port()
        return self._llm_port

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="story_plans",
            input_artifacts=["span_candidates"],
            output_artifacts=["story_plans"],
            description="Story Plan 选项生成与语义选择",
            db_reads=[],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="semantic_batch", payload={
            "span_candidates": self.resolve_artifact_path(
                bus, "span_candidates", "span_candidates"
            ),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root

        # 1. 准备 Plan 批次 (semantic/prep/plans.py)
        args = argparse.Namespace(
            job_root=root,
            backend=cfg.backend,
            candidate_arena=False,
            allow_partial=(cfg.mode == "auto"),
            max_context_chars=600000,
        )
        batch_path = prepare_plans(args)

        # 2. 执行语义批次 (jobs=[] 时跳过)
        batch = load_json(batch_path) if batch_path.is_file() else {}
        if batch.get("jobs"):
            self.llm_port.run_batch(
                batch_path,
                backend=cfg.backend,
                workers=cfg.workers,
                requests_per_minute=cfg.requests_per_minute,
                semantic_retries=cfg.semantic_retries,
            )
        else:
            print(f"[{utc_now()}] [story_plans] 当前 Plan 批次 jobs=[]; 语义批次跳过")

        ref = bus.put("story_plans", {"path": str(batch_path)}, stage="story_plans")
        update_project_stage(root / "project.json", "story_plans", "completed",
                             outputs={"story_plans": str(batch_path)})
        return [ref]
