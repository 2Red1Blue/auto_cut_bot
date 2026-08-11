"""StageOrchestrator — shared stage execution logic for domain agents.

Extracts the duplicate pattern in domain agents where they:
1. Create a PipelineConfig for each stage
2. Create an ArtifactBus
3. Instantiate the stage class
4. Call stage.prepare(bus)
5. Call stage.execute(bus, tasks)
6. Collect results and handle errors

Design principles:
- SRP: StageOrchestrator only orchestrates stages, does not know about domains.
- DIP: Depends on Stage interface (prepare/execute), not on concrete stage classes.
- No duplication: the 3 domain agents share this one orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Type

from autocut_core import Artifact, ArtifactBus, PipelineConfig, Stage


# ── StageResult ──────────────────────────────────────────────────────────────────


@dataclass
class StageResult:
    """Structured result of executing a single pipeline stage.

    Attributes:
        stage_name: Name of the stage (from StageContract.stage_name).
        status: "ok" if the stage completed successfully, "failed" if it raised.
        artifacts: List of Artifact objects produced by the stage.
        error: Error message if the stage failed; None otherwise.
    """

    stage_name: str
    status: str  # "ok" | "failed"
    artifacts: list[Artifact] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Return True if the stage completed successfully."""
        return self.status == "ok"

    def summary_line(self) -> str:
        """Return a one-line summary suitable for concatenating into a result report."""
        artifact_count = len(self.artifacts)
        if self.ok:
            return f"{self.stage_name}: OK — {artifact_count} artifact{'s' if artifact_count != 1 else ''}"
        return f"{self.stage_name}: FAILED — {self.error}"


# ── StageOrchestrator ───────────────────────────────────────────────────────────


class StageOrchestrator:
    """Shared helper that orchestrates the full lifecycle of a pipeline stage.

    Encapsulates the repeated pattern of:
        1. Build PipelineConfig
        2. Create ArtifactBus
        3. Instantiate the stage
        4. Call prepare() → execute()
        5. Wrap results into a StageResult with error handling

    Domain agents call execute_stage() for a single stage or execute_sequence()
    for an ordered list of stages. The orchestrator is stateless — it holds no
    domain knowledge and depends only on the Stage interface (prepare/execute).
    """

    @staticmethod
    async def execute_stage(
        stage_class: Type[Stage],
        job_root: str | Path,
        backend: str,
        *,
        source_kind: str = "local",
        **extra: Any,
    ) -> StageResult:
        """Execute a single stage with full lifecycle and error handling.

        Args:
            stage_class: A Stage subclass (not instance). The orchestrator
                instantiates it, sets its config, and calls prepare/execute.
            job_root: Pipeline job root directory (absolute path).
            backend: LLM backend name (e.g. 'qwen', 'doubao').
            source_kind: Source mode: 'local' scans directories, 'remote' reads URL
                manifests. Passed as PipelineConfig(source_kind=...).
            **extra: Forwarded to PipelineConfig(extra=...). Use for
                input_root, or other stage-specific config.

        Returns:
            StageResult with status, artifacts, and optional error.
            Never raises — errors are captured in the result.
        """
        if isinstance(job_root, str):
            job_root = Path(job_root).expanduser().resolve()

        cfg = PipelineConfig(
            job_root=job_root,
            backend=backend,
            source_kind=source_kind,
            extra=extra,
        )

        stage = stage_class()
        stage.config = cfg

        try:
            bus = ArtifactBus()
            tasks = stage.prepare(bus)
            artifacts = stage.execute(bus, tasks)
            return StageResult(
                stage_name=stage.contract.stage_name,
                status="ok",
                artifacts=list(artifacts),
            )
        except Exception as exc:
            # Attempt to read the stage name from the contract property even
            # on failure — it is a pure property, not dependent on prepare/execute.
            try:
                name = stage_class().contract.stage_name
            except Exception:
                name = stage_class.__name__
            return StageResult(
                stage_name=name,
                status="failed",
                error=str(exc),
            )

    @staticmethod
    async def execute_sequence(
        stages: list[tuple[Type[Stage], dict[str, Any] | None]],
        job_root: str | Path,
        backend: str = "qwen",
        *,
        source_kind: str = "local",
        **extra: Any,
    ) -> list[StageResult]:
        """Execute stages in order, stopping on the first failure.

        Each stage can optionally provide a config_overrides dict for per-stage
        PipelineConfig customization. Stages with None overrides use the defaults.

        Example:
            StageOrchestrator.execute_sequence([
                (SourceWindowsStage, {"source_kind": "local", "extra": {"input_root": "/videos"}}),
                (WindowAnalysisStage, {"backend": "qwen"}),
                (EventCardsStage, None),  # uses defaults
            ], job_root="/job", backend="qwen")

        This is a fail-fast sequence — stops on first failure.
        """
        results: list[StageResult] = []

        for stage_class, overrides in stages:
            kwargs: dict[str, Any] = {
                "job_root": job_root,
                "backend": backend,
                "source_kind": source_kind,
                **extra,
            }
            if overrides:
                kwargs.update(overrides)

            result = await StageOrchestrator.execute_stage(
                stage_class=stage_class,
                **kwargs,
            )
            results.append(result)
            if not result.ok:
                break

        return results