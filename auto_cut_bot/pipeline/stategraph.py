"""Agent StateGraph Engine — goal-driven state machine replacing linear stage loop.

Replaces the PipelineOrchestrator's sequential stage execution with a
milestone-driven state graph. The engine does NOT call sub-agents directly;
it returns which agent to call next. The caller (API handler or CLI) invokes
the sub-agent and passes the result back.

Backward compatible: PipelineOrchestrator.run() still works when the
feature flag is off (use_agent_native_v2() returns False).

Design principles:
  1. No external dependencies (no LangGraph). Custom state machine is sufficient.
  2. Serialization: AgentState is JSON-serializable via to_dict() / from_dict().
  3. Sub-agents are external: the engine only decides *which* agent to call next.
  4. HITL (Human-in-the-Loop): interrupt/resume pattern for human review gates.
  5. Checkpointing: every milestone transition writes a checkpoint for recovery.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from auto_cut_bot.pipeline.observability import MetricsCollector
from auto_cut_bot.pipeline.planner_memory import get_next_action
from auto_cut_bot.pipeline.conflict_queue import check_and_interrupt

# ── Milestone definitions ──────────────────────────────────────────────────

MILESTONES: list[str] = [
    "source_ready",
    "bible_ready",
    "script_approved",
    "rendered",
]

MILESTONE_CONDITIONS: dict[str, Callable[[AgentState], bool]] = {
    "source_ready": lambda state: state.source_analysis is not None,
    "bible_ready": lambda state: state.bible is not None,
    "script_approved": lambda state: (
        state.script is not None and state.human_decision is not None
    ),
    "rendered": lambda state: state.rendered_output is not None,
}

MILESTONE_AGENTS: dict[str, str] = {
    "source_ready": "source_agent",
    "bible_ready": "story_agent",
    "script_approved": "story_agent",
    "rendered": "production_agent",
}

# Milestones that require human review before advancing.
# Populated by register_hitl_milestone() from pipeline tool registration.
HITL_MILESTONES: set[str] = {"script_approved", "story_qc_review"}


def register_hitl_milestone(milestone: str) -> None:
    """Register a milestone as requiring human review (HITL gate).

    Called during pipeline tool registration when a tool declares
    ``human_review = True``. The engine checks HITL_MILESTONES before
    advancing past a milestone without human input.
    """
    HITL_MILESTONES.add(milestone)


def auto_discover_hitl_milestones(tool_registry: Any = None) -> int:
    """Scan registered pipeline tools for human_review=True and register them.

    Returns the number of newly registered HITL milestones.
    """
    count = 0
    try:
        from auto_cut_bot.agent.tools.registry import ToolRegistry
        from auto_cut_bot.pipeline.state import PIPELINE_ORDER

        if tool_registry is None:
            return count

        for stage_name in PIPELINE_ORDER:
            tool = tool_registry.get(stage_name)
            if tool is not None and getattr(tool, "human_review", False):
                if stage_name not in HITL_MILESTONES:
                    HITL_MILESTONES.add(stage_name)
                    count += 1
    except Exception:
        pass
    return count


# ── AgentState dataclass ──────────────────────────────────────────────────

@dataclass
class AgentState:
    """Shared state flowing through the StateGraph.

    This is the single source of truth for the pipeline's progress.
    Every milestone check and agent dispatch reads from this state.
    """

    session_id: str
    run_id: str
    project_root: str

    # Current milestone in the pipeline.
    current_milestone: str = "source_ready"

    # Ordered list of milestones reached so far.
    milestone_history: list[str] = field(default_factory=list)

    # Milestone outputs — each is set by the corresponding sub-agent.
    source_analysis: dict[str, Any] | None = None
    bible: dict[str, Any] | None = None
    script: dict[str, Any] | None = None
    rendered_output: dict[str, Any] | None = None

    # Arbitrary context passed between agents (e.g., file paths, config).
    context: dict[str, Any] = field(default_factory=dict)

    # Template variables for prompt rendering.
    variables: dict[str, Any] = field(default_factory=dict)

    # Ordered trace of every node transition for audit/debugging.
    execution_trace: list[dict[str, Any]] = field(default_factory=list)

    # Lifecycle status.
    status: str = "running"  # running, waiting_for_human, completed, failed

    # HITL interrupt state.
    interrupt_reason: str | None = None
    interrupt_data: dict[str, Any] | None = None
    human_decision: dict[str, Any] | None = None

    # Error tracking.
    errors: list[dict[str, Any]] = field(default_factory=list)
    retry_count: int = 0

    # Feature flags controlling engine behaviour.
    feature_flags: dict[str, Any] = field(default_factory=dict)

    # ── Serialization ───────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict.

        Handles nested dataclass fields, None values, and list/dict
        members that are already JSON-serializable.
        """
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "project_root": self.project_root,
            "current_milestone": self.current_milestone,
            "milestone_history": list(self.milestone_history),
            "source_analysis": self.source_analysis,
            "bible": self.bible,
            "script": self.script,
            "rendered_output": self.rendered_output,
            "context": dict(self.context),
            "variables": dict(self.variables),
            "execution_trace": list(self.execution_trace),
            "status": self.status,
            "interrupt_reason": self.interrupt_reason,
            "interrupt_data": self.interrupt_data,
            "human_decision": self.human_decision,
            "errors": list(self.errors),
            "retry_count": self.retry_count,
            "feature_flags": dict(self.feature_flags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentState:
        """Deserialize from a dict produced by to_dict()."""
        return cls(
            session_id=data["session_id"],
            run_id=data["run_id"],
            project_root=data["project_root"],
            current_milestone=data.get("current_milestone", "source_ready"),
            milestone_history=list(data.get("milestone_history", [])),
            source_analysis=data.get("source_analysis"),
            bible=data.get("bible"),
            script=data.get("script"),
            rendered_output=data.get("rendered_output"),
            context=dict(data.get("context", {})),
            variables=dict(data.get("variables", {})),
            execution_trace=list(data.get("execution_trace", [])),
            status=data.get("status", "running"),
            interrupt_reason=data.get("interrupt_reason"),
            interrupt_data=data.get("interrupt_data"),
            human_decision=data.get("human_decision"),
            errors=list(data.get("errors", [])),
            retry_count=data.get("retry_count", 0),
            feature_flags=dict(data.get("feature_flags", {})),
        )


# ── Checkpoint Manager ────────────────────────────────────────────────────

class CheckpointManager:
    """Simple file-based checkpoint persistence for AgentState.

    Checkpoints are written to ``{project_root}/.sd-cache/stategraph/``
    as timestamped JSON files. The latest checkpoint is always symlinked
    as ``latest.json`` for fast recovery.
    """

    def __init__(self, project_root: str | Path) -> None:
        self._root = Path(project_root) / ".sd-cache" / "stategraph"
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def latest_path(self) -> Path:
        return self._root / "latest.json"

    async def save(
        self, state: AgentState, status: str, node_name: str
    ) -> Path:
        """Persist a checkpoint to disk.

        Writes a timestamped file and updates the ``latest.json`` symlink.
        """
        ts = int(time.time() * 1000)
        filename = f"{ts}_{node_name}_{status}.json"
        filepath = self._root / filename

        payload = state.to_dict()
        payload["_checkpoint"] = {
            "timestamp": ts,
            "node": node_name,
            "status": status,
        }

        _write_json_atomic(filepath, payload)
        _update_symlink(filepath, self.latest_path)
        return filepath

    async def load(self) -> AgentState | None:
        """Load the latest checkpoint, or None if no checkpoint exists."""
        if not self.latest_path.is_file():
            return None
        try:
            data = _read_json(self.latest_path)
            data.pop("_checkpoint", None)
            return AgentState.from_dict(data)
        except (OSError, ValueError, KeyError) as exc:
            # Corrupt checkpoint — log and return None so caller
            # can fall back to a fresh state.
            import logging
            logging.getLogger(__name__).warning(
                "Checkpoint load failed: %s", exc
            )
            return None

    async def list_checkpoints(self) -> list[Path]:
        """List all checkpoint files sorted by timestamp (oldest first)."""
        files = sorted(
            self._root.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
        )
        # Exclude the latest symlink target from the listing.
        return [f for f in files if f.name != "latest.json"]

    async def cleanup(self, keep: int = 10) -> int:
        """Remove old checkpoints, keeping at most *keep* recent ones.

        Returns the number of files removed.
        """
        checkpoints = await self.list_checkpoints()
        if len(checkpoints) <= keep:
            return 0
        removed = 0
        for path in checkpoints[:-keep]:
            path.unlink(missing_ok=True)
            removed += 1
        return removed


# ── StateGraph Engine ─────────────────────────────────────────────────────

class StateGraphEngine:
    """Goal-driven state machine that replaces the linear stage loop.

    The engine tracks which milestone the pipeline is at, determines
    which sub-agent to call next, and handles HITL interrupt/resume
    for human-review gates.

    Usage::

        state = AgentState(session_id="...", run_id="...", project_root="...")
        engine = StateGraphEngine(state)

        while not engine.is_complete():
            next_agent = engine.get_next_agent()
            # Caller invokes the sub-agent and gets a result dict.
            result = await invoke_sub_agent(next_agent, state)
            engine.apply_result(result)
    """

    def __init__(
        self,
        state: AgentState,
        checkpointer: CheckpointManager | None = None,
        conflict_queue: Any = None,
    ) -> None:
        self.state = state
        self.checkpointer = checkpointer
        self._conflict_queue = conflict_queue
        self._metrics: Any = None

    # ── Public API ──────────────────────────────────────────────────────

    async def run(self, resume_from: str | None = None) -> AgentState:
        """Execute the state graph from the current milestone to completion.

        If *resume_from* is provided, the engine first loads the checkpoint
        and resumes from that milestone.

        The engine iterates:
          1. Check if current milestone condition is met.
          2. If met, advance to the next milestone.
          3. If HITL gate, interrupt and return (caller handles human input).
          4. Otherwise, return so the caller can invoke the next sub-agent.
        """
        if resume_from and self.checkpointer:
            restored = await self.checkpointer.load()
            if restored is not None:
                self.state = restored
                self.state.status = "running"

        self._trace("run_start", {"milestone": self.state.current_milestone})

        self._metrics = MetricsCollector(
            session_id=self.state.session_id,
            job_root=self.state.project_root,
        )
        self._metrics.record_milestone(self.state.current_milestone)

        # Advance through milestones whose conditions are already met.
        # This handles the case where a sub-agent's output was applied
        # before the engine was invoked.
        await self._advance_satisfied_milestones()

        if self._conflict_queue and check_and_interrupt(self.state, self._conflict_queue):
            return self.state
        _next = get_next_action(self.state)

        if self.is_complete():
            self.state.status = "completed"
            self._trace("run_complete", {})
            await self.checkpoint("completed", "terminal")
            return self.state

        await self.checkpoint("running", self.state.current_milestone)
        return self.state

    async def apply_result(self, result: dict[str, Any]) -> AgentState:
        """Apply a sub-agent's output to the current state.

        The caller invokes the sub-agent and passes the result here.
        The engine updates the appropriate milestone field based on
        the current milestone.
        """
        milestone = self.state.current_milestone
        field_map: dict[str, str] = {
            "source_ready": "source_analysis",
            "bible_ready": "bible",
            "script_approved": "script",
            "rendered": "rendered_output",
        }
        field = field_map.get(milestone)
        if field:
            setattr(self.state, field, result)

        self._trace("apply_result", {
            "milestone": milestone,
            "field": field,
            "result_keys": list(result.keys()) if result else [],
        })

        # Advance through milestones whose conditions are now met.
        await self._advance_satisfied_milestones()

        if self._conflict_queue and check_and_interrupt(self.state, self._conflict_queue):
            return self.state
        _next = get_next_action(self.state)

        if self.is_complete():
            self.state.status = "completed"
            self._trace("run_complete", {})
            await self.checkpoint("completed", "terminal")
            return self.state

        await self.checkpoint("running", self.state.current_milestone)
        return self.state

    async def advance_milestone(self) -> None:
        """Check current milestone conditions and advance if met.

        If the current milestone is a HITL gate and the condition is met
        but no human decision has been recorded, the engine interrupts
        instead of advancing.
        """
        current = self.state.current_milestone
        condition = MILESTONE_CONDITIONS.get(current)
        if condition is None:
            return

        if not condition(self.state):
            return

        # HITL gate: require human decision before advancing.
        if current in HITL_MILESTONES and self.state.human_decision is None:
            return

        try:
            idx = MILESTONES.index(current)
        except ValueError:
            return

        if idx + 1 < len(MILESTONES):
            next_ms = MILESTONES[idx + 1]
            self.state.milestone_history.append(current)
            self.state.current_milestone = next_ms
            self._trace("advance_milestone", {
                "from": current,
                "to": next_ms,
            })
            if self._metrics:
                self._metrics.record_milestone(next_ms)

    def get_next_agent(self) -> str | None:
        """Determine which sub-agent to call next.

        Returns None if the pipeline is complete or waiting for human input.
        """
        if self.is_complete():
            return None
        if self.state.status == "waiting_for_human":
            return None
        return MILESTONE_AGENTS.get(self.state.current_milestone)

    def get_current_milestone(self) -> str:
        """Get the current milestone name."""
        return self.state.current_milestone

    def is_complete(self) -> bool:
        """Check if all milestones have been reached."""
        return self.state.current_milestone == MILESTONES[-1] and (
            MILESTONE_CONDITIONS.get(MILESTONES[-1], lambda _: False)(self.state)
        )

    def is_hitl_node(self) -> bool:
        """Check if the current milestone is a HITL (human-in-the-loop) gate.

        Dynamically discovers HITL milestones from pipeline tool class attributes
        on first call, so tools that declare ``human_review = True`` are
        automatically treated as HITL gates.
        """
        self._ensure_hitl_discovered()
        return self.state.current_milestone in HITL_MILESTONES

    @staticmethod
    def _ensure_hitl_discovered() -> None:
        """Lazily scan pipeline tools for human_review=True on first use."""
        if StateGraphEngine._hitl_discovered:
            return
        StateGraphEngine._hitl_discovered = True
        try:
            from auto_cut_bot.agent.tools.loader import ToolLoader

            loader = ToolLoader()
            for tool_cls in loader.discover():
                if getattr(tool_cls, "human_review", False):
                    name = getattr(tool_cls, "name", "")
                    if name and name not in HITL_MILESTONES:
                        HITL_MILESTONES.add(name)
        except Exception:
            pass

    _hitl_discovered: bool = False

    # ── HITL (Human-in-the-Loop) ────────────────────────────────────────

    async def interrupt(self, reason: str, data: dict[str, Any] | None = None) -> AgentState:
        """HITL interrupt: checkpoint, set waiting_for_human, release.

        Called when the engine reaches a human-review gate. The caller
        should persist the state and present the interrupt to the user.
        """
        self.state.status = "waiting_for_human"
        self.state.interrupt_reason = reason
        self.state.interrupt_data = data or {}
        self._trace("interrupt", {"reason": reason})
        await self.checkpoint("waiting_for_human", reason)
        return self.state

    async def resume(self, decision: dict[str, Any]) -> AgentState:
        """Resume from HITL interrupt with a human decision.

        The *decision* dict should contain at minimum an "approved" key
        (bool). Additional keys are stored in ``human_decision`` for
        downstream agents to consume.
        """
        if self.state.status != "waiting_for_human":
            self._trace("resume_skip", {
                "reason": f"status is {self.state.status}, not waiting_for_human",
            })
            return self.state

        self.state.status = "running"
        self.state.human_decision = decision
        self.state.interrupt_reason = None
        self.state.interrupt_data = None
        self._trace("resume", {"decision": decision})

        await self.checkpoint("running", "resume")

        # After resume, re-run to process any satisfied milestones.
        return await self.run()

    # ── Checkpoint ──────────────────────────────────────────────────────

    async def checkpoint(self, status: str, node_name: str) -> None:
        """Write a checkpoint if a checkpointer is configured."""
        if self.checkpointer is not None:
            await self.checkpointer.save(self.state, status, node_name)

    # ── Internal helpers ────────────────────────────────────────────────

    async def _advance_satisfied_milestones(self) -> None:
        """Advance through all milestones whose conditions are satisfied.

        Called after applying a result or on initial run, so that
        milestones that are already satisfied (e.g., from a previous
        session) are skipped without re-running their sub-agents.
        """
        while True:
            current = self.state.current_milestone
            condition = MILESTONE_CONDITIONS.get(current)
            if condition is None:
                break
            if not condition(self.state):
                break
            # HITL gate: don't auto-advance without human decision.
            if current in HITL_MILESTONES and self.state.human_decision is None:
                break
            try:
                idx = MILESTONES.index(current)
            except ValueError:
                break
            if idx + 1 >= len(MILESTONES):
                break
            next_ms = MILESTONES[idx + 1]
            self.state.milestone_history.append(current)
            self.state.current_milestone = next_ms
            self._trace("advance_milestone", {
                "from": current,
                "to": next_ms,
                "auto": True,
            })
            if self._metrics:
                self._metrics.record_milestone(next_ms)

    def _trace(self, event: str, detail: dict[str, Any]) -> None:
        """Append a trace entry to the execution log."""
        self.state.execution_trace.append({
            "timestamp": time.time(),
            "event": event,
            "milestone": self.state.current_milestone,
            "detail": detail,
        })


# ── Feature Flag ──────────────────────────────────────────────────────────

def use_agent_native_v2(config: dict[str, Any] | None = None) -> bool:
    """Check if agent-native V2 (StateGraph) is enabled.

    Priority:
      1. Explicit config dict ``{"use_agent_native_v2": true}``.
      2. Environment variable ``AUTO_CUT_BOT_V2=1``.
    """
    if config and config.get("use_agent_native_v2"):
        return True
    return os.environ.get("AUTO_CUT_BOT_V2", "") == "1"


# ── File I/O helpers ─────────────────────────────────────────────────────

def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write JSON to a temp file then atomically rename."""
    import json

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.rename(path)


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file, returning a dict."""
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _update_symlink(target: Path, link: Path) -> None:
    """Create or update a symlink to *target*."""
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target.name)