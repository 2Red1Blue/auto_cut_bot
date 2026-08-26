"""Deterministic committed-VLM to Stage 2 CandidateCatalog projection."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..source_manifest import DecodedSourceManifest, decode_source_manifest
from ..store.models import (
    ArtifactMember,
    ArtifactScope,
    CommittedSemanticInputs,
    CommittedVlmSemanticInput,
    VlmSemanticPackReference,
    WholeSeriesSourceManifestReference,
    canonical_payload_hash,
)
from ..vlm.models import (
    VlmCandidateHypothesis,
    VlmEvent,
    VlmFact,
    VlmSemanticMeasurement,
)
from ..vlm.window import ProxyTimelineMap
from .candidate_catalog import (
    Candidate,
    CandidateCatalog,
    CandidateCatalogPolicy,
    CandidateEventBinding,
    CandidateMeasurement,
    CandidateSupport,
    candidate_confidence_text,
)
from .candidate_duration import conservative_support_duration
from .ledger_models import CoverageLedger
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .narrative_models import EventAttributes, GraphNode
from .stage1_result import Stage1Values


class CandidateProjectionError(ValueError):
    """Committed inputs and Stage 1 values cannot form a closed catalog."""


@dataclass(frozen=True, slots=True)
class CandidateCatalogProjection:
    member: ArtifactMember
    catalog: CandidateCatalog

    def __post_init__(self) -> None:
        if type(self.member) is not ArtifactMember or self.member.artifact_type != "candidate_catalog":  # noqa: E721
            raise CandidateProjectionError("candidate projection member is invalid")
        if type(self.catalog) is not CandidateCatalog:  # noqa: E721
            raise CandidateProjectionError("candidate projection catalog is invalid")


def _identity(
    reference: WholeSeriesSourceManifestReference | VlmSemanticPackReference,
) -> SemanticMemberIdentity:
    if type(reference) not in (WholeSeriesSourceManifestReference, VlmSemanticPackReference):  # noqa: E721
        raise CandidateProjectionError("committed member reference is invalid")
    return SemanticMemberIdentity(
        reference.artifact_type,
        reference.logical_id,
        reference.revision,
        reference.scope,
        reference.content_hash,
    )


def _require_inputs(value: object) -> CommittedSemanticInputs:
    if type(value) is not CommittedSemanticInputs:  # noqa: E721
        raise CandidateProjectionError("projection requires exact CommittedSemanticInputs")
    return value


def _require_stage1(value: object) -> Stage1Values:
    if type(value) is not Stage1Values:  # noqa: E721
        raise CandidateProjectionError("projection requires exact decoded Stage1Values")
    return value


def _artifact(scope: ArtifactScope, revision: int, catalog: CandidateCatalog) -> ArtifactMember:
    raw = canonical_json_bytes(catalog.to_mapping()).decode("utf-8")
    return ArtifactMember("candidate_catalog", "candidate_catalog", revision, scope, canonical_payload_hash(raw), raw)


def _candidate_event_binding(
    event_id: str,
    *,
    pack_identity: SemanticMemberIdentity,
    graph_identity: SemanticMemberIdentity,
    card_identity: SemanticMemberIdentity,
) -> CandidateEventBinding:
    return CandidateEventBinding(
        SemanticObjectRef(pack_identity, "vlm_event", event_id),
        SemanticObjectRef(graph_identity, "event", event_id),
        SemanticObjectRef(card_identity, "event", event_id),
    )


def _check_stage1_universe(
    inputs: CommittedSemanticInputs,
    values: Stage1Values,
) -> tuple[
    SemanticMemberIdentity,
    SemanticMemberIdentity,
    SemanticMemberIdentity,
    dict[str, tuple[VlmFact, SemanticMemberIdentity]],
    dict[str, tuple[VlmEvent, SemanticMemberIdentity, CommittedVlmSemanticInput]],
]:
    coverage = values.coverage
    cards, graph, ledger = coverage.event_cards, coverage.narrative_graph, coverage.coverage_ledger
    card_identity = coverage.identity("event_card_set")
    graph_identity = coverage.identity("narrative_graph")
    ledger_identity = coverage.identity("coverage_ledger")
    if any(identity.scope != inputs.source_manifest.reference.scope for identity in (card_identity, graph_identity, ledger_identity)):
        raise CandidateProjectionError("Stage 1 member scope differs from committed source scope")
    raw_facts: dict[str, tuple[VlmFact, SemanticMemberIdentity]] = {}
    raw_events: dict[str, tuple[VlmEvent, SemanticMemberIdentity, CommittedVlmSemanticInput]] = {}
    windows: dict[str, CommittedVlmSemanticInput] = {}
    source_identity = _identity(inputs.source_manifest.reference)
    granted = {(entry.source_id, entry.content_sha256) for entry in inputs.source_grant.sources}
    for committed in inputs.inputs:
        window, pack = committed.source_window, committed.semantic_pack.semantic_pack
        if (window.source_id, window.source_sha256) not in granted:
            raise CandidateProjectionError("committed VLM source is absent from the exact grant")
        if pack.window_manifest_sha256 != window.window_manifest_sha256:
            raise CandidateProjectionError("VLM pack does not bind its committed source window")
        if window.window_manifest_sha256 in windows:
            raise CandidateProjectionError("committed source window is duplicated")
        windows[window.window_manifest_sha256] = committed
        owner = _identity(committed.semantic_pack.reference)
        for fact in pack.facts:
            if fact.fact_id in raw_facts:
                raise CandidateProjectionError("VLM Fact identity is duplicated")
            raw_facts[fact.fact_id] = (fact, owner)
        for event in pack.events:
            if event.event_id in raw_events:
                raise CandidateProjectionError("VLM Event identity is duplicated")
            raw_events[event.event_id] = (event, owner, committed)
    graph_nodes = {node.node_id: node for node in graph.nodes}
    fact_nodes = {node.node_id for node in graph.nodes if node.node_type == "fact"}
    event_nodes = {node.node_id: node for node in graph.nodes if node.node_type == "event"}
    if fact_nodes != set(raw_facts) or set(event_nodes) != set(raw_events):
        raise CandidateProjectionError("Stage 1 Graph Fact/Event universe differs from committed VLM")
    cards_by_id = {card.event_id: card for card in cards.events}
    if set(cards_by_id) != set(raw_events):
        raise CandidateProjectionError("Stage 1 EventCard universe differs from committed VLM")
    expected_windows = {
        SemanticObjectRef(source_identity, "source_window", item.source_window.window_manifest_sha256)
        for item in inputs.inputs
    }
    actual_windows = {item.source_window_ref for item in ledger.windows}
    if actual_windows != expected_windows:
        raise CandidateProjectionError("CoverageLedger window universe differs from committed VLM")
    ledger_by_window = {item.source_window_ref.object_id: item for item in ledger.windows}
    for window_id, committed in windows.items():
        # Ledger closure deliberately names the projected Graph/Card units.  Its
        # exact object-ID universe must nevertheless be derived from the raw
        # committed VLM observations, not trusted from the ledger itself.
        expected_facts = {
            SemanticObjectRef(graph_identity, "fact", item.fact_id)
            for item in committed.semantic_pack.semantic_pack.facts
        }
        expected_events = {
            SemanticObjectRef(card_identity, "event", item.event_id)
            for item in committed.semantic_pack.semantic_pack.events
        }
        actual = ledger_by_window[window_id]
        if set(actual.fact_refs) != expected_facts or set(actual.event_refs) != expected_events:
            raise CandidateProjectionError("CoverageLedger does not retain exact VLM observation closure")
    for event_id, (event, owner, committed) in raw_events.items():
        node = graph_nodes[event_id]
        card = cards_by_id[event_id]
        if type(node) is not GraphNode or type(node.attributes) is not EventAttributes:  # noqa: E721
            raise CandidateProjectionError("Stage 1 Graph Event has the wrong node shape")
        attrs = node.attributes
        expected_card = SemanticObjectRef(card_identity, "event", event_id)
        if attrs.event_card_ref != expected_card or card.content != event.summary:
            raise CandidateProjectionError("Stage 1 Graph Event is not its exact EventCard")
        source_ref = SemanticObjectRef(source_identity, "source", committed.source_window.source_id)
        if len(card.source_range_refs) != 1 or card.source_range_refs[0].source_ref != source_ref or card.source_range_refs[0].mapped_interval != event.support.source_interval:
            raise CandidateProjectionError("Stage 1 EventCard does not preserve VLM coarse support")
        if tuple(attrs.source_range_refs) != (SemanticObjectRef(card_identity, "source_range", f"{event_id}:range:0"),):
            raise CandidateProjectionError("Stage 1 Graph Event range reference differs from EventCard")
        if node.node_id != event_id or attrs.event_card_ref.member_ref != card_identity or SemanticObjectRef(owner, "vlm_event", event_id) not in node.evidence_refs:
            raise CandidateProjectionError("Stage 1 Graph Event lacks exact VLM provenance")
    return card_identity, graph_identity, ledger_identity, raw_facts, raw_events


def _measurement(
    raw: VlmSemanticMeasurement,
    *,
    owner: SemanticMemberIdentity,
    known_facts: set[str],
    known_events: set[str],
) -> CandidateMeasurement:
    refs_facts = tuple(SemanticObjectRef(owner, "vlm_fact", item) for item in raw.fact_refs)
    refs_events = tuple(SemanticObjectRef(owner, "vlm_event", item) for item in raw.event_refs)
    if not set(raw.fact_refs) <= known_facts or not set(raw.event_refs) <= known_events:
        raise CandidateProjectionError("candidate measurement is outside its raw VLM closure")
    result = CandidateMeasurement(
        raw.measurement_kind.value,
        candidate_confidence_text(raw.value, "candidate measurement value"),
        candidate_confidence_text(raw.confidence, "candidate measurement confidence"),
        refs_facts,
        refs_events,
    )
    return result


def _candidate(
    raw: VlmCandidateHypothesis,
    committed: CommittedVlmSemanticInput,
    *,
    source_identity: SemanticMemberIdentity,
    card_identity: SemanticMemberIdentity,
    graph_identity: SemanticMemberIdentity,
    ledger: CoverageLedger,
    timeline_map: ProxyTimelineMap,
) -> Candidate:
    window = committed.source_window
    pack_identity = _identity(committed.semantic_pack.reference)
    if raw.support.core_owner_window_manifest_sha256 != window.window_manifest_sha256:
        raise CandidateProjectionError("candidate support has the wrong source-window owner")
    by_window = {item.source_window_ref.object_id: item for item in ledger.windows}
    coverage = by_window.get(window.window_manifest_sha256)
    if coverage is None:
        raise CandidateProjectionError("candidate source window is absent from CoverageLedger")
    known_facts = {item.object_id for item in coverage.fact_refs}
    known_events = {item.object_id for item in coverage.event_refs}
    candidate_events = (
        raw.anchor_event_ref, *raw.supporting_event_refs, *raw.context_event_refs, *raw.payoff_event_refs,
    )
    if not set(candidate_events) <= known_events:
        raise CandidateProjectionError("candidate Event is absent from its exact CoverageLedger window")
    def bindings(event_ids: tuple[str, ...]) -> tuple[CandidateEventBinding, ...]:
        return tuple(_candidate_event_binding(
            item, pack_identity=pack_identity, graph_identity=graph_identity, card_identity=card_identity
        ) for item in event_ids)
    return Candidate(
        SemanticObjectRef(pack_identity, "vlm_candidate", raw.candidate_id),
        SemanticObjectRef(source_identity, "source", window.source_id),
        SemanticObjectRef(source_identity, "source_window", window.window_manifest_sha256),
        coverage.window_id, raw.candidate_kind.value, raw.local_candidate_id, raw.reason, raw.anchor_summary,
        raw.payoff_or_open_question, raw.open_question, raw.dialogue_excerpt,
        bindings((raw.anchor_event_ref,))[0], bindings(raw.supporting_event_refs), bindings(raw.context_event_refs), bindings(raw.payoff_event_refs),
        tuple(item.value for item in raw.editing_modes), tuple(item.value for item in raw.narrative_functions),
        tuple(item.value for item in raw.tags),
        tuple(_measurement(
            item, owner=pack_identity, known_facts=known_facts, known_events=known_events
        ) for item in raw.measurements),
        CandidateSupport.from_vlm_support(raw.support, conservative_support_duration(raw.support, timeline_map)),
    )


def decode_candidate_source_context(inputs: CommittedSemanticInputs) -> DecodedSourceManifest:
    """Decode and bind the exact Source/Window/VLM context used by candidates."""
    inputs = _require_inputs(inputs)
    try:
        inputs.source_grant.require_purpose("render_source")
    except ValueError as error:
        raise CandidateProjectionError("render_source purpose is not granted") from error
    source = inputs.source_manifest
    if canonical_payload_hash(source.payload_json) != source.reference.content_hash:
        raise CandidateProjectionError("committed SourceManifest payload hash differs from its reference")
    try:
        decoded = decode_source_manifest(source.payload_json, source.proxy_blobs)
    except ValueError as error:
        raise CandidateProjectionError("committed SourceManifest cannot supply exact timeline maps") from error
    if decoded.census != inputs.source_grant:
        raise CandidateProjectionError("decoded SourceManifest census differs from the supplied exact grant")
    manifests = {
        episode.manifest.canonical_hash: (ordinal, episode)
        for ordinal, episode in enumerate(decoded.episodes)
    }
    expected = {item.source_window.window_manifest_sha256 for item in inputs.inputs}
    if set(manifests) != expected:
        raise CandidateProjectionError("committed SourceManifest window set differs from VLM inputs")
    for committed in inputs.inputs:
        window = committed.source_window
        ordinal, episode = manifests[window.window_manifest_sha256]
        manifest = episode.manifest
        if (
            window.episode_index != ordinal
            or window.source_id != manifest.source_id
            or window.source_sha256 != manifest.source_sha256
            or window.source_clock_id != manifest.source_clock_id
            or window.stream_index != manifest.stream_index
            or window.core_start_pts != manifest.core_range.start_pts
            or window.core_end_pts != manifest.core_range.end_pts
            or window.window_manifest_set_sha256 != episode.manifest_set.canonical_hash
            or window.proxy_blob.object_id != episode.proxy_blob.object_id
            or window.proxy_blob.content_hash != episode.proxy_blob.content_hash
            or window.proxy_blob.byte_length != episode.proxy_blob.byte_length
            or window.proxy_blob.media_type != episode.proxy_blob.media_type
        ):
            raise CandidateProjectionError("committed SourceWindow differs from decoded SourceManifest")
        try:
            committed.request_identity.assert_manifest_binding(manifest, episode.manifest_set)
        except ValueError as error:
            raise CandidateProjectionError("committed VLM request identity differs from SourceWindow") from error
        pack = committed.semantic_pack.semantic_pack
        child = committed.semantic_pack.source_child
        if (
            pack.window_manifest_sha256 != manifest.canonical_hash
            or child.window_manifest_sha256 != manifest.canonical_hash
            or child.window_manifest_set_sha256 != episode.manifest_set.canonical_hash
            or child.episode_index != window.episode_index
            or child.source_manifest_sha256 != source.reference.content_hash
            or child.source_provenance_sha256 != source.canonical_hash
            or child.request_identity_sha256 != committed.request_identity.canonical_hash
            or pack.request_identity_sha256 != committed.request_identity.canonical_hash
        ):
            raise CandidateProjectionError("committed VLM semantic pack differs from request/source identity")
    return decoded


def _timeline_maps(decoded: DecodedSourceManifest) -> dict[str, ProxyTimelineMap]:
    result: dict[str, ProxyTimelineMap] = {}
    for episode in decoded.episodes:
        manifest = episode.manifest
        if manifest.canonical_hash in result:
            raise CandidateProjectionError("committed SourceManifest has duplicate window manifests")
        result[manifest.canonical_hash] = manifest.timeline_map
    return result


def project_candidate_catalog(
    inputs: CommittedSemanticInputs,
    stage1: Stage1Values,
    *,
    scope: ArtifactScope,
    revision: int,
    policy: CandidateCatalogPolicy,
) -> CandidateCatalogProjection:
    """Project every exact committed VLM candidate without selecting a Story."""
    inputs, stage1 = _require_inputs(inputs), _require_stage1(stage1)
    if type(scope) is not ArtifactScope:  # noqa: E721
        raise CandidateProjectionError("output scope must be an exact ArtifactScope")
    if type(revision) is not int or revision < 1:  # noqa: E721
        raise CandidateProjectionError("output revision must be a positive exact integer")
    if type(policy) is not CandidateCatalogPolicy:  # noqa: E721
        raise CandidateProjectionError("projection requires exact CandidateCatalogPolicy")
    if scope != inputs.source_manifest.reference.scope:
        raise CandidateProjectionError("candidate catalog output scope differs from committed SourceManifest scope")
    decoded_source = decode_candidate_source_context(inputs)
    card_identity, graph_identity, ledger_identity, _facts, _events = _check_stage1_universe(inputs, stage1)
    timeline_maps = _timeline_maps(decoded_source)
    source_identity = _identity(inputs.source_manifest.reference)
    candidates: list[Candidate] = []
    for committed in inputs.inputs:
        for raw in committed.semantic_pack.semantic_pack.candidate_hypotheses:
            candidates.append(_candidate(
                raw, committed, source_identity=source_identity, card_identity=card_identity,
                graph_identity=graph_identity, ledger=stage1.coverage.coverage_ledger,
                timeline_map=timeline_maps[committed.source_window.window_manifest_sha256],
            ))
    candidates.sort(key=lambda item: item.candidate_id)
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise CandidateProjectionError("candidate identity is duplicated across committed VLM inputs")
    binding = stage1.coverage.coverage_ledger.input_binding_sha256
    catalog_id = canonical_json_hash({
        "schema_version": "candidate-catalog-v1", "input_binding_sha256": binding,
        "policy_sha256": policy.canonical_hash, "candidate_ids": [item.candidate_id for item in candidates],
    })
    catalog = CandidateCatalog(
        catalog_id, binding, inputs.source_grant.canonical_hash, card_identity, graph_identity,
        ledger_identity, policy.canonical_hash, tuple(candidates),
    )
    return CandidateCatalogProjection(_artifact(scope, revision, catalog), catalog)
