"""Pipeline state management — uses Agent Session instead of ArtifactBus.

Agent-native: pipeline state is stored in the agent's session metadata,
not in a separate ArtifactBus. Each tool reads/writes via the session.

DB integration: tools can optionally write to PostgreSQL via StageDBClient.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

PIPELINE_ORDER = [
    "source_windows", "source_transcripts", "window_analysis", "event_cards",
    "episode_digests", "chapter_digests", "series_registry",
    "series_assignment", "series_bible", "story_catalog",
    "story_portfolio", "story_treatments", "story_scripts",
    "story_preflight", "story_approval",
    "story_evidence", "span_candidates", "story_plans",
    "story_plans_materialize", "story_qc", "story_qc_review",
    "story_render",
]

HUMAN_REVIEW_STAGES = {"story_approval", "story_qc_review"}


def get_db_client(job_root: str) -> Any | None:
    """Get a StageDBClient if DB is configured, or None."""
    try:
        from autocut_core.config import PipelineConfig
        from autocut_core.db.client import StageDBClient

        config = PipelineConfig.resolve()
        if config.db_enabled:
            return StageDBClient(db_url=config.db_url, schema=config.db_schema)
    except Exception:
        pass
    return None


def pipeline_state_key(stage: str) -> str:
    return f"pipeline:{stage}"


def get_stage_state(context: Any, stage: str) -> dict[str, Any]:
    """Read a stage's saved state from the agent context."""
    key = pipeline_state_key(stage)
    raw = getattr(context, "attributes", {}).get(key, {})
    if isinstance(raw, dict):
        return raw
    return {}


def set_stage_state(context: Any, stage: str, state: dict[str, Any]) -> None:
    """Save stage state to the agent context."""
    key = pipeline_state_key(stage)
    if hasattr(context, "attributes"):
        context.attributes[key] = state


def validate_job_root(job_root: str) -> Path:
    """Validate and resolve a job_root path (prevents traversal)."""
    if not job_root:
        raise ValueError("job_root is required")
    p = Path(job_root).expanduser().resolve()
    if ".." in str(p):
        raise ValueError(f"Path traversal detected: {job_root}")
    return p


def get_upstream_artifact(
    context: Any, stage: str, artifact_name: str
) -> dict[str, Any] | None:
    """Get an artifact from an upstream stage's state."""
    state = get_stage_state(context, stage)
    artifacts = state.get("artifacts", {})
    return artifacts.get(artifact_name)


def mark_stage_complete(
    context: Any, stage: str, artifacts: dict[str, str]
) -> None:
    """Mark a stage as completed with its output artifacts."""
    set_stage_state(context, stage, {
        "status": "completed",
        "artifacts": artifacts,
    })


def mark_stage_failed(context: Any, stage: str, error: str) -> None:
    """Mark a stage as failed."""
    set_stage_state(context, stage, {
        "status": "failed",
        "error": error,
    })