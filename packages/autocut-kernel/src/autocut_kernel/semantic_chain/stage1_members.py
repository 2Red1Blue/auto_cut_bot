"""Decode the six coverage business members without granting Store authority."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.compiler.canonical import load_canonical_json_bytes
from ..store.models import ArtifactMember, ArtifactScope
from .diagnostic_models import ConflictDiagnostics, EvidenceDiagnostics
from .ledger_models import CoverageLedger
from .member_refs import SemanticMemberIdentity
from .narrative_models import EpisodeDigestSet, EventCardSet, NarrativeGraph

COVERAGE_MEMBER_TYPES = (
    "event_card_set", "episode_digest_set", "narrative_graph",
    "evidence_diagnostics", "conflict_diagnostics", "coverage_ledger",
)


@dataclass(frozen=True, slots=True)
class Stage1CoverageValues:
    members: tuple[ArtifactMember, ...]
    identities: tuple[SemanticMemberIdentity, ...]
    event_cards: EventCardSet
    episode_digests: EpisodeDigestSet
    narrative_graph: NarrativeGraph
    evidence_diagnostics: EvidenceDiagnostics
    conflict_diagnostics: ConflictDiagnostics
    coverage_ledger: CoverageLedger

    def identity(self, kind: str) -> SemanticMemberIdentity:
        for item in self.identities:
            if item.artifact_type == kind:
                return item
        raise ValueError("unknown coverage member identity")


def decode_coverage_members(
    members: tuple[ArtifactMember, ...], *, scope: ArtifactScope,
) -> Stage1CoverageValues:
    """Validate exact type/logical ID/hash/scope/revision and each closed payload.

    Does not prove that supplied content matches raw inputs, diagnostics are
    truthful, or that a database ever committed these values.
    """
    if type(members) is not tuple or any(type(item) is not ArtifactMember for item in members):  # noqa: E721
        raise ValueError("coverage members must be exact ArtifactMember values")
    if len(members) != len(COVERAGE_MEMBER_TYPES) or {item.artifact_type for item in members} != set(COVERAGE_MEMBER_TYPES):
        raise ValueError("coverage requires exactly six distinct business members")
    if type(scope) is not ArtifactScope or any(  # noqa: E721
        item.scope != scope or item.logical_id != item.artifact_type for item in members
    ):
        raise ValueError("coverage member scope or logical identity mismatch")
    if len({item.revision for item in members}) != 1:
        raise ValueError("coverage output revisions must agree")
    ordered = tuple(sorted(members, key=lambda item: COVERAGE_MEMBER_TYPES.index(item.artifact_type)))
    identities = tuple(SemanticMemberIdentity.from_artifact_member(item) for item in ordered)
    payloads: dict[str, object] = {}
    for item in ordered:
        # The generic Store hash is intentionally not a strict schema decoder:
        # reject duplicate JSON keys/floats here before they can be normalized.
        payload, _ = load_canonical_json_bytes(item.payload_json.encode("utf-8"), origin=item.artifact_type)
        payloads[item.artifact_type] = payload
    return Stage1CoverageValues(
        ordered, identities,
        EventCardSet.from_mapping(payloads["event_card_set"]),
        EpisodeDigestSet.from_mapping(payloads["episode_digest_set"]),
        NarrativeGraph.from_mapping(payloads["narrative_graph"]),
        EvidenceDiagnostics.from_mapping(payloads["evidence_diagnostics"]),
        ConflictDiagnostics.from_mapping(payloads["conflict_diagnostics"]),
        CoverageLedger.from_mapping(payloads["coverage_ledger"]),
    )
