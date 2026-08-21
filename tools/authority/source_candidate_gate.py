# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false
"""Pre-A verification for the reviewed authority source candidate index."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import (
    canonical_hash,
    git_index_bytes,
    git_index_paths,
    git_output,
    load_mapping_bytes,
    require_commit,
    require_list,
    require_non_empty_string,
    require_sha256,
    sha256_bytes,
)
from .conformance_gates import (
    audit_candidate_tree,
    secret_content_findings,
    validate_synthetic_fixture_manifest,
)
from .errors import GateViolation
from .receipts import make_typed_receipt

ALLOWED_SOURCE_ROOTS = ("governance/", "tools/authority/", "tests/authority/")
FORBIDDEN_PRE_A_PATHS = {
    "governance/authority-sources.yaml",
    "governance/authority-lock.yaml",
}


def verify_pre_a_source_candidate(
    *,
    root: Path,
    predecessor_commit: str,
    synthetic_fixture_manifest_path: str,
    task_id: str = "v213-authority-pre-a",
) -> dict[str, Any]:
    """Verify A before inventory B and generated-lock C exist."""

    require_commit(predecessor_commit, where="predecessor_commit")
    changed_paths = git_index_paths(root, predecessor_commit)
    if not changed_paths:
        raise GateViolation("AUTH-PRE-A-EMPTY", "source candidate has no staged changes")
    for path in changed_paths:
        if path in FORBIDDEN_PRE_A_PATHS:
            raise GateViolation("AUTH-PRE-A-PHASE-MIX", f"pre-A contains later-phase path: {path}")
        if not path.startswith(ALLOWED_SOURCE_ROOTS):
            raise GateViolation(
                "AUTH-PRE-A-SCOPE", f"pre-A path is outside authority source: {path}"
            )
        if "__pycache__" in path.split("/") or path.endswith((".pyc", ".pyo")):
            raise GateViolation("AUTH-PRE-A-RUNTIME-ARTIFACT", f"generated artifact staged: {path}")

    manifest_path = require_non_empty_string(
        synthetic_fixture_manifest_path, where="synthetic_fixture_manifest_path"
    )
    manifest_raw = git_index_bytes(root, manifest_path)
    manifest = load_mapping_bytes(manifest_raw, where=f"index:{manifest_path}")
    entries = validate_synthetic_fixture_manifest(manifest)
    declared_paths = set(entries)
    for fixture_path, entry in entries.items():
        raw = git_index_bytes(root, fixture_path)
        if sha256_bytes(raw) != require_sha256(entry["sha256"], where="fixture sha256"):
            raise GateViolation("AUTH-SYNTHETIC-HASH", f"fixture hash mismatch: {fixture_path}")
        if entry["marker"].encode("utf-8") not in raw:
            raise GateViolation("AUTH-SYNTHETIC-MARKER", f"fixture marker missing: {fixture_path}")
        if not secret_content_findings(fixture_path, raw):
            raise GateViolation(
                "AUTH-SYNTHETIC-NOT-SENSITIVE", f"fixture does not exercise scanner: {fixture_path}"
            )
    manifest_declared = {
        require_non_empty_string(item["path"], where="fixture path")
        for item in require_list(manifest["fixtures"], where="fixtures")
        if isinstance(item, dict)
    }
    if manifest_declared != declared_paths:
        raise GateViolation("AUTH-SYNTHETIC-SET", "synthetic fixture set is ambiguous")

    tree_oid = git_output(root, "write-tree")
    pre_authority_hash = canonical_hash(
        {
            "phase": "pre_a_source_candidate",
            "predecessor_commit": predecessor_commit,
            "candidate_tree_oid": tree_oid,
            "changed_paths": changed_paths,
        }
    )
    candidate = audit_candidate_tree(
        root=root,
        predecessor_commit=predecessor_commit,
        task_id=task_id,
        authority_lock_hash=pre_authority_hash,
        scan_profile="test_fixture",
        synthetic_fixture_manifest=manifest,
    )
    if candidate["staged_tree_hash"] != tree_oid:
        raise GateViolation("AUTH-PRE-A-TREE", "candidate tree changed during pre-A gate")
    return make_typed_receipt(
        "source_candidate",
        authority_lock_hash=pre_authority_hash,
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        predecessor_commit=predecessor_commit,
        candidate_tree_hash=tree_oid,
        changed_paths_hash=canonical_hash(changed_paths),
        synthetic_fixture_manifest_hash=sha256_bytes(manifest_raw),
    )
