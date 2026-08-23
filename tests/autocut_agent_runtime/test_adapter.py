from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

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
    AgentRuntimeResponse,
    AgentRuntimeStageResult,
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
    assert response.run_id == request.run_id
    assert response.profile is request.profile
    assert response.scenario is request.scenario
    assert response.state is AgentRunState.UPSTREAM_MEDIA_DENIED
    assert response.stages[0].job_key == "job-upstream"
    assert not hasattr(response, "recipe") and not hasattr(response, "pts")


def test_adapter_maps_trace_receipt_id_losslessly() -> None:
    request = _request()
    receipt_id = uuid4()
    runtime = _Runtime(
        AgentRunResult(
            request.run_id,
            request.profile,
            AgentRunState.UPSTREAM_MEDIA_DENIED,
            (AgentStageTrace(AgentRunStage.UPSTREAM_MEDIA, "job-upstream", "denied", receipt_id),),
        )
    )

    response = AgentRuntimeAdapter(runtime).run(request)

    assert response.stages == (
        AgentRuntimeStageResult(AgentRunStage.UPSTREAM_MEDIA, "job-upstream", "denied", receipt_id),
    )
    assert response.stages[0].receipt_id == receipt_id


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


def test_adapter_rejects_kernel_result_for_a_different_profile() -> None:
    request = _request()
    runtime = _Runtime(
        AgentRunResult(
            request.run_id,
            SemanticProfile.SHADOW,
            AgentRunState.REJECTED_BEFORE_START,
            (),
        )
    )
    with pytest.raises(AgentRuntimeAdapterError, match="does not bind"):
        AgentRuntimeAdapter(runtime).run(request)


@pytest.mark.parametrize(
    "run_id",
    (
        "agent_run_" + "A" * 32,
        "agent_run_" + "a" * 31,
        "agent_run_" + "a" * 32 + "/tmp/output",
        "start_pts=42",
    ),
)
def test_request_rejects_nonopaque_or_malformed_run_id(run_id: str) -> None:
    with pytest.raises(AgentRuntimeAdapterError, match="agent_run"):
        AgentRuntimeRequest(run_id, SemanticProfile.TEST, _request().scenario)


def test_stage_result_rejects_direct_forged_terminal_values() -> None:
    with pytest.raises(AgentRuntimeAdapterError, match="closed terminal trace"):
        AgentRuntimeStageResult(  # type: ignore[arg-type]
            "semantic",
            "job-semantic",
            "succeeded",
            None,
        )
    with pytest.raises(AgentRuntimeAdapterError, match="closed terminal trace"):
        AgentRuntimeStageResult(AgentRunStage.SEMANTIC, "", "score=0.99", None)


@pytest.mark.parametrize(
    ("state", "stages", "output_path"),
    (
        (
            AgentRunState.SEMANTIC_DENIED,
            (AgentRuntimeStageResult(AgentRunStage.SEMANTIC, "job-semantic", "denied", None),),
            None,
        ),
        (
            AgentRunState.DOWNSTREAM_MEDIA_DENIED,
            (
                AgentRuntimeStageResult(AgentRunStage.UPSTREAM_MEDIA, "job-upstream", "succeeded", None),
                AgentRuntimeStageResult(AgentRunStage.SEMANTIC, "job-semantic", "failed", None),
                AgentRuntimeStageResult(AgentRunStage.DOWNSTREAM_MEDIA, "job-downstream", "denied", None),
            ),
            None,
        ),
        (
            AgentRunState.UPSTREAM_MEDIA_DENIED,
            (AgentRuntimeStageResult(AgentRunStage.UPSTREAM_MEDIA, "job-upstream", "denied", None),),
            Path("current.json"),
        ),
    ),
)
def test_response_rejects_forged_or_nonterminal_stage_projection(
    state: AgentRunState,
    stages: tuple[AgentRuntimeStageResult, ...],
    output_path: Path | None,
) -> None:
    request = _request()
    with pytest.raises(AgentRuntimeAdapterError, match="closed terminal Kernel result"):
        AgentRuntimeResponse(
            request.run_id,
            request.profile,
            request.scenario,
            state,
            stages,
            output_path,
        )


def test_response_rejects_direct_forged_profile_state_and_scenario() -> None:
    request = _request()
    with pytest.raises(AgentRuntimeAdapterError, match="response.profile"):
        AgentRuntimeResponse(  # type: ignore[arg-type]
            request.run_id,
            "test",
            request.scenario,
            AgentRunState.REJECTED_BEFORE_START,
            (),
            None,
        )
    with pytest.raises(AgentRuntimeAdapterError, match="response.state"):
        AgentRuntimeResponse(  # type: ignore[arg-type]
            request.run_id,
            request.profile,
            request.scenario,
            "succeeded",
            (),
            None,
        )
    with pytest.raises(AgentRuntimeAdapterError, match="response.scenario"):
        AgentRuntimeResponse(  # type: ignore[arg-type]
            request.run_id,
            request.profile,
            "scenario_/tmp/video.mp4",
            AgentRunState.REJECTED_BEFORE_START,
            (),
            None,
        )


def test_adapter_modules_import_no_legacy_pipeline_or_provider_modules() -> None:
    root = Path(__file__).parents[2] / "auto_cut_bot" / "autocut_agent_runtime"
    forbidden = {"agent", "pipeline", "provider", "providers", "openai", "anthropic", "store"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        imports += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(part in forbidden for name in imports for part in name.split("."))
