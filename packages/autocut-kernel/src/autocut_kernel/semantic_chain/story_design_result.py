"""Strict five-member Stage 2 content decoding, not permission to execute."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.compiler.canonical import load_canonical_json_bytes
from ..store.models import ArtifactMember, ArtifactScope
from .member_refs import SemanticMemberIdentity
from .portfolio_admission import PortfolioAdmission
from .story_design_compiler import STAGE2_BUSINESS_MEMBER_TYPES
from .story_design_members import StoryDesignBusinessValues, decode_story_design_business_members

STAGE2_MEMBER_TYPES = (*STAGE2_BUSINESS_MEMBER_TYPES, "portfolio_admission")


@dataclass(frozen=True, slots=True)
class StoryDesignValues:
    members: tuple[ArtifactMember, ...]
    business: StoryDesignBusinessValues
    admission: PortfolioAdmission


def decode_story_design_members(
    members: tuple[ArtifactMember, ...], *, scope: ArtifactScope,
) -> StoryDesignValues:
    """Close identities and payloads; the committed reader must recompute truth.

    An invalid or indeterminate Admission is structurally decodable, never
    automatically executable. Missing checks cannot be filled by this decoder.
    """
    if type(members) is not tuple or any(type(member) is not ArtifactMember for member in members):  # noqa: E721
        raise ValueError("Stage 2 result requires exact ArtifactMember values")
    if tuple(member.artifact_type for member in members) != STAGE2_MEMBER_TYPES:
        raise ValueError("Stage 2 result must contain exactly five ordered members")
    business = decode_story_design_business_members(members[:4], scope=scope)
    member = members[4]
    identity = SemanticMemberIdentity.from_artifact_member(member)
    if (identity.scope != scope or identity.logical_id != "portfolio_admission"
            or identity.revision != members[0].revision):
        raise ValueError("Stage 2 Admission output identity differs from business members")
    admission = PortfolioAdmission.from_mapping(load_canonical_json_bytes(
        member.payload_json.encode("utf-8"), origin="portfolio_admission",
    )[0])
    if set(admission.business_members) != {SemanticMemberIdentity.from_artifact_member(item) for item in members[:4]}:
        raise ValueError("Stage 2 Admission subject differs from the four business members")
    if (admission.input_binding_sha256 != business.proposal_set.input_binding_sha256
            or admission.canonical_draft_sha256 != business.proposal_set.draft_sha256
            or admission.candidate_policy_sha256 != business.candidate_catalog.policy_sha256
            or admission.job_policy_sha256 != business.portfolio.job_policy_sha256
            or admission.target_story_ids != business.portfolio.target_story_ids):
        raise ValueError("Stage 2 Admission input/draft/policy/target binding differs from business content")
    return StoryDesignValues(members, business, admission)
