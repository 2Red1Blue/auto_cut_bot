"""Closed DTOs and strict codecs for the media workflow agent tools (R3).

Contract: docs/open-source-adoption-phases/03-r3-agent-tools.md.
The model never generates command names, hashes, SQL or physical endpoints —
it only emits a MediaIntentDraft; everything below is program-computed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from auto_cut_bot.agent.tools.media_workflow._codec import json_sha256

INTENT_SCHEMA = "MediaIntentDraft/v1"
PLAN_SCHEMA = "ResolvedMediaPlan/v1"
CAPABILITY_SCHEMA = "CapabilityDescriptor/v1"

GOALS = ("inspect", "analyze", "design", "render_local")
NODE_STATUSES = (
    "waiting", "ready", "running", "succeeded", "denied", "failed", "invalidated",
)
CAPABILITY_STATUSES = ("available", "unavailable")

# goals -> ordered capability chain (dependency order; resolved against the catalog)
GOAL_CAPABILITY_CHAINS: dict[str, tuple[str, ...]] = {
    "inspect": ("run_inspect",),
    "analyze": ("source_prepare", "media_preflight", "vlm_evidence"),
    "design": ("source_prepare", "media_preflight", "vlm_evidence",
               "stage1_narrative_graph", "stage2_story_portfolio"),
    "render_local": ("source_prepare", "media_preflight", "vlm_evidence",
                     "stage1_narrative_graph", "stage2_story_portfolio",
                     "stage3_editorial_blueprint", "recipe_compile",
                     "local_render", "local_qc"),
}


def _require_mapping(obj: Any, what: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError(f"{what}: expected object")
    return obj


def _reject_unknown(obj: dict[str, Any], allowed: set[str], what: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ValueError(f"{what}: unknown keys {unknown}")


def _require_str(obj: dict[str, Any], key: str, what: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what}.{key}: required string")
    return value


# ---------------------------------------------------------------------------
# Capability catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityDescriptor:
    capability_id: str
    version: str
    input_artifact_types: tuple[str, ...]
    output_artifact_types: tuple[str, ...]
    depends_on: tuple[str, ...]
    provider_calling: bool
    parallel_dimension: str | None
    recovery: str  # "resume_same_key" | "local_reprocess" | "manual"
    status: str
    unavailable_reason: str | None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": CAPABILITY_SCHEMA,
            "capability_id": self.capability_id,
            "version": self.version,
            "input_artifact_types": list(self.input_artifact_types),
            "output_artifact_types": list(self.output_artifact_types),
            "depends_on": list(self.depends_on),
            "provider_calling": self.provider_calling,
            "parallel_dimension": self.parallel_dimension,
            "recovery": self.recovery,
            "status": self.status,
            "unavailable_reason": self.unavailable_reason,
        }

    @classmethod
    def from_mapping(cls, obj: Any) -> CapabilityDescriptor:
        data = _require_mapping(obj, "capability")
        _reject_unknown(data, {
            "schema_version", "capability_id", "version", "input_artifact_types",
            "output_artifact_types", "depends_on", "provider_calling",
            "parallel_dimension", "recovery", "status", "unavailable_reason",
        }, "capability")
        if data.get("schema_version") != CAPABILITY_SCHEMA:
            raise ValueError(f"capability.schema_version must be {CAPABILITY_SCHEMA}")
        status = data.get("status")
        if status not in CAPABILITY_STATUSES:
            raise ValueError(f"capability.status must be one of {CAPABILITY_STATUSES}")
        provider_calling = data.get("provider_calling")
        if not isinstance(provider_calling, bool):
            raise ValueError("capability.provider_calling: required boolean")
        return cls(
            capability_id=_require_str(data, "capability_id", "capability"),
            version=_require_str(data, "version", "capability"),
            input_artifact_types=tuple(data.get("input_artifact_types", ())),
            output_artifact_types=tuple(data.get("output_artifact_types", ())),
            depends_on=tuple(data.get("depends_on", ())),
            provider_calling=provider_calling,
            parallel_dimension=data.get("parallel_dimension"),
            recovery=_require_str(data, "recovery", "capability"),
            status=status,
            unavailable_reason=data.get("unavailable_reason"),
        )


@dataclass(frozen=True)
class CapabilityCatalog:
    descriptors: tuple[CapabilityDescriptor, ...]

    @property
    def catalog_hash(self) -> str:
        """Hash over the sorted descriptors: identity of the capability set."""
        payload = [d.to_mapping() for d in sorted(self.descriptors, key=lambda d: d.capability_id)]
        return json_sha256(payload)

    def get(self, capability_id: str) -> CapabilityDescriptor | None:
        return next((d for d in self.descriptors if d.capability_id == capability_id), None)

    def require(self, capability_id: str) -> CapabilityDescriptor:
        d = self.get(capability_id)
        if d is None:
            raise KeyError(f"unknown capability: {capability_id}")
        return d

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": "CapabilityCatalog/v1",
            "catalog_hash": self.catalog_hash,
            "descriptors": [d.to_mapping() for d in
                            sorted(self.descriptors, key=lambda d: d.capability_id)],
        }


# ---------------------------------------------------------------------------
# MediaIntentDraft/v1 (model-authored, closed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaIntentDraft:
    goal: str
    episode_aliases: tuple[str, ...] = ()
    story_aliases: tuple[str, ...] = ()
    preferences: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, obj: Any) -> MediaIntentDraft:
        data = _require_mapping(obj, "intent")
        _reject_unknown(data, {"schema_version", "goal", "episode_aliases",
                               "story_aliases", "preferences"}, "intent")
        if data.get("schema_version") != INTENT_SCHEMA:
            raise ValueError(f"intent.schema_version must be {INTENT_SCHEMA}")
        goal = _require_str(data, "goal", "intent")
        if goal not in GOALS:
            raise ValueError(f"intent.goal must be one of {GOALS}")
        for key in ("episode_aliases", "story_aliases"):
            value = data.get(key, [])
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ValueError(f"intent.{key}: list of strings")
        preferences = data.get("preferences", {})
        if not isinstance(preferences, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in preferences.items()
        ):
            raise ValueError("intent.preferences: string-to-string object")
        return cls(
            goal=goal,
            episode_aliases=tuple(data.get("episode_aliases", ())),
            story_aliases=tuple(data.get("story_aliases", ())),
            preferences=dict(preferences),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": INTENT_SCHEMA,
            "goal": self.goal,
            "episode_aliases": list(self.episode_aliases),
            "story_aliases": list(self.story_aliases),
            "preferences": dict(self.preferences),
        }


# ---------------------------------------------------------------------------
# ResolvedMediaPlan/v1 (program-authored)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanNode:
    node_id: str
    capability_id: str
    status: str
    input_refs: tuple[str, ...]
    input_links: tuple[str, ...]  # upstream node_ids; resolved to refs at execute time
    depends_on: tuple[str, ...]
    note: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "node_id": self.node_id,
            "capability_id": self.capability_id,
            "status": self.status,
            "input_refs": list(self.input_refs),
            "input_links": list(self.input_links),
            "depends_on": list(self.depends_on),
        }
        if self.note is not None:
            out["note"] = self.note
        return out

    @classmethod
    def from_mapping(cls, obj: Any) -> PlanNode:
        data = _require_mapping(obj, "node")
        _reject_unknown(data, {"node_id", "capability_id", "status", "input_refs",
                               "input_links", "depends_on", "note"}, "node")
        status = data.get("status")
        if status not in NODE_STATUSES:
            raise ValueError(f"node.status must be one of {NODE_STATUSES}")
        return cls(
            node_id=_require_str(data, "node_id", "node"),
            capability_id=_require_str(data, "capability_id", "node"),
            status=status,
            input_refs=tuple(data.get("input_refs", ())),
            input_links=tuple(data.get("input_links", ())),
            depends_on=tuple(data.get("depends_on", ())),
            note=data.get("note"),
        )


@dataclass(frozen=True)
class ResolvedMediaPlan:
    job_ref: str  # run/job identifier resolved by the program
    base_revision: int  # snapshot version the plan was built from
    capability_catalog_hash: str
    nodes: tuple[PlanNode, ...]
    goal: str

    @property
    def plan_hash(self) -> str:
        """Normalized business identity: no timestamps, no log fields."""
        return json_sha256({
            "schema_version": PLAN_SCHEMA,
            "job_ref": self.job_ref,
            "base_revision": self.base_revision,
            "capability_catalog_hash": self.capability_catalog_hash,
            "goal": self.goal,
            "nodes": [n.to_mapping() for n in self.nodes],
        })

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": PLAN_SCHEMA,
            "plan_hash": self.plan_hash,
            "job_ref": self.job_ref,
            "base_revision": self.base_revision,
            "capability_catalog_hash": self.capability_catalog_hash,
            "goal": self.goal,
            "nodes": [n.to_mapping() for n in self.nodes],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_mapping(), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_mapping(cls, obj: Any) -> ResolvedMediaPlan:
        data = _require_mapping(obj, "plan")
        _reject_unknown(data, {"schema_version", "plan_hash", "job_ref", "base_revision",
                               "capability_catalog_hash", "goal", "nodes"}, "plan")
        if data.get("schema_version") != PLAN_SCHEMA:
            raise ValueError(f"plan.schema_version must be {PLAN_SCHEMA}")
        nodes_raw = data.get("nodes")
        if not isinstance(nodes_raw, list) or not nodes_raw:
            raise ValueError("plan.nodes: required non-empty list")
        nodes = tuple(PlanNode.from_mapping(n) for n in nodes_raw)
        revision = data.get("base_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("plan.base_revision: positive integer")
        plan = cls(
            job_ref=_require_str(data, "job_ref", "plan"),
            base_revision=revision,
            capability_catalog_hash=_require_str(data, "capability_catalog_hash", "plan"),
            goal=_require_str(data, "goal", "plan"),
            nodes=nodes,
        )
        expected_hash = data.get("plan_hash")
        if not isinstance(expected_hash, str) or expected_hash != plan.plan_hash:
            raise ValueError("plan.plan_hash does not match plan content")
        # topological check: dependencies must precede dependents
        seen: set[str] = set()
        for node in nodes:
            if not set(node.depends_on) <= seen:
                raise ValueError(f"plan node {node.node_id}: dependency ordering violated")
            seen.add(node.node_id)
        return plan
