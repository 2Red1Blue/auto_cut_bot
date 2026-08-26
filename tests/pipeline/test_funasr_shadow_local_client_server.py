"""Actual loopback transport to local-shadow service with independent fake gold.

The native model and PCM decoder are synthetic; neither these anchors nor these
results are installed calibration. HTTP, measured-profile startup, wire replay
and independent anchor projection use production implementations.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from autocut_kernel.media.calibration import CalibrationAnchor, CalibrationProducer
from autocut_kernel.media.shadow_calibration_raw import (
    ShadowCalibrationPolicies,
    ShadowCalibrationSource,
)
from autocut_kernel.media.shadow_local_calibration import (
    SHADOW_LOCAL_ALIGNMENT_POLICY_SHA256,
    ShadowLocalCalibrationCase,
    ShadowLocalCalibrationError,
    build_shadow_local_request,
)
from autocut_kernel.media.shadow_local_calibration_projection import (
    project_shadow_local_calibration,
)
from autocut_kernel.media.types import TickRange

from auto_cut_bot.pipeline.media_preflight.funasr_shadow_local_http import FunASRShadowLocalHttpPort
from auto_cut_bot.pipeline.media_preflight.funasr_window_http import (
    FunASRHttpLocalSpeechWindowPort,
    LocalSpeechWindowBusyError,
)
from auto_cut_bot.pipeline.media_preflight.models import LocalMediaToolError
from tests.pipeline.test_funasr_shadow_local_endpoint import shadow_local_case  # noqa: F401
from tests.pipeline.test_funasr_window_endpoint import SOURCE, H


def _gold(case) -> ShadowLocalCalibrationCase:
    profile, spec = case.profile, case.request.extraction
    asr, vad = profile.producers
    return ShadowLocalCalibrationCase(
        source=ShadowCalibrationSource(
            spec.source_id, spec.source_sha256, H,
            "12345678-1234-5678-1234-567812345678", spec.source_sha256, len(SOURCE), "video/mp4",
        ),
        source_provenance_sha256=H,
        extraction=spec,
        policy=case.request.policy,
        native_profile_identity_sha256=profile.native_port_identity_sha256,
        policies=ShadowCalibrationPolicies(
            profile.timed_speech_policy_sha256, profile.word_gap_policy_sha256,
            profile.vad_merge_policy_sha256, profile.utterance_gap_milliseconds,
            profile.vad_merge_gap_milliseconds,
        ),
        producer_identities=(asr, vad),
        alignment_policy_sha256=SHADOW_LOCAL_ALIGNMENT_POLICY_SHA256,
        # Independently fixed gold, never inferred from the response.
        asr_anchors=(CalibrationAnchor(
            "independent-word", CalibrationProducer.ASR, asr.producer_id,
            spec.clock_id, spec.time_base, TickRange(1100, 1200),
        ),),
        vad_anchors=(CalibrationAnchor(
            "independent-speech", CalibrationProducer.VAD, vad.producer_id,
            spec.clock_id, spec.time_base, TickRange(1050, 1250),
        ),),
    )


def _port(case):
    return FunASRShadowLocalHttpPort(
        port=case.client.make_url("/").port, shared_token="secret",
        timeout_seconds=5, max_response_bytes=100_000,
    )


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "source.mp4"
    path.write_bytes(SOURCE)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("gold_offset", [0, 7])
async def test_real_http_preserves_raw_measurement_and_independent_anchor_error(
    tmp_path, shadow_local_case, gold_offset,  # noqa: F811
):
    case = shadow_local_case
    gold = _gold(case)
    gold = replace(gold, asr_anchors=(replace(
        gold.asr_anchors[0], expected_range=TickRange(1100 + gold_offset, 1200 + gold_offset),
    ),))
    request = build_shadow_local_request(gold, max_response_bytes=100_000)
    result = await asyncio.to_thread(_port(case).produce, _source(tmp_path), request)
    measured = project_shadow_local_calibration(result.raw_response, case=gold, request=request)

    assert measured.asr_matches[0].absolute_tick == gold_offset
    assert measured.vad_matches[0].absolute_tick == 0
    assert measured.transcript == result.evidence.transcript
    assert measured.speech_activity == result.evidence.speech_activity
    assert measured.raw_response is result.raw_response
    assert measured.response_sha256 == result.evidence.decoded.response_sha256
    assert measured.case_sha256 == request.binding_sha256
    assert request.policy.service_profile_sha256 == case.profile.canonical_hash
    assert request.policy.service_profile_sha256 != gold.native_profile_identity_sha256
    assert "asr_anchors" not in request.to_mapping() and "vad_anchors" not in request.to_mapping()
    assert len(case.calls) == 1 and not case.calls[0].exists()
    assert case.service.admitted == 0 and not case.service.lock.locked()

    foreign = replace(gold, source_provenance_sha256="sha256:" + "2" * 64)
    with pytest.raises(ShadowLocalCalibrationError):
        project_shadow_local_calibration(result.raw_response, case=foreign, request=request)


@pytest.mark.asyncio
async def test_normal_transport_cannot_consume_local_shadow_service(tmp_path, shadow_local_case):  # noqa: F811
    case = shadow_local_case
    port = FunASRHttpLocalSpeechWindowPort(
        endpoint_url=str(case.client.make_url("/v2/timed-speech-window")),
        shared_token="secret", timeout_seconds=5, max_response_bytes=100_000,
    )
    with pytest.raises(LocalMediaToolError, match="HTTP failure 409"):
        await asyncio.to_thread(port.produce, _source(tmp_path), case.request)
    assert not case.calls and case.service.admitted == 0


@pytest.mark.asyncio
async def test_local_shadow_busy_is_one_dispatch_and_not_automatic_retry(tmp_path, shadow_local_case):  # noqa: F811
    case = shadow_local_case
    case.service.admitted = case.service.queue_capacity
    case.service.resource_reader = lambda: case.ns["ResourceSnapshot"](10**12, 0, 0)
    with pytest.raises(LocalSpeechWindowBusyError) as error:
        await asyncio.to_thread(_port(case).produce, _source(tmp_path), case.request)
    error.value.proof.assert_matches(case.request)
    assert error.value.raw_response == error.value.proof.to_bytes()
    assert not case.calls and case.service.admitted == case.service.queue_capacity


@pytest.mark.asyncio
async def test_local_shadow_not_ready_is_unknown_not_retry_permission(tmp_path, shadow_local_case):  # noqa: F811
    case = shadow_local_case
    case.service.ready = False
    with pytest.raises(LocalMediaToolError) as error:
        await asyncio.to_thread(_port(case).produce, _source(tmp_path), case.request)
    assert error.value.code == "TIMED_SPEECH_RESULT_UNKNOWN"
    assert not isinstance(error.value, LocalSpeechWindowBusyError)
    assert not case.calls and case.service.admitted == 0
