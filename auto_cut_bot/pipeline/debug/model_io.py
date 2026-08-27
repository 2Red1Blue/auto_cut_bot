"""Best-effort, secret-redacted file mirrors of model-provider I/O.

These files are diagnostic copies only.  Kernel BlobRefs, attempts, receipts and
artifacts remain the sole authoritative persistence and this module must never
change a provider result when disk I/O fails.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ContextManager, Generator, Protocol, cast

_LOGGER = logging.getLogger(__name__)
_SENSITIVE_KEY_PARTS = frozenset(
    {"authorization", "api_key", "apikey", "cookie", "password", "secret", "token"}
)


@dataclass(frozen=True, slots=True)
class ModelIoDebugContext:
    """Non-secret correlation identity for one provider invocation."""

    provider: str
    provider_idempotency_key: str
    model: str
    call_kind: str

    def __post_init__(self) -> None:
        for name in ("provider", "provider_idempotency_key", "model", "call_kind"):
            value = getattr(self, name)
            if type(value) is not str or not value or len(value) > 512:  # noqa: E721
                raise ValueError(f"{name} must be bounded non-empty text")


@dataclass(frozen=True, slots=True)
class PipelineStageDebugContext:
    """Non-authoritative identity of the stage currently executing in this task."""

    run_id: str
    stage: str
    command_id: str
    command_version: int
    operation: str

    def __post_init__(self) -> None:
        for name in ("run_id", "stage", "command_id", "operation"):
            value = getattr(self, name)
            if type(value) is not str or not value or len(value) > 512:  # noqa: E721
                raise ValueError(f"{name} must be bounded non-empty text")
        if type(self.command_version) is not int or self.command_version < 0:  # noqa: E721
            raise ValueError("command_version must be a non-negative integer")


_CURRENT_STAGE_DEBUG_CONTEXT: ContextVar[PipelineStageDebugContext | None] = ContextVar(
    "pipeline_stage_debug_context", default=None
)


class ModelIoDebugSink(Protocol):
    def capture_request(
        self, context: ModelIoDebugContext, *, operation: str, body: object
    ) -> None: ...

    def capture_terminal(
        self,
        context: ModelIoDebugContext,
        *,
        operation: str,
        terminal: object,
        raw_output: bytes | str | None = None,
    ) -> None: ...


class PipelineStageDebugSink(Protocol):
    """Best-effort stage diagnostics controlled by the runtime runner."""

    def stage_scope(self, context: PipelineStageDebugContext) -> ContextManager[None]: ...

    def capture_stage_input(self, context: PipelineStageDebugContext, *, value: object) -> None: ...

    def capture_stage_output(self, context: PipelineStageDebugContext, *, value: object) -> None: ...

    def capture_stage_error(self, context: PipelineStageDebugContext, error: BaseException) -> None: ...

class FileModelIoDebugSink:
    """Write atomically, redact recursively, and swallow all diagnostic failures."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("model debug root must be absolute")
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("model debug root must be a directory")
        self._root = resolved

    @property
    def root(self) -> Path:
        return self._root

    def capture_request(
        self, context: ModelIoDebugContext, *, operation: str, body: object
    ) -> None:
        self._capture(
            context,
            "request.json",
            {
                "schema_version": "model-io-debug-v1",
                "record_type": "model_request",
                "operation": operation,
                "body": _redact(body),
            },
        )

    @contextmanager
    def stage_scope(self, context: PipelineStageDebugContext) -> Generator[None, None, None]:
        """Associate nested provider I/O with one async pipeline stage.

        ``ContextVar`` keeps concurrent runs isolated and is propagated by
        ``asyncio.to_thread`` used by the VLM adapter.
        """

        token = _CURRENT_STAGE_DEBUG_CONTEXT.set(context)
        try:
            yield
        finally:
            _CURRENT_STAGE_DEBUG_CONTEXT.reset(token)

    def capture_stage_input(self, context: PipelineStageDebugContext, *, value: object) -> None:
        self._capture_stage(context, "input.json", "stage_input", value)

    def capture_stage_output(self, context: PipelineStageDebugContext, *, value: object) -> None:
        self._capture_stage(context, "output.json", "stage_output", value)

    def capture_stage_error(self, context: PipelineStageDebugContext, error: BaseException) -> None:
        # Exception text can contain an upstream request/authorization echo.
        # Keep only a stable class marker; provider terminal snapshots retain
        # the separately redacted response details.
        self._capture_stage(
            context,
            "error.json",
            "stage_error",
            {"error_type": type(error).__name__},
        )

    def capture_terminal(
        self,
        context: ModelIoDebugContext,
        *,
        operation: str,
        terminal: object,
        raw_output: bytes | str | None = None,
    ) -> None:
        self._capture(
            context,
            "terminal.json",
            {
                "schema_version": "model-io-debug-v1",
                "record_type": "model_terminal",
                "operation": operation,
                "terminal": _redact(_jsonable(terminal)),
            },
        )
        if raw_output is not None:
            raw = raw_output.encode("utf-8") if isinstance(raw_output, str) else raw_output
            self._write_bytes(context, "raw-output.bin", raw)

    def _capture(
        self, context: ModelIoDebugContext, name: str, record: dict[str, object]
    ) -> None:
        self._write_bytes(
            context,
            name,
            json.dumps(
                {
                    "captured_at": datetime.now(UTC).isoformat(),
                    "call_kind": context.call_kind,
                    "model": context.model,
                    "provider": context.provider,
                    "provider_idempotency_key": context.provider_idempotency_key,
                    **record,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
        )

    def _capture_stage(
        self,
        context: PipelineStageDebugContext,
        name: str,
        record_type: str,
        value: object,
    ) -> None:
        self._write_stage_bytes(
            context,
            name,
            json.dumps(
                {
                    "schema_version": "pipeline-stage-debug-v1",
                    "record_type": record_type,
                    "captured_at": datetime.now(UTC).isoformat(),
                    "run_id": context.run_id,
                    "stage": context.stage,
                    "command_id": context.command_id,
                    "command_version": context.command_version,
                    "operation": context.operation,
                    "value": _redact(_jsonable(value)),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
        )

    def _write_bytes(self, context: ModelIoDebugContext, name: str, value: bytes) -> None:
        try:
            directory = self._model_directory(context)
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / name
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
                handle.write(value)
                temporary = Path(handle.name)
            os.replace(temporary, target)
        except Exception:  # diagnostic output must never affect a paid Command
            _LOGGER.warning("model I/O debug artifact was not written", exc_info=True)

    def _write_stage_bytes(
        self,
        context: PipelineStageDebugContext,
        name: str,
        value: bytes,
    ) -> None:
        try:
            directory = self._stage_directory(context)
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / name
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
                handle.write(value)
                temporary = Path(handle.name)
            os.replace(temporary, target)
        except Exception:  # diagnostic output must never affect a paid Command
            _LOGGER.warning("pipeline stage debug artifact was not written", exc_info=True)

    def _stage_directory(self, context: PipelineStageDebugContext) -> Path:
        return self._root / _safe_segment(context.run_id) / _safe_segment(context.stage)

    def _model_directory(self, context: ModelIoDebugContext) -> Path:
        stage_context = _CURRENT_STAGE_DEBUG_CONTEXT.get()
        if stage_context is None:
            base = self._root / "_unscoped"
        else:
            base = self._stage_directory(stage_context)
        return base / "model" / _safe_segment(context.provider) / _call_directory(context)


def _call_directory(context: ModelIoDebugContext) -> str:
    digest = hashlib.sha256(context.provider_idempotency_key.encode("utf-8")).hexdigest()
    return f"{_safe_segment(context.call_kind)}-{digest[:20]}"


def _safe_segment(value: str) -> str:
    normalized = "".join(char if char.isalnum() or char in "-_" else "_" for char in value)
    return normalized[:96] or "unknown"


def _jsonable(value: object) -> object:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            dumped: object = dump(mode="json")
            return dumped
        except Exception:
            pass
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return {str(key): _jsonable(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        sequence = cast(list[object] | tuple[object, ...], value)
        return [_jsonable(item) for item in sequence]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        mapping = cast(dict[object, object], attributes)
        return {str(key): _jsonable(item) for key, item in mapping.items()}
    return {"type": type(value).__name__, "repr": repr(value)[:4096]}


def _redact(value: object, *, key: str | None = None) -> object:
    if key is not None and any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
        return "<redacted>"
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return {str(item_key): _redact(item, key=str(item_key)) for item_key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        sequence = cast(list[object] | tuple[object, ...], value)
        return [_redact(item) for item in sequence]
    if isinstance(value, bytes):
        return {"redacted_bytes": len(value)}
    return value
