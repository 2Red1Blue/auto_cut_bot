"""Independently check factual Stage 1 values against VLM observations/draft.

Never calls the narrative/coverage producer. These five checks do not establish
Store commitment, Ledger truth, dependency completeness or a Stage admission.
"""

from __future__ import annotations

from decimal import Decimal

from ..contracts.compiler.canonical import canonical_json_hash
from ..store.models import ArtifactMember, CommittedSemanticInputs
from .core_observations import (
    CoreEntity,
    CoreEvent,
    CoreFact,
    observation_confidence,
    observation_source_interval,
    semantic_pack,
)
from .coverage_analysis import Stage1CoveragePolicy
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .narrative_models import (
    BeatAttributes,
    CharacterAttributes,
    CharacterStateAttributes,
    CoarseSourceRange,
    Confidence,
    EntityAttributes,
    EpisodeDigest,
    EventAttributes,
    EventCard,
    FactAttributes,
    FactEntityRefValue,
    FactTextValue,
    GraphEdge,
    GraphNode,
    ObligationAttributes,
    StoryThreadAttributes,
)
from .stage1_checks import Stage1Check
from .stage1_draft import Stage1DraftPolicy, decode_stage1_draft
from .stage1_members import decode_coverage_members

FACTUAL_RULES = ("KC-GRAPH-001", "KC-GRAPH-002", "KC-AUTH-001", "KC-AUTH-002", "KC-EVENT-001")


