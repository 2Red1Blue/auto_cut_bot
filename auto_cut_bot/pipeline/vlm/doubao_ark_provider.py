"""Streaming Doubao Ark VLM adapter with durable provider-file reuse.

The adapter submits at most one Files API upload and one Responses API request
for a Kernel attempt.  It never turns a truncated stream into success and it
never retries an outcome whose remote state is unknown.
"""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from autocut_kernel.vlm import (
    GENERATION_PROVIDER_LEASE_SECONDS,
    ProviderCompleted,
    ProviderDispatchRequest,
    ProviderFailed,
    ProviderFailureDisposition,
    ProviderIndeterminate,
    ProviderPending,
    ProviderReconcileQuery,
    ProviderRequestIdCallback,
    ProviderResult,
)

from .ark_file_cache import ArkFileCachePort, ArkFileCacheRecord

DOUBAO_ARK_PROVIDER_ID = "doubao-ark-responses-stream"
DOUBAO_ARK_ADAPTER_STRATEGY_VERSION = "doubao-ark-files-responses-stream-v1"
_READY_FILE_STATUSES = frozenset({"active", "processed"})
_ERROR_FILE_STATUSES = frozenset({"error", "failed", "expired", "deleted"})
_EXPECTED_PAYLOAD_FIELDS = frozenset(
    {
        "model_id",
        "parser_strategy_version",
        "prompt",
        "prompt_version",
        "provider_id",
        "proxy_blob",
        "request_parameters",
        "retry_policy",
        "retry_policy_sha256",
        "response_schema",
        "window_manifest_set_sha256",
        "window_manifest_sha256",
    }
)
_EXPECTED_PARAMETER_FIELDS = frozenset(
    {"adapter_strategy_version", "max_output_tokens", "temperature", "video_fps"}
)


