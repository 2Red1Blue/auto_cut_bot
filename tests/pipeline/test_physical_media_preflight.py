"""Speech-free physical prelude producer tests at the local process boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from autocut_kernel.media import (
    CalibrationBinding,
    Coverage,
    CoverageOutcome,
    EvidenceContext,
    FramePtsIndexSet,
    SceneBoundarySet,
    ShotBoundarySet,
    SubtitleCueSet,
    SubtitleDetectionMode,
    SubtitleSourceOutcome,
    VisualClassification,
    VisualValidityInterval,
    VisualValiditySet,
)
from autocut_kernel.pipeline.physical_media_contract import (
    PreparePhysicalMediaEvidenceRequest,
    ResolvedPreparePhysicalMediaEvidenceRequest,
)
from autocut_kernel.pipeline.prepare_timed_media_evidence_command import (
    TimedMediaEvidenceProducerError,
    resolve_committed_timed_media_request,
)
from autocut_kernel.store import BlobRef

from auto_cut_bot.pipeline.media_preflight import (
    CommandOutput,
    LocalMediaEvidenceError,
    LocalMediaPolicyError,
    LocalMediaPreflightPort,
    LocalMediaSourceError,
    LocalMediaToolError,
    ProducerCalibrationIdentity,
    ProducerIdentity,
    ToolInvocationTrace,
    ToolTrace,
)
from auto_cut_bot.pipeline.media_preflight.physical_adapter import (
    ClaimOwnedPhysicalMediaProducer,
)
from auto_cut_bot.pipeline.media_preflight.physical_models import (
    PhysicalMediaPolicy,
    PhysicalMediaRequest,
    PhysicalMediaResult,
)
from tests.media.test_prepare_timed_media_evidence_command import (
    _request as _source_request,
)
from tests.media.test_prepare_timed_media_evidence_command import _Store as _SourceStore
from tests.pipeline.test_local_media_preflight import _request as _local_request
from tests.pipeline.test_local_media_preflight import _Runner

_PHYSICAL_KINDS = ("frame", "audio", "shot", "scene", "visual", "subtitle")


def _physical_policy(local_policy: object) -> PhysicalMediaPolicy:
    fields = (
        "policy_id", "policy_version", "analysis_fps_numerator", "analysis_fps_denominator",
        "analysis_width", "analysis_height", "max_analysis_frames", "max_stdout_bytes",
        "max_stderr_bytes", "probe_timeout_seconds", "analysis_timeout_seconds", "black_luma_max",
        "white_luma_min", "frozen_change_ppm_max", "transition_change_ppm_min",
        "shot_change_ppm_min", "scene_change_ppm_min", "subtitle_edge_delta_min",
        "subtitle_edge_fraction_ppm_min", "subtitle_min_consecutive_samples",
    )
    values = {name: getattr(local_policy, name) for name in fields}
    values["calibrations"] = tuple(
        item for item in local_policy.calibrations if item.producer_kind in _PHYSICAL_KINDS
    )
    return PhysicalMediaPolicy(**values)


def _physical_request(
    tmp_path: Path,
) -> tuple[LocalMediaPreflightPort, PhysicalMediaRequest, object]:
    runner = _Runner()
    old_port, old_request, speech = _local_request(tmp_path, runner)
    policy = _physical_policy(old_request.policy)
    port = LocalMediaPreflightPort(
        ffprobe_executable=str(tmp_path / "ffprobe"),
        ffmpeg_executable=str(tmp_path / "ffmpeg"),
        runner=runner,
    )
    return port, PhysicalMediaRequest(
        source_path=str(old_request.source_path),
        episode_id=old_request.episode_id,
        source_id=old_request.source_id,
        source_sha256=old_request.source_sha256,
        source_provenance_sha256=old_request.source_provenance_sha256,
        source_manifest_sha256=old_request.source_manifest_sha256,
        root_input_manifest_sha256=old_request.root_input_manifest_sha256,
        physical_root_id="physical-root:test",
        frame_pts_index=old_request.frame_pts_index,
        audio_sample_boundaries=old_request.audio_sample_boundaries,
        frame_detector_sha256=old_request.frame_detector_sha256,
        audio_detector_sha256=old_request.audio_detector_sha256,
        policy=policy,
    ), speech


def test_prepare_physical_runs_only_six_physical_producers(tmp_path: Path) -> None:
    port, request, speech = _physical_request(tmp_path)

    result = port.prepare_physical(
        request, kernel_max_source_bytes=1_000_000, service_max_request_bytes=1_000_000,
    )

    assert port._speech_port is None
    assert speech.requests == []
    assert tuple(item.producer_kind for item in result.producer_identities) == _PHYSICAL_KINDS
    assert tuple(item.producer_id for item in result.calibration_bindings) == tuple(
        request.policy.calibration(kind).producer_id for kind in _PHYSICAL_KINDS
    )
    provenance = result.provenance_mapping()
    assert provenance["tool_trace_sha256"] == result.tool_trace.canonical_hash
    assert {item.executable for item in result.tool_trace.invocations} <= {"ffmpeg", "ffprobe"}


@pytest.mark.parametrize("value", (False, cast(int, 10.5)))
def test_physical_policy_rejects_non_integer_luma_thresholds(
    tmp_path: Path, value: int
) -> None:
    _port, request, _speech = _physical_request(tmp_path)

    with pytest.raises(LocalMediaPolicyError, match="black/white luma"):
        replace(request.policy, black_luma_max=value)

    with pytest.raises(LocalMediaPolicyError, match="black/white luma"):
        replace(request.policy, white_luma_min=value)


def test_physical_policy_rejects_single_pixel_analysis_width(tmp_path: Path) -> None:
    _port, request, _speech = _physical_request(tmp_path)

    with pytest.raises(LocalMediaPolicyError, match="at least two pixels"):
        replace(request.policy, analysis_width=1)


def test_physical_values_reject_untyped_calibrations_and_result_members(tmp_path: Path) -> None:
    port, request, _speech = _physical_request(tmp_path)

    with pytest.raises(LocalMediaPolicyError, match="exact typed"):
        replace(
            request.policy,
            calibrations=cast(
                tuple[ProducerCalibrationIdentity, ...],
                tuple(object() for _ in _PHYSICAL_KINDS),
            ),
        )

    result = port.prepare_physical(
        request, kernel_max_source_bytes=1_000_000, service_max_request_bytes=1_000_000,
    )
    with pytest.raises(LocalMediaEvidenceError, match="tool_trace"):
        replace(result, tool_trace=cast(ToolTrace, object()))
    with pytest.raises(LocalMediaEvidenceError, match="frame_pts_index"):
        replace(result, frame_pts_index=cast(FramePtsIndexSet, object()))
    with pytest.raises(LocalMediaEvidenceError, match="identities"):
        replace(
            result,
            producer_identities=cast(
                tuple[ProducerIdentity, ...], tuple(object() for _ in _PHYSICAL_KINDS)
            ),
        )
    with pytest.raises(LocalMediaEvidenceError, match="calibration bindings"):
        replace(
            result,
            calibration_bindings=cast(
                tuple[CalibrationBinding, ...], tuple(object() for _ in _PHYSICAL_KINDS)
            ),
        )


class _MutatingRunner(_Runner):
    def __init__(self, source: Path) -> None:
        super().__init__()
        self._source = source

    def run(self, argv, *, timeout_seconds, max_stdout_bytes, max_stderr_bytes):
        output = super().run(
            argv,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )
        if "rawvideo" in argv:
            self._source.write_bytes(b"source-mutated-during-physical-prelude")
        return output


def test_prepare_physical_rejects_source_mutation_after_detectors(tmp_path: Path) -> None:
    bootstrap_runner = _Runner()
    _old_port, old_request, _speech = _local_request(tmp_path, bootstrap_runner)
    policy = _physical_policy(old_request.policy)
    runner = _MutatingRunner(old_request.source_path)
    port = LocalMediaPreflightPort(
        ffprobe_executable=str(tmp_path / "ffprobe"),
        ffmpeg_executable=str(tmp_path / "ffmpeg"),
        runner=runner,
    )
    request = PhysicalMediaRequest(
        str(old_request.source_path), old_request.episode_id, old_request.source_id,
        old_request.source_sha256, old_request.source_provenance_sha256,
        old_request.source_manifest_sha256, old_request.root_input_manifest_sha256,
        "physical-root:test", old_request.frame_pts_index, old_request.audio_sample_boundaries,
        old_request.frame_detector_sha256, old_request.audio_detector_sha256, policy,
    )

    with pytest.raises(LocalMediaSourceError, match="changed during evidence production"):
        port.prepare_physical(
            request, kernel_max_source_bytes=1_000_000, service_max_request_bytes=1_000_000,
        )


class _FailingVisualRunner(_Runner):
    def run(self, argv, *, timeout_seconds, max_stdout_bytes, max_stderr_bytes):
        if "rawvideo" in argv:
            args = tuple(argv)
            return CommandOutput(args, 1, b"", b"synthetic detector failure")
        return super().run(
            argv,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )


def test_prepare_physical_stops_on_detector_failure_without_speech(tmp_path: Path) -> None:
    bootstrap_runner = _Runner()
    _old_port, old_request, speech = _local_request(tmp_path, bootstrap_runner)
    port = LocalMediaPreflightPort(
        ffprobe_executable=str(tmp_path / "ffprobe"),
        ffmpeg_executable=str(tmp_path / "ffmpeg"),
        runner=_FailingVisualRunner(),
    )
    request = PhysicalMediaRequest(
        str(old_request.source_path), old_request.episode_id, old_request.source_id,
        old_request.source_sha256, old_request.source_provenance_sha256,
        old_request.source_manifest_sha256, old_request.root_input_manifest_sha256,
        "physical-root:test", old_request.frame_pts_index, old_request.audio_sample_boundaries,
        old_request.frame_detector_sha256, old_request.audio_detector_sha256,
        _physical_policy(old_request.policy),
    )

    with pytest.raises(LocalMediaToolError, match="visual producer exited non-zero"):
        port.prepare_physical(
            request, kernel_max_source_bytes=1_000_000, service_max_request_bytes=1_000_000,
        )

    assert speech.requests == []


def _coverage(context: EvidenceContext) -> Coverage:
    return Coverage(
        context.source_id, context.source_sha256, context.clock_id, context.time_base,
        context.origin_tick, context.end_tick, CoverageOutcome.COMPLETE,
    )


def _adapter_policy(resolved) -> PhysicalMediaPolicy:
    calibrations = tuple(
        ProducerCalibrationIdentity(
            kind,
            f"{kind}-producer",
            "1.0.0",
            f"sha256:{position + 1:064x}",
            resolved.frame_detector_sha256 if kind == "frame" else (
                resolved.audio_detector_sha256 if kind == "audio" else f"sha256:{position + 11:064x}"
            ),
            f"sha256:{position + 21:064x}",
            f"sha256:{position + 31:064x}",
            1,
        )
        for position, kind in enumerate(_PHYSICAL_KINDS)
    )
    return PhysicalMediaPolicy(
        "physical-policy", "1", 1, 1, 2, 2, 10, 1024, 1024, 1, 1,
        10, 245, 1_000, 300_000, 100_000, 200_000, 100, 500_000, 2, calibrations,
    )


def _adapter_result(resolved, policy: PhysicalMediaPolicy) -> PhysicalMediaResult:
    frame = resolved.frame_pts_index
    audio = resolved.audio_sample_boundaries
    by_kind = {item.producer_kind: item for item in policy.calibrations}

    def video_context(kind: str) -> EvidenceContext:
        calibration = by_kind[kind]
        return replace(
            frame.context,
            producer_id=calibration.producer_id,
            generation_policy_sha256=calibration.generation_policy_sha256,
        )

    shot_context, scene_context = video_context("shot"), video_context("scene")
    visual_context, subtitle_context = video_context("visual"), video_context("subtitle")
    shot = ShotBoundarySet("shot", shot_context, _coverage(shot_context), frame.canonical_hash, ())
    scene = SceneBoundarySet("scene", scene_context, _coverage(scene_context), frame.canonical_hash, ())
    visual = VisualValiditySet(
        "visual", visual_context, _coverage(visual_context), (
            VisualValidityInterval(
                "visual-0", visual_context.source_id, visual_context.source_sha256,
                visual_context.clock_id, visual_context.time_base, visual_context.origin_tick,
                visual_context.end_tick, VisualClassification.VALID_CONTENT, 1_000_000,
            ),
        ),
    )
    subtitle = SubtitleCueSet(
        "subtitle", subtitle_context, _coverage(subtitle_context),
        (SubtitleDetectionMode.BURNED_IN,), (SubtitleDetectionMode.BURNED_IN,),
        SubtitleSourceOutcome.NONE_DETECTED, (),
    )
    identities = tuple(
        ProducerIdentity(
            calibration.producer_kind, calibration.producer_id, calibration.producer_version,
            calibration.generation_policy_sha256, calibration.detector_sha256,
            calibration.calibration_policy_sha256, calibration.calibration_record_sha256, 1, None,
        )
        for calibration in policy.calibrations
    )
    bindings = tuple(
        CalibrationBinding(
            identity.producer_policy_sha256, identity.detector_sha256,
            identity.calibration_record_sha256, identity.producer_id, identity.producer_version,
            audio.context.time_base if identity.producer_kind == "audio" else frame.context.time_base,
            identity.timing_error_bound_tick, True, None,
        )
        for identity in identities
    )
    trace = ToolTrace((ToolInvocationTrace(
        "frame", "ffprobe", "sha256:" + "a" * 64, "sha256:" + "b" * 64,
        "sha256:" + "c" * 64, "sha256:" + "d" * 64, "sha256:" + "e" * 64,
    ),))
    return PhysicalMediaResult(
        frame, shot, scene, audio, visual, subtitle, trace, identities, bindings,
        resolved.source_provenance_sha256,
    )


class _PhysicalPortSpy:
    def __init__(self, result: PhysicalMediaResult) -> None:
        self.result = result
        self.requests: list[PhysicalMediaRequest] = []

    def prepare_physical(
        self, request: PhysicalMediaRequest, *, kernel_max_source_bytes: int, service_max_request_bytes: int,
    ) -> PhysicalMediaResult:
        assert kernel_max_source_bytes > 0 and service_max_request_bytes > 0
        self.requests.append(request)
        return self.result


class _Lease:
    def __init__(self, reference: BlobRef, path: Path) -> None:
        self.reference = reference
        self.path = path

    def close(self) -> None:
        return None


def test_claim_owned_adapter_binds_exact_lease_and_uses_no_speech_fields(tmp_path: Path) -> None:
    store = _SourceStore()
    parent = _source_request(store)
    source = resolve_committed_timed_media_request(store, parent)
    policy = _adapter_policy(source)
    physical = PreparePhysicalMediaEvidenceRequest(parent, policy.canonical_hash, 1024, 100_000)
    resolved = ResolvedPreparePhysicalMediaEvidenceRequest(physical, source)
    spy = _PhysicalPortSpy(_adapter_result(source, policy))
    producer = ClaimOwnedPhysicalMediaProducer(cast(LocalMediaPreflightPort, spy), policy)
    path = tmp_path / "verified.mp4"
    path.write_bytes(b"private-source")

    produced = producer.prepare(resolved, _Lease(source.source_blob, path))

    assert len(spy.requests) == 1
    local_request = spy.requests[0]
    assert local_request.source_path == str(path)
    assert local_request.physical_root_id == resolved.physical_root_id
    assert local_request.root_input_manifest_sha256 == resolved.root_input_manifest_sha256
    assert produced.physical_root.physical_root_id == resolved.physical_root_id
    assert produced.producer_policy_sha256 == policy.canonical_hash
    assert json.loads(produced.producer_provenance_json)["tool_trace_sha256"] == (
        _adapter_result(source, policy).tool_trace.canonical_hash
    )


def test_claim_owned_adapter_rejects_policy_or_lease_mismatch_before_port(tmp_path: Path) -> None:
    store = _SourceStore()
    parent = _source_request(store)
    source = resolve_committed_timed_media_request(store, parent)
    policy = _adapter_policy(source)
    physical = PreparePhysicalMediaEvidenceRequest(parent, policy.canonical_hash, 1024, 100_000)
    resolved = ResolvedPreparePhysicalMediaEvidenceRequest(physical, source)
    spy = _PhysicalPortSpy(_adapter_result(source, policy))
    producer = ClaimOwnedPhysicalMediaProducer(cast(LocalMediaPreflightPort, spy), policy)
    path = tmp_path / "verified.mp4"
    path.write_bytes(b"private-source")

    foreign = BlobRef(source.source_blob.object_id, source.source_blob.content_hash, source.source_blob.byte_length + 1, source.source_blob.media_type)
    with pytest.raises(TimedMediaEvidenceProducerError, match="does not match"):
        producer.prepare(resolved, _Lease(foreign, path))
    assert spy.requests == []

    mismatch = replace(policy, policy_version="2")
    with pytest.raises(TimedMediaEvidenceProducerError, match="installed physical policy"):
        ClaimOwnedPhysicalMediaProducer(cast(LocalMediaPreflightPort, spy), mismatch).prepare(
            resolved, _Lease(source.source_blob, path)
        )
    assert spy.requests == []


def test_claim_owned_adapter_bounds_serialized_metadata_before_contract_parse(tmp_path: Path) -> None:
    store = _SourceStore()
    parent = _source_request(store)
    source = resolve_committed_timed_media_request(store, parent)
    policy = _adapter_policy(source)
    physical = PreparePhysicalMediaEvidenceRequest(parent, policy.canonical_hash, 1024, 1)
    resolved = ResolvedPreparePhysicalMediaEvidenceRequest(physical, source)
    spy = _PhysicalPortSpy(_adapter_result(source, policy))
    producer = ClaimOwnedPhysicalMediaProducer(cast(LocalMediaPreflightPort, spy), policy)
    path = tmp_path / "verified.mp4"
    path.write_bytes(b"private-source")

    with pytest.raises(TimedMediaEvidenceProducerError, match="metadata exceeds"):
        producer.prepare(resolved, _Lease(source.source_blob, path))
    assert len(spy.requests) == 1
