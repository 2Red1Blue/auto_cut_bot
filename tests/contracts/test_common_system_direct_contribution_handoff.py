"""Structural producer-snapshot checks for the B common direct contribution.

This module deliberately does not invent B-A.  Set the three environment pins
only after the separate attestation commit exists to exercise its topology.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMMON_ROOT = REPOSITORY_ROOT / (
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common"
)
MANIFEST_PATH = COMMON_ROOT / "contributions/common-system-direct-contribution.manifest.json"
SCHEMA_PATH = COMMON_ROOT / "contributions/common-system-direct-contribution-manifest.schema.json"
INVENTORY_PATH = COMMON_ROOT / "contracts/system-contracts/common-system-decision-inventory.json"
PROVENANCE_PATH = COMMON_ROOT / "contracts/system-contracts/common-system-provenance.json"
BASE_COMMIT = "8c7e2b0ceb38673c6f62f7c4af44d5dfa222fdbe"
SELECTED_IDS = ("B0-001", "B0-002", "B0-005", "B0-006", "B0-007", "B0-008", "B0-009", "B0-010", "B0-011")
B_P_PATHS = {
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/contributions/common-system-direct-contribution-manifest.schema.json",
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/contributions/common-system-direct-contribution.manifest.json",
    "tests/contracts/test_common_system_direct_contribution_handoff.py",
}
B_A_PATHS = {
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/contributions/common-system-direct-review.json",
    "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/handoff/common-system-direct-handoff.json",
}


def _load(path: Path) -> dict:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _git(*args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(REPOSITORY_ROOT), *args), check=False,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_manifest_is_closed_and_preserves_the_b0_complement() -> None:
    schema = _load(SCHEMA_PATH)
    manifest = _load(MANIFEST_PATH)
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(manifest)
    # Source files retain one conventional terminal newline; the signed JSON
    # payload itself is JCS, so a second newline or any other byte is rejected.
    assert MANIFEST_PATH.read_bytes().rstrip(b"\n") == canonical_json_bytes(manifest)
    assert MANIFEST_PATH.read_bytes().endswith(b"\n")
    assert manifest["producer_base_commit"] == BASE_COMMIT
    assert tuple(item["candidate_id"] for item in manifest["selected"]) == SELECTED_IDS
    inventory = _load(INVENTORY_PATH)
    candidates = {item["candidate_id"]: item for item in inventory["candidates"]}
    assert len(candidates) == 44
    selected = {item["candidate_id"] for item in manifest["selected"]}
    omitted = {item["candidate_id"] for item in manifest["not_selected"]}
    assert selected | omitted == set(candidates)
    assert selected & omitted == set()
    assert len(manifest["not_selected"]) == 35
    for item in manifest["not_selected"]:
        candidate = candidates[item["candidate_id"]]
        assert {key: item[key] for key in ("disposition", "owner", "authority_change_ids")} == {
            key: candidate[key] for key in ("disposition", "owner", "authority_change_ids")
        }
    assert manifest["status"] == {
        "common_pack_ready": False, "registry_entries_declared": False,
        "generated_output_declared": False, "registry_contribution_authorized": False,
    }
    forbidden_identity_fields = {"registry_id", "artifact_id", "artifact_type", "generated_output"}
    assert not (forbidden_identity_fields & set(manifest))


def test_manifest_binds_current_b0_and_selected_source_test_bytes() -> None:
    manifest = _load(MANIFEST_PATH)
    evidence = manifest["b0_evidence"]
    assert evidence["authority_commit"] == "77f6db99019eb3e24e7b20ac92a2463a3bb3156c"
    assert evidence["inventory_raw_sha256"] == _sha256(INVENTORY_PATH.read_bytes())
    assert evidence["provenance_raw_sha256"] == _sha256(PROVENANCE_PATH.read_bytes())
    for item in manifest["selected"]:
        source = COMMON_ROOT / item["source_path"]
        raw = source.read_bytes()
        assert item["source_raw_sha256"] == _sha256(raw)
        assert item["source_jcs_sha256"] == _sha256(canonical_json_bytes(_load(source)))
        assert _load(source)["$comment"] in item["authority_evidence"]["historical_schema_comment"]
        assert "77f6db99019eb3e24e7b20ac92a2463a3bb3156c" in item["authority_evidence"]["reattested"][0]
        for test in item["test_paths"]:
            path = REPOSITORY_ROOT / test["path"]
            assert path.is_file() and REPOSITORY_ROOT in path.resolve().parents
            assert test["raw_sha256"] == _sha256(path.read_bytes())


def test_two_commit_topology_after_explicit_materialization() -> None:
    """Verify B-P/B-A only when a caller supplies the final immutable pins."""

    producer = os.environ.get("AUTOCUT_COMMON_DIRECT_PRODUCER_COMMIT")
    attestation = os.environ.get("AUTOCUT_COMMON_DIRECT_ATTESTATION_COMMIT")
    if not producer or not attestation:
        pytest.skip("B-P/B-A commits are intentionally not materialized in this producer change")
    assert _git("rev-parse", f"{producer}^") == BASE_COMMIT
    assert _git("rev-list", "--parents", "-n", "1", producer).split()[1:] == [BASE_COMMIT]
    assert set(_git("diff", "--name-only", BASE_COMMIT, producer).splitlines()) == B_P_PATHS
    assert _git("rev-parse", f"{attestation}^") == producer
    assert _git("rev-list", "--parents", "-n", "1", attestation).split()[1:] == [producer]
    assert set(_git("diff", "--name-only", producer, attestation).splitlines()) == B_A_PATHS
