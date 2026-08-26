"""Sample-content oracle independent of the streaming tracker's arithmetic."""

from fractions import Fraction

import pytest
from autocut_kernel.media.local_audio_window import (
    DecodedAudioFrameClock,
    LocalAudioWindowSpec,
    LocalAudioWindowTracker,
)
from autocut_kernel.media.types import TickRange, TimeBase

HASH = "sha256:" + "1" * 64


@pytest.mark.parametrize("origin", [-13, 0, 43])
@pytest.mark.parametrize("sample_rate", [8, 48_000])
def test_all_toy_windows_select_exact_original_samples(origin: int, sample_rate: int):
    # A decoded track with nonuniform frame lengths. Request and decoder clocks
    # have different integer units, but represent the same source presentation.
    blocks = (tuple(range(0, 3)), tuple(range(3, 8)), tuple(range(8, 12)), tuple(range(12, 20)))
    for start in range(20):
        for end in range(start + 1, 21):
            spec = LocalAudioWindowSpec(
                "source", HASH, 1, "original-audio", TimeBase(1, sample_rate),
                TickRange(origin, origin + 20), TickRange(origin + start, origin + end),
                sample_rate, 1, HASH, HASH, 1_000, 4, 64, 80,
            )
            tracker = LocalAudioWindowTracker(spec)
            selected: list[int] = []
            for block in blocks:
                frame = DecodedAudioFrameClock(
                    (origin + block[0]) * 2, TimeBase(1, sample_rate * 2), sample_rate, 1, len(block),
                )
                selection = tracker.take(frame)
                if selection is not None:
                    selected.extend(block[selection.start_sample:selection.end_sample])
                if tracker.complete:
                    break
            # Oracle selects by each sample's absolute timestamp, without
            # sharing the tracker's slice-index or cursor calculation.
            left, right = Fraction(origin + start, sample_rate), Fraction(origin + end, sample_rate)
            expected = [i for i in range(20) if left <= Fraction(origin + i, sample_rate) < right]
            assert selected == expected
            assert tracker.finish() == len(expected)
