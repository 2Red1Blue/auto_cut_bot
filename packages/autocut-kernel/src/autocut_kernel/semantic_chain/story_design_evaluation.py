"""Independently reconstruct Stage 2 business evidence from exact inputs/raw.

No producer compilation/result/check map is accepted. SD-IN-001/002 belong to
the Command after durable predecessor, registry and invocation reads. This
pure evaluator cannot turn an in-memory DTO into Store authority.
"""

from __future__ import annotations

from ..store.models import ArtifactMember, CommittedSemanticInputs
from .candidate_catalog import CandidateCatalogPolicy
from .candidate_projection import project_candidate_catalog
from .material_support import evaluate_material_support
from .material_support_models import MaterialSupportEvaluation
from .member_refs import SemanticObjectRef
from .portfolio_admission import SD_RULE_IDS, Stage2Check
from .portfolio_search import PortfolioSearchError, verify_assignment
from .stage1_result import Stage1Values
from .story_design_boundary import decode_bound_story_draft
from .story_design_compiler import material_search_universe, select_material_portfolio
from .story_design_draft import StoryDesignDraftPolicy
from .story_design_members import decode_story_design_business_members
from .story_design_models import JobPolicy, StoryDesignPolicy
from .story_design_validation import validate_story_proposals


def _check(rule: str, condition: bool, code: str) -> Stage2Check:
    # Each call follows the named actual computation below. There is no default
    # pass map or list of unexecuted rules filled on success.
    return Stage2Check(rule, "pass" if condition else "fail", () if condition else (code,))


def _taint_evidence(support: MaterialSupportEvaluation) -> tuple[object, ...]:
    return tuple((row.proposal_index, row.proposal.narrative_refs, row.narrative_taint_seed_refs,
                  row.dependency_unknown, tuple((requirement.requirement_id,
                   requirement.excluded_tainted_candidate_refs,
                   tuple(item.candidate_ref for item in requirement.alternatives))
                  for requirement in row.requirements)) for row in support.proposals)


