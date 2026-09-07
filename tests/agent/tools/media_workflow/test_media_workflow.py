"""R3 media workflow tool tests: contracts, catalog, resolver and the three
tools against a fake runtime port. No Postgres, no provider, no agent loop."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from auto_cut_bot.agent.tools.media_workflow._codec import load_json_strict  # noqa: F401
from auto_cut_bot.agent.tools.media_workflow.capability_catalog import build_default_catalog
from auto_cut_bot.agent.tools.media_workflow.contracts import (
    MediaIntentDraft,
    PlanNode,
    ResolvedMediaPlan,
    json_sha256,
)
from auto_cut_bot.agent.tools.media_workflow.execute import MediaExecuteTool
from auto_cut_bot.agent.tools.media_workflow.inspect import MediaInspectTool
from auto_cut_bot.agent.tools.media_workflow.plan import resolve_plan
from auto_cut_bot.agent.tools.media_workflow.plan_tool import MediaPlanTool


class TestContracts:
    def test_intent_closed(self) -> None:
        with pytest.raises(ValueError, match="unknown keys"):
            MediaIntentDraft.from_mapping({
                "schema_version": "MediaIntentDraft/v1", "goal": "design",
                "command": "rm -rf /",
            })
        with pytest.raises(ValueError, match="goal"):
            MediaIntentDraft.from_mapping({
                "schema_version": "MediaIntentDraft/v1", "goal": "publish_everything",
            })

    def test_catalog_hash_stable(self) -> None:
        assert build_default_catalog().catalog_hash == build_default_catalog().catalog_hash

    def test_plan_hash_binds_content(self) -> None:
        node = PlanNode(node_id="node-00-run_inspect", capability_id="run_inspect",
                        status="ready", input_refs=(), input_links=(), depends_on=())
        plan = ResolvedMediaPlan(job_ref="run-1", base_revision=3,
                                 capability_catalog_hash="abc", goal="inspect",
                                 nodes=(node,))
        other = ResolvedMediaPlan(job_ref="run-1", base_revision=4,
                                  capability_catalog_hash="abc", goal="inspect",
                                  nodes=(node,))
        assert plan.plan_hash != other.plan_hash
        restored = ResolvedMediaPlan.from_mapping(json.loads(json.dumps(plan.to_mapping())))
        assert restored.plan_hash == plan.plan_hash

    def test_plan_hash_mismatch_rejected(self) -> None:
        mapping = {
            "schema_version": "ResolvedMediaPlan/v1", "job_ref": "r", "base_revision": 1,
            "capability_catalog_hash": "abc", "goal": "inspect",
            "nodes": [{"node_id": "n1", "capability_id": "run_inspect", "status": "ready",
                       "input_refs": [], "input_links": [], "depends_on": []}],
        }
        mapping["plan_hash"] = json_sha256({"fake": True})
        with pytest.raises(ValueError, match="plan_hash"):
            ResolvedMediaPlan.from_mapping(mapping)

    def test_plan_topology_enforced(self) -> None:
        nodes = [
            {"node_id": "n2", "capability_id": "vlm_evidence", "status": "ready",
             "input_refs": [], "input_links": ["n1"], "depends_on": ["n1"]},
            {"node_id": "n1", "capability_id": "run_inspect", "status": "ready",
             "input_refs": [], "input_links": [], "depends_on": []},
        ]
        mapping: dict[str, Any] = {"schema_version": "ResolvedMediaPlan/v1", "job_ref": "r",
                                   "base_revision": 1, "capability_catalog_hash": "abc",
                                   "goal": "design", "nodes": nodes}
        mapping["plan_hash"] = json_sha256(mapping)
        with pytest.raises(ValueError, match="ordering"):
            ResolvedMediaPlan.from_mapping(mapping)


class TestCatalog:
    def test_unavailable_capabilities_have_reasons(self) -> None:
        catalog = build_default_catalog()
        for cap in ("local_render", "local_qc"):
            d = catalog.require(cap)
            assert d.status == "unavailable"
            assert d.unavailable_reason

    def test_goal_chains_resolve(self) -> None:
        catalog = build_default_catalog()
        for goal in ("inspect", "analyze", "design", "render_local"):
            resolution = resolve_plan(
                MediaIntentDraft(goal=goal), catalog,
                job_ref="run-1", base_revision=1)
            assert resolution.plan is not None, goal


class TestResolver:
    def test_dependency_completion_and_reuse(self) -> None:
        catalog = build_default_catalog()
        digest = {"version": 5, "commands": [
            {"stage": "vlm", "status": "succeeded"},
        ]}
        resolution = resolve_plan(
            MediaIntentDraft(goal="design"), catalog, job_ref="run-1",
            base_revision=5, snapshot_digest=digest)
        plan = resolution.plan
        assert plan is not None and plan.base_revision == 5
        by_cap = {n.capability_id: n for n in plan.nodes}
        assert by_cap["vlm_evidence"].status == "succeeded"
        assert by_cap["stage1_narrative_graph"].status == "ready"
        assert by_cap["stage1_narrative_graph"].depends_on == ("node-02-vlm_evidence",)

    def test_render_local_denies_unavailable_tail(self) -> None:
        catalog = build_default_catalog()
        resolution = resolve_plan(
            MediaIntentDraft(goal="render_local"), catalog,
            job_ref="run-1", base_revision=1)
        plan = resolution.plan
        assert plan is not None
        statuses = {n.capability_id: n.status for n in plan.nodes}
        assert statuses["local_render"] == "denied"
        assert statuses["local_qc"] == "denied"
        assert statuses["stage3_editorial_blueprint"] == "ready"


class _FakePort:
    def __init__(self, snapshot: dict | None) -> None:
        self.snapshot = snapshot
        self.resume_calls: list[tuple[str, int]] = []

    def status(self, run_id: str) -> dict | None:
        return self.snapshot

    def resume(self, run_id: str, expected_version: int) -> dict | None:
        self.resume_calls.append((run_id, expected_version))
        if self.snapshot is None:
            return None
        return {**self.snapshot, "version": expected_version + 1}


def _snapshot(version: int = 4) -> dict:
    return {
        "run_id": "pipeline_run_abc", "status": "running", "version": version,
        "request_hash": "hash-1", "execution_profile": "full",
        "commands": [{"command_id": "c1", "stage": "vlm", "status": "succeeded",
                      "receipt_id": "11111111-1111-1111-1111-111111111111",
                      "version": 2}],
    }


def _plan_for(base_revision: int, goal: str = "design",
              catalog_hash: str | None = None) -> dict:
    """A valid plan (or, with catalog_hash tampering, a re-signed valid plan
    whose catalog identity deliberately drifts)."""
    catalog = build_default_catalog()
    resolution = resolve_plan(
        MediaIntentDraft(goal=goal), catalog, job_ref="pipeline_run_abc",
        base_revision=base_revision)
    assert resolution.plan is not None
    mapping = resolution.plan.to_mapping()
    if catalog_hash is not None and catalog_hash != mapping["capability_catalog_hash"]:
        mapping["capability_catalog_hash"] = catalog_hash
        del mapping["plan_hash"]
        mapping["plan_hash"] = json_sha256(mapping)
    return mapping


def _patch_port(monkeypatch: pytest.MonkeyPatch, port: "_FakePort") -> None:
    """Patch the port resolver in every tool module (each holds its own
    `from ... import require_service_port` binding)."""
    for module in ("inspect", "plan_tool", "execute"):
        monkeypatch.setattr(
            f"auto_cut_bot.agent.tools.media_workflow.{module}.require_service_port",
            lambda *a: port,
        )


class TestTools:
    async def test_inspect_returns_digest_and_catalog(self, monkeypatch) -> None:
        _patch_port(monkeypatch, _FakePort(_snapshot()))
        result = await MediaInspectTool().execute(run_id="pipeline_run_abc")
        payload = json.loads(str(result))
        assert payload["snapshot"]["run_id"] == "pipeline_run_abc"
        assert payload["snapshot"]["commands"][0]["receipt_id"]  # ref kept
        assert set(payload["snapshot"]["commands"][0]) == {
            "command_id", "stage", "status", "receipt_id", "version"}  # whitelist only
        assert payload["capability_catalog"]["catalog_hash"]

    async def test_inspect_missing_run(self, monkeypatch) -> None:
        _patch_port(monkeypatch, _FakePort(None))
        result = await MediaInspectTool().execute(run_id="nope")
        assert getattr(result, "is_error", False)
        assert "run not found" in str(result)

    async def test_plan_resolves_with_reuse(self, monkeypatch) -> None:
        _patch_port(monkeypatch, _FakePort(_snapshot()))
        result = await MediaPlanTool().execute(run_id="pipeline_run_abc", goal="design")
        payload = json.loads(str(result))
        plan = payload["plan"]
        assert plan["base_revision"] == 4
        caps = {n["capability_id"]: n["status"] for n in plan["nodes"]}
        assert caps["vlm_evidence"] == "succeeded"
        assert caps["stage2_story_portfolio"] == "ready"

    async def test_plan_invalid_goal(self, monkeypatch) -> None:
        _patch_port(monkeypatch, _FakePort(_snapshot()))
        result = await MediaPlanTool().execute(run_id="pipeline_run_abc", goal="deploy")
        assert getattr(result, "is_error", False)

    def test_execute_disabled_by_default(self) -> None:
        off = SimpleNamespace(config=SimpleNamespace(media_workflow=SimpleNamespace(
            enabled=True, execute_enabled=False)))
        on = SimpleNamespace(config=SimpleNamespace(media_workflow=SimpleNamespace(
            enabled=True, execute_enabled=True)))
        assert MediaExecuteTool.enabled(off) is False
        assert MediaExecuteTool.enabled(on) is True

    async def test_execute_stale_revision_refused(self, monkeypatch) -> None:
        _patch_port(monkeypatch, _FakePort(_snapshot(9)))
        plan = _plan_for(base_revision=4, catalog_hash=build_default_catalog().catalog_hash)
        result = await MediaExecuteTool().execute(plan=json.dumps(plan), expected_revision=9)
        assert json.loads(str(result))["code"] == "STALE_REVISION"

    async def test_execute_catalog_drift_refused(self, monkeypatch) -> None:
        _patch_port(monkeypatch, _FakePort(_snapshot()))
        plan = _plan_for(base_revision=4, catalog_hash="drifted-catalog")
        result = await MediaExecuteTool().execute(plan=json.dumps(plan), expected_revision=4)
        assert json.loads(str(result))["code"] == "CAPABILITY_CATALOG_DRIFT"

    async def test_execute_tampered_plan_refused(self, monkeypatch) -> None:
        _patch_port(monkeypatch, _FakePort(_snapshot()))
        plan = _plan_for(base_revision=4)
        plan["nodes"][0]["node_id"] = "forged-node"
        result = await MediaExecuteTool().execute(plan=json.dumps(plan), expected_revision=4)
        assert getattr(result, "is_error", False)  # plan_hash no longer matches

    async def test_execute_schedules_resume_and_reports_denied(self, monkeypatch) -> None:
        port = _FakePort(_snapshot())
        _patch_port(monkeypatch, port)
        plan = _plan_for(base_revision=4, goal="render_local")
        result = await MediaExecuteTool().execute(plan=json.dumps(plan), expected_revision=4)
        payload = json.loads(str(result))
        assert port.resume_calls == [("pipeline_run_abc", 4)]
        nodes = {n["node_id"]: n["status"] for n in payload["nodes"]}
        assert any(s == "denied" for s in nodes.values())
        assert any(s == "scheduled" for s in nodes.values())
        assert payload["operation_key"] == "resume:pipeline_run_abc:4"

    async def test_execute_resume_failure_has_recovery(self, monkeypatch) -> None:
        class ExplodingPort(_FakePort):
            def resume(self, run_id: str, expected_version: int) -> dict | None:
                raise RuntimeError("CAS conflict")

        _patch_port(monkeypatch, ExplodingPort(_snapshot()))
        plan = _plan_for(base_revision=4)
        result = await MediaExecuteTool().execute(plan=json.dumps(plan), expected_revision=4)
        payload = json.loads(str(result))
        assert payload["code"] == "RESUME_FAILED"
        assert "recovery" in payload

    async def test_execute_unknown_node_ids(self, monkeypatch) -> None:
        _patch_port(monkeypatch, _FakePort(_snapshot()))
        plan = _plan_for(base_revision=4)
        result = await MediaExecuteTool().execute(
            plan=json.dumps(plan), expected_revision=4, node_ids=["ghost"])
        assert "unknown node ids" in str(result)
