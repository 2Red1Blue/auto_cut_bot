"""StateMapper — converts between Phase 3 GraphState dicts and Phase 2 DomainContext/DomainResult.

Bridges the gap between the StateGraph engine's dict-based state and
the domain agents' dataclass-based context/result types.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_cut_bot.agent.tools.pipeline._domain_result import (
    Artifact,
    DomainContext,
    DomainResult,
    DomainStatus,
)


class StateMapper:
    """Pure-function mapper between GraphState and DomainContext/DomainResult.

    Stateless — all methods are static. Handles missing keys gracefully.
    """

    @staticmethod
    def to_domain_context(state: dict[str, Any]) -> DomainContext:
        """Convert a GraphState dict to a DomainContext.

        Maps:
            state["job_root"] -> ctx.job_root
            state["config"]   -> ctx.config
            state["backend"]  -> ctx.backend
            state["mode"]     -> ctx.mode
        """
        job_root = state.get("job_root", "")
        if isinstance(job_root, str):
            job_root = Path(job_root)
        return DomainContext(
            job_root=job_root,
            config=state.get("config"),
            bus=state.get("bus"),
            backend=state.get("backend", "qwen"),
            mode=state.get("mode", "auto"),
        )

    @staticmethod
    def from_domain_result(result: DomainResult) -> dict[str, Any]:
        """Convert a DomainResult to a state dict update.

        Maps:
            result.agent_name       -> state["_agent_name"]
            result.status           -> state["_agent_status"]
            result.milestone_reached -> state["milestone"]
            result.errors           -> state["errors"] (appended)
            result.artifacts        -> state["_artifacts"]
        """
        update: dict[str, Any] = {
            "_agent_name": result.agent_name,
            "_agent_status": result.status.value,
            "_artifacts": [
                {"name": a.name, "path": a.path, "data": a.data}
                for a in result.artifacts
            ],
        }
        if result.milestone_reached:
            update["milestone"] = result.milestone_reached
        if result.errors:
            update["_errors"] = result.errors
        return update
