"""Stage 2 four-member business compilation, without persistence or Admission.

Only an independently evaluated Command may commit these pending values. A
failed/unfinished search retains proposal diagnostics but returns no business
set, no target IDs and no partial Portfolio.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.compiler.canonical import canonical_json_bytes
from ..store.models import ArtifactMember, ArtifactScope, CommittedSemanticInputs
from .candidate_catalog import CandidateCatalogPolicy
from .candidate_projection import CandidateCatalogProjection, project_candidate_catalog
from .material_support import evaluate_material_support
from .material_support_models import MaterialSupportEvaluation
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .portfolio_search import (
    CandidateAlternative,
    PortfolioSearchResult,
    ProposalAlternatives,
    RequirementAlternatives,
    search_portfolio,
    verify_assignment,
)
from .portfolio_values import InitialSourceUsageLedger, StoryPortfolio, StorySelection
from .stage1_result import Stage1Values
from .story_design_context import story_design_input_binding
from .story_design_draft import StoryDesignDraftPolicy, decode_story_design_draft
from .story_design_models import JobPolicy, StoryDesignPolicy

STAGE2_BUSINESS_MEMBER_TYPES = (
    "candidate_catalog", "proposal_set", "portfolio", "source_usage_ledger",
)


def material_search_universe(support: MaterialSupportEvaluation) -> tuple[ProposalAlternatives, ...]:
    """Preserve every original index; tainted proposals have no eligible edges.

    This is a projection of supplied evidence, not a verifier of that evidence.
    Known unsupported proposals remain in place, never renumbered or dropped.
    """
    if type(support) is not MaterialSupportEvaluation:  # noqa: E721
        raise ValueError("search projection needs exact material evidence")
    return tuple(ProposalAlternatives(
        row.proposal_index, row.proposal.proposal_id,
        tuple(RequirementAlternatives(requirement.requirement_id, tuple(
            CandidateAlternative(item.candidate_ref, item.source_ref)
            for item in requirement.alternatives
        ) if not row.narrative_taint_seed_refs and not row.dependency_unknown else ())
              for requirement in row.requirements),
    ) for row in support.proposals)


def select_material_portfolio(
    support: MaterialSupportEvaluation, *, job_policy: JobPolicy,
) -> PortfolioSearchResult:
    """Exact known universe search; unresolved material cannot be silently skipped.

    The first strategy requires all potentially eligible proposals to have a
    closed dependency/material decision before search. A proven unsupported or
    narrative-tainted proposal cannot win regardless of its other unknowns.
    """
    if type(support) is not MaterialSupportEvaluation or type(job_policy) is not JobPolicy:  # noqa: E721
        raise ValueError("selection requires exact material/policy values")
    count = len(support.proposals)
    if not job_policy.proposal_count.minimum <= count <= job_policy.proposal_count.maximum or count < job_policy.selected_story_count:
        raise ValueError("selection must preserve the declared proposal and target counts")
    if any(not row.narrative_taint_seed_refs and row.status != "unsupported"
           and (row.dependency_unknown or row.status == "indeterminate")
           for row in support.proposals):
        return PortfolioSearchResult("indeterminate", (), (), 0)
    universe = material_search_universe(support)
    result = search_portfolio(
        universe, selected_story_count=job_policy.selected_story_count,
        source_reuse=job_policy.source_reuse_policy, max_search_states=job_policy.max_search_states,
    )
    if result.status == "feasible":
        verify_assignment(universe, result.proposal_indexes, result.assignment,
                          selected_story_count=job_policy.selected_story_count,
                          source_reuse=job_policy.source_reuse_policy)
    return result


def _member(kind: str, payload: dict[str, object], *, scope: ArtifactScope, revision: int) -> ArtifactMember:
    from ..store.models import canonical_payload_hash

    raw = canonical_json_bytes(payload).decode("utf-8")
    return ArtifactMember(kind, kind, revision, scope, canonical_payload_hash(raw), raw)


@dataclass(frozen=True, slots=True)
class StoryDesignCompilation:
    projection: CandidateCatalogProjection
    proposal_set: MaterialSupportEvaluation
    search: PortfolioSearchResult
    business_members: tuple[ArtifactMember, ...]


def compose_story_design_members(
    projection: CandidateCatalogProjection, support: MaterialSupportEvaluation, *,
    job_policy: JobPolicy,
) -> StoryDesignCompilation:
    """Compose supplied pure values, not a second authority entry point."""
    if type(projection) is not CandidateCatalogProjection or type(support) is not MaterialSupportEvaluation:  # noqa: E721
        raise ValueError("business composition requires exact candidate/material values")
    catalog_ref = SemanticMemberIdentity.from_artifact_member(projection.member)
    if (catalog_ref != support.candidate_catalog_ref
            or catalog_ref.content_hash != projection.catalog.canonical_hash
            or projection.catalog.source_grant_sha256 != support.source_grant_sha256):
        raise ValueError("ProposalSet does not bind this exact CandidateCatalog/grant")
    search = select_material_portfolio(support, job_policy=job_policy)
    if search.status != "feasible":
        return StoryDesignCompilation(projection, support, search, ())
    proposal_member = _member("proposal_set", support.to_mapping(), scope=catalog_ref.scope,
                              revision=catalog_ref.revision)
    proposal_ref = SemanticMemberIdentity.from_artifact_member(proposal_member)
    selections = tuple(StorySelection(index, SemanticObjectRef(
        proposal_ref, "proposal", support.proposals[index].proposal.proposal_id,
    )) for index in search.proposal_indexes)
    portfolio = StoryPortfolio(proposal_ref, job_policy.canonical_hash, selections,
                               search.assignment, search.visited_states)
    portfolio_member = _member("portfolio", portfolio.to_mapping(), scope=catalog_ref.scope,
                               revision=catalog_ref.revision)
    usage = InitialSourceUsageLedger(SemanticMemberIdentity.from_artifact_member(portfolio_member),
                                     portfolio.target_story_ids)
    usage_member = _member("source_usage_ledger", usage.to_mapping(), scope=catalog_ref.scope,
                           revision=catalog_ref.revision)
    return StoryDesignCompilation(projection, support, search,
                                   (projection.member, proposal_member, portfolio_member, usage_member))


def compile_story_design(
    inputs: CommittedSemanticInputs, stage1: Stage1Values, raw_draft: bytes, *,
    scope: ArtifactScope, revision: int, candidate_policy: CandidateCatalogPolicy,
    job_policy: JobPolicy, story_policy: StoryDesignPolicy, draft_policy: StoryDesignDraftPolicy,
) -> StoryDesignCompilation:
    """Rebuild candidate/support evidence, decode the bound raw draft, then select.

    Raw audit durability and predecessor Store verification belong to Command.
    This function never invokes a provider or consumes physical media evidence.
    """
    projection = project_candidate_catalog(inputs, stage1, scope=scope, revision=revision,
                                           policy=candidate_policy)
    binding = story_design_input_binding(stage1, projection, job_policy=job_policy,
                                         story_policy=story_policy, candidate_policy=candidate_policy)
    draft = decode_story_design_draft(raw_draft, expected_input_binding_sha256=binding,
                                      policy=draft_policy)
    support = evaluate_material_support(inputs, stage1, projection, draft, job_policy=job_policy,
                                         story_policy=story_policy, candidate_policy=candidate_policy)
    return compose_story_design_members(projection, support, job_policy=job_policy)
