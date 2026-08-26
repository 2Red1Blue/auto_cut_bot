"""Pure exact-sample planning for native local audio extraction.

These values establish mathematical coverage, not Source ownership, committed
endpoint membership, calibration or permission to perform an inference.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .types import TickRange, TimeBase, canonical_sha256, require_pts, sha256_prefixed


class LocalAudioWindowError(ValueError):
    """A local decode cannot prove the requested continuous sample window."""


def _positive(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:  # noqa: E721
        raise LocalAudioWindowError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class LocalAudioWindowSpec:
    source_id: str
    source_sha256: str
    audio_stream_index: int
    clock_id: str
    time_base: TimeBase
    source_range: TickRange
    requested_range: TickRange
    sample_rate: int
    channels: int
    audio_boundary_set_sha256: str
    decoder_identity_sha256: str
    max_source_bytes: int
    max_decode_frames: int
    max_frame_bytes: int
    max_pcm_bytes: int

    def __post_init__(self) -> None:
        for name in ("source_id", "clock_id"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():  # noqa: E721
                raise LocalAudioWindowError(f"{name} must be exact non-empty text")
            value.encode("utf-8")
        for name in ("source_sha256", "audio_boundary_set_sha256", "decoder_identity_sha256"):
            sha256_prefixed(getattr(self, name), name)
        if type(self.audio_stream_index) is not int or self.audio_stream_index < 0:  # noqa: E721
            raise LocalAudioWindowError("audio_stream_index must be a nonnegative integer")
        if type(self.time_base) is not TimeBase:  # noqa: E721
            raise LocalAudioWindowError("time_base must be exact")
        if type(self.source_range) is not TickRange or type(self.requested_range) is not TickRange:  # noqa: E721
            raise LocalAudioWindowError("source and requested ranges must be exact")
        if not self.source_range.contains(self.requested_range):
            raise LocalAudioWindowError("window escapes original source extent")
        for name in ("sample_rate", "channels", "max_source_bytes", "max_decode_frames",
                     "max_frame_bytes", "max_pcm_bytes"):
            _positive(getattr(self, name), name)
        count = self.requested_duration * self.sample_rate
        if count.denominator != 1:
            raise LocalAudioWindowError("window duration is not an integral sample count")
        if count.numerator * self.channels * 4 > self.max_pcm_bytes:
            raise LocalAudioWindowError("requested FLOAT PCM exceeds explicit byte limit")

    def presentation(self, tick: int) -> Fraction:
        return Fraction(tick * self.time_base.numerator, self.time_base.denominator)

    @property
    def requested_duration(self) -> Fraction:
        return self.presentation(self.requested_range.end_pts - self.requested_range.start_pts)

    @property
    def expected_samples(self) -> int:
        return int(self.requested_duration * self.sample_rate)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": "local-audio-window-spec-v1",
            "source_id": self.source_id, "source_sha256": self.source_sha256,
            "audio_stream_index": self.audio_stream_index, "clock_id": self.clock_id,
            "time_base": {"numerator": self.time_base.numerator, "denominator": self.time_base.denominator},
            "source_range": {"start_pts": self.source_range.start_pts, "end_pts": self.source_range.end_pts},
            "requested_range": {"start_pts": self.requested_range.start_pts, "end_pts": self.requested_range.end_pts},
            "sample_rate": self.sample_rate, "channels": self.channels,
            "audio_boundary_set_sha256": self.audio_boundary_set_sha256,
            "decoder_identity_sha256": self.decoder_identity_sha256,
            "max_source_bytes": self.max_source_bytes, "max_decode_frames": self.max_decode_frames,
            "max_frame_bytes": self.max_frame_bytes, "max_pcm_bytes": self.max_pcm_bytes,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class DecodedAudioFrameClock:
    pts: int
    time_base: TimeBase
    sample_rate: int
    channels: int
    samples: int

    def __post_init__(self) -> None:
        require_pts(self.pts, "decoded audio PTS")
        if type(self.time_base) is not TimeBase:  # noqa: E721
            raise LocalAudioWindowError("decoded frame time base must be exact")
        for name in ("sample_rate", "channels", "samples"):
            _positive(getattr(self, name), name)

    @property
    def start(self) -> Fraction:
        return Fraction(self.pts * self.time_base.numerator, self.time_base.denominator)

    @property
    def end(self) -> Fraction:
        return self.start + Fraction(self.samples, self.sample_rate)


@dataclass(frozen=True, slots=True)
class AudioFrameSlice:
    start_sample: int
    end_sample: int


class LocalAudioWindowTracker:
    """One streaming decode; any failure poisons it instead of permitting skips."""

    def __init__(self, spec: LocalAudioWindowSpec) -> None:
        if type(spec) is not LocalAudioWindowSpec:  # noqa: E721
            raise LocalAudioWindowError("tracker requires an exact window spec")
        self.spec = spec
        self._cursor = spec.presentation(spec.requested_range.start_pts)
        self._end = spec.presentation(spec.requested_range.end_pts)
        self._prior_end: Fraction | None = None
        self._decoded_frames = 0
        self._written_samples = 0
        self._failed = False

    @property
    def complete(self) -> bool:
        return not self._failed and self._cursor == self._end

    @property
    def decoded_frames(self) -> int:
        return self._decoded_frames

    def take(self, frame: DecodedAudioFrameClock) -> AudioFrameSlice | None:
        if self._failed or self.complete:
            raise LocalAudioWindowError("window tracker is terminal")
        try:
            return self._take(frame)
        except ValueError:
            self._failed = True
            raise

    def _take(self, frame: DecodedAudioFrameClock) -> AudioFrameSlice | None:
        if type(frame) is not DecodedAudioFrameClock:  # noqa: E721
            raise LocalAudioWindowError("frame clock must be exact")
        spec = self.spec
        self._decoded_frames += 1
        if self._decoded_frames > spec.max_decode_frames:
            raise LocalAudioWindowError("decode frame budget exhausted")
        if frame.sample_rate != spec.sample_rate or frame.channels != spec.channels:
            raise LocalAudioWindowError("decoded sample rate/channel count changed")
        # Covers the largest supported (float64) input ndarray before conversion.
        if frame.samples * frame.channels * 8 > spec.max_frame_bytes:
            raise LocalAudioWindowError("decoded frame conversion exceeds byte limit")
        if (frame.start < spec.presentation(spec.source_range.start_pts)
                or frame.end > spec.presentation(spec.source_range.end_pts)):
            raise LocalAudioWindowError("decoded frame escapes original source extent")
        if self._prior_end is not None and frame.start < self._prior_end:
            raise LocalAudioWindowError("decoded frames overlap or move backwards")
        self._prior_end = frame.end
        if frame.end <= self._cursor:
            return None
        if frame.start > self._cursor:
            raise LocalAudioWindowError("decoded coverage has a gap in requested window")
        stop = min(frame.end, self._end)
        first_sample = (self._cursor - frame.start) * spec.sample_rate
        last_sample = (stop - frame.start) * spec.sample_rate
        if first_sample.denominator != 1 or last_sample.denominator != 1:
            raise LocalAudioWindowError("requested edge is not an exact decoded sample")
        result = AudioFrameSlice(first_sample.numerator, last_sample.numerator)
        self._written_samples += result.end_sample - result.start_sample
        self._cursor = stop
        return result

    def finish(self) -> int:
        if not self.complete or self._written_samples != self.spec.expected_samples:
            self._failed = True
            raise LocalAudioWindowError("decoded window is incomplete or truncated")
        return self._written_samples
