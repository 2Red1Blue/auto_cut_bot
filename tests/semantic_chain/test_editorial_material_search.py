"""Small exhaustive oracle and hostile witnesses for whole-batch subset search."""

from __future__ import annotations

from dataclasses import replace
from itertools import combinations, product

import autocut_kernel.semantic_chain.editorial_material_search as search
import pytest
from autocut_kernel.semantic_chain.editorial_material_search import (
    MaterialSearchAlternative as Alternative,
)
from autocut_kernel.semantic_chain.editorial_material_search import (
    MaterialSearchCandidate as Candidate,
)
from autocut_kernel.semantic_chain.editorial_material_search import (
    MaterialSearchChoice as Choice,
)
from autocut_kernel.semantic_chain.editorial_material_search import (
    MaterialSearchRequirement as Requirement,
)
from autocut_kernel.semantic_chain.editorial_material_search import (
    MaterialSearchResult as Result,
)


def alternative(key="alt", candidates=None, events=("e1", "e2")):
    if candidates is None:
        candidates = (Candidate("a", "s1", ("e1",)), Candidate("b", "s2", ("e2",)))
    return Alternative(key, events, candidates)


def requirement(story="story1", key="req1", satisfaction="one_of", alternatives=None):
    return Requirement(story, key, satisfaction, alternatives or (alternative(),))


def test_subset_not_single_candidate_and_exact_budget():
    universe = (requirement(),)
    unknown = search.search_editorial_materials(universe, source_reuse="allow", max_search_states=2)
    assert unknown == Result("indeterminate", (), 2)
    actual = search.search_editorial_materials(universe, source_reuse="allow", max_search_states=3)
    assert actual == Result("feasible", (Choice("story1", "req1", "alt", ("a", "b")),), 3)
    search.verify_editorial_material_assignment(universe, actual.choices, source_reuse="allow")


def test_no_stitching_half_of_different_alternatives():
    universe = (requirement(alternatives=(
        alternative("a", (Candidate("a", "s1", ("e1",)),)),
        alternative("b", (Candidate("b", "s2", ("e2",)),)),
    )),)
    assert search.search_editorial_materials(universe, source_reuse="allow", max_search_states=2) == Result("infeasible", (), 2)
    with pytest.raises(ValueError, match="outside"):
        search.verify_editorial_material_assignment(universe, (Choice("story1", "req1", "a", ("a", "b")),), source_reuse="allow")


def test_all_of_requires_each_alternative():
    alternatives = (
        alternative("a", (Candidate("a", "s1", ("e1",)),), ("e1",)),
        alternative("b", (Candidate("b", "s2", ("e2",)),), ("e2",)),
    )
    universe = (requirement(satisfaction="all_of", alternatives=alternatives),)
    actual = search.search_editorial_materials(universe, source_reuse="forbid", max_search_states=2)
    assert [choice.alternative_key for choice in actual.choices] == ["a", "b"]
    search.verify_editorial_material_assignment(universe, actual.choices, source_reuse="forbid")
    with pytest.raises(ValueError):
        search.verify_editorial_material_assignment(universe, actual.choices[:1], source_reuse="forbid")


def test_joint_source_conflict_backtracks_not_per_story_success():
    first = requirement(alternatives=(alternative(candidates=(
        Candidate("a", "source1", ("e1", "e2")), Candidate("b", "source2", ("e1", "e2")),
    )),))
    second = requirement("story2", alternatives=(alternative(candidates=(Candidate("c", "source1", ("e1", "e2")),)),))
    universe = (first, second)
    unknown = search.search_editorial_materials(universe, source_reuse="forbid", max_search_states=3)
    assert unknown == Result("indeterminate", (), 3)
    actual = search.search_editorial_materials(universe, source_reuse="forbid", max_search_states=4)
    assert tuple(row.candidate_keys for row in actual.choices) == (("b",), ("c",))
    search.verify_editorial_material_assignment(universe, actual.choices, source_reuse="forbid")
    allowed = search.search_editorial_materials(universe, source_reuse="allow", max_search_states=2)
    assert tuple(row.candidate_keys for row in allowed.choices) == (("a",), ("c",))
    with pytest.raises(ValueError, match="across Stories"):
        search.verify_editorial_material_assignment(universe, allowed.choices, source_reuse="forbid")


def test_same_source_repeated_in_same_story_and_restored_on_backtrack():
    common = (alternative(candidates=(Candidate("a", "s1", ("e1", "e2")),)),)
    universe = (requirement(alternatives=common), requirement(key="req2", alternatives=common),
                requirement("story2", alternatives=common))
    assert search.search_editorial_materials(universe[:2], source_reuse="forbid", max_search_states=2).status == "feasible"
    assert search.search_editorial_materials(universe, source_reuse="forbid", max_search_states=3) == Result("infeasible", (), 3)


def test_search_is_lazy_bounded_and_not_recursive():
    pool = tuple(Candidate(f"c{i:04}", "source", ()) for i in range(2000))
    universe = (requirement(alternatives=(alternative(candidates=pool),)),)
    assert search.search_editorial_materials(universe, source_reuse="allow", max_search_states=2) == Result("indeterminate", (), 2)
    one = (alternative(candidates=(Candidate("a", "source", ("e1", "e2")),)),)
    deep = tuple(requirement(key=f"req{i}", alternatives=one) for i in range(1500))
    result = search.search_editorial_materials(deep, source_reuse="forbid", max_search_states=1500)
    assert result.status == "feasible" and len(result.choices) == 1500
    search.verify_editorial_material_assignment(deep, result.choices, source_reuse="forbid")


