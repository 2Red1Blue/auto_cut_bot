"""story_approval Stage — 人工/Auto 审批节点 (HUMAN NODE)。

流水线位置: 故事生成段第 6 步 (故事生成段与计划编排段的分界)。
Interactive 模式下编排器在此暂停, 逐条审批交互, 决策保存为
结构化产物后继续推进; Auto 模式下由 auto 决策函数自动决策。
输入: story_preflight
输出: story_approval
"""

from __future__ import annotations

from pathlib import Path

from autocut_core import ArtifactBus, Artifact, Stage, StageContract, Task
from autocut_core.io import atomic_write_json, load_json, update_project_stage, utc_now
from autocut_core.logging import get_logger

logger = get_logger(__name__)


class ApprovalStage(Stage):
    """人工审批节点 — Interactive 模式在此暂停。

    输入: story_preflight
    输出: story_approval
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(stage_name="story_approval",
            input_artifacts=["story_preflight"],
            output_artifacts=["story_approval"],
            is_human_node=True,
            description="人工审批 (Interactive 模式逐条审批交互)",
            db_reads=[],
            db_writes=[])

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """解析预检产物路径, 生成单个 decision 任务。"""
        return [Task(type="decision", payload={
            "preflight": self.resolve_artifact_path(bus, "story_preflight", "story_preflight"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """读取审批决策并产出过滤后的审批产物。

        优先读取 ArtifactBus 中的 story_approval_approval 决策产物;
        若不存在则回退到 story-approval.json 文件审批方式。
        """
        root: Path = self.config.job_root  # type: ignore

        # 尝试读取交互式审批决策
        decision_artifact = bus.resolve("story_approval", "story_approval_approval")
        if decision_artifact is not None:
            decision_data = bus.get(decision_artifact)
            return self._apply_interactive_decisions(
                root, bus, tasks, decision_data
            )

        # 回退到 CLI 文件审批
        approval_path = root / "story-approval.json"
        if not approval_path.exists():
            raise RuntimeError(
                "审批尚未完成 — 请在 Interactive 模式下手动审批后继续"
            )
        ref = bus.put(
            "story_approval", {"path": str(approval_path)}, stage="story_approval"
        )
        update_project_stage(
            root / "project.json", "story_approval", "completed"
        )
        return [ref]

    def _apply_interactive_decisions(
        self,
        root: Path,
        bus: ArtifactBus,
        tasks: list[Task],
        decision_data: dict,
    ) -> list[Artifact]:
        """根据交互式审批决策过滤预检产物, 产出过滤后的审批结果。"""
        preflight_path = Path(tasks[0].payload["preflight"])
        preflight = load_json(preflight_path) if preflight_path.is_file() else {}

        decisions = decision_data.get("decisions", [])
        accepted_ids = {
            d["item_id"] for d in decisions if d.get("decision") == "accepted"
        }

        # 过滤 stories: 仅保留 accepted 的条目
        stories = preflight.get("stories", [])
        filtered_stories = [
            s for s in stories
            if isinstance(s, dict)
            and (s.get("story_id") or s.get("id")) in accepted_ids
        ]

        filtered = dict(preflight)
        filtered["stories"] = filtered_stories
        filtered["approved_at"] = decision_data.get("approved_at", utc_now())
        filtered["approved_by"] = "interactive"
        filtered["decisions"] = decisions

        # 落盘过滤后的审批产物
        approval_path = root / "story-approval.json"
        atomic_write_json(approval_path, filtered)

        ref = bus.put(
            "story_approval",
            {"path": str(approval_path)},
            stage="story_approval",
        )
        update_project_stage(
            root / "project.json", "story_approval", "completed"
        )
        return [ref]
