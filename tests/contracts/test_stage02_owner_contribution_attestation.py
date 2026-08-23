"""Independent C2-A attestation of the immutable C2-P source snapshot."""

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
C2_P = "20ade5b515ccd7bbf2a3ea0541992c86c1be456f"
B_A = "50f78ea0b7f754eb8f91ef800924b86da25b3083"
STAGE_ROOT = "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/stage_02"
HANDOFF_ROOT = ROOT / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/handoff"
REVIEW_PATH = HANDOFF_ROOT / "stage-02-owner-contribution-review.json"
HANDOFF_PATH = HANDOFF_ROOT / "stage-02-owner-contribution-handoff.json"
HANDOFF_SCHEMA = HANDOFF_ROOT / "handoff-manifest.schema.json"
C2_P_PATHS = {
    f"{STAGE_ROOT}/contracts/stage-02-source-obligations.json",
    f"{STAGE_ROOT}/contributions/stage-02-owner-contribution.manifest.json",
    f"{STAGE_ROOT}/contributions/stage-02-owner-contribution.manifest.schema.json",
    f"{STAGE_ROOT}/fixtures/stage-02-structural-valid.json",
    f"{STAGE_ROOT}/shapes/candidate-capability.local-shape.json",
    f"{STAGE_ROOT}/shapes/candidate-catalog.local-shape.json",
    f"{STAGE_ROOT}/shapes/candidate-measurement.local-shape.json",
    f"{STAGE_ROOT}/shapes/portfolio-local-fields.local-shape.json",
    f"{STAGE_ROOT}/shapes/portfolio.local-shape.json",
    f"{STAGE_ROOT}/shapes/proposal-set.local-shape.json",
    f"{STAGE_ROOT}/shapes/story-design.local-shape.json",
    f"{STAGE_ROOT}/tests/test-stage-02-owner-source.json",
    "tests/contracts/test_stage02_owner_contribution.py",
}
C2_A_PATHS = {
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/handoff/stage-02-owner-contribution-review.json",
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/handoff/stage-02-owner-contribution-handoff.json",
    "tests/contracts/test_stage02_owner_contribution_attestation.py",
}
SELF_EXCLUSIONS = {
    "contributions/stage-02-owner-contribution.manifest.json",
    "contributions/stage-02-owner-contribution.manifest.schema.json",
}


