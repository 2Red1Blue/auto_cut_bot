"""Exact integer-clock ffprobe evidence for whole-series source snapshots."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

from autocut_kernel.media import (
    AudioBoundaryMethod,
    AudioSampleBoundary,
    AudioSampleBoundarySet,
    AudioSourceOutcome,
    Coverage,
    CoverageOutcome,
    EvidenceCompleteness,
    EvidenceContext,
    MediaKind,
    PresentationProbeExecution,
    PresentationSegmentContinuity,
    PresentationTimelineProbe,
    PresentationTrack,
    PresentationTrackSegment,
    RationalPresentationInterval,
    TickRange,
    TimeBase,
)
from autocut_kernel.media.audio_stream_facts import AudioStreamFacts, SelectedAudioStreamMetadata
from autocut_kernel.media.ffprobe_port import (
    FFprobeError,
    FFprobeOutputError,
    FFprobePort,
    ProbeResult,
)
from autocut_kernel.media.types import canonical_sha256, sha256_prefixed

from auto_cut_bot.pipeline.detector_identity import local_detector_identity_sha256

from .models import SeriesCensusError, SeriesSource

_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
IDENTITY_FRAME_GENERATION_POLICY_SHA256 = canonical_sha256(
    {
        "endpoint_rule": "complete-decoded-frame-pts-membership-v1",
        "operation": "identity",
        "producer": "identity-source-window-v2",
    }
)
PRESENTATION_TIMELINE_PROBE_CONTRACT_SHA256 = canonical_sha256(
    {
        "audio_endpoint_rule": "complete-decoded-frame-start-and-end-v2",
        "frame_endpoint_rule": "complete-decoded-frame-start-and-duration-end-v2",
        "gap_rule": "decoded-frame-boundary-gap-is-explicit-v2",
        "invocation_schema": "ffprobe-decoded-presentation-v2",
        "producer": "source-prep-presentation-timeline-v2",
        "stream_origin_rule": "first-decoded-frame-start-never-rebased-v2",
    }
)
PRESENTATION_PROBE_INVOCATION_SCHEMA_SHA256 = canonical_sha256(
    {
        "calls": [
            ["-v", "error", "-of", "json", "-show_streams"],
            [
                "-v",
                "error",
                "-of",
                "json",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=media_type,stream_index,best_effort_timestamp,duration",
            ],
            [
                "-v",
                "error",
                "-of",
                "json",
                "-select_streams",
                "a:0",
                "-show_frames",
                "-show_entries",
                "frame=media_type,stream_index,best_effort_timestamp,duration",
            ],
            ["-version"],
        ],
        "schema_version": "ffprobe-source-prep-presentation-invocation-v2",
    }
)
DECODED_AUDIO_BOUNDARY_GENERATION_POLICY_SHA256 = canonical_sha256(
    {
        "coverage_policy": "complete-decoded-frame-boundaries-with-explicit-gap-v2",
        "endpoint_rule": "decoded-first-start-and-last-end-v2",
        "producer": "ffprobe-decoded-audio-boundaries-v3",
    }
)


class SourceMediaEvidenceError(SeriesCensusError):
    """Decoded source evidence is missing, discontinuous, or contradictory."""

    code = "INVALID_SOURCE_MEDIA_EVIDENCE"


class SourceMediaToolError(RuntimeError):
    """A required local media tool could not produce evidence."""

    code = "SOURCE_MEDIA_TOOL_FAILED"


def _decoded_stream_range(
    frames: tuple[DecodedFrame, ...],
    field_name: str,
) -> TickRange:
    if not frames:
        raise SourceMediaEvidenceError(f"decoded {field_name} frame evidence must not be empty")
    return TickRange(frames[0].start_tick, frames[-1].end_tick)


def _presentation_stream_mapping(stream: dict[str, Any]) -> dict[str, object]:
    return {
        "codec_name": stream.get("codec_name"),
        "index": stream.get("index"),
        "start_pts": stream.get("start_pts"),
        "time_base": stream.get("time_base"),
    }


def _frame_boundaries_mapping(
    frames: tuple[DecodedFrame, ...],
) -> list[dict[str, int]]:
    return [
        {"end_tick": frame.end_tick, "start_tick": frame.start_tick}
        for frame in frames
    ]


def _presentation_interval(
    tick_range: TickRange, time_base: TimeBase
) -> RationalPresentationInterval:
    return RationalPresentationInterval.from_fractions(
        Fraction(
            tick_range.start_pts * time_base.numerator,
            time_base.denominator,
        ),
        Fraction(
            tick_range.end_pts * time_base.numerator,
            time_base.denominator,
        ),
    )


def _presentation_track(
    media_kind: MediaKind,
    stream_index: int,
    time_base: TimeBase,
    frames: tuple[DecodedFrame, ...],
    index_sha256: str,
) -> PresentationTrack:
    stream_range = _decoded_stream_range(frames, media_kind.value)
    segments: list[PresentationTrackSegment] = []
    continuous: list[DecodedFrame] = [frames[0]]
    for frame in frames[1:]:
        previous = continuous[-1]
        if frame.start_tick == previous.end_tick:
            continuous.append(frame)
            continue
        run_range = TickRange(continuous[0].start_tick, continuous[-1].end_tick)
        segments.append(
            PresentationTrackSegment(
                run_range,
                _presentation_interval(run_range, time_base),
                canonical_sha256(
                    {
                        "boundaries": [
                            {"end_tick": item.end_tick, "start_tick": item.start_tick}
                            for item in continuous
                        ],
                        "kind": "decoded-continuous-run-v2",
                    }
                ),
                PresentationSegmentContinuity.CONTINUOUS_DECODED,
            )
        )
        gap_range = TickRange(previous.end_tick, frame.start_tick)
        segments.append(
            PresentationTrackSegment(
                gap_range,
                _presentation_interval(gap_range, time_base),
                canonical_sha256(
                    {
                        "after_start_tick": frame.start_tick,
                        "before_end_tick": previous.end_tick,
                        "kind": "decoded-boundary-gap-v2",
                    }
                ),
                PresentationSegmentContinuity.DECLARED_GAP,
            )
        )
        continuous = [frame]
    run_range = TickRange(continuous[0].start_tick, continuous[-1].end_tick)
    segments.append(
        PresentationTrackSegment(
            run_range,
            _presentation_interval(run_range, time_base),
            canonical_sha256(
                {
                    "boundaries": [
                        {"end_tick": item.end_tick, "start_tick": item.start_tick}
                        for item in continuous
                    ],
                    "kind": "decoded-continuous-run-v2",
                }
            ),
            PresentationSegmentContinuity.CONTINUOUS_DECODED,
        )
    )
    return PresentationTrack(
        media_kind=media_kind,
        stream_index=stream_index,
        clock_id=f"{media_kind.value}-stream-{stream_index}",
        time_base=time_base,
        origin_tick=stream_range.start_pts,
        end_tick=stream_range.end_pts,
        coverage_outcome=EvidenceCompleteness.COMPLETE,
        endpoint_proof="decoded_start_and_end",
        index_sha256=index_sha256,
        segments=tuple(segments),
    )


@dataclass(frozen=True, slots=True)
class DecodedFrame:
    start_tick: int
    end_tick: int


@dataclass(frozen=True, slots=True)
class PresentationTimelineProbeDraft:
    """Decoded source facts awaiting the immutable Blob/Window binding."""

    execution: PresentationProbeExecution
    video_stream_index: int
    video_time_base: TimeBase
    video_frames: tuple[DecodedFrame, ...]
    audio_stream_index: int
    audio_time_base: TimeBase
    audio_frames: tuple[DecodedFrame, ...]

    def bind(
        self,
        *,
        source: SeriesSource,
        source_blob: object,
        frame_pts_index_set_sha256: str,
        audio_sample_boundary_set_sha256: str,
        source_proxy_timeline_map_sha256: str,
        window_manifest_sha256: str,
    ) -> PresentationTimelineProbe:
        content_hash = getattr(source_blob, "content_hash", None)
        byte_length = getattr(source_blob, "byte_length", None)
        media_type = getattr(source_blob, "media_type", None)
        if (
            not isinstance(content_hash, str)
            or content_hash != source.content_sha256
            or type(byte_length) is not int
            or byte_length < 1
            or type(media_type) is not str
            or not media_type.strip()
        ):
            raise SourceMediaEvidenceError(
                "presentation probe cannot bind an inconsistent immutable source Blob"
            )
        return PresentationTimelineProbe(
            schema_version="presentation-map-facts-v2",
            source_id=source.source_id,
            source_sha256=source.content_sha256,
            source_blob_content_hash=content_hash,
            source_blob_byte_length=byte_length,
            source_blob_media_type=media_type,
            facts_compiler_id="source-prep-presentation-timeline-v2",
            facts_compiler_contract_sha256=PRESENTATION_TIMELINE_PROBE_CONTRACT_SHA256,
            probe_execution=self.execution,
            video=_presentation_track(
                MediaKind.VIDEO,
                self.video_stream_index,
                self.video_time_base,
                self.video_frames,
                frame_pts_index_set_sha256,
            ),
            audio=_presentation_track(
                MediaKind.AUDIO,
                self.audio_stream_index,
                self.audio_time_base,
                self.audio_frames,
                audio_sample_boundary_set_sha256,
            ),
            frame_pts_index_set_sha256=frame_pts_index_set_sha256,
            audio_sample_boundary_set_sha256=audio_sample_boundary_set_sha256,
            source_proxy_timeline_map_sha256=source_proxy_timeline_map_sha256,
            window_manifest_sha256=window_manifest_sha256,
        )


@dataclass(frozen=True, slots=True)
class SourceMediaProbe:
    source: SeriesSource
    video_probe: ProbeResult
    video_range: TickRange
    audio_sample_boundaries: AudioSampleBoundarySet
    frame_detector_sha256: str
    audio_detector_sha256: str
    presentation_timeline_probe: PresentationTimelineProbe | None = None
    presentation_video_frame_boundaries: tuple[DecodedFrame, ...] = ()
    presentation_audio_frame_boundaries: tuple[DecodedFrame, ...] = ()
    _presentation_timeline_draft: PresentationTimelineProbeDraft | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    audio_stream_facts: AudioStreamFacts | None = None

    def __post_init__(self) -> None:
        sha256_prefixed(self.frame_detector_sha256, "frame_detector_sha256")
        sha256_prefixed(self.audio_detector_sha256, "audio_detector_sha256")

    def bind_presentation_timeline(
        self,
        *,
        source_blob: object,
        frame_pts_index_set_sha256: str,
        source_proxy_timeline_map_sha256: str,
        window_manifest_sha256: str,
    ) -> SourceMediaProbe:
        if self.presentation_timeline_probe is not None:
            raise SourceMediaEvidenceError("presentation timeline facts are already bound")
        if self._presentation_timeline_draft is None:
            raise SourceMediaEvidenceError("presentation timeline facts were not produced")
        facts = self._presentation_timeline_draft.bind(
            source=self.source,
            source_blob=source_blob,
            frame_pts_index_set_sha256=frame_pts_index_set_sha256,
            audio_sample_boundary_set_sha256=self.audio_sample_boundaries.canonical_hash,
            source_proxy_timeline_map_sha256=source_proxy_timeline_map_sha256,
            window_manifest_sha256=window_manifest_sha256,
        )
        if (
            self.presentation_video_frame_boundaries
            != self._presentation_timeline_draft.video_frames
            or self.presentation_audio_frame_boundaries
            != self._presentation_timeline_draft.audio_frames
        ):
            raise SourceMediaEvidenceError(
                "presentation frame boundaries do not close over the decoded probe"
            )
        return replace(self, presentation_timeline_probe=facts)

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
            if (
                not self.presentation_video_frame_boundaries
                or not self.presentation_audio_frame_boundaries
            ):
                raise SourceMediaEvidenceError(
                    "bound presentation timeline facts require decoded frame boundary evidence"
                )
            result["decoded_video_frame_boundaries"] = _frame_boundaries_mapping(
                self.presentation_video_frame_boundaries
            )
            result["decoded_audio_frame_boundaries"] = _frame_boundaries_mapping(
                self.presentation_audio_frame_boundaries
            )
        elif self._presentation_timeline_draft is not None:
            raise SourceMediaEvidenceError(
                "unbound presentation timeline facts cannot enter a source manifest"
            )
        if self.audio_stream_facts is not None:
            if type(self.audio_stream_facts) is not AudioStreamFacts:
                raise SourceMediaEvidenceError("audio_stream_facts must be exact typed facts")
            if self.presentation_timeline_probe is None:
                raise SourceMediaEvidenceError("audio stream facts require a bound presentation probe")
            self.audio_stream_facts.assert_matches(
                self.presentation_timeline_probe, self.audio_sample_boundaries
            )
            if (
                self.audio_stream_facts.source_id != self.source.source_id
                or self.audio_stream_facts.source_sha256 != self.source.content_sha256
            ):
                raise SourceMediaEvidenceError("audio stream facts do not match the source")
            result["audio_stream_facts"] = self.audio_stream_facts.to_mapping()
        return result

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


class FFprobeSourceMediaPort:
    """Augment the Kernel video probe with exact stream duration and audio boundaries."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout_seconds: float = 120.0,
        video_port: FFprobePort | None = None,
    ) -> None:
        resolved = executable or shutil.which("ffprobe")
        if not resolved:
            raise SeriesCensusError("ffprobe executable is unavailable")
        self._executable = resolved
        self._timeout_seconds = timeout_seconds
        self._video_port = video_port or FFprobePort(resolved, timeout_seconds=timeout_seconds)

    def probe(self, source_path: Path, source: SeriesSource) -> SourceMediaProbe:
        path = Path(source_path).resolve(strict=True)
        before_executable_sha256, version_evidence_sha256 = self._tool_identity()
        try:
            video = self._video_port.probe(path)
        except FFprobeOutputError as error:
            raise SourceMediaEvidenceError("decoded video evidence is invalid") from error
        except FFprobeError as error:
            raise SourceMediaToolError("ffprobe video evidence command failed") from error
        metadata = self._json(
            [
                self._executable,
                "-v",
                "error",
                "-of",
                "json",
                "-show_streams",
                "--",
                str(path),
            ]
        )
        streams = _objects(metadata.get("streams"), "streams")
        video_records = [item for item in streams if item.get("codec_type") == "video"]
        audio_records = [item for item in streams if item.get("codec_type") == "audio"]
        if len(video_records) != 1 or len(audio_records) != 1:
            raise SourceMediaEvidenceError(
                "source must contain exactly one video and one audio stream"
            )
        video_record = video_records[0]
        video_frames = self._decoded_frames(
            path,
            selector="v:0",
            expected_media_kind="video",
            stream_index=_integer(video_record.get("index"), "video.index"),
            field_name="video",
        )
        video_start = _integer(video_record.get("start_pts"), "video.start_pts")
        video_range = _decoded_stream_range(video_frames, "video")
        if (
            _integer(video_record.get("index"), "video.index") != video.video_stream.stream_index
            or _time_base(video_record.get("time_base"), "video.time_base")
            != video.video_stream.time_base
            or video_start != video_range.start_pts
            or video.video_stream.time_base
            != _time_base(video_record.get("time_base"), "video.time_base")
            or video.video_stream.stream_index != _integer(video_record.get("index"), "video.index")
            or video.pts_index.ticks != tuple(frame.start_tick for frame in video_frames)
        ):
            raise SourceMediaEvidenceError(
                "decoded video frame boundaries do not close over the declared video stream"
            )
        audio, audio_frames = self._audio_boundaries_with_frames(path, source, audio_records[0])
        selected_audio_metadata = _selected_audio_metadata(audio_records[0])
        after_executable_sha256, after_version_evidence_sha256 = self._tool_identity()
        if (
            before_executable_sha256 != after_executable_sha256
            or version_evidence_sha256 != after_version_evidence_sha256
        ):
            raise SourceMediaToolError("ffprobe identity changed during source preparation")
        tool = (("ffprobe", before_executable_sha256, version_evidence_sha256),)
        normalized_video = replace(
            video,
            tool=replace(video.tool, executable="ffprobe"),
        )
        normalized_output_sha256 = canonical_sha256(
            {
                "audio_frames": [
                    {"end_tick": frame.end_tick, "start_tick": frame.start_tick}
                    for frame in audio_frames
                ],
                "audio_stream": _presentation_stream_mapping(audio_records[0]),
                "video_frames": [
                    {"end_tick": frame.end_tick, "start_tick": frame.start_tick}
                    for frame in video_frames
                ],
                "video_stream": _presentation_stream_mapping(video_record),
            }
        )
        draft = PresentationTimelineProbeDraft(
            PresentationProbeExecution(
                "ffprobe-decoded-presentation-v2",
                PRESENTATION_PROBE_INVOCATION_SCHEMA_SHA256,
                before_executable_sha256,
                version_evidence_sha256,
                normalized_output_sha256,
                source.content_sha256,
            ),
            normalized_video.video_stream.stream_index,
            normalized_video.video_stream.time_base,
            video_frames,
            _integer(audio_records[0].get("index"), "audio.index"),
            _time_base(audio_records[0].get("time_base"), "audio.time_base"),
            audio_frames,
        )
        return SourceMediaProbe(
            source,
            normalized_video,
            video_range,
            audio,
            local_detector_identity_sha256(
                producer_kind="frame",
                producer_generation_policy_sha256=(IDENTITY_FRAME_GENERATION_POLICY_SHA256),
                tools=tool,
            ),
            local_detector_identity_sha256(
                producer_kind="audio",
                producer_generation_policy_sha256=(DECODED_AUDIO_BOUNDARY_GENERATION_POLICY_SHA256),
                tools=tool,
            ),
            _presentation_timeline_draft=draft,
            presentation_video_frame_boundaries=video_frames,
            presentation_audio_frame_boundaries=audio_frames,
            audio_stream_facts=AudioStreamFacts(
                source.source_id,
                source.content_sha256,
                selected_audio_metadata.stream_index,
                audio.context.clock_id,
                audio.context.time_base,
                audio.context.origin_tick,
                audio.context.end_tick,
                selected_audio_metadata.sample_rate,
                selected_audio_metadata.channels,
                audio.canonical_hash,
                selected_audio_metadata,
                selected_audio_metadata.canonical_hash,
                canonical_sha256(draft.execution.to_mapping()),
            ),
        )

    def _tool_identity(self) -> tuple[str, str]:
        try:
            executable = Path(self._executable).resolve(strict=True)
            content = executable.read_bytes()
            completed = subprocess.run(
                [str(executable), "-version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SourceMediaToolError("ffprobe identity command failed") from error
        if not content or completed.returncode != 0 or not (completed.stdout or completed.stderr):
            raise SourceMediaToolError("ffprobe identity command failed")
        return (
            "sha256:" + hashlib.sha256(content).hexdigest(),
            "sha256:" + hashlib.sha256(completed.stdout + completed.stderr).hexdigest(),
        )

    def _audio_boundaries(
        self,
        path: Path,
        source: SeriesSource,
        stream: dict[str, Any],
    ) -> AudioSampleBoundarySet:
        boundaries, _ = self._audio_boundaries_with_frames(path, source, stream)
        return boundaries

    def _audio_boundaries_with_frames(
        self,
        path: Path,
        source: SeriesSource,
        stream: dict[str, Any],
    ) -> tuple[AudioSampleBoundarySet, tuple[DecodedFrame, ...]]:
        stream_index = _integer(stream.get("index"), "audio.index")
        time_base = _time_base(stream.get("time_base"), "audio.time_base")
        start = _integer(stream.get("start_pts"), "audio.start_pts")
        decoded = self._decoded_frames(
            path,
            selector="a:0",
            expected_media_kind="audio",
            stream_index=stream_index,
            field_name="audio",
        )
        stream_range = _decoded_stream_range(decoded, "audio")
        if start != stream_range.start_pts:
            raise SourceMediaEvidenceError(
                "first decoded audio frame does not prove the stream start boundary"
            )
        ticks: set[int] = set()
        for frame in decoded:
            ticks.update((frame.start_tick, frame.end_tick))
        ordered = tuple(sorted(ticks))
        policy = DECODED_AUDIO_BOUNDARY_GENERATION_POLICY_SHA256
        clock_id = f"audio-stream-{stream_index}"
        context = EvidenceContext(
            source.source_id,
            source.content_sha256,
            MediaKind.AUDIO,
            clock_id,
            time_base,
            stream_range.start_pts,
            stream_range.duration_pts,
            "ffprobe-decoded-audio-boundaries-v3",
            policy,
        )
        coverage = Coverage(
            source.source_id,
            source.content_sha256,
            clock_id,
            time_base,
            stream_range.start_pts,
            stream_range.end_pts,
            CoverageOutcome.COMPLETE,
        )
        points = tuple(
            AudioSampleBoundary(
                f"audio-boundary-{position:08d}",
                source.source_id,
                source.content_sha256,
                clock_id,
                time_base,
                tick,
                AudioBoundaryMethod.DECODER,
            )
            for position, tick in enumerate(ordered)
        )
        return AudioSampleBoundarySet(
            "audio-sample-boundaries-v2",
            context,
            coverage,
            AudioSourceOutcome.BOUNDARIES_AVAILABLE,
            points,
        ), decoded

    def _decoded_frames(
        self,
        path: Path,
        *,
        selector: str,
        expected_media_kind: str,
        stream_index: int,
        field_name: str,
    ) -> tuple[DecodedFrame, ...]:
        frames = self._json(
            [
                self._executable,
                "-v",
                "error",
                "-of",
                "json",
                "-select_streams",
                selector,
                "-show_frames",
                "-show_entries",
                "frame=media_type,stream_index,best_effort_timestamp,duration",
                "--",
                str(path),
            ]
        )
        decoded_frames = _objects(frames.get("frames"), f"{field_name}.frames")
        if not decoded_frames:
            raise SourceMediaEvidenceError(f"decoded {field_name} frame evidence must not be empty")
        result: list[DecodedFrame] = []
        previous_start: int | None = None
        previous_end: int | None = None
        for position, frame in enumerate(decoded_frames):
            if (
                frame.get("media_type") != expected_media_kind
                or _integer(
                    frame.get("stream_index"), f"{field_name}.frames[{position}].stream_index"
                )
                != stream_index
            ):
                raise SourceMediaEvidenceError(
                    f"{field_name} frame listing contains a foreign frame"
                )
            frame_start = _integer(
                frame.get("best_effort_timestamp"),
                f"{field_name}.frames[{position}].best_effort_timestamp",
            )
            try:
                frame_duration = _positive(
                    frame.get("duration"), f"{field_name}.frames[{position}].duration"
                )
            except SeriesCensusError as error:
                raise SourceMediaEvidenceError(
                    f"decoded {field_name} frame does not prove its end boundary"
                ) from error
            frame_end = frame_start + frame_duration
            if previous_start is not None and frame_start <= previous_start:
                raise SourceMediaEvidenceError(
                    f"decoded {field_name} frame starts are not strictly ordered"
                )
            if previous_end is not None and frame_start < previous_end:
                raise SourceMediaEvidenceError(f"decoded {field_name} frame boundaries overlap")
            result.append(DecodedFrame(frame_start, frame_end))
            previous_start, previous_end = frame_start, frame_end
        return tuple(result)

    def _json(self, command: list[str]) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SourceMediaToolError("ffprobe media evidence command failed") from error
        if completed.returncode != 0:
            raise SourceMediaToolError("ffprobe media evidence command failed")
        try:
            value: object = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SourceMediaEvidenceError("ffprobe media evidence is not valid JSON") from error
        if not isinstance(value, dict):
            raise SourceMediaEvidenceError("ffprobe media evidence must be a JSON object")
        return cast(dict[str, Any], value)


def _selected_audio_metadata(stream: dict[str, Any]) -> SelectedAudioStreamMetadata:
    rate = stream.get("sample_rate")
    channels = stream.get("channels")
    if (
        stream.get("codec_type") != "audio"
        or type(rate) is not str  # noqa: E721
        or re.fullmatch(r"[1-9][0-9]*", rate) is None
        or type(channels) is not int  # noqa: E721
        or channels <= 0
    ):
        raise SourceMediaEvidenceError("selected audio requires native sample_rate and channels")
    try:
        sample_rate = int(rate)
    except ValueError as error:
        raise SourceMediaEvidenceError("native audio sample_rate is invalid") from error
    return SelectedAudioStreamMetadata(
        _integer(stream.get("index"), "audio.index"),
        _time_base(stream.get("time_base"), "audio.time_base"),
        _integer(stream.get("start_pts"), "audio.start_pts"),
        sample_rate,
        channels,
    )


def _objects(value: object, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise SeriesCensusError(f"{field_name} must be an array")
    result: list[dict[str, Any]] = []
    for item in cast(list[object], value):
        if not isinstance(item, dict):
            raise SeriesCensusError(f"{field_name} must contain objects")
        result.append(cast(dict[str, Any], item))
    return result


def _integer(value: object, field_name: str) -> int:
    if type(value) is int:  # noqa: E721
        return value
    if type(value) is not str or _INTEGER.fullmatch(value) is None:  # noqa: E721
        raise SeriesCensusError(f"{field_name} must be a decimal integer tick")
    return int(value)


def _positive(value: object, field_name: str) -> int:
    result = _integer(value, field_name)
    if result <= 0:
        raise SeriesCensusError(f"{field_name} must be positive")
    return result


def _time_base(value: object, field_name: str) -> TimeBase:
    if type(value) is not str or "/" not in value:  # noqa: E721
        raise SeriesCensusError(f"{field_name} must be a rational time base")
    numerator, denominator = value.split("/", 1)
    try:
        return TimeBase(_positive(numerator, field_name), _positive(denominator, field_name))
    except ValueError as error:
        raise SeriesCensusError(f"{field_name} must be a reduced rational time base") from error


__all__ = [
    "FFprobeSourceMediaPort",
    "SourceMediaEvidenceError",
    "SourceMediaProbe",
    "SourceMediaToolError",
]
