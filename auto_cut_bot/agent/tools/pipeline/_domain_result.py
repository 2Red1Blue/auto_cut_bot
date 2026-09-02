"""DomainResult and DomainContext — shared data types for domain agent execution.

DomainStatus and DomainContext are defined in the current Agent Native
state-graph ports (DIP).  This module re-exports them for callers and adds
auto_cut_bot-specific types: Artifact and DomainResult.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Re-export from the current package; the legacy ``autocut_core`` package is
# intentionally not part of the Agent Native state-graph dependency.
from ...state_graph.ports import DomainContext, DomainStatus  # noqa: F401

__all__ = ["DomainStatus", "DomainContext", "Artifact", "DomainResult"]


@dataclass
class Artifact:
    """A named artifact produced by a domain agent."""
    name: str
    path: str | None = None
    data: dict[str, Any] | None = None


@dataclass
class DomainResult:
    """Result of a domain agent execution.

    Returned by DomainAgent.execute(). Contains the execution status,
    produced artifacts, any errors, and the milestone reached.
    """

    agent_name: str
    status: DomainStatus
    artifacts: list[Artifact] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    milestone_reached: str | None = None
    human_decision: dict[str, Any] | None = None
    duration_ms: int = 0
