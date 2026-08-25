"""Closed decoder for committed whole-series Source/Window manifests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast
from uuid import UUID

from ..media import (
    AudioBoundaryMethod,
    AudioSampleBoundary,
    AudioSampleBoundarySet,
    AudioSourceOutcome,
    Coverage,
    CoverageOutcome,
    EvidenceCompleteness,
    EvidenceContext,
    FramePtsIndexSet,
    MediaKind,
    PTSIndex,
    TickRange,
    TimeBase,
)
from ..media.ffprobe_port import ProbeResult
from ..media.stage4_predecessor import (
    PresentationProbeExecution,
    PresentationSegmentContinuity,
    PresentationTimelineProbe,
    PresentationTrack,
    PresentationTrackSegment,
    RationalPresentationInterval,
)
from ..media.types import (
    ToolEvidence,
    VideoStreamEvidence,
    canonical_sha256,
    sha256_prefixed,
)
from ..vlm import (
    WindowFrameSample,
    WindowManifest,
    WindowManifestSet,
    WindowProxyBlobRef,
)
from ..vlm.window import ProxyTimelineMap, ProxyTimelineSegment

IDENTITY_FRAME_GENERATION_POLICY_SHA256 = canonical_sha256(
    {
        "endpoint_rule": "complete-decoded-frame-pts-membership-v1",
        "operation": "identity",
        "producer": "identity-source-window-v2",
    }
)


class SourceManifestDecodeError(ValueError):
    """A committed SourceManifest cannot be reconstructed exactly."""

    code = "SOURCE_MANIFEST_INVALID"


SourceOperationPurpose = Literal["semantic_analysis", "render_source"]
SOURCE_OPERATION_POLICY_SCHEMA_VERSION = "source-operation-policy-v1"
_PURPOSE_REGISTRY: tuple[SourceOperationPurpose, ...] = (
    "semantic_analysis",
    "render_source",
)


class SourcePurposeDeniedError(ValueError):
    """The exact Source operation grant does not authorize a purpose."""

    code = "SOURCE_PURPOSE_DENIED"


@dataclass(frozen=True, slots=True)
class SourceOperationPolicy:
    """Closed authorization policy supplied at the local locator boundary."""

    authorization_id: str
    series_id: str
    expected_source_count: int
    authorized_purposes: tuple[SourceOperationPurpose, ...]
    schema_version: str = field(
        default=SOURCE_OPERATION_POLICY_SCHEMA_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        if self.schema_version != SOURCE_OPERATION_POLICY_SCHEMA_VERSION:
            raise ValueError("source operation policy schema version is unsupported")
        if not self.authorization_id.strip() or not self.series_id.strip():
            raise ValueError("source operation policy identities must be non-empty")
        if (
            type(self.expected_source_count) is not int  # noqa: E721
            or self.expected_source_count < 1
        ):
            raise ValueError("source operation expected_source_count must be positive")
        if type(self.authorized_purposes) is not tuple:  # noqa: E721
            raise ValueError("source operation authorized_purposes must be a tuple")
        supplied = self.authorized_purposes
        if not supplied:
            raise ValueError("source operation authorized_purposes must be non-empty")
        if len(supplied) != len(set(supplied)):
            raise ValueError("source operation authorized_purposes must not contain duplicates")
        if any(purpose not in _PURPOSE_REGISTRY for purpose in supplied):
            raise ValueError("source operation purpose is not registered")
        canonical = tuple(purpose for purpose in _PURPOSE_REGISTRY if purpose in supplied)
        object.__setattr__(self, "authorized_purposes", canonical)

    def to_mapping(self) -> dict[str, object]:
        return {
            "authorization_id": self.authorization_id,
            "authorized_purposes": list(self.authorized_purposes),
            "expected_source_count": self.expected_source_count,
            "schema_version": self.schema_version,
            "series_id": self.series_id,
        }

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self.to_mapping())

    def require_purpose(self, purpose: SourceOperationPurpose) -> None:
        if purpose not in _PURPOSE_REGISTRY or purpose not in self.authorized_purposes:
            raise SourcePurposeDeniedError(f"source operation purpose is not authorized: {purpose}")


class BlobIdentity(Protocol):
    @property
    def object_id(self) -> UUID: ...

    @property
    def content_hash(self) -> str: ...

    @property
    def byte_length(self) -> int: ...

    @property
    def media_type(self) -> str: ...


@dataclass(frozen=True, slots=True)
class DecodedBlobRef:
    object_id: UUID
    content_hash: str
    byte_length: int
    media_type: str

    def __post_init__(self) -> None:
        sha256_prefixed(self.content_hash, "blob.content_hash")
        if type(self.byte_length) is not int or self.byte_length < 0:  # noqa: E721
            raise ValueError("blob byte_length must be non-negative")
        if not self.media_type.strip():
            raise ValueError("blob media_type must be non-empty")

    def to_mapping(self) -> dict[str, object]:
        return {
            "byte_length": self.byte_length,
            "content_hash": self.content_hash,
            "media_type": self.media_type,
            "object_id": str(self.object_id),
        }


@dataclass(frozen=True, slots=True)
class DecodedSeriesSource:
    relative_path: str
    source_id: str
    content_sha256: str
    byte_size: int

    def __post_init__(self) -> None:
        if not self.relative_path or self.relative_path.startswith(("/", "../")):
            raise ValueError("source relative_path escapes the authorized root")
        if not self.source_id.strip():
            raise ValueError("source_id must be non-empty")
        sha256_prefixed(self.content_sha256, "source.content_sha256")
        if type(self.byte_size) is not int or self.byte_size < 1:  # noqa: E721
            raise ValueError("source byte_size must be positive")

    def to_mapping(self) -> dict[str, object]:
        return {
            "byte_size": self.byte_size,
            "content_sha256": self.content_sha256,
            "relative_path": self.relative_path,
            "source_id": self.source_id,
        }


@dataclass(frozen=True, slots=True)
class SourceOperationGrant:
    policy: SourceOperationPolicy
    completion_policy: str
    sources: tuple[DecodedSeriesSource, ...]

    def __post_init__(self) -> None:
        if type(self.policy) is not SourceOperationPolicy:  # noqa: E721
            raise ValueError("source operation grant requires an exact policy")
        sources = tuple(self.sources)
        if self.completion_policy != "all_or_nothing":
            raise ValueError("whole-series completion policy is invalid")
        paths = tuple(source.relative_path for source in sources)
        if not sources or paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("grant sources must be non-empty, sorted, and unique")
        if len(sources) != self.policy.expected_source_count:
            raise ValueError("grant source count does not match its authorization policy")
        object.__setattr__(self, "sources", sources)

    def to_mapping(self) -> dict[str, object]:
        return {
            "authorization_id": self.policy.authorization_id,
            "authorization_policy_schema_version": self.policy.schema_version,
            "authorization_policy_sha256": self.policy.policy_sha256,
            "authorized_purposes": list(self.policy.authorized_purposes),
            "completion_policy": self.completion_policy,
            "expected_source_count": self.policy.expected_source_count,
            "series_id": self.policy.series_id,
            "sources": [source.to_mapping() for source in self.sources],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def require_purpose(self, purpose: SourceOperationPurpose) -> None:
        self.policy.require_purpose(purpose)


@dataclass(frozen=True, slots=True)
class DecodedMediaProbe:
    source: DecodedSeriesSource
    video_probe: ProbeResult
    video_range: TickRange
    audio_sample_boundaries: AudioSampleBoundarySet
    frame_detector_sha256: str
    audio_detector_sha256: str
    presentation_timeline_probe: PresentationTimelineProbe | None = None
    presentation_video_frame_boundaries: tuple[tuple[int, int], ...] = ()
    presentation_audio_frame_boundaries: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        sha256_prefixed(self.frame_detector_sha256, "frame_detector_sha256")
        sha256_prefixed(self.audio_detector_sha256, "audio_detector_sha256")

    def to_mapping(self) -> dict[str, object]:
        stream = self.video_probe.video_stream
        result: dict[str, object] = {
            "audio_sample_boundaries": self.audio_sample_boundaries.to_mapping(),
            "decoded_video_frame_pts": list(self.video_probe.pts_index.ticks),
            "ffprobe": {
                "audio_detector_sha256": self.audio_detector_sha256,
                "executable": "ffprobe",
                "frame_detector_sha256": self.frame_detector_sha256,
                "stderr_sha256": self.video_probe.tool.stderr_sha256,
                "version": self.video_probe.tool.version,
            },
            "source": self.source.to_mapping(),
            "video_stream": {
                "codec_name": stream.codec_name,
                "duration_tick": self.video_range.duration_pts,
                "end_tick": self.video_range.end_pts,
                "height": stream.height,
                "index": stream.stream_index,
                "start_tick": self.video_range.start_pts,
                "time_base": {
                    "denominator": stream.time_base.denominator,
                    "numerator": stream.time_base.numerator,
                },
                "width": stream.width,
            },
        }
        if self.presentation_timeline_probe is not None:
            result["presentation_timeline_probe"] = self.presentation_timeline_probe.to_mapping()
            result["decoded_video_frame_boundaries"] = _frame_boundaries_mapping(
                self.presentation_video_frame_boundaries
            )
            result["decoded_audio_frame_boundaries"] = _frame_boundaries_mapping(
                self.presentation_audio_frame_boundaries
            )
        return result


@dataclass(frozen=True, slots=True)
class DecodedSourceEpisode:
    media_probe: DecodedMediaProbe
    proxy_blob: DecodedBlobRef
    manifest: WindowManifest
    manifest_set: WindowManifestSet

    def to_mapping(self) -> dict[str, object]:
        return {
            "media_probe": self.media_probe.to_mapping(),
            "proxy_blob": self.proxy_blob.to_mapping(),
            "window_manifest": self.manifest.to_mapping(),
            "window_manifest_set": self.manifest_set.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class DecodedSourceManifest:
    census: SourceOperationGrant
    episodes: tuple[DecodedSourceEpisode, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "census": self.census.to_mapping(),
            "census_sha256": self.census.canonical_hash,
            "completion_policy": self.census.completion_policy,
            "episodes": [episode.to_mapping() for episode in self.episodes],
        }


class _SourceLike(Protocol):
    @property
    def source_id(self) -> str: ...

    @property
    def content_sha256(self) -> str: ...


class _ProbeLike(Protocol):
    @property
    def source(self) -> _SourceLike: ...

    @property
    def video_probe(self) -> ProbeResult: ...

    @property
    def video_range(self) -> TickRange: ...


def identity_frame_index(probe: _ProbeLike) -> FramePtsIndexSet:
    """Rebuild the canonical identity frame index from exact probe evidence."""

    stream = probe.video_probe.video_stream
    clock_id = f"video-stream-{stream.stream_index}"
    context = EvidenceContext(
        probe.source.source_id,
        probe.source.content_sha256,
        MediaKind.VIDEO,
        clock_id,
        stream.time_base,
        probe.video_range.start_pts,
        probe.video_range.duration_pts,
        "identity-source-window-v2",
        IDENTITY_FRAME_GENERATION_POLICY_SHA256,
    )
    coverage = Coverage(
        probe.source.source_id,
        probe.source.content_sha256,
        clock_id,
        stream.time_base,
        probe.video_range.start_pts,
        probe.video_range.end_pts,
        CoverageOutcome.COMPLETE,
    )
    return FramePtsIndexSet(
        "identity-source-frame-pts-v2",
        context,
        coverage,
        probe.video_probe.pts_index,
        canonical_sha256(list(probe.video_probe.pts_index.ticks)),
    )


def decode_source_manifest(
    payload_json: str,
    proxy_blobs: tuple[BlobIdentity, ...],
) -> DecodedSourceManifest:
    """Strictly decode a V2 committed SourceManifest payload.

    V2 consumers, including timed-media preflight, require the source-prep
    presentation facts and both decoded frame-boundary sequences for every
    episode.  Call :func:`decode_legacy_source_manifest` only at an explicit
    legacy compatibility boundary.
    """

    decoded = decode_legacy_source_manifest(payload_json, proxy_blobs)
    for episode in decoded.episodes:
        probe = episode.media_probe
        if (
            probe.presentation_timeline_probe is None
            or not probe.presentation_video_frame_boundaries
            or not probe.presentation_audio_frame_boundaries
        ):
            raise SourceManifestDecodeError(
                "V2 source manifest requires presentation timeline probe and decoded frame boundaries"
            )
    return decoded


def decode_legacy_source_manifest(
    payload_json: str,
    proxy_blobs: tuple[BlobIdentity, ...],
) -> DecodedSourceManifest:
    """Strictly decode a legacy SourceManifest at an explicit legacy boundary."""

    try:
        raw: object = json.loads(
            payload_json,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {value}")
            ),
        )
        root = _closed_mapping(
            raw,
            {"census", "census_sha256", "completion_policy", "episodes"},
            "source manifest",
        )
        census = _decode_census(root["census"])
        if (
            _text_value(root["census_sha256"], "census_sha256") != census.canonical_hash
            or root["completion_policy"] != census.completion_policy
        ):
            raise ValueError("source manifest census certificate is invalid")
        episodes_raw = _array(root["episodes"], "episodes")
        if len(episodes_raw) != len(census.sources) or len(episodes_raw) != len(proxy_blobs):
            raise ValueError("source manifest episode count is inconsistent")
        episodes = tuple(
            _decode_episode(raw_episode, source, blob)
            for raw_episode, source, blob in zip(
                episodes_raw, census.sources, proxy_blobs, strict=True
            )
        )
        decoded = DecodedSourceManifest(census, episodes)
        if decoded.to_mapping() != root:
            raise ValueError("source manifest is not the canonical prepared mapping")
        return decoded
    except SourceManifestDecodeError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise SourceManifestDecodeError(
            "committed source manifest failed strict decoding"
        ) from error


def _decode_census(value: object) -> SourceOperationGrant:
    raw = _closed_mapping(
        value,
        {
            "authorization_id",
            "authorization_policy_schema_version",
            "authorization_policy_sha256",
            "authorized_purposes",
            "completion_policy",
            "expected_source_count",
            "series_id",
            "sources",
        },
        "census",
    )
    sources = tuple(
        DecodedSeriesSource(
            _text_value(item["relative_path"], "source.relative_path"),
            _text_value(item["source_id"], "source.source_id"),
            _text_value(item["content_sha256"], "source.content_sha256"),
            _int_value(item["byte_size"], "source.byte_size"),
        )
        for item in (
            _closed_mapping(
                entry,
                {"relative_path", "source_id", "content_sha256", "byte_size"},
                "source",
            )
            for entry in _array(raw["sources"], "census.sources")
        )
    )
    purposes = tuple(
        _text_value(item, "census.authorized_purposes")
        for item in _array(raw["authorized_purposes"], "census.authorized_purposes")
    )
    policy_schema_version = _text_value(
        raw["authorization_policy_schema_version"],
        "census.authorization_policy_schema_version",
    )
    if policy_schema_version != SOURCE_OPERATION_POLICY_SCHEMA_VERSION:
        raise ValueError("census authorization policy schema version is unsupported")
    policy = SourceOperationPolicy(
        authorization_id=_text_value(raw["authorization_id"], "census.authorization_id"),
        series_id=_text_value(raw["series_id"], "census.series_id"),
        expected_source_count=_int_value(
            raw["expected_source_count"],
            "census.expected_source_count",
        ),
        authorized_purposes=cast(tuple[SourceOperationPurpose, ...], purposes),
    )
    if (
        _text_value(
            raw["authorization_policy_sha256"],
            "census.authorization_policy_sha256",
        )
        != policy.policy_sha256
    ):
        raise ValueError("census authorization policy hash is invalid")
    result = SourceOperationGrant(
        policy,
        _text_value(raw["completion_policy"], "census.completion_policy"),
        sources,
    )
    if result.to_mapping() != raw:
        raise ValueError("census is not canonical")
    return result


def _decode_episode(
    value: object,
    source: DecodedSeriesSource,
    durable_blob: BlobIdentity,
) -> DecodedSourceEpisode:
    raw = _closed_mapping(
        value,
        {"media_probe", "proxy_blob", "window_manifest", "window_manifest_set"},
        "episode",
    )
    declared_blob = _decode_blob(raw["proxy_blob"])
    decoded_durable_blob = _copy_blob_identity(durable_blob)
    if declared_blob != decoded_durable_blob:
        raise ValueError("episode proxy BlobRef does not match durable storage")
    probe = _decode_probe(raw["media_probe"], source)
    manifest = _decode_manifest(raw["window_manifest"], probe, decoded_durable_blob)
    manifest_set = _decode_manifest_set(raw["window_manifest_set"], manifest)
    _validate_presentation_timeline_probe(probe, decoded_durable_blob, manifest)
    return DecodedSourceEpisode(probe, decoded_durable_blob, manifest, manifest_set)


def _decode_probe(
    value: object,
    source: DecodedSeriesSource,
) -> DecodedMediaProbe:
    raw = _closed_mapping(
        value,
        {
            "audio_sample_boundaries",
            "decoded_audio_frame_boundaries",
            "decoded_video_frame_pts",
            "decoded_video_frame_boundaries",
            "ffprobe",
            "source",
            "video_stream",
            "presentation_timeline_probe",
        },
        "media_probe",
        optional={
            "presentation_timeline_probe",
            "decoded_video_frame_boundaries",
            "decoded_audio_frame_boundaries",
        },
    )
    if raw["source"] != source.to_mapping():
        raise ValueError("media probe source identity is inconsistent")
    video = _closed_mapping(
        raw["video_stream"],
        {
            "codec_name",
            "duration_tick",
            "end_tick",
            "height",
            "index",
            "start_tick",
            "time_base",
            "width",
        },
        "video_stream",
    )
    time_base = _decode_time_base(video["time_base"], "video.time_base")
    video_range = TickRange(
        _int_value(video["start_tick"], "video.start_tick"),
        _int_value(video["end_tick"], "video.end_tick"),
    )
    if _int_value(video["duration_tick"], "video.duration_tick") != video_range.duration_pts:
        raise ValueError("video duration is inconsistent")
    tool = _closed_mapping(
        raw["ffprobe"],
        {
            "audio_detector_sha256",
            "executable",
            "frame_detector_sha256",
            "stderr_sha256",
            "version",
        },
        "ffprobe",
    )
    if tool["executable"] != "ffprobe":
        raise ValueError("ffprobe executable identity is not canonical")
    video_probe = ProbeResult(
        VideoStreamEvidence(
            _int_value(video["index"], "video.index"),
            _text_value(video["codec_name"], "video.codec_name"),
            _int_value(video["width"], "video.width"),
            _int_value(video["height"], "video.height"),
            time_base,
        ),
        PTSIndex(
            tuple(
                _int_value(item, "decoded_video_frame_pts")
                for item in _array(raw["decoded_video_frame_pts"], "decoded_video_frame_pts")
            )
        ),
        ToolEvidence(
            _text_value(tool["executable"], "ffprobe.executable"),
            _text_value(tool["version"], "ffprobe.version"),
            _text_value(tool["stderr_sha256"], "ffprobe.stderr_sha256"),
        ),
    )
    if any(
        not video_range.start_pts <= tick < video_range.end_pts
        for tick in video_probe.pts_index.ticks
    ):
        raise ValueError("decoded video PTS is outside the half-open stream range")
    audio = _decode_audio_boundaries(raw["audio_sample_boundaries"])
    context = audio.context
    coverage = audio.coverage
    if (
        context.source_id != source.source_id
        or context.source_sha256 != source.content_sha256
        or coverage.source_id != source.source_id
        or coverage.source_sha256 != source.content_sha256
        or coverage.clock_id != context.clock_id
        or coverage.time_base != context.time_base
        or coverage.in_tick != context.origin_tick
        or coverage.out_tick != context.end_tick
        or any(
            point.source_id != source.source_id
            or point.source_sha256 != source.content_sha256
            or point.clock_id != context.clock_id
            or point.time_base != context.time_base
            or not context.origin_tick <= point.tick <= context.end_tick
            for point in audio.points
        )
    ):
        raise ValueError("audio evidence is not bound to the episode source clock")
    has_presentation_facts = "presentation_timeline_probe" in raw
    has_boundaries = {
        "decoded_video_frame_boundaries",
        "decoded_audio_frame_boundaries",
    } <= set(raw)
    if has_presentation_facts != has_boundaries:
        raise ValueError(
            "presentation timeline facts require complete decoded frame boundary evidence"
        )
    result = DecodedMediaProbe(
        source,
        video_probe,
        video_range,
        audio,
        _text_value(tool["frame_detector_sha256"], "ffprobe.frame_detector_sha256"),
        _text_value(tool["audio_detector_sha256"], "ffprobe.audio_detector_sha256"),
        _decode_presentation_timeline_probe(raw["presentation_timeline_probe"])
        if has_presentation_facts
        else None,
        _decode_frame_boundaries(
            raw["decoded_video_frame_boundaries"], "decoded_video_frame_boundaries"
        )
        if has_presentation_facts
        else (),
        _decode_frame_boundaries(
            raw["decoded_audio_frame_boundaries"], "decoded_audio_frame_boundaries"
        )
        if has_presentation_facts
        else (),
    )
    if result.to_mapping() != raw:
        raise ValueError("media probe is not canonical")
    return result


def _decode_audio_boundaries(value: object) -> AudioSampleBoundarySet:
    raw = _closed_mapping(
        value,
        {"audio_sample_boundary_set_id", "context", "coverage", "points", "source_outcome"},
        "audio boundaries",
    )
    context_raw = _closed_mapping(
        raw["context"],
        {
            "clock_id",
            "duration_tick",
            "generation_policy_sha256",
            "media_kind",
            "origin_tick",
            "producer_id",
            "source_id",
            "source_sha256",
            "time_base",
        },
        "audio context",
    )
    context = EvidenceContext(
        _text_value(context_raw["source_id"], "audio.context.source_id"),
        _text_value(context_raw["source_sha256"], "audio.context.source_sha256"),
        MediaKind(_text_value(context_raw["media_kind"], "audio.context.media_kind")),
        _text_value(context_raw["clock_id"], "audio.context.clock_id"),
        _decode_time_base(context_raw["time_base"], "audio.context.time_base"),
        _int_value(context_raw["origin_tick"], "audio.context.origin_tick"),
        _int_value(context_raw["duration_tick"], "audio.context.duration_tick"),
        _text_value(context_raw["producer_id"], "audio.context.producer_id"),
        _text_value(
            context_raw["generation_policy_sha256"], "audio.context.generation_policy_sha256"
        ),
    )
    coverage_raw = _closed_mapping(
        raw["coverage"],
        {
            "clock_id",
            "diagnostics",
            "in_tick",
            "out_tick",
            "outcome",
            "source_id",
            "source_sha256",
            "time_base",
        },
        "audio coverage",
    )
    if _array(coverage_raw["diagnostics"], "audio.coverage.diagnostics"):
        raise ValueError("complete source audio coverage cannot contain diagnostics")
    coverage = Coverage(
        _text_value(coverage_raw["source_id"], "audio.coverage.source_id"),
        _text_value(coverage_raw["source_sha256"], "audio.coverage.source_sha256"),
        _text_value(coverage_raw["clock_id"], "audio.coverage.clock_id"),
        _decode_time_base(coverage_raw["time_base"], "audio.coverage.time_base"),
        _int_value(coverage_raw["in_tick"], "audio.coverage.in_tick"),
        _int_value(coverage_raw["out_tick"], "audio.coverage.out_tick"),
        CoverageOutcome(_text_value(coverage_raw["outcome"], "audio.coverage.outcome")),
    )
    points = tuple(
        AudioSampleBoundary(
            _text_value(item["boundary_id"], "audio.boundary_id"),
            _text_value(item["source_id"], "audio.source_id"),
            _text_value(item["source_sha256"], "audio.source_sha256"),
            _text_value(item["clock_id"], "audio.clock_id"),
            _decode_time_base(item["time_base"], "audio.time_base"),
            _int_value(item["tick"], "audio.tick"),
            AudioBoundaryMethod(_text_value(item["method"], "audio.method")),
        )
        for item in (
            _closed_mapping(
                entry,
                {
                    "boundary_id",
                    "clock_id",
                    "method",
                    "source_id",
                    "source_sha256",
                    "tick",
                    "time_base",
                },
                "audio boundary",
            )
            for entry in _array(raw["points"], "audio.points")
        )
    )
    result = AudioSampleBoundarySet(
        _text_value(raw["audio_sample_boundary_set_id"], "audio_sample_boundary_set_id"),
        context,
        coverage,
        AudioSourceOutcome(_text_value(raw["source_outcome"], "audio.source_outcome")),
        points,
    )
    if result.to_mapping() != raw:
        raise ValueError("audio boundaries are not canonical")
    return result


def _decode_manifest(
    value: object,
    probe: DecodedMediaProbe,
    blob: DecodedBlobRef,
) -> WindowManifest:
    raw = _closed_mapping(
        value,
        {
            "core_range",
            "frame_pts_index_set_sha256",
            "frame_samples",
            "preprocess_policy_sha256",
            "proxy_blob_ref",
            "source_clock_id",
            "source_id",
            "source_range",
            "source_sha256",
            "source_time_base",
            "stream_index",
            "timeline_map",
            "window_sampling_policy_sha256",
        },
        "window manifest",
    )
    frame_index = identity_frame_index(probe)
    proxy = WindowProxyBlobRef(
        str(blob.object_id), blob.content_hash, blob.byte_length, blob.media_type
    )
    if raw["proxy_blob_ref"] != proxy.to_mapping():
        raise ValueError("window proxy BlobRef does not match durable storage")
    samples = tuple(
        WindowFrameSample(
            _int_value(item["source_pts"], "frame_sample.source_pts"),
            _int_value(item["proxy_pts"], "frame_sample.proxy_pts"),
            _text_value(item["frame_sha256"], "frame_sample.frame_sha256"),
        )
        for item in (
            _closed_mapping(entry, {"frame_sha256", "proxy_pts", "source_pts"}, "frame sample")
            for entry in _array(raw["frame_samples"], "frame_samples")
        )
    )
    result = WindowManifest(
        _text_value(raw["source_id"], "window.source_id"),
        _text_value(raw["source_clock_id"], "window.source_clock_id"),
        _text_value(raw["source_sha256"], "window.source_sha256"),
        _int_value(raw["stream_index"], "window.stream_index"),
        _decode_time_base(raw["source_time_base"], "window.source_time_base"),
        _decode_range(raw["source_range"], "window.source_range"),
        _decode_range(raw["core_range"], "window.core_range"),
        frame_index,
        proxy,
        _text_value(raw["preprocess_policy_sha256"], "window.preprocess_policy_sha256"),
        _text_value(raw["window_sampling_policy_sha256"], "window.window_sampling_policy_sha256"),
        _decode_timeline_map(raw["timeline_map"]),
        samples,
    )
    if result.to_mapping() != raw:
        raise ValueError("window manifest is not canonical")
    return result


def _decode_manifest_set(value: object, manifest: WindowManifest) -> WindowManifestSet:
    raw = _closed_mapping(
        value,
        {
            "declared_source_range",
            "frame_pts_index_set_sha256",
            "manifest_hashes",
            "source_clock_id",
            "source_id",
            "source_sha256",
            "source_time_base",
            "stream_index",
        },
        "window manifest set",
    )
    result = WindowManifestSet(
        _text_value(raw["source_id"], "window_set.source_id"),
        _text_value(raw["source_clock_id"], "window_set.source_clock_id"),
        _text_value(raw["source_sha256"], "window_set.source_sha256"),
        _int_value(raw["stream_index"], "window_set.stream_index"),
        _decode_time_base(raw["source_time_base"], "window_set.source_time_base"),
        _decode_range(raw["declared_source_range"], "window_set.declared_source_range"),
        (manifest,),
    )
    if result.to_mapping() != raw:
        raise ValueError("window manifest set is not canonical")
    return result


def _decode_timeline_map(value: object) -> ProxyTimelineMap:
    raw = _closed_mapping(
        value,
        {"certificate_kind", "proxy_time_base", "segments", "source_time_base"},
        "timeline map",
    )
    segments = tuple(
        ProxyTimelineSegment(
            _decode_range(item["proxy_range"], "timeline.proxy_range"),
            _decode_range(item["source_range"], "timeline.source_range"),
            _int_value(item["max_source_error_pts"], "timeline.max_source_error_pts"),
        )
        for item in (
            _closed_mapping(
                entry, {"max_source_error_pts", "proxy_range", "source_range"}, "timeline segment"
            )
            for entry in _array(raw["segments"], "timeline.segments")
        )
    )
    result = ProxyTimelineMap(
        _decode_time_base(raw["proxy_time_base"], "timeline.proxy_time_base"),
        _decode_time_base(raw["source_time_base"], "timeline.source_time_base"),
        segments,
        _text_value(raw["certificate_kind"], "timeline.certificate_kind"),
    )
    if result.to_mapping() != raw:
        raise ValueError("timeline map is not canonical")
    return result


def _decode_presentation_timeline_probe(value: object) -> PresentationTimelineProbe:
    raw = _closed_mapping(
        value,
        {
            "audio",
            "audio_sample_boundary_set_sha256",
            "facts_compiler_contract_sha256",
            "facts_compiler_id",
            "frame_pts_index_set_sha256",
            "probe_execution",
            "schema_version",
            "source_blob_byte_length",
            "source_blob_content_hash",
            "source_blob_media_type",
            "source_id",
            "source_proxy_timeline_map_sha256",
            "source_sha256",
            "video",
            "window_manifest_sha256",
        },
        "presentation timeline probe",
    )
    result = PresentationTimelineProbe(
        schema_version=_text_value(raw["schema_version"], "presentation.schema_version"),
        source_id=_text_value(raw["source_id"], "presentation.source_id"),
        source_sha256=_text_value(raw["source_sha256"], "presentation.source_sha256"),
        source_blob_content_hash=_text_value(
            raw["source_blob_content_hash"], "presentation.source_blob_content_hash"
        ),
        source_blob_byte_length=_int_value(
            raw["source_blob_byte_length"], "presentation.source_blob_byte_length"
        ),
        source_blob_media_type=_text_value(
            raw["source_blob_media_type"], "presentation.source_blob_media_type"
        ),
        facts_compiler_id=_text_value(raw["facts_compiler_id"], "presentation.facts_compiler_id"),
        facts_compiler_contract_sha256=_text_value(
            raw["facts_compiler_contract_sha256"],
            "presentation.facts_compiler_contract_sha256",
        ),
        probe_execution=_decode_presentation_probe_execution(raw["probe_execution"]),
        video=_decode_presentation_track(raw["video"], "presentation.video"),
        audio=_decode_presentation_track(raw["audio"], "presentation.audio"),
        frame_pts_index_set_sha256=_text_value(
            raw["frame_pts_index_set_sha256"], "presentation.frame_pts_index_set_sha256"
        ),
        audio_sample_boundary_set_sha256=_text_value(
            raw["audio_sample_boundary_set_sha256"],
            "presentation.audio_sample_boundary_set_sha256",
        ),
        source_proxy_timeline_map_sha256=_optional_hash(
            raw["source_proxy_timeline_map_sha256"],
            "presentation.source_proxy_timeline_map_sha256",
        ),
        window_manifest_sha256=_optional_hash(
            raw["window_manifest_sha256"], "presentation.window_manifest_sha256"
        ),
    )
    if result.to_mapping() != raw:
        raise ValueError("presentation timeline probe is not canonical")
    return result


def _decode_presentation_probe_execution(value: object) -> PresentationProbeExecution:
    raw = _closed_mapping(
        value,
        {
            "executable_sha256",
            "invocation_schema_sha256",
            "normalized_output_sha256",
            "probe_kind",
            "source_input_sha256",
            "version_output_sha256",
        },
        "presentation probe execution",
    )
    return PresentationProbeExecution(
        _text_value(raw["probe_kind"], "presentation.execution.probe_kind"),
        _text_value(
            raw["invocation_schema_sha256"],
            "presentation.execution.invocation_schema_sha256",
        ),
        _text_value(raw["executable_sha256"], "presentation.execution.executable_sha256"),
        _text_value(
            raw["version_output_sha256"],
            "presentation.execution.version_output_sha256",
        ),
        _text_value(
            raw["normalized_output_sha256"],
            "presentation.execution.normalized_output_sha256",
        ),
        _text_value(raw["source_input_sha256"], "presentation.execution.source_input_sha256"),
    )


def _decode_presentation_track(value: object, field_name: str) -> PresentationTrack:
    raw = _closed_mapping(
        value,
        {
            "clock_id",
            "coverage_outcome",
            "end_tick",
            "endpoint_proof",
            "index_sha256",
            "media_kind",
            "origin_tick",
            "segments",
            "stream_index",
            "time_base",
        },
        field_name,
    )
    segments = tuple(
        _decode_presentation_segment(item, f"{field_name}.segments")
        for item in _array(raw["segments"], f"{field_name}.segments")
    )
    return PresentationTrack(
        MediaKind(_text_value(raw["media_kind"], f"{field_name}.media_kind")),
        _int_value(raw["stream_index"], f"{field_name}.stream_index"),
        _text_value(raw["clock_id"], f"{field_name}.clock_id"),
        _decode_time_base(raw["time_base"], f"{field_name}.time_base"),
        _int_value(raw["origin_tick"], f"{field_name}.origin_tick"),
        _int_value(raw["end_tick"], f"{field_name}.end_tick"),
        EvidenceCompleteness(
            _text_value(raw["coverage_outcome"], f"{field_name}.coverage_outcome")
        ),
        _text_value(raw["endpoint_proof"], f"{field_name}.endpoint_proof"),
        _text_value(raw["index_sha256"], f"{field_name}.index_sha256"),
        segments,
    )


def _decode_presentation_segment(value: object, field_name: str) -> PresentationTrackSegment:
    raw = _closed_mapping(
        value,
        {
            "continuity",
            "decoded_boundary_sequence_sha256",
            "presentation_interval",
            "stream_tick_range",
        },
        field_name,
    )
    tick_range = _decode_tick_range(raw["stream_tick_range"], f"{field_name}.stream_tick_range")
    interval_raw = _closed_mapping(
        raw["presentation_interval"],
        {
            "end_denominator",
            "end_numerator",
            "start_denominator",
            "start_numerator",
        },
        f"{field_name}.presentation_interval",
    )
    return PresentationTrackSegment(
        tick_range,
        RationalPresentationInterval(
            _int_value(interval_raw["start_numerator"], f"{field_name}.start_numerator"),
            _int_value(interval_raw["start_denominator"], f"{field_name}.start_denominator"),
            _int_value(interval_raw["end_numerator"], f"{field_name}.end_numerator"),
            _int_value(interval_raw["end_denominator"], f"{field_name}.end_denominator"),
        ),
        _text_value(
            raw["decoded_boundary_sequence_sha256"],
            f"{field_name}.decoded_boundary_sequence_sha256",
        ),
        PresentationSegmentContinuity(_text_value(raw["continuity"], f"{field_name}.continuity")),
    )


def _decode_tick_range(value: object, field_name: str) -> TickRange:
    raw = _closed_mapping(value, {"end_tick", "start_tick"}, field_name)
    return TickRange(
        _int_value(raw["start_tick"], f"{field_name}.start_tick"),
        _int_value(raw["end_tick"], f"{field_name}.end_tick"),
    )


def _decode_frame_boundaries(value: object, field_name: str) -> tuple[tuple[int, int], ...]:
    boundaries = tuple(
        (
            _int_value(raw["start_tick"], f"{field_name}.start_tick"),
            _int_value(raw["end_tick"], f"{field_name}.end_tick"),
        )
        for raw in (
            _closed_mapping(item, {"end_tick", "start_tick"}, field_name)
            for item in _array(value, field_name)
        )
    )
    if not boundaries:
        raise ValueError(f"{field_name} must not be empty")
    previous_start: int | None = None
    previous_end: int | None = None
    for start, end in boundaries:
        if start >= end:
            raise ValueError(f"{field_name} must contain non-empty boundaries")
        if previous_start is not None and start <= previous_start:
            raise ValueError(f"{field_name} starts must be strictly ordered")
        if previous_end is not None and start < previous_end:
            raise ValueError(f"{field_name} boundaries must not overlap")
        previous_start, previous_end = start, end
    return boundaries


def _frame_boundaries_mapping(
    boundaries: tuple[tuple[int, int], ...],
) -> list[dict[str, int]]:
    return [
        {"end_tick": end, "start_tick": start}
        for start, end in boundaries
    ]


def _expected_presentation_segments(
    boundaries: tuple[tuple[int, int], ...],
) -> tuple[tuple[TickRange, PresentationSegmentContinuity, str], ...]:
    runs: list[tuple[TickRange, PresentationSegmentContinuity, str]] = []
    continuous: list[tuple[int, int]] = [boundaries[0]]
    for boundary in boundaries[1:]:
        previous = continuous[-1]
        if boundary[0] == previous[1]:
            continuous.append(boundary)
            continue
        run_range = TickRange(continuous[0][0], continuous[-1][1])
        runs.append(
            (
                run_range,
                PresentationSegmentContinuity.CONTINUOUS_DECODED,
                canonical_sha256(
                    {
                        "boundaries": [
                            {"end_tick": end, "start_tick": start}
                            for start, end in continuous
                        ],
                        "kind": "decoded-continuous-run-v2",
                    }
                ),
            )
        )
        gap_range = TickRange(previous[1], boundary[0])
        runs.append(
            (
                gap_range,
                PresentationSegmentContinuity.DECLARED_GAP,
                canonical_sha256(
                    {
                        "after_start_tick": boundary[0],
                        "before_end_tick": previous[1],
                        "kind": "decoded-boundary-gap-v2",
                    }
                ),
            )
        )
        continuous = [boundary]
    run_range = TickRange(continuous[0][0], continuous[-1][1])
    runs.append(
        (
            run_range,
            PresentationSegmentContinuity.CONTINUOUS_DECODED,
            canonical_sha256(
                {
                    "boundaries": [
                        {"end_tick": end, "start_tick": start}
                        for start, end in continuous
                    ],
                    "kind": "decoded-continuous-run-v2",
                }
            ),
        )
    )
    return tuple(runs)


def _optional_hash(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _text_value(value, field_name)


def _validate_presentation_timeline_probe(
    probe: DecodedMediaProbe,
    blob: DecodedBlobRef,
    manifest: WindowManifest,
) -> None:
    facts = probe.presentation_timeline_probe
    if facts is None:
        return
    if (
        facts.source_id != probe.source.source_id
        or facts.source_sha256 != probe.source.content_sha256
        or facts.source_blob_content_hash != blob.content_hash
        or facts.source_blob_byte_length != blob.byte_length
        or facts.source_blob_media_type != blob.media_type
        or facts.video.stream_index != probe.video_probe.video_stream.stream_index
        or facts.video.time_base != probe.video_probe.video_stream.time_base
        or facts.video.origin_tick != probe.video_range.start_pts
        or facts.video.end_tick != probe.video_range.end_pts
        or facts.frame_pts_index_set_sha256 != manifest.frame_pts_index_set.canonical_hash
        or facts.audio_sample_boundary_set_sha256 != probe.audio_sample_boundaries.canonical_hash
        or facts.audio.clock_id != probe.audio_sample_boundaries.context.clock_id
        or facts.audio.clock_id != f"audio-stream-{facts.audio.stream_index}"
        or facts.audio.time_base != probe.audio_sample_boundaries.context.time_base
        or facts.audio.origin_tick != probe.audio_sample_boundaries.context.origin_tick
        or facts.audio.end_tick != probe.audio_sample_boundaries.context.end_tick
        or facts.source_proxy_timeline_map_sha256 != manifest.timeline_map.canonical_hash
        or facts.window_manifest_sha256 != manifest.canonical_hash
    ):
        raise ValueError("presentation timeline facts do not close over source manifest evidence")
    video_boundaries = probe.presentation_video_frame_boundaries
    audio_boundaries = probe.presentation_audio_frame_boundaries
    if (
        tuple(start for start, _ in video_boundaries) != probe.video_probe.pts_index.ticks
        or video_boundaries[0][0] != probe.video_range.start_pts
        or video_boundaries[-1][1] != probe.video_range.end_pts
        or set(point.tick for point in probe.audio_sample_boundaries.points)
        != {tick for boundary in audio_boundaries for tick in boundary}
        or audio_boundaries[0][0] != probe.audio_sample_boundaries.context.origin_tick
        or audio_boundaries[-1][1] != probe.audio_sample_boundaries.context.end_tick
    ):
        raise ValueError("presentation frame boundaries do not close over decoded source indexes")
    for track, boundaries in (
        (facts.video, video_boundaries),
        (facts.audio, audio_boundaries),
    ):
        expected_segments = _expected_presentation_segments(boundaries)
        actual_segments = tuple(
            (
                segment.stream_tick_range,
                segment.continuity,
                segment.decoded_boundary_sequence_sha256,
            )
            for segment in track.segments
        )
        if actual_segments != expected_segments:
            raise ValueError("presentation track segments do not prove decoded frame boundaries")


def _decode_blob(value: object) -> DecodedBlobRef:
    raw = _closed_mapping(
        value, {"byte_length", "content_hash", "media_type", "object_id"}, "proxy blob"
    )
    return DecodedBlobRef(
        UUID(_text_value(raw["object_id"], "proxy_blob.object_id")),
        _text_value(raw["content_hash"], "proxy_blob.content_hash"),
        _int_value(raw["byte_length"], "proxy_blob.byte_length"),
        _text_value(raw["media_type"], "proxy_blob.media_type"),
    )


def _copy_blob_identity(value: BlobIdentity) -> DecodedBlobRef:
    return DecodedBlobRef(
        value.object_id,
        value.content_hash,
        value.byte_length,
        value.media_type,
    )


def _decode_time_base(value: object, field_name: str) -> TimeBase:
    raw = _closed_mapping(value, {"denominator", "numerator"}, field_name)
    return TimeBase(
        _int_value(raw["numerator"], f"{field_name}.numerator"),
        _int_value(raw["denominator"], f"{field_name}.denominator"),
    )


def _decode_range(value: object, field_name: str) -> TickRange:
    raw = _closed_mapping(value, {"end_pts", "start_pts"}, field_name)
    return TickRange(
        _int_value(raw["start_pts"], f"{field_name}.start_pts"),
        _int_value(raw["end_pts"], f"{field_name}.end_pts"),
    )


def _closed_mapping(
    value: object,
    keys: set[str],
    field_name: str,
    *,
    optional: set[str] | None = None,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a closed object")
    mapping = cast(dict[object, object], value)
    optional_keys = optional or set()
    if (
        not optional_keys <= keys
        or not keys - optional_keys <= set(mapping)
        or not set(mapping) <= keys
    ):
        raise ValueError(f"{field_name} must be a closed object")
    return {str(key): item for key, item in mapping.items()}


def _array(value: object, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    return list(cast(list[object], value))


def _text_value(value: object, field_name: str) -> str:
    if type(value) is not str or not value:  # noqa: E721
        raise ValueError(f"{field_name} must be text")
    return value


def _int_value(value: object, field_name: str) -> int:
    if type(value) is not int:  # noqa: E721
        raise ValueError(f"{field_name} must be an integer")
    return value


__all__ = [
    "DecodedBlobRef",
    "DecodedMediaProbe",
    "DecodedSeriesSource",
    "DecodedSourceEpisode",
    "DecodedSourceManifest",
    "IDENTITY_FRAME_GENERATION_POLICY_SHA256",
    "SOURCE_OPERATION_POLICY_SCHEMA_VERSION",
    "SourceManifestDecodeError",
    "SourceOperationGrant",
    "SourceOperationPolicy",
    "SourceOperationPurpose",
    "SourcePurposeDeniedError",
    "decode_legacy_source_manifest",
    "decode_source_manifest",
    "identity_frame_index",
]