def test_verifier_never_calls_search(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("search must not implement verification")
    monkeypatch.setattr(search, "search_editorial_materials", forbidden)
    monkeypatch.setattr(search, "_options", forbidden)
    universe = (requirement(),)
    search.verify_editorial_material_assignment(universe, (Choice("story1", "req1", "alt", ("a", "b")),), source_reuse="allow")
    with pytest.raises(ValueError, match="complete alternative"):
        search.verify_editorial_material_assignment(universe, (Choice("story1", "req1", "alt", ("a",)),), source_reuse="allow")


@pytest.mark.parametrize("mutation", ["missing", "extra", "story", "requirement", "alternative", "candidate", "reorder"])
def test_witness_tampering(mutation):
    universe = (requirement(), requirement("story2"))
    choices = (Choice("story1", "req1", "alt", ("a", "b")), Choice("story2", "req1", "alt", ("a", "b")))
    if mutation == "missing":
        choices = choices[:1]
    elif mutation == "extra":
        choices += choices[:1]
    elif mutation == "reorder":
        choices = tuple(reversed(choices))
    else:
        changes = {"story": {"story_id": "other"}, "requirement": {"requirement_id": "other"},
                   "alternative": {"alternative_key": "other"}, "candidate": {"candidate_keys": ("other",)}}
        choices = (replace(choices[0], **changes[mutation]), choices[1])
    with pytest.raises(ValueError):
        search.verify_editorial_material_assignment(universe, choices, source_reuse="allow")


@pytest.mark.parametrize("kind", [Candidate, Alternative, Requirement, Choice, Result])
def test_closed_wire_roundtrip(kind):
    values = {Candidate: Candidate("a", "s", ("e",)), Alternative: alternative(), Requirement: requirement(),
              Choice: Choice("s", "r", "a", ("c",)), Result: Result("indeterminate", (), 1)}
    value = values[kind]
    assert kind.from_mapping(value.to_mapping()) == value
    with pytest.raises(ValueError):
        kind.from_mapping({**value.to_mapping(), "pass": True})


@pytest.mark.parametrize("bad", [True, 0, -1, 1.5, 2**53])
def test_search_budget_is_exact_positive_safe_integer(bad):
    with pytest.raises(ValueError):
        search.search_editorial_materials((requirement(),), source_reuse="allow", max_search_states=bad)


def test_conflicting_candidate_source_and_malformed_universe():
    one = requirement(alternatives=(alternative(candidates=(Candidate("a", "s1", ("e1", "e2")),)),))
    two = requirement("story2", alternatives=(alternative(candidates=(Candidate("a", "s2", ("e1", "e2")),)),))
    with pytest.raises(ValueError, match="different Sources"):
        search.search_editorial_materials((one, two), source_reuse="allow", max_search_states=5)
    for universe in ((), (one, one), (one, replace(one, story_id="story2"), replace(one, requirement_id="r2"))):
        with pytest.raises(ValueError):
            search.search_editorial_materials(universe, source_reuse="allow", max_search_states=5)
    with pytest.raises(ValueError):
        Candidate("a", "s", ("e2", "e1"))


def _oracle(universe, reuse):
    slots = []
    for req in universe:
        alternatives = (req.alternatives,) if req.satisfaction == "one_of" else tuple((a,) for a in req.alternatives)
        for group in alternatives:
            options = []
            for alt in group:
                for size in range(1, len(alt.candidates) + 1):
                    for selected in combinations(alt.candidates, size):
                        events = set().union(*(set(c.event_keys) for c in selected))
                        if set(alt.required_event_keys) <= events:
                            options.append((Choice(req.story_id, req.requirement_id, alt.alternative_key,
                                                   tuple(c.candidate_key for c in selected)), selected))
            slots.append(options)
    for combination in product(*slots):
        ownership = {}
        for choice, selected in combination:
            for candidate in selected:
                ownership.setdefault(candidate.source_key, set()).add(choice.story_id)
        if reuse == "allow" or all(len(stories) == 1 for stories in ownership.values()):
            return tuple(choice for choice, _ in combination)
    return ()


def test_exhaustive_small_oracle():
    # 4^4 event relations x two source policies x one_of/all_of. The oracle
    # builds the full finite product, unlike the production lazy backtracker.
    event_sets = ((), ("e1",), ("e2",), ("e1", "e2"))
    for events in product(event_sets, repeat=4):
        a = alternative("a", (Candidate("a", "s1", events[0]), Candidate("b", "s2", events[1])))
        b = alternative("b", (Candidate("c", "s1", events[2]), Candidate("d", "s3", events[3])))
        for satisfaction, reuse in product(("one_of", "all_of"), ("allow", "forbid")):
            universe = (requirement(satisfaction=satisfaction, alternatives=(a, b)),
                        requirement("story2", alternatives=(b,)))
            expected = _oracle(universe, reuse)
            result = search.search_editorial_materials(universe, source_reuse=reuse, max_search_states=1000)
            assert result.choices == expected
            assert result.status == ("feasible" if expected else "infeasible")
            if expected:
                search.verify_editorial_material_assignment(universe, expected, source_reuse=reuse)
