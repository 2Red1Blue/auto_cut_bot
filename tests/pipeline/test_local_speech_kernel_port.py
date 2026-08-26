"""Existing HTTP adapter satisfies the Kernel port; no network or native work."""

from dataclasses import replace

import pytest
from autocut_kernel.media.local_speech_window_busy import LocalSpeechWindowBusyProof
from autocut_kernel.pipeline.local_speech_window_port import (
    LocalSpeechWindowInvalidResponseError,
    LocalSpeechWindowPreDispatchBusyError,
    LocalSpeechWindowProducerPort,
    ReceivedLocalSpeechWindow,
)

from auto_cut_bot.pipeline.media_preflight.funasr_window_http import (
    LocalSpeechWindowBusyError,
    LocalSpeechWindowInvalidEvidenceError,
)
from auto_cut_bot.pipeline.media_preflight.models import (
    LocalMediaEvidenceError,
    LocalMediaPolicyError,
    LocalMediaToolError,
)
from tests.media.test_local_speech_window_codec import request_and_report
from tests.pipeline.test_funasr_window_http import _case


def test_http_result_uses_kernel_owned_value_and_dispatches_once(tmp_path):
    source, request, raw, transport, client = _case(tmp_path)
    port: LocalSpeechWindowProducerPort = client
    result = port.produce(source, request)
    assert type(result) is ReceivedLocalSpeechWindow
    assert result.raw_response is raw and len(transport.calls) == 1
    assert source.exists()  # lease lifetime belongs to the Command, not transport


def test_http_busy_retains_old_error_api_and_is_catchable_without_app_import(tmp_path):
    source, request, _, transport, client = _case(tmp_path, status=503)
    proof = LocalSpeechWindowBusyProof(
        request.canonical_hash, request.binding_sha256, request.policy.service_profile_sha256,
    )
    transport.raw = proof.to_bytes()
    with pytest.raises(LocalSpeechWindowPreDispatchBusyError) as caught:
        client.produce(source, request)
    assert isinstance(caught.value, LocalMediaToolError)
    assert type(caught.value) is LocalSpeechWindowBusyError
    assert caught.value.code == "TIMED_SPEECH_BUSY"
    assert caught.value.proof == proof and caught.value.raw_response is transport.raw
    assert len(transport.calls) == 1 and source.exists()


def test_kernel_busy_value_is_immutable_content_not_retry_authority():
    request, _ = request_and_report()
    proof = LocalSpeechWindowBusyProof(
        request.canonical_hash, request.binding_sha256, request.policy.service_profile_sha256,
    )
    error = LocalSpeechWindowPreDispatchBusyError(proof, proof.to_bytes())
    assert not hasattr(error, "retry_authorized")
    with pytest.raises(AttributeError):
        error.raw_response = b"changed"
    with pytest.raises(AttributeError):
        error.proof = replace(proof, binding_sha256="sha256:" + "a" * 64)
    for raw in (proof.to_bytes() + b"\n", bytearray(proof.to_bytes()), None):
        with pytest.raises(ValueError):
            LocalSpeechWindowPreDispatchBusyError(proof, raw)
        with pytest.raises(LocalMediaPolicyError):
            LocalSpeechWindowBusyError(proof, raw)


def test_invalid_received_types_fail_without_conferring_measurement_authority(tmp_path):
    source, request, _raw, _transport, client = _case(tmp_path)
    result = client.produce(source, request)
    with pytest.raises(ValueError):
        ReceivedLocalSpeechWindow(None, result.raw_response)
    with pytest.raises(ValueError):
        ReceivedLocalSpeechWindow(result.evidence, bytearray(result.raw_response))


@pytest.mark.parametrize("raw", [b"", b"not JSON", b' { "schema_version": "foreign" }\n'])
def test_received_invalid_is_catchable_in_kernel_and_preserves_old_error_api(tmp_path, raw):
    source, request, _, transport, client = _case(tmp_path, raw=raw)
    with pytest.raises(LocalSpeechWindowInvalidResponseError) as caught:
        client.produce(source, request)
    error = caught.value
    assert isinstance(error, LocalMediaEvidenceError)
    assert type(error) is LocalSpeechWindowInvalidEvidenceError
    assert error.code == LocalMediaEvidenceError.code
    assert error.request is request and error.request_sha256 == request.canonical_hash
    assert error.raw_response is raw
    assert len(transport.calls) == 1 and source.exists()
    for name, replacement in (("request", replace(request, max_response_bytes=1)),
                              ("request_sha256", "sha256:" + "f" * 64),
                              ("raw_response", b"replacement")):
        with pytest.raises(AttributeError):
            setattr(error, name, replacement)
    for forbidden in ("denied", "accepted", "retry_authorized", "receipt_id"):
        assert not hasattr(error, forbidden)


@pytest.mark.parametrize("variant", ["request_mapping", "bytearray", "text", "none", "overflow"])
def test_invalid_response_carrier_requires_exact_request_and_bounded_bytes(variant):
    request, _ = request_and_report()
    raw = b"invalid"
    if variant == "request_mapping":
        request = request.to_mapping()
    elif variant == "overflow":
        raw = b"x" * (request.max_response_bytes + 1)
    else:
        raw = {"bytearray": bytearray(raw), "text": "invalid", "none": None}[variant]
    with pytest.raises(ValueError):
        LocalSpeechWindowInvalidResponseError(request, raw)
    with pytest.raises(LocalMediaPolicyError):
        LocalSpeechWindowInvalidEvidenceError(request, raw)


def test_invalid_response_carrier_is_content_not_a_decoder_verdict(tmp_path):
    _, request, valid_raw, _, _ = _case(tmp_path)
    # A typed carrier is not proof that its bytes are invalid. The durable
    # consumer must compare the locked request and rerun the shared decoder.
    error = LocalSpeechWindowInvalidResponseError(request, valid_raw)
    assert error.request is request and error.raw_response is valid_raw
    assert not hasattr(error, "validation_status")
