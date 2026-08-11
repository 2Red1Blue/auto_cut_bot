"""Core domain entities for the StateGraph Engine (Phase 3).

Clean Architecture: Entities layer. No dependencies on frameworks or adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID, uuid4


class SessionStatus(str, Enum):
    RUNNING = "running"
    WAITING_FOR_HUMAN = "waiting_for_human"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"
    TIMED_OUT = "timed_out"


class NodeType(str, Enum):
    SUB_AGENT = "sub_agent"
    MILESTONE = "milestone"
    HITL_GATE = "hitl_gate"
    REVIEW_GATE = "review_gate"  # 独立审核 Agent, 自主上下文, 只读 DB


@dataclass
class RetryPolicy:
    max_retries: int = 3
    backoff: str = "exponential"
    backoff_factor: float = 2.0


@dataclass
class Node:
    id: str
    type: NodeType
    config: dict[str, Any] = field(default_factory=dict)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_sec: int = 300


@dataclass
class Edge:
    source: str
    target: str
    condition: str | None = None
    label: str = ""


@dataclass
class Milestone:
    name: str
    required_nodes: list[str] = field(default_factory=list)
    gates: list[str] = field(default_factory=list)


@dataclass
class StateGraph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    entry_point: str = ""
    milestones: list[Milestone] = field(default_factory=list)

    def get_next_node(self, current_node_id: str, state: dict[str, Any] | None = None) -> str | None:
        for edge in self.edges:
            if edge.source == current_node_id:
                if edge.condition and state:
                    if not self._evaluate_condition(edge.condition, state):
                        continue
                return edge.target
        return None

    @staticmethod
    def _evaluate_condition(condition: str, state: dict[str, Any]) -> bool:
        """Evaluate a routing condition against the current state.

        Supported conditions:
        - "approved" → state["_review_verdict"]["status"] == "approved"
        - "rejected" → state["_review_verdict"]["status"] == "rejected"
        - "completed" → state.get("_last_result") is not None
        """
        if condition == "approved":
            verdict = state.get("_review_verdict", {})
            return verdict.get("status") == "approved"
        if condition == "rejected":
            verdict = state.get("_review_verdict", {})
            return verdict.get("status") == "rejected"
        if condition == "completed":
            return state.get("_last_result") is not None
        return False  # unknown condition → deny (safer default)

    def get_node(self, node_id: str) -> Node | None:
        return self.nodes.get(node_id)


@dataclass
class NodeResult:
    status: str  # completed, failed, timeout, waiting_human
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0


@dataclass
class HumanDecision:
    approved: bool
    modifications: dict[str, Any] | None = None
    reason: str = ""
    timestamp: str = ""


@dataclass
class GraphState:
    """Immutable snapshot of the graph state at a node boundary."""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Checkpoint:
    id: UUID = field(default_factory=uuid4)
    session_id: UUID | None = None
    node_name: str = ""
    node_type: NodeType = NodeType.SUB_AGENT
    state_snapshot: dict[str, Any] = field(default_factory=dict)
    inputs_hash: str = ""
    outputs_hash: str = ""
    attempt: int = 1
    status: str = "completed"
    human_decision: dict[str, Any] | None = None
    error_message: str | None = None
    duration_ms: int = 0
    retry_count: int = 0
    next_retry_at: str | None = None
    created_at: str = ""


@dataclass
class Session:
    id: UUID = field(default_factory=uuid4)
    project_id: str = ""
    graph_config: dict[str, Any] = field(default_factory=dict)
    status: SessionStatus = SessionStatus.RUNNING
    current_node: str = ""
    current_milestone: str = ""
    state_snapshot: dict[str, Any] = field(default_factory=dict)
    feature_flag: str = "use_agent_native_v2"
    timeout_config: dict[str, int] = field(default_factory=dict)
    error_message: str | None = None
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
