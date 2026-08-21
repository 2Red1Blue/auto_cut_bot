"""Ark Responses API 封装 — 使用火山引擎官方 volcenginesdkarkruntime SDK。

为什么单独封装而不走 litellm:
  - Ark VLM 视频输入必须走 Files API（上传文件→file_id→input_video 引用），
    litellm 的 Responses API 路径对此支持不稳定
  - 视频预处理 (fps) 需要 multipart form-data 上传，SDK 原生支持
    files.create(preprocess_configs={"video": {"fps": ...}})
  - 视频文件上传后需等待服务器抽帧预处理（status=active），
    SDK 提供 files.wait_for_processing() 原生轮询
  - 缓存 SHA-keyed file_id 避免重复上传大文件

流式 SSE 解析由 SDK 原生处理。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import mimetypes
import os
import random
import time
from pathlib import Path
from typing import Any

# 火山引擎官方 Ark Runtime SDK（延迟导入，避免非 Ark 环境强制依赖）
_Ark = None

def _get_ark_class():
    global _Ark
    if _Ark is None:
        from volcenginesdkarkruntime import Ark as _ArkCls
        _Ark = _ArkCls
    return _Ark

from autocut_core.semantic.engine.provider import json_sha256, utc_now

DEFAULT_VIDEO_FPS = 1.0
DEFAULT_UPLOAD_TIMEOUT = 300.0
DEFAULT_POLL_INTERVAL = 2.0
MAX_FILE_SIZE_BYTES = 512 * 1024 * 1024  # 512 MB
FILE_READY_STATUSES = {"active", "processed"}
FILE_ERROR_STATUSES = {"error", "failed", "expired"}
FILE_CACHE_MAX_AGE_SECONDS = 6 * 24 * 60 * 60


def _get_ark_client(
    *,
    api_key: str,
    base_url: str,
    timeout: float,
) -> Ark:
    """创建 Ark SDK 客户端实例。

    设置 max_retries=0 由外层重试逻辑统一控制，避免双重重试。
    """
    ArkCls = _get_ark_class()
    return ArkCls(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=0,
    )


# ── 文件上传（使用 SDK 原生 files.create + files.wait_for_processing） ──

def _upload_file_to_ark(
    *,
    client: Ark,
    file_path: Path,
    extra_headers: dict[str, str] | None = None,
    video_fps: float = DEFAULT_VIDEO_FPS,
    purpose: str = "user_data",
) -> str:
    """通过 SDK 上传本地文件到 Ark Files API，返回 file_id。

    视频文件上传后自动触发抽帧预处理（fps 由 preprocess_configs[video][fps] 控制），
    函数会等待文件进入 active 状态才返回，确保 file_id 立即可用于 Responses API。
    """
    file_path = Path(file_path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"待上传文件不存在: {file_path}")

    size = file_path.stat().st_size
    if size > MAX_FILE_SIZE_BYTES:
        raise ValueError(
            f"文件 {file_path.name} 大小 {size / 1024 / 1024:.1f} MB 超过 Ark Files API "
            f"上限 {MAX_FILE_SIZE_BYTES / 1024 / 1024:.0f} MB"
        )

    mime = mimetypes.guess_type(file_path.name)[0] or ""
    preprocess_configs = None
    if mime.startswith("video/"):
        preprocess_configs = {"video": {"fps": video_fps}}

    with open(file_path, "rb") as f:
        file_obj = f.read()  # SDK accepts bytes or file-like

    # SDK 原生 multipart 上传
    resp = client.files.create(
        file=(file_path.name, file_obj),
        purpose=purpose,  # type: ignore[arg-type]
        preprocess_configs=preprocess_configs,
        extra_headers=extra_headers,
    )
    file_id = resp.id
    if not file_id:
        raise RuntimeError(f"Ark Files API 返回无 id 字段: {resp}")
    file_id = str(file_id)

    # 视频文件需等待服务器抽帧预处理完成（processing→active）才能在 Responses API 中引用
    if mime.startswith("video/"):
        _wait_file_ready(
            client=client,
            file_id=file_id,
            extra_headers=extra_headers,
            timeout=max(DEFAULT_UPLOAD_TIMEOUT, 180.0),
        )
    return file_id


def _wait_file_ready(
    *,
    client: Ark,
    file_id: str,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 180.0,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """轮询 Files API 直到文件处理完成 (status in active/processed) 或超时。

    优先使用 SDK 原生 wait_for_processing，失败时回退到手动轮询。
    """
    try:
        client.files.wait_for_processing(
            file_id,
            poll_interval=poll_interval,
            max_wait_seconds=timeout,
            extra_headers=extra_headers,
        )
        # SDK 等待成功后验证状态
        info = client.files.retrieve(file_id, extra_headers=extra_headers)
        if info.status in FILE_ERROR_STATUSES:
            err_info = getattr(info, "error", None)
            raise RuntimeError(
                f"Ark 文件 {file_id} 处理失败: status={info.status}, details={err_info}"
            )
        return
    except Exception as e:
        # 如果 SDK 的 wait_for_processing 因超时或其他原因失败，抛出异常
        if "timeout" in str(e).lower() or "max_wait" in str(e).lower():
            raise TimeoutError(
                f"Ark 文件 {file_id} 在 {timeout}s 内未就绪"
            ) from e
        raise


def _check_file_active(
    client: Ark,
    file_id: str,
    extra_headers: dict[str, str] | None = None,
) -> bool:
    """检查 file_id 是否仍然有效（status=active/processed）。"""
    try:
        info = client.files.retrieve(file_id, extra_headers=extra_headers)
        return info.status in FILE_READY_STATUSES
    except Exception:
        return False


def _sha256_file(file_path: Path) -> str:
    """计算文件 SHA-256（流式读取，不一次性加载大文件）。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_fresh_cache_timestamp(uploaded_at_str: object) -> bool:
    """Return whether a cache timestamp is a recent, unambiguous UTC instant."""
    if not isinstance(uploaded_at_str, str) or not uploaded_at_str:
        return False
    try:
        uploaded_at = datetime.datetime.fromisoformat(uploaded_at_str)
    except ValueError:
        return False
    if uploaded_at.tzinfo is None:
        uploaded_at = uploaded_at.replace(tzinfo=datetime.timezone.utc)
    else:
        uploaded_at = uploaded_at.astimezone(datetime.timezone.utc)
    age_seconds = (datetime.datetime.now(datetime.timezone.utc) - uploaded_at).total_seconds()
    return 0 <= age_seconds < FILE_CACHE_MAX_AGE_SECONDS


