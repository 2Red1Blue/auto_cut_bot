"""Window client dispatch/identity tests, no network/native model invocation."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace

import httpx
import pytest
from autocut_kernel.media.local_speech_window_busy import LocalSpeechWindowBusyProof
from autocut_kernel.pipeline.local_speech_window_port import LocalSpeechWindowInvalidResponseError

from auto_cut_bot.pipeline.media_preflight.funasr_shadow_local_http import FunASRShadowLocalHttpPort
from auto_cut_bot.pipeline.media_preflight.funasr_window_http import (
    FunASRHttpLocalSpeechWindowPort,
    LocalSpeechWindowBusyError,
)
from auto_cut_bot.pipeline.media_preflight.http_transport import HttpxFileTransport
from auto_cut_bot.pipeline.media_preflight.models import (
    LocalMediaEvidenceError,
    LocalMediaPolicyError,
    LocalMediaSourceError,
    LocalMediaToolError,
)
from tests.media.test_local_speech_window_codec import request_and_report, valid_native_response

ENDPOINT = "http://127.0.0.1:18765/v2/timed-speech-window"


class Transport:
    def __init__(self, status, raw):
        self.status, self.raw, self.calls = status, raw, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.status, self.raw


def _case(tmp_path, **transport_args):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic-source")
    request, report = request_and_report("sha256:" + hashlib.sha256(source.read_bytes()).hexdigest())
    raw = valid_native_response(request, report)
    transport = Transport(transport_args.get("status", 200), transport_args.get("raw", raw))
    port = FunASRHttpLocalSpeechWindowPort(endpoint_url=ENDPOINT, shared_token="secret",
        timeout_seconds=5, max_response_bytes=request.max_response_bytes, transport=transport)
    return source, request, raw, transport, port


def test_one_dispatch_retains_exact_raw_and_local_evidence(tmp_path):
    source, request, raw, transport, port = _case(tmp_path)
    result = port.produce(source, request)
    assert result.raw_response is raw and len(transport.calls) == 1
    url, call = transport.calls[0]
    assert url == ENDPOINT and call["body_path"] == source
    headers = call["headers"]
    assert headers["Authorization"] == "Bearer secret"
    assert headers["X-Local-Speech-Window-SHA256"] == request.canonical_hash
    assert json.loads(base64.b64decode(headers["X-Local-Speech-Window-Manifest"])) == request.to_mapping()
    transcript = result.evidence.transcript
    assert transcript.context.origin_tick == 48_000 and transcript.context.duration_tick == 48_000
    assert transcript.words[0].in_tick == 52_800 and transcript.words[0].out_tick == 67_200
    assert source.exists()


@pytest.mark.parametrize("status", [400, 401, 409, 413, 422, 503])
def test_received_failure_never_retries(tmp_path, status):
    source, request, _, transport, port = _case(tmp_path, status=status, raw=b"safe failure")
    with pytest.raises(LocalMediaToolError) as error:
        port.produce(source, request)
    assert len(transport.calls) == 1
    if status == 503:
        assert error.value.code == "TIMED_SPEECH_RESULT_UNKNOWN"
        assert not isinstance(error.value, LocalSpeechWindowBusyError)


def test_unknown_transport_is_preserved_without_retry(tmp_path):
    unknown = LocalMediaToolError("unknown invocation", code="TIMED_SPEECH_RESULT_UNKNOWN")
    source, request, _, transport, port = _case(tmp_path, raw=unknown)
    with pytest.raises(LocalMediaToolError) as error:
        port.produce(source, request)
    assert error.value is unknown and len(transport.calls) == 1


def test_source_and_operational_bounds_stop_before_dispatch(tmp_path):
    source, request, _, transport, port = _case(tmp_path)
    for invalid in (tmp_path / "missing", source.relative_to(tmp_path)):
        with pytest.raises(LocalMediaSourceError):
            port.produce(invalid, request)
    link = tmp_path / "link.mp4"
    link.symlink_to(source)
    with pytest.raises(LocalMediaSourceError):
        port.produce(link, request)
    with pytest.raises(LocalMediaPolicyError):
        port.produce(source, replace(request, max_response_bytes=100_001))
    assert transport.calls == []


def test_foreign_raw_and_oversized_response_rejected(tmp_path):
    source, request, raw, transport, port = _case(tmp_path)
    payload = json.loads(raw)
    payload["extraction_report"]["sample_count"] = 1
    transport.raw = json.dumps(payload).encode()
    with pytest.raises(LocalMediaEvidenceError):
        port.produce(source, request)
    transport.raw = b"x" * (request.max_response_bytes + 1)
    with pytest.raises(LocalMediaToolError, match="bound") as oversized:
        port.produce(source, request)
    assert not isinstance(oversized.value, LocalSpeechWindowInvalidResponseError)


@pytest.mark.parametrize("endpoint", [
    "http://localhost:18765/v2/timed-speech-window",
    "http://127.0.0.1:18765/v1/timed-speech-evidence",
    ENDPOINT + "?x=1", ENDPOINT + "#fragment", ENDPOINT.replace("http:", "https:"),
    ENDPOINT.replace("127.0.0.1", "user@127.0.0.1"),
])
def test_endpoint_is_exact_loopback_only(endpoint):
    with pytest.raises(LocalMediaPolicyError):
        FunASRHttpLocalSpeechWindowPort(endpoint_url=endpoint, shared_token="secret",
            timeout_seconds=5, max_response_bytes=100_000)


def test_only_exact_request_bound_busy_retains_original_proof_bytes_without_retry(tmp_path):
    source, request, _, transport, port = _case(tmp_path, status=503)
    proof = LocalSpeechWindowBusyProof(
        request.canonical_hash, request.binding_sha256, request.policy.service_profile_sha256,
    )
    transport.raw = proof.to_bytes()
    with pytest.raises(LocalSpeechWindowBusyError) as error:
        port.produce(source, request)
    assert error.value.code == "TIMED_SPEECH_BUSY"
    assert error.value.proof == proof and error.value.raw_response is transport.raw
    with pytest.raises(AttributeError):
        error.value.raw_response = b"foreign"
    with pytest.raises(AttributeError):
        error.value.proof = replace(proof, binding_sha256="sha256:" + "f" * 64)
    assert "secret" not in str(error.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("mutation", [
    "request_sha256", "binding_sha256", "service_profile_sha256", "duplicate", "extra",
    "missing", "state", "reason", "schema", "whitespace", "oversize", "type", "secret_body",
])
def test_unproven_503_is_unknown_and_body_never_leaks(tmp_path, mutation):
    source, request, _, transport, port = _case(tmp_path, status=503)
    proof = LocalSpeechWindowBusyProof(
        request.canonical_hash, request.binding_sha256, request.policy.service_profile_sha256,
    )
    body = proof.to_mapping()
    if mutation.endswith("sha256"):
        body[mutation] = "sha256:" + "f" * 64
    elif mutation == "extra":
        body["retry"] = True
    elif mutation == "missing":
        del body["reason"]
    elif mutation == "state":
        body["invocation_state"] = "started"
    elif mutation == "reason":
        body["reason"] = "inference_failed"
    elif mutation == "schema":
        body["schema_version"] = "foreign"
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    if mutation == "duplicate":
        raw = raw[:-1] + b',"reason":"admission_busy"}'
    elif mutation == "whitespace":
        raw += b"\n"
    elif mutation == "oversize":
        raw += b" " * request.max_response_bytes
    elif mutation == "type":
        raw = bytearray(raw)
    elif mutation == "secret_body":
        raw = b"secret internal path /private/native"
    transport.raw = raw
    with pytest.raises(LocalMediaToolError) as error:
        port.produce(source, request)
    assert error.value.code == "TIMED_SPEECH_RESULT_UNKNOWN"
    assert not isinstance(error.value, LocalSpeechWindowBusyError)
    assert not hasattr(error.value, "raw_response")
    assert "secret" not in str(error.value) and "/private" not in str(error.value)
    assert len(transport.calls) == 1


def test_busy_error_cannot_carry_mismatched_or_mutable_raw_bytes():
    request, _ = request_and_report()
    proof = LocalSpeechWindowBusyProof(
        request.canonical_hash, request.binding_sha256, request.policy.service_profile_sha256,
    )
    for raw in (proof.to_bytes() + b" ", bytearray(proof.to_bytes())):
        with pytest.raises(LocalMediaPolicyError):
            LocalSpeechWindowBusyError(proof, raw)


@pytest.mark.parametrize("code", ["LOCAL_MEDIA_TOOL_FAILED", "TIMED_SPEECH_BUSY"])
def test_transport_error_without_raw_proof_cannot_be_busy(tmp_path, code):
    error = LocalMediaToolError("secret transport failure", code=code)
    source, request, _, transport, port = _case(tmp_path, raw=error)
    with pytest.raises(LocalMediaToolError) as received:
        port.produce(source, request)
    assert received.value.code == "TIMED_SPEECH_RESULT_UNKNOWN"
    assert "secret" not in str(received.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("variant", ["empty", "malformed", "duplicate", "foreign_request", "projection"])
@pytest.mark.parametrize("route", ["normal", "shadow_local"])
def test_complete_invalid_http_200_preserves_exact_raw_and_locked_request(tmp_path, variant, route):
    source, request, raw, transport, port = _case(tmp_path)
    if route == "shadow_local":
        port = FunASRShadowLocalHttpPort(port=18765, shared_token="secret", timeout_seconds=5,
                                        max_response_bytes=request.max_response_bytes, transport=transport)
    payload = json.loads(raw)
    if variant == "empty":
        received = b""
    elif variant == "malformed":
        received = b' {"private": "secret /private/native", invalid }\n'
    elif variant == "duplicate":
        received = b'{"request_sha256":' + json.dumps(request.canonical_hash).encode() + b',' + raw[1:]
    else:
        if variant == "foreign_request":
            payload["request_sha256"] = "sha256:" + "a" * 64
        else:
            payload["asr_native_output"][0]["timestamp"] = [[-100, -1]]
        received = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    transport.raw = received
    with pytest.raises(LocalSpeechWindowInvalidResponseError) as caught:
        port.produce(source, request)
    assert isinstance(caught.value, LocalMediaEvidenceError)
    assert caught.value.raw_response is received
    assert caught.value.request is request
    assert caught.value.request_sha256 == request.canonical_hash
    assert "secret" not in str(caught.value) and "/private" not in repr(caught.value)
    assert len(transport.calls) == 1


def test_valid_noncanonical_json_response_still_succeeds_without_reencoding(tmp_path):
    source, request, raw, transport, port = _case(tmp_path)
    received = json.dumps(json.loads(raw), ensure_ascii=False, indent=2).encode() + b"\n"
    transport.raw = received
    result = port.produce(source, request)
    assert result.raw_response is received and received != raw
    assert len(transport.calls) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_timeout_and_connection_remain_unknown_without_invalid_response_carrier(tmp_path, monkeypatch, failure):
    source, request, _, _, _ = _case(tmp_path)
    cause = failure("synthetic transport failure")
    bodies = []

    def unavailable_stream(_method, _url, **kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        bodies.append(kwargs["content"])
        raise cause

    monkeypatch.setattr(httpx, "stream", unavailable_stream)
    port = FunASRHttpLocalSpeechWindowPort(endpoint_url=ENDPOINT, shared_token="secret",
        timeout_seconds=5, max_response_bytes=request.max_response_bytes, transport=HttpxFileTransport())
    with pytest.raises(LocalMediaToolError) as caught:
        port.produce(source, request)
    assert caught.value.code == "TIMED_SPEECH_RESULT_UNKNOWN"
    assert caught.value.__cause__ is cause
    assert not isinstance(caught.value, LocalSpeechWindowInvalidResponseError)
    assert not hasattr(caught.value, "raw_response")
    assert len(bodies) == 1 and bodies[0].closed
