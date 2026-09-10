from __future__ import annotations

import json
from dataclasses import replace

import pytest
from autocut_kernel.media.shadow_bootstrap_observation import (
    SHADOW_BOOTSTRAP_OBSERVATION_RAW_RESPONSE_SCHEMA,
    SHADOW_BOOTSTRAP_OBSERVATION_REQUEST_SCHEMA,
    SHADOW_BOOTSTRAP_OBSERVATION_SCHEMA,
    ShadowBootstrapObservationError,
    ShadowBootstrapObservationRequest,
    ShadowBootstrapObservationResult,
    ShadowBootstrapObservationSource,
    decode_shadow_bootstrap_observation_response,
    encode_shadow_bootstrap_observation_response,
    project_shadow_bootstrap_observation,
)
from autocut_kernel.media.types import TickRange, TimeBase


def _sha(number: int) -> str:
    return f"sha256:{number:064x}"


def _request() -> ShadowBootstrapObservationRequest:
    return ShadowBootstrapObservationRequest(
        ShadowBootstrapObservationSource("source-1", _sha(1), "audio-30k", TimeBase(1, 30_000), TickRange(-3_000, 6_000)),
        TickRange(0, 5_000),
        1_000_000,
        2_000_000,
        16_384,
    )


def _raw(request: ShadowBootstrapObservationRequest) -> bytes:
    return encode_shadow_bootstrap_observation_response(
        request,
        [{"text": "你好", "words": ["你", "好"], "timestamp": [[10, 40], [50, 100]]}],
        [{"value": [[5, 110], [120, 150]]}],
    )


def test_anchor_free_raw_projection_is_bound_and_permanently_untrusted() -> None:
    request = _request()
    decoded = decode_shadow_bootstrap_observation_response(_raw(request), request)
    result = project_shadow_bootstrap_observation(decoded)

    assert request.to_mapping()["schema_version"] == SHADOW_BOOTSTRAP_OBSERVATION_REQUEST_SCHEMA
    assert result.to_mapping()["schema_version"] == SHADOW_BOOTSTRAP_OBSERVATION_SCHEMA
    assert decoded.asr_observations[0].observed_range == TickRange(300, 1_200)  # 30k source ticks / second
    assert decoded.vad_observations[0].observed_range == TickRange(150, 3_300)
    assert result.trust_status == "untrusted"
    assert result.authority_eligible is False
    assert result.independent_anchor_count == 0
    assert result.to_mapping()["raw_response_sha256"] == decoded.raw_response_sha256


def test_encoder_replays_exact_native_syntax_and_decode_rejects_identity_drift() -> None:
    request = _request()
    raw = _raw(request)
    mapping = json.loads(raw)
    assert mapping["schema_version"] == SHADOW_BOOTSTRAP_OBSERVATION_RAW_RESPONSE_SCHEMA
    mapping["source"]["source_id"] = "foreign"
    foreign = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ShadowBootstrapObservationError, match="source identity drift"):
        decode_shadow_bootstrap_observation_response(foreign, request)
    with pytest.raises(ShadowBootstrapObservationError, match="request identity drift"):
        decode_shadow_bootstrap_observation_response(raw, replace(request, max_response_bytes=8_000))


@pytest.mark.parametrize(
    "asr,vad",
    [
        ([{"text": "a", "words": ["a"], "timestamp": [[30, 20]]}], [{"value": []}]),
        ([{"text": "", "words": ["a"], "timestamp": [[1, 2]]}], [{"value": []}]),
        ([{"text": "a", "words": ["a"], "timestamp": [[0, 1]]}], [{"value": [[20, 30], [10, 20]]}]),
    ],
)
def test_encoder_rejects_invalid_native_observations(asr: object, vad: object) -> None:
    with pytest.raises(ShadowBootstrapObservationError):
        encode_shadow_bootstrap_observation_response(_request(), asr, vad)


def test_empty_native_observations_remain_explicitly_untrusted() -> None:
    request = _request()
    raw = encode_shadow_bootstrap_observation_response(
        request, [{"text": "", "words": [], "timestamp": []}], [{"value": []}]
    )
    result = project_shadow_bootstrap_observation(
        decode_shadow_bootstrap_observation_response(raw, request)
    )
    assert result.decoded.asr_observations == ()
    assert result.decoded.vad_observations == ()
    with pytest.raises(ShadowBootstrapObservationError, match="never authority eligible"):
        ShadowBootstrapObservationResult(result.decoded, authority_eligible=True)
    with pytest.raises(ShadowBootstrapObservationError, match="exactly zero independent anchors"):
        ShadowBootstrapObservationResult(result.decoded, independent_anchor_count=1)
