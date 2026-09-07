"""media_inspect: read-only run snapshot for the agent (R3).

Returns a bounded, safe digest: statuses, refs and revision — never artifact
payloads, blobs or credentials.
"""

from __future__ import annotations

import json
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.media_workflow._codec import load_json_strict
from auto_cut_bot.agent.tools.media_workflow.capability_catalog import build_default_catalog
from auto_cut_bot.agent.tools.media_workflow.service_port import (
    RuntimeUnavailableError,
    require_service_port,
    snapshot_digest,
)
from auto_cut_bot.agent.tools.schema import StringSchema, tool_parameters_schema


@tool_parameters(tool_parameters_schema(
    run_id=StringSchema("Pipeline run id (pipeline_run_...) returned by media_plan or the HTTP API."),
    required=["run_id"],
    additional_properties=False,
))
class MediaInspectTool(Tool):
    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "media_inspect"

    @property
    def description(self) -> str:
        return (
            "Read the exact pipeline run snapshot: revision, per-stage status, "
            "artifact/receipt refs and which media workflow capabilities are "
            "available, waiting or blocked. Read-only."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        run_id = str(kwargs["run_id"])
        try:
            port = require_service_port()
        except RuntimeUnavailableError as exc:
            return ToolResult.error(str(exc))
        snapshot = port.status(run_id)
        if snapshot is None:
            return ToolResult.error(f"run not found: {run_id}")
        digest = snapshot_digest(snapshot)
        catalog = build_default_catalog()
        return ToolResult(json.dumps({
            "snapshot": digest,
            "capability_catalog": catalog.to_mapping(),
        }, sort_keys=True, ensure_ascii=False))


def parse_snapshot_json(text: str) -> dict[str, Any]:
    """Strict parse helper for tests and callers that round-trip digests."""
    result = load_json_strict(text)
    if not isinstance(result, dict):
        raise ValueError("snapshot json must be an object")
    return result
