from __future__ import annotations

import pytest
from autocut_kernel.media import (
    MediaValidationError,
    PTSIndex,
    TickRange,
    TimeBase,
    ValidityIntervals,
)
from autocut_kernel.physical_edit import (
    CandidatePairLimitError,
    ExactSpanValidationError,
    FixtureBeatInput,
    NoLegalSpanError,
    SpanSelectionPolicy,
    select_exact_span,
)


def _beat(**overrides: int) -> FixtureBeatInput:
    values = {
        "desired_start_pts": 0,
        "anchor_start_pts": 10,
        "anchor_end_pts": 20,
        "desired_end_pts": 30,
        "minimum_duration_pts": 10,
    }
    values.update(overrides)
    return FixtureBeatInput(**values)


def test_select_exact_span_uses_canonical_key_and_keeps_zero_pts_legal() -> None:
    selected = select_exact_span(
        _beat(),
        PTSIndex((0, 5, 10, 20, 25, 30)),
        ValidityIntervals((TickRange(0, 30),)),
        SpanSelectionPolicy(candidate_pair_limit=9),
    )

    assert selected == TickRange(10, 20)


def test_float_and_boolean_pts_are_rejected_without_coercion() -> None:
    with pytest.raises(MediaValidationError, match="integer PTS"):
        PTSIndex((0, 1.0))  # type: ignore[arg-type]
    with pytest.raises(MediaValidationError, match="integer PTS"):
        TimeBase(True, 1)  # type: ignore[arg-type]
    with pytest.raises(MediaValidationError, match="integer PTS"):
        _beat(anchor_start_pts=10.0)  # type: ignore[arg-type]


def test_unknown_and_forbidden_coverage_rejects_candidates() -> None:
    index = PTSIndex((0, 10, 20, 30))
    beat = _beat()
    with pytest.raises(ExactSpanValidationError, match="verified PTS index"):
        select_exact_span(
            beat,
            index,
            ValidityIntervals((TickRange(1, 30),)),
            SpanSelectionPolicy(candidate_pair_limit=4),
        )
    with pytest.raises(NoLegalSpanError):
        select_exact_span(
            beat,
            index,
            ValidityIntervals((TickRange(0, 30),)),
            SpanSelectionPolicy(4, (TickRange(0, 30),)),
        )


def test_pair_limit_is_checked_before_candidate_iteration() -> None:
    with pytest.raises(CandidatePairLimitError, match="pair count 9 exceeds limit 8"):
        select_exact_span(
            _beat(),
            PTSIndex((0, 5, 10, 20, 25, 30)),
            ValidityIntervals((TickRange(0, 30),)),
            SpanSelectionPolicy(candidate_pair_limit=8),
        )


def test_no_candidate_is_a_distinct_outcome() -> None:
    with pytest.raises(NoLegalSpanError, match="no legal span"):
        select_exact_span(
            _beat(minimum_duration_pts=31),
            PTSIndex((0, 10, 20, 30)),
            ValidityIntervals((TickRange(0, 30),)),
            SpanSelectionPolicy(candidate_pair_limit=4),
        )
