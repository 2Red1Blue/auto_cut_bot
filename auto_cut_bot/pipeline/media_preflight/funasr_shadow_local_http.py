"""Fixed-loopback client for measured shadow-local speech windows.

The route identifies the local-PCM calibration path.  It is deliberately not a
caller-supplied endpoint or a mode switch for the normal window transport.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .funasr_window_http import FunASRHttpLocalSpeechWindowPort
from .http_transport import FileHttpTransport
from .models import LocalMediaPolicyError

SHADOW_LOCAL_SPEECH_WINDOW_ROUTE = "/v2/shadow-calibration-speech-window"


def _endpoint(port: int) -> str:
    if type(port) is not int or not 1 <= port <= 65_535:  # noqa: E721
        raise LocalMediaPolicyError("shadow-local window port must be a valid integer")
    return f"http://127.0.0.1:{port}{SHADOW_LOCAL_SPEECH_WINDOW_ROUTE}"


def _validate_endpoint(value: str) -> str:
    if type(value) is not str:
        raise LocalMediaPolicyError("shadow-local window endpoint must be text")
    try:
        url = urlparse(value)
        port = url.port
    except ValueError as error:
        raise LocalMediaPolicyError("shadow-local window endpoint port is invalid") from error
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or url.username is not None
        or url.password is not None
        or url.path != SHADOW_LOCAL_SPEECH_WINDOW_ROUTE
        or url.params
        or url.query
        or url.fragment
    ):
        raise LocalMediaPolicyError("shadow-local window endpoint must be the exact loopback HTTP route")
    return value


class FunASRShadowLocalHttpPort(FunASRHttpLocalSpeechWindowPort):
    """Use the shared single-dispatch client against the one shadow-local route."""

    @staticmethod
    def _validate_endpoint(value: str) -> str:
        return _validate_endpoint(value)

    def __init__(
        self,
        *,
        port: int,
        shared_token: str,
        timeout_seconds: int,
        max_response_bytes: int,
        transport: FileHttpTransport | None = None,
    ) -> None:
        super().__init__(
            endpoint_url=_endpoint(port),
            shared_token=shared_token,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            transport=transport,
        )


__all__ = ["FunASRShadowLocalHttpPort", "SHADOW_LOCAL_SPEECH_WINDOW_ROUTE"]
