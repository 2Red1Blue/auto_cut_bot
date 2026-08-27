"""OpenAI-compatible HTTP API server for a fixed auto_cut_bot session.

Provides /v1/chat/completions and /v1/models endpoints.
All requests route to a single persistent API session.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json as _json
import time
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Awaitable, Callable, cast

from aiohttp import web
from loguru import logger

from auto_cut_bot.config.paths import get_media_dir
from auto_cut_bot.pipeline.runtime import (
    IdempotencyConflictError,
    PipelineRunNotFoundError,
    PipelineRunRequest,
    PipelineRunService,
    PipelineRunValidationError,
    ResumeNotAllowedError,
    SourceDeniedError,
    StaleRunVersionError,
    validate_idempotency_key,
    validate_run_id,
)
from auto_cut_bot.pipeline.runtime.composition import (
    PipelineRuntimeConfigurationError,
    PipelineRuntimePort,
    compose_pipeline_runtime_from_environment,
)
from auto_cut_bot.utils.helpers import safe_filename
from auto_cut_bot.utils.media_decode import (
    MAX_FILE_SIZE,
)
from auto_cut_bot.utils.media_decode import (
    FileSizeExceeded as _FileSizeExceeded,
)
from auto_cut_bot.utils.media_decode import (
    save_base64_data_url as _save_base64_data_url,
)
from auto_cut_bot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

if TYPE_CHECKING:
    from auto_cut_bot.agent.loop import AgentLoop

__all__ = (
    "MAX_FILE_SIZE",
    "_FileSizeExceeded",
    "_save_base64_data_url",
    "create_app",
    "handle_chat_completions",
    "handle_pipeline_run",
    "handle_pipeline_resume",
    "handle_pipeline_status",
)


API_SESSION_KEY = "api:default"
API_CHAT_ID = "default"
_AGENT_LOOP_KEY = web.AppKey[Any]("agent_loop")
_MODEL_NAME_KEY = web.AppKey[str]("model_name")
_REQUEST_TIMEOUT_KEY = web.AppKey[float]("request_timeout")
_SESSION_LOCKS_KEY = web.AppKey[dict[str, asyncio.Lock]]("session_locks")
_PREPARE_AGENT_KEY = web.AppKey[Callable[[], Awaitable[None]] | None]("prepare_agent")
_PIPELINE_RUN_SERVICE_KEY = web.AppKey[PipelineRunService | None]("pipeline_run_service")
_PIPELINE_RUNTIME_KEY = web.AppKey[PipelineRuntimePort | None]("pipeline_runtime")
_PIPELINE_AUTH_REQUIRED_KEY = web.AppKey[bool]("pipeline_auth_required")
_PIPELINE_WORKER_ERROR_KEY = web.AppKey[list[str]]("pipeline_worker_error")
_MISSING = object()
_PIPELINE_PATHS = frozenset(
    {"/v1/pipeline/run", "/v1/pipeline/resume", "/v1/pipeline/status"}
)


def _app_value(
    app: Any,
    key: web.AppKey[Any],
    legacy_key: str,
    default: Any = _MISSING,
) -> Any:
    """Read typed aiohttp state while accepting lightweight dict test doubles."""
    try:
        return app[key]
    except KeyError:
        if default is _MISSING:
            return app[legacy_key]
        return app.get(legacy_key, default)


async def _prepare_agent(app: Any) -> None:
    prepare: Callable[[], Awaitable[None]] | None = _app_value(
        app,
        _PREPARE_AGENT_KEY,
        "prepare_agent",
        None,
    )
    if prepare is not None:
        await prepare()


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _error_json(status: int, message: str, err_type: str = "invalid_request_error") -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": err_type, "code": status}},
        status=status,
    )


def _chat_completion_response(
    content: str,
    model: str,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    prompt = (usage or {}).get("prompt_tokens", 0)
    completion = (usage or {}).get("completion_tokens", 0)
    total = (usage or {}).get("total_tokens", 0) or prompt + completion
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        },
    }


def _response_text(value: Any) -> str:
    """Normalize process_direct output to plain assistant text."""
    if value is None:
        return ""
    if hasattr(value, "content"):
        return str(getattr(value, "content") or "")
    return str(value)


def _as_str(value: object) -> str:
    """Return *value* when it is text, otherwise an empty string."""
    return value if isinstance(value, str) else ""


def _require_json_object(value: object, field: str) -> dict[str, Any]:
    """Validate an object-valued field from an untrusted JSON request."""
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    return cast(dict[str, Any], value)


def _require_json_string(value: object, field: str) -> str:
    """Validate a string-valued field from an untrusted JSON request."""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse_chunk(delta: str, model: str, chunk_id: str, finish_reason: str | None = None) -> bytes:
    """Format a single OpenAI-compatible SSE chunk."""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": delta} if delta else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {_json.dumps(payload)}\n\n".encode()


_SSE_DONE = b"data: [DONE]\n\n"

# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------


def _parse_json_content(body: dict[str, Any]) -> tuple[str, list[str]]:
    """Parse JSON request body. Returns (text, media_paths)."""
    messages_value = cast(object, body.get("messages"))
    if not isinstance(messages_value, list):
        raise ValueError("Only a single user message is supported")
    messages = cast(list[object], messages_value)
    if len(messages) != 1:
        raise ValueError("Only a single user message is supported")
    message_value: object = messages[0]
    if not isinstance(message_value, dict):
        raise ValueError("Only a single user message is supported")
    message = cast(dict[str, Any], message_value)
    if message.get("role") != "user":
        raise ValueError("Only a single user message is supported")

    user_content = message.get("content", "")
    media_dir = get_media_dir("api")
    media_paths: list[str] = []

    if isinstance(user_content, list):
        text_parts: list[str] = []
        for part_value in cast(list[object], user_content):
            if not isinstance(part_value, dict):
                continue
            part = cast(dict[str, Any], part_value)
            if part.get("type") == "text":
                text_parts.append(
                    _require_json_string(
                        cast(object, part.get("text", "")),
                        "messages[0].content[].text",
                    )
                )
            elif part.get("type") == "image_url":
                image_url = _require_json_object(
                    cast(object, part.get("image_url", {})),
                    "messages[0].content[].image_url",
                )
                url = _require_json_string(
                    cast(object, image_url.get("url", "")),
                    "messages[0].content[].image_url.url",
                )
                if url.startswith("data:"):
                    saved = _save_base64_data_url(url, media_dir)
                    if saved:
                        media_paths.append(saved)
                elif url:
                    raise ValueError(
                        "Remote image URLs are not supported. "
                        "Use base64 data URLs or upload files via multipart/form-data."
                    )
        text = " ".join(text_parts)
    elif isinstance(user_content, str):
        text = user_content
    else:
        raise ValueError("Invalid content format")

    return text, media_paths


async def _parse_multipart(request: web.Request) -> tuple[str, list[str], str | None, str | None]:
    """Parse multipart/form-data. Returns (text, media_paths, session_id, model)."""
    media_dir = get_media_dir("api")
    reader = await request.multipart()
    text = ""
    session_id = None
    model = None
    media_paths: list[str] = []

    while True:
        part: Any = await reader.next()
        if part is None:
            break
        if part.name == "message":
            text = (await part.read()).decode("utf-8")
        elif part.name == "session_id":
            session_id = (await part.read()).decode("utf-8").strip()
        elif part.name == "model":
            model = (await part.read()).decode("utf-8").strip()
        elif part.name == "files":
            raw = await part.read()
            if len(raw) > MAX_FILE_SIZE:
                raise _FileSizeExceeded(
                    f"File '{part.filename}' exceeds {MAX_FILE_SIZE // (1024 * 1024)}MB limit"
                )
            base = safe_filename(part.filename or "upload.bin")
            filename = f"{uuid.uuid4().hex[:12]}_{base}"
            dest = media_dir / filename
            dest.write_bytes(raw)
            media_paths.append(str(dest))

    if not text:
        text = "请分析上传的文件"

    return text, media_paths, session_id, model


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def handle_chat_completions(request: web.Request) -> web.Response | web.StreamResponse:
    """POST /v1/chat/completions — supports JSON and multipart/form-data."""
    content_type = _as_str(cast(object, request.content_type or ""))

    agent_loop = _app_value(request.app, _AGENT_LOOP_KEY, "agent_loop")
    timeout_s: float = _app_value(
        request.app,
        _REQUEST_TIMEOUT_KEY,
        "request_timeout",
        120.0,
    )
    model_name: str = _app_value(request.app, _MODEL_NAME_KEY, "model_name", "auto_cut_bot")

    stream = False
    try:
        if content_type.startswith("multipart/"):
            text, media_paths, session_id, requested_model = await _parse_multipart(request)
        else:
            try:
                body = await request.json()
            except Exception:
                return _error_json(400, "Invalid JSON body")
            if not isinstance(body, dict):
                return _error_json(400, "Invalid JSON body")
            body = cast(dict[str, Any], body)
            stream = body.get("stream", False)
            requested_model = body.get("model")
            text, media_paths = _parse_json_content(body)
            session_id = body.get("session_id")
    except ValueError as e:
        return _error_json(400, str(e))
    except _FileSizeExceeded as e:
        return _error_json(413, str(e), err_type="invalid_request_error")
    except Exception:
        logger.exception("Error parsing upload")
        return _error_json(413, "File too large or invalid upload")

    if requested_model and requested_model != model_name:
        return _error_json(400, f"Only configured model '{model_name}' is available")

    session_key = f"api:{session_id}" if session_id else API_SESSION_KEY
    session_locks: dict[str, asyncio.Lock] = _app_value(
        request.app,
        _SESSION_LOCKS_KEY,
        "session_locks",
    )
    session_lock = session_locks.setdefault(session_key, asyncio.Lock())

    logger.info(
        "API request session_key={} media={} text={} stream={}",
        session_key, len(media_paths), text[:80], stream,
    )
    # -- streaming path --
    if stream:
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        await resp.prepare(request)

        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        stream_failed = False
        emitted_content = False

        async def _on_stream(token: str) -> None:
            nonlocal emitted_content
            if token:
                emitted_content = True
            await queue.put(token)

        async def _on_stream_end(*_a: Any, **_kw: Any) -> None:
            # Agent stream-end callbacks mark generation segment boundaries.
            # Tool-backed requests may continue after a segment ends, so the
            # HTTP SSE stream is closed only when process_direct returns.
            return None

        async def _run() -> None:
            nonlocal stream_failed
            try:
                async with session_lock:
                    async with asyncio.timeout(timeout_s):
                        await _prepare_agent(request.app)
                        response = await agent_loop.process_direct(
                            content=text,
                            media=media_paths if media_paths else None,
                            session_key=session_key,
                            channel="api",
                            chat_id=API_CHAT_ID,
                            on_stream=_on_stream,
                            on_stream_end=_on_stream_end,
                        )
                    if not emitted_content:
                        response_text = _response_text(response)
                        if response_text.strip():
                            await queue.put(response_text)
            except Exception:
                stream_failed = True
                logger.exception("Streaming error for session {}", session_key)
            finally:
                await queue.put(None)

        task = asyncio.create_task(_run())
        try:
            while True:
                token = await queue.get()
                if token is None:
                    break
                await resp.write(_sse_chunk(token, model_name, chunk_id))
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if not stream_failed:
            await resp.write(_sse_chunk("", model_name, chunk_id, finish_reason="stop"))
            await resp.write(_SSE_DONE)
        return resp

    # -- non-streaming path (original logic) --
    try:
        async with session_lock:
            try:
                async with asyncio.timeout(timeout_s):
                    await _prepare_agent(request.app)
                    response = await agent_loop.process_direct(
                        content=text,
                        media=media_paths if media_paths else None,
                        session_key=session_key,
                        channel="api",
                        chat_id=API_CHAT_ID,
                    )
                response_text = _response_text(response)
                if not response_text or not response_text.strip():
                    logger.warning("Empty response for session {}, using fallback", session_key)
                    response_text = EMPTY_FINAL_RESPONSE_MESSAGE

            except asyncio.TimeoutError:
                return _error_json(504, f"Request timed out after {timeout_s}s")
            except Exception:
                logger.exception("Error processing request for session {}", session_key)
                return _error_json(500, "Internal server error", err_type="server_error")
    except Exception:
        logger.exception("Unexpected API lock error for session {}", session_key)
        return _error_json(500, "Internal server error", err_type="server_error")

    return web.json_response(
        _chat_completion_response(response_text, model_name, getattr(agent_loop, "_last_usage", None))
    )


async def handle_models(request: web.Request) -> web.Response:
    """GET /v1/models"""
    model_name = _app_value(request.app, _MODEL_NAME_KEY, "model_name", "auto_cut_bot")
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "auto_cut_bot",
                }
            ],
        }
    )


async def handle_health(request: web.Request) -> web.Response:
    """GET /health"""
    worker_errors: list[str] = _app_value(
        request.app,
        _PIPELINE_WORKER_ERROR_KEY,
        "pipeline_worker_error",
        [],
    )
    if worker_errors:
        return web.json_response(
            {
                "status": "degraded",
                "component": "pipeline_runtime",
                "reason": worker_errors[-1],
            },
            status=503,
        )
    return web.json_response({"status": "ok"})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    agent_loop: "AgentLoop",
    model_name: str = "auto_cut_bot",
    request_timeout: float = 120.0,
    api_key: str = "",
    prepare_agent: Callable[[], Awaitable[None]] | None = None,
    pipeline_run_service: PipelineRunService | None = None,
    pipeline_runtime: PipelineRuntimePort | None = None,
    pipeline_poll_interval_seconds: float = 1.0,
) -> web.Application:
    """Create the aiohttp application.

    Args:
        agent_loop: An initialized AgentLoop instance.
        model_name: Model name reported in responses.
        request_timeout: Per-request timeout in seconds.
        api_key: Optional API key for Bearer-token authentication on API routes.
        prepare_agent: Optional application-owned readiness callback run before each turn.
        pipeline_run_service: Injected durable pipeline control-plane service.
        pipeline_runtime: Injected composed runtime, including its worker lifecycle.
        pipeline_poll_interval_seconds: Delay between bounded worker polls.
    """
    if pipeline_run_service is not None and pipeline_runtime is not None:
        raise ValueError("inject either pipeline_run_service or pipeline_runtime, not both")
    if (
        isinstance(pipeline_poll_interval_seconds, bool)
        or type(pipeline_poll_interval_seconds) not in (int, float)
        or pipeline_poll_interval_seconds <= 0
    ):
        raise ValueError("pipeline_poll_interval_seconds must be positive")
    environment_runtime = (
        compose_pipeline_runtime_from_environment()
        if pipeline_run_service is None and pipeline_runtime is None
        else None
    )
    if environment_runtime is not None and not api_key:
        raise PipelineRuntimeConfigurationError(
            "environment-composed pipeline runtime requires configured HTTP API authentication"
        )
    composed_runtime = pipeline_runtime or environment_runtime
    app = web.Application(client_max_size=20 * 1024 * 1024)  # 20MB for base64 images
    app[_AGENT_LOOP_KEY] = agent_loop
    app[_MODEL_NAME_KEY] = model_name
    app[_REQUEST_TIMEOUT_KEY] = request_timeout
    app[_SESSION_LOCKS_KEY] = {}  # per-user locks, keyed by session_key
    app[_PREPARE_AGENT_KEY] = prepare_agent
    app[_PIPELINE_RUNTIME_KEY] = composed_runtime
    app[_PIPELINE_RUN_SERVICE_KEY] = (
        pipeline_run_service
        if pipeline_run_service is not None
        else (None if composed_runtime is None else composed_runtime.service)
    )
    app[_PIPELINE_AUTH_REQUIRED_KEY] = environment_runtime is not None
    app[_PIPELINE_WORKER_ERROR_KEY] = []

    if composed_runtime is not None:

        async def pipeline_runtime_lifecycle(_app: web.Application) -> AsyncIterator[None]:
            await composed_runtime.startup_reconstruct()
            stop_event = asyncio.Event()
            worker_task = asyncio.create_task(
                composed_runtime.run_forever(
                    stop_event,
                    poll_interval_seconds=float(pipeline_poll_interval_seconds),
                ),
                name="pipeline-runtime-worker",
            )
            shutdown_started = asyncio.Event()

            def observe_worker_result(task: asyncio.Task[None]) -> None:
                if task.cancelled():
                    return
                error = task.exception()
                if error is None:
                    if shutdown_started.is_set():
                        return
                    _app[_PIPELINE_WORKER_ERROR_KEY].append(
                        "pipeline worker stopped before application shutdown"
                    )
                    logger.error("Pipeline runtime worker stopped before application shutdown")
                    return
                _app[_PIPELINE_WORKER_ERROR_KEY].append("pipeline worker failed")
                logger.error(
                    "Pipeline runtime worker failed with {}",
                    type(error).__name__,
                )

            worker_task.add_done_callback(observe_worker_result)
            try:
                yield
            finally:
                shutdown_started.set()
                stop_event.set()
                # run_forever stops claiming immediately, then returns only after
                # the active bounded batch and its lease heartbeats have drained.
                # Cancelling an asyncio.to_thread call would not stop local media
                # or provider work and would discard supervision of its outcome.
                with contextlib.suppress(Exception):
                    await worker_task

        app.cleanup_ctx.append(pipeline_runtime_lifecycle)

    @web.middleware
    async def auth_middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        # Allow unauthenticated health checks.
        if request.path == "/health":
            return await handler(request)
        if (
            request.path in _PIPELINE_PATHS
            and _app_value(
                request.app,
                _PIPELINE_AUTH_REQUIRED_KEY,
                "pipeline_auth_required",
                False,
            )
            and not api_key
        ):
            return _error_json(
                503,
                "Pipeline API authentication is not configured",
                "server_error",
            )
        if not api_key:
            return await handler(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return _error_json(401, "Missing Authorization header. Use: Bearer <api_key>")
        if not hmac.compare_digest(auth[len("Bearer "):], api_key):
            return _error_json(401, "Invalid API key")
        return await handler(request)

    app.middlewares.append(auth_middleware)

    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_post("/v1/pipeline/run", handle_pipeline_run)
    app.router.add_post("/v1/pipeline/resume", handle_pipeline_resume)
    app.router.add_get("/v1/pipeline/status", handle_pipeline_status)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    return app


async def handle_pipeline_run(request: web.Request) -> web.Response:
    """Persist and enqueue one closed pipeline run intent."""
    service = _pipeline_run_service(request)
    if service is None:
        return _error_json(503, "Pipeline run service is not configured", "server_error")
    try:
        body = await request.json()
    except Exception:
        return _error_json(400, "Invalid JSON body")
    if not isinstance(body, dict):
        return _error_json(400, "JSON body must be an object")
    idempotency_key = request.headers.get("Idempotency-Key", "")
    try:
        run_request = PipelineRunRequest.from_mapping(cast(dict[str, object], body))
        validate_idempotency_key(idempotency_key)
    except PipelineRunValidationError as error:
        return _error_json(400, str(error))
    try:
        claim = await service.submit(run_request, idempotency_key)
    except SourceDeniedError as error:
        return _error_json(403, str(error), "permission_error")
    except IdempotencyConflictError as error:
        return _error_json(409, str(error), "conflict_error")
    except PipelineRunValidationError:
        logger.exception("Pipeline run store returned an invalid projection")
        return _error_json(500, "Pipeline run persistence invariant failed", "server_error")

    return web.json_response(
        {
            "run_id": claim.snapshot.run_id,
            "status": claim.snapshot.status,
            "replayed": claim.replayed,
        },
        status=202,
    )


async def handle_pipeline_resume(request: web.Request) -> web.Response:
    """CAS-resume work or explicitly recheck a run awaiting calibration."""
    service = _pipeline_run_service(request)
    if service is None:
        return _error_json(503, "Pipeline run service is not configured", "server_error")
    try:
        body = await request.json()
    except Exception:
        return _error_json(400, "Invalid JSON body")
    if not isinstance(body, dict):
        return _error_json(400, "JSON body must contain only run_id")
    resume_body = cast(dict[str, object], body)
    if set(resume_body) != {"run_id", "expected_version"}:
        return _error_json(400, "JSON body must contain only run_id and expected_version")
    run_id = resume_body.get("run_id")
    expected_version = resume_body.get("expected_version")
    if not isinstance(run_id, str):
        return _error_json(400, "run_id must be a string")
    if type(expected_version) is not int or expected_version < 0:  # noqa: E721
        return _error_json(400, "expected_version must be a non-negative integer")
    try:
        validate_run_id(run_id)
    except PipelineRunValidationError as error:
        return _error_json(400, str(error))
    try:
        snapshot = await service.resume(run_id, expected_version=expected_version)
    except PipelineRunNotFoundError as error:
        return _error_json(404, f"Pipeline run not found: {error}")
    except StaleRunVersionError as error:
        return _error_json(409, f"Pipeline run version is stale: {error}", "conflict_error")
    except ResumeNotAllowedError as error:
        return _error_json(409, f"Pipeline run cannot be resumed: {error}", "conflict_error")
    except PipelineRunValidationError:
        logger.exception("Pipeline resume store returned an invalid projection")
        return _error_json(500, "Pipeline run persistence invariant failed", "server_error")
    return web.json_response(
        {"run_id": snapshot.run_id, "status": snapshot.status, "version": snapshot.version},
        status=202,
    )


async def handle_pipeline_status(request: web.Request) -> web.Response:
    """Read one persisted run and its command/Receipt projection."""
    service = _pipeline_run_service(request)
    if service is None:
        return _error_json(503, "Pipeline run service is not configured", "server_error")
    if set(request.query) != {"run_id"}:
        return _error_json(400, "run_id is the only supported query parameter")
    run_id = request.query.get("run_id", "")
    try:
        validate_run_id(run_id)
    except PipelineRunValidationError as error:
        return _error_json(400, str(error))
    try:
        snapshot = await service.status(run_id)
    except PipelineRunNotFoundError as error:
        return _error_json(404, f"Pipeline run not found: {error}")
    except PipelineRunValidationError:
        logger.exception("Pipeline status store returned an invalid projection")
        return _error_json(500, "Pipeline run persistence invariant failed", "server_error")
    return web.json_response(snapshot.to_mapping())


def _pipeline_run_service(request: web.Request) -> PipelineRunService | None:
    """Return the injected typed service without reaching into runtime stages."""
    return _app_value(
        request.app,
        _PIPELINE_RUN_SERVICE_KEY,
        "pipeline_run_service",
        None,
    )
