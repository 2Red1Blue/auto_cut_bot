"""Explicit, local-only v1 -> compact-v2 migration over bound saved raw bytes.

This does not grant Admission or create a Receipt. Its caller must verify the
original request/attempt/blob and persist a new append-only derivation. It never
modifies the old raw response or invokes a provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from ..contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from .member_refs import SemanticObjectRef
from .narrative_models import EntityAttributes
from .story_design_compact import (
    COMPACT_WIRE_SCHEMA_VERSION,
    StoryDesignCompactContext,
    decode_story_design_compact,
)
from .story_design_compact_models import ProposalDraftSetV2
from .story_design_draft import StoryDesignDraftPolicy, decode_story_design_draft

COMPACT_MIGRATION_VERSION = "stage2-draft-v1-to-compact-v2"


@dataclass(frozen=True, slots=True)
class CompactMigrationResult:
    original_raw_sha256: str
    wire_bytes: bytes
    draft: ProposalDraftSetV2
    changes_json: str

    @property
    def changes(self) -> tuple[dict[str, object], ...]:
        return tuple(cast(list[dict[str, object]], json.loads(self.changes_json)))


def migrate_story_design_v1_to_compact(
    raw: bytes, *, context: StoryDesignCompactContext, policy: StoryDesignDraftPolicy,
) -> CompactMigrationResult:
    legacy = decode_story_design_draft(raw, expected_input_binding_sha256=context.input_binding_sha256,
                                       policy=policy)
    nodes = {node.node_id: node for node in context.graph.nodes}
    profiles = sorted(context.story_policy.editing_profiles, key=lambda profile: canonical_json_bytes(profile.to_mapping()))
    profile_aliases = {profile: f"style{index}" for index, profile in enumerate(profiles, 1)}
    changes: list[dict[str, object]] = []
    proposals = []
    for index, proposal in enumerate(legacy.proposals):
        subject_refs = []
        for ref_index, ref in enumerate(proposal.key_character_refs):
            target = nodes.get(ref.object_id)
            if ref.member_ref != context.graph_owner or target is None:
                raise ValueError("COMPACT_MIGRATION_REFERENCE_NOT_FOUND")
            if target.node_type == "character":
                subject_refs.append(context.alias_for(ref))
            elif (target.node_type == "entity" and type(target.attributes) is EntityAttributes
                  and target.attributes.entity_kind == "person"):
                actual = SemanticObjectRef(context.graph_owner, "entity", ref.object_id)
                subject_refs.append(context.alias_for(actual))
                changes.append({"kind": "declared_character_to_observed_person",
                                "json_path": f"$.proposals[{index}].key_character_refs[{ref_index}]",
                                "before": ref.to_mapping(), "after": actual.to_mapping()})
            else:
                raise ValueError("COMPACT_MIGRATION_SUBJECT_NOT_PERSON")
        # Even fields derived by v2 must resolve in the original exact graph;
        # unknown or foreign v1 facts cannot be hidden by recomputing closure.
        for ref in (*proposal.thread_refs, *proposal.required_obligation_refs, *proposal.required_fact_refs):
            context.alias_for(ref)
        if proposal.editing_profile not in profile_aliases:
            raise ValueError("COMPACT_MIGRATION_EDITING_PROFILE_NOT_FOUND")
        requirements = []
        for requirement in proposal.material_requirements:
            if not set(context.story_policy.required_physical_requirements) <= set(requirement.physical_requirements):
                raise ValueError("COMPACT_MIGRATION_MISSING_PHYSICAL_REQUIREMENT")
            source = requirement.source_constraints
            requirements.append({
                "obligation_ref": context.alias_for(requirement.obligation_ref),
                "minimum_usable_seconds": requirement.minimum_usable_seconds,
                "additional_checks": [check.to_mapping() for check in requirement.physical_requirements],
                "source_constraints": {
                    "source_selection": "subset" if source.allowed_source_refs else "all_granted",
                    "allowed_source_refs": [context.alias_for(ref) for ref in source.allowed_source_refs],
                    "forbidden_source_refs": [context.alias_for(ref) for ref in source.forbidden_source_refs],
                },
            })
        proposals.append({
            "title": proposal.title, "narrative_claim": proposal.narrative_claim,
            "thread_refs": [context.alias_for(ref) for ref in proposal.thread_refs],
            "obligation_refs": [context.alias_for(ref) for ref in proposal.required_obligation_refs],
            "key_subject_refs": subject_refs, "genre_tags": list(proposal.genre_tags),
            "editing_profile_ref": profile_aliases[proposal.editing_profile],
            "target_duration_seconds": proposal.target_duration_seconds.to_mapping(),
            "teaser_strategy": proposal.teaser_strategy, "audience_hook": proposal.audience_hook,
            "material_requirements": requirements,
        })
    wire = canonical_json_bytes({"schema_version": COMPACT_WIRE_SCHEMA_VERSION, "proposals": proposals})
    draft = decode_story_design_compact(wire, context=context, policy=policy)
    for index, (before, after) in enumerate(zip(legacy.proposals, draft.proposals, strict=True)):
        if set(before.required_fact_refs) != set(after.required_fact_refs):
            changes.append({"kind": "derived_obligation_fact_closure", "json_path": f"$.proposals[{index}].required_fact_refs",
                            "before": [ref.to_mapping() for ref in before.required_fact_refs],
                            "after": [ref.to_mapping() for ref in after.required_fact_refs]})
        changes.append({"kind": "program_owned_proposal_identity", "json_path": f"$.proposals[{index}].proposal_id",
                        "before": before.proposal_id, "after": after.proposal_id})
        for requirement_index, (old, new) in enumerate(zip(before.material_requirements, after.material_requirements, strict=True)):
            changes.append({"kind": "program_owned_material_projection",
                            "json_path": f"$.proposals[{index}].material_requirements[{requirement_index}]",
                            "before": old.to_mapping(), "after": new.to_mapping()})
    return CompactMigrationResult(sha256_bytes(raw), wire, draft, canonical_json_bytes(changes).decode("utf-8"))
