"""Immutable, integer-only source video timing primitives.

These records deliberately model source-native PTS ticks instead of seconds.
They are dependency-free so preflight, compilation, and persistence adapters
can share one fail-closed timing vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import gcd
from typing import Iterable


class MediaDomainError(ValueError):
    """Base error for invalid media-domain data."""


class MediaValidationError(MediaDomainError):
    """Raised when a closed media-domain record violates its invariants."""


def require_pts(value: object, field_name: str) -> int:
    """Return an exact PTS integer, rejecting booleans and all numeric coercion."""
    if type(value) is not int:  # noqa: E721 - bool is intentionally not a PTS integer
        raise MediaValidationError(f"{field_name} must be an integer PTS tick")
    return value


@dataclass(frozen=True, slots=True)
class TimeBase:
    """A reduced positive rational duration, in seconds per PTS tick."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        numerator = require_pts(self.numerator, "time_base.numerator")
        denominator = require_pts(self.denominator, "time_base.denominator")
        if numerator <= 0 or denominator <= 0:
            raise MediaValidationError("time_base numerator and denominator must be positive")
        if gcd(numerator, denominator) != 1:
            raise MediaValidationError("time_base must be reduced")


@dataclass(frozen=True, slots=True, order=True)
class TickRange:
    """A non-empty half-open PTS range, ``[start_pts, end_pts)``."""

    start_pts: int
    end_pts: int

    def __post_init__(self) -> None:
        start_pts = require_pts(self.start_pts, "start_pts")
        end_pts = require_pts(self.end_pts, "end_pts")
        if start_pts >= end_pts:
            raise MediaValidationError("PTS range must satisfy start_pts < end_pts")

    @property
    def duration_pts(self) -> int:
        return self.end_pts - self.start_pts

    def contains(self, other: TickRange) -> bool:
        """Whether this half-open range wholly contains ``other``."""
        return self.start_pts <= other.start_pts and other.end_pts <= self.end_pts

    def overlaps(self, other: TickRange) -> bool:
        """Whether two half-open ranges share at least one tick interval."""
        return self.start_pts < other.end_pts and other.start_pts < self.end_pts


@dataclass(frozen=True, slots=True)
class PTSIndex:
    """The complete strictly increasing set of verified decoded video PTS ticks."""

    ticks: tuple[int, ...]

    def __post_init__(self) -> None:
        ticks = tuple(self.ticks)
        if not ticks:
            raise MediaValidationError("PTS index must not be empty")
        for position, tick in enumerate(ticks):
            require_pts(tick, f"pts_index.ticks[{position}]")
        if any(left >= right for left, right in zip(ticks, ticks[1:], strict=False)):
            raise MediaValidationError("PTS index ticks must be strictly increasing and deduplicated")
        object.__setattr__(self, "ticks", ticks)

    @classmethod
    def from_ticks(cls, ticks: Iterable[int]) -> PTSIndex:
        """Construct an index from an iterable without sorting or deduplicating it."""
        return cls(tuple(ticks))

    def contains(self, tick: object) -> bool:
        return type(tick) is int and tick in self.ticks  # noqa: E721

    def require_member(self, tick: object, field_name: str) -> int:
        tick = require_pts(tick, field_name)
        if tick not in self.ticks:
            raise MediaValidationError(f"{field_name} must be a member of the verified PTS index")
        return tick

    def ticks_between(self, start_pts: int, end_pts: int) -> tuple[int, ...]:
        """Return indexed endpoints in inclusive numeric bounds, in source order."""
        return tuple(tick for tick in self.ticks if start_pts <= tick <= end_pts)


@dataclass(frozen=True, slots=True)
class ValidityIntervals:
    """Ordered, non-overlapping half-open intervals permitted by visual evidence."""

    intervals: tuple[TickRange, ...]

    def __post_init__(self) -> None:
        intervals = tuple(self.intervals)
        if not intervals:
            raise MediaValidationError("validity intervals must not be empty")
        if any(
            left.end_pts > right.start_pts
            for left, right in zip(intervals, intervals[1:], strict=False)
        ):
            raise MediaValidationError("validity intervals must be ordered and non-overlapping")
        object.__setattr__(self, "intervals", intervals)

    @classmethod
    def from_ranges(cls, intervals: Iterable[TickRange]) -> ValidityIntervals:
        return cls(tuple(intervals))

    def require_indexed(self, pts_index: PTSIndex, field_name: str = "validity interval") -> None:
        for position, interval in enumerate(self.intervals):
            pts_index.require_member(interval.start_pts, f"{field_name}[{position}].start_pts")
            pts_index.require_member(interval.end_pts, f"{field_name}[{position}].end_pts")

    def covers(self, candidate: TickRange) -> bool:
        """Whether one evidence interval continuously covers the candidate range."""
        return any(interval.contains(candidate) for interval in self.intervals)
