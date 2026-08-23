from __future__ import annotations

import ast
from pathlib import Path

import pytest
from autocut_kernel.agent_runtime import (
    AgentRunResult,
    AgentRunStage,
    AgentRunState,
    AgentStageTrace,
)
from autocut_kernel.scenario_registry import ScenarioRef
from autocut_kernel.semantic_chain import SemanticProfile

from auto_cut_bot.autocut_agent_runtime import (
    AgentRuntimeAdapter,
    AgentRuntimeAdapterError,
    AgentRuntimeRequest,
)


def _request(profile: SemanticProfile = SemanticProfile.TEST) -> AgentRuntimeRequest:
    return AgentRuntimeRequest(
        "agent_run_" + "a" * 32,
        profile,
        ScenarioRef("scenario_" + "b" * 32),
    )


class _Runtime:
    def __init__(self, result: AgentRunResult) -> None:
        self.result = result
        self.intent = None

    def run(self, intent):
        self.intent = intent
        return self.result


def test_adapter_maps_closed_kernel_terminal_result_without_physical_fields() -> None:
    request = _request()
    result = AgentRunResult(
        request.run_id,
        request.profile,
        AgentRunState.UPSTREAM_MEDIA_DENIED,
        (AgentStageTrace(AgentRunStage.UPSTREAM_MEDIA, "job-upstream", "denied", None),),
    )
    runtime = _Runtime(result)

    response = AgentRuntimeAdapter(runtime).run(request)

    assert runtime.intent.run_id == request.run_id
    assert runtime.intent.scenario == request.scenario
    assert response.state is AgentRunState.UPSTREAM_MEDIA_DENIED
    assert response.stages[0].job_key == "job-upstream"
    assert not hasattr(response, "recipe") and not hasattr(response, "pts")


def test_adapter_forwards_production_to_kernel_pre_start_rejection() -> None:
    request = _request(SemanticProfile.PRODUCTION)
    runtime = _Runtime(
        AgentRunResult(
            request.run_id,
            request.profile,
            AgentRunState.REJECTED_BEFORE_START,
            (),
        )
    )

    response = AgentRuntimeAdapter(runtime).run(request)

    assert runtime.intent.profile is SemanticProfile.PRODUCTION
    assert response.state is AgentRunState.REJECTED_BEFORE_START
    assert response.stages == ()


@pytest.mark.parametrize("value", ({"path": "/tmp/video.mp4"}, "start_pts=42", "score=0.99"))
def test_adapter_rejects_untyped_or_physical_scenario_intent(value: object) -> None:
    with pytest.raises(Exception):
        ScenarioRef(value)  # type: ignore[arg-type]
    with pytest.raises(AgentRuntimeAdapterError):
        AgentRuntimeRequest("agent_run_" + "a" * 32, SemanticProfile.TEST, value)  # type: ignore[arg-type]


def test_adapter_rejects_kernel_result_for_a_different_request() -> None:
    request = _request()
    runtime = _Runtime(
        AgentRunResult(
            "agent_run_" + "c" * 32,
            SemanticProfile.TEST,
            AgentRunState.REJECTED_BEFORE_START,
            (),
        )
    )
    with pytest.raises(AgentRuntimeAdapterError, match="does not bind"):
        AgentRuntimeAdapter(runtime).run(request)


def test_adapter_modules_import_no_legacy_pipeline_or_provider_modules() -> None:
    root = Path(__file__).parents[2] / "auto_cut_bot" / "autocut_agent_runtime"
    forbidden = {"agent", "pipeline", "provider", "providers", "openai", "anthropic", "store"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        imports += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(part in forbidden for name in imports for part in name.split("."))
