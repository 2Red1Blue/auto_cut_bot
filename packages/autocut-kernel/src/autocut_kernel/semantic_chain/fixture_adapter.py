"""Boundary interfaces for resolving semantic catalog references in fixtures.

The semantic package deliberately does not own a physical-edit catalog. A
trusted persistence adapter loads committed ``MediaEvidence`` before a catalog
service can resolve an opaque candidate reference. Python object privacy is not
a hostile-code security boundary; these interfaces make provenance checks
explicit at the integration edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..media.types import MediaEvidence, canonical_sha256
from .chain import SemanticChain
from .models import CatalogCandidateRef, EvidenceRef, SemanticChainDenied, SemanticProfile


class FixtureAdapterDenied(SemanticChainDenied):
    """A fixture-catalog resolution did not meet its closed admission rules."""


@dataclass(frozen=True, slots=True)
class CatalogResolution:
    """Opaque confirmation that a catalog candidate was resolved.

    Timing and physical-edit payloads remain in the registry-owning layer and
    are not exposed by this semantic public API.
    """

    candidate: CatalogCandidateRef
    evidence_artifact: EvidenceRef

    def __post_init__(self) -> None:
        if type(self.candidate) is not CatalogCandidateRef:  # noqa: E721
            raise FixtureAdapterDenied("resolution requires a CatalogCandidateRef")
        if type(self.evidence_artifact) is not EvidenceRef:  # noqa: E721
            raise FixtureAdapterDenied("resolution requires an EvidenceRef")
        if self.candidate.evidence != self.evidence_artifact:
            raise FixtureAdapterDenied("resolution must bind the candidate evidence artifact")


@runtime_checkable
class PersistedMediaEvidenceLoader(Protocol):
    """Trusted persistence boundary that returns the exact persisted evidence object."""

    def load_exact(self, evidence_artifact: EvidenceRef) -> MediaEvidence:
        """Load the exact persisted evidence identified by ``evidence_artifact``."""
        raise NotImplementedError


@runtime_checkable
class FixtureCandidateRegistry(Protocol):
    """Fixture-owned service for opaque candidate resolution.

    Implementations must reject a merely self-consistent object that is not
    their exact persisted evidence record. They must check catalog source
    ID/hash, profile, media-evidence artifact hash, PTS-index hash, source
    hash, and fixture manifest/sidecar hashes before returning a resolution.
    """

    def resolve_exact(
        self,
        candidate: CatalogCandidateRef,
        media_evidence: MediaEvidence,
    ) -> CatalogResolution:
        """Resolve ``candidate`` only for the exact trusted media evidence."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class FixtureCatalogAdapter:
    """Bind a semantic candidate to trusted persisted evidence before registry lookup."""

    provenance_loader: PersistedMediaEvidenceLoader
    registry: FixtureCandidateRegistry

    def __post_init__(self) -> None:
        if not callable(getattr(self.provenance_loader, "load_exact", None)):
            raise FixtureAdapterDenied("adapter requires a persisted evidence loader")
        if not callable(getattr(self.registry, "resolve_exact", None)):
            raise FixtureAdapterDenied("adapter requires a fixture candidate registry")

    def resolve(self, chain: SemanticChain, candidate: CatalogCandidateRef) -> CatalogResolution:
        """Resolve only a candidate already carried by this exact semantic chain.

        Callers cannot supply PTS, a physical-edit input, a media mapping, or a
        path here. The trusted loader supplies committed media evidence and the
        registry verifies it against its persisted catalog registration.
        """

        if type(chain) is not SemanticChain:  # noqa: E721
            raise FixtureAdapterDenied("adapter requires a SemanticChain")
        if type(candidate) is not CatalogCandidateRef:  # noqa: E721
            raise FixtureAdapterDenied("adapter requires a CatalogCandidateRef")
        if chain.profile is SemanticProfile.PRODUCTION or candidate.profile is SemanticProfile.PRODUCTION:
            raise FixtureAdapterDenied("production profile is denied for fixture catalog resolution")
        if chain.profile is not candidate.profile:
            raise FixtureAdapterDenied("chain and candidate profiles must match exactly")
        if not any(beat.candidate == candidate for beat in chain.blueprint.beats):
            raise FixtureAdapterDenied("semantic chain does not bind the exact catalog candidate")
        media_evidence = self.provenance_loader.load_exact(candidate.evidence)
        if type(media_evidence) is not MediaEvidence:  # noqa: E721
            raise FixtureAdapterDenied("provenance loader must return committed MediaEvidence")
        if candidate.evidence.content_hash != canonical_sha256(media_evidence.to_json()):
            raise FixtureAdapterDenied("media evidence artifact hash does not match committed MediaEvidence")
        resolution = self.registry.resolve_exact(candidate, media_evidence)
        if type(resolution) is not CatalogResolution:  # noqa: E721
            raise FixtureAdapterDenied("registry must return a CatalogResolution")
        if resolution.candidate != candidate or resolution.evidence_artifact != candidate.evidence:
            raise FixtureAdapterDenied("registry resolution does not bind the requested candidate and evidence")
        return resolution
