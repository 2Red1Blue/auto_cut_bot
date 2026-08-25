"""Single-attempt Qwen video adapter for the durable VLM command.

The adapter intentionally has no retry loop.  Ambiguous transport outcomes are
returned as ``ProviderIndeterminate`` so only the Kernel attempt state machine
can decide what happens next.
"""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Protocol, cast

from autocut_kernel.vlm import (
    ProviderCompleted,
    ProviderDispatchRequest,
    ProviderFailed,
    ProviderIndeterminate,
    ProviderReconcileQuery,
    ProviderResult,
)

_PROVIDER_ID = "qwen-openai-chat"
QWEN_ADAPTER_STRATEGY_VERSION = "qwen-video-json-object-schema-prompt-v2"
_EXPECTED_PAYLOAD_FIELDS = frozenset(
    {
        "model_id",
        "parser_strategy_version",
        "prompt",
        "prompt_version",
        "provider_id",
        "proxy_blob",
        "request_parameters",
        "response_schema",
        "window_manifest_set_sha256",
        "window_manifest_sha256",
    }
)
_EXPECTED_PARAMETER_FIELDS = frozenset(
    {"adapter_strategy_version", "fps", "max_tokens", "temperature"}
)


@dataclass(frozen=True, slots=True)
class QwenVlmProviderConfig:
    api_key: str
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    timeout_seconds: float = 300.0
    max_video_bytes: int = 20 * 1024 * 1024

    def __post_init__(self) -> None:
        if type(self.api_key) is not str or not self.api_key.strip():  # noqa: E721
            raise ValueError("Qwen API key must be non-empty")
        if type(self.base_url) is not str or not self.base_url.startswith("https://"):  # noqa: E721
            raise ValueError("Qwen base_url must use HTTPS")
        if type(self.timeout_seconds) not in (int, float) or self.timeout_seconds <= 0:
            raise ValueError("Qwen timeout_seconds must be positive")
        if type(self.max_video_bytes) is not int or self.max_video_bytes < 1:  # noqa: E721
            raise ValueError("Qwen max_video_bytes must be a positive integer")


class ClientFactory(Protocol):
    def __call__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float,
        max_retries: int,
    ) -> object: ...


class QwenVlmProvider:
    """OpenAI-compatible Qwen Chat adapter with exactly one network submission."""

    provider_id = _PROVIDER_ID

    def __init__(
        self,
        config: QwenVlmProviderConfig,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        if type(config) is not QwenVlmProviderConfig:  # noqa: E721
            raise TypeError("config must be a QwenVlmProviderConfig")
        self._config = config
        self._client_factory = client_factory or _openai_client

    def dispatch(self, request: ProviderDispatchRequest) -> ProviderResult:
        if type(request) is not ProviderDispatchRequest:  # noqa: E721
            raise TypeError("request must be an exact ProviderDispatchRequest")
        if request.provider_id != self.provider_id:
            return ProviderFailed(
                "PROVIDER_ID_MISMATCH",
                '{"retryable":false}',
            )
        if len(request.proxy_content) > self._config.max_video_bytes:
            return ProviderFailed(
                "PROVIDER_MEDIA_LIMIT_EXCEEDED",
                json.dumps(
                    {
                        "byte_length": len(request.proxy_content),
                        "limit": self._config.max_video_bytes,
                        "retryable": False,
                    },
                    separators=(",", ":"),
                ),
            )
        try:
            payload = _request_payload(request.request_payload)
            parameters = _request_parameters(payload["request_parameters"])
            response_schema = json.dumps(
                payload["response_schema"],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            prompt = (
                f"{payload['prompt']}\n"
                "必须输出 Semantic Pack v3 的全部 JSON 根字段：schema_version、"
                "window_summary、continuity、entities、facts、events 和 candidate_hypotheses。"
                "所有时序证据必须放在 support.proxy_interval 中并引用 supporting_frame_ids；"
                "禁止输出 observations 或 type/start_pts/end_pts/frame_id 等 v2 扁平别名。"
                f"完整 JSON Schema：{response_schema}"
            )
            client = self._client_factory(
                api_key=self._config.api_key,
                base_url=self._config.base_url,
                timeout=self._config.timeout_seconds,
                max_retries=0,
            )
            response = cast(Any, client).chat.completions.create(
                model=request.model_id,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Judge only from the supplied video and manifest-bound prompt. "
                            "Never invent facts or timestamps. Return strict JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "video_url",
                                "video_url": {
                                    "url": "data:video/mp4;base64,"
                                    + base64.b64encode(request.proxy_content).decode("ascii")
                                },
                                "fps": parameters["fps"],
                            },
                            {"type": "text", "text": prompt},
                        ],
                    },
                ],
                temperature=parameters["temperature"],
                max_tokens=parameters["max_tokens"],
                response_format={"type": "json_object"},
                extra_headers={"X-DashScope-DataInspection": "disable"},
            )
            content = response.choices[0].message.content
            if type(content) is not str or not content.strip():  # noqa: E721
                return ProviderFailed("PROVIDER_EMPTY_RESPONSE", '{"retryable":false}')
            response_id = getattr(response, "id", None)
            provider_request_id = (
                response_id if isinstance(response_id, str) and response_id else None
            )
            return ProviderCompleted(content.encode("utf-8"), provider_request_id)
        except ValueError:
            return ProviderFailed("INVALID_PROVIDER_REQUEST", '{"retryable":false}')
        except Exception as error:
            return _map_provider_error(error)

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult:
        if type(query) is not ProviderReconcileQuery:  # noqa: E721
            raise TypeError("query must be an exact ProviderReconcileQuery")
        if query.provider_id != self.provider_id:
            return ProviderFailed("PROVIDER_ID_MISMATCH", '{"retryable":false}')
        return ProviderIndeterminate(
            "PROVIDER_RECONCILIATION_UNSUPPORTED",
            query.provider_request_id,
        )


