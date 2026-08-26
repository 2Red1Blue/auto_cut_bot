"""Window client dispatch/identity tests, no network/native model invocation."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace

import pytest

from auto_cut_bot.pipeline.media_preflight.funasr_window_http import FunASRHttpLocalSpeechWindowPort
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
        assert error.value.code == "TIMED_SPEECH_BUSY"


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
    with pytest.raises(LocalMediaToolError, match="bound"):
        port.produce(source, request)


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
