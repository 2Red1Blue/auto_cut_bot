"""Stage 1 fact, graph, coverage, diagnostics, proof, and admission models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Final, Mapping, Sequence, cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..contracts.compiler.refs import ArtifactRef, DomainRef
from .production_common import (
    CanonicalModel,
    EvaluatorOwnedModel,
    PendingBusinessSet,
    ProductionModelError,
    RuleResult,
    TimeBaseValue,
    canonical_domain_refs,
    canonical_ids,
    canonical_values,
    computed_rule_results,
    domain_ref,
    identifier,
    integer,
    jcs_key,
    mapping,
    object_list,
    safe_token,
    text,
)


class NarrativeNodeType(str, Enum):
    FACT = "fact"
    EVENT = "event"
    BEAT = "beat"
    OBLIGATION = "obligation"
    STORY_THREAD = "story_thread"
    CHARACTER = "character"
    RELATIONSHIP = "relationship"
    QUESTION = "question"
    FORESHADOW = "foreshadow"


class CoverageUnitType(str, Enum):
    VLM_OBSERVATION = "vlm_observation"
    VLM_WINDOW = "vlm_window"
    EVENT = "event"
    OBLIGATION = "obligation"


class CoverageResolution(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    CONFLICTED = "conflicted"


class CoverageDisposition(str, Enum):
    NARRATIVE = "narrative"
    SUPPORTING = "supporting"
    INTENTIONALLY_EXCLUDED = "intentionally_excluded"
    UNASSIGNED = "unassigned"


@dataclass(frozen=True, slots=True)
class CoveragePolicy(CanonicalModel):
    policy_id: str
    coverage_mode: str

    def __post_init__(self) -> None:
        identifier(self.policy_id, "coverage policy_id")
        if self.coverage_mode not in {"strict_global", "dependency_scoped"}:
            raise ProductionModelError("coverage_mode is unknown")

    def to_mapping(self) -> dict[str, object]:
        return {"coverage_mode": self.coverage_mode, "policy_id": self.policy_id}


@dataclass(frozen=True, slots=True)
class DependencyPropagationPolicy(CanonicalModel):
    """Frozen Stage 1 policy defining the only graph arcs that propagate taint."""

    policy_id: str
    policy_version: str
    propagating_edge_types: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier(self.policy_id, "dependency policy_id")
        safe_token(self.policy_version, "dependency policy_version")
        values = tuple(self.propagating_edge_types)
        if not values or set(values) - _EDGE_TYPES:
            raise ProductionModelError("dependency policy contains an unknown edge type")
        if values != tuple(sorted(values, key=jcs_key)) or len(values) != len(set(values)):
            raise ProductionModelError("dependency edge types must be unique and canonical")
        object.__setattr__(self, "propagating_edge_types", values)

    def to_mapping(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "propagating_edge_types": list(self.propagating_edge_types),
        }


_EDGE_TYPES: Final = frozenset(
    {
        "supports",
        "satisfies",
        "requires",
        "precedes",
        "causes",
        "contradicts",
        "involves",
        "resolves",
    }
)
_ATTRIBUTE_FIELDS: Final[dict[str, set[str]]] = {
    "fact": {"attribute_type", "subject_node_id", "predicate", "value", "conflict_status"},
    "event": {
        "attribute_type",
        "event_card_ref",
        "episode_id",
        "summary",
        "source_range_refs",
    },
    "beat": {"attribute_type", "summary", "phase", "obligation_ids"},
    "obligation": {
        "attribute_type",
        "description",
        "required_fact_ids",
        "success_criteria",
    },
    "story_thread": {"attribute_type", "title", "premise", "obligation_ids"},
    "character": {"attribute_type", "canonical_name", "aliases", "state_fact_ids"},
    "relationship": {
        "attribute_type",
        "subject_node_id",
        "object_node_id",
        "relation_type",
    },
    "question": {"attribute_type", "text", "status", "answer_fact_ids"},
    "foreshadow": {"attribute_type", "setup_event_ids", "payoff_event_ids", "status"},
}


@dataclass(frozen=True, slots=True)
class SourceRangeRef(CanonicalModel):
    source_id: str
    clock_id: str
    time_base: TimeBaseValue
    in_tick: int
    out_tick: int
    artifact_ref: ArtifactRef

    def __post_init__(self) -> None:
        identifier(self.source_id, "source_range.source_id")
        identifier(self.clock_id, "source_range.clock_id")
        if type(self.time_base) is not TimeBaseValue:  # noqa: E721
            raise ProductionModelError("source_range.time_base is invalid")
        start = integer(self.in_tick, "source_range.in_tick")
        end = integer(self.out_tick, "source_range.out_tick", minimum=1)
        if start >= end:
            raise ProductionModelError("source range must be a non-empty [in,out) interval")
        if type(self.artifact_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("source_range.artifact_ref must be an ArtifactRef")

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_ref": self.artifact_ref.to_mapping(),
            "clock_id": self.clock_id,
            "in_tick": self.in_tick,
            "interval": "[in,out)",
            "out_tick": self.out_tick,
            "source_id": self.source_id,
            "time_base": self.time_base.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class EpisodeDigest(CanonicalModel):
    episode_id: str
    ordinal: int
    summary: str
    source_window_refs: tuple[DomainRef, ...]
    evidence_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        identifier(self.episode_id, "episode_id")
        integer(self.ordinal, "ordinal", minimum=1)
        text(self.summary, "summary")
        object.__setattr__(
            self,
            "source_window_refs",
            canonical_domain_refs(self.source_window_refs, "source_window_refs", nonempty=True),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            canonical_domain_refs(self.evidence_refs, "evidence_refs", nonempty=True),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
            "ordinal": self.ordinal,
            "source_window_refs": [item.to_mapping() for item in self.source_window_refs],
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class EpisodeDigestSet(CanonicalModel):
    episode_digest_set_id: str
    digests: tuple[EpisodeDigest, ...]

    def __post_init__(self) -> None:
        identifier(self.episode_digest_set_id, "episode_digest_set_id")
        digests = cast(
            tuple[EpisodeDigest, ...],
            canonical_values(self.digests, EpisodeDigest, "digests", nonempty=True),
        )
        if len({item.episode_id for item in digests}) != len(digests):
            raise ProductionModelError("digests episode IDs must be unique")
        if len({item.ordinal for item in digests}) != len(digests):
            raise ProductionModelError("digests ordinals must be unique")
        object.__setattr__(self, "digests", digests)

    def to_mapping(self) -> dict[str, object]:
        return {
            "digests": [item.to_mapping() for item in self.digests],
            "episode_digest_set_id": self.episode_digest_set_id,
        }


@dataclass(frozen=True, slots=True)
class EventCard(CanonicalModel):
    """Fact-layer event only; capability/editing fields are unrepresentable."""

    event_id: str
    episode_id: str
    content: str
    source_range_refs: tuple[SourceRangeRef, ...]
    evidence_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        identifier(self.event_id, "event_id")
        identifier(self.episode_id, "episode_id")
        text(self.content, "content")
        object.__setattr__(
            self,
            "source_range_refs",
            cast(
                tuple[SourceRangeRef, ...],
                canonical_values(
                    self.source_range_refs, SourceRangeRef, "source_range_refs", nonempty=True
                ),
            ),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            canonical_domain_refs(self.evidence_refs, "evidence_refs", nonempty=True),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "content": self.content,
            "episode_id": self.episode_id,
            "event_id": self.event_id,
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
            "source_range_refs": [item.to_mapping() for item in self.source_range_refs],
        }


@dataclass(frozen=True, slots=True)
class EventCardSet(CanonicalModel):
    event_card_set_id: str
    events: tuple[EventCard, ...]

    def __post_init__(self) -> None:
        identifier(self.event_card_set_id, "event_card_set_id")
        events = cast(
            tuple[EventCard, ...],
            canonical_values(self.events, EventCard, "events", nonempty=True),
        )
        if len({item.event_id for item in events}) != len(events):
            raise ProductionModelError("events must have unique IDs")
        object.__setattr__(self, "events", events)

    def to_mapping(self) -> dict[str, object]:
        return {
            "event_card_set_id": self.event_card_set_id,
            "events": [item.to_mapping() for item in self.events],
        }


@dataclass(frozen=True, slots=True, init=False)
class NarrativeAttributes(CanonicalModel):
    attribute_type: NarrativeNodeType
    _payload: bytes

    @classmethod
    def from_mapping(cls, value: object) -> NarrativeAttributes:
        if type(value) is not dict:  # noqa: E721
            raise ProductionModelError("narrative attributes must be an object")
        raw = cast(Mapping[str, object], value)
        try:
            variant = NarrativeNodeType(text(raw.get("attribute_type"), "attribute_type"))
        except ValueError as error:
            raise ProductionModelError("attribute_type is unknown") from error
        item = mapping(raw, _ATTRIBUTE_FIELDS[variant.value], "narrative_attributes")
        _validate_attribute_variant(variant, item)
        instance = object.__new__(cls)
        object.__setattr__(instance, "attribute_type", variant)
        object.__setattr__(instance, "_payload", canonical_json_bytes(raw))
        return instance

    def to_mapping(self) -> dict[str, object]:
        result = json.loads(self._payload)
        if type(result) is not dict:  # pragma: no cover - constructor proves this.
            raise AssertionError("attributes payload stopped being an object")
        return cast(dict[str, object], result)


def _validate_id_array(
    value: object, label: str, *, nonempty: bool, canonical: bool = True
) -> tuple[str, ...]:
    values = tuple(identifier(item, label) for item in object_list(value, label))
    if nonempty and not values:
        raise ProductionModelError(f"{label} must not be empty")
    if len(values) != len(set(values)):
        raise ProductionModelError(f"{label} must not contain duplicates")
    if canonical and tuple(jcs_key(item) for item in values) != tuple(
        sorted(jcs_key(item) for item in values)
    ):
        raise ProductionModelError(f"{label} must use canonical JCS-byte order")
    return values


def _validate_attribute_variant(variant: NarrativeNodeType, item: Mapping[str, object]) -> None:
    if variant is NarrativeNodeType.FACT:
        identifier(item["subject_node_id"], "fact.subject_node_id")
        identifier(item["predicate"], "fact.predicate")
        raw_value = item["value"]
        if type(raw_value) is not dict:  # noqa: E721
            raise ProductionModelError("fact.value must be an object")
        value = cast(Mapping[str, object], raw_value)
        kind = text(value.get("kind"), "fact.value.kind")
        expected = {
            "text": {"kind", "text"},
            "number": {"kind", "number"},
            "boolean": {"kind", "boolean"},
            "entity_ref": {"kind", "node_id"},
        }.get(kind)
        if expected is None:
            raise ProductionModelError("fact value kind is unknown")
        fact_value = mapping(value, expected, "fact.value")
        if kind == "text":
            text(fact_value["text"], "fact.value.text")
        elif kind == "number":
            text(fact_value["number"], "fact.value.number")
        elif kind == "boolean" and type(fact_value["boolean"]) is not bool:  # noqa: E721
            raise ProductionModelError("fact.value.boolean must be a boolean")
        elif kind == "entity_ref":
            identifier(fact_value["node_id"], "fact.value.node_id")
        if item["conflict_status"] not in {"none", "conflicted"}:
            raise ProductionModelError("fact.conflict_status is unknown")
    elif variant is NarrativeNodeType.EVENT:
        ref = domain_ref(item["event_card_ref"], "event.event_card_ref")
        if ref.object_type != "event":
            raise ProductionModelError("event_card_ref must point to an event")
        identifier(item["episode_id"], "event.episode_id")
        text(item["summary"], "event.summary")
        refs = tuple(
            domain_ref(value, "event.source_range_refs")
            for value in object_list(item["source_range_refs"], "event.source_range_refs")
        )
        canonical_domain_refs(refs, "event.source_range_refs", nonempty=True)
    elif variant is NarrativeNodeType.BEAT:
        text(item["summary"], "beat.summary")
        if item["phase"] not in {
            "setup",
            "escalation",
            "turn",
            "reveal",
            "payoff",
            "consequence",
            "coda",
        }:
            raise ProductionModelError("beat.phase is unknown")
        _validate_id_array(item["obligation_ids"], "beat.obligation_ids", nonempty=True)
    elif variant is NarrativeNodeType.OBLIGATION:
        text(item["description"], "obligation.description")
        _validate_id_array(item["required_fact_ids"], "obligation.required_fact_ids", nonempty=True)
        text(item["success_criteria"], "obligation.success_criteria")
    elif variant is NarrativeNodeType.STORY_THREAD:
        text(item["title"], "story_thread.title")
        text(item["premise"], "story_thread.premise")
        _validate_id_array(item["obligation_ids"], "story_thread.obligation_ids", nonempty=True)
    elif variant is NarrativeNodeType.CHARACTER:
        text(item["canonical_name"], "character.canonical_name")
        aliases = object_list(item["aliases"], "character.aliases")
        if any(type(value) is not str for value in aliases):  # noqa: E721
            raise ProductionModelError("character.aliases contains an invalid value")
        if len(aliases) != len(set(cast(list[str], aliases))):
            raise ProductionModelError("character.aliases contains duplicates")
        _validate_id_array(item["state_fact_ids"], "character.state_fact_ids", nonempty=True)
    elif variant is NarrativeNodeType.RELATIONSHIP:
        identifier(item["subject_node_id"], "relationship.subject_node_id")
        identifier(item["object_node_id"], "relationship.object_node_id")
        if item["relation_type"] not in {
            "family",
            "ally",
            "opponent",
            "romantic",
            "authority",
            "dependency",
            "unknown",
        }:
            raise ProductionModelError("relationship.relation_type is unknown")
    elif variant is NarrativeNodeType.QUESTION:
        text(item["text"], "question.text")
        if item["status"] not in {"open", "answered", "invalidated"}:
            raise ProductionModelError("question.status is unknown")
        _validate_id_array(item["answer_fact_ids"], "question.answer_fact_ids", nonempty=False)
    else:
        _validate_id_array(item["setup_event_ids"], "foreshadow.setup_event_ids", nonempty=True)
        _validate_id_array(item["payoff_event_ids"], "foreshadow.payoff_event_ids", nonempty=False)
        if item["status"] not in {"setup_only", "paid_off", "broken"}:
            raise ProductionModelError("foreshadow.status is unknown")


@dataclass(frozen=True, slots=True)
class NarrativeConfidence(CanonicalModel):
    value: str
    method: str

    def __post_init__(self) -> None:
        # Confidence is exact decimal text at the Python boundary.
        from .production_common import exact_decimal

        exact_decimal(self.value, "confidence.value")
        if self.method not in {"model", "rule", "source"}:
            raise ProductionModelError("confidence.method is unknown")

    def to_mapping(self) -> dict[str, object]:
        return {"method": self.method, "value": self.value}


@dataclass(frozen=True, slots=True)
class NarrativeNode(CanonicalModel):
    node_id: str
    node_type: NarrativeNodeType
    label: str
    attributes: NarrativeAttributes
    evidence_refs: tuple[DomainRef, ...]
    confidence: NarrativeConfidence

    def __post_init__(self) -> None:
        identifier(self.node_id, "node_id")
        if type(self.node_type) is not NarrativeNodeType:  # noqa: E721
            raise ProductionModelError("node_type is unknown")
        if type(self.attributes) is not NarrativeAttributes:  # noqa: E721
            raise ProductionModelError("attributes must be closed NarrativeAttributes")
        if self.attributes.attribute_type is not self.node_type:
            raise ProductionModelError("attributes.attribute_type must equal node_type")
        text(self.label, "label")
        object.__setattr__(
            self,
            "evidence_refs",
            canonical_domain_refs(
                self.evidence_refs,
                "evidence_refs",
                nonempty=self.node_type is not NarrativeNodeType.STORY_THREAD,
            ),
        )
        if type(self.confidence) is not NarrativeConfidence:  # noqa: E721
            raise ProductionModelError("confidence is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "attributes": self.attributes.to_mapping(),
            "confidence": self.confidence.to_mapping(),
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
            "label": self.label,
            "node_id": self.node_id,
            "node_type": self.node_type.value,
        }


@dataclass(frozen=True, slots=True)
class NarrativeEdge(CanonicalModel):
    edge_id: str
    edge_type: str
    from_node_id: str
    to_node_id: str
    evidence_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        identifier(self.edge_id, "edge_id")
        if self.edge_type not in _EDGE_TYPES:
            raise ProductionModelError("edge_type is unknown")
        identifier(self.from_node_id, "from_node_id")
        identifier(self.to_node_id, "to_node_id")
        object.__setattr__(
            self, "evidence_refs", canonical_domain_refs(self.evidence_refs, "evidence_refs")
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "edge_id": self.edge_id,
            "edge_type": self.edge_type,
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
            "from_node_id": self.from_node_id,
            "to_node_id": self.to_node_id,
        }


@dataclass(frozen=True, slots=True)
class NarrativeGraph(CanonicalModel):
    graph_id: str
    nodes: tuple[NarrativeNode, ...]
    edges: tuple[NarrativeEdge, ...]

    def __post_init__(self) -> None:
        identifier(self.graph_id, "graph_id")
        nodes = cast(
            tuple[NarrativeNode, ...],
            canonical_values(self.nodes, NarrativeNode, "nodes", nonempty=True),
        )
        edges = cast(
            tuple[NarrativeEdge, ...], canonical_values(self.edges, NarrativeEdge, "edges")
        )
        node_ids = {item.node_id for item in nodes}
        if len(node_ids) != len(nodes):
            raise ProductionModelError("nodes must have unique IDs")
        if len({item.edge_id for item in edges}) != len(edges):
            raise ProductionModelError("edges must have unique IDs")
        if any(
            edge.from_node_id not in node_ids or edge.to_node_id not in node_ids for edge in edges
        ):
            raise ProductionModelError("edge endpoints must resolve to graph nodes")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)

    def to_mapping(self) -> dict[str, object]:
        return {
            "edges": [item.to_mapping() for item in self.edges],
            "graph_id": self.graph_id,
            "nodes": [item.to_mapping() for item in self.nodes],
        }


@dataclass(frozen=True, slots=True)
class ExclusionEvidence(CanonicalModel):
    classification: str
    evidence_refs: tuple[DomainRef, ...]
    rule_id: str
    rule_version: str

    def __post_init__(self) -> None:
        if self.classification not in {"recap", "preview", "bts", "water_content", "credits"}:
            raise ProductionModelError("exclusion classification is unknown")
        object.__setattr__(
            self,
            "evidence_refs",
            canonical_domain_refs(self.evidence_refs, "exclusion evidence", nonempty=True),
        )
        identifier(self.rule_id, "exclusion rule_id")
        safe_token(self.rule_version, "exclusion rule_version")

    def to_mapping(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
        }


@dataclass(frozen=True, slots=True)
class CoverageRow(CanonicalModel):
    coverage_id: str
    unit_type: CoverageUnitType
    unit_ref: DomainRef
    resolution_status: CoverageResolution
    disposition: CoverageDisposition
    graph_node_refs: tuple[DomainRef, ...]
    evidence_refs: tuple[DomainRef, ...]
    exclusion_evidence: ExclusionEvidence | None
    diagnostic_refs: tuple[DomainRef, ...]
    taint_seed_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        identifier(self.coverage_id, "coverage_id")
        if type(self.unit_type) is not CoverageUnitType or type(self.unit_ref) is not DomainRef:  # noqa: E721
            raise ProductionModelError("coverage unit is invalid")
        if type(self.resolution_status) is not CoverageResolution:  # noqa: E721
            raise ProductionModelError("resolution_status is unknown")
        if type(self.disposition) is not CoverageDisposition:  # noqa: E721
            raise ProductionModelError("disposition is unknown")
        if self.resolution_status is CoverageResolution.UNRESOLVED:
            if self.disposition is not CoverageDisposition.UNASSIGNED:
                raise ProductionModelError("unresolved coverage must remain unassigned")
        elif self.resolution_status is CoverageResolution.RESOLVED:
            if self.disposition is CoverageDisposition.UNASSIGNED:
                raise ProductionModelError("resolved coverage must be disposed")
        elif self.disposition is CoverageDisposition.INTENTIONALLY_EXCLUDED:
            raise ProductionModelError("conflicted coverage cannot be intentionally excluded")
        if self.disposition is CoverageDisposition.INTENTIONALLY_EXCLUDED:
            if type(self.exclusion_evidence) is not ExclusionEvidence:  # noqa: E721
                raise ProductionModelError("intentional exclusion requires exclusion_evidence")
        elif self.exclusion_evidence is not None:
            raise ProductionModelError("exclusion_evidence is forbidden for other dispositions")
        graph = canonical_domain_refs(self.graph_node_refs, "graph_node_refs")
        evidence = canonical_domain_refs(self.evidence_refs, "evidence_refs", nonempty=True)
        diagnostics = canonical_domain_refs(self.diagnostic_refs, "diagnostic_refs")
        seeds = canonical_domain_refs(self.taint_seed_refs, "taint_seed_refs")
        if any(item.object_type != "diagnostic" for item in diagnostics):
            raise ProductionModelError("diagnostic_refs must point to diagnostics")
        if any(item.object_type != "taint_seed" for item in seeds):
            raise ProductionModelError("taint_seed_refs must point to taint seeds")
        if self.resolution_status is CoverageResolution.RESOLVED and seeds:
            raise ProductionModelError("resolved coverage cannot carry taint seeds")
        if self.resolution_status is not CoverageResolution.RESOLVED and len(seeds) != 1:
            raise ProductionModelError("unresolved/conflicted coverage requires one taint seed")
        object.__setattr__(self, "graph_node_refs", graph)
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(self, "diagnostic_refs", diagnostics)
        object.__setattr__(self, "taint_seed_refs", seeds)

    def to_mapping(self) -> dict[str, object]:
        return {
            "coverage_id": self.coverage_id,
            "diagnostic_refs": [item.to_mapping() for item in self.diagnostic_refs],
            "disposition": self.disposition.value,
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
            "exclusion_evidence": (
                None if self.exclusion_evidence is None else self.exclusion_evidence.to_mapping()
            ),
            "graph_node_refs": [item.to_mapping() for item in self.graph_node_refs],
            "resolution_status": self.resolution_status.value,
            "taint_seed_refs": [item.to_mapping() for item in self.taint_seed_refs],
            "unit_ref": self.unit_ref.to_mapping(),
            "unit_type": self.unit_type.value,
        }


@dataclass(frozen=True, slots=True)
class CoverageConservation(CanonicalModel):
    input_unit_count: int
    ledger_unit_count: int
    duplicate_unit_refs: tuple[DomainRef, ...]
    missing_unit_refs: tuple[DomainRef, ...]
    unexpected_unit_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        integer(self.input_unit_count, "input_unit_count")
        integer(self.ledger_unit_count, "ledger_unit_count")
        for name in ("duplicate_unit_refs", "missing_unit_refs", "unexpected_unit_refs"):
            object.__setattr__(self, name, canonical_domain_refs(getattr(self, name), name))

    def to_mapping(self) -> dict[str, object]:
        return {
            "duplicate_unit_refs": [item.to_mapping() for item in self.duplicate_unit_refs],
            "input_unit_count": self.input_unit_count,
            "ledger_unit_count": self.ledger_unit_count,
            "missing_unit_refs": [item.to_mapping() for item in self.missing_unit_refs],
            "unexpected_unit_refs": [item.to_mapping() for item in self.unexpected_unit_refs],
        }


@dataclass(frozen=True, slots=True, init=False)
class CoverageLedger(EvaluatorOwnedModel):
    ledger_id: str
    rows: tuple[CoverageRow, ...]
    conservation: CoverageConservation

    @classmethod
    def from_inputs(
        cls,
        ledger_id: str,
        *,
        input_unit_refs: Sequence[DomainRef],
        rows: Sequence[CoverageRow],
    ) -> CoverageLedger:
        identifier(ledger_id, "ledger_id")
        expected = canonical_domain_refs(input_unit_refs, "input_unit_refs", nonempty=True)
        row_values = cast(
            tuple[CoverageRow, ...],
            canonical_values(rows, CoverageRow, "rows", nonempty=True),
        )
        if len({item.coverage_id for item in row_values}) != len(row_values):
            raise ProductionModelError("coverage rows contain duplicate coverage IDs")
        row_keys = tuple(jcs_key(item.unit_ref) for item in row_values)
        duplicates = tuple(
            sorted(
                {
                    item.unit_ref
                    for item in row_values
                    if row_keys.count(jcs_key(item.unit_ref)) > 1
                },
                key=jcs_key,
            )
        )
        expected_by_key = {jcs_key(item): item for item in expected}
        actual_by_key = {jcs_key(item.unit_ref): item.unit_ref for item in row_values}
        missing = tuple(
            expected_by_key[key] for key in sorted(set(expected_by_key) - set(actual_by_key))
        )
        unexpected = tuple(
            actual_by_key[key] for key in sorted(set(actual_by_key) - set(expected_by_key))
        )
        conservation = CoverageConservation(
            len(expected), len(row_values), duplicates, missing, unexpected
        )
        if (
            conservation.input_unit_count != conservation.ledger_unit_count
            or conservation.duplicate_unit_refs
            or conservation.missing_unit_refs
            or conservation.unexpected_unit_refs
        ):
            raise ProductionModelError("coverage conservation failed")
        instance = object.__new__(cls)
        object.__setattr__(instance, "ledger_id", ledger_id)
        object.__setattr__(instance, "rows", row_values)
        object.__setattr__(instance, "conservation", conservation)
        return instance

    def to_mapping(self) -> dict[str, object]:
        return {
            "conservation": self.conservation.to_mapping(),
            "ledger_id": self.ledger_id,
            "rows": [item.to_mapping() for item in self.rows],
        }


_EVIDENCE_DIAGNOSTIC_KINDS: Final = frozenset(
    {
        "missing_reference",
        "source_hash_mismatch",
        "missing_source_range",
        "insufficient_evidence",
        "authorization_unknown",
        "authorization_denied",
        "low_confidence",
    }
)
_CONFLICT_DIAGNOSTIC_KINDS: Final = frozenset(
    {
        "fact_value_conflict",
        "timeline_order_conflict",
        "character_identity_conflict",
        "character_state_conflict",
        "source_range_conflict",
        "possible_duplicate",
    }
)


@dataclass(frozen=True, slots=True)
class DiagnosticItem(CanonicalModel):
    diagnostic_id: str
    kind: str
    severity: str
    scope_ref: DomainRef
    rule_id: str
    message: str
    evidence_refs: tuple[DomainRef, ...]
    affected_refs: tuple[DomainRef, ...]
    competing_claim_refs: tuple[DomainRef, ...] = ()

    def __post_init__(self) -> None:
        identifier(self.diagnostic_id, "diagnostic_id")
        if self.kind not in _EVIDENCE_DIAGNOSTIC_KINDS | _CONFLICT_DIAGNOSTIC_KINDS:
            raise ProductionModelError("diagnostic kind is unknown")
        if self.severity not in {"warning", "error"}:
            raise ProductionModelError("diagnostic severity is unknown")
        if type(self.scope_ref) is not DomainRef:  # noqa: E721
            raise ProductionModelError("diagnostic scope_ref is invalid")
        identifier(self.rule_id, "diagnostic rule_id")
        text(self.message, "diagnostic message")
        object.__setattr__(
            self, "evidence_refs", canonical_domain_refs(self.evidence_refs, "evidence_refs")
        )
        object.__setattr__(
            self,
            "affected_refs",
            canonical_domain_refs(self.affected_refs, "affected_refs", nonempty=True),
        )
        competing = canonical_domain_refs(self.competing_claim_refs, "competing_claim_refs")
        if self.kind in _CONFLICT_DIAGNOSTIC_KINDS and len(competing) < 2:
            raise ProductionModelError("conflict diagnostics require at least two competing claims")
        if self.kind in _EVIDENCE_DIAGNOSTIC_KINDS and competing:
            raise ProductionModelError("evidence diagnostics cannot carry competing claims")
        object.__setattr__(self, "competing_claim_refs", competing)

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "affected_refs": [item.to_mapping() for item in self.affected_refs],
            "diagnostic_id": self.diagnostic_id,
            "evidence_refs": [item.to_mapping() for item in self.evidence_refs],
            "kind": self.kind,
            "message": self.message,
            "rule_id": self.rule_id,
            "scope_ref": self.scope_ref.to_mapping(),
            "severity": self.severity,
        }
        if self.kind in _CONFLICT_DIAGNOSTIC_KINDS:
            result["competing_claim_refs"] = [
                item.to_mapping() for item in self.competing_claim_refs
            ]
        return result


@dataclass(frozen=True, slots=True)
class EvidenceDiagnostics(CanonicalModel):
    evidence_diagnostics_id: str
    items: tuple[DiagnosticItem, ...]

    def __post_init__(self) -> None:
        identifier(self.evidence_diagnostics_id, "evidence_diagnostics_id")
        items = cast(
            tuple[DiagnosticItem, ...], canonical_values(self.items, DiagnosticItem, "items")
        )
        if any(item.kind not in _EVIDENCE_DIAGNOSTIC_KINDS for item in items):
            raise ProductionModelError("EvidenceDiagnostics contains a conflict diagnostic")
        object.__setattr__(self, "items", items)

    def to_mapping(self) -> dict[str, object]:
        return {
            "evidence_diagnostics_id": self.evidence_diagnostics_id,
            "items": [item.to_mapping() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class ConflictDiagnostics(CanonicalModel):
    conflict_diagnostics_id: str
    items: tuple[DiagnosticItem, ...]

    def __post_init__(self) -> None:
        identifier(self.conflict_diagnostics_id, "conflict_diagnostics_id")
        items = cast(
            tuple[DiagnosticItem, ...], canonical_values(self.items, DiagnosticItem, "items")
        )
        if any(item.kind not in _CONFLICT_DIAGNOSTIC_KINDS for item in items):
            raise ProductionModelError("ConflictDiagnostics contains an evidence diagnostic")
        object.__setattr__(self, "items", items)

    def to_mapping(self) -> dict[str, object]:
        return {
            "conflict_diagnostics_id": self.conflict_diagnostics_id,
            "items": [item.to_mapping() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class DependencyArc(CanonicalModel):
    from_ref: DomainRef
    to_ref: DomainRef
    kind: str
    source_ref: DomainRef

    def __post_init__(self) -> None:
        if any(
            type(value) is not DomainRef for value in (self.from_ref, self.to_ref, self.source_ref)
        ):  # noqa: E721
            raise ProductionModelError("dependency arc refs must be DomainRefs")
        identifier(self.kind, "dependency arc kind")

    def to_mapping(self) -> dict[str, object]:
        return {
            "from_ref": self.from_ref.to_mapping(),
            "kind": self.kind,
            "source_ref": self.source_ref.to_mapping(),
            "to_ref": self.to_ref.to_mapping(),
        }


@dataclass(frozen=True, slots=True, init=False)
class DependencyScc(EvaluatorOwnedModel):
    scc_id: str
    node_refs: tuple[DomainRef, ...]
    outgoing_scc_ids: tuple[str, ...]

    @classmethod
    def from_nodes(
        cls, node_refs: Sequence[DomainRef], outgoing_scc_ids: Sequence[str]
    ) -> DependencyScc:
        nodes = canonical_domain_refs(node_refs, "scc.node_refs", nonempty=True)
        outgoing = canonical_ids(outgoing_scc_ids, "scc.outgoing_scc_ids")
        instance = object.__new__(cls)
        object.__setattr__(
            instance,
            "scc_id",
            canonical_json_hash([item.to_mapping() for item in nodes]),
        )
        object.__setattr__(instance, "node_refs", nodes)
        object.__setattr__(instance, "outgoing_scc_ids", outgoing)
        return instance

    def to_mapping(self) -> dict[str, object]:
        return {
            "node_refs": [item.to_mapping() for item in self.node_refs],
            "outgoing_scc_ids": list(self.outgoing_scc_ids),
            "scc_id": self.scc_id,
        }


@dataclass(frozen=True, slots=True, init=False)
class TaintSeedProof(EvaluatorOwnedModel):
    taint_seed_id: str
    root_refs: tuple[DomainRef, ...]
    affected_refs: tuple[DomainRef, ...]
    frontier_refs: tuple[DomainRef, ...]
    isolation_status: str
    closure_hash: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "affected_refs": [item.to_mapping() for item in self.affected_refs],
            "closure_hash": self.closure_hash,
            "frontier_refs": [item.to_mapping() for item in self.frontier_refs],
            "isolation_status": self.isolation_status,
            "root_refs": [item.to_mapping() for item in self.root_refs],
            "taint_seed_id": self.taint_seed_id,
        }


@dataclass(frozen=True, slots=True, init=False)
class DependencyClosureProof(EvaluatorOwnedModel):
    dependency_closure_proof_id: str
    graph_ref: ArtifactRef
    policy_ref: ArtifactRef
    dependency_arcs: tuple[DependencyArc, ...]
    sccs: tuple[DependencyScc, ...]
    seed_proofs: tuple[TaintSeedProof, ...]

    def __post_init__(self) -> None:
        identifier(self.dependency_closure_proof_id, "dependency_closure_proof_id")
        if type(self.graph_ref) is not ArtifactRef or type(self.policy_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("dependency proof refs must be ArtifactRefs")
        arcs = cast(
            tuple[DependencyArc, ...],
            canonical_values(self.dependency_arcs, DependencyArc, "dependency_arcs"),
        )
        sccs = cast(
            tuple[DependencyScc, ...],
            canonical_values(self.sccs, DependencyScc, "sccs", nonempty=True),
        )
        seeds = cast(
            tuple[TaintSeedProof, ...],
            canonical_values(self.seed_proofs, TaintSeedProof, "seed_proofs"),
        )
        if len({item.scc_id for item in sccs}) != len(sccs):
            raise ProductionModelError("dependency proof has duplicate SCCs")
        if len({item.taint_seed_id for item in seeds}) != len(seeds):
            raise ProductionModelError("dependency proof has duplicate seed proofs")
        known_sccs = {item.scc_id for item in sccs}
        if any(not set(item.outgoing_scc_ids) <= known_sccs for item in sccs):
            raise ProductionModelError("dependency SCC points outside the exact SCC set")
        object.__setattr__(self, "dependency_arcs", arcs)
        object.__setattr__(self, "sccs", sccs)
        object.__setattr__(self, "seed_proofs", seeds)

    @property
    def arc_set_hash(self) -> str:
        return canonical_json_hash([item.to_mapping() for item in self.dependency_arcs])

    @property
    def scc_set_hash(self) -> str:
        return canonical_json_hash([item.to_mapping() for item in self.sccs])

    @property
    def dependency_closure_hash(self) -> str:
        return canonical_json_hash([item.to_mapping() for item in self.seed_proofs])

    def to_mapping(self) -> dict[str, object]:
        return {
            "arc_set_hash": self.arc_set_hash,
            "dependency_arcs": [item.to_mapping() for item in self.dependency_arcs],
            "dependency_closure_proof_id": self.dependency_closure_proof_id,
            "graph_ref": self.graph_ref.to_mapping(),
            "policy_ref": self.policy_ref.to_mapping(),
            "scc_set_hash": self.scc_set_hash,
            "sccs": [item.to_mapping() for item in self.sccs],
            "seed_proofs": [item.to_mapping() for item in self.seed_proofs],
        }


def _graph_node_ref(graph_ref: ArtifactRef, node_id: str) -> DomainRef:
    return DomainRef(graph_ref, "narrative_node", node_id)


def _strongly_connected_components(
    graph_ref: ArtifactRef,
    node_ids: Sequence[str],
    adjacency: Mapping[str, tuple[str, ...]],
) -> tuple[DependencyScc, ...]:
    """Deterministic Tarjan SCC projection over the exact frozen graph."""

    next_index = 0
    index_by_id: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node_id: str) -> None:
        nonlocal next_index
        index_by_id[node_id] = next_index
        lowlink[node_id] = next_index
        next_index += 1
        stack.append(node_id)
        on_stack.add(node_id)
        for target_id in adjacency[node_id]:
            if target_id not in index_by_id:
                visit(target_id)
                lowlink[node_id] = min(lowlink[node_id], lowlink[target_id])
            elif target_id in on_stack:
                lowlink[node_id] = min(lowlink[node_id], index_by_id[target_id])
        if lowlink[node_id] != index_by_id[node_id]:
            return
        members: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            members.append(member)
            if member == node_id:
                break
        components.append(tuple(sorted(members, key=jcs_key)))

    for node_id in sorted(node_ids, key=jcs_key):
        if node_id not in index_by_id:
            visit(node_id)
    component_by_node: dict[str, str] = {}
    component_ids: dict[tuple[str, ...], str] = {}
    for component in components:
        refs = tuple(sorted((_graph_node_ref(graph_ref, item) for item in component), key=jcs_key))
        component_id = canonical_json_hash([item.to_mapping() for item in refs])
        component_ids[component] = component_id
        for node_id in component:
            component_by_node[node_id] = component_id
    values: list[DependencyScc] = []
    for component in components:
        own_id = component_ids[component]
        outgoing = {
            component_by_node[target]
            for node_id in component
            for target in adjacency[node_id]
            if component_by_node[target] != own_id
        }
        values.append(
            DependencyScc.from_nodes(
                tuple(_graph_node_ref(graph_ref, item) for item in component),
                tuple(sorted(outgoing, key=jcs_key)),
            )
        )
    return tuple(sorted(values, key=jcs_key))


class DependencyClosureEvaluator:
    """Recompute arcs, SCCs, roots, closure and frontier from authoritative DTOs."""

    @staticmethod
    def evaluate(
        *,
        proof_id: str,
        episode_digests: EpisodeDigestSet,
        event_cards_ref: ArtifactRef,
        event_cards: EventCardSet,
        graph_ref: ArtifactRef,
        graph: NarrativeGraph,
        ledger: CoverageLedger,
        evidence_diagnostics_ref: ArtifactRef,
        evidence_diagnostics: EvidenceDiagnostics,
        conflict_diagnostics_ref: ArtifactRef,
        conflict_diagnostics: ConflictDiagnostics,
        policy_ref: ArtifactRef,
        policy: DependencyPropagationPolicy,
    ) -> DependencyClosureProof:
        identifier(proof_id, "dependency_closure_proof_id")
        if graph_ref.content_hash != graph.canonical_hash:
            raise ProductionModelError("dependency evaluator graph ref does not bind graph payload")
        if policy_ref.content_hash != policy.canonical_hash:
            raise ProductionModelError("dependency policy ref does not bind policy payload")
        if event_cards_ref.content_hash != event_cards.canonical_hash:
            raise ProductionModelError("EventCardSet ref does not bind EventCard content")
        evidence_ids = {item.diagnostic_id for item in evidence_diagnostics.items}
        conflict_ids = {item.diagnostic_id for item in conflict_diagnostics.items}
        evidence_by_id = {item.diagnostic_id: item for item in evidence_diagnostics.items}
        conflict_by_id = {item.diagnostic_id: item for item in conflict_diagnostics.items}
        node_ids = tuple(item.node_id for item in graph.nodes)
        graph_refs = {item: _graph_node_ref(graph_ref, item) for item in node_ids}
        graph_event_ref_by_event_id: dict[str, DomainRef] = {}
        for node in graph.nodes:
            if node.node_type is not NarrativeNodeType.EVENT:
                continue
            event_ref = domain_ref(
                node.attributes.to_mapping()["event_card_ref"],
                "event_card_ref",
            )
            if event_ref.artifact_ref != event_cards_ref:
                raise ProductionModelError("Graph Event has the wrong EventCardSet owner")
            graph_event_ref_by_event_id[event_ref.object_id] = graph_refs[node.node_id]
        event_graph_refs_by_evidence: dict[bytes, dict[bytes, DomainRef]] = {}
        for event in event_cards.events:
            graph_event_ref = graph_event_ref_by_event_id.get(event.event_id)
            if graph_event_ref is None:
                continue
            for evidence_ref in event.evidence_refs:
                event_graph_refs_by_evidence.setdefault(jcs_key(evidence_ref), {})[
                    jcs_key(graph_event_ref)
                ] = graph_event_ref
        digest_evidence_by_window: dict[bytes, dict[bytes, DomainRef]] = {}
        for digest in episode_digests.digests:
            for window_ref in digest.source_window_refs:
                evidence = digest_evidence_by_window.setdefault(jcs_key(window_ref), {})
                for evidence_ref in digest.evidence_refs:
                    evidence[jcs_key(evidence_ref)] = evidence_ref

        def authoritative_graph_roots(row: CoverageRow) -> tuple[DomainRef, ...]:
            roots: dict[bytes, DomainRef] = {}
            if row.unit_type is CoverageUnitType.EVENT:
                graph_event_ref = graph_event_ref_by_event_id.get(row.unit_ref.object_id)
                if graph_event_ref is not None:
                    roots[jcs_key(graph_event_ref)] = graph_event_ref
            elif row.unit_type is CoverageUnitType.VLM_OBSERVATION:
                roots.update(event_graph_refs_by_evidence.get(jcs_key(row.unit_ref), {}))
            elif row.unit_type is CoverageUnitType.VLM_WINDOW:
                for evidence_ref in digest_evidence_by_window.get(
                    jcs_key(row.unit_ref), {}
                ).values():
                    roots.update(event_graph_refs_by_evidence.get(jcs_key(evidence_ref), {}))
            else:
                node = next(
                    (
                        item
                        for item in graph.nodes
                        if item.node_id == row.unit_ref.object_id
                        and item.node_type is NarrativeNodeType.OBLIGATION
                    ),
                    None,
                )
                if node is not None:
                    graph_ref_value = graph_refs[node.node_id]
                    roots[jcs_key(graph_ref_value)] = graph_ref_value
            return tuple(roots[key] for key in sorted(roots))

        arcs = tuple(
            sorted(
                (
                    DependencyArc(
                        graph_refs[edge.from_node_id],
                        graph_refs[edge.to_node_id],
                        edge.edge_type,
                        DomainRef(graph_ref, "narrative_edge", edge.edge_id),
                    )
                    for edge in graph.edges
                    if edge.edge_type in policy.propagating_edge_types
                ),
                key=jcs_key,
            )
        )
        adjacency_lists: dict[str, list[str]] = {item: [] for item in node_ids}
        for edge in graph.edges:
            if edge.edge_type in policy.propagating_edge_types:
                adjacency_lists[edge.from_node_id].append(edge.to_node_id)
        adjacency = {
            key: tuple(sorted(set(values), key=jcs_key)) for key, values in adjacency_lists.items()
        }
        sccs = _strongly_connected_components(graph_ref, node_ids, adjacency)
        seeds: list[TaintSeedProof] = []
        for row in ledger.rows:
            derived_graph_roots = authoritative_graph_roots(row)
            if row.graph_node_refs != derived_graph_roots:
                raise ProductionModelError(
                    "coverage row graph_node_refs differ from canonical graph roots"
                )
            if row.resolution_status is CoverageResolution.RESOLVED:
                continue
            diagnostic_ids = {item.object_id for item in row.diagnostic_refs}
            if not diagnostic_ids:
                raise ProductionModelError("unresolved/conflicted coverage requires diagnostics")
            expected_owner = (
                conflict_diagnostics_ref
                if row.resolution_status is CoverageResolution.CONFLICTED
                else evidence_diagnostics_ref
            )
            expected_ids = (
                conflict_ids
                if row.resolution_status is CoverageResolution.CONFLICTED
                else evidence_ids
            )
            expected_diagnostics = (
                conflict_by_id
                if row.resolution_status is CoverageResolution.CONFLICTED
                else evidence_by_id
            )
            if any(item.artifact_ref != expected_owner for item in row.diagnostic_refs):
                raise ProductionModelError("coverage diagnostic has the wrong authoritative owner")
            if not diagnostic_ids <= expected_ids:
                raise ProductionModelError("coverage diagnostic does not resolve exactly")
            if any(
                jcs_key(row.unit_ref)
                not in {jcs_key(ref) for ref in expected_diagnostics[item].affected_refs}
                for item in diagnostic_ids
            ):
                raise ProductionModelError(
                    "coverage diagnostic does not identify its exact affected unit"
                )
            seed_id = row.taint_seed_refs[0].object_id
            roots_by_key = {jcs_key(row.unit_ref): row.unit_ref}
            roots_by_key.update({jcs_key(item): item for item in derived_graph_roots})
            roots = tuple(roots_by_key[key] for key in sorted(roots_by_key))
            affected_by_key = {jcs_key(item): item for item in roots}
            pending_nodes = [item.object_id for item in derived_graph_roots]
            visited: set[str] = set()
            while pending_nodes:
                node_id = pending_nodes.pop()
                if node_id in visited:
                    continue
                visited.add(node_id)
                ref = graph_refs[node_id]
                affected_by_key[jcs_key(ref)] = ref
                pending_nodes.extend(adjacency[node_id])
            affected = tuple(affected_by_key[key] for key in sorted(affected_by_key))
            seed = object.__new__(TaintSeedProof)
            object.__setattr__(seed, "taint_seed_id", seed_id)
            object.__setattr__(
                seed, "root_refs", canonical_domain_refs(roots, "root_refs", nonempty=True)
            )
            object.__setattr__(seed, "affected_refs", affected)
            object.__setattr__(seed, "frontier_refs", ())
            object.__setattr__(seed, "isolation_status", "bounded")
            object.__setattr__(
                seed,
                "closure_hash",
                canonical_json_hash([item.to_mapping() for item in affected]),
            )
            seeds.append(seed)
        instance = object.__new__(DependencyClosureProof)
        object.__setattr__(instance, "dependency_closure_proof_id", proof_id)
        object.__setattr__(instance, "graph_ref", graph_ref)
        object.__setattr__(instance, "policy_ref", policy_ref)
        object.__setattr__(instance, "dependency_arcs", arcs)
        object.__setattr__(instance, "sccs", sccs)
        object.__setattr__(instance, "seed_proofs", tuple(sorted(seeds, key=jcs_key)))
        return instance


@dataclass(frozen=True, slots=True, init=False)
class CoverageAdmission(EvaluatorOwnedModel):
    admission_id: str
    pending_set_hash: str
    ledger_ref: ArtifactRef
    coverage_policy_ref: ArtifactRef
    coverage_mode: str
    next_action: str
    taint_seed_ids: tuple[str, ...]
    dependency_closure_proof_ref: ArtifactRef
    taint_seeds_hash: str
    dependency_closure_hash: str
    rule_results: tuple[RuleResult, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "admission_id": self.admission_id,
            "coverage_mode": self.coverage_mode,
            "coverage_policy_ref": self.coverage_policy_ref.to_mapping(),
            "dependency_closure_hash": self.dependency_closure_hash,
            "dependency_closure_proof_ref": self.dependency_closure_proof_ref.to_mapping(),
            "kind": "coverage",
            "ledger_ref": self.ledger_ref.to_mapping(),
            "next_action": self.next_action,
            "pending_set_hash": self.pending_set_hash,
            "rule_results": [item.to_mapping() for item in self.rule_results],
            "taint_seed_ids": list(self.taint_seed_ids),
            "taint_seeds_hash": self.taint_seeds_hash,
        }


_COVERAGE_RULES: Final = {
    "KC-IN-001",
    "KC-GRAPH-001",
    "KC-GRAPH-002",
    "KC-COV-001",
    "KC-COV-002",
    "KC-COV-003",
    "KC-COV-004",
    "KC-COV-005",
    "KC-EXCLUDE-001",
    "KC-EVENT-001",
    "KC-DEP-001",
    "KC-DEP-002",
    "KC-DEP-003",
    "KC-ISO-001",
    "KC-GATE-001",
}
_STAGE1_MEMBER_TYPES: Final = {
    "episode_digest_set",
    "event_card_set",
    "narrative_graph",
    "coverage_ledger",
    "evidence_diagnostics",
    "conflict_diagnostics",
    "dependency_closure_proof",
}


class CoverageAdmissionEvaluator:
    """Only path that can mint a Stage 1 Admission over an exact pending set."""

    @staticmethod
    def evaluate(
        *,
        admission_id: str,
        pending_set: PendingBusinessSet,
        episode_digests: EpisodeDigestSet,
        event_cards: EventCardSet,
        graph: NarrativeGraph,
        ledger: CoverageLedger,
        evidence_diagnostics: EvidenceDiagnostics,
        conflict_diagnostics: ConflictDiagnostics,
        dependency_proof: DependencyClosureProof,
        coverage_policy_ref: ArtifactRef,
        coverage_policy: CoveragePolicy,
        dependency_policy_ref: ArtifactRef,
        dependency_policy: DependencyPropagationPolicy,
    ) -> CoverageAdmission:
        identifier(admission_id, "admission_id")
        if type(pending_set) is not PendingBusinessSet or pending_set.admission_kind != "coverage":  # noqa: E721
            raise ProductionModelError("coverage evaluator requires the coverage pending set")
        pending_set.require_exact_types(_STAGE1_MEMBER_TYPES)
        pending_set.require_member("episode_digest_set", episode_digests)
        event_ref = pending_set.require_member("event_card_set", event_cards)
        graph_ref = pending_set.require_member("narrative_graph", graph)
        ledger_ref = pending_set.require_member("coverage_ledger", ledger)
        evidence_ref = pending_set.require_member("evidence_diagnostics", evidence_diagnostics)
        conflict_ref = pending_set.require_member("conflict_diagnostics", conflict_diagnostics)
        proof_ref = pending_set.require_member("dependency_closure_proof", dependency_proof)
        if dependency_proof.graph_ref != graph_ref:
            raise ProductionModelError("dependency proof does not bind the exact NarrativeGraph")
        if (
            type(coverage_policy_ref) is not ArtifactRef
            or type(coverage_policy) is not CoveragePolicy
        ):  # noqa: E721
            raise ProductionModelError("coverage evaluator requires a frozen CoveragePolicy")
        if coverage_policy_ref.content_hash != coverage_policy.canonical_hash:
            raise ProductionModelError("coverage policy ref does not bind the exact policy")
        if dependency_policy_ref.content_hash != dependency_policy.canonical_hash:
            raise ProductionModelError("dependency policy ref does not bind the exact policy")
        if dependency_proof.policy_ref != dependency_policy_ref:
            raise ProductionModelError("dependency proof does not bind the frozen policy")
        coverage_mode = coverage_policy.coverage_mode
        event_ids = {item.event_id for item in event_cards.events}
        for node in graph.nodes:
            if node.node_type is NarrativeNodeType.EVENT:
                attributes = node.attributes.to_mapping()
                owner = domain_ref(attributes["event_card_ref"], "event_card_ref")
                if owner.artifact_ref != event_ref or owner.object_id != node.node_id:
                    raise ProductionModelError("Graph Event event_card_ref has the wrong owner/ID")
                if node.node_id not in event_ids:
                    raise ProductionModelError("Graph Event does not resolve to EventCardSet")
        expected_units: dict[bytes, tuple[CoverageUnitType, DomainRef]] = {}
        for digest in episode_digests.digests:
            for ref in digest.source_window_refs:
                expected_units[jcs_key(ref)] = (CoverageUnitType.VLM_WINDOW, ref)
            for ref in digest.evidence_refs:
                expected_units[jcs_key(ref)] = (CoverageUnitType.VLM_OBSERVATION, ref)
        for event in event_cards.events:
            ref = DomainRef(event_ref, "event", event.event_id)
            expected_units[jcs_key(ref)] = (CoverageUnitType.EVENT, ref)
        for node in graph.nodes:
            if node.node_type is NarrativeNodeType.OBLIGATION:
                ref = _graph_node_ref(graph_ref, node.node_id)
                expected_units[jcs_key(ref)] = (CoverageUnitType.OBLIGATION, ref)
        actual_units = {jcs_key(row.unit_ref): row for row in ledger.rows}
        if set(actual_units) != set(expected_units):
            raise ProductionModelError(
                "Coverage universe does not exactly match Digest/Event/Obligation authority"
            )
        if any(actual_units[key].unit_type is not expected_units[key][0] for key in expected_units):
            raise ProductionModelError("Coverage unit type does not match authoritative unit")
        recomputed = DependencyClosureEvaluator.evaluate(
            proof_id=dependency_proof.dependency_closure_proof_id,
            episode_digests=episode_digests,
            event_cards_ref=event_ref,
            event_cards=event_cards,
            graph_ref=graph_ref,
            graph=graph,
            ledger=ledger,
            evidence_diagnostics_ref=evidence_ref,
            evidence_diagnostics=evidence_diagnostics,
            conflict_diagnostics_ref=conflict_ref,
            conflict_diagnostics=conflict_diagnostics,
            policy_ref=dependency_policy_ref,
            policy=dependency_policy,
        )
        if recomputed != dependency_proof:
            raise ProductionModelError(
                "dependency proof was not recomputed from exact Stage 1 inputs"
            )
        seed_ids = tuple(ref.object_id for row in ledger.rows for ref in row.taint_seed_refs)
        if len(seed_ids) != len(set(seed_ids)):
            raise ProductionModelError("each taint seed must belong to exactly one coverage row")
        proof_by_id = {item.taint_seed_id: item for item in dependency_proof.seed_proofs}
        if set(seed_ids) != set(proof_by_id):
            raise ProductionModelError(
                "coverage taint seeds and dependency proofs do not join exactly"
            )
        all_bounded = all(item.isolation_status == "bounded" for item in proof_by_id.values())
        next_action = "continue"
        if seed_ids and coverage_mode == "strict_global":
            next_action = "quarantine"
        elif seed_ids and not all_bounded:
            next_action = "quarantine"
        failed_rules: set[str] = {"KC-GATE-001"} if next_action != "continue" else set()
        rules = computed_rule_results(
            _COVERAGE_RULES,
            pending_set.canonical_hash,
            failed_rule_ids=failed_rules,
        )
        instance = object.__new__(CoverageAdmission)
        object.__setattr__(instance, "admission_id", admission_id)
        object.__setattr__(instance, "pending_set_hash", pending_set.canonical_hash)
        object.__setattr__(instance, "ledger_ref", ledger_ref)
        object.__setattr__(instance, "coverage_policy_ref", coverage_policy_ref)
        object.__setattr__(instance, "coverage_mode", coverage_mode)
        object.__setattr__(instance, "next_action", next_action)
        ordered_seeds = tuple(sorted(seed_ids, key=jcs_key))
        object.__setattr__(instance, "taint_seed_ids", ordered_seeds)
        object.__setattr__(instance, "dependency_closure_proof_ref", proof_ref)
        object.__setattr__(instance, "taint_seeds_hash", canonical_json_hash(list(ordered_seeds)))
        object.__setattr__(
            instance, "dependency_closure_hash", dependency_proof.dependency_closure_hash
        )
        object.__setattr__(instance, "rule_results", rules)
        return instance


__all__ = [
    "ConflictDiagnostics",
    "CoverageAdmission",
    "CoverageAdmissionEvaluator",
    "CoverageConservation",
    "CoverageDisposition",
    "CoverageLedger",
    "CoveragePolicy",
    "CoverageResolution",
    "CoverageRow",
    "CoverageUnitType",
    "DependencyArc",
    "DependencyClosureProof",
    "DependencyClosureEvaluator",
    "DependencyPropagationPolicy",
    "DependencyScc",
    "DiagnosticItem",
    "EpisodeDigest",
    "EpisodeDigestSet",
    "EventCard",
    "EventCardSet",
    "EvidenceDiagnostics",
    "ExclusionEvidence",
    "NarrativeAttributes",
    "NarrativeConfidence",
    "NarrativeEdge",
    "NarrativeGraph",
    "NarrativeNode",
    "NarrativeNodeType",
    "SourceRangeRef",
    "TaintSeedProof",
]
