from __future__ import annotations

import hashlib

import pytest
from autocut_kernel.vlm import (
    ProviderCompleted,
    ProviderDispatchRequest,
    ProviderFailed,
    ProviderReconcileQuery,
    VlmValidationError,
    WindowProxyBlobRef,
)


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def test_dispatch_request_binds_exact_payload_and_proxy_bytes() -> None:
    payload = b'{"prompt":"exact"}'
    proxy = b"proxy-video"
    reference = WindowProxyBlobRef(
        "proxy-object",
        _digest(proxy),
        len(proxy),
        "video/mp4",
    )

    request = ProviderDispatchRequest(
        "provider-test",
        "model-test",
        "idempotency-test",
        payload,
        _digest(payload),
        reference,
        proxy,
    )

    assert request.request_payload == payload
    with pytest.raises(VlmValidationError, match="payload hash mismatch"):
        ProviderDispatchRequest(
            "provider-test",
            "model-test",
            "idempotency-test",
            payload + b"x",
            _digest(payload),
            reference,
            proxy,
        )
    with pytest.raises(VlmValidationError, match="proxy bytes"):
        ProviderDispatchRequest(
            "provider-test",
            "model-test",
            "idempotency-test",
            payload,
            _digest(payload),
            reference,
            proxy + b"x",
        )


def test_provider_results_and_reconcile_query_are_closed() -> None:
    assert ProviderCompleted(b"{}").raw_response == b"{}"
    assert ProviderReconcileQuery(
        "provider-test",
        "model-test",
        "idempotency-test",
        None,
    ).provider_request_id is None
    with pytest.raises(VlmValidationError, match="non-empty raw bytes"):
        ProviderCompleted(b"")
    with pytest.raises(VlmValidationError, match="JSON object"):
        ProviderFailed("FAILED", "[]")
