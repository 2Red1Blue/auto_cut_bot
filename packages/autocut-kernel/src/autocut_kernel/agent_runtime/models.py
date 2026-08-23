"""Closed agent-facing values for the local runtime MVP."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath
from typing import Final, cast
from uuid import UUID

from ..scenario_registry import ScenarioRef
from ..semantic_chain import SemanticProfile

_RUN_ID: Final = re.compile(r"agent_run_[0-9a-f]{32}\Z")


class AgentRuntimeError(ValueError):
    """Base error for invalid closed runtime intent or port output."""


class AgentRunState(str, Enum):
    REJECTED_BEFORE_START = "rejected_before_start"
    UPSTREAM_MEDIA_DENIED = "upstream_media_denied"
    UPSTREAM_MEDIA_FAILED = "upstream_media_failed"
    SEMANTIC_DENIED = "semantic_denied"
    SEMANTIC_FAILED = "semantic_failed"
    DOWNSTREAM_MEDIA_DENIED = "downstream_media_denied"
    DOWNSTREAM_MEDIA_FAILED = "downstream_media_failed"
    RENDER_DENIED = "render_denied"
    RENDER_FAILED = "render_failed"
    SUCCEEDED = "succeeded"


class AgentRunStage(str, Enum):
    UPSTREAM_MEDIA = "upstream_media"
    SEMANTIC = "semantic"
    DOWNSTREAM_MEDIA = "downstream_media"
    RENDER = "render"


def _run_id(value: object) -> str:
    if type(value) is not str or not _RUN_ID.fullmatch(value):  # noqa: E721
        raise AgentRuntimeError("run_id must be agent_run_<32 lowercase-hex>")
    return value


@dataclass(frozen=True, slots=True)
class AgentRunIntent:
    """The only agent input: an opaque scenario reference and profile."""

    run_id: str
    profile: SemanticProfile
    scenario: ScenarioRef

    def __post_init__(self) -> None:
        _run_id(self.run_id)
        if type(self.profile) is not SemanticProfile:  # noqa: E721
            raise AgentRuntimeError("profile must be a SemanticProfile")
        if type(self.scenario) is not ScenarioRef:  # noqa: E721
            raise AgentRuntimeError("scenario must be a ScenarioRef")


@dataclass(frozen=True, slots=True)
class AgentStageTrace:
    """Trace-only terminal observation for one durable Job boundary."""

    stage: AgentRunStage
    job_key: str
    command_state: str
    receipt_id: UUID | None

    def __post_init__(self) -> None:
        if type(self.stage) is not AgentRunStage:  # noqa: E721
            raise AgentRuntimeError("trace.stage must be an AgentRunStage")
        if type(self.job_key) is not str or not self.job_key:
            raise AgentRuntimeError("trace.job_key must be a non-empty string")
        if self.command_state not in {"succeeded", "denied", "failed"}:
            raise AgentRuntimeError("trace.command_state must be a terminal command state")
        if self.receipt_id is not None and not isinstance(cast(object, self.receipt_id), UUID):
            raise AgentRuntimeError("trace.receipt_id must be a UUID when present")


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Closed terminal result with durable trace and an optional promoted path."""

    run_id: str
    profile: SemanticProfile
    state: AgentRunState
    traces: tuple[AgentStageTrace, ...]
    output_path: PurePath | None = None

    def __post_init__(self) -> None:
        _run_id(self.run_id)
        if type(self.profile) is not SemanticProfile:  # noqa: E721
            raise AgentRuntimeError("result.profile must be a SemanticProfile")
        if type(self.state) is not AgentRunState:  # noqa: E721
            raise AgentRuntimeError("result.state must be an AgentRunState")
        traces = tuple(self.traces)
        if any(type(trace) is not AgentStageTrace for trace in traces):  # noqa: E721
            raise AgentRuntimeError("result.traces must contain AgentStageTrace values")
        if self.state is AgentRunState.REJECTED_BEFORE_START:
            if traces or self.output_path is not None:
                raise AgentRuntimeError("pre-start rejection cannot contain trace or output")
        elif not traces:
            raise AgentRuntimeError("terminal run result must retain prior stage traces")
        if self.state is AgentRunState.SUCCEEDED:
            if len(traces) != len(AgentRunStage) or self.output_path is None:
                raise AgentRuntimeError("success requires all stage traces and a promoted output path")
        elif self.output_path is not None:
            raise AgentRuntimeError("only success may expose a promoted output path")
        if self.output_path is not None and not isinstance(cast(object, self.output_path), PurePath):
            raise AgentRuntimeError("output_path must be an immutable path")
        object.__setattr__(self, "traces", traces)
