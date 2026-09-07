"""media_plan: MediaIntentDraft -> checked ResolvedMediaPlan (R3).

The model supplies only the closed intent; the program resolves the DAG,
reuses succeeded stages and reports structural diagnostics. Plans are pure
data — persisting them as Artifacts through the existing Store happens at
runtime composition (CreateMediaExecutionPlanCommand), not in chat.
"""

from __future__ import annotations

import json
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.media_workflow._codec import load_json_strict
from auto_cut_bot.agent.tools.media_workflow.capability_catalog import build_default_catalog
from auto_cut_bot.agent.tools.media_workflow.contracts import MediaIntentDraft
from auto_cut_bot.agent.tools.media_workflow.plan import resolve_plan
from auto_cut_bot.agent.tools.media_workflow.service_port import (
    RuntimeUnavailableError,
    require_service_port,
    snapshot_digest,
)
from auto_cut_bot.agent.tools.schema import (
    ArraySchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)


@tool_parameters(tool_parameters_schema(
    run_id=StringSchema("Pipeline run id to plan against (from media_inspect)."),
    goal=StringSchema("One of: inspect, analyze, design, render_local."),
    episode_aliases=ArraySchema(StringSchema(""), description="Optional episode aliases the user named."),
    story_aliases=ArraySchema(StringSchema(""), description="Optional story aliases the user named."),
    preferences=ObjectSchema(description="Optional user preferences (language, duration, style)."),
    required=["run_id", "goal"],
    additional_properties=False,
))
class MediaPlanTool(Tool):
    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "media_plan"

    @property
    def description(self) -> str:
        return (
            "Turn a user goal into a checked media execution plan (node DAG with "
            "dependency completion, reuse of succeeded stages and explicit denial "
            "of unavailable capabilities). Does not execute anything."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        try:
            intent = MediaIntentDraft.from_mapping({
                "schema_version": "MediaIntentDraft/v1",
                "goal": kwargs["goal"],
                "episode_aliases": kwargs.get("episode_aliases") or [],
                "story_aliases": kwargs.get("story_aliases") or [],
                "preferences": kwargs.get("preferences") or {},
            })
        except ValueError as exc:
            return ToolResult.error(f"invalid intent: {exc}")
        try:
            port = require_service_port()
        except RuntimeUnavailableError as exc:
            return ToolResult.error(str(exc))
        run_id = str(kwargs["run_id"])
        snapshot = port.status(run_id)
        if snapshot is None:
            return ToolResult.error(f"run not found: {run_id}")
        digest = snapshot_digest(snapshot)
        catalog = build_default_catalog()
        resolution = resolve_plan(
            intent, catalog, job_ref=run_id,
            base_revision=int(digest.get("version") or 1),
            snapshot_digest=digest,
        )
        if resolution.plan is None:
            return ToolResult(json.dumps({
                "plan": None, "diagnostics": list(resolution.diagnostics),
            }, sort_keys=True, ensure_ascii=False))
        return ToolResult(json.dumps({
            "plan": resolution.plan.to_mapping(),
            "diagnostics": list(resolution.diagnostics),
        }, sort_keys=True, ensure_ascii=False))


def parse_plan_json(text: str) -> dict[str, Any]:
    result = load_json_strict(text)
    if not isinstance(result, dict):
        raise ValueError("plan json must be an object")
    return result
