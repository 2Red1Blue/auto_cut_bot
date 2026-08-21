# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
"""Fail-closed candidate, commit, history and conformance gates.

These gates inspect Git objects or closed manifests.  They intentionally do
not import runtime packages: Phase -1 remains able to judge later phases.
"""

from __future__ import annotations

import ast
import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .common import (
    canonical_hash,
    git_bytes,
    git_index_bytes,
    git_index_paths,
    git_output,
    load_mapping_bytes,
    require_closed,
    require_commit,
    require_list,
    require_non_empty_string,
    require_sha256,
    sha256_bytes,
)
from .errors import GateViolation
from .receipts import make_typed_receipt, validate_typed_receipt

ZERO_HASH = "sha256:" + "0" * 64
CONFLICT_MARKERS = re.compile(rb"(?m)^(?:<<<<<<< |=======\s*$|>>>>>>> )")
SENSITIVE_PATH_PATTERNS = (
    re.compile(r"(?:^|/)\.env(?:\.|$)", re.IGNORECASE),
    re.compile(r"(?:^|/)(?:id_rsa|id_ed25519)(?:\.|$)", re.IGNORECASE),
    re.compile(r"(?:^|/)(?:\.sessions?|runtime[_-]?artifacts?)(?:/|$)", re.IGNORECASE),
    re.compile(
        r"(?:^|/)(?:runtime|artifacts?)/(?:sessions?|websocket[_-]?logs?)(?:/|$)", re.IGNORECASE
    ),
    re.compile(r"(?:^|/)logs?/(?:sessions?|websocket)(?:/|$)", re.IGNORECASE),
)
SECRET_CONTENT_PATTERNS = (
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{24,}\b")),
    ("openai_key", re.compile(rb"\bsk-[A-Za-z0-9_-]{24,}\b")),
    (
        "assigned_secret",
        re.compile(
            rb"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\b"
            rb"\s*[:=]\s*([\"'])[A-Za-z0-9_./+\-=]{20,}\1"
        ),
    ),
    (
        "authorization_bearer",
        re.compile(rb"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    ),
    ("cookie_header", re.compile(rb"(?i)(?:^|\n)cookie\s*:\s*[^\n]{16,}")),
)
AUTHORITY_REFERENCE_PATTERN = re.compile(rb"\b(?:SA|G|KC|SD|SS)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
LEGACY_ROOTS = {"autocut_core", "ac_auto_cut", "artifact_bus"}
PHASE_ORDER = {
    "phase_minus_1": -1,
    "phase_0": 0,
    "phase_1": 1,
    "phase_2": 2,
    "phase_3": 3,
    "phase_4": 4,
    "phase_5": 5,
    "phase_6": 6,
}


def _locked_mapping(
    *,
    authority_lock: Mapping[str, Any],
    repository_roots: Mapping[str, Path],
    path: str,
) -> tuple[dict[str, Any], str]:
    entries = [entry for entry in authority_lock["entries"] if entry["path"] == path]
    if len(entries) != 1:
        raise GateViolation("AUTH-CONFORMANCE-LOCKED-SOURCE", f"locked source missing: {path}")
    entry = entries[0]
    repository = str(entry["repository"])
    if repository not in repository_roots:
        raise GateViolation("AUTH-CONFORMANCE-REPOSITORY", f"unbound repository: {repository}")
    raw = git_bytes(
        repository_roots[repository],
        authority_lock["repositories"][repository]["source_commit"],
        path,
    )
    actual = sha256_bytes(raw)
    if actual != entry["sha256"]:
        raise GateViolation("AUTH-CONFORMANCE-SOURCE-HASH", f"locked source mismatch: {path}")
    return load_mapping_bytes(raw, where=path, suffix=Path(path).suffix), actual


def _tree_hash(root: Path) -> str:
    return git_output(root, "write-tree")


def _candidate_index_entries(root: Path) -> list[tuple[str, str, str]]:
    """Return every candidate-tree entry as (mode, oid, canonical path)."""

    try:
        result = subprocess.run(
            ["git", "ls-files", "--stage", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GateViolation("AUTH-CANDIDATE-INDEX", "cannot enumerate candidate index") from exc
    entries: list[tuple[str, str, str]] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise GateViolation("AUTH-CANDIDATE-INDEX", "non-canonical index entry") from exc
        if stage != "0":
            raise GateViolation("AUTH-CANDIDATE-UNMERGED", f"unmerged index entry: {path}")
        entries.append((mode, require_commit(oid, where=f"index oid {path}"), path))
    return entries


def _sensitive_path(path: str) -> bool:
    return any(pattern.search(path) for pattern in SENSITIVE_PATH_PATTERNS)


def _conflict_findings(path: str, raw: bytes) -> list[str]:
    if b"\0" in raw:
        return []
    return [f"conflict_marker:{path}"] if CONFLICT_MARKERS.search(raw) else []


def secret_content_findings(path: str, raw: bytes) -> list[str]:
    if b"\0" in raw:
        return []
    findings: list[str] = []
    for finding_id, pattern in SECRET_CONTENT_PATTERNS:
        if pattern.search(raw):
            findings.append(f"sensitive_content:{finding_id}:{path}")
    return findings


def validate_synthetic_fixture_manifest(
    manifest: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    require_closed(
        manifest,
        required=("schema_version", "allowed_root", "marker", "fixtures"),
        where="synthetic sensitive fixture manifest",
    )
    if manifest["schema_version"] != "1.0.0":
        raise GateViolation("AUTH-SYNTHETIC-VERSION", "unsupported synthetic fixture manifest")
    allowed_root = require_non_empty_string(manifest["allowed_root"], where="allowed_root").rstrip(
        "/"
    )
    if allowed_root != "tests/authority/fixtures/synthetic-sensitive":
        raise GateViolation("AUTH-SYNTHETIC-ROOT", "synthetic fixture root is not exact")
    marker = require_non_empty_string(manifest["marker"], where="marker")
    entries: dict[str, dict[str, str]] = {}
    for index, item in enumerate(require_list(manifest["fixtures"], where="fixtures")):
        if not isinstance(item, dict):
            raise GateViolation("AUTH-SYNTHETIC-ENTRY", f"fixture {index} must be object")
        require_closed(
            item,
            required=("fixture_id", "path", "sha256", "profile"),
            where=f"synthetic fixture {index}",
        )
        path = require_non_empty_string(item["path"], where="fixture path")
        if not path.startswith(f"{allowed_root}/") or path in entries:
            raise GateViolation("AUTH-SYNTHETIC-PATH", "fixture path is outside exact root")
        if item["profile"] != "test_fixture":
            raise GateViolation("AUTH-SYNTHETIC-PROFILE", "fixture profile must be test_fixture")
        entries[path] = {
            "sha256": require_sha256(item["sha256"], where="fixture sha256"),
            "marker": marker,
        }
    return entries


def _synthetic_secret_allowed(
    *,
    path: str,
    raw: bytes,
    scan_profile: str,
    synthetic_fixture_manifest: Mapping[str, Any] | None,
) -> bool:
    if scan_profile == "production":
        return False
    if scan_profile != "test_fixture" or synthetic_fixture_manifest is None:
        raise GateViolation("AUTH-SYNTHETIC-PROFILE", "unknown or unbound scan profile")
    entries = validate_synthetic_fixture_manifest(synthetic_fixture_manifest)
    entry = entries.get(path)
    return bool(
        entry and entry["sha256"] == sha256_bytes(raw) and entry["marker"].encode("utf-8") in raw
    )


def audit_candidate_tree(
    *,
    root: Path,
    predecessor_commit: str,
    task_id: str,
    authority_lock_hash: str,
    scan_profile: str = "production",
    synthetic_fixture_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    changed_paths = git_index_paths(root, predecessor_commit)
    if git_output(root, "ls-files", "-u"):
        raise GateViolation("AUTH-CANDIDATE-UNMERGED", "candidate index contains unmerged entries")
    entries = _candidate_index_entries(root)
    paths = [path for _mode, _oid, path in entries]
    aliases: Counter[str] = Counter(unicodedata.normalize("NFC", path).casefold() for path in paths)
    findings: list[str] = []
    for alias, count in aliases.items():
        if count > 1:
            findings.append(f"path_alias:{alias}")
    changed_set = set(changed_paths)
    for mode, _oid, path in entries:
        if mode in {"120000", "160000"}:
            findings.append(f"non_regular_entry:{path}")
            continue
        if _sensitive_path(path):
            findings.append(f"sensitive_path:{path}")
        raw = git_index_bytes(root, path)
        findings.extend(_conflict_findings(path, raw))
        if path in changed_set:
            secret_findings = secret_content_findings(path, raw)
            if secret_findings and not _synthetic_secret_allowed(
                path=path,
                raw=raw,
                scan_profile=scan_profile,
                synthetic_fixture_manifest=synthetic_fixture_manifest,
            ):
                findings.extend(secret_findings)
    if findings:
        raise GateViolation("AUTH-CANDIDATE-UNSAFE", ",".join(sorted(findings)))
    tree = _tree_hash(root)
    return make_typed_receipt(
        "candidate_tree_audit",
        authority_lock_hash=authority_lock_hash,
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        staged_tree_hash=tree,
        index_tree_hash=tree,
        path_set_hash=canonical_hash(paths),
        findings=[],
    )


def verify_commit_tree(
    *, root: Path, candidate_commit: str, approved_tree: str, task_id: str, authority_lock_hash: str
) -> dict[str, Any]:
    require_commit(candidate_commit, where="candidate_commit")
    require_commit(approved_tree, where="approved_tree")
    actual = git_output(root, "rev-parse", f"{candidate_commit}^{{tree}}")
    if actual != approved_tree:
        raise GateViolation("AUTH-COMMIT-TREE-MISMATCH", "commit tree differs from approved index")
    return make_typed_receipt(
        "commit_tree",
        authority_lock_hash=authority_lock_hash,
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        candidate_commit=candidate_commit,
        committed_tree_hash=actual,
        approved_staged_tree_hash=approved_tree,
    )


def audit_history_publication(
    *,
    root: Path,
    task_id: str,
    authority_lock_hash: str,
    candidate_commit: str,
    remote_attestation: Mapping[str, Any],
    scan_profile: str = "production",
    synthetic_fixture_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Audit every commit/blob that would become newly externally visible."""

    validate_typed_receipt(remote_attestation, expected_type="remote_protection")
    require_commit(candidate_commit, where="candidate_commit")
    if remote_attestation["candidate_commit"] != candidate_commit:
        raise GateViolation("AUTH-HISTORY-CANDIDATE", "remote attestation binds another candidate")
    expected_remote = str(remote_attestation["expected_remote_oid"])
    if git_output(root, "merge-base", "--is-ancestor", expected_remote, candidate_commit) != "":
        # merge-base --is-ancestor communicates through exit status; successful
        # output is deliberately empty.
        raise GateViolation("AUTH-HISTORY-ANCESTRY", "unexpected ancestry output")
    commits = git_output(
        root, "rev-list", "--reverse", f"{expected_remote}..{candidate_commit}"
    ).splitlines()
    if not commits:
        raise GateViolation("AUTH-HISTORY-EMPTY", "candidate exposes no new commits")
    blobs: list[str] = []
    findings: list[str] = []
    for commit in commits:
        require_commit(commit, where="history commit")
        changed_paths = sorted(
            set(
                git_output(
                    root,
                    "diff-tree",
                    "--root",
                    "-m",
                    "--no-commit-id",
                    "--diff-filter=AM",
                    "--name-only",
                    "-r",
                    commit,
                    "--",
                ).splitlines()
            )
        )
        for path in changed_paths:
            line = git_output(root, "ls-tree", commit, "--", path)
            try:
                metadata, observed_path = line.split("\t", 1)
                mode, kind, oid = metadata.split()
            except ValueError as exc:
                raise GateViolation("AUTH-HISTORY-TREE", "unexpected ls-tree record") from exc
            if observed_path != path or kind != "blob" or not mode.startswith("100"):
                findings.append(f"non_regular_entry:{commit}:{path}")
                continue
            blobs.append(f"{commit}:{path}:{oid}")
            if _sensitive_path(path):
                findings.append(f"forbidden_path:{commit}:{path}")
            raw = git_bytes(root, commit, path)
            findings.extend(f"{finding}:{commit}" for finding in _conflict_findings(path, raw))
            secret_findings = secret_content_findings(path, raw)
            if secret_findings and not _synthetic_secret_allowed(
                path=path,
                raw=raw,
                scan_profile=scan_profile,
                synthetic_fixture_manifest=synthetic_fixture_manifest,
            ):
                findings.extend(f"{finding}:{commit}" for finding in secret_findings)
    if findings:
        raise GateViolation("AUTH-HISTORY-UNSAFE", ",".join(sorted(findings)))
    candidate_tree = git_output(root, "rev-parse", f"{candidate_commit}^{{tree}}")
    return make_typed_receipt(
        "history_publication_audit",
        authority_lock_hash=authority_lock_hash,
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        remote_canonical_url=remote_attestation["remote_canonical_url"],
        target_ref=remote_attestation["target_ref"],
        expected_remote_oid=expected_remote,
        candidate_commit=candidate_commit,
        candidate_tree_hash=candidate_tree,
        range_algorithm="git-rev-list-expected-remote-exclusive-v1",
        commit_set_hash=canonical_hash(commits),
        blob_set_hash=canonical_hash(sorted(blobs)),
        findings=[],
        policy_hash=remote_attestation["policy_hash"],
        remote_protection_attestation_hash=remote_attestation["receipt_id"],
        fetched_at=remote_attestation["fetched_at"],
        expires_at=remote_attestation["expires_at"],
    )


def verify_validation_receipt_set(
    *,
    task_id: str,
    authority_lock_hash: str,
    staged_tree_hash: str,
    environment: Mapping[str, Any],
    command_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Reject caller-supplied command results.

    Retained as an explicit fail-closed compatibility boundary so older callers
    cannot accidentally turn JSON testimony into a validation allow receipt.
    Use :func:`run_validation_commands` instead.
    """

    del task_id, authority_lock_hash, staged_tree_hash, environment, command_results
    raise GateViolation(
        "AUTH-VALIDATION-SELF-REPORT",
        "validation results must be collected by the authority command runner",
    )


def run_validation_commands(
    *,
    root: Path,
    predecessor_commit: str,
    repository: str,
    task_id: str,
    authority_lock_hash: str,
    command_specs: Sequence[Mapping[str, Any]],
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Execute declared validations against an immutable checkout of the index tree."""

    require_commit(predecessor_commit, where="predecessor_commit")
    staged_tree_hash = _tree_hash(root)
    safe_environment = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        **dict(environment or {}),
    }
    for key in safe_environment:
        if not key:
            raise GateViolation("AUTH-VALIDATION-ENV", "runner environment must be strings")
        if any(token in key.casefold() for token in ("token", "secret", "password", "key")):
            raise GateViolation("AUTH-VALIDATION-ENV-SECRET", f"secret-like env denied: {key}")
    try:
        commit = subprocess.run(
            [
                "git",
                "commit-tree",
                staged_tree_hash,
                "-p",
                predecessor_commit,
                "-m",
                "gate snapshot",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "Authority Gate",
                "GIT_AUTHOR_EMAIL": "authority@example.invalid",
                "GIT_COMMITTER_NAME": "Authority Gate",
                "GIT_COMMITTER_EMAIL": "authority@example.invalid",
            },
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GateViolation("AUTH-VALIDATION-SNAPSHOT", "cannot materialize index tree") from exc
    require_commit(commit, where="validation snapshot commit")
    normalized: list[dict[str, Any]] = []
    temp_root = Path(tempfile.mkdtemp(prefix="authority-validation-"))
    checkout = temp_root / "checkout"
    try:
        try:
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(checkout), commit],
                cwd=root,
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GateViolation(
                "AUTH-VALIDATION-CHECKOUT", "cannot create immutable validation checkout"
            ) from exc
        for index, spec in enumerate(command_specs):
            require_closed(
                spec,
                required=("command_id", "repository", "argv"),
                where=f"validation command {index}",
            )
            if spec["repository"] != repository:
                continue
            command_id = require_non_empty_string(spec["command_id"], where="command_id")
            argv = require_list(spec["argv"], where=f"{command_id}.argv", non_empty=True)
            if not all(isinstance(arg, str) and arg for arg in argv):
                raise GateViolation("AUTH-VALIDATION-ARGV", "argv must contain strings")
            try:
                result = subprocess.run(
                    cast(list[str], argv),
                    cwd=checkout,
                    capture_output=True,
                    env=safe_environment,
                    timeout=900,
                )
                exit_code = result.returncode
                stdout_hash = sha256_bytes(result.stdout)
                stderr_hash = sha256_bytes(result.stderr)
            except (OSError, subprocess.TimeoutExpired) as exc:
                exit_code = 124 if isinstance(exc, subprocess.TimeoutExpired) else 127
                stdout_hash = ZERO_HASH
                stderr_hash = sha256_bytes(str(exc).encode())
            item = {
                "command_id": command_id,
                "argv_hash": canonical_hash(argv),
                "exit_code": exit_code,
                "candidate_tree_hash": staged_tree_hash,
                "stdout_hash": stdout_hash,
                "stderr_hash": stderr_hash,
            }
            normalized.append(item)
    finally:
        if checkout.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(checkout)],
                cwd=root,
                check=False,
                capture_output=True,
            )
        shutil.rmtree(temp_root, ignore_errors=True)
    if not normalized:
        raise GateViolation("AUTH-VALIDATION-NO-COMMAND", f"no command for {repository}")
    failed: list[str] = []
    failed.extend(item["command_id"] for item in normalized if item["exit_code"] != 0)
    if failed:
        raise GateViolation("AUTH-VALIDATION-FAILED", f"failed validations: {sorted(failed)}")
    attestation = {
        "runner": "authority-command-runner-v1",
        "repository": repository,
        "base_commit": predecessor_commit,
        "staged_tree_hash": staged_tree_hash,
        "environment": safe_environment,
        "results": normalized,
    }
    return make_typed_receipt(
        "validation_receipt_set",
        authority_lock_hash=authority_lock_hash,
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        staged_tree_hash=staged_tree_hash,
        environment_hash=canonical_hash(safe_environment),
        command_results_hash=canonical_hash(normalized),
        runner_attestation_hash=canonical_hash(attestation),
        failed_command_ids=[],
    )


def verify_runtime_predicate(
    *,
    task_id: str,
    authority_lock: Mapping[str, Any],
    repository_roots: Mapping[str, Path],
    profile: str,
    predicate_id: str,
    observed_pass: bool | None,
    profile_policy_path: str = "governance/activation-profiles.yaml",
) -> dict[str, Any]:
    profile_policy, locked_policy_hash = _locked_mapping(
        authority_lock=authority_lock,
        repository_roots=repository_roots,
        path=profile_policy_path,
    )
    authority_lock_hash = require_sha256(
        authority_lock.get("bundle_hash"), where="authority_lock.bundle_hash"
    )
    require_closed(profile_policy, required=("schema_version", "profiles"), where="profiles")
    profiles = profile_policy["profiles"]
    if not isinstance(profiles, dict) or profile not in profiles:
        raise GateViolation("AUTH-PROFILE-UNKNOWN", f"unknown activation profile: {profile}")
    selected = profiles[profile]
    if not isinstance(selected, dict):
        raise GateViolation("AUTH-PROFILE-INVALID", "profile must be an object")
    require_closed(selected, required=("current_phase", "predicates"), where=f"profile {profile}")
    predicates = selected["predicates"]
    if not isinstance(predicates, dict):
        raise GateViolation("AUTH-PROFILE-INVALID", "predicates must be an object")
    predicate = cast(dict[str, Any], predicates).get(predicate_id)
    if not isinstance(predicate, dict):
        raise GateViolation("AUTH-PREDICATE-UNKNOWN", f"unregistered predicate: {predicate_id}")
    require_closed(predicate, required=("minimum_phase",), where=f"predicate {predicate_id}")
    minimum = predicate["minimum_phase"]
    current = selected["current_phase"]
    if minimum not in PHASE_ORDER or current not in PHASE_ORDER:
        raise GateViolation("AUTH-PROFILE-PHASE", "unknown phase")
    policy_hash = canonical_hash(profile_policy)
    if policy_hash != locked_policy_hash:
        # ``locked_policy_hash`` binds canonical source bytes while the policy
        # hash binds parsed semantics.  Both are included in the signature so
        # neither an equivalent reserialization nor caller policy can be used.
        policy_hash = canonical_hash(
            {"semantic_hash": policy_hash, "locked_blob_hash": locked_policy_hash}
        )
    if PHASE_ORDER[current] < PHASE_ORDER[minimum]:
        status, decision, reason = "not_applicable", "not_applicable", "predicate_not_activated"
    else:
        if observed_pass is None:
            raise GateViolation(
                "AUTH-PREDICATE-EVIDENCE-MISSING", "active predicate needs evidence"
            )
        status = "pass" if observed_pass else "fail"
        decision = "allow" if observed_pass else "deny"
        reason = "predicate_passed" if observed_pass else "predicate_failed"
    signature_body = {
        "task_id": task_id,
        "predicate_id": predicate_id,
        "profile": profile,
        "minimum_phase": minimum,
        "current_phase": current,
        "status": status,
        "reason": reason,
        "profile_policy_hash": policy_hash,
    }
    return make_typed_receipt(
        "runtime_conformance",
        authority_lock_hash=authority_lock_hash,
        decision=decision,
        reason_codes=[] if decision != "deny" else ["AUTH-PREDICATE-FAILED"],
        fields={
            **signature_body,
            "profile_signature": canonical_hash(
                {**signature_body, "authority_lock_hash": authority_lock_hash}
            ),
        },
    )


def verify_authority_references(
    *,
    root: Path,
    predecessor_commit: str,
    task_id: str,
    authority_lock_hash: str,
    authority_lock: Mapping[str, Any],
    repository_roots: Mapping[str, Path],
    registry_paths: Sequence[str],
) -> dict[str, Any]:
    staged_tree_hash = _tree_hash(root)
    references: set[str] = set()
    scanned_blobs: list[dict[str, str]] = []
    for path in git_index_paths(root, predecessor_commit):
        if Path(path).suffix.casefold() not in {".py", ".md", ".json", ".yaml", ".yml"}:
            continue
        raw = git_index_bytes(root, path)
        scanned_blobs.append({"path": path, "sha256": sha256_bytes(raw)})
        references.update(
            match.decode("ascii") for match in AUTHORITY_REFERENCE_PATTERN.findall(raw)
        )
    registry_ids: list[str] = []
    registry_hashes: list[str] = []
    for path in registry_paths:
        registry, source_hash = _locked_mapping(
            authority_lock=authority_lock, repository_roots=repository_roots, path=path
        )
        require_closed(registry, required=("schema_version", "ids"), where=f"registry {path}")
        registry_ids.extend(
            require_non_empty_string(item, where=f"registry {path} id")
            for item in require_list(registry["ids"], where=f"registry {path}.ids")
        )
        registry_hashes.append(source_hash)
    unresolved = sorted(references - set(registry_ids))
    if unresolved:
        raise GateViolation("AUTH-REFERENCE-UNRESOLVED", f"unresolved references: {unresolved}")
    return make_typed_receipt(
        "authority_reference",
        authority_lock_hash=authority_lock_hash,
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        staged_tree_hash=staged_tree_hash,
        references_hash=canonical_hash(
            {
                "references": sorted(references),
                "scanned_blobs": scanned_blobs,
                "registries": sorted(registry_hashes),
            }
        ),
        unresolved_ids=[],
    )


def verify_reuse_admission(
    *,
    root: Path,
    predecessor_commit: str,
    task_id: str,
    authority_lock: Mapping[str, Any],
    repository_roots: Mapping[str, Path],
    ledger_path: str,
) -> dict[str, Any]:
    ledger, ledger_hash = _locked_mapping(
        authority_lock=authority_lock,
        repository_roots=repository_roots,
        path=ledger_path,
    )
    require_closed(ledger, required=("schema_version", "entries"), where="reuse ledger")
    approved: dict[str, str] = {}
    for index, item in enumerate(require_list(ledger["entries"], where="reuse entries")):
        if not isinstance(item, dict):
            raise GateViolation("AUTH-REUSE-LEDGER", f"entry {index} invalid")
        require_closed(item, required=("module", "disposition"), where=f"reuse entry {index}")
        module = require_non_empty_string(item["module"], where="reuse module")
        disposition = item["disposition"]
        if disposition not in {
            "banned",
            "fixture_only",
            "algorithm_candidate",
            "approved_adapter",
            "migrated",
        }:
            raise GateViolation("AUTH-REUSE-DISPOSITION", f"unknown disposition: {disposition}")
        approved[module] = disposition
    observed: list[str] = []
    violations: list[str] = []
    for path in git_index_paths(root, predecessor_commit):
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(git_index_bytes(root, path), filename=path)
        except (SyntaxError, ValueError) as exc:
            raise GateViolation("AUTH-REUSE-AST", f"cannot parse {path}: {exc}") from exc
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        for module in sorted(modules):
            root_name = module.split(".", 1)[0]
            if root_name not in LEGACY_ROOTS:
                continue
            observed.append(f"{path}:{module}")
            disposition = approved.get(module) or approved.get(root_name)
            if path.startswith("packages/autocut-kernel/") or disposition not in {
                "approved_adapter",
                "migrated",
            }:
                violations.append(f"{path}:{module}")
    if violations:
        raise GateViolation("AUTH-REUSE-DENY", f"legacy imports denied: {sorted(violations)}")
    return make_typed_receipt(
        "reuse_admission",
        authority_lock_hash=str(authority_lock["bundle_hash"]),
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        staged_tree_hash=_tree_hash(root),
        ledger_hash=ledger_hash,
        observed_imports_hash=canonical_hash(sorted(observed)),
        violations=[],
    )


def verify_independent_check(
    *,
    task_manifest: Mapping[str, Any],
    authority_lock: Mapping[str, Any],
    repository_roots: Mapping[str, Path],
    root: Path,
    checker_collector_id: str,
) -> dict[str, Any]:
    collector = _CHECKER_RUN_COLLECTORS.get(checker_collector_id)
    if collector is None:
        raise GateViolation(
            "AUTH-CHECK-COLLECTOR-UNAVAILABLE",
            "independent check requires an approved live checker-run collector",
        )
    observation = collector(root, task_manifest)
    manifest = observation.normalized
    required = (
        "task_id",
        "implementer_run_identity",
        "checker_run_identity",
        "implementation_context_hash",
        "check_context_hash",
        "candidate_tree_hash",
        "checker_input_manifest_hash",
        "protected_oracle_hashes",
        "command_results_hash",
        "conclusion",
    )
    require_closed(manifest, required=required, where="independent check")
    if manifest["implementer_run_identity"] == manifest["checker_run_identity"]:
        raise GateViolation("AUTH-CHECK-RUN-IDENTITY", "checker must be a different run")
    if manifest["implementation_context_hash"] == manifest["check_context_hash"]:
        raise GateViolation("AUTH-CHECK-CONTEXT", "checker context must be independent")
    if manifest["task_id"] != task_manifest.get("task_id"):
        raise GateViolation("AUTH-CHECK-TASK", "checker attestation binds another task")
    implementer = task_manifest.get("implementer")
    if not isinstance(implementer, dict) or manifest["implementer_run_identity"] != implementer.get(
        "run_identity"
    ):
        raise GateViolation("AUTH-CHECK-IMPLEMENTER", "checker binds another implementer")
    if manifest["conclusion"] != "pass":
        raise GateViolation("AUTH-CHECK-CONCLUSION", "independent checker did not pass")
    for field in (
        "implementation_context_hash",
        "check_context_hash",
        "checker_input_manifest_hash",
        "command_results_hash",
    ):
        require_sha256(manifest[field], where=field)
    actual_tree = _tree_hash(root)
    if manifest["candidate_tree_hash"] != actual_tree:
        raise GateViolation("AUTH-CHECK-CANDIDATE", "checker attestation binds another tree")
    for field_name in ("implementation_context", "check_context"):
        context = task_manifest.get(field_name)
        if not isinstance(context, dict):
            raise GateViolation("AUTH-CHECK-CONTEXT", f"missing task {field_name}")
        repository = str(context.get("repository"))
        relative = str(context.get("path"))
        if repository not in repository_roots:
            raise GateViolation("AUTH-CHECK-CONTEXT", "context repository is unbound")
        try:
            context_path = (repository_roots[repository] / relative).resolve(strict=True)
            context_path.relative_to(repository_roots[repository].resolve(strict=True))
            actual_context_hash = sha256_bytes(context_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise GateViolation("AUTH-CHECK-CONTEXT", "cannot read bound context") from exc
        if manifest[f"{field_name}_hash"] != actual_context_hash:
            raise GateViolation("AUTH-CHECK-CONTEXT", f"{field_name} hash mismatch")
    expected_checker_input_hash = canonical_hash(
        {
            "task_id": task_manifest.get("task_id"),
            "authority_lock_hash": authority_lock.get("bundle_hash"),
            "candidate_tree_hash": actual_tree,
            "check_context_hash": manifest["check_context_hash"],
            "checker_requirements": task_manifest.get("checker_requirements"),
        }
    )
    if manifest["checker_input_manifest_hash"] != expected_checker_input_hash:
        raise GateViolation("AUTH-CHECK-INPUT", "checker input manifest is not reproducible")
    oracles = require_list(
        manifest["protected_oracle_hashes"], where="protected oracles", non_empty=True
    )
    for item in oracles:
        require_sha256(item, where="protected oracle hash")
    locked_oracles: list[str] = []
    for entry in authority_lock["entries"]:
        if entry["class"] != "blocking_fixture":
            continue
        repository = str(entry["repository"])
        if repository not in repository_roots:
            raise GateViolation("AUTH-CHECK-ORACLE-REPOSITORY", "oracle repository is unbound")
        actual = sha256_bytes(
            git_bytes(
                repository_roots[repository],
                authority_lock["repositories"][repository]["source_commit"],
                entry["path"],
            )
        )
        if actual != entry["sha256"]:
            raise GateViolation("AUTH-CHECK-ORACLE-BLOB", "locked oracle blob mismatch")
        locked_oracles.append(actual)
    if not locked_oracles or sorted(oracles) != sorted(locked_oracles):
        raise GateViolation(
            "AUTH-CHECK-ORACLE-SET", "checker oracle set differs from authority lock"
        )
    return make_typed_receipt(
        "independent_check",
        authority_lock_hash=str(authority_lock["bundle_hash"]),
        decision="allow",
        reason_codes=[],
        task_id=manifest["task_id"],
        implementer_run_identity=manifest["implementer_run_identity"],
        checker_run_identity=manifest["checker_run_identity"],
        implementation_context_hash=manifest["implementation_context_hash"],
        check_context_hash=manifest["check_context_hash"],
        candidate_tree_hash=manifest["candidate_tree_hash"],
        checker_input_manifest_hash=manifest["checker_input_manifest_hash"],
        protected_oracle_hashes_hash=canonical_hash(sorted(oracles)),
        checker_run_attestation_hash=sha256_bytes(observation.raw_evidence),
        checker_command_results_hash=manifest["command_results_hash"],
    )


@dataclass(frozen=True)
class _CheckerRunObservation:
    normalized: dict[str, Any]
    raw_evidence: bytes


CheckerRunCollector = Callable[[Path, Mapping[str, Any]], _CheckerRunObservation]


_CHECKER_RUN_COLLECTORS: dict[str, CheckerRunCollector] = {}


def verify_upstream_parity(
    *,
    manifest: Mapping[str, Any],
    root: Path,
    protected_patterns: Sequence[str],
    task_id: str,
    authority_lock_hash: str,
) -> dict[str, Any]:
    require_closed(
        manifest,
        required=(
            "upstream_base",
            "upstream_head",
            "local_base",
            "local_candidate",
            "mapping_registry_path",
        ),
        where="upstream parity",
    )
    for field in ("upstream_base", "upstream_head", "local_base", "local_candidate"):
        require_commit(manifest[field], where=field)
    upstream_paths = git_output(
        root,
        "diff",
        "--name-only",
        manifest["upstream_base"],
        manifest["upstream_head"],
        "--",
    ).splitlines()
    capability_ids = [f"path:{path}" for path in sorted(upstream_paths)]
    mapping_path = require_non_empty_string(
        manifest["mapping_registry_path"], where="mapping_registry_path"
    )
    registry_raw = git_bytes(root, manifest["local_candidate"], mapping_path)
    registry = load_mapping_bytes(
        registry_raw,
        where=f"{manifest['local_candidate']}:{mapping_path}",
        suffix=Path(mapping_path).suffix,
    )
    require_closed(
        registry,
        required=("schema_version", "upstream_base", "upstream_head", "mappings"),
        where="upstream mapping registry",
    )
    if (
        registry["upstream_base"] != manifest["upstream_base"]
        or registry["upstream_head"] != manifest["upstream_head"]
    ):
        raise GateViolation("AUTH-UPSTREAM-REGISTRY-RANGE", "mapping registry binds another range")
    mappings = require_list(registry["mappings"], where="mappings")
    mapped: list[str] = []
    for index, item in enumerate(mappings):
        if not isinstance(item, dict):
            raise GateViolation("AUTH-UPSTREAM-MAPPING", f"mapping {index} invalid")
        require_closed(
            item, required=("capability_id", "disposition", "evidence"), where=f"mapping {index}"
        )
        mapped.append(require_non_empty_string(item["capability_id"], where="capability_id"))
        if item["disposition"] not in {"preserved", "replaced", "intentional_omission"}:
            raise GateViolation("AUTH-UPSTREAM-DISPOSITION", "unknown upstream disposition")
        evidence = require_list(item["evidence"], where="mapping evidence", non_empty=True)
        if item["disposition"] == "intentional_omission" and len(evidence) < 3:
            raise GateViolation(
                "AUTH-UPSTREAM-OMISSION", "omission needs owner, reason and test evidence"
            )
        for evidence_path in evidence:
            require_non_empty_string(evidence_path, where="mapping evidence path")
            # Evidence must be a blob in the candidate tree, not prose supplied
            # alongside the request.
            git_bytes(root, manifest["local_candidate"], evidence_path)
    counts = Counter(mapped)
    unmapped = sorted(set(capability_ids) - set(mapped))
    duplicates = sorted(item for item, count in counts.items() if count != 1)
    if unmapped or duplicates or set(mapped) - set(capability_ids):
        raise GateViolation("AUTH-UPSTREAM-INCOMPLETE", "capability mapping is not one-to-one")
    changed = git_output(
        root,
        "diff",
        "--name-only",
        manifest["local_base"],
        manifest["local_candidate"],
        "--",
    ).splitlines()
    protected_changes = [
        path
        for path in changed
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in protected_patterns)
    ]
    if protected_changes:
        raise GateViolation(
            "AUTH-UPSTREAM-PROTECTED-DIFF",
            f"protected upstream surface changed: {protected_changes}",
        )
    return make_typed_receipt(
        "upstream_parity",
        authority_lock_hash=authority_lock_hash,
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        upstream_base=manifest["upstream_base"],
        upstream_head=manifest["upstream_head"],
        local_base=manifest["local_base"],
        local_candidate=manifest["local_candidate"],
        capability_inventory_hash=canonical_hash(
            {
                "range": [manifest["upstream_base"], manifest["upstream_head"]],
                "paths": capability_ids,
            }
        ),
        mapping_set_hash=canonical_hash(
            {"registry_blob": sha256_bytes(registry_raw), "mappings": mappings}
        ),
        unmapped_capabilities=[],
        protected_zero_diff=True,
    )


def verify_baseline_failure(
    *,
    manifest: Mapping[str, Any],
    root: Path,
    task_id: str,
    authority_lock_hash: str,
) -> dict[str, Any]:
    required = (
        "baseline_commit",
        "candidate_commit",
        "argv",
        "environment",
        "classification_owner",
    )
    require_closed(manifest, required=required, where="baseline failure")
    for field in ("baseline_commit", "candidate_commit"):
        require_commit(manifest[field], where=field)
    argv = require_list(manifest["argv"], where="argv", non_empty=True)
    if not all(isinstance(item, str) and item for item in argv):
        raise GateViolation("AUTH-BASELINE-ARGV", "argv must contain strings")
    environment = manifest["environment"]
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and key and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise GateViolation("AUTH-BASELINE-ENV", "environment must be string mapping")
    typed_environment = cast(dict[str, str], environment)
    if any(
        any(token in key.casefold() for token in ("token", "secret", "password", "key"))
        for key in typed_environment
    ):
        raise GateViolation("AUTH-BASELINE-ENV-SECRET", "secret-like environment key denied")
    changed = set(
        git_output(
            root,
            "diff",
            "--name-only",
            manifest["baseline_commit"],
            manifest["candidate_commit"],
            "--",
        ).splitlines()
    )
    if changed:
        # Phase -1 has no trusted dependency graph that can prove a changed
        # blob is unrelated to a command.  Treat such a waiver as unprovable.
        raise GateViolation(
            "AUTH-BASELINE-UNPROVEN-SCOPE",
            "baseline waiver cannot prove changed blobs are unrelated",
        )
    baseline_result = _run_command_at_commit(
        root=root,
        commit=str(manifest["baseline_commit"]),
        argv=cast(list[str], argv),
        environment=typed_environment,
    )
    candidate_result = _run_command_at_commit(
        root=root,
        commit=str(manifest["candidate_commit"]),
        argv=cast(list[str], argv),
        environment=typed_environment,
    )
    if baseline_result["exit_code"] == 0 or candidate_result["exit_code"] == 0:
        raise GateViolation("AUTH-BASELINE-NOT-FAILURE", "both exact commits must fail")
    baseline_signature = canonical_hash(baseline_result)
    candidate_signature = canonical_hash(candidate_result)
    if baseline_signature != candidate_signature:
        raise GateViolation("AUTH-BASELINE-SIGNATURE", "independently observed failures differ")
    owner = require_non_empty_string(manifest["classification_owner"], where="classification_owner")
    return make_typed_receipt(
        "baseline_failure",
        authority_lock_hash=authority_lock_hash,
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        baseline_commit=manifest["baseline_commit"],
        candidate_commit=manifest["candidate_commit"],
        command_hash=canonical_hash(argv),
        environment_hash=canonical_hash(environment),
        failure_signature_hash=candidate_signature,
        baseline_signature_hash=baseline_signature,
        related_inputs_hash=canonical_hash([]),
        changed_scope_hash=canonical_hash(sorted(changed)),
        classification_owner=owner,
        classification="verified_preexisting",
    )


def _run_command_at_commit(
    *, root: Path, commit: str, argv: list[str], environment: dict[str, str]
) -> dict[str, Any]:
    checkout_parent = Path(tempfile.mkdtemp(prefix="authority-baseline-"))
    checkout = checkout_parent / "checkout"
    try:
        try:
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(checkout), commit],
                cwd=root,
                check=True,
                capture_output=True,
            )
            result = subprocess.run(
                argv,
                cwd=checkout,
                capture_output=True,
                timeout=900,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PYTHONHASHSEED": "0",
                    **environment,
                },
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            if isinstance(exc, subprocess.TimeoutExpired):
                return {"exit_code": 124, "stdout_hash": ZERO_HASH, "stderr_hash": ZERO_HASH}
            raise GateViolation("AUTH-BASELINE-RUNNER", "cannot execute exact commit") from exc
        return {
            "exit_code": result.returncode,
            "stdout_hash": sha256_bytes(result.stdout),
            "stderr_hash": sha256_bytes(result.stderr),
        }
    finally:
        if checkout.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(checkout)],
                cwd=root,
                check=False,
                capture_output=True,
            )
        shutil.rmtree(checkout_parent, ignore_errors=True)
