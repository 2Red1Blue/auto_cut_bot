from __future__ import annotations

import hashlib

import pytest
from autocut_kernel.vlm import (
    GENERATION_RETRY_STRATEGY_VERSION,
    GenerationRetryPolicy,
    ProviderCompleted,
    ProviderDispatchRequest,
    ProviderFailed,
    ProviderFailureDisposition,
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
    assert (
        ProviderFailed("FAILED", "{}").disposition
        is ProviderFailureDisposition.NONRETRYABLE
    )
    assert ProviderFailed(
        "OVERLOADED",
        '{"status":503}',
        disposition=ProviderFailureDisposition.RETRYABLE,
    ).disposition is ProviderFailureDisposition.RETRYABLE
    with pytest.raises(VlmValidationError, match="disposition"):
        ProviderFailed("FAILED", "{}", disposition="retryable")  # type: ignore[arg-type]


def test_generation_retry_policy_is_explicit_closed_and_canonical() -> None:
    policy = GenerationRetryPolicy(
        GENERATION_RETRY_STRATEGY_VERSION,
        3,
        (0, 2),
    )

    assert policy.to_mapping() == {
        "backoff_seconds": [0, 2],
        "max_attempts": 3,
        "strategy_version": GENERATION_RETRY_STRATEGY_VERSION,
    }
    assert policy.canonical_hash.startswith("sha256:")
    assert policy.backoff_after(1) == 0
    assert policy.backoff_after(2) == 2
    with pytest.raises(VlmValidationError, match="max_attempts - 1"):
        GenerationRetryPolicy(GENERATION_RETRY_STRATEGY_VERSION, 3, (0,))
    with pytest.raises(VlmValidationError, match="between one and three"):
        GenerationRetryPolicy(GENERATION_RETRY_STRATEGY_VERSION, 4, (0, 0, 0))
