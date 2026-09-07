"""Intent -> ResolvedMediaPlan resolver (program logic; no model decisions)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from auto_cut_bot.agent.tools.media_workflow.contracts import (
    GOAL_CAPABILITY_CHAINS,
    CapabilityCatalog,
    MediaIntentDraft,
    PlanNode,
    ResolvedMediaPlan,
)

# capability_id -> the control-plane stage name that reports its status
CAPABILITY_STAGE: dict[str, str] = {
    "source_prepare": "source_prep",
    "media_preflight": "media_preflight",
    "vlm_evidence": "vlm",
    "stage1_narrative_graph": "stage1",
    "stage2_story_portfolio": "stage2",
    "stage3_editorial_blueprint": "stage3",
    "recipe_compile": "recipe",
}


@dataclass(frozen=True)
class PlanResolution:
    plan: ResolvedMediaPlan | None
    diagnostics: tuple[dict[str, str], ...]


def _succeeded_stages(snapshot_digest: dict[str, Any] | None) -> set[str]:
    if not snapshot_digest:
        return set()
    return {
        c.get("stage") for c in snapshot_digest.get("commands", [])
        if isinstance(c, dict) and c.get("status") == "succeeded" and c.get("stage")
    }


def resolve_plan(
    intent: MediaIntentDraft,
    catalog: CapabilityCatalog,
    *,
    job_ref: str,
    base_revision: int,
    snapshot_digest: dict[str, Any] | None = None,
) -> PlanResolution:
    """Build the DAG for the intent's goal capability chain.

    - missing prerequisites are added by walking the chain (dependency completion)
    - stages already succeeded on the snapshot become `succeeded` reuse nodes
    - unavailable capabilities become `denied` nodes with the registry reason
    - the program-built chain is topological by construction and asserted so
    """
    diagnostics: list[dict[str, str]] = []
    if intent.goal not in GOAL_CAPABILITY_CHAINS:
        return PlanResolution(None, ({"code": "UNKNOWN_GOAL", "goal": intent.goal},))
    succeeded = _succeeded_stages(snapshot_digest)
    chain = GOAL_CAPABILITY_CHAINS[intent.goal]
    nodes: list[PlanNode] = []
    previous: str | None = None
    for capability_id in chain:
        descriptor = catalog.get(capability_id)
        if descriptor is None:
            diagnostics.append({"code": "UNKNOWN_CAPABILITY", "capability_id": capability_id})
            return PlanResolution(None, tuple(diagnostics))
        status = "ready"
        note: str | None = None
        if descriptor.status == "unavailable":
            status = "denied"
            note = descriptor.unavailable_reason
        else:
            stage = CAPABILITY_STAGE.get(capability_id)
            if stage and stage in succeeded:
                status = "succeeded"
                note = "reused: stage already succeeded on this run"
        node_id = f"node-{len(nodes):02d}-{capability_id}"
        nodes.append(PlanNode(
            node_id=node_id, capability_id=capability_id, status=status,
            input_refs=(), input_links=(previous,) if previous else (),
            depends_on=(previous,) if previous else (), note=note,
        ))
        previous = node_id
    seen: set[str] = set()
    for node in nodes:
        assert set(node.depends_on) <= seen, "resolver produced a non-topological DAG"
        seen.add(node.node_id)
    plan = ResolvedMediaPlan(
        job_ref=job_ref, base_revision=base_revision,
        capability_catalog_hash=catalog.catalog_hash, goal=intent.goal,
        nodes=tuple(nodes),
    )
    return PlanResolution(plan, tuple(diagnostics))
