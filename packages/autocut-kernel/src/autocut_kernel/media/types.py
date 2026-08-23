"""Immutable, integer-only source video timing primitives.

These records deliberately model source-native PTS ticks instead of seconds.
They are dependency-free so preflight, compilation, and persistence adapters
can share one fail-closed timing vocabulary.
"""

from __future__ import annotations

import hashlib
import json
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


def sha256_prefixed(value: str, field_name: str) -> str:
    """Validate and normalize a ``sha256:<lowercase hex>`` identity."""
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise MediaValidationError(f"{field_name} must be a sha256:<lowercase-hex> identity")
    digest = value.removeprefix("sha256:")
    if any(character not in "0123456789abcdef" for character in digest):
        raise MediaValidationError(f"{field_name} must be a sha256:<lowercase-hex> identity")
    return value


def canonical_sha256(payload: object) -> str:
    """Return the stable content identity of an immutable JSON-compatible payload."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Content identity calculated from the local media bytes."""

    sha256: str
    byte_size: int

    def __post_init__(self) -> None:
        sha256_prefixed(self.sha256, "source.sha256")
        size = require_pts(self.byte_size, "source.byte_size")
        if size <= 0:
            raise MediaValidationError("source.byte_size must be positive")


@dataclass(frozen=True, slots=True)
class VideoStreamEvidence:
    """The sole selected decoded video stream and its exact clock."""

    stream_index: int
    codec_name: str
    width: int
    height: int
    time_base: TimeBase

    def __post_init__(self) -> None:
        if require_pts(self.stream_index, "stream_index") < 0:
            raise MediaValidationError("stream_index must be non-negative")
        if not isinstance(self.codec_name, str) or not self.codec_name:
            raise MediaValidationError("codec_name must be a non-empty string")
        if require_pts(self.width, "width") <= 0 or require_pts(self.height, "height") <= 0:
            raise MediaValidationError("video dimensions must be positive")


@dataclass(frozen=True, slots=True)
class ToolEvidence:
    """Observable ffprobe execution provenance; stderr is represented by a digest."""

    executable: str
    version: str
    stderr_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.executable, str) or not self.executable:
            raise MediaValidationError("ffprobe executable must be a non-empty string")
        if not isinstance(self.version, str) or not self.version:
            raise MediaValidationError("ffprobe version must be a non-empty string")
        sha256_prefixed(self.stderr_sha256, "ffprobe.stderr_sha256")


@dataclass(frozen=True, slots=True)
class MediaEvidence:
    """Complete, immutable evidence required before a source span may be selected."""

    source: SourceIdentity
    video_stream: VideoStreamEvidence
    pts_index: PTSIndex
    validity_intervals: ValidityIntervals
    pts_index_sha256: str
    ffprobe: ToolEvidence
    fixture_id: str
    fixture_manifest_sha256: str
    fixture_sidecar_sha256: str
    fixture_schema_version: int
    evidence_mode: str

    def __post_init__(self) -> None:
        if self.pts_index_sha256 != canonical_sha256(list(self.pts_index.ticks)):
            raise MediaValidationError("pts_index_sha256 must match the complete PTS index")
        self.validity_intervals.require_indexed(self.pts_index)
        if not isinstance(self.fixture_id, str) or not self.fixture_id:
            raise MediaValidationError("fixture_id must be a non-empty string")
        sha256_prefixed(self.fixture_manifest_sha256, "fixture_manifest_sha256")
        sha256_prefixed(self.fixture_sidecar_sha256, "fixture_sidecar_sha256")
        if require_pts(self.fixture_schema_version, "fixture_schema_version") <= 0:
            raise MediaValidationError("fixture_schema_version must be positive")
        if self.evidence_mode != "fixture_ground_truth_v1":
            raise MediaValidationError("unsupported evidence_mode")

    def to_json(self) -> dict[str, object]:
        """Produce a closed JSON-ready evidence artifact without mutable pass flags."""
        return {
            "source": {"sha256": self.source.sha256, "byte_size": self.source.byte_size},
            "video_stream": {
                "stream_index": self.video_stream.stream_index,
                "codec_name": self.video_stream.codec_name,
                "width": self.video_stream.width,
                "height": self.video_stream.height,
                "time_base": {
                    "numerator": self.video_stream.time_base.numerator,
                    "denominator": self.video_stream.time_base.denominator,
                },
            },
            "pts_index": list(self.pts_index.ticks),
            "pts_index_sha256": self.pts_index_sha256,
            "validity_intervals": [
                {"start_pts": item.start_pts, "end_pts": item.end_pts}
                for item in self.validity_intervals.intervals
            ],
            "ffprobe": {
                "executable": self.ffprobe.executable,
                "version": self.ffprobe.version,
                "stderr_sha256": self.ffprobe.stderr_sha256,
            },
            "fixture_id": self.fixture_id,
            "fixture_manifest_sha256": self.fixture_manifest_sha256,
            "fixture_sidecar_sha256": self.fixture_sidecar_sha256,
            "fixture_schema_version": self.fixture_schema_version,
            "evidence_mode": self.evidence_mode,
        }