def _get_or_upload_file_id(
    *,
    client: Ark,
    file_path: str | Path,
    file_sha256: str | None,
    extra_headers: dict[str, str] | None,
    video_fps: float,
    cache_dir: Path | None = None,
) -> str:
    """获取文件 file_id，优先读 SHA-256 缓存，未命中则上传并写缓存。

    缓存位置（按优先级）：
      1. 显式传入 cache_dir
      2. 文件同目录 .ark_files_cache/
      3. ~/.cache/ark-files/
    """
    file_path = Path(file_path).expanduser().resolve()
    sha = file_sha256 or _sha256_file(file_path)

    candidates: list[Path] = []
    if cache_dir is not None:
        candidates.append(cache_dir / "ark_files")
    candidates.append(file_path.parent / ".ark_files_cache")
    candidates.append(Path.home() / ".cache" / "ark-files")

    upload_kwargs = dict(
        client=client,
        file_path=file_path,
        extra_headers=extra_headers,
        video_fps=video_fps,
    )

    cache_payload_str = None
    for cdir in candidates:
        try:
            cdir.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        cache_file = cdir / f"{sha[:24]}.json"
        if cache_file.is_file():
            try:
                meta = json.loads(cache_file.read_text(encoding="utf-8"))
                fid = meta.get("file_id")
                if fid:
                    if _is_fresh_cache_timestamp(meta.get("uploaded_at")) and _check_file_active(
                        client, str(fid), extra_headers=extra_headers
                    ):
                        return str(fid)
            except (OSError, json.JSONDecodeError):
                pass
        # 在此目录尝试上传并写缓存
        if cache_payload_str is None:
            try:
                file_id = _upload_file_to_ark(**upload_kwargs)
                cache_payload_str = json.dumps(
                    {
                        "file_id": file_id,
                        "sha256": sha,
                        "path": str(file_path),
                        "uploaded_at": utc_now(),
                        "video_fps": video_fps,
                    },
                    ensure_ascii=False,
                )
            except Exception:
                continue
        try:
            cache_file.write_text(cache_payload_str, encoding="utf-8")
        except OSError:
            pass
        return json.loads(cache_payload_str)["file_id"]

    # 所有候选目录均不可写，直接上传（不缓存）
    return _upload_file_to_ark(**upload_kwargs)

