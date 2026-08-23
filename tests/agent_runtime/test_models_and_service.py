from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from autocut_kernel.agent_runtime import (
    AgentRunIntent,
    AgentRunState,
    AgentRuntimeError,
    AgentRuntimeService,
    LocalOutputConfiguration,
)
from autocut_kernel.scenario_registry import ScenarioRef
from autocut_kernel.semantic_chain import SemanticProfile
from autocut_kernel.store import (
    ArtifactScope,
    CommandOutcome,
    MediaEvidenceReference,
    PersistedMediaOutputs,
    RecipeReference,
)


def _intent(profile: SemanticProfile = SemanticProfile.TEST) -> AgentRunIntent:
    return AgentRunIntent(
        "agent_run_" + "a" * 32,
        profile,
        ScenarioRef("scenario_" + "b" * 32),
    )


class _Scenarios:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def prepare_upstream(self, *_):
        self.calls.append("upstream")
        return SimpleNamespace(request=object())

    def prepare_semantic(self, *_):
        self.calls.append("semantic")
        return SimpleNamespace(request=object())

    def prepare_downstream(self, *_):
        self.calls.append("downstream")
        return SimpleNamespace(request=object())


class _Media:
    def __init__(self, outcome: CommandOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    def execute(self, _):
        self.calls += 1
        return self.outcome


class _Semantic:
    def __init__(self, outcome: CommandOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    def execute(self, _):
        self.calls += 1
        return SimpleNamespace(outcome=self.outcome)


class _Reader:
    def __init__(self) -> None:
        self.calls = 0

    def read_succeeded_media_outputs(self, job):
        self.calls += 1
        scope = ArtifactScope("pipeline", "job", job.job_key)
        return PersistedMediaOutputs(
            MediaEvidenceReference(scope, "media_evidence", 1, "sha256:" + "c" * 64),
            RecipeReference(scope, "recipe", 1, "sha256:" + "d" * 64),
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
        )


class _Renderer:
    def __init__(self) -> None:
        self.calls = 0

    def execute_persisted(self, _):
        self.calls += 1
        raise AssertionError("renderer must not run in these terminal-stop tests")


def _service(upstream: CommandOutcome, semantic: CommandOutcome):
    scenarios = _Scenarios()
    upstream_port = _Media(upstream)
    semantic_port = _Semantic(semantic)
    downstream_port = _Media(CommandOutcome(uuid4(), "succeeded"))
    reader = _Reader()
    renderer = _Renderer()
    service = AgentRuntimeService(
        scenarios,
        upstream_port,
        semantic_port,
        downstream_port,
        reader,
        renderer,
        LocalOutputConfiguration(Path("runtime-output")),
    )
    return service, scenarios, upstream_port, semantic_port, downstream_port, reader, renderer


def test_production_is_rejected_before_any_port_call() -> None:
    service, scenarios, upstream, semantic, downstream, reader, renderer = _service(
        CommandOutcome(uuid4(), "succeeded"), CommandOutcome(uuid4(), "succeeded")
    )
    result = service.run(_intent(SemanticProfile.PRODUCTION))
    assert result.state is AgentRunState.REJECTED_BEFORE_START
    assert result.traces == ()
    assert scenarios.calls == []
    assert upstream.calls == semantic.calls == downstream.calls == reader.calls == renderer.calls == 0


def test_upstream_denial_stops_before_reader_semantic_downstream_and_render() -> None:
    service, scenarios, upstream, semantic, downstream, reader, renderer = _service(
        CommandOutcome(uuid4(), "denied"), CommandOutcome(uuid4(), "succeeded")
    )
    result = service.run(_intent())
    assert result.state is AgentRunState.UPSTREAM_MEDIA_DENIED
    assert scenarios.calls == ["upstream"]
    assert upstream.calls == 1
    assert semantic.calls == downstream.calls == reader.calls == renderer.calls == 0


def test_semantic_denial_stops_before_downstream_and_render() -> None:
    service, scenarios, upstream, semantic, downstream, reader, renderer = _service(
        CommandOutcome(uuid4(), "succeeded"), CommandOutcome(uuid4(), "denied")
    )
    result = service.run(_intent())
    assert result.state is AgentRunState.SEMANTIC_DENIED
    assert scenarios.calls == ["upstream", "semantic"]
    assert upstream.calls == semantic.calls == reader.calls == 1
    assert downstream.calls == renderer.calls == 0


def test_intent_accepts_only_typed_scenario_ref_and_no_physical_agent_values() -> None:
    with pytest.raises(AgentRuntimeError):
        AgentRunIntent("agent_run_" + "a" * 32, SemanticProfile.TEST, {"path": "/tmp/video.mp4"})  # type: ignore[arg-type]
    with pytest.raises(Exception):
        ScenarioRef("start_pts=42")
    with pytest.raises(Exception):
        ScenarioRef("score=0.99")


def test_runtime_modules_import_no_legacy_or_provider_clients() -> None:
    root = Path(__file__).parents[2] / "packages" / "autocut-kernel" / "src" / "autocut_kernel" / "agent_runtime"
    forbidden = {"auto_cut_bot", "autocut_core", "provider", "openai", "anthropic", "llm"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        imports += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(part in forbidden for name in imports for part in name.split("."))
