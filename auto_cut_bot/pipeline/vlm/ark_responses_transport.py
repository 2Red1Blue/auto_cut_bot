"""Shared single-attempt Ark Responses streaming/retrieval transport.

Media upload and text request validation stay with their adapters. This module
owns the one response parser and failure classification for both. Unknown
remote outcomes are never retried here.
"""

# pyright: reportMissingTypeStubs=false
from __future__ import annotations

import importlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import httpx
from autocut_kernel.vlm import (
    GENERATION_PROVIDER_LEASE_SECONDS,
    ProviderCompleted,
    ProviderFailed,
    ProviderFailureDisposition,
    ProviderIndeterminate,
    ProviderPending,
    ProviderReconcileQuery,
    ProviderRequestIdCallback,
    ProviderResult,
)


class ClientFactory(Protocol):
    def __call__(
        self, *, api_key: str, base_url: str, timeout: float, max_retries: int
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class ArkResponsesTransportConfig:
    api_key: str = field(repr=False)
    base_url: str
    timeout_seconds: float
    max_stream_bytes: int

    def __post_init__(self) -> None:
        if type(self.api_key) is not str or not self.api_key.strip():  # noqa: E721
            raise ValueError("Ark API key must be explicit non-empty text")
        if (
            type(self.base_url) is not str
            or self.base_url != self.base_url.strip()
            or any(  # noqa: E721
                ord(char) < 32 or ord(char) == 127 for char in self.base_url
            )
        ):
            raise ValueError("Ark base_url must be explicit HTTPS")
        split = urlsplit(self.base_url)
        if (
            split.scheme != "https"
            or not split.hostname
            or split.username
            or split.password
            or split.query
            or split.fragment
        ):
            raise ValueError("Ark base_url must be HTTPS without credentials, query or fragment")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds < GENERATION_PROVIDER_LEASE_SECONDS
        ):
            raise ValueError("Ark timeout must be finite and fit inside generation lease")
        if type(self.max_stream_bytes) is not int or self.max_stream_bytes < 1:  # noqa: E721
            raise ValueError("Ark stream limit must be an explicit positive integer")


def create_ark_client(*, api_key: str, base_url: str, timeout: float, max_retries: int) -> object:
    """Official SDK, zero SDK retries and no environment-selected HTTP proxy."""
    if type(max_retries) is not int or max_retries != 0:  # noqa: E721
        raise ValueError("Ark transport forbids hidden SDK retries")
    constructor = getattr(importlib.import_module("volcenginesdkarkruntime"), "Ark")
    http_client = httpx.Client(timeout=timeout, trust_env=False)
    try:
        return cast(
            object,
            constructor(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                max_retries=0,
                http_client=http_client,
            ),
        )
    except Exception:
        http_client.close()
        raise


def close_ark_resource(value: object) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass  # cleanup must not replace the authoritative remote result


class ArkResponsesTransport:
    def __init__(
        self, config: ArkResponsesTransportConfig, *, client_factory: ClientFactory | None = None
    ) -> None:
        if type(config) is not ArkResponsesTransportConfig:  # noqa: E721
            raise TypeError("config must be exact ArkResponsesTransportConfig")
        self._config = config
        self._factory = client_factory or create_ark_client

    def create_client(self) -> object:
        return self._factory(
            api_key=self._config.api_key,
            base_url=self._config.base_url,
            timeout=self._config.timeout_seconds,
            max_retries=0,
        )

    def dispatch(
        self,
        body: dict[str, object],
        *,
        expected_model: str,
        on_provider_request_id: ProviderRequestIdCallback,
        client: object | None = None,
    ) -> ProviderResult:
        if (
            body.get("model") != expected_model
            or body.get("stream") is not True
            or body.get("store") is not True
        ):
            return provider_failure("INVALID_PROVIDER_REQUEST")
        if not callable(on_provider_request_id):
            return provider_failure("PROVIDER_REQUEST_ID_CALLBACK_REQUIRED")
        if client is None:
            try:
                client = self.create_client()
            except Exception as error:
                return map_ark_client_error(error)
        stream: object | None = None
        try:
            stream = cast(Any, client).responses.create(**body)
            return _consume_stream(
                stream,
                expected_model=expected_model,
                max_stream_bytes=self._config.max_stream_bytes,
                on_provider_request_id=on_provider_request_id,
            )
        except Exception as error:
            return _map_response_create_error(error)
        finally:
            if stream is not None:
                close_ark_resource(stream)
            close_ark_resource(client)

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult:
        if type(query) is not ProviderReconcileQuery:  # noqa: E721
            raise TypeError("query must be exact ProviderReconcileQuery")
        if query.provider_request_id is None:
            return ProviderIndeterminate("PROVIDER_REQUEST_ID_UNKNOWN")
        client: object | None = None
        try:
            client = self.create_client()
            response = cast(Any, client).responses.retrieve(query.provider_request_id)
            status = str(getattr(response, "status", "")).lower()
            response_id, response_model = _response_id(response), _response_model(response)
            if response_id != query.provider_request_id or response_model != query.model_id:
                return provider_failure(
                    "PROVIDER_RESPONSE_IDENTITY_MISMATCH",
                    provider_request_id=query.provider_request_id,
                )
            assert response_id is not None
            if status == "completed":
                text = _authoritative_response_text(response)
                if text is None:
                    return provider_failure(
                        "PROVIDER_RESPONSE_OUTPUT_INVALID", provider_request_id=response_id
                    )
                if len(text.encode("utf-8")) > self._config.max_stream_bytes:
                    return provider_failure(
                        "PROVIDER_STREAM_LIMIT_EXCEEDED", provider_request_id=response_id
                    )
                return ProviderCompleted(text.encode("utf-8"), response_id)
            if status in {"queued", "in_progress", "processing"}:
                return ProviderPending(response_id)
            if status == "failed":
                return provider_failure(
                    "PROVIDER_RESPONSE_FAILED",
                    provider_request_id=response_id,
                    disposition=_response_failure_disposition(response),
                )
            if status == "incomplete":
                return provider_failure(
                    "PROVIDER_RESPONSE_INCOMPLETE",
                    provider_request_id=response_id,
                    disposition=ProviderFailureDisposition.REPAIRABLE,
                )
            if status == "cancelled":
                return provider_failure(
                    "PROVIDER_RESPONSE_CANCELLED", provider_request_id=response_id
                )
            return ProviderIndeterminate("PROVIDER_RESPONSE_STATUS_UNKNOWN", response_id)
        except Exception as error:
            return _map_reconcile_error(error, response_id=query.provider_request_id)
        finally:
            if client is not None:
                close_ark_resource(client)


