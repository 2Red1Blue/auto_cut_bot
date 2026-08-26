"""Strict eight-member Stage 1 structural decoding, without admission authority."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.compiler.canonical import canonical_json_bytes, load_canonical_json_bytes
from ..store.models import ArtifactMember, ArtifactScope
from .coverage_admission import BUSINESS_MEMBER_TYPES, CoverageAdmission
from .dependency_proof import DependencyClosureProof
from .member_refs import SemanticMemberIdentity
from .stage1_members import Stage1CoverageValues, decode_coverage_members

STAGE1_MEMBER_TYPES = (*BUSINESS_MEMBER_TYPES, "coverage_admission")


@dataclass(frozen=True, slots=True)
class Stage1Values:
    """Closed decoded Stage 1 content; not a Store-read or accept decision."""

    members: tuple[ArtifactMember, ...]
    coverage: Stage1CoverageValues
    dependency_proof: DependencyClosureProof
    admission: CoverageAdmission


def _payload(member: ArtifactMember, label: str) -> object:
    return load_canonical_json_bytes(member.payload_json.encode("utf-8"), origin=label)[0]


def decode_stage1_members(members: tuple[ArtifactMember, ...], *, scope: ArtifactScope) -> Stage1Values:
    """Decode and bind exactly the Store-ordinal eight-member Stage 1 result.

    This establishes structural closure only.  In particular an Admission whose
    next action is ``quarantine`` remains a valid decodable record.
    """
    if type(members) is not tuple or any(type(item) is not ArtifactMember for item in members):  # noqa: E721
        raise ValueError("Stage 1 members must be exact ArtifactMember values")
    if type(scope) is not ArtifactScope:  # noqa: E721
        raise ValueError("Stage 1 scope must be exact ArtifactScope")
    if len(members) != len(STAGE1_MEMBER_TYPES) or tuple(item.artifact_type for item in members) != STAGE1_MEMBER_TYPES:
        raise ValueError("Stage 1 members must use exact canonical Store ordinal types")
    if any(item.logical_id != item.artifact_type or item.scope != scope for item in members):
        raise ValueError("Stage 1 member logical identity or scope mismatch")
    if len({item.revision for item in members}) != 1:
        raise ValueError("Stage 1 members must share one revision")
    identities = tuple(SemanticMemberIdentity.from_artifact_member(item) for item in members)
    coverage = decode_coverage_members(members[:6], scope=scope)
    try:
        proof = DependencyClosureProof.from_mapping(_payload(members[6], "dependency_closure_proof"))
        admission = CoverageAdmission.from_mapping(_payload(members[7], "coverage_admission"))
    except ValueError as error:
        raise ValueError("Stage 1 proof or Admission payload is not strict closed content") from error
    source = scope
    by_type = {item.artifact_type: identity for item, identity in zip(members, identities, strict=True)}
    if proof.source_member_ref.scope != source:
        raise ValueError("dependency proof source scope differs from Stage 1 scope")
    if (
        proof.graph_member_ref != by_type["narrative_graph"]
        or proof.event_card_member_ref != by_type["event_card_set"]
        or proof.ledger_member_ref != by_type["coverage_ledger"]
    ):
        raise ValueError("dependency proof does not bind exact Graph/Card/Ledger identities")
    if tuple(sorted(admission.business_members, key=lambda item: canonical_json_bytes(item.to_mapping()))) != tuple(
        sorted(identities[:7], key=lambda item: canonical_json_bytes(item.to_mapping()))
    ):
        raise ValueError("Admission subject does not bind exact seven business identities")
    ledger = coverage.coverage_ledger
    evidence, conflicts = coverage.evidence_diagnostics, coverage.conflict_diagnostics
    if (
        proof.input_binding_sha256 != ledger.input_binding_sha256
        or admission.input_binding_sha256 != ledger.input_binding_sha256
        or evidence.input_binding_sha256 != ledger.input_binding_sha256
        or conflicts.input_binding_sha256 != ledger.input_binding_sha256
    ):
        raise ValueError("Stage 1 input binding differs across members")
    if (
        proof.canonical_draft_sha256 != ledger.draft_sha256
        or admission.canonical_draft_sha256 != ledger.draft_sha256
        or evidence.canonical_draft_sha256 != ledger.draft_sha256
        or conflicts.canonical_draft_sha256 != ledger.draft_sha256
    ):
        raise ValueError("Stage 1 canonical draft binding differs across members")
    if evidence.raw_draft_sha256 != conflicts.raw_draft_sha256 or admission.raw_draft_sha256 != evidence.raw_draft_sha256:
        raise ValueError("Stage 1 raw draft binding differs across members")
    if (
        proof.coverage_policy_sha256 != ledger.coverage_policy_sha256
        or admission.coverage_policy_sha256 != ledger.coverage_policy_sha256
        or admission.dependency_policy_sha256 != proof.dependency_policy_sha256
    ):
        raise ValueError("Stage 1 policy binding differs across members")
    return Stage1Values(members, coverage, proof, admission)
