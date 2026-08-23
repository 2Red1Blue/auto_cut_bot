"""Immutable test/shadow scenario planning with no Store or command effects."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..media.preflight import MediaPreflightRequest
from ..physical_edit import FixtureBeatInput, SpanSelectionPolicy
from ..pipeline import (
    FixtureBeatResolver,
    LocalMediaCommandRequest,
    SemanticChainCommandRequest,
    SemanticChainCommandResult,
)
from ..semantic_chain import (
    CatalogCandidateRef,
    EvidenceRef,
    FactKind,
    FixtureCandidateRegistry,
    RegisteredFact,
    SemanticChainInput,
    SemanticProfile,
)
from ..store import Job, MediaEvidenceReference, RecipeReference
from ..store.models import canonical_recipe_scope

_SCENARIO = re.compile(r"scenario_[0-9a-f]{32}\Z")
_FIXTURE = re.compile(r"fixture_[0-9a-f]{32}\Z")
_CANDIDATE = re.compile(r"candidate_[0-9a-f]{32}\Z")
_CATALOG = re.compile(r"catalog_[0-9a-f]{32}\Z")
_FACT = re.compile(r"fact_[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ScenarioRegistryDenied(ValueError):  # noqa: N818 - outcome vocabulary is intentional.
    """A scenario cannot create the requested closed stage plan."""


@dataclass(frozen=True, slots=True)
class ScenarioRef:
    """The sole agent-facing scenario identity."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or not _SCENARIO.fullmatch(self.value):  # noqa: E721
            raise ScenarioRegistryDenied("scenario ref must be scenario_<32 lowercase-hex>")


@dataclass(frozen=True, slots=True)
class FixtureScenario:
    """Application-composed fixture registration; it owns all physical data."""

    ref: ScenarioRef
    profile: SemanticProfile
    source_path: Path
    fixture_id: str
    expected_source_sha256: str
    manifest_path: Path
    sidecar_path: Path
    upstream_beat: FixtureBeatInput
    policy: SpanSelectionPolicy
    candidate_id: str
    catalog_source_id: str
    catalog_source_hash: str
    fact_id: str
    fact_kind: FactKind
    registry: FixtureCandidateRegistry
    beat_resolver: FixtureBeatResolver

    def __post_init__(self) -> None:
        if type(self.ref) is not ScenarioRef:  # noqa: E721
            raise ScenarioRegistryDenied("fixture scenario requires a ScenarioRef")
        if type(self.profile) is not SemanticProfile:  # noqa: E721
            raise ScenarioRegistryDenied("fixture scenario profile must be a SemanticProfile")
        if self.profile is SemanticProfile.PRODUCTION:
            raise ScenarioRegistryDenied("production scenarios are forbidden")
        for field_name, value, pattern in (
            ("fixture_id", self.fixture_id, _FIXTURE),
            ("candidate_id", self.candidate_id, _CANDIDATE),
            ("catalog_source_id", self.catalog_source_id, _CATALOG),
            ("fact_id", self.fact_id, _FACT),
        ):
            if type(value) is not str or not pattern.fullmatch(value):  # noqa: E721
                raise ScenarioRegistryDenied(f"{field_name} must be a closed typed token")
        if type(self.expected_source_sha256) is not str or not _SHA256.fullmatch(self.expected_source_sha256):  # noqa: E721
            raise ScenarioRegistryDenied("expected_source_sha256 must be a lowercase sha256 digest")
        if type(self.catalog_source_hash) is not str or not _SHA256.fullmatch(self.catalog_source_hash):  # noqa: E721
            raise ScenarioRegistryDenied("catalog_source_hash must be a lowercase sha256 digest")
        if type(self.fact_kind) is not FactKind:  # noqa: E721
            raise ScenarioRegistryDenied("fact_kind must be a FactKind")
        if not all(
            isinstance(cast(object, path), Path)
            for path in (self.source_path, self.manifest_path, self.sidecar_path)
        ):
            raise ScenarioRegistryDenied("fixture scenario paths must be Path values")
        if (
            type(self.upstream_beat) is not FixtureBeatInput
            or type(self.policy) is not SpanSelectionPolicy
        ):  # noqa: E721
            raise ScenarioRegistryDenied("fixture scenario requires closed physical fixture values")
        if not callable(getattr(self.registry, "resolve_exact", None)):
            raise ScenarioRegistryDenied("fixture scenario requires an exact candidate registry")
        if not callable(getattr(self.beat_resolver, "resolve_beat", None)):
            raise ScenarioRegistryDenied("fixture scenario requires a fixture beat resolver")


@dataclass(frozen=True, slots=True)
class UpstreamScenarioPlan:
    scenario: ScenarioRef
    request: LocalMediaCommandRequest


@dataclass(frozen=True, slots=True)
class UpstreamScenarioOutputs:
    """Exact immutable identities produced by the upstream media command."""

    job: Job
    media_evidence: MediaEvidenceReference
    recipe: RecipeReference

    def __post_init__(self) -> None:
        if self.media_evidence.scope != canonical_recipe_scope(self.job):
            raise ScenarioRegistryDenied("upstream media evidence must use its job scope")
        if self.recipe.scope != canonical_recipe_scope(self.job):
            raise ScenarioRegistryDenied("upstream recipe must use its job scope")


@dataclass(frozen=True, slots=True)
class SemanticScenarioPlan:
    scenario: ScenarioRef
    candidate: CatalogCandidateRef
    request: SemanticChainCommandRequest


