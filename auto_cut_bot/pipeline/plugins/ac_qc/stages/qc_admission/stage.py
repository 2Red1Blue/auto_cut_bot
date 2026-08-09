"""story_plans_qc_admission Stage — QC 准入审批节点 (HUMAN NODE C)。

Interactive 模式: 编排器在此暂停, 逐条审批交互, 审批决策保存为
结构化产物后继续推进后续 Stage。
Auto 模式: 由 orchestrator/auto.py 的 auto_process_plan_selection 自动决策。
"""

from __future__ import annotations

from pathlib import Path

from autocut_core import ArtifactBus, Artifact, Stage, StageContract, Task
from autocut_core.io import atomic_write_json, load_json, update_project_stage, utc_now
from autocut_core.logging import get_logger

logger = get_logger(__name__)


class QCAdmissionStage(Stage):
    """QC 准入审批 — HUMAN NODE C。

    输入: story_plans_materialized (MaterializeStage 产出)
    输出: story_plans_qc_admission
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="story_plans_qc_admission",
            input_artifacts=["story_plans_materialized"],
            output_artifacts=["story_plans_qc_admission"],
            is_human_node=True,
            description="QC 准入审批 (HUMAN NODE C — Interactive 模式逐条审批交互)",
            db_reads=[],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="decision", payload={
            "plans_index": self.resolve_artifact_path(
                bus, "story_plans_materialize", "story_plans_materialized"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """Interactive 模式: 读取审批决策, 过滤 Plan 条目后产出。

        Auto 模式: 由 orchestrator/auto.py 自动决策, 不经过本 Stage。
        """
        root: Path = self.config.job_root  # type: ignore
        plans_index = Path(tasks[0].payload["plans_index"])

        # 尝试读取交互式审批决策
        decision_artifact = bus.resolve(
            "story_plans_qc_admission", "story_plans_qc_admission_approval"
        )
        if decision_artifact is not None:
            decision_data = bus.get(decision_artifact)
            return self._apply_interactive_decisions(
                root, bus, plans_index, decision_data
            )

        # 无审批决策 — 原样通过
        ref = bus.put("story_plans_qc_admission",
                       {"path": str(plans_index)},
                       stage="story_plans_qc_admission")
        update_project_stage(root / "project.json", "story_plans_qc_admission", "completed")
        return [ref]

    def _apply_interactive_decisions(
        self,
        root: Path,
        bus: ArtifactBus,
        plans_index: Path,
        decision_data: dict,
    ) -> list[Artifact]:
        """根据交互式审批决策过滤 Plan 索引, 产出过滤后的结果。"""
        index_data = load_json(plans_index) if plans_index.is_file() else {}

        decisions = decision_data.get("decisions", [])
        accepted_ids = {
            d["item_id"] for d in decisions if d.get("decision") == "accepted"
        }

        # 过滤 plans: 仅保留 accepted 的条目
        plans = index_data.get("plans", []) or index_data.get("stories", [])
        filtered_plans = [
            p for p in plans
            if isinstance(p, dict)
            and (p.get("story_id") or p.get("id") or p.get("name")) in accepted_ids
        ]

        filtered = dict(index_data)
        if "plans" in index_data:
            filtered["plans"] = filtered_plans
        elif "stories" in index_data:
            filtered["stories"] = filtered_plans
        filtered["approved_at"] = decision_data.get("approved_at", utc_now())
        filtered["approved_by"] = "interactive"
        filtered["decisions"] = decisions

        # 落盘过滤后的产物
        output_path = root / "story-plans-qc-admission.json"
        atomic_write_json(output_path, filtered)

        ref = bus.put(
            "story_plans_qc_admission",
            {"path": str(output_path)},
            stage="story_plans_qc_admission",
        )
        update_project_stage(
            root / "project.json", "story_plans_qc_admission", "completed"
        )
        return [ref]
