"""story_plans_preflight Stage — Plan 选项范围扩展节点 (HUMAN NODE B)。

Interactive 模式: 编排器在此暂停, 逐条审批交互, 审批决策保存为
结构化产物后继续推进后续 Stage。
Auto 模式: 由 orchestrator/auto.py 的 auto_process_plan_selection +
auto_process_scope_expansion 自动决策。
"""

from __future__ import annotations

from pathlib import Path

from autocut_core import ArtifactBus, Artifact, Stage, StageContract, Task
from autocut_core.io import atomic_write_json, load_json, update_project_stage, utc_now
from autocut_core.logging import get_logger

logger = get_logger(__name__)


class PlansPreflightStage(Stage):
    """Plan 选项范围扩展 — HUMAN NODE B。

    输入: story_plans (PlanOptionsStage 产出)
    输出: story_plans_preflight
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="story_plans_preflight",
            input_artifacts=["story_plans"],
            output_artifacts=["story_plans_preflight"],
            is_human_node=True,
            description="Plan 选项范围扩展 (HUMAN NODE B — Interactive 模式逐条审批交互)",
            db_reads=[],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="decision", payload={
            "plan_batch": self.resolve_artifact_path(bus, "story_plans", "story_plans"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """Interactive 模式: 读取审批决策, 过滤 Plan 条目后产出。

        Auto 模式: 由 orchestrator/auto.py 自动决策, 不经过本 Stage。
        """
        root: Path = self.config.job_root  # type: ignore
        plan_batch = Path(tasks[0].payload["plan_batch"])

        # 尝试读取交互式审批决策
        decision_artifact = bus.resolve(
            "story_plans_preflight", "story_plans_preflight_approval"
        )
        if decision_artifact is not None:
            decision_data = bus.get(decision_artifact)
            return self._apply_interactive_decisions(
                root, bus, plan_batch, decision_data
            )

        # 无审批决策 — 原样通过
        ref = bus.put("story_plans_preflight",
                       {"path": str(plan_batch)},
                       stage="story_plans_preflight")
        update_project_stage(root / "project.json", "story_plans_preflight", "completed")
        return [ref]

    def _apply_interactive_decisions(
        self,
        root: Path,
        bus: ArtifactBus,
        plan_batch: Path,
        decision_data: dict,
    ) -> list[Artifact]:
        """根据交互式审批决策过滤 Plan 批次, 产出过滤后的结果。"""
        batch = load_json(plan_batch) if plan_batch.is_file() else {}

        decisions = decision_data.get("decisions", [])
        accepted_ids = {
            d["item_id"] for d in decisions if d.get("decision") == "accepted"
        }

        # 过滤 plans: 仅保留 accepted 的条目
        plans = batch.get("plans", []) or batch.get("stories", [])
        filtered_plans = [
            p for p in plans
            if isinstance(p, dict)
            and (p.get("story_id") or p.get("id") or p.get("name")) in accepted_ids
        ]

        filtered = dict(batch)
        if "plans" in batch:
            filtered["plans"] = filtered_plans
        elif "stories" in batch:
            filtered["stories"] = filtered_plans
        filtered["approved_at"] = decision_data.get("approved_at", utc_now())
        filtered["approved_by"] = "interactive"
        filtered["decisions"] = decisions

        # 落盘过滤后的产物
        output_path = root / "story-plans-preflight.json"
        atomic_write_json(output_path, filtered)

        ref = bus.put(
            "story_plans_preflight",
            {"path": str(output_path)},
            stage="story_plans_preflight",
        )
        update_project_stage(
            root / "project.json", "story_plans_preflight", "completed"
        )
        return [ref]