def evaluate_story_design_business_members(
    inputs: CommittedSemanticInputs, stage1: Stage1Values, raw_draft: bytes, *,
    members: tuple[ArtifactMember, ...], candidate_policy: CandidateCatalogPolicy,
    job_policy: JobPolicy, story_policy: StoryDesignPolicy, draft_policy: StoryDesignDraftPolicy,
    prompt_version: str | None = None,
) -> tuple[Stage2Check, ...]:
    """Perform all seventeen business checks; unreadable content raises ValueError.

    Rebuild the projection and every eligible alternative, revalidate the raw
    proposals, independently check the selected assignment, then repeat the
    exact bounded canonical search. Never approve merely because a Portfolio
    claims it is supported, or because its assignment is locally feasible.
    """
    if type(inputs) is not CommittedSemanticInputs or type(stage1) is not Stage1Values:  # noqa: E721
        raise ValueError("Stage 2 evaluation requires exact typed predecessor content")
    scope = inputs.source_manifest.reference.scope
    values = decode_story_design_business_members(members, scope=scope)
    expected_projection = project_candidate_catalog(inputs, stage1, scope=scope,
                                                    revision=members[0].revision, policy=candidate_policy)
    draft = decode_bound_story_draft(
        inputs, stage1, expected_projection, raw_draft, job_policy=job_policy,
        story_policy=story_policy, candidate_policy=candidate_policy,
        draft_policy=draft_policy, prompt_version=prompt_version,
    )
    graph = stage1.coverage.narrative_graph
    graph_ref = stage1.coverage.identity("narrative_graph")
    source_owner = stage1.dependency_proof.source_member_ref
    graph_refs = tuple(SemanticObjectRef(graph_ref, node.node_type, node.node_id) for node in graph.nodes)
    source_refs = tuple(SemanticObjectRef(source_owner, "source", source.source_id) for source in inputs.source_grant.sources)
    # This performs REF/ENUM/DUR/PHYS validation over the original provider
    # draft, not a caller's rewritten ProposalSet. Invalid drafts are not
    # evaluatable business sets and never yield partially populated checks.
    validate_story_proposals(draft, graph=graph, graph_object_refs=graph_refs, source_refs=source_refs,
                              job_policy=job_policy, story_policy=story_policy)
    expected = evaluate_material_support(inputs, stage1, expected_projection, draft,
                                          job_policy=job_policy, story_policy=story_policy,
                                          candidate_policy=candidate_policy)
    actual_proposals = tuple(row.proposal for row in values.proposal_set.proposals)
    original_retained = actual_proposals == draft.proposals
    graph_universe = set(graph_refs)
    actual_refs = tuple(ref for proposal in actual_proposals for ref in proposal.narrative_refs)
    profiles = set(story_policy.editing_profiles)
    genres, teasers = set(story_policy.allowed_genre_tags), set(story_policy.teaser_strategies)
    actual_catalog, expected_catalog = values.candidate_catalog, expected_projection.catalog
    semantic_match = actual_catalog == expected_catalog
    actual_capabilities = tuple((candidate.candidate_id, candidate.editing_modes,
                                candidate.narrative_functions, candidate.measurements, candidate.support.confidence)
                               for candidate in actual_catalog.candidates)
    expected_capabilities = tuple((candidate.candidate_id, candidate.editing_modes,
                                  candidate.narrative_functions, candidate.measurements, candidate.support.confidence)
                                 for candidate in expected_catalog.candidates)
    physical_preserved = original_retained and all(
        tuple(requirement.physical_requirements_hash for requirement in row.requirements)
        == tuple(requirement.physical_requirements_hash for requirement in raw.material_requirements)
        for row, raw in zip(values.proposal_set.proposals, draft.proposals, strict=True)
    )
    portfolio = values.portfolio
    indexes = tuple(item.proposal_index for item in portfolio.selections)
    selected_expected = tuple(expected.proposals[index] for index in indexes) if all(
        index < len(expected.proposals) for index in indexes
    ) else ()
    universe = material_search_universe(expected)
    assignment_valid = True
    try:
        verify_assignment(universe, indexes, portfolio.requirement_assignments,
                          selected_story_count=job_policy.selected_story_count,
                          source_reuse=job_policy.source_reuse_policy)
    except PortfolioSearchError:
        assignment_valid = False
    search = select_material_portfolio(expected, job_policy=job_policy)
    feasible = Stage2Check("SD-PORT-003", "indeterminate", ("search_or_material_unresolved",)) if search.status == "indeterminate" else _check(
        "SD-PORT-003", search.status == "feasible", "no_feasible_portfolio",
    )
    results = (
        _check("SD-PROP-001", original_retained and job_policy.proposal_count.minimum <= len(actual_proposals) <= job_policy.proposal_count.maximum, "original_proposal_count_or_content_changed"),
        _check("SD-REF-001", original_retained and all(ref in graph_universe for ref in actual_refs), "narrative_reference_changed"),
        _check("SD-ENUM-001", all(proposal.editing_profile in profiles and set(proposal.genre_tags) <= genres and proposal.teaser_strategy in teasers for proposal in actual_proposals), "editorial_value_outside_policy"),
        _check("SD-DUR-001", all(job_policy.target_duration_seconds.minimum <= proposal.target_duration_seconds.minimum <= proposal.target_duration_seconds.maximum <= job_policy.target_duration_seconds.maximum for proposal in actual_proposals), "duration_outside_policy"),
        _check("SD-MAT-001", values.proposal_set == expected, "material_evidence_differs_from_recomputation"),
        _check("SD-MAT-002", len(selected_expected) == len(indexes) and all(row.status == "supported" and not row.dependency_unknown for row in selected_expected), "selected_material_not_supported"),
        _check("SD-PHYS-DEFER-001", physical_preserved, "physical_requirements_changed"),
        _check("SD-CAND-SEM-001", semantic_match, "candidate_projection_differs_from_committed_vlm"),
        _check("SD-CAND-CAP-001", actual_capabilities == expected_capabilities and actual_catalog.policy_sha256 == candidate_policy.canonical_hash, "candidate_capabilities_differ_from_bound_vlm_policy"),
        _check("SD-TAINT-001", len(selected_expected) == len(indexes) and all(not row.narrative_taint_seed_refs and not row.dependency_unknown for row in selected_expected), "selected_dependencies_not_clean"),
        _check("SD-TAINT-002", _taint_evidence(values.proposal_set) == _taint_evidence(expected), "taint_or_safe_candidates_differ_from_recomputation"),
        _check("SD-PORT-001", len(indexes) == job_policy.selected_story_count, "selected_story_count_changed"),
        _check("SD-PORT-002", assignment_valid, "assignment_violates_exact_material_or_reuse"),
        feasible,
        _check("SD-OBJ-001", search.status == "feasible" and indexes == search.proposal_indexes and portfolio.requirement_assignments == search.assignment and portfolio.visited_states == search.visited_states, "selection_is_not_canonical_first_feasible"),
        _check("SD-FREEZE-001", portfolio.job_policy_sha256 == job_policy.canonical_hash and portfolio.target_story_ids == tuple(item.story_id for item in portfolio.selections), "target_or_job_policy_binding_changed"),
        _check("SD-USAGE-001", values.source_usage_ledger.target_story_ids == portfolio.target_story_ids, "initial_usage_targets_changed"),
    )
    expected_ids = set(SD_RULE_IDS) - {"SD-IN-001", "SD-IN-002"}
    if len(results) != len(expected_ids) or {result.rule_id for result in results} != expected_ids:
        raise ValueError("Stage 2 evaluator did not perform all seventeen business rules")
    return tuple(sorted(results, key=lambda result: result.rule_id))
