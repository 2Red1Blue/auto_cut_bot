"""Compose independent business checks, without claiming durable input reads.

The Command alone performs KC-IN-001. This function does not accept an Admission
or caller check map, and cannot turn a typed in-memory input into Store authority.
"""

from __future__ import annotations

from ..store.models import ArtifactMember, CommittedSemanticInputs
from .coverage_admission import BUSINESS_MEMBER_TYPES
from .coverage_analysis import Stage1CoveragePolicy
from .coverage_verification import verify_coverage_members
from .dependency_projection import DependencyProjectionPolicy
from .dependency_verification import verify_dependency_proof
from .factual_verification import verify_factual_members
from .member_refs import SemanticMemberIdentity
from .stage1_checks import KC_RULE_IDS, Stage1Check
from .stage1_draft import Stage1DraftPolicy
from .stage1_members import COVERAGE_MEMBER_TYPES


def evaluate_stage1_business_members(
    inputs: CommittedSemanticInputs,
    raw_draft: bytes,
    *,
    members: tuple[ArtifactMember, ...],
    draft_policy: Stage1DraftPolicy,
    coverage_policy: Stage1CoveragePolicy,
    dependency_policy: DependencyProjectionPolicy,
) -> tuple[Stage1Check, ...]:
    """Run all sixteen business rules, or reject unreadable/incomplete content.

    Malformed shape is ValueError, not a partially populated successful result.
    All actual business mismatches retain the responsible failed rule(s).
    """
    if type(inputs) is not CommittedSemanticInputs:  # noqa: E721
        raise ValueError("Stage 1 evaluation requires exact typed inputs")
    if type(members) is not tuple or any(type(item) is not ArtifactMember for item in members):  # noqa: E721
        raise ValueError("Stage 1 evaluation requires exact pending members")
    if len(members) != 7 or {item.artifact_type for item in members} != set(BUSINESS_MEMBER_TYPES):
        raise ValueError("Stage 1 evaluation requires all seven distinct business members")
    identities = tuple(SemanticMemberIdentity.from_artifact_member(item) for item in members)
    if any(item.scope != inputs.source_manifest.reference.scope or item.logical_id != item.artifact_type
           for item in identities) or len({item.revision for item in identities}) != 1:
        raise ValueError("Stage 1 business member scope/logical identity/revision mismatch")
    by_type = {item.artifact_type: item for item in members}
    six = tuple(by_type[kind] for kind in COVERAGE_MEMBER_TYPES)
    factual = verify_factual_members(
        inputs, raw_draft, members=six, draft_policy=draft_policy, coverage_policy=coverage_policy,
    )
    coverage = verify_coverage_members(
        inputs, raw_draft, members=six, draft_policy=draft_policy, coverage_policy=coverage_policy,
    )
    dependency = verify_dependency_proof(
        inputs, graph_member=by_type["narrative_graph"],
        event_card_member=by_type["event_card_set"], ledger_member=by_type["coverage_ledger"],
        proof_member=by_type["dependency_closure_proof"], policy=dependency_policy,
    )
    results = (*factual, *coverage, *(Stage1Check(item.rule_id, item.status, item.violation_codes)
                                   for item in dependency))
    expected = set(KC_RULE_IDS) - {"KC-IN-001"}
    if (len(results) != len(expected) or {item.rule_id for item in results} != expected
            or any(type(item) is not Stage1Check for item in results)):
        raise ValueError("Stage 1 evaluators did not perform exactly the required business checks")
    return tuple(sorted(results, key=lambda item: item.rule_id))
