"""Provider-neutral, side-effect-only VLM invocation port.

Adapters may authenticate, upload the exact proxy bytes and invoke a remote
model.  They may not parse observations, assign core ownership, persist
artifacts, select physical endpoints, or retry outside the durable attempt
state machine.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol, TypeAlias, runtime_checkable

from ..media.types import sha256_prefixed
from .models import VlmValidationError
from .window import WindowProxyBlobRef


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise VlmValidationError(f"{field_name} must be non-empty text")
    return value


@runtime_checkable
class ProviderRequestIdCallback(Protocol):
    """Persist the immutable provider request identity at ``response.created``."""

    def __call__(self, provider_request_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderDispatchRequest:
    provider_id: str
    model_id: str
    provider_idempotency_key: str
    request_payload: bytes
    request_payload_sha256: str
    proxy_blob_ref: WindowProxyBlobRef
    proxy_content: bytes
    on_provider_request_id: ProviderRequestIdCallback | None = None

    def __post_init__(self) -> None:
        _text(self.provider_id, "dispatch.provider_id")
        _text(self.model_id, "dispatch.model_id")
        _text(self.provider_idempotency_key, "dispatch.provider_idempotency_key")
        if type(self.request_payload) is not bytes:  # noqa: E721
            raise VlmValidationError("dispatch.request_payload must be exact bytes")
        sha256_prefixed(self.request_payload_sha256, "dispatch.request_payload_sha256")
        if "sha256:" + hashlib.sha256(self.request_payload).hexdigest() != self.request_payload_sha256:
            raise VlmValidationError("dispatch request payload hash mismatch")
        if type(self.proxy_blob_ref) is not WindowProxyBlobRef:  # noqa: E721
            raise VlmValidationError("dispatch.proxy_blob_ref must be a WindowProxyBlobRef")
        if type(self.proxy_content) is not bytes:  # noqa: E721
            raise VlmValidationError("dispatch.proxy_content must be exact bytes")
        if (
            len(self.proxy_content) != self.proxy_blob_ref.byte_length
            or "sha256:" + hashlib.sha256(self.proxy_content).hexdigest()
            != self.proxy_blob_ref.content_hash
        ):
            raise VlmValidationError("dispatch proxy bytes do not match the immutable BlobRef")
        if self.on_provider_request_id is not None and not callable(
            self.on_provider_request_id
        ):
            raise VlmValidationError("dispatch.on_provider_request_id must be callable")


@dataclass(frozen=True, slots=True)
class ProviderReconcileQuery:
    provider_id: str
    model_id: str
    provider_idempotency_key: str
    provider_request_id: str | None

    def __post_init__(self) -> None:
        _text(self.provider_id, "reconcile.provider_id")
        _text(self.model_id, "reconcile.model_id")
        _text(self.provider_idempotency_key, "reconcile.provider_idempotency_key")
        if self.provider_request_id is not None:
            _text(self.provider_request_id, "reconcile.provider_request_id")


@dataclass(frozen=True, slots=True)
class ProviderCompleted:
    raw_response: bytes
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.raw_response) is not bytes or not self.raw_response:  # noqa: E721
            raise VlmValidationError("completed provider result requires non-empty raw bytes")
        if self.provider_request_id is not None:
            _text(self.provider_request_id, "result.provider_request_id")


@dataclass(frozen=True, slots=True)
class ProviderPending:
    provider_request_id: str

    def __post_init__(self) -> None:
        _text(self.provider_request_id, "result.provider_request_id")


@dataclass(frozen=True, slots=True)
class ProviderIndeterminate:
    reason_code: str
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.reason_code, "result.reason_code")
        if self.provider_request_id is not None:
            _text(self.provider_request_id, "result.provider_request_id")


@dataclass(frozen=True, slots=True)
class ProviderFailed:
    failure_code: str
    failure_detail_json: str
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.failure_code, "result.failure_code")
        _text(self.failure_detail_json, "result.failure_detail_json")
        try:
            detail = json.loads(self.failure_detail_json)
        except (TypeError, ValueError) as error:
            raise VlmValidationError("provider failure detail must be JSON") from error
        if not isinstance(detail, dict):
            raise VlmValidationError("provider failure detail must be a JSON object")
        if self.provider_request_id is not None:
            _text(self.provider_request_id, "result.provider_request_id")


ProviderResult: TypeAlias = (
    ProviderCompleted | ProviderPending | ProviderIndeterminate | ProviderFailed
)


@runtime_checkable
class VlmProviderPort(Protocol):
    def dispatch(self, request: ProviderDispatchRequest) -> ProviderResult: ...

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult: ...


__all__ = [
    "ProviderCompleted",
    "ProviderDispatchRequest",
    "ProviderFailed",
    "ProviderIndeterminate",
    "ProviderPending",
    "ProviderRequestIdCallback",
    "ProviderReconcileQuery",
    "ProviderResult",
    "VlmProviderPort",
]
