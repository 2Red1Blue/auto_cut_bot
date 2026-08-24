"""Exact integer-clock ffprobe evidence for whole-series source snapshots."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from autocut_kernel.media import (
    AudioBoundaryMethod,
    AudioSampleBoundary,
    AudioSampleBoundarySet,
    AudioSourceOutcome,
    Coverage,
    CoverageOutcome,
    EvidenceContext,
    MediaKind,
    TickRange,
    TimeBase,
)
from autocut_kernel.media.ffprobe_port import (
    FFprobeError,
    FFprobeOutputError,
    FFprobePort,
    ProbeResult,
)
from autocut_kernel.media.types import canonical_sha256

from .models import SeriesCensusError, SeriesSource

_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
DECODED_AUDIO_BOUNDARY_GENERATION_POLICY_SHA256 = canonical_sha256(
    {
        "coverage_policy": "strict-contiguous-decoded-frames-no-gap-v1",
        "endpoint_rule": "decoded-first-start-and-last-end-equal-stream-v1",
        "producer": "ffprobe-decoded-audio-boundaries-v2",
    }
)


class SourceMediaEvidenceError(SeriesCensusError):
    """Decoded source evidence is missing, discontinuous, or contradictory."""

    code = "INVALID_SOURCE_MEDIA_EVIDENCE"


class SourceMediaToolError(RuntimeError):
    """A required local media tool could not produce evidence."""

    code = "SOURCE_MEDIA_TOOL_FAILED"


@dataclass(frozen=True, slots=True)
class SourceMediaProbe:
    source: SeriesSource
    video_probe: ProbeResult
    video_range: TickRange
    audio_sample_boundaries: AudioSampleBoundarySet

    def to_mapping(self) -> dict[str, object]:
        stream = self.video_probe.video_stream
        return {
            "audio_sample_boundaries": self.audio_sample_boundaries.to_mapping(),
            "decoded_video_frame_pts": list(self.video_probe.pts_index.ticks),
            "ffprobe": {
                "executable": self.video_probe.tool.executable,
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
        video_start = _integer(video_record.get("start_pts"), "video.start_pts")
        video_duration = _positive(video_record.get("duration_ts"), "video.duration_ts")
        video_range = TickRange(video_start, video_start + video_duration)
        if (
            _integer(video_record.get("index"), "video.index") != video.video_stream.stream_index
            or _time_base(video_record.get("time_base"), "video.time_base")
            != video.video_stream.time_base
            or any(not video_range.start_pts <= tick < video_range.end_pts for tick in video.pts_index.ticks)
        ):
            raise SourceMediaEvidenceError(
                "decoded video PTS do not close over the declared video stream"
            )
        audio = self._audio_boundaries(path, source, audio_records[0])
        return SourceMediaProbe(source, video, video_range, audio)

    def _audio_boundaries(
        self,
        path: Path,
        source: SeriesSource,
        stream: dict[str, Any],
    ) -> AudioSampleBoundarySet:
        stream_index = _integer(stream.get("index"), "audio.index")
        time_base = _time_base(stream.get("time_base"), "audio.time_base")
        start = _integer(stream.get("start_pts"), "audio.start_pts")
        duration = _positive(stream.get("duration_ts"), "audio.duration_ts")
        end = start + duration
        frames = self._json(
            [
                self._executable,
                "-v",
                "error",
                "-of",
                "json",
                "-select_streams",
                "a:0",
                "-show_frames",
                "-show_entries",
                "frame=media_type,stream_index,best_effort_timestamp,duration",
                "--",
                str(path),
            ]
        )
        decoded_frames = _objects(frames.get("frames"), "audio.frames")
        if not decoded_frames:
            raise SourceMediaEvidenceError("decoded audio frame evidence must not be empty")
        ticks: set[int] = set()
        previous_end: int | None = None
        for position, frame in enumerate(decoded_frames):
            if frame.get("media_type") != "audio" or _integer(
                frame.get("stream_index"), f"audio.frames[{position}].stream_index"
            ) != stream_index:
                raise SourceMediaEvidenceError(
                    "audio frame listing contains a foreign frame"
                )
            frame_start = _integer(
                frame.get("best_effort_timestamp"),
                f"audio.frames[{position}].best_effort_timestamp",
            )
            frame_duration = _positive(
                frame.get("duration"), f"audio.frames[{position}].duration"
            )
            frame_end = frame_start + frame_duration
            if not start <= frame_start < frame_end <= end:
                raise SourceMediaEvidenceError(
                    "decoded audio frame lies outside the declared audio stream"
                )
            if position == 0 and frame_start != start:
                raise SourceMediaEvidenceError(
                    "first decoded audio frame does not prove the stream start boundary"
                )
            if previous_end is not None and frame_start != previous_end:
                raise SourceMediaEvidenceError(
                    "decoded audio frames are not gap-free and contiguous"
                )
            ticks.update((frame_start, frame_end))
            previous_end = frame_end
        if previous_end != end:
            raise SourceMediaEvidenceError(
                "last decoded audio frame does not prove the stream end boundary"
            )
        ordered = tuple(sorted(ticks))
        policy = DECODED_AUDIO_BOUNDARY_GENERATION_POLICY_SHA256
        clock_id = f"audio-stream-{stream_index}"
        context = EvidenceContext(
            source.source_id,
            source.content_sha256,
            MediaKind.AUDIO,
            clock_id,
            time_base,
            start,
            duration,
            "ffprobe-decoded-audio-boundaries-v2",
            policy,
        )
        coverage = Coverage(
            source.source_id,
            source.content_sha256,
            clock_id,
            time_base,
            start,
            end,
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
        )

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
            raise SourceMediaEvidenceError(
                "ffprobe media evidence is not valid JSON"
            ) from error
        if not isinstance(value, dict):
            raise SourceMediaEvidenceError(
                "ffprobe media evidence must be a JSON object"
            )
        return cast(dict[str, Any], value)


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
