# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
"""Aggregate commit and push gates with independently recomputed evidence.

The aggregate APIs are the only APIs permitted to issue final commit/push
decisions.  Leaf receipts remain diagnostic evidence and cannot be promoted to
a final decision without this closure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .common import (
    canonical_hash,
    git_output,
    load_mapping,
    require_list,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from .conformance_gates import (
    audit_candidate_tree,
    audit_history_publication,
    run_validation_commands,
    verify_authority_references,
    verify_commit_tree,
    verify_independent_check,
    verify_reuse_admission,
    verify_runtime_predicate,
)
from .errors import GateViolation
from .lock import verify_authority_lock_data
from .receipts import make_typed_receipt, validate_typed_receipt
from .remote_gate import verify_remote_protection
from .task_gate import admit_task, check_change_scopes


def _assert_receipt_binding(
    receipt: Mapping[str, Any], *, receipt_type: str, task_id: str, authority_hash: str
) -> None:
    validate_typed_receipt(receipt, expected_type=receipt_type)
    if receipt["decision"] != "allow":
        raise GateViolation("AUTH-AGGREGATE-DENY", f"{receipt_type} did not allow")
    if receipt.get("task_id") != task_id:
        raise GateViolation("AUTH-AGGREGATE-TASK", f"{receipt_type} binds another task")
    if receipt["authority_lock_hash"] != authority_hash:
        raise GateViolation("AUTH-AGGREGATE-AUTHORITY", f"{receipt_type} authority is stale")


def _context_snapshot(manifest: Mapping[str, Any], repository_roots: Mapping[str, Path]) -> str:
    contexts: list[Mapping[str, Any]] = []
    for artifact in require_list(manifest.get("planning_artifacts"), where="planning_artifacts"):
        if not isinstance(artifact, dict):
            raise GateViolation("AUTH-AGGREGATE-CONTEXT", "planning context is invalid")
        contexts.append(artifact)
    for field in ("implementation_context", "check_context"):
        context = manifest.get(field)
        if not isinstance(context, dict):
            raise GateViolation("AUTH-AGGREGATE-CONTEXT", f"{field} is invalid")
        contexts.append(context)
    observed: list[dict[str, Any]] = []
    for context in contexts:
        repository = str(context.get("repository"))
        relative = str(context.get("path"))
        if repository not in repository_roots:
            raise GateViolation("AUTH-AGGREGATE-CONTEXT", "context repository is unbound")
        try:
            root = repository_roots[repository].resolve(strict=True)
            path = (root / relative).resolve(strict=True)
            path.relative_to(root)
            raw = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise GateViolation("AUTH-AGGREGATE-CONTEXT", "cannot reread context bytes") from exc
        digest = sha256_bytes(raw)
        if digest != context.get("sha256") or len(raw) != context.get("byte_length"):
            raise GateViolation("AUTH-AGGREGATE-CONTEXT-DRIFT", f"context changed: {relative}")
        observed.append(
            {"repository": repository, "path": relative, "sha256": digest, "bytes": len(raw)}
        )
    return canonical_hash(observed)


def verify_change(
    *,
    task_manifest_path: Path,
    authority_lock_path: Path,
    model_policy_path: Path,
    protected_paths_path: Path,
    repository_roots: Mapping[str, Path],
    registry_paths: Sequence[str],
    reuse_ledger_path: str,
    checker_collector_ids: Mapping[str, str],
    validation_environment: Mapping[str, Mapping[str, str]] | None = None,
    scan_profile: str = "production",
    synthetic_fixture_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Recompute every Phase -1 commit predicate and close its receipts."""

    manifest = load_mapping(task_manifest_path)
    initial_manifest_hash = sha256_file(task_manifest_path)
    initial_context_hash = _context_snapshot(manifest, repository_roots)
    lock = load_mapping(authority_lock_path)
    verified_authority = verify_authority_lock_data(lock, repository_roots)
    authority_hash = require_sha256(lock.get("bundle_hash"), where="authority bundle hash")
    synthetic_manifest_path_value = synthetic_fixture_manifest_path
    synthetic_manifest = (
        load_mapping(synthetic_manifest_path_value)
        if synthetic_manifest_path_value is not None
        else None
    )
    if synthetic_manifest is not None and synthetic_manifest_path_value is not None:
        entries = [
            entry
            for entry in lock["entries"]
            if entry["path"] == "governance/synthetic-sensitive-fixtures.manifest.yaml"
        ]
        if len(entries) != 1 or sha256_file(synthetic_manifest_path_value) != entries[0]["sha256"]:
            raise GateViolation("AUTH-SYNTHETIC-UNLOCKED", "synthetic manifest is not locked")
    refs = admit_task(
        manifest_path=task_manifest_path,
        authority_lock_path=authority_lock_path,
        model_policy_path=model_policy_path,
        protected_paths_path=protected_paths_path,
        repository_roots=repository_roots,
    )
    task_id = str(manifest["task_id"])
    heads = [
        {
            "repository": ref["repository"],
            "head": git_output(repository_roots[ref["repository"]], "rev-parse", "HEAD"),
        }
        for ref in refs
    ]
    task_receipt = make_typed_receipt(
        "task_admission",
        authority_lock_hash=authority_hash,
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        context_hash=canonical_hash(manifest),
        repository_heads_hash=canonical_hash(heads),
        authorization_id=(
            "authority-locked-grant" if manifest["task_type"] == "authority_change" else None
        ),
    )
    scopes = check_change_scopes(
        manifest_path=task_manifest_path,
        protected_paths_path=protected_paths_path,
        repository_roots=repository_roots,
        authority_lock_hash=authority_hash,
    )
    evidence: list[dict[str, Any]] = [task_receipt, *scopes]
    environments = validation_environment or {}
    for ref in refs:
        repository = ref["repository"]
        root = repository_roots[repository]
        predecessor = ref["predecessor_commit"]
        scope = next(item for item in scopes if item["repository"] == repository)
        tree_oid = git_output(root, "write-tree")
        if scope["staged_tree_hash"] != tree_oid:
            raise GateViolation("AUTH-AGGREGATE-TREE", "scope tree changed during verification")
        candidate = audit_candidate_tree(
            root=root,
            predecessor_commit=predecessor,
            task_id=task_id,
            authority_lock_hash=authority_hash,
            scan_profile=scan_profile,
            synthetic_fixture_manifest=synthetic_manifest,
        )
        references = verify_authority_references(
            root=root,
            predecessor_commit=predecessor,
            task_id=task_id,
            authority_lock_hash=authority_hash,
            authority_lock=lock,
            repository_roots=repository_roots,
            registry_paths=registry_paths,
        )
        reuse = verify_reuse_admission(
            root=root,
            predecessor_commit=predecessor,
            task_id=task_id,
            authority_lock=lock,
            repository_roots=repository_roots,
            ledger_path=reuse_ledger_path,
        )
        validation = run_validation_commands(
            root=root,
            predecessor_commit=predecessor,
            repository=repository,
            task_id=task_id,
            authority_lock_hash=authority_hash,
            command_specs=manifest["validation_commands"],
            environment=environments.get(repository),
        )
        checker_id = checker_collector_ids.get(repository)
        if checker_id is None:
            raise GateViolation("AUTH-CHECK-COLLECTOR-MISSING", f"no checker for {repository}")
        independent = verify_independent_check(
            task_manifest=manifest,
            authority_lock=lock,
            repository_roots=repository_roots,
            root=root,
            checker_collector_id=checker_id,
        )
        if independent["checker_command_results_hash"] != validation["command_results_hash"]:
            raise GateViolation(
                "AUTH-AGGREGATE-CHECKER-VALIDATION",
                "checker result set differs from validation result set",
            )
        for receipt, receipt_type in (
            (scope, "change_scope"),
            (candidate, "candidate_tree_audit"),
            (references, "authority_reference"),
            (reuse, "reuse_admission"),
            (validation, "validation_receipt_set"),
            (independent, "independent_check"),
        ):
            _assert_receipt_binding(
                receipt, receipt_type=receipt_type, task_id=task_id, authority_hash=authority_hash
            )
            if receipt.get("staged_tree_hash", receipt.get("candidate_tree_hash")) != tree_oid:
                raise GateViolation(
                    "AUTH-AGGREGATE-TREE", f"{receipt_type} binds another candidate tree"
                )
        evidence.extend([candidate, references, reuse, validation, independent])

    runtime = verify_runtime_predicate(
        task_id=task_id,
        authority_lock=lock,
        repository_roots=repository_roots,
        profile=str(manifest["activation_profile"]),
        predicate_id="runtime_conformance",
        observed_pass=None,
    )
    validate_typed_receipt(runtime, expected_type="runtime_conformance")
    if runtime["decision"] == "deny":
        raise GateViolation("AUTH-AGGREGATE-RUNTIME", "runtime predicate denied")
    evidence.append(runtime)

    if sha256_file(task_manifest_path) != initial_manifest_hash:
        raise GateViolation("AUTH-AGGREGATE-MANIFEST-DRIFT", "task manifest changed during gate")
    final_manifest = load_mapping(task_manifest_path)
    if canonical_hash(final_manifest) != canonical_hash(manifest):
        raise GateViolation("AUTH-AGGREGATE-MANIFEST-DRIFT", "task manifest content changed")
    if _context_snapshot(final_manifest, repository_roots) != initial_context_hash:
        raise GateViolation("AUTH-AGGREGATE-CONTEXT-DRIFT", "task context changed during gate")
    for scope in scopes:
        if (
            git_output(repository_roots[scope["repository"]], "write-tree")
            != scope["staged_tree_hash"]
        ):
            raise GateViolation("AUTH-AGGREGATE-TREE", "candidate index changed during gate")

    repository_trees = [
        {
            "repository": scope["repository"],
            "base_commit": scope["base_commit"],
            "tree_oid": scope["staged_tree_hash"],
        }
        for scope in scopes
    ]
    closure = [str(item["receipt_id"]) for item in evidence]
    aggregate = make_typed_receipt(
        "change_verification",
        authority_lock_hash=authority_hash,
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        base_commits_hash=canonical_hash(
            [
                {"repository": item["repository"], "base_commit": item["base_commit"]}
                for item in repository_trees
            ]
        ),
        repository_tree_oids_hash=canonical_hash(repository_trees),
        receipt_closure_hash=canonical_hash(closure),
    )
    return {
        "receipt": aggregate,
        "evidence_receipts": evidence,
        "authority_evidence_hash": canonical_hash(verified_authority),
        "repository_trees": repository_trees,
    }


