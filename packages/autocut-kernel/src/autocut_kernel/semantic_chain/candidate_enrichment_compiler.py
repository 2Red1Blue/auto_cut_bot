"""Pure V4/Stage1 to CandidateCatalog V2 enrichment compiler.

This module performs no Store or provider I/O.  A later generation lifecycle
can use :func:`candidate_enrichment_prompt_inputs` to build an invocation and
then pass the audited raw response to :func:`compile_candidate_enrichment`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..media.types import TickRange
from ..store.models import (
    ArtifactMember,
    ArtifactScope,
    CommittedSemanticInputs,
    PersistedVlmSemanticPackV4,
    canonical_payload_hash,
)
from ..vlm.models import MappedSourceInterval
from ..vlm.semantic_pack_v4 import VlmEventV4, VlmFactV4, VlmSemanticPackV4
from .candidate_catalog_v2 import (
    CandidateAnchorRefV2,
    CandidateCapabilityPolicy,
    CandidateCatalogV2,
    CandidateCatalogV2Policy,
    CandidateCoarseSupportV2,
    CandidateSemanticMeasurementV2,
    CandidateV2,
    canonical_decimal,
    evaluate_candidate_capabilities,
)
from .candidate_enrichment_draft import (
    CandidateEnrichmentAlias,
    CandidateEnrichmentCandidateDraft,
    CandidateEnrichmentDraft,
    CandidateEnrichmentDraftPolicy,
    CandidateEnrichmentReferenceCatalog,
    candidate_enrichment_response_schema,
    decode_candidate_enrichment_draft,
)
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .narrative_models import EventAttributes, FactAttributes, GraphNode
from .stage1_result import Stage1Values


class CandidateEnrichmentCompilerError(ValueError):
    """Exact V4/Stage1 inputs cannot deterministically form CandidateCatalog V2."""


@dataclass(frozen=True, slots=True)
class CandidateEnrichmentPrompt:
    input_binding_sha256: str
    reference_catalog: CandidateEnrichmentReferenceCatalog
    prompt_inputs: dict[str, object]
    response_schema: dict[str, object]

    def __post_init__(self) -> None:
        if type(self.reference_catalog) is not CandidateEnrichmentReferenceCatalog:  # noqa: E721
            raise CandidateEnrichmentCompilerError("candidate prompt reference catalog is invalid")
        if type(self.prompt_inputs) is not dict or type(self.response_schema) is not dict:  # noqa: E721
            raise CandidateEnrichmentCompilerError("candidate prompt mappings are invalid")


@dataclass(frozen=True, slots=True)
class CandidateCatalogV2Compilation:
    member: ArtifactMember
    catalog: CandidateCatalogV2
    draft: CandidateEnrichmentDraft
    prompt: CandidateEnrichmentPrompt

    def __post_init__(self) -> None:
        if (
            type(self.member) is not ArtifactMember  # noqa: E721
            or self.member.artifact_type != "candidate_catalog"
            or self.member.logical_id != "candidate_catalog"
            or type(self.catalog) is not CandidateCatalogV2  # noqa: E721
            or type(self.draft) is not CandidateEnrichmentDraft  # noqa: E721
            or type(self.prompt) is not CandidateEnrichmentPrompt  # noqa: E721
        ):
            raise CandidateEnrichmentCompilerError("candidate compilation result is invalid")


@dataclass(frozen=True, slots=True)
class _ObservationEntry:
    alias: CandidateEnrichmentAlias
    pack_identity: SemanticMemberIdentity
    observation: VlmEventV4 | VlmFactV4
    source_id: str


@dataclass(frozen=True, slots=True)
class _CompilerContext:
    prompt: CandidateEnrichmentPrompt
    source_identity: SemanticMemberIdentity
    card_identity: SemanticMemberIdentity
    graph_identity: SemanticMemberIdentity
    ledger_identity: SemanticMemberIdentity
    by_alias: dict[str, _ObservationEntry]
    source_by_owner_window: dict[str, str]


def _source_identity(inputs: CommittedSemanticInputs) -> SemanticMemberIdentity:
    reference = inputs.source_manifest.reference
    return SemanticMemberIdentity(
        reference.artifact_type,
        reference.logical_id,
        reference.revision,
        reference.scope,
        reference.content_hash,
    )


def _pack_identity(persisted: PersistedVlmSemanticPackV4) -> SemanticMemberIdentity:
    reference = persisted.reference
    return SemanticMemberIdentity(
        reference.artifact_type,
        reference.logical_id,
        reference.revision,
        reference.scope,
        reference.content_hash,
    )


def _require_policies(
    draft_policy: object,
    catalog_policy: object,
    capability_policy: object,
) -> tuple[CandidateEnrichmentDraftPolicy, CandidateCatalogV2Policy, CandidateCapabilityPolicy]:
    if type(draft_policy) is not CandidateEnrichmentDraftPolicy:  # noqa: E721
        raise CandidateEnrichmentCompilerError("candidate draft policy must be exact")
    if type(catalog_policy) is not CandidateCatalogV2Policy:  # noqa: E721
        raise CandidateEnrichmentCompilerError("candidate catalog policy must be exact")
    if type(capability_policy) is not CandidateCapabilityPolicy:  # noqa: E721
        raise CandidateEnrichmentCompilerError("candidate capability policy must be exact")
    return draft_policy, catalog_policy, capability_policy


def _require_inputs(
    inputs: object,
    stage1: object,
) -> tuple[CommittedSemanticInputs, Stage1Values]:
    if type(inputs) is not CommittedSemanticInputs:  # noqa: E721
        raise CandidateEnrichmentCompilerError("candidate compiler requires committed semantic inputs")
    if type(stage1) is not Stage1Values:  # noqa: E721
        raise CandidateEnrichmentCompilerError("candidate compiler requires exact Stage1 values")
    if stage1.admission.validation_status != "valid" or stage1.admission.next_action != "continue":
        raise CandidateEnrichmentCompilerError("candidate compiler requires admitted Stage1 values")
    if not inputs.inputs:
        raise CandidateEnrichmentCompilerError("candidate compiler requires V4 observations")
    try:
        inputs.source_grant.require_purpose("render_source")
    except ValueError as error:
        raise CandidateEnrichmentCompilerError("render_source purpose is not granted") from error
    if any(type(item.semantic_pack) is not PersistedVlmSemanticPackV4 for item in inputs.inputs):  # noqa: E721
        raise CandidateEnrichmentCompilerError("candidate compiler accepts only exact persisted V4 observations")
    return inputs, stage1


def _context(
    inputs: CommittedSemanticInputs,
    stage1: Stage1Values,
    *,
    draft_policy: CandidateEnrichmentDraftPolicy,
    catalog_policy: CandidateCatalogV2Policy,
    capability_policy: CandidateCapabilityPolicy,
) -> _CompilerContext:
    source_identity = _source_identity(inputs)
    card_identity = stage1.coverage.identity("event_card_set")
    graph_identity = stage1.coverage.identity("narrative_graph")
    ledger_identity = stage1.coverage.identity("coverage_ledger")
    scope = inputs.source_manifest.reference.scope
    if any(
        item.scope != scope
        for item in (source_identity, card_identity, graph_identity, ledger_identity)
    ):
        raise CandidateEnrichmentCompilerError("candidate inputs cross semantic scopes")
    cards = {item.event_id: item for item in stage1.coverage.event_cards.events}
    graph = {item.node_id: item for item in stage1.coverage.narrative_graph.nodes}
    graph_facts = {item.node_id for item in graph.values() if item.node_type == "fact"}
    graph_events = {item.node_id for item in graph.values() if item.node_type == "event"}
    ledger_windows = {
        item.source_window_ref.object_id: item
        for item in stage1.coverage.coverage_ledger.windows
    }
    committed = sorted(inputs.inputs, key=lambda item: item.source_window.window_manifest_sha256)
    if len({item.source_window.window_manifest_sha256 for item in committed}) != len(committed):
        raise CandidateEnrichmentCompilerError("committed V4 window identity is duplicated")
    source_by_owner_window = {
        item.source_window.window_manifest_sha256: item.source_window.source_id for item in committed
    }
    granted = {(item.source_id, item.content_sha256) for item in inputs.source_grant.sources}
    entries: list[_ObservationEntry] = []
    event_ids: set[str] = set()
    fact_ids: set[str] = set()
    prompt_windows: list[dict[str, object]] = []
    for ordinal, item in enumerate(committed, start=1):
        persisted = item.semantic_pack
        if type(persisted) is not PersistedVlmSemanticPackV4:  # noqa: E721
            raise CandidateEnrichmentCompilerError("candidate compiler received a non-V4 pack")
        pack = persisted.semantic_pack
        if type(pack) is not VlmSemanticPackV4:  # noqa: E721
            raise CandidateEnrichmentCompilerError("persisted V4 pack has a wrong exact type")
        window = item.source_window
        if (
            pack.window_manifest_sha256 != window.window_manifest_sha256
            or persisted.source_child.window_manifest_sha256 != window.window_manifest_sha256
            or persisted.source_child.request_identity_sha256 != item.request_identity.canonical_hash
            or pack.request_identity_sha256 != item.request_identity.canonical_hash
            or (window.source_id, window.source_sha256) not in granted
        ):
            raise CandidateEnrichmentCompilerError("committed V4 observation provenance does not close")
        ledger_window = ledger_windows.get(window.window_manifest_sha256)
        if ledger_window is None:
            raise CandidateEnrichmentCompilerError("Stage1 Ledger omits a V4 observation window")
        pack_identity = _pack_identity(persisted)
        prefix = f"w{ordinal:04d}"
        fact_aliases = {
            fact.fact_id: f"{prefix}/fact/{fact.local_fact_id}" for fact in pack.facts
        }
        event_aliases = {
            event.event_id: f"{prefix}/event/{event.local_event_id}" for event in pack.events
        }
        prompt_facts: list[dict[str, object]] = []
        prompt_events: list[dict[str, object]] = []
        for fact in pack.facts:
            if fact.fact_id in fact_ids:
                raise CandidateEnrichmentCompilerError("V4 Fact identity is duplicated")
            fact_ids.add(fact.fact_id)
            alias = CandidateEnrichmentAlias(
                fact_aliases[fact.fact_id],
                "fact",
                window.window_manifest_sha256,
                fact.fact_id,
            )
            entries.append(_ObservationEntry(alias, pack_identity, fact, window.source_id))
            prompt_facts.append(
                {
                    "ref": alias.alias,
                    "fact_kind": fact.fact_kind.value,
                    "summary": fact.summary,
                    "confidence": canonical_decimal(fact.support.confidence, "fact confidence"),
                }
            )
        for event in pack.events:
            if event.event_id in event_ids:
                raise CandidateEnrichmentCompilerError("V4 Event identity is duplicated")
            event_ids.add(event.event_id)
            direct_facts = tuple(sorted(fact_aliases[ref] for ref in event.fact_refs))
            alias = CandidateEnrichmentAlias(
                event_aliases[event.event_id],
                "event",
                window.window_manifest_sha256,
                event.event_id,
                direct_facts,
            )
            entries.append(_ObservationEntry(alias, pack_identity, event, window.source_id))
            prompt_events.append(
                {
                    "ref": alias.alias,
                    "event_kind": event.event_kind.value,
                    "summary": event.summary,
                    "fact_refs": list(direct_facts),
                    "open_question": event.open_question,
                    "confidence": canonical_decimal(event.support.confidence, "event confidence"),
                }
            )
            card = cards.get(event.event_id)
            node = graph.get(event.event_id)
            expected_vlm_ref = SemanticObjectRef(pack_identity, "vlm_event", event.event_id)
            if (
                card is None
                or len(card.source_range_refs) != 1
                or card.content != event.summary
                or card.source_range_refs[0].mapped_interval != event.support.source_interval
                or expected_vlm_ref not in card.evidence_refs
                or type(node) is not GraphNode  # noqa: E721
                or type(node.attributes) is not EventAttributes
                or node.attributes.event_card_ref
                != SemanticObjectRef(card_identity, "event", event.event_id)
                or expected_vlm_ref not in node.evidence_refs
            ):
                raise CandidateEnrichmentCompilerError(
                    "admitted Stage1 Event does not preserve exact V4 support/provenance"
                )
        expected_ledger_facts = {
            SemanticObjectRef(graph_identity, "fact", item.fact_id) for item in pack.facts
        }
        expected_ledger_events = {
            SemanticObjectRef(card_identity, "event", item.event_id) for item in pack.events
        }
        if (
            set(ledger_window.fact_refs) != expected_ledger_facts
            or set(ledger_window.event_refs) != expected_ledger_events
        ):
            raise CandidateEnrichmentCompilerError("Stage1 Ledger observation closure differs from V4")
        prompt_windows.append(
            {
                "window_ref": prefix,
                "facts": prompt_facts,
                "events": prompt_events,
                "summary": pack.window_summary.summary,
            }
        )
    if graph_facts != fact_ids or graph_events != event_ids or set(cards) != event_ids:
        raise CandidateEnrichmentCompilerError("admitted Stage1 Fact/Event universe differs from V4")
    by_alias = {item.alias.alias: item for item in entries}
    for entry in entries:
        if entry.alias.object_type != "fact":
            continue
        node = graph.get(entry.alias.object_id)
        expected = SemanticObjectRef(entry.pack_identity, "vlm_fact", entry.alias.object_id)
        if (
            type(node) is not GraphNode  # noqa: E721
            or type(node.attributes) is not FactAttributes
            or expected not in node.evidence_refs
        ):
            raise CandidateEnrichmentCompilerError("admitted Stage1 Fact lacks exact V4 provenance")
    aliases = CandidateEnrichmentReferenceCatalog(
        tuple(sorted((entry.alias for entry in entries), key=lambda item: item.alias))
    )
    stage1_identities = tuple(
        SemanticMemberIdentity.from_artifact_member(item) for item in stage1.members
    )
    binding = canonical_json_hash(
        {
            "strategy_version": "candidate-enrichment-input-v1",
            "source_manifest": source_identity.to_mapping(),
            "source_grant_sha256": inputs.source_grant.canonical_hash,
            "vlm_semantic_pack_set": inputs.vlm_semantic_pack_set.to_mapping(),
            "v4_packs": [
                {
                    "member": _pack_identity(item.semantic_pack).to_mapping(),  # type: ignore[arg-type]
                    "semantic_pack_sha256": item.semantic_pack.semantic_pack.canonical_hash,
                }
                for item in committed
            ],
            "stage1_members": [item.to_mapping() for item in stage1_identities],
            "draft_policy_sha256": draft_policy.canonical_hash,
            "catalog_policy_sha256": catalog_policy.canonical_hash,
            "capability_policy_sha256": capability_policy.canonical_hash,
            "reference_catalog_sha256": aliases.canonical_hash,
        }
    )
    narrative = stage1.coverage.narrative_graph
    narrative_context = [
        {
            "node_id": node.node_id,
            "node_type": node.node_type,
            "label": node.label,
        }
        for node in narrative.nodes
        if node.node_type in ("beat", "obligation", "story_thread")
    ]
    prompt_inputs: dict[str, object] = {
        "schema_version": "candidate-enrichment-context-v1",
        "input_binding_sha256": binding,
        "windows": prompt_windows,
        "admitted_narrative_context": narrative_context,
        "capability_rules": [item.to_mapping() for item in capability_policy.rules],
    }
    prompt = CandidateEnrichmentPrompt(
        binding,
        aliases,
        prompt_inputs,
        candidate_enrichment_response_schema(draft_policy),
    )
    return _CompilerContext(
        prompt,
        source_identity,
        card_identity,
        graph_identity,
        ledger_identity,
        by_alias,
        source_by_owner_window,
    )


def candidate_enrichment_prompt_inputs(
    inputs: CommittedSemanticInputs,
    stage1: Stage1Values,
    *,
    draft_policy: CandidateEnrichmentDraftPolicy,
    catalog_policy: CandidateCatalogV2Policy,
    capability_policy: CandidateCapabilityPolicy,
) -> CandidateEnrichmentPrompt:
    """Build the exact bounded model context for a future generation lifecycle."""

    inputs, stage1 = _require_inputs(inputs, stage1)
    draft_policy, catalog_policy, capability_policy = _require_policies(
        draft_policy, catalog_policy, capability_policy
    )
    return _context(
        inputs,
        stage1,
        draft_policy=draft_policy,
        catalog_policy=catalog_policy,
        capability_policy=capability_policy,
    ).prompt


def _coarse_support(
    anchors: tuple[_ObservationEntry, ...],
    *,
    strategy_version: str,
) -> CandidateCoarseSupportV2:
    events = tuple(item.observation for item in anchors)
    if any(type(item) is not VlmEventV4 for item in events):  # noqa: E721
        raise CandidateEnrichmentCompilerError("candidate anchor is not an exact V4 Event")
    typed = tuple(item for item in events if type(item) is VlmEventV4)  # noqa: E721
    supports = tuple(item.support for item in typed)
    owners = {item.core_owner_window_manifest_sha256 for item in supports}
    if len(owners) != 1:
        raise CandidateEnrichmentCompilerError("candidate anchor supports cross core-owner windows")
    intervals = tuple(item.source_interval for item in supports)
    source_bases = {item.source_time_base for item in intervals}
    proxy_bases = {item.proxy_time_base for item in intervals}
    if len(source_bases) != 1 or len(proxy_bases) != 1:
        raise CandidateEnrichmentCompilerError("candidate anchor supports cross timing clocks")
    try:
        envelope = MappedSourceInterval(
            TickRange(
                min(item.coarse_range.start_pts for item in intervals),
                max(item.coarse_range.end_pts for item in intervals),
            ),
            max(item.mapping_error_bound_source_pts for item in intervals),
            intervals[0].source_time_base,
            max(item.provider_uncertainty_proxy_pts for item in intervals),
            intervals[0].proxy_time_base,
        )
    except ValueError as error:
        raise CandidateEnrichmentCompilerError("candidate coarse support cannot be enveloped") from error
    return CandidateCoarseSupportV2(
        strategy_version,
        envelope,
        canonical_decimal(min(item.confidence for item in supports), "support confidence"),
        next(iter(owners)),
    )


def _candidate(
    draft: CandidateEnrichmentCandidateDraft,
    *,
    context: _CompilerContext,
    input_binding_sha256: str,
    catalog_policy: CandidateCatalogV2Policy,
    capability_policy: CandidateCapabilityPolicy,
) -> CandidateV2:
    if type(draft) is not CandidateEnrichmentCandidateDraft:  # noqa: E721
        raise CandidateEnrichmentCompilerError("candidate draft item has a wrong exact type")
    candidate_id = canonical_json_hash(
        {
            "strategy_version": catalog_policy.candidate_id_strategy_version,
            "input_binding_sha256": input_binding_sha256,
            "candidate": draft.to_mapping(),
        }
    )
    anchor_entries = tuple(context.by_alias[item] for item in draft.anchor_refs)
    owner = anchor_entries[0].pack_identity
    anchors = tuple(
        sorted(
            (
                CandidateAnchorRefV2(
                    SemanticObjectRef(item.pack_identity, "vlm_event", item.alias.object_id),
                    SemanticObjectRef(context.graph_identity, "event", item.alias.object_id),
                    SemanticObjectRef(context.card_identity, "event", item.alias.object_id),
                )
                for item in anchor_entries
            ),
            key=lambda item: item.object_id,
        )
    )
    measurements: list[CandidateSemanticMeasurementV2] = []
    for raw in draft.semantic_measurements:
        evidence = tuple(
            sorted(
                (
                    SemanticObjectRef(
                        context.by_alias[alias].pack_identity,
                        "vlm_event" if context.by_alias[alias].alias.object_type == "event" else "vlm_fact",
                        context.by_alias[alias].alias.object_id,
                    )
                    for alias in raw.evidence_refs
                ),
                key=lambda item: canonical_json_bytes(item.to_mapping()),
            )
        )
        if any(item.member_ref != owner for item in evidence):
            raise CandidateEnrichmentCompilerError("candidate measurement expansion crosses owners")
        measurement_id = canonical_json_hash(
            {
                "strategy_version": "candidate-semantic-measurement-id-v1",
                "candidate_id": candidate_id,
                "measurement_kind": raw.measurement_kind,
                "value": raw.value,
                "confidence": raw.confidence,
                "evidence_refs": [item.to_mapping() for item in evidence],
            }
        )
        measurements.append(
            CandidateSemanticMeasurementV2(
                measurement_id,
                raw.measurement_kind,
                raw.value,
                raw.confidence,
                evidence,
            )
        )
    measurements_tuple = tuple(measurements)
    support = _coarse_support(
        anchor_entries,
        strategy_version=catalog_policy.coarse_support_strategy_version,
    )
    source_id = anchor_entries[0].source_id
    owner_source = context.source_by_owner_window.get(
        support.core_owner_window_manifest_sha256
    )
    if owner_source is None or owner_source != source_id:
        raise CandidateEnrichmentCompilerError("candidate core-owner window/source is unavailable")
    return CandidateV2(
        candidate_id,
        draft.local_candidate_id,
        draft.summary,
        anchors,
        measurements_tuple,
        SemanticObjectRef(context.source_identity, "source", source_id),
        SemanticObjectRef(
            context.source_identity,
            "source_window",
            support.core_owner_window_manifest_sha256,
        ),
        support,
        evaluate_candidate_capabilities(measurements_tuple, capability_policy),
    )


def compile_candidate_enrichment(
    inputs: CommittedSemanticInputs,
    stage1: Stage1Values,
    raw_response: bytes,
    *,
    scope: ArtifactScope,
    revision: int,
    draft_policy: CandidateEnrichmentDraftPolicy,
    catalog_policy: CandidateCatalogV2Policy,
    capability_policy: CandidateCapabilityPolicy,
) -> CandidateCatalogV2Compilation:
    """Compile one audited response into a deterministic pending catalog member."""

    inputs, stage1 = _require_inputs(inputs, stage1)
    draft_policy, catalog_policy, capability_policy = _require_policies(
        draft_policy, catalog_policy, capability_policy
    )
    if type(scope) is not ArtifactScope or scope != inputs.source_manifest.reference.scope:  # noqa: E721
        raise CandidateEnrichmentCompilerError("candidate output scope differs from committed inputs")
    if type(revision) is not int or not 1 <= revision <= 2**53 - 1:  # noqa: E721
        raise CandidateEnrichmentCompilerError("candidate output revision must be a positive safe integer")
    context = _context(
        inputs,
        stage1,
        draft_policy=draft_policy,
        catalog_policy=catalog_policy,
        capability_policy=capability_policy,
    )
    try:
        draft = decode_candidate_enrichment_draft(
            raw_response,
            policy=draft_policy,
            references=context.prompt.reference_catalog,
        )
    except ValueError as error:
        raise CandidateEnrichmentCompilerError("candidate enrichment draft is invalid") from error
    candidates = tuple(
        sorted(
            (
                _candidate(
                    item,
                    context=context,
                    input_binding_sha256=context.prompt.input_binding_sha256,
                    catalog_policy=catalog_policy,
                    capability_policy=capability_policy,
                )
                for item in draft.candidates
            ),
            key=lambda item: item.candidate_id,
        )
    )
    catalog_id = canonical_json_hash(
        {
            "schema_version": "candidate-catalog-v2",
            "input_binding_sha256": context.prompt.input_binding_sha256,
            "canonical_draft_sha256": draft.canonical_hash,
            "candidate_ids": [item.candidate_id for item in candidates],
            "catalog_policy_sha256": catalog_policy.canonical_hash,
            "capability_policy_sha256": capability_policy.canonical_hash,
        }
    )
    catalog = CandidateCatalogV2(
        catalog_id,
        context.prompt.input_binding_sha256,
        draft.canonical_hash,
        draft_policy.canonical_hash,
        catalog_policy.canonical_hash,
        capability_policy.canonical_hash,
        context.source_identity,
        context.card_identity,
        context.graph_identity,
        context.ledger_identity,
        candidates,
    )
    raw_catalog = canonical_json_bytes(catalog.to_mapping()).decode("utf-8")
    member = ArtifactMember(
        "candidate_catalog",
        "candidate_catalog",
        revision,
        scope,
        canonical_payload_hash(raw_catalog),
        raw_catalog,
    )
    return CandidateCatalogV2Compilation(member, catalog, draft, context.prompt)
