"""Pure exact-sample coverage tests for candidate-local PCM extraction."""

from __future__ import annotations

from dataclasses import replace

import pytest
from autocut_kernel.media.local_audio_window import (
    AudioFrameSlice,
    DecodedAudioFrameClock,
    LocalAudioWindowError,
    LocalAudioWindowSpec,
    LocalAudioWindowTracker,
)
from autocut_kernel.media.types import MediaValidationError, TickRange, TimeBase

_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64
_HASH_C = "sha256:" + "c" * 64
_AUDIO_BASE = TimeBase(1, 48_000)


def _spec(
    *,
    source_range: TickRange = TickRange(-48_000, 96_000),
    requested_range: TickRange = TickRange(0, 480),
    **overrides: object,
) -> LocalAudioWindowSpec:
    baseline = LocalAudioWindowSpec(
        source_id="source-local-audio",
        source_sha256=_HASH_A,
        audio_stream_index=1,
        clock_id="source-local-audio:audio",
        time_base=_AUDIO_BASE,
        source_range=source_range,
        requested_range=requested_range,
        sample_rate=48_000,
        channels=2,
        audio_boundary_set_sha256=_HASH_B,
        decoder_identity_sha256=_HASH_C,
        max_source_bytes=100_000,
        max_decode_frames=10,
        max_frame_bytes=100_000,
        max_pcm_bytes=100_000,
    )
    return replace(baseline, **overrides)


def _frame(
    pts: int,
    samples: int,
    *,
    time_base: TimeBase = _AUDIO_BASE,
    sample_rate: int = 48_000,
    channels: int = 2,
) -> DecodedAudioFrameClock:
    return DecodedAudioFrameClock(pts, time_base, sample_rate, channels, samples)


def test_tracker_selects_exact_samples_from_negative_pts_with_other_time_base() -> None:
    tracker = LocalAudioWindowTracker(_spec())

    actual = tracker.take(_frame(-960, 1_920, time_base=TimeBase(1, 96_000)))

    assert actual == AudioFrameSlice(480, 960)
    assert tracker.finish() == 480


def test_tracker_discards_prefix_and_allows_gap_wholly_before_requested_window() -> None:
    tracker = LocalAudioWindowTracker(_spec())

    assert tracker.take(_frame(-960, 480)) is None
    assert tracker.take(_frame(0, 480)) == AudioFrameSlice(0, 480)
    assert tracker.finish() == 480


def test_tracker_stitches_adjacent_frames_at_exact_sample_positions() -> None:
    tracker = LocalAudioWindowTracker(_spec())

    assert tracker.take(_frame(0, 240)) == AudioFrameSlice(0, 240)
    assert tracker.take(_frame(240, 240)) == AudioFrameSlice(0, 240)
    assert tracker.complete
    assert tracker.finish() == 480


def test_tracker_rejects_gap_in_requested_window_and_is_poisoned() -> None:
    tracker = LocalAudioWindowTracker(_spec())
    assert tracker.take(_frame(0, 240)) == AudioFrameSlice(0, 240)

    with pytest.raises(LocalAudioWindowError, match="gap"):
        tracker.take(_frame(241, 239))
    with pytest.raises(LocalAudioWindowError, match="terminal"):
        tracker.take(_frame(240, 240))
    with pytest.raises(LocalAudioWindowError, match="incomplete"):
        tracker.finish()


def test_tracker_rejects_overlapping_frames_and_is_poisoned() -> None:
    tracker = LocalAudioWindowTracker(_spec())
    assert tracker.take(_frame(0, 240)) == AudioFrameSlice(0, 240)

    with pytest.raises(LocalAudioWindowError, match="overlap"):
        tracker.take(_frame(239, 241))
    with pytest.raises(LocalAudioWindowError, match="terminal"):
        tracker.take(_frame(240, 240))
    with pytest.raises(LocalAudioWindowError, match="incomplete"):
        tracker.finish()


