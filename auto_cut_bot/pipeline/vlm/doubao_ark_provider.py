"""Streaming Doubao Ark VLM adapter with durable provider-file reuse.

The adapter submits at most one Files API upload and one Responses API request
for a Kernel attempt.  It never turns a truncated stream into success and it
never retries an outcome whose remote state is unknown.
"""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from urllib.parse import urlsplit

from autocut_kernel.vlm import (
    GENERATION_PROVIDER_LEASE_SECONDS,
    ProviderDispatchRequest,
    ProviderFailed,
    ProviderFailureDisposition,
    ProviderReconcileQuery,
    ProviderResult,
)

from auto_cut_bot.pipeline.debug import ModelIoDebugContext, ModelIoDebugSink

from .ark_file_cache import ArkFileCachePort, ArkFileCacheRecord
from .ark_responses_transport import (
    ArkResponsesTransport,
    ArkResponsesTransportConfig,
    ClientFactory,
    ark_error_status,
    ark_trace_id,
    close_ark_resource,
    map_ark_client_error,
    provider_failure,
)

DOUBAO_ARK_PROVIDER_ID = "doubao-ark-responses-stream"
DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION = "doubao-ark-files-responses-stream-v2"
# v3 binds the MIME-bearing multipart tuple, but preserves the SDK's nested
# ``text.format.json_schema`` wire shape.  Ark's production endpoint rejects
# that shape, even though the installed SDK declares it, so it remains only a
# historical replay contract.
DOUBAO_ARK_NESTED_SCHEMA_ADAPTER_STRATEGY_VERSION = "doubao-ark-files-responses-stream-v3"
# v4 keeps the v3 multipart media contract and changes only the Responses
# structured-output wire shape to the endpoint-verified direct form.  Request
# identity is distinct from v3; the byte-identical MIME-bearing Files upload
# may safely reuse a v3 provider-media cache entry.
DOUBAO_ARK_ADAPTER_STRATEGY_VERSION = "doubao-ark-files-responses-stream-v4"
# v5 adds an explicit Responses thinking mode without changing v4's default
# or its media-upload/cache and structured-output contracts.
DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION = "doubao-ark-files-responses-stream-v5"
DOUBAO_ARK_SUPPORTED_ADAPTER_STRATEGY_VERSIONS = frozenset({
    DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_NESTED_SCHEMA_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION,
})
_READY_FILE_STATUSES = frozenset({"active", "processed"})
_ERROR_FILE_STATUSES = frozenset({"error", "failed", "expired", "deleted"})
_EXPECTED_PAYLOAD_FIELDS = frozenset(
    {
        "model_id",
        "parse_policy",
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
        if self.upload_timeout_seconds + self.timeout_seconds >= GENERATION_PROVIDER_LEASE_SECONDS:
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


class DoubaoArkVlmProvider:
    """Official Ark SDK adapter using Files API plus Responses SSE streaming."""

    provider_id = DOUBAO_ARK_PROVIDER_ID

    def __init__(
        self,
        config: DoubaoArkVlmProviderConfig,
        *,
        file_cache: ArkFileCachePort,
        client_factory: ClientFactory | None = None,
        debug_sink: ModelIoDebugSink | None = None,
    ) -> None:
        if type(config) is not DoubaoArkVlmProviderConfig:  # noqa: E721
            raise TypeError("config must be a DoubaoArkVlmProviderConfig")
        self._config = config
        self._file_cache = file_cache
        self._transport = ArkResponsesTransport(
            ArkResponsesTransportConfig(
                config.api_key, config.base_url, config.timeout_seconds, config.max_stream_bytes
            ),
            client_factory=client_factory,
            debug_sink=debug_sink,
        )

    def dispatch(self, request: ProviderDispatchRequest) -> ProviderResult:
        if type(request) is not ProviderDispatchRequest:  # noqa: E721
            raise TypeError("request must be an exact ProviderDispatchRequest")
        if request.provider_id != self.provider_id:
            return provider_failure("PROVIDER_ID_MISMATCH")
        if request.on_provider_request_id is None:
            return provider_failure("PROVIDER_REQUEST_ID_CALLBACK_REQUIRED")
        if len(request.proxy_content) > self._config.max_video_bytes:
            return provider_failure(
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
            return provider_failure("INVALID_PROVIDER_REQUEST")
        try:
            client = self._transport.create_client()
        except Exception as error:
            return map_ark_client_error(error)
        file_result = self._get_file_id(
            client=client,
            request=request,
            adapter_strategy_version=cast(str, parameters["adapter_strategy_version"]),
            video_fps=cast(float, parameters["video_fps"]),
        )
        if not isinstance(file_result, str):
            close_ark_resource(client)
            return file_result
        body: dict[str, object] = dict(
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
            text=_response_text_format(
                cast(str, parameters["adapter_strategy_version"]),
                cast(dict[str, object], payload["response_schema"]),
            ),
            max_output_tokens=parameters["max_output_tokens"],
            temperature=parameters["temperature"],
            stream=True,
            store=True,
        )
        if parameters["adapter_strategy_version"] == DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION:
            body["thinking"] = {"type": parameters["thinking_type"]}
        return self._transport.dispatch(
            body,
            expected_model=request.model_id,
            on_provider_request_id=request.on_provider_request_id,
            client=client,
            debug_context=ModelIoDebugContext(
                provider=self.provider_id,
                provider_idempotency_key=request.provider_idempotency_key,
                model=request.model_id,
                call_kind="vlm_semantic_evidence",
            ),
        )

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult:
        if type(query) is not ProviderReconcileQuery:  # noqa: E721
            raise TypeError("query must be an exact ProviderReconcileQuery")
        if query.provider_id != self.provider_id:
            return provider_failure("PROVIDER_ID_MISMATCH")
        return self._transport.reconcile(query)

    def _get_file_id(
        self,
        *,
        client: Any,
        request: ProviderDispatchRequest,
        adapter_strategy_version: str,
        video_fps: float,
    ) -> str | ProviderResult:
        policy_hash = _preprocess_policy_hash(adapter_strategy_version, video_fps)
        record, lease_acquired = self._file_cache.claim(
            provider_id=self.provider_id,
            provider_scope_fingerprint=self._config.provider_scope_fingerprint,
            content_hash=request.proxy_blob_ref.content_hash,
            byte_length=request.proxy_blob_ref.byte_length,
            media_type=request.proxy_blob_ref.media_type,
            preprocess_policy_hash=policy_hash,
            lease_seconds=self._config.file_cache_lease_seconds,
            unknown_outcome_quarantine_seconds=(self._config.unknown_upload_quarantine_seconds),
        )
        if record.state == "available":
            if record.expires_at is None or record.expires_at <= datetime.now(timezone.utc):
                self._file_cache.mark_expired(
                    record.media_object_id,
                    expected_version=record.version,
                    provider_status="local_ttl_expired",
                )
                return provider_failure("PROVIDER_MEDIA_EXPIRED")
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
                    return provider_failure(
                        "PROVIDER_MEDIA_NOT_AVAILABLE", provider_status="not_found"
                    )
                return mapped
            status = str(getattr(info, "status", "")).lower()
            if status in _READY_FILE_STATUSES:
                return cast(str, record.provider_file_id)
            self._file_cache.mark_expired(
                record.media_object_id,
                expected_version=record.version,
                provider_status=status or "provider_status_unknown",
            )
            return provider_failure("PROVIDER_MEDIA_NOT_AVAILABLE", provider_status=status)
        if record.state == "processing":
            if not lease_acquired:
                return provider_failure(
                    "PROVIDER_MEDIA_UPLOAD_IN_PROGRESS",
                    disposition=ProviderFailureDisposition.REPAIRABLE,
                )
            if record.provider_file_id is None:
                return provider_failure("PROVIDER_MEDIA_CACHE_INVALID")
            return self._finish_processing_file(
                client=client,
                record=record,
                file_id=record.provider_file_id,
            )
        if record.state == "indeterminate":
            return provider_failure(
                "PROVIDER_MEDIA_UPLOAD_OUTCOME_UNKNOWN",
                disposition=ProviderFailureDisposition.REPAIRABLE,
            )
        if not lease_acquired:
            if record.state == "reserved":
                return provider_failure(
                    "PROVIDER_MEDIA_UPLOAD_IN_PROGRESS",
                    disposition=ProviderFailureDisposition.REPAIRABLE,
                )
            return provider_failure(
                "PROVIDER_MEDIA_TERMINAL",
                state=record.state,
                failure_code=record.failure_code,
            )

        try:
            uploaded = client.files.create(
                file=_upload_file_argument(
                    adapter_strategy_version,
                    request.proxy_content,
                    request.proxy_blob_ref.media_type,
                ),
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
                    + timedelta(seconds=self._config.unknown_upload_quarantine_seconds),
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
                    return provider_failure(
                        "PROVIDER_MEDIA_PROCESSING_FAILED", provider_status=status
                    )
                self._file_cache.release_processing(
                    record.media_object_id,
                    expected_version=record.version,
                    expected_lease_token=record.lease_token,
                    provider_status=status or "provider_status_unknown",
                )
                return provider_failure(
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
            return provider_failure(
                "PROVIDER_MEDIA_PROCESSING_UNKNOWN",
                disposition=ProviderFailureDisposition.RETRYABLE,
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


def _request_parameters(value: object) -> dict[str, str | int | float]:
    if not isinstance(value, dict):
        raise ValueError("Ark request_parameters must be an object")
    parameters = cast(dict[str, object], value)
    adapter_strategy_version = parameters.get("adapter_strategy_version")
    if (
        type(adapter_strategy_version) is not str  # noqa: E721
        or adapter_strategy_version not in DOUBAO_ARK_SUPPORTED_ADAPTER_STRATEGY_VERSIONS
    ):
        raise ValueError("Ark adapter strategy version is not registered")
    explicit_thinking = adapter_strategy_version == DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION
    expected_fields = _EXPECTED_PARAMETER_FIELDS | {"thinking_type"} if explicit_thinking else _EXPECTED_PARAMETER_FIELDS
    if frozenset(parameters) != expected_fields:
        raise ValueError("Ark request_parameters are not closed")
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
    result: dict[str, str | int | float] = {
        "adapter_strategy_version": adapter_strategy_version,
        "video_fps": float(fps),
        "max_output_tokens": tokens,
        "temperature": float(temperature),
    }
    if explicit_thinking:
        thinking_type = parameters["thinking_type"]
        if type(thinking_type) is not str or thinking_type not in {"enabled", "disabled", "auto"}:  # noqa: E721
            raise ValueError("thinking_type must be an explicit enabled, disabled, or auto mode")
        result["thinking_type"] = thinking_type
    return result


def _upload_file_argument(
    adapter_strategy_version: str,
    content: bytes,
    media_type: str,
) -> tuple[str, bytes] | tuple[str, bytes, str]:
    """Reproduce the exact historical upload wire contract for each profile."""
    if adapter_strategy_version == DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION:
        return ("window.mp4", content)
    if adapter_strategy_version in {
        DOUBAO_ARK_NESTED_SCHEMA_ADAPTER_STRATEGY_VERSION,
        DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
        DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION,
    }:
        # Ark validates the multipart part's declared MIME type.  Without it
        # the SDK infers application/octet-stream, which is a distinct v2
        # wire contract and is rejected before a Responses object is created.
        return ("window.mp4", content, media_type)
    raise ValueError("Ark adapter strategy version is not registered")


def _response_text_format(
    adapter_strategy_version: str, response_schema: dict[str, object]
) -> dict[str, object]:
    """Return the versioned Ark Responses structured-output wire contract.

    The official SDK's v3 annotations describe a nested ``json_schema``
    object, while the actual Ark endpoint rejects that field.  The observed
    endpoint-accepted v4 form is direct under ``text.format``.  A persisted
    v3 request must still reproduce its original wire contract during replay.
    """
    descriptor = {
        "name": "vlm_semantic_pack_v3",
        "strict": True,
        "schema": response_schema,
    }
    if adapter_strategy_version in {
        DOUBAO_ARK_LEGACY_ADAPTER_STRATEGY_VERSION,
        DOUBAO_ARK_NESTED_SCHEMA_ADAPTER_STRATEGY_VERSION,
    }:
        return {"format": {"type": "json_schema", "json_schema": descriptor}}
    if adapter_strategy_version in {
        DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
        DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION,
    }:
        return {"format": {"type": "json_schema", **descriptor}}
    raise ValueError("Ark adapter strategy version is not registered")


def _preprocess_policy_hash(adapter_strategy_version: str, video_fps: float) -> str:
    # v3, v4, and v5 differ only in the Responses request after file upload. Their
    # completed Files objects are byte/MIME/purpose-equivalent and may be
    # reused. v2 remains isolated because it omitted the multipart MIME type.
    media_adapter_strategy_version = (
        DOUBAO_ARK_NESTED_SCHEMA_ADAPTER_STRATEGY_VERSION
        if adapter_strategy_version in {
            DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
            DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION,
        }
        else adapter_strategy_version
    )
    encoded = json.dumps(
        {
            "adapter_strategy_version": media_adapter_strategy_version,
            "purpose": "user_data",
            "video_fps": video_fps,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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


def _map_file_error(error: Exception) -> ProviderResult:
    status_code = ark_error_status(error)
    if status_code in {400, 401, 403, 404, 409, 422}:
        return provider_failure(
            f"PROVIDER_HTTP_{status_code}",
            http_status=status_code,
            provider_trace_id=ark_trace_id(error),
        )
    if status_code in {429, 500, 502, 503, 504}:
        return provider_failure(
            f"PROVIDER_FILE_HTTP_{status_code}",
            disposition=ProviderFailureDisposition.RETRYABLE,
            http_status=status_code,
            provider_trace_id=ark_trace_id(error),
        )
    return provider_failure(
        "PROVIDER_FILE_OUTCOME_UNKNOWN",
        disposition=ProviderFailureDisposition.REPAIRABLE,
        http_status=status_code,
        provider_trace_id=ark_trace_id(error),
    )


__all__ = [
    "DOUBAO_ARK_ADAPTER_STRATEGY_VERSION",
    "DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION",
    "DOUBAO_ARK_NESTED_SCHEMA_ADAPTER_STRATEGY_VERSION",
    "DOUBAO_ARK_PROVIDER_ID",
    "DoubaoArkVlmProvider",
    "DoubaoArkVlmProviderConfig",
]
