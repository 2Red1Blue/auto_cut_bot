"""Synthetic Stage 1 business values; no accepted identity or Store proof."""

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal

import pytest
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.narrative_models import (
    BeatAttributes,
    CharacterAttributes,
    CharacterStateAttributes,
    CoarseSourceRange,
    Confidence,
    EntityAttributes,
    EpisodeDigest,
    EpisodeDigestSet,
    EventAttributes,
    EventCard,
    EventCardSet,
    FactAttributes,
    FactBooleanValue,
    FactEntityRefValue,
    FactNumberValue,
    FactTextValue,
    ForeshadowAttributes,
    GraphEdge,
    GraphNode,
    NarrativeGraph,
    NarrativeModelError,
    ObligationAttributes,
    QuestionAttributes,
    RelationshipAttributes,
    StoryThreadAttributes,
)
from autocut_kernel.store import ArtifactScope
from autocut_kernel.vlm.models import MappedSourceInterval


def _hash(value):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


SCOPE = ArtifactScope("pipeline", "job", "synthetic-model-test")
SOURCE = SemanticMemberIdentity("whole_series_source_manifest", "source", 1, SCOPE, _hash("source"))
VLM = SemanticMemberIdentity("vlm_semantic_pack", "pack", 1, SCOPE, _hash("vlm"))
SOURCE_REF = SemanticObjectRef(SOURCE, "source", "source-one")
WINDOW_REF = SemanticObjectRef(SOURCE, "source_window", _hash("window"))
RAW_EVENT = SemanticObjectRef(VLM, "vlm_event", _hash("event"))
RAW_FACT = SemanticObjectRef(VLM, "vlm_fact", _hash("fact"))
RAW_ENTITY = SemanticObjectRef(VLM, "vlm_entity", _hash("entity"))
CONFIDENCE = Confidence("0.9", "model")
RANGE = CoarseSourceRange(
    SOURCE_REF,
    "source-video",
    MappedSourceInterval(
        TickRange(-3, 20),
        2,
        TimeBase(1, 1000),
        3,
        TimeBase(1, 100),
    ),
)
CARD = EventCard("event", "episode-one", "找到钥匙", (RANGE,), (RAW_EVENT,))
CARDS = EventCardSet("events", (CARD,))
EVENT_OWNER = SemanticMemberIdentity("event_card_set", "events", 1, SCOPE, CARDS.canonical_hash)
EVENT_REF = SemanticObjectRef(EVENT_OWNER, "event", "event")
RANGE_REF = SemanticObjectRef(EVENT_OWNER, "source_range", "event:range:0")
DIGEST = EpisodeDigest("episode-one", 1, "Discovery", (WINDOW_REF,), (EVENT_REF,))
DIGESTS = EpisodeDigestSet("digests", (DIGEST,))


def _node(node_id, kind, attrs, evidence=(RAW_FACT,)):
    return GraphNode(node_id, kind, "Original summary: " + node_id, attrs, evidence, CONFIDENCE)


