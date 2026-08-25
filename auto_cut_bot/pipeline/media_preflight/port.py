"""Production local producers for a complete root timed-media evidence bundle."""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, cast

from autocut_kernel.media import (
    AudioSourceOutcome,
    CalibrationBinding,
    Coverage,
    CoverageOutcome,
    EvidenceCompleteness,
    EvidenceContext,
    RootMediaEvidenceBundle,
    SceneBoundarySet,
    ShotBoundarySet,
    SpeechActivitySegment,
    SpeechActivitySet,
    SpeechSourceOutcome,
    SubtitleCue,
    SubtitleCueSet,
    SubtitleDetectionMode,
    SubtitleKind,
    SubtitleSourceOutcome,
    TimeBase,
    TimingErrorBound,
    TranscriptCompleteness,
    TranscriptSegment,
    TranscriptSentence,
    TranscriptSet,
    TranscriptSourceOutcome,
    TranscriptWord,
    VideoBoundaryMethod,
    VideoBoundaryPoint,
    VideoBoundaryType,
    VisualClassification,
    VisualValidityInterval,
    VisualValiditySet,
)
from autocut_kernel.media.types import canonical_sha256

from auto_cut_bot.pipeline.detector_identity import local_detector_identity_sha256

from .funasr_http import FunASRHttpTimedSpeechEvidencePort
from .models import (
    LocalMediaEvidenceError,
    LocalMediaPolicyError,
    LocalMediaPreflightPolicy,
    LocalMediaPreflightRequest,
    LocalMediaPreflightResult,
    LocalMediaSourceError,
    LocalMediaToolError,
    ProducerIdentity,
    ProducerKind,
    ToolInvocationTrace,
    ToolTrace,
)
from .process import BoundedSubprocessRunner, CommandOutput, CommandRunner
from .speech_port import (
    TimedSpeechEvidence,
    TimedSpeechEvidencePort,
    TimedSpeechEvidenceRequest,
    TimedSpeechExpectedProducer,
)

_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_SILENCE = re.compile(rb"silence_(?P<kind>start|end):\s*(?P<seconds>-?(?:\d+(?:\.\d*)?|\.\d+))")
_SHOWINFO = re.compile(
    rb"showinfo[^\r\n]*?\bn:\s*(?P<index>\d+)\s+pts:\s*-?\d+\s+pts_time:(?P<seconds>-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
)
_TERMINAL_PUNCTUATION = re.compile(r"[.!?。！？…][\]\[)'\"”’】》」』）]*\s*\Z")
_HASH_BLOCK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Stream:
    index: int
    codec_type: str
    time_base: TimeBase
    start_tick: int
    duration_tick: int


@dataclass(frozen=True, slots=True)
class _SampleAnalysis:
    sample_ticks: tuple[int, ...]
    classes: tuple[VisualClassification, ...]
    confidences: tuple[int, ...]
    change_ppm: tuple[int, ...]
    subtitle_presence: tuple[bool, ...]


def _sha_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while block := source.read(_HASH_BLOCK_SIZE):
                digest.update(block)
                size += len(block)
    except OSError as error:
        raise LocalMediaSourceError("materialized source bytes are unreadable") from error
    if size <= 0:
        raise LocalMediaSourceError("materialized source bytes are empty")
    return f"sha256:{digest.hexdigest()}", size


def _int(value: object, field_name: str) -> int:
    if type(value) is int:  # noqa: E721
        return value
    if type(value) is str and _INTEGER.fullmatch(value) is not None:  # noqa: E721
        return int(value)
    raise LocalMediaEvidenceError(f"{field_name} must be a decimal integer")


def _time_base(value: object, field_name: str) -> TimeBase:
    if type(value) is not str or "/" not in value:  # noqa: E721
        raise LocalMediaEvidenceError(f"{field_name} must be a rational time base")
    numerator, denominator = value.split("/", 1)
    try:
        return TimeBase(_int(numerator, field_name), _int(denominator, field_name))
    except ValueError as error:
        raise LocalMediaEvidenceError(f"{field_name} is invalid") from error


