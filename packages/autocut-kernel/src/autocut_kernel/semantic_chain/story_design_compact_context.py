"""Rich model view and private exact references for compact Stage 2."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..store.models import CommittedSemanticInputs
from .candidate_catalog import CandidateCatalogPolicy
from .candidate_projection import CandidateCatalogProjection
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .narrative_models import EntityAttributes, NarrativeGraph
from .stage1_result import Stage1Values
from .story_design_context import story_design_input_binding
from .story_design_models import JobPolicy, StoryDesignPolicy


@dataclass(frozen=True, slots=True)
class StoryDesignCompactContext:
    """Pure content, not Store authority. Rebuild this at every audit boundary."""

    input_binding_sha256: str
    aliases: tuple[tuple[str, SemanticObjectRef], ...]
    graph: NarrativeGraph
    graph_owner: SemanticMemberIdentity
    granted_sources: tuple[SemanticObjectRef, ...]
    job_policy: JobPolicy
    story_policy: StoryDesignPolicy
    candidate_policy: CandidateCatalogPolicy
    model_view_json: str

    def model_view(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.model_view_json))

    def private_mapping(self) -> dict[str, object]:
        return {
            "schema_version": "stage2-compact-reference-map-v2",
            "input_binding_sha256": self.input_binding_sha256,
            "aliases": [{"alias": alias, "reference": ref.to_mapping()} for alias, ref in self.aliases],
            "job_policy_sha256": self.job_policy.canonical_hash,
            "story_policy_sha256": self.story_policy.canonical_hash,
            "candidate_policy_sha256": self.candidate_policy.canonical_hash,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.private_mapping())

    def resolve(self, alias: object, prefix: str) -> SemanticObjectRef:
        if type(alias) is not str or not alias.startswith(prefix):  # noqa: E721
            raise ValueError("COMPACT_REFERENCE_TYPE_MISMATCH")
        for name, ref in self.aliases:
            if name == alias:
                return ref
        raise ValueError("COMPACT_REFERENCE_NOT_FOUND")

    def alias_for(self, ref: SemanticObjectRef) -> str:
        for alias, reference in self.aliases:
            if reference == ref:
                return alias
        raise ValueError("COMPACT_REFERENCE_NOT_FOUND")


def build_story_design_compact_context(
    inputs: CommittedSemanticInputs, stage1: Stage1Values, projection: CandidateCatalogProjection, *,
    job_policy: JobPolicy, story_policy: StoryDesignPolicy, candidate_policy: CandidateCatalogPolicy,
) -> StoryDesignCompactContext:
    if type(inputs) is not CommittedSemanticInputs:  # noqa: E721
        raise ValueError("compact context requires exact semantic inputs")
    binding = story_design_input_binding(stage1, projection, job_policy=job_policy,
                                        story_policy=story_policy, candidate_policy=candidate_policy)
    inputs.source_grant.require_purpose("render_source")
    graph = stage1.coverage.narrative_graph
    owner = stage1.coverage.identity("narrative_graph")
    source_owner = SemanticMemberIdentity.from_committed_member_reference(inputs.source_manifest.reference)
    if source_owner != stage1.dependency_proof.source_member_ref:
        raise ValueError("compact context Source differs from admitted Stage 1")
    sources = tuple(SemanticObjectRef(source_owner, "source", source.source_id)
                    for source in inputs.source_grant.sources)
    if not set(job_policy.source_constraints.allowed_source_refs
               + job_policy.source_constraints.forbidden_source_refs) <= set(sources):
        raise ValueError("compact Job constraint names a foreign Source")
    groups: dict[str, list[SemanticObjectRef]] = {key: [] for key in ("p", "f", "e", "t", "o", "c", "s", "n")}
    for node in graph.nodes:
        prefix = {"fact": "f", "event": "e", "story_thread": "t", "obligation": "o",
                  "character": "p"}.get(node.node_type, "n")
        if type(node.attributes) is EntityAttributes and node.attributes.entity_kind == "person":
            prefix = "p"
        groups[prefix].append(SemanticObjectRef(owner, node.node_type, node.node_id))
    catalog_owner = SemanticMemberIdentity.from_artifact_member(projection.member)
    groups["c"] = [SemanticObjectRef(catalog_owner, "candidate", row.candidate_id)
                   for row in projection.catalog.candidates]
    groups["s"] = list(sources)
    aliases = tuple((f"{prefix}{index}", ref) for prefix, refs in groups.items()
                    for index, ref in enumerate(sorted(refs, key=lambda ref: canonical_json_bytes(ref.to_mapping())), 1))
    by_ref = {ref: alias for alias, ref in aliases}
    by_node = {ref.object_id: alias for alias, ref in aliases if ref.member_ref == owner}
    episodes = stage1.coverage.episode_digests.digests
    episode_ordinals = {episode.episode_id: episode.ordinal for episode in episodes}

    def graph_value(value: object) -> object:
        if type(value) is str:
            if value in by_node:
                return by_node[value]
            if value in episode_ordinals:
                return episode_ordinals[value]
            if value.startswith("sha256:"):
                raise ValueError("unprojected technical identity in compact graph attributes")
            return value
        if type(value) is list:
            return [graph_value(item) for item in cast(list[object], value)]
        if type(value) is dict:
            item = cast(dict[str, object], value)
            # Exact upstream ownership remains private; all narrative attributes
            # survive, including conflict status and unresolved identity facts.
            technical = {"event_card_ref", "source_range_refs", "identity_evidence_refs",
                         "source_window_ref", "evidence_refs"}
            return {key: graph_value(child) for key, child in item.items() if key not in technical}
        return value

    nodes = [{"ref": by_node[node.node_id], "kind": node.node_type, "label": node.label,
              "attributes": graph_value(node.attributes.to_mapping()), "confidence": node.confidence.to_mapping()}
             for node in graph.nodes]
    cards = {card.event_id: card for card in stage1.coverage.event_cards.events}
    for row in nodes:
        if row["kind"] == "entity" and str(row["ref"]).startswith("p"):
            row["identity_status"] = "observed_person"
        elif row["kind"] == "character":
            row["identity_status"] = "established_character"
    events = []
    for row in nodes:
        if row["kind"] != "event":
            continue
        ref = next(ref for alias, ref in aliases if alias == row["ref"])
        card = cards[ref.object_id]
        events.append({**row, "content": card.content,
                       "regions": [{"source_ref": by_ref[region.source_ref],
                                    "coarse_interval": region.mapped_interval.to_mapping()}
                                   for region in card.source_range_refs]})
    candidates = []
    for candidate in projection.catalog.candidates:
        direct = (candidate.anchor_event, *candidate.supporting_events, *candidate.payoff_events)
        candidates.append({
            "ref": by_ref[SemanticObjectRef(catalog_owner, "candidate", candidate.candidate_id)],
            "source_ref": by_ref[candidate.source_ref], "kind": candidate.candidate_kind,
            "reason": candidate.reason, "anchor_summary": candidate.anchor_summary,
            "payoff_or_open_question": candidate.payoff_or_open_question,
            "open_question": candidate.open_question, "dialogue_excerpt": candidate.dialogue_excerpt,
            "anchor_event": by_ref[candidate.anchor_event.graph_event_ref],
            "supporting_events": [by_ref[row.graph_event_ref] for row in candidate.supporting_events],
            "context_events": [by_ref[row.graph_event_ref] for row in candidate.context_events],
            "payoff_events": [by_ref[row.graph_event_ref] for row in candidate.payoff_events],
            "editing_modes": list(candidate.editing_modes), "narrative_functions": list(candidate.narrative_functions),
            "tags": list(candidate.tags),
            "measurements": [{"kind": measure.measurement_kind, "value": measure.value,
                              "confidence": measure.confidence,
                              "fact_refs": [by_node[ref.object_id] for ref in measure.fact_refs],
                              "event_refs": [by_node[ref.object_id] for ref in measure.event_refs]}
                             for measure in candidate.measurements],
            "coarse_region": candidate.support.source_interval.to_mapping(),
            "conservative_duration": candidate.support.conservative_duration.to_mapping(),
            "confidence": candidate.support.confidence,
            "material_support_status": "requires_independent_evaluation",
            "direct_event_refs": [by_ref[event.graph_event_ref] for event in direct],
        })
    profiles = sorted(story_policy.editing_profiles, key=lambda profile: canonical_json_bytes(profile.to_mapping()))
    view: dict[str, object] = {
        "schema_version": "stage2-proposal-context-compact-v2",
        "subjects": [row for row in nodes if str(row["ref"]).startswith("p")],
        "facts": [row for row in nodes if row["kind"] == "fact"], "events": events,
        "threads": [row for row in nodes if row["kind"] == "story_thread"],
        "obligations": [row for row in nodes if row["kind"] == "obligation"],
        "context_nodes": [row for row in nodes if str(row["ref"]).startswith("n")],
        "connections": [{"kind": edge.edge_type, "from_ref": by_node[edge.from_node_id],
                         "to_ref": by_node[edge.to_node_id]} for edge in graph.edges],
        "episodes": [{"ordinal": episode.ordinal, "summary": episode.summary} for episode in episodes],
        "candidates": candidates, "sources": [{"ref": by_ref[ref]} for ref in sources],
        "policy_choices": {
            "genre_tags": list(story_policy.allowed_genre_tags),
            "editing_profiles": [{"ref": f"style{index}", **profile.to_mapping()}
                                 for index, profile in enumerate(profiles, 1)],
            "teaser_strategies": list(story_policy.teaser_strategies),
            "target_duration_seconds": job_policy.target_duration_seconds.to_mapping(),
            "proposal_count": job_policy.proposal_count.to_mapping(),
            "selected_story_count": job_policy.selected_story_count,
            "source_reuse_policy": job_policy.source_reuse_policy,
            "allowed_source_refs": [by_ref[ref] for ref in (job_policy.source_constraints.allowed_source_refs or sources)],
            "forbidden_source_refs": [by_ref[ref] for ref in job_policy.source_constraints.forbidden_source_refs],
            "required_physical_checks": [item.to_mapping() for item in story_policy.required_physical_requirements],
        },
    }
    return StoryDesignCompactContext(binding, aliases, graph, owner, sources, job_policy, story_policy,
                                     candidate_policy, canonical_json_bytes(view).decode("utf-8"))
