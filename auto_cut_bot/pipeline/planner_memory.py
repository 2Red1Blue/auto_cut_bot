"""Planner Memory — Phase 2b: Goal-driven planner with playbook evolution.

Tracks the current pipeline goal, selects the best playbook for the current
milestone, records outcomes to evolve playbook effectiveness over time, and
determines the next action (which agent to call with which goal).

Key components:
  1. Goal — a named objective tied to a milestone, with priority and dependencies
  2. Playbook — a versioned sequence of actions with a success-rate history
  3. MILESTONE_GOALS — predefined goal inventory for each pipeline milestone
  4. PlannerMemory — selects, records, and evolves playbooks
  5. get_next_action — convenience function that returns the next agent, goal, and playbook

Usage::

    from auto_cut_bot.pipeline.stategraph import AgentState
    from auto_cut_bot.pipeline.planner_memory import PlannerMemory, get_next_action

    planner = PlannerMemory()
    action = get_next_action(state, planner)
    # action == {"agent": "source_agent", "goal": "source_ready", "playbook": Playbook(...)}
"""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


# ── Goal dataclass ────────────────────────────────────────────────────────────────


@dataclass
class Goal:
    """A named objective tied to a pipeline milestone.

    Attributes:
        goal_id: Unique identifier, e.g. "source_ready_v1".
        description: Human-readable goal description.
        milestone_target: Milestone name this goal maps to (matches AgentState.current_milestone).
        priority: Lower = higher priority. 0 = critical, 1 = high, 2 = normal.
        dependencies: Goal IDs that must be completed before this one.
    """

    goal_id: str
    description: str
    milestone_target: str
    priority: int = 2
    dependencies: list[str] = field(default_factory=list)

    def is_blocked(self, completed_goals: set[str]) -> bool:
        """Return True if any dependency has not yet been completed."""
        return not set(self.dependencies).issubset(completed_goals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "description": self.description,
            "milestone_target": self.milestone_target,
            "priority": self.priority,
            "dependencies": list(self.dependencies),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Goal:
        return cls(
            goal_id=data["goal_id"],
            description=data.get("description", ""),
            milestone_target=data.get("milestone_target", ""),
            priority=data.get("priority", 2),
            dependencies=list(data.get("dependencies", [])),
        )


# ── Playbook dataclass ───────────────────────────────────────────────────────────


@dataclass
class Playbook:
    """A versioned, evolvable sequence of actions for achieving a goal.

    Attributes:
        name: Human-readable name, e.g. "source_analysis_default".
        version: Integer version number, incremented on evolution.
        steps: Ordered list of actions; each action is a dict with keys
               like {"tool": str, "prompt": str, "params": dict}.
        success_rate: Float in [0, 1]; updated by record_outcome.
        total_runs: Number of times this playbook has been executed.
        last_used: Unix timestamp of the most recent execution.
        derived_from: If this playbook evolved from a parent, that parent's name.
    """

    name: str
    version: int = 1
    steps: list[dict[str, Any]] = field(default_factory=list)
    success_rate: float = 0.5
    total_runs: int = 0
    last_used: float = 0.0
    derived_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "steps": [dict(s) for s in self.steps],
            "success_rate": self.success_rate,
            "total_runs": self.total_runs,
            "last_used": self.last_used,
            "derived_from": self.derived_from,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Playbook:
        return cls(
            name=data["name"],
            version=data.get("version", 1),
            steps=list(data.get("steps", [])),
            success_rate=data.get("success_rate", 0.5),
            total_runs=data.get("total_runs", 0),
            last_used=data.get("last_used", 0.0),
            derived_from=data.get("derived_from"),
        )


# ── MILESTONE_GOALS — predefined goal inventory ─────────────────────────────────


MILESTONE_GOALS: dict[str, Goal] = {
    "source_ready": Goal(
        goal_id="source_ready",
        description="Analyse source material (video, transcripts, metadata) into structured event cards.",
        milestone_target="source_ready",
        priority=0,
    ),
    "bible_ready": Goal(
        goal_id="bible_ready",
        description="Build the series bible: characters, world, tone, genre, and narrative arcs.",
        milestone_target="bible_ready",
        priority=0,
        dependencies=["source_ready"],
    ),
    "script_approved": Goal(
        goal_id="script_approved",
        description="Generate a story treatment and script, then submit for human review.",
        milestone_target="script_approved",
        priority=0,
        dependencies=["bible_ready"],
    ),
    "rendered": Goal(
        goal_id="rendered",
        description="Render the approved script into final video output.",
        milestone_target="rendered",
        priority=0,
        dependencies=["script_approved"],
    ),
}


# ── Default playbook templates ───────────────────────────────────────────────────


_DEFAULT_PLAYBOOKS: dict[str, Playbook] = {
    "source_ready": Playbook(
        name="source_analysis_default",
        steps=[
            {"tool": "source_agent", "prompt": "Extract video segments and transcribe audio.", "params": {}},
            {"tool": "source_agent", "prompt": "Analyse scenes and generate event cards.", "params": {}},
        ],
    ),
    "bible_ready": Playbook(
        name="bible_default",
        steps=[
            {"tool": "story_agent", "prompt": "Build character registry from event cards.", "params": {}},
            {"tool": "story_agent", "prompt": "Define world, tone, and narrative arcs.", "params": {}},
        ],
    ),
    "script_approved": Playbook(
        name="script_default",
        steps=[
            {"tool": "story_agent", "prompt": "Generate story treatment.", "params": {}},
            {"tool": "story_agent", "prompt": "Write script from treatment.", "params": {}},
            {"tool": "human_review", "prompt": "Submit script for human approval.", "params": {}},
        ],
    ),
    "rendered": Playbook(
        name="render_default",
        steps=[
            {"tool": "production_agent", "prompt": "Render final video output.", "params": {}},
        ],
    ),
}


# ── PlannerMemory ────────────────────────────────────────────────────────────────


class PlannerMemory:
    """Goal-driven planner that selects, records, and evolves playbooks.

    The planner maintains a registry of playbooks keyed by milestone name.
    When a milestone is targeted, ``select_playbook`` returns the best
    playbook (highest success_rate, breaking ties with recency).  After
    execution, ``record_outcome`` updates the playbook's success rate using
    an exponential moving average.  ``evolve_playbook`` creates a new
    version of a playbook incorporating feedback.

    Attributes:
        playbooks: Mapping from milestone name to list of Playbook candidates.
        completed_goals: Set of goal IDs that have been completed.
    """

    def __init__(
        self,
        playbooks: dict[str, list[Playbook]] | None = None,
        completed_goals: set[str] | None = None,
    ) -> None:
        self.playbooks: dict[str, list[Playbook]] = playbooks or {}
        self.completed_goals: set[str] = completed_goals or set()

        # Seed default playbooks for each milestone if none were provided.
        for milestone, default_pb in _DEFAULT_PLAYBOOKS.items():
            if milestone not in self.playbooks:
                self.playbooks[milestone] = [deepcopy(default_pb)]

    # ── Playbook selection ─────────────────────────────────────────────────────

    def select_playbook(self, state: Any) -> Playbook | None:
        """Select the best playbook for the current milestone.

        Matches the current milestone to the playbook with the highest
        success_rate.  Ties are broken by recency (prefer the most recently
        used).  Returns None if no playbook is registered for this milestone.

        Args:
            state: An AgentState whose ``current_milestone`` drives selection.
        """
        milestone = getattr(state, "current_milestone", None)
        if milestone is None:
            return None

        candidates = self.playbooks.get(milestone, [])
        if not candidates:
            return None

        # Sort by success_rate descending, then last_used descending.
        candidates.sort(key=lambda pb: (pb.success_rate, pb.last_used), reverse=True)
        return candidates[0]

    def select_playbook_for_goal(self, goal: Goal) -> Playbook | None:
        """Select the best playbook for a specific Goal.

        Like ``select_playbook`` but driven by a Goal instance rather than
        the current state's milestone.
        """
        candidates = self.playbooks.get(goal.milestone_target, [])
        if not candidates:
            return None
        candidates.sort(key=lambda pb: (pb.success_rate, pb.last_used), reverse=True)
        return candidates[0]

    # ── Outcome recording ──────────────────────────────────────────────────────

    def record_outcome(
        self,
        playbook: Playbook,
        success: bool,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """Update a playbook's success rate after execution.

        Uses an exponential moving average (EMA) with alpha = 0.3 so that
        recent outcomes have more weight than historical ones.  Also
        increments ``total_runs`` and sets ``last_used``.

        Args:
            playbook: The playbook that was executed.
            success: Whether the execution achieved its goal.
            metrics: Optional dict of execution metrics (latency, tokens, etc.).
        """
        alpha = 0.3
        outcome = 1.0 if success else 0.0
        playbook.success_rate = alpha * outcome + (1 - alpha) * playbook.success_rate
        playbook.total_runs += 1
        playbook.last_used = time.time()

        if success:
            milestone = self._milestone_for_playbook(playbook)
            if milestone:
                goal = MILESTONE_GOALS.get(milestone)
                if goal:
                    self.completed_goals.add(goal.goal_id)

    # ── Playbook evolution ─────────────────────────────────────────────────────

    def evolve_playbook(self, playbook: Playbook, feedback: str) -> Playbook:
        """Create a new version of a playbook incorporating feedback.

        The new playbook inherits the parent's steps but gets a new version
        number, a reset success_rate (0.5), and records the parent as
        ``derived_from``.  The caller is expected to modify the new
        playbook's steps based on the feedback content.

        Args:
            playbook: The parent playbook to evolve from.
            feedback: Human-readable feedback describing what to improve.

        Returns:
            A new Playbook instance with version incremented.
        """
        new_playbook = Playbook(
            name=playbook.name,
            version=playbook.version + 1,
            steps=deepcopy(playbook.steps),
            success_rate=0.5,
            derived_from=f"{playbook.name}@{playbook.version}",
        )

        # Register the new version in the playbook registry.
        milestone = self._milestone_for_playbook(playbook)
        if milestone:
            if milestone not in self.playbooks:
                self.playbooks[milestone] = []
            self.playbooks[milestone].append(new_playbook)

        return new_playbook

    # ── Registration ───────────────────────────────────────────────────────────

    def register_playbook(self, milestone: str, playbook: Playbook) -> None:
        """Register a playbook for a given milestone.

        If the milestone has no playbooks yet, seeds the list.  Otherwise
        appends.  Does not deduplicate — callers should ensure uniqueness.
        """
        if milestone not in self.playbooks:
            self.playbooks[milestone] = []
        self.playbooks[milestone].append(playbook)

    def register_goal(self, goal: Goal) -> None:
        """Register a goal in the global MILESTONE_GOALS table."""
        MILESTONE_GOALS[goal.goal_id] = goal

    # ── Serialization ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "playbooks": {
                milestone: [pb.to_dict() for pb in pbs]
                for milestone, pbs in self.playbooks.items()
            },
            "completed_goals": sorted(self.completed_goals),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlannerMemory:
        playbooks: dict[str, list[Playbook]] = {}
        for milestone, pb_list in data.get("playbooks", {}).items():
            playbooks[milestone] = [Playbook.from_dict(pb) for pb in pb_list]
        completed = set(data.get("completed_goals", []))
        return cls(playbooks=playbooks, completed_goals=completed)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _milestone_for_playbook(self, playbook: Playbook) -> str | None:
        """Find which milestone a playbook is registered under."""
        for milestone, pbs in self.playbooks.items():
            if playbook in pbs:
                return milestone
        return None


# ── Convenience function ─────────────────────────────────────────────────────────


def get_next_action(
    state: Any,
    planner: PlannerMemory | None = None,
) -> dict[str, Any]:
    """Determine the next action (agent, goal, playbook) for the current state.

    This is the primary entry point for the caller (API handler or CLI).
    It looks up the current milestone, finds the matching goal, checks
    dependencies, selects the best playbook, and returns a dict the caller
    can use to invoke the correct sub-agent.

    Args:
        state: An AgentState with ``current_milestone`` set.
        planner: Optional PlannerMemory instance.  If None, a fresh one is created.

    Returns:
        A dict with keys:
          - agent: str — which sub-agent to invoke (or None if blocked/complete)
          - goal: str — goal ID for the current milestone
          - playbook: Playbook or None — the selected playbook
          - blocked: bool — True if dependencies are not yet satisfied
    """
    if planner is None:
        planner = PlannerMemory()

    milestone = getattr(state, "current_milestone", None)
    if milestone is None:
        return {"agent": None, "goal": None, "playbook": None, "blocked": False}

    goal = MILESTONE_GOALS.get(milestone)
    if goal is None:
        return {"agent": None, "goal": None, "playbook": None, "blocked": False}

    if goal.is_blocked(planner.completed_goals):
        return {"agent": None, "goal": goal.goal_id, "playbook": None, "blocked": True}

    playbook = planner.select_playbook(state)

    from auto_cut_bot.pipeline.stategraph import MILESTONE_AGENTS

    agent = MILESTONE_AGENTS.get(milestone)

    return {
        "agent": agent,
        "goal": goal.goal_id,
        "playbook": playbook,
        "blocked": False,
    }