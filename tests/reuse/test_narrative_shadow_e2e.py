"""End-to-end: narrative shadow A/B arms run through the R0 runner CLI.

Both arms use the same frozen compact context (exported from the kernel's
synthetic Stage 1 fixture) and the fake provider; only the prompt strategy
differs. This verifies the full contract path: registry -> fixture -> adapter
-> budget -> storage -> metrics -> report.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import tools.reuse.run as run_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "packages" / "autocut-kernel" / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "packages" / "autocut-kernel" / "src"))

from tests.reuse.test_narrative_strategy import _wire  # noqa: E402
from tests.semantic_chain.test_material_support import material_case  # noqa: E402
from tests.semantic_chain.test_story_design_draft import POLICY  # noqa: E402
from tools.reuse.models import file_sha256  # noqa: E402


def _shadow_payload(tmp_path: Path) -> Path:
    """Export the synthetic Stage 1 context as a frozen shadow payload."""
    from autocut_kernel.semantic_chain.story_design_compact import (
        build_story_design_compact_context,
    )

    from tools.reuse.narrative_strategy.context_io import write_shadow_payload

    case = material_case()
    context = build_story_design_compact_context(
        case["inputs"], case["stage1"], case["projection"],
        **{key: case[key] for key in ("job_policy", "story_policy", "candidate_policy")},
    )
    return write_shadow_payload(context, tmp_path / "shadow-payload" / "context.json", POLICY)


def _candidate_raw(tmp_path: Path) -> str:
    """A valid candidate draft: kernel-fixture compact wire + duty annotations."""
    ctx = _narrative_ctx()
    raw = _wire(ctx, {0: [
        {"duty_kind": "hook", "target_ref": "proposal-0", "reason": "开场即钩子"},
        {"duty_kind": "setup", "target_ref": "proposal-0", "reason": "铺垫线索"},
        {"duty_kind": "payoff", "target_ref": "proposal-0", "reason": "回收铺垫",
         "setup_ref": "f1", "payoff_ref": "e1"},
    ]})
    return raw


def _narrative_ctx():
    from autocut_kernel.semantic_chain.story_design_compact import (
        build_story_design_compact_context,
    )

    case = material_case()
    context = build_story_design_compact_context(
        case["inputs"], case["stage1"], case["projection"],
        **{key: case[key] for key in ("job_policy", "story_policy", "candidate_policy")},
    )
    return case, context


@dataclass
class ShadowCli:
    repo_root: Path

    def invoke(self, spec_dict: dict[str, Any], *, mode: str) -> int:
        spec_path = self.repo_root / "specs" / f"{spec_dict['experiment_id']}.json"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(json.dumps(spec_dict, indent=2, ensure_ascii=False), encoding="utf-8")
        return run_mod.main([
            "--spec", str(spec_path), "--mode", mode,
            "--media-root", str(self.repo_root / "media"),
            "--out-root", str(self.repo_root / "artifacts" / "shadow" / "reuse"),
        ])

    def attempts(self, experiment_id: str) -> list[Path]:
        exp = self.repo_root / "artifacts" / "shadow" / "reuse" / experiment_id
        return sorted(p for p in exp.iterdir() if p.is_dir() and p.name.startswith("attempt-"))


def _spec(experiment_id: str, variant: str, manifest_path: Path) -> dict[str, Any]:
    return {
        "schema": "ExperimentSpec/v1",
        "experiment_id": experiment_id,
        "producer_id": "narrative-shadow",
        "producer_commit": "builtin:narrative-shadow",
        "adapter_version": "narrative-shadow-1",
        "fixture_ref": str(manifest_path),
        "fixture_sha256": file_sha256(manifest_path),
        "variant": variant,
        "mode": "live",
        "model_request": {
            "provider": "fake", "model": "fake-model",
            "prompt_id": "narrative-shadow", "schema_id": "stage2-story-design",
        },
        "budget": {"max_calls": 5, "max_input_tokens": 100_000, "max_output_tokens": 100_000,
                   "max_concurrency": 1},
        "metric_policy": {"policy_version": "v1", "revision": "r1a-synthetic"},
        "seed": None,
    }


@pytest.fixture()
def shadow_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ShadowCli, dict[str, str], str]:
    """Hermetic repo with the shadow payload fixture and a scripted provider raw."""
    (tmp_path / "tools" / "reuse").mkdir(parents=True)
    import shutil

    shutil.copy(REPO_ROOT / "tools" / "reuse" / "projects.json",
                tmp_path / "tools" / "reuse" / "projects.json")
    payload_path = _shadow_payload(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    input_path = media_root / "context-input.json"
    input_path.write_text("{}", encoding="utf-8")
    manifest = {
        "schema": "FixtureManifest/v2",
        "set_id": "narrative-shadow-synthetic",
        "frozen": True,
        "label_kind": "pipeline_baseline",
        "alignment_status": "not_applicable",
        "inputs": [{"path": "context-input.json", "sha256": file_sha256(input_path),
                    "size_bytes": input_path.stat().st_size, "episode": None}],
        "labels_ref": str(payload_path),
        "allowed_context_refs": [],
    }
    manifest_path = tmp_path / "fixtures" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # ScriptedProvider: returns the migration-produced compact/candidate drafts
    # so both arms decode against real kernel rules.
    candidate_raw = _candidate_raw(tmp_path)
    ctx = _narrative_ctx()
    baseline_payload = json.loads(_wire(ctx))
    baseline_payload["schema_version"] = "stage2-story-design-compact-v2"
    baseline_raw = json.dumps(baseline_payload, ensure_ascii=False)

    from tools.reuse import run as run_mod_inner

    class ScriptedProvider:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, request):  # type: ignore[no-untyped-def]
            self.calls += 1
            from tools.reuse.models import ProviderResult, Usage

            raw = candidate_raw if request.call_id == "shadow-candidate" else baseline_raw
            return ProviderResult(raw=raw, provider_response_id=f"resp-{self.calls}",
                                  usage=Usage(100, 150), status="done")

        def resolve_unknown(self, call_id, response_id):  # type: ignore[no-untyped-def]
            raise AssertionError("scripted provider never goes unknown")

    monkeypatch.setattr(run_mod_inner, "FakeProvider", ScriptedProvider)
    monkeypatch.setattr(run_mod, "resolve_repo_root", lambda: tmp_path)
    return ShadowCli(repo_root=tmp_path), {"baseline_raw": baseline_raw, "candidate_raw": candidate_raw}, candidate_raw


class TestNarrativeShadowThroughRunner:
    def test_baseline_and_candidate_arms_succeed(self, shadow_repo) -> None:
        cli, _, _ = shadow_repo
        manifest = cli.repo_root / "fixtures" / "manifest.json"
        assert cli.invoke(_spec("shadow-base", "baseline", manifest), mode="live") == 0
        assert cli.invoke(_spec("shadow-cand", "candidate", manifest), mode="live") == 0
        base_attempt = cli.attempts("shadow-base")[0]
        cand_attempt = cli.attempts("shadow-cand")[0]
        base_projection = json.loads((base_attempt / "projection" / "projection.json").read_text())
        cand_projection = json.loads((cand_attempt / "projection" / "projection.json").read_text())
        assert base_projection["items"][0]["decode_ok"] is True
        assert cand_projection["items"][0]["decode_ok"] is True
        assert cand_projection["items"][0]["duty_count"] == 3
        # candidate metrics carry duty coverage with a denominator
        cand_metrics = json.loads((cand_attempt / "metrics.json").read_text())
        names = {row["metric"] for row in cand_metrics["results"]}
        assert "decode_pass_candidate" in names
        assert "duty_coverage_candidate" in names

    def test_candidate_duty_violation_fails_decode_not_crash(self, shadow_repo, monkeypatch) -> None:
        cli, _, candidate_raw = shadow_repo
        manifest = cli.repo_root / "fixtures" / "manifest.json"
        # poison the candidate raw with an unknown duty kind
        poisoned = json.loads(candidate_raw)
        poisoned["proposals"][0]["narrative_duties"][0]["duty_kind"] = "plot_twist"
        poisoned_raw = json.dumps(poisoned, ensure_ascii=False)

        from tools.reuse import run as run_mod_inner

        class PoisonedProvider:
            def call(self, request):  # type: ignore[no-untyped-def]
                from tools.reuse.models import ProviderResult, Usage

                return ProviderResult(raw=poisoned_raw, provider_response_id="resp-x",
                                      usage=Usage(100, 150), status="done")

            def resolve_unknown(self, call_id, response_id):  # type: ignore[no-untyped-def]
                raise AssertionError

        monkeypatch.setattr(run_mod_inner, "FakeProvider", PoisonedProvider)
        assert cli.invoke(_spec("shadow-poison", "candidate", manifest), mode="live") == 0
        # the run still finalizes: decode failure is recorded, not a crash
        attempt = cli.attempts("shadow-poison")[0]
        meta = json.loads((attempt / "metadata.json").read_text())
        assert meta["status"] == "succeeded"
        projection = json.loads((attempt / "projection" / "projection.json").read_text())
        assert projection["items"][0]["decode_ok"] is False
        metrics = json.loads((attempt / "metrics.json").read_text())
        decode_pass = next(r for r in metrics["results"] if r["metric"] == "decode_pass_candidate")
        assert decode_pass["value"] == 0.0 and decode_pass["denom"] == 1.0
