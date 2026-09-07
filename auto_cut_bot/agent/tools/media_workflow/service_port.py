"""Service port bridging agent tools to the pipeline runtime.

The tools never import runtime composition at module scope (keeps discovery
cheap and tests hermetic). `resolve_service_port()` composes the real
DurablePipelineRunService from the environment when configured and returns
None otherwise — tools then answer with a clean diagnostic instead of
crashing or falling back to legacy job_root files.
"""

from __future__ import annotations

from typing import Any, Protocol


class MediaWorkflowServicePort(Protocol):
    """Read/schedule surface the tools are allowed to touch.

    This is the intersection of PipelineRunService the workflow needs; the
    runtime object satisfies it structurally."""

    def status(self, run_id: str) -> Any: ...

    def resume(self, run_id: str, expected_version: int) -> Any: ...


class RuntimeUnavailableError(RuntimeError):
    """Raised when the pipeline runtime is not composed in this process."""


def resolve_service_port() -> Any | None:
    """Compose the real run service from the environment, or None.

    Returns the runtime's PipelineRunService (structural match for the port)
    so both agent and HTTP entry points share one runtime — never a second
    business state."""
    from auto_cut_bot.pipeline.runtime.composition import (
        compose_pipeline_run_service_from_environment,
    )

    return compose_pipeline_run_service_from_environment()


def require_service_port() -> Any:
    port = resolve_service_port()
    if port is None:
        raise RuntimeUnavailableError(
            "pipeline runtime is not configured in this process; "
            "media workflow tools need the Kernel runtime (see "
            "auto_cut_bot.pipeline.runtime.composition)"
        )
    return port


def snapshot_digest(snapshot: Any, *, max_commands: int = 40) -> dict[str, Any]:
    """Safe summary of a PipelineRunSnapshot: statuses and refs only.

    Never returns artifact payloads, blobs or credentials; bounded by count
    so a huge run cannot flood the chat context."""
    mapping = snapshot.to_mapping() if hasattr(snapshot, "to_mapping") else dict(snapshot)
    commands = mapping.get("commands") or []
    summarized = [
        {
            "command_id": c.get("command_id") or c.get("id"),
            "stage": c.get("stage"),
            "status": c.get("status"),
            "receipt_id": c.get("receipt_id"),
            "version": c.get("version"),
        }
        for c in commands[:max_commands]
    ]
    return {
        "run_id": mapping.get("run_id"),
        "status": mapping.get("status"),
        "version": mapping.get("version"),
        "request_hash": mapping.get("request_hash"),
        "execution_profile": mapping.get("execution_profile"),
        "command_count": len(commands),
        "commands_truncated": len(commands) > max_commands,
        "commands": summarized,
    }