def _graph():
    entities = tuple(
        _node(kind, "entity", EntityAttributes(kind, kind, "Visible " + kind), (RAW_ENTITY,))
        for kind in ("person", "object", "location", "screen_text_source")
    )
    nodes = (
        *entities,
        _node(
            "person_two",
            "entity",
            EntityAttributes("person", "Other", "Another person"),
            (RAW_ENTITY,),
        ),
        _node(
            "fact",
            "fact",
            FactAttributes("person", "visible_state", FactTextValue("Waiting"), "none"),
        ),
        _node(
            "object_fact",
            "fact",
            FactAttributes("object", "visible_state", FactNumberValue("2.5"), "none"),
        ),
        _node(
            "relation_fact",
            "fact",
            FactAttributes("person", "visible_relation", FactEntityRefValue("object"), "none"),
        ),
        _node(
            "text_fact",
            "fact",
            FactAttributes("screen_text_source", "screen_text", FactTextValue("出口"), "none"),
        ),
        _node(
            "standalone_fact",
            "fact",
            FactAttributes("location", "scene_context", FactBooleanValue(True), "none"),
        ),
        _node(
            "event",
            "event",
            EventAttributes(
                EVENT_REF, "episode-one", "找到钥匙", (RANGE_REF,), ("person", "object")
            ),
            (EVENT_REF,),
        ),
        _node(
            "obligation",
            "obligation",
            ObligationAttributes("Show state", ("fact",), "Retain evidence"),
        ),
        _node("beat", "beat", BeatAttributes("Reveal", "reveal", ("obligation",))),
        _node(
            "thread",
            "story_thread",
            StoryThreadAttributes("Discovery", "A key appears", ("obligation",)),
            (),
        ),
        _node(
            "character",
            "character",
            CharacterAttributes("Person", (), ("fact",), ("person",), (RAW_ENTITY,)),
        ),
        _node(
            "character_two",
            "character",
            CharacterAttributes("Other", (), (), ("person_two",), (RAW_ENTITY,)),
        ),
        _node(
            "state", "character_state", CharacterStateAttributes("character", WINDOW_REF, ("fact",))
        ),
        _node(
            "relationship",
            "relationship",
            RelationshipAttributes("character", "character_two", "unknown"),
        ),
        _node("question", "question", QuestionAttributes("What state?", "answered", ("fact",))),
        _node("foreshadow", "foreshadow", ForeshadowAttributes(("event",), (), "setup_only")),
    )
    edges = (
        GraphEdge("edge_support", "supports", "fact", "event", (RAW_FACT,)),
        GraphEdge("edge_involves", "involves", "event", "person", ()),
    )
    return NarrativeGraph("graph", nodes, edges)


def _change(graph, node_id, **changes):
    return replace(
        graph,
        nodes=tuple(
            replace(node, attributes=replace(node.attributes, **changes))
            if node.node_id == node_id
            else node
            for node in graph.nodes
        ),
    )


def _oracle(value):
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
    )


@pytest.mark.parametrize("value", [CARDS, DIGESTS, _graph()])
def test_complete_business_values_closed_roundtrip_and_independent_jcs_hash(value):
    mapping = value.to_mapping()
    assert type(value).from_mapping(mapping) == value
    assert value.canonical_hash == _oracle(mapping)
    assert set(mapping) == {"event_card_set_id", "events"} if type(value) is EventCardSet else True
    assert not hasattr(value, "admission") and not hasattr(value, "accepted")


def test_all_variants_and_four_entity_kinds_and_standalone_fact_survive_roundtrip():
    graph = NarrativeGraph.from_mapping(_graph().to_mapping())
    assert {node.node_type for node in graph.nodes} == {
        "entity",
        "fact",
        "event",
        "beat",
        "obligation",
        "story_thread",
        "character",
        "character_state",
        "relationship",
        "question",
        "foreshadow",
    }
    assert {node.attributes.entity_kind for node in graph.nodes if node.node_type == "entity"} == {
        "person",
        "object",
        "location",
        "screen_text_source",
    }
    assert {type(node.attributes.value) for node in graph.nodes if node.node_type == "fact"} == {
        FactTextValue,
        FactNumberValue,
        FactBooleanValue,
        FactEntityRefValue,
    }
    standalone = next(node for node in graph.nodes if node.node_id == "standalone_fact")
    assert standalone.label == "Original summary: standalone_fact"
    assert all(edge.from_node_id != standalone.node_id for edge in graph.edges)


def test_coarse_range_retains_complete_vlm_mapping_not_physical_endpoints():
    mapping = RANGE.to_mapping()
    assert set(mapping) == {"source_ref", "clock_id", "mapped_interval"}
    assert mapping["mapped_interval"] == RANGE.mapped_interval.to_mapping()
    assert mapping["mapped_interval"]["semantic_precision"] == "coarse_only"
    assert mapping["mapped_interval"]["mapping_error_bound"]["tick"] == 2
    assert mapping["mapped_interval"]["provider_uncertainty"]["tick"] == 3
    assert CoarseSourceRange.from_mapping(mapping) == RANGE
    assert RANGE.mapped_interval.coarse_range.start_pts == -3


