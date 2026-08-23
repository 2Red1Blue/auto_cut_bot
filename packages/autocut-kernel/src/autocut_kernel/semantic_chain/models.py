"""Closed, dependency-free values for the semantic narrative MVP.

The semantic chain intentionally carries only registered opaque identities and
evidence digests.  It is not a transport for prose, media locations, clocks,
or ranking data.  Every record is immutable and has a deterministic canonical
JSON representation suitable for content-addressed persistence by a later
layer.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

_SHA256: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TOKEN: Final = re.compile(r"[a-z]+_[0-9a-f]{32}\Z")
_EVIDENCE_ARTIFACT_ID: Final = re.compile(r"(?:media_evidence|evidence_[0-9a-f]{32})\Z")


class SemanticChainError(ValueError):
    """Base error for a denied semantic-chain input or construction."""


class SemanticChainDenied(SemanticChainError):  # noqa: N818 - outcome vocabulary is intentional.
    """A request does not meet the closed MVP admission rules."""


class ProductionProfileDenied(SemanticChainDenied):
    """The pure semantic MVP cannot run under a production profile."""


class SemanticProfile(str, Enum):
    """The only runtime profiles admitted by the semantic MVP."""

    TEST = "test"
    SHADOW = "shadow"
    PRODUCTION = "production"


class FactKind(str, Enum):
    """Closed semantic fact classifications; values are not user prose."""

    OBSERVATION = "observation"
    CHANGE = "change"
    RELATION = "relation"


class EventKind(str, Enum):
    """Closed event classifications deterministically derived from facts."""

    OBSERVATION = "observation"
    CHANGE = "change"
    RELATION = "relation"


class BeatRole(str, Enum):
    """Closed blueprint beat roles."""

    SETUP = "setup"
    ESCALATION = "escalation"
    PAYOFF = "payoff"


def _opaque_id(value: object, field_name: str, prefix: str) -> str:
    """Validate a typed, non-semantic opaque token.

    Evidence artifact IDs cross into the trusted provenance loader, so they
    must be as non-semantic as catalog candidate/source identities. Fact IDs
    do not cross that boundary, but use the same grammar to keep the persisted
    semantic artifacts incapable of transporting paths, clocks, or scores.
    """

    if type(value) is not str or not _TOKEN.fullmatch(value) or not value.startswith(f"{prefix}_"):  # noqa: E721
        raise SemanticChainDenied(f"{field_name} must be a {prefix}_<32 lowercase-hex> opaque token")
    return value


def _evidence_artifact_id(value: object) -> str:
    """Accept only trusted local-media's canonical ID or a future digest token.

    ``media_evidence`` is a persisted logical ID emitted by local-media,
    rather than caller prose. Other evidence records must use a typed digest
    token, so paths, PTS values, float seconds, and scores remain denied.
    """

    if type(value) is not str or not _EVIDENCE_ARTIFACT_ID.fullmatch(value):  # noqa: E721
        raise SemanticChainDenied("evidence.artifact_id must be media_evidence or evidence_<32 lowercase-hex>")
    return value


def _sha256(value: object, field_name: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):  # noqa: E721
        raise SemanticChainDenied(f"{field_name} must be a lowercase sha256 digest")
    return value


def canonical_json_bytes(value: object) -> bytes:
    """Return deterministic JSON bytes for closed, scalar-only artifact maps."""

    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    except (TypeError, ValueError) as error:
        raise SemanticChainDenied("semantic artifact is not canonically serializable") from error


def canonical_sha256(value: object) -> str:
    """Return the stable content digest for a canonical semantic artifact map."""

    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def _unique(values: tuple[str, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise SemanticChainDenied(f"{field_name} must not contain duplicates")


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """An opaque immutable source artifact identity."""

    artifact_id: str
    content_hash: str

    def __post_init__(self) -> None:
        _evidence_artifact_id(self.artifact_id)
        _sha256(self.content_hash, "evidence.content_hash")

    def to_mapping(self) -> dict[str, str]:
        return {"artifact_id": self.artifact_id, "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class CatalogCandidateRef:
    """An opaque catalog candidate selected by a registered source fact.

    This is deliberately a reference, not a cut request: it has no location,
    timing, mapping, or media payload.  The catalog source hash makes a
    candidate name meaningful only within one immutable catalog revision.
    """

    candidate_id: str
    catalog_source_id: str
    catalog_source_hash: str
    evidence: EvidenceRef
    profile: SemanticProfile

    def __post_init__(self) -> None:
        _opaque_id(self.candidate_id, "candidate.candidate_id", "candidate")
        _opaque_id(self.catalog_source_id, "candidate.catalog_source_id", "catalog")
        _sha256(self.catalog_source_hash, "candidate.catalog_source_hash")
        if type(self.evidence) is not EvidenceRef:  # noqa: E721
            raise SemanticChainDenied("candidate.evidence must be an EvidenceRef")
        if type(self.profile) is not SemanticProfile or self.profile is SemanticProfile.PRODUCTION:  # noqa: E721
            raise SemanticChainDenied("candidate.profile must be a non-production SemanticProfile")

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "catalog_source_hash": self.catalog_source_hash,
            "catalog_source_id": self.catalog_source_id,
            "evidence": self.evidence.to_mapping(),
            "profile": self.profile.value,
        }


@dataclass(frozen=True, slots=True)
class RegisteredFact:
    """One typed fact whose evidence must be registered by the chain input."""

    fact_id: str
    kind: FactKind
    evidence: EvidenceRef
    candidate: CatalogCandidateRef

    def __post_init__(self) -> None:
        _opaque_id(self.fact_id, "fact.fact_id", "fact")
        if type(self.kind) is not FactKind:  # noqa: E721
            raise SemanticChainDenied("fact.kind must be a FactKind")
        if type(self.evidence) is not EvidenceRef:  # noqa: E721
            raise SemanticChainDenied("fact.evidence must be an EvidenceRef")
        if type(self.candidate) is not CatalogCandidateRef:  # noqa: E721
            raise SemanticChainDenied("fact.candidate must be a CatalogCandidateRef")
        if self.candidate.evidence != self.evidence:
            raise SemanticChainDenied("fact candidate must bind its exact evidence")

    def to_mapping(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.to_mapping(),
            "evidence": self.evidence.to_mapping(),
            "fact_id": self.fact_id,
            "kind": self.kind.value,
        }


@dataclass(frozen=True, slots=True)
class SemanticChainInput:
    """The only admissible source for deterministic semantic-chain building."""

    profile: SemanticProfile
    evidence: tuple[EvidenceRef, ...]
    facts: tuple[RegisteredFact, ...]

    def __post_init__(self) -> None:
        if type(self.profile) is not SemanticProfile:  # noqa: E721
            raise SemanticChainDenied("input.profile must be a SemanticProfile")
        if self.profile is SemanticProfile.PRODUCTION:
            raise ProductionProfileDenied("production profile is denied for the semantic MVP")
        evidence = tuple(self.evidence)
        facts = tuple(self.facts)
        if not evidence:
            raise SemanticChainDenied("input.evidence must not be empty")
        if not facts:
            raise SemanticChainDenied("input.facts must not be empty")
        if any(type(item) is not EvidenceRef for item in evidence):  # noqa: E721
            raise SemanticChainDenied("input.evidence must contain EvidenceRef values")
        if any(type(item) is not RegisteredFact for item in facts):  # noqa: E721
            raise SemanticChainDenied("input.facts must contain RegisteredFact values")
        evidence_keys = tuple(f"{item.artifact_id}:{item.content_hash}" for item in evidence)
        _unique(evidence_keys, "input.evidence")
        _unique(tuple(item.fact_id for item in facts), "input.facts")
        registered = {(item.artifact_id, item.content_hash) for item in evidence}
        if any((fact.evidence.artifact_id, fact.evidence.content_hash) not in registered for fact in facts):
            raise SemanticChainDenied("fact evidence is missing from the registered input evidence")
        if any(fact.candidate.profile is not self.profile for fact in facts):
            raise SemanticChainDenied("fact candidates must bind the input profile")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "facts", facts)

    def to_mapping(self) -> dict[str, object]:
        return {
            "evidence": [
                item.to_mapping()
                for item in sorted(self.evidence, key=lambda item: (item.artifact_id, item.content_hash))
            ],
            "facts": [item.to_mapping() for item in sorted(self.facts, key=lambda item: item.fact_id)],
            "profile": self.profile.value,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class NarrativeNode:
    node_id: str
    fact_id: str
    kind: FactKind
    evidence: EvidenceRef
    candidate: CatalogCandidateRef

    def __post_init__(self) -> None:
        _opaque_id(self.node_id, "narrative_node.node_id", "node")
        _opaque_id(self.fact_id, "narrative_node.fact_id", "fact")
        if type(self.kind) is not FactKind:  # noqa: E721
            raise SemanticChainDenied("narrative_node.kind must be a FactKind")
        if type(self.evidence) is not EvidenceRef:  # noqa: E721
            raise SemanticChainDenied("narrative_node.evidence must be an EvidenceRef")
        if type(self.candidate) is not CatalogCandidateRef or self.candidate.evidence != self.evidence:  # noqa: E721
            raise SemanticChainDenied("narrative_node must bind its exact candidate evidence")

    def to_mapping(self) -> dict[str, object]:
        return {"candidate": self.candidate.to_mapping(), "evidence": self.evidence.to_mapping(), "fact_id": self.fact_id, "kind": self.kind.value, "node_id": self.node_id}


@dataclass(frozen=True, slots=True)
class NarrativeGraph:
    narrative_id: str
    profile: SemanticProfile
    nodes: tuple[NarrativeNode, ...]
    input_hash: str

    def __post_init__(self) -> None:
        _opaque_id(self.narrative_id, "narrative.narrative_id", "narrative")
        if type(self.profile) is not SemanticProfile or self.profile is SemanticProfile.PRODUCTION:  # noqa: E721
            raise SemanticChainDenied("narrative.profile must be a non-production SemanticProfile")
        nodes = tuple(self.nodes)
        if not nodes or any(type(item) is not NarrativeNode for item in nodes):  # noqa: E721
            raise SemanticChainDenied("narrative.nodes must be non-empty NarrativeNode values")
        _unique(tuple(item.node_id for item in nodes), "narrative.nodes")
        _sha256(self.input_hash, "narrative.input_hash")
        object.__setattr__(self, "nodes", nodes)

    def to_mapping(self) -> dict[str, object]:
        return {"input_hash": self.input_hash, "narrative_id": self.narrative_id, "nodes": [item.to_mapping() for item in self.nodes], "profile": self.profile.value}

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class EventCard:
    event_id: str
    node_id: str
    kind: EventKind
    evidence: EvidenceRef
    candidate: CatalogCandidateRef

    def __post_init__(self) -> None:
        _opaque_id(self.event_id, "event.event_id", "event")
        _opaque_id(self.node_id, "event.node_id", "node")
        if type(self.kind) is not EventKind or type(self.evidence) is not EvidenceRef:  # noqa: E721
            raise SemanticChainDenied("event must contain an EventKind and EvidenceRef")
        if type(self.candidate) is not CatalogCandidateRef or self.candidate.evidence != self.evidence:  # noqa: E721
            raise SemanticChainDenied("event must bind its exact candidate evidence")

    def to_mapping(self) -> dict[str, object]:
        return {"candidate": self.candidate.to_mapping(), "evidence": self.evidence.to_mapping(), "event_id": self.event_id, "kind": self.kind.value, "node_id": self.node_id}


@dataclass(frozen=True, slots=True)
class Story:
    story_id: str
    narrative_hash: str
    profile: SemanticProfile
    events: tuple[EventCard, ...]

    def __post_init__(self) -> None:
        _opaque_id(self.story_id, "story.story_id", "story")
        _sha256(self.narrative_hash, "story.narrative_hash")
        if type(self.profile) is not SemanticProfile or self.profile is SemanticProfile.PRODUCTION:  # noqa: E721
            raise SemanticChainDenied("story.profile must be a non-production SemanticProfile")
        events = tuple(self.events)
        if not events or any(type(item) is not EventCard for item in events):  # noqa: E721
            raise SemanticChainDenied("story.events must be non-empty EventCard values")
        _unique(tuple(item.event_id for item in events), "story.events")
        object.__setattr__(self, "events", events)

    def to_mapping(self) -> dict[str, object]:
        return {"events": [item.to_mapping() for item in self.events], "narrative_hash": self.narrative_hash, "profile": self.profile.value, "story_id": self.story_id}

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class BlueprintBeat:
    beat_id: str
    event_id: str
    role: BeatRole
    evidence: EvidenceRef
    candidate: CatalogCandidateRef

    def __post_init__(self) -> None:
        _opaque_id(self.beat_id, "blueprint_beat.beat_id", "beat")
        _opaque_id(self.event_id, "blueprint_beat.event_id", "event")
        if type(self.role) is not BeatRole or type(self.evidence) is not EvidenceRef:  # noqa: E721
            raise SemanticChainDenied("blueprint beat must contain a BeatRole and EvidenceRef")
        if type(self.candidate) is not CatalogCandidateRef or self.candidate.evidence != self.evidence:  # noqa: E721
            raise SemanticChainDenied("blueprint beat must bind its exact candidate evidence")

    def to_mapping(self) -> dict[str, object]:
        return {"beat_id": self.beat_id, "candidate": self.candidate.to_mapping(), "event_id": self.event_id, "evidence": self.evidence.to_mapping(), "role": self.role.value}


@dataclass(frozen=True, slots=True)
class EditorialBlueprint:
    blueprint_id: str
    story_hash: str
    profile: SemanticProfile
    beats: tuple[BlueprintBeat, ...]

    def __post_init__(self) -> None:
        _opaque_id(self.blueprint_id, "blueprint.blueprint_id", "blueprint")
        _sha256(self.story_hash, "blueprint.story_hash")
        if type(self.profile) is not SemanticProfile or self.profile is SemanticProfile.PRODUCTION:  # noqa: E721
            raise SemanticChainDenied("blueprint.profile must be a non-production SemanticProfile")
        beats = tuple(self.beats)
        if not beats or any(type(item) is not BlueprintBeat for item in beats):  # noqa: E721
            raise SemanticChainDenied("blueprint.beats must be non-empty BlueprintBeat values")
        _unique(tuple(item.beat_id for item in beats), "blueprint.beats")
        object.__setattr__(self, "beats", beats)

    def to_mapping(self) -> dict[str, object]:
        return {"beats": [item.to_mapping() for item in self.beats], "blueprint_id": self.blueprint_id, "profile": self.profile.value, "story_hash": self.story_hash}

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())
