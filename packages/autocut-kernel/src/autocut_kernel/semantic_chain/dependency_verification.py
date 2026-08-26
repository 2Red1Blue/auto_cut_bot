"""Independent dependency-proof checks; never an Admission decision."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from ..contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
    load_canonical_json_bytes,
)
from ..store.models import ArtifactMember, CommittedSemanticInputs
from .dependency_graph import DependencyArc, DependencySeed, analyze_dependency_graph
from .dependency_projection import DependencyProjectionPolicy
from .dependency_proof import DependencyClosureProof
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


class DependencyVerificationError(ValueError):
    """A verifier input is malformed, unreadable, or not independently closed."""


_RULE_IDS = ("KC-DEP-001", "KC-DEP-002", "KC-DEP-003", "KC-ISO-001")
_STATUSES = ("pass", "fail", "indeterminate")
_CODES = frozenset(
    {
        "member_identity_invalid",
        "policy_mismatch",
        "projection_missing_arc",
        "projection_extra_arc",
        "projection_reversed_arc",
        "projection_missing_node",
        "projection_extra_node",
        "raw_universe_mismatch",
        "ledger_window_mismatch",
        "seed_set_mismatch",
        "seed_root_mismatch",
        "seed_frontier_mismatch",
        "scc_mismatch",
        "condensation_mismatch",
        "closure_mismatch",
        "closure_hash_mismatch",
        "unbounded_frontier",
        "proof_decode_invalid",
    }
)


@dataclass(frozen=True, slots=True)
class DependencyCheckResult:
    """One closed, non-authoritative dependency rule result."""

    rule_id: str
    status: str
    violation_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.rule_id) is not str or self.rule_id not in _RULE_IDS:  # noqa: E721
            raise DependencyVerificationError("dependency check rule_id is unsupported")
        if type(self.status) is not str or self.status not in _STATUSES:  # noqa: E721
            raise DependencyVerificationError("dependency check status is unsupported")
        if type(self.violation_codes) is not tuple or any(type(code) is not str for code in self.violation_codes):  # noqa: E721
            raise DependencyVerificationError("dependency check violation_codes must be a tuple of strings")
        if any(code not in _CODES for code in self.violation_codes):
            raise DependencyVerificationError("dependency check violation_codes are unsupported")
        if self.violation_codes != tuple(sorted(set(self.violation_codes))):
            raise DependencyVerificationError("dependency check violation_codes must be sorted and unique")
        if (self.status == "pass") != (not self.violation_codes):
            raise DependencyVerificationError("dependency check status must exactly match its violations")

    def to_mapping(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "status": self.status,
            "violation_codes": list(self.violation_codes),
        }


def _result(rule_id: str, codes: set[str]) -> DependencyCheckResult:
    return DependencyCheckResult(rule_id, "fail" if codes else "pass", tuple(sorted(codes)))


def _payload(member: ArtifactMember) -> object:
    value, _ = load_canonical_json_bytes(
        member.payload_json.encode("utf-8"), origin="dependency verification member"
    )
    if type(value) is not dict:  # noqa: E721
        raise DependencyVerificationError("member payload must be an object")
    return cast(dict[str, object], value)


def _identity(member: object, artifact_type: str, inputs: CommittedSemanticInputs) -> SemanticMemberIdentity:
    if type(member) is not ArtifactMember:  # noqa: E721
        raise DependencyVerificationError("proof members must be exact ArtifactMember values")
    typed = member
    if typed.artifact_type != artifact_type or typed.scope != inputs.source_manifest.reference.scope:
        raise DependencyVerificationError("proof member has wrong type or scope")
    return SemanticMemberIdentity.from_artifact_member(typed)


def _key(value: SemanticObjectRef) -> bytes:
    return canonical_json_bytes(value.to_mapping())


def _source_identity(inputs: CommittedSemanticInputs) -> SemanticMemberIdentity:
    source = inputs.source_manifest.reference
    return SemanticMemberIdentity(source.artifact_type, source.logical_id, source.revision, source.scope, source.content_hash)


def _node_ref(graph: SemanticMemberIdentity, card: SemanticMemberIdentity, node: GraphNode) -> SemanticObjectRef:
    node_id = node.node_id
    if node.node_type == "event":
        attrs = cast(EventAttributes, node.attributes)
        if attrs.event_card_ref.member_ref != card or attrs.event_card_ref.object_id != node_id:
            raise DependencyVerificationError("Graph Event lacks its canonical EventCard reference")
        return attrs.event_card_ref
    return SemanticObjectRef(graph, node.node_type, node_id)


def _derive_expected(
    inputs: CommittedSemanticInputs,
    graph_member: ArtifactMember,
    event_card_member: ArtifactMember,
    ledger_member: ArtifactMember,
) -> tuple[tuple[SemanticObjectRef, ...], tuple[DependencyArc, ...], tuple[DependencySeed, ...], CoverageLedger]:
    """Independently enumerate every registered semantic and external relation."""
    graph_identity = _identity(graph_member, "narrative_graph", inputs)
    card_identity = _identity(event_card_member, "event_card_set", inputs)
    ledger_identity = _identity(ledger_member, "coverage_ledger", inputs)
    try:
        graph = NarrativeGraph.from_mapping(_payload(graph_member))
        cards = EventCardSet.from_mapping(_payload(event_card_member))
        ledger = CoverageLedger.from_mapping(_payload(ledger_member))
    except ValueError as error:
        raise DependencyVerificationError("semantic member payload is not exact closed content") from error
    source_identity = _source_identity(inputs)
    raw_facts: dict[str, tuple[SemanticMemberIdentity, object]] = {}
    raw_events: dict[str, object] = {}
    windows = {item.source_window.window_manifest_sha256: item.source_window for item in inputs.inputs}
    for item in inputs.inputs:
        pack = item.semantic_pack.reference
        pack_identity = SemanticMemberIdentity(pack.artifact_type, pack.logical_id, pack.revision, pack.scope, pack.content_hash)
        for fact in item.semantic_pack.semantic_pack.facts:
            if fact.fact_id in raw_facts:
                raise DependencyVerificationError("raw fact IDs are duplicated")
            raw_facts[fact.fact_id] = (pack_identity, item.source_window)
        for event in item.semantic_pack.semantic_pack.events:
            if event.event_id in raw_events:
                raise DependencyVerificationError("raw event IDs are duplicated")
            raw_events[event.event_id] = item.source_window
    graph_fact_ids = {node.node_id for node in graph.nodes if node.node_type == "fact"}
    graph_event_ids = {node.node_id for node in graph.nodes if node.node_type == "event"}
    if graph_fact_ids != set(raw_facts) or graph_event_ids != set(raw_events) or {item.event_id for item in cards.events} != set(raw_events):
        raise DependencyVerificationError("graph/card raw universe is incomplete")
    graph_refs = {node.node_id: _node_ref(graph_identity, card_identity, node) for node in graph.nodes}
    card_by_id = {item.event_id: item for item in cards.events}
    nodes: set[SemanticObjectRef] = set(graph_refs.values())
    arcs: set[DependencyArc] = set()

    def require_ref(value: str) -> SemanticObjectRef:
        try:
            return graph_refs[value]
        except KeyError as error:
            raise DependencyVerificationError("Graph relation names an absent node") from error

    def add(left: SemanticObjectRef, right: SemanticObjectRef, kind: str, owner: SemanticObjectRef) -> None:
        arcs.add(DependencyArc(left, right, kind, owner))

    for node in graph.nodes:
        ref, attrs = graph_refs[node.node_id], node.attributes
        if isinstance(attrs, EventAttributes):
            card = card_by_id.get(node.node_id)
            if card is None or card.episode_id != attrs.episode_id or card.content != attrs.summary:
                raise DependencyVerificationError("Graph Event differs from EventCard")
            for item in attrs.participant_node_ids:
                add(require_ref(item), ref, "participant_to_event", ref)
        elif isinstance(attrs, FactAttributes):
            raw = raw_facts.get(node.node_id)
            if raw is None or SemanticObjectRef(raw[0], "vlm_fact", node.node_id) not in node.evidence_refs:
                raise DependencyVerificationError("Graph Fact lacks its raw evidence")
            add(require_ref(attrs.subject_node_id), ref, "subject_to_fact", ref)
            if isinstance(attrs.value, FactEntityRefValue):
                add(require_ref(attrs.value.node_id), ref, "entity_ref_value_to_fact", ref)
        elif isinstance(attrs, ObligationAttributes):
            for item in attrs.required_fact_ids:
                add(require_ref(item), ref, "required_fact_to_obligation", ref)
        elif isinstance(attrs, BeatAttributes):
            for item in attrs.obligation_ids:
                add(require_ref(item), ref, "obligation_to_beat", ref)
        elif isinstance(attrs, StoryThreadAttributes):
            for item in attrs.obligation_ids:
                add(require_ref(item), ref, "obligation_to_story_thread", ref)
        elif isinstance(attrs, CharacterAttributes):
            for item in attrs.entity_node_ids:
                add(require_ref(item), ref, "entity_to_character", ref)
            for item in attrs.state_fact_ids:
                add(require_ref(item), ref, "state_fact_to_character", ref)
        elif isinstance(attrs, CharacterStateAttributes):
            add(require_ref(attrs.character_node_id), ref, "character_to_character_state", ref)
            for item in attrs.state_fact_ids:
                add(require_ref(item), ref, "state_fact_to_character_state", ref)
        elif isinstance(attrs, QuestionAttributes):
            for item in attrs.answer_fact_ids:
                add(require_ref(item), ref, "answer_fact_to_question", ref)
        elif isinstance(attrs, ForeshadowAttributes):
            for item in (*attrs.setup_event_ids, *attrs.payoff_event_ids):
                add(require_ref(item), ref, "setup_payoff_event_to_foreshadow", ref)
        elif isinstance(attrs, RelationshipAttributes):
            add(require_ref(attrs.subject_node_id), ref, "subject_object_character_to_relationship", ref)
            add(require_ref(attrs.object_node_id), ref, "subject_object_character_to_relationship", ref)
    for edge in graph.edges:
        source = SemanticObjectRef(graph_identity, "edge", edge.edge_id)
        if edge.edge_type in {"supports", "satisfies", "causes", "resolves"}:
            add(require_ref(edge.from_node_id), require_ref(edge.to_node_id), edge.edge_type, source)
        elif edge.edge_type == "requires":
            add(require_ref(edge.to_node_id), require_ref(edge.from_node_id), edge.edge_type, source)
    if {item.source_window_ref.object_id for item in ledger.windows} != set(windows):
        raise DependencyVerificationError("Ledger does not have exact input window coverage")
    for window in ledger.windows:
        raw_window = windows.get(window.source_window_ref.object_id)
        if raw_window is None or window.source_window_ref.member_ref != source_identity:
            raise DependencyVerificationError("Ledger window has foreign source owner")
        source_ref = SemanticObjectRef(source_identity, "source", raw_window.source_id)
        if window.source_ref != source_ref:
            raise DependencyVerificationError("Ledger window has incorrect source")
        expected_facts = {graph_refs[key] for key, (_owner, item) in raw_facts.items() if item == raw_window}
        expected_events = {graph_refs[key] for key, item in raw_events.items() if item == raw_window}
        if set(window.fact_refs) != expected_facts or set(window.event_refs) != expected_events:
            raise DependencyVerificationError("Ledger window omits raw units")
        coverage = SemanticObjectRef(ledger_identity, "coverage_window", window.window_id)
        nodes.update((window.source_window_ref, source_ref, coverage))
        add(window.source_window_ref, coverage, "source_window_to_coverage_window", coverage)
        add(coverage, source_ref, "coverage_window_to_source", coverage)
        for item in window.fact_refs:
            add(coverage, item, "coverage_window_to_fact", coverage)
            add(source_ref, item, "source_to_fact", source_ref)
        for item in window.event_refs:
            add(coverage, item, "coverage_window_to_event", coverage)
            add(source_ref, item, "source_to_event", source_ref)
    def expand(refs: tuple[SemanticObjectRef, ...], local_ids: tuple[str, ...]) -> tuple[SemanticObjectRef, ...]:
        return tuple(sorted((*refs, *(SemanticObjectRef(ledger_identity, "coverage_window", item) for item in local_ids)), key=_key))
    seeds = tuple(DependencySeed(seed.seed_id, expand(seed.root_refs, seed.root_window_ids), expand(seed.frontier_refs, seed.frontier_window_ids)) for seed in ledger.taint_seeds)
    return tuple(sorted(nodes, key=_key)), tuple(sorted(arcs, key=lambda arc: arc.canonical_key)), seeds, ledger


def verify_dependency_proof(
    inputs: CommittedSemanticInputs,
    *,
    graph_member: ArtifactMember,
    event_card_member: ArtifactMember,
    ledger_member: ArtifactMember,
    proof_member: ArtifactMember,
    policy: DependencyProjectionPolicy,
) -> tuple[DependencyCheckResult, ...]:
    """Return all four independent, non-authoritative proof checks."""
    if type(inputs) is not CommittedSemanticInputs or type(policy) is not DependencyProjectionPolicy:  # noqa: E721
        raise DependencyVerificationError("verifier requires exact inputs and explicit policy")
    codes = {rule: set[str]() for rule in _RULE_IDS}
    try:
        graph_ref = _identity(graph_member, "narrative_graph", inputs)
        card_ref = _identity(event_card_member, "event_card_set", inputs)
        ledger_ref = _identity(ledger_member, "coverage_ledger", inputs)
        _identity(proof_member, "dependency_closure_proof", inputs)
        proof = DependencyClosureProof.from_mapping(_payload(proof_member))
    except (ValueError, TypeError, json.JSONDecodeError):
        return tuple(_result(rule, {"proof_decode_invalid"}) for rule in _RULE_IDS)
    source_ref = _source_identity(inputs)
    if (
        proof_member.logical_id != "dependency_closure_proof"
        or len({graph_member.revision, event_card_member.revision, ledger_member.revision, proof_member.revision}) != 1
    ):
        codes["KC-DEP-001"].add("member_identity_invalid")
    if (proof.source_member_ref, proof.graph_member_ref, proof.event_card_member_ref, proof.ledger_member_ref) != (source_ref, graph_ref, card_ref, ledger_ref):
        codes["KC-DEP-001"].add("member_identity_invalid")
    try:
        nodes, arcs, seeds, ledger = _derive_expected(inputs, graph_member, event_card_member, ledger_member)
        expected = analyze_dependency_graph(nodes, arcs, seeds)
    except (ValueError, TypeError, KeyError):
        codes["KC-DEP-001"].add("raw_universe_mismatch")
        codes["KC-DEP-002"].add("proof_decode_invalid")
        codes["KC-DEP-003"].add("proof_decode_invalid")
        codes["KC-ISO-001"].add("proof_decode_invalid")
        return tuple(_result(rule, codes[rule]) for rule in _RULE_IDS)
    if (proof.input_binding_sha256, proof.canonical_draft_sha256, proof.coverage_policy_sha256, proof.dependency_policy_sha256) != (ledger.input_binding_sha256, ledger.draft_sha256, ledger.coverage_policy_sha256, policy.canonical_hash):
        codes["KC-DEP-001"].add("policy_mismatch")
    proof_arcs, expected_arcs = set(proof.analysis.arcs), set(expected.arcs)
    if expected_arcs - proof_arcs:
        codes["KC-DEP-001"].add("projection_missing_arc")
    if proof_arcs - expected_arcs:
        codes["KC-DEP-001"].add("projection_extra_arc")
    if any(DependencyArc(arc.to_ref, arc.from_ref, arc.kind, arc.source_ref) in proof_arcs for arc in expected_arcs - proof_arcs):
        codes["KC-DEP-001"].add("projection_reversed_arc")
    if set(expected.node_refs) - set(proof.analysis.node_refs):
        codes["KC-DEP-001"].add("projection_missing_node")
    if set(proof.analysis.node_refs) - set(expected.node_refs):
        codes["KC-DEP-001"].add("projection_extra_node")
    if proof.analysis.sccs != expected.sccs:
        codes["KC-DEP-002"].add("scc_mismatch")
    if proof.analysis.condensation_arcs != expected.condensation_arcs:
        codes["KC-DEP-002"].add("condensation_mismatch")
    expected_by_seed = {item.seed_id: item for item in expected.seed_closures}
    proof_by_seed = {item.seed_id: item for item in proof.analysis.seed_closures}
    if set(expected_by_seed) != set(proof_by_seed):
        codes["KC-DEP-003"].add("seed_set_mismatch")
        codes["KC-ISO-001"].add("closure_mismatch")
    for key, closure in expected_by_seed.items():
        actual = proof_by_seed.get(key)
        if actual is None:
            continue
        if actual.root_refs != closure.root_refs:
            codes["KC-DEP-003"].add("seed_root_mismatch")
        if actual.frontier_refs != closure.frontier_refs:
            codes["KC-DEP-003"].add("seed_frontier_mismatch")
        if actual != closure:
            codes["KC-DEP-002"].add("closure_mismatch")
            codes["KC-DEP-003"].add("closure_mismatch")
            codes["KC-ISO-001"].add("closure_mismatch")
        if canonical_json_hash([item.to_mapping() for item in actual.affected_refs]) != canonical_json_hash([item.to_mapping() for item in closure.affected_refs]):
            codes["KC-ISO-001"].add("closure_hash_mismatch")
    if any(seed.frontier_refs for seed in expected.seed_closures):
        codes["KC-DEP-003"].add("unbounded_frontier")
    return tuple(_result(rule, codes[rule]) for rule in _RULE_IDS)
