from __future__ import annotations

from pathlib import Path

import pytest
from autocut_kernel.physical_edit import FixtureBeatInput, SpanSelectionPolicy
from autocut_kernel.scenario_registry import (
    FixtureScenario,
    FixtureScenarioRegistry,
    ScenarioRef,
    ScenarioRegistryDenied,
    UpstreamScenarioOutputs,
)
from autocut_kernel.semantic_chain import CatalogResolution, FactKind, SemanticProfile
from autocut_kernel.store import ArtifactScope, Job, MediaEvidenceReference, RecipeReference


class _Registry:
    def resolve_exact(self, candidate, _):
        return CatalogResolution(candidate, candidate.evidence)


class _Resolver:
    def resolve_beat(self, _):
        return FixtureBeatInput(0, 10, 20, 30, 10)


def _fixture(profile: SemanticProfile = SemanticProfile.TEST) -> FixtureScenario:
    return FixtureScenario(
        ScenarioRef("scenario_11111111111111111111111111111111"),
        profile,
        Path("source.mp4"),
        "fixture_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "sha256:" + "a" * 64,
        Path("manifest.json"),
        Path("sidecar.json"),
        FixtureBeatInput(0, 10, 20, 30, 10),
        SpanSelectionPolicy(4),
        "candidate_22222222222222222222222222222222",
        "catalog_33333333333333333333333333333333",
        "sha256:" + "b" * 64,
        "fact_44444444444444444444444444444444",
        FactKind.OBSERVATION,
        _Registry(),
        _Resolver(),
    )


def test_registry_creates_only_closed_upstream_plan() -> None:
    fixture = _fixture()
    registry = FixtureScenarioRegistry((fixture,))
    plan = registry.prepare_upstream(fixture.ref, Job("upstream", "test"))
    assert plan.request.beat == fixture.upstream_beat
    assert plan.request.preflight_request.source_path == fixture.source_path
    with pytest.raises(ScenarioRegistryDenied, match="profile"):
        registry.prepare_upstream(fixture.ref, Job("wrong", "shadow"))


def test_ref_and_production_registration_are_denied() -> None:
    with pytest.raises(ScenarioRegistryDenied):
        ScenarioRef("source.mp4")
    with pytest.raises(ScenarioRegistryDenied, match="production"):
        _fixture(SemanticProfile.PRODUCTION)


@pytest.mark.parametrize("value", ("/tmp/fixture.mp4", "start_pts=42", "score=0.99", {"scenario": "free"}))
def test_agent_facing_scenario_ref_rejects_physical_and_untyped_payloads(value: object) -> None:
    with pytest.raises(ScenarioRegistryDenied):
        ScenarioRef(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("candidate_id", "candidate_start_pts_42"),
        ("catalog_source_id", "/tmp/catalog.json"),
        ("catalog_source_hash", "score=0.99"),
        ("fact_id", "fact_path_clip_mp4"),
        ("profile", "test"),
    ),
)
def test_malformed_composition_registration_is_denied(field: str, value: object) -> None:
    fixture = _fixture()
    values = {name: getattr(fixture, name) for name in fixture.__dataclass_fields__}
    values[field] = value
    with pytest.raises(ScenarioRegistryDenied):
        FixtureScenario(**values)  # type: ignore[arg-type]


def test_plan_creation_never_executes_store_or_command_ports() -> None:
    calls: list[str] = []

    class Registry:
        def resolve_exact(self, candidate, _):
            calls.append("registry")
            return CatalogResolution(candidate, candidate.evidence)

    class Resolver:
        def resolve_beat(self, _):
            calls.append("resolver")
            return FixtureBeatInput(0, 10, 20, 30, 10)

    fixture = _fixture()
    values = {name: getattr(fixture, name) for name in fixture.__dataclass_fields__}
    values["registry"] = Registry()
    values["beat_resolver"] = Resolver()
    registry = FixtureScenarioRegistry((FixtureScenario(**values),))  # type: ignore[arg-type]
    upstream_job = Job("upstream-no-execute", "test")
    registry.prepare_upstream(fixture.ref, upstream_job)
    upstream = UpstreamScenarioOutputs(
        upstream_job,
        MediaEvidenceReference(
            ArtifactScope("pipeline", "job", upstream_job.job_key),
            "media_evidence",
            1,
            "sha256:" + "a" * 64,
        ),
        RecipeReference(
            ArtifactScope("pipeline", "job", upstream_job.job_key),
            "recipe",
            1,
            "sha256:" + "b" * 64,
        ),
    )
    registry.prepare_semantic(fixture.ref, Job("semantic-no-execute", "test"), upstream)
    assert calls == []
