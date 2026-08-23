from __future__ import annotations

from dataclasses import replace

import pytest
from autocut_kernel.media.types import (
    MediaEvidence,
    PTSIndex,
    SourceIdentity,
    TickRange,
    TimeBase,
    ToolEvidence,
    ValidityIntervals,
    VideoStreamEvidence,
    canonical_sha256,
)
from autocut_kernel.physical_edit import FixtureBeatInput
from autocut_kernel.semantic_chain import (
    EvidenceRef,
    FactKind,
    FixtureAdapterDenied,
    FixtureCandidateBinding,
    FixtureCatalog,
    FixtureCatalogAdapter,
    RegisteredFact,
    SemanticChain,
    SemanticChainBuilder,
    SemanticChainInput,
    SemanticProfile,
)


def _hash(digit: str) -> str:
    return f"sha256:{digit * 64}"


def _media() -> MediaEvidence:
    ticks = PTSIndex((0, 10, 20, 30, 40))
    return MediaEvidence(
        source=SourceIdentity(_hash("a"), 100),
        video_stream=VideoStreamEvidence(0, "mpeg4", 64, 48, TimeBase(1, 10)),
        pts_index=ticks,
        validity_intervals=ValidityIntervals((TickRange(0, 40),)),
        pts_index_sha256=canonical_sha256(list(ticks.ticks)),
        ffprobe=ToolEvidence("ffprobe", "fixture", _hash("b")),
        fixture_id="fixture_v1",
        fixture_manifest_sha256=_hash("c"),
        fixture_sidecar_sha256=_hash("d"),
        fixture_schema_version=1,
        evidence_mode="fixture_ground_truth_v1",
    )


def _registered() -> tuple[
    FixtureCatalogAdapter,
    SemanticChain,
    MediaEvidence,
    EvidenceRef,
    FixtureCandidateBinding,
    FixtureBeatInput,
]:
    media = _media()
    evidence = EvidenceRef("media_evidence_v1", canonical_sha256(media.to_json()))
    chain = SemanticChainBuilder().build(
        SemanticChainInput(
            SemanticProfile.TEST,
            (evidence,),
            (RegisteredFact("fact_v1", FactKind.OBSERVATION, evidence),),
        )
    )
    binding = FixtureCandidateBinding(
        "candidate_v1", evidence, SemanticProfile.TEST, "fixture_catalog_v1"
    )
    beat = FixtureBeatInput(0, 10, 20, 40, 10)
    catalog = FixtureCatalog.registered(
        "fixture_catalog_v1", SemanticProfile.TEST, ((binding, beat),)
    )
    return FixtureCatalogAdapter(catalog), chain, media, evidence, binding, beat


def test_adapter_resolves_only_the_registered_candidate_for_exact_committed_evidence() -> None:
    adapter, chain, media, evidence, binding, beat = _registered()
    assert adapter.resolve(chain, media, evidence, binding) is beat


def test_adapter_denies_foreign_or_uncommitted_evidence_and_profile() -> None:
    adapter, chain, media, evidence, binding, _ = _registered()
    foreign = EvidenceRef("media_evidence_other", evidence.content_hash)
    with pytest.raises(FixtureAdapterDenied, match="exact committed"):
        adapter.resolve(chain, media, foreign, binding)
    altered = replace(media, fixture_id="other_fixture")
    with pytest.raises(FixtureAdapterDenied, match="does not match"):
        adapter.resolve(chain, altered, evidence, binding)
    shadow_chain = SemanticChainBuilder().build(
        SemanticChainInput(
            SemanticProfile.SHADOW,
            (evidence,),
            (RegisteredFact("fact_v1", FactKind.OBSERVATION, evidence),),
        )
    )
    with pytest.raises(FixtureAdapterDenied, match="profiles must match"):
        adapter.resolve(shadow_chain, media, evidence, binding)


def test_adapter_denies_pts_or_mapping_injection_through_semantic_inputs() -> None:
    adapter, chain, media, evidence, binding, _ = _registered()
    with pytest.raises(FixtureAdapterDenied):
        adapter.resolve(chain, media, evidence, {"candidate_id": binding.candidate_id})  # type: ignore[arg-type]
    with pytest.raises(FixtureAdapterDenied):
        adapter.resolve(chain, {"path": "fixture.mp4"}, evidence, binding)  # type: ignore[arg-type]
    with pytest.raises(FixtureAdapterDenied):
        adapter.resolve(chain, None, evidence, binding)  # type: ignore[arg-type]
    with pytest.raises(FixtureAdapterDenied):
        FixtureCandidateBinding(
            "production_candidate", evidence, SemanticProfile.PRODUCTION, "fixture_catalog_v1"
        )
    with pytest.raises(TypeError):
        FixtureCandidateBinding(
            "candidate_v2",
            evidence,
            SemanticProfile.TEST,
            "fixture_catalog_v1",
            desired_start_pts=999,
        )  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        adapter.resolve(chain, media, evidence, binding, desired_start_pts=999)  # type: ignore[call-arg]
    semantic_artifact = chain.to_mapping()
    assert "pts" not in repr(semantic_artifact).lower()
