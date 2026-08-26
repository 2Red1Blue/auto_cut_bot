"""Dependency projection contract tests; ledger fixtures land with its owner."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import cast

import pytest
from autocut_kernel.semantic_chain.dependency_projection import (
    DependencyProjectionError,
    DependencyProjectionPolicy,
    project_dependencies,
)
from autocut_kernel.semantic_chain.ledger_models import (
    CoverageCounts,
    CoverageLedger,
    CoverageRow,
    CoverageWindow,
    LocalCoverageWindowRef,
)
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.narrative_models import (
    CharacterAttributes,
    CharacterStateAttributes,
    EventAttributes,
    FactAttributes,
    FactEntityRefValue,
    ForeshadowAttributes,
    GraphEdge,
    GraphNode,
    NarrativeGraph,
    QuestionAttributes,
    RelationshipAttributes,
)
from autocut_kernel.semantic_chain.narrative_projection import (
    NarrativeProjection,
    project_narrative,
)
from autocut_kernel.semantic_chain.stage1_draft import decode_stage1_draft
from autocut_kernel.store import ArtifactMember
from autocut_kernel.store.models import canonical_payload_hash

from tests.semantic_chain.test_stage1_draft import POLICY, _draft, _synthetic_inputs

HASH = "sha256:" + "a" * 64


def _artifact(artifact_type, scope, payload):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ArtifactMember(artifact_type, artifact_type, 1, scope, canonical_payload_hash(encoded), encoded)


def _projection_with_ledger(*, augment: bool = False):
    inputs = _synthetic_inputs()
    draft = decode_stage1_draft(json.dumps(_draft(inputs)).encode(), inputs=inputs, policy=POLICY)
    values = project_narrative(inputs, draft, scope=inputs.source_manifest.reference.scope, revision=1)
    graph = NarrativeGraph.from_mapping(json.loads(values.narrative_graph.payload_json))
    if augment:
        entity = next(node for node in graph.nodes if node.node_type == "entity")
        fact = next(node for node in graph.nodes if node.node_type == "fact")
        event = next(node for node in graph.nodes if node.node_type == "event")
        source = inputs.source_manifest.reference
        source_identity = SemanticMemberIdentity(source.artifact_type, source.logical_id, source.revision, source.scope, source.content_hash)
        window_ref = SemanticObjectRef(source_identity, "source_window", inputs.inputs[0].source_window.window_manifest_sha256)
        character = GraphNode(
            "character_1", "character", "Character",
            CharacterAttributes("Character", (), (fact.node_id,), (entity.node_id,), entity.evidence_refs),
            entity.evidence_refs, entity.confidence,
        )
        state = GraphNode(
            "state_1", "character_state", "State",
            CharacterStateAttributes(character.node_id, window_ref, (fact.node_id,)),
            fact.evidence_refs, fact.confidence,
        )
        relationship = GraphNode(
            "relationship_1", "relationship", "Relationship",
            RelationshipAttributes(character.node_id, character.node_id, "ally"), entity.evidence_refs, fact.confidence,
        )
        question = GraphNode(
            "question_1", "question", "Question",
            QuestionAttributes("What happens?", "open", (fact.node_id,)), entity.evidence_refs, fact.confidence,
        )
        foreshadow = GraphNode(
            "foreshadow_1", "foreshadow", "Foreshadow",
            ForeshadowAttributes((event.node_id,), (event.node_id,), "paid_off"), entity.evidence_refs, fact.confidence,
        )
        changed_fact = replace(
            fact,
            attributes=FactAttributes(
                fact.attributes.subject_node_id,
                fact.attributes.predicate,
                FactEntityRefValue(entity.node_id),
                "none",
            ),
        )
        obligation = next(node for node in graph.nodes if node.node_type == "obligation")
        graph = replace(
            graph,
            nodes=tuple(node for node in graph.nodes if node.node_id != fact.node_id)
            + (changed_fact, character, state, relationship, question, foreshadow),
            edges=graph.edges + (
                GraphEdge("edge_requires", "requires", fact.node_id, obligation.node_id, ()),
                GraphEdge("edge_precedes", "precedes", fact.node_id, obligation.node_id, ()),
                GraphEdge("edge_involves", "involves", fact.node_id, obligation.node_id, ()),
                GraphEdge("edge_conflicts", "contradicts", fact.node_id, obligation.node_id, ()),
            ),
        )
        graph_member = _artifact("narrative_graph", values.narrative_graph.scope, graph.to_mapping())
        values = NarrativeProjection(values.event_cards, values.episode_digests, graph_member)
    graph_identity = SemanticMemberIdentity.from_artifact_member(values.narrative_graph)
    source = inputs.source_manifest.reference
    source_identity = SemanticMemberIdentity(source.artifact_type, source.logical_id, source.revision, source.scope, source.content_hash)
    facts = {node.node_id: SemanticObjectRef(graph_identity, "fact", node.node_id) for node in graph.nodes if node.node_type == "fact"}
    events = {
        node.node_id: cast(EventAttributes, node.attributes).event_card_ref
        for node in graph.nodes
        if node.node_type == "event"
    }
    windows, rows = [], []
    for index, item in enumerate(inputs.inputs):
        window = item.source_window
        window_ref = SemanticObjectRef(source_identity, "source_window", window.window_manifest_sha256)
        source_ref = SemanticObjectRef(source_identity, "source", window.source_id)
        pack = item.semantic_pack.semantic_pack
        fact_refs = tuple(facts[fact.fact_id] for fact in pack.facts)
        event_refs = tuple(events[event.event_id] for event in pack.events)
        window_id = f"coverage-window-{index + 1}"
        windows.append(CoverageWindow(window_id, window_ref, source_ref, fact_refs, event_refs))
        rows.append(CoverageRow(f"window-{index}", "source_window", LocalCoverageWindowRef(window_id), "resolved", "supporting", (), (), (), None))
        rows.extend(CoverageRow(f"fact-{fact.object_id}", "fact", fact, "resolved", "supporting", (), (), (), None) for fact in fact_refs)
        rows.extend(CoverageRow(f"event-{event.object_id}", "event", event, "resolved", "supporting", (), (), (), None) for event in event_refs)
    obligations = [SemanticObjectRef(graph_identity, "obligation", node.node_id) for node in graph.nodes if node.node_type == "obligation"]
    rows.extend(CoverageRow(f"obligation-{ref.object_id}", "obligation", ref, "resolved", "supporting", (), (), (), None) for ref in obligations)
    counts = CoverageCounts(
        sum(row.unit_type == "fact" for row in rows),
        sum(row.unit_type == "event" for row in rows),
        sum(row.unit_type == "source_window" for row in rows),
        sum(row.unit_type == "obligation" for row in rows),
    )
    ledger = CoverageLedger("ledger", HASH, HASH, HASH, tuple(windows), tuple(rows), (), counts)
    return inputs, values, _artifact("coverage_ledger", inputs.source_manifest.reference.scope, ledger.to_mapping())


def test_policy_is_exact_closed_and_input_bound():
    policy = DependencyProjectionPolicy("semantic-dependencies-v1")
    assert policy.to_mapping()["strategy_version"] == "semantic-dependencies-v1"
    assert policy.canonical_hash.startswith("sha256:")
    with pytest.raises(DependencyProjectionError):
        DependencyProjectionPolicy("caller-selected")


def test_projects_registered_graph_attributes_edges_and_exact_coverage_roots():
    inputs, values, ledger = _projection_with_ledger()
    result = project_dependencies(
        inputs,
        graph_member=values.narrative_graph,
        event_card_member=values.event_cards,
        ledger_member=ledger,
        policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
    )
    kinds = {arc.kind for arc in result.arcs}
    assert {
        "subject_to_fact",
        "participant_to_event",
        "required_fact_to_obligation",
        "obligation_to_beat",
        "obligation_to_story_thread",
        "source_window_to_coverage_window",
        "coverage_window_to_fact",
        "source_to_event",
    } <= kinds
    assert any(ref.member_ref.artifact_type == "event_card_set" and ref.object_type == "event" for ref in result.nodes)


@pytest.mark.parametrize("member_name,change", [
    ("graph", "scope"),
    ("graph", "hash"),
    ("card", "type"),
])
def test_rejects_wrong_pending_member_identity_before_projection(member_name, change):
    inputs, values, ledger = _projection_with_ledger()
    graph, card = values.narrative_graph, values.event_cards
    if member_name == "graph" and change == "scope":
        graph = replace(graph, scope=replace(graph.scope, key="foreign"))
    elif member_name == "graph":
        graph = replace(graph, content_hash="sha256:" + "b" * 64)
    else:
        card = replace(card, artifact_type="foreign_card")
    with pytest.raises(DependencyProjectionError, match="member"):
        project_dependencies(
            inputs,
            graph_member=graph,
            event_card_member=card,
            ledger_member=ledger,
            policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
        )


def test_rejects_wrong_card_content_and_wrong_coverage_window_owner():
    inputs, values, ledger = _projection_with_ledger()
    card_payload = json.loads(values.event_cards.payload_json)
    card_payload["events"][0]["content"] = "forged event content"
    wrong_card = _artifact("event_card_set", values.event_cards.scope, card_payload)
    with pytest.raises(DependencyProjectionError, match="exact EventCard"):
        project_dependencies(
            inputs,
            graph_member=values.narrative_graph,
            event_card_member=wrong_card,
            ledger_member=ledger,
            policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
        )

    ledger_payload = json.loads(ledger.payload_json)
    ledger_payload["windows"][0]["source_ref"]["member_ref"]["content_hash"] = "sha256:" + "b" * 64
    ledger_payload["windows"][0]["source_window_ref"]["member_ref"]["content_hash"] = "sha256:" + "b" * 64
    wrong_ledger = _artifact("coverage_ledger", ledger.scope, ledger_payload)
    with pytest.raises(DependencyProjectionError, match="CoverageWindow"):
        project_dependencies(
            inputs,
            graph_member=values.narrative_graph,
            event_card_member=values.event_cards,
            ledger_member=wrong_ledger,
            policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
        )

    omitted_payload = json.loads(ledger.payload_json)
    omitted_payload["windows"][0]["fact_refs"] = []
    omitted = _artifact("coverage_ledger", ledger.scope, omitted_payload)
    with pytest.raises(DependencyProjectionError, match="exactly retain every raw Fact/Event"):
        project_dependencies(
            inputs,
            graph_member=values.narrative_graph,
            event_card_member=values.event_cards,
            ledger_member=omitted,
            policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
        )


def test_projection_is_deterministic_and_has_no_recursive_precedes_involves_or_conflict_arcs():
    inputs, values, ledger = _projection_with_ledger()
    first = project_dependencies(
        inputs, graph_member=values.narrative_graph, event_card_member=values.event_cards,
        ledger_member=ledger, policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
    )
    second = project_dependencies(
        inputs, graph_member=values.narrative_graph, event_card_member=values.event_cards,
        ledger_member=ledger, policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
    )
    assert first == second
    assert not {"precedes", "involves", "contradicts"} & {arc.kind for arc in first.arcs}


def test_rejects_ledger_that_silently_drops_a_complete_source_window():
    inputs, values, ledger = _projection_with_ledger()
    payload = json.loads(ledger.payload_json)
    removed = inputs.inputs[0].source_window
    removed_fact_ids = {fact.fact_id for fact in inputs.inputs[0].semantic_pack.semantic_pack.facts}
    removed_event_ids = {event.event_id for event in inputs.inputs[0].semantic_pack.semantic_pack.events}
    payload["windows"] = payload["windows"][1:]
    payload["rows"] = [
        row for row in payload["rows"]
        if row["coverage_id"] != "window-0"
        and row["unit_ref"].get("object_id") not in removed_fact_ids | removed_event_ids
    ]
    payload["conservation"] = {
        kind: {
            "input_count": sum(row["unit_type"] == kind for row in payload["rows"]),
            "ledger_count": sum(row["unit_type"] == kind for row in payload["rows"]),
        }
        for kind in ("fact", "event", "source_window", "obligation")
    }
    assert removed.window_manifest_sha256 not in {
        item["source_window_ref"]["object_id"] for item in payload["windows"]
    }
    shortened = _artifact("coverage_ledger", ledger.scope, payload)
    with pytest.raises(DependencyProjectionError, match="exactly cover committed SourceWindows"):
        project_dependencies(
            inputs, graph_member=values.narrative_graph, event_card_member=values.event_cards,
            ledger_member=shortened, policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
        )


def test_rejects_card_domain_omission_before_any_event_arc_is_projected():
    inputs, values, ledger = _projection_with_ledger()
    payload = json.loads(values.event_cards.payload_json)
    payload["events"] = payload["events"][1:]
    missing_card = _artifact("event_card_set", values.event_cards.scope, payload)
    with pytest.raises(DependencyProjectionError, match="Graph/Card Event IDs"):
        project_dependencies(
            inputs, graph_member=values.narrative_graph, event_card_member=missing_card,
            ledger_member=ledger, policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
        )


def test_rejects_graph_fact_domain_omission_as_typed_error_not_lookup_failure():
    inputs, values, ledger = _projection_with_ledger()
    empty_graph = _artifact("narrative_graph", values.narrative_graph.scope, {"graph_id": "empty", "nodes": [], "edges": []})
    empty_cards = _artifact("event_card_set", values.event_cards.scope, {"event_card_set_id": "empty", "events": []})
    with pytest.raises(DependencyProjectionError, match="Graph Fact IDs"):
        project_dependencies(
            inputs, graph_member=empty_graph, event_card_member=empty_cards,
            ledger_member=ledger, policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
        )


def test_every_registered_attribute_direction_and_requires_reverse_is_projected_once():
    inputs, values, ledger = _projection_with_ledger(augment=True)
    result = project_dependencies(
        inputs, graph_member=values.narrative_graph, event_card_member=values.event_cards,
        ledger_member=ledger, policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
    )
    kinds = {arc.kind for arc in result.arcs}
    assert {
        "subject_to_fact", "entity_ref_value_to_fact", "participant_to_event",
        "required_fact_to_obligation", "obligation_to_beat", "obligation_to_story_thread",
        "entity_to_character", "state_fact_to_character", "character_to_character_state",
        "state_fact_to_character_state", "answer_fact_to_question",
        "setup_payoff_event_to_foreshadow", "subject_object_character_to_relationship", "requires",
    } <= kinds
    required = next(arc for arc in result.arcs if arc.kind == "requires")
    assert required.from_ref.object_type == "obligation" and required.to_ref.object_type == "fact"
    assert not {"precedes", "involves", "contradicts"} & kinds
