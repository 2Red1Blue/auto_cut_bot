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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, cast

from aiohttp import web
from loguru import logger

from auto_cut_bot.config.paths import get_media_dir
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
_MISSING = object()


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
                    response = await asyncio.wait_for(
                        agent_loop.process_direct(
                            content=text,
                            media=media_paths if media_paths else None,
                            session_key=session_key,
                            channel="api",
                            chat_id=API_CHAT_ID,
                            on_stream=_on_stream,
                            on_stream_end=_on_stream_end,
                        ),
                        timeout=timeout_s,
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
                response = await asyncio.wait_for(
                    agent_loop.process_direct(
                        content=text,
                        media=media_paths if media_paths else None,
                        session_key=session_key,
                        channel="api",
                        chat_id=API_CHAT_ID,
                    ),
                    timeout=timeout_s,
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
    return web.json_response({"status": "ok"})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    agent_loop: "AgentLoop",
    model_name: str = "auto_cut_bot",
    request_timeout: float = 120.0,
    api_key: str = "",
) -> web.Application:
    """Create the aiohttp application.

    Args:
        agent_loop: An initialized AgentLoop instance.
        model_name: Model name reported in responses.
        request_timeout: Per-request timeout in seconds.
        api_key: Optional API key for Bearer-token authentication on API routes.
    """
    app = web.Application(client_max_size=20 * 1024 * 1024)  # 20MB for base64 images
    app[_AGENT_LOOP_KEY] = agent_loop
    app[_MODEL_NAME_KEY] = model_name
    app[_REQUEST_TIMEOUT_KEY] = request_timeout
    app[_SESSION_LOCKS_KEY] = {}  # per-user locks, keyed by session_key

    @web.middleware
    async def auth_middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        # Allow unauthenticated health checks.
        if request.path == "/health":
            return await handler(request)
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
    """POST /v1/pipeline/run — trigger the pipeline orchestrator directly.

    Uses the PipelineOrchestratorTool to run all 21 stages in sequence,
    bypassing the LLM agent loop for efficiency.

    Request JSON:
        {
            "book_id": "test-001",
            "mode": "auto",
            "source_path": "/data/videos/test-001.mp4",
            "stage_from": null,
            "stage_to": null,
            "backend": "qwen",
            "dry_run": false,
            "force": false
        }

    Returns:
        {"job_root": "...", "status": "completed", "stages": {...}}
    """
    try:
        body = await request.json()
    except Exception:
        return _error_json(400, "Invalid JSON body")

    book_id = body.get("book_id")
    if not book_id:
        return _error_json(400, "book_id is required")

    mode = body.get("mode", "auto")
    source_path = body.get("source_path", "")
    stage_from = body.get("stage_from")
    stage_to = body.get("stage_to")
    backend = body.get("backend")
    dry_run = body.get("dry_run", False)
    force = body.get("force", False)

    # Derive job_root from book_id — use a configured workspace or cwd
    import os as _os
    workspace = _os.environ.get("AUTO_CUT_BOT_WORKSPACE", _os.getcwd())
    job_root = Path(workspace) / "jobs" / book_id

    # Build orchestrator kwargs
    orchestrator_kwargs: dict[str, Any] = {
        "job_root": str(job_root),
        "mode": mode,
        "dry_run": dry_run,
        "force": force,
    }
    if backend:
        orchestrator_kwargs["backend"] = backend
    if stage_from:
        orchestrator_kwargs["from_stage"] = stage_from
    if stage_to:
        orchestrator_kwargs["to_stage"] = stage_to
    if source_path:
        source_p = Path(source_path)
        if source_p.is_file():
            orchestrator_kwargs["input_root"] = str(source_p.parent)
        elif source_p.is_dir():
            orchestrator_kwargs["input_root"] = str(source_p)
        orchestrator_kwargs["source_kind"] = "local"

    # Use PipelineOrchestratorTool directly — no LLM round-trips needed
    from auto_cut_bot.agent.tools.pipeline.orchestrator import PipelineOrchestratorTool

    tool = PipelineOrchestratorTool()
    result = await tool.execute(**orchestrator_kwargs)

    # Read project.json for stage status
    project_path = job_root / "project.json"
    stages_summary: dict[str, Any] = {}
    if project_path.is_file():
        import json as _json
        project = _json.loads(project_path.read_text(encoding="utf-8"))
        stages_summary = project.get("stages", {})

    response_status = "completed"
    if "failed" in str(result):
        response_status = "failed"

    return web.json_response({
        "job_root": str(job_root),
        "book_id": book_id,
        "status": response_status,
        "message": str(result)[:1000],
        "stages": stages_summary,
    })


async def handle_pipeline_resume(request: web.Request) -> web.Response:
    """POST /v1/pipeline/resume — resume a HITL-interrupted session.

    Request JSON:
        {
            "session_id": "pipeline:book-001:abc123",
            "decision": {
                "action": "approved",
                "notes": "Looks good, proceed"
            }
        }

    Returns:
        {"session_id": "...", "status": "resumed", "current_milestone": "..."}
    """
    import os as _os

    try:
        body = await request.json()
    except Exception:
        return _error_json(400, "Invalid JSON body")

    session_id = body.get("session_id")
    if not session_id:
        return _error_json(400, "session_id is required")

    decision = body.get("decision")
    if not isinstance(decision, dict):
        return _error_json(400, "decision must be an object")
    if "action" not in decision:
        return _error_json(400, "decision.action is required")

    # Derive project_root from session_id.
    # Session IDs follow the format: "pipeline:{book_id}:{run_id}"
    # Extract book_id from the session_id and construct the job root.
    workspace = _os.environ.get("AUTO_CUT_BOT_WORKSPACE", _os.getcwd())
    parts = session_id.split(":", 2)
    if len(parts) >= 2:
        book_id = parts[1]
    else:
        book_id = session_id

    project_root = Path(workspace) / "jobs" / book_id

    # Create CheckpointManager and load latest checkpoint.
    from auto_cut_bot.pipeline.stategraph import (
        AgentState,
        CheckpointManager as FileCheckpointManager,
        StateGraphEngine,
    )
    from auto_cut_bot.pipeline.checkpoint import CheckpointManager

    # Determine which checkpoint backend to use.
    db_url = _os.environ.get("AUTO_CUT_BOT_DB_URL", "")
    if db_url:
        ckpt = CheckpointManager(db_url=db_url)
    else:
        ckpt = FileCheckpointManager(project_root=project_root)

    # Load latest checkpoint for this session.
    state = await ckpt.load(session_id)
    if state is None:
        return web.json_response({
            "session_id": session_id,
            "status": "not_found",
            "error": "No checkpoint found for this session",
        }, status=404)

    if not isinstance(state, AgentState):
        return _error_json(500, "Checkpoint deserialization failed: unexpected type")

    # Set the human decision on the state.
    state.human_decision = decision

    # Create engine with loaded state and resume.
    engine = StateGraphEngine(state, checkpointer=ckpt)
    result_state = await engine.resume(decision)

    # Save a checkpoint after resume.
    await ckpt.save(result_state, result_state.status, result_state.current_milestone)

    return web.json_response({
        "session_id": session_id,
        "status": "resumed",
        "current_milestone": result_state.current_milestone,
        "milestone_history": result_state.milestone_history,
        "state_status": result_state.status,
    })


async def handle_pipeline_status(request: web.Request) -> web.Response:
    """GET /v1/pipeline/status?job_root=<path> — query pipeline progress.

    Reads project.json from the job directory and returns:
    - Overall status (pending / running / completed / failed)
    - Per-stage status with timestamps
    - Stage completion percentage

    Query params:
        job_root: Path to the pipeline job directory (required)

    Returns:
        {
            "job_root": "...",
            "overall_status": "completed",
            "progress": "15/21",
            "stages": {...}
        }
    """
    import os as _os

    workspace = _os.environ.get("AUTO_CUT_BOT_WORKSPACE", _os.getcwd())

    # Support both job_root and book_id query params
    job_root = request.query.get("job_root")
    book_id = request.query.get("book_id")

    if not job_root and not book_id:
        return _error_json(400, "job_root or book_id query parameter is required")

    if book_id and not job_root:
        job_root = str(Path(workspace) / "jobs" / book_id)

    project_path = Path(job_root) / "project.json"  # type: ignore[arg-type]
    if not project_path.is_file():
        return web.json_response({
            "job_root": job_root,
            "overall_status": "not_started",
            "progress": "0/0",
            "stages": {},
            "message": "No project.json found — pipeline has not been started.",
        })

    try:
        import json as _json
        project = _json.loads(project_path.read_text(encoding="utf-8"))
    except Exception:
        return _error_json(500, "Failed to read project.json")

    stages = project.get("stages", {})
    if not isinstance(stages, dict):
        stages = {}

    completed = sum(
        1 for s in stages.values()
        if isinstance(s, dict) and s.get("status") == "completed"
    )
    failed = sum(
        1 for s in stages.values()
        if isinstance(s, dict) and s.get("status") == "failed"
    )
    total = len(stages)

    if failed > 0:
        overall = "failed"
    elif completed == total and total > 0:
        overall = "completed"
    elif completed > 0:
        overall = "in_progress"
    else:
        overall = "not_started"

    # Check for failure.json
    failure_path = Path(job_root) / "failure.json"  # type: ignore[arg-type]
    failure_info = None
    if failure_path.is_file():
        try:
            failure_info = _json.loads(failure_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # ── Agent-native V2: stategraph checkpoint status ──────────────────
    v2_state: dict[str, Any] | None = None
    try:
        # Derive session_id from book_id to match checkpoint scope.
        session_id = f"pipeline:{book_id or ''}:latest"

        from auto_cut_bot.pipeline.stategraph import (
            CheckpointManager as FileCheckpointManager,
        )
        from auto_cut_bot.pipeline.checkpoint import CheckpointManager

        db_url = _os.environ.get("AUTO_CUT_BOT_DB_URL", "")
        if db_url:
            ckpt = CheckpointManager(db_url=db_url)
        else:
            ckpt = FileCheckpointManager(project_root=job_root)

        state = await ckpt.load(session_id)
        if state is not None:
            v2_state = {
                "current_milestone": getattr(state, "current_milestone", None),
                "milestone_history": getattr(state, "milestone_history", []),
                "status": getattr(state, "status", None),
                "interrupt_reason": getattr(state, "interrupt_reason", None),
                "human_decision": getattr(state, "human_decision", None),
                "retry_count": getattr(state, "retry_count", 0),
                "errors": getattr(state, "errors", []),
            }
    except Exception:
        # V2 state is best-effort; never fail the status endpoint.
        pass

    response_payload: dict[str, Any] = {
        "job_root": job_root,
        "overall_status": overall,
        "progress": f"{completed}/{total}",
        "completed": completed,
        "failed": failed,
        "total": total,
        "stages": stages,
        "failure": failure_info,
        "updated_at": project.get("updated_at"),
    }

    if v2_state is not None:
        response_payload["v2_state"] = v2_state

    return web.json_response(response_payload)
