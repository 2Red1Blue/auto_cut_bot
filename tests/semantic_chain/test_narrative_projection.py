"""Deterministic projection tests for the first three Stage 1 business values."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal, localcontext

import pytest
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.narrative_projection import (
    NarrativeProjectionError,
    _confidence,
    _project_edges,
    project_narrative,
)
from autocut_kernel.semantic_chain.stage1_draft import (
    decode_stage1_draft,
    stage1_draft_prompt_inputs,
)
from autocut_kernel.store import ArtifactScope
from autocut_kernel.store.errors import StoreValidationError
from autocut_kernel.vlm.models import VlmEntityKind, derive_vlm_global_id

from tests.semantic_chain.test_coverage_analysis import _replace_pack
from tests.semantic_chain.test_stage1_draft import POLICY, _draft, _synthetic_inputs


@pytest.fixture
def inputs():
    return _synthetic_inputs()


def _decoded(inputs):
    return decode_stage1_draft(json.dumps(_draft(inputs)).encode(), inputs=inputs, policy=POLICY)


def test_projects_all_observations_and_draft_nodes_without_admission(inputs):
    draft = _decoded(inputs)
    result = project_narrative(inputs, draft, scope=inputs.source_manifest.reference.scope, revision=1)
    event_payload = json.loads(result.event_cards.payload_json)
    digest_payload = json.loads(result.episode_digests.payload_json)
    graph = json.loads(result.narrative_graph.payload_json)

    assert {item["event_id"] for item in event_payload["events"]} == {
        event.event_id for item in inputs.inputs for event in item.semantic_pack.semantic_pack.events
    }
    assert len(digest_payload["digests"]) == 2
    kinds = {item["node_type"] for item in graph["nodes"]}
    assert {"entity", "fact", "event", "beat", "obligation", "story_thread"} <= kinds
    assert {edge["edge_type"] for edge in graph["edges"]} >= {"supports", "involves"}
    assert not hasattr(result, "admission") and not hasattr(result, "coverage")


def test_projection_is_input_order_stable_and_members_are_hash_closed(inputs):
    draft = _decoded(inputs)
    expected = project_narrative(inputs, draft, scope=inputs.source_manifest.reference.scope, revision=1)
    # Constructor itself rejects non-canonical Store inputs.  Reprojecting the
    # valid closure must remain byte-stable instead of accepting a caller order.
    assert expected == project_narrative(inputs, draft, scope=inputs.source_manifest.reference.scope, revision=1)
    with pytest.raises(StoreValidationError):
        replace(inputs, inputs=tuple(reversed(inputs.inputs)))


def test_rejects_scope_substitution_and_missing_source_authorization(inputs):
    draft = _decoded(inputs)
    with pytest.raises(NarrativeProjectionError, match="scope"):
        project_narrative(inputs, draft, scope=ArtifactScope("pipeline", "job", "other"), revision=1)
    denied = replace(
        inputs,
        source_grant=replace(
            inputs.source_grant,
            policy=replace(inputs.source_grant.policy, authorized_purposes=("render_source",)),
        ),
    )
    with pytest.raises(NarrativeProjectionError, match="purpose"):
        project_narrative(denied, draft, scope=inputs.source_manifest.reference.scope, revision=1)


def test_digest_only_cites_summary_declared_observations(inputs):
    draft = _decoded(inputs)
    result = project_narrative(inputs, draft, scope=inputs.source_manifest.reference.scope, revision=1)
    digests = json.loads(result.episode_digests.payload_json)["digests"]
    allowed = {
        item.semantic_pack.semantic_pack.window_manifest_sha256: {
            *item.semantic_pack.semantic_pack.window_summary.fact_refs,
            *item.semantic_pack.semantic_pack.window_summary.event_refs,
        }
        for item in inputs.inputs
    }
    for digest in digests:
        for evidence in digest["evidence_refs"]:
            assert evidence["object_type"] == "source_window" or evidence["object_id"] in set().union(*allowed.values())


def test_empty_window_summary_refs_retain_only_window_provenance(inputs):
    payload = _draft(inputs)
    inputs = _replace_pack(
        inputs,
        1,
        lambda pack: replace(pack, window_summary=replace(pack.window_summary, fact_refs=(), event_refs=())),
    )
    payload["input_binding_sha256"] = stage1_draft_prompt_inputs(inputs, policy=POLICY)["input_binding_sha256"]
    draft = decode_stage1_draft(json.dumps(payload).encode(), inputs=inputs, policy=POLICY)
    result = project_narrative(inputs, draft, scope=inputs.source_manifest.reference.scope, revision=1)
    digest = json.loads(result.episode_digests.payload_json)["digests"][1]
    assert [(item["object_type"], item["object_id"]) for item in digest["evidence_refs"]] == [
        ("source_window", inputs.inputs[1].source_window.window_manifest_sha256)
    ]
    graph = json.loads(result.narrative_graph.payload_json)
    assert sum(node["node_type"] == "fact" for node in graph["nodes"]) == 2
    assert sum(node["node_type"] == "event" for node in graph["nodes"]) == 2
    assert not hasattr(result, "admission") and not hasattr(result, "coverage")


def test_rule_confidence_never_rounds_under_a_low_ambient_decimal_precision():
    with localcontext() as context:
        context.prec = 6
        confidence = _confidence(Decimal("0.123456789012345678901234567890"), method="model")
    assert confidence.value == "0.12345678901234567890123456789"
    assert _confidence(Decimal("-0.000"), method="model").value == "0"


def test_thread_evidence_deduplicates_a_fact_shared_by_two_obligations(inputs):
    payload = _draft(inputs)
    second = dict(payload["obligations"][0])
    second["obligation_id"] = "obligation_2"
    payload["obligations"].append(second)
    payload["story_threads"][0]["obligation_ids"] = ["obligation_1", "obligation_2"]
    draft = decode_stage1_draft(json.dumps(payload).encode(), inputs=inputs, policy=POLICY)
    result = project_narrative(inputs, draft, scope=inputs.source_manifest.reference.scope, revision=1)
    graph = json.loads(result.narrative_graph.payload_json)
    thread = next(item for item in graph["nodes"] if item["node_type"] == "story_thread")
    assert len(thread["evidence_refs"]) == 1


def test_reciprocal_cause_effect_declarations_form_one_edge_with_both_evidences(inputs):
    draft = replace(_decoded(inputs), beats=())
    pack = inputs.inputs[0].semantic_pack.semantic_pack
    original = pack.events[0]
    first_id, second_id = (f"sha256:{digit * 64}" for digit in ("b", "c"))
    first = replace(original, event_id=first_id, cause_event_refs=(), effect_event_refs=(second_id,))
    second = replace(original, event_id=second_id, cause_event_refs=(first_id,), effect_event_refs=())
    owner = inputs.inputs[0].semantic_pack.reference
    identity = SemanticMemberIdentity(
        owner.artifact_type, owner.logical_id, owner.revision, owner.scope, owner.content_hash
    )
    member_ref = SemanticObjectRef(identity, "vlm_event", first_id)
    second_ref = replace(member_ref, object_id=second_id)
    facts = {fact.fact_id: (fact, SemanticObjectRef(member_ref.member_ref, "vlm_fact", fact.fact_id)) for fact in pack.facts}
    edges = _project_edges(
        facts,
        {first_id: (first, member_ref, "episode-1"), second_id: (second, second_ref, "episode-1")},
        draft,
    )
    causes = [edge for edge in edges if edge.edge_type == "causes"]
    assert len(causes) == 1
    assert {ref.object_id for ref in causes[0].evidence_refs} == {first_id, second_id}


def test_four_raw_entity_kinds_and_an_unmentioned_standalone_fact_are_preserved(inputs):
    def observations(pack):
        entities = [pack.entities[0]]
        for index, kind in enumerate(
            (VlmEntityKind.OBJECT, VlmEntityKind.LOCATION, VlmEntityKind.SCREEN_TEXT_SOURCE), start=2
        ):
            entities.append(
                replace(
                    pack.entities[0],
                    local_entity_id=f"entity_{index}",
                    entity_id=derive_vlm_global_id("entity", f"entity_{index}", pack.request_identity_sha256),
                    entity_kind=kind,
                )
            )
        fact = replace(
            pack.facts[0],
            local_fact_id="fact_extra",
            fact_id=derive_vlm_global_id("fact", "fact_extra", pack.request_identity_sha256),
        )
        return replace(pack, entities=tuple(entities), facts=(*pack.facts, fact))

    inputs = _replace_pack(inputs, 1, observations)
    draft = _decoded(inputs)
    result = project_narrative(inputs, draft, scope=inputs.source_manifest.reference.scope, revision=1)
    graph = json.loads(result.narrative_graph.payload_json)
    raw_pack = inputs.inputs[1].semantic_pack.semantic_pack
    kinds = {
        node["attributes"]["entity_kind"]
        for node in graph["nodes"]
        if node["node_type"] == "entity"
    }
    standalone = next(fact for fact in raw_pack.facts if fact.local_fact_id == "fact_extra")
    assert kinds == {"person", "object", "location", "screen_text_source"}
    assert any(node["node_id"] == standalone.fact_id for node in graph["nodes"])
    assert all(edge["from_node_id"] != standalone.fact_id for edge in graph["edges"])


def test_eventless_window_keeps_its_fact_without_fabricating_an_event(inputs):
    payload = _draft(inputs)
    payload["merge_proposals"] = []
    inputs = _replace_pack(
        inputs,
        1,
        lambda pack: replace(
            pack,
            events=(),
            candidate_hypotheses=(),
            window_summary=replace(pack.window_summary, event_refs=()),
        ),
    )
    payload["input_binding_sha256"] = stage1_draft_prompt_inputs(inputs, policy=POLICY)["input_binding_sha256"]
    draft = decode_stage1_draft(json.dumps(payload).encode(), inputs=inputs, policy=POLICY)
    result = project_narrative(inputs, draft, scope=inputs.source_manifest.reference.scope, revision=1)
    graph = json.loads(result.narrative_graph.payload_json)
    assert len(json.loads(result.event_cards.payload_json)["events"]) == 1
    assert sum(node["node_type"] == "fact" for node in graph["nodes"]) == 2


def test_rejects_changed_source_hash_and_same_global_id_with_a_wrong_pack_owner(inputs):
    draft = _decoded(inputs)
    changed_window = replace(
        inputs.inputs[0].source_window,
        source_sha256="sha256:" + "b" * 64,
    )
    changed_inputs = replace(inputs, inputs=(replace(inputs.inputs[0], source_window=changed_window), *inputs.inputs[1:]))
    with pytest.raises(NarrativeProjectionError, match="source grant"):
        project_narrative(changed_inputs, draft, scope=inputs.source_manifest.reference.scope, revision=1)

    wrong_owner = replace(
        draft.beats[0].event_refs[0],
        window_manifest_sha256=inputs.inputs[1].source_window.window_manifest_sha256,
    )
    wrong_draft = replace(draft, beats=(replace(draft.beats[0], event_refs=(wrong_owner,)),))
    with pytest.raises(NarrativeProjectionError, match="wrong source window owner"):
        project_narrative(inputs, wrong_draft, scope=inputs.source_manifest.reference.scope, revision=1)


def test_event_card_preserves_the_complete_raw_coarse_mapping(inputs):
    draft = _decoded(inputs)
    result = project_narrative(inputs, draft, scope=inputs.source_manifest.reference.scope, revision=1)
    card = json.loads(result.event_cards.payload_json)["events"][0]
    raw_event = inputs.inputs[0].semantic_pack.semantic_pack.events[0]
    assert card["source_range_refs"][0]["mapped_interval"] == raw_event.support.source_interval.to_mapping()
