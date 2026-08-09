"""Provider HTTP 调用与工具函数 — 从 semantic_engine.py 提取。

包含:
  - call_provider: 向 provider 发起语义请求
  - retry_after_seconds: 从 429 响应提取 Retry-After (保留兼容, 不再被 call_provider 使用)
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

from autocut_core.backends._base import MULTIMODAL_TASKS
from autocut_core.io import json_sha256, utc_now

# ── 常量 ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "你是 Story-first AI 短剧语义分析器。只根据提供的视频、音频和结构化上下文判断，"
    "不得补写不存在的剧情、对白、人物关系或时间码。所有引用 ID 必须来自输入。"
    "只输出符合严格 JSON Schema 的一个 JSON 对象，不输出 Markdown 或推理过程。"
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
    """从 429 响应的 Retry-After 头提取建议等待秒数; 缺失/非法时返回 None。

    不再被 call_provider 使用 (litellm 内置重试感知),
    保留以兼容外部调用者。
    """
    value = exc.headers.get("Retry-After") if exc.headers else None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def sanitize_url(value: str) -> str:
    """去掉 URL 的 query/fragment (含签名参数), 用于日志与缓存键的脱敏表示。"""
    parsed = urllib.parse.urlsplit(value)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def parse_model_json(response: dict[str, Any]) -> dict[str, Any]:
    """从 provider 响应中提取模型输出并解析为 JSON 对象。

    兼容 list/str 两种 content 形态; 自动剥离 ``` 围栏。
    """
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
) -> dict[str, Any]:
    """向 provider 发起语义请求, 使用 litellm 作为 HTTP 传输层。

    替换 urllib.request.urlopen → litellm.completion(), 保留:
      - 外层指数退避重试循环 (per-attempt 台账记录 + AIMD 粒度)
      - _entry_symbol 补丁兼容性 (call_provider 导入到 batch_runner 命名空间)
      - concurrency.acquire/release 信号 (每个 attempt 释放)
      - response_sha256 计算 (从 model_dump() 序列化)
      - token_usage 提取

    关键修复 (相对于计划):
      - api_base / api_key 按每次调用传入, 避免全局状态污染 (Critical #1, #2)
      - enable_thinking 通过 extra_body 传递, 绕过 DashScope provider 过滤 (Critical #3)
      - num_retries=0: 我们自行管理重试, 不依赖 litellm 内置重试
      - drop_params=True: 丢弃 litellm 不支持的参数 (安全网)
    """
    api_key = backend.api_key()
    if not api_key:
        raise RuntimeError(f"missing API key environment variable {backend.api_key_env}")

    # ── 构建 litellm kwargs ──────────────────────────────────────────────
    litellm_kwargs: dict[str, Any] = {
        "model": f"openai/{payload['model']}",
        "messages": payload["messages"],
        "temperature": payload.get("temperature", 0.0),
        "max_tokens": payload.get("max_tokens"),
        "response_format": payload.get("response_format"),
        "api_base": backend.base_url,
        "api_key": api_key,
        "extra_headers": dict(backend.extra_headers),
        "num_retries": 0,  # 我们自行管理重试
        "drop_params": True,
    }

    # enable_thinking 必须通过 extra_body 传递 (Critical #3):
    # LiteLLM 的 DashScope provider 在 provider 级别过滤参数,
    # get_supported_openai_params() 不包含 enable_thinking,
    # 放在顶层参数中会被静默剥离。
    if payload.get("enable_thinking") is not None:
        litellm_kwargs["extra_body"] = {"enable_thinking": payload["enable_thinking"]}

    # ── 重试循环 ─────────────────────────────────────────────────────────
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
            response = litellm.completion(
                **litellm_kwargs,
                timeout=timeout,
            )
            success = True
            http_status = 200
            # 转换为 dict 兼容 parse_model_json
            result = response.model_dump()
            response_sha256 = json_sha256(
                json.dumps(result, ensure_ascii=False)
            )
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
            # 尝试从 litellm 异常中提取 Retry-After
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
    """从 litellm RateLimitError 中提取 Retry-After 头 (秒)。

    litellm 的 RateLimitError 可能携带原始 HTTP 响应;
    尝试从 response.headers 中解析 Retry-After。
    """
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
