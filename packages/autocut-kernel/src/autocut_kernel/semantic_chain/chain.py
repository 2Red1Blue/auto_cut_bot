"""Pure deterministic construction of the semantic narrative MVP chain."""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    BeatRole,
    BlueprintBeat,
    EditorialBlueprint,
    EventCard,
    EventKind,
    NarrativeGraph,
    NarrativeNode,
    RegisteredFact,
    SemanticChainDenied,
    SemanticChainInput,
    SemanticProfile,
    Story,
    canonical_sha256,
)


def _derived_id(kind: str, payload: object) -> str:
    """Create an opaque deterministic identifier without interpreting input IDs."""

    return f"{kind}_{canonical_sha256(payload)[7:39]}"


def _event_kind(fact: RegisteredFact) -> EventKind:
    return EventKind(fact.kind.value)


def _beat_role(position: int, total: int) -> BeatRole:
    if position == 0:
        return BeatRole.SETUP
    if position == total - 1:
        return BeatRole.PAYOFF
    return BeatRole.ESCALATION


@dataclass(frozen=True, slots=True)
class SemanticChain:
    """The complete immutable output of the three pure semantic stages."""

    narrative: NarrativeGraph
    story: Story
    blueprint: EditorialBlueprint

    def __post_init__(self) -> None:
        if type(self.narrative) is not NarrativeGraph or type(self.story) is not Story or type(self.blueprint) is not EditorialBlueprint:  # noqa: E721
            raise SemanticChainDenied("semantic chain must contain only closed semantic artifacts")
        if not (self.narrative.profile is self.story.profile is self.blueprint.profile):
            raise SemanticChainDenied("semantic chain artifacts must bind the same profile")
        if self.story.narrative_hash != self.narrative.canonical_hash:
            raise SemanticChainDenied("story must bind its exact narrative artifact")
        if self.blueprint.story_hash != self.story.canonical_hash:
            raise SemanticChainDenied("blueprint must bind its exact story artifact")

    @property
    def profile(self) -> SemanticProfile:
        return self.narrative.profile

    def to_mapping(self) -> dict[str, object]:
        return {"blueprint": self.blueprint.to_mapping(), "narrative": self.narrative.to_mapping(), "story": self.story.to_mapping()}

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


class SemanticChainBuilder:
    """Build the MVP's narrative, event/story, and blueprint stages in order."""

    def build_narrative(self, source: SemanticChainInput) -> NarrativeGraph:
        self._require_input(source)
        nodes = tuple(
            NarrativeNode(
                _derived_id("node", {"fact_id": fact.fact_id, "candidate": fact.candidate.to_mapping()}),
                fact.fact_id,
                fact.evidence,
                fact.candidate,
            )
            for fact in sorted(source.facts, key=lambda item: item.fact_id)
        )
        return NarrativeGraph(_derived_id("narrative", [node.to_mapping() for node in nodes]), source.profile, nodes, source.canonical_hash)

    def build_story(self, source: SemanticChainInput, narrative: NarrativeGraph) -> Story:
        self._require_input(source)
        expected_narrative = self.build_narrative(source)
        if type(narrative) is not NarrativeGraph or narrative.canonical_hash != expected_narrative.canonical_hash:  # noqa: E721
            raise SemanticChainDenied("story stage requires the exact narrative for its registered input")
        facts = {fact.fact_id: fact for fact in source.facts}
        events = tuple(
            EventCard(
                _derived_id("event", node.to_mapping()),
                node.node_id,
                _event_kind(facts[node.fact_id]),
                node.evidence,
                node.candidate,
            )
            for node in narrative.nodes
        )
        return Story(_derived_id("story", [event.to_mapping() for event in events]), narrative.canonical_hash, source.profile, events)

    def build_blueprint(self, source: SemanticChainInput, story: Story) -> EditorialBlueprint:
        self._require_input(source)
        expected_story = self.build_story(source, self.build_narrative(source))
        if type(story) is not Story or story.canonical_hash != expected_story.canonical_hash:  # noqa: E721
            raise SemanticChainDenied("blueprint stage requires the exact story for its registered input")
        beats = tuple(
            BlueprintBeat(
                _derived_id("beat", event.to_mapping()),
                event.event_id,
                _beat_role(index, len(story.events)),
                event.evidence,
                event.candidate,
            )
            for index, event in enumerate(story.events)
        )
        return EditorialBlueprint(_derived_id("blueprint", [beat.to_mapping() for beat in beats]), story.canonical_hash, source.profile, beats)

    def build(self, source: SemanticChainInput) -> SemanticChain:
        narrative = self.build_narrative(source)
        story = self.build_story(source, narrative)
        return SemanticChain(narrative, story, self.build_blueprint(source, story))

    @staticmethod
    def _require_input(source: SemanticChainInput) -> None:
        if type(source) is not SemanticChainInput:  # noqa: E721
            raise SemanticChainDenied("semantic builder accepts only SemanticChainInput")
