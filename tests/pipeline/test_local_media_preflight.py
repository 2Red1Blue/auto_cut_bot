"""Production-port tests use a fake process boundary, never fake evidence outcomes."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from autocut_kernel.media import (
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
    SpeechActivitySegment,
    SpeechActivitySet,
    SpeechSourceOutcome,
    SubtitleSourceOutcome,
    TimeBase,
    TranscriptCompleteness,
    TranscriptSegment,
    TranscriptSet,
    TranscriptSourceOutcome,
    TranscriptWord,
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
    LocalMediaSourceError,
    LocalMediaToolError,
    ProducerCalibrationIdentity,
    TimedSpeechEvidence,
    TimedSpeechEvidenceRequest,
    TimedSpeechInvocationTrace,
    TimedSpeechProducerIdentity,
    TimedSpeechTimingErrorBound,
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


def _policy(_model: Path, frame_policy: str, audio_policy: str) -> LocalMediaPreflightPolicy:
    producer_ids = {
        "frame": "frame-decoder-v1",
        "audio": "audio-decoder-v1",
        "asr": "sensevoice-asr-v1",
        "vad": "fsmn-vad-v1",
        "shot": "pixel-shot-v1",
        "scene": "pixel-scene-v1",
        "visual": "pixel-visual-v1",
        "subtitle": "subtitle-presence-v1",
    }
    policies = {
        "frame": frame_policy,
        "audio": audio_policy,
        **{
            kind: f"sha256:{position + 1:064x}"
            for position, kind in enumerate(tuple(producer_ids)[2:])
        },
    }
    return LocalMediaPreflightPolicy(
        policy_id="media-preflight-policy-1",
        policy_version="1.0.0",
        timed_speech_endpoint_url="http://127.0.0.1:8765/v1/timed-speech-evidence",
        timed_speech_provider_id="funasr-http-v1",
        timed_speech_provider_version="1.0.0",
        timed_speech_service_sha256="sha256:" + "a" * 64,
        funasr_version="1.4.3",
        torch_version="2.7.1",
        speech_device="cpu",
        word_timing_capability="required",
        asr_model_id="SenseVoiceSmall",
        asr_model_revision="master",
        asr_model_sha256="sha256:" + "6" * 64,
        vad_model_id="fsmn-vad",
        vad_model_revision="v2.0.4",
        vad_model_sha256="sha256:" + "7" * 64,
        timed_speech_policy_sha256="sha256:" + "8" * 64,
        timed_speech_calibration_sha256="sha256:" + "9" * 64,
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
        timed_speech_timeout_seconds=30,
        timed_speech_max_response_bytes=1024 * 1024,
        utterance_gap_milliseconds=700,
        vad_merge_gap_milliseconds=350,
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
            _calibration(kind, producer_ids[kind], policies[kind]) for kind in producer_ids
        ),
    )


class _Runner:
    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []

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
        raise AssertionError(args)


class _SpeechPort:
    def __init__(self) -> None:
        self.requests: list[TimedSpeechEvidenceRequest] = []

    def produce(self, r: TimedSpeechEvidenceRequest) -> TimedSpeechEvidence:
        self.requests.append(r)
        a, v = r.expected_producers
        ac = replace(
            EvidenceContext(
                r.source_id,
                r.source_sha256,
                MediaKind.AUDIO,
                r.clock_id,
                r.time_base,
                r.origin_tick,
                r.duration_tick,
                a.producer_id,
                a.generation_policy_sha256,
            )
        )
        vc = replace(
            ac, producer_id=v.producer_id, generation_policy_sha256=v.generation_policy_sha256
        )
        cov = Coverage(
            r.source_id,
            r.source_sha256,
            r.clock_id,
            r.time_base,
            r.origin_tick,
            r.requested_out_tick,
            CoverageOutcome.COMPLETE,
        )
        w = TranscriptWord(
            "w", r.source_id, r.source_sha256, r.clock_id, r.time_base, 500, 1000, "你好。"
        )
        g = TranscriptSegment(
            "g", r.source_id, r.source_sha256, r.clock_id, r.time_base, 500, 1000, (), "你好。"
        )
        tr = TranscriptSet(
            "t",
            ac,
            cov,
            TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE,
            TranscriptCompleteness(
                EvidenceCompleteness.COMPLETE,
                EvidenceCompleteness.COMPLETE,
                EvidenceCompleteness.NOT_APPLICABLE,
            ),
            (g,),
            (w,),
            (),
        )
        sp = SpeechActivitySet(
            "v",
            vc,
            cov,
            SpeechSourceOutcome.SPEECH_DETECTED,
            (
                SpeechActivitySegment(
                    "v", r.source_id, r.source_sha256, r.clock_id, r.time_base, 400, 1100, None
                ),
            ),
        )
        ids = tuple(
            TimedSpeechProducerIdentity(
                x.producer_kind,
                r.provider_id,
                r.provider_version,
                r.funasr_version,
                r.torch_version,
                r.device,
                x.model_id,
                x.model_revision,
                x.model_sha256,
                x.producer_id,
                x.producer_version,
                x.generation_policy_sha256,
                x.detector_sha256,
                x.calibration_policy_sha256,
                x.calibration_record_sha256,
                x.service_sha256,
                x.inference_kind,
            )
            for x in r.expected_producers
        )
        return TimedSpeechEvidence(
            tr,
            sp,
            ids,
            tuple(
                TimedSpeechTimingErrorBound(x.producer_kind, 1000, 1000, r.time_base)
                for x in r.expected_producers
            ),
            TimedSpeechInvocationTrace(
                r.endpoint_url,
                r.identity_sha256,
                "sha256:" + "a" * 64,
                "sha256:" + "b" * 64,
                r.expected_producers[0].service_sha256,
            ),
        )  # type: ignore[arg-type]


def _request(
    tmp_path: Path, runner: _Runner
) -> tuple[LocalMediaPreflightPort, LocalMediaPreflightRequest, _SpeechPort]:
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"verified-mp4-materialization")
    model = tmp_path / "tiny.pt"
    model.write_bytes(b"verified-whisper-model")
    executables = []
    for name in ("ffprobe", "ffmpeg"):
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
        "source-1",
        _sha(source.read_bytes()),
        "source-1:video",
        TimeBase(1, 100),
        0,
        400,
        CoverageOutcome.COMPLETE,
    )
    audio_coverage = Coverage(
        "source-1",
        _sha(source.read_bytes()),
        "source-1:audio",
        TimeBase(1, 1000),
        0,
        4000,
        CoverageOutcome.COMPLETE,
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
    speech = _SpeechPort()
    port = LocalMediaPreflightPort(
        ffprobe_executable=str(executables[0]),
        ffmpeg_executable=str(executables[1]),
        speech_port=speech,
        runner=runner,
    )
    policy = _policy(model.resolve(), frame_policy, audio_policy)
    measured = port.measure_detector_identity_sha256s(policy)
    measured["asr"] = policy.timed_speech_detector_sha256("asr")
    measured["vad"] = policy.timed_speech_detector_sha256("vad")
    policy = replace(
        policy,
        calibrations=tuple(
            replace(item, detector_sha256=measured[item.producer_kind])
            for item in policy.calibrations
        ),
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
        measured["frame"],
        measured["audio"],
        policy,
    )
    return port, request, speech


def test_prepare_builds_conjunctive_real_tool_evidence(tmp_path: Path) -> None:
    runner = _Runner()
    port, request, speech = _request(tmp_path, runner)

    result = port.prepare(
        request, kernel_max_source_bytes=1024 * 1024, service_max_request_bytes=1024 * 1024
    )

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
    assert result.provenance_mapping()["tool_trace_sha256"] == (result.tool_trace.canonical_hash)
    assert {item.executable for item in result.tool_trace.invocations} <= {
        "ffmpeg",
        "ffprobe",
        "funasr-http",
    }
    speech_traces = [
        item for item in result.tool_trace.invocations if item.executable == "funasr-http"
    ]
    assert [item.producer_kind for item in speech_traces] == ["asr", "vad"]
    assert all(item.executable_sha256 == request.policy.timed_speech_service_sha256 for item in speech_traces)
    assert result.producer_identities[2].detector_sha256 == speech.requests[0].expected_producers[0].detector_sha256
    assert result.producer_identities[3].detector_sha256 == speech.requests[0].expected_producers[1].detector_sha256
    assert speech.requests[0].word_timing_capability == "required"
    assert all("-nostdin" in argv for argv in runner.argvs if "ffmpeg" in Path(argv[0]).name)
    rawvideo_argv = next(argv for argv in runner.argvs if "rawvideo" in argv)
    assert "-copyts" in rawvideo_argv


def test_replaced_detector_binary_cannot_reuse_old_calibration(tmp_path: Path) -> None:
    port, request, _ = _request(tmp_path, _Runner())
    mismatched = replace(
        request.policy,
        calibrations=(
            replace(request.policy.calibrations[0], detector_sha256="sha256:" + "0" * 64),
            *request.policy.calibrations[1:],
        ),
    )

    with pytest.raises(LocalMediaEvidenceError, match="detector bytes/version/model"):
        port.prepare(
            replace(request, policy=mismatched),
            kernel_max_source_bytes=1024 * 1024,
            service_max_request_bytes=1024 * 1024,
        )


def test_production_process_never_invokes_whisper_or_silencedetect(tmp_path: Path) -> None:
    runner = _Runner()
    port, request, _ = _request(tmp_path, runner)
    port.prepare(
        request, kernel_max_source_bytes=1024 * 1024, service_max_request_bytes=1024 * 1024
    )
    flattened = " ".join(x for argv in runner.argvs for x in argv).lower()
    assert "whisper" not in flattened and "silencedetect" not in flattened


def test_effective_source_limit_stops_detectors_and_timed_speech_dispatch(tmp_path: Path) -> None:
    runner = _Runner()
    port, request, speech = _request(tmp_path, runner)
    runner.argvs.clear()

    with pytest.raises(LocalMediaSourceError, match="effective source-byte limit"):
        port.prepare(request, kernel_max_source_bytes=1024 * 1024, service_max_request_bytes=1)

    assert runner.argvs == []
    assert speech.requests == []


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