# ── 参数构造 ──

def _build_responses_input(
    messages: list[dict[str, Any]],
    *,
    client: Ark,
    media_source: dict[str, Any] | None,
    extra_headers: dict[str, str],
    video_fps: float,
) -> list[dict[str, Any]]:
    """将 Chat Completions 风格 messages 转换为 Responses API input 数组。

    输出格式:
        [
          {"role": "user", "content": [
              {"type": "input_video", "file_id": "file-xxx"},
              {"type": "input_text",  "text": "..."},
              {"type": "input_image_url", "image_url": {"url": "..."}},
          ]},
        ]

    - 本地视频/图片走 Files API 上传（带缓存），转为 file_id 引用
    - HTTP(S) URL 直接使用 input_video.video_url
    - data: 内联 base64 视频不被 Ark 支持，退化为 input_image_url（兼容模式）
    - system 角色在纯文本模式下保留（Ark Responses API 纯文本支持稳定），
      多模态模式下映射为 user（视频输入时 system 支持不稳定）
    """
    has_media = media_source is not None
    input_items: list[dict[str, Any]] = []
    media_inserted = False

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        # 纯文本模式保留 system 角色；多模态模式映射为 user
        api_role = ("user" if role == "system" and has_media else role)
        api_content: list[dict[str, Any]] = []

        # 首轮 user 消息注入媒体
        if role == "user" and not media_inserted and media_source is not None:
            kind = media_source.get("kind")
            if kind == "file":
                file_id = _get_or_upload_file_id(
                    client=client,
                    file_path=media_source["path"],
                    file_sha256=media_source.get("sha256"),
                    extra_headers=extra_headers,
                    video_fps=video_fps,
                )
                api_content.append({"type": "input_video", "file_id": file_id})
            elif kind == "url":
                url = media_source.get("url", "") or ""
                if url.startswith("data:video/"):
                    api_content.append({
                        "type": "input_image_url",
                        "image_url": {"url": url},
                    })
                else:
                    api_content.append({
                        "type": "input_video",
                        "video_url": {"url": url},
                    })
            media_inserted = True

        # 处理 content（多模态 list 或纯 str）
        if isinstance(content, list):
            for part in content:
                ptype = part.get("type")
                if ptype == "text":
                    api_content.append({"type": "input_text", "text": part.get("text", "")})
                elif ptype == "video_url" and media_inserted:
                    continue
                elif ptype == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    api_content.append({"type": "input_image_url", "image_url": {"url": url}})
        elif isinstance(content, str):
            api_content.append({"type": "input_text", "text": content})

        if not api_content:
            api_content.append({"type": "input_text", "text": ""})

        input_items.append({"role": api_role, "content": api_content})

    return input_items


