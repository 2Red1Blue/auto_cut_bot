"""Production-port tests use a fake process boundary, never fake evidence outcomes."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from autocut_kernel.media import (
    AudioBoundaryMethod,
    AudioSampleBoundary,
    AudioSampleBoundarySet,
    AudioSourceOutcome,
    Coverage,
    CoverageOutcome,
    EvidenceContext,
    FramePtsIndexSet,
    MediaKind,
    PTSIndex,
    SpeechSourceOutcome,
    SubtitleSourceOutcome,
    TimeBase,
    VisualClassification,
)
from autocut_kernel.media.types import canonical_sha256

from auto_cut_bot.pipeline.media_preflight import (
    BoundedSubprocessRunner,
    CommandOutput,
    LocalMediaEvidenceError,
    LocalMediaPolicyError,
    LocalMediaPreflightPolicy,
    LocalMediaPreflightPort,
    LocalMediaPreflightRequest,
    LocalMediaToolError,
    ProducerCalibrationIdentity,
)


def _sha(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _calibration(kind: str, producer_id: str, policy_sha: str) -> ProducerCalibrationIdentity:
    return ProducerCalibrationIdentity(
        producer_kind=kind,  # type: ignore[arg-type]
        producer_id=producer_id,
        producer_version="1.0.0",
        generation_policy_sha256=policy_sha,
        detector_sha256="sha256:" + "d" * 64,
        calibration_policy_sha256="sha256:" + "c" * 64,
        calibration_record_sha256="sha256:" + "b" * 64,
        timing_error_bound_microseconds=1_000_000,
    )


def _policy(model: Path, frame_policy: str, audio_policy: str) -> LocalMediaPreflightPolicy:
    producer_ids = {
        "frame": "frame-decoder-v1",
        "audio": "audio-decoder-v1",
        "asr": "whisper-asr-v1",
        "vad": "silencedetect-vad-v1",
        "shot": "pixel-shot-v1",
        "scene": "pixel-scene-v1",
        "visual": "pixel-visual-v1",
        "subtitle": "subtitle-presence-v1",
    }
    policies = {
        "frame": frame_policy,
        "audio": audio_policy,
        **{kind: f"sha256:{position + 1:064x}" for position, kind in enumerate(tuple(producer_ids)[2:])},
    }
    return LocalMediaPreflightPolicy(
        policy_id="media-preflight-policy-1",
        policy_version="1.0.0",
        whisper_model_name="tiny",
        whisper_model_path=model,
        whisper_model_sha256=_sha(model.read_bytes()),
        whisper_language="zh",
        initial_left_expansion_milliseconds=500,
        initial_right_expansion_milliseconds=500,
        expansion_step_milliseconds=250,
        max_expansion_count=4,
        boundary_touch_margin_milliseconds=100,
        analysis_fps_numerator=1,
        analysis_fps_denominator=1,
        analysis_width=2,
        analysis_height=2,
        max_analysis_frames=10,
        max_stdout_bytes=1024 * 1024,
        max_stderr_bytes=64 * 1024,
        probe_timeout_seconds=10,
        analysis_timeout_seconds=20,
        whisper_timeout_seconds=30,
        vad_noise_db=-35,
        vad_min_silence_milliseconds=200,
        black_luma_max=10,
        white_luma_min=245,
        frozen_change_ppm_max=1_000,
        transition_change_ppm_min=300_000,
        shot_change_ppm_min=100_000,
        scene_change_ppm_min=200_000,
        subtitle_edge_delta_min=100,
        subtitle_edge_fraction_ppm_min=500_000,
        subtitle_min_consecutive_samples=2,
        calibrations=tuple(
            _calibration(kind, producer_ids[kind], policies[kind])
            for kind in producer_ids
        ),
    )


class _Runner:
    def __init__(
        self, *, complete_sentence: bool = True, split_sentence: bool = False
    ) -> None:
        self.argvs: list[tuple[str, ...]] = []
        self.complete_sentence = complete_sentence
        self.split_sentence = split_sentence

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CommandOutput:
        del timeout_seconds, max_stdout_bytes, max_stderr_bytes
        args = tuple(argv)
        self.argvs.append(args)
        if "-version" in args or "--help" in args:
            return CommandOutput(args, 0, b"tool version 1\n", b"")
        if "-show_streams" in args:
            payload = {
                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "time_base": "1/100",
                        "start_pts": 0,
                        "duration_ts": 400,
                    },
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "time_base": "1/1000",
                        "start_pts": 0,
                        "duration_ts": 4000,
                    },
                ],
            }
            return CommandOutput(args, 0, json.dumps(payload).encode(), b"")
        if "rawvideo" in args:
            return CommandOutput(
                args,
                0,
                bytes([128] * 4 + [128] * 4 + [0] * 4 + [255] * 4),
                b"\n".join(
                    f"[Parsed_showinfo_3] n: {index} pts: {index} pts_time:{index}".encode()
                    for index in range(4)
                ),
            )
        if any("silencedetect=" in item for item in args):
            return CommandOutput(args, 0, b"", b"")
        if "--output_format" in args:
            output_dir = Path(args[args.index("--output_dir") + 1])
            source = Path(args[1])
            text = "你好。" if self.complete_sentence else "你好"
            segments = [
                    {
                        "start": 0.5,
                        "end": 1.0,
                        "text": text,
                        "words": [{"start": 0.5, "end": 1.0, "word": text}],
                    }
                ]
            if self.split_sentence:
                segments = [
                    {
                        "start": 0.5,
                        "end": 0.75,
                        "text": "你",
                        "words": [{"start": 0.5, "end": 0.75, "word": "你"}],
                    },
                    {
                        "start": 0.75,
                        "end": 1.0,
                        "text": "好。",
                        "words": [{"start": 0.75, "end": 1.0, "word": "好。"}],
                    },
                ]
            payload = {"segments": segments}
            (output_dir / f"{source.stem}.json").write_text(json.dumps(payload))
            return CommandOutput(args, 0, b"", b"")
        raise AssertionError(args)


def _request(tmp_path: Path, runner: _Runner) -> tuple[LocalMediaPreflightPort, LocalMediaPreflightRequest]:
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"verified-mp4-materialization")
    model = tmp_path / "tiny.pt"
    model.write_bytes(b"verified-whisper-model")
    executables = []
    for name in ("ffprobe", "ffmpeg", "whisper"):
        executable = tmp_path / name
        executable.write_bytes(name.encode())
        executables.append(executable)
    frame_policy = "sha256:" + "1" * 64
    audio_policy = "sha256:" + "2" * 64
    video_context = EvidenceContext(
        "source-1",
        _sha(source.read_bytes()),
        MediaKind.VIDEO,
        "source-1:video",
        TimeBase(1, 100),
        0,
        400,
        "frame-decoder-v1",
        frame_policy,
    )
    audio_context = EvidenceContext(
        "source-1",
        _sha(source.read_bytes()),
        MediaKind.AUDIO,
        "source-1:audio",
        TimeBase(1, 1000),
        0,
        4000,
        "audio-decoder-v1",
        audio_policy,
    )
    frame_coverage = Coverage(
        "source-1", _sha(source.read_bytes()), "source-1:video", TimeBase(1, 100), 0, 400, CoverageOutcome.COMPLETE
    )
    audio_coverage = Coverage(
        "source-1", _sha(source.read_bytes()), "source-1:audio", TimeBase(1, 1000), 0, 4000, CoverageOutcome.COMPLETE
    )
    pts = PTSIndex((0, 100, 200, 300))
    frame_set = FramePtsIndexSet(
        "frames", video_context, frame_coverage, pts, canonical_sha256(list(pts.ticks))
    )
    audio_set = AudioSampleBoundarySet(
        "audio",
        audio_context,
        audio_coverage,
        AudioSourceOutcome.BOUNDARIES_AVAILABLE,
        tuple(
            AudioSampleBoundary(
                f"audio-{tick}",
                "source-1",
                _sha(source.read_bytes()),
                "source-1:audio",
                TimeBase(1, 1000),
                tick,
                AudioBoundaryMethod.DECODER,
            )
            for tick in (0, 4000)
        ),
    )
    policy = _policy(model.resolve(), frame_policy, audio_policy)
    port = LocalMediaPreflightPort(
        ffprobe_executable=str(executables[0]),
        ffmpeg_executable=str(executables[1]),
        whisper_executable=str(executables[2]),
        runner=runner,
    )
    request = LocalMediaPreflightRequest(
        source.resolve(),
        "episode-1",
        "source-1",
        _sha(source.read_bytes()),
        "sha256:" + "3" * 64,
        "sha256:" + "4" * 64,
        "sha256:" + "5" * 64,
        frame_set,
        audio_set,
        policy,
    )
    return port, request


def test_prepare_builds_conjunctive_real_tool_evidence(tmp_path: Path) -> None:
    runner = _Runner()
    port, request = _request(tmp_path, runner)

    result = port.prepare(request)

    assert result.evidence.transcript.words[0].in_tick == 500
    assert result.evidence.speech_activity.source_outcome is SpeechSourceOutcome.SPEECH_DETECTED
    assert result.evidence.subtitle_cues.source_outcome is SubtitleSourceOutcome.NONE_DETECTED
    assert [item.classification for item in result.evidence.visual_validity.intervals] == [
        VisualClassification.VALID_CONTENT,
        VisualClassification.FROZEN,
        VisualClassification.BLACK,
        VisualClassification.WHITE,
    ]
    assert len(result.producer_identities) == 8
    assert len(result.calibration_bindings) == 8
    assert all("-nostdin" in argv for argv in runner.argvs if "ffmpeg" in Path(argv[0]).name)
    rawvideo_argv = next(argv for argv in runner.argvs if "rawvideo" in argv)
    assert "-copyts" in rawvideo_argv


def test_incomplete_whisper_sentence_is_indeterminate(tmp_path: Path) -> None:
    port, request = _request(tmp_path, _Runner(complete_sentence=False))

    with pytest.raises(LocalMediaEvidenceError, match="unclosed sentence tail"):
        port.prepare(request)


def test_sentence_can_close_across_whisper_segments(tmp_path: Path) -> None:
    port, request = _request(tmp_path, _Runner(split_sentence=True))

    result = port.prepare(request)

    assert len(result.evidence.transcript.segments) == 2
    assert len(result.evidence.transcript.sentences) == 1
    assert result.evidence.transcript.sentences[0].text == "你好。"


def test_policy_mapping_is_closed_and_hashes_all_values(tmp_path: Path) -> None:
    model = tmp_path / "tiny.pt"
    model.write_bytes(b"model")
    policy = _policy(model.resolve(), "sha256:" + "1" * 64, "sha256:" + "2" * 64)
    mapping = policy.to_mapping()

    restored = LocalMediaPreflightPolicy.from_mapping(mapping)

    assert restored.canonical_hash == policy.canonical_hash
    adaptive = restored.adaptive_window_policy(TimeBase(1, 90_000))
    assert adaptive.initial_left_expansion_pts == 45_000
    with pytest.raises(LocalMediaPolicyError, match="schema is not closed"):
        LocalMediaPreflightPolicy.from_mapping({**mapping, "unexpected": True})


def test_process_runner_enforces_hard_stderr_bound() -> None:
    runner = BoundedSubprocessRunner()

    with pytest.raises(LocalMediaToolError, match="bounded stderr"):
        runner.run(
            [sys.executable, "-c", "import sys;sys.stderr.write('x'*10000)"],
            timeout_seconds=5,
            max_stdout_bytes=100,
            max_stderr_bytes=100,
        )
