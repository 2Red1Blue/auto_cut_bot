"""Independent C1-A attestation of the immutable C1-P source snapshot."""

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
C1_P = "a915bc8b3ce847c6a521efa8f0631408c294f022"
B_A = "50f78ea0b7f754eb8f91ef800924b86da25b3083"
STAGE_ROOT = "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/stage_01"
HANDOFF_ROOT = ROOT / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/handoff"
REVIEW_PATH = HANDOFF_ROOT / "stage-01-owner-contribution-review.json"
HANDOFF_PATH = HANDOFF_ROOT / "stage-01-owner-contribution-handoff.json"
HANDOFF_SCHEMA = HANDOFF_ROOT / "handoff-manifest.schema.json"
C1_P_PATHS = {
    f"{STAGE_ROOT}/contracts/stage-01-rule-obligations.json",
    f"{STAGE_ROOT}/contracts/stage-01-vector-obligations.json",
    f"{STAGE_ROOT}/contributions/stage-01-owner-contribution.manifest.json",
    f"{STAGE_ROOT}/contributions/stage-01-owner-contribution.manifest.schema.json",
    f"{STAGE_ROOT}/fixtures/stage-01-owner-source-valid.json",
    f"{STAGE_ROOT}/schemas/coverage-ledger.schema.json",
    f"{STAGE_ROOT}/schemas/dependency-closure-proof.schema.json",
    f"{STAGE_ROOT}/schemas/dependency-propagation-policy.schema.json",
    f"{STAGE_ROOT}/schemas/episode-digest-set.schema.json",
    f"{STAGE_ROOT}/schemas/event-card-set.schema.json",
    f"{STAGE_ROOT}/schemas/narrative-graph.schema.json",
    f"{STAGE_ROOT}/schemas/source-knowledge-input-set.schema.json",
    f"{STAGE_ROOT}/tests/test-stage-01-owner-source.json",
    "tests/contracts/test_stage01_owner_contribution.py",
}
C1_A_PATHS = {
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/handoff/stage-01-owner-contribution-review.json",
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/handoff/stage-01-owner-contribution-handoff.json",
    "tests/contracts/test_stage01_owner_contribution_attestation.py",
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
    files = _git("ls-tree", "-r", "--name-only", C1_P, "--", STAGE_ROOT).splitlines()
    assert set(files) == C1_P_PATHS - {"tests/contracts/test_stage01_owner_contribution.py"}
    return {
        path.removeprefix(f"{STAGE_ROOT}/"): _sha256(_git_bytes("show", f"{C1_P}:{path}"))
        for path in files
    }


def _snapshot_hashes(inventory: dict[str, str]) -> tuple[str, str]:
    tree = [{"path": path, "file_hash": digest} for path, digest in sorted(inventory.items())]
    tree_hash = _sha256(canonical_json_bytes(tree))
    inventory_hash = _sha256(canonical_json_bytes({"source_paths": tree}))
    return tree_hash, inventory_hash


def test_c1_p_is_exactly_parented_by_b_a_and_changes_only_its_allowlist() -> None:
    assert _git("rev-parse", f"{C1_P}^") == B_A
    assert _git("rev-list", "--parents", "-n", "1", C1_P).split()[1:] == [B_A]
    assert set(_git("diff", "--name-only", B_A, C1_P).splitlines()) == C1_P_PATHS


def test_attestation_uses_only_c1_p_git_show_evidence_and_binds_all_13_files() -> None:
    review = _strict_jcs(REVIEW_PATH)
    handoff = _strict_jcs(HANDOFF_PATH)
    inventory = _producer_inventory()
    tree_hash, inventory_hash = _snapshot_hashes(inventory)

    assert len(inventory) == 13
    assert handoff["source_paths"] == inventory
    assert review["producer_git_commit"] == handoff["producer"]["producer_git_commit"] == C1_P
    assert review["source_tree_hash"] == handoff["source_tree_hash"] == handoff["producer"]["source_revision"] == tree_hash
    assert review["raw_inventory_hash"] == inventory_hash
    assert handoff["review"]["review_record_hash"] == _sha256(REVIEW_PATH.read_bytes())
    assert handoff["review"] == {
        "producer_git_commit": C1_P,
        "review_record_hash": _sha256(REVIEW_PATH.read_bytes()),
        "reviewer_id": "stage_01_owner_contribution_reviewer",
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
        "producer_git_commit": C1_P,
        "raw_inventory_hash": review["raw_inventory_hash"],
        "reviewer_id": "stage_01_owner_contribution_reviewer",
        "scope": "stage_01_owner_contribution",
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


def test_c1_a_topology_and_allowlist_after_materialization() -> None:
    """The caller supplies C1-A only once these three files are committed."""

    c1_a = os.environ.get("AUTOCUT_STAGE01_OWNER_ATTESTATION_COMMIT")
    if not c1_a:
        pytest.skip("C1-A is intentionally not materialized in this producer change")
    assert _git("rev-parse", f"{c1_a}^") == C1_P
    assert _git("rev-list", "--parents", "-n", "1", c1_a).split()[1:] == [C1_P]
    assert set(_git("diff", "--name-only", C1_P, c1_a).splitlines()) == C1_A_PATHS
    assert not set(_git("diff", "--name-only", C1_P, c1_a, "--", STAGE_ROOT).splitlines())
