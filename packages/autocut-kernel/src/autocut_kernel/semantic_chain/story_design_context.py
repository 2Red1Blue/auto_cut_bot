"""Single owner of Stage 2 semantic input binding; no generation or admission.

Provider/model/prompt/retry/decoder resource limits belong to the complete
Command request identity. This semantic binding covers exact predecessor and
pending candidate content plus all policies affecting material selection.
"""

from __future__ import annotations

from ..contracts.compiler.canonical import canonical_json_hash
from .candidate_catalog import CandidateCatalogPolicy
from .candidate_projection import CandidateCatalogProjection
from .member_refs import SemanticMemberIdentity
from .stage1_result import Stage1Values, decode_stage1_members
from .story_design_models import JobPolicy, StoryDesignPolicy


def story_design_input_binding(
    stage1: Stage1Values, projection: CandidateCatalogProjection, *,
    job_policy: JobPolicy, story_policy: StoryDesignPolicy,
    candidate_policy: CandidateCatalogPolicy,
) -> str:
    """Bind content, not commitment; caller must use the exact predecessor reader.

    The same function is used before proposal generation and during material
    evaluation. It does not accept a caller-provided expected digest as evidence
    that the inputs were read. Candidate truth is separately reconstructed by
    the projection/evaluator over committed Source and VLM inputs.
    """
    if (type(stage1) is not Stage1Values or type(projection) is not CandidateCatalogProjection  # noqa: E721
            or type(job_policy) is not JobPolicy or type(story_policy) is not StoryDesignPolicy  # noqa: E721
            or type(candidate_policy) is not CandidateCatalogPolicy):  # noqa: E721
        raise ValueError("Stage 2 binding requires exact typed semantic inputs and policies")
    member = projection.member
    identity = SemanticMemberIdentity.from_artifact_member(member)
    catalog = projection.catalog
    if (identity.artifact_type != "candidate_catalog" or identity.logical_id != "candidate_catalog"
            or identity.content_hash != catalog.canonical_hash):
        raise ValueError("Stage 2 pending CandidateCatalog content and identity differ")
    # Decode actual member payloads again so a manually composed Stage1Values
    # cannot mix one member set with another decoded Graph/Admission.
    decoded = decode_stage1_members(stage1.members, scope=identity.scope)
    if decoded != stage1 or stage1.admission.next_action != "continue":
        raise ValueError("Stage 2 binding requires the complete admitted Stage 1 content")
    coverage = stage1.coverage
    if (catalog.input_binding_sha256 != stage1.admission.input_binding_sha256
            or catalog.event_card_member_ref != coverage.identity("event_card_set")
            or catalog.narrative_graph_member_ref != coverage.identity("narrative_graph")
            or catalog.coverage_ledger_member_ref != coverage.identity("coverage_ledger")):
        raise ValueError("CandidateCatalog references different Stage 1 predecessors")
    if (catalog.policy_sha256 != candidate_policy.canonical_hash
            or job_policy.story_design_policy_sha256 != story_policy.canonical_hash):
        raise ValueError("Stage 2 semantic policy identities do not close")
    return canonical_json_hash({
        "schema_version": "stage2-story-design-input-binding-v1",
        "stage1_input_binding_sha256": stage1.admission.input_binding_sha256,
        "stage1_members": [SemanticMemberIdentity.from_artifact_member(item).to_mapping() for item in stage1.members],
        "candidate_catalog_member": identity.to_mapping(),
        "candidate_policy_sha256": candidate_policy.canonical_hash,
        "job_policy_sha256": job_policy.canonical_hash,
        "story_design_policy_sha256": story_policy.canonical_hash,
    })
