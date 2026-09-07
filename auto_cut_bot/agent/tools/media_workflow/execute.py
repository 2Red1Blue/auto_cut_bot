"""media_execute: checked plan -> existing runtime operations (R3).

Conservative first enablement (doc task 4): execute maps ready plan nodes onto
the runtime's own CAS resume operation, reusing the HTTP runtime — the agent
never creates a second business state. Stage-specific recompute entry points
and the CreateMediaExecutionPlanCommand persistence wiring land with the
runtime integration; unsupported nodes are denied with structured reasons,
never silently skipped. Disabled by default until integration passes.
"""

from __future__ import annotations

import json
from typing import Any

from auto_cut_bot.agent.tools.base import Tool, ToolResult, tool_parameters
from auto_cut_bot.agent.tools.media_workflow._codec import load_json_strict
from auto_cut_bot.agent.tools.media_workflow.capability_catalog import build_default_catalog
from auto_cut_bot.agent.tools.media_workflow.contracts import ResolvedMediaPlan
from auto_cut_bot.agent.tools.media_workflow.service_port import (
    RuntimeUnavailableError,
    require_service_port,
    snapshot_digest,
)
from auto_cut_bot.agent.tools.schema import (
    ArraySchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)


@tool_parameters(tool_parameters_schema(
    plan=StringSchema("ResolvedMediaPlan/v1 JSON previously returned by media_plan."),
    expected_revision=IntegerSchema(description="Snapshot revision the plan was built from (CAS check)."),
    node_ids=ArraySchema(StringSchema(""), description="Optional subset of node ids to execute; defaults to all non-denied nodes."),
    required=["plan", "expected_revision"],
    additional_properties=False,
))
class MediaExecuteTool(Tool):
    _scopes = {"core"}
    config_key = "media_workflow"

    @property
    def name(self) -> str:
        return "media_execute"

    @property
    def description(self) -> str:
        return (
            "Execute a checked media plan against the pipeline runtime. Refuses "
            "stale revisions, drifted capability catalogs and unavailable "
            "capabilities; `succeeded` means the target nodes finished, not that "
            "a local render or any publication succeeded."
        )

    @classmethod
    def config_cls(cls) -> type | None:
        from auto_cut_bot.agent.tools.media_workflow.config import MediaWorkflowToolConfig

        return MediaWorkflowToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        cfg = getattr(ctx.config, "media_workflow", None)
        return bool(cfg and cfg.enabled and cfg.execute_enabled)

    async def execute(self, **kwargs: Any) -> Any:
        try:
            plan = ResolvedMediaPlan.from_mapping(load_json_strict(str(kwargs["plan"])))
        except ValueError as exc:
            return ToolResult.error(f"invalid plan: {exc}")
        expected = int(kwargs["expected_revision"])
        if plan.base_revision != expected:
            return ToolResult.error(json.dumps({
                "code": "STALE_REVISION",
                "plan_base_revision": plan.base_revision,
                "expected_revision": expected,
            }, sort_keys=True))
        catalog = build_default_catalog()
        if plan.capability_catalog_hash != catalog.catalog_hash:
            return ToolResult.error(json.dumps({
                "code": "CAPABILITY_CATALOG_DRIFT",
                "plan_catalog_hash": plan.capability_catalog_hash,
                "current_catalog_hash": catalog.catalog_hash,
            }, sort_keys=True))
        node_ids = kwargs.get("node_ids") or [n.node_id for n in plan.nodes]
        unknown = set(node_ids) - {n.node_id for n in plan.nodes}
        if unknown:
            return ToolResult.error(f"unknown node ids: {sorted(unknown)}")
        try:
            port = require_service_port()
        except RuntimeUnavailableError as exc:
            return ToolResult.error(str(exc))
        # CAS resume of the runtime run; the runtime owns scheduling, receipts
        # and provider calls — the tool never issues commands itself.
        operation_key = f"resume:{plan.job_ref}:{expected}"
        try:
            snapshot = port.resume(plan.job_ref, expected)
        except Exception as exc:  # CAS conflict, unknown run, store failure
            return ToolResult.error(json.dumps({
                "code": "RESUME_FAILED",
                "operation_key": operation_key,
                "detail": str(exc)[:300],
                "recovery": "re-run media_inspect, re-plan at the current revision, then re-execute",
            }, sort_keys=True))
        digest = snapshot_digest(snapshot)
        results: list[dict[str, Any]] = []
        for node in plan.nodes:
            if node.node_id not in node_ids:
                continue
            descriptor = catalog.get(node.capability_id)
            if node.status == "denied" or descriptor is None or descriptor.status == "unavailable":
                results.append({
                    "node_id": node.node_id, "status": "denied",
                    "reason": (node.note or (descriptor.unavailable_reason if descriptor else None)),
                })
            elif node.status == "succeeded":
                results.append({"node_id": node.node_id, "status": "reused"})
            else:
                results.append({
                    "node_id": node.node_id, "status": "scheduled",
                    "operation_key": operation_key,
                })
        return ToolResult(json.dumps({
            "operation_key": operation_key,
            "snapshot": digest,
            "nodes": results,
            "note": "succeeded/scheduled refers to these nodes only; no render or "
                    "publication claim is made",
        }, sort_keys=True, ensure_ascii=False))
