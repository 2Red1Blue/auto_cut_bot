"""Typed, immutable fixture scenario planning."""

from .registry import (
    DownstreamScenarioPlan,
    FixtureScenarioRegistry,
    ScenarioRef,
    ScenarioRegistryDenied,
    SemanticScenarioPlan,
    SemanticScenarioSuccess,
    UpstreamScenarioOutputs,
    UpstreamScenarioPlan,
)

__all__ = [
    "DownstreamScenarioPlan",
    "FixtureScenarioRegistry",
    "ScenarioRef",
    "ScenarioRegistryDenied",
    "SemanticScenarioPlan",
    "SemanticScenarioSuccess",
    "UpstreamScenarioOutputs",
    "UpstreamScenarioPlan",
]
