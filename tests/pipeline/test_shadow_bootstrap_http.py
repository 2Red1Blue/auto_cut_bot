"""Shadow-bootstrap HTTP dispatch tests without network or native inference."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest
from autocut_kernel.media.shadow_bootstrap_observation import (
    ShadowBootstrapObservationRequest,
    ShadowBootstrapObservationSource,
    encode_shadow_bootstrap_observation_response,
)
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.store.models import BlobRef

from auto_cut_bot.pipeline.media_preflight.models import (
    LocalMediaEvidenceError,
    LocalMediaPolicyError,
    LocalMediaSourceError,
    LocalMediaToolError,
)
from auto_cut_bot.pipeline.media_preflight.shadow_bootstrap_http import (
    SHADOW_BOOTSTRAP_OBSERVATION_ROUTE,
    ShadowBootstrapObservationHttpPort,
)

PORT = 18_767
ENDPOINT = f"http://127.0.0.1:{PORT}{SHADOW_BOOTSTRAP_OBSERVATION_ROUTE}"


class Transport:
    def __init__(self, status: object, raw: object) -> None:
        self.status = status
        self.raw = raw
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, url: str, **kwargs: object) -> tuple[int, bytes]:
        self.calls.append((url, kwargs))
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.status, self.raw  # type: ignore[return-value]


@dataclass
class Lease:
    reference: BlobRef
    path: Path

    def close(self) -> None:
        pass


def _request(source: Path) -> ShadowBootstrapObservationRequest:
    return ShadowBootstrapObservationRequest(
        ShadowBootstrapObservationSource(
            "source-1",
            "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
            "audio-30k",
            TimeBase(1, 30_000),
            TickRange(-3_000, 6_000),
        ),
        TickRange(0, 5_000),
        1_000_000,
        2_000_000,
        16_384,
    )


def _raw(request: ShadowBootstrapObservationRequest) -> bytes:
    return encode_shadow_bootstrap_observation_response(
        request,
        [{"text": "hello", "words": ["hello"], "timestamp": [[10, 40]]}],
        [{"value": [[5, 50]]}],
    )


def _case(
    tmp_path: Path, *, status: object = 200, raw: object | None = None
) -> tuple[Lease, ShadowBootstrapObservationRequest, Transport, ShadowBootstrapObservationHttpPort]:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic-bootstrap-source")
    request = _request(source)
    lease = Lease(
        BlobRef(uuid4(), request.source.source_sha256, source.stat().st_size, "video/mp4"), source
    )
    transport = Transport(status, _raw(request) if raw is None else raw)
    port = ShadowBootstrapObservationHttpPort(
        endpoint_url=ENDPOINT,
        shared_token="bootstrap-secret",
        timeout_seconds=5,
        max_response_bytes=request.max_response_bytes,
        transport=transport,
    )
    return lease, request, transport, port


def test_one_dispatch_sends_exact_canonical_manifest_and_projects_untrusted_result(tmp_path: Path) -> None:
    source, request, transport, port = _case(tmp_path)

    result = port.observe(request, source)

    assert result.decoded.request is request
    assert result.trust_status == "untrusted"
    assert result.authority_eligible is False
    assert result.independent_anchor_count == 0
    assert len(transport.calls) == 1
    url, call = transport.calls[0]
    assert url == ENDPOINT
    assert call["body_path"] == source.path
    assert call["timeout_seconds"] == 5
    assert call["max_response_bytes"] == request.max_response_bytes
    assert call["headers"] == {
        "Content-Type": "application/octet-stream",
        "Authorization": "Bearer bootstrap-secret",
        "X-Shadow-Bootstrap-Timed-Observation-Manifest": base64.b64encode(
            json.dumps(request.to_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).decode("ascii"),
        "X-Shadow-Bootstrap-Timed-Observation-Request-SHA256": request.canonical_hash,
    }


@pytest.mark.parametrize("status", [400, 401, 409, 413, 422, 503, 307])
def test_non_200_response_is_not_projected_or_retried(tmp_path: Path, status: int) -> None:
    source, request, transport, port = _case(tmp_path, status=status, raw=b"complete failure")

    with pytest.raises(LocalMediaToolError) as error:
        port.observe(request, source)

    assert error.value.code == "LOCAL_MEDIA_TOOL_FAILED"
    assert "complete failure" not in str(error.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "raw",
    [
        LocalMediaToolError("unknown outcome", code="TIMED_SPEECH_RESULT_UNKNOWN"),
        LocalMediaToolError("response stream failed"),
        bytearray(b"not immutable bytes"),
    ],
)
def test_uncertain_transport_cannot_be_represented_as_observation(tmp_path: Path, raw: object) -> None:
    source, request, transport, port = _case(tmp_path, raw=raw)

    with pytest.raises(LocalMediaToolError) as error:
        port.observe(request, source)

    assert error.value.code == "TIMED_SPEECH_RESULT_UNKNOWN"
    assert len(transport.calls) == 1


def test_source_and_response_bounds_stop_before_dispatch(tmp_path: Path) -> None:
    source, request, transport, port = _case(tmp_path)
    for invalid_path in (tmp_path / "missing.mp4", source.path.relative_to(tmp_path)):
        with pytest.raises(LocalMediaSourceError):
            port.observe(request, Lease(source.reference, invalid_path))
    link = tmp_path / "link.mp4"
    link.symlink_to(source.path)
    with pytest.raises(LocalMediaSourceError):
        port.observe(request, Lease(source.reference, link))
    with pytest.raises(LocalMediaPolicyError):
        port.observe(replace(request, max_response_bytes=16_385), source)
    assert transport.calls == []


def test_http_200_uses_kernel_strict_decode_and_project_only(tmp_path: Path) -> None:
    source, request, transport, port = _case(tmp_path)
    payload = json.loads(transport.raw)
    payload["authority_eligible"] = True
    received = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    transport.raw = received

    with pytest.raises(LocalMediaEvidenceError) as error:
        port.observe(request, source)

    assert "authority" not in str(error.value).lower()
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:18767/v1/shadow-bootstrap-timed-observation",
        "http://localhost:18767/v1/shadow-bootstrap-timed-observation",
        "http://127.0.0.1:18767/v1/timed-speech-evidence",
        ENDPOINT + "?x=1",
        ENDPOINT + "#fragment",
        "http://user@127.0.0.1:18767/v1/shadow-bootstrap-timed-observation",
    ],
)
def test_endpoint_is_exact_loopback_fixed_route(endpoint: str) -> None:
    with pytest.raises(LocalMediaPolicyError):
        ShadowBootstrapObservationHttpPort(
            endpoint_url=endpoint,
            shared_token="bootstrap-secret",
            timeout_seconds=5,
            max_response_bytes=16_384,
        )
