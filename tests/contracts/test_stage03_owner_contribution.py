from __future__ import annotations

import hashlib
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from autocut_kernel.contracts.compiler.canonical import (
    CanonicalizationError,
    canonical_json_bytes,
    load_canonical_json_bytes,
)
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/stage_03"
B_A = "50f78ea0b7f754eb8f91ef800924b86da25b3083"
C3_A_PATHS = {
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/handoff/stage-03-owner-contribution-review.json",
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/handoff/stage-03-owner-contribution-handoff.json",
    "tests/contracts/test_stage03_owner_contribution_attestation.py",
}


def strict(raw: bytes, origin: str):
    value, canonical = load_canonical_json_bytes(raw, origin=origin)
    if raw != canonical:
        raise ValueError(f"{origin}: source bytes must be canonical JCS")
    return value


def load(path: Path):
    return strict(path.read_bytes(), str(path))


def paths():
    return {path.relative_to(PACK).as_posix() for path in PACK.rglob("*.json")}


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(ROOT), *args),
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def producer_commit_from_attestation() -> str:
    c3_a = os.environ.get("AUTOCUT_STAGE03_OWNER_ATTESTATION_COMMIT")
    if not c3_a:
        pytest.skip("C3-P/C3-A commits are intentionally not materialized in this producer change")

    parents = git("rev-list", "--parents", "-n", "1", c3_a).split()
    assert parents[1:] and len(parents[1:]) == 1
    return parents[1]


def walk_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk_keys(nested)


def test_manifest_is_closed_and_exactly_pinned():
    manifest = load(PACK / "contributions/stage-03-owner-contribution.manifest.json")
    schema = load(PACK / "contributions/stage-03-owner-contribution.manifest.schema.json")
    validator = Draft202012Validator(schema)
    assert validator.is_valid(manifest)
    assert manifest["producer_base_commit"] == B_A
    assert manifest["b_input"]["used_primitive_blobs"] == [
        {"path": "schemas/primitives/artifact-ref.schema.json", "raw_sha256": "sha256:9df321d5ccc33dd8ff307aae6f33d1e4d2accfc278ea4fb5ed11072e5e5295db"},
        {"path": "schemas/primitives/domain-ref.schema.json", "raw_sha256": "sha256:72211809fa19f967345ccc41574fea5319feb40bcbde05a3564cf6bc8c63f896"},
    ]
    mutated = deepcopy(manifest)
    mutated["b_input"]["handoff_git_commit"] = "0" * 40
    assert not validator.is_valid(mutated)
    assert not {"status", "registration", "action", "outcome", "evaluator", "trace", "admission"} & set(manifest)


def test_jcs_duplicate_rejection_and_self_exclusion_inventory():
    manifest = load(PACK / "contributions/stage-03-owner-contribution.manifest.json")
    for relative in paths():
        path = PACK / relative
        assert path.read_bytes() == canonical_json_bytes(load(path))
    manifest_path = PACK / "contributions/stage-03-owner-contribution.manifest.json"
    with pytest.raises((ValueError, CanonicalizationError)):
        strict(manifest_path.read_bytes() + b"\n", str(manifest_path))
    with pytest.raises(CanonicalizationError, match="duplicate JSON object key"):
        strict(manifest_path.read_bytes()[:-1] + b',"format":"duplicate"}', str(manifest_path))
    protocol = manifest["closed_self_exclusion_protocol"]
    external = {item["path"] for item in protocol["external_attestation_bound_files"]}
    assert len(paths()) == manifest["producer_file_count"] == 12
    assert len(manifest["producer_files"]) == 10
    assert paths() == external | {item["path"] for item in manifest["producer_files"]}
    assert [item["path"] for item in manifest["producer_files"]] == sorted(item["path"] for item in manifest["producer_files"])
    for item in manifest["producer_files"]:
        assert item["raw_sha256"] == sha(PACK / item["path"])
    assert manifest["repository_validation"]["path"] == "tests/contracts/test_stage03_owner_contribution.py"
    assert manifest["repository_validation"]["raw_sha256"].startswith("sha256:")


def test_structural_shapes_and_synthetic_fixture_only():
    fixture = load(PACK / "fixtures/structural/stage-03-owner-source-valid.json")
    assert fixture["fixture_kind"] == "structural_synthetic_only"
    schemas = {
        "editorial_blueprint": "editorial-blueprint.shape.json",
        "evidence_closure_set": "evidence-closure-set.shape.json",
        "context_manifest": "context-manifest.shape.json",
        "generation_partition_plan": "generation-partition-plan.shape.json",
        "merge_policy": "merge-policy.shape.json",
    }
    forbidden = {"scope", "scope_ref", "admission", "result", "action", "actions", "outcome", "outcomes", "evaluator", "evaluation", "registration", "rule_id", "rule_version", "trace"}
    for name, filename in schemas.items():
        shape = load(PACK / "shapes" / filename)
        assert shape["additionalProperties"] is False
        assert not set(walk_keys(shape)) & forbidden
        assert Draft202012Validator(shape).is_valid({name: fixture[name]})
    fragment = fixture["editorial_blueprint"]["fragments"][0]
    assert set(fragment) == {"blueprint_fragment_id", "story_id", "partition_id", "generation_invocation_ref", "parse_normalization_record_ref", "beats", "ordering_constraints", "fragment_hash"}
    assert set(fragment["beats"][0]["normalized_beat_payload"]) == {"blueprint_beat_id", "narrative_role", "narrative_function", "summary", "required_obligation_ids", "required_fact_ids", "evidence_requirements", "candidate_preferences", "span_policy", "duration_seconds"}
    assert set(fixture["editorial_blueprint"]["beats"][0]) == set(fragment["beats"][0]["normalized_beat_payload"]) | {"stable_beat_id"}


def test_no_external_topology_claim_without_explicit_attestation_pins():
    c3_a = os.environ.get("AUTOCUT_STAGE03_OWNER_ATTESTATION_COMMIT")
    if not c3_a:
        pytest.skip("C3-A is intentionally not materialized in this producer change")
    c3_p = producer_commit_from_attestation()
    assert git("rev-list", "--parents", "-n", "1", c3_p).split()[1:] == [B_A]
    manifest = load(PACK / "contributions/stage-03-owner-contribution.manifest.json")
    assert manifest["producer_base_commit"] == B_A
    assert set(git("diff", "--name-only", c3_p, c3_a).splitlines()) == C3_A_PATHS
