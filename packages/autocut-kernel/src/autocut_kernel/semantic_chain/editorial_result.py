"""Exact ordered 3N+1 Stage 3 structural decoding, never execution permission."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.compiler.canonical import load_canonical_json_bytes
from ..store.models import ArtifactMember
from .editorial_admission import SemanticFeasibilityAdmission
from .editorial_context import EditorialContextBatch
from .editorial_members import EditorialBusinessValues, decode_editorial_business_members
from .member_refs import SemanticMemberIdentity


@dataclass(frozen=True, slots=True)
class EditorialValues:
    members: tuple[ArtifactMember, ...]
    business: EditorialBusinessValues
    admission: SemanticFeasibilityAdmission


def decode_editorial_members(members: tuple[ArtifactMember, ...], *, contexts: EditorialContextBatch) -> EditorialValues:
    """Decode exact content; the execution owner must independently recompute it."""
    if (type(contexts) is not EditorialContextBatch or type(members) is not tuple  # noqa: E721
            or any(type(member) is not ArtifactMember for member in members)):  # noqa: E721
        raise ValueError("Stage 3 result requires exact immutable context/members")
    if len(members) != 3 * len(contexts.stories) + 1:
        raise ValueError("Stage 3 result must contain exactly 3N+1 members")
    business = decode_editorial_business_members(members[:-1], contexts=contexts)
    member = members[-1]
    identity = SemanticMemberIdentity.from_artifact_member(member)
    if (identity.artifact_type != "semantic_feasibility_admission" or identity.logical_id != "semantic_feasibility_admission"
            or identity.scope != members[0].scope or identity.revision != members[0].revision):
        raise ValueError("Stage 3 Admission identity differs from exact business batch")
    admission = SemanticFeasibilityAdmission.from_mapping(load_canonical_json_bytes(
        member.payload_json.encode("utf-8"), origin="semantic_feasibility_admission",
    )[0])
    if (admission.business_members != tuple(SemanticMemberIdentity.from_artifact_member(item) for item in members[:-1])
            or admission.target_story_ids != contexts.target_story_ids
            or admission.input_binding_sha256 != contexts.input_binding_sha256
            or admission.feasibility.projection_sha256 != business.projection.canonical_hash):
        raise ValueError("Stage 3 Admission subject/context/projection binding differs from actual content")
    return EditorialValues(members, business, admission)
