"""Single-dispatch native window transport; no planning, retry or commitment."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
from pathlib import Path
from urllib.parse import urlparse

from autocut_kernel.media.local_speech_window import LocalSpeechWindowRequest
from autocut_kernel.media.local_speech_window_busy import (
    LocalSpeechWindowBusyProof,
    decode_local_speech_window_busy_proof,
)
from autocut_kernel.media.local_speech_window_codec import decode_local_speech_window_response
from autocut_kernel.media.local_speech_window_projection import project_local_speech_window
from autocut_kernel.pipeline.local_speech_window_port import (
    LocalSpeechWindowPreDispatchBusyError,
    ReceivedLocalSpeechWindow,
)

from .http_transport import FileHttpTransport, HttpxFileTransport
from .models import (
    LocalMediaEvidenceError,
    LocalMediaPolicyError,
    LocalMediaSourceError,
    LocalMediaToolError,
)


class LocalSpeechWindowBusyError(LocalMediaToolError, LocalSpeechWindowPreDispatchBusyError):
    """Verified wire correlation only; no retry or durable receipt is created."""

    code = "TIMED_SPEECH_BUSY"

    def __init__(self, proof: LocalSpeechWindowBusyProof, raw_response: bytes) -> None:
        try:
            LocalSpeechWindowPreDispatchBusyError.__init__(self, proof, raw_response)
        except ValueError as error:
            raise LocalMediaPolicyError(str(error)) from error


def validate_local_speech_window_endpoint(value: str) -> str:
    if type(value) is not str:
        raise LocalMediaPolicyError("window endpoint must be text")
    try:
        url = urlparse(value)
        port = url.port
    except ValueError as error:
        raise LocalMediaPolicyError("window endpoint port is invalid") from error
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or port is None
            or not 1 <= port <= 65_535 or url.username is not None or url.password is not None
            or url.path != "/v2/timed-speech-window" or url.params or url.query or url.fragment):
        raise LocalMediaPolicyError("window endpoint must be the exact loopback HTTP route")
    return value


class FunASRHttpLocalSpeechWindowPort:
    @staticmethod
    def _validate_endpoint(value: str) -> str:
        return validate_local_speech_window_endpoint(value)

    def __init__(
        self, *, endpoint_url: str, shared_token: str, timeout_seconds: int,
        max_response_bytes: int, transport: FileHttpTransport | None = None,
    ) -> None:
        self.endpoint_url = self._validate_endpoint(endpoint_url)
        if (type(shared_token) is not str or not shared_token
                or any(ord(ch) < 33 or ord(ch) > 126 for ch in shared_token)):
            raise LocalMediaPolicyError("window shared token must be nonempty ASCII header text")
        if (type(timeout_seconds) is not int or timeout_seconds <= 0
                or type(max_response_bytes) is not int or max_response_bytes <= 0):
            raise LocalMediaPolicyError("window operational limits must be positive integers")
        self._token = shared_token
        self._timeout = timeout_seconds
        self._max_response = max_response_bytes
        self._transport = transport if transport is not None else HttpxFileTransport()

    def produce(self, source_path: Path, request: LocalSpeechWindowRequest) -> ReceivedLocalSpeechWindow:
        """Caller owns the verified private source lease and the durable claim."""
        if type(request) is not LocalSpeechWindowRequest:
            raise LocalMediaPolicyError("window request must be an exact typed request")
        if request.max_response_bytes > self._max_response:
            raise LocalMediaPolicyError("window response bound exceeds installed transport limit")
        if not source_path.is_absolute():
            raise LocalMediaSourceError("window source path must be absolute")
        try:
            info = source_path.lstat()
        except OSError as error:
            raise LocalMediaSourceError("window source lease is unavailable") from error
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= request.extraction.max_source_bytes:
            raise LocalMediaSourceError("window source must be a bounded nonempty regular file")
        manifest = json.dumps(request.to_mapping(), sort_keys=True, separators=(",", ":")).encode()
        try:
            status, raw = self._transport.post(
                self.endpoint_url,
                headers={
                    "Content-Type": "application/octet-stream", "Authorization": f"Bearer {self._token}",
                    "X-Local-Speech-Window-Manifest": base64.b64encode(manifest).decode("ascii"),
                    "X-Local-Speech-Window-SHA256": request.canonical_hash,
                },
                body_path=source_path, timeout_seconds=self._timeout,
                max_response_bytes=request.max_response_bytes,
            )
        except LocalMediaToolError as error:
            if error.code == "TIMED_SPEECH_RESULT_UNKNOWN":
                raise
            # A bounded read can abort before exposing HTTP status/body. Without
            # a complete proof it cannot establish that dispatch never happened.
            raise LocalMediaToolError(
                "window result is unknown after incomplete transport response",
                code="TIMED_SPEECH_RESULT_UNKNOWN",
            ) from None
        if type(status) is int and status == 503:
            try:
                proof = decode_local_speech_window_busy_proof(raw, request)
            except (TypeError, ValueError):
                raise LocalMediaToolError(
                    "window 503 has no verified pre-dispatch evidence",
                    code="TIMED_SPEECH_RESULT_UNKNOWN",
                ) from None
            raise LocalSpeechWindowBusyError(proof, raw)
        if type(raw) is not bytes or len(raw) > request.max_response_bytes:
            raise LocalMediaToolError("window response exceeded byte bound")
        if type(status) is not int or status != 200:
            digest = hashlib.sha256(raw).hexdigest()
            raise LocalMediaToolError(f"window HTTP failure {status} (sha256:{digest})")
        try:
            decoded = decode_local_speech_window_response(raw, request)
            evidence = project_local_speech_window(decoded)
        except (TypeError, ValueError) as error:
            raise LocalMediaEvidenceError("local speech window evidence is invalid") from error
        return ReceivedLocalSpeechWindow(evidence, raw)
