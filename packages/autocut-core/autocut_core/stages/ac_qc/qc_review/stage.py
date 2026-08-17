"""story_qc_review Stage — QC 结果评审节点 (HUMAN NODE)。

将 QC 报告的 approved/review/blocked 状态分类为可渲染子集与丢弃子集。
Interactive 模式: 编排器在此节点前暂停, 人工评审 story-qc/index.json
后重跑继续。
Auto 模式: 直接 import pipeline_auto.auto_process_qc_report。
输入: story_qc
输出: story_qc_review
"""

from __future__ import annotations

from pathlib import Path

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.io import load_json, update_project_stage, utc_now
# 运行时导入，避免模块级依赖
def _get_auto_process_qc_report():
    try:
        from ac_auto_cut.orchestrator.auto import auto_process_qc_report
        return auto_process_qc_report
    except ImportError:
        return None


class QCReviewStage(Stage):
    """QC Review — 人工评审节点 (Interactive 模式由编排器暂停)。

    输入: story_qc (StoryQCStage 产出)
    输出: story_qc_review (story-qc/index.json 评审结论)
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="story_qc_review",
            input_artifacts=["story_qc"],
            output_artifacts=["story_qc_review"],
            is_human_node=True,
            description="QC 评审 (HUMAN NODE — Interactive 模式在此暂停)",
            db_reads=[],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="decision", payload={
            "qc_index": self.resolve_artifact_path(bus, "story_qc", "story_qc"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        qc_index = root / "story-qc" / "index.json"
        if not qc_index.is_file():
            raise RuntimeError(f"QC 索引缺失: {qc_index}")

        if cfg.mode == "auto":
            # Auto 模式: 直接 import auto_process_qc_report
            result = auto_process_qc_report(root)
            print(f"[{utc_now()}] [story_qc_review] auto 决策完成 — "
                  f"render: {len(result['render_story_ids'])} stories, "
                  f"dropped: {len(result['dropped_story_ids'])} stories")
        else:
            # Interactive 模式: 编排器已在此节点前暂停, 人工确认后续跑
            index = load_json(qc_index)
            statuses = [
                f"{rep.get('story_id')}={rep.get('status')}"
                for rep in index.get("reports", [])
                if isinstance(rep, dict)
            ]
            print(f"[{utc_now()}] [story_qc_review] 人工评审已确认 — "
                  f"QC 状态: {', '.join(statuses) or '(无报告)'}")

        ref = bus.put("story_qc_review", {"path": str(qc_index)},
                      stage="story_qc_review")
        update_project_stage(root / "project.json", "story_qc_review", "completed",
                             outputs={"story_qc_review": str(qc_index)})
        return [ref]