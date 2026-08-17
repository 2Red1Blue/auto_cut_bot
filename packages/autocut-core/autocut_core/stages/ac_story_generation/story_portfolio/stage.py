"""story_portfolio Stage — 对故事目录做 Primary/Reserve 分槽与去重。

流水线位置: 故事生成段第 2 步。纯本地计算 (无 LLM 调用)。
输入: story_catalog, series_bible
输出: story_portfolio
"""

from __future__ import annotations

from pathlib import Path

from autocut_core import ArtifactBus, Artifact, Stage, StageContract, Task
from autocut_core.io import atomic_write_json, load_json, update_project_stage
from autocut_core.libs.build_story_portfolio import build_portfolio


class PortfolioStage(Stage):
    """纯本地 Portfolio 分槽 + 去重。

    从故事目录中筛选入选故事并划分 Primary (主用) / Reserve (备用) 两个槽位,
    同时去除与 Series Bible 重复的内容。
    输入: story_catalog, series_bible
    输出: story_portfolio
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(stage_name="story_portfolio",
            input_artifacts=["story_catalog", "series_bible"],
            output_artifacts=["story_portfolio"],
            description="Primary/Reserve 分槽",
            db_reads=[],
            db_writes=[])

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="local", payload={
            "catalog": self.resolve_artifact_path(bus, "story_catalog", "story_catalog"),
            "bible": self.resolve_artifact_path(bus, "series_bible", "series_bible"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        root: Path = self.config.job_root  # type: ignore
        p = tasks[0].payload
        catalog_path = Path(p["catalog"])
        bible_path = Path(p["bible"])
        output = root / "story-portfolio.json"
        portfolio = build_portfolio(load_json(catalog_path), load_json(bible_path))
        atomic_write_json(output, portfolio)
        ref = bus.put("story_portfolio", {"path": str(output)}, stage="story_portfolio")
        update_project_stage(root / "project.json", "story_portfolio", "completed")
        return [ref]