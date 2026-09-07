"""Context snapshot export/import for the narrative shadow experiment.

The shadow payload freezes everything needed to rebuild the Stage 2 compact
context deterministically without a Store: input binding, alias map, narrative
graph, policies and the model view. Real runs export this from committed
Stage 1 artifacts on the experiment machine; tests export it from the kernel's
synthetic fixtures. Import never trusts the payload's hashes — it rebuilds the
context and the adapter recomputes canonical_hash itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autocut_kernel.semantic_chain.candidate_catalog import CandidateCatalogPolicy
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.narrative_models import NarrativeGraph
from autocut_kernel.semantic_chain.story_design_compact_context import StoryDesignCompactContext
from autocut_kernel.semantic_chain.story_design_models import JobPolicy, StoryDesignPolicy

SHADOW_PAYLOAD_SCHEMA = "narrative-shadow-fixture-v1"


def export_shadow_payload(
    context: StoryDesignCompactContext, draft_policy: Any
) -> dict[str, Any]:
    """Serialize a compact context plus the Stage 2 draft policy into the
    frozen shadow payload format. The draft policy is required for both the
    response schema and the kernel's strict decode."""
    return {
        "schema_version": SHADOW_PAYLOAD_SCHEMA,
        "input_binding_sha256": context.input_binding_sha256,
        "aliases": [
            {"alias": alias, "reference": ref.to_mapping()} for alias, ref in context.aliases
        ],
        "graph": context.graph.to_mapping(),
        "graph_owner": context.graph_owner.to_mapping(),
        "granted_sources": [ref.to_mapping() for ref in context.granted_sources],
        "job_policy": context.job_policy.to_mapping(),
        "story_policy": context.story_policy.to_mapping(),
        "candidate_policy": context.candidate_policy.to_mapping(),
        "draft_policy": draft_policy.to_mapping(),
        # keep the raw string: model view serialization must roundtrip byte-exact
        "model_view_json": context.model_view_json,
    }


def write_shadow_payload(context: StoryDesignCompactContext, path: Path,
                         draft_policy: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(export_shadow_payload(context, draft_policy), ensure_ascii=False,
                   indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def rebuild_context(payload: dict[str, Any]) -> StoryDesignCompactContext:
    """Inverse of export; used by tests and audit tooling."""
    return StoryDesignCompactContext(
        input_binding_sha256=payload["input_binding_sha256"],
        aliases=tuple(
            (entry["alias"], SemanticObjectRef.from_mapping(entry["reference"]))
            for entry in payload["aliases"]
        ),
        graph=NarrativeGraph.from_mapping(payload["graph"]),
        graph_owner=SemanticMemberIdentity.from_mapping(payload["graph_owner"]),
        granted_sources=tuple(SemanticObjectRef.from_mapping(ref) for ref in payload["granted_sources"]),
        job_policy=JobPolicy.from_mapping(payload["job_policy"]),
        story_policy=StoryDesignPolicy.from_mapping(payload["story_policy"]),
        candidate_policy=CandidateCatalogPolicy.from_mapping(payload["candidate_policy"]),
        model_view_json=payload["model_view_json"],
    )