@dataclass(frozen=True, slots=True)
class DoubaoArkVlmProviderConfig:
    api_key: str
    tenant_id: str
    project_id: str
    base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    timeout_seconds: float = 300.0
    upload_timeout_seconds: float = 300.0
    upload_poll_interval_seconds: float = 2.0
    provider_file_ttl_seconds: int = 5 * 24 * 60 * 60
    file_cache_lease_seconds: int = 15 * 60
    unknown_upload_quarantine_seconds: int = 5 * 24 * 60 * 60
    max_video_bytes: int = 512 * 1024 * 1024
    max_stream_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if type(self.api_key) is not str or not self.api_key.strip():  # noqa: E721
            raise ValueError("Ark API key must be non-empty")
        for value, name in ((self.tenant_id, "tenant_id"), (self.project_id, "project_id")):
            if type(value) is not str or not value.strip() or len(value) > 256:  # noqa: E721
                raise ValueError(f"Ark {name} must be non-empty non-secret identity text")
        if type(self.base_url) is not str or not self.base_url.startswith("https://"):  # noqa: E721
            raise ValueError("Ark base_url must use HTTPS")
        split = urlsplit(self.base_url)
        if not split.hostname or split.username or split.password or split.query or split.fragment:
            raise ValueError("Ark base_url must have a host and no credentials, query, or fragment")
        for name, value in (
            ("timeout_seconds", self.timeout_seconds),
            ("upload_timeout_seconds", self.upload_timeout_seconds),
            ("upload_poll_interval_seconds", self.upload_poll_interval_seconds),
        ):
            if isinstance(value, bool) or type(value) not in (int, float) or value <= 0:
                raise ValueError(f"Ark {name} must be positive")
        if type(self.provider_file_ttl_seconds) is not int or self.provider_file_ttl_seconds < 60:  # noqa: E721
            raise ValueError("Ark provider_file_ttl_seconds must be at least 60")
        if (
            type(self.file_cache_lease_seconds) is not int  # noqa: E721
            or self.file_cache_lease_seconds
            < self.upload_timeout_seconds + (2 * self.timeout_seconds)
        ):
            raise ValueError("Ark file_cache_lease_seconds must cover Files API recovery")
        if (
            self.upload_timeout_seconds + self.timeout_seconds
            >= GENERATION_PROVIDER_LEASE_SECONDS
        ):
            raise ValueError(
                "Ark upload and response timeouts must fit inside the generation lease"
            )
        if (
            type(self.unknown_upload_quarantine_seconds) is not int  # noqa: E721
            or self.unknown_upload_quarantine_seconds < self.provider_file_ttl_seconds
        ):
            raise ValueError(
                "Ark unknown_upload_quarantine_seconds must cover provider_file_ttl_seconds"
            )
        if type(self.max_video_bytes) is not int or self.max_video_bytes < 1:  # noqa: E721
            raise ValueError("Ark max_video_bytes must be a positive integer")
        if type(self.max_stream_bytes) is not int or self.max_stream_bytes < 1:  # noqa: E721
            raise ValueError("Ark max_stream_bytes must be a positive integer")

    @property
    def provider_scope_fingerprint(self) -> str:
        encoded = json.dumps(
            {
                "origin": _ark_origin(self.base_url),
                "project": self.project_id,
                "tenant": self.tenant_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ClientFactory(Protocol):
    def __call__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float,
        max_retries: int,
    ) -> object: ...


class DoubaoArkVlmProvider:
    """Official Ark SDK adapter using Files API plus Responses SSE streaming."""

    provider_id = DOUBAO_ARK_PROVIDER_ID

    def __init__(
        self,
        config: DoubaoArkVlmProviderConfig,
        *,
        file_cache: ArkFileCachePort,
        client_factory: ClientFactory | None = None,
    ) -> None:
        if type(config) is not DoubaoArkVlmProviderConfig:  # noqa: E721
            raise TypeError("config must be a DoubaoArkVlmProviderConfig")
        self._config = config
        self._file_cache = file_cache
        self._client_factory = client_factory or _ark_client

    def dispatch(self, request: ProviderDispatchRequest) -> ProviderResult:
        if type(request) is not ProviderDispatchRequest:  # noqa: E721
            raise TypeError("request must be an exact ProviderDispatchRequest")
        if request.provider_id != self.provider_id:
            return _failure("PROVIDER_ID_MISMATCH")
        if request.on_provider_request_id is None:
            return _failure("PROVIDER_REQUEST_ID_CALLBACK_REQUIRED")
        if len(request.proxy_content) > self._config.max_video_bytes:
            return _failure(
                "PROVIDER_MEDIA_LIMIT_EXCEEDED",
                byte_length=len(request.proxy_content),
                limit=self._config.max_video_bytes,
            )
        try:
            payload = _request_payload(request.request_payload)
            parameters = _request_parameters(payload["request_parameters"])
            if payload["model_id"] != request.model_id:
                raise ValueError("request model_id does not match dispatch identity")
            if payload["proxy_blob"] != request.proxy_blob_ref.to_mapping():
                raise ValueError("request proxy_blob does not match dispatch identity")
        except ValueError:
            return _failure("INVALID_PROVIDER_REQUEST")
        try:
            client = cast(
                Any,
                self._client_factory(
                    api_key=self._config.api_key,
                    base_url=self._config.base_url,
                    timeout=self._config.timeout_seconds,
                    max_retries=0,
                ),
            )
        except Exception as error:
            return _map_client_error(error)
        file_result = self._get_file_id(
            client=client,
            request=request,
            video_fps=cast(float, parameters["video_fps"]),
        )
        if not isinstance(file_result, str):
            return file_result
        try:
            stream = client.responses.create(
                model=request.model_id,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_video", "file_id": file_result},
                            {
                                "type": "input_text",
                                "text": (
                                    "只依据随附视频和下述清单约束判断；禁止臆造，禁止输出物理剪辑点。"
                                    "只输出严格 JSON。\n" + cast(str, payload["prompt"])
                                ),
                            },
                        ],
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "vlm_observation_set",
                        "strict": True,
                        "schema": payload["response_schema"],
                    }
                },
                max_output_tokens=parameters["max_output_tokens"],
                temperature=parameters["temperature"],
                stream=True,
                store=True,
            )
            return _consume_stream(
                stream,
                expected_model=request.model_id,
                max_stream_bytes=self._config.max_stream_bytes,
                on_provider_request_id=request.on_provider_request_id,
            )
        except Exception as error:
            return _map_response_create_error(error)

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult:
        if type(query) is not ProviderReconcileQuery:  # noqa: E721
            raise TypeError("query must be an exact ProviderReconcileQuery")
        if query.provider_id != self.provider_id:
            return _failure("PROVIDER_ID_MISMATCH")
        if query.provider_request_id is None:
            return ProviderIndeterminate("PROVIDER_REQUEST_ID_UNKNOWN")
        try:
            client = cast(
                Any,
                self._client_factory(
                    api_key=self._config.api_key,
                    base_url=self._config.base_url,
                    timeout=self._config.timeout_seconds,
                    max_retries=0,
                ),
            )
            response = client.responses.retrieve(query.provider_request_id)
            status = str(getattr(response, "status", "")).lower()
            response_id = _response_id(response)
            response_model = _response_model(response)
            if response_id != query.provider_request_id or response_model != query.model_id:
                return _failure(
                    "PROVIDER_RESPONSE_IDENTITY_MISMATCH",
                    provider_request_id=query.provider_request_id,
                )
            assert response_id is not None
            if status == "completed":
                text = _authoritative_response_text(response)
                if text is None:
                    return _failure(
                        "PROVIDER_RESPONSE_OUTPUT_INVALID",
                        provider_request_id=response_id,
                    )
                if len(text.encode("utf-8")) > self._config.max_stream_bytes:
                    return _failure(
                        "PROVIDER_STREAM_LIMIT_EXCEEDED",
                        provider_request_id=response_id,
                    )
                return ProviderCompleted(text.encode("utf-8"), response_id)
            if status in {"queued", "in_progress", "processing"}:
                return ProviderPending(response_id)
            if status == "failed":
                return _failure(
                    "PROVIDER_RESPONSE_FAILED",
                    provider_request_id=response_id,
                    disposition=_response_failure_disposition(response),
                )
            if status == "incomplete":
                return _failure(
                    "PROVIDER_RESPONSE_INCOMPLETE",
                    provider_request_id=response_id,
                    disposition=ProviderFailureDisposition.REPAIRABLE,
                )
            if status == "cancelled":
                return _failure(
                    "PROVIDER_RESPONSE_CANCELLED",
                    provider_request_id=response_id,
                )
            return ProviderIndeterminate("PROVIDER_RESPONSE_STATUS_UNKNOWN", response_id)
        except Exception as error:
            return _map_reconcile_error(error, response_id=query.provider_request_id)

    def _get_file_id(
        self,
        *,
        client: Any,
        request: ProviderDispatchRequest,
        video_fps: float,
    ) -> str | ProviderResult:
        policy_hash = _preprocess_policy_hash(video_fps)
        record, lease_acquired = self._file_cache.claim(
            provider_id=self.provider_id,
            provider_scope_fingerprint=self._config.provider_scope_fingerprint,
            content_hash=request.proxy_blob_ref.content_hash,
            byte_length=request.proxy_blob_ref.byte_length,
            media_type=request.proxy_blob_ref.media_type,
            preprocess_policy_hash=policy_hash,
            lease_seconds=self._config.file_cache_lease_seconds,
            unknown_outcome_quarantine_seconds=(
                self._config.unknown_upload_quarantine_seconds
            ),
        )
        if record.state == "available":
            if record.expires_at is None or record.expires_at <= datetime.now(timezone.utc):
                self._file_cache.mark_expired(
                    record.media_object_id,
                    expected_version=record.version,
                    provider_status="local_ttl_expired",
                )
                return _failure("PROVIDER_MEDIA_EXPIRED")
            try:
                info = client.files.retrieve(cast(str, record.provider_file_id))
            except Exception as error:
                mapped = _map_file_error(error)
                if (
                    isinstance(mapped, ProviderFailed)
                    and mapped.failure_code == "PROVIDER_HTTP_404"
                ):
                    self._file_cache.mark_expired(
                        record.media_object_id,
                        expected_version=record.version,
                        provider_status="provider_not_found",
                    )
                    return _failure("PROVIDER_MEDIA_NOT_AVAILABLE", provider_status="not_found")
                return mapped
            status = str(getattr(info, "status", "")).lower()
            if status in _READY_FILE_STATUSES:
                return cast(str, record.provider_file_id)
            self._file_cache.mark_expired(
                record.media_object_id,
                expected_version=record.version,
                provider_status=status or "provider_status_unknown",
            )
            return _failure("PROVIDER_MEDIA_NOT_AVAILABLE", provider_status=status)
        if record.state == "processing":
            if not lease_acquired:
                return _failure(
                    "PROVIDER_MEDIA_UPLOAD_IN_PROGRESS",
                    disposition=ProviderFailureDisposition.REPAIRABLE,
                )
            if record.provider_file_id is None:
                return _failure("PROVIDER_MEDIA_CACHE_INVALID")
            return self._finish_processing_file(
                client=client,
                record=record,
                file_id=record.provider_file_id,
            )
        if record.state == "indeterminate":
            return _failure(
                "PROVIDER_MEDIA_UPLOAD_OUTCOME_UNKNOWN",
                disposition=ProviderFailureDisposition.REPAIRABLE,
            )
        if not lease_acquired:
            if record.state == "reserved":
                return _failure(
                    "PROVIDER_MEDIA_UPLOAD_IN_PROGRESS",
                    disposition=ProviderFailureDisposition.REPAIRABLE,
                )
            return _failure(
                "PROVIDER_MEDIA_TERMINAL",
                state=record.state,
                failure_code=record.failure_code,
            )

        try:
            uploaded = client.files.create(
                file=("window.mp4", request.proxy_content),
                purpose="user_data",
                preprocess_configs={"video": {"fps": video_fps}},
            )
            file_id = _required_text(getattr(uploaded, "id", None), "Ark file id")
            processing = self._file_cache.record_processing(
                record.media_object_id,
                expected_version=record.version,
                expected_lease_token=record.lease_token,
                provider_file_id=file_id,
                provider_status=str(getattr(uploaded, "status", "processing")),
            )
        except Exception as error:
            mapped = _map_file_error(error)
            if (
                isinstance(mapped, ProviderFailed)
                and mapped.failure_code != "PROVIDER_FILE_OUTCOME_UNKNOWN"
            ):
                self._file_cache.record_failed(
                    record.media_object_id,
                    expected_version=record.version,
                    expected_lease_token=record.lease_token,
                    provider_status="upload_failed",
                    failure_code=mapped.failure_code,
                )
            else:
                self._file_cache.record_indeterminate(
                    record.media_object_id,
                    expected_version=record.version,
                    expected_lease_token=record.lease_token,
                    provider_status="upload_outcome_unknown",
                    audit_expires_at=datetime.now(timezone.utc)
                    + timedelta(
                        seconds=self._config.unknown_upload_quarantine_seconds
                    ),
                )
            return mapped
        return self._finish_processing_file(
            client=client,
            record=processing,
            file_id=file_id,
        )

    def _finish_processing_file(
        self,
        *,
        client: Any,
        record: ArkFileCacheRecord,
        file_id: str,
    ) -> str | ProviderResult:
        if type(record) is not ArkFileCacheRecord:  # noqa: E721
            raise TypeError("record must be an ArkFileCacheRecord")
        try:
            info = client.files.retrieve(file_id)
            status = str(getattr(info, "status", "")).lower()
            if status not in _READY_FILE_STATUSES and status not in _ERROR_FILE_STATUSES:
                client.files.wait_for_processing(
                    file_id,
                    poll_interval=self._config.upload_poll_interval_seconds,
                    max_wait_seconds=self._config.upload_timeout_seconds,
                )
                info = client.files.retrieve(file_id)
                status = str(getattr(info, "status", "")).lower()
            if status not in _READY_FILE_STATUSES:
                if status in _ERROR_FILE_STATUSES:
                    self._file_cache.record_failed(
                        record.media_object_id,
                        expected_version=record.version,
                        expected_lease_token=record.lease_token,
                        provider_status=status,
                        failure_code="PROVIDER_MEDIA_PROCESSING_FAILED",
                    )
                    return _failure("PROVIDER_MEDIA_PROCESSING_FAILED", provider_status=status)
                self._file_cache.release_processing(
                    record.media_object_id,
                    expected_version=record.version,
                    expected_lease_token=record.lease_token,
                    provider_status=status or "provider_status_unknown",
                )
                return _failure(
                    "PROVIDER_MEDIA_STATUS_UNKNOWN",
                    disposition=ProviderFailureDisposition.REPAIRABLE,
                )
            self._file_cache.record_available(
                record.media_object_id,
                expected_version=record.version,
                expected_lease_token=record.lease_token,
                provider_status=status,
                expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=self._config.provider_file_ttl_seconds),
            )
            return file_id
        except Exception as error:
            mapped = _map_file_error(error)
            if (
                isinstance(mapped, ProviderFailed)
                and mapped.failure_code != "PROVIDER_FILE_OUTCOME_UNKNOWN"
            ):
                self._file_cache.record_failed(
                    record.media_object_id,
                    expected_version=record.version,
                    expected_lease_token=record.lease_token,
                    provider_status="processing_failed",
                    failure_code=mapped.failure_code,
                )
                return mapped
            self._file_cache.release_processing(
                record.media_object_id,
                expected_version=record.version,
                expected_lease_token=record.lease_token,
                provider_status="processing_outcome_unknown",
            )
            return _failure(
                "PROVIDER_MEDIA_PROCESSING_UNKNOWN",
                disposition=ProviderFailureDisposition.RETRYABLE,
            )