def _build_text_format(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
    """将 Chat Completions 的 response_format 转换为 Responses API 的 text.format。

    A schema descriptor must explicitly provide ``name``, ``strict`` and
    ``schema``.  This adapter must not invent a different contract for Ark.
    """
    if not response_format:
        return None
    rtype = response_format.get("type")
    if rtype == "json_schema":
        descriptor = response_format.get("json_schema")
        return _build_json_schema_text_format(descriptor)
    if rtype == "json_object":
        schema = response_format.get("json_schema")
        if schema is not None:
            return _build_json_schema_text_format(schema)
        return {"format": {"type": "json_object"}}
    return None


def _build_json_schema_text_format(descriptor: object) -> dict[str, Any] | None:
    if not isinstance(descriptor, dict):
        return None
    name = descriptor.get("name")
    strict = descriptor.get("strict")
    schema = descriptor.get("schema")
    if (
        not isinstance(name, str)
        or not name
        or type(strict) is not bool
        or not isinstance(schema, dict)
    ):
        return None
    return {
        "format": {
            "type": "json_schema",
            "name": name,
            "strict": strict,
            "schema": schema,
        }
    }

# ── 主入口 ──

def call_ark_responses(
    backend: Any,
    payload: dict[str, Any],
    *,
    api_key: str,
    timeout: float,
    stream: bool,
    limiter: Any,
    concurrency: Any,
    retries: int,
    recorder: Any = None,
) -> dict[str, Any]:
    """调用火山方舟 Responses API（支持多模态视频 + SSE 流式 + 结构化输出）。

    使用火山引擎官方 volcenginesdkarkruntime SDK 作为 HTTP/SSE 传输层。
    返回 Chat Completions 兼容 dict，下游 parse_model_json 直接可用。

    Parameters
    ----------
    backend : SemanticBackend
        需具备 base_url 属性；可选 video_preprocess_fps（float，默认 1.0）、
        extra_headers（dict/tuple）。
    payload : dict
        Chat Completions 风格 payload；额外支持:
          - _media_source: {"kind": "file"|"url", ...} 供 Files API 上传使用
          - response_format: {"type": "json_object"} 或 json_schema
    """
    messages = payload.get("messages", []) or []
    media_source = payload.get("_media_source")

    extra_headers = dict(getattr(backend, "extra_headers", {}) or {})
    video_fps = float(getattr(backend, "video_preprocess_fps", DEFAULT_VIDEO_FPS))
    base_url = backend.base_url
    model = payload["model"]

    # 创建 Ark 客户端（max_retries=0，由外层重试逻辑统一控制）
    client = _get_ark_client(api_key=api_key, base_url=base_url, timeout=timeout)

    # 构造多模态 input（本地视频自动走 Files API 上传+缓存+等待就绪）
    input_items = _build_responses_input(
        messages,
        client=client,
        media_source=media_source,
        extra_headers=extra_headers,
        video_fps=video_fps,
    )

    # 构造 SDK 调用参数
    kwargs: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "extra_headers": extra_headers or None,
    }

    text_format = _build_text_format(payload.get("response_format"))
    if text_format is not None:
        kwargs["text"] = text_format

    if payload.get("max_tokens"):
        # Ark reasoning 模型会在 output 中包含 reasoning_summary，
        # 消耗 output_tokens 配额。为保证最终 JSON 输出不被截断，
        # 将 max_output_tokens 设为 请求值 + reasoning 预算（上限 32768）。
        # reasoning_budget 从 backend 配置读取（默认 16384，避免推理截断导致输出不完整）。
        requested = int(payload["max_tokens"])
        reasoning_budget = int(getattr(backend, "reasoning_budget", 16384))
        kwargs["max_output_tokens"] = min(requested + reasoning_budget, 32768)
    if payload.get("temperature") is not None:
        kwargs["temperature"] = payload["temperature"]

    # enable_thinking 透传（仅在显式 True 时启用）
    # 注意：Chat Completions API 用 enable_thinking，Responses API 不确定参数名，
    # 暂时不透传到 Responses API，避免 400 错误。
    # TODO: 确认 Ark Responses API SDK 的 thinking 参数名后启用

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
            content_text = ""
            final_usage: dict[str, int] = {}
            http_status = 200

            if stream:
                # ── SSE 流式（SDK 原生解析） ──
                # 事件参考: https://ark.volcengine.com/docs/82379/1599499
                #   response.output_text.delta — 增量文本片段（累积用）
                #   response.output_text.done  — 文本完成（携带完整 text）
                #   response.completed         — 正常完成（含 usage + output）
                #   response.incomplete        — 因 max_tokens 等原因未完成
                #   response.failed / error    — 请求失败
                stream_resp = client.responses.create(
                    **kwargs, stream=True
                )
                resp_obj = None
                for event in stream_resp:
                    if event.type == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if isinstance(delta, str) and delta:
                            content_text += delta
                    elif event.type == "response.output_text.done":
                        # done 事件携带完整 text，比 delta 累积更可靠
                        done_text = getattr(event, "text", "")
                        if isinstance(done_text, str) and done_text:
                            content_text = done_text
                    elif event.type in ("response.completed", "response.incomplete"):
                        resp_obj = event.response
                        if hasattr(resp_obj, "usage") and resp_obj.usage:
                            u = resp_obj.usage
                            final_usage = {
                                "prompt_tokens": getattr(u, "input_tokens", 0) or 0,
                                "completion_tokens": getattr(u, "output_tokens", 0) or 0,
                                "total_tokens": getattr(u, "total_tokens", 0) or 0,
                            }
                        # 从 output 提取最终文本（权威来源，覆盖 delta 累积）
                        output_text_parts: list[str] = []
                        if hasattr(resp_obj, "output"):
                            for item in resp_obj.output:
                                itype = getattr(item, "type", None)
                                if itype == "message":
                                    for ci in getattr(item, "content", []) or []:
                                        if getattr(ci, "type", None) in ("output_text", "text"):
                                            t = getattr(ci, "text", "")
                                            if isinstance(t, str) and t:
                                                output_text_parts.append(t)
                        if output_text_parts:
                            content_text = "".join(output_text_parts)
                        if event.type == "response.incomplete":
                            incomplete = getattr(resp_obj, "incomplete_details", None)
                            if not content_text:
                                # reasoning 阶段就被截断，没有任何输出文本 → 抛异常触发重试
                                raise RuntimeError(
                                    f"Ark response incomplete with empty output "
                                    f"(max_output_tokens too small for reasoning): {incomplete}"
                                )
                            # 有部分输出（delta 已累积），打 warning 但继续使用已有内容
                            import sys as _sys
                            print(
                                f"[ark_responses] WARNING: response incomplete "
                                f"(truncated, using partial content): {incomplete}",
                                file=_sys.stderr,
                            )
                    elif event.type == "response.failed":
                        resp_obj = event.response
                        err = getattr(resp_obj, "error", None) if resp_obj else None
                        raise RuntimeError(f"Ark Responses API failed: {err}")
                    elif event.type == "error":
                        err_code = getattr(event, "code", "")
                        err_msg = getattr(event, "message", "")
                        raise RuntimeError(
                            f"Ark Responses API stream error: code={err_code}, msg={err_msg}"
                        )
            else:
                # ── 非流式 ──
                resp = client.responses.create(**kwargs)
                http_status = 200
                # 提取 output 文本
                output_parts: list[str] = []
                for item in resp.output:
                    if getattr(item, "type", None) == "message":
                        for ci in getattr(item, "content", []) or []:
                            if getattr(ci, "type", None) in ("output_text", "text"):
                                t = getattr(ci, "text", "")
                                if isinstance(t, str):
                                    output_parts.append(t)
                content_text = "".join(output_parts)
                if hasattr(resp, "usage") and resp.usage:
                    u = resp.usage
                    final_usage = {
                        "prompt_tokens": getattr(u, "input_tokens", 0) or 0,
                        "completion_tokens": getattr(u, "output_tokens", 0) or 0,
                        "total_tokens": getattr(u, "total_tokens", 0) or 0,
                    }

            response_sha256 = json_sha256(
                json.dumps({"text": content_text, "usage": final_usage}, ensure_ascii=False)
            )
            token_usage = {
                "prompt_tokens": final_usage.get("prompt_tokens", 0),
                "completion_tokens": final_usage.get("completion_tokens", 0),
                "total_tokens": final_usage.get("total_tokens", 0),
            }

            result = {
                "id": "",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content_text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": token_usage,
            }
            success = True

            if recorder is not None and hasattr(recorder, "record_http_attempt"):
                latency_ms = (time.monotonic() - attempt_started_monotonic) * 1000.0
                recorder.record_http_attempt(
                    attempt_index=attempt + 1,
                    started_at=attempt_started_at,
                    ended_at=utc_now(),
                    latency_ms=latency_ms,
                    http_status=http_status,
                    error_kind=None,
                    throttled=False,
                    response_sha256=response_sha256,
                    token_usage=token_usage,
                )
            concurrency.release(
                success=success,
                throttled=throttled,
                cooldown_seconds=retry_delay if throttled else 0.0,
            )
            return result

        except Exception as exc:
            last_error = exc
            error_kind = "http"
            # 检查是否是 429 限频
            status_code = getattr(exc, "status_code", None)
            if status_code == 429:
                throttled = True
                error_kind = "rate_limit"
                retry_after = None
                if hasattr(exc, "response") and exc.response is not None:
                    retry_after = exc.response.headers.get("Retry-After")
                if retry_after:
                    try:
                        retry_delay = float(retry_after)
                    except ValueError:
                        pass
                else:
                    retry_delay = min(60.0, (2**attempt) + random.random())
            elif status_code and 400 <= status_code < 500:
                error_kind = "http"
                # 4xx 不重试（除 408, 425, 429 外）
                if attempt >= retries or status_code not in (408, 425, 429):
                    pass
            else:
                error_kind = "transport"

            if recorder is not None and hasattr(recorder, "record_http_attempt"):
                latency_ms = (time.monotonic() - attempt_started_monotonic) * 1000.0
                try:
                    recorder.record_http_attempt(
                        attempt_index=attempt + 1,
                        started_at=attempt_started_at,
                        ended_at=utc_now(),
                        latency_ms=latency_ms,
                        http_status=status_code or http_status,
                        error_kind=error_kind or "unknown",
                        throttled=throttled,
                        response_sha256=None,
                        token_usage=None,
                    )
                except Exception:
                    pass
            # max_output_tokens 不足时自动增大（每次翻倍，上限 32768）
            err_msg = str(last_error)
            if "max_output_tokens" in err_msg or "incomplete" in err_msg.lower():
                current_max = kwargs.get("max_output_tokens", 16384)
                new_max = min(current_max * 2, 32768)
                if new_max > current_max:
                    kwargs["max_output_tokens"] = new_max
                    print(
                        f"[ark_responses] Auto-increasing max_output_tokens "
                        f"{current_max} → {new_max} for retry",
                        file=__import__("sys").stderr,
                    )
                    # 不 sleep，立即用更大 token 预算重试
                    concurrency.release(
                        success=False, throttled=False, cooldown_seconds=0.0,
                    )
                    continue

            concurrency.release(
                success=False,
                throttled=throttled,
                cooldown_seconds=retry_delay if throttled else 0.0,
            )
            if attempt >= retries:
                break
            if not throttled:
                time.sleep(retry_delay)

    raise RuntimeError(
        f"Ark Responses API 调用失败 ({retries + 1} attempts): {last_error}"
    ) from last_error
