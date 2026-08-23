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
from autocut_kernel.semantic_chain import (
    CatalogCandidateRef,
    CatalogResolution,
    EvidenceRef,
    FactKind,
    FixtureAdapterDenied,
    FixtureCatalogAdapter,
    RegisteredFact,
    SemanticChain,
    SemanticChainBuilder,
    SemanticChainInput,
    SemanticProfile,
)


def _hash(digit: str) -> str:
    return f"sha256:{digit * 64}"


def _token(kind: str, digit: str) -> str:
    return f"{kind}_{digit * 32}"


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


class _Loader:
    def __init__(self, evidence: MediaEvidence) -> None:
        self.evidence = evidence

    def load_exact(self, evidence_artifact: EvidenceRef) -> MediaEvidence:
        assert evidence_artifact.content_hash == canonical_sha256(self.evidence.to_json())
        return self.evidence


class _Registry:
    """Test-only trusted registry: identity guards provenance, hashes guard drift."""

    def __init__(self, candidate: CatalogCandidateRef, evidence: MediaEvidence) -> None:
        self.candidate = candidate
        self.evidence = evidence

    def resolve_exact(self, candidate: CatalogCandidateRef, media_evidence: MediaEvidence) -> CatalogResolution:
        if candidate != self.candidate:
            raise FixtureAdapterDenied("candidate is not registered by this fixture catalog")
        if media_evidence is not self.evidence:
            raise FixtureAdapterDenied("registry requires the exact persisted MediaEvidence identity")
        if candidate.catalog_source_id != _token("catalog", "e") or candidate.catalog_source_hash != _hash("e"):
            raise FixtureAdapterDenied("catalog source ID/hash do not match registration")
        if candidate.profile is not SemanticProfile.TEST:
            raise FixtureAdapterDenied("candidate profile does not match registration")
        if candidate.evidence.content_hash != canonical_sha256(media_evidence.to_json()):
            raise FixtureAdapterDenied("media evidence artifact hash does not match registration")
        if media_evidence.pts_index_sha256 != canonical_sha256(list(media_evidence.pts_index.ticks)):
            raise FixtureAdapterDenied("PTS index hash does not match registration")
        if media_evidence.source.sha256 != _hash("a"):
            raise FixtureAdapterDenied("source hash does not match registration")
        if (media_evidence.fixture_manifest_sha256, media_evidence.fixture_sidecar_sha256) != (_hash("c"), _hash("d")):
            raise FixtureAdapterDenied("fixture hashes do not match registration")
        return CatalogResolution(candidate, candidate.evidence)


def _registered() -> tuple[FixtureCatalogAdapter, SemanticChain, CatalogCandidateRef, MediaEvidence]:
    media = _media()
    evidence = EvidenceRef(_token("evidence", "a"), canonical_sha256(media.to_json()))
    candidate = CatalogCandidateRef(_token("candidate", "a"), _token("catalog", "e"), _hash("e"), evidence, SemanticProfile.TEST)
    chain = SemanticChainBuilder().build(
        SemanticChainInput(SemanticProfile.TEST, (evidence,), (RegisteredFact(_token("fact", "a"), FactKind.OBSERVATION, evidence, candidate),))
    )
    return FixtureCatalogAdapter(_Loader(media), _Registry(candidate, media)), chain, candidate, media


def test_adapter_resolves_only_candidate_bound_through_the_semantic_chain() -> None:
    adapter, chain, candidate, _ = _registered()
    assert adapter.resolve(chain, candidate) == CatalogResolution(candidate, candidate.evidence)
    foreign = CatalogCandidateRef(_token("candidate", "b"), _token("catalog", "e"), _hash("e"), candidate.evidence, SemanticProfile.TEST)
    with pytest.raises(FixtureAdapterDenied, match="exact catalog candidate"):
        adapter.resolve(chain, foreign)


def test_adapter_rejects_forged_self_consistent_evidence_without_trusted_provenance_identity() -> None:
    _, chain, candidate, media = _registered()
    forged = replace(media)
    adapter = FixtureCatalogAdapter(_Loader(forged), _Registry(candidate, media))
    with pytest.raises(FixtureAdapterDenied, match="exact persisted MediaEvidence identity"):
        adapter.resolve(chain, candidate)


def test_adapter_accepts_only_an_opaque_catalog_reference() -> None:
    adapter, chain, candidate, _ = _registered()
    with pytest.raises(FixtureAdapterDenied):
        adapter.resolve(chain, {"candidate_id": candidate.candidate_id})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        adapter.resolve(chain, candidate, desired_start_pts=999)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        adapter.resolve(chain, candidate, {"path": "fixture.mp4"})  # type: ignore[call-arg]
    assert "pts" not in repr(chain.to_mapping()).lower()