@pytest.mark.parametrize("value", ["0", "1", "0.01", "0.12345678901234567890123456789"])
def test_confidence_is_canonical_exact_decimal_without_float_rounding(value):
    result = Confidence(value, "source")
    assert result.value == value
    assert Confidence.from_mapping(result.to_mapping()) == result


@pytest.mark.parametrize("value", ["-12.003", "0", "23", "123456789012345678901234567890.001"])
def test_fact_number_retains_exact_decimal_text(value):
    assert FactNumberValue(value).to_mapping() == {"kind": "number", "number": value}


@pytest.mark.parametrize(
    "value",
    [
        0.9,
        1,
        True,
        Decimal("0.9"),
        "NaN",
        "Infinity",
        "-Infinity",
        "1e-3",
        "01",
        ".5",
        "1.",
        "0.90",
        "1.0",
        "-0",
        "-0.0",
        " 1",
        "1\n",
        "",
    ],
)
def test_noncanonical_decimal_and_numeric_types_rejected(value):
    with pytest.raises(NarrativeModelError):
        Confidence(value, "model")
    with pytest.raises(NarrativeModelError):
        FactNumberValue(value)


@pytest.mark.parametrize("value", ["-0.1", "1.01", "2"])
def test_confidence_bounds(value):
    with pytest.raises(NarrativeModelError):
        Confidence(value, "model")


@pytest.mark.parametrize("value", [0, 1, "true", None, [], {}])
def test_fact_boolean_never_coerces_other_types(value):
    with pytest.raises(NarrativeModelError):
        FactBooleanValue(value)


@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1", 2**53])
def test_digest_ordinal_positive_safe_int(value):
    with pytest.raises(NarrativeModelError):
        replace(DIGEST, ordinal=value)


@pytest.mark.parametrize("field", ["episode_id", "ordinal"])
def test_digest_set_unique_episode_and_ordinal(field):
    other = replace(DIGEST, episode_id="two", ordinal=2)
    other = replace(other, **{field: getattr(DIGEST, field)})
    with pytest.raises(NarrativeModelError):
        EpisodeDigestSet("digests", (DIGEST, other))


def test_set_and_graph_canonical_order_is_deterministic():
    graph = _graph()
    assert (
        replace(graph, nodes=tuple(reversed(graph.nodes)), edges=tuple(reversed(graph.edges)))
        == graph
    )
    other = replace(DIGEST, episode_id="two", ordinal=2)
    assert EpisodeDigestSet("digests", (other, DIGEST)) == EpisodeDigestSet(
        "digests", (DIGEST, other)
    )
    other_card = replace(CARD, event_id="other")
    assert EventCardSet("cards", (other_card, CARD)) == EventCardSet("cards", (CARD, other_card))


@pytest.mark.parametrize(
    "node_id,field,value",
    [
        ("fact", "subject_node_id", "missing"),
        ("fact", "subject_node_id", "event"),
        ("relation_fact", "value", FactEntityRefValue("missing")),
        ("relation_fact", "value", FactEntityRefValue("event")),
        ("event", "participant_node_ids", ("missing",)),
        ("event", "participant_node_ids", ("fact",)),
        ("beat", "obligation_ids", ("fact",)),
        ("thread", "obligation_ids", ("missing",)),
        ("obligation", "required_fact_ids", ("person",)),
        ("obligation", "required_fact_ids", ("missing",)),
        ("character", "entity_node_ids", ("object",)),
        ("character", "entity_node_ids", ("fact",)),
        ("character", "state_fact_ids", ("object_fact",)),
        ("character", "state_fact_ids", ("missing",)),
        ("state", "character_node_id", "person"),
        ("state", "character_node_id", "missing"),
        ("state", "state_fact_ids", ("object_fact",)),
        ("state", "state_fact_ids", ("missing",)),
        ("relationship", "subject_node_id", "person"),
        ("relationship", "object_node_id", "missing"),
        ("question", "answer_fact_ids", ("event",)),
        ("question", "answer_fact_ids", ("missing",)),
        ("foreshadow", "setup_event_ids", ("person",)),
        ("foreshadow", "payoff_event_ids", ("missing",)),
    ],
)
def test_every_local_reference_checks_existence_and_target_variant(node_id, field, value):
    with pytest.raises(NarrativeModelError):
        _change(_graph(), node_id, **{field: value})