def _consume_stream(
    stream: object,
    *,
    expected_model: str,
    max_stream_bytes: int,
    on_provider_request_id: ProviderRequestIdCallback,
) -> ProviderResult:
    request_id: str | None = None
    # Delta bytes are bounded as transient stream telemetry.  The terminal
    # authoritative body is checked independently below: Ark may repeat the
    # same text in ``output_text.done`` and ``response.completed``, so adding
    # all three representations would make dispatch stricter than reconcile.
    telemetry_bytes = 0
    try:
        for event in cast(Any, stream):
            event_type = str(getattr(event, "type", ""))
            response = getattr(event, "response", None)
            if event_type == "response.output_text.delta":
                telemetry = getattr(event, "delta", "")
                if isinstance(telemetry, str):
                    telemetry_bytes += len(telemetry.encode("utf-8"))
                    if telemetry_bytes > max_stream_bytes:
                        return provider_failure(
                            "PROVIDER_STREAM_LIMIT_EXCEEDED",
                            provider_request_id=request_id,
                            disposition=ProviderFailureDisposition.REPAIRABLE,
                            limit=max_stream_bytes,
                        )
            elif event_type == "response.created":
                created_id = _response_id(response)
                created_model = _response_model(response)
                if (
                    created_id is None
                    or created_model != expected_model
                    or (request_id is not None and request_id != created_id)
                ):
                    return provider_failure(
                        "PROVIDER_RESPONSE_IDENTITY_MISMATCH",
                        provider_request_id=request_id,
                    )
                if request_id is None:
                    request_id = created_id
                    try:
                        on_provider_request_id(created_id)
                    except Exception:
                        return ProviderIndeterminate(
                            "PROVIDER_REQUEST_ID_PERSIST_FAILED",
                            request_id,
                        )
            elif event_type == "response.completed":
                completed_id = _response_id(response)
                completed_model = _response_model(response)
                if (
                    request_id is None
                    or completed_id != request_id
                    or completed_model != expected_model
                    or str(getattr(response, "status", "")).lower() != "completed"
                ):
                    return provider_failure(
                        "PROVIDER_RESPONSE_IDENTITY_MISMATCH",
                        provider_request_id=request_id,
                    )
                final_text = _authoritative_response_text(response)
                if final_text is None:
                    return provider_failure(
                        "PROVIDER_RESPONSE_OUTPUT_INVALID",
                        provider_request_id=request_id,
                        disposition=ProviderFailureDisposition.REPAIRABLE,
                    )
                if len(final_text.encode("utf-8")) > max_stream_bytes:
                    return provider_failure(
                        "PROVIDER_STREAM_LIMIT_EXCEEDED",
                        provider_request_id=request_id,
                        disposition=ProviderFailureDisposition.REPAIRABLE,
                        limit=max_stream_bytes,
                    )
                return ProviderCompleted(final_text.encode("utf-8"), request_id)
            elif event_type in {"response.incomplete", "response.failed"}:
                expected_status = event_type.removeprefix("response.")
                if (
                    request_id is None
                    or _response_id(response) != request_id
                    or _response_model(response) != expected_model
                    or str(getattr(response, "status", "")).lower() != expected_status
                ):
                    # A valid SDK event is not proof that it belongs to this
                    # invocation. Never reserve a retry from an unrelated failure.
                    return ProviderIndeterminate("PROVIDER_TERMINAL_IDENTITY_UNVERIFIED", request_id)
                return provider_failure(
                    "PROVIDER_RESPONSE_INCOMPLETE" if expected_status == "incomplete"
                    else "PROVIDER_RESPONSE_FAILED",
                    provider_request_id=request_id,
                    disposition=(ProviderFailureDisposition.REPAIRABLE if expected_status == "incomplete"
                                 else _response_failure_disposition(response)),
                )
            elif event_type == "error":
                return ProviderIndeterminate("PROVIDER_STREAM_ERROR_EVENT", request_id)
    except Exception:
        return ProviderIndeterminate("PROVIDER_STREAM_INTERRUPTED", request_id)
    return ProviderIndeterminate("PROVIDER_STREAM_TERMINAL_EVENT_MISSING", request_id)


