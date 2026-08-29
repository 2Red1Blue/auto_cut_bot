"""Fail-closed subprocess port for collecting exact decoded video PTS evidence."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

from .types import (
    MediaDomainError,
    MediaValidationError,
    PTSIndex,
    TimeBase,
    ToolEvidence,
    VideoStreamEvidence,
)

_SUPPORTED_VIDEO_CODECS = frozenset({"av1", "h264", "hevc", "mpeg4", "vp8", "vp9"})
_DECIMAL_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_WSL_MOUNT_PATH = re.compile(r"^/mnt/(?P<drive>[A-Za-z])(?:/(?P<relative>.*))?\Z")
_MAX_STDERR_BYTES = 16 * 1024


class FFprobeError(MediaDomainError):
    """Base failure for probe execution or untrusted probe output."""

    code = "SOURCE_PROBE_FAILED"


class FFprobeUnavailableError(FFprobeError):
    code = "FFPROBE_UNAVAILABLE"


class FFprobeExecutionError(FFprobeError):
    code = "SOURCE_PROBE_FAILED"


class FFprobeOutputError(FFprobeError):
    code = "INVALID_EVIDENCE"


class FFprobePtsIndexError(FFprobeOutputError):
    code = "MEDIA_PTS_INDEX_INVALID"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Facts from the required metadata and decoded-frame probe calls."""

    video_stream: VideoStreamEvidence
    pts_index: PTSIndex
    tool: ToolEvidence


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


def _stderr_digest(stderr: bytes) -> str:
    return f"sha256:{hashlib.sha256(stderr[:_MAX_STDERR_BYTES]).hexdigest()}"


def _as_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FFprobeOutputError(f"ffprobe {label} must be a JSON object")
    return cast(dict[str, Any], value)