def _openai_client(
    *,
    api_key: str,
    base_url: str,
    timeout: float,
    max_retries: int,
) -> object:
    from openai import OpenAI

    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


def _request_payload(raw: bytes) -> dict[str, object]:
    try:
        value = cast(object, json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("request payload must be strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("request payload does not match the closed Qwen adapter contract")
    result = cast(dict[str, object], value)
    if frozenset(result) != _EXPECTED_PAYLOAD_FIELDS:
        raise ValueError("request payload does not match the closed Qwen adapter contract")
    for field in ("model_id", "prompt", "prompt_version", "provider_id"):
        if type(result[field]) is not str or not cast(str, result[field]).strip():  # noqa: E721
            raise ValueError(f"request payload {field} must be non-empty text")
    if result["provider_id"] != _PROVIDER_ID:
        raise ValueError("request payload provider_id mismatch")
    if not isinstance(result["response_schema"], dict):
        raise ValueError("request payload response_schema must be an object")
    return result


def _request_parameters(value: object) -> dict[str, int | float]:
    if not isinstance(value, dict):
        raise ValueError("Qwen request_parameters must contain only fps/max_tokens/temperature")
    parameters = cast(dict[str, object], value)
    if frozenset(parameters) != _EXPECTED_PARAMETER_FIELDS:
        raise ValueError("Qwen request_parameters must contain only fps/max_tokens/temperature")
    if parameters["adapter_strategy_version"] != QWEN_ADAPTER_STRATEGY_VERSION:
        raise ValueError("Qwen adapter strategy version is not registered")
    fps = parameters["fps"]
    max_tokens = parameters["max_tokens"]
    temperature = parameters["temperature"]
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not 0.1 <= fps <= 10:
        raise ValueError("Qwen fps must be between 0.1 and 10")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 16_384:  # noqa: E721
        raise ValueError("Qwen max_tokens must be an integer between 1 and 16384")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise ValueError("Qwen temperature must be numeric")
    if not 0 <= temperature <= 2:
        raise ValueError("Qwen temperature must be between 0 and 2")
    return {"fps": float(fps), "max_tokens": max_tokens, "temperature": float(temperature)}


def _map_provider_error(error: Exception) -> ProviderResult:
    status = getattr(error, "status_code", None)
    status_code = status if isinstance(status, int) and not isinstance(status, bool) else None
    request_id_value = getattr(error, "request_id", None)
    request_id = (
        request_id_value if isinstance(request_id_value, str) and request_id_value else None
    )
    if status_code in {400, 401, 403, 404, 422}:
        return ProviderFailed(
            f"PROVIDER_HTTP_{status_code}",
            json.dumps({"http_status": status_code, "retryable": False}, separators=(",", ":")),
            request_id,
        )
    reason = (
        f"PROVIDER_HTTP_{status_code}" if status_code is not None else "PROVIDER_TRANSPORT_UNKNOWN"
    )
    return ProviderIndeterminate(reason, request_id)


__all__ = [
    "QWEN_ADAPTER_STRATEGY_VERSION",
    "QwenVlmProvider",
    "QwenVlmProviderConfig",
]