def verify_push(
    *,
    root: Path,
    repository: str,
    task_id: str,
    authority_lock_path: Path,
    repository_roots: Mapping[str, Path],
    change_bundle: Mapping[str, Any],
    candidate_commit: str,
    remote_attestation_path: Path,
    remote_policy_path: Path,
    scan_profile: str = "production",
    synthetic_fixture_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Issue push permission only after fresh provider and history verification."""

    lock = load_mapping(authority_lock_path)
    verify_authority_lock_data(lock, repository_roots)
    authority_hash = require_sha256(lock.get("bundle_hash"), where="authority bundle hash")
    change_receipt = change_bundle.get("receipt")
    if not isinstance(change_receipt, dict):
        raise GateViolation("AUTH-PUSH-CHANGE", "change verification bundle is missing")
    _assert_receipt_binding(
        change_receipt,
        receipt_type="change_verification",
        task_id=task_id,
        authority_hash=authority_hash,
    )
    trees = change_bundle.get("repository_trees")
    if not isinstance(trees, list):
        raise GateViolation("AUTH-PUSH-CHANGE", "repository tree evidence is missing")
    matches = [
        item for item in trees if isinstance(item, dict) and item.get("repository") == repository
    ]
    if len(matches) != 1:
        raise GateViolation("AUTH-PUSH-REPOSITORY", "candidate repository tree is ambiguous")
    approved_tree = str(matches[0].get("tree_oid"))
    commit_receipt = verify_commit_tree(
        root=root,
        candidate_commit=candidate_commit,
        approved_tree=approved_tree,
        task_id=task_id,
        authority_lock_hash=authority_hash,
    )
    policy_entries = [
        entry
        for entry in lock["entries"]
        if entry["path"] == "governance/remote-protection-policy.yaml"
    ]
    if len(policy_entries) != 1 or sha256_file(remote_policy_path) != policy_entries[0]["sha256"]:
        raise GateViolation(
            "AUTH-PUSH-POLICY-UNLOCKED",
            "remote policy must be the exact authority-locked Git blob",
        )
    remote = verify_remote_protection(
        attestation_path=remote_attestation_path,
        policy_path=remote_policy_path,
        repository_root=root,
        candidate_commit=candidate_commit,
    )
    _assert_receipt_binding(
        remote, receipt_type="remote_protection", task_id=task_id, authority_hash=authority_hash
    )
    synthetic_manifest_path_value = synthetic_fixture_manifest_path
    synthetic_manifest = (
        load_mapping(synthetic_manifest_path_value)
        if synthetic_manifest_path_value is not None
        else None
    )
    if synthetic_manifest is not None and synthetic_manifest_path_value is not None:
        synthetic_entries = [
            entry
            for entry in lock["entries"]
            if entry["path"] == "governance/synthetic-sensitive-fixtures.manifest.yaml"
        ]
        if (
            len(synthetic_entries) != 1
            or sha256_file(synthetic_manifest_path_value) != synthetic_entries[0]["sha256"]
        ):
            raise GateViolation("AUTH-SYNTHETIC-UNLOCKED", "synthetic manifest is not locked")
    history = audit_history_publication(
        root=root,
        task_id=task_id,
        authority_lock_hash=authority_hash,
        candidate_commit=candidate_commit,
        remote_attestation=remote,
        scan_profile=scan_profile,
        synthetic_fixture_manifest=synthetic_manifest,
    )
    candidate_tree = git_output(root, "rev-parse", f"{candidate_commit}^{{tree}}")
    aggregate = make_typed_receipt(
        "push_verification",
        authority_lock_hash=authority_hash,
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        candidate_commit=candidate_commit,
        candidate_tree_hash=candidate_tree,
        expected_remote_oid=remote["expected_remote_oid"],
        target_ref=remote["target_ref"],
        receipt_closure_hash=canonical_hash(
            [
                change_receipt["receipt_id"],
                commit_receipt["receipt_id"],
                remote["receipt_id"],
                history["receipt_id"],
            ]
        ),
    )
    return {
        "receipt": aggregate,
        "evidence_receipts": [change_receipt, commit_receipt, remote, history],
    }
