"""Exact consistency of editorial durations, not physical material feasibility.

Beats are contiguous in the declared output order. A max-gap spans the end of
the earlier Beat to the start of the later Beat, not a Source clock distance.
Targets are preferences; only min/max are constraints. No implicit teaser,
transition, silence or padding is added. Stage 4 still owns actual A/V timing.
"""

from __future__ import annotations

from fractions import Fraction

from .editorial_models import Adjacent, DurationRange, MaxGap, OrderingConstraint, Precedes


def _validate(
    beats: tuple[DurationRange, ...], story: DurationRange,
    ordering: tuple[OrderingConstraint, ...],
) -> None:
    if (type(beats) is not tuple or not beats  # noqa: E721
            or any(type(beat) is not DurationRange for beat in beats)  # noqa: E721
            or type(story) is not DurationRange or type(ordering) is not tuple):  # noqa: E721
        raise ValueError("editorial timing requires exact immutable duration/order values")
    for item in ordering:
        if type(item) not in (Precedes, Adjacent, MaxGap):
            raise ValueError("editorial timing has an unknown ordering constraint")
        if isinstance(item, Adjacent):
            left, right = item.first_ordinal, item.second_ordinal
        else:
            left, right = item.before_ordinal, item.after_ordinal
        if not 0 <= left < len(beats) or not 0 <= right < len(beats):
            raise ValueError("editorial ordering references an unknown Beat")
    if len(set(ordering)) != len(ordering):
        raise ValueError("editorial ordering contains duplicates")


def _ordered(ordering: tuple[OrderingConstraint, ...]) -> bool:
    return all(
        item.second_ordinal == item.first_ordinal + 1 if isinstance(item, Adjacent)
        else item.before_ordinal < item.after_ordinal
        for item in ordering
    )


def solve_editorial_timing(
    beats: tuple[DurationRange, ...], story: DurationRange,
    ordering: tuple[OrderingConstraint, ...],
) -> tuple[Fraction, ...] | None:
    """Return a deterministic rational duration witness, or exact infeasibility.

Difference constraints use cumulative boundaries x[0..N]. Edge (u,v,c)
means x[v] <= x[u]+c. All-zero initialization is an implicit super-source;
a relaxation on pass N+1 proves a negative cycle. This checks all max-gaps
and the total Story range jointly, not separate overlapping interval checks.
The caller owns parser limits and material-capacity validation.
"""
    _validate(beats, story, ordering)
    if not _ordered(ordering):
        return None
    edges: list[tuple[int, int, Fraction]] = []
    for index, beat in enumerate(beats):
        edges.extend(((index, index + 1, Fraction(beat.maximum)),
                      (index + 1, index, Fraction(-beat.minimum))))
    edges.extend(((0, len(beats), Fraction(story.maximum)),
                  (len(beats), 0, Fraction(-story.minimum))))
    for item in ordering:
        if isinstance(item, MaxGap):
            gap = item.maximum_gap
            edges.append((item.before_ordinal + 1, item.after_ordinal,
                          Fraction(gap.tick * gap.time_base.numerator, gap.time_base.denominator)))
    # Sorting makes the witness independent of semantically identical orderings.
    edges.sort()
    distances = [Fraction(0)] * (len(beats) + 1)
    for _ in range(len(distances)):
        changed = False
        for source, target, bound in edges:
            candidate = distances[source] + bound
            if distances[target] > candidate:
                distances[target] = candidate
                changed = True
        if not changed:
            result = tuple(right - left for left, right in zip(distances, distances[1:]))
            verify_editorial_timing(beats, story, ordering, result)
            return result
    return None


def verify_editorial_timing(
    beats: tuple[DurationRange, ...], story: DurationRange,
    ordering: tuple[OrderingConstraint, ...], durations: tuple[Fraction, ...],
) -> None:
    """Check a witness directly; never call or trust the solver's graph replay."""
    _validate(beats, story, ordering)
    if (type(durations) is not tuple or len(durations) != len(beats)  # noqa: E721
            or any(type(value) is not Fraction for value in durations)):  # noqa: E721
        raise ValueError("timing witness must contain one exact Fraction per Beat")
    if not _ordered(ordering):
        raise ValueError("timing witness contradicts the fixed Beat order")
    for beat, duration in zip(beats, durations, strict=True):
        if not beat.minimum <= duration <= beat.maximum:
            raise ValueError("timing witness exceeds a Beat duration range")
    if not story.minimum <= sum(durations) <= story.maximum:
        raise ValueError("timing witness exceeds the Story duration range")
    for item in ordering:
        if isinstance(item, MaxGap):
            gap = item.maximum_gap
            maximum = Fraction(gap.tick * gap.time_base.numerator, gap.time_base.denominator)
            if sum(durations[item.before_ordinal + 1:item.after_ordinal]) > maximum:
                raise ValueError("timing witness exceeds a max-gap constraint")
