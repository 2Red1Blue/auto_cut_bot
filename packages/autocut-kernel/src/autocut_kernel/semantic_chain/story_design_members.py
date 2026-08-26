"""Exact four-business-member decoding; structural closure is not Admission."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.compiler.canonical import load_canonical_json_bytes
from ..store.models import ArtifactMember, ArtifactScope
from .candidate_catalog import CandidateCatalog
from .material_support_models import MaterialSupportEvaluation
from .member_refs import SemanticMemberIdentity
from .portfolio_values import InitialSourceUsageLedger, StoryPortfolio
from .story_design_compiler import STAGE2_BUSINESS_MEMBER_TYPES


@dataclass(frozen=True, slots=True)
class StoryDesignBusinessValues:
    members: tuple[ArtifactMember, ...]
    candidate_catalog: CandidateCatalog
    proposal_set: MaterialSupportEvaluation
    portfolio: StoryPortfolio
    source_usage_ledger: InitialSourceUsageLedger


def decode_story_design_business_members(
    members: tuple[ArtifactMember, ...], *, scope: ArtifactScope,
) -> StoryDesignBusinessValues:
    """Validate hashes, closed payloads and full member/object joins, not truth.

    The external evidence, policy, canonical search and generation audit still
    require independent recomputation. No supplied Admission is read here.
    """
    if type(members) is not tuple or any(type(item) is not ArtifactMember for item in members):  # noqa: E721
        raise ValueError("Stage 2 business members must be exact ArtifactMember values")
    if tuple(member.artifact_type for member in members) != STAGE2_BUSINESS_MEMBER_TYPES:
        raise ValueError("Stage 2 requires all four business members in canonical order")
    if type(scope) is not ArtifactScope or any(member.scope != scope or member.logical_id != member.artifact_type for member in members):  # noqa: E721
        raise ValueError("Stage 2 business scope/logical identity mismatch")
    if len({member.revision for member in members}) != 1:
        raise ValueError("Stage 2 business revisions differ")
    identities = tuple(SemanticMemberIdentity.from_artifact_member(member) for member in members)
    payloads = tuple(load_canonical_json_bytes(member.payload_json.encode("utf-8"), origin=member.artifact_type)[0]
                     for member in members)
    catalog = CandidateCatalog.from_mapping(payloads[0])
    proposals = MaterialSupportEvaluation.from_mapping(payloads[1])
    portfolio = StoryPortfolio.from_mapping(payloads[2])
    usage = InitialSourceUsageLedger.from_mapping(payloads[3])
    if catalog.narrative_graph_member_ref.scope != scope:
        raise ValueError("Stage 2 catalog predecessor scope differs from output scope")
    if (proposals.candidate_catalog_ref != identities[0]
            or proposals.source_grant_sha256 != catalog.source_grant_sha256
            or portfolio.proposal_set_ref != identities[1]
            or usage.portfolio_ref != identities[2]
            or usage.target_story_ids != portfolio.target_story_ids):
        raise ValueError("Stage 2 member DAG/targets do not bind exact predecessors")
    candidates = {item.candidate_id: item for item in catalog.candidates}
    for proposal in proposals.proposals:
        if any(ref.member_ref != catalog.narrative_graph_member_ref for ref in proposal.proposal.narrative_refs):
            raise ValueError("ProposalSet narrative references a different Graph")
        if any(ref.member_ref != catalog.coverage_ledger_member_ref for ref in proposal.narrative_taint_seed_refs):
            raise ValueError("ProposalSet taint refers to a different Ledger")
        for requirement in proposal.requirements:
            if requirement.examined_candidate_count != len(candidates):
                raise ValueError("material counts do not cover the full Catalog")
            for ref in requirement.excluded_tainted_candidate_refs:
                if ref.object_id not in candidates:
                    raise ValueError("taint exclusion names an unknown Candidate")
            for alternative in requirement.alternatives:
                candidate = candidates.get(alternative.candidate_ref.object_id)
                if (candidate is None or alternative.source_ref != candidate.source_ref
                        or alternative.conservative_duration != candidate.support.conservative_duration):
                    raise ValueError("material alternative differs from Catalog source/duration")
                for fact in alternative.fact_witnesses:
                    if (fact.vlm_fact_ref.member_ref != candidate.candidate_ref.member_ref
                            or any(ref.member_ref != catalog.event_card_member_ref for ref in fact.via_event_refs)):
                        raise ValueError("material witness names a foreign raw Fact/Event owner")
                    direct_events = {event.event_card_ref for event in (
                        candidate.anchor_event, *candidate.supporting_events, *candidate.payoff_events,
                    )}
                    if not set(fact.via_event_refs) <= direct_events:
                        raise ValueError("material witness uses an undeclared or context-only Event")
    expected_assignments: list[tuple[int, str]] = []
    for selected in portfolio.selections:
        if not 0 <= selected.proposal_index < len(proposals.proposals):
            raise ValueError("selection index is outside the original ProposalSet")
        proposal = proposals.proposals[selected.proposal_index]
        if (selected.proposal_ref.object_id != proposal.proposal.proposal_id
                or proposal.status != "supported" or proposal.narrative_taint_seed_refs
                or proposal.dependency_unknown):
            raise ValueError("selection is not the exact supported untainted proposal")
        expected_assignments.extend((selected.proposal_index, row.requirement_id) for row in proposal.requirements)
    if tuple(expected_assignments) != tuple((row.proposal_index, row.requirement_id) for row in portfolio.requirement_assignments):
        raise ValueError("selection assignments omit/reorder original requirements")
    for assignment in portfolio.requirement_assignments:
        proposal = proposals.proposals[assignment.proposal_index]
        requirement = next(row for row in proposal.requirements if row.requirement_id == assignment.requirement_id)
        if not any(assignment.alternative.candidate_ref == row.candidate_ref
                   and assignment.alternative.source_ref == row.source_ref for row in requirement.alternatives):
            raise ValueError("assignment is not a supported requirement alternative")
    return StoryDesignBusinessValues(members, catalog, proposals, portfolio, usage)
