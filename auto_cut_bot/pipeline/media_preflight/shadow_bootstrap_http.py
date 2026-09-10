"""One bounded HTTP dispatch for anchor-free shadow-bootstrap observations."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
from pathlib import Path
from urllib.parse import urlparse

from autocut_kernel.media.shadow_bootstrap_observation import (
    ShadowBootstrapObservationRequest,
    ShadowBootstrapObservationResult,
    decode_shadow_bootstrap_observation_response,
    project_shadow_bootstrap_observation,
)
from autocut_kernel.store.models import BlobRef, VerifiedMaterializedBlob

from .http_transport import FileHttpTransport, HttpxFileTransport
from .models import (
    LocalMediaEvidenceError,
    LocalMediaPolicyError,
    LocalMediaSourceError,
    LocalMediaToolError,
)

SHADOW_BOOTSTRAP_OBSERVATION_ROUTE = "/v1/shadow-bootstrap-timed-observation"


def validate_shadow_bootstrap_observation_endpoint(value: str) -> str:
    """Accept only the dedicated loopback route; callers cannot select another service."""
    if type(value) is not str:
        raise LocalMediaPolicyError("shadow bootstrap endpoint must be text")
    try:
        url = urlparse(value)
        port = url.port
    except ValueError as error:
        raise LocalMediaPolicyError("shadow bootstrap endpoint port is invalid") from error
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or url.username is not None
        or url.password is not None
        or url.path != SHADOW_BOOTSTRAP_OBSERVATION_ROUTE
        or url.params
        or url.query
        or url.fragment
    ):
        raise LocalMediaPolicyError(
            "shadow bootstrap endpoint must be the exact loopback HTTP route"
        )
    return value


def _manifest_bytes(request: ShadowBootstrapObservationRequest) -> bytes:
    """Match the Kernel request identity's stable JSON encoding exactly."""
    return json.dumps(
        request.to_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


class ShadowBootstrapObservationHttpPort:
    """Dispatch one source file and return only its strict, untrusted Kernel projection."""

    @staticmethod
    def _validate_endpoint(value: str) -> str:
        return validate_shadow_bootstrap_observation_endpoint(value)

    def __init__(
        self,
        *,
        endpoint_url: str,
        shared_token: str,
        timeout_seconds: int,
        max_response_bytes: int,
        transport: FileHttpTransport | None = None,
    ) -> None:
        self.endpoint_url = self._validate_endpoint(endpoint_url)
        if (
            type(shared_token) is not str
            or not shared_token
            or any(ord(character) < 33 or ord(character) > 126 for character in shared_token)
        ):
            raise LocalMediaPolicyError(
                "shadow bootstrap shared token must be nonempty ASCII header text"
            )
        if (
            type(timeout_seconds) is not int
            or timeout_seconds <= 0
            or type(max_response_bytes) is not int
            or max_response_bytes <= 0
        ):
            raise LocalMediaPolicyError(
                "shadow bootstrap operational limits must be positive integers"
            )
        self._token = shared_token
        self._timeout = timeout_seconds
        self._max_response = max_response_bytes
        self._transport = transport if transport is not None else HttpxFileTransport()

    def observe(
        self, request: ShadowBootstrapObservationRequest, source: VerifiedMaterializedBlob
    ) -> ShadowBootstrapObservationResult:
        """Send once. This boundary has neither retry nor persistence authority."""
        if type(request) is not ShadowBootstrapObservationRequest:
            raise LocalMediaPolicyError("shadow bootstrap request must be exact typed request")
        source_path = getattr(source, "path", None)
        source_reference = getattr(source, "reference", None)
        if not isinstance(source_path, Path) or type(source_reference) is not BlobRef:
            raise LocalMediaSourceError("shadow bootstrap source must be an exact verified lease")
        if source_reference.content_hash != request.source.source_sha256:
            raise LocalMediaSourceError("shadow bootstrap source lease differs from request identity")
        if request.max_response_bytes > self._max_response:
            raise LocalMediaPolicyError(
                "shadow bootstrap response bound exceeds installed transport limit"
            )
        if not source_path.is_absolute():
            raise LocalMediaSourceError("shadow bootstrap source path must be absolute")
        try:
            info = source_path.lstat()
        except OSError as error:
            raise LocalMediaSourceError("shadow bootstrap source is unavailable") from error
        effective_max_source_bytes = min(
            request.kernel_max_source_bytes, request.service_max_request_bytes
        )
        if (
            not stat.S_ISREG(info.st_mode)
            or not 0 < info.st_size <= effective_max_source_bytes
        ):
            raise LocalMediaSourceError(
                "shadow bootstrap source must be a bounded nonempty regular file"
            )
        try:
            status, raw_response = self._transport.post(
                self.endpoint_url,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Authorization": f"Bearer {self._token}",
                    "X-Shadow-Bootstrap-Timed-Observation-Manifest": base64.b64encode(
                        _manifest_bytes(request)
                    ).decode("ascii"),
                    "X-Shadow-Bootstrap-Timed-Observation-Request-SHA256": request.canonical_hash,
                },
                body_path=source_path,
                timeout_seconds=self._timeout,
                max_response_bytes=request.max_response_bytes,
            )
        except LocalMediaToolError as error:
            if error.code == "TIMED_SPEECH_RESULT_UNKNOWN":
                raise
            raise LocalMediaToolError(
                "shadow bootstrap result is unknown after incomplete transport response",
                code="TIMED_SPEECH_RESULT_UNKNOWN",
            ) from None
        if type(status) is not int or type(raw_response) is not bytes:
            raise LocalMediaToolError(
                "shadow bootstrap result is unknown after incomplete transport response",
                code="TIMED_SPEECH_RESULT_UNKNOWN",
            )
        if len(raw_response) > request.max_response_bytes:
            raise LocalMediaToolError("shadow bootstrap response exceeded byte bound")
        if status != 200:
            digest = hashlib.sha256(raw_response).hexdigest()
            raise LocalMediaToolError(f"shadow bootstrap HTTP failure {status} (sha256:{digest})")
        try:
            decoded = decode_shadow_bootstrap_observation_response(raw_response, request)
            return project_shadow_bootstrap_observation(decoded)
        except (TypeError, ValueError) as error:
            raise LocalMediaEvidenceError("shadow bootstrap observation response is invalid") from error


__all__ = [
    "SHADOW_BOOTSTRAP_OBSERVATION_ROUTE",
    "ShadowBootstrapObservationHttpPort",
    "validate_shadow_bootstrap_observation_endpoint",
]
