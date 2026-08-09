"""story_treatments Stage — 为入选故事编译 Treatment 讲法方案。

输入: story_catalog, story_portfolio, series_bible, highlight_hook_catalog
输出: story_treatments
"""

from __future__ import annotations

from pathlib import Path

from autocut_core import ArtifactBus, Artifact, Stage, StageContract, Task
from autocut_core.io import atomic_write_json, update_project_stage
import sys
from pathlib import Path
_SCRIPTS = Path(__file__).resolve().parents[4] / "_legacy_v4" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from compile_story_treatments import compile_from_paths as compile_treatments_from_paths


class TreatmentsStage(Stage):
    """纯本地 Treatment 讲法编译。

    基于 Portfolio 入选故事, 编译顺叙/冷开场等讲法策略供后续选择。
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(stage_name="story_treatments",
            input_artifacts=["story_catalog", "story_portfolio", "series_bible", "event_cards"],
            output_artifacts=["story_treatments"],
            description="三种讲法策略编译 (顺叙/冷开场)",
            db_reads=["relationships"],
            db_writes=[])

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="local", payload={
            "catalog": self.resolve_artifact_path(bus, "story_catalog", "story_catalog"),
            "portfolio": self.resolve_artifact_path(bus, "story_portfolio", "story_portfolio"),
            "bible": self.resolve_artifact_path(bus, "series_bible", "series_bible"),
            "candidate": self.resolve_artifact_path(bus, "event_cards", "highlight_hook_catalog"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        root: Path = self.config.job_root  # type: ignore
        p = tasks[0].payload
        output = root / "story-treatment-options.json"
        payload = compile_treatments_from_paths(
            story_catalog_path=Path(p["catalog"]),
            story_portfolio_path=Path(p["portfolio"]),
            series_bible_path=Path(p["bible"]),
            candidate_catalog_path=Path(p["candidate"]),
        )
        atomic_write_json(output, payload)
        ref = bus.put("story_treatments", {"path": str(output)}, stage="story_treatments")
        update_project_stage(root / "project.json", "story_treatments", "completed")
        return [ref]