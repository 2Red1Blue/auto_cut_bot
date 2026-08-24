"""Typed one-way adapter from committed VLM evidence into the semantic chain."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..media.types import TimeBase, canonical_sha256
from ..semantic_chain import (
    CatalogCandidateRef,
    EvidenceRef,
    FactKind,
    RegisteredFact,
    SemanticChainDenied,
    SemanticChainInput,
    SemanticProfile,
)
from ..store import ArtifactMember
from ..store.models import canonical_payload_hash
from ..vlm import (
    MappedSourceInterval,
    VlmObservationSet,
    VlmRequestIdentity,
    WindowManifest,
)


@dataclass(frozen=True, slots=True)
class VlmCandidateCatalogEntry:
    """Trusted provenance plus untrusted semantic text; never a cut endpoint."""

    candidate_id: str
    observation_id: str
    kind: FactKind
    summary: str
    supporting_frame_ids: tuple[str, ...]
    source_id: str
    source_sha256: str
    source_clock_id: str
    source_time_base: TimeBase
    coarse_interval: MappedSourceInterval
    evidence: EvidenceRef

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "coarse_interval": self.coarse_interval.to_mapping(),
            "evidence": self.evidence.to_mapping(),
            "kind": self.kind.value,
            "observation_id": self.observation_id,
            "source_clock_id": self.source_clock_id,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "source_time_base": {
                "numerator": self.source_time_base.numerator,
                "denominator": self.source_time_base.denominator,
            },
            "summary": self.summary,
            "summary_trust": "untrusted_provider_data",
            "supporting_frame_ids": list(self.supporting_frame_ids),
        }


@dataclass(frozen=True, slots=True)
class VlmCandidateCatalog:
    catalog_source_id: str
    catalog_source_hash: str
    request_identity_sha256: str
    window_manifest_sha256: str
    observation_artifact: EvidenceRef
    entries: tuple[VlmCandidateCatalogEntry, ...]

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        if not entries:
            raise SemanticChainDenied("VLM candidate catalog requires owned observations")
        if tuple(sorted(entries, key=lambda item: item.candidate_id)) != entries:
            raise SemanticChainDenied("VLM candidate catalog entries must be canonical")
        if len({item.candidate_id for item in entries}) != len(entries):
            raise SemanticChainDenied("VLM candidate catalog entries must be unique")
        object.__setattr__(self, "entries", entries)

    def to_mapping(self) -> dict[str, object]:
        return {
            "catalog_source_hash": self.catalog_source_hash,
            "catalog_source_id": self.catalog_source_id,
            "entries": [item.to_mapping() for item in self.entries],
            "observation_artifact": self.observation_artifact.to_mapping(),
            "request_identity_sha256": self.request_identity_sha256,
            "window_manifest_sha256": self.window_manifest_sha256,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class VlmSemanticAdapterResult:
    semantic_input: SemanticChainInput
    candidate_catalog: VlmCandidateCatalog


def adapt_vlm_observations(
    *,
    profile: SemanticProfile,
    manifest: WindowManifest,
    request_identity: VlmRequestIdentity,
    observation_set: VlmObservationSet,
    observation_artifact: ArtifactMember,
) -> VlmSemanticAdapterResult:
    """Admit only globally-owned observations from one exact committed artifact."""

    if profile is SemanticProfile.PRODUCTION:
        raise SemanticChainDenied(
            "production semantic admission remains closed until production evaluators are wired"
        )
    if type(manifest) is not WindowManifest:  # noqa: E721
        raise SemanticChainDenied("manifest must be a WindowManifest")
    if type(request_identity) is not VlmRequestIdentity:  # noqa: E721
        raise SemanticChainDenied("request_identity must be a VlmRequestIdentity")
    if type(observation_set) is not VlmObservationSet:  # noqa: E721
        raise SemanticChainDenied("observation_set must be a VlmObservationSet")
    if type(observation_artifact) is not ArtifactMember:  # noqa: E721
        raise SemanticChainDenied("observation_artifact must be an ArtifactMember")
    if observation_artifact.artifact_type != "vlm_observation_set":
        raise SemanticChainDenied("observation artifact type must be vlm_observation_set")
    if not observation_artifact.logical_id.startswith("evidence_"):
        raise SemanticChainDenied("observation artifact must use the evidence identity grammar")
    if observation_set.request_identity_sha256 != request_identity.canonical_hash:
        raise SemanticChainDenied("observation set request identity mismatch")
    if observation_set.window_manifest_sha256 != manifest.canonical_hash:
        raise SemanticChainDenied("observation set window identity mismatch")
    try:
        artifact_payload = json.loads(observation_artifact.payload_json)
    except (TypeError, ValueError) as error:
        raise SemanticChainDenied("observation artifact payload is invalid JSON") from error
    if artifact_payload != observation_set.to_mapping():
        raise SemanticChainDenied("observation artifact payload does not match the exact set")
    if canonical_payload_hash(observation_artifact.payload_json) != observation_artifact.content_hash:
        raise SemanticChainDenied("observation artifact content hash mismatch")

    evidence = EvidenceRef(
        observation_artifact.logical_id,
        observation_artifact.content_hash,
    )
    owned = tuple(item for item in observation_set.observations if item.core_owned)
    if not owned:
        raise SemanticChainDenied("no globally-owned VLM observations are available")
    catalog_source_hash = canonical_sha256(
        {
            "artifact": evidence.to_mapping(),
            "observation_set_sha256": observation_set.canonical_hash,
            "request_identity_sha256": request_identity.canonical_hash,
        }
    )
    catalog_source_id = f"catalog_{catalog_source_hash[7:39]}"
    entries: list[VlmCandidateCatalogEntry] = []
    facts: list[RegisteredFact] = []
    for observation in owned:
        candidate_hash = canonical_sha256(
            {
                "catalog_source_hash": catalog_source_hash,
                "observation_id": observation.observation_id,
            }
        )
        candidate_id = f"candidate_{candidate_hash[7:39]}"
        kind = FactKind(observation.kind.value)
        candidate = CatalogCandidateRef(
            candidate_id,
            catalog_source_id,
            catalog_source_hash,
            evidence,
            profile,
        )
        fact_hash = canonical_sha256(
            {"candidate_id": candidate_id, "observation_id": observation.observation_id}
        )
        facts.append(
            RegisteredFact(
                f"fact_{fact_hash[7:39]}",
                kind,
                evidence,
                candidate,
            )
        )
        entries.append(
            VlmCandidateCatalogEntry(
                candidate_id,
                observation.observation_id,
                kind,
                observation.summary,
                observation.supporting_frame_ids,
                manifest.source_id,
                manifest.source_sha256,
                manifest.source_clock_id,
                manifest.source_time_base,
                observation.source_interval,
                evidence,
            )
        )
    canonical_entries = tuple(sorted(entries, key=lambda item: item.candidate_id))
    catalog = VlmCandidateCatalog(
        catalog_source_id,
        catalog_source_hash,
        request_identity.canonical_hash,
        manifest.canonical_hash,
        evidence,
        canonical_entries,
    )
    semantic_input = SemanticChainInput(
        profile,
        (evidence,),
        tuple(sorted(facts, key=lambda item: item.fact_id)),
    )
    return VlmSemanticAdapterResult(semantic_input, catalog)


__all__ = [
    "VlmCandidateCatalog",
    "VlmCandidateCatalogEntry",
    "VlmSemanticAdapterResult",
    "adapt_vlm_observations",
]
