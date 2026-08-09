"""story_evidence Stage — 为已批准故事构建 Story Evidence Packet (证据包)。

输入: story_scripts (经 story_approval 审批后的脚本集)
输出: story_evidence (story-evidence/index.json)
"""

from __future__ import annotations

import sys
from pathlib import Path

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.contracts.evidence_validation import validate as validate_story_evidence
from autocut_core.io import update_project_stage


class EvidenceStage(Stage):
    """Story Evidence — 为已批准故事构建证据包。"""

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="story_evidence",
            input_artifacts=["story_scripts", "story_approval"],
            output_artifacts=["story_evidence"],
            description="构建 Story Evidence Packet",
            db_reads=[],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="assemble", payload={
            "story_approval": self.resolve_artifact_path(bus, "story_approval", "story_approval"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        p = tasks[0].payload

        from build_story_evidence_packet import main as _evidence_main
        saved_argv = sys.argv[:]
        sys.argv = [
            "build_story_evidence_packet.py", str(p["story_approval"]),
            "--project", str(root / "project.json"),
        ]
        try:
            exit_code = _evidence_main()
            if exit_code not in (0, 2):
                raise RuntimeError(
                    f"build_story_evidence_packet.main() 返回 exit code {exit_code}"
                )
        finally:
            sys.argv = saved_argv

        validate_story_evidence(root)

        index_path = root / "story-evidence" / "index.json"
        ref = bus.put("story_evidence", {"path": str(index_path)}, stage="story_evidence")
        update_project_stage(root / "project.json", "story_evidence", "completed",
                             outputs={"story_evidence": str(index_path)})
        return [ref]