"""Pure Stage 3 Blueprint projection; it neither admits nor proves feasibility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ..contracts.compiler.canonical import canonical_json_hash
from ..vlm.models import VlmNarrativeFunction
from .editorial_draft import EditorialBlueprintDraft
from .editorial_models import (
    DurationRange,
    EditingIntent,
    EditorialBeatDraft,
    EvidenceAlternative,
    EvidenceRequirementDraft,
    OrderingConstraint,
    SpanPolicy,
    StoryBlueprintDraft,
    TeaserIntent,
    decode_editorial_ordering,
    editorial_array,
    editorial_hash,
    editorial_integer,
    editorial_mapping,
    editorial_text,
    editorial_tuple,
)
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .stage1_result import Stage1Values
from .story_design_models import MaterialRequirement, PhysicalRequirement, SourceConstraints
from .story_design_result import StoryDesignValues

EDITORIAL_BLUEPRINT_STRATEGY_VERSION = "unpartitioned-batch-v1"


class EditorialBlueprintError(ValueError):
    """A closed Stage 3 draft does not resolve against frozen predecessors."""


def _beat_id(story_id: str, ordinal: int) -> str:
    return canonical_json_hash({
        "schema_version": "stage3-editorial-beat-id-v1",
        "story_id": story_id,
        "strategy_version": EDITORIAL_BLUEPRINT_STRATEGY_VERSION,
        "ordinal": ordinal,
    })


def _evidence_requirement_id(
    story_id: str, beat_ordinal: int, requirement_ordinal: int,
    material_requirement_id: str,
) -> str:
    """Bind every copied material requirement to its exact Blueprint position."""
    return canonical_json_hash({
        "schema_version": "stage3-evidence-requirement-id-v1",
        "story_id": story_id,
        "strategy_version": EDITORIAL_BLUEPRINT_STRATEGY_VERSION,
        "beat_ordinal": beat_ordinal,
        "requirement_ordinal": requirement_ordinal,
        "material_requirement_id": material_requirement_id,
    })


@dataclass(frozen=True, slots=True)
class BlueprintEvidenceRequirement:
    evidence_requirement_id: str
    material_requirement_id: str
    obligation_ref: SemanticObjectRef
    required_fact_refs: tuple[SemanticObjectRef, ...]
    minimum_usable_seconds: int
    physical_requirements: tuple[PhysicalRequirement, ...]
    physical_requirements_hash: str
    source_constraints: SourceConstraints
    satisfaction: str
    alternatives: tuple[EvidenceAlternative, ...]

    def __post_init__(self) -> None:
        editorial_hash(self.evidence_requirement_id)
        editorial_text(self.material_requirement_id)
        if (type(self.obligation_ref) is not SemanticObjectRef  # noqa: E721
                or self.obligation_ref.member_ref.artifact_type != "narrative_graph"
                or self.obligation_ref.object_type != "obligation"):
            raise EditorialBlueprintError("Blueprint requirement obligation must be exact")
        editorial_tuple(self.required_fact_refs, SemanticObjectRef)
        for ref in self.required_fact_refs:
            if (ref.member_ref.artifact_type != "narrative_graph" or ref.object_type != "fact"
                    or ref.member_ref != self.obligation_ref.member_ref):
                raise EditorialBlueprintError("Blueprint requirement Fact owner/type is invalid")
        if len(set(self.required_fact_refs)) != len(self.required_fact_refs):
            raise EditorialBlueprintError("Blueprint requirement repeats a required Fact")
        try:
            editorial_integer(self.minimum_usable_seconds, minimum=1)
            EvidenceRequirementDraft(self.material_requirement_id, self.satisfaction, self.alternatives)
            MaterialRequirement(
                self.material_requirement_id, self.obligation_ref, self.minimum_usable_seconds,
                self.physical_requirements, self.source_constraints,
            )
        except ValueError as error:
            raise EditorialBlueprintError("Blueprint requirement draft fields are invalid") from error
        editorial_tuple(self.physical_requirements, PhysicalRequirement)
        editorial_hash(self.physical_requirements_hash)
        if self.physical_requirements_hash != canonical_json_hash([
            item.to_mapping() for item in self.physical_requirements
        ]):
            raise EditorialBlueprintError("Blueprint physical requirement hash differs from copied requirements")
        if type(self.source_constraints) is not SourceConstraints:  # noqa: E721
            raise EditorialBlueprintError("Blueprint source constraints must be exact")
        editorial_tuple(self.alternatives, EvidenceAlternative, nonempty=True)

    def to_mapping(self) -> dict[str, object]:
        return {
            "evidence_requirement_id": self.evidence_requirement_id,
            "material_requirement_id": self.material_requirement_id,
            "obligation_ref": self.obligation_ref.to_mapping(),
            "required_fact_refs": [item.to_mapping() for item in self.required_fact_refs],
            "minimum_usable_seconds": self.minimum_usable_seconds,
            "physical_requirements": [item.to_mapping() for item in self.physical_requirements],
            "physical_requirements_hash": self.physical_requirements_hash,
            "source_constraints": self.source_constraints.to_mapping(),
            "satisfaction": self.satisfaction,
            "alternatives": [item.to_mapping() for item in self.alternatives],
        }

    @classmethod
    def from_mapping(cls, value: object) -> BlueprintEvidenceRequirement:
        item = editorial_mapping(value, ("evidence_requirement_id", "material_requirement_id", "obligation_ref",
            "required_fact_refs", "minimum_usable_seconds", "physical_requirements", "physical_requirements_hash",
            "source_constraints", "satisfaction", "alternatives"))
        return cls(editorial_hash(item["evidence_requirement_id"]), editorial_text(item["material_requirement_id"]),
            SemanticObjectRef.from_mapping(item["obligation_ref"]), editorial_array(item["required_fact_refs"], SemanticObjectRef.from_mapping),
            cast(int, item["minimum_usable_seconds"]), editorial_array(item["physical_requirements"], PhysicalRequirement.from_mapping),
            editorial_hash(item["physical_requirements_hash"]), SourceConstraints.from_mapping(item["source_constraints"]),
            editorial_text(item["satisfaction"]), editorial_array(item["alternatives"], EvidenceAlternative.from_mapping))


@dataclass(frozen=True, slots=True)
class EditorialBlueprintBeat:
    beat_id: str
    ordinal: int
    narrative_role: str
    narrative_function: VlmNarrativeFunction
    summary: str
    required_obligation_refs: tuple[SemanticObjectRef, ...]
    required_fact_refs: tuple[SemanticObjectRef, ...]
    evidence_requirements: tuple[BlueprintEvidenceRequirement, ...]
    candidate_preferences: tuple[SemanticObjectRef, ...]
    span_policy: SpanPolicy
    duration_seconds: DurationRange

    def __post_init__(self) -> None:
        editorial_hash(self.beat_id)
        try:
            editorial_integer(self.ordinal)
        except ValueError as error:
            raise EditorialBlueprintError("Blueprint Beat ordinal is invalid") from error
        if type(self.narrative_function) is not VlmNarrativeFunction:  # noqa: E721
            raise EditorialBlueprintError("Blueprint Beat narrative function must use VLM v3 enum")
        editorial_tuple(self.required_obligation_refs, SemanticObjectRef)
        editorial_tuple(self.required_fact_refs, SemanticObjectRef)
        editorial_tuple(self.evidence_requirements, BlueprintEvidenceRequirement, nonempty=True)
        editorial_tuple(self.candidate_preferences, SemanticObjectRef)
        if type(self.span_policy) is not SpanPolicy or type(self.duration_seconds) is not DurationRange:  # noqa: E721
            raise EditorialBlueprintError("Blueprint Beat duration/span must be exact")
        try:
            EditorialBeatDraft(
                self.narrative_role, self.narrative_function, self.summary,
                self.required_obligation_refs, self.required_fact_refs,
                tuple(EvidenceRequirementDraft(
                    item.material_requirement_id, item.satisfaction, item.alternatives,
                ) for item in self.evidence_requirements),
                self.candidate_preferences, self.span_policy, self.duration_seconds,
            )
        except ValueError as error:
            raise EditorialBlueprintError("Blueprint Beat does not preserve the closed draft shape") from error
        for item in self.evidence_requirements:
            if (item.obligation_ref not in self.required_obligation_refs
                    or not set(item.required_fact_refs) <= set(self.required_fact_refs)):
                raise EditorialBlueprintError("Blueprint Beat drops a nested material obligation or Fact")

    def to_mapping(self) -> dict[str, object]:
        return {
            "beat_id": self.beat_id, "ordinal": self.ordinal,
            "narrative_role": self.narrative_role, "narrative_function": self.narrative_function.value,
            "summary": self.summary,
            "required_obligation_refs": [item.to_mapping() for item in self.required_obligation_refs],
            "required_fact_refs": [item.to_mapping() for item in self.required_fact_refs],
            "evidence_requirements": [item.to_mapping() for item in self.evidence_requirements],
            "candidate_preferences": [item.to_mapping() for item in self.candidate_preferences],
            "span_policy": self.span_policy.to_mapping(), "duration_seconds": self.duration_seconds.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> EditorialBlueprintBeat:
        item = editorial_mapping(value, ("beat_id", "ordinal", "narrative_role", "narrative_function", "summary",
            "required_obligation_refs", "required_fact_refs", "evidence_requirements", "candidate_preferences",
            "span_policy", "duration_seconds"))
        return cls(editorial_hash(item["beat_id"]), cast(int, item["ordinal"]), editorial_text(item["narrative_role"]),
            VlmNarrativeFunction(editorial_text(item["narrative_function"])), editorial_text(item["summary"]),
            editorial_array(item["required_obligation_refs"], SemanticObjectRef.from_mapping),
            editorial_array(item["required_fact_refs"], SemanticObjectRef.from_mapping),
            editorial_array(item["evidence_requirements"], BlueprintEvidenceRequirement.from_mapping),
            editorial_array(item["candidate_preferences"], SemanticObjectRef.from_mapping),
            SpanPolicy.from_mapping(item["span_policy"]), DurationRange.from_mapping(item["duration_seconds"]))


@dataclass(frozen=True, slots=True)
class EditorialBlueprint:
    story_id: str
    proposal_ref: SemanticObjectRef
    strategy_version: str
    beats: tuple[EditorialBlueprintBeat, ...]
    ordering_constraints: tuple[OrderingConstraint, ...]
    story_duration_seconds: DurationRange
    editing_intent: EditingIntent
    teaser_intent: TeaserIntent

    def __post_init__(self) -> None:
        editorial_hash(self.story_id)
        if (type(self.proposal_ref) is not SemanticObjectRef  # noqa: E721
                or self.proposal_ref.member_ref.artifact_type != "proposal_set"
                or self.proposal_ref.object_type != "proposal"
                or self.strategy_version != EDITORIAL_BLUEPRINT_STRATEGY_VERSION):
            raise EditorialBlueprintError("Blueprint identity/strategy is invalid")
        beats = editorial_tuple(self.beats, EditorialBlueprintBeat, nonempty=True)
        if tuple(item.ordinal for item in beats) != tuple(range(len(beats))):
            raise EditorialBlueprintError("Blueprint Beat order must be complete and canonical")
        if tuple(item.beat_id for item in beats) != tuple(_beat_id(self.story_id, item.ordinal) for item in beats):
            raise EditorialBlueprintError("Blueprint Beat IDs do not bind Story/order/strategy")
        requirements = tuple(item for beat in beats for item in beat.evidence_requirements)
        if len({item.material_requirement_id for item in requirements}) != len(requirements):
            raise EditorialBlueprintError("Blueprint repeats a material requirement")
        if len({item.evidence_requirement_id for item in requirements}) != len(requirements):
            raise EditorialBlueprintError("Blueprint repeats a derived evidence requirement ID")
        for beat in beats:
            for requirement_ordinal, requirement in enumerate(beat.evidence_requirements):
                if requirement.evidence_requirement_id != _evidence_requirement_id(
                    self.story_id, beat.ordinal, requirement_ordinal,
                    requirement.material_requirement_id,
                ):
                    raise EditorialBlueprintError("Blueprint requirement ID differs from its derived identity")
        if type(self.ordering_constraints) is not tuple or type(self.story_duration_seconds) is not DurationRange:  # noqa: E721
            raise EditorialBlueprintError("Blueprint ordering/duration is invalid")
        if type(self.editing_intent) is not EditingIntent or type(self.teaser_intent) is not TeaserIntent:  # noqa: E721
            raise EditorialBlueprintError("Blueprint intent is invalid")
        try:
            StoryBlueprintDraft(
                self.story_id, self.proposal_ref,
                tuple(EditorialBeatDraft(
                    beat.narrative_role, beat.narrative_function, beat.summary,
                    beat.required_obligation_refs, beat.required_fact_refs,
                    tuple(EvidenceRequirementDraft(
                        requirement.material_requirement_id, requirement.satisfaction,
                        requirement.alternatives,
                    ) for requirement in beat.evidence_requirements),
                    beat.candidate_preferences, beat.span_policy, beat.duration_seconds,
                ) for beat in beats),
                self.ordering_constraints, self.story_duration_seconds,
                self.editing_intent, self.teaser_intent,
            )
        except ValueError as error:
            raise EditorialBlueprintError("Blueprint does not preserve the closed Story draft shape") from error

    def to_mapping(self) -> dict[str, object]:
        return {
            "story_id": self.story_id, "proposal_ref": self.proposal_ref.to_mapping(),
            "strategy_version": self.strategy_version,
            "beats": [item.to_mapping() for item in self.beats],
            "ordering_constraints": [item.to_mapping() for item in self.ordering_constraints],
            "story_duration_seconds": self.story_duration_seconds.to_mapping(),
            "editing_intent": self.editing_intent.to_mapping(), "teaser_intent": self.teaser_intent.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> EditorialBlueprint:
        item = editorial_mapping(value, ("story_id", "proposal_ref", "strategy_version", "beats", "ordering_constraints",
            "story_duration_seconds", "editing_intent", "teaser_intent"))
        return cls(editorial_hash(item["story_id"]), SemanticObjectRef.from_mapping(item["proposal_ref"]),
            editorial_text(item["strategy_version"]), editorial_array(item["beats"], EditorialBlueprintBeat.from_mapping),
            editorial_array(item["ordering_constraints"], decode_editorial_ordering),
            DurationRange.from_mapping(item["story_duration_seconds"]), EditingIntent.from_mapping(item["editing_intent"]),
            TeaserIntent.from_mapping(item["teaser_intent"]))

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class EditorialBlueprintProjection:
    input_binding_sha256: str
    strategy_version: str
    blueprints: tuple[EditorialBlueprint, ...]

    def __post_init__(self) -> None:
        editorial_hash(self.input_binding_sha256)
        if self.strategy_version != EDITORIAL_BLUEPRINT_STRATEGY_VERSION:
            raise EditorialBlueprintError("unsupported Editorial Blueprint strategy")
        blueprints = editorial_tuple(self.blueprints, EditorialBlueprint, nonempty=True)
        if len({item.story_id for item in blueprints}) != len(blueprints):
            raise EditorialBlueprintError("Blueprint projection repeats a Story")

    def to_mapping(self) -> dict[str, object]:
        return {"input_binding_sha256": self.input_binding_sha256, "strategy_version": self.strategy_version,
                "blueprints": [item.to_mapping() for item in self.blueprints]}

    @classmethod
    def from_mapping(cls, value: object) -> EditorialBlueprintProjection:
        item = editorial_mapping(value, ("input_binding_sha256", "strategy_version", "blueprints"))
        return cls(editorial_hash(item["input_binding_sha256"]), editorial_text(item["strategy_version"]),
                   editorial_array(item["blueprints"], EditorialBlueprint.from_mapping))

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


def project_editorial_blueprints(
    stage1: Stage1Values, stage2: StoryDesignValues, draft: EditorialBlueprintDraft,
    *, expected_input_binding_sha256: str, strategy_version: str,
) -> EditorialBlueprintProjection:
    """Project one exact Blueprint per frozen Stage 2 target; no feasibility claim."""
    if strategy_version != EDITORIAL_BLUEPRINT_STRATEGY_VERSION:
        raise EditorialBlueprintError("unsupported Editorial Blueprint strategy")
    if type(stage1) is not Stage1Values or type(stage2) is not StoryDesignValues or type(draft) is not EditorialBlueprintDraft:  # noqa: E721
        raise EditorialBlueprintError("projection requires exact committed Stage 1/2 values and draft")
    binding = editorial_hash(expected_input_binding_sha256)
    if draft.input_binding_sha256 != binding:
        raise EditorialBlueprintError("draft differs from expected committed input binding")
    portfolio = stage2.business.portfolio
    if tuple(item.story_id for item in draft.stories) != portfolio.target_story_ids:
        raise EditorialBlueprintError("draft Story order differs from frozen Portfolio targets")
    proposals = {row.proposal_index: row for row in stage2.business.proposal_set.proposals}
    graph_identity = SemanticMemberIdentity.from_artifact_member(stage1.members[2])
    card_identity = SemanticMemberIdentity.from_artifact_member(stage1.members[0])
    catalog_identity = SemanticMemberIdentity.from_artifact_member(stage2.business.members[0])
    graph_refs = {
        SemanticObjectRef(graph_identity, item.node_type, item.node_id)
        for item in stage1.coverage.narrative_graph.nodes
    }
    candidates = {
        SemanticObjectRef(catalog_identity, "candidate", item.candidate_id)
        for item in stage2.business.candidate_catalog.candidates
    }
    cards = {
        SemanticObjectRef(card_identity, "event", item.event_id)
        for item in stage1.coverage.event_cards.events
    }
    blueprints: list[EditorialBlueprint] = []
    for selection, story in zip(portfolio.selections, draft.stories, strict=True):
        support = proposals.get(selection.proposal_index)
        if support is None or story.proposal_ref != selection.proposal_ref:
            raise EditorialBlueprintError("draft Story proposal differs from frozen selection")
        proposal = support.proposal
        if (story.story_duration_seconds.minimum < proposal.target_duration_seconds.minimum
                or story.story_duration_seconds.maximum > proposal.target_duration_seconds.maximum):
            raise EditorialBlueprintError("draft Story duration expands frozen Proposal duration bounds")
        if story.teaser_intent.strategy != proposal.teaser_strategy:
            raise EditorialBlueprintError("draft Story teaser strategy differs from frozen Proposal")
        requirements = {item.requirement_id: item for item in proposal.material_requirements}
        support_requirements = {item.requirement_id: item for item in support.requirements}
        used = {item.source_material_requirement_id for beat in story.beats for item in beat.evidence_requirements}
        if used != set(requirements):
            raise EditorialBlueprintError("draft dropped, duplicated, or introduced material requirements")
        if {ref for beat in story.beats for ref in beat.required_obligation_refs} != set(proposal.required_obligation_refs):
            raise EditorialBlueprintError("draft dropped or introduced mandatory proposal obligations")
        if {ref for beat in story.beats for ref in beat.required_fact_refs} != set(proposal.required_fact_refs):
            raise EditorialBlueprintError("draft dropped or introduced mandatory proposal facts")
        beats: list[EditorialBlueprintBeat] = []
        for ordinal, beat in enumerate(story.beats):
            for ref in (*beat.required_obligation_refs, *beat.required_fact_refs):
                if ref not in graph_refs:
                    raise EditorialBlueprintError("draft references an unknown Stage 1 Graph object")
            if not set(beat.candidate_preferences) <= candidates:
                raise EditorialBlueprintError("draft candidate preference is not in exact Catalog")
            compiled: list[BlueprintEvidenceRequirement] = []
            for requirement_ordinal, requirement in enumerate(beat.evidence_requirements):
                original = requirements[requirement.source_material_requirement_id]
                material = support_requirements[requirement.source_material_requirement_id]
                if (original.obligation_ref not in beat.required_obligation_refs
                        or not set(material.required_fact_refs) <= set(beat.required_fact_refs)):
                    raise EditorialBlueprintError("Beat does not conserve its material obligation/fact requirements")
                for alternative in requirement.alternative_sets:
                    if not set(alternative.event_refs) <= cards or not set(alternative.candidate_refs) <= candidates:
                        raise EditorialBlueprintError("draft evidence alternative has an unknown predecessor reference")
                compiled.append(BlueprintEvidenceRequirement(
                    _evidence_requirement_id(
                        story.story_id, ordinal, requirement_ordinal, original.requirement_id,
                    ),
                    original.requirement_id, original.obligation_ref, material.required_fact_refs,
                    original.minimum_usable_seconds, original.physical_requirements, original.physical_requirements_hash,
                    original.source_constraints, requirement.satisfaction, requirement.alternative_sets,
                ))
            beats.append(EditorialBlueprintBeat(
                _beat_id(story.story_id, ordinal), ordinal, story.beats[ordinal].narrative_role,
                story.beats[ordinal].narrative_function, story.beats[ordinal].summary,
                story.beats[ordinal].required_obligation_refs, story.beats[ordinal].required_fact_refs,
                tuple(compiled), story.beats[ordinal].candidate_preferences,
                story.beats[ordinal].span_policy, story.beats[ordinal].duration_seconds,
            ))
        blueprints.append(EditorialBlueprint(
            story.story_id, story.proposal_ref, strategy_version,
            tuple(beats), story.ordering_constraints, story.story_duration_seconds,
            story.editing_intent, story.teaser_intent,
        ))
    return EditorialBlueprintProjection(binding, strategy_version, tuple(blueprints))
