"""Closed request and response values for the cut_bot runtime composition seam."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Final
from uuid import UUID

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


_RUN_ID: Final = re.compile(r"agent_run_[0-9a-f]{32}\Z")


@dataclass(frozen=True, slots=True)
class AgentRuntimeRequest:
    """Agent-facing intent: opaque run/scenario identity and closed profile only."""

    run_id: str
    profile: SemanticProfile
    scenario: ScenarioRef

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or not _RUN_ID.fullmatch(self.run_id):  # noqa: E721
            raise AgentRuntimeAdapterError("run_id must be agent_run_<32 lowercase-hex>")
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
    receipt_id: UUID | None

    def __post_init__(self) -> None:
        try:
            AgentStageTrace(self.stage, self.job_key, self.state, self.receipt_id)
        except ValueError as error:
            raise AgentRuntimeAdapterError("stage result must be a closed terminal trace") from error

    @classmethod
    def from_trace(cls, trace: AgentStageTrace) -> AgentRuntimeStageResult:
        if type(trace) is not AgentStageTrace:  # noqa: E721
            raise AgentRuntimeAdapterError("Kernel trace must be an AgentStageTrace")
        return cls(trace.stage, trace.job_key, trace.command_state, trace.receipt_id)


@dataclass(frozen=True, slots=True)
class AgentRuntimeResponse:
    """Safe terminal response projected one-way from Kernel runtime output."""

    run_id: str
    profile: SemanticProfile
    scenario: ScenarioRef
    state: AgentRunState
    stages: tuple[AgentRuntimeStageResult, ...]
    output_path: PurePath | None

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or not _RUN_ID.fullmatch(self.run_id):  # noqa: E721
            raise AgentRuntimeAdapterError("response.run_id must be agent_run_<32 lowercase-hex>")
        if type(self.profile) is not SemanticProfile:  # noqa: E721
            raise AgentRuntimeAdapterError("response.profile must be a SemanticProfile")
        if type(self.scenario) is not ScenarioRef:  # noqa: E721
            raise AgentRuntimeAdapterError("response.scenario must be a ScenarioRef")
        if type(self.state) is not AgentRunState:  # noqa: E721
            raise AgentRuntimeAdapterError("response.state must be an AgentRunState")
        try:
            stages = tuple(self.stages)
        except TypeError as error:
            raise AgentRuntimeAdapterError("response.stages must be a tuple of stage results") from error
        if any(type(stage) is not AgentRuntimeStageResult for stage in stages):  # noqa: E721
            raise AgentRuntimeAdapterError("response.stages must contain AgentRuntimeStageResult values")
        try:
            AgentRunResult(
                self.run_id,
                self.profile,
                self.state,
                tuple(
                    AgentStageTrace(stage.stage, stage.job_key, stage.state, stage.receipt_id)
                    for stage in stages
                ),
                self.output_path,
            )
        except ValueError as error:
            raise AgentRuntimeAdapterError("response must project a closed terminal Kernel result") from error
        object.__setattr__(self, "stages", stages)

    @classmethod
    def from_kernel(cls, request: AgentRuntimeRequest, result: AgentRunResult) -> AgentRuntimeResponse:
        if type(request) is not AgentRuntimeRequest:  # noqa: E721
            raise AgentRuntimeAdapterError("request must be an AgentRuntimeRequest")
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
