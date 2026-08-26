"""Compose/decode the complete Stage 3 business batch, never an Admission.

The DAG is predecessor pool -> closure -> manifest -> Blueprint. Serialization
order remains Blueprint/closure/manifest per frozen Story. Hashes do not depend
on serialization order, database IDs, the request envelope or an Admission.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.compiler.canonical import canonical_json_bytes, load_canonical_json_bytes
from ..store.models import ArtifactMember, canonical_payload_hash
from .editorial_blueprint import EditorialBlueprint, EditorialBlueprintProjection
from .editorial_context import EditorialContextBatch
from .editorial_models import editorial_mapping
from .member_refs import SemanticMemberIdentity

EDITORIAL_STORY_MEMBER_TYPES = ("editorial_blueprint", "evidence_closure_set", "context_manifest")


@dataclass(frozen=True, slots=True)
class EditorialBusinessValues:
    members: tuple[ArtifactMember, ...]
    projection: EditorialBlueprintProjection


def _check_join(contexts: EditorialContextBatch, projection: EditorialBlueprintProjection) -> None:
    if type(contexts) is not EditorialContextBatch or type(projection) is not EditorialBlueprintProjection:  # noqa: E721
        raise ValueError("editorial composition requires exact context/projection values")
    if (projection.input_binding_sha256 != contexts.input_binding_sha256
            or projection.strategy_version != contexts.policy.strategy
            or tuple(blueprint.story_id for blueprint in projection.blueprints) != contexts.target_story_ids):
        raise ValueError("editorial composition changes the frozen input/strategy/target order")
    for context, blueprint in zip(contexts.stories, projection.blueprints, strict=True):
        closure = context.closure
        if blueprint.proposal_ref != closure.proposal_ref:
            raise ValueError("Blueprint Proposal differs from its frozen Story closure")
        expected = {row.source_material_requirement_id: row for row in closure.requirements}
        actual = tuple(row for beat in blueprint.beats for row in beat.evidence_requirements)
        if len(actual) != len(expected) or {row.material_requirement_id for row in actual} != set(expected):
            raise ValueError("Blueprint does not conserve the complete Story material closure")
        for row in actual:
            original = expected[row.material_requirement_id]
            if (row.obligation_ref != original.obligation_ref
                    or set(row.required_fact_refs) != set(original.required_fact_refs)):
                raise ValueError("Blueprint material facts/obligation differ from its exact closure")


def compose_editorial_business_members(
    contexts: EditorialContextBatch, projection: EditorialBlueprintProjection,
) -> EditorialBusinessValues:
    """All 3N pending members or an error; no partial batch, I/O or self-pass."""
    _check_join(contexts, projection)
    members: list[ArtifactMember] = []
    for context, blueprint in zip(contexts.stories, projection.blueprints, strict=True):
        manifest = SemanticMemberIdentity.from_artifact_member(context.context_member)
        payload = canonical_json_bytes({
            "schema_version": "stage3-editorial-blueprint-member-v1",
            "input_binding_sha256": contexts.input_binding_sha256,
            "context_manifest_ref": manifest.to_mapping(),
            "blueprint": blueprint.to_mapping(),
        }).decode("utf-8")
        member = ArtifactMember(
            "editorial_blueprint", f"editorial_blueprint@{blueprint.story_id}",
            manifest.revision, manifest.scope, canonical_payload_hash(payload), payload,
        )
        members.extend((member, context.closure_member, context.context_member))
    return EditorialBusinessValues(tuple(members), projection)


def decode_editorial_business_members(
    members: tuple[ArtifactMember, ...], *, contexts: EditorialContextBatch,
) -> EditorialBusinessValues:
    """Strict member and DAG closure; callers still independently verify truth.

Contexts must be reconstructed from the exact predecessors and frozen policy by
the execution owner. Supplying this typed value is not evidence of a Store read.
This decoder neither accepts nor generates a SemanticFeasibilityAdmission.
"""
    if (type(contexts) is not EditorialContextBatch or type(members) is not tuple  # noqa: E721
            or any(type(member) is not ArtifactMember for member in members)):  # noqa: E721
        raise ValueError("editorial decoding requires exact immutable members/context")
    if tuple(member.artifact_type for member in members) != EDITORIAL_STORY_MEMBER_TYPES * len(contexts.stories):
        raise ValueError("editorial business set must preserve every ordered Story trio")
    blueprints: list[EditorialBlueprint] = []
    for index, context in enumerate(contexts.stories):
        member, closure, manifest = members[index * 3:index * 3 + 3]
        identity = SemanticMemberIdentity.from_artifact_member(member)
        if (identity.logical_id != f"editorial_blueprint@{context.story_id}"
                or identity.scope != context.context_member.scope
                or identity.revision != context.context_member.revision
                or closure != context.closure_member or manifest != context.context_member):
            raise ValueError("editorial member identity/content differs from its exact Story context")
        payload = editorial_mapping(load_canonical_json_bytes(
            member.payload_json.encode("utf-8"), origin="editorial Blueprint",
        )[0], ("schema_version", "input_binding_sha256", "context_manifest_ref", "blueprint"))
        if (payload["schema_version"] != "stage3-editorial-blueprint-member-v1"
                or payload["input_binding_sha256"] != contexts.input_binding_sha256
                or SemanticMemberIdentity.from_mapping(payload["context_manifest_ref"])
                != SemanticMemberIdentity.from_artifact_member(context.context_member)):
            raise ValueError("editorial Blueprint does not bind the actual input/ContextManifest")
        blueprint = EditorialBlueprint.from_mapping(payload["blueprint"])
        if blueprint.story_id != context.story_id:
            raise ValueError("editorial Blueprint payload names a different Story")
        blueprints.append(blueprint)
    projection = EditorialBlueprintProjection(contexts.input_binding_sha256, contexts.policy.strategy, tuple(blueprints))
    _check_join(contexts, projection)
    return EditorialBusinessValues(members, projection)
