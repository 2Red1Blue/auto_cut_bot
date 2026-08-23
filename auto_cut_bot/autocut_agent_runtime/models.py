"""Closed request and response values for the cut_bot runtime composition seam."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath

from autocut_kernel.agent_runtime import (
    AgentRunResult,
    AgentRunStage,
    AgentRunState,
    AgentStageTrace,
)
from autocut_kernel.scenario_registry import ScenarioRef
from autocut_kernel.semantic_chain import SemanticProfile


class AgentRuntimeAdapterError(ValueError):
    """A cut_bot request or Kernel terminal result is outside this closed edge."""


@dataclass(frozen=True, slots=True)
class AgentRuntimeRequest:
    """Agent-facing intent: opaque run/scenario identity and closed profile only."""

    run_id: str
    profile: SemanticProfile
    scenario: ScenarioRef

    def __post_init__(self) -> None:
        if type(self.run_id) is not str:  # noqa: E721
            raise AgentRuntimeAdapterError("run_id must be a closed runtime token string")
        if type(self.profile) is not SemanticProfile:  # noqa: E721
            raise AgentRuntimeAdapterError("profile must be a SemanticProfile")
        if type(self.scenario) is not ScenarioRef:  # noqa: E721
            raise AgentRuntimeAdapterError("scenario must be a ScenarioRef")


@dataclass(frozen=True, slots=True)
class AgentRuntimeStageResult:
    """Terminal stage observation without command inputs or physical data."""

    stage: AgentRunStage
    job_key: str
    state: str

    @classmethod
    def from_trace(cls, trace: AgentStageTrace) -> AgentRuntimeStageResult:
        if type(trace) is not AgentStageTrace:  # noqa: E721
            raise AgentRuntimeAdapterError("Kernel trace must be an AgentStageTrace")
        return cls(trace.stage, trace.job_key, trace.command_state)


@dataclass(frozen=True, slots=True)
class AgentRuntimeResponse:
    """Safe terminal response projected one-way from Kernel runtime output."""

    run_id: str
    profile: SemanticProfile
    scenario: ScenarioRef
    state: AgentRunState
    stages: tuple[AgentRuntimeStageResult, ...]
    output_path: PurePath | None

    @classmethod
    def from_kernel(cls, request: AgentRuntimeRequest, result: AgentRunResult) -> AgentRuntimeResponse:
        if type(result) is not AgentRunResult:  # noqa: E721
            raise AgentRuntimeAdapterError("runtime must return an AgentRunResult")
        if result.run_id != request.run_id or result.profile is not request.profile:
            raise AgentRuntimeAdapterError("Kernel result does not bind the submitted request")
        return cls(
            request.run_id,
            request.profile,
            request.scenario,
            result.state,
            tuple(AgentRuntimeStageResult.from_trace(trace) for trace in result.traces),
            result.output_path,
        )