def verify_factual_members(
    inputs: CommittedSemanticInputs, raw_draft: bytes, *,
    members: tuple[ArtifactMember, ...], draft_policy: Stage1DraftPolicy,
    coverage_policy: Stage1CoveragePolicy,
) -> tuple[Stage1Check, ...]:
    """Read every expected observation and compare full values, not producer hashes."""
    if type(coverage_policy) is not Stage1CoveragePolicy:  # noqa: E721
        raise ValueError("factual checks require an explicit coverage policy")
    draft = decode_stage1_draft(raw_draft, inputs=inputs, policy=draft_policy)
    values = decode_coverage_members(members, scope=inputs.source_manifest.reference.scope)
    source = inputs.source_manifest.reference
    source_owner = SemanticMemberIdentity(
        source.artifact_type, source.logical_id, source.revision, source.scope, source.content_hash,
    )
    card_owner, digest_owner = values.identity("event_card_set"), values.identity("episode_digest_set")
    errors: dict[str, set[str]] = {rule: set() for rule in FACTUAL_RULES}

    def identity(kind: str, local_id: str = "") -> str:
        # Registered ID format, independent of the producer's helper.
        return canonical_json_hash({
            "schema_version": "stage1-narrative-projection-id-v1",
            "input_binding_sha256": draft.input_binding_sha256, "kind": kind, "local_id": local_id,
        })

    entities: dict[str, tuple[CoreEntity, SemanticObjectRef]] = {}
    facts: dict[str, tuple[CoreFact, SemanticObjectRef]] = {}
    events: dict[str, tuple[CoreEvent, SemanticObjectRef, str]] = {}
    expected_cards: list[EventCard] = []
    known_refs: set[SemanticObjectRef] = set()
    episodes: dict[int, list[tuple[str, SemanticObjectRef, tuple[SemanticObjectRef, ...]]]] = {}
    threshold = Decimal(coverage_policy.minimum_confidence)
    authorized = {(item.source_id, item.content_sha256) for item in inputs.source_grant.sources}
    try:
        inputs.source_grant.require_purpose("semantic_analysis")
    except ValueError:
        errors["KC-AUTH-001"].add("semantic_analysis_not_authorized")
    for item in inputs.inputs:
        window, pack = item.source_window, semantic_pack(item)
        if (window.source_id, window.source_sha256) not in authorized:
            errors["KC-AUTH-001"].add("source_hash_not_authorized")
        ref = item.semantic_pack.reference
        owner = SemanticMemberIdentity(ref.artifact_type, ref.logical_id, ref.revision, ref.scope, ref.content_hash)
        source_ref = SemanticObjectRef(source_owner, "source", window.source_id)
        window_ref = SemanticObjectRef(source_owner, "source_window", window.window_manifest_sha256)
        known_refs.update((source_ref, window_ref))
        for entity in pack.entities:
            raw_ref = SemanticObjectRef(owner, "vlm_entity", entity.entity_id)
            entities[entity.entity_id] = (entity, raw_ref)
            known_refs.add(raw_ref)
        for fact in pack.facts:
            raw_ref = SemanticObjectRef(owner, "vlm_fact", fact.fact_id)
            facts[fact.fact_id] = (fact, raw_ref)
            known_refs.add(raw_ref)
        for event in pack.events:
            raw_ref = SemanticObjectRef(owner, "vlm_event", event.event_id)
            events[event.event_id] = (event, raw_ref, f"episode-{window.episode_index + 1}")
            known_refs.update((raw_ref, SemanticObjectRef(card_owner, "event", event.event_id),
                               SemanticObjectRef(card_owner, "source_range", f"{event.event_id}:range:0")))
            expected_cards.append(EventCard(
                event.event_id, f"episode-{window.episode_index + 1}", event.summary,
                (CoarseSourceRange(source_ref, window.source_clock_id, observation_source_interval(event)),), (raw_ref,),
            ))
        summary = pack.window_summary
        if observation_confidence(summary) < threshold:
            errors["KC-GRAPH-002"].add("summary_low_confidence")
        if not summary.fact_refs and not summary.event_refs:
            errors["KC-GRAPH-002"].add("summary_missing_evidence")
        support = tuple(SemanticObjectRef(owner, "vlm_fact", key) for key in summary.fact_refs) + tuple(
            SemanticObjectRef(card_owner, "event", key) for key in summary.event_refs
        )
        episodes.setdefault(window.episode_index, []).append((summary.summary, window_ref, support or (window_ref,)))

    expected_digests = tuple(EpisodeDigest(
        f"episode-{index + 1}", ordinal, "\n".join(item[0] for item in windows),
        tuple(item[1] for item in windows), tuple(ref for item in windows for ref in item[2]),
    ) for ordinal, (index, windows) in enumerate(sorted(episodes.items()), start=1))
    known_refs.update(SemanticObjectRef(digest_owner, "episode_digest", item.episode_id) for item in expected_digests)
    if (values.episode_digests.digests != expected_digests
            or values.episode_digests.episode_digest_set_id != identity("episode_digest_set")):
        errors["KC-GRAPH-001"].add("digest_projection_mismatch")
    if (values.event_cards.events != tuple(sorted(expected_cards, key=lambda card: card.event_id))
            or values.event_cards.event_card_set_id != identity("event_card_set")):
        errors["KC-EVENT-001"].add("event_factual_projection_mismatch")

    expected_nodes: list[GraphNode] = []

    def node(
        key: str, kind: str, label: str,
        attrs: EntityAttributes | FactAttributes | EventAttributes | BeatAttributes | ObligationAttributes | StoryThreadAttributes,
        refs: tuple[SemanticObjectRef, ...], scores: tuple[Decimal, ...], method: str,
    ) -> None:
        if not scores or any(score < threshold for score in scores):
            errors["KC-GRAPH-002"].add("node_low_confidence")
        expected_nodes.append(GraphNode(key, kind, label, attrs, refs, Confidence.from_decimal(min(scores), method=method)))

    for entity, ref in entities.values():
        node(entity.entity_id, "entity", entity.display_label,
             EntityAttributes(entity.entity_kind.value, entity.display_label, entity.visual_description),
             (ref,), (observation_confidence(entity),), "model")
    for fact, ref in facts.values():
        fact_value = FactTextValue(fact.summary) if fact.object_ref is None else FactEntityRefValue(fact.object_ref)
        node(fact.fact_id, "fact", fact.summary, FactAttributes(fact.subject_ref, fact.fact_kind.value, fact_value, "none"),
             (ref,), (observation_confidence(fact),), "model")
    for event, ref, episode in events.values():
        event_ref = SemanticObjectRef(card_owner, "event", event.event_id)
        node(event.event_id, "event", event.summary,
             EventAttributes(event_ref, episode, event.summary,
                             (SemanticObjectRef(card_owner, "source_range", f"{event.event_id}:range:0"),), event.participant_refs),
             (ref, event_ref, SemanticObjectRef(digest_owner, "episode_digest", episode)), (observation_confidence(event),), "model")
    obligation_refs: dict[str, tuple[SemanticObjectRef, ...]] = {}
    obligation_scores: dict[str, tuple[Decimal, ...]] = {}
    for obligation in draft.obligations:
        keys = tuple(ref.object_id for ref in obligation.required_fact_refs)
        refs = tuple(facts[key][1] for key in keys)
        scores = tuple(observation_confidence(facts[key][0]) for key in keys)
        obligation_refs[obligation.obligation_id], obligation_scores[obligation.obligation_id] = refs, scores
        node(identity("obligation", obligation.obligation_id), "obligation", obligation.description,
             ObligationAttributes(obligation.description, keys, obligation.success_criteria), refs, scores, "rule")
    for beat in draft.beats:
        node(identity("beat", beat.beat_id), "beat", beat.summary,
             BeatAttributes(beat.summary, beat.phase, tuple(identity("obligation", key) for key in beat.obligation_ids)),
             tuple(events[ref.object_id][1] for ref in beat.event_refs),
             tuple(observation_confidence(events[ref.object_id][0]) for ref in beat.event_refs), "rule")
    for thread in draft.story_threads:
        node(identity("story_thread", thread.story_thread_id), "story_thread", thread.title,
             StoryThreadAttributes(thread.title, thread.premise, tuple(identity("obligation", key) for key in thread.obligation_ids)),
             tuple({ref for key in thread.obligation_ids for ref in obligation_refs[key]}),
             tuple(score for key in thread.obligation_ids for score in obligation_scores[key]), "rule")

    # Compare full edge semantics/evidence independently; the ID is not an oracle.
    edge_sources: dict[tuple[str, str, str], set[SemanticObjectRef]] = {}

    def edge(kind: str, start: str, end: str, refs: tuple[SemanticObjectRef, ...]) -> None:
        edge_sources.setdefault((kind, start, end), set()).update(refs)

    for event, ref, _episode in events.values():
        for fact_id in event.fact_refs:
            edge("supports", fact_id, event.event_id, (facts[fact_id][1], ref))
        for entity_id in event.participant_refs:
            edge("involves", entity_id, event.event_id, (ref,))
        for cause in event.cause_event_refs:
            edge("causes", cause, event.event_id, (ref,))
        for effect in event.effect_event_refs:
            edge("causes", event.event_id, effect, (ref,))
    for beat in draft.beats:
        for event_ref in beat.event_refs:
            edge("supports", event_ref.object_id, identity("beat", beat.beat_id), (events[event_ref.object_id][1],))
    expected_edges = tuple(GraphEdge(
        canonical_json_hash({"schema_version": "stage1-narrative-projection-edge-id-v1",
                             "edge_type": kind, "from_node_id": start, "to_node_id": end}),
        kind, start, end, tuple(refs),
    ) for (kind, start, end), refs in edge_sources.items())
    graph = values.narrative_graph
    if (set(graph.nodes) != set(expected_nodes) or set(graph.edges) != set(expected_edges)
            or graph.graph_id != identity("narrative_graph")):
        errors["KC-GRAPH-001"].add("graph_projection_mismatch")

    used_refs = {ref for item in graph.nodes for ref in item.evidence_refs}
    used_refs.update(ref for item in graph.edges for ref in item.evidence_refs)
    used_refs.update(ref for item in values.episode_digests.digests for ref in (*item.evidence_refs, *item.source_window_refs))
    used_refs.update(ref for item in values.event_cards.events for ref in item.evidence_refs)
    used_refs.update(span.source_ref for item in values.event_cards.events for span in item.source_range_refs)
    for item in graph.nodes:
        if isinstance(item.attributes, EventAttributes):
            used_refs.update((item.attributes.event_card_ref, *item.attributes.source_range_refs))
        elif isinstance(item.attributes, CharacterAttributes):
            used_refs.update(item.attributes.identity_evidence_refs)
        elif isinstance(item.attributes, CharacterStateAttributes):
            used_refs.add(item.attributes.source_window_ref)
    # Raw observations describe expected objects; they do not prove that a
    # producer actually retained those objects in its earlier output members.
    actual_refs = {ref for ref in known_refs if ref.member_ref not in (card_owner, digest_owner)}
    for card in values.event_cards.events:
        actual_refs.add(SemanticObjectRef(card_owner, "event", card.event_id))
        actual_refs.update(SemanticObjectRef(card_owner, "source_range", f"{card.event_id}:range:{index}")
                           for index in range(len(card.source_range_refs)))
    actual_refs.update(SemanticObjectRef(digest_owner, "episode_digest", digest.episode_id)
                       for digest in values.episode_digests.digests)
    if not used_refs <= actual_refs:
        errors["KC-GRAPH-001"].add("unresolved_output_reference")
    if errors["KC-AUTH-001"] or not used_refs <= known_refs or not used_refs <= actual_refs:
        errors["KC-AUTH-002"].add("source_evidence_not_authorized_or_resolved")
    return tuple(Stage1Check(rule, "fail" if codes else "pass", tuple(sorted(codes)))
                 for rule, codes in errors.items())
