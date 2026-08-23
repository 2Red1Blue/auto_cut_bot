from __future__ import annotations

from pathlib import Path

import pytest
from autocut_kernel.physical_edit import FixtureBeatInput, SpanSelectionPolicy
from autocut_kernel.scenario_registry import (
    FixtureScenario,
    FixtureScenarioRegistry,
    ScenarioRef,
    ScenarioRegistryDenied,
)
from autocut_kernel.semantic_chain import CatalogResolution, FactKind, SemanticProfile
from autocut_kernel.store import Job


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
        "fixture",
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
