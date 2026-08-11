"""DomainAgentContract — defines the contract for a domain sub-agent.

Each domain agent declares its contract: what stages it owns, what artifacts
it consumes and produces, its milestone, and whether it includes a HITL gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DomainAgentContract:
    """Immutable contract declaring a domain agent's responsibilities.

    Used by the StateGraph engine (Phase 3) to validate the graph topology
    and by the DomainRegistry (Phase 2) to discover agent capabilities.
    """

    agent_name: str
    """Unique name of the domain agent (e.g. 'source_agent')."""

    stage_names: tuple[str, ...]
    """Names of the pipeline stages this agent owns, in execution order."""

    input_artifacts: tuple[str, ...] = ()
    """Artifact names this agent requires as input."""

    output_artifacts: tuple[str, ...] = ()
    """Artifact names this agent produces as output."""

    milestone: str = ""
    """Milestone reached when this agent completes (e.g. 'source_ready')."""

    is_human_node: bool = False
    """Whether this agent includes a HITL gate (requires human approval)."""

    skill_names: tuple[str, ...] = ()
    """Skill directory names injected into the sub-agent's context."""

    description: str = ""
    """Human-readable description of the agent's responsibilities."""


# ── Pre-defined contracts for the three domain agents ──────────────────────────

SOURCE_AGENT_CONTRACT = DomainAgentContract(
    agent_name="source_agent",
    stage_names=(
        "source_windows", "source_metadata", "source_script_load",
        "source_script_save", "source_script_chunk_parse",
        "source_transcripts", "reconciliation",
        "window_analysis", "event_cards",
        "episode_digests", "chapter_digests",
        "series_registry", "series_assignment",
    ),
    input_artifacts=(),
    output_artifacts=("source_script", "source_metadata", "event_cards",
                      "episode_digests", "chapter_digests", "series_registry"),
    milestone="source_ready",
    is_human_node=False,
    skill_names=("ac_source_prep",),
    description="Source material preparation: ingestion, script parsing, "
                "VLM analysis, metadata, ASR transcription, series registration.",
)

STORY_AGENT_CONTRACT = DomainAgentContract(
    agent_name="story_agent",
    stage_names=(
        "series_bible", "story_catalog", "story_portfolio",
        "story_treatments", "story_scripts", "story_preflight",
        "story_approval", "story_evidence", "span_candidates",
        "story_plans", "story_plans_materialize",
        "story_plans_preflight", "story_plans_qc_admission",
    ),
    input_artifacts=("source_script", "event_cards", "series_registry"),
    output_artifacts=("story_scripts", "story_plans", "series_bible"),
    milestone="script_approved",
    is_human_node=True,
    skill_names=("ac_story_generation",),
    description="Story generation: bible, character extraction, plot structure, "
                "script generation, HITL approval gates.",
)

PRODUCTION_AGENT_CONTRACT = DomainAgentContract(
    agent_name="production_agent",
    stage_names=(
        "story_qc", "story_qc_review", "story_render",
    ),
    input_artifacts=("story_scripts", "story_plans"),
    output_artifacts=("rendered_video",),
    milestone="rendered",
    is_human_node=True,
    skill_names=("ac_qc", "ac_render"),
    description="Production: QC checks, human review, final rendering.",
)