def _git(*args: str) -> str:
    result = subprocess.run(("git", "-C", str(ROOT), *args), check=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _git_bytes(*args: str) -> bytes:
    result = subprocess.run(("git", "-C", str(ROOT), *args), check=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


def _strict_jcs(path: Path) -> dict:
    return _strict_jcs_bytes(path.read_bytes(), origin=str(path))


def _strict_jcs_bytes(raw: bytes, *, origin: str) -> dict:
    value, canonical = load_canonical_json_bytes(raw, origin=origin)
    if raw != canonical:
        raise ValueError(f"{origin}: bytes must be canonical JCS")
    return value


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _producer_inventory() -> dict[str, str]:
    files = _git("ls-tree", "-r", "--name-only", C2_P, "--", STAGE_ROOT).splitlines()
    assert set(files) == C2_P_PATHS - {"tests/contracts/test_stage02_owner_contribution.py"}
    return {
        path.removeprefix(f"{STAGE_ROOT}/"): _sha256(_git_bytes("show", f"{C2_P}:{path}"))
        for path in files
    }


def _snapshot_hashes(inventory: dict[str, str]) -> tuple[str, str]:
    tree = [{"path": path, "file_hash": digest} for path, digest in sorted(inventory.items())]
    tree_hash = _sha256(canonical_json_bytes(tree))
    inventory_hash = _sha256(canonical_json_bytes({"source_paths": tree}))
    return tree_hash, inventory_hash


def test_c2_p_is_exactly_parented_by_b_a_and_changes_only_its_allowlist() -> None:
    assert _git("rev-parse", f"{C2_P}^") == B_A
    assert _git("rev-list", "--parents", "-n", "1", C2_P).split()[1:] == [B_A]
    assert set(_git("diff", "--name-only", B_A, C2_P).splitlines()) == C2_P_PATHS


def test_attestation_uses_only_c2_p_git_show_evidence_and_binds_all_12_pack_files() -> None:
    review = _strict_jcs(REVIEW_PATH)
    handoff = _strict_jcs(HANDOFF_PATH)
    inventory = _producer_inventory()
    manifest = _strict_jcs_bytes(
        _git_bytes("show", f"{C2_P}:{STAGE_ROOT}/contributions/stage-02-owner-contribution.manifest.json"),
        origin=f"{C2_P}:stage-02-owner-contribution.manifest.json",
    )
    tree_hash, inventory_hash = _snapshot_hashes(inventory)

    assert len(inventory) == 12
    assert handoff["source_paths"] == inventory
    assert {item["path"] for item in manifest["producer_files"]} == set(inventory) - SELF_EXCLUSIONS
    assert len(manifest["producer_files"]) == 10
    assert manifest["closed_self_exclusion_protocol"]["external_attestation_bound_files"] == [
        {"path": "contributions/stage-02-owner-contribution.manifest.json"},
        {"path": "contributions/stage-02-owner-contribution.manifest.schema.json"},
    ]
    assert review["producer_git_commit"] == handoff["producer"]["producer_git_commit"] == C2_P
    assert review["source_tree_hash"] == handoff["source_tree_hash"] == handoff["producer"]["source_revision"] == tree_hash
    assert review["raw_inventory_hash"] == inventory_hash
    assert handoff["review"]["review_record_hash"] == _sha256(REVIEW_PATH.read_bytes())
    assert handoff["review"] == {
        "producer_git_commit": C2_P,
        "review_record_hash": _sha256(REVIEW_PATH.read_bytes()),
        "reviewer_id": "stage_02_owner_contribution_reviewer",
    }


def test_attestation_artifacts_are_strict_jcs_and_use_the_existing_handoff_schema() -> None:
    review = _strict_jcs(REVIEW_PATH)
    handoff = _strict_jcs(HANDOFF_PATH)
    schema = json.loads(HANDOFF_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(handoff)
    assert review == {
        "authority_anchors": review["authority_anchors"],
        "format": "autocut.registry-source.review/v1",
        "producer_git_commit": C2_P,
        "raw_inventory_hash": review["raw_inventory_hash"],
        "reviewer_id": "stage_02_owner_contribution_reviewer",
        "scope": "stage_02_owner_contribution",
        "source_tree_hash": review["source_tree_hash"],
        "status": "approved",
    }
    for path in (REVIEW_PATH, HANDOFF_PATH):
        raw = path.read_bytes()
        assert not raw.endswith(b"\n")
        assert raw == canonical_json_bytes(_strict_jcs(path))
        with pytest.raises((CanonicalizationError, ValueError)):
            _strict_jcs_bytes(raw + b"\n", origin=str(path))
        with pytest.raises(CanonicalizationError, match="duplicate JSON object key"):
            _strict_jcs_bytes(raw[:-1] + b',"format":"duplicate"}', origin=str(path))


def test_c2_a_topology_and_allowlist_after_materialization() -> None:
    """The caller supplies C2-A only once these three files are committed."""

    c2_a = os.environ.get("AUTOCUT_STAGE02_OWNER_ATTESTATION_COMMIT")
    if not c2_a:
        pytest.skip("C2-A is intentionally not materialized in this producer change")
    assert _git("rev-parse", f"{c2_a}^") == C2_P
    assert _git("rev-list", "--parents", "-n", "1", c2_a).split()[1:] == [C2_P]
    assert set(_git("diff", "--name-only", C2_P, c2_a).splitlines()) == C2_A_PATHS
    assert not set(_git("diff", "--name-only", C2_P, c2_a, "--", STAGE_ROOT).splitlines())
