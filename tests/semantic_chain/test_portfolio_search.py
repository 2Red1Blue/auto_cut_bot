"""Small-space product oracle deliberately does not share solver traversal."""

from itertools import combinations, product

import pytest
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.portfolio_search import (
    CandidateAlternative,
    PortfolioSearchError,
    ProposalAlternatives,
    RequirementAlternatives,
    search_portfolio,
    verify_assignment,
)
from autocut_kernel.store.models import ArtifactScope


def ref(kind: str, name: str) -> SemanticObjectRef:
    artifact_type = "candidate_catalog" if kind == "candidate" else "whole_series_source_manifest"
    return SemanticObjectRef(
        SemanticMemberIdentity(artifact_type, "test", 1, ArtifactScope("test", "job", "j"), "sha256:" + "a" * 64),
        kind, name,
    )


def proposal(index: int, *sources: str) -> ProposalAlternatives:
    return ProposalAlternatives(index, f"p{index}", tuple(
        RequirementAlternatives(f"r{ri}", tuple(
            CandidateAlternative(ref("candidate", source), ref("source", source))
            for source in chars
        )) for ri, chars in enumerate(sources)
    ))


def oracle(proposals: tuple[ProposalAlternatives, ...], count: int, reuse: str):
    for indexes in combinations(range(len(proposals)), count):
        rows = [(pi, req) for pi in indexes for req in proposals[pi].requirements]
        for choices in product(*(req.alternatives for _, req in rows)):
            owners = {}
            for (pi, _), choice in zip(rows, choices, strict=True):
                owners.setdefault(choice.source_ref, set()).add(pi)
            if reuse == "allow" or all(len(value) == 1 for value in owners.values()):
                return indexes, tuple((pi, req.requirement_id, alt) for (pi, req), alt in zip(rows, choices, strict=True))
    return None


@pytest.mark.parametrize(("rows", "count", "reuse", "expected"), [
    ((proposal(0, "X", "X"),), 1, "forbid", (0,)),
    ((proposal(0, "X"), proposal(1, "X")), 2, "forbid", None),
    ((proposal(0, "XY"), proposal(1, "X")), 2, "forbid", (0, 1)),
    ((proposal(0, "XY"), proposal(1, "XY"), proposal(2, "XY")), 3, "forbid", None),
    ((proposal(0, "X", "Y"), proposal(1, "XY")), 2, "forbid", None),
    ((proposal(0, "X"), proposal(1, "X"), proposal(2, "Y")), 2, "forbid", (0, 2)),
    ((proposal(0, ""), proposal(1, "Y")), 1, "forbid", (1,)),
    ((proposal(0, "X"), proposal(1, "X")), 2, "allow", (0, 1)),
])
def test_regressions(rows, count, reuse, expected):
    result = search_portfolio(rows, selected_story_count=count, source_reuse=reuse, max_search_states=1000)
    assert result.status == ("feasible" if expected is not None else "infeasible")
    assert result.proposal_indexes == (() if expected is None else expected)
    if expected is not None:
        verify_assignment(rows, result.proposal_indexes, result.assignment, selected_story_count=count, source_reuse=reuse)
        wanted = oracle(rows, count, reuse)
        assert wanted is not None
        assert tuple((item.proposal_index, item.requirement_id, item.alternative) for item in result.assignment) == wanted[1]


def test_small_domain_matches_independent_product_oracle():
    # 12 row patterns, 1728 three-story universes, both reuse policies.
    patterns = [(value,) for value in ("X", "Y", "XY")]
    patterns += list(product(("X", "Y", "XY"), repeat=2))
    for selected in product(patterns, repeat=3):
        rows = tuple(proposal(i, *values) for i, values in enumerate(selected))
        for reuse in ("allow", "forbid"):
            wanted = oracle(rows, 2, reuse)
            result = search_portfolio(rows, selected_story_count=2, source_reuse=reuse, max_search_states=1000)
            if wanted is None:
                assert result.status == "infeasible"
            else:
                assert result.status == "feasible"
                assert result.proposal_indexes == wanted[0]
                assert tuple((item.proposal_index, item.requirement_id, item.alternative) for item in result.assignment) == wanted[1]


def test_budget_is_exact_deterministic_and_never_skips_unfinished_tuple():
    rows = (proposal(0, "X"), proposal(1, "X"), proposal(2, "Y"))
    full = search_portfolio(rows, selected_story_count=2, source_reuse="forbid", max_search_states=1000)
    assert full.visited_states == 6  # two tuples + two inspected edges per tuple
    for budget in range(1, full.visited_states):
        result = search_portfolio(rows, selected_story_count=2, source_reuse="forbid", max_search_states=budget)
        assert result.status == "indeterminate"
        assert result.visited_states == budget
        assert result.proposal_indexes == result.assignment == ()
    exact = search_portfolio(rows, selected_story_count=2, source_reuse="forbid", max_search_states=6)
    assert exact == full
    assert search_portfolio((proposal(0, ""),), selected_story_count=1, source_reuse="forbid", max_search_states=1).status == "infeasible"


def test_iterative_solver_handles_long_requirement_chain():
    rows = (proposal(0, *("X" for _ in range(1500))),)
    result = search_portfolio(rows, selected_story_count=1, source_reuse="forbid", max_search_states=1501)
    assert result.status == "feasible"
    assert len(result.assignment) == 1500


def test_witness_checker_rejects_partial_or_cross_story_duplicate():
    rows = (proposal(0, "X"), proposal(1, "X"))
    result = search_portfolio(rows, selected_story_count=2, source_reuse="allow", max_search_states=10)
    with pytest.raises(PortfolioSearchError):
        verify_assignment(rows, result.proposal_indexes, result.assignment[:-1], selected_story_count=2, source_reuse="allow")
    with pytest.raises(PortfolioSearchError):
        verify_assignment(rows, result.proposal_indexes, result.assignment, selected_story_count=2, source_reuse="forbid")


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1.0, 2**53])
def test_search_rejects_invalid_budget(bad):
    with pytest.raises(PortfolioSearchError):
        search_portfolio((proposal(0, "X"),), selected_story_count=1, source_reuse="allow", max_search_states=bad)


def test_closed_input_identity_and_tuple_validation():
    with pytest.raises(PortfolioSearchError):
        RequirementAlternatives("r", list(proposal(0, "X").requirements[0].alternatives))
    with pytest.raises(PortfolioSearchError):
        RequirementAlternatives("r", proposal(0, "XX").requirements[0].alternatives)
    with pytest.raises(PortfolioSearchError):
        proposal(0, "YX")
    with pytest.raises(PortfolioSearchError):
        search_portfolio((proposal(1, "X"),), selected_story_count=1, source_reuse="allow", max_search_states=10)
    with pytest.raises(PortfolioSearchError):
        search_portfolio((proposal(0, "X"),), selected_story_count=True, source_reuse="allow", max_search_states=10)
    with pytest.raises(PortfolioSearchError):
        CandidateAlternative(ref("source", "X"), ref("source", "X"))
