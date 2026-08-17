"""Provider HTTP 调用与工具函数 — 从 semantic_engine.py 提取。

包含:
  - call_provider: 向 provider 发起语义请求 (支持 stream)
  - _call_provider_stream: SSE 流式接收, 逐 chunk 积累
  - retry_after_seconds: 从 429 响应提取 Retry-After
  - sanitize_url: URL 脱敏
  - parse_model_json: 解析 provider 响应
  - job_uses_multimodal_model: 判断任务是否需要多模态模型
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
from typing import Any

import litellm
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from autocut_core.semantic.constants import MULTIMODAL_TASKS
from autocut_core.io import json_sha256, utc_now

# ── 常量 ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a Story-first AI video narrative semantic analyzer. "
    "Judge only from the provided video, audio, and structured context. "
    "Do not invent plots, dialogues, character relationships, or timestamps that do not exist. "
    "All referenced IDs must come from the input. "
    "Output only a single JSON object conforming to the strict JSON Schema; no Markdown or reasoning. "
    "Chinese narrative content should be analyzed in Chinese; other languages follow the input."
)

JUNCTION_CONTENT_SIGNATURE_VERSION = (
    "story-junction-semantic-media-content-v1"
)


# ── 工具函数 ────────────────────────────────────────────────────────────


def job_uses_multimodal_model(job: dict[str, Any]) -> bool:
    """判断任务是否需要多模态模型 (带视频/音频输入)。"""
    task = job.get("task")
    return task in MULTIMODAL_TASKS or (
        task == "story_plan_selection"
        and bool(job.get("media_file") or job.get("media_url"))
    )


def retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def sanitize_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def parse_model_json(response: dict[str, Any]) -> dict[str, Any]:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("provider response has no choices[0].message.content") from exc
    if isinstance(content, list):
        text = "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    elif isinstance(content, str):
        text = content
    else:
        raise ValueError("provider response content must be text")
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1])
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("model output must be a JSON object")
    return value


# ── Stream helper ────────────────────────────────────────────────────────


_STREAM_CHUNK_TIMEOUT = 300.0  # 每个 chunk 的超时 (5 分钟)


def _call_provider_stream(
    litellm_kwargs: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    """SSE 流式接收 provider 响应, 逐 chunk 积累为完整 dict。

    流式模式下 litellm 返回一个 generator, 每个 chunk 是一个
    ModelResponse-like 对象。此函数收集所有 chunk 的 content,
    合并后构造与 model_dump() 兼容的 dict 返回。

    流式接收解决了长视频 VLM 分析超时问题:
    - 每个 chunk 有独立的 _STREAM_CHUNK_TIMEOUT (5 分钟)
    - 只要 chunk 持续到达, 总处理时间可以超过 timeout
    """
    import logging
    _log = logging.getLogger(__name__)

    stream_kwargs = dict(litellm_kwargs)
    stream_kwargs["stream"] = True
    # stream 模式下某些 provider 不接受 response_format
    stream_kwargs.pop("response_format", None)

    response = litellm.completion(**stream_kwargs, timeout=timeout)

    collected_content: list[str] = []
    model_name = ""
    usage: dict[str, Any] = {}
    chunk_count = 0
    t_last = time.monotonic()

    for chunk in response:
        chunk_count += 1
        t_last = time.monotonic()

        try:
            chunk_dict = chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)
        except Exception:
            chunk_dict = {}

        choices = chunk_dict.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {}) or choices[0].get("message", {})
            content = delta.get("content", "")
            if content:
                collected_content.append(str(content))

        if not model_name:
            model_name = chunk_dict.get("model", "")

        if chunk_dict.get("usage"):
            usage = chunk_dict["usage"]

        # 检查 chunk 间超时
        if time.monotonic() - t_last > _STREAM_CHUNK_TIMEOUT:
            raise Timeout(f"stream chunk timeout after {_STREAM_CHUNK_TIMEOUT}s, {chunk_count} chunks received")

    full_content = "".join(collected_content)
    _log.debug("stream: %d chunks, %d chars", chunk_count, len(full_content))

    return {
        "id": f"stream-{utc_now().isoformat()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": full_content,
            },
            "finish_reason": "stop",
        }],
        "usage": usage,
    }


# ── call_provider ────────────────────────────────────────────────────────


def call_provider(
    backend: Any,
    payload: dict[str, Any],
    *,
    timeout: float,
    retries: int,
    limiter: Any,
    concurrency: Any,
    recorder: Any = None,
    stream: bool = False,
) -> dict[str, Any]:
    """向 provider 发起语义请求, 使用 litellm 作为 HTTP 传输层。

    stream=True 时使用 SSE 流式接收, 逐 chunk 积累完整响应,
    避免长视频 VLM 分析因单次请求超时失败。
    """
    api_key = backend.api_key()
    if not api_key:
        raise RuntimeError(f"missing API key environment variable {backend.api_key_env}")

    # ── Ark Responses API 路由 (三种模式) ─────────────────────────────
    # - auto: 有媒体时走 Responses API，纯文本走 litellm (兼容旧行为)
    # - force_responses: 所有请求都走 Responses API (推荐，自动启用上下文缓存)
    # - force_chat: 所有请求走 litellm Chat Completions
    # should_use_responses_api() 方法封装了模式判断逻辑
    has_media = bool(payload.get("_media_source"))
    use_responses = getattr(backend, "should_use_responses_api", None)
    if use_responses is not None:
        should_use_ark = use_responses(has_media)
    else:
        # 兼容旧版本 backend (无 should_use_responses_api 方法)
        should_use_ark = getattr(backend, "use_responses_api", False) and has_media

    if should_use_ark:
        try:
            from autocut_core.semantic.engine.ark_responses import call_ark_responses
            return call_ark_responses(
                backend=backend,
                payload=payload,
                api_key=api_key,
                timeout=timeout,
                stream=stream,
                limiter=limiter,
                concurrency=concurrency,
                retries=retries,
                recorder=recorder,
            )
        except ImportError as exc:
            # Ark SDK 未安装，降级到 litellm (仅纯文本可用)
            if has_media:
                raise RuntimeError(
                    "Ark SDK (volcenginesdkarkruntime) 未安装，无法处理多模态请求。"
                    "请安装: pip install volcenginesdkarkruntime"
                ) from exc
            # 纯文本请求可以降级到 litellm
            import logging
            logging.getLogger(__name__).warning(
                "Ark SDK 未安装，降级到 litellm Chat Completions (纯文本模式)"
            )

    litellm_kwargs: dict[str, Any] = {
        "model": f"openai/{payload['model']}",
        "messages": payload["messages"],
        "temperature": payload.get("temperature", 0.0),
        "max_tokens": payload.get("max_tokens"),
        "response_format": payload.get("response_format"),
        "api_base": backend.base_url,
        "api_key": api_key,
        "extra_headers": dict(backend.extra_headers),
        "num_retries": 0,
        "drop_params": True,
    }

    if payload.get("enable_thinking") is not None:
        litellm_kwargs["extra_body"] = {"enable_thinking": payload["enable_thinking"]}

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        limiter.wait()
        concurrency.acquire()
        success = False
        throttled = False
        retry_delay = min(30.0, (2**attempt) + random.random())
        attempt_started_at = utc_now()
        attempt_started_monotonic = time.monotonic()
        http_status: int | None = None
        error_kind: str | None = None
        response_sha256: str | None = None
        token_usage: dict[str, Any] | None = None
        try:
            if stream:
                result = _call_provider_stream(litellm_kwargs, timeout=timeout)
            else:
                response = litellm.completion(**litellm_kwargs, timeout=timeout)
                result = response.model_dump()
            success = True
            http_status = 200
            response_sha256 = json_sha256(json.dumps(result, ensure_ascii=False))
            usage = result.get("usage") if isinstance(result, dict) else None
            if isinstance(usage, dict):
                token_usage = {
                    key: usage.get(key)
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                    if usage.get(key) is not None
                }
            return result
        except RateLimitError as exc:
            http_status = 429
            last_error = exc
            throttled = True
            error_kind = "rate_limit"
            retry_after = _retry_after_from_litellm_exc(exc)
            if retry_after is not None:
                retry_delay = max(retry_delay, retry_after)
            if attempt >= retries:
                break
        except ServiceUnavailableError as exc:
            http_status = getattr(exc, "status_code", 503) or 503
            last_error = exc
            error_kind = "provider_http_error"
            if http_status not in {408, 409, 429, 500, 502, 503, 504} or attempt >= retries:
                break
        except APIError as exc:
            http_status = getattr(exc, "status_code", None)
            last_error = exc
            error_kind = "provider_http_error"
            if http_status not in {408, 409, 429, 500, 502, 503, 504} or attempt >= retries:
                break
        except Timeout as exc:
            last_error = exc
            error_kind = "transport_error"
            if attempt >= retries:
                break
        except APIConnectionError as exc:
            last_error = exc
            error_kind = "transport_error"
            if attempt >= retries:
                break
        except json.JSONDecodeError as exc:
            last_error = exc
            error_kind = "non_json"
            if attempt >= retries:
                break
        except Exception as exc:
            last_error = exc
            error_kind = "unknown"
            if attempt >= retries:
                break
        finally:
            concurrency.release(
                success=success,
                throttled=throttled,
                cooldown_seconds=retry_delay if throttled else 0.0,
            )
            if recorder is not None:
                latency_ms = (time.monotonic() - attempt_started_monotonic) * 1000.0
                recorder.record_http_attempt(
                    attempt_index=attempt + 1,
                    started_at=attempt_started_at,
                    ended_at=utc_now(),
                    latency_ms=latency_ms,
                    http_status=http_status,
                    error_kind=None if success else (error_kind or "unknown"),
                    throttled=throttled,
                    response_sha256=response_sha256,
                    token_usage=token_usage,
                )
        if not throttled:
            time.sleep(retry_delay)
    raise RuntimeError(f"semantic request failed: {last_error}")


def _retry_after_from_litellm_exc(exc: RateLimitError) -> float | None:
    try:
        response = getattr(exc, "response", None)
        if response is None:
            return None
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        value = headers.get("Retry-After")
        if value is None:
            return None
        return max(0.0, float(value))
    except (TypeError, ValueError, AttributeError):
        return None