def _objects(value: object, field_name: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise LocalMediaEvidenceError(f"{field_name} must be an array")
    result: list[dict[str, Any]] = []
    for item in cast(list[object], value):
        if not isinstance(item, dict):
            raise LocalMediaEvidenceError(f"{field_name} must contain objects")
        result.append(cast(dict[str, Any], item))
    return tuple(result)


def _json_object(raw: bytes, field_name: str) -> dict[str, Any]:
    try:
        value: object = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalMediaEvidenceError(f"{field_name} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise LocalMediaEvidenceError(f"{field_name} must be a JSON object")
    return cast(dict[str, Any], value)


def _floor(value: Fraction) -> int:
    return value.numerator // value.denominator


def _ceil(value: Fraction) -> int:
    return -((-value.numerator) // value.denominator)


def _decimal_fraction(value: object, field_name: str) -> Fraction:
    if type(value) is int:  # noqa: E721
        return Fraction(value)
    if type(value) is Decimal:  # noqa: E721
        numerator, denominator = value.as_integer_ratio()
        return Fraction(numerator, denominator)
    if type(value) is str:  # noqa: E721
        try:
            decimal = Decimal(value)
        except Exception as error:
            raise LocalMediaEvidenceError(f"{field_name} must be decimal seconds") from error
        numerator, denominator = decimal.as_integer_ratio()
        return Fraction(numerator, denominator)
    raise LocalMediaEvidenceError(f"{field_name} must be decimal seconds")


def _relative_seconds_tick(
    value: object,
    context: EvidenceContext,
    field_name: str,
    *,
    end: bool,
) -> int:
    seconds = _decimal_fraction(value, field_name)
    if seconds < 0:
        raise LocalMediaEvidenceError(f"{field_name} must be non-negative")
    ticks = seconds * context.time_base.denominator / context.time_base.numerator
    result = context.origin_tick + (_ceil(ticks) if end else _floor(ticks))
    return min(max(result, context.origin_tick), context.end_tick)


def _microseconds_tick(value: int, time_base: TimeBase) -> int:
    return _ceil(Fraction(value, 1_000_000) * time_base.denominator / time_base.numerator)


def _coverage(context: EvidenceContext) -> Coverage:
    return Coverage(
        context.source_id,
        context.source_sha256,
        context.clock_id,
        context.time_base,
        context.origin_tick,
        context.end_tick,
        CoverageOutcome.COMPLETE,
    )


class LocalMediaPreflightPort:
    """Prepare real local evidence. Tool failure always aborts the whole bundle."""

    def __init__(
        self,
        *,
        ffprobe_executable: str | None = None,
        ffmpeg_executable: str | None = None,
        speech_port: TimedSpeechEvidencePort | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self._ffprobe = self._resolve_executable(ffprobe_executable, "ffprobe")
        self._ffmpeg = self._resolve_executable(ffmpeg_executable, "ffmpeg")
        self._speech_port = speech_port or FunASRHttpTimedSpeechEvidencePort()
        self._whisper: Path  # legacy parser methods are unreachable compatibility code
        self._runner = runner or BoundedSubprocessRunner()

    @staticmethod
    def _resolve_executable(value: str | None, name: str) -> Path:
        resolved = value or shutil.which(name)
        if not resolved:
            raise LocalMediaToolError(f"required {name} executable is unavailable")
        try:
            path = Path(resolved).resolve(strict=True)
        except OSError as error:
            raise LocalMediaToolError(f"required {name} executable is unreadable") from error
        if not path.is_file():
            raise LocalMediaToolError(f"required {name} executable is not a file")
        return path

    def prepare(
        self,
        request: LocalMediaPreflightRequest,
        *,
        kernel_max_source_bytes: int,
        service_max_request_bytes: int,
    ) -> LocalMediaPreflightResult:
        """Build all root evidence conjunctively from one immutable MP4 materialization."""

        for value, name in (
            (kernel_max_source_bytes, "kernel_max_source_bytes"),
            (service_max_request_bytes, "service_max_request_bytes"),
        ):
            if type(value) is not int or value <= 0:  # noqa: E721
                raise LocalMediaPolicyError(f"{name} must be a positive integer")
        effective_max_source_bytes = min(kernel_max_source_bytes, service_max_request_bytes)

        try:
            source_is_symlink = request.source_path.is_symlink()
            path = request.source_path.resolve(strict=True)
        except OSError as error:
            raise LocalMediaSourceError("source_path materialization is unavailable") from error
        if path != request.source_path or source_is_symlink or not path.is_file():
            raise LocalMediaSourceError("source_path must be a direct regular-file materialization")
        initial_hash, initial_size = _sha_file(path)
        if initial_size > effective_max_source_bytes:
            raise LocalMediaSourceError("source exceeds the frozen effective source-byte limit")
        if initial_hash != request.source_sha256:
            raise LocalMediaSourceError(
                "materialized source hash does not match the committed Blob"
            )
        traces: list[ToolInvocationTrace] = []
        versions = {
            self._ffprobe: self._version(self._ffprobe, request.policy),
            self._ffmpeg: self._version(self._ffmpeg, request.policy),
        }
        measured_detectors = self._detector_identity_sha256s(
            request.policy,
            versions=versions,
        )
        measured_detectors["frame"] = request.frame_detector_sha256
        measured_detectors["audio"] = request.audio_detector_sha256
        self._validate_detector_identities(request.policy, measured_detectors)
        metadata_output, metadata_trace = self._invoke(
            "probe",
            self._ffprobe,
            [
                str(self._ffprobe),
                "-v",
                "error",
                "-of",
                "json",
                "-show_format",
                "-show_streams",
                "--",
                str(path),
            ],
            request,
            versions,
            timeout=request.policy.probe_timeout_seconds,
        )
        traces.append(metadata_trace)
        streams = self._validate_probe(
            _json_object(metadata_output.stdout, "ffprobe metadata"), request
        )
        self._validate_physical_calibrations(request)

        raw_output, raw_trace = self._invoke(
            "visual",
            self._ffmpeg,
            self._analysis_argv(path, request.policy),
            request,
            versions,
            timeout=request.policy.analysis_timeout_seconds,
            stdout_limit=self._analysis_stdout_limit(request.policy),
        )
        traces.append(raw_trace)
        samples = self._analyze_samples(
            raw_output.stdout,
            raw_output.stderr,
            request.policy,
            request.frame_pts_index.context,
        )
        shot_boundaries, scene_boundaries = self._boundary_sets(samples, request)
        visual_validity = self._visual_set(samples, request)

        embedded_cues: tuple[SubtitleCue, ...] = ()
        subtitle_streams = tuple(item for item in streams if item.codec_type == "subtitle")
        if subtitle_streams:
            packet_output, packet_trace = self._invoke(
                "subtitle",
                self._ffprobe,
                [
                    str(self._ffprobe),
                    "-v",
                    "error",
                    "-of",
                    "json",
                    "-select_streams",
                    "s",
                    "-show_packets",
                    "-show_entries",
                    "packet=stream_index,pts,duration",
                    "--",
                    str(path),
                ],
                request,
                versions,
                timeout=request.policy.probe_timeout_seconds,
            )
            traces.append(packet_trace)
            embedded_cues = self._embedded_cues(
                _json_object(packet_output.stdout, "subtitle packets"),
                subtitle_streams,
                request,
            )
        subtitle_cues = self._subtitle_set(samples, embedded_cues, bool(subtitle_streams), request)

        if request.audio_sample_boundaries.source_outcome is AudioSourceOutcome.NOT_APPLICABLE:
            timed: TimedSpeechEvidence | None = None
            transcript = self._not_applicable_transcript(request)
            speech_activity = self._not_applicable_speech(request)
        else:
            timed = self._speech_port.produce(
                self._speech_request(
                    path,
                    request,
                    kernel_max_source_bytes=kernel_max_source_bytes,
                    service_max_request_bytes=service_max_request_bytes,
                )
            )
            self._validate_timed_speech_evidence(timed, request)
            traces.extend(self._timed_speech_traces(timed))
            transcript, speech_activity = timed.transcript, timed.speech_activity

        final_hash, _ = _sha_file(path)
        if final_hash != initial_hash:
            raise LocalMediaSourceError("materialized source changed during evidence production")
        bundle = RootMediaEvidenceBundle(
            root_media_evidence_bundle_id=f"{request.episode_id}:root-media-evidence",
            source_id=request.source_id,
            source_sha256=request.source_sha256,
            source_manifest_sha256=request.source_manifest_sha256,
            root_input_manifest_sha256=request.root_input_manifest_sha256,
            frame_pts_index=request.frame_pts_index,
            shot_boundaries=shot_boundaries,
            scene_boundaries=scene_boundaries,
            audio_sample_boundaries=request.audio_sample_boundaries,
            transcript=transcript,
            speech_activity=speech_activity,
            visual_validity=visual_validity,
            subtitle_cues=subtitle_cues,
        )
        identities = tuple(
            self._producer_identity(kind, request, timed)
            for kind in (
                "frame",
                "audio",
                "asr",
                "vad",
                "shot",
                "scene",
                "visual",
                "subtitle",
            )
        )
        bindings = tuple(self._calibration_binding(item, request) for item in identities)
        return LocalMediaPreflightResult(
            bundle,
            ToolTrace(tuple(traces)),
            identities,
            bindings,
            request.source_provenance_sha256,
        )

    def measure_detector_identity_sha256s(
        self,
        policy: LocalMediaPreflightPolicy,
    ) -> dict[ProducerKind, str]:
        """Measure the exact local tool/model identities used by a deployment policy."""

        versions = {
            self._ffprobe: self._version(self._ffprobe, policy),
            self._ffmpeg: self._version(self._ffmpeg, policy),
        }
        return self._detector_identity_sha256s(policy, versions=versions)

    def _detector_identity_sha256s(
        self,
        policy: LocalMediaPreflightPolicy,
        *,
        versions: dict[Path, str],
    ) -> dict[ProducerKind, str]:
        tools = {
            "ffmpeg": {
                "executable_sha256": _sha_file(self._ffmpeg)[0],
                "version_evidence_sha256": versions[self._ffmpeg],
            },
            "ffprobe": {
                "executable_sha256": _sha_file(self._ffprobe)[0],
                "version_evidence_sha256": versions[self._ffprobe],
            },
        }
        tool_kinds: dict[ProducerKind, tuple[str, ...]] = {
            "frame": ("ffprobe",),
            "audio": ("ffprobe",),
            "shot": ("ffmpeg",),
            "scene": ("ffmpeg",),
            "visual": ("ffmpeg",),
            "subtitle": ("ffmpeg", "ffprobe"),
        }
        result: dict[ProducerKind, str] = {}
        for kind, names in tool_kinds.items():
            result[kind] = local_detector_identity_sha256(
                producer_kind=kind,
                producer_generation_policy_sha256=policy.producer_policy_sha256(kind),
                tools=tuple(
                    (
                        name,
                        tools[name]["executable_sha256"],
                        tools[name]["version_evidence_sha256"],
                    )
                    for name in names
                ),
                model_sha256=None,
            )
        return result

    @staticmethod
    def _validate_detector_identities(
        policy: LocalMediaPreflightPolicy,
        measured: dict[ProducerKind, str],
    ) -> None:
        for kind, detector_sha256 in measured.items():
            if detector_sha256 != policy.calibration(kind).detector_sha256:
                raise LocalMediaEvidenceError(
                    f"{kind} detector bytes/version/model do not match calibration"
                )

    @staticmethod
    def _validate_physical_calibrations(request: LocalMediaPreflightRequest) -> None:
        physical: tuple[tuple[ProducerKind, EvidenceContext], ...] = (
            ("frame", request.frame_pts_index.context),
            ("audio", request.audio_sample_boundaries.context),
        )
        for kind, context in physical:
            calibration = request.policy.calibration(kind)
            if (
                calibration.producer_id != context.producer_id
                or calibration.generation_policy_sha256 != context.generation_policy_sha256
            ):
                raise LocalMediaEvidenceError(
                    f"{kind} calibration does not bind the committed physical producer policy"
                )

    def _version(self, executable: Path, policy: LocalMediaPreflightPolicy) -> str:
        flag = "-version"
        argv = [str(executable)]
        if executable == self._ffmpeg:
            argv.append("-nostdin")
        argv.append(flag)
        output = self._runner.run(
            argv,
            timeout_seconds=policy.probe_timeout_seconds,
            max_stdout_bytes=min(policy.max_stdout_bytes, 1024 * 1024),
            max_stderr_bytes=policy.max_stderr_bytes,
        )
        if output.returncode != 0:
            raise LocalMediaToolError("required tool identity command failed")
        evidence = output.stdout + output.stderr
        if not evidence:
            raise LocalMediaToolError("required tool identity output is empty")
        return _sha_bytes(evidence)

    def _invoke(
        self,
        producer_kind: str,
        executable: Path,
        argv: list[str],
        request: LocalMediaPreflightRequest,
        versions: dict[Path, str],
        *,
        timeout: int,
        stdout_limit: int | None = None,
        private_paths: Sequence[Path] = (),
    ) -> tuple[CommandOutput, ToolInvocationTrace]:
        output = self._runner.run(
            argv,
            timeout_seconds=timeout,
            max_stdout_bytes=stdout_limit or request.policy.max_stdout_bytes,
            max_stderr_bytes=request.policy.max_stderr_bytes,
        )
        if output.returncode != 0:
            detail_hash = _sha_bytes(output.stderr)
            raise LocalMediaToolError(f"{producer_kind} producer exited non-zero ({detail_hash})")
        normalized: list[str] = []
        for item in argv:
            if item == str(request.source_path):
                normalized.append("$SOURCE_MATERIALIZATION")
            elif any(item == str(path) for path in private_paths):
                normalized.append("$PRIVATE_OUTPUT")
            else:
                normalized.append(item)
        executable_sha, _ = _sha_file(executable)
        executable_name = (
            "ffprobe"
            if executable == self._ffprobe
            else "ffmpeg"
            if executable == self._ffmpeg
            else None
        )
        if executable_name is None:  # pragma: no cover - closed constructor tools
            raise LocalMediaToolError("producer used an unregistered local executable")
        trace = ToolInvocationTrace(
            producer_kind,
            executable_name,
            executable_sha,
            versions[executable],
            canonical_sha256(normalized),
            _sha_bytes(output.stdout),
            _sha_bytes(output.stderr),
        )
        return output, trace

    @staticmethod
    def _analysis_stdout_limit(policy: LocalMediaPreflightPolicy) -> int:
        required = policy.analysis_width * policy.analysis_height * policy.max_analysis_frames
        return min(required, policy.max_stdout_bytes)

    def _analysis_argv(self, path: Path, policy: LocalMediaPreflightPolicy) -> list[str]:
        return [
            str(self._ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "info",
            "-copyts",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            (
                f"fps={policy.analysis_fps_numerator}/{policy.analysis_fps_denominator},"
                f"scale={policy.analysis_width}:{policy.analysis_height}:flags=area,"
                "format=gray,showinfo"
            ),
            "-frames:v",
            str(policy.max_analysis_frames),
            "-f",
            "rawvideo",
            "pipe:1",
        ]

    def _vad_argv(self, path: Path, policy: LocalMediaPreflightPolicy) -> list[str]:
        duration = Decimal(policy.vad_min_silence_milliseconds) / Decimal(1000)
        return [
            str(self._ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            f"silencedetect=noise={policy.vad_noise_db}dB:d={duration}",
            "-f",
            "null",
            "-",
        ]

    @staticmethod
    def _validate_probe(
        payload: dict[str, Any], request: LocalMediaPreflightRequest
    ) -> tuple[_Stream, ...]:
        format_object = payload.get("format")
        if not isinstance(format_object, dict):
            raise LocalMediaEvidenceError("ffprobe format object is required")
        format_name = cast(dict[str, object], format_object).get("format_name")
        if type(format_name) is not str or "mp4" not in format_name.split(","):  # noqa: E721
            raise LocalMediaSourceError("materialized source is not an ffprobe-confirmed MP4")
        streams: list[_Stream] = []
        for position, item in enumerate(_objects(payload.get("streams"), "streams")):
            kind = item.get("codec_type")
            if type(kind) is not str:  # noqa: E721
                raise LocalMediaEvidenceError(f"streams[{position}].codec_type is required")
            if kind not in {"video", "audio", "subtitle"}:
                continue
            duration_value = item.get("duration_ts")
            start_value = item.get("start_pts")
            if kind == "subtitle" and duration_value is None:
                duration_value = 1
            if kind == "subtitle" and start_value is None:
                start_value = 0
            duration = _int(duration_value, f"streams[{position}].duration_ts")
            if duration <= 0:
                raise LocalMediaEvidenceError("stream duration must be positive")
            streams.append(
                _Stream(
                    _int(item.get("index"), f"streams[{position}].index"),
                    kind,
                    _time_base(item.get("time_base"), f"streams[{position}].time_base"),
                    _int(start_value, f"streams[{position}].start_pts"),
                    duration,
                )
            )
        video = tuple(item for item in streams if item.codec_type == "video")
        audio = tuple(item for item in streams if item.codec_type == "audio")
        if len(video) != 1:
            raise LocalMediaEvidenceError("source must contain exactly one video stream")
        video_context = request.frame_pts_index.context
        if (
            video[0].time_base != video_context.time_base
            or video[0].start_tick != video_context.origin_tick
            or video[0].duration_tick != video_context.duration_tick
        ):
            raise LocalMediaSourceError("ffprobe video clock disagrees with committed frame PTS")
        audio_na = (
            request.audio_sample_boundaries.source_outcome is AudioSourceOutcome.NOT_APPLICABLE
        )
        if audio_na != (len(audio) == 0) or len(audio) > 1:
            raise LocalMediaSourceError("ffprobe audio layout disagrees with committed boundaries")
        if audio:
            audio_context = request.audio_sample_boundaries.context
            if (
                audio[0].time_base != audio_context.time_base
                or audio[0].start_tick != audio_context.origin_tick
                or audio[0].duration_tick != audio_context.duration_tick
            ):
                raise LocalMediaSourceError(
                    "ffprobe audio clock disagrees with committed sample boundaries"
                )
        return tuple(streams)

    @staticmethod
    def _analyze_samples(
        raw: bytes,
        stderr: bytes,
        policy: LocalMediaPreflightPolicy,
        context: EvidenceContext,
    ) -> _SampleAnalysis:
        frame_size = policy.analysis_width * policy.analysis_height
        if not raw or len(raw) % frame_size:
            raise LocalMediaEvidenceError("ffmpeg visual samples are empty or truncated")
        count = len(raw) // frame_size
        if count >= policy.max_analysis_frames:
            raise LocalMediaEvidenceError(
                "visual sampling reached its policy limit; full coverage is unproved"
            )
        timestamp_records = tuple(
            (int(match.group("index")), match.group("seconds").decode("ascii"))
            for match in _SHOWINFO.finditer(stderr)
        )
        if tuple(item[0] for item in timestamp_records) != tuple(range(count)):
            raise LocalMediaEvidenceError(
                "ffmpeg showinfo sample timestamps are missing or non-canonical"
            )
        sample_ticks: list[int] = []
        for _, seconds_value in timestamp_records:
            seconds = _decimal_fraction(seconds_value, "showinfo.pts_time")
            absolute = seconds * context.time_base.denominator / context.time_base.numerator
            tick = _floor(absolute)
            if not context.origin_tick <= tick < context.end_tick:
                raise LocalMediaEvidenceError("visual sample timestamp is outside video coverage")
            sample_ticks.append(tick)
        if len(sample_ticks) != len(set(sample_ticks)) or sample_ticks != sorted(sample_ticks):
            raise LocalMediaEvidenceError("visual sample timestamps must strictly increase")
        classes: list[VisualClassification] = []
        confidences: list[int] = []
        changes: list[int] = []
        subtitle_presence: list[bool] = []
        prior: bytes | None = None
        bottom_start = policy.analysis_height * 3 // 5
        for index in range(count):
            frame = raw[index * frame_size : (index + 1) * frame_size]
            mean = sum(frame) // frame_size
            change = 0
            if prior is not None:
                change = sum(abs(left - right) for left, right in zip(frame, prior, strict=True))
                change = change * 1_000_000 // (255 * frame_size)
            changes.append(change)
            if mean <= policy.black_luma_max:
                classification = VisualClassification.BLACK
                confidence = (
                    (policy.black_luma_max - mean + 1) * 1_000_000 // (policy.black_luma_max + 1)
                )
            elif mean >= policy.white_luma_min:
                classification = VisualClassification.WHITE
                confidence = (
                    (mean - policy.white_luma_min + 1) * 1_000_000 // (256 - policy.white_luma_min)
                )
            elif prior is not None and change <= policy.frozen_change_ppm_max:
                classification = VisualClassification.FROZEN
                confidence = (
                    (policy.frozen_change_ppm_max - change + 1)
                    * 1_000_000
                    // (policy.frozen_change_ppm_max + 1)
                )
            elif prior is not None and change >= policy.transition_change_ppm_min:
                classification = VisualClassification.TRANSITION
                confidence = min(change, 1_000_000)
            else:
                classification = VisualClassification.VALID_CONTENT
                confidence = min(
                    mean - policy.black_luma_max,
                    policy.white_luma_min - mean,
                    max(change - policy.frozen_change_ppm_max, 1),
                    max(policy.transition_change_ppm_min - change, 1),
                )
                confidence = max(1, min(1_000_000, confidence * 10_000))
            classes.append(classification)
            confidences.append(confidence)
            edges = 0
            comparisons = 0
            for row in range(bottom_start, policy.analysis_height):
                offset = row * policy.analysis_width
                for column in range(1, policy.analysis_width):
                    comparisons += 1
                    if (
                        abs(frame[offset + column] - frame[offset + column - 1])
                        >= policy.subtitle_edge_delta_min
                    ):
                        edges += 1
            fraction_ppm = edges * 1_000_000 // comparisons
            subtitle_presence.append(fraction_ppm >= policy.subtitle_edge_fraction_ppm_min)
            prior = frame
        return _SampleAnalysis(
            tuple(sample_ticks),
            tuple(classes),
            tuple(confidences),
            tuple(changes),
            tuple(subtitle_presence),
        )

    @staticmethod
    def _sample_influence_ranges(
        sample_ticks: tuple[int, ...],
        error_tick: int,
        context: EvidenceContext,
    ) -> tuple[tuple[int, int], ...]:
        ranges: list[tuple[int, int]] = []
        for position, tick in enumerate(sample_ticks):
            left_midpoint = (
                context.origin_tick
                if position == 0
                else (sample_ticks[position - 1] + tick + 1) // 2
            )
            right_midpoint = (
                context.end_tick
                if position == len(sample_ticks) - 1
                else (tick + sample_ticks[position + 1] + 1) // 2
            )
            start = max(left_midpoint, tick - error_tick, context.origin_tick)
            end = min(right_midpoint, tick + error_tick, context.end_tick)
            if start >= end:
                raise LocalMediaEvidenceError(
                    "calibrated sample error bound cannot cover a non-empty interval"
                )
            ranges.append((start, end))
        return tuple(ranges)

    def _boundary_sets(
        self,
        samples: _SampleAnalysis,
        request: LocalMediaPreflightRequest,
    ) -> tuple[ShotBoundarySet, SceneBoundarySet]:
        points_by_kind: dict[ProducerKind, list[VideoBoundaryPoint]] = {
            "shot": [],
            "scene": [],
        }
        frame_ticks = request.frame_pts_index.pts_index.ticks
        context = request.frame_pts_index.context
        boundary_specs: tuple[tuple[ProducerKind, int, VideoBoundaryType], ...] = (
            ("shot", request.policy.shot_change_ppm_min, VideoBoundaryType.SHOT),
            ("scene", request.policy.scene_change_ppm_min, VideoBoundaryType.SCENE),
        )
        for kind, threshold, boundary_type in boundary_specs:
            calibration = request.policy.calibration(kind)
            seen: set[int] = set()
            for position, change in enumerate(samples.change_ppm[1:], start=1):
                if change < threshold:
                    continue
                ideal = samples.sample_ticks[position]
                tick = min(frame_ticks, key=lambda candidate: (abs(candidate - ideal), candidate))
                if tick in seen or tick in {context.origin_tick, context.end_tick}:
                    continue
                seen.add(tick)
                points_by_kind[kind].append(
                    VideoBoundaryPoint(
                        f"{request.episode_id}:{kind}:{len(seen):08d}",
                        request.source_id,
                        request.source_sha256,
                        context.clock_id,
                        context.time_base,
                        tick,
                        boundary_type,
                        VideoBoundaryMethod.DETECTOR,
                        min(change, 1_000_000),
                    )
                )
            policy_hash = request.policy.producer_policy_sha256(kind)
            if calibration.producer_id == "":  # pragma: no cover - model validation
                raise LocalMediaEvidenceError("producer identity is empty")
            points_by_kind[kind].sort(
                key=lambda item: (item.source_id, item.tick, item.boundary_id)
            )
            if policy_hash == "":  # pragma: no cover - canonical hash is non-empty
                raise LocalMediaEvidenceError("producer policy hash is empty")
        shot_context = replace(
            context,
            producer_id=request.policy.calibration("shot").producer_id,
            generation_policy_sha256=request.policy.producer_policy_sha256("shot"),
        )
        scene_context = replace(
            context,
            producer_id=request.policy.calibration("scene").producer_id,
            generation_policy_sha256=request.policy.producer_policy_sha256("scene"),
        )
        return (
            ShotBoundarySet(
                f"{request.episode_id}:shot-boundaries",
                shot_context,
                _coverage(shot_context),
                request.frame_pts_index.canonical_hash,
                tuple(points_by_kind["shot"]),
            ),
            SceneBoundarySet(
                f"{request.episode_id}:scene-boundaries",
                scene_context,
                _coverage(scene_context),
                request.frame_pts_index.canonical_hash,
                tuple(points_by_kind["scene"]),
            ),
        )

    @staticmethod
    def _visual_set(
        samples: _SampleAnalysis,
        request: LocalMediaPreflightRequest,
    ) -> VisualValiditySet:
        context = replace(
            request.frame_pts_index.context,
            producer_id=request.policy.calibration("visual").producer_id,
            generation_policy_sha256=request.policy.producer_policy_sha256("visual"),
        )
        error_tick = _microseconds_tick(
            request.policy.calibration("visual").timing_error_bound_microseconds,
            context.time_base,
        )
        ranges = LocalMediaPreflightPort._sample_influence_ranges(
            samples.sample_ticks, error_tick, context
        )
        merged: list[VisualValidityInterval] = []
        cursor = context.origin_tick

        def append_interval(
            start: int,
            end: int,
            classification: VisualClassification,
            confidence: int,
            position: int,
        ) -> None:
            if start >= end:
                return
            if (
                merged
                and merged[-1].classification is classification
                and merged[-1].confidence_ppm == confidence
                and merged[-1].out_tick == start
            ):
                merged[-1] = replace(merged[-1], out_tick=end)
                return
            merged.append(
                VisualValidityInterval(
                    f"{request.episode_id}:visual:{position:08d}",
                    request.source_id,
                    request.source_sha256,
                    context.clock_id,
                    context.time_base,
                    start,
                    end,
                    classification,
                    confidence,
                )
            )

        for position, ((start, end), classification, confidence) in enumerate(
            zip(ranges, samples.classes, samples.confidences, strict=True)
        ):
            if cursor < start:
                append_interval(
                    cursor,
                    start,
                    VisualClassification.UNKNOWN,
                    1_000_000,
                    position * 2,
                )
            append_interval(start, end, classification, confidence, position * 2 + 1)
            cursor = end
        if cursor < context.end_tick:
            append_interval(
                cursor,
                context.end_tick,
                VisualClassification.UNKNOWN,
                1_000_000,
                len(ranges) * 2,
            )
        return VisualValiditySet(
            f"{request.episode_id}:visual-validity",
            context,
            _coverage(context),
            tuple(merged),
        )

    @staticmethod
    def _embedded_cues(
        payload: dict[str, Any],
        streams: tuple[_Stream, ...],
        request: LocalMediaPreflightRequest,
    ) -> tuple[SubtitleCue, ...]:
        by_index = {item.index: item for item in streams}
        video_context = request.frame_pts_index.context
        error_tick = _microseconds_tick(
            request.policy.calibration("subtitle").timing_error_bound_microseconds,
            video_context.time_base,
        )
        cues: list[SubtitleCue] = []
        for position, packet in enumerate(_objects(payload.get("packets"), "subtitle.packets")):
            stream = by_index.get(_int(packet.get("stream_index"), "packet.stream_index"))
            if stream is None:
                raise LocalMediaEvidenceError("subtitle packet references an unknown stream")
            pts = _int(packet.get("pts"), "packet.pts")
            duration = _int(packet.get("duration"), "packet.duration")
            if duration <= 0:
                raise LocalMediaEvidenceError("subtitle packet duration must be positive")
            start_seconds = Fraction(pts * stream.time_base.numerator, stream.time_base.denominator)
            end_seconds = Fraction(
                (pts + duration) * stream.time_base.numerator,
                stream.time_base.denominator,
            )
            video_scale = Fraction(
                video_context.time_base.denominator,
                video_context.time_base.numerator,
            )
            start = max(video_context.origin_tick, _floor(start_seconds * video_scale))
            end = min(video_context.end_tick, _ceil(end_seconds * video_scale))
            if start >= end:
                raise LocalMediaEvidenceError("embedded subtitle cue lies outside video coverage")
            cues.append(
                SubtitleCue(
                    f"{request.episode_id}:embedded-subtitle:{position:08d}",
                    request.source_id,
                    request.source_sha256,
                    video_context.clock_id,
                    video_context.time_base,
                    start,
                    end,
                    SubtitleKind.SUBTITLE,
                    SubtitleDetectionMode.EMBEDDED,
                    1_000_000,
                    TimingErrorBound(video_context.time_base, error_tick, error_tick),
                )
            )
        return tuple(cues)

    @staticmethod
    def _subtitle_set(
        samples: _SampleAnalysis,
        embedded: tuple[SubtitleCue, ...],
        has_embedded_stream: bool,
        request: LocalMediaPreflightRequest,
    ) -> SubtitleCueSet:
        context = replace(
            request.frame_pts_index.context,
            producer_id=request.policy.calibration("subtitle").producer_id,
            generation_policy_sha256=request.policy.producer_policy_sha256("subtitle"),
        )
        error_tick = _microseconds_tick(
            request.policy.calibration("subtitle").timing_error_bound_microseconds,
            context.time_base,
        )
        ranges = LocalMediaPreflightPort._sample_influence_ranges(
            samples.sample_ticks, error_tick, context
        )
        cursor = context.origin_tick
        for start, end in ranges:
            if start != cursor:
                raise LocalMediaEvidenceError(
                    "subtitle presence sampling does not prove complete source coverage"
                )
            cursor = end
        if cursor != context.end_tick:
            raise LocalMediaEvidenceError(
                "subtitle presence sampling does not prove complete source coverage"
            )
        minimum = request.policy.subtitle_min_consecutive_samples
        burned: list[SubtitleCue] = []
        start_position: int | None = None
        flags = (*samples.subtitle_presence, False)
        for position, present in enumerate(flags):
            if present and start_position is None:
                start_position = position
            elif not present and start_position is not None:
                if position - start_position >= minimum:
                    burned.append(
                        SubtitleCue(
                            f"{request.episode_id}:burned-subtitle:{len(burned):08d}",
                            request.source_id,
                            request.source_sha256,
                            context.clock_id,
                            context.time_base,
                            ranges[start_position][0],
                            ranges[position - 1][1],
                            SubtitleKind.SUBTITLE,
                            SubtitleDetectionMode.BURNED_IN,
                            1_000_000,
                            TimingErrorBound(context.time_base, error_tick, error_tick),
                        )
                    )
                start_position = None
        required = (
            (SubtitleDetectionMode.EMBEDDED, SubtitleDetectionMode.BURNED_IN)
            if has_embedded_stream
            else (SubtitleDetectionMode.BURNED_IN,)
        )
        cues = tuple(
            sorted(
                (*embedded, *burned),
                key=lambda item: (item.in_tick, item.out_tick, item.subtitle_cue_id),
            )
        )
        outcome = (
            SubtitleSourceOutcome.CUES_DETECTED if cues else SubtitleSourceOutcome.NONE_DETECTED
        )
        return SubtitleCueSet(
            f"{request.episode_id}:subtitle-cues",
            context,
            _coverage(context),
            required,
            required,
            outcome,
            cues,
        )

    @staticmethod
    def _speech(stderr: bytes, request: LocalMediaPreflightRequest) -> SpeechActivitySet:
        context = replace(
            request.audio_sample_boundaries.context,
            producer_id=request.policy.calibration("vad").producer_id,
            generation_policy_sha256=request.policy.producer_policy_sha256("vad"),
        )
        silence: list[tuple[int, int]] = []
        open_start: int | None = None
        for match in _SILENCE.finditer(stderr):
            seconds = match.group("seconds").decode("ascii")
            if match.group("kind") == b"start":
                if open_start is not None:
                    raise LocalMediaEvidenceError("VAD emitted nested silence intervals")
                open_start = _relative_seconds_tick(seconds, context, "silence_start", end=False)
            else:
                if open_start is None:
                    raise LocalMediaEvidenceError("VAD emitted silence_end without silence_start")
                end = _relative_seconds_tick(seconds, context, "silence_end", end=True)
                if open_start < end:
                    silence.append((open_start, end))
                open_start = None
        if open_start is not None:
            silence.append((open_start, context.end_tick))
        silence.sort()
        if any(left[1] > right[0] for left, right in zip(silence, silence[1:], strict=False)):
            raise LocalMediaEvidenceError("VAD silence intervals overlap")
        speech: list[tuple[int, int]] = []
        cursor = context.origin_tick
        for start, end in silence:
            if cursor < start:
                speech.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < context.end_tick:
            speech.append((cursor, context.end_tick))
        segments = tuple(
            SpeechActivitySegment(
                f"{request.episode_id}:speech:{position:08d}",
                request.source_id,
                request.source_sha256,
                context.clock_id,
                context.time_base,
                start,
                end,
                1_000_000,
            )
            for position, (start, end) in enumerate(speech)
        )
        return SpeechActivitySet(
            f"{request.episode_id}:speech-activity",
            context,
            _coverage(context),
            SpeechSourceOutcome.SPEECH_DETECTED if segments else SpeechSourceOutcome.NONE_DETECTED,
            segments,
        )

    def _transcript(
        self,
        path: Path,
        request: LocalMediaPreflightRequest,
        versions: dict[Path, str],
    ) -> tuple[TranscriptSet, ToolInvocationTrace]:
        policy = request.policy
        model_path = policy.whisper_model_path.resolve(strict=True)
        expected_name = f"{policy.whisper_model_name}.pt"
        if model_path.name != expected_name:
            raise LocalMediaSourceError(
                "whisper_model_path must name the exact '<model>.pt' file loaded by the CLI"
            )
        with tempfile.TemporaryDirectory(prefix="autocut-asr-") as directory:
            output_dir = Path(directory)
            argv = [
                str(self._whisper),
                str(path),
                "--model",
                policy.whisper_model_name,
                "--model_dir",
                str(model_path.parent),
                "--language",
                policy.whisper_language,
                "--output_dir",
                str(output_dir),
                "--output_format",
                "json",
                "--word_timestamps",
                "True",
                "--verbose",
                "False",
            ]
            output, trace = self._invoke(
                "asr",
                self._whisper,
                argv,
                request,
                versions,
                timeout=policy.whisper_timeout_seconds,
                private_paths=(output_dir, model_path.parent),
            )
            result_path = output_dir / f"{path.stem}.json"
            try:
                if result_path.stat().st_size > policy.max_stdout_bytes:
                    raise LocalMediaToolError("Whisper JSON exceeded its policy byte bound")
                raw = result_path.read_bytes()
            except OSError as error:
                raise LocalMediaToolError(
                    "Whisper did not materialize its required JSON"
                ) from error
            trace = replace(
                trace,
                stdout_sha256=_sha_bytes(output.stdout + raw),
            )
        return self._parse_transcript(_json_object(raw, "Whisper output"), request), trace

    @staticmethod
    def _parse_transcript(
        payload: dict[str, Any], request: LocalMediaPreflightRequest
    ) -> TranscriptSet:
        context = replace(
            request.audio_sample_boundaries.context,
            producer_id=request.policy.calibration("asr").producer_id,
            generation_policy_sha256=request.policy.producer_policy_sha256("asr"),
        )
        raw_segments = _objects(payload.get("segments"), "whisper.segments")
        if not raw_segments:
            return TranscriptSet(
                f"{request.episode_id}:transcript",
                context,
                _coverage(context),
                TranscriptSourceOutcome.NO_SPEECH,
                TranscriptCompleteness(
                    EvidenceCompleteness.COMPLETE,
                    EvidenceCompleteness.COMPLETE,
                    EvidenceCompleteness.COMPLETE,
                ),
                (),
                (),
                (),
            )
        words: list[TranscriptWord] = []
        sentences: list[TranscriptSentence] = []
        raw_segment_records: list[tuple[str, int, int, str, tuple[str, ...]]] = []
        pending_word_ids: list[str] = []
        pending_text: list[str] = []
        pending_start: int | None = None
        pending_end: int | None = None
        for segment_position, item in enumerate(raw_segments):
            text = item.get("text")
            if type(text) is not str or not text.strip():  # noqa: E721
                raise LocalMediaEvidenceError("Whisper segment text must be non-empty")
            start = _relative_seconds_tick(
                item.get("start"), context, "whisper.segment.start", end=False
            )
            end = _relative_seconds_tick(item.get("end"), context, "whisper.segment.end", end=True)
            if start >= end:
                raise LocalMediaEvidenceError("Whisper segment tick range is empty")
            raw_words = _objects(item.get("words"), "whisper.segment.words")
            if not raw_words:
                raise LocalMediaEvidenceError("Whisper word timestamps are required")
            word_ids: list[str] = []
            for word_position, raw_word in enumerate(raw_words):
                word_text = raw_word.get("word")
                if type(word_text) is not str or not word_text.strip():  # noqa: E721
                    raise LocalMediaEvidenceError("Whisper word text must be non-empty")
                word_start = _relative_seconds_tick(
                    raw_word.get("start"), context, "whisper.word.start", end=False
                )
                word_end = _relative_seconds_tick(
                    raw_word.get("end"), context, "whisper.word.end", end=True
                )
                if not start <= word_start < word_end <= end:
                    raise LocalMediaEvidenceError("Whisper word tick is outside its exact segment")
                word_id = f"{request.episode_id}:word:{len(words):08d}"
                word_ids.append(word_id)
                words.append(
                    TranscriptWord(
                        word_id,
                        request.source_id,
                        request.source_sha256,
                        context.clock_id,
                        context.time_base,
                        word_start,
                        word_end,
                        word_text.strip(),
                    )
                )
                if pending_start is None:
                    pending_start = word_start
                pending_end = word_end
                pending_word_ids.append(word_id)
                pending_text.append(word_text)
                closes_sentence = _TERMINAL_PUNCTUATION.search(word_text) is not None
                if (
                    word_position == len(raw_words) - 1
                    and _TERMINAL_PUNCTUATION.search(text) is not None
                ):
                    closes_sentence = True
                if closes_sentence:
                    sentence_id = f"{request.episode_id}:sentence:{len(sentences):08d}"
                    sentences.append(
                        TranscriptSentence(
                            sentence_id,
                            request.source_id,
                            request.source_sha256,
                            context.clock_id,
                            context.time_base,
                            pending_start,
                            pending_end,
                            tuple(pending_word_ids),
                            "".join(pending_text).strip(),
                        )
                    )
                    pending_word_ids = []
                    pending_text = []
                    pending_start = None
                    pending_end = None
            segment_id = f"{request.episode_id}:segment:{segment_position:08d}"
            raw_segment_records.append((segment_id, start, end, text.strip(), tuple(word_ids)))
        if pending_word_ids:
            raise LocalMediaEvidenceError("Whisper transcript ends with an unclosed sentence tail")
        segments: list[TranscriptSegment] = []
        for segment_id, start, end, text, _word_ids in raw_segment_records:
            contained_sentence_ids = tuple(
                sentence.sentence_id
                for sentence in sentences
                if start <= sentence.in_tick and sentence.out_tick <= end
            )
            segments.append(
                TranscriptSegment(
                    segment_id,
                    request.source_id,
                    request.source_sha256,
                    context.clock_id,
                    context.time_base,
                    start,
                    end,
                    contained_sentence_ids,
                    text,
                )
            )
        return TranscriptSet(
            f"{request.episode_id}:transcript",
            context,
            _coverage(context),
            TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE,
            TranscriptCompleteness(
                EvidenceCompleteness.COMPLETE,
                EvidenceCompleteness.COMPLETE,
                EvidenceCompleteness.COMPLETE,
            ),
            tuple(segments),
            tuple(words),
            tuple(sentences),
        )

    @staticmethod
    def _not_applicable_transcript(request: LocalMediaPreflightRequest) -> TranscriptSet:
        context = replace(
            request.audio_sample_boundaries.context,
            producer_id=request.policy.calibration("asr").producer_id,
            generation_policy_sha256=request.policy.producer_policy_sha256("asr"),
        )
        return TranscriptSet(
            f"{request.episode_id}:transcript",
            context,
            _coverage(context),
            TranscriptSourceOutcome.NOT_APPLICABLE,
            TranscriptCompleteness(
                EvidenceCompleteness.NOT_APPLICABLE,
                EvidenceCompleteness.NOT_APPLICABLE,
                EvidenceCompleteness.NOT_APPLICABLE,
            ),
            (),
            (),
            (),
        )

    @staticmethod
    def _not_applicable_speech(request: LocalMediaPreflightRequest) -> SpeechActivitySet:
        context = replace(
            request.audio_sample_boundaries.context,
            producer_id=request.policy.calibration("vad").producer_id,
            generation_policy_sha256=request.policy.producer_policy_sha256("vad"),
        )
        return SpeechActivitySet(
            f"{request.episode_id}:speech-activity",
            context,
            _coverage(context),
            SpeechSourceOutcome.NOT_APPLICABLE,
            (),
        )

    @staticmethod
    def _speech_request(
        path: Path,
        request: LocalMediaPreflightRequest,
        *,
        kernel_max_source_bytes: int,
        service_max_request_bytes: int,
    ) -> TimedSpeechEvidenceRequest:
        policy = request.policy
        context = request.audio_sample_boundaries.context
        if policy.word_timing_capability != "required":
            raise LocalMediaPolicyError(
                "sensevoice_word_guard_v1 requires real word timestamps"
            )
        expected = []
        for kind in cast(tuple[Literal["asr", "vad"], ...], ("asr", "vad")):
            c = policy.calibration(kind)
            expected.append(
                TimedSpeechExpectedProducer(
                    kind,
                    c.producer_id,
                    c.producer_version,
                    c.generation_policy_sha256,
                    c.detector_sha256,
                    c.calibration_policy_sha256,
                    c.calibration_record_sha256,
                    _microseconds_tick(c.timing_error_bound_microseconds, context.time_base),
                    policy.asr_model_id if kind == "asr" else policy.vad_model_id,
                    policy.asr_model_revision if kind == "asr" else policy.vad_model_revision,
                    policy.asr_model_sha256 if kind == "asr" else policy.vad_model_sha256,
                    policy.timed_speech_service_sha256,
                    "sensevoice-word-timestamp" if kind == "asr" else "fsmn-vad-direct",
                )
            )
        return TimedSpeechEvidenceRequest(
            path,
            request.source_id,
            request.source_sha256,
            kernel_max_source_bytes,
            service_max_request_bytes,
            min(kernel_max_source_bytes, service_max_request_bytes),
            context.clock_id,
            context.time_base,
            context.origin_tick,
            context.duration_tick,
            context.origin_tick,
            context.end_tick,
            policy.timed_speech_endpoint_url,
            policy.timed_speech_provider_id,
            policy.timed_speech_provider_version,
            policy.funasr_version,
            policy.torch_version,
            cast(Literal["cpu", "mps"], policy.speech_device),
            "required",
            policy.timed_speech_policy_sha256,
            policy.timed_speech_calibration_sha256,
            cast(tuple[TimedSpeechExpectedProducer, TimedSpeechExpectedProducer], tuple(expected)),
            policy.timed_speech_timeout_seconds,
            policy.timed_speech_max_response_bytes,
            policy.utterance_gap_milliseconds,
            policy.vad_merge_gap_milliseconds,
        )

    @staticmethod
    def _validate_timed_speech_evidence(
        evidence: TimedSpeechEvidence, request: LocalMediaPreflightRequest
    ) -> None:
        context = request.audio_sample_boundaries.context
        if request.policy.word_timing_capability != "required":
            raise LocalMediaPolicyError(
                "sensevoice_word_guard_v1 requires real word timestamps"
            )
        expected_word = EvidenceCompleteness.COMPLETE
        if (
            evidence.transcript.completeness
            != TranscriptCompleteness(
                EvidenceCompleteness.COMPLETE,
                expected_word,
                EvidenceCompleteness.NOT_APPLICABLE,
            )
            or evidence.transcript.sentences
            or any(segment.sentence_ids for segment in evidence.transcript.segments)
        ):
            raise LocalMediaEvidenceError(
                "SenseVoice word-gap output cannot claim sentence evidence"
            )
        for actual in (evidence.transcript.context, evidence.speech_activity.context):
            if (
                actual.source_id,
                actual.source_sha256,
                actual.clock_id,
                actual.time_base,
                actual.origin_tick,
                actual.duration_tick,
            ) != (
                request.source_id,
                request.source_sha256,
                context.clock_id,
                context.time_base,
                context.origin_tick,
                context.duration_tick,
            ):
                raise LocalMediaSourceError("timed speech evidence source clock drift")
        for actual, expected, bound in zip(
            evidence.producer_identities,
            request.policy.calibrations[2:4],
            evidence.timing_error_bounds,
            strict=True,
        ):
            model_id = (
                request.policy.asr_model_id
                if actual.producer_kind == "asr"
                else request.policy.vad_model_id
            )
            model_revision = (
                request.policy.asr_model_revision
                if actual.producer_kind == "asr"
                else request.policy.vad_model_revision
            )
            model_sha256 = (
                request.policy.asr_model_sha256
                if actual.producer_kind == "asr"
                else request.policy.vad_model_sha256
            )
            if (
                actual.producer_id,
                actual.producer_version,
                actual.generation_policy_sha256,
                actual.detector_sha256,
                actual.calibration_policy_sha256,
                actual.calibration_record_sha256,
                actual.model_id,
                actual.model_revision,
                actual.model_sha256,
                actual.service_sha256,
                actual.device,
            ) != (
                expected.producer_id,
                expected.producer_version,
                expected.generation_policy_sha256,
                expected.detector_sha256,
                expected.calibration_policy_sha256,
                expected.calibration_record_sha256,
                model_id,
                model_revision,
                model_sha256,
                request.policy.timed_speech_service_sha256,
                request.policy.speech_device,
            ):
                raise LocalMediaSourceError("measured timed speech producer identity drift")
            expected_bound = _microseconds_tick(
                expected.timing_error_bound_microseconds, bound.time_base
            )
            if max(bound.early_tick, bound.late_tick) > expected_bound:
                raise LocalMediaEvidenceError("measured timing error exceeds calibration")

    @staticmethod
    def _timed_speech_traces(evidence: TimedSpeechEvidence) -> tuple[ToolInvocationTrace, ...]:
        trace = evidence.invocation_trace
        empty_sha256 = _sha_bytes(b"")
        return tuple(
            ToolInvocationTrace(
                identity.producer_kind,
                "funasr-http",
                trace.service_sha256,
                canonical_sha256(
                    {name: getattr(identity, name) for name in identity.__dataclass_fields__}
                ),
                trace.request_sha256,
                trace.response_sha256,
                empty_sha256,
            )
            for identity in evidence.producer_identities
        )

    @staticmethod
    def _producer_identity(
        kind: ProducerKind,
        request: LocalMediaPreflightRequest,
        timed: TimedSpeechEvidence | None,
    ) -> ProducerIdentity:
        calibration = request.policy.calibration(kind)
        time_base = (
            request.audio_sample_boundaries.context.time_base
            if kind in {"audio", "asr", "vad"}
            else request.frame_pts_index.context.time_base
        )
        if kind in {"asr", "vad"} and timed is not None:
            position = 0 if kind == "asr" else 1
            measured = timed.producer_identities[position]
            bound = timed.timing_error_bounds[position]
            return ProducerIdentity(
                kind,
                measured.producer_id,
                measured.producer_version,
                measured.generation_policy_sha256,
                measured.detector_sha256,
                measured.calibration_policy_sha256,
                measured.calibration_record_sha256,
                max(bound.early_tick, bound.late_tick),
            )
        return ProducerIdentity(
            kind,
            calibration.producer_id,
            calibration.producer_version,
            request.policy.producer_policy_sha256(kind),
            calibration.detector_sha256,
            calibration.calibration_policy_sha256,
            calibration.calibration_record_sha256,
            _microseconds_tick(calibration.timing_error_bound_microseconds, time_base),
        )

    @staticmethod
    def _calibration_binding(
        identity: ProducerIdentity, request: LocalMediaPreflightRequest
    ) -> CalibrationBinding:
        time_base = (
            request.audio_sample_boundaries.context.time_base
            if identity.producer_kind in {"audio", "asr", "vad"}
            else request.frame_pts_index.context.time_base
        )
        return CalibrationBinding(
            policy_sha256=identity.producer_policy_sha256,
            detector_sha256=identity.detector_sha256,
            calibration_record_sha256=identity.calibration_record_sha256,
            producer_id=identity.producer_id,
            producer_version=identity.producer_version,
            time_base=time_base,
            timing_error_bound_tick=identity.timing_error_bound_tick,
            active=True,
        )


__all__ = ["LocalMediaPreflightPort"]
