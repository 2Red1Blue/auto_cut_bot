"""Fixed shadow-local HTTP wrapper tests with no network or native model."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
from typing import cast

import pytest
from autocut_kernel.media.local_speech_window_busy import LocalSpeechWindowBusyProof

from auto_cut_bot.pipeline.media_preflight.funasr_shadow_local_http import (
    SHADOW_LOCAL_SPEECH_WINDOW_ROUTE,
    FunASRShadowLocalHttpPort,
)
from auto_cut_bot.pipeline.media_preflight.funasr_window_http import (
    FunASRHttpLocalSpeechWindowPort,
    LocalSpeechWindowBusyError,
)
from auto_cut_bot.pipeline.media_preflight.models import LocalMediaPolicyError, LocalMediaToolError
from tests.media.test_local_speech_window_codec import request_and_report, valid_native_response

PORT = 18_766
ENDPOINT = f"http://127.0.0.1:{PORT}{SHADOW_LOCAL_SPEECH_WINDOW_ROUTE}"
NORMAL_ENDPOINT = f"http://127.0.0.1:{PORT}/v2/timed-speech-window"


class Transport:
    def __init__(self, status: int, raw: bytes | Exception) -> None:
        self.status = status
        self.raw = raw
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, url: str, **kwargs: object) -> tuple[int, bytes]:
        self.calls.append((url, kwargs))
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.status, self.raw


def _case(tmp_path, *, status: int = 200, raw: bytes | Exception | None = None):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic-shadow-local-source")
    source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    request, report = request_and_report(source_sha256)
    response = valid_native_response(request, report) if raw is None else raw
    transport = Transport(status, response)
    port = FunASRShadowLocalHttpPort(
        port=PORT,
        shared_token="shadow-secret",
        timeout_seconds=5,
        max_response_bytes=request.max_response_bytes,
        transport=transport,
    )
    return source, request, transport, port


def test_fixed_shadow_local_route_dispatches_exact_normal_window_wire(tmp_path) -> None:
    source, request, transport, port = _case(tmp_path)

    result = port.produce(source, request)

    assert result.raw_response is transport.raw
    assert port.endpoint_url == ENDPOINT
    assert len(transport.calls) == 1
    url, call = transport.calls[0]
    assert url == ENDPOINT
    headers = call["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer shadow-secret"
    assert headers["X-Local-Speech-Window-SHA256"] == request.canonical_hash
    assert json.loads(base64.b64decode(headers["X-Local-Speech-Window-Manifest"])) == request.to_mapping()
    assert result.evidence.decoded.request == request


@pytest.mark.parametrize("value", [True, False, 0, -1, 65_536, 1.5, "18766"])
def test_shadow_local_port_is_the_only_endpoint_input_and_is_strict(value: object) -> None:
    with pytest.raises(LocalMediaPolicyError, match="shadow-local window port"):
        FunASRShadowLocalHttpPort(
            port=cast(int, value),
            shared_token="shadow-secret",
            timeout_seconds=5,
            max_response_bytes=100_000,
        )


def test_shadow_local_constructor_does_not_accept_caller_selected_endpoint() -> None:
    assert "endpoint_url" not in inspect.signature(FunASRShadowLocalHttpPort).parameters


@pytest.mark.parametrize(
    "endpoint",
    [
        NORMAL_ENDPOINT,
        ENDPOINT.replace("127.0.0.1", "localhost"),
        ENDPOINT.replace("127.0.0.1", "127.0.0.2"),
        ENDPOINT.replace("http://", "http://user@"),
        ENDPOINT + "?mode=normal",
        ENDPOINT + "#fragment",
    ],
)
def test_normal_and_shadow_routes_are_strictly_separate(endpoint: str) -> None:
    with pytest.raises(LocalMediaPolicyError):
        FunASRShadowLocalHttpPort._validate_endpoint(endpoint)

    with pytest.raises(LocalMediaPolicyError):
        FunASRHttpLocalSpeechWindowPort(
            endpoint_url=ENDPOINT,
            shared_token="shadow-secret",
            timeout_seconds=5,
            max_response_bytes=100_000,
        )


def test_shadow_local_busy_proof_and_unknown_503_reuse_normal_single_dispatch_behavior(tmp_path) -> None:
    source, request, transport, port = _case(tmp_path, status=503, raw=b"unproven")
    with pytest.raises(LocalMediaToolError) as unknown:
        port.produce(source, request)
    assert unknown.value.code == "TIMED_SPEECH_RESULT_UNKNOWN"
    assert len(transport.calls) == 1

    proof = LocalSpeechWindowBusyProof(
        request.canonical_hash,
        request.binding_sha256,
        request.policy.service_profile_sha256,
    )
    transport.raw = proof.to_bytes()
    with pytest.raises(LocalSpeechWindowBusyError) as busy:
        port.produce(source, request)
    assert busy.value.proof == proof
    assert busy.value.raw_response is transport.raw
    assert len(transport.calls) == 2


def test_shadow_local_unknown_transport_is_not_retried(tmp_path) -> None:
    unknown = LocalMediaToolError("incomplete response", code="TIMED_SPEECH_RESULT_UNKNOWN")
    source, request, transport, port = _case(tmp_path, raw=unknown)

    with pytest.raises(LocalMediaToolError) as caught:
        port.produce(source, request)

    assert caught.value is unknown
    assert len(transport.calls) == 1