def _objects(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise FFprobeOutputError(f"ffprobe {label} must be an array")
    objects: list[dict[str, Any]] = []
    for position, item in enumerate(cast(list[object], value)):
        objects.append(_as_object(item, f"{label}[{position}]"))
    return objects


def _strict_decimal_integer(value: object, label: str) -> int:
    if type(value) is int:
        return value
    if type(value) is not str or _DECIMAL_INTEGER.fullmatch(value) is None:
        raise FFprobePtsIndexError(f"{label} must be a decimal integer best_effort_timestamp")
    return int(value)


def _source_argument(executable: str, path: Path) -> str:
    """Return a path understood by the selected probe executable.

    WSL can launch a Windows ``ffprobe.exe``, but that binary does not resolve
    Linux ``/mnt/<drive>/...`` paths.  Convert only for an explicitly selected
    Windows executable; native Linux/macOS ffprobe keeps the resolved POSIX
    path unchanged.  The argument is still passed as one subprocess argv item,
    so spaces and option-like filenames remain safe.
    """

    value = str(path)
    if not Path(executable).name.casefold().endswith(".exe"):
        return value
    match = _WSL_MOUNT_PATH.fullmatch(value)
    if match is None:
        return value
    relative = match.group("relative") or ""
    return f"{match.group('drive').upper()}:/{relative}"


class FFprobePort:
    """Execute a small, fixed ffprobe surface without shell interpolation."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout_seconds: float = 15.0,
        runner: Runner = subprocess.run,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        resolved = executable or shutil.which("ffprobe")
        if not resolved:
            raise FFprobeUnavailableError("ffprobe executable is unavailable")
        self._executable = resolved
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    @property
    def executable(self) -> str:
        return self._executable

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = self._runner(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise FFprobeExecutionError("ffprobe timed out") from error
        except OSError as error:
            raise FFprobeExecutionError("ffprobe could not be executed") from error
        if completed.returncode != 0:
            details = completed.stderr[:_MAX_STDERR_BYTES].decode("utf-8", "replace").strip()
            raise FFprobeExecutionError(f"ffprobe failed: {details or 'no stderr'}")
        return completed

    def _json(self, arguments: list[str]) -> tuple[dict[str, Any], bytes]:
        completed = self._run(arguments)
        try:
            parsed: object = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FFprobeOutputError("ffprobe did not emit valid UTF-8 JSON") from error
        return _as_object(parsed, "output"), completed.stderr

    def _version(self) -> tuple[str, bytes]:
        completed = self._run([self._executable, "-version"])
        line = completed.stdout.decode("utf-8", "replace").splitlines()
        if not line or not line[0].strip():
            raise FFprobeOutputError("ffprobe version output is empty")
        return line[0].strip(), completed.stderr

    @staticmethod
    def _video_stream(metadata: dict[str, Any]) -> VideoStreamEvidence:
        streams = _objects(metadata.get("streams"), "metadata.streams")
        video_streams = [item for item in streams if item.get("codec_type") == "video"]
        if len(video_streams) != 1:
            raise FFprobeOutputError("source must contain exactly one video stream")
        stream = video_streams[0]
        codec = stream.get("codec_name")
        if type(codec) is not str or codec not in _SUPPORTED_VIDEO_CODECS:
            raise FFprobeOutputError("source video codec is unsupported")
        try:
            index = _strict_decimal_integer(stream["index"], "stream.index")
            width = _strict_decimal_integer(stream["width"], "stream.width")
            height = _strict_decimal_integer(stream["height"], "stream.height")
        except (KeyError, FFprobePtsIndexError) as error:
            raise FFprobeOutputError("video stream metadata is incomplete") from error
        time_base = stream.get("time_base")
        if type(time_base) is not str or "/" not in time_base:
            raise FFprobeOutputError("video stream time_base is invalid")
        numerator, separator, denominator = time_base.partition("/")
        if not separator or _DECIMAL_INTEGER.fullmatch(numerator) is None or _DECIMAL_INTEGER.fullmatch(denominator) is None:
            raise FFprobeOutputError("video stream time_base is invalid")
        try:
            return VideoStreamEvidence(index, codec, width, height, TimeBase(int(numerator), int(denominator)))
        except MediaValidationError as error:
            raise FFprobeOutputError(str(error)) from error

    @staticmethod
    def _pts_index(frames_payload: dict[str, Any], stream_index: int) -> PTSIndex:
        frames = _objects(frames_payload.get("frames"), "frames")
        if not frames:
            raise FFprobePtsIndexError("ffprobe frames must be a non-empty array")
        ticks: list[int] = []
        for position, frame_object in enumerate(frames):
            if frame_object.get("media_type") != "video":
                raise FFprobePtsIndexError("frame listing contains a non-video frame")
            if frame_object.get("stream_index") != stream_index:
                raise FFprobePtsIndexError("frame listing contains a foreign stream")
            ticks.append(_strict_decimal_integer(frame_object.get("best_effort_timestamp"), f"frames[{position}].best_effort_timestamp"))
        try:
            return PTSIndex(tuple(ticks))
        except MediaValidationError as error:
            raise FFprobePtsIndexError(str(error)) from error

    def probe(self, source_path: Path) -> ProbeResult:
        """Collect one stream record and an exact decoded-frame PTS index."""
        path = Path(source_path).resolve()
        source_argument = _source_argument(self._executable, path)
        metadata, metadata_stderr = self._json(
            [self._executable, "-v", "error", "-of", "json", "-show_format", "-show_streams", "--", source_argument]
        )
        video_stream = self._video_stream(metadata)
        frames, frames_stderr = self._json(
            [
                self._executable, "-v", "error", "-of", "json", "-select_streams", "v:0", "-show_frames",
                "-show_entries", "frame=media_type,stream_index,best_effort_timestamp,pkt_dts,pkt_pts,key_frame,pict_type",
                "--", source_argument,
            ]
        )
        pts_index = self._pts_index(frames, video_stream.stream_index)
        version, version_stderr = self._version()
        return ProbeResult(video_stream, pts_index, ToolEvidence(self._executable, version, _stderr_digest(metadata_stderr + frames_stderr + version_stderr)))