def test_state_is_real_graph_node_bound_to_character_entities_not_assumed_state_truth():
    graph = _graph()
    state = next(node for node in graph.nodes if node.node_type == "character_state")
    assert state.node_id == "state"
    assert state.attributes.source_window_ref == WINDOW_REF
    assert state.attributes.character_node_id == "character"
    # Kind/window proof is an evaluator input, not guessed by the value model.
    assert _change(graph, "fact", predicate="visible_action")
    with pytest.raises(NarrativeModelError):
        _change(graph, "state", state_fact_ids=())
    without_state = replace(
        graph, nodes=tuple(node for node in graph.nodes if node.node_id != "state")
    )
    assert _change(without_state, "character", state_fact_ids=())


def test_noncontainer_node_requires_evidence_but_story_thread_may_be_pure_container():
    for node in _graph().nodes:
        if node.node_type == "story_thread":
            assert replace(node, evidence_refs=()).evidence_refs == ()
        else:
            with pytest.raises(NarrativeModelError):
                replace(node, evidence_refs=())
    with pytest.raises(NarrativeModelError):
        _change(_graph(), "character", identity_evidence_refs=())


def test_closed_node_variant_rejects_dictionary_wrong_variant_and_wrong_node_type():
    node = _graph().nodes[0]
    for attrs in ({}, FactTextValue("text"), Confidence("1", "rule")):
        with pytest.raises(NarrativeModelError):
            replace(node, attributes=attrs)
    with pytest.raises(NarrativeModelError):
        replace(node, node_type="invented")


def test_exact_eventcard_owner_range_id_and_event_identity():
    event = next(node for node in _graph().nodes if node.node_type == "event")
    with pytest.raises(NarrativeModelError):
        replace(event, node_id="foreign")
    for ref in (
        replace(EVENT_REF, object_type="vlm_event"),
        replace(EVENT_REF, member_ref=VLM),
        replace(EVENT_REF, member_ref=replace(EVENT_OWNER, content_hash=_hash("other"))),
    ):
        with pytest.raises(NarrativeModelError):
            replace(event.attributes, event_card_ref=ref)
    for ref in (
        replace(RANGE_REF, object_id="event:range:1"),
        replace(RANGE_REF, object_id="foreign:range:0"),
        replace(RANGE_REF, object_type="event"),
        replace(RANGE_REF, member_ref=replace(EVENT_OWNER, revision=2)),
    ):
        with pytest.raises(NarrativeModelError):
            replace(event.attributes, source_range_refs=(ref,))
    foreign = replace(EVENT_OWNER, logical_id="foreign-set")
    attrs = EventAttributes(
        SemanticObjectRef(foreign, "event", "other_event"),
        "episode-one",
        "Other",
        (SemanticObjectRef(foreign, "source_range", "other_event:range:0"),),
        (),
    )
    with pytest.raises(NarrativeModelError, match="one exact"):
        replace(_graph(), nodes=(*_graph().nodes, _node("other_event", "event", attrs)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("member_ref", EVENT_OWNER),
        ("object_type", "coverage_window"),
        ("object_id", "not-a-window-hash"),
    ],
)
def test_window_refs_are_source_owned_not_ledger_coverage_alias(field, value):
    ref = replace(WINDOW_REF, **{field: value})
    with pytest.raises(NarrativeModelError):
        replace(DIGEST, source_window_refs=(ref,))
    with pytest.raises(NarrativeModelError):
        CharacterStateAttributes("character", ref, ("fact",))


def test_source_range_wrong_owner_and_direct_type_rejected():
    for ref in (replace(SOURCE_REF, member_ref=VLM), replace(SOURCE_REF, object_type="event")):
        with pytest.raises(NarrativeModelError):
            replace(RANGE, source_ref=ref)
    with pytest.raises(NarrativeModelError):
        replace(RANGE, mapped_interval=RANGE.mapped_interval.to_mapping())
    with pytest.raises(NarrativeModelError):
        replace(
            RANGE, mapped_interval=replace(RANGE.mapped_interval, coarse_range=TickRange(0, 2**53))
        )


