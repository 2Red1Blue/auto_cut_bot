"""Kernel-owned VLM windows and conservative proxy-to-source time mapping."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from ..media.root_evidence import FramePtsIndexSet
from ..media.types import (
    TickRange,
    TimeBase,
    canonical_sha256,
    require_pts,
    sha256_prefixed,
)
from .models import MappedSourceInterval, VlmValidationError

_OPAQUE_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MEDIA_TYPE: Final = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}\Z"
)


def _non_negative(value: object, field_name: str) -> int:
    result = require_pts(value, field_name)
    if result < 0:
        raise VlmValidationError(f"{field_name} must be non-negative")
    return result


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _opaque_id(value: object, field_name: str) -> str:
    if type(value) is not str or not _OPAQUE_ID.fullmatch(value):  # noqa: E721
        raise VlmValidationError(f"{field_name} must be an opaque Kernel identity")
    return value


@dataclass(frozen=True, slots=True)
class ProxyTimelineSegment:
    """One monotonic rational mapping segment with a Source-tick error bound."""

    proxy_range: TickRange
    source_range: TickRange
    max_source_error_pts: int

    def __post_init__(self) -> None:
        if type(self.proxy_range) is not TickRange or type(self.source_range) is not TickRange:  # noqa: E721
            raise VlmValidationError("timeline segment ranges must be TickRange values")
        _non_negative(self.max_source_error_pts, "timeline_segment.max_source_error_pts")

    def to_mapping(self) -> dict[str, object]:
        return {
            "max_source_error_pts": self.max_source_error_pts,
            "proxy_range": {
                "end_pts": self.proxy_range.end_pts,
                "start_pts": self.proxy_range.start_pts,
            },
            "source_range": {
                "end_pts": self.source_range.end_pts,
                "start_pts": self.source_range.start_pts,
            },
        }


def _map_segment_floor(segment: ProxyTimelineSegment, proxy_pts: int) -> int:
    relative = proxy_pts - segment.proxy_range.start_pts
    return segment.source_range.start_pts + (
        relative * segment.source_range.duration_pts // segment.proxy_range.duration_pts
    )


def _map_segment_ceil(segment: ProxyTimelineSegment, proxy_pts: int) -> int:
    relative = proxy_pts - segment.proxy_range.start_pts
    return segment.source_range.start_pts + _ceil_div(
        relative * segment.source_range.duration_pts,
        segment.proxy_range.duration_pts,
    )


@dataclass(frozen=True, slots=True)
class ProxyTimelineMap:
    """A complete monotonic proxy clock mapping.

    ``translation_certificate`` is deliberately strict: it is admitted only
    for one segment with equal proxy/source tick durations and equal time
    bases.  All other maps must declare ``piecewise_monotonic``.
    """

    proxy_time_base: TimeBase
    source_time_base: TimeBase
    segments: tuple[ProxyTimelineSegment, ...]
    certificate_kind: str

    def __post_init__(self) -> None:
        if type(self.proxy_time_base) is not TimeBase or type(self.source_time_base) is not TimeBase:  # noqa: E721
            raise VlmValidationError("timeline map time bases must be TimeBase values")
        segments = tuple(self.segments)
        if not segments or any(type(item) is not ProxyTimelineSegment for item in segments):  # noqa: E721
            raise VlmValidationError("timeline map must contain ProxyTimelineSegment values")
        for left, right in zip(segments, segments[1:], strict=False):
            if left.proxy_range.end_pts != right.proxy_range.start_pts:
                raise VlmValidationError("timeline proxy segments must be ordered, contiguous, and gap-free")
            if left.source_range.end_pts != right.source_range.start_pts:
                raise VlmValidationError(
                    "timeline source segments must be ordered, contiguous, and gap-free"
                )
        if self.certificate_kind not in {"translation_certificate", "piecewise_monotonic"}:
            raise VlmValidationError("unsupported timeline certificate kind")
        if self.certificate_kind == "translation_certificate":
            if len(segments) != 1:
                raise VlmValidationError("translation certificate must contain exactly one segment")
            segment = segments[0]
            if self.proxy_time_base != self.source_time_base:
                raise VlmValidationError("translation certificate requires identical time bases")
            if segment.proxy_range.duration_pts != segment.source_range.duration_pts:
                raise VlmValidationError("translation certificate requires equal tick durations")
        object.__setattr__(self, "segments", segments)

    @classmethod
    def translation(
        cls,
        *,
        time_base: TimeBase,
        proxy_range: TickRange,
        source_start_pts: int,
        max_source_error_pts: int = 0,
    ) -> ProxyTimelineMap:
        source_start = require_pts(source_start_pts, "source_start_pts")
        return cls(
            proxy_time_base=time_base,
            source_time_base=time_base,
            segments=(
                ProxyTimelineSegment(
                    proxy_range=proxy_range,
                    source_range=TickRange(source_start, source_start + proxy_range.duration_pts),
                    max_source_error_pts=max_source_error_pts,
                ),
            ),
            certificate_kind="translation_certificate",
        )

    @property
    def proxy_range(self) -> TickRange:
        return TickRange(self.segments[0].proxy_range.start_pts, self.segments[-1].proxy_range.end_pts)

    @property
    def source_range(self) -> TickRange:
        return TickRange(self.segments[0].source_range.start_pts, self.segments[-1].source_range.end_pts)

    @property
    def maximum_error_pts(self) -> int:
        return max(segment.max_source_error_pts for segment in self.segments)

    def _segment_for_boundary(self, proxy_pts: int, *, end_boundary: bool) -> ProxyTimelineSegment:
        if end_boundary:
            for segment in self.segments:
                if segment.proxy_range.start_pts < proxy_pts <= segment.proxy_range.end_pts:
                    return segment
        else:
            for segment in self.segments:
                if segment.proxy_range.start_pts <= proxy_pts < segment.proxy_range.end_pts:
                    return segment
        if proxy_pts == self.proxy_range.start_pts:
            return self.segments[0]
        if proxy_pts == self.proxy_range.end_pts:
            return self.segments[-1]
        raise VlmValidationError("proxy tick is outside the timeline map")

    def map_point_bounds(self, proxy_pts: object) -> tuple[int, int]:
        """Return inclusive conservative Source bounds for one proxy tick."""

        tick = require_pts(proxy_pts, "proxy_pts")
        if not self.proxy_range.start_pts <= tick < self.proxy_range.end_pts:
            raise VlmValidationError("proxy point is outside the timeline map")
        segment = self._segment_for_boundary(tick, end_boundary=False)
        lower = _map_segment_floor(segment, tick) - segment.max_source_error_pts
        upper = _map_segment_ceil(segment, tick) + segment.max_source_error_pts
        return (
            max(self.source_range.start_pts, lower),
            min(self.source_range.end_pts - 1, upper),
        )

    def map_interval(
        self,
        proxy_interval: TickRange,
        *,
        provider_uncertainty_proxy_pts: int = 0,
    ) -> MappedSourceInterval:
        """Conservatively map an interval, propagating quantization uncertainty."""

        if type(proxy_interval) is not TickRange:  # noqa: E721
            raise VlmValidationError("proxy_interval must be a TickRange")
        uncertainty = _non_negative(
            provider_uncertainty_proxy_pts,
            "provider_uncertainty_proxy_pts",
        )
        if not self.proxy_range.contains(proxy_interval):
            raise VlmValidationError("proxy interval is outside the timeline map")
        expanded_start = max(self.proxy_range.start_pts, proxy_interval.start_pts - uncertainty)
        expanded_end = min(self.proxy_range.end_pts, proxy_interval.end_pts + uncertainty)
        start_segment = self._segment_for_boundary(expanded_start, end_boundary=False)
        end_segment = self._segment_for_boundary(expanded_end, end_boundary=True)
        start = _map_segment_floor(start_segment, expanded_start) - start_segment.max_source_error_pts
        end = _map_segment_ceil(end_segment, expanded_end) + end_segment.max_source_error_pts
        start = max(self.source_range.start_pts, start)
        end = min(self.source_range.end_pts, end)
        if start >= end:
            raise VlmValidationError("mapped Source interval is empty")
        return MappedSourceInterval(
            coarse_range=TickRange(start, end),
            mapping_error_bound_source_pts=max(
                segment.max_source_error_pts
                for segment in self.segments
                if segment.proxy_range.start_pts < expanded_end
                and expanded_start < segment.proxy_range.end_pts
            ),
            source_time_base=self.source_time_base,
            provider_uncertainty_proxy_pts=uncertainty,
            proxy_time_base=self.proxy_time_base,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "certificate_kind": self.certificate_kind,
            "proxy_time_base": {
                "denominator": self.proxy_time_base.denominator,
                "numerator": self.proxy_time_base.numerator,
            },
            "segments": [item.to_mapping() for item in self.segments],
            "source_time_base": {
                "denominator": self.source_time_base.denominator,
                "numerator": self.source_time_base.numerator,
            },
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class WindowFrameSample:
    """One Kernel-extracted proxy frame bound to its exact Source PTS and bytes."""

    source_pts: int
    proxy_pts: int
    frame_sha256: str

    def __post_init__(self) -> None:
        require_pts(self.source_pts, "frame_sample.source_pts")
        require_pts(self.proxy_pts, "frame_sample.proxy_pts")
        sha256_prefixed(self.frame_sha256, "frame_sample.frame_sha256")

    def to_mapping(self) -> dict[str, object]:
        return {
            "frame_sha256": self.frame_sha256,
            "proxy_pts": self.proxy_pts,
            "source_pts": self.source_pts,
        }

    @property
    def frame_id(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class WindowProxyBlobRef:
    """Kernel-neutral immutable proxy bytes identity with no storage locator."""

    object_id: str
    content_hash: str
    byte_length: int
    media_type: str

    def __post_init__(self) -> None:
        _opaque_id(self.object_id, "proxy_blob.object_id")
        sha256_prefixed(self.content_hash, "proxy_blob.content_hash")
        if require_pts(self.byte_length, "proxy_blob.byte_length") <= 0:
            raise VlmValidationError("proxy_blob.byte_length must be positive")
        if type(self.media_type) is not str or not _MEDIA_TYPE.fullmatch(self.media_type):  # noqa: E721
            raise VlmValidationError("proxy_blob.media_type must be a canonical media type")

    def to_mapping(self) -> dict[str, object]:
        return {
            "byte_length": self.byte_length,
            "content_hash": self.content_hash,
            "media_type": self.media_type,
            "object_id": self.object_id,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class WindowManifest:
    """A Kernel-owned VLM window with a non-overlapping semantic core."""

    source_id: str
    source_clock_id: str
    source_sha256: str
    stream_index: int
    source_time_base: TimeBase
    source_range: TickRange
    core_range: TickRange
    frame_pts_index_set: FramePtsIndexSet
    proxy_blob_ref: WindowProxyBlobRef
    preprocess_policy_sha256: str
    window_sampling_policy_sha256: str
    timeline_map: ProxyTimelineMap
    frame_samples: tuple[WindowFrameSample, ...]

    def __post_init__(self) -> None:
        _opaque_id(self.source_id, "window.source_id")
        _opaque_id(self.source_clock_id, "window.source_clock_id")
        sha256_prefixed(self.source_sha256, "window.source_sha256")
        if require_pts(self.stream_index, "window.stream_index") < 0:
            raise VlmValidationError("window.stream_index must be non-negative")
        if type(self.source_time_base) is not TimeBase:  # noqa: E721
            raise VlmValidationError("window.source_time_base must be a TimeBase")
        if type(self.source_range) is not TickRange or type(self.core_range) is not TickRange:  # noqa: E721
            raise VlmValidationError("window Source/core ranges must be TickRange values")
        if not self.source_range.contains(self.core_range):
            raise VlmValidationError("window core range must be contained in the context range")
        if type(self.frame_pts_index_set) is not FramePtsIndexSet:  # noqa: E721
            raise VlmValidationError(
                "window.frame_pts_index_set must be an exact FramePtsIndexSet"
            )
        frame_context = self.frame_pts_index_set.context
        if (
            frame_context.source_id != self.source_id
            or frame_context.source_sha256 != self.source_sha256
            or frame_context.clock_id != self.source_clock_id
            or frame_context.time_base != self.source_time_base
        ):
            raise VlmValidationError(
                "window FramePtsIndexSet must bind the exact Source video clock"
            )
        if (
            self.source_range.start_pts < frame_context.origin_tick
            or self.source_range.end_pts > frame_context.end_tick
        ):
            raise VlmValidationError(
                "window context range must stay within the FramePtsIndexSet clock"
            )
        if type(self.proxy_blob_ref) is not WindowProxyBlobRef:  # noqa: E721
            raise VlmValidationError("window.proxy_blob_ref must be a WindowProxyBlobRef")
        sha256_prefixed(
            self.preprocess_policy_sha256,
            "window.preprocess_policy_sha256",
        )
        sha256_prefixed(
            self.window_sampling_policy_sha256,
            "window.window_sampling_policy_sha256",
        )
        if type(self.timeline_map) is not ProxyTimelineMap:  # noqa: E721
            raise VlmValidationError("window.timeline_map must be a ProxyTimelineMap")
        if self.timeline_map.source_time_base != self.source_time_base:
            raise VlmValidationError("window and timeline map Source time bases must match")
        if self.timeline_map.source_range != self.source_range:
            raise VlmValidationError("window context range must equal the timeline map Source coverage")
        samples = tuple(self.frame_samples)
        if not samples or any(type(item) is not WindowFrameSample for item in samples):  # noqa: E721
            raise VlmValidationError("window.frame_samples must contain WindowFrameSample values")
        if any(left.proxy_pts >= right.proxy_pts for left, right in zip(samples, samples[1:], strict=False)):
            raise VlmValidationError("window frame proxy PTS must be strictly increasing")
        if any(left.source_pts >= right.source_pts for left, right in zip(samples, samples[1:], strict=False)):
            raise VlmValidationError("window frame Source PTS must be strictly increasing")
        for sample in samples:
            if not self.source_range.start_pts <= sample.source_pts < self.source_range.end_pts:
                raise VlmValidationError("window frame Source PTS is outside the context range")
            if not self.frame_pts_index_set.pts_index.contains(sample.source_pts):
                raise VlmValidationError(
                    "window frame Source PTS must be a member of the exact FramePtsIndexSet"
                )
            lower, upper = self.timeline_map.map_point_bounds(sample.proxy_pts)
            if not lower <= sample.source_pts <= upper:
                raise VlmValidationError("window frame does not satisfy the certified timeline map")
        frame_ids = tuple(item.frame_id for item in samples)
        if len(frame_ids) != len(set(frame_ids)):
            raise VlmValidationError("window frame identities must be unique")
        object.__setattr__(self, "frame_samples", samples)

    @property
    def frame_samples_sha256(self) -> str:
        return canonical_sha256([item.to_mapping() for item in self.frame_samples])

    @property
    def frame_pts_index_set_sha256(self) -> str:
        return self.frame_pts_index_set.canonical_hash

    @property
    def frame_by_id(self) -> dict[str, WindowFrameSample]:
        return {item.frame_id: item for item in self.frame_samples}

    def owns_interval(self, source_interval: TickRange) -> bool:
        """Use the lower midpoint of a coarse half-open interval as its core owner."""

        if type(source_interval) is not TickRange:  # noqa: E721
            raise VlmValidationError("source_interval must be a TickRange")
        if not self.source_range.contains(source_interval):
            raise VlmValidationError("source interval is outside the window context")
        anchor = source_interval.start_pts + (source_interval.duration_pts - 1) // 2
        return self.core_range.start_pts <= anchor < self.core_range.end_pts

    def to_mapping(self) -> dict[str, object]:
        return {
            "core_range": {"end_pts": self.core_range.end_pts, "start_pts": self.core_range.start_pts},
            "frame_samples": [item.to_mapping() for item in self.frame_samples],
            "frame_pts_index_set_sha256": self.frame_pts_index_set_sha256,
            "preprocess_policy_sha256": self.preprocess_policy_sha256,
            "proxy_blob_ref": self.proxy_blob_ref.to_mapping(),
            "source_clock_id": self.source_clock_id,
            "source_id": self.source_id,
            "source_range": {"end_pts": self.source_range.end_pts, "start_pts": self.source_range.start_pts},
            "source_sha256": self.source_sha256,
            "source_time_base": {
                "denominator": self.source_time_base.denominator,
                "numerator": self.source_time_base.numerator,
            },
            "stream_index": self.stream_index,
            "timeline_map": self.timeline_map.to_mapping(),
            "window_sampling_policy_sha256": self.window_sampling_policy_sha256,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def window_id(self) -> str:
        return self.canonical_hash


@dataclass(frozen=True, slots=True)
class WindowManifestSet:
    """A complete, gap-free set of ordered semantic cores for one Source stream."""

    source_id: str
    source_clock_id: str
    source_sha256: str
    stream_index: int
    source_time_base: TimeBase
    declared_source_range: TickRange
    manifests: tuple[WindowManifest, ...]

    def __post_init__(self) -> None:
        _opaque_id(self.source_id, "window_set.source_id")
        _opaque_id(self.source_clock_id, "window_set.source_clock_id")
        sha256_prefixed(self.source_sha256, "window_set.source_sha256")
        if require_pts(self.stream_index, "window_set.stream_index") < 0:
            raise VlmValidationError("window_set.stream_index must be non-negative")
        if type(self.source_time_base) is not TimeBase:  # noqa: E721
            raise VlmValidationError("window_set.source_time_base must be a TimeBase")
        if type(self.declared_source_range) is not TickRange:  # noqa: E721
            raise VlmValidationError("window_set.declared_source_range must be a TickRange")
        manifests = tuple(self.manifests)
        if not manifests or any(type(item) is not WindowManifest for item in manifests):  # noqa: E721
            raise VlmValidationError("window_set.manifests must contain WindowManifest values")
        for manifest in manifests:
            if (
                manifest.source_id != self.source_id
                or manifest.source_clock_id != self.source_clock_id
                or manifest.source_sha256 != self.source_sha256
                or manifest.stream_index != self.stream_index
                or manifest.source_time_base != self.source_time_base
                or manifest.frame_pts_index_set_sha256
                != manifests[0].frame_pts_index_set_sha256
            ):
                raise VlmValidationError(
                    "every window manifest must bind the exact WindowManifestSet Source stream clock"
                )
        if manifests[0].core_range.start_pts != self.declared_source_range.start_pts:
            raise VlmValidationError("window cores must start at the declared Source range")
        for left, right in zip(manifests, manifests[1:], strict=False):
            if left.core_range.end_pts != right.core_range.start_pts:
                raise VlmValidationError("window cores must be ordered, non-overlapping, and gap-free")
        if manifests[-1].core_range.end_pts != self.declared_source_range.end_pts:
            raise VlmValidationError("window cores must end at the declared Source range")
        object.__setattr__(self, "manifests", manifests)

    @property
    def frame_pts_index_set_sha256(self) -> str:
        return self.manifests[0].frame_pts_index_set_sha256

    def require_member(self, manifest: WindowManifest) -> None:
        """Require exact immutable membership, not a caller-supplied window hash."""

        if type(manifest) is not WindowManifest or manifest not in self.manifests:  # noqa: E721
            raise VlmValidationError(
                "request WindowManifest must be an exact WindowManifestSet member"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "declared_source_range": {
                "end_pts": self.declared_source_range.end_pts,
                "start_pts": self.declared_source_range.start_pts,
            },
            "manifest_hashes": [item.canonical_hash for item in self.manifests],
            "frame_pts_index_set_sha256": self.frame_pts_index_set_sha256,
            "source_clock_id": self.source_clock_id,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "source_time_base": {
                "denominator": self.source_time_base.denominator,
                "numerator": self.source_time_base.numerator,
            },
            "stream_index": self.stream_index,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def select_core_owner(
    manifest_set: WindowManifestSet,
    source_interval: TickRange,
) -> WindowManifest:
    """Select one owner only from a previously validated complete manifest set."""

    if type(manifest_set) is not WindowManifestSet:  # noqa: E721
        raise VlmValidationError("core ownership requires a WindowManifestSet")
    if type(source_interval) is not TickRange or not manifest_set.declared_source_range.contains(  # noqa: E721
        source_interval
    ):
        raise VlmValidationError("coarse interval must be inside the declared Source range")
    owners = [
        item
        for item in manifest_set.manifests
        if item.source_range.contains(source_interval) and item.owns_interval(source_interval)
    ]
    if len(owners) != 1:
        raise VlmValidationError("coarse interval must have exactly one Kernel-owned core")
    return owners[0]
