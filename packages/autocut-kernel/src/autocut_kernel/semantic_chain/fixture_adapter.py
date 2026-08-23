"""Closed test-fixture bridge from semantic identities to physical-edit PTS.

PTS values are deliberately kept inside this module's registered catalog.  A
semantic chain can name a candidate, but it cannot carry or override that
candidate's timing values.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..media.types import MediaEvidence, canonical_sha256
from ..physical_edit import FixtureBeatInput
from .chain import SemanticChain
from .models import EvidenceRef, SemanticChainDenied, SemanticProfile, _opaque_id


class FixtureAdapterDenied(SemanticChainDenied):
    """A fixture-catalog resolution did not meet its closed admission rules."""


def _non_production_profile(value: object, field_name: str) -> SemanticProfile:
    if type(value) is not SemanticProfile or value is SemanticProfile.PRODUCTION:  # noqa: E721
        raise FixtureAdapterDenied(f"{field_name} must be a non-production SemanticProfile")
    return value


@dataclass(frozen=True, slots=True)
class FixtureCandidateBinding:
    """Opaque semantic identity for one catalog-owned fixture candidate."""

    candidate_id: str
    evidence: EvidenceRef
    profile: SemanticProfile
    catalog_source_id: str

    def __post_init__(self) -> None:
        _opaque_id(self.candidate_id, "candidate.candidate_id")
        if type(self.evidence) is not EvidenceRef:  # noqa: E721
            raise FixtureAdapterDenied("candidate.evidence must be an EvidenceRef")
        _non_production_profile(self.profile, "candidate.profile")
        _opaque_id(self.catalog_source_id, "candidate.catalog_source_id")


@dataclass(frozen=True, slots=True)
class _FixtureCatalogEntry:
    """One private catalog record; its PTS values never cross the semantic API."""

    binding: FixtureCandidateBinding
    beat: FixtureBeatInput

    def __post_init__(self) -> None:
        if type(self.binding) is not FixtureCandidateBinding:  # noqa: E721
            raise FixtureAdapterDenied("catalog entry must contain a FixtureCandidateBinding")
        if type(self.beat) is not FixtureBeatInput:  # noqa: E721
            raise FixtureAdapterDenied("catalog entry must contain a FixtureBeatInput")


@dataclass(frozen=True, slots=True)
class FixtureCatalog:
    """Closed in-memory registration of fixture-only candidate timing records."""

    catalog_source_id: str
    profile: SemanticProfile
    _entries: tuple[_FixtureCatalogEntry, ...]

    def __post_init__(self) -> None:
        _opaque_id(self.catalog_source_id, "catalog.catalog_source_id")
        _non_production_profile(self.profile, "catalog.profile")
        entries = tuple(self._entries)
        if not entries or any(type(item) is not _FixtureCatalogEntry for item in entries):  # noqa: E721
            raise FixtureAdapterDenied(
                "catalog entries must be non-empty registered fixture entries"
            )
        candidate_ids = tuple(item.binding.candidate_id for item in entries)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise FixtureAdapterDenied("catalog candidate identifiers must be unique")
        if any(
            item.binding.catalog_source_id != self.catalog_source_id
            or item.binding.profile is not self.profile
            for item in entries
        ):
            raise FixtureAdapterDenied("catalog entries must bind this exact source and profile")
        object.__setattr__(self, "_entries", entries)

    @classmethod
    def registered(
        cls,
        catalog_source_id: str,
        profile: SemanticProfile,
        registrations: tuple[tuple[FixtureCandidateBinding, FixtureBeatInput], ...],
    ) -> FixtureCatalog:
        """Create the only catalog construction path, from typed registrations."""

        return cls(
            catalog_source_id,
            profile,
            tuple(_FixtureCatalogEntry(binding, beat) for binding, beat in registrations),
        )

    def _resolve(self, binding: FixtureCandidateBinding) -> FixtureBeatInput:
        for entry in self._entries:
            if entry.binding == binding:
                return entry.beat
        raise FixtureAdapterDenied("candidate is not registered by this fixture catalog")


@dataclass(frozen=True, slots=True)
class FixtureCatalogAdapter:
    """Resolve a semantic candidate only against its exact committed fixture evidence."""

    catalog: FixtureCatalog

    def __post_init__(self) -> None:
        if type(self.catalog) is not FixtureCatalog:  # noqa: E721
            raise FixtureAdapterDenied("adapter requires a FixtureCatalog")

    def resolve(
        self,
        chain: SemanticChain,
        media_evidence: MediaEvidence,
        evidence_artifact: EvidenceRef,
        candidate: FixtureCandidateBinding,
    ) -> FixtureBeatInput:
        """Return catalog PTS only after exact chain, evidence, and source binding checks."""

        if type(chain) is not SemanticChain:  # noqa: E721
            raise FixtureAdapterDenied("adapter requires a SemanticChain")
        if type(media_evidence) is not MediaEvidence:  # noqa: E721
            raise FixtureAdapterDenied("adapter requires committed MediaEvidence")
        if type(evidence_artifact) is not EvidenceRef:  # noqa: E721
            raise FixtureAdapterDenied("adapter requires an EvidenceRef artifact identity")
        if type(candidate) is not FixtureCandidateBinding:  # noqa: E721
            raise FixtureAdapterDenied("adapter requires a FixtureCandidateBinding")
        if chain.profile is SemanticProfile.PRODUCTION:
            raise FixtureAdapterDenied(
                "production profile is denied for fixture catalog resolution"
            )
        if not (chain.profile is self.catalog.profile is candidate.profile):
            raise FixtureAdapterDenied("chain, catalog, and candidate profiles must match exactly")
        if candidate.catalog_source_id != self.catalog.catalog_source_id:
            raise FixtureAdapterDenied("candidate catalog source does not match this catalog")
        if candidate.evidence != evidence_artifact:
            raise FixtureAdapterDenied("candidate must bind the exact committed evidence artifact")
        if evidence_artifact.content_hash != canonical_sha256(media_evidence.to_json()):
            raise FixtureAdapterDenied(
                "evidence artifact hash does not match committed MediaEvidence"
            )
        if not any(beat.evidence == evidence_artifact for beat in chain.blueprint.beats):
            raise FixtureAdapterDenied(
                "semantic chain does not bind the committed evidence artifact"
            )
        return self.catalog._resolve(candidate)