def _ark_client(*, api_key: str, base_url: str, timeout: float, max_retries: int) -> object:
    ark_constructor = getattr(importlib.import_module("volcenginesdkarkruntime"), "Ark")
    return cast(
        object,
        ark_constructor(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        ),
    )


def _request_payload(raw: bytes) -> dict[str, object]:
    try:
        value = cast(object, json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("request payload must be strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("request payload must be an object")
    payload = cast(dict[str, object], value)
    if frozenset(payload) != _EXPECTED_PAYLOAD_FIELDS:
        raise ValueError("request payload does not match the closed Ark adapter contract")
    for field in ("model_id", "prompt", "prompt_version", "provider_id"):
        _required_text(payload[field], field)
    if payload["provider_id"] != DOUBAO_ARK_PROVIDER_ID:
        raise ValueError("request payload provider_id mismatch")
    if not isinstance(payload["response_schema"], dict):
        raise ValueError("response_schema must be an object")
    if not isinstance(payload["retry_policy"], dict):
        raise ValueError("retry_policy must be an object")
    retry_policy_bytes = json.dumps(
        payload["retry_policy"],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    expected_retry_hash = "sha256:" + hashlib.sha256(retry_policy_bytes).hexdigest()
    if payload["retry_policy_sha256"] != expected_retry_hash:
        raise ValueError("retry_policy_sha256 does not bind retry_policy")
    return payload


def _request_parameters(value: object) -> dict[str, int | float]:
    if not isinstance(value, dict):
        raise ValueError("Ark request_parameters must be an object")
    parameters = cast(dict[str, object], value)
    if frozenset(parameters) != _EXPECTED_PARAMETER_FIELDS:
        raise ValueError("Ark request_parameters are not closed")
    if parameters["adapter_strategy_version"] != DOUBAO_ARK_ADAPTER_STRATEGY_VERSION:
        raise ValueError("Ark adapter strategy version is not registered")
    fps = parameters["video_fps"]
    tokens = parameters["max_output_tokens"]
    temperature = parameters["temperature"]
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not 0.1 <= fps <= 10:
        raise ValueError("video_fps must be between 0.1 and 10")
    if type(tokens) is not int or not 1 <= tokens <= 32_768:  # noqa: E721
        raise ValueError("max_output_tokens must be between 1 and 32768")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise ValueError("temperature must be numeric")
    if not 0 <= temperature <= 2:
        raise ValueError("temperature must be between 0 and 2")
    return {
        "video_fps": float(fps),
        "max_output_tokens": tokens,
        "temperature": float(temperature),
    }


def _preprocess_policy_hash(video_fps: float) -> str:
    encoded = json.dumps(
        {
            "adapter_strategy_version": DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
            "purpose": "user_data",
            "video_fps": video_fps,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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
                        return _failure(
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
                    return _failure(
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
                    return _failure(
                        "PROVIDER_RESPONSE_IDENTITY_MISMATCH",
                        provider_request_id=request_id,
                    )
                final_text = _authoritative_response_text(response)
                if final_text is None:
                    return _failure(
                        "PROVIDER_RESPONSE_OUTPUT_INVALID",
                        provider_request_id=request_id,
                        disposition=ProviderFailureDisposition.REPAIRABLE,
                    )
                if len(final_text.encode("utf-8")) > max_stream_bytes:
                    return _failure(
                        "PROVIDER_STREAM_LIMIT_EXCEEDED",
                        provider_request_id=request_id,
                        disposition=ProviderFailureDisposition.REPAIRABLE,
                        limit=max_stream_bytes,
                    )
                return ProviderCompleted(final_text.encode("utf-8"), request_id)
            elif event_type == "response.incomplete":
                return _failure(
                    "PROVIDER_RESPONSE_INCOMPLETE",
                    provider_request_id=request_id,
                    disposition=ProviderFailureDisposition.REPAIRABLE,
                )
            elif event_type == "response.failed":
                return _failure(
                    "PROVIDER_RESPONSE_FAILED",
                    provider_request_id=request_id,
                    disposition=_response_failure_disposition(response),
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


def _ark_origin(base_url: str) -> str:
    split = urlsplit(base_url)
    host = cast(str, split.hostname).lower()
    default_port = 443 if split.scheme.lower() == "https" else None
    port = split.port
    authority = host if port in (None, default_port) else f"{host}:{port}"
    return f"{split.scheme.lower()}://{authority}"


def _required_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise ValueError(f"{field_name} must be non-empty text")
    return value


def _failure(
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


def _error_status(error: Exception) -> int | None:
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _trace_id(error: Exception) -> str | None:
    value = getattr(error, "request_id", None)
    # Ark SDK exceptions expose an HTTP trace/request id.  It is not the
    # Responses API response.id and must never be persisted as reconcile identity.
    return value if isinstance(value, str) and value else None


def _map_client_error(error: Exception) -> ProviderResult:
    status_code = _error_status(error)
    return _failure(
        (
            f"PROVIDER_CLIENT_HTTP_{status_code}"
            if status_code is not None
            else "PROVIDER_CLIENT_INITIALIZATION_FAILED"
        ),
        http_status=status_code,
        provider_trace_id=_trace_id(error),
    )


def _map_response_create_error(error: Exception) -> ProviderResult:
    status_code = _error_status(error)
    if status_code in {429, 500, 502, 503, 504}:
        return _failure(
            f"PROVIDER_HTTP_{status_code}",
            disposition=ProviderFailureDisposition.RETRYABLE,
            http_status=status_code,
            provider_trace_id=_trace_id(error),
        )
    if status_code in {400, 401, 403, 404, 409, 422}:
        return _failure(
            f"PROVIDER_HTTP_{status_code}",
            http_status=status_code,
            provider_trace_id=_trace_id(error),
        )
    reason = (
        f"PROVIDER_CREATE_HTTP_{status_code}"
        if status_code is not None
        else "PROVIDER_TRANSPORT_UNKNOWN"
    )
    return ProviderIndeterminate(reason)


def _map_reconcile_error(error: Exception, *, response_id: str) -> ProviderResult:
    status_code = _error_status(error)
    if status_code in {400, 401, 403, 404, 409, 422}:
        return _failure(
            f"PROVIDER_RECONCILE_HTTP_{status_code}",
            provider_request_id=response_id,
            http_status=status_code,
            provider_trace_id=_trace_id(error),
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
    if isinstance(status, int) and not isinstance(status, bool) and status in {
        429,
        500,
        502,
        503,
        504,
    }:
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


def _map_file_error(error: Exception) -> ProviderResult:
    status_code = _error_status(error)
    if status_code in {400, 401, 403, 404, 409, 422}:
        return _failure(
            f"PROVIDER_HTTP_{status_code}",
            http_status=status_code,
            provider_trace_id=_trace_id(error),
        )
    if status_code in {429, 500, 502, 503, 504}:
        return _failure(
            f"PROVIDER_FILE_HTTP_{status_code}",
            disposition=ProviderFailureDisposition.RETRYABLE,
            http_status=status_code,
            provider_trace_id=_trace_id(error),
        )
    return _failure(
        "PROVIDER_FILE_OUTCOME_UNKNOWN",
        disposition=ProviderFailureDisposition.REPAIRABLE,
        http_status=status_code,
        provider_trace_id=_trace_id(error),
    )


__all__ = [
    "DOUBAO_ARK_ADAPTER_STRATEGY_VERSION",
    "DOUBAO_ARK_PROVIDER_ID",
    "DoubaoArkVlmProvider",
    "DoubaoArkVlmProviderConfig",
]
