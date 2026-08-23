"""Deterministic exhaustive compiler for one fixture-only source span."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..media.types import (
    MediaValidationError,
    PTSIndex,
    TickRange,
    ValidityIntervals,
    require_pts,
)


class ExactSpanError(ValueError):
    """Base error for exact-span compilation outcomes."""


class ExactSpanValidationError(ExactSpanError):
    """The request or its supplied source evidence is not a legal domain input."""


class CandidatePairLimitError(ExactSpanValidationError):
    """The full Cartesian candidate domain exceeds the declared hard limit."""


class NoLegalSpanError(ExactSpanError):
    """The valid complete domain contains no candidate satisfying the policy."""


@dataclass(frozen=True, slots=True)
class FixtureBeatInput:
    """Fixture/shadow editorial bounds for compiling exactly one source span."""

    desired_start_pts: int
    anchor_start_pts: int
    anchor_end_pts: int
    desired_end_pts: int
    minimum_duration_pts: int

    def __post_init__(self) -> None:
        desired_start = require_pts(self.desired_start_pts, "desired_start_pts")
        anchor_start = require_pts(self.anchor_start_pts, "anchor_start_pts")
        anchor_end = require_pts(self.anchor_end_pts, "anchor_end_pts")
        desired_end = require_pts(self.desired_end_pts, "desired_end_pts")
        minimum_duration = require_pts(self.minimum_duration_pts, "minimum_duration_pts")
        if not desired_start <= anchor_start < anchor_end <= desired_end:
            raise ExactSpanValidationError(
                "fixture beat must satisfy desired_start <= anchor_start < anchor_end <= desired_end"
            )
        if minimum_duration <= 0:
            raise ExactSpanValidationError("minimum_duration_pts must be positive")


@dataclass(frozen=True, slots=True)
class SpanSelectionPolicy:
    """Bounded enumeration policy; no candidate is omitted within its domain."""

    candidate_pair_limit: int
    forbidden_ranges: tuple[TickRange, ...] = ()

    def __post_init__(self) -> None:
        candidate_pair_limit = require_pts(self.candidate_pair_limit, "candidate_pair_limit")
        if candidate_pair_limit <= 0:
            raise ExactSpanValidationError("candidate_pair_limit must be positive")
        object.__setattr__(self, "forbidden_ranges", tuple(self.forbidden_ranges))

    @classmethod
    def with_forbidden_ranges(
        cls, candidate_pair_limit: int, forbidden_ranges: Iterable[TickRange]
    ) -> SpanSelectionPolicy:
        return cls(candidate_pair_limit, tuple(forbidden_ranges))


def _validate_indexed_range(pts_index: PTSIndex, value: TickRange, field_name: str) -> None:
    pts_index.require_member(value.start_pts, f"{field_name}.start_pts")
    pts_index.require_member(value.end_pts, f"{field_name}.end_pts")


def _validate_domain(
    beat: FixtureBeatInput,
    pts_index: PTSIndex,
    validity_intervals: ValidityIntervals,
    policy: SpanSelectionPolicy,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    try:
        for field_name in (
            "desired_start_pts",
            "anchor_start_pts",
            "anchor_end_pts",
            "desired_end_pts",
        ):
            pts_index.require_member(getattr(beat, field_name), field_name)
        validity_intervals.require_indexed(pts_index)
        for position, forbidden_range in enumerate(policy.forbidden_ranges):
            _validate_indexed_range(pts_index, forbidden_range, f"forbidden_ranges[{position}]")
    except MediaValidationError as error:
        raise ExactSpanValidationError(str(error)) from error

    starts = pts_index.ticks_between(beat.desired_start_pts, beat.anchor_start_pts)
    ends = pts_index.ticks_between(beat.anchor_end_pts, beat.desired_end_pts)
    pair_count = len(starts) * len(ends)
    if pair_count > policy.candidate_pair_limit:
        raise CandidatePairLimitError(
            f"candidate pair count {pair_count} exceeds limit {policy.candidate_pair_limit}"
        )
    return starts, ends


def select_exact_span(
    beat: FixtureBeatInput,
    pts_index: PTSIndex,
    validity_intervals: ValidityIntervals,
    policy: SpanSelectionPolicy,
) -> TickRange:
    """Exhaustively select the lexicographically canonical legal source span.

    The candidate domain is all indexed starts in ``[desired_start, anchor_start]``
    crossed with all indexed ends in ``[anchor_end, desired_end]``.  It is checked
    against the complete pair limit before any candidate is considered.
    """
    starts, ends = _validate_domain(beat, pts_index, validity_intervals, policy)
    selected: TickRange | None = None
    selected_key: tuple[int, int, int, int, int] | None = None
    for start_pts in starts:
        for end_pts in ends:
            if start_pts >= end_pts:
                continue
            candidate = TickRange(start_pts, end_pts)
            if candidate.duration_pts < beat.minimum_duration_pts:
                continue
            if not validity_intervals.covers(candidate):
                continue
            if any(candidate.overlaps(forbidden) for forbidden in policy.forbidden_ranges):
                continue
            key = (
                beat.anchor_start_pts - start_pts,
                end_pts - beat.anchor_end_pts,
                candidate.duration_pts,
                start_pts,
                end_pts,
            )
            if selected_key is None or key < selected_key:
                selected = candidate
                selected_key = key
    if selected is None:
        raise NoLegalSpanError("no legal span exists in the complete candidate domain")
    return selected


@dataclass(frozen=True, slots=True)
class ExactSpanCompiler:
    """A closed compiler instance binding source PTS evidence and selection policy."""

    pts_index: PTSIndex
    validity_intervals: ValidityIntervals
    policy: SpanSelectionPolicy

    def compile(self, beat: FixtureBeatInput) -> TickRange:
        return select_exact_span(beat, self.pts_index, self.validity_intervals, self.policy)
