"""Text-only semantic draft adapter using the shared Ark Responses transport.

No Files API, media/proxy substitution, semantic parser, Store write, prompt
prefix or local retry is permitted. Provider idempotency keys identify durable
attempts; they are not a claim that Ark supports safe redispatch without an ID.
"""

from __future__ import annotations

from typing import cast

from autocut_kernel.semantic_chain.draft_provider import (
    MAX_DRAFT_REQUEST_BYTES,
    DraftDispatchRequest,
    DraftProviderError,
)
from autocut_kernel.vlm.provider_port import ProviderReconcileQuery, ProviderResult

from auto_cut_bot.pipeline.debug import ModelIoDebugContext, ModelIoDebugSink

from .ark_responses_transport import (
    ArkResponsesTransport,
    ArkResponsesTransportConfig,
    ClientFactory,
    provider_failure,
)

DOUBAO_DRAFT_PROVIDER_ID = "doubao-ark-text-responses-stream"
DOUBAO_DRAFT_ADAPTER_STRATEGY_VERSION = "doubao-ark-text-responses-stream-v1"


class DoubaoDraftProvider:
    provider_id = DOUBAO_DRAFT_PROVIDER_ID
    strategy_version = DOUBAO_DRAFT_ADAPTER_STRATEGY_VERSION

    def __init__(
        self,
        config: ArkResponsesTransportConfig,
        *,
        max_request_bytes: int,
        client_factory: ClientFactory | None = None,
        debug_sink: ModelIoDebugSink | None = None,
    ) -> None:
        if (
            type(max_request_bytes) is not int
            or not 0 < max_request_bytes <= MAX_DRAFT_REQUEST_BYTES
        ):  # noqa: E721
            raise ValueError("draft request byte budget must be explicit and bounded")
        self._max_request_bytes = max_request_bytes
        self._transport = ArkResponsesTransport(
            config,
            client_factory=client_factory,
            debug_sink=debug_sink,
        )

    def dispatch(self, request: DraftDispatchRequest) -> ProviderResult:
        if type(request) is not DraftDispatchRequest:  # noqa: E721
            raise TypeError("request must be exact DraftDispatchRequest")
        if request.provider_id != self.provider_id:
            return provider_failure("PROVIDER_ID_MISMATCH")
        if request.on_provider_request_id is None:
            return provider_failure("PROVIDER_REQUEST_ID_CALLBACK_REQUIRED")
        if type(request.request_payload) is not bytes:  # noqa: E721
            return provider_failure("INVALID_PROVIDER_REQUEST")
        if len(request.request_payload) > self._max_request_bytes:
            return provider_failure(
                "PROVIDER_REQUEST_LIMIT_EXCEEDED",
                limit=self._max_request_bytes,
                byte_length=len(request.request_payload),
            )
        try:
            body = request.to_provider_body()
        except DraftProviderError:
            return provider_failure("INVALID_PROVIDER_REQUEST")
        text = cast(dict[str, object], body["text"])
        format_value = cast(dict[str, object], text["format"])
        schema = cast(dict[str, object], format_value["json_schema"])
        schema_name = cast(str, schema["name"])
        return self._transport.dispatch(
            body,
            expected_model=request.model_id,
            on_provider_request_id=request.on_provider_request_id,
            debug_context=ModelIoDebugContext(
                provider=self.provider_id,
                provider_idempotency_key=request.provider_idempotency_key,
                model=request.model_id,
                call_kind=f"semantic_draft_{schema_name}",
            ),
        )

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult:
        if type(query) is not ProviderReconcileQuery:  # noqa: E721
            raise TypeError("query must be exact ProviderReconcileQuery")
        if query.provider_id != self.provider_id:
            return provider_failure("PROVIDER_ID_MISMATCH")
        return self._transport.reconcile(query)
