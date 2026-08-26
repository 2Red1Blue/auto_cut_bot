"""Bounded file transport shared by local speech evidence and calibration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import httpx

from .models import LocalMediaToolError


class FileHttpTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body_path: Path,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> tuple[int, bytes]: ...


class HttpxFileTransport:
    """Send once; endpoint validation and retry decisions belong to the caller."""

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body_path: Path,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> tuple[int, bytes]:
        if (
            type(timeout_seconds) is not int
            or timeout_seconds <= 0
            or type(max_response_bytes) is not int
            or max_response_bytes <= 0
        ):
            raise LocalMediaToolError("transport limits must be positive integers")
        try:
            with (
                body_path.open("rb") as body,
                httpx.stream(
                    "POST",
                    url,
                    headers=dict(headers),
                    content=body,
                    timeout=float(timeout_seconds),
                    follow_redirects=False,
                    trust_env=False,
                ) as response,
            ):
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=min(max_response_bytes + 1, 65_536)):
                    if len(raw) + len(chunk) > max_response_bytes:
                        raise LocalMediaToolError("response exceeded byte bound")
                    raw.extend(chunk)
            return response.status_code, bytes(raw)
        except httpx.TimeoutException as error:
            raise LocalMediaToolError(
                "timed speech result is unknown after timeout",
                code="TIMED_SPEECH_RESULT_UNKNOWN",
            ) from error
        except LocalMediaToolError:
            raise
        except (httpx.HTTPError, OSError) as error:
            raise LocalMediaToolError(
                "timed speech result is unknown after transport failure",
                code="TIMED_SPEECH_RESULT_UNKNOWN",
            ) from error
