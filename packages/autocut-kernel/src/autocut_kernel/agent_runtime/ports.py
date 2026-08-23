"""Typed composition ports for the runtime's accepted local boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from ..pipeline import (
    LocalMediaCommandRequest,
    PersistedRenderLocalRequest,
    RenderLocalOutcome,
    SemanticChainCommandRequest,
    SemanticChainCommandResult,
)
from ..scenario_registry import (
    DownstreamScenarioPlan,
    ScenarioRef,
    SemanticScenarioPlan,
    SemanticScenarioSuccess,
    UpstreamScenarioOutputs,
    UpstreamScenarioPlan,
)
from ..store import CommandOutcome, Job, PersistedMediaOutputs


class ScenarioRegistryPort(Protocol):
    def prepare_upstream(self, ref: ScenarioRef, job: Job) -> UpstreamScenarioPlan: ...

    def prepare_semantic(
        self, ref: ScenarioRef, semantic_job: Job, upstream: UpstreamScenarioOutputs
    ) -> SemanticScenarioPlan: ...

    def prepare_downstream(
        self, ref: ScenarioRef, downstream_job: Job, semantic: SemanticScenarioSuccess
    ) -> DownstreamScenarioPlan: ...


class MediaCommandPort(Protocol):
    def execute(self, request: LocalMediaCommandRequest) -> CommandOutcome: ...


class SemanticCommandPort(Protocol):
    def execute(self, request: SemanticChainCommandRequest) -> SemanticChainCommandResult: ...


class SucceededMediaArtifactsReader(Protocol):
    def read_succeeded_media_outputs(self, job: Job) -> PersistedMediaOutputs: ...


class PersistedRenderPort(Protocol):
    def execute_persisted(self, request: PersistedRenderLocalRequest) -> RenderLocalOutcome: ...


@dataclass(frozen=True, slots=True)
class LocalOutputConfiguration:
    """Trusted composition configuration; it is deliberately not agent intent."""

    output_root: Path

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.output_root), Path):
            raise ValueError("output_root must be a Path")