def _response_id(response: object) -> str | None:
    value = getattr(response, "id", None)
    return value if isinstance(value, str) and value else None


def _response_model(response: object) -> str | None:
    value = getattr(response, "model", None)
    return value if isinstance(value, str) and value else None


def _authoritative_response_text(response: object) -> str | None:
    output = getattr(response, "output", None)
    if not isinstance(output, (list, tuple)):
        return None
    output_items = cast(list[object] | tuple[object, ...], output)
    messages: list[object] = [
        item for item in output_items if getattr(item, "type", None) == "message"
    ]
    if len(messages) != 1:
        return None
    content = getattr(messages[0], "content", None)
    if not isinstance(content, (list, tuple)):
        return None
    content_items = cast(list[object] | tuple[object, ...], content)
    if len(content_items) != 1:
        return None
    item = content_items[0]
    value = getattr(item, "text", None)
    if getattr(item, "type", None) != "output_text" or not isinstance(value, str) or not value:
        return None
    return value


def provider_failure(
    code: str,
    *,
    provider_request_id: str | None = None,
    disposition: ProviderFailureDisposition = ProviderFailureDisposition.NONRETRYABLE,
    **details: object,
) -> ProviderFailed:
    return ProviderFailed(
        code,
        json.dumps(
            {
                "disposition": disposition.value,
                "retryable": disposition is ProviderFailureDisposition.RETRYABLE,
                **details,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        provider_request_id,
        disposition,
    )


def ark_error_status(error: Exception) -> int | None:
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def ark_trace_id(error: Exception) -> str | None:
    value = getattr(error, "request_id", None)
    # Ark SDK exceptions expose an HTTP trace/request id.  It is not the
    # Responses API response.id and must never be persisted as reconcile identity.
    return value if isinstance(value, str) and value else None


def map_ark_client_error(error: Exception) -> ProviderResult:
    status_code = ark_error_status(error)
    return provider_failure(
        (
            f"PROVIDER_CLIENT_HTTP_{status_code}"
            if status_code is not None
            else "PROVIDER_CLIENT_INITIALIZATION_FAILED"
        ),
        http_status=status_code,
        provider_trace_id=ark_trace_id(error),
    )


def _map_response_create_error(error: Exception) -> ProviderResult:
    status_code = ark_error_status(error)
    if status_code in {429, 500, 502, 503, 504}:
        return provider_failure(
            f"PROVIDER_HTTP_{status_code}",
            disposition=ProviderFailureDisposition.RETRYABLE,
            http_status=status_code,
            provider_trace_id=ark_trace_id(error),
        )
    if status_code in {400, 401, 403, 404, 409, 422}:
        return provider_failure(
            f"PROVIDER_HTTP_{status_code}",
            http_status=status_code,
            provider_trace_id=ark_trace_id(error),
        )
    reason = (
        f"PROVIDER_CREATE_HTTP_{status_code}"
        if status_code is not None
        else "PROVIDER_TRANSPORT_UNKNOWN"
    )
    return ProviderIndeterminate(reason)


def _map_reconcile_error(error: Exception, *, response_id: str) -> ProviderResult:
    status_code = ark_error_status(error)
    if status_code in {400, 401, 403, 404, 409, 422}:
        return provider_failure(
            f"PROVIDER_RECONCILE_HTTP_{status_code}",
            provider_request_id=response_id,
            http_status=status_code,
            provider_trace_id=ark_trace_id(error),
        )
    reason = (
        f"PROVIDER_RECONCILE_HTTP_{status_code}"
        if status_code is not None
        else "PROVIDER_RECONCILE_TRANSPORT_UNKNOWN"
    )
    return ProviderIndeterminate(reason, response_id)


def _response_failure_disposition(response: object) -> ProviderFailureDisposition:
    """Retry only terminal response failures carrying explicit transient evidence."""

    error = getattr(response, "error", None)
    status = getattr(error, "status_code", None)
    if (
        isinstance(status, int)
        and not isinstance(status, bool)
        and status
        in {
            429,
            500,
            502,
            503,
            504,
        }
    ):
        return ProviderFailureDisposition.RETRYABLE
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.lower() in {
        "rate_limit_exceeded",
        "server_error",
        "service_unavailable",
        "timeout",
    }:
        return ProviderFailureDisposition.RETRYABLE
    return ProviderFailureDisposition.NONRETRYABLE