def test_tracker_rejects_non_integral_frame_internal_sample_cut() -> None:
    tracker = LocalAudioWindowTracker(_spec())

    with pytest.raises(LocalAudioWindowError, match="exact decoded sample"):
        tracker.take(_frame(-1, 1_000, time_base=TimeBase(1, 100_000)))
    with pytest.raises(LocalAudioWindowError, match="terminal"):
        tracker.take(_frame(0, 480))


def test_tracker_rejects_rate_or_channel_change_after_prefix_decode() -> None:
    for changed in (
        _frame(0, 480, sample_rate=44_100),
        _frame(0, 480, channels=1),
    ):
        tracker = LocalAudioWindowTracker(_spec())
        assert tracker.take(_frame(-480, 480)) is None

        with pytest.raises(LocalAudioWindowError, match="rate/channel"):
            tracker.take(changed)
        with pytest.raises(LocalAudioWindowError, match="terminal"):
            tracker.take(_frame(0, 480))


def test_tracker_rejects_truncated_eof_and_does_not_allow_more_frames() -> None:
    tracker = LocalAudioWindowTracker(_spec())
    assert tracker.take(_frame(0, 479)) == AudioFrameSlice(0, 479)

    with pytest.raises(LocalAudioWindowError, match="incomplete or truncated"):
        tracker.finish()
    with pytest.raises(LocalAudioWindowError, match="terminal"):
        tracker.take(_frame(479, 1))


def test_tracker_enforces_decode_frame_and_frame_byte_budgets() -> None:
    frame_budget = LocalAudioWindowTracker(_spec(max_decode_frames=1))
    assert frame_budget.take(_frame(0, 240)) == AudioFrameSlice(0, 240)
    with pytest.raises(LocalAudioWindowError, match="frame budget"):
        frame_budget.take(_frame(240, 240))

    byte_budget = LocalAudioWindowTracker(_spec(max_frame_bytes=3_839))
    with pytest.raises(LocalAudioWindowError, match="conversion exceeds"):
        byte_budget.take(_frame(0, 240))


def test_spec_enforces_pcm_budget_integral_duration_and_bounded_numeric_fields() -> None:
    with pytest.raises(LocalAudioWindowError, match="FLOAT PCM"):
        _spec(max_pcm_bytes=3_839)
    with pytest.raises(LocalAudioWindowError, match="integral sample"):
        _spec(requested_range=TickRange(0, 1), sample_rate=44_100)

    for field in (
        "audio_stream_index",
        "sample_rate",
        "channels",
        "max_source_bytes",
        "max_decode_frames",
        "max_frame_bytes",
        "max_pcm_bytes",
    ):
        with pytest.raises(LocalAudioWindowError):
            _spec(**{field: True})
        with pytest.raises(LocalAudioWindowError):
            _spec(**{field: 1.5})
    for field in (
        "sample_rate",
        "channels",
        "max_source_bytes",
        "max_decode_frames",
        "max_frame_bytes",
        "max_pcm_bytes",
    ):
        with pytest.raises(LocalAudioWindowError):
            _spec(**{field: 0})


def test_frame_clock_rejects_missing_float_or_boolean_pts_and_dimensions() -> None:
    baseline = _frame(0, 480)
    for pts in (None, 0.0, True):
        with pytest.raises(MediaValidationError, match="PTS"):
            replace(baseline, pts=pts)
    for field in ("sample_rate", "channels", "samples"):
        with pytest.raises(LocalAudioWindowError):
            replace(baseline, **{field: True})
        with pytest.raises(LocalAudioWindowError):
            replace(baseline, **{field: 1.5})


def test_spec_hash_binds_source_window_and_decoder_identity() -> None:
    spec = _spec()

    assert spec.canonical_hash != replace(spec, decoder_identity_sha256=_HASH_A).canonical_hash
    assert spec.canonical_hash != replace(spec, audio_boundary_set_sha256=_HASH_C).canonical_hash
    assert spec.canonical_hash != replace(spec, requested_range=TickRange(480, 960)).canonical_hash
