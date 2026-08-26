from fractions import Fraction
from itertools import product

import pytest
from autocut_kernel.media.types import TimeBase
from autocut_kernel.semantic_chain.editorial_models import (
    Adjacent,
    DurationRange,
    GapDuration,
    MaxGap,
    Precedes,
)
from autocut_kernel.semantic_chain.editorial_timing import (
    solve_editorial_timing,
    verify_editorial_timing,
)


def _range(low, high):
    return DurationRange(low, low, high)


def _gap(left, right, numerator, denominator=1):
    return MaxGap(left, right, GapDuration(numerator, TimeBase(1, denominator)))


def test_joint_story_minimum_requires_durations_above_individual_minima():
    beats = (_range(1, 5),) * 3
    story = _range(10, 12)
    constraints = (Precedes(0, 2), _gap(0, 2, 1))
    witness = solve_editorial_timing(beats, story, constraints)
    assert witness is not None
    assert sum(witness) >= 10
    assert witness[1] == 1
    verify_editorial_timing(beats, story, constraints, witness)
    assert witness == solve_editorial_timing(beats, story, tuple(reversed(constraints)))


def test_overlapping_gap_constraints_are_joint_not_individually_feasible():
    beats = (_range(1, 1), _range(1, 4), _range(1, 4), _range(1, 1))
    story = _range(7, 10)
    constraints = (_gap(0, 2, 2), _gap(1, 3, 2))
    assert solve_editorial_timing(beats, story, constraints[:1]) is not None
    assert solve_editorial_timing(beats, story, constraints[1:]) is not None
    assert solve_editorial_timing(beats, story, constraints) is None


def test_rational_gap_is_not_rounded_to_seconds_or_source_ticks():
    beats = (_range(1, 1), _range(1, 2), _range(1, 2), _range(1, 1))
    story = _range(5, 5)
    constraints = (_gap(0, 2, 3, 2), _gap(1, 3, 3, 2))
    assert solve_editorial_timing(beats, story, constraints) == (
        Fraction(1), Fraction(3, 2), Fraction(3, 2), Fraction(1),
    )
    assert solve_editorial_timing(beats, story, (_gap(0, 2, 1499, 1000), constraints[1])) is None


@pytest.mark.parametrize("constraints", [
    (Precedes(1, 0),), (Precedes(0, 1), Precedes(1, 0)),
    (Adjacent(1, 0),), (Adjacent(0, 2),), (_gap(2, 0, 100),),
])
def test_fixed_order_conflict_never_reorders_beats(constraints):
    beats = (_range(1, 2),) * 3
    assert solve_editorial_timing(beats, _range(3, 6), constraints) is None
    with pytest.raises(ValueError, match="fixed Beat order"):
        verify_editorial_timing(beats, _range(3, 6), constraints, (Fraction(1),) * 3)


def test_adjacent_zero_gap_and_targets_are_soft_no_implicit_teaser_padding():
    beats = (DurationRange(1, 10, 10),) * 2
    story = DurationRange(2, 2, 2)
    witness = solve_editorial_timing(beats, story, (Adjacent(0, 1), _gap(0, 1, 0)))
    assert witness == (Fraction(1), Fraction(1))
    assert solve_editorial_timing((_range(2, 3),), _range(1, 1), ()) is None
    assert solve_editorial_timing((_range(2, 3),), _range(4, 5), ()) is None


def test_independent_verifier_rejects_invalid_witness_without_solver(monkeypatch):
    import autocut_kernel.semantic_chain.editorial_timing as timing

    def forbidden(*args, **kwargs):
        raise RuntimeError("verifier called solver")

    monkeypatch.setattr(timing, "solve_editorial_timing", forbidden)
    beats, story, constraints = (_range(1, 3),) * 3, _range(4, 6), (_gap(0, 2, 1),)
    verify_editorial_timing(beats, story, constraints, (Fraction(2), Fraction(1), Fraction(1)))
    for values in ((Fraction(1),) * 3, (Fraction(3),) * 3,
                   (Fraction(0), Fraction(1), Fraction(3)),
                   (Fraction(1), Fraction(2), Fraction(1)), (Fraction(1),),
                   (1, 1, 2), [Fraction(2), Fraction(1), Fraction(1)]):
        with pytest.raises(ValueError):
            verify_editorial_timing(beats, story, constraints, values)


@pytest.mark.parametrize("beats,story,ordering", [
    ((), _range(1, 2), ()), ([_range(1, 2)], _range(1, 2), ()),
    ((_range(1, 2),), {"min": 1}, ()), ((_range(1, 2),), _range(1, 2), []),
    ((_range(1, 2),), _range(1, 2), (Precedes(0, 1),)),
    ((_range(1, 2),) * 2, _range(1, 2), (Precedes(0, 1), Precedes(0, 1))),
    ((_range(1, 2),), _range(1, 2), ({"constraint_type": "precedes"},)),
])
def test_untyped_or_unresolved_input_is_not_reported_as_infeasibility(beats, story, ordering):
    with pytest.raises(ValueError):
        solve_editorial_timing(beats, story, ordering)


def test_small_integer_spaces_match_independent_exhaustive_oracle():
    # Integer difference bounds have integral feasible vertices. Exhaustion is
    # therefore a complete oracle for these cases, not a sample of rational ones.
    for uppers in product((1, 2, 3), repeat=3):
        beats = tuple(_range(1, maximum) for maximum in uppers)
        for minimum, maximum, gap in product(range(1, 9), range(1, 9), range(4)):
            if minimum > maximum:
                continue
            story, constraints = _range(minimum, maximum), (_gap(0, 2, gap),)
            expected = any(minimum <= sum(values) <= maximum and values[1] <= gap
                           for values in product(*(range(1, maximum + 1) for maximum in uppers)))
            actual = solve_editorial_timing(beats, story, constraints)
            assert (actual is not None) == expected
