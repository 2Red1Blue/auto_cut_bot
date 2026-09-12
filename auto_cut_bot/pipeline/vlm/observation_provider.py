"""Ark Files/Responses adapter for the rich observation generation command.

The durable request envelope intentionally contains provenance and optional alias
maps.  This adapter treats it as private and projects only the four provider
inputs: model, frozen proxy bytes, prompt, and response schema/parameters.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from autocut_kernel.pipeline.observation_generation import ObservationDispatchRequest
from autocut_kernel.vlm import ProviderDispatchRequest, ProviderReconcileQuery

from .ark_file_cache import ArkFileCachePort
from .ark_responses_transport import (
    ArkResponsesTransport,
    ArkResponsesTransportConfig,
    ClientFactory,
    close_ark_resource,
    map_ark_client_error,
    provider_failure,
)
from .doubao_ark_provider import (
    DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_PROVIDER_ID,
    DoubaoArkVlmProvider,
    DoubaoArkVlmProviderConfig,
)
from .observation_factory import (
    OBSERVATION_ARK_ADAPTER_STRATEGY_VERSION,
    build_observation_ark_body,
)

OBSERVATION_ARK_PROVIDER_ID = DOUBAO_ARK_PROVIDER_ID
_READY = frozenset(("active", "processed"))


class _ObservationMediaHelper(DoubaoArkVlmProvider):
    """Expose only the already-tested Files lifecycle; never dispatch legacy VLM."""

    def get_observation_file_id(self, *, client: Any, request: ProviderDispatchRequest,
                                video_fps: float):
        return self._get_file_id(
            client=client, request=request,
            adapter_strategy_version=DOUBAO_ARK_EXPLICIT_THINKING_ADAPTER_STRATEGY_VERSION,
            video_fps=video_fps,
        )


class ObservationArkProvider:
    provider_id = OBSERVATION_ARK_PROVIDER_ID

    def __init__(self, config: DoubaoArkVlmProviderConfig, *, file_cache: ArkFileCachePort,
                 client_factory: ClientFactory | None = None) -> None:
        self._config, self._cache = config, file_cache
        self._media = _ObservationMediaHelper(config, file_cache=file_cache, client_factory=client_factory)
        self._transport = ArkResponsesTransport(ArkResponsesTransportConfig(
            config.api_key, config.base_url, config.timeout_seconds, config.max_stream_bytes), client_factory=client_factory)

    def dispatch(self, request: ObservationDispatchRequest):
        if type(request) is not ObservationDispatchRequest or request.provider_id != self.provider_id:
            return provider_failure("PROVIDER_ID_MISMATCH")
        try:
            payload = cast(dict[str, object], json.loads(request.request_payload.decode("utf-8")))
            if type(payload) is not dict or payload.get("model_id") != request.model_id:
                raise ValueError("model identity differs")
            prompt, schema, parameters = payload["prompt"], payload["response_schema"], payload["request_parameters"]
            if type(prompt) is not str or type(schema) is not dict or type(parameters) is not dict:
                raise ValueError("observation public payload is malformed")
            parameters = cast(dict[str, object], parameters)
            if set(parameters) != {"adapter_strategy_version", "max_output_tokens", "temperature", "video_fps"} or parameters["adapter_strategy_version"] != OBSERVATION_ARK_ADAPTER_STRATEGY_VERSION:
                raise ValueError("observation request parameters are not registered")
            fps = parameters["video_fps"]
            if type(fps) not in (int, float) or isinstance(fps, bool):
                raise ValueError("video_fps invalid")
            video_fps = cast(int | float, fps)
            if video_fps <= 0:
                raise ValueError("video_fps invalid")
        except (UnicodeDecodeError, ValueError, KeyError, TypeError) as error:
            return provider_failure("INVALID_PROVIDER_REQUEST", validation_message=str(error))
        if len(request.proxy_content) > self._config.max_video_bytes:
            return provider_failure("PROVIDER_MEDIA_LIMIT_EXCEEDED")
        try:
            client = self._transport.create_client()
        except Exception as error:
            return map_ark_client_error(error)
        file_id = self._file(client, request, float(video_fps))
        if not isinstance(file_id, str):
            close_ark_resource(client)
            return file_id
        body = build_observation_ark_body(model_id=request.model_id, file_id=file_id,
            prompt=prompt, response_schema=cast(dict[str, object], schema),
            max_output_tokens=cast(int, parameters["max_output_tokens"]), temperature=cast(int | float, parameters["temperature"]))
        def ignored_provider_request_id(provider_request_id: str) -> None:
            del provider_request_id
        return self._transport.dispatch(body, expected_model=request.model_id,
            on_provider_request_id=request.on_provider_request_id or ignored_provider_request_id, client=client)

    def reconcile(self, query: ProviderReconcileQuery): return self._transport.reconcile(query)

    def _file(self, client: Any, request: ObservationDispatchRequest, fps: float):
        """Delegate only Files cache/upload state transitions to the proven adapter."""
        bridge_payload = b"{}"
        bridge = ProviderDispatchRequest(
            self.provider_id, request.model_id, request.provider_idempotency_key,
            bridge_payload, "sha256:" + hashlib.sha256(bridge_payload).hexdigest(),
            request.proxy_blob_ref, request.proxy_content, request.on_provider_request_id,
        )
        return self._media.get_observation_file_id(client=client, request=bridge, video_fps=fps)


__all__ = ["OBSERVATION_ARK_PROVIDER_ID", "ObservationArkProvider"]