@dataclass(frozen=True, slots=True)
class SemanticScenarioSuccess:
    """A typed semantic completion tied to its exact registry-created plan."""

    plan: SemanticScenarioPlan
    result: SemanticChainCommandResult

    def __post_init__(self) -> None:
        if self.result.outcome.state != "succeeded" or self.result.resolved_beat is None:
            raise ScenarioRegistryDenied("semantic completion must be a succeeded resolved result")
        refs = self.result.resolved_beat.semantic_artifacts
        expected_scope = canonical_recipe_scope(self.plan.request.job)
        if any(
            item.scope != expected_scope
            for item in (refs.narrative_graph, refs.story_set, refs.editorial_blueprint)
        ):
            raise ScenarioRegistryDenied("semantic artifacts must bind the semantic job scope")


@dataclass(frozen=True, slots=True)
class DownstreamScenarioPlan:
    scenario: ScenarioRef
    semantic: SemanticScenarioSuccess
    request: LocalMediaCommandRequest


class FixtureScenarioRegistry:
    """Read-only fixture plan creator; it never queries or executes dependencies."""

    def __init__(self, scenarios: tuple[FixtureScenario, ...]) -> None:
        values = tuple(scenarios)
        if not values or any(type(item) is not FixtureScenario for item in values):  # noqa: E721
            raise ScenarioRegistryDenied(
                "scenarios must be non-empty FixtureScenario registrations"
            )
        refs = tuple(item.ref for item in values)
        if len(refs) != len(set(refs)):
            raise ScenarioRegistryDenied("scenario registrations must have unique refs")
        self._scenarios = {item.ref: item for item in values}

    def prepare_upstream(self, ref: ScenarioRef, job: Job) -> UpstreamScenarioPlan:
        scenario = self._scenario(ref, job)
        request = LocalMediaCommandRequest(
            job,
            f"{ref.value}:upstream",
            MediaPreflightRequest(
                scenario.profile.value,
                scenario.source_path,
                scenario.fixture_id,
                scenario.expected_source_sha256,
                scenario.manifest_path,
                scenario.sidecar_path,
            ),
            scenario.upstream_beat,
            scenario.policy,
            canonical_recipe_scope(job),
        )
        return UpstreamScenarioPlan(ref, request)

    def prepare_semantic(
        self, ref: ScenarioRef, semantic_job: Job, upstream: UpstreamScenarioOutputs
    ) -> SemanticScenarioPlan:
        scenario = self._scenario(ref, semantic_job)
        if upstream.job.profile != semantic_job.profile:
            raise ScenarioRegistryDenied("upstream and semantic jobs must share a profile")
        evidence = upstream.media_evidence
        if evidence.logical_id != "media_evidence":
            raise ScenarioRegistryDenied(
                "upstream evidence must be the canonical media_evidence artifact"
            )
        evidence_ref = EvidenceRef(evidence.logical_id, evidence.content_hash)
        candidate = CatalogCandidateRef(
            scenario.candidate_id,
            scenario.catalog_source_id,
            scenario.catalog_source_hash,
            evidence_ref,
            scenario.profile,
        )
        source = SemanticChainInput(
            scenario.profile,
            (evidence_ref,),
            (RegisteredFact(scenario.fact_id, scenario.fact_kind, evidence_ref, candidate),),
        )
        request = SemanticChainCommandRequest(
            semantic_job,
            f"{ref.value}:semantic",
            source,
            candidate,
            upstream.job,
            evidence,
            scenario.registry,
            scenario.beat_resolver,
            canonical_recipe_scope(semantic_job),
        )
        return SemanticScenarioPlan(ref, candidate, request)

    def prepare_downstream(
        self, ref: ScenarioRef, downstream_job: Job, semantic: SemanticScenarioSuccess
    ) -> DownstreamScenarioPlan:
        scenario = self._scenario(ref, downstream_job)
        if (
            semantic.plan.scenario != ref
            or semantic.plan.candidate.catalog_source_hash != scenario.catalog_source_hash
        ):
            raise ScenarioRegistryDenied("semantic plan does not bind this scenario catalog")
        if semantic.plan.request.job == downstream_job:
            raise ScenarioRegistryDenied("downstream media job must be distinct from semantic job")
        resolved = semantic.result.resolved_beat
        if resolved is None:  # Defensive narrowing; SemanticScenarioSuccess rejects this state.
            raise ScenarioRegistryDenied("semantic completion has no resolved beat")
        request = LocalMediaCommandRequest(
            downstream_job,
            f"{ref.value}:downstream",
            MediaPreflightRequest(
                scenario.profile.value,
                scenario.source_path,
                scenario.fixture_id,
                scenario.expected_source_sha256,
                scenario.manifest_path,
                scenario.sidecar_path,
            ),
            resolved.beat,
            scenario.policy,
            canonical_recipe_scope(downstream_job),
        )
        return DownstreamScenarioPlan(ref, semantic, request)

    def _scenario(self, ref: ScenarioRef, job: Job) -> FixtureScenario:
        if type(ref) is not ScenarioRef:  # noqa: E721
            raise ScenarioRegistryDenied("registry accepts only ScenarioRef")
        scenario = self._scenarios.get(ref)
        if scenario is None:
            raise ScenarioRegistryDenied("unknown scenario")
        if job.profile != scenario.profile.value:
            raise ScenarioRegistryDenied("scenario profile does not match job")
        return scenario
