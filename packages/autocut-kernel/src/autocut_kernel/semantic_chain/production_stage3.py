"""Stage 3 Blueprint, partition/merge, closure, context, and admission models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Sequence, cast

from ..contracts.compiler.canonical import canonical_json_hash
from ..contracts.compiler.refs import ArtifactRef, DomainRef
from .production_common import (
    CanonicalModel,
    DurationRangeSeconds,
    EvaluatorOwnedModel,
    PendingBusinessSet,
    ProductionModelError,
    RuleResult,
    TimeBaseValue,
    canonical_artifact_refs,
    canonical_domain_refs,
    canonical_ids,
    canonical_values,
    computed_rule_results,
    identifier,
    integer,
    jcs_key,
    mapping,
    safe_token,
    sha256,
    text,
)
from .production_stage2 import (
    Candidate,
    CandidateCatalog,
    MaterialRequirement,
    NarrativeFunction,
    PhysicalRequirement,
    Portfolio,
    PortfolioAdmission,
    ProposalSet,
    SourceAuthorizationRef,
    SourceUsageLedger,
    physical_tuple,
)


@dataclass(frozen=True, slots=True)
class CandidateAlternative(CanonicalModel):
    alternative_id: str
    event_refs: tuple[DomainRef, ...]
    candidate_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        identifier(self.alternative_id, "alternative_id")
        events = canonical_domain_refs(self.event_refs, "alternative event_refs", nonempty=True)
        candidates = canonical_domain_refs(
            self.candidate_refs, "alternative candidate_refs", nonempty=True
        )
        if any(item.object_type != "event" for item in events):
            raise ProductionModelError("alternative event_refs must point to Events")
        if any(item.object_type != "candidate" for item in candidates):
            raise ProductionModelError("alternative candidate_refs must point to Candidates")
        object.__setattr__(self, "event_refs", events)
        object.__setattr__(self, "candidate_refs", candidates)

    def to_mapping(self) -> dict[str, object]:
        return {
            "alternative_id": self.alternative_id,
            "candidate_refs": [item.to_mapping() for item in self.candidate_refs],
            "event_refs": [item.to_mapping() for item in self.event_refs],
        }


@dataclass(frozen=True, slots=True)
class RequiredCandidate(CanonicalModel):
    candidate_ref: DomainRef
    reason: str
    authorization_ref: SourceAuthorizationRef

    def __post_init__(self) -> None:
        if type(self.candidate_ref) is not DomainRef or self.candidate_ref.object_type != "candidate":  # noqa: E721
            raise ProductionModelError("required Candidate ref has the wrong type")
        text(self.reason, "required Candidate reason")
        if type(self.authorization_ref) is not SourceAuthorizationRef:  # noqa: E721
            raise ProductionModelError("required Candidate authorization is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "authorization_ref": self.authorization_ref.to_mapping(),
            "candidate_ref": self.candidate_ref.to_mapping(),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class EvidenceRequirement(CanonicalModel):
    requirement_id: str
    source_material_requirement_id: str
    satisfaction: str
    alternative_sets: tuple[CandidateAlternative, ...]
    physical_requirements: tuple[PhysicalRequirement, ...]
    required_candidates: tuple[RequiredCandidate, ...] = ()

    def __post_init__(self) -> None:
        identifier(self.requirement_id, "requirement_id")
        identifier(self.source_material_requirement_id, "source_material_requirement_id")
        if self.satisfaction not in {"one_of", "all_of"}:
            raise ProductionModelError("satisfaction is unknown")
        alternatives = cast(
            tuple[CandidateAlternative, ...],
            canonical_values(
                self.alternative_sets,
                CandidateAlternative,
                "alternative_sets",
                nonempty=True,
            ),
        )
        if len({item.alternative_id for item in alternatives}) != len(alternatives):
            raise ProductionModelError("alternative sets must have unique IDs")
        required = cast(
            tuple[RequiredCandidate, ...],
            canonical_values(self.required_candidates, RequiredCandidate, "required_candidates"),
        )
        object.__setattr__(self, "alternative_sets", alternatives)
        object.__setattr__(self, "required_candidates", required)
        object.__setattr__(
            self,
            "physical_requirements",
            physical_tuple(self.physical_requirements, "physical_requirements"),
        )

    @property
    def physical_requirements_hash(self) -> str:
        return canonical_json_hash([item.to_mapping() for item in self.physical_requirements])

    def to_mapping(self) -> dict[str, object]:
        return {
            "alternative_sets": [item.to_mapping() for item in self.alternative_sets],
            "physical_requirements": [item.to_mapping() for item in self.physical_requirements],
            "physical_requirements_hash": self.physical_requirements_hash,
            "required_candidates": [item.to_mapping() for item in self.required_candidates],
            "requirement_id": self.requirement_id,
            "satisfaction": self.satisfaction,
            "source_material_requirement_id": self.source_material_requirement_id,
        }


@dataclass(frozen=True, slots=True)
class SpanPolicy(CanonicalModel):
    preferred: str
    allowed: tuple[str, ...]
    fallback_order: tuple[str, ...]

    def __post_init__(self) -> None:
        vocabulary = {"tight", "scene", "context"}
        allowed = tuple(self.allowed)
        fallback = tuple(self.fallback_order)
        if not allowed or set(allowed) - vocabulary or len(allowed) != len(set(allowed)):
            raise ProductionModelError("span_policy.allowed is not a closed unique set")
        if tuple(sorted(allowed, key=jcs_key)) != allowed:
            raise ProductionModelError("span_policy.allowed must use canonical JCS-byte order")
        if self.preferred not in allowed:
            raise ProductionModelError("span_policy.preferred must be allowed")
        if len(fallback) != len(set(fallback)) or set(fallback) != set(allowed):
            raise ProductionModelError("span_policy.fallback_order must permute allowed")
        object.__setattr__(self, "allowed", allowed)
        object.__setattr__(self, "fallback_order", fallback)

    def to_mapping(self) -> dict[str, object]:
        return {
            "allowed": list(self.allowed),
            "fallback_order": list(self.fallback_order),
            "preferred": self.preferred,
        }


@dataclass(frozen=True, slots=True)
class BlueprintBeat(CanonicalModel):
    blueprint_beat_id: str
    stable_beat_id: str
    narrative_role: str
    narrative_function: NarrativeFunction
    summary: str
    required_obligation_refs: tuple[DomainRef, ...]
    required_fact_refs: tuple[DomainRef, ...]
    evidence_requirements: tuple[EvidenceRequirement, ...]
    candidate_preferences: tuple[DomainRef, ...]
    span_policy: SpanPolicy
    duration_seconds: DurationRangeSeconds

    def __post_init__(self) -> None:
        identifier(self.blueprint_beat_id, "blueprint_beat_id")
        sha256(self.stable_beat_id, "stable_beat_id")
        if self.narrative_role not in {
            "setup",
            "escalation",
            "turn",
            "reveal",
            "payoff",
            "consequence",
            "coda",
        }:
            raise ProductionModelError("narrative_role is unknown")
        if type(self.narrative_function) is not NarrativeFunction:  # noqa: E721
            raise ProductionModelError("narrative_function is not in the closed vocabulary")
        text(self.summary, "beat summary")
        obligations = canonical_domain_refs(
            self.required_obligation_refs, "required_obligation_refs", nonempty=True
        )
        facts = canonical_domain_refs(self.required_fact_refs, "required_fact_refs", nonempty=True)
        if any(item.object_type != "obligation" for item in obligations):
            raise ProductionModelError("required obligations have the wrong type")
        if any(item.object_type != "fact" for item in facts):
            raise ProductionModelError("required facts have the wrong type")
        requirements = cast(
            tuple[EvidenceRequirement, ...],
            canonical_values(
                self.evidence_requirements,
                EvidenceRequirement,
                "evidence_requirements",
                nonempty=True,
            ),
        )
        if len({item.requirement_id for item in requirements}) != len(requirements):
            raise ProductionModelError("evidence requirements must have unique IDs")
        preferences = canonical_domain_refs(self.candidate_preferences, "candidate_preferences")
        alternatives = {
            jcs_key(ref)
            for requirement in requirements
            for alternative in requirement.alternative_sets
            for ref in alternative.candidate_refs
        }
        if any(jcs_key(item) not in alternatives for item in preferences):
            raise ProductionModelError("candidate preferences must be legal alternatives")
        if type(self.span_policy) is not SpanPolicy or type(self.duration_seconds) is not DurationRangeSeconds:  # noqa: E721
            raise ProductionModelError("beat span/duration policy is invalid")
        object.__setattr__(self, "required_obligation_refs", obligations)
        object.__setattr__(self, "required_fact_refs", facts)
        object.__setattr__(self, "evidence_requirements", requirements)
        object.__setattr__(self, "candidate_preferences", preferences)

    def to_mapping(self) -> dict[str, object]:
        return {
            "blueprint_beat_id": self.blueprint_beat_id,
            "candidate_preferences": [item.to_mapping() for item in self.candidate_preferences],
            "duration_seconds": self.duration_seconds.to_mapping(),
            "evidence_requirements": [item.to_mapping() for item in self.evidence_requirements],
            "narrative_function": self.narrative_function.value,
            "narrative_role": self.narrative_role,
            "required_fact_refs": [item.to_mapping() for item in self.required_fact_refs],
            "required_obligation_refs": [
                item.to_mapping() for item in self.required_obligation_refs
            ],
            "span_policy": self.span_policy.to_mapping(),
            "stable_beat_id": self.stable_beat_id,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class TickGap(CanonicalModel):
    tick: int
    time_base: TimeBaseValue

    def __post_init__(self) -> None:
        integer(self.tick, "maximum_gap.tick", minimum=1)
        if type(self.time_base) is not TimeBaseValue:  # noqa: E721
            raise ProductionModelError("maximum_gap.time_base is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {"tick": self.tick, "time_base": self.time_base.to_mapping()}


@dataclass(frozen=True, slots=True)
class OrderingConstraint(CanonicalModel):
    constraint_type: str
    first_beat_id: str
    second_beat_id: str
    maximum_gap: TickGap | None = None

    def __post_init__(self) -> None:
        if self.constraint_type not in {"precedes", "adjacent", "max_gap"}:
            raise ProductionModelError("ordering constraint type is unknown")
        sha256(self.first_beat_id, "ordering first Beat ID")
        sha256(self.second_beat_id, "ordering second Beat ID")
        if self.first_beat_id == self.second_beat_id:
            raise ProductionModelError("ordering constraint cannot self-reference")
        if self.constraint_type == "max_gap":
            if type(self.maximum_gap) is not TickGap:  # noqa: E721
                raise ProductionModelError("max_gap requires a tick/time-base bound")
        elif self.maximum_gap is not None:
            raise ProductionModelError("only max_gap may contain maximum_gap")

    @classmethod
    def from_mapping(cls, value: object) -> OrderingConstraint:
        if type(value) is not dict:  # noqa: E721
            raise ProductionModelError("ordering constraint must be an object")
        raw = cast(dict[str, object], value)
        kind = text(raw.get("constraint_type"), "constraint_type")
        if kind == "precedes":
            item = mapping(raw, {"constraint_type", "before_beat_id", "after_beat_id"}, "precedes")
            return cls(
                kind,
                sha256(item["before_beat_id"], "before_beat_id"),
                sha256(item["after_beat_id"], "after_beat_id"),
            )
        if kind == "adjacent":
            item = mapping(raw, {"constraint_type", "first_beat_id", "second_beat_id"}, "adjacent")
            return cls(
                kind,
                sha256(item["first_beat_id"], "first_beat_id"),
                sha256(item["second_beat_id"], "second_beat_id"),
            )
        if kind == "max_gap":
            item = mapping(
                raw,
                {"constraint_type", "before_beat_id", "after_beat_id", "maximum_gap"},
                "max_gap",
            )
            gap = mapping(item["maximum_gap"], {"tick", "time_base"}, "maximum_gap")
            base = mapping(gap["time_base"], {"num", "den"}, "maximum_gap.time_base")
            return cls(
                kind,
                sha256(item["before_beat_id"], "before_beat_id"),
                sha256(item["after_beat_id"], "after_beat_id"),
                TickGap(
                    integer(gap["tick"], "maximum_gap.tick", minimum=1),
                    TimeBaseValue(
                        integer(base["num"], "maximum_gap.time_base.num", minimum=1),
                        integer(base["den"], "maximum_gap.time_base.den", minimum=1),
                    ),
                ),
            )
        raise ProductionModelError("ordering constraint type is unknown")

    def to_mapping(self) -> dict[str, object]:
        if self.constraint_type == "adjacent":
            return {
                "constraint_type": self.constraint_type,
                "first_beat_id": self.first_beat_id,
                "second_beat_id": self.second_beat_id,
            }
        result: dict[str, object] = {
            "after_beat_id": self.second_beat_id,
            "before_beat_id": self.first_beat_id,
            "constraint_type": self.constraint_type,
        }
        if self.maximum_gap is not None:
            result["maximum_gap"] = self.maximum_gap.to_mapping()
        return result


def stable_beat_id(story_id: str, partition_id: str, local_ordinal: int) -> str:
    identifier(story_id, "stable Beat story_id")
    identifier(partition_id, "stable Beat partition_id")
    integer(local_ordinal, "stable Beat local_ordinal")
    return canonical_json_hash([story_id, partition_id, local_ordinal])


@dataclass(frozen=True, slots=True)
class RequiredClosure(CanonicalModel):
    closure_id: str
    closure_hash: str

    def __post_init__(self) -> None:
        identifier(self.closure_id, "closure_id")
        sha256(self.closure_hash, "closure_hash")

    def to_mapping(self) -> dict[str, object]:
        return {"closure_hash": self.closure_hash, "closure_id": self.closure_id}


@dataclass(frozen=True, slots=True)
class GenerationPartition(CanonicalModel):
    partition_id: str
    writer_obligation_ids: tuple[str, ...]
    writer_requirement_ids: tuple[str, ...]
    writer_closure_refs: tuple[RequiredClosure, ...]
    shared_read_only_closure_refs: tuple[RequiredClosure, ...]
    context_slice_hash: str
    single_call_token_limit: int

    def __post_init__(self) -> None:
        identifier(self.partition_id, "partition_id")
        object.__setattr__(
            self,
            "writer_obligation_ids",
            canonical_ids(self.writer_obligation_ids, "writer_obligation_ids", nonempty=True),
        )
        object.__setattr__(
            self,
            "writer_requirement_ids",
            canonical_ids(self.writer_requirement_ids, "writer_requirement_ids", nonempty=True),
        )
        object.__setattr__(
            self,
            "writer_closure_refs",
            cast(
                tuple[RequiredClosure, ...],
                canonical_values(
                    self.writer_closure_refs,
                    RequiredClosure,
                    "writer_closure_refs",
                    nonempty=True,
                ),
            ),
        )
        object.__setattr__(
            self,
            "shared_read_only_closure_refs",
            cast(
                tuple[RequiredClosure, ...],
                canonical_values(
                    self.shared_read_only_closure_refs,
                    RequiredClosure,
                    "shared_read_only_closure_refs",
                ),
            ),
        )
        sha256(self.context_slice_hash, "context_slice_hash")
        integer(self.single_call_token_limit, "single_call_token_limit", minimum=1)

    def to_mapping(self) -> dict[str, object]:
        return {
            "context_slice_hash": self.context_slice_hash,
            "partition_id": self.partition_id,
            "shared_read_only_closure_refs": [
                item.to_mapping() for item in self.shared_read_only_closure_refs
            ],
            "single_call_token_limit": self.single_call_token_limit,
            "writer_closure_refs": [item.to_mapping() for item in self.writer_closure_refs],
            "writer_obligation_ids": list(self.writer_obligation_ids),
            "writer_requirement_ids": list(self.writer_requirement_ids),
        }


@dataclass(frozen=True, slots=True)
class GenerationPartitionPlan(CanonicalModel):
    partition_plan_id: str
    story_id: str
    partition_ids: tuple[str, ...]
    partitions: tuple[GenerationPartition, ...]
    aggregate_token_limit: int
    merge_policy_ref: ArtifactRef

    def __post_init__(self) -> None:
        identifier(self.partition_plan_id, "partition_plan_id")
        identifier(self.story_id, "partition plan story_id")
        partition_ids = tuple(identifier(item, "partition_ids") for item in self.partition_ids)
        if not partition_ids or len(partition_ids) != len(set(partition_ids)):
            raise ProductionModelError("partition_ids must be non-empty and unique")
        partitions = tuple(self.partitions)  # frozen plan order is authoritative.
        if any(type(item) is not GenerationPartition for item in partitions):  # noqa: E721
            raise ProductionModelError("partitions contain an invalid value")
        if tuple(item.partition_id for item in partitions) != partition_ids:
            raise ProductionModelError("partitions do not exactly follow frozen partition_ids")
        aggregate = integer(self.aggregate_token_limit, "aggregate_token_limit", minimum=1)
        if sum(item.single_call_token_limit for item in partitions) > aggregate:
            raise ProductionModelError("partition calls exceed aggregate token limit")
        if type(self.merge_policy_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("merge_policy_ref must be an ArtifactRef")
        object.__setattr__(self, "partition_ids", partition_ids)
        object.__setattr__(self, "partitions", partitions)

    def to_mapping(self) -> dict[str, object]:
        return {
            "aggregate_token_limit": self.aggregate_token_limit,
            "merge_policy_ref": self.merge_policy_ref.to_mapping(),
            "partition_ids": list(self.partition_ids),
            "partition_plan_id": self.partition_plan_id,
            "partitions": [item.to_mapping() for item in self.partitions],
            "story_id": self.story_id,
        }


@dataclass(frozen=True, slots=True)
class MergePolicy(CanonicalModel):
    merge_policy_id: str = "blueprint-merge-2.1.3"
    local_key_kind: str = "zero_based_fragment_ordinal"
    scc_topological_tie_break: str = "jcs_bytes_ascending"
    partition_order_source: str = "generation_partition_plan.partition_ids"
    beat_id_algorithm: str = "sha256-jcs-story-partition-ordinal"
    merge_sort_key: tuple[str, ...] = (
        "scc_topological_rank",
        "partition_position",
        "local_ordinal",
    )
    ordering_constraint_mode: str = "preserve_only"
    conflict_mode: str = "reject"

    def __post_init__(self) -> None:
        if self.to_mapping() != {
            "beat_id_algorithm": "sha256-jcs-story-partition-ordinal",
            "conflict_mode": "reject",
            "local_key_kind": "zero_based_fragment_ordinal",
            "merge_policy_id": "blueprint-merge-2.1.3",
            "merge_sort_key": [
                "scc_topological_rank",
                "partition_position",
                "local_ordinal",
            ],
            "ordering_constraint_mode": "preserve_only",
            "partition_order_source": "generation_partition_plan.partition_ids",
            "scc_topological_tie_break": "jcs_bytes_ascending",
        }:
            raise ProductionModelError("MergePolicy is not the frozen 2.1.3 policy")

    def to_mapping(self) -> dict[str, object]:
        return {
            "beat_id_algorithm": self.beat_id_algorithm,
            "conflict_mode": self.conflict_mode,
            "local_key_kind": self.local_key_kind,
            "merge_policy_id": self.merge_policy_id,
            "merge_sort_key": list(self.merge_sort_key),
            "ordering_constraint_mode": self.ordering_constraint_mode,
            "partition_order_source": self.partition_order_source,
            "scc_topological_tie_break": self.scc_topological_tie_break,
        }


@dataclass(frozen=True, slots=True)
class FragmentBeat(CanonicalModel):
    local_ordinal: int
    stable_beat_id: str
    normalized_beat: BlueprintBeat

    def __post_init__(self) -> None:
        integer(self.local_ordinal, "fragment local_ordinal")
        sha256(self.stable_beat_id, "fragment stable_beat_id")
        if type(self.normalized_beat) is not BlueprintBeat:  # noqa: E721
            raise ProductionModelError("fragment normalized beat is invalid")
        if self.normalized_beat.stable_beat_id != self.stable_beat_id:
            raise ProductionModelError("fragment Beat stable ID does not match normalized Beat")

    def to_mapping(self) -> dict[str, object]:
        return {
            "local_ordinal": self.local_ordinal,
            "normalized_beat_payload": self.normalized_beat.to_mapping(),
            "stable_beat_id": self.stable_beat_id,
        }


@dataclass(frozen=True, slots=True, init=False)
class BlueprintFragment(EvaluatorOwnedModel):
    blueprint_fragment_id: str
    story_id: str
    partition_id: str
    generation_invocation_ref: ArtifactRef
    parse_normalization_record_ref: ArtifactRef
    beats: tuple[FragmentBeat, ...]
    ordering_constraints: tuple[OrderingConstraint, ...]
    fragment_hash: str

    @classmethod
    def from_normalized_beats(
        cls,
        *,
        blueprint_fragment_id: str,
        story_id: str,
        partition_id: str,
        generation_invocation_ref: ArtifactRef,
        parse_normalization_record_ref: ArtifactRef,
        beats: Sequence[BlueprintBeat],
        ordering_constraints: Sequence[OrderingConstraint],
    ) -> BlueprintFragment:
        identifier(blueprint_fragment_id, "blueprint_fragment_id")
        identifier(story_id, "fragment story_id")
        identifier(partition_id, "fragment partition_id")
        if type(generation_invocation_ref) is not ArtifactRef or type(parse_normalization_record_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("fragment audit refs must be ArtifactRefs")
        beat_values = tuple(beats)  # parser order is authoritative.
        if not beat_values or any(type(item) is not BlueprintBeat for item in beat_values):  # noqa: E721
            raise ProductionModelError("fragment beats must be non-empty normalized Beats")
        fragment_beats: list[FragmentBeat] = []
        for ordinal, beat in enumerate(beat_values):
            expected = stable_beat_id(story_id, partition_id, ordinal)
            if beat.stable_beat_id != expected:
                raise ProductionModelError("fragment stable Beat ID was not derived from ordinal")
            fragment_beats.append(FragmentBeat(ordinal, expected, beat))
        constraints = tuple(ordering_constraints)  # explicit order semantics are preserved.
        if any(type(item) is not OrderingConstraint for item in constraints):  # noqa: E721
            raise ProductionModelError("fragment ordering constraints are invalid")
        if len({jcs_key(item) for item in constraints}) != len(constraints):
            raise ProductionModelError("fragment ordering constraints contain duplicates")
        beat_ids = {item.stable_beat_id for item in fragment_beats}
        if any(
            item.first_beat_id not in beat_ids or item.second_beat_id not in beat_ids
            for item in constraints
        ):
            raise ProductionModelError("fragment ordering constraint points outside fragment")
        payload = {
            "beats": [item.to_mapping() for item in fragment_beats],
            "generation_invocation_ref": generation_invocation_ref.to_mapping(),
            "ordering_constraints": [item.to_mapping() for item in constraints],
            "parse_normalization_record_ref": parse_normalization_record_ref.to_mapping(),
            "partition_id": partition_id,
        }
        instance = object.__new__(cls)
        object.__setattr__(instance, "blueprint_fragment_id", blueprint_fragment_id)
        object.__setattr__(instance, "story_id", story_id)
        object.__setattr__(instance, "partition_id", partition_id)
        object.__setattr__(instance, "generation_invocation_ref", generation_invocation_ref)
        object.__setattr__(
            instance, "parse_normalization_record_ref", parse_normalization_record_ref
        )
        object.__setattr__(instance, "beats", tuple(fragment_beats))
        object.__setattr__(instance, "ordering_constraints", constraints)
        object.__setattr__(instance, "fragment_hash", canonical_json_hash(payload))
        return instance

    @property
    def domain_ref(self) -> DomainRef:
        return DomainRef(
            ArtifactRef(self.blueprint_fragment_id, self.fragment_hash),
            "blueprint_fragment",
            self.blueprint_fragment_id,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "beats": [item.to_mapping() for item in self.beats],
            "blueprint_fragment_id": self.blueprint_fragment_id,
            "fragment_hash": self.fragment_hash,
            "generation_invocation_ref": self.generation_invocation_ref.to_mapping(),
            "ordering_constraints": [item.to_mapping() for item in self.ordering_constraints],
            "parse_normalization_record_ref": self.parse_normalization_record_ref.to_mapping(),
            "partition_id": self.partition_id,
            "story_id": self.story_id,
        }


@dataclass(frozen=True, slots=True, init=False)
class EditorialBlueprint(EvaluatorOwnedModel):
    blueprint_id: str
    story_id: str
    proposal_ref: DomainRef
    beats: tuple[BlueprintBeat, ...]
    ordering_constraints: tuple[OrderingConstraint, ...]
    fragments: tuple[BlueprintFragment, ...]
    merge_policy_ref: ArtifactRef
    merge_result_hash: str
    generation_partition_plan_ref: ArtifactRef
    story_duration_seconds: DurationRangeSeconds
    pacing: str
    continuity_priority: str
    teaser_strategy: str
    teaser_duration_seconds: DurationRangeSeconds

    @property
    def required_obligation_refs(self) -> tuple[DomainRef, ...]:
        values = {jcs_key(ref): ref for beat in self.beats for ref in beat.required_obligation_refs}
        return tuple(values[key] for key in sorted(values))

    @property
    def required_fact_refs(self) -> tuple[DomainRef, ...]:
        values = {jcs_key(ref): ref for beat in self.beats for ref in beat.required_fact_refs}
        return tuple(values[key] for key in sorted(values))

    @property
    def evidence_requirements(self) -> tuple[EvidenceRequirement, ...]:
        return tuple(
            requirement for beat in self.beats for requirement in beat.evidence_requirements
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "beats": [item.to_mapping() for item in self.beats],
            "blueprint_id": self.blueprint_id,
            "editing_intent": {
                "continuity_priority": self.continuity_priority,
                "pacing": self.pacing,
            },
            "fragments": [item.to_mapping() for item in self.fragments],
            "generation_partition_plan_ref": self.generation_partition_plan_ref.to_mapping(),
            "merge_policy_ref": self.merge_policy_ref.to_mapping(),
            "merge_result_hash": self.merge_result_hash,
            "ordering_constraints": [item.to_mapping() for item in self.ordering_constraints],
            "proposal_ref": self.proposal_ref.to_mapping(),
            "story_duration_seconds": self.story_duration_seconds.to_mapping(),
            "story_id": self.story_id,
            "teaser_intent": {
                "duration_seconds": self.teaser_duration_seconds.to_mapping(),
                "strategy": self.teaser_strategy,
            },
        }


class BlueprintMerger:
    """Validate hard-order acyclicity and merge fragments independently of arrival order."""

    @staticmethod
    def merge(
        *,
        blueprint_id: str,
        story_id: str,
        proposal_ref: DomainRef,
        partition_plan_ref: ArtifactRef,
        partition_plan: GenerationPartitionPlan,
        merge_policy_ref: ArtifactRef,
        merge_policy: MergePolicy,
        fragments: Sequence[BlueprintFragment],
        story_duration_seconds: DurationRangeSeconds,
        pacing: str,
        continuity_priority: str,
        teaser_strategy: str,
        teaser_duration_seconds: DurationRangeSeconds,
    ) -> EditorialBlueprint:
        identifier(blueprint_id, "blueprint_id")
        identifier(story_id, "blueprint story_id")
        if type(proposal_ref) is not DomainRef or proposal_ref.object_type != "proposal":  # noqa: E721
            raise ProductionModelError("Blueprint proposal_ref is invalid")
        if partition_plan_ref.content_hash != partition_plan.canonical_hash:
            raise ProductionModelError("partition plan ref does not bind exact plan")
        if merge_policy_ref.content_hash != merge_policy.canonical_hash:
            raise ProductionModelError("merge policy ref does not bind exact policy")
        if partition_plan.story_id != story_id:
            raise ProductionModelError("partition plan belongs to another Story")
        by_partition = {item.partition_id: item for item in fragments}
        if len(by_partition) != len(tuple(fragments)) or set(by_partition) != set(
            partition_plan.partition_ids
        ):
            raise ProductionModelError("fragments do not exactly cover frozen partitions")
        ordered_fragments = tuple(by_partition[item] for item in partition_plan.partition_ids)
        if any(item.story_id != story_id for item in ordered_fragments):
            raise ProductionModelError("fragment belongs to another Story")
        beat_owner: dict[str, tuple[int, int, BlueprintBeat]] = {}
        constraints: list[OrderingConstraint] = []
        for partition_position, fragment in enumerate(ordered_fragments):
            for item in fragment.beats:
                if item.stable_beat_id in beat_owner:
                    raise ProductionModelError("fragments wrote the same stable Beat ID")
                beat_owner[item.stable_beat_id] = (
                    partition_position,
                    item.local_ordinal,
                    item.normalized_beat,
                )
            constraints.extend(fragment.ordering_constraints)
        if len({jcs_key(item) for item in constraints}) != len(constraints):
            raise ProductionModelError("merged ordering constraints contain duplicates")
        beat_ids = set(beat_owner)
        if any(
            item.first_beat_id not in beat_ids or item.second_beat_id not in beat_ids
            for item in constraints
        ):
            raise ProductionModelError("ordering constraint points outside merged Beats")
        outgoing = {beat_id: set[str]() for beat_id in beat_ids}
        indegree = {beat_id: 0 for beat_id in beat_ids}
        for constraint in constraints:
            if constraint.second_beat_id not in outgoing[constraint.first_beat_id]:
                outgoing[constraint.first_beat_id].add(constraint.second_beat_id)
                indegree[constraint.second_beat_id] += 1
        ranks: dict[str, int] = {}
        ready = sorted((item for item, degree in indegree.items() if degree == 0), key=jcs_key)
        rank = 0
        while ready:
            current = ready.pop(0)
            ranks[current] = rank
            rank += 1
            for target in sorted(outgoing[current], key=jcs_key):
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
                    ready.sort(key=jcs_key)
        if len(ranks) != len(beat_ids):
            raise ProductionModelError("hard ordering constraints contain a cycle")
        ordered_beat_ids = tuple(
            sorted(
                beat_ids,
                key=lambda item: (
                    ranks[item],
                    beat_owner[item][0],
                    beat_owner[item][1],
                ),
            )
        )
        ordered_beats = tuple(beat_owner[item][2] for item in ordered_beat_ids)
        merge_result_hash = canonical_json_hash(
            {
                "fragment_refs": [item.domain_ref.to_mapping() for item in ordered_fragments],
                "merge_policy_ref": merge_policy_ref.to_mapping(),
                "ordered_stable_beat_ids": list(ordered_beat_ids),
                "preserved_ordering_constraints": [item.to_mapping() for item in constraints],
            }
        )
        if pacing not in {"slow", "balanced", "fast"}:
            raise ProductionModelError("pacing is unknown")
        if continuity_priority not in {"low", "medium", "high"}:
            raise ProductionModelError("continuity priority is unknown")
        if teaser_strategy not in {"cold_open", "delayed_reprise", "chronological"}:
            raise ProductionModelError("teaser strategy is unknown")
        instance = object.__new__(EditorialBlueprint)
        object.__setattr__(instance, "blueprint_id", blueprint_id)
        object.__setattr__(instance, "story_id", story_id)
        object.__setattr__(instance, "proposal_ref", proposal_ref)
        object.__setattr__(instance, "beats", ordered_beats)
        object.__setattr__(instance, "ordering_constraints", tuple(constraints))
        object.__setattr__(instance, "fragments", ordered_fragments)
        object.__setattr__(instance, "merge_policy_ref", merge_policy_ref)
        object.__setattr__(instance, "merge_result_hash", merge_result_hash)
        object.__setattr__(instance, "generation_partition_plan_ref", partition_plan_ref)
        object.__setattr__(instance, "story_duration_seconds", story_duration_seconds)
        object.__setattr__(instance, "pacing", pacing)
        object.__setattr__(instance, "continuity_priority", continuity_priority)
        object.__setattr__(instance, "teaser_strategy", teaser_strategy)
        object.__setattr__(instance, "teaser_duration_seconds", teaser_duration_seconds)
        return instance


@dataclass(frozen=True, slots=True)
class EvidenceClosureMember(CanonicalModel):
    kind: str
    source_artifact_ref: ArtifactRef
    object_id: str
    object_content_hash: str

    def __post_init__(self) -> None:
        if self.kind not in {
            "narrative_node",
            "fact",
            "event",
            "vlm_observation",
            "character_state",
            "candidate_metadata",
        }:
            raise ProductionModelError("evidence closure member kind is unknown")
        if type(self.source_artifact_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("closure source_artifact_ref is invalid")
        identifier(self.object_id, "closure object_id")
        sha256(self.object_content_hash, "object_content_hash")

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "object_content_hash": self.object_content_hash,
            "object_id": self.object_id,
            "source_artifact_ref": self.source_artifact_ref.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class EvidenceClosure(CanonicalModel):
    closure_id: str
    requirement_id: str
    members: tuple[EvidenceClosureMember, ...]
    dependency_refs: tuple[DomainRef, ...]

    def __post_init__(self) -> None:
        identifier(self.closure_id, "closure_id")
        identifier(self.requirement_id, "closure requirement_id")
        object.__setattr__(
            self,
            "members",
            cast(
                tuple[EvidenceClosureMember, ...],
                canonical_values(
                    self.members, EvidenceClosureMember, "closure members", nonempty=True
                ),
            ),
        )
        object.__setattr__(
            self,
            "dependency_refs",
            canonical_domain_refs(self.dependency_refs, "closure dependency_refs"),
        )

    @property
    def closure_hash(self) -> str:
        return canonical_json_hash(
            {
                "dependency_refs": [item.to_mapping() for item in self.dependency_refs],
                "members": [item.to_mapping() for item in self.members],
                "requirement_id": self.requirement_id,
            }
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "closure_hash": self.closure_hash,
            "closure_id": self.closure_id,
            "dependency_refs": [item.to_mapping() for item in self.dependency_refs],
            "members": [item.to_mapping() for item in self.members],
            "requirement_id": self.requirement_id,
        }


@dataclass(frozen=True, slots=True)
class EvidenceClosureSet(CanonicalModel):
    evidence_closure_set_id: str
    story_id: str
    closures: tuple[EvidenceClosure, ...]

    def __post_init__(self) -> None:
        identifier(self.evidence_closure_set_id, "evidence_closure_set_id")
        identifier(self.story_id, "closure set story_id")
        closures = cast(
            tuple[EvidenceClosure, ...],
            canonical_values(self.closures, EvidenceClosure, "closures", nonempty=True),
        )
        if len({item.closure_id for item in closures}) != len(closures):
            raise ProductionModelError("closures must have unique IDs")
        if len({item.requirement_id for item in closures}) != len(closures):
            raise ProductionModelError("each requirement must have exactly one closure")
        object.__setattr__(self, "closures", closures)

    @property
    def closure_set_hash(self) -> str:
        return canonical_json_hash(
            [
                {"closure_hash": item.closure_hash, "closure_id": item.closure_id}
                for item in sorted(self.closures, key=lambda value: jcs_key(value.closure_id))
            ]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "closure_set_hash": self.closure_set_hash,
            "closures": [item.to_mapping() for item in self.closures],
            "evidence_closure_set_id": self.evidence_closure_set_id,
            "story_id": self.story_id,
        }


@dataclass(frozen=True, slots=True)
class ContextBudget(CanonicalModel):
    unit: str
    limit: int
    used: int
    tokenizer_id: str
    tokenizer_version: str

    def __post_init__(self) -> None:
        if self.unit != "tokens":
            raise ProductionModelError("context budget unit must be tokens")
        limit = integer(self.limit, "budget.limit", minimum=1)
        used = integer(self.used, "budget.used")
        if used > limit:
            raise ProductionModelError("context budget used must not exceed limit")
        identifier(self.tokenizer_id, "tokenizer_id")
        safe_token(self.tokenizer_version, "tokenizer_version")

    def to_mapping(self) -> dict[str, object]:
        return {
            "limit": self.limit,
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_version": self.tokenizer_version,
            "unit": self.unit,
            "used": self.used,
        }


@dataclass(frozen=True, slots=True)
class ContextOmission(CanonicalModel):
    ref: DomainRef
    reason: str
    semantic_impact: str

    def __post_init__(self) -> None:
        if type(self.ref) is not DomainRef:  # noqa: E721
            raise ProductionModelError("omission ref is invalid")
        if self.reason != "optional_priority_cut" or self.semantic_impact != "none":
            raise ProductionModelError("Context may omit only optional no-impact data")

    def to_mapping(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "ref": self.ref.to_mapping(),
            "semantic_impact": self.semantic_impact,
        }


@dataclass(frozen=True, slots=True, init=False)
class ContextManifest(EvaluatorOwnedModel):
    context_manifest_id: str
    story_id: str
    input_refs: tuple[ArtifactRef, ...]
    evidence_closure_set_ref: ArtifactRef
    required_closures: tuple[RequiredClosure, ...]
    optional_context_refs: tuple[DomainRef, ...]
    omissions: tuple[ContextOmission, ...]
    budget: ContextBudget
    builder_version: str

    @classmethod
    def for_closure_set(
        cls,
        *,
        context_manifest_id: str,
        story_id: str,
        input_refs: Sequence[ArtifactRef],
        evidence_closure_set_ref: ArtifactRef,
        closure_set: EvidenceClosureSet,
        optional_context_refs: Sequence[DomainRef],
        omissions: Sequence[ContextOmission],
        budget: ContextBudget,
        builder_version: str,
    ) -> ContextManifest:
        identifier(context_manifest_id, "context_manifest_id")
        identifier(story_id, "context story_id")
        if closure_set.story_id != story_id or evidence_closure_set_ref.content_hash != closure_set.canonical_hash:
            raise ProductionModelError("Context does not bind exact Story EvidenceClosureSet")
        inputs = canonical_artifact_refs(input_refs, "context input_refs", nonempty=True)
        required = tuple(
            RequiredClosure(item.closure_id, item.closure_hash) for item in closure_set.closures
        )
        # Projection follows the already-canonical ClosureSet order and cannot be caller-trimmed.
        optional = canonical_domain_refs(optional_context_refs, "optional_context_refs")
        omission_values = cast(
            tuple[ContextOmission, ...],
            canonical_values(omissions, ContextOmission, "omissions"),
        )
        if type(budget) is not ContextBudget:  # noqa: E721
            raise ProductionModelError("Context budget is invalid")
        safe_token(builder_version, "builder_version")
        instance = object.__new__(cls)
        object.__setattr__(instance, "context_manifest_id", context_manifest_id)
        object.__setattr__(instance, "story_id", story_id)
        object.__setattr__(instance, "input_refs", inputs)
        object.__setattr__(instance, "evidence_closure_set_ref", evidence_closure_set_ref)
        object.__setattr__(instance, "required_closures", required)
        object.__setattr__(instance, "optional_context_refs", optional)
        object.__setattr__(instance, "omissions", omission_values)
        object.__setattr__(instance, "budget", budget)
        object.__setattr__(instance, "builder_version", builder_version)
        return instance

    def to_mapping(self) -> dict[str, object]:
        return {
            "budget": self.budget.to_mapping(),
            "builder_version": self.builder_version,
            "context_manifest_id": self.context_manifest_id,
            "evidence_closure_set_ref": self.evidence_closure_set_ref.to_mapping(),
            "input_refs": [item.to_mapping() for item in self.input_refs],
            "omissions": [item.to_mapping() for item in self.omissions],
            "optional_context_refs": [item.to_mapping() for item in self.optional_context_refs],
            "required_closures": [item.to_mapping() for item in self.required_closures],
            "story_id": self.story_id,
        }


@dataclass(frozen=True, slots=True, init=False)
class SemanticFeasibilityAdmission(EvaluatorOwnedModel):
    admission_id: str
    pending_set_hash: str
    story_id: str
    blueprint_ref: ArtifactRef
    evidence_closure_set_ref: ArtifactRef
    context_manifest_ref: ArtifactRef
    generation_partition_plan_ref: ArtifactRef
    proposal_ref: DomainRef
    portfolio_ref: ArtifactRef
    portfolio_admission_ref: ArtifactRef
    source_usage_ledger_ref: ArtifactRef
    candidate_catalog_ref: ArtifactRef
    required_obligations_hash: str
    required_requirements_hash: str
    next_action: str
    rule_results: tuple[RuleResult, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "admission_id": self.admission_id,
            "blueprint_ref": self.blueprint_ref.to_mapping(),
            "context_manifest_ref": self.context_manifest_ref.to_mapping(),
            "evidence_closure_set_ref": self.evidence_closure_set_ref.to_mapping(),
            "generation_partition_plan_ref": self.generation_partition_plan_ref.to_mapping(),
            "kind": "semantic_feasibility",
            "next_action": self.next_action,
            "pending_set_hash": self.pending_set_hash,
            "candidate_catalog_ref": self.candidate_catalog_ref.to_mapping(),
            "portfolio_ref": self.portfolio_ref.to_mapping(),
            "portfolio_admission_ref": self.portfolio_admission_ref.to_mapping(),
            "proposal_ref": self.proposal_ref.to_mapping(),
            "required_obligations_hash": self.required_obligations_hash,
            "required_requirements_hash": self.required_requirements_hash,
            "rule_results": [item.to_mapping() for item in self.rule_results],
            "source_usage_ledger_ref": self.source_usage_ledger_ref.to_mapping(),
            "story_id": self.story_id,
        }


_SEMANTIC_RULES: Final = {
    "SS-IN-001",
    "SS-OBL-001",
    "SS-EV-002",
    "SS-CAND-CAP-001",
    "SS-PHYS-DEFER-001",
    "SS-CTX-001",
    "SS-PART-001",
    "SS-MERGE-001",
    "SS-HASH-001",
    "SS-DUR-001",
}
_STAGE3_MEMBER_TYPES: Final = {
    "editorial_blueprint",
    "evidence_closure_set",
    "context_manifest",
    "generation_partition_plan",
}


class SemanticFeasibilityEvaluator:
    """Join the exact Story/Proposal/Candidate/Closure/Context chain before continue."""

    @staticmethod
    def evaluate(
        *,
        admission_id: str,
        pending_set: PendingBusinessSet,
        blueprint: EditorialBlueprint,
        closure_set: EvidenceClosureSet,
        context_manifest: ContextManifest,
        partition_plan: GenerationPartitionPlan,
        proposal_set: ProposalSet,
        portfolio_ref: ArtifactRef,
        portfolio: Portfolio,
        portfolio_admission_ref: ArtifactRef,
        portfolio_admission: PortfolioAdmission,
        source_usage_ledger_ref: ArtifactRef,
        source_usage_ledger: SourceUsageLedger,
        candidate_catalog_ref: ArtifactRef,
        candidate_catalog: CandidateCatalog,
    ) -> SemanticFeasibilityAdmission:
        identifier(admission_id, "admission_id")
        if type(pending_set) is not PendingBusinessSet or pending_set.admission_kind != "semantic_feasibility":  # noqa: E721
            raise ProductionModelError("semantic evaluator requires semantic pending set")
        pending_set.require_exact_types(_STAGE3_MEMBER_TYPES)
        blueprint_ref = pending_set.require_member("editorial_blueprint", blueprint)
        closure_ref = pending_set.require_member("evidence_closure_set", closure_set)
        context_ref = pending_set.require_member("context_manifest", context_manifest)
        plan_ref = pending_set.require_member("generation_partition_plan", partition_plan)
        if (
            blueprint.story_id != closure_set.story_id
            or blueprint.story_id != context_manifest.story_id
            or blueprint.story_id != partition_plan.story_id
        ):
            raise ProductionModelError("Stage 3 business members belong to different Stories")
        if blueprint.generation_partition_plan_ref != plan_ref:
            raise ProductionModelError("Blueprint does not bind exact partition plan")
        if context_manifest.evidence_closure_set_ref != closure_ref:
            raise ProductionModelError("Context does not bind exact closure set")
        expected_projection = tuple(
            RequiredClosure(item.closure_id, item.closure_hash) for item in closure_set.closures
        )
        if context_manifest.required_closures != expected_projection:
            raise ProductionModelError("Context required_closures is not full closure projection")
        if any(
            type(value) is not ArtifactRef
            for value in (
                portfolio_ref,
                portfolio_admission_ref,
                source_usage_ledger_ref,
                candidate_catalog_ref,
            )
        ):
            raise ProductionModelError("semantic evaluator input refs must be ArtifactRefs")
        if portfolio_ref.content_hash != portfolio.canonical_hash:
            raise ProductionModelError("portfolio_ref does not bind exact Portfolio")
        if portfolio.proposal_set_ref.content_hash != proposal_set.canonical_hash:
            raise ProductionModelError("Portfolio does not bind exact ProposalSet payload")
        if portfolio_admission_ref.content_hash != portfolio_admission.canonical_hash:
            raise ProductionModelError("portfolio_admission_ref does not bind exact Admission")
        if source_usage_ledger_ref.content_hash != source_usage_ledger.canonical_hash:
            raise ProductionModelError("source_usage_ledger_ref does not bind exact ledger")
        if candidate_catalog_ref.content_hash != candidate_catalog.canonical_hash:
            raise ProductionModelError("candidate_catalog_ref does not bind exact catalog")
        if portfolio_admission.portfolio_ref != portfolio_ref or portfolio_admission.next_action != "continue":
            raise ProductionModelError("Story is not backed by exact continuing PortfolioAdmission")
        if portfolio_admission.source_usage_ledger_ref != source_usage_ledger_ref:
            raise ProductionModelError("PortfolioAdmission does not bind exact SourceUsageLedger")
        if portfolio_admission.target_story_ids != portfolio.target_story_ids:
            raise ProductionModelError("PortfolioAdmission target freeze mismatch")
        if blueprint.story_id not in portfolio.target_story_ids:
            raise ProductionModelError("Blueprint Story is outside frozen target set")
        if tuple(item.story_id for item in source_usage_ledger.rows) != portfolio.target_story_ids:
            raise ProductionModelError("SourceUsageLedger target join is incomplete")
        record = next(
            (item for item in portfolio.selection_records if item.story_id == blueprint.story_id), None
        )
        if record is None or record.proposal_index >= len(proposal_set.proposals):
            raise ProductionModelError("Blueprint Story has no selected Proposal")
        proposal = proposal_set.proposals[record.proposal_index]
        if (
            proposal.proposal_id != record.proposal_id
            or blueprint.proposal_ref.object_id != proposal.proposal_id
            or blueprint.proposal_ref.artifact_ref != portfolio.proposal_set_ref
        ):
            raise ProductionModelError("Blueprint Proposal ref does not join selected Proposal")
        if set(blueprint.required_obligation_refs) != set(proposal.required_obligation_refs):
            raise ProductionModelError("Blueprint obligation join is incomplete")
        if set(blueprint.required_fact_refs) != set(proposal.required_fact_refs):
            raise ProductionModelError("Blueprint fact join is incomplete")
        requirements = blueprint.evidence_requirements
        material_by_id: dict[str, MaterialRequirement] = {
            item.requirement_id: item for item in proposal.material_requirements
        }
        source_ids = tuple(item.source_material_requirement_id for item in requirements)
        if len(source_ids) != len(set(source_ids)) or set(source_ids) != set(material_by_id):
            raise ProductionModelError("Proposal material requirements and Blueprint evidence requirements are not bijective")
        closure_requirement_ids = {item.requirement_id for item in closure_set.closures}
        if closure_requirement_ids != {item.requirement_id for item in requirements}:
            raise ProductionModelError("EvidenceClosureSet does not exactly cover evidence requirements")
        candidates = {item.candidate_id: item for item in candidate_catalog.candidates}
        for beat in blueprint.beats:
            for requirement in beat.evidence_requirements:
                material = material_by_id[requirement.source_material_requirement_id]
                if (
                    requirement.physical_requirements != material.physical_requirements
                    or requirement.physical_requirements_hash
                    != material.physical_requirements_hash
                ):
                    raise ProductionModelError("Stage 3 changed deferred physical requirements")
                alternative_statuses: list[bool] = []
                for alternative in requirement.alternative_sets:
                    resolved = tuple(candidates.get(ref.object_id) for ref in alternative.candidate_refs)
                    if any(ref.artifact_ref != candidate_catalog_ref for ref in alternative.candidate_refs):
                        raise ProductionModelError("alternative Candidate has the wrong catalog owner")
                    if any(item is None for item in resolved):
                        raise ProductionModelError("alternative points outside CandidateCatalog")
                    typed = cast(tuple[Candidate, ...], resolved)
                    covered = {
                        jcs_key(event)
                        for candidate in typed
                        for event in candidate.event_refs
                    }
                    alternative_statuses.append(
                        {jcs_key(event) for event in alternative.event_refs} <= covered
                        and all(
                            beat.narrative_function
                            in candidate.supported_narrative_functions
                            for candidate in typed
                        )
                        and any(
                            candidate.duration_proof.supports_seconds(
                                beat.duration_seconds.minimum
                            )
                            for candidate in typed
                        )
                    )
                satisfied = (
                    any(alternative_statuses)
                    if requirement.satisfaction == "one_of"
                    else all(alternative_statuses)
                )
                if not satisfied:
                    raise ProductionModelError(
                        "evidence requirement has no complete capability/duration witness"
                    )
                alternative_candidate_ids = {
                    ref.object_id
                    for alternative in requirement.alternative_sets
                    for ref in alternative.candidate_refs
                }
                for required in requirement.required_candidates:
                    candidate = candidates.get(required.candidate_ref.object_id)
                    if (
                        candidate is None
                        or candidate.candidate_id not in alternative_candidate_ids
                        or candidate.authorization_ref != required.authorization_ref
                    ):
                        raise ProductionModelError(
                            "required Candidate is not an authorized alternative"
                        )
        for closure in closure_set.closures:
            for member in closure.members:
                if member.kind == "candidate_metadata" and (
                    member.source_artifact_ref != candidate_catalog_ref
                    or member.object_id not in candidates
                    or member.object_content_hash != candidates[member.object_id].canonical_hash
                ):
                    raise ProductionModelError(
                        "Candidate closure member does not bind exact CandidateCatalog object"
                    )
        beat_minimum = sum(item.duration_seconds.minimum for item in blueprint.beats)
        beat_maximum = sum(item.duration_seconds.maximum for item in blueprint.beats)
        if (
            beat_minimum > blueprint.story_duration_seconds.maximum
            or beat_maximum < blueprint.story_duration_seconds.minimum
        ):
            raise ProductionModelError("Story duration cannot be satisfied by Beat ranges")
        writer_obligations = tuple(
            value for part in partition_plan.partitions for value in part.writer_obligation_ids
        )
        writer_requirements = tuple(
            value for part in partition_plan.partitions for value in part.writer_requirement_ids
        )
        if (
            len(writer_obligations) != len(set(writer_obligations))
            or set(writer_obligations)
            != {item.object_id for item in proposal.required_obligation_refs}
            or len(writer_requirements) != len(set(writer_requirements))
            or set(writer_requirements) != {item.requirement_id for item in requirements}
        ):
            raise ProductionModelError("partition writers do not exactly cover obligations/requirements")
        rules = computed_rule_results(_SEMANTIC_RULES, pending_set.canonical_hash)
        instance = object.__new__(SemanticFeasibilityAdmission)
        object.__setattr__(instance, "admission_id", admission_id)
        object.__setattr__(instance, "pending_set_hash", pending_set.canonical_hash)
        object.__setattr__(instance, "story_id", blueprint.story_id)
        object.__setattr__(instance, "blueprint_ref", blueprint_ref)
        object.__setattr__(instance, "evidence_closure_set_ref", closure_ref)
        object.__setattr__(instance, "context_manifest_ref", context_ref)
        object.__setattr__(instance, "generation_partition_plan_ref", plan_ref)
        object.__setattr__(instance, "proposal_ref", blueprint.proposal_ref)
        object.__setattr__(instance, "portfolio_ref", portfolio_ref)
        object.__setattr__(instance, "portfolio_admission_ref", portfolio_admission_ref)
        object.__setattr__(instance, "source_usage_ledger_ref", source_usage_ledger_ref)
        object.__setattr__(instance, "candidate_catalog_ref", candidate_catalog_ref)
        object.__setattr__(
            instance,
            "required_obligations_hash",
            canonical_json_hash([item.to_mapping() for item in blueprint.required_obligation_refs]),
        )
        object.__setattr__(
            instance,
            "required_requirements_hash",
            canonical_json_hash(
                [item.to_mapping() for item in sorted(requirements, key=jcs_key)]
            ),
        )
        object.__setattr__(instance, "next_action", "continue")
        object.__setattr__(instance, "rule_results", rules)
        return instance


@dataclass(frozen=True, slots=True)
class SemanticStoryEvaluation(CanonicalModel):
    story_id: str
    admission: SemanticFeasibilityAdmission

    def __post_init__(self) -> None:
        identifier(self.story_id, "semantic batch story_id")
        if type(self.admission) is not SemanticFeasibilityAdmission or self.admission.story_id != self.story_id:  # noqa: E721
            raise ProductionModelError("semantic batch result has a mismatched admission")

    def to_mapping(self) -> dict[str, object]:
        return {"admission": self.admission.to_mapping(), "story_id": self.story_id}


class SemanticBatchEvaluator:
    """Enforce all-or-nothing target membership without dropping a failed Story."""

    @staticmethod
    def require_complete(
        portfolio: Portfolio, results: Sequence[SemanticStoryEvaluation]
    ) -> tuple[SemanticStoryEvaluation, ...]:
        values = tuple(results)  # frozen target order, not set order.
        if tuple(item.story_id for item in values) != portfolio.target_story_ids:
            raise ProductionModelError("semantic batch does not exactly cover frozen targets")
        if any(item.admission.next_action != "continue" for item in values):
            raise ProductionModelError("all_or_nothing batch contains a failed Story")
        return values


__all__ = [
    "BlueprintBeat",
    "BlueprintFragment",
    "BlueprintMerger",
    "CandidateAlternative",
    "ContextBudget",
    "ContextManifest",
    "ContextOmission",
    "EditorialBlueprint",
    "EvidenceClosure",
    "EvidenceClosureMember",
    "EvidenceClosureSet",
    "EvidenceRequirement",
    "FragmentBeat",
    "GenerationPartition",
    "GenerationPartitionPlan",
    "MergePolicy",
    "OrderingConstraint",
    "RequiredCandidate",
    "RequiredClosure",
    "SemanticBatchEvaluator",
    "SemanticFeasibilityAdmission",
    "SemanticFeasibilityEvaluator",
    "SemanticStoryEvaluation",
    "SpanPolicy",
    "TickGap",
    "stable_beat_id",
]
