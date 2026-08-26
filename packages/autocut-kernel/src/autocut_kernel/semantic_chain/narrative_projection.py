"""Deterministic, non-authoritative Stage 1 narrative value projection.

This module transforms the exact committed VLM observations and an already
decoded cross-window draft into the first three pending business members.  It
does not decide coverage, resolve identities, read heads, or establish Store
admission.  The command boundary must re-decode its audited draft bytes before
using this value projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Protocol

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..store.models import (
    ArtifactMember,
    ArtifactScope,
    CommittedSemanticInputs,
    SourceWindowIdentity,
    canonical_payload_hash,
)
from ..vlm.models import VlmEntity, VlmEvent, VlmFact, VlmSemanticPack
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .narrative_models import (
    BeatAttributes,
    CoarseSourceRange,
    Confidence,
    EntityAttributes,
    EpisodeDigest,
    EpisodeDigestSet,
    EventAttributes,
    EventCard,
    EventCardSet,
    FactAttributes,
    FactEntityRefValue,
    FactTextValue,
    GraphEdge,
    GraphNode,
    NarrativeGraph,
    ObligationAttributes,
    StoryThreadAttributes,
)
from .stage1_draft import Stage1Draft


class NarrativeProjectionError(ValueError):
    """The supplied value inputs cannot form a closed deterministic projection."""


class _MemberReference(Protocol):
    @property
    def artifact_type(self) -> str: ...

    @property
    def logical_id(self) -> str: ...

    @property
    def revision(self) -> int: ...

    @property
    def scope(self) -> ArtifactScope: ...

    @property
    def content_hash(self) -> str: ...


@dataclass(frozen=True, slots=True)
class NarrativeProjection:
    """The three pending members in their required acyclic construction order."""

    event_cards: ArtifactMember
    episode_digests: ArtifactMember
    narrative_graph: ArtifactMember

    def __post_init__(self) -> None:
        for member, artifact_type in (
            (self.event_cards, "event_card_set"),
            (self.episode_digests, "episode_digest_set"),
            (self.narrative_graph, "narrative_graph"),
        ):
            if type(member) is not ArtifactMember or member.artifact_type != artifact_type:  # noqa: E721
                raise NarrativeProjectionError("projection members have wrong types")


def _require_inputs(value: object) -> CommittedSemanticInputs:
    if type(value) is not CommittedSemanticInputs:  # noqa: E721
        raise NarrativeProjectionError("projection requires exact CommittedSemanticInputs")
    return value


def _require_draft(value: object) -> Stage1Draft:
    if type(value) is not Stage1Draft:  # noqa: E721
        raise NarrativeProjectionError("projection requires an exact decoded Stage1Draft")
    return value


def _committed_identity(reference: _MemberReference) -> SemanticMemberIdentity:
    return SemanticMemberIdentity(
        reference.artifact_type,
        reference.logical_id,
        reference.revision,
        reference.scope,
        reference.content_hash,
    )


def _confidence(value: Decimal, *, method: str) -> Confidence:
    if type(value) is not Decimal or not Decimal(0) <= value <= Decimal(1):  # noqa: E721
        raise NarrativeProjectionError("VLM confidence must be a closed decimal in [0, 1]")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text == "-0":
        text = "0"
    return Confidence(text, method)


def _minimum_confidence(values: Iterable[Decimal]) -> Confidence:
    members = tuple(values)
    if not members:
        raise NarrativeProjectionError("draft node has no underlying VLM evidence")
    return _confidence(min(members), method="rule")


def _derived_id(input_binding_sha256: str, kind: str, local_id: str = "") -> str:
    return canonical_json_hash(
        {
            "schema_version": "stage1-narrative-projection-id-v1",
            "input_binding_sha256": input_binding_sha256,
            "kind": kind,
            "local_id": local_id,
        }
    )


def _draft_node_id(draft: Stage1Draft, node_type: str, local_id: str) -> str:
    return _derived_id(draft.input_binding_sha256, node_type, local_id)


def _artifact(
    *, artifact_type: str, logical_id: str, revision: int, scope: ArtifactScope, payload: object
) -> ArtifactMember:
    payload_json = canonical_json_bytes(payload).decode("utf-8")
    return ArtifactMember(
        artifact_type,
        logical_id,
        revision,
        scope,
        canonical_payload_hash(payload_json),
        payload_json,
    )


def _edge_id(edge_type: str, from_node_id: str, to_node_id: str) -> str:
    return canonical_json_hash(
        {
            "schema_version": "stage1-narrative-projection-edge-id-v1",
            "edge_type": edge_type,
            "from_node_id": from_node_id,
            "to_node_id": to_node_id,
        }
    )


def _project_edges(
    facts: dict[str, tuple[VlmFact, SemanticObjectRef]],
    events: dict[str, tuple[VlmEvent, SemanticObjectRef, str]],
    draft: Stage1Draft,
) -> tuple[GraphEdge, ...]:
    """Aggregate every raw declaration of one logical directional Graph edge."""

    edge_evidence: dict[tuple[str, str, str], set[SemanticObjectRef]] = {}

    def add_edge(
        edge_type: str,
        from_node_id: str,
        to_node_id: str,
        evidence: tuple[SemanticObjectRef, ...],
    ) -> None:
        edge_evidence.setdefault((edge_type, from_node_id, to_node_id), set()).update(evidence)

    for event, event_ref, _episode in events.values():
        for fact_id in event.fact_refs:
            add_edge("supports", fact_id, event.event_id, (facts[fact_id][1], event_ref))
        for entity_id in event.participant_refs:
            add_edge("involves", entity_id, event.event_id, (event_ref,))
        for cause_id in event.cause_event_refs:
            add_edge("causes", cause_id, event.event_id, (event_ref,))
        for effect_id in event.effect_event_refs:
            add_edge("causes", event.event_id, effect_id, (event_ref,))
    for beat in draft.beats:
        beat_id = _draft_node_id(draft, "beat", beat.beat_id)
        for draft_event_ref in beat.event_refs:
            add_edge("supports", draft_event_ref.object_id, beat_id, (events[draft_event_ref.object_id][1],))
    return tuple(
        GraphEdge(
            _edge_id(edge_type, from_node_id, to_node_id),
            edge_type,
            from_node_id,
            to_node_id,
            tuple(sorted(evidence, key=lambda ref: canonical_json_bytes(ref.to_mapping()))),
        )
        for (edge_type, from_node_id, to_node_id), evidence in edge_evidence.items()
    )


def project_narrative(
    inputs: CommittedSemanticInputs,
    draft: Stage1Draft,
    *,
    scope: ArtifactScope,
    revision: int,
) -> NarrativeProjection:
    """Project exact VLM observation and draft values into three pending members.

    The caller-supplied scope must be the committed source scope.  The draft is
    deliberately not treated as an authority capability here; no coverage or
    admission result is produced.
    """

    inputs, draft = _require_inputs(inputs), _require_draft(draft)
    if type(scope) is not ArtifactScope or scope != inputs.source_manifest.reference.scope:  # noqa: E721
        raise NarrativeProjectionError("projection scope must equal the committed source scope")
    if type(revision) is not int or revision < 1:  # noqa: E721
        raise NarrativeProjectionError("projection revision must be a positive exact integer")
    try:
        inputs.source_grant.require_purpose("semantic_analysis")
    except ValueError as error:
        raise NarrativeProjectionError("semantic_analysis source purpose is not granted") from error

    source_identity = _committed_identity(inputs.source_manifest.reference)
    granted_sources = {(item.source_id, item.content_sha256) for item in inputs.source_grant.sources}

    entities: dict[str, tuple[VlmEntity, SemanticObjectRef]] = {}
    facts: dict[str, tuple[VlmFact, SemanticObjectRef]] = {}
    events: dict[str, tuple[VlmEvent, SemanticObjectRef, str]] = {}
    pack_owners: dict[str, SemanticMemberIdentity] = {}
    episode_windows: dict[int, list[tuple[SourceWindowIdentity, SemanticObjectRef, VlmSemanticPack]]] = {}

    for committed in inputs.inputs:
        window, pack = committed.source_window, committed.semantic_pack.semantic_pack
        if (window.source_id, window.source_sha256) not in granted_sources:
            raise NarrativeProjectionError("VLM input source is not in the exact source grant")
        if pack.window_manifest_sha256 != window.window_manifest_sha256:
            raise NarrativeProjectionError("VLM pack does not bind its committed source window")
        pack_identity = _committed_identity(committed.semantic_pack.reference)
        pack_owners[window.window_manifest_sha256] = pack_identity
        window_ref = SemanticObjectRef(source_identity, "source_window", window.window_manifest_sha256)
        episode_windows.setdefault(window.episode_index, []).append((window, window_ref, pack))
        for item in pack.entities:
            ref = SemanticObjectRef(pack_identity, "vlm_entity", item.entity_id)
            if item.entity_id in entities:
                raise NarrativeProjectionError("VLM entity identity is duplicated across committed inputs")
            entities[item.entity_id] = (item, ref)
        for item in pack.facts:
            ref = SemanticObjectRef(pack_identity, "vlm_fact", item.fact_id)
            if item.fact_id in facts:
                raise NarrativeProjectionError("VLM fact identity is duplicated across committed inputs")
            facts[item.fact_id] = (item, ref)
        for item in pack.events:
            ref = SemanticObjectRef(pack_identity, "vlm_event", item.event_id)
            if item.event_id in events:
                raise NarrativeProjectionError("VLM event identity is duplicated across committed inputs")
            events[item.event_id] = (item, ref, f"episode-{window.episode_index + 1}")

    for fact, _fact_ref in facts.values():
        if fact.subject_ref not in entities or (fact.object_ref is not None and fact.object_ref not in entities):
            raise NarrativeProjectionError("VLM Fact has an unresolved entity reference")
    for event, _event_ref, _episode in events.values():
        if (
            any(value not in entities for value in event.participant_refs)
            or any(value not in facts for value in event.fact_refs)
            or any(value not in events for value in (*event.cause_event_refs, *event.effect_event_refs))
        ):
            raise NarrativeProjectionError("VLM Event has an unresolved projection reference")

    event_card_set_id = _derived_id(draft.input_binding_sha256, "event_card_set")
    cards = tuple(
        EventCard(
            event.event_id,
            episode_id,
            event.summary,
            (
                CoarseSourceRange(
                    SemanticObjectRef(source_identity, "source", committed.source_window.source_id),
                    committed.source_window.source_clock_id,
                    event.support.source_interval,
                ),
            ),
            (event_ref,),
        )
        for committed in inputs.inputs
        for event, event_ref, episode_id in (
            (item, events[item.event_id][1], events[item.event_id][2])
            for item in committed.semantic_pack.semantic_pack.events
        )
    )
    event_set = EventCardSet(event_card_set_id, cards)
    event_member = _artifact(
        artifact_type="event_card_set", logical_id="event_card_set", revision=revision, scope=scope, payload=event_set.to_mapping()
    )
    event_identity = SemanticMemberIdentity.from_artifact_member(event_member)

    digests: list[EpisodeDigest] = []
    for ordinal, episode_index in enumerate(sorted(episode_windows), start=1):
        windows = sorted(episode_windows[episode_index], key=lambda item: item[0].canonical_order_key)
        summaries = tuple(item[2].window_summary.summary for item in windows)
        digest_evidence: list[SemanticObjectRef] = []
        for _window, window_ref, pack in windows:
            window_evidence: list[SemanticObjectRef] = []
            for fact_id in pack.window_summary.fact_refs:
                window_evidence.append(facts[fact_id][1])
            for event_id in pack.window_summary.event_refs:
                window_evidence.append(SemanticObjectRef(event_identity, "event", event_id))
            # A summary with no declared observation retains only its exact
            # window provenance.  It is not upgraded to Fact/Event support;
            # coverage independently records the missing summary evidence.
            digest_evidence.extend(window_evidence or (window_ref,))
        digests.append(
            EpisodeDigest(
                f"episode-{episode_index + 1}",
                ordinal,
                "\n".join(summaries),
                tuple(item[1] for item in windows),
                tuple(digest_evidence),
            )
        )
    digest_set = EpisodeDigestSet(_derived_id(draft.input_binding_sha256, "episode_digest_set"), tuple(digests))
    digest_member = _artifact(
        artifact_type="episode_digest_set", logical_id="episode_digest_set", revision=revision, scope=scope, payload=digest_set.to_mapping()
    )
    digest_identity = SemanticMemberIdentity.from_artifact_member(digest_member)

    nodes: list[GraphNode] = []
    for entity, entity_evidence in entities.values():
        nodes.append(
            GraphNode(
                entity.entity_id,
                "entity",
                entity.display_label,
                EntityAttributes(entity.entity_kind.value, entity.display_label, entity.visual_description),
                (entity_evidence,),
                _confidence(entity.support.confidence, method="model"),
            )
        )
    for fact, fact_evidence in facts.values():
        value = FactEntityRefValue(fact.object_ref) if fact.object_ref is not None else FactTextValue(fact.summary)
        nodes.append(
            GraphNode(
                fact.fact_id,
                "fact",
                fact.summary,
                FactAttributes(fact.subject_ref, fact.fact_kind.value, value, "none"),
                (fact_evidence,),
                _confidence(fact.support.confidence, method="model"),
            )
        )
    for event, event_evidence, episode_id in events.values():
        card_ref = SemanticObjectRef(event_identity, "event", event.event_id)
        range_refs = tuple(
            SemanticObjectRef(event_identity, "source_range", f"{event.event_id}:range:{index}")
            for index in range(1)
        )
        nodes.append(
            GraphNode(
                event.event_id,
                "event",
                event.summary,
                EventAttributes(card_ref, episode_id, event.summary, range_refs, event.participant_refs),
                (
                    event_evidence,
                    SemanticObjectRef(event_identity, "event", event.event_id),
                    SemanticObjectRef(digest_identity, "episode_digest", episode_id),
                ),
                _confidence(event.support.confidence, method="model"),
            )
        )

    obligation_evidence: dict[str, tuple[SemanticObjectRef, ...]] = {}
    obligation_confidence: dict[str, Confidence] = {}
    for obligation in draft.obligations:
        obligation_refs: list[SemanticObjectRef] = []
        obligation_confidences: list[Decimal] = []
        fact_ids: list[str] = []
        for draft_evidence in obligation.required_fact_refs:
            if draft_evidence.object_type != "fact" or draft_evidence.object_id not in facts:
                raise NarrativeProjectionError("draft obligation does not name an exact projected Fact")
            fact, fact_ref = facts[draft_evidence.object_id]
            if pack_owners.get(draft_evidence.window_manifest_sha256) != fact_ref.member_ref:
                raise NarrativeProjectionError("draft Fact evidence has the wrong source window owner")
            obligation_refs.append(fact_ref)
            obligation_confidences.append(fact.support.confidence)
            fact_ids.append(fact.fact_id)
        node_id = _draft_node_id(draft, "obligation", obligation.obligation_id)
        obligation_evidence[obligation.obligation_id] = tuple(obligation_refs)
        obligation_confidence[obligation.obligation_id] = _minimum_confidence(obligation_confidences)
        nodes.append(
            GraphNode(
                node_id,
                "obligation",
                obligation.description,
                ObligationAttributes(obligation.description, tuple(fact_ids), obligation.success_criteria),
                tuple(obligation_refs),
                obligation_confidence[obligation.obligation_id],
            )
        )
    for beat in draft.beats:
        beat_refs: list[SemanticObjectRef] = []
        beat_confidences: list[Decimal] = []
        for draft_evidence in beat.event_refs:
            if draft_evidence.object_type != "event" or draft_evidence.object_id not in events:
                raise NarrativeProjectionError("draft Beat does not name an exact projected Event")
            event, event_ref, _episode = events[draft_evidence.object_id]
            if pack_owners.get(draft_evidence.window_manifest_sha256) != event_ref.member_ref:
                raise NarrativeProjectionError("draft Event evidence has the wrong source window owner")
            beat_refs.append(event_ref)
            beat_confidences.append(event.support.confidence)
        nodes.append(
            GraphNode(
                _draft_node_id(draft, "beat", beat.beat_id),
                "beat",
                beat.summary,
                BeatAttributes(
                    beat.summary,
                    beat.phase,
                    tuple(_draft_node_id(draft, "obligation", item) for item in beat.obligation_ids),
                ),
                tuple(beat_refs),
                _minimum_confidence(beat_confidences),
            )
        )
    for thread in draft.story_threads:
        thread_refs = tuple(
            sorted(
                {ref for obligation_id in thread.obligation_ids for ref in obligation_evidence[obligation_id]},
                key=lambda ref: canonical_json_bytes(ref.to_mapping()),
            )
        )
        nodes.append(
            GraphNode(
                _draft_node_id(draft, "story_thread", thread.story_thread_id),
                "story_thread",
                thread.title,
                StoryThreadAttributes(
                    thread.title,
                    thread.premise,
                    tuple(_draft_node_id(draft, "obligation", item) for item in thread.obligation_ids),
                ),
                thread_refs,
                _minimum_confidence(
                    [Decimal(obligation_confidence[item].value) for item in thread.obligation_ids]
                ),
            )
        )

    edges = _project_edges(facts, events, draft)
    graph = NarrativeGraph(_derived_id(draft.input_binding_sha256, "narrative_graph"), tuple(nodes), edges)
    graph_member = _artifact(
        artifact_type="narrative_graph", logical_id="narrative_graph", revision=revision, scope=scope, payload=graph.to_mapping()
    )
    return NarrativeProjection(event_member, digest_member, graph_member)
