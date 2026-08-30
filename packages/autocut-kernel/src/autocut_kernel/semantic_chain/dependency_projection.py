"""Closed Stage 1 Graph/Ledger dependency projection, without admission authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..store.models import ArtifactMember, CommittedSemanticInputs, SourceWindowIdentity
from .core_observations import (
    CoreEvent,
    CoreFact,
    observation_source_interval,
    semantic_pack,
)
from .dependency_graph import DependencyArc
from .ledger_models import CoverageLedger
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .narrative_models import (
    BeatAttributes,
    CharacterAttributes,
    CharacterStateAttributes,
    EventAttributes,
    EventCardSet,
    FactAttributes,
    FactEntityRefValue,
    ForeshadowAttributes,
    GraphNode,
    NarrativeGraph,
    ObligationAttributes,
    QuestionAttributes,
    RelationshipAttributes,
    StoryThreadAttributes,
)


class DependencyProjectionError(ValueError):
    """A supplied pending member or dependency reference is not exact and closed."""


_EDGE_FORWARD = frozenset({"supports", "satisfies", "causes", "resolves"})
_EDGE_REVERSE = frozenset({"requires"})
_OWNER_BY_OBJECT_TYPE = {
    "entity": "narrative_graph",
    "fact": "narrative_graph",
    "event": "event_card_set",
    "beat": "narrative_graph",
    "obligation": "narrative_graph",
    "story_thread": "narrative_graph",
    "character": "narrative_graph",
    "character_state": "narrative_graph",
    "relationship": "narrative_graph",
    "question": "narrative_graph",
    "foreshadow": "narrative_graph",
    "source": "whole_series_source_manifest",
    "source_window": "whole_series_source_manifest",
    "coverage_window": "coverage_ledger",
}
_EDGE_PROJECTIONS = {
    "supports": "from_to",
    "satisfies": "from_to",
    "causes": "from_to",
    "resolves": "from_to",
    "requires": "to_from",
    "precedes": "no_propagation_seed_both_on_conflict",
    "involves": "attributes_only",
    "contradicts": "seed_both_no_recursive_arc",
}
_ATTRIBUTE_PROJECTIONS = (
    "subject_to_fact",
    "entity_ref_value_to_fact",
    "participant_to_event",
    "required_fact_to_obligation",
    "obligation_to_beat",
    "obligation_to_story_thread",
    "entity_to_character",
    "state_fact_to_character",
    "character_to_character_state",
    "state_fact_to_character_state",
    "answer_fact_to_question",
    "setup_payoff_event_to_foreshadow",
    "subject_object_character_to_relationship",
)
_EXTERNAL_PROJECTIONS = (
    "source_window_to_coverage_window",
    "coverage_window_to_source",
    "coverage_window_to_fact",
    "coverage_window_to_event",
    "source_to_fact",
    "source_to_event",
)


@dataclass(frozen=True, slots=True)
class DependencyProjectionPolicy:
    """The one explicit, closed projection registry for the first implementation."""

    strategy_version: str

    def __post_init__(self) -> None:
        if type(self.strategy_version) is not str or self.strategy_version != "semantic-dependencies-v1":  # noqa: E721
            raise DependencyProjectionError("dependency projection requires semantic-dependencies-v1")

    def to_mapping(self) -> dict[str, object]:
        return {
            "strategy_version": self.strategy_version,
            "canonical_owner_by_object_type": dict(_OWNER_BY_OBJECT_TYPE),
            "edge_projections": dict(_EDGE_PROJECTIONS),
            "attribute_projections": list(_ATTRIBUTE_PROJECTIONS),
            "external_root_projections": list(_EXTERNAL_PROJECTIONS),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class DependencyProjection:
    """Canonical value input to SCC/reachability analysis, never an admission result."""

    nodes: tuple[SemanticObjectRef, ...]
    arcs: tuple[DependencyArc, ...]

    def __post_init__(self) -> None:
        if type(self.nodes) is not tuple or any(type(item) is not SemanticObjectRef for item in self.nodes):  # noqa: E721
            raise DependencyProjectionError("dependency projection nodes must be exact SemanticObjectRef tuples")
        if type(self.arcs) is not tuple or any(type(item) is not DependencyArc for item in self.arcs):  # noqa: E721
            raise DependencyProjectionError("dependency projection arcs must be exact DependencyArc tuples")
        node_keys = tuple(canonical_json_bytes(item.to_mapping()) for item in self.nodes)
        if len(set(node_keys)) != len(node_keys):
            raise DependencyProjectionError("dependency projection nodes must be unique")
        if any(item.from_ref not in self.nodes or item.to_ref not in self.nodes for item in self.arcs):
            raise DependencyProjectionError("dependency projection must include every arc endpoint")
        object.__setattr__(self, "nodes", tuple(sorted(self.nodes, key=lambda item: canonical_json_bytes(item.to_mapping()))))
        object.__setattr__(self, "arcs", tuple(sorted(self.arcs, key=lambda item: item.canonical_key)))

    def to_mapping(self) -> dict[str, object]:
        return {
            "nodes": [item.to_mapping() for item in self.nodes],
            "arcs": [item.to_mapping() for item in self.arcs],
        }


def _member(member: object, artifact_type: str, inputs: CommittedSemanticInputs) -> SemanticMemberIdentity:
    if type(member) is not ArtifactMember:  # noqa: E721
        raise DependencyProjectionError("dependency projection requires exact ArtifactMember values")
    value = member
    if value.artifact_type != artifact_type or value.scope != inputs.source_manifest.reference.scope:
        raise DependencyProjectionError("dependency projection member has wrong type or scope")
    try:
        return SemanticMemberIdentity.from_artifact_member(value)
    except ValueError as error:
        raise DependencyProjectionError("dependency projection member payload hash is invalid") from error


def _payload(member: ArtifactMember, label: str) -> object:
    try:
        return json.loads(member.payload_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise DependencyProjectionError(f"{label} payload is not an exact closed value") from error


def _source_identity(inputs: CommittedSemanticInputs) -> SemanticMemberIdentity:
    reference = inputs.source_manifest.reference
    return SemanticMemberIdentity(
        reference.artifact_type,
        reference.logical_id,
        reference.revision,
        reference.scope,
        reference.content_hash,
    )


def _node_ref(graph: SemanticMemberIdentity, card: SemanticMemberIdentity, node: GraphNode) -> SemanticObjectRef:
    if node.node_type == "event":
        attrs = cast(EventAttributes, node.attributes)
        if attrs.event_card_ref.member_ref != card or attrs.event_card_ref.object_id != node.node_id:
            raise DependencyProjectionError("Graph Event must canonicalize to its exact EventCard")
        return attrs.event_card_ref
    return SemanticObjectRef(graph, node.node_type, node.node_id)


def project_dependencies(
    inputs: CommittedSemanticInputs,
    *,
    graph_member: ArtifactMember,
    event_card_member: ArtifactMember,
    ledger_member: ArtifactMember,
    policy: DependencyProjectionPolicy,
) -> DependencyProjection:
    """Project exact pending Graph/Card/Ledger values into registered arcs only."""

    if type(inputs) is not CommittedSemanticInputs or type(policy) is not DependencyProjectionPolicy:  # noqa: E721
        raise DependencyProjectionError("dependency projection requires exact inputs and explicit policy")
    graph_identity = _member(graph_member, "narrative_graph", inputs)
    card_identity = _member(event_card_member, "event_card_set", inputs)
    ledger_identity = _member(ledger_member, "coverage_ledger", inputs)
    try:
        graph = NarrativeGraph.from_mapping(_payload(graph_member, "NarrativeGraph"))
        cards = EventCardSet.from_mapping(_payload(event_card_member, "EventCardSet"))
    except ValueError as error:
        raise DependencyProjectionError("Graph/Card payload is not an exact closed value") from error
    try:
        ledger = CoverageLedger.from_mapping(_payload(ledger_member, "CoverageLedger"))
    except ValueError as error:
        raise DependencyProjectionError("CoverageLedger payload is not an exact closed value") from error

    source_identity = _source_identity(inputs)
    source_windows = {item.source_window.window_manifest_sha256: item.source_window for item in inputs.inputs}
    sources = {(item.source_window.source_id, item.source_window.source_sha256) for item in inputs.inputs}
    raw_facts: dict[str, tuple[SemanticMemberIdentity, SourceWindowIdentity, CoreFact]] = {}
    raw_events: dict[str, tuple[SemanticMemberIdentity, SourceWindowIdentity, CoreEvent]] = {}
    for item in inputs.inputs:
        pack_identity = SemanticMemberIdentity(
            item.semantic_pack.reference.artifact_type,
            item.semantic_pack.reference.logical_id,
            item.semantic_pack.reference.revision,
            item.semantic_pack.reference.scope,
            item.semantic_pack.reference.content_hash,
        )
        pack = semantic_pack(item)
        for fact in pack.facts:
            if fact.fact_id in raw_facts:
                raise DependencyProjectionError("committed VLM Fact identity is duplicated")
            raw_facts[fact.fact_id] = (pack_identity, item.source_window, fact)
        for event in pack.events:
            if event.event_id in raw_events:
                raise DependencyProjectionError("committed VLM Event identity is duplicated")
            raw_events[event.event_id] = (pack_identity, item.source_window, event)
    graph_fact_ids = {node.node_id for node in graph.nodes if node.node_type == "fact"}
    graph_event_ids = {node.node_id for node in graph.nodes if node.node_type == "event"}
    card_event_ids = {card.event_id for card in cards.events}
    if graph_fact_ids != set(raw_facts):
        raise DependencyProjectionError("Graph Fact IDs must exactly cover committed raw VLM Facts")
    if graph_event_ids != card_event_ids or card_event_ids != set(raw_events):
        raise DependencyProjectionError("Graph/Card Event IDs must exactly cover committed raw VLM Events")
    graph_refs = {node.node_id: _node_ref(graph_identity, card_identity, node) for node in graph.nodes}
    card_by_id = {card.event_id: card for card in cards.events}
    card_windows: dict[str, SourceWindowIdentity] = {}
    for card in cards.events:
        raw = raw_events.get(card.event_id)
        if raw is None:
            raise DependencyProjectionError("EventCard names an event absent from committed VLM inputs")
        pack_identity, window, raw_event = raw
        if SemanticObjectRef(pack_identity, "vlm_event", card.event_id) not in card.evidence_refs:
            raise DependencyProjectionError("EventCard does not retain its exact raw VLM Event evidence")
        expected_source = SemanticObjectRef(source_identity, "source", window.source_id)
        if any(
            item.source_ref != expected_source
            or item.clock_id != window.source_clock_id
            or item.mapped_interval != observation_source_interval(raw_event)
            for item in card.source_range_refs
        ):
            raise DependencyProjectionError("EventCard source range is not its exact committed VLM/source mapping")
        card_windows[card.event_id] = window
    expected_window_ids = set(source_windows)
    if {window.source_window_ref.object_id for window in ledger.windows} != expected_window_ids:
        raise DependencyProjectionError("CoverageLedger windows must exactly cover committed SourceWindows")
    nodes: set[SemanticObjectRef] = set(graph_refs.values())
    arcs: set[DependencyArc] = set()

    def add_arc(from_ref: SemanticObjectRef, to_ref: SemanticObjectRef, kind: str, source_ref: SemanticObjectRef) -> None:
        arcs.add(DependencyArc(from_ref, to_ref, kind, source_ref))

    for node in graph.nodes:
        node_ref = graph_refs[node.node_id]
        attrs = node.attributes
        if isinstance(attrs, EventAttributes):
            card = card_by_id.get(node.node_id)
            if card is None or card.episode_id != attrs.episode_id or card.content != attrs.summary:
                raise DependencyProjectionError("Graph Event does not match its exact EventCard content")
            if len(card.source_range_refs) != len(attrs.source_range_refs) or tuple(
                item.object_id for item in attrs.source_range_refs
            ) != tuple(f"{node.node_id}:range:{index}" for index in range(len(card.source_range_refs))):
                raise DependencyProjectionError("Graph Event does not match its exact EventCard ranges")
            if any(participant not in graph_refs for participant in attrs.participant_node_ids):
                raise DependencyProjectionError("Graph Event has an unresolved participant")
            for participant in attrs.participant_node_ids:
                add_arc(graph_refs[participant], node_ref, "participant_to_event", node_ref)
        elif isinstance(attrs, FactAttributes):
            raw_fact = raw_facts.get(node.node_id)
            if raw_fact is None or SemanticObjectRef(raw_fact[0], "vlm_fact", node.node_id) not in node.evidence_refs:
                raise DependencyProjectionError("Graph Fact does not retain its exact committed VLM Fact evidence")
            if attrs.subject_node_id not in graph_refs:
                raise DependencyProjectionError("Graph Fact has an unresolved subject")
            add_arc(graph_refs[attrs.subject_node_id], node_ref, "subject_to_fact", node_ref)
            if isinstance(attrs.value, FactEntityRefValue):
                if attrs.value.node_id not in graph_refs:
                    raise DependencyProjectionError("Graph Fact has an unresolved entity value")
                add_arc(graph_refs[attrs.value.node_id], node_ref, "entity_ref_value_to_fact", node_ref)
        elif isinstance(attrs, ObligationAttributes):
            for fact_id in attrs.required_fact_ids:
                if fact_id not in graph_refs:
                    raise DependencyProjectionError("Graph Obligation has an unresolved Fact")
                add_arc(graph_refs[fact_id], node_ref, "required_fact_to_obligation", node_ref)
        elif isinstance(attrs, BeatAttributes):
            for obligation_id in attrs.obligation_ids:
                if obligation_id not in graph_refs:
                    raise DependencyProjectionError("Graph Beat has an unresolved Obligation")
                add_arc(graph_refs[obligation_id], node_ref, "obligation_to_beat", node_ref)
        elif isinstance(attrs, StoryThreadAttributes):
            for obligation_id in attrs.obligation_ids:
                if obligation_id not in graph_refs:
                    raise DependencyProjectionError("Graph StoryThread has an unresolved Obligation")
                add_arc(graph_refs[obligation_id], node_ref, "obligation_to_story_thread", node_ref)
        elif isinstance(attrs, CharacterAttributes):
            for entity_id in attrs.entity_node_ids:
                if entity_id not in graph_refs:
                    raise DependencyProjectionError("Graph Character has an unresolved entity")
                add_arc(graph_refs[entity_id], node_ref, "entity_to_character", node_ref)
            for fact_id in attrs.state_fact_ids:
                if fact_id not in graph_refs:
                    raise DependencyProjectionError("Graph Character has an unresolved state Fact")
                add_arc(graph_refs[fact_id], node_ref, "state_fact_to_character", node_ref)
        elif isinstance(attrs, CharacterStateAttributes):
            if attrs.character_node_id not in graph_refs:
                raise DependencyProjectionError("Graph CharacterState has an unresolved Character")
            add_arc(graph_refs[attrs.character_node_id], node_ref, "character_to_character_state", node_ref)
            for fact_id in attrs.state_fact_ids:
                if fact_id not in graph_refs:
                    raise DependencyProjectionError("Graph CharacterState has an unresolved state Fact")
                add_arc(graph_refs[fact_id], node_ref, "state_fact_to_character_state", node_ref)
        elif isinstance(attrs, QuestionAttributes):
            for fact_id in attrs.answer_fact_ids:
                if fact_id not in graph_refs:
                    raise DependencyProjectionError("Graph Question has an unresolved answer Fact")
                add_arc(graph_refs[fact_id], node_ref, "answer_fact_to_question", node_ref)
        elif isinstance(attrs, ForeshadowAttributes):
            for event_id in (*attrs.setup_event_ids, *attrs.payoff_event_ids):
                if event_id not in graph_refs:
                    raise DependencyProjectionError("Graph Foreshadow has an unresolved Event")
                add_arc(graph_refs[event_id], node_ref, "setup_payoff_event_to_foreshadow", node_ref)
        elif isinstance(attrs, RelationshipAttributes):
            for character_id in (attrs.subject_node_id, attrs.object_node_id):
                if character_id not in graph_refs:
                    raise DependencyProjectionError("Graph Relationship has an unresolved Character")
                add_arc(graph_refs[character_id], node_ref, "subject_object_character_to_relationship", node_ref)

    for edge in graph.edges:
        edge_ref = SemanticObjectRef(graph_identity, "edge", edge.edge_id)
        if edge.edge_type in _EDGE_FORWARD:
            add_arc(graph_refs[edge.from_node_id], graph_refs[edge.to_node_id], edge.edge_type, edge_ref)
        elif edge.edge_type in _EDGE_REVERSE:
            add_arc(graph_refs[edge.to_node_id], graph_refs[edge.from_node_id], edge.edge_type, edge_ref)

    for window in ledger.windows:
        window_ref = window.source_window_ref
        if window_ref.member_ref != source_identity or window_ref.object_type != "source_window":
            raise DependencyProjectionError("CoverageWindow must use an exact committed SourceWindow")
        source_window = source_windows.get(window_ref.object_id)
        if source_window is None:
            raise DependencyProjectionError("CoverageWindow names an unknown SourceWindow")
        source_ref = SemanticObjectRef(source_identity, "source", source_window.source_id)
        if window.source_ref != source_ref or (source_window.source_id, source_window.source_sha256) not in sources:
            raise DependencyProjectionError("CoverageWindow has the wrong exact Source owner")
        coverage_ref = SemanticObjectRef(ledger_identity, "coverage_window", window.window_id)
        nodes.update((window_ref, source_ref, coverage_ref))
        add_arc(window_ref, coverage_ref, "source_window_to_coverage_window", coverage_ref)
        add_arc(coverage_ref, source_ref, "coverage_window_to_source", coverage_ref)
        expected_facts = {
            graph_refs[fact_id]
            for fact_id, (_owner, raw_window, _fact) in raw_facts.items()
            if raw_window == source_window
        }
        expected_events = {
            graph_refs[event_id]
            for event_id, raw_window in card_windows.items()
            if raw_window == source_window
        }
        if set(window.fact_refs) != expected_facts or set(window.event_refs) != expected_events:
            raise DependencyProjectionError("CoverageWindow must exactly retain every raw Fact/Event for its SourceWindow")
        for fact_ref in window.fact_refs:
            if fact_ref != graph_refs.get(fact_ref.object_id) or fact_ref.object_type != "fact":
                raise DependencyProjectionError("CoverageWindow Fact is not an exact Graph Fact")
            raw_fact = raw_facts.get(fact_ref.object_id)
            if raw_fact is None or raw_fact[1] != source_window:
                raise DependencyProjectionError("CoverageWindow Fact does not belong to its exact SourceWindow")
            nodes.add(fact_ref)
            add_arc(coverage_ref, fact_ref, "coverage_window_to_fact", coverage_ref)
            add_arc(source_ref, fact_ref, "source_to_fact", source_ref)
        for event_ref in window.event_refs:
            if event_ref != graph_refs.get(event_ref.object_id) or event_ref.member_ref != card_identity:
                raise DependencyProjectionError("CoverageWindow Event is not an exact canonical EventCard")
            if card_windows.get(event_ref.object_id) != source_window:
                raise DependencyProjectionError("CoverageWindow Event does not belong to its exact SourceWindow")
            nodes.add(event_ref)
            add_arc(coverage_ref, event_ref, "coverage_window_to_event", coverage_ref)
            add_arc(source_ref, event_ref, "source_to_event", source_ref)

    return DependencyProjection(tuple(nodes), tuple(arcs))
