"""Independent P5-A attestation of the immutable P5-P source snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from autocut_kernel.contracts.compiler.canonical import (
    CanonicalizationError,
    canonical_json_bytes,
    load_canonical_json_bytes,
)
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
P5_P = "31e3c1a461395a7f77cb9ed13e8dcd8111f8dac1"
B_A = "50f78ea0b7f754eb8f91ef800924b86da25b3083"
STAGE_ROOT = "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/stage_05"
HANDOFF_ROOT = ROOT / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/handoff"
REVIEW_PATH = HANDOFF_ROOT / "stage-05-owner-contribution-review.json"
HANDOFF_PATH = HANDOFF_ROOT / "stage-05-owner-contribution-handoff.json"
HANDOFF_SCHEMA_PATH = HANDOFF_ROOT / "handoff-manifest.schema.json"
AUTHORITY_SOURCE_PATH = (
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/"
    "handoff/common-system-direct-handoff.json"
)
P5_P_PATHS = {
    f"{STAGE_ROOT}/contributions/stage-05-owner-contribution.manifest.json",
    f"{STAGE_ROOT}/contributions/stage-05-owner-contribution.manifest.schema.json",
    f"{STAGE_ROOT}/fixtures/stage-05-structural-valid.json",
    f"{STAGE_ROOT}/obligations/stage-05-source-anchor-worklist.json",
    f"{STAGE_ROOT}/obligations/stage-05-vector-anchor-worklist.json",
    f"{STAGE_ROOT}/shapes/publication-evidence-wire.local-shape.json",
    f"{STAGE_ROOT}/shapes/publication-target-surface-wire.local-shape.json",
    f"{STAGE_ROOT}/shapes/qc-metric-profile.local-shape.json",
    f"{STAGE_ROOT}/shapes/qc-observation-wire.local-shape.json",
    f"{STAGE_ROOT}/shapes/release-lineage-wire.local-shape.json",
    f"{STAGE_ROOT}/shapes/render-attempt-observation.local-shape.json",
    f"{STAGE_ROOT}/shapes/render-input-wire.local-shape.json",
    f"{STAGE_ROOT}/shapes/rendered-asset-wire.local-shape.json",
    f"{STAGE_ROOT}/tests/test-stage-05-owner-source.json",
    "tests/contracts/test_stage05_owner_contribution.py",
}
P5_A_PATHS = {
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/handoff/stage-05-owner-contribution-review.json",
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/handoff/stage-05-owner-contribution-handoff.json",
    "tests/contracts/test_stage05_owner_contribution_attestation.py",
}
SELF_EXCLUSIONS = {
    "contributions/stage-05-owner-contribution.manifest.json",
    "contributions/stage-05-owner-contribution.manifest.schema.json",
}


def _git(*args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(ROOT), *args), check=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _git_bytes(*args: str) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(ROOT), *args), check=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


def _strict_jcs_bytes(raw: bytes, *, origin: str) -> dict:
    value, canonical = load_canonical_json_bytes(raw, origin=origin)
    if raw != canonical:
        raise ValueError(f"{origin}: bytes must be canonical JCS")
    return value


def _strict_jcs(path: Path) -> dict:
    return _strict_jcs_bytes(path.read_bytes(), origin=str(path))


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _producer_inventory() -> dict[str, str]:
    paths = _git("ls-tree", "-r", "--name-only", P5_P, "--", STAGE_ROOT).splitlines()
    assert set(paths) == P5_P_PATHS - {"tests/contracts/test_stage05_owner_contribution.py"}
    return {
        path.removeprefix(f"{STAGE_ROOT}/"): _sha256(_git_bytes("show", f"{P5_P}:{path}"))
        for path in paths
    }


def _snapshot_hashes(inventory: dict[str, str]) -> tuple[str, str]:
    tree = [{"path": path, "file_hash": digest} for path, digest in sorted(inventory.items())]
    return _sha256(canonical_json_bytes(tree)), _sha256(canonical_json_bytes({"source_paths": tree}))


def test_p5_p_is_the_sole_child_of_b_a_with_its_exact_allowlist() -> None:
    assert _git("rev-parse", f"{P5_P}^") == B_A
    assert _git("rev-list", "--parents", "-n", "1", P5_P).split()[1:] == [B_A]
    assert set(_git("diff", "--name-only", B_A, P5_P).splitlines()) == P5_P_PATHS


def test_attestation_uses_p5_p_git_evidence_for_all_14_pack_files() -> None:
    review = _strict_jcs(REVIEW_PATH)
    handoff = _strict_jcs(HANDOFF_PATH)
    inventory = _producer_inventory()
    manifest = _strict_jcs_bytes(
        _git_bytes("show", f"{P5_P}:{STAGE_ROOT}/contributions/stage-05-owner-contribution.manifest.json"),
        origin=f"{P5_P}:stage-05-owner-contribution.manifest.json",
    )
    authority_source = json.loads(_git_bytes("show", f"{P5_P}:{AUTHORITY_SOURCE_PATH}"))
    tree_hash, inventory_hash = _snapshot_hashes(inventory)

    assert len(inventory) == 14
    assert handoff["source_paths"] == inventory
    assert review["authority_anchors"] == handoff["authority_anchors"] == authority_source["authority_anchors"]
    assert {item["path"] for item in manifest["producer_files"]} == set(inventory) - SELF_EXCLUSIONS
    assert len(manifest["producer_files"]) == 12
    assert review["producer_git_commit"] == handoff["producer"]["producer_git_commit"] == P5_P
    assert review["source_tree_hash"] == handoff["source_tree_hash"] == handoff["producer"]["source_revision"] == tree_hash
    assert review["raw_inventory_hash"] == inventory_hash
    assert handoff["review"] == {"producer_git_commit": P5_P, "review_record_hash": _sha256(REVIEW_PATH.read_bytes()), "reviewer_id": "stage_05_owner_contribution_reviewer"}


def test_attestation_artifacts_are_jcs_reject_duplicates_and_validate_generic_handoff_schema() -> None:
    review = _strict_jcs(REVIEW_PATH)
    handoff = _strict_jcs(HANDOFF_PATH)
    schema = json.loads(HANDOFF_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(handoff)
    assert review == {"authority_anchors": handoff["authority_anchors"], "format": "autocut.registry-source.review/v1", "producer_git_commit": P5_P, "raw_inventory_hash": review["raw_inventory_hash"], "reviewer_id": "stage_05_owner_contribution_reviewer", "scope": "stage_05_owner_contribution", "source_tree_hash": review["source_tree_hash"], "status": "approved"}
    for path in (REVIEW_PATH, HANDOFF_PATH):
        raw = path.read_bytes()
        assert not raw.endswith(b"\n")
        assert raw == canonical_json_bytes(_strict_jcs(path))
        with pytest.raises((CanonicalizationError, ValueError)):
            _strict_jcs_bytes(raw + b"\n", origin=str(path))
        with pytest.raises(CanonicalizationError, match="duplicate JSON object key"):
            _strict_jcs_bytes(raw[:-1] + b',"format":"duplicate"}', origin=str(path))


def test_p5_a_topology_and_allowlist_after_materialization() -> None:
    p5_a = os.environ.get("AUTOCUT_STAGE05_OWNER_ATTESTATION_COMMIT")
    if not p5_a:
        pytest.skip("P5-A is intentionally not materialized in this attestation change")
    assert _git("rev-parse", f"{p5_a}^") == P5_P
    assert _git("rev-list", "--parents", "-n", "1", p5_a).split()[1:] == [P5_P]
    assert set(_git("diff", "--name-only", P5_P, p5_a).splitlines()) == P5_A_PATHS
    assert not set(_git("diff", "--name-only", P5_P, p5_a, "--", STAGE_ROOT).splitlines())
