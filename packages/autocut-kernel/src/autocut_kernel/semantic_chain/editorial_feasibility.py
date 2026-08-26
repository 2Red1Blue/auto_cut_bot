"""Stage 3 material/timing witnesses, not Admission or physical edit safety.

Inputs come from the independently replayed predecessor reader. This pure
domain rebinds their actual payloads and selected material proofs, then checks
the tighter Blueprint declarations. It never treats a DTO as commitment.
Positive witnesses are independently checkable; negative search claims and
examined-state telemetry require recomputation by the future Admission owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Literal, cast

from ..contracts.compiler.canonical import canonical_json_hash
from ..store.models import CommittedSemanticInputs, CommittedVlmSemanticInput
from ..vlm.models import VlmCandidateHypothesis, VlmEvent
from ..vlm.window import ProxyTimelineMap
from .candidate_catalog import Candidate, CandidateSupport
from .candidate_duration import conservative_support_bounds, conservative_support_duration
from .candidate_projection import decode_candidate_source_context
from .editorial_blueprint import EditorialBlueprintProjection, project_editorial_blueprints
from .editorial_draft import EditorialBlueprintDraft
from .editorial_material_search import (
    MaterialSearchAlternative,
    MaterialSearchCandidate,
    MaterialSearchRequirement,
    MaterialSearchResult,
    search_editorial_materials,
    verify_editorial_material_assignment,
)
from .editorial_models import (
    EditorialBeatDraft,
    EvidenceRequirementDraft,
    StoryBlueprintDraft,
    editorial_array,
    editorial_hash,
    editorial_integer,
    editorial_mapping,
    editorial_text,
    editorial_tuple,
)
from .editorial_timing import solve_editorial_timing, verify_editorial_timing
from .material_support_models import RequirementAlternativeProof, RequirementMaterialSupport
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .narrative_models import (
    CoarseSourceRange,
    EventAttributes,
    FactAttributes,
    ObligationAttributes,
)
from .stage1_result import Stage1Values, decode_stage1_members
from .story_design_models import JobPolicy, SourceConstraints
from .story_design_result import StoryDesignValues, decode_story_design_members

FEASIBILITY_STRATEGY_VERSION = "editorial-material-feasibility-v1"
_MAX_SEARCH_STATES = 1_000_000
_MAX_RATIONAL_DIGITS = 65_536


class EditorialFeasibilityError(ValueError):
    """Malformed or unsupported declared material, not a provider failure."""


@dataclass(frozen=True, slots=True)
class EditorialFeasibilityPolicy:
    strategy_version: str
    max_search_states: int

    def __post_init__(self) -> None:
        if editorial_text(self.strategy_version) != FEASIBILITY_STRATEGY_VERSION:
            raise EditorialFeasibilityError("unsupported editorial feasibility strategy")
        if editorial_integer(self.max_search_states, minimum=1) > _MAX_SEARCH_STATES:
            raise EditorialFeasibilityError("editorial search budget exceeds implementation ceiling")

    def to_mapping(self) -> dict[str, object]:
        return {"strategy_version": self.strategy_version, "max_search_states": self.max_search_states}

    @classmethod
    def from_mapping(cls, value: object) -> EditorialFeasibilityPolicy:
        item = editorial_mapping(value, ("strategy_version", "max_search_states"))
        return cls(editorial_text(item["strategy_version"]), editorial_integer(item["max_search_states"], minimum=1))

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


def _decimal_integer(value: int) -> str:
    # Avoid both JSON-safe-integer loss and Python's process-global int/string
    # conversion limit. Denominator LCMs may exceed either; never clamp/round.
    chunks: list[int] = []
    while value:
        value, remainder = divmod(value, 10**9)
        chunks.append(remainder)
        if len(chunks) * 9 > _MAX_RATIONAL_DIGITS + 8:
            raise EditorialFeasibilityError("rational witness exceeds explicit digit ceiling")
    text = str(chunks.pop()) + "".join(f"{chunk:09d}" for chunk in reversed(chunks)) if chunks else "0"
    if len(text) > _MAX_RATIONAL_DIGITS:
        raise EditorialFeasibilityError("rational witness exceeds explicit digit ceiling")
    return text


def _fraction_mapping(value: Fraction) -> dict[str, str]:
    return {"numerator_decimal": _decimal_integer(value.numerator),
            "denominator_decimal": _decimal_integer(value.denominator)}


def _positive_decimal(value: object) -> int:
    text = editorial_text(value)
    if (len(text) > _MAX_RATIONAL_DIGITS or text[0] == "0"
            or any(character not in "0123456789" for character in text)):
        raise EditorialFeasibilityError("rational components must be bounded canonical positive decimal strings")
    result = 0
    for start in range(0, len(text), 9):
        chunk = text[start:start + 9]
        result = result * 10**len(chunk) + int(chunk)
    return result


def _fraction(value: object) -> Fraction:
    item = editorial_mapping(value, ("numerator_decimal", "denominator_decimal"))
    result = Fraction(_positive_decimal(item["numerator_decimal"]), _positive_decimal(item["denominator_decimal"]))
    if _fraction_mapping(result) != item:
        raise EditorialFeasibilityError("rational timing witness must use a reduced fraction")
    return result


@dataclass(frozen=True, slots=True)
class EditorialTimingWitness:
    story_id: str
    durations: tuple[Fraction, ...] | None

    def __post_init__(self) -> None:
        editorial_hash(self.story_id)
        if self.durations is not None:
            editorial_tuple(self.durations, Fraction, nonempty=True)
            for duration in self.durations:
                if duration <= 0:
                    raise EditorialFeasibilityError("Beat witness duration must be positive")
                _fraction_mapping(duration)

    def to_mapping(self) -> dict[str, object]:
        return {"story_id": self.story_id, "durations": None if self.durations is None else [
            _fraction_mapping(duration) for duration in self.durations
        ]}

    @classmethod
    def from_mapping(cls, value: object) -> EditorialTimingWitness:
        item = editorial_mapping(value, ("story_id", "durations"))
        return cls(editorial_hash(item["story_id"]), None if item["durations"] is None else editorial_array(item["durations"], _fraction))


@dataclass(frozen=True, slots=True)
class EditorialFeasibilityResult:
    input_binding_sha256: str
    projection_sha256: str
    policy_sha256: str
    timing_witnesses: tuple[EditorialTimingWitness, ...]
    material_search: MaterialSearchResult

    def __post_init__(self) -> None:
        for value in (self.input_binding_sha256, self.projection_sha256, self.policy_sha256):
            editorial_hash(value)
        editorial_tuple(self.timing_witnesses, EditorialTimingWitness, nonempty=True)
        if len({item.story_id for item in self.timing_witnesses}) != len(self.timing_witnesses):
            raise EditorialFeasibilityError("timing witnesses repeat a Story")
        if type(self.material_search) is not MaterialSearchResult:  # noqa: E721
            raise EditorialFeasibilityError("material search result must be an exact closed value")

    @property
    def status(self) -> Literal["feasible", "infeasible", "indeterminate"]:
        return "infeasible" if any(item.durations is None for item in self.timing_witnesses) else self.material_search.status

    def to_mapping(self) -> dict[str, object]:
        return {"schema_version": "editorial-feasibility-result-v1", "input_binding_sha256": self.input_binding_sha256,
                "projection_sha256": self.projection_sha256, "policy_sha256": self.policy_sha256,
                "timing_witnesses": [item.to_mapping() for item in self.timing_witnesses],
                "material_search": self.material_search.to_mapping(), "status": self.status}

    @classmethod
    def from_mapping(cls, value: object) -> EditorialFeasibilityResult:
        item = editorial_mapping(value, ("schema_version", "input_binding_sha256", "projection_sha256", "policy_sha256",
                                         "timing_witnesses", "material_search", "status"))
        if item["schema_version"] != "editorial-feasibility-result-v1":
            raise EditorialFeasibilityError("unsupported editorial feasibility result version")
        result = cls(editorial_hash(item["input_binding_sha256"]), editorial_hash(item["projection_sha256"]),
                     editorial_hash(item["policy_sha256"]), editorial_array(item["timing_witnesses"], EditorialTimingWitness.from_mapping),
                     MaterialSearchResult.from_mapping(item["material_search"]))
        if item["status"] != result.status:
            raise EditorialFeasibilityError("claimed feasibility differs from actual witness shape")
        return result

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


def _draft(projection: EditorialBlueprintProjection) -> EditorialBlueprintDraft:
    return EditorialBlueprintDraft(projection.input_binding_sha256, tuple(StoryBlueprintDraft(
        story.story_id, story.proposal_ref, tuple(EditorialBeatDraft(
            beat.narrative_role, beat.narrative_function, beat.summary, beat.required_obligation_refs, beat.required_fact_refs,
            tuple(EvidenceRequirementDraft(row.material_requirement_id, row.satisfaction, row.alternatives)
                  for row in beat.evidence_requirements), beat.candidate_preferences, beat.span_policy, beat.duration_seconds,
        ) for beat in story.beats), story.ordering_constraints, story.story_duration_seconds, story.editing_intent, story.teaser_intent,
    ) for story in projection.blueprints))


def _source_allowed(source: SemanticObjectRef, constraints: SourceConstraints) -> bool:
    return source not in constraints.forbidden_source_refs and (
        not constraints.allowed_source_refs or source in constraints.allowed_source_refs
    )


@dataclass(frozen=True, slots=True)
class _MaterialCandidate:
    candidate: Candidate
    raw: VlmCandidateHypothesis
    committed: CommittedVlmSemanticInput
    timeline: ProxyTimelineMap
    inner_start: int
    inner_end: int
    full_events: frozenset[SemanticObjectRef]
    direct_events: dict[SemanticObjectRef, VlmEvent]


def _candidate_domain(semantic: CommittedSemanticInputs, stage1: Stage1Values, stage2: StoryDesignValues) -> dict[SemanticObjectRef, _MaterialCandidate]:
    decoded = decode_candidate_source_context(semantic)
    catalog = stage2.business.candidate_catalog
    if catalog.source_grant_sha256 != decoded.census.canonical_hash or stage2.business.proposal_set.source_grant_sha256 != decoded.census.canonical_hash:
        raise EditorialFeasibilityError("material does not bind the actual Source grant")
    source = semantic.source_manifest.reference
    source_owner = SemanticMemberIdentity(source.artifact_type, source.logical_id, source.revision, source.scope, source.content_hash)
    if stage1.dependency_proof.source_member_ref != source_owner:
        raise EditorialFeasibilityError("Stage 1 proof belongs to a different Source")
    maps = {episode.manifest.canonical_hash: episode.manifest.timeline_map for episode in decoded.episodes}
    raw_candidates: dict[SemanticObjectRef, tuple[VlmCandidateHypothesis, CommittedVlmSemanticInput]] = {}
    for committed in semantic.inputs:
        ref = committed.semantic_pack.reference
        owner = SemanticMemberIdentity(ref.artifact_type, ref.logical_id, ref.revision, ref.scope, ref.content_hash)
        for raw in committed.semantic_pack.semantic_pack.candidate_hypotheses:
            key = SemanticObjectRef(owner, "vlm_candidate", raw.candidate_id)
            if key in raw_candidates:
                raise EditorialFeasibilityError("raw candidate identity is duplicated")
            raw_candidates[key] = (raw, committed)
    card_owner, graph_owner = stage1.coverage.identity("event_card_set"), stage1.coverage.identity("narrative_graph")
    cards = {item.event_id: item for item in stage1.coverage.event_cards.events}
    graph = {item.node_id: item for item in stage1.coverage.narrative_graph.nodes}
    result: dict[SemanticObjectRef, _MaterialCandidate] = {}
    catalog_owner = SemanticMemberIdentity.from_artifact_member(stage2.members[0])
    windows = {window.window_id: window for window in stage1.coverage.coverage_ledger.windows}
    for candidate in catalog.candidates:
        if candidate.candidate_ref not in raw_candidates:
            raise EditorialFeasibilityError("Catalog candidate is not in the actual raw owner")
        raw, committed = raw_candidates[candidate.candidate_ref]
        window = committed.source_window
        timeline = maps[window.window_manifest_sha256]
        expected_source = SemanticObjectRef(source_owner, "source", window.source_id)
        coverage_window = windows.get(candidate.coverage_window_id)
        if (candidate.source_ref != expected_source
                or candidate.source_window_ref != SemanticObjectRef(source_owner, "source_window", window.window_manifest_sha256)
                or coverage_window is None or coverage_window.source_ref != expected_source
                or coverage_window.source_window_ref != candidate.source_window_ref
                or candidate.narrative_functions != tuple(item.value for item in raw.narrative_functions)
                or candidate.editing_modes != tuple(item.value for item in raw.editing_modes)
                or candidate.support != CandidateSupport.from_vlm_support(raw.support, conservative_support_duration(raw.support, timeline))):
            raise EditorialFeasibilityError("Catalog candidate differs from raw capability/Source/support")
        inner_start, inner_end = conservative_support_bounds(raw.support, timeline)
        events = {item.event_id: item for item in committed.semantic_pack.semantic_pack.events}
        full_events: set[SemanticObjectRef] = set()
        direct_events: dict[SemanticObjectRef, VlmEvent] = {}
        for bindings, raw_ids, direct in (
            ((candidate.anchor_event,), (raw.anchor_event_ref,), True),
            (candidate.supporting_events, raw.supporting_event_refs, True),
            (candidate.payoff_events, raw.payoff_event_refs, True),
            (candidate.context_events, raw.context_event_refs, False),
        ):
            if {item.vlm_event_ref.object_id for item in bindings} != set(raw_ids):
                raise EditorialFeasibilityError("Catalog changed a raw candidate event role")
            for binding in bindings:
                event = events.get(binding.vlm_event_ref.object_id)
                if event is None:
                    raise EditorialFeasibilityError("candidate event is absent from its actual raw pack")
                card = cards.get(event.event_id)
                node = graph.get(event.event_id)
                support = event.support
                if (binding.vlm_event_ref != SemanticObjectRef(candidate.candidate_ref.member_ref, "vlm_event", event.event_id)
                        or binding.event_card_ref != SemanticObjectRef(card_owner, "event", event.event_id)
                        or binding.graph_event_ref != SemanticObjectRef(graph_owner, "event", event.event_id)
                        or card is None or card.evidence_refs != (binding.vlm_event_ref,)
                        or card.source_range_refs != (CoarseSourceRange(expected_source, window.source_clock_id, support.source_interval),)
                        or node is None or type(node.attributes) is not EventAttributes  # noqa: E721
                        or node.attributes.event_card_ref != binding.event_card_ref
                        or support.core_owner_window_manifest_sha256 != window.window_manifest_sha256
                        or timeline.map_interval(support.proxy_interval.proxy_range,
                                                 provider_uncertainty_proxy_pts=support.proxy_interval.uncertainty_pts) != support.source_interval):
                    raise EditorialFeasibilityError("direct Event owner/Card/window/mapping does not close")
                if direct:
                    direct_events[binding.event_card_ref] = event
                    outer = support.source_interval.coarse_range
                    if inner_start <= outer.start_pts < outer.end_pts <= inner_end:
                        full_events.add(binding.event_card_ref)
        result[SemanticObjectRef(catalog_owner, "candidate", candidate.candidate_id)] = _MaterialCandidate(
            candidate, raw, committed, timeline, inner_start, inner_end, frozenset(full_events), direct_events,
        )
    return result


def _material_proof(
    value: _MaterialCandidate, proof: RequirementAlternativeProof, support: RequirementMaterialSupport,
    stage1: Stage1Values, *, job_policy: JobPolicy, constraints: SourceConstraints,
) -> None:
    candidate, raw_pack = value.candidate, value.committed.semantic_pack.semantic_pack
    graph = {item.node_id: item for item in stage1.coverage.narrative_graph.nodes}
    raw_facts = {item.fact_id: item for item in raw_pack.facts}
    if (proof.source_ref != candidate.source_ref or proof.conservative_duration != candidate.support.conservative_duration
            or proof.conservative_duration.fraction < support.minimum_usable_seconds
            or not _source_allowed(candidate.source_ref, job_policy.source_constraints)
            or not _source_allowed(candidate.source_ref, constraints)):
        raise EditorialFeasibilityError("eligible material Source/minimum-duration proof does not close")
    roots = {candidate.source_ref, SemanticObjectRef(stage1.coverage.identity("coverage_ledger"), "coverage_window", candidate.coverage_window_id)}
    roots.update(item.event_card_ref for item in (candidate.anchor_event, *candidate.supporting_events, *candidate.payoff_events, *candidate.context_events))
    if any(seed.frontier_refs or roots.intersection(seed.affected_refs) for seed in stage1.dependency_proof.analysis.seed_closures):
        raise EditorialFeasibilityError("declared candidate has tainted or unknown dependency provenance")
    if tuple(item.graph_fact_ref for item in proof.fact_witnesses) != support.required_fact_refs:
        raise EditorialFeasibilityError("eligible candidate changed mandatory fact proof")
    for witness in proof.fact_witnesses:
        fact = raw_facts.get(witness.vlm_fact_ref.object_id)
        node = graph.get(witness.graph_fact_ref.object_id)
        via = {ref for ref, event in value.direct_events.items() if witness.graph_fact_ref.object_id in event.fact_refs}
        if (fact is None or node is None or type(node.attributes) is not FactAttributes  # noqa: E721
                or witness.vlm_fact_ref != SemanticObjectRef(candidate.candidate_ref.member_ref, "vlm_fact", fact.fact_id)
                or node.evidence_refs != (witness.vlm_fact_ref,) or set(witness.via_event_refs) != via
                or not via or witness.graph_fact_ref.member_ref != stage1.coverage.identity("narrative_graph")
                or fact.support.core_owner_window_manifest_sha256 != candidate.source_window_ref.object_id
                or value.timeline.map_interval(fact.support.proxy_interval.proxy_range,
                                               provider_uncertainty_proxy_pts=fact.support.proxy_interval.uncertainty_pts) != fact.support.source_interval
                or not value.inner_start <= fact.support.source_interval.coarse_range.start_pts < fact.support.source_interval.coarse_range.end_pts <= value.inner_end):
            raise EditorialFeasibilityError("eligible candidate does not carry exact required facts")


def _domain(
    stage1: Stage1Values, stage2: StoryDesignValues, projection: EditorialBlueprintProjection, *,
    semantic: CommittedSemanticInputs, job_policy: JobPolicy, policy: EditorialFeasibilityPolicy,
) -> tuple[MaterialSearchRequirement, ...]:
    if (type(stage1) is not Stage1Values or type(stage2) is not StoryDesignValues  # noqa: E721
            or type(projection) is not EditorialBlueprintProjection or type(semantic) is not CommittedSemanticInputs  # noqa: E721
            or type(job_policy) is not JobPolicy or type(policy) is not EditorialFeasibilityPolicy):  # noqa: E721
        raise EditorialFeasibilityError("feasibility requires exact explicit predecessor/projection/policy values")
    scope = semantic.source_manifest.reference.scope
    if (decode_stage1_members(stage1.members, scope=scope) != stage1 or decode_story_design_members(stage2.members, scope=scope) != stage2
            or stage1.admission.next_action != "continue" or stage2.admission.next_action != "continue"
            or job_policy.canonical_hash != stage2.business.portfolio.job_policy_sha256):
        raise EditorialFeasibilityError("feasibility predecessors or frozen JobPolicy differ from actual members")
    if project_editorial_blueprints(stage1, stage2, _draft(projection), expected_input_binding_sha256=projection.input_binding_sha256,
                                    strategy_version=projection.strategy_version) != projection:
        raise EditorialFeasibilityError("Blueprint changed actual selected Proposal/material fields")
    catalog = stage2.business.candidate_catalog
    if (catalog.event_card_member_ref != stage1.coverage.identity("event_card_set")
            or catalog.narrative_graph_member_ref != stage1.coverage.identity("narrative_graph")
            or catalog.coverage_ledger_member_ref != stage1.coverage.identity("coverage_ledger")
            or catalog.input_binding_sha256 != stage1.admission.input_binding_sha256):
        raise EditorialFeasibilityError("Catalog belongs to different Stage 1 members")
    candidates = _candidate_domain(semantic, stage1, stage2)
    graph = {item.node_id: item for item in stage1.coverage.narrative_graph.nodes}
    requirements: list[MaterialSearchRequirement] = []
    for story, selection in zip(projection.blueprints, stage2.business.portfolio.selections, strict=True):
        selected = stage2.business.proposal_set.proposals[selection.proposal_index]
        if (selected.narrative_taint_seed_refs or selected.dependency_unknown or selected.status != "supported"
                or any(set(selected.proposal.narrative_refs).intersection(seed.affected_refs)
                       for seed in stage1.dependency_proof.analysis.seed_closures)):
            raise EditorialFeasibilityError("selected Proposal has no untainted complete material support")
        material = {row.requirement_id: row for row in selected.requirements}
        for beat in story.beats:
            pool_refs = {ref for row in beat.evidence_requirements for alternative in row.alternatives for ref in alternative.candidate_refs}
            if not set(beat.candidate_preferences) <= pool_refs:
                raise EditorialFeasibilityError("Beat preferences must belong to its legal alternative pools")
            for row in beat.evidence_requirements:
                support = material[row.material_requirement_id]
                obligation = graph[row.obligation_ref.object_id]
                if (type(obligation.attributes) is not ObligationAttributes  # noqa: E721
                        or set(obligation.attributes.required_fact_ids) != {ref.object_id for ref in support.required_fact_refs}):
                    raise EditorialFeasibilityError("material facts differ from actual Graph obligation")
                eligible = {item.candidate_ref: item for item in support.alternatives}
                alternatives: list[MaterialSearchAlternative] = []
                for alternative in row.alternatives:
                    options: list[MaterialSearchCandidate] = []
                    for ref in alternative.candidate_refs:
                        if ref not in candidates or ref not in eligible:
                            raise EditorialFeasibilityError("declared candidate is not an eligible selected material alternative")
                        value = candidates[ref]
                        if beat.narrative_function.value not in value.candidate.narrative_functions:
                            raise EditorialFeasibilityError("declared candidate cannot support the Beat narrative function")
                        _material_proof(value, eligible[ref], support, stage1, job_policy=job_policy, constraints=row.source_constraints)
                        events = value.full_events.intersection(alternative.event_refs)
                        options.append(MaterialSearchCandidate(ref.canonical_hash, value.candidate.source_ref.canonical_hash,
                                                               tuple(sorted(event.canonical_hash for event in events))))
                    alternatives.append(MaterialSearchAlternative(alternative.alternative_id,
                        tuple(sorted(ref.canonical_hash for ref in alternative.event_refs)), tuple(sorted(options, key=lambda item: item.candidate_key))))
                requirements.append(MaterialSearchRequirement(story.story_id, row.evidence_requirement_id,
                                                               cast(Literal["one_of", "all_of"], row.satisfaction),
                                                               tuple(sorted(alternatives, key=lambda item: item.alternative_key))))
    return tuple(requirements)


def _binding(stage1: Stage1Values, stage2: StoryDesignValues, projection: EditorialBlueprintProjection,
             semantic: CommittedSemanticInputs, policy: EditorialFeasibilityPolicy) -> str:
    return canonical_json_hash({"schema_version": "editorial-feasibility-input-v1",
        "source_provenance_sha256": semantic.source_manifest.canonical_hash,
        "vlm_semantic_pack_set": semantic.vlm_semantic_pack_set.to_mapping(),
        "stage1_members": [SemanticMemberIdentity.from_artifact_member(member).to_mapping() for member in stage1.members],
        "stage2_members": [SemanticMemberIdentity.from_artifact_member(member).to_mapping() for member in stage2.members],
        "projection_sha256": projection.canonical_hash, "policy_sha256": policy.canonical_hash})


def evaluate_editorial_feasibility(
    stage1: Stage1Values, stage2: StoryDesignValues, projection: EditorialBlueprintProjection, *,
    semantic: CommittedSemanticInputs, job_policy: JobPolicy, policy: EditorialFeasibilityPolicy,
) -> EditorialFeasibilityResult:
    """Solve complete editorial intent and one joint nonempty material assignment."""
    requirements = _domain(stage1, stage2, projection, semantic=semantic, job_policy=job_policy, policy=policy)
    timing = tuple(EditorialTimingWitness(story.story_id, solve_editorial_timing(
        tuple(beat.duration_seconds for beat in story.beats), story.story_duration_seconds, story.ordering_constraints,
    )) for story in projection.blueprints)
    search = search_editorial_materials(requirements, source_reuse=cast(Literal["allow", "forbid"], job_policy.source_reuse_policy),
                                        max_search_states=policy.max_search_states)
    return EditorialFeasibilityResult(_binding(stage1, stage2, projection, semantic, policy), projection.canonical_hash,
                                      policy.canonical_hash, timing, search)


def verify_editorial_feasibility(
    stage1: Stage1Values, stage2: StoryDesignValues, projection: EditorialBlueprintProjection,
    result: EditorialFeasibilityResult, *, semantic: CommittedSemanticInputs, job_policy: JobPolicy,
    policy: EditorialFeasibilityPolicy,
) -> None:
    """Verify only a complete positive witness, without either solver/evaluator.

    Does not certify canonical search order, negative/completeness claims or
    exact examined-state telemetry. The independent Admission recomputes those.
    """
    requirements = _domain(stage1, stage2, projection, semantic=semantic, job_policy=job_policy, policy=policy)
    if (type(result) is not EditorialFeasibilityResult or result.status != "feasible"  # noqa: E721
            or result.input_binding_sha256 != _binding(stage1, stage2, projection, semantic, policy)
            or result.projection_sha256 != projection.canonical_hash or result.policy_sha256 != policy.canonical_hash
            or tuple(item.story_id for item in result.timing_witnesses) != tuple(story.story_id for story in projection.blueprints)
            or result.material_search.examined_states > policy.max_search_states):
        raise EditorialFeasibilityError("result is not a complete positive witness for these exact inputs")
    for story, timing in zip(projection.blueprints, result.timing_witnesses, strict=True):
        if timing.durations is None:
            raise EditorialFeasibilityError("positive result is missing a timing witness")
        verify_editorial_timing(tuple(beat.duration_seconds for beat in story.beats), story.story_duration_seconds,
                                story.ordering_constraints, timing.durations)
    verify_editorial_material_assignment(requirements, result.material_search.choices,
                                        source_reuse=cast(Literal["allow", "forbid"], job_policy.source_reuse_policy))
