"""Closed cut_bot edge adapter for an injected Kernel Agent Runtime."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

from typing import Protocol

from autocut_kernel.agent_runtime import AgentRunIntent, AgentRunResult

from .models import AgentRuntimeAdapterError, AgentRuntimeRequest, AgentRuntimeResponse


class KernelAgentRuntimePort(Protocol):
    """Narrow one-way Kernel runtime contract supplied by composition."""

    def run(self, intent: AgentRunIntent) -> AgentRunResult: ...


class AgentRuntimeAdapter:
    """Map only opaque scenario intent and terminal Kernel output."""

    def __init__(self, runtime: KernelAgentRuntimePort) -> None:
        if not callable(getattr(runtime, "run", None)):
            raise AgentRuntimeAdapterError("runtime must implement the Kernel Agent Runtime port")
        self._runtime = runtime

    def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResponse:
        if type(request) is not AgentRuntimeRequest:  # noqa: E721
            raise AgentRuntimeAdapterError("adapter accepts only AgentRuntimeRequest")
        result = self._runtime.run(AgentRunIntent(request.run_id, request.profile, request.scenario))
        return AgentRuntimeResponse.from_kernel(request, result)