def test_cycles_and_conflicting_edges_are_retained_for_independent_diagnostics():
    graph = _graph()
    extra = (
        GraphEdge("cycle_one", "causes", "person", "fact", ()),
        GraphEdge("cycle_two", "causes", "fact", "person", ()),
        GraphEdge("conflict", "contradicts", "fact", "relation_fact", ()),
    )
    value = replace(graph, edges=(*graph.edges, *extra))
    assert len(value.edges) == len(graph.edges) + 3
    assert NarrativeGraph.from_mapping(value.to_mapping()) == value


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_node",
        "duplicate_edge",
        "node_edge_collision",
        "missing_from",
        "missing_to",
        "bad_edge_kind",
    ],
)
def test_graph_id_and_edge_closure(change):
    graph = _graph()
    with pytest.raises(NarrativeModelError):
        if change == "duplicate_node":
            replace(graph, nodes=(*graph.nodes, graph.nodes[0]))
        elif change == "duplicate_edge":
            replace(graph, edges=(*graph.edges, graph.edges[0]))
        else:
            field, value = {
                "node_edge_collision": ("edge_id", "fact"),
                "missing_from": ("from_node_id", "missing"),
                "missing_to": ("to_node_id", "missing"),
                "bad_edge_kind": ("edge_type", "identity_merge"),
            }[change]
            replace(graph, edges=(replace(graph.edges[0], **{field: value}),))


def _all_values():
    graph = _graph()
    return (
        RANGE,
        CARD,
        CARDS,
        DIGEST,
        DIGESTS,
        CONFIDENCE,
        graph,
        *graph.nodes,
        *graph.edges,
        *(node.attributes for node in graph.nodes),
        *(node.attributes.value for node in graph.nodes if node.node_type == "fact"),
    )


@pytest.mark.parametrize("value", _all_values(), ids=lambda value: type(value).__name__)
def test_all_direct_text_fields_reject_actual_wrong_types_or_invalid_unicode(value):
    for field in fields(value):
        if type(getattr(value, field.name)) is str:
            for invalid in (True, 1.0, "", "\ud800"):
                with pytest.raises(NarrativeModelError):
                    replace(value, **{field.name: invalid})


@pytest.mark.parametrize("value", _all_values(), ids=lambda value: type(value).__name__)
def test_all_direct_tuple_fields_reject_mutable_lists_and_wrong_members(value):
    for field in fields(value):
        current = getattr(value, field.name)
        if type(current) is tuple:
            for invalid in (list(current), (None,)):
                with pytest.raises(NarrativeModelError):
                    replace(value, **{field.name: invalid})


def _dict_paths(value, path=()):
    if type(value) is dict:
        yield path
        for key, child in value.items():
            yield from _dict_paths(child, (*path, key))
    elif type(value) is list:
        for index, child in enumerate(value):
            yield from _dict_paths(child, (*path, index))


@pytest.mark.parametrize("value", [CARDS, DIGESTS, _graph()])
def test_every_nested_wire_object_rejects_unknown_and_missing_fields(value):
    original = value.to_mapping()
    for path in _dict_paths(original):
        for change in ("unknown", "missing"):
            wire = deepcopy(original)
            target = wire
            for part in path:
                target = target[part]
            if change == "unknown":
                target["accepted"] = True
            else:
                target.pop(next(iter(target)))
            with pytest.raises(NarrativeModelError):
                type(value).from_mapping(wire)


