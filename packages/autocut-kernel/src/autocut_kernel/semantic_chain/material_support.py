"""Pure Stage 2 material support over exact decoded semantic inputs.

No I/O, selection, Story IDs or Admission. The future Command owns committed
reads and the full Stage 1 audit. This boundary rebinds content, reconstructs
the Catalog and dependency proof, and never treats their Python types as
authority. Physical requirements remain deferred even for supported rows.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from ..contracts.compiler.canonical import canonical_json_bytes
from ..store.models import (
    CommittedSemanticInputs,
    CommittedVlmSemanticInput,
)
from ..vlm.models import VlmCandidateHypothesis, VlmFact
from ..vlm.window import ProxyTimelineMap
from .candidate_catalog import Candidate, CandidateCatalogPolicy
from .candidate_duration import conservative_support_bounds
from .candidate_projection import (
    CandidateCatalogProjection,
    decode_candidate_source_context,
    project_candidate_catalog,
)
from .dependency_projection import DependencyProjectionPolicy
from .dependency_verification import verify_dependency_proof
from .material_support_models import (
    ExclusionReasonCount,
    FactCarryWitness,
    MaterialSupportError,
    MaterialSupportEvaluation,
    ProposalMaterialSupport,
    RequirementAlternativeProof,
    RequirementMaterialSupport,
)
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .narrative_models import (
    Confidence,
    FactAttributes,
    FactEntityRefValue,
    FactTextValue,
    ObligationAttributes,
)
from .stage1_result import Stage1Values
from .story_design_context import story_design_input_binding
from .story_design_draft import ProposalDraftSet
from .story_design_models import (
    JobPolicy,
    MaterialRequirement,
    SourceConstraints,
    StoryDesignPolicy,
)
from .story_design_validation import validate_story_proposals


def _sorted_refs(refs: set[SemanticObjectRef]) -> tuple[SemanticObjectRef, ...]:
    return tuple(sorted(refs, key=lambda ref: canonical_json_bytes(ref.to_mapping())))


@dataclass(frozen=True, slots=True)
class _Fact:
    value: VlmFact
    raw_ref: SemanticObjectRef
    window_id: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    value: Candidate
    raw: VlmCandidateHypothesis
    committed: CommittedVlmSemanticInput
    direct_fact_events: dict[str, set[str]]
    context_only_facts: frozenset[str]
    inner_start: int
    inner_end: int
    tainted: bool
    ineligibility_reasons: tuple[str, ...]


def _source_context(inputs: CommittedSemanticInputs) -> dict[str, ProxyTimelineMap]:
    decoded = decode_candidate_source_context(inputs)
    return {episode.manifest.canonical_hash: episode.manifest.timeline_map for episode in decoded.episodes}


def _fact_context(inputs: CommittedSemanticInputs, stage1: Stage1Values) -> dict[str, _Fact]:
    nodes = {node.node_id: node for node in stage1.coverage.narrative_graph.nodes}
    result: dict[str, _Fact] = {}
    for item in inputs.inputs:
        reference = item.semantic_pack.reference
        owner = SemanticMemberIdentity(reference.artifact_type, reference.logical_id,
                                       reference.revision, reference.scope, reference.content_hash)
        for fact in item.semantic_pack.semantic_pack.facts:
            if fact.fact_id in result:
                raise MaterialSupportError("raw Fact identity is duplicated")
            raw_ref = SemanticObjectRef(owner, "vlm_fact", fact.fact_id)
            node = nodes.get(fact.fact_id)
            expected_value = FactEntityRefValue(fact.object_ref) if fact.object_ref is not None else FactTextValue(fact.summary)
            if (node is None or node.node_type != "fact" or type(node.attributes) is not FactAttributes  # noqa: E721
                    or node.evidence_refs != (raw_ref,) or node.label != fact.summary
                    or node.attributes.subject_node_id != fact.subject_ref
                    or node.attributes.predicate != fact.fact_kind.value
                    or node.attributes.value != expected_value
                    or node.confidence != Confidence.from_decimal(fact.support.confidence, method="model")):
                raise MaterialSupportError("Graph Fact differs from exact raw fact/owner/confidence")
            result[fact.fact_id] = _Fact(fact, raw_ref, item.source_window.window_manifest_sha256)
    return result


def _dependency_context(inputs: CommittedSemanticInputs, stage1: Stage1Values) -> bool:
    members = {member.artifact_type: member for member in stage1.members}
    # This is the sole registered algorithm identity, not a policy fallback.
    # The verifier requires its hash to match the supplied proof exactly.
    checks = verify_dependency_proof(
        inputs, graph_member=members["narrative_graph"], event_card_member=members["event_card_set"],
        ledger_member=members["coverage_ledger"], proof_member=members["dependency_closure_proof"],
        policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
    )
    if any(set(check.violation_codes) - {"unbounded_frontier"} for check in checks):
        raise MaterialSupportError("dependency proof differs from independently reconstructed inputs")
    return any(seed.frontier_refs for seed in stage1.dependency_proof.analysis.seed_closures)


def _seed_refs(stage1: Stage1Values, roots: set[SemanticObjectRef]) -> tuple[SemanticObjectRef, ...]:
    ledger = stage1.coverage.identity("coverage_ledger")
    return _sorted_refs({SemanticObjectRef(ledger, "taint_seed", seed.seed_id)
                         for seed in stage1.dependency_proof.analysis.seed_closures
                         if roots.intersection(seed.affected_refs)})


def _candidates(
    inputs: CommittedSemanticInputs, stage1: Stage1Values, projection: CandidateCatalogProjection,
    maps: dict[str, ProxyTimelineMap], policy: CandidateCatalogPolicy,
) -> tuple[_Candidate, ...]:
    projected = {item.candidate_id: item for item in projection.catalog.candidates}
    result: list[_Candidate] = []
    for item in inputs.inputs:
        pack = item.semantic_pack.semantic_pack
        events = {event.event_id: event for event in pack.events}
        for raw in pack.candidate_hypotheses:
            candidate = projected[raw.candidate_id]
            direct: dict[str, set[str]] = {}
            for event_id in {raw.anchor_event_ref, *raw.supporting_event_refs, *raw.payoff_event_refs}:
                for fact_id in events[event_id].fact_refs:
                    direct.setdefault(fact_id, set()).add(event_id)
            context = {fact_id for event_id in raw.context_event_refs for fact_id in events[event_id].fact_refs}
            all_events = (candidate.anchor_event, *candidate.supporting_events,
                          *candidate.payoff_events, *candidate.context_events)
            roots = {binding.event_card_ref for binding in all_events}
            roots.update((candidate.source_ref, SemanticObjectRef(
                projection.catalog.coverage_ledger_member_ref, "coverage_window", candidate.coverage_window_id,
            )))
            start, end = conservative_support_bounds(raw.support, maps[item.source_window.window_manifest_sha256])
            reasons = _candidate_policy_reasons(candidate, policy)
            result.append(_Candidate(candidate, raw, item, direct, frozenset(context - direct.keys()),
                                     start, end, bool(_seed_refs(stage1, roots)), reasons))
    return tuple(sorted(result, key=lambda item: item.value.candidate_id))


def _candidate_policy_reasons(candidate: Candidate, policy: CandidateCatalogPolicy) -> tuple[str, ...]:
    """Eligibility does not delete structurally valid candidates from Catalog."""
    threshold = Decimal(policy.minimum_confidence)
    reasons: set[str] = set()
    if Decimal(candidate.support.confidence) < threshold:
        reasons.add("candidate_confidence_below_policy")
    if any(Decimal(item.confidence) < threshold for item in candidate.measurements):
        reasons.add("measurement_confidence_below_policy")
    if not set(policy.required_measurement_kinds) <= {item.measurement_kind for item in candidate.measurements}:
        reasons.add("required_measurement_missing")
    return tuple(sorted(reasons))


def _source_reason(source: SemanticObjectRef, constraints: SourceConstraints) -> str | None:
    if source in constraints.forbidden_source_refs:
        return "source_forbidden"
    if constraints.allowed_source_refs and source not in constraints.allowed_source_refs:
        return "source_not_allowed"
    return None


def _requirement(
    requirement: MaterialRequirement, facts: tuple[SemanticObjectRef, ...], *,
    candidates: tuple[_Candidate, ...], raw_facts: dict[str, _Fact], maps: dict[str, ProxyTimelineMap],
    catalog_ref: SemanticMemberIdentity, card_ref: SemanticMemberIdentity,
    job_policy: JobPolicy, dependency_unknown: bool,
) -> RequirementMaterialSupport:
    alternatives: list[RequirementAlternativeProof] = []
    excluded_tainted: set[SemanticObjectRef] = set()
    counts: Counter[str] = Counter()
    for candidate in candidates:
        value = candidate.value
        ref = SemanticObjectRef(catalog_ref, "candidate", value.candidate_id)
        witnesses: list[FactCarryWitness] = []
        reasons: set[str] = set(candidate.ineligibility_reasons)
        if candidate.tainted:
            excluded_tainted.add(ref)
            reasons.add("candidate_tainted")
        for constraints in (job_policy.source_constraints, requirement.source_constraints):
            reason = _source_reason(value.source_ref, constraints)
            if reason is not None:
                reasons.add(reason)
        for graph_fact in facts:
            raw = raw_facts[graph_fact.object_id]
            if graph_fact.object_id not in candidate.direct_fact_events:
                reasons.add("fact_context_only" if graph_fact.object_id in candidate.context_only_facts
                            else "fact_not_declared")
                continue
            window_id = candidate.committed.source_window.window_manifest_sha256
            if raw.window_id != window_id or raw.raw_ref.member_ref != candidate.value.candidate_ref.member_ref:
                raise MaterialSupportError("direct Fact has foreign window/pack owner")
            support = raw.value.support
            timeline = maps[window_id]
            mapped = timeline.map_interval(support.proxy_interval.proxy_range,
                                           provider_uncertainty_proxy_pts=support.proxy_interval.uncertainty_pts)
            if (mapped != support.source_interval
                    or support.core_owner_window_manifest_sha256 != window_id):
                raise MaterialSupportError("Fact support differs from exact window timeline")
            outer = support.source_interval.coarse_range
            if not candidate.inner_start <= outer.start_pts < outer.end_pts <= candidate.inner_end:
                reasons.add("fact_outside_support")
                continue
            witnesses.append(FactCarryWitness(graph_fact, raw.raw_ref, _sorted_refs({
                SemanticObjectRef(card_ref, "event", event_id)
                for event_id in candidate.direct_fact_events[graph_fact.object_id]
            })))
        if value.support.conservative_duration.fraction < requirement.minimum_usable_seconds:
            reasons.add("duration_insufficient")
        # Each excluded candidate has exactly one primary reason. Known failures
        # take precedence over unknown isolation; no unfinished possibility can
        # be counted as proven unsupported merely to make selection progress.
        if reasons:
            counts[min(reasons)] += 1
        elif dependency_unknown:
            counts["dependency_frontier_unknown"] += 1
        else:
            alternatives.append(RequirementAlternativeProof(
                ref, value.source_ref, tuple(witnesses), value.support.conservative_duration,
            ))
    alternatives.sort(key=lambda item: canonical_json_bytes(item.candidate_ref.to_mapping()))
    return RequirementMaterialSupport(
        requirement.requirement_id, facts, requirement.minimum_usable_seconds,
        requirement.physical_requirements_hash, tuple(alternatives), _sorted_refs(excluded_tainted),
        tuple(ExclusionReasonCount(reason, count) for reason, count in sorted(counts.items())), len(candidates),
    )


def evaluate_material_support(
    inputs: CommittedSemanticInputs, stage1: Stage1Values, projection: CandidateCatalogProjection,
    draft: ProposalDraftSet, *, job_policy: JobPolicy, story_policy: StoryDesignPolicy,
    candidate_policy: CandidateCatalogPolicy,
) -> MaterialSupportEvaluation:
    """Retain every proposal/requirement and every safe alternative, with no top-K."""
    if type(inputs) is not CommittedSemanticInputs or type(draft) is not ProposalDraftSet:  # noqa: E721
        raise MaterialSupportError("material support requires exact typed semantic inputs/draft")
    try:
        binding = story_design_input_binding(stage1, projection, job_policy=job_policy,
                                            story_policy=story_policy, candidate_policy=candidate_policy)
        if binding != draft.input_binding_sha256:
            raise MaterialSupportError("draft does not bind actual Stage 2 semantic inputs")
        catalog_ref = SemanticMemberIdentity.from_artifact_member(projection.member)
        if catalog_ref.scope != inputs.source_manifest.reference.scope:
            raise MaterialSupportError("Catalog differs from exact Source scope")
        maps = _source_context(inputs)
        expected = project_candidate_catalog(inputs, stage1, scope=catalog_ref.scope,
                                             revision=catalog_ref.revision, policy=candidate_policy)
        if expected.catalog != projection.catalog or SemanticMemberIdentity.from_artifact_member(expected.member) != catalog_ref:
            raise MaterialSupportError("Catalog differs from recomputed committed VLM projection")
        graph_ref = stage1.coverage.identity("narrative_graph")
        source = inputs.source_manifest.reference
        source_owner = SemanticMemberIdentity(source.artifact_type, source.logical_id,
                                              source.revision, source.scope, source.content_hash)
        validate_story_proposals(
            draft, graph=stage1.coverage.narrative_graph,
            graph_object_refs=tuple(SemanticObjectRef(graph_ref, node.node_type, node.node_id)
                                    for node in stage1.coverage.narrative_graph.nodes),
            source_refs=tuple(SemanticObjectRef(source_owner, "source", item.source_id)
                              for item in inputs.source_grant.sources),
            job_policy=job_policy, story_policy=story_policy,
        )
        raw_facts = _fact_context(inputs, stage1)
        unknown = _dependency_context(inputs, stage1)
        candidates = _candidates(inputs, stage1, projection, maps, candidate_policy)
        nodes = {node.node_id: node for node in stage1.coverage.narrative_graph.nodes}
        proposals: list[ProposalMaterialSupport] = []
        for index, proposal in enumerate(draft.proposals):
            rows: list[RequirementMaterialSupport] = []
            for requirement in proposal.material_requirements:
                attributes = nodes[requirement.obligation_ref.object_id].attributes
                if type(attributes) is not ObligationAttributes:  # noqa: E721
                    raise MaterialSupportError("material requirement has a non-obligation Graph target")
                facts = _sorted_refs({SemanticObjectRef(graph_ref, "fact", fact_id)
                                      for fact_id in attributes.required_fact_ids})
                rows.append(_requirement(
                    requirement, facts, candidates=candidates, raw_facts=raw_facts, maps=maps,
                    catalog_ref=catalog_ref, card_ref=stage1.coverage.identity("event_card_set"),
                    job_policy=job_policy, dependency_unknown=unknown,
                ))
            proposals.append(ProposalMaterialSupport(
                index, proposal, tuple(rows), _seed_refs(stage1, set(proposal.narrative_refs)), unknown,
            ))
        return MaterialSupportEvaluation(binding, draft.canonical_hash, catalog_ref,
                                         inputs.source_grant.canonical_hash, tuple(proposals))
    except (ValueError, KeyError) as error:
        if isinstance(error, MaterialSupportError):
            raise
        raise MaterialSupportError("material input/provenance does not close") from error
