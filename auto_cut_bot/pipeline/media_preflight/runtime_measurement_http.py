"""Read one authenticated, self-measured FunASR runtime identity.

This is an admission read, not timed-speech dispatch.  The endpoint is fixed
from the already validated timed-speech endpoint so a pipeline configuration
cannot route the capability check to another host or service.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol, cast
from urllib.parse import urlparse

import httpx
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.media.runtime_measurement_identity import (
    RuntimeMeasurementIdentity,
    RuntimeMeasurementIdentityError,
    decode_runtime_measurement_identity,
)

from .models import LocalMediaPolicyError, LocalMediaToolError, validate_timed_speech_endpoint

RUNTIME_MEASUREMENT_IDENTITY_ROUTE = "/v1/runtime-measurement-identity"
_RESPONSE_SCHEMA = "funasr-runtime-measurement-identity-response-v1"
_MAX_RESPONSE_BYTES = 64 * 1024


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")


class RuntimeMeasurementIdentityPort(Protocol):
    """Only the fresh runtime measurement needed by the Preflight admission."""

    def read_identity(self) -> RuntimeMeasurementIdentity: ...


class FunASRRuntimeMeasurementIdentityHttpPort:
    """Strict loopback HTTP reader with no proxy, redirect, or endpoint seam."""

    def __init__(
        self, *, timed_speech_endpoint_url: str, shared_token: str, timeout_seconds: int = 10
    ) -> None:
        endpoint = validate_timed_speech_endpoint(timed_speech_endpoint_url)
        parsed = urlparse(endpoint)
        assert parsed.port is not None  # established by validate_timed_speech_endpoint
        self._endpoint = f"http://127.0.0.1:{parsed.port}{RUNTIME_MEASUREMENT_IDENTITY_ROUTE}"
        if (
            type(shared_token) is not str
            or not shared_token
            or any(ord(character) < 33 or ord(character) > 126 for character in shared_token)
        ):
            raise LocalMediaPolicyError("runtime identity shared token must be nonempty ASCII header text")
        if type(timeout_seconds) is not int or timeout_seconds <= 0:  # noqa: E721
            raise LocalMediaPolicyError("runtime identity timeout must be a positive integer")
        self._token = shared_token
        self._timeout_seconds = timeout_seconds

    @property
    def endpoint_url(self) -> str:
        return self._endpoint

    def read_identity(self) -> RuntimeMeasurementIdentity:
        try:
            with httpx.stream(
                "GET",
                self._endpoint,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=float(self._timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            ) as response:
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=16 * 1024):
                    if len(raw) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise LocalMediaToolError("runtime identity response exceeded byte bound")
                    raw.extend(chunk)
        except LocalMediaToolError:
            raise
        except (httpx.HTTPError, OSError) as error:
            raise LocalMediaToolError(
                "runtime identity is unavailable", code="RUNTIME_IDENTITY_UNAVAILABLE"
            ) from error
        if response.status_code != 200:
            if response.status_code == 409:
                raise LocalMediaToolError(
                    "runtime identity requires recalibration",
                    code="RUNTIME_IDENTITY_RECOMPUTE_NEEDED",
                )
            if response.status_code == 401:
                raise LocalMediaToolError(
                    "runtime identity authentication is invalid",
                    code="RUNTIME_IDENTITY_INVALID",
                )
            if response.status_code == 503:
                raise LocalMediaToolError(
                    f"runtime identity HTTP {response.status_code}",
                    code="RUNTIME_IDENTITY_UNAVAILABLE",
                )
            raise LocalMediaToolError(
                f"runtime identity HTTP {response.status_code}",
                code="RUNTIME_IDENTITY_INVALID",
            )
        try:
            payload = json.loads(
                bytes(raw).decode("utf-8"),
                object_pairs_hook=_closed_object,
                parse_constant=_reject_json_constant,
            )
            if type(payload) is not dict or frozenset(cast(Mapping[str, object], payload)) != frozenset(
                {"schema_version", "runtime_measurement_identity"}
            ):
                raise ValueError("response schema is not closed")
            mapping = cast(Mapping[str, object], payload)
            if mapping["schema_version"] != _RESPONSE_SCHEMA:
                raise ValueError("response schema version is unsupported")
            return decode_runtime_measurement_identity(
                canonical_json_bytes(mapping["runtime_measurement_identity"])
            )
        except (TypeError, UnicodeError, ValueError, RuntimeMeasurementIdentityError) as error:
            raise LocalMediaToolError(
                "runtime identity response is invalid", code="RUNTIME_IDENTITY_INVALID"
            ) from error


__all__ = [
    "FunASRRuntimeMeasurementIdentityHttpPort",
    "RUNTIME_MEASUREMENT_IDENTITY_ROUTE",
    "RuntimeMeasurementIdentityPort",
]