@pytest.mark.parametrize(
    "mutate",
    [
        "precision",
        "source_clock",
        "proxy_clock",
        "source_base",
        "empty",
        "float",
        "bool",
        "negative_error",
        "negative_uncertainty",
        "extra_endpoint",
    ],
)
def test_coarse_range_decoder_rejects_drift_and_physical_claims(mutate):
    wire = RANGE.to_mapping()
    mapped = wire["mapped_interval"]
    if mutate == "precision":
        mapped["semantic_precision"] = "exact"
    elif mutate == "source_clock":
        mapped["mapping_error_bound"]["clock"] = "proxy"
    elif mutate == "proxy_clock":
        mapped["provider_uncertainty"]["clock"] = "source"
    elif mutate == "source_base":
        mapped["mapping_error_bound"]["time_base"]["denominator"] = 100
    elif mutate == "empty":
        mapped["coarse_range"]["end_pts"] = mapped["coarse_range"]["start_pts"]
    elif mutate == "float":
        mapped["coarse_range"]["start_pts"] = 1.5
    elif mutate == "bool":
        mapped["coarse_range"]["end_pts"] = True
    elif mutate == "negative_error":
        mapped["mapping_error_bound"]["tick"] = -1
    elif mutate == "negative_uncertainty":
        mapped["provider_uncertainty"]["tick"] = -1
    else:
        wire["snap_endpoint"] = "claimed"
    with pytest.raises(NarrativeModelError):
        CoarseSourceRange.from_mapping(wire)


@pytest.mark.parametrize(
    "variant",
    [
        "fact",
        "event",
        "entity",
        "character",
        "character_state",
        "relationship",
        "question",
        "foreshadow",
        "beat",
    ],
)
def test_attributes_unknown_enum_rejected_at_wire_boundary(variant):
    node = next(node for node in _graph().nodes if node.node_type == variant)
    wire = node.to_mapping()
    fields_by_kind = {
        "fact": "conflict_status",
        "entity": "entity_kind",
        "relationship": "relation_type",
        "question": "status",
        "foreshadow": "status",
        "beat": "phase",
    }
    field = fields_by_kind.get(variant, "attribute_type")
    wire["attributes"][field] = "unsupported"
    with pytest.raises(NarrativeModelError):
        GraphNode.from_mapping(wire)


def test_frozen_nested_types_and_fresh_deep_mappings():
    graph = _graph()
    for value in _all_values():
        field = fields(value)[0]
        with pytest.raises(FrozenInstanceError):
            setattr(value, field.name, getattr(value, field.name))
    original = graph.to_mapping()
    parsed = NarrativeGraph.from_mapping(original)
    original["nodes"][0]["evidence_refs"][0]["member_ref"]["scope"]["key"] = "changed"
    assert parsed == graph
    assert graph.to_mapping() != original
    source = RANGE.to_mapping()
    source["mapped_interval"]["mapping_error_bound"]["time_base"]["numerator"] = 999
    assert RANGE.to_mapping() != source


def test_empty_sets_are_content_only_and_do_not_claim_input_conservation():
    for value in (
        EventCardSet("empty", ()),
        EpisodeDigestSet("empty", ()),
        NarrativeGraph("empty", (), ()),
    ):
        assert type(value).from_mapping(value.to_mapping()) == value
        assert not hasattr(value, "coverage_admission")


@pytest.mark.parametrize(
    "artifact_type,object_type",
    [
        ("narrative_graph", "fact"),
        ("coverage_ledger", "coverage_window"),
        ("coverage_admission", "admission"),
        ("evidence_diagnostics", "diagnostic"),
        ("conflict_diagnostics", "claim"),
        ("dependency_closure_proof", "closure"),
        ("transcript_set", "asr"),
        ("speech_activity_set", "vad"),
        ("whole_series_source_manifest", "source_grant"),
        ("vlm_semantic_pack", "candidate"),
        ("vlm_semantic_pack", "event"),
        ("event_card_set", "vlm_event"),
        ("episode_digest_set", "event"),
    ],
)
def test_backward_self_future_and_nonsemantic_evidence_owners_are_rejected(
    artifact_type, object_type
):
    owner = SemanticMemberIdentity(artifact_type, "foreign", 1, SCOPE, _hash("foreign"))
    ref = SemanticObjectRef(owner, object_type, _hash("object"))
    node = next(node for node in _graph().nodes if node.node_type == "entity")
    character = next(node.attributes for node in _graph().nodes if node.node_type == "character")
    for value, field in (
        (CARD, "evidence_refs"),
        (DIGEST, "evidence_refs"),
        (node, "evidence_refs"),
        (_graph().edges[0], "evidence_refs"),
        (character, "identity_evidence_refs"),
    ):
        with pytest.raises(NarrativeModelError, match="owner"):
            replace(value, **{field: (ref,)})


