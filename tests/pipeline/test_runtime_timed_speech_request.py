"""CUDA v2 timed-speech request tests; CPU v1 remains a separate grammar."""

from __future__ import annotations

from dataclasses import replace

import pytest
from autocut_kernel.media import TimeBase

from auto_cut_bot.pipeline.media_preflight.models import LocalMediaPolicyError
from auto_cut_bot.pipeline.media_preflight.runtime_policy import (
    project_pc_cuda_runtime_timed_speech_policy,
)
from auto_cut_bot.pipeline.media_preflight.runtime_speech import (
    RUNTIME_TIMED_SPEECH_REQUEST_SCHEMA,
    RUNTIME_TIMED_SPEECH_RESPONSE_SCHEMA,
    RUNTIME_TIMED_SPEECH_ROUTE,
    RuntimeTimedSpeechEvidenceRequest,
)
from tests.authority.test_runtime_timed_speech import _capability, _runtime_measurement, _selector
from tests.pipeline.runtime_profile_fixture import media_preflight_policy


def _request(tmp_path):
    measurement = _runtime_measurement()
    projection = _selector(measurement).select(_capability(measurement), measurement)
    static = media_preflight_policy(
        timed_speech_policy_sha256=projection.timed_speech_policy_sha256,
        utterance_gap_milliseconds=300,
        vad_merge_gap_milliseconds=200,
    )
    source = (tmp_path / "episode.mp4").resolve()
    source.write_bytes(b"runtime-cuda-source")
    return RuntimeTimedSpeechEvidenceRequest(
        source,
        "episode-0001",
        "sha256:" + "a" * 64,
        1000,
        800,
        800,
        projection.source_clock_id,
        projection.source_time_base,
        0,
        1000,
        0,
        1000,
        project_pc_cuda_runtime_timed_speech_policy(static, projection),
    )


def test_runtime_cuda_request_uses_dedicated_route_and_closed_authority(tmp_path) -> None:
    request = _request(tmp_path)

    assert request.endpoint_url.endswith(RUNTIME_TIMED_SPEECH_ROUTE)
    assert request.response_schema_version == RUNTIME_TIMED_SPEECH_RESPONSE_SCHEMA
    assert request.to_mapping()["schema_version"] == RUNTIME_TIMED_SPEECH_REQUEST_SCHEMA
    assert "profile" not in request.to_mapping()
    assert "expected_producers" not in request.to_mapping()
    assert request.to_mapping()["runtime_authority"] == request.runtime_policy.to_mapping()
    assert request.runtime_policy.to_mapping()["operation"]["endpoint_url"].endswith(
        RUNTIME_TIMED_SPEECH_ROUTE
    )
    assert tuple(item.timing_error_bound_tick for item in request.expected_producers) == tuple(
        item.timing_error_bound_tick for item in request.runtime_policy.producers
    )


def test_runtime_cuda_request_rejects_cross_clock_and_authority_drift(tmp_path) -> None:
    request = _request(tmp_path)

    with pytest.raises(LocalMediaPolicyError, match="source clock"):
        replace(request, clock_id="foreign-clock")
    with pytest.raises(LocalMediaPolicyError, match="authority differs"):
        request.validate_response_authority({})
    request.validate_response_authority(request.runtime_policy.to_mapping())


def test_runtime_cuda_request_does_not_offer_a_cpu_or_mps_device_switch(tmp_path) -> None:
    request = _request(tmp_path)

    assert request.device == "cuda"
    assert request.time_base == TimeBase(1, 1_000)
    assert request.runtime_policy.runtime_capability_id == "pc_cuda"