def test_card_digest_and_graph_have_distinct_closed_earlier_owner_sets():
    with pytest.raises(NarrativeModelError):
        replace(CARD, evidence_refs=(EVENT_REF,))
    with pytest.raises(NarrativeModelError):
        replace(CARD, evidence_refs=(RAW_FACT,))
    with pytest.raises(NarrativeModelError):
        replace(DIGEST, evidence_refs=(RAW_ENTITY,))
    assert replace(DIGEST, evidence_refs=(RAW_FACT, RAW_EVENT, WINDOW_REF, EVENT_REF))
    owner = SemanticMemberIdentity(
        "episode_digest_set", "digests", 1, SCOPE, DIGESTS.canonical_hash
    )
    digest_ref = SemanticObjectRef(owner, "episode_digest", DIGEST.episode_id)
    with pytest.raises(NarrativeModelError):
        replace(DIGEST, evidence_refs=(digest_ref,))
    node = next(node for node in _graph().nodes if node.node_type == "entity")
    assert replace(
        node,
        evidence_refs=(
            SOURCE_REF,
            WINDOW_REF,
            RAW_ENTITY,
            RAW_FACT,
            RAW_EVENT,
            EVENT_REF,
            RANGE_REF,
            digest_ref,
        ),
    )


@pytest.mark.parametrize(
    "ref",
    [
        replace(RAW_EVENT, object_id="local_event_id"),
        replace(WINDOW_REF, object_id="local_window_id"),
    ],
)
def test_observation_global_ids_are_not_replaced_with_unbound_local_ids(ref):
    with pytest.raises(NarrativeModelError, match="global hash"):
        replace(DIGEST, evidence_refs=(ref,))


def test_duplicate_reference_sets_are_rejected_not_silently_deduplicated():
    node = next(node for node in _graph().nodes if node.node_type == "entity")
    for value, field, duplicate in (
        (CARD, "evidence_refs", (RAW_EVENT, RAW_EVENT)),
        (CARD, "source_range_refs", (RANGE, RANGE)),
        (DIGEST, "source_window_refs", (WINDOW_REF, WINDOW_REF)),
        (node, "evidence_refs", (RAW_ENTITY, RAW_ENTITY)),
        (
            BeatAttributes("Beat", "setup", ("obligation",)),
            "obligation_ids",
            ("obligation", "obligation"),
        ),
    ):
        with pytest.raises(NarrativeModelError):
            replace(value, **{field: duplicate})
    with pytest.raises(NarrativeModelError):
        EventCardSet("cards", (CARD, CARD))


@pytest.mark.parametrize(
    "model",
    [
        EventCardSet,
        EpisodeDigestSet,
        NarrativeGraph,
        GraphNode,
        GraphEdge,
        Confidence,
        CoarseSourceRange,
    ],
)
@pytest.mark.parametrize("value", [None, [], (), True, "{}"])
def test_closed_mapping_boundary_does_not_coerce_wrong_containers(model, value):
    with pytest.raises(NarrativeModelError):
        model.from_mapping(value)


def test_subclasses_cannot_smuggle_different_runtime_shape_into_direct_models():
    class CustomConfidence(Confidence):
        pass

    class CustomEntity(EntityAttributes):
        pass

    class CustomRange(CoarseSourceRange):
        pass

    class Text(str):
        pass

    node = next(node for node in _graph().nodes if node.node_type == "entity")
    for field, value in (
        ("confidence", CustomConfidence("1", "source")),
        ("attributes", CustomEntity("person", "Person", "Visible person")),
        ("label", Text("label")),
    ):
        with pytest.raises(NarrativeModelError):
            replace(node, **{field: value})
    with pytest.raises(NarrativeModelError):
        replace(
            CARD,
            source_range_refs=(CustomRange(SOURCE_REF, RANGE.clock_id, RANGE.mapped_interval),),
        )
